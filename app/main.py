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
from datetime import datetime, timezone
from decimal import Decimal

from rich.console import Console
from rich.live import Live

from app.connectors.ccxt_manager import (
    BOOKS_TOPIC,
    FUNDING_TOPIC,
    BookFeed,
    FundingPoller,
    fetch_minimal_markets,
    fetch_unified_account_health,
)
from app.connectors.normalizer import FundingRate, L2OrderBook
from app.core.circuit_breaker import CircuitBreaker
from app.core.config import get_settings
from app.core.decimals import D
from app.core.events import EventBus
from app.database.duckdb_store import TickStore
from app.database.repository import Repository
from app.engines.candidates import CarryContext, OpenCarry, handle_funding_candidate
from app.engines.arbitrage import FundingOpportunity
from app.engines.monitor import MonitorAction, evaluate_carry
from app.engines.paper_trader import PaperFill, PaperTrader
from app.engines.risk import CarryPositionRisk, StalenessGate, UtaAccountState
from app.engines import screener as scr
from app.connectors import screener_io
from app.engines.validator import uta_allocation
from app.ui.cli_dashboard import render
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
        # both supported perp venues are 5bps taker (krakenfutures/okx tier base)
        perp_taker_fee_bps=settings.taker_fee_for(settings.perp_exchange_list[0]),
        slippage_buffer_bps=settings.slippage_buffer_bps,
        liquidity_haircut=settings.liquidity_haircut,
    )

    stop = asyncio.Event()
    spot_symbols = settings.spot_symbol_list
    tasks = []
    # One minimal market table per venue, shared across that venue's feeds
    # (full load_markets per instance OOMs a 2GB sandbox).
    venues = list(dict.fromkeys(
        list(settings.spot_exchange_list) + list(settings.perp_exchange_list)
    ))
    venue_markets = {}
    for v in venues:
        try:
            venue_markets[v] = await fetch_minimal_markets(v)
        except Exception as exc:
            logger.warning("minimal markets %s failed: %s", v, exc)
            venue_markets[v] = None
    spot_ex = settings.spot_exchange_list[0]

    # Feeds are tracked per symbol so the screener can subscribe/unsubscribe
    # without restarting the engine. Each feed gets its own stop event.
    feed_tasks: dict[str, tuple[asyncio.Task, asyncio.Event]] = {}

    def start_feed(name: str, factory) -> None:
        ev = asyncio.Event()
        t = asyncio.create_task(factory(ev))
        tasks.append(t)
        feed_tasks[name] = (t, ev)

    def stop_feed(name: str) -> None:
        t, ev = feed_tasks.pop(name)
        ev.set()
        t.cancel()
        if t in tasks:
            tasks.remove(t)

    def start_book_feed(ex: str, sym: str) -> None:
        start_feed(
            f"book:{ex}:{sym}",
            lambda ev: BookFeed(ex, [sym], markets_list=venue_markets.get(ex)).run(bus, ev),
        )

    def start_funding_feed(ex: str, sym: str) -> None:
        start_feed(
            f"fund:{ex}:{sym}",
            lambda ev: FundingPoller(ex, [sym], markets_list=venue_markets.get(ex)).run(bus, ev),
        )

    books_q = bus.subscribe(BOOKS_TOPIC)
    funding_q = bus.subscribe(FUNDING_TOPIC)
    books: dict[str, L2OrderBook] = {}
    funding: dict[str, FundingRate] = {}
    opportunities: deque[FundingOpportunity] = deque(maxlen=MAX_OPPORTUNITIES_KEPT)
    open_carries: dict[str, OpenCarry] = {}
    realized_pnl = Decimal("0")
    unified = settings.account_margin_mode.strip().upper() == "UNIFIED"
    # multi-asset UTA: survival math uses the WORST configured haircut
    spot_bases = [sym.split("/")[0] for sym in spot_symbols]
    coll_ratio = min(settings.collateral_ratio_for(b) for b in spot_bases)
    uta_state = (
        UtaAccountState(
            equity=D(settings.paper_start_balance_usd),
            utilization=D(settings.uta_capital_utilization_pct),
            collateral_ratio=coll_ratio,
            sim_mmr_rate=D(settings.sim_maintenance_margin_rate),
            leverage=D(settings.short_leverage_for("krakenfutures")),
        )
        if unified
        else None
    )
    # UNIFIED: utilization budget split evenly across spot bases;
    # SPLIT: fixed quote size
    notional = (
        uta_allocation(D(settings.paper_start_balance_usd), D(settings.uta_capital_utilization_pct))[0]
        / max(1, len(spot_bases))
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
    last_tick_flush = 0.0
    last_book_record: dict[str, float] = {}

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
            risk=CarryPositionRisk.build(
                        D(pos.perp_entry_vwap), D(settings.short_leverage_for(pos.exchange_perp))
                    ),
            funding_collected=D(pos.funding_collected),
            last_payment_ts=None,  # unknown: first payment after restart is flagged
            accrual_uncertain=True,
            uta=uta_state,
        )
        logger.info("recovered open position %s (%s)", pos.id, perp_key)

    carry_ctx = CarryContext(
        settings=settings,
        breaker=breaker,
        gate=gate,
        paper=paper,
        ticks=ticks,
        console=console,
        notifier=notifier,
        books=books,
        opportunities=opportunities,
        open_carries=open_carries,
        spot_ex=spot_ex,
        notional=notional,
        uta_state=uta_state,
        max_safe_pump=max_safe_pump,
        eligible={
            f"{ex}:{sym}"
            for ex in settings.perp_exchange_list
            for sym in settings.perp_symbols_for(ex)
        },
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    def reconcile_feeds(eligible: set[str]) -> None:
        """Start/stop feeds so exactly the eligible perps (+ their spot legs
        + anything with an open carry) are subscribed."""
        keys = set(eligible)
        for c in open_carries.values():  # never unsubscribe an open carry
            keys.add(c.perp_key)
            keys.add(c.spot_key)
        for base in scr.eligible_bases(eligible):
            sym = settings.spot_symbol_for_base(base) or f"{base}/USD"
            keys.add(f"{spot_ex}:{sym}")
        books_wanted = set()
        funds_wanted = set()
        for key in keys:
            ex, sym = key.split(":", 1)
            books_wanted.add((ex, sym))
            if ex != spot_ex:
                funds_wanted.add((ex, sym))
        wanted = {f"book:{e}:{s_}" for e, s_ in books_wanted} | {
            f"fund:{e}:{s_}" for e, s_ in funds_wanted
        }
        for name in list(feed_tasks):
            if name not in wanted:
                logger.info("screener: stopping feed %s", name)
                stop_feed(name)
        for ex, sym in sorted(books_wanted):
            if f"book:{ex}:{sym}" not in feed_tasks:
                start_book_feed(ex, sym)
        for ex, sym in sorted(funds_wanted):
            if f"fund:{ex}:{sym}" not in feed_tasks:
                start_funding_feed(ex, sym)

    async def screener_loop() -> None:
        """Periodic re-selection of eligible perps (public data, no keys)."""
        hyst = scr.load_state(settings.screener_state_path)
        # configured pairs are protected from the cut (user decision, e.g.
        # SOL is OKX-only on purpose): snapshot them before they can mutate
        seeded_keys = set(carry_ctx.eligible or set())
        for key in seeded_keys:
            hyst.seed(key)
        await asyncio.sleep(60)  # let the feeds warm up first
        while not stop.is_set():
            try:
                spot_bases = await screener_io.fetch_kraken_spot_bases()
                min_vol = float(settings.screener_min_volume_usd)
                raw = await screener_io.fetch_okx_xperp_metrics(
                    min_volume_usd=min_vol, bases=spot_bases)
                raw += await screener_io.fetch_krakenfutures_metrics(
                    min_volume_usd=min_vol, bases=spot_bases)
                new_eligible = scr.run_sweep(
                    [scr.from_raw(r) for r in raw],
                    hyst,
                    min_volume_usd=D(settings.screener_min_volume_usd),
                    min_positive_pct=D(settings.screener_min_positive_pct),
                    cost_bps_for=lambda ex: scr.roundtrip_cost_bps(
                        D(settings.taker_fee_for(spot_ex)),
                        D(settings.taker_fee_for(ex)),
                        D(settings.slippage_buffer_bps),
                    ),
                    spot_bases=spot_bases,
                    whitelist_keys=seeded_keys,
                    blacklist_keys=settings.screener_blacklist_keys,
                )
                if new_eligible and new_eligible != (carry_ctx.eligible or set()):
                    logger.info("screener eligible: %s", sorted(new_eligible))
                    if carry_ctx.eligible is not None:
                        carry_ctx.eligible.clear()
                        carry_ctx.eligible.update(new_eligible)
                    if uta_state is not None:
                        n = max(1, len(scr.eligible_bases(new_eligible)))
                        carry_ctx.notional = uta_state.notional / n
                    reconcile_feeds(new_eligible)
                scr.save_state(settings.screener_state_path, hyst)
            except Exception as exc:
                logger.warning("screener sweep failed: %s", exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.screener_interval_s)
            except asyncio.TimeoutError:
                pass

    reconcile_feeds(carry_ctx.eligible or set())
    if settings.screener_enabled:
        tasks.append(asyncio.create_task(screener_loop()))

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
                # sample: 1 snapshot/s per symbol is enough for VWAP/slippage
                # analysis - every tick floods the store and the sandbox RAM
                bk = f"{book.exchange}:{book.symbol}"
                now_mono = time.monotonic()
                if now_mono - last_book_record.get(bk, 0.0) >= 1.0:
                    last_book_record[bk] = now_mono
                    await asyncio.to_thread(ticks.record_book, book)

            while not funding_q.empty():
                rate = funding_q.get_nowait()
                fkey = f"{rate.exchange}:{rate.symbol}"
                funding[fkey] = rate
                await asyncio.to_thread(ticks.record_funding, rate)
                append_funding_log(rate)
                await handle_funding_candidate(carry_ctx, rate, now_ns)

            if time.monotonic() - last_tick_flush > 30:
                last_tick_flush = time.monotonic()
                await asyncio.to_thread(ticks.flush)

            # real account health (read-only keys); simulated UTA is the fallback
            health_venues = []
            if settings.krakenfutures_api_key:
                health_venues.append(
                    ("krakenfutures", settings.krakenfutures_api_key,
                     settings.krakenfutures_api_secret, "", "")
                )
            if settings.okx_api_key:
                health_venues.append(
                    ("okx", settings.okx_api_key, settings.okx_api_secret,
                     settings.okx_api_passphrase, settings.okx_hostname)
                )
            if health_venues and time.monotonic() - last_health_check > 60:
                last_health_check = time.monotonic()
                for hv, hk, hs, hp, hh in health_venues:
                    try:
                        health = await fetch_unified_account_health(hv, hk, hs, hp, hh)
                    except Exception as exc:
                        health = None
                        logger.warning("account health fetch %s failed: %s", hv, exc)
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
                    f"worst ratio {coll_ratio * 100:.0f}% | "
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
