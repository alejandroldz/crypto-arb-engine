"""UTA / cross-collateral portfolio margin tests (user spec section 3)."""
import time
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from app.connectors.normalizer import L2OrderBook, OrderBookLevel
from app.core.decimals import D
from app.engines.monitor import MonitorAction, evaluate_carry
from app.engines.risk import CarryPositionRisk, UtaAccountState
from app.engines.validator import approve_unified_carry, uta_allocation

EMERGENCY = D("0.75")
WARNING = D("0.65")
BTC = UtaAccountState(
    equity=D("1000"), utilization=D("0.90"),
    collateral_ratio=D("0.95"), sim_mmr_rate=D("0.25"),
)
SOL = UtaAccountState(
    equity=D("1000"), utilization=D("0.90"),
    collateral_ratio=D("0.85"), sim_mmr_rate=D("0.25"),
)


def book(price: str) -> L2OrderBook:
    p = D(price)
    return L2OrderBook(
        exchange="krakenfutures", symbol="BTC/USD:USD",
        bids=(OrderBookLevel(price=str(p - 1), qty="50"),),
        asks=(OrderBookLevel(price=str(p), qty="50"),),
        local_ts_ns=time.monotonic_ns(),
    )


def test_capital_efficiency_90_vs_50():
    notional, buffer = uta_allocation(D("1000"), D("0.90"))
    assert notional == D("900") and buffer == D("100")
    # ISOLATED_SPLIT deploys equity/2 per leg: 500 vs unified 900
    assert notional == D("1000") / 2 * D("1.8")  # 80% more funding yield


def test_allocation_defaults_match_spec():
    n, b = uta_allocation(D("1000"), D("0.90"))
    assert n + b == D("1000")


def test_haircut_stress_100pct_pump_safe_for_btc_and_sol():
    assert BTC.account_mmr(D("1")) < WARNING  # +100% pump: ~49.5%
    assert SOL.account_mmr(D("1")) < WARNING  # ~61.6%
    # an asset with zero collateral value is rejected BEFORE entry
    gate = approve_unified_carry(
        collateral_ratio=D("0.00"),
        min_collateral_ratio=D("0.80"),
        max_safe_pump_pct=None,
    )
    assert not gate.approved and "collateral ratio" in gate.reason


def test_emergency_mmr_unwind_on_200pct_surge():
    assert BTC.account_mmr(D("2")) >= EMERGENCY  # ~78.0%
    decision = evaluate_carry(
        qty_base=D("0.01"),
        perp_entry_vwap=D("50000"),
        last_payment_ts=None,
        rate=None,
        perp_book=book("150000"),  # +200%
        risk=CarryPositionRisk.build(D("50000"), D("1")),
        liquidation_buffer_pct=D("15"),
        uta=BTC,
        mmr_emergency=EMERGENCY,
    )
    assert decision.action is MonitorAction.UNWIND
    assert decision.reason == "account mmr"
    assert decision.account_mmr >= EMERGENCY


def test_max_safe_pump_matches_emergency_crossing():
    pump = BTC.max_safe_pump_pct(EMERGENCY)
    assert pump is not None and D("1.8") < pump < D("2.0")  # ~190%
    # at the computed pump the MMR crosses the emergency threshold
    assert abs(BTC.account_mmr(pump) - EMERGENCY) < D("0.0001")
    # BTC comfortably survives the required +80%
    gate = approve_unified_carry(
        collateral_ratio=D("0.95"),
        min_collateral_ratio=D("0.80"),
        max_safe_pump_pct=pump,
    )
    assert gate.approved


def test_below_warning_is_quiet():
    decision = evaluate_carry(
        qty_base=D("0.01"),
        perp_entry_vwap=D("50000"),
        last_payment_ts=None,
        rate=None,
        perp_book=book("50010"),
        risk=CarryPositionRisk.build(D("50000"), D("1")),
        liquidation_buffer_pct=D("15"),
        uta=BTC,
        mmr_emergency=EMERGENCY,
    )
    assert decision.action is MonitorAction.NONE
    assert decision.account_mmr is not None and decision.account_mmr < WARNING


@given(
    c=st.decimals(min_value=Decimal("0.8"), max_value=Decimal("1"), places=2).map(str),
    dp1=st.decimals(min_value=Decimal("0"), max_value=Decimal("2.5"), places=2).map(str),
    dp2=st.decimals(min_value=Decimal("0"), max_value=Decimal("2.5"), places=2).map(str),
)
def test_mmr_monotone_in_pump(c: str, dp1: str, dp2: str):
    state = UtaAccountState(
        equity=D("1000"), utilization=D("0.90"),
        collateral_ratio=D(c), sim_mmr_rate=D("0.25"),
    )
    lo, hi = (D(dp1), D(dp2)) if D(dp1) <= D(dp2) else (D(dp2), D(dp1))
    assert state.account_mmr(lo) <= state.account_mmr(hi)
