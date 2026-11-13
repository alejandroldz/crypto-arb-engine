"""Soak report: parses the durable artifacts without the locked DuckDB."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.soak_report import funding_report, load_funding, log_report


def test_load_funding_and_report(tmp_path):
    csv_path = tmp_path / "funding_log.csv"
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=10)
    csv_path.write_text(
        "exchange,symbol,ts,rate,predicted_rate\n"
        f"okx,BTC/USD:USD-310404,{old.isoformat()},0.9,\n"  # out of window
        f"okx,BTC/USD:USD-310404,{now.isoformat()},0.0001,\n"
        f"okx,BTC/USD:USD-310404,{now.isoformat()},-0.0001,\n"
        "okx,BTC/USD:USD-310404,basura,0.0001,\n"
    )
    series = load_funding(csv_path, now - timedelta(days=1))
    rows = series[("okx", "BTC/USD:USD-310404")]
    assert len(rows) == 2  # old + garbage rows dropped
    lines = funding_report(series)
    joined = "\n".join(lines)
    assert "flips" in joined and " 1 " in joined.replace("  1 ", " 1 ")
    assert "okx TOTAL" in joined


def test_log_report_counts_and_windows(tmp_path):
    log = tmp_path / "engine.log"
    old = (datetime.now(timezone.utc) - timedelta(days=5)).strftime(
        "%Y-%m-%d %H:%M:%S")
    new = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    log.write_text(
        f"{old},000 main ERROR ancient\n"
        f"{new},000 main ERROR duckdb busy\n"
        f"{new},001 main WARNING funding source mismatch x\n"
    )
    lines = log_report(log, datetime.now(timezone.utc) - timedelta(days=1))
    joined = "\n".join(lines)
    assert "ERROR" in joined and "quarantine" in joined
    assert "ancient" not in joined  # outside the window
