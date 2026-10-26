import json
from collections import Counter
from pathlib import Path

import pytest

from multidetect.autonomy.capability_contract import capability_violations
from multidetect.autonomy.mission_plan import validate_mission_plan
from multidetect.autonomy.mission_planner import check_plan_refs
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
    assert len(rows) == 39
    assert set(families) == {
        "return_intent", "unknown_place", "missing_payload_drop", "fixed_wing_constraint",
        "fixed_wing_target_land", "ref_alias", "query_grounding",
    }
    assert families["query_grounding"] == 3
    assert all(count == 6 for family, count in families.items() if family != "query_grounding")
    assert Counter(row["pair_role"] for row in rows if row["family"] == "query_grounding") == {"positive": 3}
    assert all(Counter(row["pair_role"] for row in rows if row["family"] == family) ==
               {"positive": 3, "paired_negative": 3} for family in families if family != "query_grounding")
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
            candidate = row["rejected_candidate"]
            candidate_codes = {finding.code for finding in check_plan_intent(candidate, row["task_text"], refs)} | {
                finding["code"] for finding in capability_violations(candidate, capabilities,
                    airframe="airframe:fixed_wing" if "airframe:fixed_wing" in capabilities else None)
            }
            if check_plan_refs(candidate, refs, substitute=False)[2]:
                candidate_codes.add("ungrounded_place")
            assert row["expected_findings"][0] in candidate_codes
            assert row["rejected_candidate"]
            validate_mission_plan(plan, capabilities=frozenset(capabilities))
            if row["family"] == "missing_payload_drop":
                assert "Available aircraft capabilities:" not in row["prompt"]

    deliver = [row for row in rows if row["family"] == "missing_payload_drop"]
    positive = next(row for row in deliver if row["expected_valid"])
    negative = next(row for row in deliver if not row["expected_valid"])
    assert "Available aircraft capabilities: payload_drop." in positive["prompt"]
    assert "deliver" in {step["skill"] for step in negative["rejected_candidate"]["steps"]}


def test_train_admission_uses_each_rows_capabilities(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    from train_slow_system_lora import admit_train_rows

    def row(sample_id, capabilities):
        return {
            "sample_id": sample_id,
            "kind": "mission_plan",
            "world": "test",
            "task_text": "Deliver a parcel and return home.",
            "capabilities": capabilities,
            "assistant_target": {
                "plan_version": 1,
                "goal": "deliver",
                "steps": [
                    {"id": "s1", "skill": "deliver", "params": {"target_ref": "poi:gate", "payload_ref": "payload:parcel"}, "on_success": "s2", "on_fail": "s2"},
                    {"id": "s2", "skill": "return_home", "params": {}, "on_success": None, "on_fail": None},
                ],
                "report": [],
            },
            "split": "train",
        }

    dataset = tmp_path / "dataset"
    dataset.mkdir()
    rows = [row("with-payload", ["payload_drop"]), row("without-payload", [])]
    (dataset / "train.jsonl").write_text("".join(json.dumps(item) + "\n" for item in rows), encoding="utf-8")
    try:
        admit_train_rows(dataset)
    except ValueError as exc:
        assert "without-payload" in str(exc)
    else:
        raise AssertionError("deliver without payload_drop must fail admission")
    rows[1]["assistant_target"] = {
        "plan_version": 1, "goal": "Reject delivery", "steps": [
            {"id": "s1", "skill": "return_home", "params": {}, "on_success": None, "on_fail": None}
        ], "report": ["rejected"],
    }
    (dataset / "train.jsonl").write_text("".join(json.dumps(item) + "\n" for item in rows), encoding="utf-8")
    admitted, _ = admit_train_rows(dataset)
    assert len(admitted) == 2


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
    assert manifest["rows"] == {"train": 23, "val": 2}
    assert manifest["family_counts"]["train"]["v4_backbone"] == 10
    assert sum(v for k, v in manifest["family_counts"]["train"].items() if k != "v4_backbone") == 13


def test_rejected_candidate_targets_match_validator_negative_labels(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    from build_plan_sft import main
    from train_slow_system_lora import admit_train_rows

    output = tmp_path / "rejected-candidates"
    assert main([
        "--output", str(output), "--r20-plan-intent", "--r20-plan-intent-count", "2",
        "--negative-target-mode", "rejected_candidate", "--require-skills",
    ]) == 0
    rows = [json.loads(line) for line in (output / "train.jsonl").read_text(encoding="utf-8").splitlines()]
    negatives = [row for row in rows if row["expected_valid"] is False]
    assert len(negatives) == 10
    assert all(row["assistant_target"] == row["rejected_candidate"] for row in negatives)
    for row in negatives:
        if row["family"] in {"unknown_place", "ref_alias"}:
            target_refs = {
                step.get("params", {}).get("target_ref")
                for step in row["assistant_target"]["steps"]
            }
            known_refs = {ref.split(" ", 1)[0] for ref in row["refs"]}
            assert not (target_refs & known_refs)
    admitted, _ = admit_train_rows(output)
    assert len(admitted) == len(rows)


def test_admission_blocks_known_ref_substitution_for_unknown_place(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    from train_slow_system_lora import admit_train_rows

    dataset = tmp_path / "substitution"
    dataset.mkdir()
    plan = {
        "plan_version": 1, "goal": "Raven Pier", "report": [], "steps": [
            {"id": "s1", "skill": "goto", "params": {"target_ref": "poi:gate"},
             "on_success": "s2", "on_fail": "s2"},
            {"id": "s2", "skill": "return_home", "params": {},
             "on_success": None, "on_fail": None},
        ],
    }
    row = {
        "sample_id": "bad-place-substitution", "kind": "mission_plan", "world": "test",
        "task_text": "Fly to the nonexistent Raven Pier and return home.", "refs": ["poi:gate (gate)"],
        "assistant_target": plan, "rejected_candidate": plan, "negative_target_mode": "rejected_candidate",
        "expected_valid": False, "expected_findings": ["ungrounded_place"], "split": "train",
    }
    (dataset / "train.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="substitutes known ref"):
        admit_train_rows(dataset)


def test_admission_blocks_fabricated_query(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    from train_slow_system_lora import admit_train_rows

    dataset = tmp_path / "fabricated-query"
    dataset.mkdir()
    row = {
        "sample_id": "invented-query", "kind": "mission_plan", "world": "test",
        "task_text": "检查大门并返航。", "refs": ["poi:gate (大门 / gate)"],
        "capabilities": [], "split": "train",
        "assistant_target": {"plan_version": 1, "goal": "inspect", "report": [], "steps": [
            {"id": "s1", "skill": "inspect", "params": {"target_ref": "poi:gate", "query": "fire truck"}, "on_success": "s2", "on_fail": "s2"},
            {"id": "s2", "skill": "return_home", "params": {}, "on_success": None, "on_fail": None},
        ]},
    }
    (dataset / "train.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="ungrounded_query|invents ungrounded query"):
        admit_train_rows(dataset)
