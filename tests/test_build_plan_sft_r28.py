import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import build_plan_sft as sft  # noqa: E402
from multidetect.autonomy.capability_contract import capability_violations  # noqa: E402
from multidetect.autonomy.plan_intent import check_plan_intent  # noqa: E402
from train_slow_system_lora import admit_train_rows  # noqa: E402


def test_r28_rows_cover_stop_semantics_and_fixed_wing_capability_pairs(tmp_path):
    output = tmp_path / "r28"
    assert sft.main([
        "--output", str(output), "--version", "v4", "--rows", "30", "--val-rows", "0",
        "--r23-v3-count", "0", "--r23-deliver-count", "0",
        "--r24-reference-count", "0", "--r24-context-count", "0",
        "--r27-refusal-count", "2", "--r27-complex-count", "10",
        "--r28-recipe", "--r28-stop-count", "12", "--r28-fixed-wing-count", "4",
        "--r28-return-count", "6",
    ]) == 0
    rows = [json.loads(line) for line in (output / "train.jsonl").read_text(encoding="utf-8").splitlines()]
    r28 = [row for row in rows if row.get("world") == "synthetic_plan_r28"]
    by_family = {}
    for family in {row["family"] for row in r28}:
        by_family[family] = [row for row in r28 if row["family"] == family]

    stop_rows = by_family["r28_stop_threshold"]
    assert len(stop_rows) == 12
    assert Counter("zh" if any("\u4e00" <= char <= "\u9fff" for char in row["task_text"]) else "en"
                   for row in stop_rows) == {"zh": 6, "en": 6}
    assert {row["assistant_target"]["steps"][0]["skill"] for row in stop_rows} == {
        "count", "area_search", "patrol",
    }
    assert sum(row["assistant_target"]["steps"][-1]["skill"] == "land" for row in stop_rows) == 3
    assert all(row["assistant_target"]["steps"][-1]["skill"] == "land"
               for row in stop_rows if any(term in row["task_text"].casefold()
                                           for term in ("降落", "着陆", "落地", "land", "touch down")))

    return_rows = by_family["r28_return_intent"]
    assert len(return_rows) == 6
    scooter = next(row for row in return_rows if "电瓶车" in row["task_text"])
    assert [step["skill"] for step in scooter["assistant_target"]["steps"]] == ["count", "return_home"]

    for family, capability in (("r28_fixed_wing_hover", "hover"),
                               ("r28_fixed_wing_target_land", "landing_on_target")):
        examples = by_family[family]
        assert len(examples) == 4
        assert Counter(row["pair_role"] for row in examples) == {"positive": 2, "refusal": 2}
        for row in examples:
            candidate = row.get("rejected_candidate", row["assistant_target"])
            if row["pair_role"] == "positive":
                assert not check_plan_intent(candidate, row["task_text"], tuple(row["refs"]))
                assert not capability_violations(candidate, row["capabilities"])
            else:
                findings = capability_violations(candidate, row["capabilities"],
                                                 airframe="airframe:fixed_wing")
                assert any(item["code"] == "missing_capability" for item in findings)
                assert row["assistant_target"]["reason_code"] == "missing_capability"

    held_out = {
        sft.normalize_task(task["text"])
        for path in (ROOT / "configs/onboard").glob("plan-eval-tasks*.json")
        for task in json.loads(path.read_text(encoding="utf-8"))["tasks"]
    }
    assert not held_out & {sft.normalize_task(row["task_text"]) for row in rows}
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["r28_recipe"]["structured_refusal_share_train"] <= 0.02
    admitted, _ = admit_train_rows(output)
    assert len(admitted) == len(rows)
