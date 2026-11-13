"""RISK 6: kill switch and order-rate limiter."""
import time
from decimal import Decimal

from app.core.circuit_breaker import CircuitBreaker, TokenBucket


def make_breaker() -> CircuitBreaker:
    return CircuitBreaker(
        max_consecutive_failures=3,
        max_daily_loss_eur=Decimal("25"),
        max_orders_per_window=5,
        window_seconds=60,
    )


def test_halts_after_more_than_3_consecutive_failures():
    cb = make_breaker()
    for _ in range(3):
        cb.record_failure()
    assert not cb.halted  # exactly 3 is still allowed
    cb.record_failure()
    assert cb.halted and "failures" in cb.halt_reason


def test_success_resets_failure_streak():
    cb = make_breaker()
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    cb.record_failure()
    assert not cb.halted


def test_daily_loss_limit_trips():
    cb = make_breaker()
    cb.record_pnl_eur(Decimal("-20"))
    assert not cb.halted
    cb.record_pnl_eur(Decimal("-6"))  # cumulative -26 <= -25
    assert cb.halted and "daily loss" in cb.halt_reason


def test_order_rate_limit_trips():
    cb = make_breaker()
    for _ in range(5):
        assert cb.record_order()
    assert not cb.record_order()
    assert cb.halted and "rate" in cb.halt_reason


def test_reconciliation_mismatch_halts():
    cb = make_breaker()
    cb.reconciliation_mismatch()
    assert cb.halted and "reconciliation" in cb.halt_reason


def test_manual_reset_clears_halt_but_not_daily_reset():
    cb = make_breaker()
    cb.record_pnl_eur(Decimal("-30"))
    assert cb.halted
    cb.reset_daily()
    assert cb.halted  # halt survives a day rollover; needs a human
    cb.manual_reset()
    assert not cb.halted


def test_token_bucket():
    bucket = TokenBucket(capacity=2, refill_per_second=1)
    assert bucket.allow()
    assert bucket.allow()
    assert not bucket.allow()
    time.sleep(1.05)
    assert bucket.allow()
