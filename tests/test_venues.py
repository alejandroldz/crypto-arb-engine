"""Venue wiring: default exchange lists, per-venue fees, bybit support."""
from app.core.config import Settings


def test_bybit_supported_but_not_in_default_pair():
    s = Settings()
    # bybit stays in fee config and code, but the default principal pair is
    # krakenfutures + okx (user decision: legal/EU venues)
    assert "bybit" not in s.perp_exchange_list[:2]


def test_per_venue_taker_fees():
    s = Settings()
    assert s.taker_fee_for("kraken") == 80  # kraken pro spot tier 1 real (2026-11)
    assert s.taker_fee_for("krakenfutures") == 5
    assert s.taker_fee_for("bybit") == s.taker_fee_bps_map["bybit"]


def test_collateral_ratio_map_and_unknown_asset():
    s = Settings()
    assert s.collateral_ratio_for("BTC") == s.collateral_ratio_map["BTC"]
    assert s.collateral_ratio_for("btc") == s.collateral_ratio_map["BTC"]
    assert s.collateral_ratio_for("DOGE") == 0  # unknown -> no collateral value


def test_okx_is_principal_second_venue():
    s = Settings()
    assert s.perp_exchange_list[:2] == ["krakenfutures", "okx"]
    # OKX EU X-Perp: linear USD-margined 5y expiry future with 8h funding
    # (BTC-USD_UM_XPERP-310404), not the global inverse swap BTC/USD:BTC
    assert "BTC/USD:USD-310404" in s.perp_symbols_for("okx")
    # SOL only on OKX: its Kraken Futures funding history is negative
    assert "SOL/USD:USD-310404" in s.perp_symbols_for("okx")
    assert "SOL/USD:USD" not in s.perp_symbols_for("krakenfutures")
    assert s.taker_fee_for("okx") == 5


def test_bybit_still_supported_not_principal():
    s = Settings()
    assert "bybit" in s.taker_fee_bps_map  # fee config stays
    assert "bybit" not in s.perp_exchange_list[:2]  # but not the principal pair


def test_config_maps_reject_infinity_nan_negative():
    import pytest

    from app.core.config import Settings
    with pytest.raises(ValueError, match="taker_fee_bps"):
        Settings(taker_fee_bps="kraken:Infinity").taker_fee_bps_map
    with pytest.raises(ValueError, match="taker_fee_bps"):
        Settings(taker_fee_bps="kraken:-5").taker_fee_bps_map
    with pytest.raises(ValueError, match="maker_fee_bps"):
        Settings(maker_fee_bps="okx:NaN").maker_fee_bps_map
    with pytest.raises(ValueError, match="collateral_haircut_map"):
        Settings(collateral_haircut_map="BTC:1.5").collateral_ratio_map
    with pytest.raises(ValueError, match="funding_interval_hours"):
        Settings(funding_interval_hours="okx:abc").funding_interval_h_for("okx")


def test_malformed_decimal_field_names_value():
    import pytest
    from pydantic import ValidationError

    from app.core.config import Settings
    with pytest.raises(ValidationError) as exc:
        Settings(min_net_profit_usd="abc")
    assert "min_net_profit_usd" in str(exc.value)  # antes: InvalidOperation cruda


def test_target_quote_sizes_empty_or_nonpositive():
    import pytest

    from app.core.config import Settings
    with pytest.raises(ValueError, match="target_quote_sizes"):
        Settings(target_quote_sizes="").target_quote_size_list
    with pytest.raises(ValueError, match="target_quote_sizes"):
        Settings(target_quote_sizes="0,-5").target_quote_size_list


def test_D_bad_string_raises_valueerror():
    import pytest

    from app.core.decimals import D
    for bad in ("", "..", "abc", "0,5", "1 2"):
        with pytest.raises(ValueError):
            D(bad)
    assert D(" 1.5 ") == D("1.5")
