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


def test_r29_families_are_bilingual_admitted_and_holdout_disjoint(tmp_path):
    output = tmp_path / "r29"
    assert sft.main([
        "--output", str(output), "--version", "v4", "--rows", "1", "--val-rows", "0",
        "--require-skills", "--r29-recipe", "--r23-v3-count", "0", "--r23-deliver-count", "0",
        "--r24-reference-count", "0", "--r24-unknown-count", "0", "--r24-context-count", "0",
        "--r29-query-ungrounded-count", "4", "--r29-return-negated-land-count", "4",
        "--r29-query-grounding-count", "4", "--r29-ref-alias-count", "4",
        "--r29-return-intent-count", "4",
    ]) == 0
    rows = [json.loads(line) for line in (output / "train.jsonl").read_text(encoding="utf-8").splitlines()]
    generated = [row for row in rows if row.get("world") == "synthetic_plan_r29"]
    assert len(generated) == 20
    counts = Counter(row["family"] for row in generated)
    assert counts == {"query_ungrounded": 4, "return_negated_land": 4,
                      "query_grounding": 4, "ref_alias": 4, "return_intent": 4}
    for family in counts:
        subset = [row for row in generated if row["family"] == family]
        assert Counter("zh" if re.search(r"[\u3400-\u9fff]", row["task_text"]) else "en"
                       for row in subset) == {"zh": 2, "en": 2}
        assert all(row["expected_valid"] is True and not row["expected_findings"] for row in subset)
        assert all(not check_plan_intent(row["assistant_target"], row["task_text"], tuple(row["refs"]))
                   for row in subset)
    excluded = {
        sft.normalize_task(task["text"])
        for path in (ROOT / "configs/onboard").glob("plan-eval-tasks*.json")
        for task in json.loads(path.read_text(encoding="utf-8"))["tasks"]
    }
    excluded |= {
        sft.normalize_task(task["text"])
        for task in json.loads((ROOT / "configs/onboard/plan-probe-tasks-return-home.json").read_text(encoding="utf-8"))["tasks"]
    }
    assert not excluded & {sft.normalize_task(row["task_text"]) for row in rows}
    admitted, _ = admit_train_rows(output)
    assert len(admitted) == len(rows)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["r29_recipe"]["structured_refusal_rows"] == 0
