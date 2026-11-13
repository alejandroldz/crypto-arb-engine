"""Risk gates: staleness (RISK 3), liquidation (RISK 5), funding flip (RISK 4)."""
from app.connectors.normalizer import L2OrderBook, OrderBookLevel
from app.core.decimals import D
from app.engines.risk import (
    CarryPositionRisk,
    StalenessGate,
    approaching_liquidation,
    funding_flip_should_unwind,
    leverage_ok,
    short_liquidation_price,
)


def book(ts_ns: int) -> L2OrderBook:
    return L2OrderBook(
        exchange="test",
        symbol="BTC/EUR",
        bids=(OrderBookLevel(price="99", qty="1"),),
        asks=(OrderBookLevel(price="100", qty="1"),),
        local_ts_ns=ts_ns,
    )


def test_staleness_gate():
    gate = StalenessGate(max_age_ms=2000)
    base = 10_000_000_000
    assert gate.is_fresh(book(base), base + 1_500_000_000)  # 1500ms old
    assert not gate.is_fresh(book(base), base + 2_500_000_000)  # 2500ms old


def test_short_liquidation_price():
    assert short_liquidation_price(D("100"), D("2")) == D("150")
    assert short_liquidation_price(D("100"), D("1")) == D("200")


def test_approaching_liquidation_buffer():
    liq = D("150")
    assert approaching_liquidation(D("130"), liq, D("15"))  # threshold 127.5
    assert approaching_liquidation(D("127.5"), liq, D("15"))
    assert not approaching_liquidation(D("100"), liq, D("15"))


def test_carry_position_must_close():
    pos = CarryPositionRisk.build(D("100"), D("2"))
    assert pos.liquidation_price == D("150")
    assert pos.must_close(D("140"), D("15"))
    assert not pos.must_close(D("110"), D("15"))


def test_funding_flip():
    assert funding_flip_should_unwind(D("-0.0001"))
    assert not funding_flip_should_unwind(D("0.0001"))


def test_leverage_bounds():
    assert leverage_ok(D("1"), D("2"))
    assert leverage_ok(D("2"), D("2"))
    assert not leverage_ok(D("3"), D("2"))
    assert not leverage_ok(D("0.5"), D("2"))
