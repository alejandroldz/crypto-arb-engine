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

from app.core.decimals import D, div
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
    last_rate: object = None  # dif D5: pre-cut rate for OKX accrual
    uta: "UtaAccountState | None" = None
    mmr_warned: bool = False
    books_stale_warned: bool = False


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
    spot_ex: str
    notional: Decimal  # per-carry size; run() rebalances it on watchlist changes
    uta_state: "UtaAccountState | None"
    max_safe_pump: "Decimal | None"
    # perp keys ("exchange:symbol") allowed for ENTRY; None = everything
    # configured. The screener (or config default) owns the contents; an
    # open carry is never closed by removal from this set.
    eligible: "set[str] | None" = None
    # screener's venue-historical funding mean per perp key, refreshed each
    # sweep; used to corroborate the polled history (audit screener#11)
    screener_means: "dict[str, Decimal] | None" = None
    # notional used by OTHER strategies (e.g. perp-perp); run() wires this
    # so the portfolio budget is shared, not per-module
    external_allocated: "Callable[[], Decimal] | None" = None


def sources_disagree(screener_mean: "Decimal", polled_mean: "Decimal") -> bool:
    """Material discrepancy between two funding sources (audit screener#11):
    opposite signs, or a >3x magnitude gap. Zero/None never 'disagree'."""
    if not screener_mean or not polled_mean:
        return False
    if (screener_mean > 0) != (polled_mean > 0):
        return True
    a, b = abs(screener_mean), abs(polled_mean)
    return max(a, b) > 3 * min(a, b)


async def handle_funding_candidate(
    ctx: CarryContext, rate: "FundingRate", now_ns: int
) -> OpenCarry | None:
    """Process one fresh funding rate; return the opened carry, if any."""
    if ctx.breaker.halted or not ctx.gate.is_fresh(rate, now_ns):
        return None
    fkey = f"{rate.exchange}:{rate.symbol}"
    if fkey in ctx.settings.screener_blacklist_keys:
        return None
    if ctx.eligible is not None and fkey not in ctx.eligible:
        return None
    # the spot leg lives on the same base asset (BTC perp -> BTC/USD spot)
    base = rate.symbol.split("/")[0]
    spot_symbol = ctx.settings.spot_symbol_for_base(base) or f"{base}/USD"
    spot_key = f"{ctx.spot_ex}:{spot_symbol}"
    pkey = f"{spot_key}|{fkey}"
    if pkey in ctx.open_carries:
        return None
    if ctx.settings.fees_blocked:
        return None  # fee guardrail: entradas bloqueadas hasta revision manual
    # holding horizon 30 days expressed in THIS venue's funding intervals
    interval_h = ctx.settings.funding_interval_h_for(rate.exchange)
    history = await asyncio.to_thread(
        ctx.ticks.funding_history, rate.exchange, rate.symbol
    )
    # audit screener#11: corroborate the polled history against the
    # screener's independent venue-historical mean; a material discrepancy
    # quarantines ENTRIES (open positions keep being managed)
    smean = (ctx.screener_means or {}).get(fkey)
    if smean is not None and len(history) >= ctx.settings.min_funding_history:
        pmean = sum(history) / len(history)
        if sources_disagree(D(smean), pmean):
            logger.warning(
                "funding source mismatch %s: screener mean %s vs polled mean %s - entries quarantined",
                fkey, smean, pmean,
            )
            return None
    opp = detect_funding_carry(
        rate,
        notional=ctx.notional,
        holding_intervals=int(Decimal("720") / interval_h),
        spot_taker_fee_bps=ctx.settings.taker_fee_for(ctx.spot_ex),
        perp_taker_fee_bps=ctx.settings.taker_fee_for(rate.exchange),
        slippage_buffer_bps=ctx.settings.slippage_buffer_bps,
        min_net_profit_usd=ctx.settings.min_net_profit_usd,
        spot_entry_fee_bps=ctx.settings.entry_fee_for(ctx.spot_ex),
        perp_entry_fee_bps=ctx.settings.entry_fee_for(rate.exchange),
        historical_rates=history,
        min_history_samples=ctx.settings.min_funding_history,
    )
    if opp is None:
        return None
    if ctx.settings.sizing_mode == "kelly":
        from dataclasses import replace

        from app.engines.sizing import dynamic_notional

        allocated_now = sum(D(c.opp.notional) for c in ctx.open_carries.values())
        if ctx.external_allocated is not None:
            allocated_now += D(ctx.external_allocated())
        budget = (
            D(ctx.uta_state.notional) - allocated_now
            if ctx.uta_state is not None
            else D(ctx.notional)
        )
        sized = dynamic_notional(
            edge_per_interval=opp.funding_rate,
            history=history,
            budget_remaining=budget,
            kelly_fraction=D(ctx.settings.kelly_fraction),
            full_score=D(ctx.settings.sizing_full_score),
            min_notional=D(ctx.settings.sizing_min_notional_usd),
            max_notional=D(ctx.settings.sizing_max_notional_usd),
        )
        if sized <= 0:
            logger.info("sizing kelly descarta %s %s (size bajo minimo)", rate.exchange, rate.symbol)
            return None
        if sized != opp.notional:
            # payout y costes escalan linealmente con el nocional (fees en bps)
            scale = sized / D(opp.notional)
            opp = replace(opp, notional=sized, projected_net=D(opp.projected_net) * scale)
    ctx.opportunities.append(opp)
    print_opportunity(ctx.console, opp)

    perp_key = f"{rate.exchange}:{rate.symbol}"
    if spot_key not in ctx.books or perp_key not in ctx.books:
        return None
    # F06: observable spread gate - the signal is only real if both books
    # are sane. Fees+slippage buffer do not capture a 99-dollar spread.
    max_spread = D(ctx.settings.max_book_spread_bps)
    for key in (spot_key, perp_key):
        b = ctx.books[key]
        mid = (b.best_bid.price + b.best_ask.price) / 2
        spread_bps = div(b.best_ask.price - b.best_bid.price, mid) * D("10000")
        if spread_bps > max_spread:
            logger.info("spread %s bps > %s en %s: senal rechazada",
                        spread_bps, max_spread, key)
            return None
    if not (
        ctx.gate.is_fresh(ctx.books[spot_key], now_ns)
        and ctx.gate.is_fresh(ctx.books[perp_key], now_ns)
    ):
        return None
    if ctx.uta_state is not None:
        # portfolio budget: total open notional + this carry must fit the
        # utilization cap, or several small carries silently over-deploy
        allocated = sum(D(c.opp.notional) for c in ctx.open_carries.values())
        if ctx.external_allocated is not None:
            allocated += D(ctx.external_allocated())
        if allocated + D(opp.notional) > ctx.uta_state.notional:
            logger.info("UTA budget full, skipping %s", fkey)
            return None
        uta_gate = approve_unified_carry(
            collateral_ratio=ctx.settings.collateral_ratio_for(base),
            min_collateral_ratio=D(ctx.settings.min_collateral_ratio_required),
            max_safe_pump_pct=ctx.max_safe_pump,
        )
        if not uta_gate.approved:
            logger.info("UTA gate rejected %s: %s", rate.symbol, uta_gate.reason)
            return None
    if not ctx.breaker.reserve_orders(2):
        logger.warning("breaker rechaza entrada %s %s (rate limit / HALT)",
                       rate.exchange, rate.symbol)
        return None

    def fetch() -> tuple["L2OrderBook", "L2OrderBook"]:
        return ctx.books[spot_key], ctx.books[perp_key]

    fill = await ctx.paper.open_carry(opp, fetch, spot_symbol=spot_symbol)
    if fill is None:
        ctx.breaker.record_failure()
        return None
    ctx.breaker.record_success()
    carry = OpenCarry(
        fill=fill,
        opp=opp,
        spot_key=spot_key,
        perp_key=perp_key,
        funding_key=fkey,
        risk=CarryPositionRisk.build(
            fill.perp_vwap, D(ctx.settings.short_leverage_for(rate.exchange))
        ),
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
