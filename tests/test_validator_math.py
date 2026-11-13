"""Unit tests for the deterministic funding-carry math validator (RISK 4)."""
from app.core.decimals import D
from app.engines.validator import (
    approve_trade,
    funding_breakeven_intervals,
    funding_net_pnl,
    funding_payout_per_interval,
    funding_roundtrip_costs,
)


def test_funding_math():
    # 1000 notional, 0.01% funding per 8h, 10 bps taker, 5 bps buffer
    payout = funding_payout_per_interval(D("1000"), D("0.0001"))
    assert payout == D("0.1")
    costs = funding_roundtrip_costs(
        notional=D("1000"), taker_fee_bps=D("10"), slippage_buffer_bps=D("5")
    )
    # turnover 4000 -> fees 4 + buffer 2
    assert costs == D("6")
    be = funding_breakeven_intervals(
        notional=D("1000"),
        funding_rate=D("0.0001"),
        taker_fee_bps=D("10"),
        slippage_buffer_bps=D("5"),
    )
    assert be == 60  # 6 / 0.1
    pnl = funding_net_pnl(
        notional=D("1000"),
        funding_rate=D("0.0001"),
        intervals=90,
        taker_fee_bps=D("10"),
        slippage_buffer_bps=D("5"),
    )
    assert pnl == D("9") - D("6")


def test_funding_breakeven_none_on_negative_rate():
    assert (
        funding_breakeven_intervals(
            notional=D("1000"),
            funding_rate=D("-0.0001"),
            taker_fee_bps=D("10"),
            slippage_buffer_bps=D("5"),
        )
        is None
    )


def test_approve_trade_strictly_greater():
    assert not approve_trade(D("5"), D("5")).approved  # equal is not enough
    assert approve_trade(D("5.01"), D("5")).approved
    assert not approve_trade(D("-1"), D("5")).approved
