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
