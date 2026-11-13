"""Chaos-regression tests for the circuit breaker (caos H1/H2/H3/H11, fuzz F18)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.circuit_breaker import CircuitBreaker


def _breaker(tmp_path, **kw):
    kw.setdefault("state_path", str(tmp_path / "breaker_state.json"))
    return CircuitBreaker(
        max_consecutive_failures=3, max_daily_loss_usd=Decimal("100"), **kw
    )


def test_h1_clock_jump_back_does_not_reset_daily_loss(tmp_path, monkeypatch):
    b = _breaker(tmp_path)
    b.record_pnl_usd(Decimal("-80"))
    assert b._daily_pnl_usd == Decimal("-80")
    today = b._day
    # clock jumps back one day
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    monkeypatch.setattr(CircuitBreaker, "_today", staticmethod(lambda: yesterday))
    b.record_pnl_usd(Decimal("-1"))
    assert b._daily_pnl_usd == Decimal("-81")
    assert b._day == today
    # forward roll still works
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    monkeypatch.setattr(CircuitBreaker, "_today", staticmethod(lambda: tomorrow))
    b.record_pnl_usd(Decimal("-1"))
    assert b._daily_pnl_usd == Decimal("-1")
    assert b._day == tomorrow


def test_h2_corrupt_state_file_fails_closed(tmp_path):
    path = tmp_path / "breaker_state.json"
    path.write_text("{not json!!")
    b = _breaker(tmp_path)
    assert b.halted is True
    assert "unreadable" in (b.halt_reason or "")


def test_h3_persist_failure_does_not_raise(tmp_path, monkeypatch):
    b = _breaker(tmp_path)
    from pathlib import Path
    monkeypatch.setattr(
        Path, "write_text",
        lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left on device")),
    )
    b.record_failure()  # must not raise
    assert b._consecutive_failures == 1


def test_h11_partial_reservation_refunds_and_does_not_halt(tmp_path):
    b = _breaker(tmp_path, max_orders_per_window=2)
    assert b.reserve_orders(2) is True
    # window full: a 2-slot reservation must fail WITHOUT consuming or halting
    assert b.reserve_orders(2) is False
    assert b.halted is False
    # single exit tracking still works
    b.record_exit()
    assert len(b._order_times) == 3


def test_f18_nonfinite_pnl_rejected(tmp_path):
    b = _breaker(tmp_path)
    b.record_pnl_usd(Decimal("NaN"))
    assert b._daily_pnl_usd == Decimal("0")
    with pytest.raises(ValueError, match="finite"):
        b.seed_daily_pnl(Decimal("Infinity"))


def test_f28_f29_nonfinite_daily_pnl_on_disk_fails_closed(tmp_path):
    for bad in ("NaN", "Infinity", "-Infinity"):
        path = tmp_path / "breaker_state.json"
        path.write_text(json.dumps({
            "halted": False, "halt_reason": None, "consecutive_failures": 0,
            "day": datetime.now(timezone.utc).date().isoformat(),
            "daily_pnl_usd": bad,
        }))
        b = _breaker(tmp_path)
        assert b.halted is True, bad


def test_f32b_nonpositive_reservation_refused(tmp_path):
    b = _breaker(tmp_path)
    assert b.reserve_orders(0) is False
    assert b.reserve_orders(-5) is False
    assert len(b._order_times) == 0
