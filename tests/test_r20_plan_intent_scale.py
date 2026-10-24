import json
from collections import Counter
from pathlib import Path

from multidetect.autonomy.capability_contract import capability_violations
from multidetect.autonomy.mission_plan import validate_mission_plan
from multidetect.autonomy.plan_intent import check_plan_intent

ROOT = Path(__file__).resolve().parents[1]


def _held_out():
    return {
        " ".join(task["text"].split())
        for path in (ROOT / "configs/onboard").glob("plan-eval-tasks*.json")
        for task in json.loads(path.read_text(encoding="utf-8"))["tasks"]
    }


def test_r20_scale_count_distribution_and_rule_contract(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    sys.path.insert(0, str(ROOT / "src"))
    import build_plan_sft

    output = tmp_path / "r20"
    assert build_plan_sft.main([
        "--output", str(output), "--r20-plan-intent", "--r20-plan-intent-count", "3",
        "--require-skills",
    ]) == 0
    rows = [json.loads(line) for line in (output / "train.jsonl").read_text(encoding="utf-8").splitlines()]
    families = Counter(row["family"] for row in rows)
    assert len(rows) == 42
    assert set(families) == {
        "return_intent", "unknown_place", "missing_payload_drop", "fixed_wing_constraint",
        "fixed_wing_target_land", "ref_alias", "query_grounding",
    }
    assert all(count == 6 for count in families.values())
    assert all(Counter(row["pair_role"] for row in rows if row["family"] == family) ==
               {"positive": 3, "paired_negative": 3} for family in families)
    held = _held_out()
    assert not ({" ".join(row["task_text"].split()) for row in rows} & held)
    for row in rows:
        plan = row["assistant_target"]
        refs = tuple(row["refs"])
        intent = check_plan_intent(plan, row["task_text"], refs)
        capabilities = tuple(row["capabilities"])
        capability = capability_violations(
            plan, capabilities,
            airframe="airframe:fixed_wing" if "airframe:fixed_wing" in capabilities else None,
        )
        codes = {finding.code for finding in intent} | {finding["code"] for finding in capability}
        if row["expected_valid"]:
            validate_mission_plan(plan, capabilities=frozenset(capabilities))
            assert not codes, (row["family"], row["task_text"], codes)
        else:
            assert row["expected_findings"][0] in codes


def test_r20_mix_keeps_normal_backbone_rows(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_plan_sft

    output = tmp_path / "mixed"
    assert build_plan_sft.main([
        "--output", str(output), "--version", "v4", "--rows", "10", "--val-rows", "2",
        "--r20-plan-intent", "--r20-plan-intent-count", "1", "--r20-plan-intent-mix",
        "--require-skills",
    ]) == 0
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["rows"] == {"train": 24, "val": 2}
    assert manifest["family_counts"]["train"]["v4_backbone"] == 10
    assert sum(v for k, v in manifest["family_counts"]["train"].items() if k != "v4_backbone") == 14
