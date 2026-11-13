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
    def funding_history(self, exchange, symbol, limit=500, interval_h=1):
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
        spot_ex="kraken",
        notional=Decimal("3000"),
        uta_state=UtaAccountState(
            equity=Decimal("10000"), utilization=Decimal("0.9"),
            collateral_ratio=Decimal("0.95"), sim_mmr_rate=Decimal("0.04"),
        ),
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


def _eth_ctx(settings):
    ctx = _ctx(settings)
    ctx.books["kraken:ETH/USD"] = _book("kraken", "ETH/USD")
    ctx.books["okx:ETH/USD:USD-310404"] = _book("okx", "ETH/USD:USD-310404")
    return ctx


async def test_multi_base_resolves_matching_spot_book():
    """An ETH funding candidate pairs with the ETH/USD spot book, not BTC's."""
    settings = Settings()
    ctx = _eth_ctx(settings)
    rate = _rate("okx", "ETH/USD:USD-310404")
    carry = await handle_funding_candidate(ctx, rate, time.monotonic_ns())
    assert carry is not None
    assert carry.spot_key == "kraken:ETH/USD"
    assert carry.perp_key == "okx:ETH/USD:USD-310404"


async def test_unknown_base_skipped():
    settings = Settings()
    ctx = _ctx(settings)
    rate = _rate("okx", "DOGE/USD:USD-310404")  # not in configured spot symbols
    assert await handle_funding_candidate(ctx, rate, time.monotonic_ns()) is None


async def test_ineligible_perp_skipped():
    settings = Settings()
    ctx = _ctx(settings)
    ctx.eligible = {"okx:BTC/USD:USD-310404"}  # krakenfutures not eligible
    rate = _rate("krakenfutures", "BTC/USD:USD")
    assert await handle_funding_candidate(ctx, rate, time.monotonic_ns()) is None


async def test_portfolio_budget_blocks_overdeployment():
    settings = Settings()
    ctx = _ctx(settings)
    # budget = 10000 * 0.9 = 9000; one 9000 carry already open
    ctx.open_carries["kraken:BTC/USD|okx:BTC/USD:USD-310404"] = SimpleNamespace(
        opp=SimpleNamespace(notional=Decimal("9000"))
    )
    ctx.notional = Decimal("9000")
    rate = _rate("krakenfutures", "BTC/USD:USD")
    assert await handle_funding_candidate(ctx, rate, time.monotonic_ns()) is None


@pytest.mark.asyncio
async def test_breaker_rate_limit_blocks_entry_before_execution():
    """Fix: with window=1 the 2-leg reservation fails and NOTHING opens."""
    settings = Settings(
        min_net_profit_usd=Decimal("0.01"), min_funding_history=10,
    )
    ctx = _ctx(settings)
    ctx.breaker = CircuitBreaker(
        max_consecutive_failures=3, max_daily_loss_usd=Decimal("25"),
        max_orders_per_window=1, window_seconds=60,
    )
    now = time.monotonic_ns()
    c = await handle_funding_candidate(ctx, _rate("krakenfutures", "BTC/USD:USD"), now)
    assert c is None
    assert ctx.paper.opened == 0
    assert ctx.breaker.halted


@pytest.mark.asyncio
async def test_carry_budget_counts_perp_perp_allocation():
    """Fix: the shared portfolio budget subtracts PP usage from carries."""
    settings = Settings(
        min_net_profit_usd=Decimal("0.01"), min_funding_history=10,
    )
    ctx = _ctx(settings)
    # UTA budget 9000; PP already uses 7000 -> 3000-carry no longer fits
    ctx.external_allocated = lambda: Decimal("7000")
    now = time.monotonic_ns()
    c = await handle_funding_candidate(ctx, _rate("krakenfutures", "BTC/USD:USD"), now)
    assert c is None
    assert ctx.paper.opened == 0


@pytest.mark.asyncio
async def test_blacklisted_key_never_opens():
    """Fix: the blacklist guards the entry path itself, not just the set."""
    settings = Settings(
        min_net_profit_usd=Decimal("0.01"), min_funding_history=10,
        screener_blacklist="krakenfutures:BTC/USD:USD",
    )
    ctx = _ctx(settings)
    now = time.monotonic_ns()
    c = await handle_funding_candidate(ctx, _rate("krakenfutures", "BTC/USD:USD"), now)
    assert c is None
    assert ctx.paper.opened == 0


@pytest.mark.asyncio
async def test_huge_spread_candidate_rejected():
    """F06: bid=1/ask=100 books are a false signal, not an opportunity."""
    settings = Settings(
        min_net_profit_usd=Decimal("0.01"), min_funding_history=10,
    )
    ctx = _ctx(settings)
    wide = _book("kraken", "BTC/USD")
    object.__setattr__(wide, "bids", wide.bids.__class__((type(wide.bids[0])(price="1", qty="500"),)))
    object.__setattr__(wide, "asks", wide.asks.__class__((type(wide.asks[0])(price="100", qty="500"),)))
    ctx.books["kraken:BTC/USD"] = wide
    now = time.monotonic_ns()
    c = await handle_funding_candidate(ctx, _rate("krakenfutures", "BTC/USD:USD"), now)
    assert c is None
    assert ctx.paper.opened == 0


def test_sources_disagree():
    from app.engines.candidates import sources_disagree
    assert sources_disagree(Decimal("-0.0002"), Decimal("0.0001"))
    assert sources_disagree(Decimal("0.0002"), Decimal("-0.0001"))
    assert sources_disagree(Decimal("0.001"), Decimal("0.0002"))
    assert not sources_disagree(Decimal("0.00010"), Decimal("0.00012"))
    assert not sources_disagree(Decimal("0"), Decimal("0.001"))


@pytest.mark.asyncio
async def test_source_mismatch_quarantines_entry():
    settings = Settings(
        spot_exchange_list=["kraken"], perp_exchange_list=["krakenfutures"],
        min_net_profit_usd=Decimal("0.01"), min_funding_history=10,
    )
    ctx = _ctx(settings)
    ctx.screener_means = {"krakenfutures:BTC/USD:USD": Decimal("-0.0002")}
    r = _rate("krakenfutures", "BTC/USD:USD")
    assert await handle_funding_candidate(ctx, r, time.monotonic_ns()) is None
    assert ctx.paper.opened == 0


@pytest.mark.asyncio
async def test_corroborating_sources_still_enter():
    settings = Settings(
        spot_exchange_list=["kraken"], perp_exchange_list=["krakenfutures"],
        min_net_profit_usd=Decimal("0.01"), min_funding_history=10,
    )
    ctx = _ctx(settings)
    ctx.screener_means = {"krakenfutures:BTC/USD:USD": Decimal("0.0009")}
    r = _rate("krakenfutures", "BTC/USD:USD")
    assert await handle_funding_candidate(ctx, r, time.monotonic_ns()) is not None
