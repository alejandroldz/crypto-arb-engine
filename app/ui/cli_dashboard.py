"""Rich live terminal dashboard: books, funding, opportunities, breaker."""
from __future__ import annotations

import time
from decimal import Decimal

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from app.connectors.normalizer import FundingRate, L2OrderBook
from app.core.circuit_breaker import CircuitBreaker
from app.engines.arbitrage import FundingOpportunity


def render(
    books: dict[str, L2OrderBook],
    funding: dict[str, FundingRate],
    opportunities: list[FundingOpportunity],
    breaker: CircuitBreaker,
    open_positions: int,
    paper_pnl_usd: Decimal,
    uta_line: str = "",
) -> Table:
    now_ns = time.monotonic_ns()
    table = Table(title="crypto-arb-engine (paper trading)")
    table.add_column("Feed")
    table.add_column("Symbol")
    table.add_column("Best bid")
    table.add_column("Best ask / rate")
    table.add_column("Age ms")

    # fuzz F38: venue-supplied strings go through Rich markup - escape them
    # or a symbol like "BTC[/green]" kills the whole dashboard loop
    for key, book in sorted(books.items()):
        table.add_row(
            escape(book.exchange),
            escape(book.symbol),
            str(book.best_bid.price),
            str(book.best_ask.price),
            str(book.age_ms(now_ns)),
        )
    for key, rate in sorted(funding.items()):
        table.add_row(
            escape(rate.exchange), escape(rate.symbol), "funding",
            str(rate.rate), str(rate.age_ms(now_ns)),
        )
    table.caption = (
        f"opportunities: {len(opportunities)} | open positions: {open_positions} | "
        f"paper PnL: {paper_pnl_usd} USD | breaker: "
        f"{'HALTED: ' + escape(breaker.halt_reason or '') if breaker.halted else 'ok'}"
        + (f"\n{uta_line}" if uta_line else "")
    )
    return table


def print_opportunity(console: Console, opp: FundingOpportunity) -> None:
    # fuzz F39: same escape for opportunity lines
    console.print(
        f"[green]CARRY[/green] {escape(opp.exchange)} {escape(opp.symbol)} notional={opp.notional} "
        f"rate={opp.funding_rate}/interval breakeven={opp.breakeven_intervals} "
        f"projected_net={opp.projected_net} USD over {opp.holding_intervals} intervals"
    )
