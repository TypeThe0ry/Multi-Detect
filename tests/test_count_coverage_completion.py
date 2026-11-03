from __future__ import annotations

from multidetect.autonomy.mission_executor import MissionExecutor
from multidetect.autonomy.mission_plan import validate_mission_plan
from multidetect.autonomy.onboard_loop import CameraModel, LoopConfig, OnboardLoop
from multidetect.sim_teacher.primitives import ActionPrimitive

CAMERA = CameraModel(fx=540, fy=540, cx=640, cy=480, width=1280, height=960)


def test_count_coverage_completion_hands_off_to_home() -> None:
    plan = validate_mission_plan(
        {
            "plan_version": 1,
            "goal": "count cones",
            "steps": [
                {
                    "id": "count",
                    "skill": "count",
                    "params": {"region": "zone:lot", "query": "cone"},
                    "on_success": "home",
                    "on_fail": "home",
                },
                {
                    "id": "home",
                    "skill": "return_home",
                    "params": {},
                    "on_success": None,
                    "on_fail": None,
                },
            ],
            "report": ["count"],
        }
    )

    def finished_factory(task_text: str, region=None, **overrides) -> OnboardLoop:
        return OnboardLoop(
            camera=CAMERA,
            search_action=lambda pose, yaw: ActionPrimitive.stop(),
            search_outcome=lambda: "coverage_complete",
            config=LoopConfig(**overrides),
        )

    executor = MissionExecutor(
        plan,
        loop_factory=finished_factory,
        refs={"zone:lot": (0.0, 0.0, 10.0)},
    )
    action, _ = executor.next_action(pose_ned=(0.0, 0.0, -20.0), yaw_deg=0.0)

    assert executor.history[0] == {
        "step": "count",
        "skill": "count",
        "state": "succeeded",
        "reason": "coverage_complete",
        "count_is_lower_bound": False,
        "revisit_candidate_snapshots": [
            {
                "kind": kind, "remaining_primitives": 60,
                "energy_margin_s": None, "candidates": [],
                "remaining_primitives_after": 60,
            }
            for kind in ("revisit", "rejection")
        ],
    }
    assert executor.history[1]["step"] == "home"
    assert executor.done
    assert action.kind == "stop"
