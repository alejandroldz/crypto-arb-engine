"""Fee oracle: detection, fallback chain, guardrail."""
import json

import pytest

from app.core.config import Settings
from app.core.decimals import D
from app.engines.fee_oracle import FeeOracle, FeeQuote


def make_oracle(tmp_path, **kw):
    s = Settings(**kw)
    return FeeOracle(s, str(tmp_path / "fees.json")), s


async def fake(maker, taker):
    return D(maker), D(taker)


async def boom():
    raise ConnectionError("api down")


async def test_detect_and_apply(tmp_path):
    oracle, s = make_oracle(tmp_path)
    out = await oracle.refresh({"krakenfutures": lambda: fake("2", "5")})
    assert out["krakenfutures"].source == "api"
    assert s.taker_fee_for("krakenfutures") == D("5")
    assert s.maker_fee_for("krakenfutures") == D("2")
    # detected overrides config fallback
    assert s.entry_fee_for("krakenfutures") == D("2")  # execution_mode=maker
    state = json.loads((tmp_path / "fees.json").read_text())
    assert state["venues"]["krakenfutures"]["taker_bps"] == "5"


async def test_fallback_to_cache(tmp_path):
    oracle, s = make_oracle(tmp_path)
    await oracle.refresh({"okx": lambda: fake("2", "5")})
    out = await oracle.refresh({"okx": boom})
    assert out["okx"].source == "cache"
    assert out["okx"].taker_bps == D("5")


async def test_fallback_to_config_when_no_cache(tmp_path):
    oracle, s = make_oracle(tmp_path)
    out = await oracle.refresh({"kraken": boom})
    assert out["kraken"].source == "config"
    assert out["kraken"].taker_bps == D("80")  # kraken spot config fallback


async def test_guardrail_blocks_on_big_change(tmp_path):
    oracle, s = make_oracle(tmp_path)
    await oracle.refresh({"krakenfutures": lambda: fake("2", "5")})
    out = await oracle.refresh({"krakenfutures": lambda: fake("2", "8")})  # +60%
    assert "krakenfutures" in s.fees_blocked
    state = json.loads((tmp_path / "fees.json").read_text())
    assert "krakenfutures" in state["review_required"]


async def test_small_change_does_not_block(tmp_path):
    oracle, s = make_oracle(tmp_path)
    await oracle.refresh({"krakenfutures": lambda: fake("2", "5")})
    await oracle.refresh({"krakenfutures": lambda: fake("2", "5.5")})  # +10%
    assert not s.fees_blocked


async def test_detected_fees_used_in_economics(tmp_path):
    oracle, s = make_oracle(tmp_path)
    await oracle.refresh({"kraken": lambda: fake("40", "80"), "okx": lambda: fake("2", "5")})
    assert s.taker_fee_for("kraken") == D("80")
    assert s.entry_fee_for("kraken") == D("40")   # maker entry
    assert s.entry_fee_for("okx") == D("2")
"""fuzz F20/F30: fee oracle admission control."""
from decimal import Decimal

import pytest

from app.engines.fee_oracle import FeeQuote, fee_plausible


def test_fee_plausible_bounds():
    assert fee_plausible(Decimal("2"), Decimal("5"))
    assert fee_plausible(Decimal("-1"), Decimal("5"))  # maker rebate
    for bad_m, bad_t in [
        (Decimal("NaN"), Decimal("5")),
        (Decimal("5"), Decimal("Infinity")),
        (Decimal("-200"), Decimal("5")),
        (Decimal("5"), Decimal("5000")),
    ]:
        assert not fee_plausible(bad_m, bad_t)


@pytest.mark.asyncio
async def test_nan_fee_from_fetcher_falls_back_to_config(tmp_path):
    from app.core.config import Settings
    settings = Settings(
        spot_exchanges="kraken", perp_exchanges="krakenfutures",
        default_taker_fee_bps=Decimal("5"),
    )
    from app.engines.fee_oracle import FeeOracle
    oracle = FeeOracle(settings, state_path=str(tmp_path / "fees.json"))

    async def bad_fetch():
        return Decimal("NaN"), Decimal("5")

    out = await oracle.refresh({"krakenfutures": bad_fetch})
    assert out["krakenfutures"].source == "config"
    assert out["krakenfutures"].taker_bps == Decimal("5")
    # and the NaN was NOT persisted to the state file
    assert "NaN" not in (tmp_path / "fees.json").read_text()


def test_f54_load_state_survives_hostile_file(tmp_path):
    """UnicodeDecodeError / RecursionError killed fee_refresh_loop forever;
    corrupt state = fresh start."""
    from app.engines.fee_oracle import FeeOracle

    class S:  # minimal settings stub
        pass

    bad = tmp_path / "fees.json"
    bad.write_bytes(b'{"fees": "\xff"}')
    assert FeeOracle(S(), str(bad))._load_state() == {}
    bad.write_text("[" * 100000 + "]" * 100000)
    assert FeeOracle(S(), str(bad))._load_state() == {}


async def test_guardrail_blocks_zero_to_huge_jump(tmp_path):
    """Fuzzer note: prev_taker=0 disabled the % guard entirely - a venue
    going from zero-fee to 900 bps went straight through."""
    oracle, s = make_oracle(tmp_path)
    await oracle.refresh({"krakenfutures": lambda: fake("0", "0")})
    await oracle.refresh({"krakenfutures": lambda: fake("2", "900")})
    assert "krakenfutures" in s.fees_blocked


async def test_guardrail_ignores_tiny_zero_base_noise(tmp_path):
    """0 -> 3 bps is a listing change, not a policy shock: no block."""
    oracle, s = make_oracle(tmp_path)
    await oracle.refresh({"krakenfutures": lambda: fake("0", "0")})
    await oracle.refresh({"krakenfutures": lambda: fake("1", "3")})
    assert not s.fees_blocked
