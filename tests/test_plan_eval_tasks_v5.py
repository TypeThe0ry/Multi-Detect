from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from multidetect.autonomy.capability_contract import capability_violations
from multidetect.autonomy.mission_plan import validate_mission_plan
from multidetect.autonomy.mission_planner import check_plan_refs
from multidetect.autonomy.plan_intent import check_plan_intent
from scripts.build_plan_sft import normalize_task


ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "configs/onboard/plan-eval-tasks-v5.json"


def _plan_from_expected(item: dict) -> dict:
    expected = item["expected_steps"]
    steps = []
    for index, step in enumerate(expected):
        next_id = f"s{index + 2}" if index + 1 < len(expected) else None
        steps.append(
            {
                "id": f"s{index + 1}",
                "skill": step["skill"],
                "params": step["params"],
                "on_success": next_id,
                "on_fail": next_id,
            }
        )
    return {"plan_version": 1, "goal": item["text"], "steps": steps, "report": ["status"]}


def test_v5_shape_language_and_category_counts():
    document = json.loads(TASKS.read_text(encoding="utf-8"))
    tasks = document["tasks"]
    assert 120 <= len(tasks) <= 150
    assert Counter(item["category"] for item in tasks) == {
        category: 14
        for category in (
            "return_intent",
            "return_negated_land",
            "unknown_place",
            "missing_payload_drop",
            "fixed_wing_hover",
            "fixed_wing_target_land",
            "ref_alias",
            "query_grounding",
            "query_ungrounded",
        )
    }
    assert Counter(item["language"] for item in tasks) == {"zh": 63, "en": 63}
    assert len({normalize_task(item["text"]) for item in tasks}) == len(tasks)


def test_v5_expected_plans_and_rejections_match_current_contract():
    document = json.loads(TASKS.read_text(encoding="utf-8"))
    refs = tuple(document["refs"])
    for item in document["tasks"]:
        if not item.get("expect_invalid"):
            plan = _plan_from_expected(item)
            validate_mission_plan(plan, capabilities=frozenset(item.get("capabilities", ())))
            assert not check_plan_intent(
                plan, item["text"], tuple(item.get("refs", refs))
            )
            assert not check_plan_refs(
                plan, tuple(item.get("refs", refs)), substitute=False
            )[2]
            assert not capability_violations(
                plan,
                item.get("capabilities", ()),
                airframe=(
                    "airframe:fixed_wing"
                    if "airframe:fixed_wing" in item.get("capabilities", ())
                    else None
                ),
            )
            continue

        category = item["category"]
        if category == "unknown_place":
            plan = {
                "plan_version": 1,
                "goal": "unknown place",
                "steps": [
                    {"id": "s1", "skill": "goto", "params": {"target_ref": "poi:pad"}, "on_success": "s2", "on_fail": "s2"},
                    {"id": "s2", "skill": "return_home", "params": {}, "on_success": None, "on_fail": None},
                ],
                "report": [],
            }
            validate_mission_plan(plan)
            assert check_plan_intent(plan, item["text"], refs)
        elif category == "missing_payload_drop":
            plan = {
                "plan_version": 1,
                "goal": "deliver",
                "steps": [
                    {"id": "s1", "skill": "deliver", "params": {"target_ref": "poi:gate", "payload_ref": "payload:parcel"}, "on_success": "s2", "on_fail": "s2"},
                    {"id": "s2", "skill": "return_home", "params": {}, "on_success": None, "on_fail": None},
                ],
                "report": [],
            }
            validate_mission_plan(plan, capabilities=frozenset({"payload_drop"}))
            assert capability_violations(plan, ())
        elif category == "fixed_wing_hover":
            plan = {
                "plan_version": 1,
                "goal": "hover",
                "steps": [
                    {"id": "s1", "skill": "hover", "params": {"region": "zone:lot", "duration_s": 60}, "on_success": "s2", "on_fail": "s2"},
                    {"id": "s2", "skill": "return_home", "params": {}, "on_success": None, "on_fail": None},
                ],
                "report": [],
            }
            validate_mission_plan(plan, capabilities=frozenset(item["capabilities"]))
            assert capability_violations(plan, item["capabilities"], airframe="airframe:fixed_wing")
        elif category == "fixed_wing_target_land":
            plan = {
                "plan_version": 1,
                "goal": "target landing",
                "steps": [{"id": "s1", "skill": "land", "params": {"target_ref": "poi:gate"}, "on_success": None, "on_fail": None}],
                "report": [],
            }
            validate_mission_plan(plan, capabilities=frozenset(item["capabilities"]))
            assert capability_violations(plan, item["capabilities"], airframe="airframe:fixed_wing")
        else:
            query = "ambulance" if "救护车" in item["text"] else "fire truck"
            plan = {
                "plan_version": 1,
                "goal": "grounding",
                "steps": [
                    {"id": "s1", "skill": "inspect", "params": {"target_ref": "poi:gate", "query": query, "views": 2, "standoff_m": 8}, "on_success": "s2", "on_fail": "s2"},
                    {"id": "s2", "skill": "return_home", "params": {}, "on_success": None, "on_fail": None},
                ],
                "report": [],
            }
            validate_mission_plan(plan)
            assert check_plan_intent(plan, item["text"], refs)


def test_v5_texts_do_not_collide_with_r24_or_r27_training_rows():
    held_out = {
        normalize_task(item["text"])
        for item in json.loads(TASKS.read_text(encoding="utf-8"))["tasks"]
    }
    for relative in (
        "artifacts/scratch/cx-planner-round/r24/dataset/train.jsonl",
        "artifacts/scratch/cx-planner-round/r27/dataset/train.jsonl",
    ):
        path = ROOT / relative
        assert path.is_file(), path
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                assert normalize_task(row["task_text"]) not in held_out
