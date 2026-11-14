"""RISK 1: atomic leg coordinator, partial fills and panic unwind."""
import asyncio

import pytest

from app.core.decimals import D
from app.engines.execution import (
    AtomicLegCoordinator,
    ExecutionResult,
    LegOrder,
    LegState,
    LiveTradingDisabledError,
)

LEG1 = LegOrder(exchange="kraken", symbol="BTC/USD", side="buy", qty_base=D("0.1"), limit_price=D("50000"))
LEG2 = LegOrder(exchange="krakenfutures", symbol="BTC/USD:USD", side="sell", qty_base=D("0.1"), limit_price=D("50010"))


def make(place_ioc, place_market=None, reconcile=None, delay_ms=100):
    return AtomicLegCoordinator(
        live_trading_enabled=True,
        place_ioc=place_ioc,
        place_market=place_market or (lambda *a: _ret(D("0"))),
        max_leg_delay_ms=delay_ms,
        reconcile_fills=reconcile,
    )


async def _ret(value):
    return value


def test_refuses_when_live_trading_disabled():
    with pytest.raises(LiveTradingDisabledError):
        AtomicLegCoordinator(
            live_trading_enabled=False,
            place_ioc=None,
            place_market=None,
            max_leg_delay_ms=1500,
        )


async def test_happy_path_hedged():
    coord = make(lambda leg: _ret(D("0.1")))
    r = await coord.execute(LEG1, LEG2)
    assert r.state == LegState.HEDGED
    assert r.filled_leg1 == D("0.1") and r.filled_leg2 == D("0.1")


async def test_leg1_no_fill_stays_pending_and_leg2_never_sent():
    sent = []

    async def ioc(leg):
        sent.append(leg)
        return D("0")

    r = await make(ioc).execute(LEG1, LEG2)
    assert r.state == LegState.PENDING
    assert len(sent) == 1


async def test_leg1_partial_fill_sizes_leg2_to_actual():
    sent = []

    async def ioc(leg):
        sent.append(leg)
        return D("0.06")  # partial fill on leg1; leg2 fills fully

    coord = make(ioc)
    # leg2 must be sized to 0.06, not the requested 0.1
    r = await coord.execute(LEG1, LEG2)
    assert r.state == LegState.HEDGED
    assert sent[1].qty_base == D("0.06")
    assert r.filled_leg1 == r.filled_leg2 == D("0.06")


async def test_leg2_partial_fill_flattens_both_legs():
    unwound = []

    async def ioc(leg):
        return D("0.1") if leg is LEG1 else D("0.04")  # leg2 partial

    async def market(exchange, symbol, side, qty):
        unwound.append((exchange, symbol, side, qty))
        return D("49900")

    r = await make(ioc, market).execute(LEG1, LEG2)
    assert r.state == LegState.UNWOUND
    # both fills are closed: net exposure zero
    assert ("kraken", "BTC/USD", "sell", D("0.1")) in unwound
    assert ("krakenfutures", "BTC/USD:USD", "buy", D("0.04")) in unwound
    assert r.unwound_leg1 == D("0.1") and r.unwound_leg2 == D("0.04")


async def test_leg2_venue_failure_caught_and_unwound():
    unwound = []

    async def ioc(leg):
        if leg is LEG1:
            return D("0.1")
        raise ConnectionError("venue rejected the order")

    async def market(exchange, symbol, side, qty):
        unwound.append((side, qty))
        return D("49900")

    async def reconcile(leg):
        return D("0")  # venue confirms: nothing filled

    r = await make(ioc, market, reconcile).execute(LEG1, LEG2)
    assert r.state == LegState.UNWOUND
    assert unwound == [("sell", D("0.1"))]


async def test_leg2_timeout_reconciled_partial_fill_unwinds_both():
    unwound = []

    async def ioc(leg):
        if leg is LEG1:
            return D("0.1")
        await asyncio.sleep(5)  # hangs past MAX_LEG_DELAY_MS
        return D("0")

    async def market(exchange, symbol, side, qty):
        unwound.append((side, qty))
        return D("49900")

    async def reconcile(leg):
        return D("0.03")  # it actually partially filled during the timeout

    r = await make(ioc, market, reconcile, delay_ms=50).execute(LEG1, LEG2)
    assert r.state == LegState.UNWOUND
    assert ("sell", D("0.1")) in unwound
    assert ("buy", D("0.03")) in unwound


async def test_leg2_timeout_unknown_outcome_flags_uncertain():
    async def ioc(leg):
        if leg is LEG1:
            return D("0.1")
        await asyncio.sleep(5)
        return D("0")

    async def reconcile(leg):
        return None  # venue cannot say

    r = await make(ioc, reconcile=reconcile, delay_ms=50).execute(LEG1, LEG2)
    assert r.state == LegState.UNWIND_UNCERTAIN
    assert "unknown" in r.note
    assert r.unwound_leg1 == D("0.1")  # leg1 still flattened


async def test_unwind_failure_flags_uncertain():
    async def ioc(leg):
        return D("0.1") if leg is LEG1 else D("0")

    async def market(exchange, symbol, side, qty):
        raise ConnectionError("venue down")

    r = await make(ioc, market).execute(LEG1, LEG2)
    assert r.state == LegState.UNWIND_UNCERTAIN
    assert "leg1 unwind failed" in r.note


@pytest.mark.asyncio
async def test_f17_nan_fill_flags_uncertain_not_crash():
    async def nan_ioc(leg):
        return "NaN"
    coord = make(place_ioc=nan_ioc)
    r = await coord.execute(LEG1, LEG2)
    assert r.state is LegState.UNWIND_UNCERTAIN
    assert "manual check" in r.note


@pytest.mark.asyncio
async def test_f17_infinite_leg2_fill_never_fakes_clean_unwound():
    calls = {"n": 0}

    async def ioc(leg):
        calls["n"] += 1
        return D("0.1") if calls["n"] == 1 else "Infinity"

    flattened = []

    async def market(exchange, symbol, side, qty):
        flattened.append((exchange, side, qty))
        return D("0")

    coord = make(place_ioc=ioc, place_market=market)
    r = await coord.execute(LEG1, LEG2)
    assert r.state is LegState.UNWIND_UNCERTAIN
    assert flattened  # leg1 was flattened
    assert "both legs flattened" not in r.note


@pytest.mark.asyncio
async def test_f17_reconciliation_nan_is_unknown_not_crash():
    """F17 residual: NaN from reconcile_fills on the leg2 timeout path."""
    calls = {"n": 0}

    async def ioc(leg):
        calls["n"] += 1
        if calls["n"] == 1:
            return D("0.1")
        await asyncio.sleep(0.2)  # leg2 times out
        return D("0.1")

    async def market(exchange, symbol, side, qty):
        return D("0")

    coord = AtomicLegCoordinator(
        live_trading_enabled=True,
        place_ioc=ioc,
        place_market=market,
        max_leg_delay_ms=50,
        reconcile_fills=lambda leg: _ret(D("NaN")),
    )
    r = await coord.execute(LEG1, LEG2)
    assert r.state is LegState.UNWIND_UNCERTAIN


@pytest.mark.asyncio
async def test_f17_leg1_reconcile_nan_flags_uncertain():
    async def boom(leg):
        raise ConnectionError("venue down")

    coord = AtomicLegCoordinator(
        live_trading_enabled=True,
        place_ioc=boom,
        place_market=lambda *a: _ret(D("0")),
        max_leg_delay_ms=50,
        reconcile_fills=lambda leg: _ret(D("NaN")),
    )
    r = await coord.execute(LEG1, LEG2)
    assert r.state is LegState.UNWIND_UNCERTAIN


@pytest.mark.asyncio
async def test_f55_failed_reconcile_still_yields_unwind_uncertain():
    """F55: order fails AND reconcile_fills raises -> the result must be
    UNWIND_UNCERTAIN ('manual check required'), never a raw exception."""

    async def boom_ioc(leg):
        raise ConnectionError("venue down")

    async def boom_reconcile(leg):
        raise ConnectionError("reconcile down")

    coord = make(boom_ioc, reconcile=boom_reconcile)
    result = await coord.execute(LEG1, LEG2)  # no raise
    assert result.state == LegState.UNWIND_UNCERTAIN
    assert "manual check required" in result.note


@pytest.mark.asyncio
async def test_f55_reconcile_garbage_string_is_unknown():
    async def ok_ioc(leg):
        return leg.qty_base

    async def garbage_reconcile(leg):
        return "not-a-number"

    coord = make(ok_ioc, reconcile=garbage_reconcile)

    async def timeout_ioc(leg):
        raise asyncio.TimeoutError()

    coord2 = make(timeout_ioc, reconcile=garbage_reconcile)
    result = await coord2.execute(LEG1, LEG2)  # no raise from D()
    assert result.state == LegState.UNWIND_UNCERTAIN
