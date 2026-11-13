"""Unit tests for the deterministic funding-carry math validator (RISK 4)."""
from app.core.decimals import D
from app.engines.validator import (
    approve_trade,
    funding_breakeven_intervals,
    funding_net_pnl,
    funding_payout_per_interval,
    funding_roundtrip_costs,
)

SPOT_FEE = D("40")  # kraken spot taker, worst tier
PERP_FEE = D("5")  # krakenfutures/okx taker
BUFFER = D("5")


def test_funding_math_per_venue_fees():
    payout = funding_payout_per_interval(D("1000"), D("0.0001"))
    assert payout == D("0.1")
    costs = funding_roundtrip_costs(
        notional=D("1000"),
        spot_taker_fee_bps=SPOT_FEE,
        perp_taker_fee_bps=PERP_FEE,
        slippage_buffer_bps=BUFFER,
    )
    # spot turnover 2000 * 40bps = 8 ; perp turnover 2000 * 5bps = 1 ; buffer 4000 * 5bps = 2
    assert costs == D("11")
    be = funding_breakeven_intervals(
        notional=D("1000"),
        funding_rate=D("0.0001"),
        spot_taker_fee_bps=SPOT_FEE,
        perp_taker_fee_bps=PERP_FEE,
        slippage_buffer_bps=BUFFER,
    )
    assert be == 110  # 11 / 0.1
    pnl = funding_net_pnl(
        notional=D("1000"),
        funding_rate=D("0.0001"),
        intervals=150,
        spot_taker_fee_bps=SPOT_FEE,
        perp_taker_fee_bps=PERP_FEE,
        slippage_buffer_bps=BUFFER,
    )
    assert pnl == D("15") - D("11")


def test_spot_fees_dominate_economics():
    # same rate, cheap-venue fantasy vs real kraken fees: breaks the trade
    optimistic = funding_roundtrip_costs(
        notional=D("1000"), spot_taker_fee_bps=D("5"),
        perp_taker_fee_bps=D("5"), slippage_buffer_bps=D("0"),
    )
    realistic = funding_roundtrip_costs(
        notional=D("1000"), spot_taker_fee_bps=SPOT_FEE,
        perp_taker_fee_bps=PERP_FEE, slippage_buffer_bps=D("0"),
    )
    assert realistic == D("9") and optimistic == D("2")


def test_funding_breakeven_none_on_negative_rate():
    assert (
        funding_breakeven_intervals(
            notional=D("1000"),
            funding_rate=D("-0.0001"),
            spot_taker_fee_bps=SPOT_FEE,
            perp_taker_fee_bps=PERP_FEE,
            slippage_buffer_bps=BUFFER,
        )
        is None
    )


def test_approve_trade_strictly_greater():
    assert not approve_trade(D("5"), D("5")).approved
    assert approve_trade(D("5.01"), D("5")).approved
    assert not approve_trade(D("-1"), D("5")).approved


def test_approve_trade_rejects_non_finite():
    a = approve_trade("Infinity", "5")
    assert not a.approved and "non-finite" in a.reason
    a = approve_trade("NaN", "5")
    assert not a.approved  # antes: InvalidOperation cruda
    a = approve_trade("10", "Infinity")
    assert not a.approved
