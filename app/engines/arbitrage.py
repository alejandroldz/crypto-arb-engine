"""Strategy A detector: cash-and-carry / funding-rate arbitrage.

Long spot + short perp of equal notional, delta-neutral, collecting positive
funding. An opportunity exists when the funding rate (current or predicted)
is positive enough that the projected net PnL over the planned holding
window clears MIN_NET_PROFIT_USD after worst-case costs.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.connectors.normalizer import FundingRate
from app.core.decimals import D
from app.engines.validator import (
    approve_trade,
    funding_breakeven_intervals,
    funding_net_pnl,
)


@dataclass(frozen=True)
class FundingOpportunity:
    exchange: str
    symbol: str
    notional: Decimal
    funding_rate: Decimal
    holding_intervals: int
    breakeven_intervals: int
    projected_net: Decimal


def detect_funding_carry(
    funding: FundingRate,
    *,
    notional: Decimal,
    holding_intervals: int,
    taker_fee_bps: Decimal,
    slippage_buffer_bps: Decimal,
    min_net_profit_usd: Decimal,
) -> FundingOpportunity | None:
    """Evaluate one funding snapshot as a carry opportunity.

    Uses the predicted rate when available (what we will actually be paid),
    else the current rate. Returns None unless approved.
    """
    rate = funding.predicted_rate if funding.predicted_rate is not None else funding.rate
    if rate <= 0:
        return None
    projected = funding_net_pnl(
        notional=D(notional),
        funding_rate=rate,
        intervals=holding_intervals,
        taker_fee_bps=D(taker_fee_bps),
        slippage_buffer_bps=D(slippage_buffer_bps),
    )
    if not approve_trade(projected, D(min_net_profit_usd)).approved:
        return None
    breakeven = funding_breakeven_intervals(
        notional=D(notional),
        funding_rate=rate,
        taker_fee_bps=D(taker_fee_bps),
        slippage_buffer_bps=D(slippage_buffer_bps),
    )
    if breakeven is None or breakeven > holding_intervals:
        return None
    return FundingOpportunity(
        exchange=funding.exchange,
        symbol=funding.symbol,
        notional=D(notional),
        funding_rate=rate,
        holding_intervals=holding_intervals,
        breakeven_intervals=breakeven,
        projected_net=projected,
    )
