"""Orchestration monitoring: accrual, missed payments, flips, liquidation."""
from datetime import datetime, timedelta, timezone

from app.connectors.normalizer import FundingRate, L2OrderBook, OrderBookLevel
from app.core.decimals import D
from app.engines.monitor import MonitorAction, evaluate_carry
from app.engines.risk import CarryPositionRisk

T0 = datetime(2026, 11, 13, 8, 0, tzinfo=timezone.utc)
RISK = CarryPositionRisk.build(D("50000"), D("1"))  # liq at 100000


def rate(next_ts, r="0.0003", predicted=None):
    return FundingRate(
        exchange="okx", symbol="BTC/USD:USD-310404", rate=r,
        predicted_rate=predicted, local_ts_ns=1, next_funding_ts=next_ts,
    )


def book(ask: str) -> L2OrderBook:
    return L2OrderBook(
        exchange="okx", symbol="BTC/USD:USD-310404",
        bids=(OrderBookLevel(price="49990", qty="1"),),
        asks=(OrderBookLevel(price=ask, qty="1"),),
        local_ts_ns=1,
    )


KW = dict(qty_base=D("0.01"), perp_entry_vwap=D("50000"), liquidation_buffer_pct=D("15"))


def test_accrue_on_new_payment():
    d = evaluate_carry(
        last_payment_ts=T0, rate=rate(T0 + timedelta(hours=8)),
        perp_book=book("50010"), risk=RISK, **KW,
    )
    assert d.action is MonitorAction.ACCRUE
    assert d.accrual == D("500") * D("0.0003")  # notional 500 * rate
    assert d.new_payment_ts == T0 + timedelta(hours=8)
    assert not d.accrual_uncertain


def test_missed_payments_flag_uncertain():
    d = evaluate_carry(
        last_payment_ts=T0, rate=rate(T0 + timedelta(hours=32)),  # 4 intervals gap
        perp_book=book("50010"), risk=RISK, **KW,
    )
    assert d.action is MonitorAction.ACCRUE
    assert d.accrual_uncertain


def test_restart_unknown_payments_flag_uncertain_no_accrual():
    d = evaluate_carry(
        last_payment_ts=None, rate=rate(T0), perp_book=book("50010"), risk=RISK, **KW,
    )
    assert d.accrual == 0
    assert d.accrual_uncertain
    assert d.new_payment_ts == T0


def test_funding_flip_unwinds_with_accrual():
    d = evaluate_carry(
        last_payment_ts=T0,
        rate=rate(T0 + timedelta(hours=8), r="0.0003", predicted="-0.0005"),
        perp_book=book("50010"), risk=RISK, **KW,
    )
    assert d.action is MonitorAction.UNWIND
    assert d.reason == "funding flip"
    assert d.accrual > 0  # the last payment is still collected


def test_liquidation_buffer_unwinds_immediately():
    # liq 100000, buffer 15% -> threshold 85000; ask 90000 breaches
    d = evaluate_carry(
        last_payment_ts=T0, rate=None, perp_book=book("90000"), risk=RISK, **KW,
    )
    assert d.action is MonitorAction.UNWIND
    assert d.reason == "liquidation buffer"


def test_no_change_is_none():
    d = evaluate_carry(
        last_payment_ts=T0, rate=rate(T0), perp_book=book("50010"), risk=RISK, **KW,
    )
    assert d.action is MonitorAction.NONE
    assert d.accrual == 0


def test_replayed_timestamps_never_accrue_twice():
    """F02: 09:00 -> 08:00 -> 09:00 must not pay three times."""
    t9 = T0 + timedelta(hours=1)
    d1 = evaluate_carry(last_payment_ts=T0, rate=rate(t9), perp_book=book("50010"),
                        risk=RISK, **KW)
    assert d1.action is MonitorAction.ACCRUE
    # regressed timestamp: no accrual, flagged, watermark not rewound
    d2 = evaluate_carry(last_payment_ts=d1.new_payment_ts, rate=rate(T0),
                        perp_book=book("50010"), risk=RISK, **KW)
    assert d2.action is MonitorAction.NONE
    assert d2.accrual == D("0")
    assert d2.new_payment_ts == t9
    assert d2.accrual_uncertain
    # replayed t9: no second accrual
    d3 = evaluate_carry(last_payment_ts=d2.new_payment_ts, rate=rate(t9),
                        perp_book=book("50010"), risk=RISK, **KW)
    assert d3.action is MonitorAction.NONE
    assert d3.accrual == D("0")


def test_negative_accrual_flows_as_cost():
    """F04: a negative payment is a signed accrual, carried into the unwind."""
    d = evaluate_carry(last_payment_ts=T0, rate=rate(T0 + timedelta(hours=1), r="-0.01"),
                       perp_book=book("50010"), risk=RISK, **KW)
    assert d.action is MonitorAction.UNWIND  # negative rate is also a flip
    assert d.accrual == D("500") * D("-0.01")  # the cost travels with the decision


def test_flip_without_timestamp_change_unwinds():
    """F03: rate turns negative mid-interval -> UNWIND even if ts is unchanged."""
    d = evaluate_carry(last_payment_ts=T0, rate=rate(T0, r="-0.01"),
                       perp_book=book("50010"), risk=RISK, **KW)
    assert d.action is MonitorAction.UNWIND
    assert "flip" in d.reason


def test_flip_without_any_timestamp_unwinds():
    d = evaluate_carry(last_payment_ts=None, rate=rate(None, r="-0.01"),
                       perp_book=book("50010"), risk=RISK, **KW)
    assert d.action is MonitorAction.UNWIND


def test_uta_monitor_ignores_isolated_liquidation_formula():
    """Fix: with UTA the isolated-leg buffer must NOT close the carry."""
    from app.engines.risk import UtaAccountState

    uta = UtaAccountState(equity=D("10000"), utilization=D("0.9"),
                          collateral_ratio=D("0.95"), sim_mmr_rate=D("0.04"),
                          leverage=D("5"))
    risk5x = CarryPositionRisk.build(D("50000"), D("5"))  # isolated liq ~+17%
    # price +3%: isolated formula with 15% buffer would close; UTA must not
    d = evaluate_carry(last_payment_ts=T0, rate=None, perp_book=book("51500"),
                       risk=risk5x, uta=uta, **KW)
    assert d.action is MonitorAction.NONE
    # without UTA the isolated formula still applies and closes
    d_iso = evaluate_carry(last_payment_ts=T0, rate=None, perp_book=book("51500"),
                           risk=risk5x, **KW)
    assert d_iso.action is MonitorAction.UNWIND
