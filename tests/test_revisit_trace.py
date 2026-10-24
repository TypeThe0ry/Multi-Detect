from __future__ import annotations

import json

from multidetect.autonomy.mission_executor import MissionExecutor, plan_for_find_task
from multidetect.autonomy.onboard_loop import CameraModel, LoopConfig, OnboardLoop
from multidetect.sim_teacher.primitives import ActionPrimitive


_CAMERA = CameraModel(fx=540.0, fy=540.0, cx=640.0, cy=480.0, width=1280, height=960)


def _executor():
    def factory(_text, **_kwargs):
        return OnboardLoop(
            camera=_CAMERA,
            search_action=lambda pose, yaw: ActionPrimitive.hold(1.0),
            segment_clear=lambda pose, n, e: True,
            config=LoopConfig(),
        )

    return MissionExecutor(plan_for_find_task("hydrant"), loop_factory=factory)


def test_revisit_snapshot_is_bounded_and_json_safe():
    executor = _executor()
    run = executor._run
    loop = run.loop
    candidates = [
        {"ground": (float(i), 1.23456), "min_side_px": [12.34567], "track_id": f"t{i}"}
        for i in range(40)
    ]
    plan = executor._new_revisit_plan(run, "rejection", candidates, loop, (0.0, 0.0), 17)

    assert len(plan["candidates"]) == 24
    assert plan["remaining_primitives"] == 17
    assert plan["energy_margin_s"] is None
    assert all(set(row) >= {"ground_ne", "kind", "priority_key", "predicted_min_side_px", "selected", "skip_reason"} for row in plan["candidates"])
    assert all(len(row["ground_ne"]) == 2 for row in plan["candidates"])
    assert all(value is None or abs(value * 1000 - round(value * 1000)) < 1e-9 for row in plan["candidates"] for value in row["ground_ne"] + row["priority_key"] + [row["predicted_min_side_px"]])
    assert len(json.dumps(plan, separators=(",", ":"))) < 10000


def test_revisit_snapshot_selection_flag_can_be_set_without_changing_candidate_order():
    executor = _executor()
    run = executor._run
    loop = run.loop
    candidates = [
        {"ground": (10.0, 0.0), "min_side_px": [8.0]},
        {"ground": (2.0, 0.0), "min_side_px": [16.0]},
    ]
    plan = executor._new_revisit_plan(run, "rejection", candidates, loop, (0.0, 0.0), 9)
    before = [row["ground_ne"] for row in plan["candidates"]]
    plan["candidates"][1]["selected"] = True
    assert [row["ground_ne"] for row in plan["candidates"]] == before
    assert [row["selected"] for row in plan["candidates"]] == [False, True]
