"""DuckDB tick store: order book + funding snapshots for analysis/backtesting."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal

import duckdb

from app.core.decimals import D

logger = logging.getLogger(__name__)
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
                datetime.now(timezone.utc).replace(tzinfo=None),
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
        try:
            self._conn.executemany(
                "INSERT INTO book_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
                self._book_buf,
            )
        except Exception:
            # op.#15: keep the batch for the next retry instead of dropping
            # it on the floor; cap it so a dead DB cannot grow RAM unbounded
            logger.exception("book flush failed; batch kept for retry")
            del self._book_buf[:-5000]
            return
        self._book_buf = []

    def funding_max_ts_by_series(self) -> dict:
        """op.#16: watermark PER SERIES - a global max cannot repair gaps in
        series whose latest sample is older than another series' max."""
        rows = self._conn.execute(
            "SELECT exchange, symbol, max(ts_utc) FROM funding_snapshots "
            "GROUP BY 1, 2"
        ).fetchall()
        return {(r[0], r[1]): r[2] for r in rows}

    def funding_max_ts(self):
        row = self._conn.execute(
            "SELECT max(ts_utc) FROM funding_snapshots"
        ).fetchone()
        return row[0].isoformat() if row and row[0] else ""

    def insert_funding_raw(
        self, exchange: str, symbol: str, ts_iso: str, rate: str, predicted
    ) -> None:
        # Direct insert for seeding from the CSV log. op.#16: failures are
        # REPORTED, not swallowed as if they were duplicates.
        try:
            ts = datetime.fromisoformat(ts_iso)
            if ts.tzinfo is not None:
                ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
            self._conn.execute(
                "INSERT INTO funding_snapshots VALUES (?, ?, ?, ?, ?, ?)",
                [ts, exchange, symbol, rate, predicted, None],
            )
        except Exception as exc:
            logger.warning(
                "funding seed insert failed %s %s %s: %s",
                exchange, symbol, ts_iso, exc,
            )
            return False
        return True

    @staticmethod
    def _naive(dt):
        # DuckDB TIMESTAMP is timezone-naive; normalize everything to UTC
        # wall time so inserts and dedupe comparisons agree
        if dt is None:
            return None
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt

    def record_funding(self, rate: FundingRate) -> None:
        # F08: dedupe replays. Polls within the same funding interval carry
        # the same rate and the same next_funding_ts; storing each one lets a
        # replayed snapshot fill the history and pass the min-samples gate.
        row = self._conn.execute(
            "SELECT rate, next_funding_ts FROM funding_snapshots "
            "WHERE exchange = ? AND symbol = ? ORDER BY ts_utc DESC LIMIT 1",
            [rate.exchange, rate.symbol],
        ).fetchone()
        if row is not None:
            same_rate = Decimal(str(row[0])) == D(rate.rate)
            same_interval = self._naive(row[1]) == self._naive(rate.next_funding_ts)
            if same_rate and same_interval:
                return  # replay of the same interval observation
        self._conn.execute(
            "INSERT INTO funding_snapshots VALUES (?, ?, ?, ?, ?, ?)",
            [
                datetime.now(timezone.utc).replace(tzinfo=None),
                rate.exchange,
                rate.symbol,
                str(rate.rate),
                str(rate.predicted_rate) if rate.predicted_rate is not None else None,
                self._naive(rate.next_funding_ts),
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
        try:
            self.flush()  # op.#15: a clean shutdown must not lose the buffer
        finally:
            self._conn.close()
