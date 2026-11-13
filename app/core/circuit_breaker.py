"""Kill switch and rate limiting (RISK 6).

Halts all execution when any trip condition fires:
  1. more than N consecutive failed/unwound trades
  2. daily realized+unrealized drawdown beyond MAX_DAILY_LOSS_EUR
  3. more than M orders within the rolling window
  4. reconciliation mismatch between local state and exchange balances
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

from app.core.decimals import D


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
    max_daily_loss_eur: Decimal
    max_orders_per_window: int
    window_seconds: int
    halted: bool = False
    halt_reason: str | None = None
    _consecutive_failures: int = 0
    _daily_pnl_eur: Decimal = Decimal("0")
    _order_times: deque = field(default_factory=deque)

    def _halt(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason

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

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures > self.max_consecutive_failures:
            self._halt("consecutive failures exceeded")

    def record_success(self) -> None:
        self._consecutive_failures = 0

    def record_pnl_eur(self, amount: Decimal) -> None:
        self._daily_pnl_eur += D(amount)
        if self._daily_pnl_eur <= -D(self.max_daily_loss_eur):
            self._halt("daily loss limit exceeded")

    def reconciliation_mismatch(self) -> None:
        self._halt("reconciliation mismatch: local state vs exchange balances")

    def reset_daily(self) -> None:
        """New trading day: PnL resets; a halt does NOT auto-clear."""
        self._daily_pnl_eur = Decimal("0")

    def manual_reset(self) -> None:
        self.halted = False
        self.halt_reason = None
        self._consecutive_failures = 0
