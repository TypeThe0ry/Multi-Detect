import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import build_plan_sft as sft  # noqa: E402
from train_slow_system_lora import admit_train_rows  # noqa: E402


def test_r27_refusals_are_narrow_paired_and_below_cap(tmp_path):
    output = tmp_path / "r27"
    assert sft.main([
        "--output", str(output), "--version", "v4", "--rows", "20", "--val-rows", "2",
        "--r27-recipe", "--r27-refusal-count", "2", "--r27-complex-count", "10",
        "--r23-v3-count", "0", "--r23-deliver-count", "0",
        "--r24-reference-count", "0", "--r24-context-count", "0",
    ]) == 0
    rows = [json.loads(line) for line in (output / "train.jsonl").read_text(encoding="utf-8").splitlines()]
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    r27 = [row for row in rows if row.get("world") == "synthetic_plan_r27"]
    refusals = [row for row in r27 if row["assistant_target"].get("decision") in {"reject", "clarify"}]
    assert {row["reason_code"] for row in refusals} == {"missing_capability", "unknown_place"}
    assert len(refusals) / len(rows) <= 0.02
    counts = Counter(row["reason_code"] for row in refusals)
    assert counts == {"missing_capability": 2, "unknown_place": 2}
    for row in refusals:
        positives = [item for item in r27 if item.get("pair_role") == "positive"
                     and item.get("reason_code") == row["reason_code"]]
        assert len(positives) == 2
        if row["reason_code"] == "missing_capability":
            assert "Payload drop is unavailable." in row["prompt"]
        else:
            assert row["assistant_target"]["unresolved_refs"]
    assert manifest["r27_recipe"]["structured_refusal_share_train"] <= 0.02
    assert manifest["r27_recipe"]["complex_positive_requested"] == 10
    admitted, _ = admit_train_rows(output)
    assert len(admitted) == len(rows)


def test_r27_complex_rows_are_holdout_disjoint_and_have_all_requested_families(tmp_path):
    output = tmp_path / "r27"
    assert sft.main([
        "--output", str(output), "--version", "v4", "--rows", "60", "--val-rows", "0",
        "--r27-recipe", "--r27-refusal-count", "4", "--r27-complex-count", "20",
        "--r23-v3-count", "0", "--r23-deliver-count", "0",
        "--r24-reference-count", "0", "--r24-context-count", "0",
    ]) == 0
    rows = [json.loads(line) for line in (output / "train.jsonl").read_text(encoding="utf-8").splitlines()]
    complex_rows = [row for row in rows if row.get("world") == "synthetic_plan_r27"
                    and row.get("pair_role") is None]
    families = {row["family"] for row in complex_rows}
    assert len(complex_rows) >= 20
    assert {"r27_multi_step", "r27_colloquial", "r27_negation", "r27_mapping",
            "r27_monitoring", "r27_counting", "r27_local_position"} <= families
    held_out = {
        sft.normalize_task(task["text"])
        for path in (ROOT / "configs/onboard").glob("plan-eval-tasks*.json")
        for task in json.loads(path.read_text(encoding="utf-8"))["tasks"]
    }
    assert not held_out & {sft.normalize_task(row["task_text"]) for row in rows}
    assert all(row.get("expected_valid") is True for row in complex_rows)
    admitted, _ = admit_train_rows(output)
    assert len(admitted) == len(rows)
