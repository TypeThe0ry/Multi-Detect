import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_plan_sft as sft  # noqa: E402


def test_r31_small_recipe_is_balanced_and_holdout_disjoint(tmp_path):
    output = tmp_path / "r31"
    assert sft.main([
        "--output", str(output), "--version", "v4", "--rows", "1", "--val-rows", "0",
        "--r31-recipe", "--replay-planner-train", str(ROOT / "artifacts/datasets/plan-sft-v4/train.jsonl"),
        "--r31-direct-follow-count", "4", "--r31-step-fidelity-count", "4",
        "--r31-terminal-intent-count", "4", "--r31-grounded-refs-count", "4",
    ]) == 0
    rows = [json.loads(line) for line in (output / "train.jsonl").read_text(encoding="utf-8").splitlines()]
    generated = [row for row in rows if row.get("world") == "synthetic_plan_r31"]
    assert len(rows) == 1516
    assert Counter(row["family"] for row in generated) == {
        "direct_follow": 4, "step_fidelity": 4, "terminal_intent": 4, "grounded_refs": 4,
    }
    assert Counter("zh" if re.search(r"[\u3400-\u9fff]", row["task_text"]) else "en"
                   for row in generated) == {"zh": 8, "en": 8}
    assert all(row["expected_valid"] and not row["expected_findings"] for row in generated)
    assert not any(row.get("assistant_target", {}).get("decision") in {"reject", "clarify"}
                   for row in generated)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["r31_recipe"]["replay_rows"] == 1500
    assert manifest["r31_recipe"]["holdout_collisions"] == 0
    assert manifest["r31_recipe"]["structured_refusal_rows"] == 0
