"""Development Rule 1 linter: no floats inside app/engines."""
import ast
from pathlib import Path

ENGINES_DIR = Path(__file__).resolve().parent.parent / "app" / "engines"


def _float_violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            problems.append(f"{path.name}:{node.lineno} float literal {node.value}")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "float"
        ):
            problems.append(f"{path.name}:{node.lineno} float() call")
        if isinstance(node, ast.Name) and node.id == "float" and isinstance(
            node.ctx, ast.Load
        ):
            if not any("float()" in p for p in problems):
                problems.append(f"{path.name}:{node.lineno} reference to 'float'")
    return problems


def test_no_float_usage_in_engines():
    violations: list[str] = []
    for path in sorted(ENGINES_DIR.glob("*.py")):
        violations.extend(_float_violations(path))
    assert not violations, "float usage in engines/:\n" + "\n".join(violations)
