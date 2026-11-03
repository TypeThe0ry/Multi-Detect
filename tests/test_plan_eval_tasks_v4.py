import json
import re
from pathlib import Path

from multidetect.autonomy.mission_plan import validate_mission_plan
from multidetect.autonomy.plan_intent import check_plan_intent

ROOT = Path(__file__).resolve().parents[1]


# planning-v5 deliberately reused five v4 prompts when it was frozen (its
# published scores include them), so they stay; any new overlap still fails.
FROZEN_V5_V4_OVERLAP = frozenset({
    "固定翼飞到大门上方定点降落。",
    "A fixed-wing aircraft should land precisely on the gate.",
    "数停车场里的红色轿车，然后返航。",
    "Count the bicycles in the parking lot and return home.",
    "Inspect the gate and return home, but do not invent a fire truck target.",
})


def test_v4_structure_holdout_categories_and_expected_plans():
    doc = json.loads((ROOT / "configs/onboard/plan-eval-tasks-v4.json").read_text(encoding="utf-8"))
    tasks = doc["tasks"]
    assert len(tasks) == 20
    assert {t["category"] for t in tasks} == {
        "return_intent", "return_negated_land", "unknown_place", "missing_payload_drop",
        "fixed_wing_hover", "fixed_wing_target_land", "ref_alias", "query_grounding", "query_ungrounded",
    }
    assert sum(t.get("expect_invalid", False) for t in tasks) == 10
    assert all(bool(re.search(r"[\u4e00-\u9fff]", t["text"])) == (t["language"] == "zh") for t in tasks)
    for task in tasks:
        if task.get("expect_invalid"):
            assert not task.get("expected_steps")
            continue
        expected = task["expected_steps"]
        steps = [{**s, "id": f"s{i+1}", "on_success": f"s{i+2}" if i + 1 < len(expected) else None,
                  "on_fail": f"s{len(expected)}" if i + 1 < len(expected) else None}
                 for i, s in enumerate(expected)]
        plan = validate_mission_plan({"plan_version": 1, "goal": task["text"], "steps": steps, "report": []})
        assert check_plan_intent(plan, task["text"], task.get("refs", doc["refs"])) == []


def test_v4_texts_are_unique_across_eval_files():
    seen = set()
    for path in sorted((ROOT / "configs/onboard").glob("plan-eval-tasks*.json")):
        for task in json.loads(path.read_text(encoding="utf-8"))["tasks"]:
            text = " ".join(task["text"].split())
            if path.name == "plan-eval-tasks-v5.json" and text in FROZEN_V5_V4_OVERLAP:
                continue
            assert text not in seen
            seen.add(text)
