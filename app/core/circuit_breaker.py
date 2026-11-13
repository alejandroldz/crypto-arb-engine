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


@dataclass
class CircuitBreaker:
    max_consecutive_failures: int
    max_daily_loss_usd: Decimal
    max_orders_per_window: int = 6
    window_seconds: int = 60
    halted: bool = False
    halt_reason: str | None = None
    state_path: str | None = None
    on_halt: object = None  # callable(reason); op.#17: halt must alert
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
                    pnl = D(st.get("daily_pnl_usd", "0"))
                    # fuzz F28/F29: NaN/Infinity on disk would either crash
                    # the next record or silently defeat the daily limit.
                    # Raising here hits the fail-closed handler below.
                    if not pnl.is_finite():
                        raise ValueError(f"non-finite daily_pnl_usd {pnl!r}")
                    self._daily_pnl_usd = pnl
                if self.halted:
                    logger.warning(
                        "breaker restored HALT from disk: %s", self.halt_reason
                    )
            except Exception as exc:
                # caos H2: an unreadable state file may hide a real HALT.
                # Fail closed: assume halted until a human reviews it.
                self.halted = True
                self.halt_reason = f"state file unreadable: {exc}"
                logger.error("breaker state unreadable, failing CLOSED (halted): %s", exc)

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _persist(self) -> None:
        if not self.state_path:
            return
        # caos H3: disk failures (ENOSPC) must not kill the engine - this runs
        # inside error handlers. In-memory state still protects the process.
        try:
            tmp = Path(self.state_path + ".tmp")
            tmp.write_text(json.dumps({
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "consecutive_failures": self._consecutive_failures,
            "day": self._day or self._today(),
            "daily_pnl_usd": str(self._daily_pnl_usd),
        }))
            tmp.replace(self.state_path)
        except OSError as exc:
            logger.error("breaker state persist failed (disk?): %s", exc)

    def _maybe_roll_day(self) -> None:
        """op.#8: 'daily' means a defined UTC day - roll even without restart."""
        today = self._today()
        # caos H1: `!=` let a clock jump BACK erase the accumulated daily
        # loss. ISO dates compare lexicographically; roll forward only.
        if self._day and today <= self._day:
            return
        self._day = today
        self._daily_pnl_usd = Decimal("0")
        self._persist()

    def seed_daily_pnl(self, amount: Decimal) -> None:
        """op.#8: rebuild today's realized PnL from the ledger at startup."""
        self._day = self._today()
        amount = D(amount)
        if not amount.is_finite():
            # fuzz F11/F18: a NaN from the ledger (NULL pnl sums) must not
            # seed garbage; fail loud instead of silently trading on.
            raise ValueError(f"daily pnl seed must be finite, got {amount!r}")
        self._daily_pnl_usd = amount
        if self._daily_pnl_usd <= -D(self.max_daily_loss_usd):
            self._halt("daily loss limit exceeded")
        self._persist()

    def record_mtm_usd(self, amount: Decimal) -> None:
        """op.#8: mark-to-market of open exposure counts toward the daily
        loss limit, not only realized closes."""
        amount = D(amount)
        if not amount.is_finite():
            # fuzz F46: same guard as record_pnl_usd (F18) - a NaN here
            # escaped to the scan loop (main.py call site has no try)
            # instead of being dropped by the kill switch
            logger.error("non-finite mtm ignored by breaker: %r", amount)
            return
        self._maybe_roll_day()
        self._mtm_usd = amount
        if self._daily_pnl_usd + self._mtm_usd <= -D(self.max_daily_loss_usd):
            self._halt("daily loss limit exceeded (realized + MTM)")

    def _halt(self, reason: str) -> None:
        if self.halted and self.halt_reason == reason:
            return  # do not re-alert the same halt every cycle
        self.halted = True
        self.halt_reason = reason
        logger.warning("CIRCUIT BREAKER HALT: %s", reason)
        self._persist()
        if self.on_halt is not None:
            try:
                self.on_halt(reason)
            except Exception:
                logger.exception("on_halt callback failed")

    def halt(self, reason: str) -> None:
        """Public operator/policy halt (op.#17: e.g. real MMR emergency)."""
        self._halt(reason)

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

        Returns False if the window cannot take all n - the caller MUST
        abort the entry. Fixes the bug where the second leg's HALT was
        ignored and the entry executed anyway.

        caos H11: the old loop consumed partial slots before failing AND
        persisted a HALT although no real order was sent. Now a failed
        reservation consumes nothing and does not halt.
        """
        if n <= 0:
            return False  # fuzz F32b: a non-positive reservation is nonsense
        now = time.monotonic()
        while self._order_times and now - self._order_times[0] > self.window_seconds:
            self._order_times.popleft()
        if len(self._order_times) + n > self.max_orders_per_window:
            logger.warning(
                "order rate window full: cannot reserve %d slots (%d/%d used)",
                n, len(self._order_times), self.max_orders_per_window,
            )
            return False
        for _ in range(n):
            self._order_times.append(now)
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
        amount = D(amount)
        if not amount.is_finite():
            # fuzz F18: NaN/Infinity would silently defeat the daily limit
            logger.error("non-finite pnl ignored by breaker: %r", amount)
            return
        self._maybe_roll_day()
        self._daily_pnl_usd += amount
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
