"""Screener: cut filters, hysteresis (2-in/3-out), lists, persistence."""
from decimal import Decimal

from app.engines.screener import (
    Hysteresis,
    PerpMetrics,
    eligible_bases,
    from_raw,
    load_state,
    passes_cut,
    roundtrip_cost_bps,
    run_sweep,
    save_state,
)

SPOT = {"BTC", "ETH", "SOL"}
KW = dict(
    min_volume_usd=Decimal("10000000"),
    min_positive_pct=Decimal("0.60"),
    cost_bps_for=lambda ex: Decimal("95"),  # (40 + 5 + 5) * 2 rounded
    spot_bases=SPOT,
    whitelist_keys=set(),
    blacklist_keys=set(),
)


def _m(exchange="okx", symbol="BTC/USD:USD-310404", base="BTC", vol="50000000",
       mean="0.000126", pct="0.95"):
    return PerpMetrics(
        exchange=exchange, symbol=symbol, base=base,
        volume_24h_usd=Decimal(vol),
        funding_mean=None if mean is None else Decimal(mean),
        funding_pct_positive=None if pct is None else Decimal(pct),
    )


def test_passes_cut_happy_path():
    assert passes_cut(
        _m(), min_volume_usd=KW["min_volume_usd"],
        min_positive_pct=KW["min_positive_pct"],
        roundtrip_cost_bps=Decimal("95"), spot_bases=SPOT)


def test_cut_rejects_thin_market():
    assert not passes_cut(
        _m(vol="1000000"), min_volume_usd=KW["min_volume_usd"],
        min_positive_pct=KW["min_positive_pct"],
        roundtrip_cost_bps=Decimal("95"), spot_bases=SPOT)


def test_cut_rejects_no_history():
    assert not passes_cut(
        _m(mean=None, pct=None), min_volume_usd=KW["min_volume_usd"],
        min_positive_pct=KW["min_positive_pct"],
        roundtrip_cost_bps=Decimal("95"), spot_bases=SPOT)


def test_cut_rejects_unstable_funding():
    assert not passes_cut(
        _m(pct="0.50"), min_volume_usd=KW["min_volume_usd"],
        min_positive_pct=KW["min_positive_pct"],
        roundtrip_cost_bps=Decimal("95"), spot_bases=SPOT)


def test_cut_rejects_funding_below_costs():
    # mean 0.0001%/interval * 90 intervals = 0.9bps earned << 95bps costs
    assert not passes_cut(
        _m(mean="0.000001"), min_volume_usd=KW["min_volume_usd"],
        min_positive_pct=KW["min_positive_pct"],
        roundtrip_cost_bps=Decimal("95"), spot_bases=SPOT)


def test_cut_rejects_base_without_spot():
    assert not passes_cut(
        _m(base="DOGE"), min_volume_usd=KW["min_volume_usd"],
        min_positive_pct=KW["min_positive_pct"],
        roundtrip_cost_bps=Decimal("95"), spot_bases=SPOT)


def test_hysteresis_two_in_three_out():
    h = Hysteresis()
    key = "okx:BTC/USD:USD-310404"
    assert h.update(key, True) is False   # 1st pass: not yet
    assert h.update(key, True) is True    # 2nd consecutive: in
    assert h.update(key, False) is True   # 1st fail: still in
    assert h.update(key, False) is True   # 2nd fail: still in
    assert h.update(key, False) is False  # 3rd consecutive fail: out
    # a single pass after exclusion does not re-enter
    assert h.update(key, True) is False


def test_hysteresis_streak_resets_on_flip():
    h = Hysteresis()
    h.update("k", True)
    h.update("k", False)  # resets the pass streak
    assert h.update("k", True) is False  # only 1 consecutive pass again


def test_whitelist_forces_in_blacklist_forces_out():
    h = Hysteresis()
    eligible = run_sweep(
        [_m(base="BTC", vol="1", mean="0", pct="0"),  # fails every filter
         _m(exchange="okx", symbol="ETH/USD:USD-310404", base="ETH")],
        h, **{**KW, "whitelist_keys": {"okx:BTC/USD:USD-310404"},
              "blacklist_keys": {"okx:ETH/USD:USD-310404"}},
    )
    assert "okx:BTC/USD:USD-310404" in eligible      # whitelisted
    assert "okx:ETH/USD:USD-310404" not in eligible  # blacklisted


def test_state_roundtrip(tmp_path):
    h = Hysteresis()
    h.seed("okx:BTC/USD:USD-310404")
    h.update("okx:ETH/USD:USD-310404", True)
    path = str(tmp_path / "state.json")
    save_state(path, h)
    h2 = load_state(path)
    assert h2.to_dict() == h.to_dict()


def test_from_raw_converts_floats():
    class Raw:
        exchange = "okx"
        symbol = "BTC/USD:USD-310404"
        base = "BTC"
        volume_24h_usd = 83616599.0
        funding_mean = 0.000126
        funding_pct_positive = 1.0
        funding_interval_h = 8.0
    m = from_raw(Raw())
    assert isinstance(m.volume_24h_usd, Decimal)
    assert m.funding_mean == Decimal("0.000126")


def test_roundtrip_cost_and_bases():
    assert roundtrip_cost_bps(Decimal("40"), Decimal("5"), Decimal("5")) == Decimal("100")
    keys = {"okx:BTC/USD:USD-310404", "krakenfutures:ETH/USD:USD"}
    assert eligible_bases(keys) == {"BTC", "ETH"}


def test_omission_is_one_fail_event_not_instant_removal():
    h = Hysteresis()
    sweep = lambda: run_sweep([_m()], h, **KW)
    sweep()
    assert sweep() == {"okx:BTC/USD:USD-310404"}  # 2 passes: in
    # connector omits it (low volume pre-filter): ONE omission keeps it
    assert run_sweep([], h, **KW) == {"okx:BTC/USD:USD-310404"}
    assert run_sweep([], h, **KW) == {"okx:BTC/USD:USD-310404"}
    # three consecutive omissions: out
    assert run_sweep([], h, **KW) == set()


def test_pass_omit_pass_does_not_reenter():
    h = Hysteresis()
    run_sweep([_m()], h, **KW)          # pass (streak 1)
    run_sweep([], h, **KW)              # omission resets the streak
    assert run_sweep([_m()], h, **KW) == set()  # 1 pass again: NOT in yet
    assert run_sweep([_m()], h, **KW) == {"okx:BTC/USD:USD-310404"}


def test_whitelisted_key_survives_omission():
    h = Hysteresis()
    kw = dict(KW, whitelist_keys={"okx:BTC/USD:USD-310404"})
    assert run_sweep([], h, **kw) == {"okx:BTC/USD:USD-310404"}


def test_f15_corrupt_state_discarded(tmp_path):
    import json

    from app.engines.screener import load_state
    path = tmp_path / "screener_state.json"
    for content in ("42", "[1,2]", "null", '"str"'):
        path.write_text(content)
        h = load_state(str(path))
        assert h.to_dict() == {}
    path.write_text(json.dumps({
        "okx:BTC/USD:USD-310404": {"in": True, "streak": 5, "last": True},
        "bad": None,
        "garbagekey": {"in": True, "streak": 1, "last": True},
        "okx:ETH": {"streak": "x"},
        "krakenfutures:SOL/USD:USD": {"in": False, "streak": 2, "last": False},
    }))
    h = load_state(str(path))
    state = h.to_dict()
    assert set(state) == {"okx:BTC/USD:USD-310404", "krakenfutures:SOL/USD:USD"}
    # and a sweep over the loaded state never crashes
    assert h.update("okx:BTC/USD:USD-310404", False) in (True, False)
