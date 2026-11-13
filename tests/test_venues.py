"""Venue wiring: default exchange lists, per-venue fees, bybit support."""
from app.core.config import Settings


def test_bybit_supported_but_not_in_default_pair():
    s = Settings()
    # bybit stays in fee config and code, but the default principal pair is
    # krakenfutures + okx (user decision: legal/EU venues)
    assert "bybit" not in s.perp_exchange_list[:2]


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


def test_okx_is_principal_second_venue():
    s = Settings()
    assert s.perp_exchange_list[:2] == ["krakenfutures", "okx"]
    i = s.perp_exchange_list.index("okx")
    # OKX EU X-Perp: linear USD-margined 5y expiry future with 8h funding
    # (BTC-USD_UM_XPERP-310404), not the global inverse swap BTC/USD:BTC
    assert s.perp_symbol_list[i] == "BTC/USD:USD-310404"
    assert s.taker_fee_for("okx") == 5


def test_bybit_still_supported_not_principal():
    s = Settings()
    assert "bybit" in s.taker_fee_bps_map  # fee config stays
    assert "bybit" not in s.perp_exchange_list[:2]  # but not the principal pair
