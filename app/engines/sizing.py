"""Dynamic position sizing (reduced Kelly, hard-capped).

Signal score = expected net edge per funding interval / observed funding
volatility (Sharpe-like). The raw Kelly instinct (mu/sigma^2) explodes for
funding carries because the payout is a rate stream, not a return per unit
exposure; so we bound it deliberately: score >= full_score deploys the full
cap, lower scores scale linearly, and kelly_fraction shaves everything.
The result NEVER exceeds the per-carry cap nor the remaining portfolio
budget, and a size below min_notional means "do not trade".
"""
from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from app.core.decimals import D, ZERO, div, mul


def rate_std(rates: Sequence[Decimal]) -> Decimal:
    """Population stddev of observed funding rates (Decimal-only)."""
    n = len(rates)
    if n < 2:
        return ZERO
    xs = [D(r) for r in rates]
    mean = sum(xs) / D(n)
    var = sum((x - mean) * (x - mean) for x in xs) / D(n)
    return var.sqrt()


def dynamic_notional(
    *,
    edge_per_interval: Decimal,
    history: Sequence[Decimal],
    budget_remaining: Decimal,
    kelly_fraction: Decimal,
    full_score: Decimal,
    min_notional: Decimal,
    max_notional: Decimal,
) -> Decimal:
    """Notional for one carry under reduced-Kelly sizing. ZERO = skip."""
    edge = D(edge_per_interval)
    budget = D(budget_remaining)
    if edge <= ZERO or budget <= ZERO:
        return ZERO
    std = rate_std(history)
    # zero observed variance = the strongest possible signal
    score = div(edge, std) if std > ZERO else D("999999")
    # fuzz F53: the min() capped the SCORE, but kelly multiplied AFTER the
    # cap - kelly_fraction=100 gave 100x the documented per-carry cap.
    # Clamp the product: fraction NEVER exceeds 1.
    fraction = min(D("1"),
                   min(D("1"), div(score, D(full_score))) * D(kelly_fraction))
    notional = min(D(max_notional), budget) * fraction
    if notional < D(min_notional):
        return ZERO
    return notional
