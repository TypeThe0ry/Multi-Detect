from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from multidetect.autonomy.mission_plan import SKILLS, validate_mission_plan
from multidetect.autonomy.mission_planner import check_plan_refs

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "configs/onboard/plan-eval-tasks-v3.json"


# planning-v5 deliberately reused five v4 prompts when it was frozen (its
# published scores include them), so they stay; any new overlap still fails.
FROZEN_V5_V4_OVERLAP = frozenset({
    "固定翼飞到大门上方定点降落。",
    "A fixed-wing aircraft should land precisely on the gate.",
    "数停车场里的红色轿车，然后返航。",
    "Count the bicycles in the parking lot and return home.",
    "Inspect the gate and return home, but do not invent a fire truck target.",
})


def test_v3_size_languages_coverage_and_held_out_texts():
    document = json.loads(TASKS.read_text(encoding="utf-8"))
    tasks = document["tasks"]
    assert len(tasks) == 80
    assert Counter(task["language"] for task in tasks) == {"zh": 40, "en": 40}
    assert Counter(
        task["language"] for task in tasks if task.get("expect_invalid")
    ) == {"zh": 4, "en": 4}
    coverage = Counter(skill for task in tasks for skill in set(task["skills"]))
    assert all(coverage[skill] >= 5 for skill in SKILLS if skill != "deliver")
    assert coverage["deliver"] == 0
    seen = set()
    for path in sorted(TASKS.parent.glob("plan-eval-tasks*.json")):
        for task in json.loads(path.read_text(encoding="utf-8"))["tasks"]:
            text = task["text"].strip()
            if path.name == "plan-eval-tasks-v5.json" and text in FROZEN_V5_V4_OVERLAP:
                continue
            assert text not in seen, (path, text)
            seen.add(text)
    for task in tasks:
        has_chinese = bool(re.search(r"[\u4e00-\u9fff]", task["text"]))
        assert has_chinese == (task["language"] == "zh")
        assert "?" * 3 not in task["text"]


def test_v3_expected_plans_have_legal_params_paths_and_known_refs():
    document = json.loads(TASKS.read_text(encoding="utf-8"))
    for task in document["tasks"]:
        if task.get("expect_invalid"):
            assert task["skills"] == []
            assert "expected_steps" not in task
            continue
        expected = task["expected_steps"]
        assert [step["skill"] for step in expected] == task["skills"]
        assert expected[-1]["skill"] in {"return_home", "land"}
        terminal = f"s{len(expected)}"
        steps = [
            {
                **step,
                "id": f"s{index + 1}",
                "on_success": f"s{index + 2}" if index + 1 < len(expected) else None,
                "on_fail": terminal if index + 1 < len(expected) else None,
            }
            for index, step in enumerate(expected)
        ]
        raw = {"plan_version": 1, "goal": task["text"], "steps": steps, "report": []}
        plan = validate_mission_plan(raw)
        assert [step.skill for step in plan.steps] == task["skills"]
        refs = tuple(task.get("refs", document["refs"]))
        _checked, repairs, unknown = check_plan_refs(raw, refs, substitute=False)
        assert not repairs and not unknown, (task["text"], unknown)
        found = 0
        for step in plan.steps:
            for key in ("target_ref", "track_ref"):
                ref = step.params.get(key, "")
                if isinstance(ref, str) and ref.startswith(("finding:", "track:")):
                    assert 1 <= int(ref.split(":")[1]) <= found, task["text"]
            found += step.skill == "area_search"
