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
