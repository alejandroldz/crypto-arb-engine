"""Deterministic math validator for the funding-rate carry (RISK 4).

Approves trades only on worst-case economics: 100% taker fees on every leg,
entry AND exit, plus a configurable slippage buffer. All Decimal math.

Required approval rule:
    Net Profit = Gross Payout - (Cost_leg1 + Cost_leg2 + Fee_leg1
                 + Fee_leg2 + SlippageBuffer) > MIN_NET_PROFIT_USD

For the cash-and-carry the gross payout is the collected funding; the cost
legs are the spot purchase and the perp short; each of the four fills (open
spot, open short, close spot, close short) pays worst-case taker fees.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from app.core.decimals import D, ZERO, bps_of, div, mul


@dataclass(frozen=True)
class TradeApproval:
    approved: bool
    net_profit: Decimal
    reason: str


def funding_turnover(notional: Decimal) -> Decimal:
    """Total traded notional of one funding-carry round trip.

    Entry: buy spot + open short perp. Exit: sell spot + close short.
    """
    return mul(D(notional), D("4"))


def funding_roundtrip_costs(
    *,
    notional: Decimal,
    spot_taker_fee_bps: Decimal,
    perp_taker_fee_bps: Decimal,
    slippage_buffer_bps: Decimal,
) -> Decimal:
    """Worst-case cost of one entry+exit round trip (4 taker fills).

    Two fills happen on the spot venue (buy + sell) and two on the perp
    venue (open + close short); each venue charges its own taker fee.
    """
    spot_turnover = mul(D(notional), D("2"))
    perp_turnover = mul(D(notional), D("2"))
    fees = bps_of(spot_turnover, D(spot_taker_fee_bps)) + bps_of(
        perp_turnover, D(perp_taker_fee_bps)
    )
    return fees + bps_of(spot_turnover + perp_turnover, D(slippage_buffer_bps))


def funding_payout_per_interval(notional: Decimal, funding_rate: Decimal) -> Decimal:
    """Funding received per interval by the short leg when rate > 0."""
    return mul(D(notional), D(funding_rate))


def funding_net_pnl(
    *,
    notional: Decimal,
    funding_rate: Decimal,
    intervals: int,
    spot_taker_fee_bps: Decimal,
    perp_taker_fee_bps: Decimal,
    slippage_buffer_bps: Decimal,
) -> Decimal:
    """Net PnL of holding the carry for ``intervals`` funding intervals."""
    payout = mul(funding_payout_per_interval(notional, funding_rate), D(intervals))
    return payout - funding_roundtrip_costs(
        notional=notional,
        spot_taker_fee_bps=spot_taker_fee_bps,
        perp_taker_fee_bps=perp_taker_fee_bps,
        slippage_buffer_bps=slippage_buffer_bps,
    )


def funding_breakeven_intervals(
    *,
    notional: Decimal,
    funding_rate: Decimal,
    spot_taker_fee_bps: Decimal,
    perp_taker_fee_bps: Decimal,
    slippage_buffer_bps: Decimal,
) -> int | None:
    """Intervals needed to recoup round-trip costs; None if rate <= 0."""
    payout = funding_payout_per_interval(notional, funding_rate)
    if payout <= ZERO:
        return None
    costs = funding_roundtrip_costs(
        notional=notional,
        spot_taker_fee_bps=spot_taker_fee_bps,
        perp_taker_fee_bps=perp_taker_fee_bps,
        slippage_buffer_bps=slippage_buffer_bps,
    )
    return int(div(costs, payout).to_integral_value(rounding=ROUND_CEILING))


def approve_trade(net_profit: Decimal, min_net_profit_usd: Decimal) -> TradeApproval:
    """The single approval gate: strictly greater than the configured minimum."""
    net = D(net_profit)
    minimum = D(min_net_profit_usd)
    approved = net > minimum
    return TradeApproval(
        approved=approved,
        net_profit=net,
        reason="net profit above minimum" if approved else "net profit below minimum",
    )
