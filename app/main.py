"""Async entrypoint: ingestion -> detection -> paper trading -> dashboard.

`python -m app.main`                 run the paper-trading engine
`python -m app.main export-tax FILE` export the AEAT tax CSV and exit
"""
from __future__ import annotations

import asyncio
import signal
import sys
import time
from decimal import Decimal

from rich.console import Console
from rich.live import Live

from app.connectors.ccxt_manager import (
    BOOKS_TOPIC,
    FUNDING_TOPIC,
    BookFeed,
    FundingPoller,
)
from app.core.circuit_breaker import CircuitBreaker
from app.core.config import get_settings
from app.core.events import EventBus
from app.database.repository import Repository
from app.engines.arbitrage import detect_funding_carry
from app.engines.risk import StalenessGate
from app.ui.cli_dashboard import print_opportunity, render
from app.ui.notifier import TelegramNotifier


async def run() -> None:
    settings = get_settings()
    console = Console()
    bus = EventBus()
    repo = Repository(settings.sqlite_url)
    await repo.init()
    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    breaker = CircuitBreaker(
        max_consecutive_failures=settings.cb_max_consecutive_failures,
        max_daily_loss_eur=settings.max_daily_loss_eur,
        max_orders_per_window=settings.cb_max_orders_per_window,
        window_seconds=settings.cb_order_window_seconds,
    )
    funding_gate = StalenessGate(settings.max_data_age_ms_funding)

    stop = asyncio.Event()
    feeds = [
        BookFeed(ex, ["BTC/EUR"]).run(bus, stop)
        for ex in settings.spot_exchange_list
    ]
    symbols = settings.perp_symbol_list
    pollers = [
        FundingPoller(ex, [symbols[i]] if i < len(symbols) else symbols[:1]).run(bus, stop)
        for i, ex in enumerate(settings.perp_exchange_list)
    ]
    tasks = [asyncio.create_task(c) for c in feeds + pollers]

    books_q = bus.subscribe(BOOKS_TOPIC)
    funding_q = bus.subscribe(FUNDING_TOPIC)
    books: dict = {}
    funding: dict = {}
    opportunities: list = []
    notional = settings.target_quote_size_list[0]

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    with Live(console=console, refresh_per_second=2) as live:
        while not stop.is_set():
            now_ns = time.monotonic_ns()
            while not books_q.empty():
                book = books_q.get_nowait()
                books[f"{book.exchange}:{book.symbol}"] = book
            while not funding_q.empty():
                rate = funding_q.get_nowait()
                funding[f"{rate.exchange}:{rate.symbol}"] = rate
                if not breaker.halted and funding_gate.is_fresh(
                    _booklike(rate), now_ns
                ):
                    opp = detect_funding_carry(
                        rate,
                        notional=notional,
                        holding_intervals=90,
                        taker_fee_bps=settings.default_taker_fee_bps,
                        slippage_buffer_bps=settings.slippage_buffer_bps,
                        min_net_profit_usd=settings.min_net_profit_usd,
                    )
                    if opp is not None and opp not in opportunities:
                        opportunities.append(opp)
                        print_opportunity(console, opp)
                        await notifier.send(
                            f"CARRY {opp.exchange} {opp.symbol} rate={opp.funding_rate} "
                            f"net={opp.projected_net} USD over {opp.holding_intervals}"
                        )
            open_positions = await repo.open_positions()
            live.update(
                render(books, funding, opportunities, breaker, len(open_positions), Decimal("0"))
            )
            await asyncio.sleep(0.5)

    stop.set()
    for t in tasks:
        t.cancel()
    await repo.close()


def _booklike(rate):
    return rate  # FundingRate exposes the same age_ms(now_ns) interface


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
