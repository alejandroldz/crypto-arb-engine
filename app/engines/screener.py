"""Perp screener: decides which perps the engine may open carries on.

Pure Decimal logic + JSON state; network lives in connectors/screener_io.py.
A perp passes the cut when it clears all four filters (legality is handled
by construction: only krakenfutures + OKX X-Perps are swept):

1. liquidity: 24h USD volume >= screener_min_volume_usd
2. funding economics: mean funding over the history window covers worst-case
   round-trip costs (entry+exit taker fees + slippage) over HOLDING_INTERVALS
3. funding stability: at least screener_min_positive_pct of intervals positive
4. spot leg exists: base asset trades <BASE>/USD on the spot venue

Hysteresis: a candidate ENTERS after 2 consecutive passing sweeps and LEAVES
after 3 consecutive failing ones, so one bad print never churns positions.
Whitelist (bases) is always eligible; blacklist (perp keys) never is.
Removal blocks new entries only - open carries are unaffected (they keep
their feeds and unwind on the normal monitor rules).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from app.core.decimals import D, ZERO

logger = logging.getLogger("main")

HOLDING_HOURS = 720  # 30 days, same horizon as detect_funding_carry
INCLUDE_AFTER = 2
EXCLUDE_AFTER = 3


@dataclass(frozen=True)
class PerpMetrics:
    exchange: str
    symbol: str
    base: str
    volume_24h_usd: Decimal
    funding_mean: Decimal | None
    funding_pct_positive: Decimal | None
    funding_interval_h: Decimal = Decimal("8")

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.symbol}"


def from_raw(raw: object) -> PerpMetrics | None:
    """Decimal-ize a connectors.screener_io.RawPerpMetrics (floats -> str).

    F10: non-finite input fails CLOSED (returns None) instead of poisoning
    the sweep: NaN funding history means "unknown", and unknown is unsafe.
    One bad symbol must not abort the whole sweep.
    """
    def conv(x: object) -> Decimal | None:
        if x is None:
            return None
        d = D(str(x))
        return d if d.is_finite() else None

    volume = D(str(raw.volume_24h_usd))
    interval = D(str(raw.funding_interval_h))
    if not volume.is_finite() or not interval.is_finite() or interval <= 0:
        return None
    return PerpMetrics(
        exchange=raw.exchange,
        symbol=raw.symbol,
        base=raw.base,
        volume_24h_usd=volume,
        funding_mean=conv(raw.funding_mean),
        funding_pct_positive=conv(raw.funding_pct_positive),
        funding_interval_h=interval,
    )


def passes_cut(
    m: PerpMetrics,
    *,
    min_volume_usd: Decimal,
    min_positive_pct: Decimal,
    roundtrip_cost_bps: Decimal,
    spot_bases: set[str],
) -> bool:
    """All four filters; missing history is a fail (unknown is unsafe)."""
    if m.base not in spot_bases:
        return False
    if D(m.volume_24h_usd) < D(min_volume_usd):
        return False
    if m.funding_mean is None or m.funding_pct_positive is None:
        return False
    if D(m.funding_pct_positive) < D(min_positive_pct):
        return False
    intervals = HOLDING_HOURS / D(m.funding_interval_h)
    earned_bps = D(m.funding_mean) * intervals * Decimal("10000")
    return earned_bps > D(roundtrip_cost_bps)


class Hysteresis:
    """2 consecutive passes to include, 3 consecutive fails to exclude."""

    def __init__(self, state: dict[str, dict] | None = None):
        # key -> {"in": bool, "streak": int} (streak of consecutive same outcomes)
        self._state: dict[str, dict] = dict(state or {})

    def update(self, key: str, passed: bool) -> bool:
        """Record one sweep outcome; return whether the key is eligible now."""
        e = self._state.setdefault(key, {"in": False, "streak": 0})
        last_passed = e.get("last")
        e["streak"] = e["streak"] + 1 if passed == last_passed else 1
        e["last"] = passed
        if passed and not e["in"] and e["streak"] >= INCLUDE_AFTER:
            e["in"] = True
        elif not passed and e["in"] and e["streak"] >= EXCLUDE_AFTER:
            e["in"] = False
        return e["in"]

    def seed(self, key: str) -> None:
        """Pre-existing configured symbols start eligible with no history."""
        self._state.setdefault(key, {"in": True, "streak": 0, "last": True})

    def to_dict(self) -> dict[str, dict]:
        return self._state


def roundtrip_cost_bps(spot_fee_bps: Decimal, perp_fee_bps: Decimal, slippage_bps: Decimal) -> Decimal:
    """Worst case: taker in AND out on both legs, plus slippage buffer."""
    return (D(spot_fee_bps) + D(perp_fee_bps) + D(slippage_bps)) * 2


def run_sweep(
    metrics: list[PerpMetrics],
    hyst: Hysteresis,
    *,
    min_volume_usd: Decimal,
    min_positive_pct: Decimal,
    cost_bps_for,
    spot_bases: set[str],
    whitelist_keys: set[str],
    blacklist_keys: set[str],
) -> set[str]:
    """One sweep over all candidates; returns the new eligible perp keys."""
    eligible: set[str] = set()
    seen: set[str] = set()
    for m in metrics:
        seen.add(m.key)
        if m.key in blacklist_keys:
            continue
        passed = passes_cut(
            m,
            min_volume_usd=min_volume_usd,
            min_positive_pct=min_positive_pct,
            roundtrip_cost_bps=cost_bps_for(m.exchange),
            spot_bases=spot_bases,
        )
        if hyst.update(m.key, passed) or m.key in whitelist_keys:
            if m.base in spot_bases:
                eligible.add(m.key)
    # audit #2: an instrument MISSING from the response (connector volume
    # pre-filter, failed download) is ONE fail event for the hysteresis -
    # it takes 3 consecutive omissions to drop, never a single sweep. And a
    # whitelisted (user-configured) key survives omission.
    for key in whitelist_keys - blacklist_keys:
        if key not in seen:
            hyst.seed(key)
    for key, entry in hyst.to_dict().items():
        if key in seen or key in blacklist_keys:
            continue
        still_in = hyst.update(key, False)
        if still_in or key in whitelist_keys:
            if ":" not in key:
                continue  # fuzz F15: corrupt key, never split blindly
            base = key.split(":", 1)[1].split("/")[0]
            if base in spot_bases:
                eligible.add(key)
    return eligible


def _sanitize_state(raw: object) -> dict[str, dict]:
    """fuzz F15: keep only well-formed {key: {in, streak, last}} entries.
    Corrupt entries (nulls, wrong types) would crash Hysteresis.update
    inside every sweep; dropping them repairs the state on the next save."""
    if not isinstance(raw, dict):
        return {}
    clean: dict[str, dict] = {}
    dropped = 0
    for key, entry in raw.items():
        if (isinstance(key, str) and ":" in key and isinstance(entry, dict)
                and isinstance(entry.get("in"), bool)):
            try:
                clean[key] = {
                    "in": entry["in"],
                    "streak": int(entry.get("streak", 0)),
                    "last": bool(entry.get("last", entry["in"])),
                }
            except (TypeError, ValueError):
                dropped += 1
        else:
            dropped += 1
    if dropped:
        logger.warning("screener state: %d corrupt entries dropped", dropped)
    return clean


def load_state(path: str) -> Hysteresis:
    p = Path(path)
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            # F51: UnicodeDecodeError (invalid bytes) and RecursionError
            # (deep nesting) escaped the narrow catch and killed
            # screener_loop - an UNSUPERVISED task, so the engine kept
            # trading the initial eligible set forever with no alert
            logger.warning("screener state unreadable, starting fresh")
        else:
            return Hysteresis(_sanitize_state(raw))
    return Hysteresis()


def save_state(path: str, hyst: Hysteresis) -> None:
    p = Path(path)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(hyst.to_dict(), indent=1), encoding="utf-8")
    tmp.replace(p)  # atomic on POSIX


def eligible_bases(eligible: set[str]) -> set[str]:
    """Base assets of eligible perps (spot feeds subscribe to these)."""
    return {key.split(":")[1].split("/")[0] for key in eligible}
