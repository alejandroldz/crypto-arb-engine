"""Strategy A detector: cash-and-carry / funding-rate arbitrage.

Long spot + short perp of equal notional, delta-neutral, collecting positive
funding. An opportunity exists when the funding rate (current or predicted)
is positive enough that the projected net PnL over the planned holding
window clears MIN_NET_PROFIT_USD after worst-case costs.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from app.connectors.normalizer import FundingRate
from app.core.decimals import D
from app.engines.validator import (
    approve_trade,
    funding_breakeven_intervals,
    funding_net_pnl,
)


@dataclass(frozen=True)
class FundingRateStats:
    samples: int
    mean: Decimal
    median: Decimal
    p25: Decimal  # pessimistic percentile: 75% of observations were better


def funding_rate_stats(history: Sequence[Decimal]) -> FundingRateStats | None:
    """Distribution of observed funding rates (all Decimal, nearest-rank)."""
    rates = sorted(D(r) for r in history)
    n = len(rates)
    if n == 0:
        return None

    def rank(pct: int) -> Decimal:
        idx = max(0, min(n - 1, (pct * n + 99) // 100 - 1))
        return rates[idx]

    return FundingRateStats(
        samples=n,
        mean=sum(rates) / D(n),
        median=rank(50),
        p25=rank(25),
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
    spot_taker_fee_bps: Decimal,
    perp_taker_fee_bps: Decimal,
    slippage_buffer_bps: Decimal,
    min_net_profit_usd: Decimal,
    spot_entry_fee_bps: Decimal | None = None,
    perp_entry_fee_bps: Decimal | None = None,
    historical_rates: Sequence[Decimal] = (),
    min_history_samples: int = 30,
) -> FundingOpportunity | None:
    """Evaluate one funding snapshot as a carry opportunity.

    Never approves on the spot rate alone: without at least
    ``min_history_samples`` historical observations the answer is None.
    The effective rate is the pessimistic blend min(current/predicted, p25
    of history), so a momentary funding spike cannot open a carry.
    """
    stats = funding_rate_stats(historical_rates)
    if stats is None or stats.samples < min_history_samples:
        return None
    spot_rate = (
        funding.predicted_rate if funding.predicted_rate is not None else funding.rate
    )
    if spot_rate <= 0:
        return None
    rate = min(spot_rate, stats.p25)
    if rate <= 0:
        return None
    projected = funding_net_pnl(
        notional=D(notional),
        funding_rate=rate,
        intervals=holding_intervals,
        spot_taker_fee_bps=D(spot_taker_fee_bps),
        perp_taker_fee_bps=D(perp_taker_fee_bps),
        slippage_buffer_bps=D(slippage_buffer_bps),
        spot_entry_fee_bps=spot_entry_fee_bps,
        perp_entry_fee_bps=perp_entry_fee_bps,
    )
    if not approve_trade(projected, D(min_net_profit_usd)).approved:
        return None
    breakeven = funding_breakeven_intervals(
        notional=D(notional),
        funding_rate=rate,
        spot_taker_fee_bps=D(spot_taker_fee_bps),
        perp_taker_fee_bps=D(perp_taker_fee_bps),
        slippage_buffer_bps=D(slippage_buffer_bps),
        spot_entry_fee_bps=spot_entry_fee_bps,
        perp_entry_fee_bps=perp_entry_fee_bps,
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
