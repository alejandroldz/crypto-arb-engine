"""Strategy A detector tests: history-gated, pessimistic rate selection."""
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from app.connectors.normalizer import FundingRate
from app.core.decimals import D
from app.engines.arbitrage import detect_funding_carry, funding_rate_stats
from app.engines.validator import funding_payout_per_interval, funding_roundtrip_costs

HISTORY_3BP = [D("0.0003")] * 30  # 30 samples of 0.03%
KW = dict(
    notional=D("1000"),
    holding_intervals=90,
    spot_taker_fee_bps=D("40"),
    perp_taker_fee_bps=D("5"),
    slippage_buffer_bps=D("5"),
    min_net_profit_usd=D("2"),
    historical_rates=HISTORY_3BP,
    min_history_samples=30,
)


def funding(rate: str, predicted: str | None = None) -> FundingRate:
    return FundingRate(
        exchange="deribit",
        symbol="BTC-PERPETUAL",
        rate=rate,
        predicted_rate=predicted,
        local_ts_ns=1,
    )


def test_stats_known_values():
    stats = funding_rate_stats([D("0.0001"), D("0.0002"), D("0.0003"), D("0.001")])
    assert stats.samples == 4
    assert stats.mean == D("0.0004")
    assert stats.median == D("0.0002")  # nearest-rank p50
    assert stats.p25 == D("0.0001")


def test_stats_empty():
    assert funding_rate_stats([]) is None


def test_detects_profitable_carry():
    # effective rate 0.0003, costs 11 USD, payout 0.3/interval * 90 = 27 -> net 16
    opp = detect_funding_carry(funding("0.0003"), **KW)
    assert opp is not None
    assert opp.funding_rate == D("0.0003")
    assert opp.projected_net == D("27") - D("11")


def test_no_history_no_trade():
    kw = dict(KW, historical_rates=[])
    assert detect_funding_carry(funding("0.0003"), **kw) is None
    kw2 = dict(KW, historical_rates=[D("0.001")] * 29)
    assert detect_funding_carry(funding("0.0003"), **kw2) is None


def test_spike_rejected_by_pessimistic_percentile():
    # current rate is a 10x spike; history p25 is 0.0003 -> effective 0.0003
    opp = detect_funding_carry(funding("0.003"), **KW)
    assert opp is not None and opp.funding_rate == D("0.0003")
    # and if history says 0 while the spike is live, no trade
    kw = dict(KW, historical_rates=[D("0")] * 30)
    assert detect_funding_carry(funding("0.003"), **kw) is None


def test_rejects_nonpositive_rate():
    assert detect_funding_carry(funding("0"), **KW) is None
    assert detect_funding_carry(funding("-0.0002"), **KW) is None
    assert detect_funding_carry(funding("0.0003", predicted="-0.0001"), **KW) is None


def test_rejects_when_net_below_minimum():
    kw = dict(KW, min_net_profit_usd=D("1000"))
    assert detect_funding_carry(funding("0.0003"), **kw) is None


def test_rejects_when_breakeven_beyond_holding():
    kw = dict(KW, holding_intervals=10)  # breakeven is 37 intervals
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
        spot_taker_fee_bps=D("40"),
        perp_taker_fee_bps=D("5"),
        slippage_buffer_bps=D("5"),
    )
    assert be is not None and be >= 1
    payout = funding_payout_per_interval(D(notional), D(rate))
    costs = funding_roundtrip_costs(
        notional=D(notional), spot_taker_fee_bps=D("40"),
        perp_taker_fee_bps=D("5"), slippage_buffer_bps=D("5"),
    )
    assert payout * be >= costs
