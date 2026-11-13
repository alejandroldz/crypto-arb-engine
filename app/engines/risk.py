"""Risk gates: staleness (RISK 3), liquidation distance (RISK 5),
funding flips (RISK 4). Pure Decimal math, no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.core.decimals import D, ZERO, div, mul, pct_of


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


@dataclass(frozen=True)
class UtaAccountState:
    """Simulated Unified Trading Account (cross-collateral) state.

    The purchased spot asset backs the perp short as collateral with a
    haircut. ``sim_mmr_rate`` is the simulated perp maintenance-margin
    rate: real perp MMR (~0.5%) makes a 75% account-MMR breach practically
    unreachable at 1x leverage, so paper mode uses a conservative 25%
    (roughly a 4x-leverage-equivalent account) to keep the emergency
    unwind path exercisable. Documented fallback, not exchange data.
    """

    equity: Decimal
    utilization: Decimal
    collateral_ratio: Decimal
    sim_mmr_rate: Decimal

    @property
    def notional(self) -> Decimal:
        return mul(D(self.equity), D(self.utilization))

    @property
    def buffer_cash(self) -> Decimal:
        return D(self.equity) - self.notional

    def effective_margin_balance(self, delta_p: Decimal) -> Decimal:
        """Buffer + collateral value of spot - unrealized short loss."""
        dp = D(delta_p)
        n = self.notional
        return (
            self.buffer_cash
            + mul(mul(n, D("1") + dp), D(self.collateral_ratio))
            - mul(n, dp)
        )

    def maintenance_margin(self, delta_p: Decimal) -> Decimal:
        return mul(mul(self.notional, D("1") + D(delta_p)), D(self.sim_mmr_rate))

    def account_mmr(self, delta_p: Decimal) -> Decimal:
        """totalMaintenanceMargin / totalMarginBalance; 1 when balance <= 0."""
        balance = self.effective_margin_balance(delta_p)
        if balance <= ZERO:
            return Decimal("1")
        return div(self.maintenance_margin(delta_p), balance)

    def max_safe_pump_pct(self, emergency_mmr: Decimal) -> Decimal | None:
        """Solve account_mmr(dp) = emergency_mmr for dp analytically.

        Returns None when no finite pump breaches the threshold, 0 when the
        account already starts at or above it.
        """
        u, m, c, r = D(self.utilization), D(self.sim_mmr_rate), D(self.collateral_ratio), D(emergency_mmr)
        a = mul(u, m)          # maintenance coefficient
        b = D("1") - u         # cash buffer fraction
        cc = mul(u, c)         # collateral coefficient
        num = mul(r, b + cc) - a
        den = a - mul(r, cc) + mul(r, u)
        if num <= ZERO:
            return ZERO
        if den <= ZERO:
            return None
        return div(num, den)
