"""Audit regressions F01/F08/F09/F10: adversarial data rejected at the
boundary; replays cannot fill history."""
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.connectors.ccxt_manager import normalize_book, normalize_funding
from app.core.decimals import D
from app.core.feed_guards import (
    remote_book_stale,
    sequence_regressed,
)
from app.database.duckdb_store import TickStore
from app.connectors.normalizer import FundingRate
from app.engines import screener as scr


def raw_book(ts_ms=None, nonce=None, bid="100", ask="100.01"):
    return {
        "timestamp": ts_ms,
        "nonce": nonce,
        "bids": [[bid, "5"]],
        "asks": [[ask, "5"]],
    }


# ---- F09: representability at the boundary ----

def test_book_rejects_extreme_magnitude():
    with pytest.raises(ValueError):  # crossed-book guard fires first here
        normalize_book("kraken", "BTC/USD", raw_book(bid="1e30"))
    with pytest.raises(ValueError, match="bounds"):
        normalize_book("kraken", "BTC/USD", raw_book(bid="100", ask="1e30"))
    with pytest.raises(ValueError, match="bounds"):
        normalize_book("kraken", "BTC/USD", {
            "timestamp": None, "nonce": None,
            "bids": [["100", "1e30"]], "asks": [["100.01", "5"]],
        })


def test_funding_rejects_extreme_magnitude():
    with pytest.raises(ValueError, match="bounds"):
        normalize_funding("okx", "BTC/USD:USD", {"fundingRate": "1e30"})


# ---- F08: funding rate plausibility ----

def test_funding_rejects_implausible_rate():
    # rate=10 is +1000% per interval: units garbage, not a market move
    with pytest.raises(ValueError, match="implausible"):
        normalize_funding("okx", "BTC/USD:USD", {"fundingRate": "10"})
    with pytest.raises(ValueError, match="implausible"):
        normalize_funding("okx", "BTC/USD:USD", {"fundingRate": "-10"})
    # a hot but realistic rate passes
    r = normalize_funding("okx", "BTC/USD:USD", {"fundingRate": "0.01"})
    assert r.rate == D("0.01")


# ---- F08: replay cannot fill funding history ----

def fr(rate="0.0005", next_ts=None):
    return FundingRate(
        exchange="okx", symbol="BTC/USD:USD", rate=rate,
        local_ts_ns=time.monotonic_ns(), next_funding_ts=next_ts,
    )


def test_funding_dedupes_replayed_interval(tmp_path):
    store = TickStore(str(tmp_path / "t.duckdb"))
    t1 = datetime(2026, 11, 13, 16, 0, tzinfo=timezone.utc)
    for _ in range(30):  # the audit's replay attack
        store.record_funding(fr("0.0005", t1))
    assert len(store.funding_history("okx", "BTC/USD:USD")) == 1
    # the next interval IS a new observation
    t2 = t1 + timedelta(hours=8)
    store.record_funding(fr("0.0005", t2))
    # and a rate change within the interval is new information too
    store.record_funding(fr("0.0006", t2))
    assert len(store.funding_history("okx", "BTC/USD:USD")) == 3


# ---- F01: remote timestamp and sequence guards ----

def test_remote_book_stale():
    now = datetime.now(timezone.utc)
    assert remote_book_stale(now - timedelta(hours=24), now=now)
    assert not remote_book_stale(now - timedelta(seconds=2), now=now)
    assert not remote_book_stale(None, now=now)  # absent ts: local clock rules


def test_sequence_regressed():
    t1 = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
    t2 = t1 + timedelta(seconds=5)
    assert sequence_regressed(100, t2, 100, t2)          # duplicate
    assert sequence_regressed(100, t2, 99, t1)           # yesterday's replay
    assert not sequence_regressed(100, t2, 101, t2)      # normal advance
    assert not sequence_regressed(100, t2, 3, t2 + timedelta(seconds=9))  # resync
    assert not sequence_regressed(None, t2, 99, t1)      # venue without nonce


# ---- F10: screener fails closed on non-finite input ----

def raw_metrics(volume=5_000_000.0, mean=0.0002, pct=0.8, interval=8.0):
    return SimpleNamespace(
        exchange="okx", symbol="BTC/USD:USD", base="BTC",
        volume_24h_usd=volume, funding_mean=mean, funding_pct_positive=pct,
        funding_interval_h=interval,
    )


def test_screener_from_raw_rejects_non_finite():
    assert scr.from_raw(raw_metrics(volume=float("nan"))) is None
    assert scr.from_raw(raw_metrics(mean=float("inf"), pct=float("inf"))) is not None
    # inf funding history degrades to "unknown" (fail closed), not a pass
    m = scr.from_raw(raw_metrics(mean=float("inf"), pct=float("inf")))
    assert m.funding_mean is None and m.funding_pct_positive is None
    assert scr.from_raw(raw_metrics(interval=float("nan"))) is None
    assert scr.from_raw(raw_metrics(interval=0.0)) is None
    assert scr.from_raw(raw_metrics()) is not None


def test_f7_check_magnitude_hostile_inputs_raise_valueerror():
    import pytest

    from app.core.feed_guards import check_magnitude
    for bad in ("1e999999999999", object(), [1], {"a": 1}, "NaN", "Infinity"):
        with pytest.raises(ValueError):
            check_magnitude(bad, "test value")


def test_f8_naive_aware_mixed_timestamps():
    from datetime import datetime, timedelta, timezone

    from app.core.feed_guards import remote_book_stale, sequence_regressed
    naive_old = datetime(2026, 11, 13, 10, 0)  # UTC-naive (duckdb style)
    aware_now = datetime(2026, 11, 13, 10, 2, tzinfo=timezone.utc)
    assert remote_book_stale(naive_old, now=aware_now) is True
    assert remote_book_stale(aware_now, now=naive_old + timedelta(minutes=2)) is False
    # sequence guard with mixed tz: no TypeError
    assert sequence_regressed(5, naive_old, 5, aware_now) is True


def test_f16_monitor_accepts_naive_last_payment_ts():
    from datetime import datetime, timedelta, timezone

    from app.connectors.normalizer import FundingRate
    from app.core.decimals import D
    from app.engines.monitor import evaluate_carry
    from app.engines.risk import CarryPositionRisk
    naive = datetime(2026, 11, 13, 8, 0)
    rate = FundingRate(
        exchange="krakenfutures", symbol="BTC/USD:USD", rate="0.0003",
        local_ts_ns=1,
        next_funding_ts=datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc),
    )
    d = evaluate_carry(
        qty_base=D("0.01"), perp_entry_vwap=D("50000"),
        last_payment_ts=naive, rate=rate, perp_book=None,
        risk=CarryPositionRisk.build(D("50000"), D("1")),
        liquidation_buffer_pct=D("15"),
    )
    assert d.accrual != 0
