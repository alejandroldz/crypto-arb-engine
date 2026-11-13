"""DuckDB tick store: order book + funding snapshots for analysis/backtesting."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import duckdb

from app.connectors.normalizer import FundingRate, L2OrderBook


class TickStore:
    def __init__(self, path: str):
        self._conn = duckdb.connect(path)
        self._conn.execute("PRAGMA memory_limit='700MB'")
        self._book_buf: list[list] = []
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
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS funding_snapshots (
                ts_utc TIMESTAMP,
                exchange VARCHAR,
                symbol VARCHAR,
                rate DECIMAL(38,18),
                predicted_rate DECIMAL(38,18),
                next_funding_ts TIMESTAMP
            )
            """
        )

    def record_book(self, book: L2OrderBook) -> None:
        # Buffered: one INSERT per book update is DuckDB's pathological case
        # (catalog/transaction overhead per single-row txn grows RSS without
        # bound on a 2GB sandbox). Batch via executemany on flush().
        self._book_buf.append(
            [
                datetime.now(timezone.utc),
                book.exchange,
                book.symbol,
                str(book.best_bid.price),
                str(book.best_ask.price),
                json.dumps([[str(l.price), str(l.qty)] for l in book.bids]),
                json.dumps([[str(l.price), str(l.qty)] for l in book.asks]),
            ]
        )
        if len(self._book_buf) >= 200:
            self.flush()

    def flush(self) -> None:
        if not self._book_buf:
            return
        buf, self._book_buf = self._book_buf, []
        self._conn.executemany(
            "INSERT INTO book_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)", buf
        )

    def funding_max_ts(self):
        row = self._conn.execute(
            "SELECT max(ts_utc) FROM funding_snapshots"
        ).fetchone()
        return row[0].isoformat() if row and row[0] else ""

    def insert_funding_raw(
        self, exchange: str, symbol: str, ts_iso: str, rate: str, predicted
    ) -> None:
        # Direct insert for seeding from the CSV log (deduped by unique row).
        try:
            self._conn.execute(
                "INSERT INTO funding_snapshots VALUES (?, ?, ?, ?, ?, ?)",
                [ts_iso, exchange, symbol, rate, predicted, None],
            )
        except Exception:
            pass  # duplicate sample already seeded

    def record_funding(self, rate: FundingRate) -> None:
        self._conn.execute(
            "INSERT INTO funding_snapshots VALUES (?, ?, ?, ?, ?, ?)",
            [
                datetime.now(timezone.utc),
                rate.exchange,
                rate.symbol,
                str(rate.rate),
                str(rate.predicted_rate) if rate.predicted_rate is not None else None,
                rate.next_funding_ts,
            ],
        )

    def funding_history(
        self, exchange: str, symbol: str, limit: int = 500
    ) -> list[Decimal]:
        """Observed funding rates, oldest first, for percentile statistics."""
        rows = self._conn.execute(
            "SELECT rate FROM funding_snapshots WHERE exchange = ? AND symbol = ? "
            "ORDER BY ts_utc DESC LIMIT ?",
            [exchange, symbol, limit],
        ).fetchall()
        return [Decimal(str(r[0])) for r in reversed(rows)]

    def recent(self, symbol: str, limit: int = 100) -> list[tuple]:
        return self._conn.execute(
            "SELECT ts_utc, exchange, best_bid, best_ask FROM book_snapshots "
            "WHERE symbol = ? ORDER BY ts_utc DESC LIMIT ?",
            [symbol, limit],
        ).fetchall()

    def close(self) -> None:
        self._conn.close()
