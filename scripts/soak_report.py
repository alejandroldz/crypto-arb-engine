"""Soak analysis report: what did the engine actually see and do?

Reads the durable artifacts (works on a live checkout, a soak_restore, or a
soak-data branch copy - it never needs the locked DuckDB):

- funding_log.csv (or funding_history.csv from a snapshot): per-venue and
  per-symbol funding stats - samples, mean, %positive, std, sign flips,
  annualized APR at the venue's funding interval
- engine.log (optional): error/warning counts by signature, quarantine
  events, feed deaths, opens/closes

Run:  uv run python scripts/soak_report.py [--days N] [--dir PATH]
"""
from __future__ import annotations

import argparse
import csv
import logging
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

INTERVALS_PER_YEAR = {"krakenfutures": 24 * 365, "okx": 3 * 365}


def _to_utc(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_funding(path: Path, since: datetime) -> dict[tuple[str, str], list]:
    """(exchange, symbol) -> [(ts, rate)] with rate None-predicted skipped."""
    series: dict[tuple[str, str], list] = defaultdict(list)
    if not path.exists():
        return series
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                ts = _to_utc(row.get("ts") or row.get("ts_utc") or "")
                if ts is None or ts < since:
                    continue
                rate = float(row["rate"])
            except (KeyError, TypeError, ValueError):
                continue
            series[(row["exchange"], row["symbol"])].append((ts, rate))
    for v in series.values():
        v.sort()
    return series


def funding_report(series: dict[tuple[str, str], list]) -> list[str]:
    lines = ["", "=== funding por venue/símbolo ===",
             f"{'serie':40s} {'n':>6s} {'media':>10s} {'%pos':>6s} "
             f"{'std':>10s} {'flips':>5s} {'APR':>8s}"]
    by_venue: dict[str, list[float]] = defaultdict(list)
    for (ex, sym), rows in sorted(series.items()):
        rates = [r for _, r in rows]
        if not rates:
            continue
        signs = [r > 0 for r in rates]
        flips = sum(1 for a, b in zip(signs, signs[1:]) if a != b)
        mean = statistics.fmean(rates)
        std = statistics.pstdev(rates) if len(rates) > 1 else 0.0
        pos = sum(signs) / len(rates)
        apr = mean * INTERVALS_PER_YEAR.get(ex, 3 * 365) * 100
        by_venue[ex].extend(rates)
        lines.append(
            f"{ex + ':' + sym:40.40s} {len(rates):6d} {mean:10.2e} "
            f"{pos:6.1%} {std:10.2e} {flips:5d} {apr:8.2f}%"
        )
    for ex, rates in sorted(by_venue.items()):
        mean = statistics.fmean(rates)
        apr = mean * INTERVALS_PER_YEAR.get(ex, 3 * 365) * 100
        lines.append(
            f"  {ex} TOTAL: {len(rates)} muestras, media {mean:.3e}, "
            f"APR medio {apr:.2f}%"
        )
    return lines


LOG_PATTERNS = {
    "ERROR": re.compile(r" ERROR "),
    "feed died": re.compile(r"feed \S+ died"),
    "feed outage": re.compile(r"feed OUTAGE"),
    "quarantine": re.compile(r"funding source mismatch"),
    "breaker halt": re.compile(r"breaker HALT|failing CLOSED", re.I),
    "open": re.compile(r"OPEN carry|pp open", re.I),
    "close": re.compile(r"CLOSE carry|pp close", re.I),
}


def log_report(path: Path, since: datetime) -> list[str]:
    if not path.exists():
        return []
    counts: Counter[str] = Counter()
    first_last: dict[str, list[str]] = defaultdict(list)
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
            if not m:
                continue
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc)
            if ts < since:
                continue
            for name, pat in LOG_PATTERNS.items():
                if pat.search(line):
                    counts[name] += 1
                    first_last[name].append(m.group(1))
    lines = ["", "=== engine.log ==="]
    for name, n in counts.most_common():
        lines.append(f"  {name:14s} {n:5d}  "
                     f"({first_last[name][0]} .. {first_last[name][-1]})")
    if not counts:
        lines.append("  (sin eventos en la ventana)")
    return lines


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=1.0)
    ap.add_argument("--dir", type=Path, default=Path("."))
    args = ap.parse_args()
    since = datetime.now(timezone.utc) - timedelta(days=args.days)

    funding_csv = args.dir / "funding_log.csv"
    if not funding_csv.exists():  # soak-data snapshot layout
        funding_csv = args.dir / "funding_history.csv"
    print(f"ventana: últimos {args.days} días | fuente: {funding_csv}")
    series = load_funding(funding_csv, since)
    if not series:
        print("  (sin muestras de funding en la ventana)")
    for line in funding_report(series):
        print(line)
    for line in log_report(args.dir / "engine.log", since):
        print(line)


if __name__ == "__main__":
    main()
