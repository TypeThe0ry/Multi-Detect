import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import build_plan_sft as sft  # noqa: E402
from multidetect.autonomy.plan_intent import check_plan_intent  # noqa: E402
from train_slow_system_lora import admit_train_rows  # noqa: E402


def test_r30_targets_follow_order_terminals_and_refs_without_leakage(tmp_path):
    output = tmp_path / "r30"
    assert sft.main([
        "--output", str(output), "--version", "v4", "--rows", "1", "--val-rows", "0",
        "--require-skills", "--r30-recipe", "--r23-v3-count", "0", "--r23-deliver-count", "0",
        "--r24-reference-count", "0", "--r24-unknown-count", "0", "--r24-context-count", "0",
        "--r30-direct-follow-count", "4", "--r30-step-fidelity-count", "4",
        "--r30-terminal-intent-count", "4", "--r30-grounded-refs-count", "4",
    ]) == 0
    rows = [json.loads(line) for line in (output / "train.jsonl").read_text(encoding="utf-8").splitlines()]
    generated = [row for row in rows if row.get("world") == "synthetic_plan_r30"]
    assert len(generated) == 16
    assert Counter(row["family"] for row in generated) == {
        "direct_follow": 4, "step_fidelity": 4,
        "terminal_intent": 4, "grounded_refs": 4,
    }
    assert Counter(
        "zh" if re.search(r"[\u3400-\u9fff]", row["task_text"]) else "en"
        for row in generated
    ) == {"zh": 8, "en": 8}
    follow = [row for row in generated if row["family"] == "direct_follow"]
    assert all(row["assistant_target"]["steps"][0]["skill"] == "follow" for row in follow)
    assert all("area_search" not in row["task_text"] for row in follow)
    assert all("area_search" not in [step["skill"] for step in row["assistant_target"]["steps"]]
               for row in follow)
    assert all(row["expected_valid"] is True and not row["expected_findings"] for row in generated)
    assert all(not check_plan_intent(row["assistant_target"], row["task_text"], tuple(row["refs"]))
               for row in generated)
    holdout = {
        sft.normalize_task(task["text"])
        for path in (ROOT / "configs/onboard").glob("plan-eval-tasks*.json")
        for task in json.loads(path.read_text(encoding="utf-8"))["tasks"]
    }
    holdout |= {
        sft.normalize_task(task["text"])
        for task in json.loads((ROOT / "configs/onboard/plan-probe-tasks-return-home.json").read_text(encoding="utf-8"))["tasks"]
    }
    holdout |= {
        sft.normalize_task(task["task_text"])
        for path in (ROOT / "configs/onboard").glob("suite-follow*.json")
        for task in json.loads(path.read_text(encoding="utf-8")).get("tasks", ())
    }
    assert not holdout & {sft.normalize_task(row["task_text"]) for row in rows}
    admitted, _ = admit_train_rows(output)
    assert len(admitted) == len(rows)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["r30_recipe"]["holdout_collisions"] == 0
    assert manifest["r30_recipe"]["structured_refusal_rows"] == 0
