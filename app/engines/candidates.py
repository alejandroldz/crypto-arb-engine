"""Funding-carry candidate handling, decoupled from the asyncio main loop.

run() owns IO (queues, websockets, REST); this module owns the decision
flow for one funding-rate candidate: staleness -> duplicate -> economics ->
UTA gate -> paper open. Pure enough to drive from tests with fakes, which
is how the multi-candidate regression (gate shadowing) is covered.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from app.core.decimals import D
from app.engines.arbitrage import FundingOpportunity, detect_funding_carry
from app.engines.risk import CarryPositionRisk, StalenessGate, UtaAccountState
from app.engines.validator import approve_unified_carry
from app.ui.cli_dashboard import print_opportunity

if TYPE_CHECKING:
    from app.connectors.normalizer import FundingRate, L2OrderBook
    from app.core.circuit_breaker import CircuitBreaker
    from app.core.config import Settings
    from app.database.duckdb_store import TickStore
    from app.engines.paper_trader import PaperFill, PaperTrader
    from app.ui.notifier import TelegramNotifier

logger = logging.getLogger("main")


@dataclass
class OpenCarry:
    fill: "PaperFill"
    opp: FundingOpportunity
    spot_key: str
    perp_key: str
    funding_key: str
    risk: CarryPositionRisk
    funding_collected: Decimal
    last_payment_ts: object
    accrual_uncertain: bool = False
    uta: "UtaAccountState | None" = None
    mmr_warned: bool = False


@dataclass
class CarryContext:
    """Everything handle_funding_candidate needs; owned and mutated by run()."""

    settings: "Settings"
    breaker: "CircuitBreaker"
    gate: StalenessGate
    paper: "PaperTrader"
    ticks: "TickStore"
    console: object
    notifier: "TelegramNotifier"
    books: dict[str, "L2OrderBook"]
    opportunities: object  # deque[FundingOpportunity]
    open_carries: dict[str, OpenCarry]
    spot_key_default: str
    spot_symbol: str
    spot_ex: str
    notional: Decimal
    uta_state: "UtaAccountState | None"
    coll_ratio: Decimal
    max_safe_pump: "Decimal | None"


async def handle_funding_candidate(
    ctx: CarryContext, rate: "FundingRate", now_ns: int
) -> OpenCarry | None:
    """Process one fresh funding rate; return the opened carry, if any."""
    if ctx.breaker.halted or not ctx.gate.is_fresh(rate, now_ns):
        return None
    fkey = f"{rate.exchange}:{rate.symbol}"
    pkey = f"{ctx.spot_key_default}|{fkey}"
    if pkey in ctx.open_carries:
        return None
    history = await asyncio.to_thread(
        ctx.ticks.funding_history, rate.exchange, rate.symbol
    )
    opp = detect_funding_carry(
        rate,
        notional=ctx.notional,
        holding_intervals=90,
        spot_taker_fee_bps=ctx.settings.taker_fee_for(ctx.spot_ex),
        perp_taker_fee_bps=ctx.settings.taker_fee_for(rate.exchange),
        slippage_buffer_bps=ctx.settings.slippage_buffer_bps,
        min_net_profit_usd=ctx.settings.min_net_profit_usd,
        historical_rates=history,
        min_history_samples=ctx.settings.min_funding_history,
    )
    if opp is None:
        return None
    ctx.opportunities.append(opp)
    print_opportunity(ctx.console, opp)

    perp_key = f"{rate.exchange}:{rate.symbol}"
    if ctx.spot_key_default not in ctx.books or perp_key not in ctx.books:
        return None
    if not (
        ctx.gate.is_fresh(ctx.books[ctx.spot_key_default], now_ns)
        and ctx.gate.is_fresh(ctx.books[perp_key], now_ns)
    ):
        return None
    if ctx.uta_state is not None:
        uta_gate = approve_unified_carry(
            collateral_ratio=ctx.coll_ratio,
            min_collateral_ratio=D(ctx.settings.min_collateral_ratio_required),
            max_safe_pump_pct=ctx.max_safe_pump,
        )
        if not uta_gate.approved:
            logger.info("UTA gate rejected %s: %s", rate.symbol, uta_gate.reason)
            return None
    ctx.breaker.record_order()
    ctx.breaker.record_order()

    def fetch() -> tuple["L2OrderBook", "L2OrderBook"]:
        return ctx.books[ctx.spot_key_default], ctx.books[perp_key]

    fill = await ctx.paper.open_carry(opp, fetch, spot_symbol=ctx.spot_symbol)
    if fill is None:
        ctx.breaker.record_failure()
        return None
    ctx.breaker.record_success()
    carry = OpenCarry(
        fill=fill,
        opp=opp,
        spot_key=ctx.spot_key_default,
        perp_key=perp_key,
        funding_key=fkey,
        risk=CarryPositionRisk.build(fill.perp_vwap, D(ctx.settings.short_leverage)),
        funding_collected=Decimal("0"),
        last_payment_ts=rate.next_funding_ts,
        uta=ctx.uta_state,
    )
    ctx.open_carries[pkey] = carry
    logger.info(
        "paper open %s %s notional %s rate %s projected %s",
        opp.exchange, opp.symbol, opp.notional, opp.funding_rate, opp.projected_net,
    )
    await ctx.notifier.send(
        f"PAPER OPEN {opp.exchange} {opp.symbol} notional {opp.notional} "
        f"rate {opp.funding_rate} projected {opp.projected_net} USD"
    )
    return carry
