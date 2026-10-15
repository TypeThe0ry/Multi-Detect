"""Exercise mission-package wiring with constructors that cannot run models."""

from __future__ import annotations

import json
import time

import pytest
from test_mission_supervisor import document

from multidetect.autonomy.mission_executor import MissionExecutor
from multidetect.autonomy.mission_plan import validate_mission_plan
from multidetect.autonomy.mission_supervisor import MissionSupervisor
from scripts import eval_slow_system, onboard_policy

CAMERA = {"fx": 540.0, "fy": 540.0, "cx": 640.0, "cy": 480.0}
COLLECTOR_LABELS = {
    "state",
    "detections",
    "tracks",
    "slow_calls",
    "confirmed",
    "alert_vote_clusters",
    "fast_latency_s",
    "mission",
}


@pytest.fixture
def model_stubs(monkeypatch):
    constructed = {"proposers": [], "slow": []}

    class Proposer:
        def __init__(self, *args, **kwargs):
            constructed["proposers"].append((args, kwargs))

        def __call__(self, image):
            pytest.fail("test must not run FAST inference")

    class Slow:
        def __init__(self, *args, **kwargs):
            self.port = kwargs["port"]
            self.closed = False
            constructed["slow"].append(self)

        def __call__(self, row):
            pytest.fail("test must not run SLOW inference")

        def close(self):
            self.closed = True

    monkeypatch.setattr(onboard_policy, "YoloeProposer", Proposer)
    monkeypatch.setattr(eval_slow_system, "LlamaServerRunner", Slow)
    monkeypatch.setattr(
        eval_slow_system,
        "_http_json",
        lambda *args, **kwargs: pytest.fail("test must not call a model server"),
    )
    return constructed


def _construct(tmp_path, config):
    return onboard_policy.OnboardPolicy(
        task_text="World task must not override package task",
        world_path=None,
        altitude_m=20.0,
        camera_info=CAMERA,
        image_size=(1280, 960),
        work_dir=tmp_path / "runtime",
        config=config,
    )


def _clock(policy):
    now = [policy.supervisor.started]
    policy.supervisor.clock = lambda: now[0]
    return now


def _event(kind, **kwargs):
    return {"mission_id": "sortie-1", "type": kind, **kwargs}


def test_precompiled_package_config_loads_refs_home_and_supervisor(tmp_path, model_stubs):
    package = document()
    package["home"] = [-12, 7]
    package["task_text"] = "Visit the uploaded gate and return."
    package["zones"] = {"zone:yard": {"centre_ne": [0, 0], "radius_m": 10}}
    package["routes"] = {"route:gate": [[0, 0], [40, 0]]}
    package_path = tmp_path / "uploaded-mission.json"
    package_path.write_text(json.dumps(package), encoding="utf-8")
    config_path = tmp_path / "policy.json"
    config_path.write_text(
        json.dumps({"mission_package": str(package_path), "refs": {"poi:wrong": [1, 2]}}),
        encoding="utf-8",
    )
    policy = onboard_policy.build_onboard_policy(
        task_text="World task",
        world_path=None,
        altitude_m=20.0,
        camera_info=CAMERA,
        image_size=(1280, 960),
        work_dir=tmp_path / "runtime",
        config_path=config_path,
    )
    try:
        assert isinstance(policy.executor, MissionSupervisor)
        assert policy.executor is policy.supervisor
        assert policy.task_text == package["task_text"]
        assert policy.supervisor.executor.home == (-12, 7)
        assert policy.supervisor.executor.refs == {
            "poi:gate": (40.0, 0.0),
            "zone:yard": (0.0, 0.0, 10.0),
            "route:gate": ((0.0, 0.0), (40.0, 0.0)),
        }
        assert policy.supervisor.log.path.is_file()
        assert len(model_stubs["slow"]) == len(model_stubs["proposers"]) == 1
    finally:
        policy.close()
    assert model_stubs["slow"][0].closed
    assert policy.supervisor.log.summary_path.is_file()


def test_text_package_compiles_with_onboard_refs(tmp_path, model_stubs, monkeypatch):
    package = document()
    plan = validate_mission_plan(package.pop("plan"))
    package["task_text"] = "Visit the gate and return."
    package["ref_labels"] = {"poi:gate": "Uploaded gate"}
    calls = []

    def compile_plan(post, task, **kwargs):
        calls.append((task, kwargs))
        return plan, {"error": None, "attempts": [0.125], "repairs": []}

    monkeypatch.setattr(onboard_policy, "compile_mission_plan", compile_plan)
    policy = _construct(tmp_path, {"mission_package": package})
    try:
        assert policy.mission_plan == plan
        assert policy.supervisor.executor.plan == plan
        assert calls[0][0] == package["task_text"]
        assert calls[0][1]["refs"] == ("poi:gate (Uploaded gate)",)
        assert policy.mission_plan_report["error"] is None
    finally:
        policy.close()


@pytest.mark.parametrize("failure", ["unknown_ref", "compile_error"])
def test_text_package_refuses_unusable_compilation(
    tmp_path, model_stubs, monkeypatch, failure
):
    package = document()
    plan_doc = package.pop("plan")
    package["task_text"] = "Visit the gate and return."
    if failure == "unknown_ref":
        plan_doc["steps"][0]["params"]["target_ref"] = "poi:not-uploaded"
    plan = validate_mission_plan(plan_doc)
    report = {"error": "invalid model output" if failure == "compile_error" else None}
    monkeypatch.setattr(
        onboard_policy, "compile_mission_plan", lambda *args, **kwargs: (plan, report)
    )
    with pytest.raises(ValueError, match="unknown onboard|compilation failed"):
        _construct(tmp_path, {"mission_package": package})
    assert model_stubs["slow"][0].closed
    assert not (tmp_path / "runtime" / "mission_log").exists()


@pytest.mark.parametrize("config", [{}, {"mission": "find"}])
def test_no_package_preserves_legacy_unsupervised_mode(tmp_path, model_stubs, config):
    policy = _construct(tmp_path, config)
    try:
        assert policy.mission_package is policy.supervisor is policy.downlink is None
        assert (policy.executor is None) == (not config)
        if config:
            assert isinstance(policy.executor, MissionExecutor)
        assert not policy.ground_event(_event("abort"))
        policy.set_downlink_sender(lambda record: pytest.fail("legacy mode has no queue"))
        assert not (tmp_path / "runtime" / "mission_log").exists()
    finally:
        policy.close()


@pytest.mark.parametrize(
    "observation,reason",
    [
        ({"ground_events": [_event("abort")]}, "abort"),
        ({"ground_events": [_event("pause")]}, "pause"),
        ({"mission_estimates": {"remaining_energy_wh": 30}}, "energy"),
        ({"mission_estimates": {"remaining_time_s": 60}}, "time"),
    ],
)
def test_recovery_observations_bypass_image_and_models(
    tmp_path, model_stubs, observation, reason
):
    policy = _construct(tmp_path, {"mission_package": document()})
    _clock(policy)
    try:
        # No RGB or camera keys: any image/perception access would fail this test.
        action, labels = policy.act(observation, pose_ned=(30, 0, -20), yaw_deg=180)
        assert action.kind == "hold"
        assert COLLECTOR_LABELS <= labels["onboard"].keys()
        assert reason in labels["onboard"]["supervisor_reason"]
        assert labels["onboard"]["slow_calls"] == []
        assert not list(policy.work_dir.glob("frame_*.png"))
        assert policy._step == 0
        if reason == "pause":
            assert policy.ground_event(_event("resume"))
            assert policy.supervisor.evaluate() == "continue"
        else:
            action, labels = policy.act({}, pose_ned=(30, 0, -20), yaw_deg=180)
            assert action.kind == "translate"
            assert labels["onboard"]["supervisor_action"] == "return_now"
    finally:
        policy.close()


def test_package_mission_execution_keeps_collector_labels(tmp_path, model_stubs):
    policy = _construct(tmp_path, {"mission_package": document()})
    _clock(policy)
    try:
        action, labels = policy._act_mission(
            tmp_path / "frame.png",
            [],
            0.01,
            pose_ned=(0, 0, -20),
            yaw_deg=0,
            ground_point=None,
        )
        assert action.kind == "translate"
        onboard = labels["onboard"]
        assert COLLECTOR_LABELS <= onboard.keys()
        assert {"history", "evidence", "answers", "alerts", "positions", "counts"} <= (
            onboard["mission"].keys()
        )
        assert onboard["supervisor_action"] == "continue"
    finally:
        policy.close()


def test_optional_transport_attaches_and_drains_on_heartbeat(tmp_path, model_stubs):
    policy = _construct(tmp_path, {"mission_package": document()})
    _clock(policy)
    received = []
    try:
        policy.set_downlink_sender(received.append)
        assert received == []
        assert policy.ground_event(_event("heartbeat"))
        target = len(policy.supervisor.log.path.read_text(encoding="utf-8").splitlines())
        deadline = time.monotonic() + 2.0
        while policy.downlink.acknowledged_sequence < target and time.monotonic() < deadline:
            time.sleep(0.005)
        assert policy.downlink.acknowledged_sequence == target
        assert [record["sequence"] for record in received] == list(range(1, target + 1))
        assert all(record["mission_id"] == "sortie-1" for record in received)
        assert policy.supervisor.log.cursor_path.is_file()
    finally:
        policy.close()
