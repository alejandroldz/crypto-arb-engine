"""Circuit breaker: trading halts on the kill switch (RISK 3)."""
import pytest
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
    # caos H11: no real order was sent, so a failed reservation must NOT
    # halt (and must consume nothing) - the caller sees False and aborts.
    assert not b.halted
    assert b.reserve_orders(1) is True


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


def test_f46_record_mtm_ignores_non_finite(tmp_path):
    """F46: NaN mtm used to raise InvalidOperation out of the scan loop;
    the kill switch drops it like record_pnl_usd does."""
    from app.core.circuit_breaker import CircuitBreaker

    cb = CircuitBreaker(max_consecutive_failures=3,
                        max_daily_loss_usd=Decimal("25"),
                        max_orders_per_window=5, window_seconds=60,
                        state_path=str(tmp_path / "cb.json"))
    cb.record_mtm_usd(Decimal("NaN"))  # no raise
    cb.record_mtm_usd(Decimal("Infinity"))
    assert not cb.halted
    cb.record_mtm_usd(Decimal("-100"))  # still works after the junk
    assert cb.halted


def test_f47_order_window_cannot_be_disabled_by_typo():
    """F47: cb_order_window_seconds=0 silently disabled the rate limit."""
    import pydantic
    from app.core.config import Settings

    with pytest.raises(pydantic.ValidationError):
        Settings(cb_order_window_seconds=0)
    with pytest.raises(pydantic.ValidationError):
        Settings(cb_max_orders_per_window=0)
    with pytest.raises(pydantic.ValidationError):
        Settings(cb_max_consecutive_failures=-1)


def test_f57_risk_decimals_have_bounds():
    """F57: config typos silently disabled RISK protections or broke
    sizing; each now fails loud at startup."""
    import pydantic
    from app.core.config import Settings

    bad = [
        {"slippage_buffer_bps": Decimal("-1000")},   # negative costs
        {"liquidation_buffer_pct": Decimal("-15")},  # RISK 5 off
        {"liquidation_buffer_pct": Decimal("0")},
        {"uta_capital_utilization_pct": Decimal("1.5")},  # over-deploy
        {"min_collateral_ratio_required": Decimal("-1")},
        {"max_account_mmr_warning": Decimal("0")},
        {"max_account_mmr_emergency_unwind": Decimal("1e9")},
        {"sim_maintenance_margin_rate": Decimal("0")},  # all UTA gates open
        {"liquidity_haircut": Decimal("100")},
    ]
    for kwargs in bad:
        with pytest.raises(pydantic.ValidationError), \
                pytest.MonkeyPatch().context() as m:
            # env must not leak real settings into the constructor
            for k in list(kwargs):
                m.delenv(k.upper(), raising=False)
            Settings(**kwargs)
    Settings()  # defaults still valid


def test_f58_screener_interval_cannot_hot_loop():
    """F58: interval 0 hammered the public APIs at 100% CPU (ban risk)."""
    import pydantic
    from app.core.config import Settings

    with pytest.raises(pydantic.ValidationError):
        Settings(screener_interval_s=0)
    with pytest.raises(pydantic.ValidationError):
        Settings(screener_interval_s=30)
    Settings(screener_interval_s=60)
    # hygiene bounds on the remaining ints
    with pytest.raises(pydantic.ValidationError):
        Settings(max_leg_delay_ms=0)
    with pytest.raises(pydantic.ValidationError):
        Settings(book_retention_days=0)
    with pytest.raises(pydantic.ValidationError):
        Settings(pp_holding_hours=-1)
