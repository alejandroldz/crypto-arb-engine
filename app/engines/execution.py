"""Atomic 2-leg execution coordinator (RISK 1). DISABLED by default.

State machine:
    PENDING -> LEG1_FILLED -> LEG2_FILLED -> HEDGED
    PENDING -> LEG1_FILLED -> LEG2_FAILED -> EMERGENCY_UNWIND -> UNWOUND

Rules:
  - the LESS liquid leg goes first (caller passes legs in that order)
  - legs use IOC limit orders; if leg 2 is not fully filled within
    MAX_LEG_DELAY_MS a market order unwinds leg 1 immediately
  - the whole module refuses to run unless LIVE_TRADING_ENABLED is True
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Awaitable, Callable

from app.core.decimals import D, ZERO


class LiveTradingDisabledError(RuntimeError):
    pass


class LegState:
    PENDING = "PENDING"
    LEG1_FILLED = "LEG1_FILLED"
    LEG2_FILLED = "LEG2_FILLED"
    HEDGED = "HEDGED"
    LEG2_FAILED = "LEG2_FAILED"
    EMERGENCY_UNWIND = "EMERGENCY_UNWIND"
    UNWOUND = "UNWOUND"


@dataclass(frozen=True)
class LegOrder:
    exchange: str
    symbol: str
    side: str  # "buy" / "sell"
    qty_base: Decimal
    limit_price: Decimal


# async (leg) -> filled base qty
PlaceIOC = Callable[[LegOrder], Awaitable[Decimal]]
# async (exchange, symbol, side, qty) -> realized average price
PlaceMarket = Callable[[str, str, str, Decimal], Awaitable[Decimal]]


class AtomicLegCoordinator:
    def __init__(
        self,
        *,
        live_trading_enabled: bool,
        place_ioc: PlaceIOC,
        place_market: PlaceMarket,
        max_leg_delay_ms: int,
    ):
        if not live_trading_enabled:
            raise LiveTradingDisabledError(
                "LIVE_TRADING_ENABLED is False: live execution is disabled"
            )
        self.place_ioc = place_ioc
        self.place_market = place_market
        self.max_leg_delay_ms = max_leg_delay_ms

    async def execute(self, leg1: LegOrder, leg2: LegOrder) -> str:
        """Run the atomic pair. leg1 MUST be the less liquid leg."""
        state = LegState.PENDING

        filled1 = D(await self.place_ioc(leg1))
        if filled1 <= ZERO:
            return state  # nothing on the table, safe to walk away
        state = LegState.LEG1_FILLED

        try:
            filled2 = D(
                await asyncio.wait_for(
                    self.place_ioc(leg2), timeout=self.max_leg_delay_ms / 1000
                )
            )
        except asyncio.TimeoutError:
            filled2 = ZERO

        if filled2 >= D(leg2.qty_base):
            return LegState.HEDGED

        state = LegState.LEG2_FAILED
        # panic unwind: market order to flatten the naked leg immediately
        unwind_side = "sell" if leg1.side == "buy" else "buy"
        await self.place_market(leg1.exchange, leg1.symbol, unwind_side, filled1)
        state = LegState.EMERGENCY_UNWIND
        return LegState.UNWOUND
