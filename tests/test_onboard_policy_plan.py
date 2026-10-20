"""Pure-Python coverage for text-planned SIM missions, without model startup."""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

from multidetect.autonomy.mission_executor import plan_for_find_task
from multidetect.autonomy.mission_plan import MissionReport, MissionStep
from multidetect.autonomy.mission_planner import plan_json_schema
from scripts.onboard_policy import OnboardPolicy, build_mission_from_text, perception_task_text
from scripts.slow_system_runtime import trained_key_order_schema


def _site_search_plan() -> dict:
    return {
        "plan_version": 1,
        "goal": "Find the red car at the site",
        "steps": [
            {
                "id": "search",
                "skill": "area_search",
                "params": {"region": "zone:site", "query": "red car"},
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
        "report": ["findings", "positions"],
    }


def test_perception_task_text_uses_ascii_step_query():
    step = plan_for_find_task("red car").steps[0]

    assert perception_task_text(step, "Find anything unusual in the yard.") == (
        "Find the red car and record its position."
    )


def test_perception_task_text_keeps_original_for_chinese_query():
    step = MissionStep("search", "area_search", {"query": "红色汽车"}, "home", "home")
    original = "找到院子里的红色汽车并记录位置。"

    assert perception_task_text(step, original) == original


def test_build_mission_from_text_passes_refs_and_sorted_schema():
    refs = {"zone:site": [0.0, 0.0, 55.0]}
    raw = json.dumps(_site_search_plan())
    calls = []

    def post(prompt, schema):
        calls.append((prompt, schema))
        return raw

    plan, report = build_mission_from_text("Find the red car at the site", refs, post)

    assert [step.skill for step in plan.steps] == ["area_search", "return_home"]
    assert plan.steps[0].params["region"] == "zone:site"
    assert len(calls) == 1
    assert "Known places: zone:site." in calls[0][0]
    assert calls[0][1] == trained_key_order_schema(plan_json_schema())
    assert list(calls[0][1]["properties"]) == ["goal", "plan_version", "report", "steps"]
    assert report["raw"] == raw
    assert report["error"] is None


def test_build_mission_from_text_can_use_unsorted_schema():
    schemas = []

    def post(prompt, schema):
        schemas.append(schema)
        return json.dumps(_site_search_plan())

    plan, report = build_mission_from_text(
        "Find the red car at the site",
        {"zone:site": (0.0, 0.0, 55.0)},
        post,
        sorted_keys=False,
    )

    assert plan.steps[0].skill == "area_search"
    assert schemas == [plan_json_schema()]
    assert report["error"] is None


def test_build_mission_from_text_falls_back_to_single_return_home():
    prompts = []

    def post(prompt, schema):
        prompts.append(prompt)
        return "not a JSON plan"

    plan, report = build_mission_from_text(
        "Find the red car at the site",
        {"zone:site": [0.0, 0.0, 55.0]},
        post,
        sorted_keys=False,
    )

    assert len(prompts) == 2
    assert len(plan.steps) == 1
    assert plan.steps[0].skill == "return_home"
    assert plan.steps[0].on_success is None and plan.steps[0].on_fail is None
    assert isinstance(report["error"], str) and report["error"]
    assert report["raw"] == "not a JSON plan"
    assert len(report["attempts"]) == 2


def test_slow_identity_sends_step_text_override_and_preserves_original_default():
    rows = []

    def slow(row):
        rows.append(row)
        return (
            json.dumps(
                {
                    "decision": "confirm_target",
                    "args": {"track_id": "track-1", "region": None, "duration_s": None},
                    "reason": "visible red car",
                }
            ),
            0.01,
        )

    slow.last_logprobs = None
    policy = SimpleNamespace(
        task_text="Find a red car and then count bicycles.",
        image_size=(1280, 960),
        slow=slow,
        slow_calls=0,
    )
    focus = SimpleNamespace(track_id="observed-7", bbox=(100, 200, 300, 400))
    step_text = "Find the red car and record its position."

    verdict = OnboardPolicy._slow_identity(
        policy, Path("frame.png"), [focus], focus, task_text=step_text
    )
    OnboardPolicy._slow_identity(policy, Path("frame.png"), [focus], focus)

    assert verdict["decision"] == "confirm_target"
    assert [row["task_text"] for row in rows] == [step_text, policy.task_text]
    assert policy.slow_calls == 2


def test_mission_labels_include_compile_details_once():
    plan = plan_for_find_task("red car")
    candidate = SimpleNamespace(
        track_id="observed-7", bbox=(100.0, 200.0, 300.0, 400.0), verdict=None
    )
    asked_texts = []
    executor = SimpleNamespace(
        findings=[],
        current_tracks=(candidate,),
        current_step=plan.steps[0],
        current_task_text="Find the red car and record its position.",
        history=[],
        counts={},
        observe=lambda *args, **kwargs: [candidate],
        apply_verdict=lambda *args: None,
        next_action=lambda **kwargs: (None, {}),
        report=MissionReport,
    )
    repair = "search.on_fail: search -> home"
    policy = SimpleNamespace(
        task_text="Find a red car and return.",
        executor=executor,
        mission_plan=plan,
        mission_plan_report={"repairs": [repair], "error": None, "attempts": [0.125, 0.375]},
        _mission_plan_labeled=False,
    )
    policy._mission_plan_label = lambda: OnboardPolicy._mission_plan_label(policy)

    def slow_identity(frame_path, current, focus, task_text):
        assert focus is candidate
        asked_texts.append(task_text)
        return {"decision": "confirm_target", "confidence": None}

    policy._slow_identity = slow_identity

    labels = []
    for index in range(2):
        _, result = OnboardPolicy._act_mission(
            policy,
            Path(f"frame-{index}.png"),
            [],
            0.01,
            pose_ned=(0.0, 0.0, -20.0),
            yaw_deg=0.0,
            ground_point=None,
        )
        labels.append(result["onboard"]["mission"])

    assert labels[0]["plan"] == {
        "skills": ["area_search", "return_home"],
        "repairs": [repair],
        "error": None,
        "latency_s": 0.5,
    }
    assert "plan" not in labels[1]
    assert policy.task_text != executor.current_task_text
    assert asked_texts == [executor.current_task_text] * 2


def test_mission_policy_passes_landed_observation_through_executor():
    plan = plan_for_find_task("red car")
    candidate = SimpleNamespace(
        track_id="observed-7",
        bbox=(100.0, 200.0, 300.0, 400.0),
        verdict=None,
        ground=None,
    )
    observed = {}
    executor = SimpleNamespace(
        findings=[],
        current_tracks=(candidate,),
        current_step=plan.steps[0],
        current_task_text="Find the red car and record its position.",
        history=[],
        counts={},
        observe=lambda *args, **kwargs: (observed.__setitem__("observe", kwargs), [candidate])[1],
        apply_verdict=lambda *args: None,
        next_action=lambda **kwargs: (observed.setdefault("next_action", kwargs), {}),
        report=MissionReport,
    )
    policy = SimpleNamespace(
        task_text="Find a red car and return.",
        executor=executor,
        mission_plan=plan,
        mission_plan_report=None,
        _mission_plan_labeled=True,
        _slow_identity=lambda *args, **kwargs: {"decision": "confirm_target"},
        alert_vote_clusters=(),
    )

    OnboardPolicy._act_mission(
        policy,
        Path("frame.png"),
        [],
        0.01,
        pose_ned=(0.0, 0.0, -20.0),
        yaw_deg=0.0,
        ground_point=None,
        landed=True,
    )

    assert observed["observe"]["landed"] is True
    assert observed["next_action"]["landed"] is True


def test_act_reads_landed_from_policy_observation(tmp_path, monkeypatch):
    from PIL import Image

    forwarded = []

    def act_mission(self, *args, **kwargs):
        forwarded.append(kwargs["landed"])
        return None, {}

    monkeypatch.setattr(OnboardPolicy, "_act_mission", act_mission)
    image = Image.new("RGB", (1, 1))
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    policy = OnboardPolicy.__new__(OnboardPolicy)
    policy._step = 0
    policy.work_dir = tmp_path
    policy.proposer = lambda _image: []
    policy.image_size = (1, 1)
    policy.executor = object()

    policy.act(
        {"rgb": encoded.getvalue(), "camera_info": {}, "landed": True},
        pose_ned=(0.0, 0.0, -1.0),
        yaw_deg=0.0,
    )

    assert forwarded == [True]


def test_each_mission_step_starts_the_shared_search_route_afresh(tmp_path) -> None:
    # A finished count left the shared search policy at the end of its route with
    # a coverage_complete outcome; the next search step then ended at once.
    from onboard_factories import make_policy

    policy = make_policy(work_dir=tmp_path, config={"mission": "count"})
    search = policy._search
    search._waypoint_index = len(search._waypoints)
    search._search_outcome = "coverage_complete"
    policy.executor.loop_factory("count the cones", restart_route=False)
    assert search._waypoint_index == 0 and search.search_outcome == "running"
    assert search.restart_route is False
