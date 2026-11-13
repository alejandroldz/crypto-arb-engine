"""RISK 1: atomic leg coordinator and panic unwind."""
import asyncio
from decimal import Decimal

import pytest

from app.core.decimals import D
from app.engines.execution import (
    AtomicLegCoordinator,
    LegOrder,
    LegState,
    LiveTradingDisabledError,
)

LEG1 = LegOrder(exchange="kraken", symbol="BTC/EUR", side="buy", qty_base=D("0.1"), limit_price=D("50000"))
LEG2 = LegOrder(exchange="bybit", symbol="BTC/USDT:USDT", side="sell", qty_base=D("0.1"), limit_price=D("50010"))


def test_refuses_when_live_trading_disabled():
    with pytest.raises(LiveTradingDisabledError):
        AtomicLegCoordinator(
            live_trading_enabled=False,
            place_ioc=None,
            place_market=None,
            max_leg_delay_ms=1500,
        )


async def test_happy_path_hedged():
    coord = AtomicLegCoordinator(
        live_trading_enabled=True,
        place_ioc=lambda leg: asyncio.coroutine(lambda: D("0.1"))() if False else _ret(D("0.1")),
        place_market=_ret(D("49900")),
        max_leg_delay_ms=1500,
    )
    assert await coord.execute(LEG1, LEG2) == LegState.HEDGED


async def _ret(value):
    return value


async def test_leg1_no_fill_stays_pending():
    calls = []

    async def ioc(leg):
        calls.append(leg)
        return D("0")

    coord = AtomicLegCoordinator(
        live_trading_enabled=True, place_ioc=ioc, place_market=_ret(D("0")),
        max_leg_delay_ms=1500,
    )
    assert await coord.execute(LEG1, LEG2) == LegState.PENDING
    assert len(calls) == 1  # leg 2 never attempted


async def test_leg2_failure_triggers_market_unwind():
    unwound = []

    async def ioc(leg):
        return D("0.1") if leg is LEG1 else D("0")

    async def market(exchange, symbol, side, qty):
        unwound.append((exchange, symbol, side, qty))
        return D("49800")

    coord = AtomicLegCoordinator(
        live_trading_enabled=True, place_ioc=ioc, place_market=market,
        max_leg_delay_ms=1500,
    )
    assert await coord.execute(LEG1, LEG2) == LegState.UNWOUND
    assert unwound == [("kraken", "BTC/EUR", "sell", D("0.1"))]


async def test_leg2_timeout_triggers_unwind():
    unwound = []

    async def ioc(leg):
        if leg is LEG1:
            return D("0.1")
        await asyncio.sleep(5)  # hangs past MAX_LEG_DELAY_MS
        return D("0.1")

    async def market(exchange, symbol, side, qty):
        unwound.append(qty)
        return D("49800")

    coord = AtomicLegCoordinator(
        live_trading_enabled=True, place_ioc=ioc, place_market=market,
        max_leg_delay_ms=100,
    )
    assert await coord.execute(LEG1, LEG2) == LegState.UNWOUND
    assert unwound == [D("0.1")]
