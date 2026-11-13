"""Multi-candidate regression for the funding-carry decision flow.

The original bug: main.py shadowed the StalenessGate with the UTA approval
result, so the SECOND candidate in a batch crashed on gate.is_fresh().
Driving two candidates through handle_funding_candidate covers the real
behavior (the AST guard in test_gate_shadowing.py covers main.py itself).
"""
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.connectors.normalizer import FundingRate, L2OrderBook, OrderBookLevel
from app.core.circuit_breaker import CircuitBreaker
from app.core.config import Settings
from app.core.decimals import D
from app.engines.candidates import CarryContext, handle_funding_candidate
from app.engines.risk import StalenessGate, UtaAccountState


class FakeTicks:
    def funding_history(self, exchange, symbol, limit=500):
        return [Decimal("0.001")] * 50  # p25 blend keeps the full rate


class FakePaper:
    def __init__(self):
        self.opened = 0

    async def open_carry(self, opp, fetch, spot_symbol=None):
        self.opened += 1
        return SimpleNamespace(
            perp_vwap=Decimal("69000"),
            qty_base=Decimal("0.1"),
        )


class FakeNotifier:
    async def send(self, msg):
        pass


class FakeConsole:
    def print(self, *a, **k):
        pass


def _book(exchange, symbol):
    lvl = OrderBookLevel(price=Decimal("69000"), qty=Decimal("1"))
    return L2OrderBook(
        exchange=exchange, symbol=symbol, bids=(lvl,), asks=(lvl,),
        local_ts_ns=time.monotonic_ns(),
    )


def _rate(exchange, symbol, rate="0.001"):
    return FundingRate(
        exchange=exchange, symbol=symbol, rate=Decimal(rate),
        local_ts_ns=time.monotonic_ns(),
    )


def _ctx(settings):
    return CarryContext(
        settings=settings,
        breaker=CircuitBreaker(
            max_consecutive_failures=3, max_daily_loss_usd=Decimal("25"),
            max_orders_per_window=50, window_seconds=60,
        ),
        gate=StalenessGate(2000),
        paper=FakePaper(),
        ticks=FakeTicks(),
        console=FakeConsole(),
        notifier=FakeNotifier(),
        books={
            "kraken:BTC/USD": _book("kraken", "BTC/USD"),
            "krakenfutures:BTC/USD:USD": _book("krakenfutures", "BTC/USD:USD"),
            "okx:BTC/USD:USD-310404": _book("okx", "BTC/USD:USD-310404"),
        },
        opportunities=[],
        open_carries={},
        spot_key_default="kraken:BTC/USD",
        spot_symbol="BTC/USD",
        spot_ex="kraken",
        notional=Decimal("9000"),
        uta_state=UtaAccountState(
            equity=Decimal("10000"), utilization=Decimal("0.9"),
            collateral_ratio=Decimal("0.95"), sim_mmr_rate=Decimal("0.04"),
        ),
        coll_ratio=Decimal("0.95"),
        max_safe_pump=Decimal("2"),
    )


@pytest.mark.asyncio
async def test_two_candidates_in_sequence_both_processed():
    settings = Settings(
        min_net_profit_usd=Decimal("0.01"), min_funding_history=10,
    )
    ctx = _ctx(settings)
    now = time.monotonic_ns()
    # candidate 1 opens a carry; candidate 2 (different venue) must not crash
    # and must also open - this is the path the gate-shadowing bug broke
    c1 = await handle_funding_candidate(ctx, _rate("krakenfutures", "BTC/USD:USD"), now)
    c2 = await handle_funding_candidate(ctx, _rate("okx", "BTC/USD:USD-310404"), now)
    assert c1 is not None
    assert c2 is not None
    assert ctx.paper.opened == 2
    assert len(ctx.open_carries) == 2


@pytest.mark.asyncio
async def test_duplicate_candidate_is_skipped():
    settings = Settings(
        min_net_profit_usd=Decimal("0.01"), min_funding_history=10,
    )
    ctx = _ctx(settings)
    now = time.monotonic_ns()
    rate = _rate("krakenfutures", "BTC/USD:USD")
    assert await handle_funding_candidate(ctx, rate, now) is not None
    assert await handle_funding_candidate(ctx, rate, now) is None  # same pkey
    assert ctx.paper.opened == 1


@pytest.mark.asyncio
async def test_stale_candidate_is_skipped():
    settings = Settings(
        min_net_profit_usd=Decimal("0.01"), min_funding_history=10,
    )
    ctx = _ctx(settings)
    stale = _rate("krakenfutures", "BTC/USD:USD")
    object.__setattr__(stale, "local_ts_ns", time.monotonic_ns() - 10_000_000_000)
    assert await handle_funding_candidate(ctx, stale, time.monotonic_ns()) is None
    assert ctx.paper.opened == 0
