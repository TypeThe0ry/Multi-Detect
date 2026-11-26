"""`_progress(phase, **fields)` takes the phase positionally; a `phase=` field keyword raises TypeError at runtime."""
import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "collect_sim_teacher_episodes.py"


def test_progress_calls_never_pass_phase_keyword():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
        if name == "_progress" and any(keyword.arg == "phase" for keyword in node.keywords):
            bad.append(node.lineno)
    assert not bad, f"_progress called with a phase= keyword at lines {bad}"
