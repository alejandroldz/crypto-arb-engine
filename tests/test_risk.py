"""Risk gates: staleness (RISK 3), liquidation (RISK 5), funding flip (RISK 4)."""
import pytest
from app.connectors.normalizer import L2OrderBook, OrderBookLevel
from app.core.decimals import D
from app.engines.risk import (
    capital_efficiency,
    isolated_max_pump_pct,
    CarryPositionRisk,
    StalenessGate,
    UtaAccountState,
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


def test_capital_efficiency_levels():
    assert capital_efficiency(D("1")) == D("0.5")
    assert capital_efficiency(D("3")) == D("0.75")
    assert capital_efficiency(D("5")) == D("5") / D("6")
    # monotonic and bounded
    assert capital_efficiency(D("10")) > capital_efficiency(D("5"))
    assert capital_efficiency(D("10")) < D("1")


def test_isolated_max_pump():
    assert isolated_max_pump_pct(D("1")) == D("1")
    assert isolated_max_pump_pct(D("2")) == D("0.5")
    assert isolated_max_pump_pct(D("5")) == D("0.2")
    # only 1x survives the validator's +80% rule in isolated mode
    assert isolated_max_pump_pct(D("1")) >= D("0.80")
    assert isolated_max_pump_pct(D("3")) < D("0.80")


def test_uta_margin_locked_scales_with_leverage():
    base = UtaAccountState(
        equity=D("10000"), utilization=D("0.90"),
        collateral_ratio=D("0.95"), sim_mmr_rate=D("0.25"),
    )
    lev3 = UtaAccountState(
        equity=D("10000"), utilization=D("0.90"),
        collateral_ratio=D("0.95"), sim_mmr_rate=D("0.25"), leverage=D("3"),
    )
    assert base.margin_locked == D("9000")
    assert lev3.margin_locked == D("3000")


def test_short_leverage_config_bounds():
    from app.core.config import Settings
    st = Settings(short_leverage_map="krakenfutures:5,okx:1")
    assert st.short_leverage_for("krakenfutures") == D("5")
    assert st.short_leverage_for("okx") == D("1")
    assert st.short_leverage_for("bybit") == D("1")  # default
    with pytest.raises(Exception):
        Settings(short_leverage_map="okx:20")  # beyond EU cap
    with pytest.raises(Exception):
        Settings(short_leverage_map="okx:0.5")  # below 1x


def test_survival_rule_rejects_unsafe_levels():
    from app.core.config import Settings
    st = Settings()
    # krakenfutures unified: configured 5x must pass the +80% account rule
    assert st.short_leverage_for("krakenfutures") == D("5")
    # okx treated as isolated: only 1x survives +80%
    assert isolated_max_pump_pct(st.short_leverage_for("okx")) >= D("0.80")
    assert isolated_max_pump_pct(D("3")) < D("0.80")
