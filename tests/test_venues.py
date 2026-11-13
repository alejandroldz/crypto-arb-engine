"""Venue wiring: default exchange lists, per-venue fees, bybit support."""
from app.core.config import Settings


def test_bybit_is_a_default_perp_venue():
    s = Settings()
    assert "bybit" in s.perp_exchange_list
    i = s.perp_exchange_list.index("bybit")
    assert s.perp_symbol_list[i] == "BTC/USD:BTC"  # inverse perp, USD-quoted


def test_per_venue_taker_fees():
    s = Settings()
    assert s.taker_fee_for("kraken") == 40
    assert s.taker_fee_for("krakenfutures") == 5
    assert s.taker_fee_for("deribit") == 5
    assert s.taker_fee_for("bybit") == s.taker_fee_bps_map["bybit"]


def test_collateral_ratio_map_and_unknown_asset():
    s = Settings()
    assert s.collateral_ratio_for("BTC") == s.collateral_ratio_map["BTC"]
    assert s.collateral_ratio_for("btc") == s.collateral_ratio_map["BTC"]
    assert s.collateral_ratio_for("DOGE") == 0  # unknown -> no collateral value
