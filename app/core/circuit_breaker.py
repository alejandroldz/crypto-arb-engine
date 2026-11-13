"""Kill switch and rate limiting (RISK 6).

Halts all execution when any trip condition fires:
  1. more than N consecutive failed/unwound trades
  2. daily realized+unrealized drawdown beyond MAX_DAILY_LOSS_EUR
  3. more than M orders within the rolling window
  4. reconciliation mismatch between local state and exchange balances
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

from app.core.decimals import D

logger = logging.getLogger("breaker")


class TokenBucket:
    """Per-exchange request rate limiter (respect ccxt rate limits)."""

    def __init__(self, capacity: int, refill_per_second: int):
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self._tokens = float(capacity)
        self._last = time.monotonic()

    def allow(self) -> bool:
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        self._tokens = min(
            self.capacity, self._tokens + elapsed * self.refill_per_second
        )
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        return False


@dataclass
class CircuitBreaker:
    max_consecutive_failures: int
    max_daily_loss_usd: Decimal
    max_orders_per_window: int = 6
    window_seconds: int = 60
    halted: bool = False
    halt_reason: str | None = None
    state_path: str | None = None
    _consecutive_failures: int = 0
    _daily_pnl_usd: Decimal = Decimal("0")
    _mtm_usd: Decimal = Decimal("0")
    _day: str = ""
    _order_times: deque = field(default_factory=deque)

    def __post_init__(self) -> None:
        # op.#8: a HALT survives restarts. Without this, restarting after
        # tripping the daily loss limit re-enabled entries immediately.
        if self.state_path and Path(self.state_path).exists():
            try:
                st = json.loads(Path(self.state_path).read_text())
                self.halted = bool(st.get("halted", False))
                self.halt_reason = st.get("halt_reason")
                self._consecutive_failures = int(st.get("consecutive_failures", 0))
                self._day = str(st.get("day", ""))
                if self._day == self._today():
                    self._daily_pnl_usd = D(st.get("daily_pnl_usd", "0"))
                if self.halted:
                    logger.warning(
                        "breaker restored HALT from disk: %s", self.halt_reason
                    )
            except Exception as exc:
                logger.error("breaker state unreadable, starting clean: %s", exc)

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _persist(self) -> None:
        if not self.state_path:
            return
        tmp = Path(self.state_path + ".tmp")
        tmp.write_text(json.dumps({
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "consecutive_failures": self._consecutive_failures,
            "day": self._day or self._today(),
            "daily_pnl_usd": str(self._daily_pnl_usd),
        }))
        tmp.replace(self.state_path)

    def _maybe_roll_day(self) -> None:
        """op.#8: 'daily' means a defined UTC day - roll even without restart."""
        today = self._today()
        if self._day != today:
            self._day = today
            self._daily_pnl_usd = Decimal("0")
            self._persist()

    def seed_daily_pnl(self, amount: Decimal) -> None:
        """op.#8: rebuild today's realized PnL from the ledger at startup."""
        self._day = self._today()
        self._daily_pnl_usd = D(amount)
        if self._daily_pnl_usd <= -D(self.max_daily_loss_usd):
            self._halt("daily loss limit exceeded")
        self._persist()

    def record_mtm_usd(self, amount: Decimal) -> None:
        """op.#8: mark-to-market of open exposure counts toward the daily
        loss limit, not only realized closes."""
        self._maybe_roll_day()
        self._mtm_usd = D(amount)
        if self._daily_pnl_usd + self._mtm_usd <= -D(self.max_daily_loss_usd):
            self._halt("daily loss limit exceeded (realized + MTM)")

    def _halt(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason
        logger.warning("CIRCUIT BREAKER HALT: %s", reason)
        self._persist()

    def record_order(self) -> bool:
        """Register an order attempt; returns False and halts over the limit."""
        now = time.monotonic()
        while self._order_times and now - self._order_times[0] > self.window_seconds:
            self._order_times.popleft()
        if len(self._order_times) >= self.max_orders_per_window:
            self._halt("order rate exceeded")
            return False
        self._order_times.append(now)
        return True

    def reserve_orders(self, n: int) -> bool:
        """Atomically reserve capacity for n orders of one ENTRY.

        Returns False (and halts per record_order) if the window cannot
        take all n - the caller MUST abort the entry. Fixes the bug where
        the second leg's HALT was ignored and the entry executed anyway.
        """
        for _ in range(n):
            if not self.record_order():
                return False
        return True

    def record_exit(self) -> None:
        """Track a risk-reducing exit order WITHOUT halting: an unwind
        must always be allowed through, even over the entry rate limit."""
        now = time.monotonic()
        self._order_times.append(now)

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        self._persist()
        if self._consecutive_failures > self.max_consecutive_failures:
            self._halt("consecutive failures exceeded")

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._persist()

    def record_pnl_usd(self, amount: Decimal) -> None:
        self._maybe_roll_day()
        self._daily_pnl_usd += D(amount)
        self._persist()
        if self._daily_pnl_usd + self._mtm_usd <= -D(self.max_daily_loss_usd):
            self._halt("daily loss limit exceeded")

    def reconciliation_mismatch(self) -> None:
        self._halt("reconciliation mismatch: local state vs exchange balances")

    def reset_daily(self) -> None:
        """New trading day: PnL resets; a halt does NOT auto-clear."""
        self._daily_pnl_usd = Decimal("0")
        self._day = self._today()
        self._persist()

    def manual_reset(self) -> None:
        """Explicit operator reset: the ONLY way a persisted halt clears."""
        self.halted = False
        self.halt_reason = None
        self._consecutive_failures = 0
        self._persist()
