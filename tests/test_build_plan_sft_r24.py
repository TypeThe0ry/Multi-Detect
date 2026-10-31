import hashlib
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


def _rows(output):
    return [json.loads(line) for line in (output / "train.jsonl").read_text(encoding="utf-8").splitlines()]


def test_r24_families_are_grounded_admitted_and_holdout_disjoint(tmp_path):
    output = tmp_path / "r24"
    assert sft.main([
        "--output", str(output), "--version", "v4", "--rows", "60", "--val-rows", "8",
        "--r24-recipe", "--r23-v3-count", "6", "--r23-deliver-count", "4",
        "--r24-reference-count", "18", "--r24-unknown-count", "12",
        "--r24-context-count", "12", "--negative-target-mode", "rejected_candidate",
        "--require-skills",
    ]) == 0
    rows = _rows(output)
    counts = Counter(row.get("family") for row in rows)
    assert counts["r24_reference_type"] == 18
    assert counts["r24_unknown_place"] == 12
    assert counts["r24_context_land"] + counts["r24_context_return"] == 12
    r24_rows = [row for row in rows if str(row.get("family", "")).startswith("r24_")]
    assert any(re.search(r"[\u4e00-\u9fff]", row["task_text"]) for row in r24_rows)
    assert any(not re.search(r"[\u4e00-\u9fff]", row["task_text"]) for row in r24_rows)
    for family in ("r24_reference_type", "r24_unknown_place", "r24_context_land", "r24_context_return"):
        subset = [row for row in rows if row.get("family") == family]
        assert len({sft.normalize_task(row["task_text"]) for row in subset}) == len(subset)
        assert all(not check_plan_intent(row["assistant_target"], row["task_text"], row.get("refs", ()))
                   for row in subset if row.get("expected_valid", True))

    typed = [row for row in rows if row.get("family") == "r24_reference_type"]
    assert any(any(ref.startswith("poi:bridge ") for ref in row["refs"])
               and "region" not in row["assistant_target"]["steps"][1]["params"]
               for row in typed if row["assistant_target"]["steps"][1]["skill"] == "hover")
    assert any(any(ref.startswith("poi:bridge ") for ref in row["refs"])
               and any(ref.startswith("zone:bridge ") for ref in row["refs"])
               and row["assistant_target"]["steps"][0]["params"].get("region") == "zone:bridge"
               for row in typed if row["assistant_target"]["steps"][0]["skill"] == "monitor")
    unknown = [row for row in rows if row.get("family") == "r24_unknown_place"]
    assert all(row["negative_target_mode"] == "rejected_candidate"
               and row["expected_findings"] == ["ungrounded_place"]
               and row["rejected_candidate"] == row["assistant_target"] for row in unknown)
    assert all(not (set(ref.split(" ", 1)[0] for ref in row["refs"])
                    & {step["params"].get("target_ref") for step in row["assistant_target"]["steps"]})
               for row in unknown)
    contextual = [row for row in rows if str(row.get("family", "")).startswith("r24_context_")]
    assert all(not any("target_ref" in step["params"] for step in row["assistant_target"]["steps"])
               for row in contextual)

    holdout = set()
    for path in (ROOT / "configs/onboard").glob("plan-eval-tasks*.json"):
        holdout |= {sft.normalize_task(task["text"])
                    for task in json.loads(path.read_text(encoding="utf-8"))["tasks"]}
    assert not holdout & {sft.normalize_task(row["task_text"]) for row in rows}
    admitted, _ = admit_train_rows(output)
    assert len(admitted) == 60 + 6 + 4 + 18 + 12 + 12


def test_r23_recipe_bytes_remain_stable_after_r24_addition(tmp_path):
    # The existing v5 suite pins v4 byte-for-byte; this pins the r23 opt-in
    # output at a representative size, including its stats sidecar.
    output = tmp_path / "r23"
    assert sft.main(["--output", str(output), "--version", "v4", "--rows", "80",
                     "--val-rows", "5", "--r23-recipe", "--r23-v3-count", "12",
                     "--r23-deliver-count", "6", "--require-skills"]) == 0
    expected = {
        "manifest.json": "cc99d9159619cde55900b3c564430eb1d89442eda2c8e0fe62904bfa0ed2118c",
        "train.jsonl": "1ee109ca33514d0535009da9a4d66fded685738471c51e5375d2414a049a79d6",
        "val.jsonl": "4ae517f5889090c5d30e492f172ee8a107532c5b857497f8b09290f00fe18ca6",
        "r23-data-stats.md": "28a50eb1d2002d824c7f85205291dbfc1498bfc53dbae7fcc047f9d813a765f7",
    }
    for filename, digest in expected.items():
        assert hashlib.sha256((output / filename).read_bytes()).hexdigest() == digest
