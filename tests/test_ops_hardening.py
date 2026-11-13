"""op.#13/#15/#16/#17: watchdog, duckdb buffer safety, seed dedupe,
alert outbox."""
import asyncio
import json
import time
from datetime import datetime, timezone

import pytest

from app.connectors import ccxt_manager
from app.connectors.ccxt_manager import BOOKS_TOPIC, BookFeed
from app.core.circuit_breaker import CircuitBreaker
from app.core.decimals import D
from app.core.events import EventBus
from app.database.duckdb_store import TickStore
from app.connectors.normalizer import L2OrderBook, OrderBookLevel
from app.ui.notifier import TelegramNotifier


def book(exchange="kraken"):
    return L2OrderBook(
        exchange=exchange, symbol="BTC/USD",
        bids=(OrderBookLevel(price="100", qty="5"),),
        asks=(OrderBookLevel(price="100.01", qty="5"),),
        local_ts_ns=time.monotonic_ns(),
    )


# ---- op.#15: clean close flushes; failed flush keeps the batch ----

def test_close_flushes_buffer(tmp_path):
    store = TickStore(str(tmp_path / "t.duckdb"))
    store.record_book(book())
    store.close()
    check = TickStore(str(tmp_path / "t.duckdb"))
    n = check._conn.execute("SELECT count(*) FROM book_snapshots").fetchone()[0]
    assert n == 1


def test_failed_flush_keeps_batch(tmp_path):
    store = TickStore(str(tmp_path / "t.duckdb"))
    store.record_book(book())

    class FlakyConn:  # duckdb conn attributes are read-only: proxy instead
        def __init__(self, real):
            self._real = real
            self.calls = 0

        def executemany(self, *a, **kw):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("db down")
            return self._real.executemany(*a, **kw)

    store._conn = FlakyConn(store._conn)
    store.flush()  # fails, batch kept
    assert len(store._book_buf) == 1
    store.flush()  # retry succeeds
    assert len(store._book_buf) == 0


# ---- op.#16: seed dedupes per series across instant-equal timestamps ----

def test_seed_dedupes_naive_vs_aware_same_instant(tmp_path):
    import app.main as m

    csv_path = tmp_path / "funding_log.csv"
    csv_path.write_text(
        "exchange,symbol,ts,rate,predicted\n"
        "okx,BTC/USD:USD,2026-11-13T16:00:00+00:00,0.0005,\n"
    )
    monkey = pytest.MonkeyPatch()
    monkey.setattr(m, "FUNDING_LOG_CSV", str(csv_path))
    store = TickStore(str(tmp_path / "t.duckdb"))
    assert m.seed_funding_history(store) == 1
    assert m.seed_funding_history(store) == 0  # the R8 double-seed is gone
    assert len(store.funding_history("okx", "BTC/USD:USD")) == 1
    monkey.undo()


# ---- op.#17: failed alerts land in the durable outbox ----

async def test_failed_alert_goes_to_outbox(tmp_path):
    outbox = str(tmp_path / "outbox.jsonl")
    n = TelegramNotifier("x", "y", timeout_s=1, outbox_path=outbox)
    n._url = "http://127.0.0.1:1/dead"  # connection refused, no network
    assert await n.send("HALT: test") is False
    lines = (tmp_path / "outbox.jsonl").read_text().strip().splitlines()
    assert json.loads(lines[0])["text"] == "HALT: test"
    # disabled notifier also persists instead of dropping
    n2 = TelegramNotifier("", "", outbox_path=outbox)
    assert await n2.send("second") is False
    assert len((tmp_path / "outbox.jsonl").read_text().strip().splitlines()) == 2


def test_breaker_halt_fires_callback(tmp_path):
    seen = []
    b = CircuitBreaker(
        max_consecutive_failures=1, max_daily_loss_usd=D("25"),
        state_path=str(tmp_path / "b.json"), on_halt=seen.append,
    )
    b.halt("real account MMR emergency: 0.9")
    assert seen == ["real account MMR emergency: 0.9"]
    # same halt does not re-alert every cycle
    b.halt("real account MMR emergency: 0.9")
    assert len(seen) == 1


# ---- op.#13: a hung watch falls back to REST instead of stalling ----

class FakeHungEx:
    def __init__(self, cfg=None):
        pass

    async def load_markets(self):
        raise ConnectionError("no metadata in test")

    async def watch_order_book(self, symbol):
        await asyncio.sleep(600)  # the R10 hang: never returns, never raises

    async def fetch_order_book(self, symbol):
        return {"timestamp": None, "nonce": None,
                "bids": [["100", "5"]], "asks": [["100.01", "5"]]}

    async def close(self):
        pass


async def test_hung_watch_falls_back_to_rest(monkeypatch):
    monkeypatch.setattr(ccxt_manager.ccxtpro, "fakehung", FakeHungEx, raising=False)
    bus = EventBus()
    q = bus.subscribe(BOOKS_TOPIC)
    stop = asyncio.Event()
    feed = BookFeed("fakehung", ["BTC/USD"], rest_backoff_s=0,
                    derivative=False, watch_timeout_s=0.05)
    task = asyncio.create_task(feed.run(bus, stop))
    got = await asyncio.wait_for(q.get(), timeout=5)
    assert got.exchange == "fakehung"
    stop.set()
    await asyncio.wait_for(task, timeout=5)


# ---- op.#10: DB defaults live under the mounted volume ----

def test_db_defaults_under_data_dir():
    from app.core.config import Settings
    s = Settings()
    assert "/data/" in s.sqlite_url
    assert s.duckdb_path.startswith("./data/")


# ---- op.#18: derivative feed without market table fails closed ----

async def test_derivative_feed_without_markets_fails_closed(monkeypatch):
    monkeypatch.setattr(ccxt_manager.ccxtpro, "fakenometa", FakeHungEx, raising=False)
    bus = EventBus()
    q = bus.subscribe(BOOKS_TOPIC)
    stop = asyncio.Event()
    feed = BookFeed("fakenometa", ["BTC/USD:USD"], rest_backoff_s=0,
                    markets_list=None, derivative=True, watch_timeout_s=0.05)
    task = asyncio.create_task(feed.run(bus, stop))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert q.empty()  # nothing published without contract metadata


def test_seed_compacts_csv_to_7_days(tmp_path):
    """caos: funding_log.csv grew unbounded; seeding rewrites it keeping
    only the last 7 days (DuckDB is the durable store)."""
    import app.main as m

    csv_path = tmp_path / "funding_log.csv"
    csv_path.write_text(
        "exchange,symbol,ts,rate,predicted\n"
        "okx,BTC/USD:USD,2020-01-01T00:00:00+00:00,0.0005,\n"
        "okx,BTC/USD:USD,2026-11-13T16:00:00+00:00,0.0006,\n"
    )
    monkey = pytest.MonkeyPatch()
    monkey.setattr(m, "FUNDING_LOG_CSV", str(csv_path))
    store = TickStore(str(tmp_path / "t.duckdb"))
    m.seed_funding_history(store)
    lines = csv_path.read_text().strip().splitlines()
    assert len(lines) == 2  # header + the recent row only
    assert "2026-11-13" in lines[1]
    monkey.undo()
