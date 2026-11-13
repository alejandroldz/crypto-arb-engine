"""caos H9: disk retention - book pruning + outbox rotation."""
import json
from datetime import datetime, timedelta

import pytest

from app.database.duckdb_store import TickStore


def _book(ts=None):
    from app.connectors.normalizer import L2OrderBook, OrderBookLevel
    b = L2OrderBook(
        exchange="t", symbol="X/USD",
        bids=(OrderBookLevel(price="100", qty="1"),),
        asks=(OrderBookLevel(price="101", qty="1"),),
        local_ts_ns=1,
    )
    return b


def test_h9_prune_books_keeps_recent(tmp_path):
    store = TickStore(str(tmp_path / "t.duckdb"))
    store.record_book(_book())
    store.flush()
    old = (datetime.utcnow() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
    store._conn.execute(
        "INSERT INTO book_snapshots VALUES (?, 't', 'X/USD', 100, 101, '[]', '[]')",
        [old],
    )
    deleted = store.prune_books(keep_days=3)
    assert deleted == 1
    (n,) = store._conn.execute("SELECT count(*) FROM book_snapshots").fetchone()
    assert n == 1  # the fresh row survives
    store.close()


def test_h9_outbox_rotates(tmp_path):
    from app.ui.notifier import TelegramNotifier
    path = tmp_path / "out.jsonl"
    n = TelegramNotifier("", "", outbox_path=str(path))
    path.write_bytes(b"x" * 1_100_000)
    n._outbox("fresh alert")
    assert path.stat().st_size < 600_000
    last = path.read_text().strip().splitlines()[-1]
    assert json.loads(last)["text"] == "fresh alert"
