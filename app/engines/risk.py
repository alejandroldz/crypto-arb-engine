"""Risk gates: staleness (RISK 3), liquidation distance (RISK 5),
funding flips (RISK 4). Pure Decimal math, no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.core.decimals import D, div, mul, pct_of


class StalenessGate:
    """Hard staleness gate (RISK 3): reject data older than the budget."""

    def __init__(self, max_age_ms: int):
        self.max_age_ms = max_age_ms

    def is_fresh(self, snapshot: object, now_ns: int) -> bool:
        """Accepts any snapshot exposing age_ms(now_ns) (books, funding)."""
        return snapshot.age_ms(now_ns) <= self.max_age_ms


def short_liquidation_price(entry_price: Decimal, leverage: Decimal) -> Decimal:
    """Estimated liquidation price of an isolated-margin short.

    Approximation: liq = entry * (1 + 1/leverage), i.e. the position is
    wiped out when adverse move equals the posted margin. Maintenance
    margin makes real liquidation slightly EARLIER, so this estimate is
    optimistic and the 15% buffer (RISK 5) compensates.
    """
    entry = D(entry_price)
    lev = D(leverage)
    return mul(entry, D("1") + div(D("1"), lev))


def approaching_liquidation(
    current_price: Decimal, liquidation_price: Decimal, buffer_pct: Decimal
) -> bool:
    """True when price is within ``buffer_pct`` of the short's liq price."""
    threshold = D(liquidation_price) - pct_of(D(liquidation_price), D(buffer_pct))
    return D(current_price) >= threshold


def funding_flip_should_unwind(
    predicted_rate: Decimal, negative_threshold: Decimal = Decimal("0")
) -> bool:
    """RISK 4: predicted funding below threshold -> propose unwind."""
    return D(predicted_rate) < D(negative_threshold)


def leverage_ok(leverage: Decimal, max_leverage: Decimal) -> bool:
    lev = D(leverage)
    return D("1") <= lev <= D(max_leverage)


@dataclass(frozen=True)
class CarryPositionRisk:
    """Risk view of an open cash-and-carry position."""

    entry_price: Decimal
    leverage: Decimal
    liquidation_price: Decimal

    @classmethod
    def build(cls, entry_price: Decimal, leverage: Decimal) -> "CarryPositionRisk":
        return cls(
            entry_price=D(entry_price),
            leverage=D(leverage),
            liquidation_price=short_liquidation_price(entry_price, leverage),
        )

    def must_close(self, current_price: Decimal, buffer_pct: Decimal) -> bool:
        return approaching_liquidation(
            current_price, self.liquidation_price, buffer_pct
        )
