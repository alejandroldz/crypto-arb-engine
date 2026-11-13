"""Async entrypoint: ingestion -> detection -> paper trading -> dashboard.

`python -m app.main`                 run the paper-trading engine
`python -m app.main export-tax FILE` export the AEAT tax CSV (live fills only)

Orchestration (paper trading default):
  - funding snapshots are recorded to DuckDB; a carry is only approved on
    pessimistic trailing statistics (min(polled rate, p25 of history)),
    never on a spot-rate linear projection
  - approved opportunities auto-open a paper position (latency simulation
    + post-delay revalidation inside PaperTrader)
  - open positions are monitored every cycle via engines/monitor.py and
    rebuilt from SQLite on restart
  - events also go to engine.log (breaker halts, feed errors, opens/closes)
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from rich.console import Console
from rich.live import Live

from app.connectors.ccxt_manager import (
    BOOKS_TOPIC,
    FUNDING_TOPIC,
    BookFeed,
    FundingPoller,
    fetch_unified_account_health,
)
from app.connectors.normalizer import FundingRate, L2OrderBook
from app.core.circuit_breaker import CircuitBreaker
from app.core.config import get_settings
from app.core.decimals import D
from app.core.events import EventBus
from app.database.duckdb_store import TickStore
from app.database.repository import Repository
from app.engines.arbitrage import FundingOpportunity, detect_funding_carry
from app.engines.monitor import MonitorAction, evaluate_carry
from app.engines.paper_trader import PaperFill, PaperTrader
from app.engines.risk import CarryPositionRisk, StalenessGate, UtaAccountState
from app.engines.validator import approve_unified_carry, uta_allocation
from app.ui.cli_dashboard import print_opportunity, render
from app.ui.notifier import TelegramNotifier

logger = logging.getLogger("main")

MAX_OPPORTUNITIES_KEPT = 200

FUNDING_LOG_CSV = "funding_log.csv"


def seed_funding_history(ticks):
    # Seed DuckDB funding history from the CSV log (survives restarts).
    import csv

    try:
        with open(FUNDING_LOG_CSV, newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
    except FileNotFoundError:
        return 0
    seeded = 0
    max_ts = ticks.funding_max_ts()
    for exchange, symbol, ts_iso, rate, predicted in rows[1:] if rows else []:
        if ts_iso <= max_ts:
            continue  # already in DuckDB
        ticks.insert_funding_raw(exchange, symbol, ts_iso, rate, predicted or None)
        seeded += 1
    return seeded


def append_funding_log(rate):
    import csv
    import os

    new = not os.path.exists(FUNDING_LOG_CSV)
    with open(FUNDING_LOG_CSV, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["exchange", "symbol", "ts", "rate", "predicted_rate"])
        w.writerow([
            rate.exchange, rate.symbol,
            datetime.now(timezone.utc).isoformat(),
            str(rate.rate),
            "" if rate.predicted_rate is None else str(rate.predicted_rate),
        ])



@dataclass
class OpenCarry:
    fill: PaperFill
    opp: FundingOpportunity
    spot_key: str
    perp_key: str
    funding_key: str
    risk: CarryPositionRisk
    funding_collected: Decimal
    last_payment_ts: datetime | None
    accrual_uncertain: bool = False
    uta: "UtaAccountState | None" = None
    mmr_warned: bool = False


async def run() -> None:
    logging.basicConfig(
        filename="engine.log",
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    settings = get_settings()
    console = Console()
    bus = EventBus()
    repo = Repository(settings.sqlite_url)
    await repo.init()
    ticks = TickStore(settings.duckdb_path)
    seeded = seed_funding_history(ticks)
    if seeded:
        logger.info("seeded %d funding samples from %s", seeded, FUNDING_LOG_CSV)
    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    breaker = CircuitBreaker(
        max_consecutive_failures=settings.cb_max_consecutive_failures,
        max_daily_loss_usd=settings.max_daily_loss_usd,
        max_orders_per_window=settings.cb_max_orders_per_window,
        window_seconds=settings.cb_order_window_seconds,
    )
    gate = StalenessGate(settings.max_data_age_ms_funding)
    paper = PaperTrader(
        repo,
        latency_ms=settings.dry_run_latency_ms,
        spot_taker_fee_bps=settings.taker_fee_for(settings.spot_exchange_list[0]),
        perp_taker_fee_bps=settings.default_taker_fee_bps,
        slippage_buffer_bps=settings.slippage_buffer_bps,
        liquidity_haircut=settings.liquidity_haircut,
    )

    stop = asyncio.Event()
    spot_symbols = settings.spot_symbol_list
    perp_symbols = settings.perp_symbol_list
    spot_ex = settings.spot_exchange_list[0]
    tasks = []
    for i, ex in enumerate(settings.spot_exchange_list):
        sym = spot_symbols[i] if i < len(spot_symbols) else spot_symbols[0]
        tasks.append(asyncio.create_task(BookFeed(ex, [sym]).run(bus, stop)))
    for i, ex in enumerate(settings.perp_exchange_list):
        sym = perp_symbols[i] if i < len(perp_symbols) else perp_symbols[0]
        tasks.append(asyncio.create_task(BookFeed(ex, [sym]).run(bus, stop)))
        tasks.append(asyncio.create_task(FundingPoller(ex, [sym]).run(bus, stop)))

    books_q = bus.subscribe(BOOKS_TOPIC)
    funding_q = bus.subscribe(FUNDING_TOPIC)
    books: dict[str, L2OrderBook] = {}
    funding: dict[str, FundingRate] = {}
    opportunities: deque[FundingOpportunity] = deque(maxlen=MAX_OPPORTUNITIES_KEPT)
    open_carries: dict[str, OpenCarry] = {}
    realized_pnl = Decimal("0")
    unified = settings.account_margin_mode.strip().upper() == "UNIFIED"
    base_asset = spot_symbols[0].split("/")[0]
    coll_ratio = settings.collateral_ratio_for(base_asset)
    uta_state = (
        UtaAccountState(
            equity=D(settings.paper_start_balance_usd),
            utilization=D(settings.uta_capital_utilization_pct),
            collateral_ratio=coll_ratio,
            sim_mmr_rate=D(settings.sim_maintenance_margin_rate),
        )
        if unified
        else None
    )
    # UNIFIED: one notional per leg from total equity; SPLIT: fixed quote size
    notional = (
        uta_allocation(D(settings.paper_start_balance_usd), D(settings.uta_capital_utilization_pct))[0]
        if unified
        else settings.target_quote_size_list[0]
    )
    max_safe_pump = (
        uta_state.max_safe_pump_pct(D(settings.max_account_mmr_emergency_unwind))
        if uta_state is not None
        else None
    )
    last_mmr: Decimal | None = None
    last_health_check = 0.0
    spot_key_default = f"{spot_ex}:{spot_symbols[0]}"

    # --- restart recovery: rebuild open carries from SQLite ---
    for pos in await repo.open_positions():
        perp_key = f"{pos.exchange_perp}:{pos.symbol}"
        spot_key = f"{pos.exchange_spot}:{pos.spot_symbol or spot_symbols[0]}"
        open_carries[f"{spot_key}|{perp_key}"] = OpenCarry(
            fill=PaperFill(
                position_id=pos.id,
                qty_base=D(pos.qty_base),
                spot_vwap=D(pos.spot_entry_vwap),
                perp_vwap=D(pos.perp_entry_vwap),
            ),
            opp=FundingOpportunity(
                exchange=pos.exchange_perp,
                symbol=pos.symbol,
                notional=D(pos.notional),
                funding_rate=D(pos.entry_funding_rate),
                holding_intervals=pos.holding_intervals,
                breakeven_intervals=0,
                projected_net=D("0"),
            ),
            spot_key=spot_key,
            perp_key=perp_key,
            funding_key=perp_key,
            risk=CarryPositionRisk.build(D(pos.perp_entry_vwap), D("1")),
            funding_collected=D(pos.funding_collected),
            last_payment_ts=None,  # unknown: first payment after restart is flagged
            accrual_uncertain=True,
            uta=uta_state,
        )
        logger.info("recovered open position %s (%s)", pos.id, perp_key)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async def close_carry(key: str, carry: OpenCarry, reason: str) -> None:
        nonlocal realized_pnl

        def fetch() -> tuple[L2OrderBook, L2OrderBook]:
            return books[carry.spot_key], books[carry.perp_key]

        breaker.record_order()
        breaker.record_order()
        try:
            pnl = await paper.close_carry(carry.fill, carry.funding_collected, fetch)
        except (KeyError, AssertionError) as exc:
            breaker.record_failure()
            logger.error("unwind failed %s: %s", key, exc)
            await notifier.send(f"UNWIND FAILED {key}: {exc}")
            return
        realized_pnl += pnl
        breaker.record_pnl_usd(pnl)
        del open_carries[key]
        logger.info("unwound %s (%s): pnl %s USD", key, reason, pnl)
        await notifier.send(f"UNWOUND {key} ({reason}): pnl {pnl} USD")

    with Live(console=console, refresh_per_second=2) as live:
        while not stop.is_set():
            now_ns = time.monotonic_ns()

            while not books_q.empty():
                book = books_q.get_nowait()
                books[f"{book.exchange}:{book.symbol}"] = book
                await asyncio.to_thread(ticks.record_book, book)

            while not funding_q.empty():
                rate = funding_q.get_nowait()
                fkey = f"{rate.exchange}:{rate.symbol}"
                funding[fkey] = rate
                await asyncio.to_thread(ticks.record_funding, rate)
                append_funding_log(rate)
                if breaker.halted or not gate.is_fresh(rate, now_ns):
                    continue
                pkey = f"{spot_key_default}|{fkey}"
                if pkey in open_carries:
                    continue
                history = await asyncio.to_thread(
                    ticks.funding_history, rate.exchange, rate.symbol
                )
                opp = detect_funding_carry(
                    rate,
                    notional=notional,
                    holding_intervals=90,
                    spot_taker_fee_bps=settings.taker_fee_for(spot_ex),
                    perp_taker_fee_bps=settings.taker_fee_for(rate.exchange),
                    slippage_buffer_bps=settings.slippage_buffer_bps,
                    min_net_profit_usd=settings.min_net_profit_usd,
                    historical_rates=history,
                    min_history_samples=settings.min_funding_history,
                )
                if opp is None:
                    continue
                opportunities.append(opp)
                print_opportunity(console, opp)

                perp_key = f"{rate.exchange}:{rate.symbol}"
                if spot_key_default not in books or perp_key not in books:
                    continue
                if not (
                    gate.is_fresh(books[spot_key_default], now_ns)
                    and gate.is_fresh(books[perp_key], now_ns)
                ):
                    continue
                if uta_state is not None:
                    gate = approve_unified_carry(
                        collateral_ratio=coll_ratio,
                        min_collateral_ratio=D(settings.min_collateral_ratio_required),
                        max_safe_pump_pct=max_safe_pump,
                    )
                    if not gate.approved:
                        logger.info("UTA gate rejected %s: %s", rate.symbol, gate.reason)
                        continue
                breaker.record_order()
                breaker.record_order()

                def fetch() -> tuple[L2OrderBook, L2OrderBook]:
                    return books[spot_key_default], books[perp_key]

                fill = await paper.open_carry(
                    opp, fetch, spot_symbol=spot_symbols[0]
                )
                if fill is None:
                    breaker.record_failure()
                    continue
                breaker.record_success()
                open_carries[pkey] = OpenCarry(
                    fill=fill,
                    opp=opp,
                    spot_key=spot_key_default,
                    perp_key=perp_key,
                    funding_key=fkey,
                    risk=CarryPositionRisk.build(fill.perp_vwap, D("1")),
                    funding_collected=Decimal("0"),
                    last_payment_ts=rate.next_funding_ts,
                    uta=uta_state,
                )
                logger.info(
                    "paper open %s %s notional %s rate %s projected %s",
                    opp.exchange, opp.symbol, opp.notional, opp.funding_rate,
                    opp.projected_net,
                )
                await notifier.send(
                    f"PAPER OPEN {opp.exchange} {opp.symbol} notional {opp.notional} "
                    f"rate {opp.funding_rate} projected {opp.projected_net} USD"
                )

            # real account health (read-only keys); simulated UTA is the fallback
            if settings.krakenfutures_api_key and time.monotonic() - last_health_check > 60:
                last_health_check = time.monotonic()
                try:
                    health = await fetch_unified_account_health(
                        "krakenfutures",
                        settings.krakenfutures_api_key,
                        settings.krakenfutures_api_secret,
                    )
                except Exception as exc:
                    health = None
                    logger.warning("account health fetch failed: %s", exc)
                if health is not None:
                    logger.info(
                        "real account health %s: equity %s mmr %s",
                        health.exchange, health.total_equity, health.account_mmr,
                    )
                    if health.account_mmr >= D(settings.max_account_mmr_emergency_unwind):
                        logger.warning("REAL account MMR %s >= emergency", health.account_mmr)
                        await notifier.send(f"REAL MMR EMERGENCY: {health.account_mmr}")
            for key, carry in list(open_carries.items()):
                decision = evaluate_carry(
                    qty_base=carry.fill.qty_base,
                    perp_entry_vwap=carry.fill.perp_vwap,
                    last_payment_ts=carry.last_payment_ts,
                    rate=funding.get(carry.funding_key),
                    perp_book=books.get(carry.perp_key),
                    risk=carry.risk,
                    liquidation_buffer_pct=settings.liquidation_buffer_pct,
                    uta=carry.uta,
                    mmr_emergency=D(settings.max_account_mmr_emergency_unwind),
                )
                if decision.account_mmr is not None:
                    last_mmr = decision.account_mmr
                    if (
                        decision.account_mmr >= D(settings.max_account_mmr_warning)
                        and not carry.mmr_warned
                    ):
                        carry.mmr_warned = True
                        logger.warning("account MMR warning %s: %s", key, decision.account_mmr)
                        await notifier.send(f"MMR WARNING {key}: {decision.account_mmr}")
                if decision.accrual_uncertain and not carry.accrual_uncertain:
                    carry.accrual_uncertain = True
                    logger.warning("funding accrual uncertain for %s (missed payments)", key)
                    await notifier.send(f"ACCRUAL UNCERTAIN {key}: missed funding payments")
                carry.last_payment_ts = decision.new_payment_ts
                if decision.accrual > 0:
                    carry.funding_collected += decision.accrual
                    await repo.add_funding(carry.fill.position_id, decision.accrual)
                if decision.action is MonitorAction.UNWIND:
                    await close_carry(key, carry, decision.reason)

            uta_line = ""
            if uta_state is not None:
                mmr_txt = f"{last_mmr * 100:.1f}%" if last_mmr is not None else "n/a"
                pump_txt = (
                    f"+{max_safe_pump * 100:.0f}%" if max_safe_pump is not None else "inf"
                )
                uta_line = (
                    f"margin: UNIFIED {settings.uta_capital_utilization_pct * 100:.0f}% util | "
                    f"{base_asset} ratio {coll_ratio * 100:.0f}% | "
                    f"MMR {mmr_txt} (warn {settings.max_account_mmr_warning * 100:.0f}%, "
                    f"unwind {settings.max_account_mmr_emergency_unwind * 100:.0f}%) | "
                    f"max safe pump {pump_txt}"
                )
            live.update(
                render(
                    books,
                    funding,
                    list(opportunities),
                    breaker,
                    len(open_carries),
                    realized_pnl,
                    uta_line,
                )
            )
            await asyncio.sleep(0.5)

    stop.set()
    for t in tasks:
        t.cancel()
    ticks.close()
    await repo.close()


def entrypoint() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "export-tax":
        kind = sys.argv[3] if len(sys.argv) >= 4 else "live"

        async def _export() -> int:
            repo = Repository(get_settings().sqlite_url)
            await repo.init()
            rows = await repo.export_tax_csv(sys.argv[2], kind=kind)
            await repo.close()
            return rows

        rows = asyncio.run(_export())
        print(f"exported {rows} {kind} audit rows to {sys.argv[2]}")
        return
    asyncio.run(run())


if __name__ == "__main__":
    entrypoint()
