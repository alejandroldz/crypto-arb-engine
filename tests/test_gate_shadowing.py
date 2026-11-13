"""Regression: the StalenessGate `gate` in the main loop must never be
shadowed. A first UTA-approved candidate once replaced it with the approval
result, so the NEXT candidate in the same queue batch crashed on
`gate.is_fresh(...)` with AttributeError. This AST guard fails on any
reassignment of the name `gate` in app/main.py."""
import ast
from pathlib import Path

MAIN = Path(__file__).resolve().parents[1] / "app" / "main.py"


def test_staleness_gate_name_is_never_reassigned():
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id == "gate"
    ]
    assert len(assignments) == 1, (
        f"`gate` assigned {len(assignments)} times in main.py; "
        "the StalenessGate must be assigned exactly once"
    )
    value = assignments[0].value
    assert isinstance(value, ast.Call)
    assert getattr(value.func, "id", "") == "StalenessGate"


def test_okx_hostname_allowlist():
    """Security: credentials only go to known OKX domains."""
    import pytest

    from app.core.config import Settings

    Settings(okx_hostname="my.okx.com")  # allowed
    with pytest.raises(Exception):
        Settings(okx_hostname="evil-okx.example.com")
    with pytest.raises(Exception):
        Settings(okx_hostname="my.okx.com.evil.com")


def test_telegram_failure_logged_without_token(caplog):
    """Security: a failing alert is visible, the token is not."""
    import asyncio

    from app.ui.notifier import TelegramNotifier

    n = TelegramNotifier(bot_token="SECRETTOKEN123", chat_id="42", timeout_s=1)
    with caplog.at_level("WARNING"):
        ok = asyncio.run(n.send("hi"))  # no network in tests -> fails
    assert ok is False
    assert any("telegram alert failed" in r.message for r in caplog.records)
    assert "SECRETTOKEN123" not in caplog.text
