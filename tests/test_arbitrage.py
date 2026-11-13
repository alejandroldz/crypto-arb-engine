"""Strategy A detector tests."""
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from app.connectors.normalizer import FundingRate
from app.core.decimals import D
from app.engines.arbitrage import detect_funding_carry
from app.engines.validator import funding_payout_per_interval, funding_roundtrip_costs

KW = dict(
    notional=D("1000"),
    holding_intervals=90,
    taker_fee_bps=D("10"),
    slippage_buffer_bps=D("5"),
    min_net_profit_usd=D("2"),
)


def funding(rate: str, predicted: str | None = None) -> FundingRate:
    return FundingRate(
        exchange="bybit",
        symbol="BTC/USDT:USDT",
        rate=rate,
        predicted_rate=predicted,
        local_ts_ns=1,
    )


def test_detects_profitable_carry():
    # 0.03% per 8h over 90 intervals: 27 - 6 costs = 21 > 2
    opp = detect_funding_carry(funding("0.0003"), **KW)
    assert opp is not None
    assert opp.funding_rate == D("0.0003")
    assert opp.breakeven_intervals == 20
    assert opp.projected_net == D("27") - D("6")


def test_predicted_rate_wins_over_current():
    opp = detect_funding_carry(funding("0.0003", predicted="0.0004"), **KW)
    assert opp is not None and opp.funding_rate == D("0.0004")


def test_rejects_nonpositive_rate():
    assert detect_funding_carry(funding("0"), **KW) is None
    assert detect_funding_carry(funding("-0.0002"), **KW) is None
    # current positive but predicted flip to negative -> reject
    assert detect_funding_carry(funding("0.0003", predicted="-0.0001"), **KW) is None


def test_rejects_when_net_below_minimum():
    kw = dict(KW, min_net_profit_usd=D("1000"))
    assert detect_funding_carry(funding("0.0003"), **kw) is None


def test_rejects_when_breakeven_beyond_holding():
    kw = dict(KW, holding_intervals=5)  # breakeven is 20 intervals
    assert detect_funding_carry(funding("0.0003"), **kw) is None


@given(
    rate=st.decimals(min_value=Decimal("0.00001"), max_value=Decimal("0.01"), places=8).map(str),
    notional=st.decimals(min_value=Decimal("10"), max_value=Decimal("100000"), places=2).map(str),
)
def test_breakeven_always_recoups_costs(rate: str, notional: str):
    from app.engines.validator import funding_breakeven_intervals

    be = funding_breakeven_intervals(
        notional=D(notional),
        funding_rate=D(rate),
        taker_fee_bps=D("10"),
        slippage_buffer_bps=D("5"),
    )
    assert be is not None and be >= 1
    payout = funding_payout_per_interval(D(notional), D(rate))
    costs = funding_roundtrip_costs(
        notional=D(notional), taker_fee_bps=D("10"), slippage_buffer_bps=D("5")
    )
    assert payout * be >= costs  # breakeven truly covers costs
