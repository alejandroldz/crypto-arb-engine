"""Async entrypoint: ingestion -> detection -> paper trading -> dashboard.

`python -m app.main`                 run the paper-trading engine
`python -m app.main export-tax FILE` export the AEAT tax CSV and exit

Orchestration (paper trading default):
  - an approved funding-carry opportunity auto-opens a paper position
    (latency simulation + post-delay revalidation inside PaperTrader)
  - open positions are monitored every cycle: funding accrues on each
    observed funding payment; a predicted funding flip or price within the
    liquidation buffer triggers an automatic paper unwind + alert
"""
from __future__ import annotations

import asyncio
import signal
import sys
import time
from dataclasses import dataclass
from decimal import Decimal

from rich.console import Console
from rich.live import Live

from app.connectors.ccxt_manager import (
    BOOKS_TOPIC,
    FUNDING_TOPIC,
    BookFeed,
    FundingPoller,
)
from app.connectors.normalizer import FundingRate, L2OrderBook
from app.core.circuit_breaker import CircuitBreaker
from app.core.config import get_settings
from app.core.decimals import D, mul
from app.core.events import EventBus
from app.database.repository import Repository
from app.database.duckdb_store import TickStore
from app.engines.arbitrage import FundingOpportunity, detect_funding_carry
from app.engines.paper_trader import PaperFill, PaperTrader
from app.engines.risk import CarryPositionRisk, StalenessGate, funding_flip_should_unwind
from app.ui.cli_dashboard import print_opportunity, render
from app.ui.notifier import TelegramNotifier


@dataclass
class OpenCarry:
    fill: PaperFill
    opp: FundingOpportunity
    spot_key: str
    perp_key: str
    funding_key: str
    risk: CarryPositionRisk
    funding_collected: Decimal
    last_payment_ts: object  # datetime | None


async def run() -> None:
    settings = get_settings()
    console = Console()
    bus = EventBus()
    repo = Repository(settings.sqlite_url)
    await repo.init()
    ticks = TickStore(settings.duckdb_path)
    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    breaker = CircuitBreaker(
        max_consecutive_failures=settings.cb_max_consecutive_failures,
        max_daily_loss_eur=settings.max_daily_loss_eur,
        max_orders_per_window=settings.cb_max_orders_per_window,
        window_seconds=settings.cb_order_window_seconds,
    )
    gate = StalenessGate(settings.max_data_age_ms_funding)
    paper = PaperTrader(
        repo,
        latency_ms=settings.dry_run_latency_ms,
        taker_fee_bps=settings.default_taker_fee_bps,
        slippage_buffer_bps=settings.slippage_buffer_bps,
        liquidity_haircut=settings.liquidity_haircut,
    )

    stop = asyncio.Event()
    spot_symbols = settings.spot_symbol_list
    perp_symbols = settings.perp_symbol_list
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
    opportunities: list[FundingOpportunity] = []
    open_carries: dict[str, OpenCarry] = {}
    realized_pnl = Decimal("0")
    notional = settings.target_quote_size_list[0]
    spot_key_default = f"{settings.spot_exchange_list[0]}:{spot_symbols[0]}"

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async def close_carry(key: str, carry: OpenCarry, reason: str) -> None:
        nonlocal realized_pnl

        def fetch() -> tuple[L2OrderBook, L2OrderBook]:
            return books[carry.spot_key], books[carry.perp_key]

        try:
            pnl = await paper.close_carry(carry.fill, carry.funding_collected, fetch)
        except (KeyError, AssertionError) as exc:
            breaker.record_failure()
            await notifier.send(f"UNWIND FAILED {key}: {exc}")
            return
        realized_pnl += pnl
        breaker.record_pnl_eur(pnl)
        del open_carries[key]
        await notifier.send(f"UNWOUND {key} ({reason}): pnl {pnl} EUR")

    with Live(console=console, refresh_per_second=2) as live:
        while not stop.is_set():
            now_ns = time.monotonic_ns()

            while not books_q.empty():
                book = books_q.get_nowait()
                books[f"{book.exchange}:{book.symbol}"] = book
                ticks.record_book(book)

            while not funding_q.empty():
                rate = funding_q.get_nowait()
                fkey = f"{rate.exchange}:{rate.symbol}"
                funding[fkey] = rate
                if breaker.halted or not gate.is_fresh(rate, now_ns):
                    continue
                pkey = f"{spot_key_default}|{fkey}"
                if pkey in open_carries:
                    continue  # one carry per market pair
                opp = detect_funding_carry(
                    rate,
                    notional=notional,
                    holding_intervals=90,
                    taker_fee_bps=settings.default_taker_fee_bps,
                    slippage_buffer_bps=settings.slippage_buffer_bps,
                    min_net_profit_usd=settings.min_net_profit_usd,
                )
                if opp is None:
                    continue
                opportunities.append(opp)
                print_opportunity(console, opp)

                if spot_key_default not in books or fkey not in books:
                    continue  # books not ready yet; retry on next funding poll
                if not (gate.is_fresh(books[spot_key_default], now_ns)
                        and gate.is_fresh(books[f"{rate.exchange}:{rate.symbol}"], now_ns)):
                    continue
                breaker.record_order()
                breaker.record_order()

                def fetch() -> tuple[L2OrderBook, L2OrderBook]:
                    return books[spot_key_default], books[f"{rate.exchange}:{rate.symbol}"]

                fill = await paper.open_carry(opp, fetch)
                if fill is None:
                    breaker.record_failure()
                    continue
                breaker.record_success()
                open_carries[pkey] = OpenCarry(
                    fill=fill,
                    opp=opp,
                    spot_key=spot_key_default,
                    perp_key=f"{rate.exchange}:{rate.symbol}",
                    funding_key=fkey,
                    risk=CarryPositionRisk.build(fill.perp_vwap, D("1")),
                    funding_collected=Decimal("0"),
                    last_payment_ts=rate.next_funding_ts,
                )
                await notifier.send(
                    f"PAPER OPEN {opp.exchange} {opp.symbol} notional {opp.notional} "
                    f"rate {opp.funding_rate} projected {opp.projected_net} USD"
                )

            for key, carry in list(open_carries.items()):
                rate = funding.get(carry.funding_key)
                if rate is not None and rate.next_funding_ts != carry.last_payment_ts:
                    # a funding payment happened since our last observation
                    carry.funding_collected += mul(carry.fill.qty_base, carry.fill.perp_vwap) * rate.rate
                    await repo.add_funding(
                        carry.fill.position_id,
                        mul(carry.fill.qty_base, carry.fill.perp_vwap) * rate.rate,
                    )
                    carry.last_payment_ts = rate.next_funding_ts
                    if funding_flip_should_unwind(
                        rate.predicted_rate if rate.predicted_rate is not None else rate.rate
                    ):
                        await close_carry(key, carry, "funding flip")
                        continue
                perp_book = books.get(carry.perp_key)
                if perp_book is not None and carry.risk.must_close(
                    perp_book.best_ask.price, settings.liquidation_buffer_pct
                ):
                    await close_carry(key, carry, "liquidation buffer")

            live.update(
                render(
                    books,
                    funding,
                    opportunities,
                    breaker,
                    len(open_carries),
                    realized_pnl,
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
        async def _export() -> int:
            repo = Repository(get_settings().sqlite_url)
            await repo.init()
            rows = await repo.export_tax_csv(sys.argv[2])
            await repo.close()
            return rows

        rows = asyncio.run(_export())
        print(f"exported {rows} audit rows to {sys.argv[2]}")
        return
    asyncio.run(run())


if __name__ == "__main__":
    entrypoint()
