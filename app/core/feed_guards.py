"""Adversarial feed guards (audit F01/F08/F09): validate remote data at the
boundary, before it reaches engines or persistence."""
from datetime import datetime, timezone
from decimal import Decimal

from app.core.decimals import D

# A book whose venue timestamp is older than this is a replay, not data.
MAX_REMOTE_BOOK_AGE_S = 60
# |funding rate| per interval above this is a units error, not a market move
# (realistic extremes stay under ~3%; the audit demonstrated rate=10 = 1000%).
MAX_FUNDING_RATE_ABS = Decimal("0.05")
# DECIMAL(38,18) leaves 20 integer digits; beyond that persistence dies.
MAX_REPRESENTABLE = Decimal("1e20")


def check_magnitude(value, what: str) -> Decimal:
    """F09: reject non-finite or non-representable values at the boundary.

    fuzz F7: hostile payloads must surface as ValueError, never as raw
    OverflowError/TypeError/ArithmeticError leaking out of the guard."""
    try:
        d = D(value)
        if not d.is_finite():
            raise ValueError(f"non-finite {what}")
        if abs(d) >= MAX_REPRESENTABLE:
            raise ValueError(f"{what} exceeds DECIMAL(38,18) bounds: {d}")
        return d
    except (OverflowError, TypeError, ArithmeticError) as exc:
        raise ValueError(f"unusable {what}: {value!r}") from exc


def aware(dt: datetime) -> datetime:
    """fuzz F8: duckdb persistence is UTC-naive; ccxt payloads are aware.
    Mixing them in a subtraction/comparison raises TypeError. Naive is UTC
    by convention everywhere in this codebase."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def funding_rate_plausible(rate) -> Decimal:
    """F08: magnitude + representability check for funding rates."""
    d = check_magnitude(rate, "funding rate")
    if abs(d) > MAX_FUNDING_RATE_ABS:
        raise ValueError(
            f"implausible funding rate {d} (>|{MAX_FUNDING_RATE_ABS}| per interval)"
        )
    return d


def remote_book_stale(
    exchange_ts: datetime | None, *, now: datetime | None = None,
    max_age_s: int = MAX_REMOTE_BOOK_AGE_S,
) -> bool:
    """F01: a venue may serve an old snapshot as new. Absent remote ts is an
    explicit policy choice: the local monotonic clock governs then."""
    if exchange_ts is None:
        return False
    now = aware(now or datetime.now(timezone.utc))
    return (now - aware(exchange_ts)).total_seconds() > max_age_s


def sequence_regressed(prev_seq, prev_ts, new_seq, new_ts) -> bool:
    """F01: duplicate or replayed book snapshot. A lower nonce carrying a
    NEWER remote ts is a reconnect resync, not a replay: let it through."""
    if prev_seq is None or new_seq is None:
        return False
    if int(new_seq) > int(prev_seq):
        return False
    if (new_ts is not None and prev_ts is not None
            and aware(new_ts) > aware(prev_ts)):
        return False
    return True
