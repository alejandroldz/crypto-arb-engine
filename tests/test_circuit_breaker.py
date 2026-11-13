"""Circuit breaker: trading halts on the kill switch (RISK 3)."""
from decimal import Decimal

from app.core.decimals import D
from app.core.circuit_breaker import CircuitBreaker


def breaker() -> CircuitBreaker:
    return CircuitBreaker(max_consecutive_failures=3, max_daily_loss_usd=Decimal("500"))


def test_halts_after_more_than_3_consecutive_failures():
    cb = breaker()
    for _ in range(3):
        cb.record_failure()
    assert not cb.halted
    cb.record_failure()
    assert cb.halted and cb.halt_reason == "consecutive failures exceeded"


def test_success_resets_failure_streak():
    cb = breaker()
    for _ in range(3):
        cb.record_failure()
    cb.record_success()
    for _ in range(3):
        cb.record_failure()
    assert not cb.halted


def test_daily_loss_limit_trips():
    cb = breaker()
    cb.record_pnl_usd(Decimal("-400"))
    assert not cb.halted
    cb.record_pnl_usd(Decimal("-150"))
    assert cb.halted and cb.halt_reason == "daily loss limit exceeded"


def test_reset_daily_resets_pnl_but_not_halt():
    cb = breaker()
    cb.record_pnl_usd(Decimal("-550"))
    assert cb.halted
    cb.reset_daily()
    assert cb.halted  # halt needs a manual reset
    cb.manual_reset()
    assert not cb.halted and cb.halt_reason is None


def test_order_rate_limit_trips():
    cb = CircuitBreaker(max_consecutive_failures=3, max_daily_loss_usd=Decimal("500"), max_orders_per_window=2)
    assert cb.record_order()
    assert cb.record_order()
    assert not cb.record_order()
    assert cb.halted and cb.halt_reason == "order rate exceeded"


def test_reconciliation_mismatch_halts():
    cb = breaker()
    cb.reconciliation_mismatch()
    assert cb.halted and cb.halt_reason.startswith("reconciliation mismatch")


def test_manual_reset_clears_halt():
    cb = breaker()
    for _ in range(4):
        cb.record_failure()
    assert cb.halted
    cb.manual_reset()
    assert not cb.halted and cb.halt_reason is None


def test_reserve_orders_aborts_atomically_when_window_full():
    """Fix: an entry whose 2nd leg would HALT must not execute."""
    b = CircuitBreaker(max_consecutive_failures=3, max_daily_loss_usd=Decimal("25"),
                       max_orders_per_window=1)
    assert b.reserve_orders(2) is False
    assert b.halted  # the HALT fires...
    # ...and the caller must see False BEFORE sending anything


def test_reserve_orders_success_path():
    b = CircuitBreaker(max_consecutive_failures=3, max_daily_loss_usd=Decimal("25"),
                       max_orders_per_window=2)
    assert b.reserve_orders(2) is True
    assert not b.halted


def test_record_exit_never_halts():
    b = CircuitBreaker(max_consecutive_failures=3, max_daily_loss_usd=Decimal("25"),
                       max_orders_per_window=1)
    for _ in range(10):
        b.record_exit()
    assert not b.halted


def test_halt_persists_across_restart(tmp_path):
    state = str(tmp_path / "breaker.json")
    b = CircuitBreaker(
        max_consecutive_failures=1, max_daily_loss_usd=D("25"), state_path=state
    )
    b.record_pnl_usd(D("-30"))
    assert b.halted
    # a "restart" (new instance, same state file) stays halted
    b2 = CircuitBreaker(
        max_consecutive_failures=1, max_daily_loss_usd=D("25"), state_path=state
    )
    assert b2.halted and b2.halt_reason == "daily loss limit exceeded"
    # only an explicit operator reset clears it
    b2.manual_reset()
    b3 = CircuitBreaker(
        max_consecutive_failures=1, max_daily_loss_usd=D("25"), state_path=state
    )
    assert not b3.halted


def test_seed_daily_pnl_from_ledger_trips_and_rolls(tmp_path):
    state = str(tmp_path / "breaker.json")
    b = CircuitBreaker(
        max_consecutive_failures=1, max_daily_loss_usd=D("25"), state_path=state
    )
    b.seed_daily_pnl(D("-30"))  # restart reconstruction: yesterday's loss is in the ledger
    assert b.halted
    # a stale day on disk does not count against today
    import json
    from datetime import datetime, timezone
    st = json.loads((tmp_path / "breaker.json").read_text())
    st["halted"] = False
    st["day"] = "2000-01-01"
    st["daily_pnl_usd"] = "-30"
    (tmp_path / "breaker.json").write_text(json.dumps(st))
    b2 = CircuitBreaker(
        max_consecutive_failures=1, max_daily_loss_usd=D("25"), state_path=state
    )
    assert not b2.halted
    b2.record_pnl_usd(D("1"))  # rolls the day: -30 from 2000 no longer counts
    assert not b2.halted


def test_mtm_counts_toward_daily_loss(tmp_path):
    b = CircuitBreaker(
        max_consecutive_failures=1, max_daily_loss_usd=D("25"),
        state_path=str(tmp_path / "b.json"),
    )
    b.seed_daily_pnl(D("-20"))
    assert not b.halted
    b.record_mtm_usd(D("-10"))  # -20 realized + -10 open = beyond -25
    assert b.halted
    assert "MTM" in b.halt_reason
