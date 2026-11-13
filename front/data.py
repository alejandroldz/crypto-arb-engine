"""Read-only data access for the Streamlit front.

Decoupled by construction: reads only the engine's data files
(funding_log.csv, arb_ticks.duckdb, arb_state.db, engine.log). It never
imports the engine loop and never writes. Safe to run while the soak runs
(DuckDB read_only=True, SQLite immutable=1).
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FUNDING_CSV = ROOT / "funding_log.csv"
DUCKDB_PATH = ROOT / "arb_ticks.duckdb"
SQLITE_PATH = ROOT / "arb_state.db"
ENGINE_LOG = ROOT / "engine.log"

# venues the engine trades today; older soak artifacts may carry removed venues
ACTIVE_VENUES = ("kraken", "krakenfutures", "okx")


def funding_log() -> pd.DataFrame:
    """All funding snapshots from the CSV log (survives engine restarts)."""
    if not FUNDING_CSV.exists():
        return pd.DataFrame(columns=["exchange", "symbol", "ts", "rate", "predicted_rate"])
    df = pd.read_csv(FUNDING_CSV)
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    df["rate"] = pd.to_numeric(df["rate"], errors="coerce")
    df = df.dropna(subset=["ts"])
    return df[df["exchange"].isin(ACTIVE_VENUES)]


def latest_books() -> pd.DataFrame:
    """Newest book snapshot per exchange/symbol from the tick store."""
    if not DUCKDB_PATH.exists():
        return pd.DataFrame()
    try:
        # can fail while the engine holds the write lock; report empty instead
        con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
        return con.execute(
            """
            SELECT exchange, symbol, best_bid, best_ask, ts_utc
            FROM book_snapshots
            WHERE exchange IN (SELECT unnest(['kraken', 'krakenfutures', 'okx']))
            QUALIFY row_number() OVER (
                PARTITION BY exchange, symbol ORDER BY ts_utc DESC
            ) = 1
            ORDER BY exchange
            """
        ).df()
    except duckdb.Error:
        return pd.DataFrame()
    finally:
        try:
            con.close()
        except Exception:
            pass


def positions() -> pd.DataFrame:
    """Paper positions from the engine's SQLite state."""
    if not SQLITE_PATH.exists():
        return pd.DataFrame()
    con = sqlite3.connect(f"file:{SQLITE_PATH}?immutable=1", uri=True)
    try:
        return pd.read_sql_query(
            """
            SELECT symbol, exchange_spot, exchange_perp, notional,
                   funding_collected, status, opened_at, realized_pnl_usd
            FROM positions ORDER BY opened_at DESC
            """,
            con,
        )
    except sqlite3.Error:
        return pd.DataFrame()
    finally:
        con.close()


def account_health() -> list[dict]:
    """Latest 'real account health' log line per venue."""
    if not ENGINE_LOG.exists():
        return []
    pattern = re.compile(
        r"^(\S+ \S+) .*real account health (\w+): equity (\S+) mmr (\S+)"
    )
    latest: dict[str, dict] = {}
    with open(ENGINE_LOG, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = pattern.match(line)
            if m:
                if m.group(2) not in ACTIVE_VENUES:
                    continue
                latest[m.group(2)] = {
                    "venue": m.group(2),
                    "equity": m.group(3),
                    "mmr": m.group(4),
                    "ts": m.group(1),
                }
    return list(latest.values())
