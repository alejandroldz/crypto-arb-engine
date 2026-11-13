"""Atomic 2-leg execution coordinator (RISK 1). DISABLED by default.

State machine:
    PENDING -> HEDGED | UNWOUND | UNWIND_UNCERTAIN

Rules:
  - the LESS liquid leg goes first (caller passes legs in that order)
  - leg 2 is sized to leg 1's ACTUAL fill, never to the requested qty
  - legs use IOC limit orders; if leg 2 is not fully filled within
    MAX_LEG_DELAY_MS, both legs are market-unwound to ZERO net exposure
  - timeouts and venue failures trigger a reconcile attempt (fetch actual
    fills) before deciding; a fill outcome that stays unknown yields
    UNWIND_UNCERTAIN, which always requires human attention
  - the whole module refuses to run unless LIVE_TRADING_ENABLED is True
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Awaitable, Callable

from app.core.decimals import D, ZERO


class LiveTradingDisabledError(RuntimeError):
    pass


class LegState:
    PENDING = "PENDING"
    HEDGED = "HEDGED"
    UNWOUND = "UNWOUND"
    UNWIND_UNCERTAIN = "UNWIND_UNCERTAIN"


@dataclass(frozen=True)
class LegOrder:
    exchange: str
    symbol: str
    side: str  # "buy" / "sell"
    qty_base: Decimal
    limit_price: Decimal


@dataclass(frozen=True)
class ExecutionResult:
    state: str
    filled_leg1: Decimal
    filled_leg2: Decimal
    unwound_leg1: Decimal
    unwound_leg2: Decimal
    note: str = ""


# async (leg) -> filled base qty
PlaceIOC = Callable[[LegOrder], Awaitable[Decimal]]
# async (exchange, symbol, side, qty) -> realized average price
PlaceMarket = Callable[[str, str, str, Decimal], Awaitable[Decimal]]
# async (leg) -> actual filled base qty, or None if the venue cannot say
ReconcileFills = Callable[[LegOrder], Awaitable[Decimal | None]]


def _opposite(side: str) -> str:
    return "sell" if side == "buy" else "buy"


class AtomicLegCoordinator:
    def __init__(
        self,
        *,
        live_trading_enabled: bool,
        place_ioc: PlaceIOC,
        place_market: PlaceMarket,
        max_leg_delay_ms: int,
        reconcile_fills: ReconcileFills | None = None,
    ):
        if not live_trading_enabled:
            raise LiveTradingDisabledError(
                "LIVE_TRADING_ENABLED is False: live execution is disabled"
            )
        self.place_ioc = place_ioc
        self.place_market = place_market
        self.max_leg_delay_ms = max_leg_delay_ms
        self.reconcile_fills = reconcile_fills

    async def _actual_fills(self, leg: LegOrder) -> Decimal | None:
        if self.reconcile_fills is None:
            return None
        actual = await self.reconcile_fills(leg)
        return D(actual) if actual is not None else None

    async def _flatten(self, leg1: LegOrder, filled1: Decimal, leg2: LegOrder,
                       filled2: Decimal) -> ExecutionResult:
        """Market-unwind both legs to zero net exposure."""
        unwound1 = ZERO
        unwound2 = ZERO
        problems: list[str] = []
        if filled1 > ZERO:
            try:
                await self.place_market(
                    leg1.exchange, leg1.symbol, _opposite(leg1.side), filled1
                )
                unwound1 = filled1
            except Exception as exc:
                problems.append(f"leg1 unwind failed: {type(exc).__name__}")
        if filled2 > ZERO:
            try:
                await self.place_market(
                    leg2.exchange, leg2.symbol, _opposite(leg2.side), filled2
                )
                unwound2 = filled2
            except Exception as exc:
                problems.append(f"leg2 unwind failed: {type(exc).__name__}")
        if problems:
            return ExecutionResult(
                state=LegState.UNWIND_UNCERTAIN,
                filled_leg1=filled1,
                filled_leg2=filled2,
                unwound_leg1=unwound1,
                unwound_leg2=unwound2,
                note="; ".join(problems) + " - manual check required",
            )
        return ExecutionResult(
            state=LegState.UNWOUND,
            filled_leg1=filled1,
            filled_leg2=filled2,
            unwound_leg1=unwound1,
            unwound_leg2=unwound2,
            note="both legs flattened",
        )

    async def execute(self, leg1: LegOrder, leg2: LegOrder) -> ExecutionResult:
        """Run the atomic pair. leg1 MUST be the less liquid leg."""
        # --- leg 1 ---
        try:
            filled1 = D(await self.place_ioc(leg1))
        except Exception as exc:
            actual = await self._actual_fills(leg1)
            if actual is None:
                return ExecutionResult(
                    state=LegState.UNWIND_UNCERTAIN,
                    filled_leg1=ZERO,
                    filled_leg2=ZERO,
                    unwound_leg1=ZERO,
                    unwound_leg2=ZERO,
                    note=f"leg1 order failed ({type(exc).__name__}) and fill "
                    "outcome is unknown - manual check required",
                )
            if actual == ZERO:
                return ExecutionResult(
                    state=LegState.PENDING,
                    filled_leg1=ZERO,
                    filled_leg2=ZERO,
                    unwound_leg1=ZERO,
                    unwound_leg2=ZERO,
                    note=f"leg1 order failed ({type(exc).__name__}), reconciled: no fill",
                )
            filled1 = actual

        if filled1 <= ZERO:
            return ExecutionResult(
                state=LegState.PENDING,
                filled_leg1=ZERO,
                filled_leg2=ZERO,
                unwound_leg1=ZERO,
                unwound_leg2=ZERO,
                note="leg1 did not fill",
            )

        # --- leg 2, sized to the ACTUAL leg1 fill ---
        leg2_sized = replace(leg2, qty_base=filled1)
        filled2: Decimal | None
        try:
            filled2 = D(
                await asyncio.wait_for(
                    self.place_ioc(leg2_sized), timeout=self.max_leg_delay_ms / 1000
                )
            )
        except asyncio.TimeoutError:
            filled2 = await self._actual_fills(leg2_sized)  # None = unknown
        except Exception as exc:
            actual = await self._actual_fills(leg2_sized)
            filled2 = actual if actual is not None else ZERO
            if actual is None:
                result = await self._flatten(leg1, filled1, leg2_sized, ZERO)
                return replace(
                    result,
                    note=f"leg2 venue failure ({type(exc).__name__}), "
                    f"fills unreconcilable, assumed zero; {result.note}",
                )

        if filled2 is None:
            # leg2 outcome unknown: flatten leg1, flag leg2 for a human
            result = await self._flatten(leg1, filled1, leg2_sized, ZERO)
            return ExecutionResult(
                state=LegState.UNWIND_UNCERTAIN,
                filled_leg1=filled1,
                filled_leg2=ZERO,
                unwound_leg1=result.unwound_leg1,
                unwound_leg2=ZERO,
                note="leg2 fill outcome unknown after timeout - manual check required",
            )

        if filled2 == filled1:
            return ExecutionResult(
                state=LegState.HEDGED,
                filled_leg1=filled1,
                filled_leg2=filled2,
                unwound_leg1=ZERO,
                unwound_leg2=ZERO,
                note="both legs filled, delta-neutral",
            )

        # partial or zero leg2 fill: flatten everything to zero net exposure
        return await self._flatten(leg1, filled1, leg2_sized, filled2)
