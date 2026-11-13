"""DuckDB tick store: order book snapshots for later backtesting."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import duckdb

from app.connectors.normalizer import L2OrderBook


class TickStore:
    def __init__(self, path: str):
        self._conn = duckdb.connect(path)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS book_snapshots (
                ts_utc TIMESTAMP,
                exchange VARCHAR,
                symbol VARCHAR,
                best_bid DECIMAL(38,18),
                best_ask DECIMAL(38,18),
                bids_json VARCHAR,
                asks_json VARCHAR
            )
            """
        )

    def record_book(self, book: L2OrderBook) -> None:
        self._conn.execute(
            "INSERT INTO book_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                datetime.now(timezone.utc),
                book.exchange,
                book.symbol,
                str(book.best_bid.price),
                str(book.best_ask.price),
                json.dumps([[str(l.price), str(l.qty)] for l in book.bids]),
                json.dumps([[str(l.price), str(l.qty)] for l in book.asks]),
            ],
        )

    def recent(self, symbol: str, limit: int = 100) -> list[tuple]:
        return self._conn.execute(
            "SELECT ts_utc, exchange, best_bid, best_ask FROM book_snapshots "
            "WHERE symbol = ? ORDER BY ts_utc DESC LIMIT ?",
            [symbol, limit],
        ).fetchall()

    def close(self) -> None:
        self._conn.close()
