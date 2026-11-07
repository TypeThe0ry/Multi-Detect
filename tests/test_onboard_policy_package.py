"""Exercise mission-package wiring with constructors that cannot run models."""

from __future__ import annotations

import ast
import json
import time
from pathlib import Path

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


def test_split_policy_parts_have_no_script_imports_or_global_namespace_injection():
    parts = Path(onboard_policy.ROOT) / "src/multidetect/autonomy/onboard_policy_parts"
    for path in parts.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(name.name.split(".")[0] != "scripts" for name in node.names), path
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] != "scripts", path
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"globals", "__import__", "exec", "eval"}, path


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
        assert policy.supervisor.checkpoint_store.path == (
            tmp_path / "runtime" / "crash_resume" / "checkpoint.json"
        )
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
def test_text_package_refuses_unusable_compilation(tmp_path, model_stubs, monkeypatch, failure):
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
def test_recovery_observations_bypass_image_and_models(tmp_path, model_stubs, observation, reason):
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
        # The cursor passes every record; a routine heartbeat record stays local.
        assert [record["kind"] for record in received] == ["mission_started"]
        assert all(record["mission_id"] == "sortie-1" for record in received)
        assert policy.supervisor.log.cursor_path.is_file()
    finally:
        policy.close()


@pytest.mark.parametrize(
    "events",
    [
        None,
        {},
        [{"t_s": -1, "event": "abort"}],
        [{"t_s": True, "event": "abort"}],
        [{"t_s": float("nan"), "event": "abort"}],
        [{"t_s": 1, "event": "typo"}],
        [{"t_s": 1, "event": "abort", "extra": 0}],
        [{"t_s": 2, "event": "pause"}, {"t_s": 1, "event": "resume"}],
    ],
)
def test_sim_timeline_rejects_bad_config_before_models(tmp_path, model_stubs, events):
    with pytest.raises(ValueError):
        _construct(tmp_path, {"mission_package": document(), "sim_ground_events": events})
    assert model_stubs == {"proposers": [], "slow": []}


def test_sim_timeline_requires_package():
    with pytest.raises(ValueError, match="requires a mission_package"):
        onboard_policy.validate_sim_ground_events({"sim_ground_events": []})


def _sim_advance(policy, seconds):
    policy.sim_timeline.advance(
        {"frame_sensor_stamp_ns": 10_000_000_000 + int(seconds * 1e9)}, policy.supervisor
    )
    return policy.supervisor.evaluate()


def _records(policy):
    return [json.loads(line) for line in policy.supervisor.log.path.read_text().splitlines()]


def test_empty_sim_timeline_has_no_ground_events_and_uses_sim_time(tmp_path, model_stubs):
    policy = _construct(tmp_path, {"mission_package": document(), "sim_ground_events": []})
    try:
        assert _sim_advance(policy, 0) == "continue"
        assert _sim_advance(policy, 500) == "continue"
        assert policy.supervisor.started == 0
        assert policy.supervisor.last_heartbeat is None
        assert not [r for r in _records(policy) if "ground_event" in r["kind"]]
        with pytest.raises(ValueError, match="backwards"):
            _sim_advance(policy, 499)
    finally:
        policy.close()


def test_sim_link_silence_timeout_latches_before_late_reconnect(tmp_path, model_stubs):
    policy = _construct(
        tmp_path,
        {
            "mission_package": document("continue_then_return", timeout=30),
            "sim_ground_events": [
                {"t_s": 120, "event": "link_lost"},
                {"t_s": 160, "event": "link_restored"},
            ],
        },
    )
    try:
        assert _sim_advance(policy, 0) == "continue"
        assert _sim_advance(policy, 121) == "continue"
        assert _sim_advance(policy, 149) == "continue"
        assert _sim_advance(policy, 170) == "return_now"
        assert policy.supervisor.reason == "operator policy silence/pause timeout"
        assert _sim_advance(policy, 170) == "return_now"
        events = [r for r in _records(policy) if r["kind"] == "sim_ground_event"]
        assert [(r["timestamp"], r["event"]) for r in events] == [
            (120, "link_lost"),
            (160, "link_restored"),
        ]
    finally:
        policy.close()


def test_sim_pause_resume_order_and_abort_act_bypass_models(tmp_path, model_stubs):
    policy = _construct(
        tmp_path,
        {
            "mission_package": document(),
            "sim_ground_events": [
                {"t_s": 10, "event": "pause"},
                {"t_s": 20, "event": "resume"},
                {"t_s": 30, "event": "abort"},
            ],
        },
    )
    try:
        assert _sim_advance(policy, 0) == "continue"
        assert _sim_advance(policy, 10) == "hold"
        assert _sim_advance(policy, 20) == "continue"
        obs = {"frame_sensor_stamp_ns": 40_000_000_000}
        action, labels = policy.act(obs, pose_ned=(30, 0, -20), yaw_deg=180)
        assert action.kind == "hold"
        assert labels["onboard"]["supervisor_reason"] == "explicit operator abort"
        action, _ = policy.act(obs, pose_ned=(30, 0, -20), yaw_deg=180)
        assert action.kind == "translate"
        assert policy.supervisor._recovery_kind == "return_home"
        assert [
            r["event_type"]
            for r in _records(policy)
            if r["kind"] == "ground_event" and r["event_type"] != "heartbeat"
        ] == ["pause", "resume", "abort"]
    finally:
        policy.close()


@pytest.mark.parametrize("mode", ["link", "budget"])
def test_sim_return_now_and_budget_escalation(tmp_path, model_stubs, mode):
    package = document("return_now" if mode == "link" else "continue")
    if mode == "budget":
        # Admission must fit the mandatory 40 m out-and-back at the calibrated
        # 0.75 m/s speed; the supervisor still escalates once reserve is reached.
        package["budget"].update(time_s=180, reserve_time_s=60)
    policy = _construct(
        tmp_path,
        {
            "mission_package": package,
            "sim_ground_events": [{"t_s": 10, "event": "link_lost"}] if mode == "link" else [],
        },
    )
    try:
        assert _sim_advance(policy, 0) == "continue"
        assert _sim_advance(policy, 10) == "continue"
        # Reserve escalation occurs after the admitted 120 s usable window.
        assert _sim_advance(policy, 130) == "return_now"
        assert policy.supervisor.reason == (
            "operator link-loss policy" if mode == "link" else "remaining time at or below reserve"
        )
    finally:
        policy.close()


@pytest.mark.parametrize(
    "clock_field,units_per_ms",
    [("pose_time_boot_ms", 1), ("pose_sim_stamp_ns", 1_000_000)],
)
def test_sim_reserve_follows_vehicle_clock_when_camera_runs_ahead(
    tmp_path, model_stubs, clock_field, units_per_ms
):
    package = document()
    package["budget"].update(time_s=150, reserve_time_s=30)
    package["zones"] = {"zone:site": {"centre_ne": [0, 0], "radius_m": 55}}
    package["plan"]["steps"][0] = {
        "id": "work",
        "skill": "monitor",
        "params": {"region": "zone:site", "query": "backpacks", "duration_s": 900},
        "on_success": "home",
        "on_fail": "home",
    }
    policy = _construct(tmp_path, {"mission_package": package, "sim_ground_events": []})
    supervisor = policy.supervisor

    def advance(frame_ns, vehicle_ms):
        policy.sim_timeline.advance(
            {"frame_sensor_stamp_ns": frame_ns, clock_field: vehicle_ms * units_per_ms},
            supervisor,
            pose_ned=(30, 0, -20),
        )
        return supervisor.evaluate(pose_ned=(30, 0, -20))

    try:
        # Nightly step 0, then steps 33/34/42: PX4 stopped at 122.988 s
        # while camera stamps advanced from 156.556 to 364.224 s.
        assert advance(21_616_000_000, 21_616) == "continue"
        assert advance(120_188_000_000, 120_188) == "continue"
        assert advance(156_556_000_000, 122_988) == "continue"
        assert advance(364_224_000_000, 122_988) == "continue"
        assert policy.sim_timeline.now == pytest.approx(101.372)
        assert supervisor._terminal is None
        assert not supervisor.log.summary_path.exists()

        # The original 150/30 s reserve contract still latches at exactly
        # 120 s of vehicle progress, then brakes before starting return_home.
        assert advance(400_000_000_000, 141_615) == "continue"
        assert advance(401_000_000_000, 141_616) == "return_now"
        assert policy.sim_timeline.now == 120
        assert supervisor.reason == "remaining time at or below reserve"
        action, _ = supervisor.next_action(pose_ned=(30, 0, -20), yaw_deg=180)
        assert action.kind == "hold"
        action, info = supervisor.next_action(pose_ned=(30, 0, -20), yaw_deg=180)
        assert action.kind == "translate" and action.forward_m > 0
        assert info["skill"] == "return_home"
        assert info["supervisor_action"] == "return_now"
        assert supervisor.executor.history == []
        assert not [r for r in _records(policy) if r["kind"] == "ground_event"]
    finally:
        policy.close()


@pytest.mark.parametrize("value", [None, True, -1, float("nan"), float("inf"), "1000"])
def test_sim_timeline_keeps_bound_vehicle_clock_when_stamp_is_invalid(
    tmp_path, model_stubs, value
):
    policy = _construct(tmp_path, {"mission_package": document(), "sim_ground_events": []})
    try:
        policy.sim_timeline.advance(
            {"frame_sensor_stamp_ns": 10_000_000_000, "pose_time_boot_ms": 0},
            policy.supervisor,
        )
        assert policy.sim_timeline.now == 0
        with pytest.raises(ValueError, match="pose_time_boot_ms"):
            policy.sim_timeline.advance(
                {"frame_sensor_stamp_ns": 200_000_000_000, "pose_time_boot_ms": value},
                policy.supervisor,
            )
        assert policy.sim_timeline.now == 0
    finally:
        policy.close()


def test_sim_timeline_rejects_vehicle_clock_regression_despite_advancing_camera(
    tmp_path, model_stubs
):
    policy = _construct(tmp_path, {"mission_package": document(), "sim_ground_events": []})
    try:
        for frame_ns, vehicle_ms in [(10_000_000_000, 1000), (20_000_000_000, 2000)]:
            policy.sim_timeline.advance(
                {"frame_sensor_stamp_ns": frame_ns, "pose_time_boot_ms": vehicle_ms},
                policy.supervisor,
            )
        assert policy.sim_timeline.now == 1
        with pytest.raises(ValueError, match="backwards"):
            policy.sim_timeline.advance(
                {"frame_sensor_stamp_ns": 30_000_000_000, "pose_time_boot_ms": 1999},
                policy.supervisor,
            )
        assert policy.sim_timeline.now == 1
    finally:
        policy.close()


def test_sim_ground_events_follow_bound_vehicle_clock_in_order(tmp_path, model_stubs):
    events = [
        {"t_s": 10, "event": "pause"},
        {"t_s": 20, "event": "resume"},
        {"t_s": 30, "event": "abort"},
    ]
    policy = _construct(tmp_path, {"mission_package": document(), "sim_ground_events": events})
    try:
        for index, (vehicle_ms, expected) in enumerate(
            [
                (0, "continue"),
                (0, "continue"),
                (10_000, "hold"),
                (20_000, "continue"),
                (30_000, "return_now"),
                (30_000, "return_now"),
            ]
        ):
            policy.sim_timeline.advance(
                {
                    "frame_sensor_stamp_ns": (100 + index * 100) * 1_000_000_000,
                    "pose_time_boot_ms": vehicle_ms,
                    # The boot clock takes precedence over the secondary pose stamp too.
                    "pose_sim_stamp_ns": (100 + index * 100) * 1_000_000_000,
                },
                policy.supervisor,
                pose_ned=(30, 0, -20),
            )
            assert policy.supervisor.evaluate(pose_ned=(30, 0, -20)) == expected
        assert [
            (r["timestamp"], r["event"])
            for r in _records(policy)
            if r["kind"] == "sim_ground_event"
        ] == [(10, "pause"), (20, "resume"), (30, "abort")]
        assert policy.supervisor.reason == "explicit operator abort"
    finally:
        policy.close()


def test_live_battery_energy_can_stop_mission_before_image_or_models(tmp_path, model_stubs):
    policy = _construct(tmp_path, {"mission_package": document()})
    now = _clock(policy)
    try:
        observation = {
            "energy_telemetry": [
                {
                    "received_monotonic_s": now[0],
                    "message": {
                        "mavpackettype": "BATTERY_STATUS",
                        "id": 0,
                        "battery_remaining": 12,
                        "voltages": [16000] + [65535] * 9,
                        "current_battery": -1,
                        "current_consumed": 0,
                    },
                }
            ]
        }
        action, labels = policy.act(observation, pose_ned=(100, 0, -20), yaw_deg=180)
        assert action.kind == "hold"
        energy = labels["onboard"]["energy"]
        assert energy["action_margin_wh"] < 0
        assert energy["remaining_energy_wh"] >= energy["return_energy_wh"]
        assert labels["onboard"]["supervisor_action"] == "return_now"
        assert not labels["onboard"]["slow_calls"]
        assert not list(policy.work_dir.glob("frame_*.png"))
    finally:
        policy.close()


def test_sim_energy_receipt_timestamp_is_converted_to_sim_clock(tmp_path, model_stubs):
    policy = _construct(tmp_path, {"mission_package": document(), "sim_ground_events": []})
    try:
        action, labels = policy.act(
            {
                "frame_sensor_stamp_ns": 0,
                "mission_estimates": {"remaining_energy_wh": 0},
                "energy_telemetry": [
                    {
                        "received_monotonic_s": time.monotonic(),
                        "message": {
                            "mavpackettype": "BATTERY_STATUS",
                            "id": 0,
                            "battery_remaining": 90,
                            "voltages": [16000] + [65535] * 9,
                            "current_battery": -1,
                            "current_consumed": 0,
                        },
                    }
                ],
            },
            pose_ned=(0, 0, -20),
            yaw_deg=0,
        )
        assert action is not None
        assert labels["onboard"]["energy"] is not None
    finally:
        policy.close()


def test_sim_ground_timeline_evaluates_the_supervisor_with_the_vehicle_pose() -> None:
    # Without the pose the energy model has no return geometry and orders an
    # immediate return: every SIM safety mission returned for that reason.
    from scripts.onboard_policy import SimGroundTimeline

    class Supervisor:
        def __init__(self):
            self.poses = []
            self.package = type("P", (), {"mission_id": "m"})()
            self.log = type("L", (), {"append": lambda *a, **k: None})()

        def evaluate(self, pose_ned=None):
            self.poses.append(pose_ned)
            return "continue"

        def ground_event(self, event):
            pass

    supervisor = Supervisor()
    timeline = SimGroundTimeline([{"t_s": 0.0, "event": "pause"}])
    timeline.advance({"frame_sensor_stamp_ns": 0}, supervisor, pose_ned=(1.0, 2.0, -10.0))
    assert supervisor.poses == [(1.0, 2.0, -10.0)]


class _Evaluated(BaseException):
    """Stop act() right after supervision; continuing would need the models."""


def _battery_sample(host_s, vehicle_s=None):
    sample = {
        "received_monotonic_s": host_s,
        "message": {
            "mavpackettype": "BATTERY_STATUS",
            "id": 0,
            "battery_remaining": 90,
            "voltages": [16000] + [65535] * 9,
            "current_battery": -1,
            "current_consumed": 0,
        },
    }
    if vehicle_s is not None:
        sample["received_vehicle_s"] = vehicle_s
    return sample


def _supervised_steps(policy, steps, *, stall_s=0.0, gaps_s=None):
    """Feed (host_s, vehicle_s, samples) observations; return energy per step.

    ``stall_s`` advances the supervisor's host clock between ingest and
    evaluate, as the 5.6 s mission-log stall in SITL run8 did.
    """
    now = _clock(policy)
    # Operator-link SIM: the supervisor runs on host monotonic time, the same
    # clock as the reader's receipts, so the receipt offset is zero.
    policy._energy_receipt_clock_offset = 0.0
    supervisor = policy.supervisor
    real_update, real_evaluate = supervisor.update_estimates, supervisor.evaluate
    estimates = []

    def update_estimates(**kwargs):
        now[0] += stall_s
        return real_update(**kwargs)

    def evaluate(**kwargs):
        result = real_evaluate(**kwargs)
        estimates.append((result, dict(supervisor.energy_estimate)))
        raise _Evaluated

    supervisor.update_estimates, supervisor.evaluate = update_estimates, evaluate
    for host_s, vehicle_s, samples in steps:
        now[0] = max(now[0], host_s)
        observation = {"pose_time_boot_ms": int(vehicle_s * 1000), "energy_telemetry": samples}
        if gaps_s is not None:
            observation["vehicle_telemetry_gaps_s"] = gaps_s
        with pytest.raises(_Evaluated):
            policy.act(observation, pose_ned=(0, 0, -20), yaw_deg=0)
    return estimates


def _slow_sitl_steps(start_s, *, rtf, battery_period_s, steps, step_vehicle_s):
    """A lockstep SITL at ``rtf`` x real time; BATTERY_STATUS on the sim clock.

    Each observation lands just before the next battery message, the worst
    phase for a host-clock age (run8 saw 4.3 s at the observation itself).
    """
    rows = []
    for index in range(1, steps + 1):
        vehicle = 200.0 + index * step_vehicle_s
        battery_vehicle = vehicle - battery_period_s + 0.05
        host = start_s + (vehicle - 200.0) / rtf
        battery_host = host - (vehicle - battery_vehicle) / rtf
        rows.append((host, vehicle, [_battery_sample(battery_host, battery_vehicle)]))
    return rows


@pytest.mark.parametrize(
    "scenario",
    [
        # PX4 lockstep at 0.3x real time, 0.5 Hz BATTERY_STATUS, 7 s translates.
        "slow_lockstep",
        # run8: battery 0.14 s old at the observation, 5.6 s host stall before
        # evaluate while the simulation advanced only 0.2 s.
        "policy_stall",
    ],
)
def test_sitl_battery_freshness_is_judged_on_the_vehicle_clock(tmp_path, model_stubs, scenario):
    policy = _construct(tmp_path, {"mission_package": document()})
    start = policy.supervisor.started
    try:
        if scenario == "slow_lockstep":
            steps = _slow_sitl_steps(
                start, rtf=0.3, battery_period_s=2.0, steps=6, step_vehicle_s=7.0
            )
            estimates = _supervised_steps(policy, steps)
        else:
            steps = [
                (
                    start + 16.0 * i,
                    228.592 + 7.5 * i,
                    [_battery_sample(start + 16.0 * i - 0.14, 228.55 + 7.5 * i)],
                )
                for i in range(1, 4)
            ]
            estimates = _supervised_steps(policy, steps, stall_s=5.6)
        assert [result for result, _ in estimates] == ["continue"] * len(steps)
        assert all(energy["action"] == "continue" for _, energy in estimates)
        # The newest sample's age is its age on the simulation clock.
        assert max(energy["battery_age_s"] for _, energy in estimates) < 2.0
        assert policy.supervisor.energy_estimator.clock_bound
    finally:
        policy.close()


def test_sitl_battery_that_stops_arriving_still_returns(tmp_path, model_stubs):
    policy = _construct(tmp_path, {"mission_package": document()})
    start = policy.supervisor.started
    try:
        # Battery received once at vehicle 200.5 s; pose keeps advancing the
        # vehicle clock, so the reading ages on that clock until it is stale.
        steps = [(start + 1.0, 201.0, [_battery_sample(start + 0.5, 200.5)])]
        steps += [(start + 1.0 + 3.0 * i, 201.0 + 1.0 * i, []) for i in range(1, 7)]
        estimates = _supervised_steps(policy, steps)
        assert estimates[0][0] == "continue"
        assert estimates[-1][0] == "return_now"
        assert policy.supervisor.reason == "battery telemetry stale; return while feasible"
        assert [energy["action"] for _, energy in estimates].index("return_now") == 5
        assert estimates[-1][1]["battery_age_s"] == pytest.approx(6.5)
    finally:
        policy.close()


@pytest.mark.parametrize("reader_saw_gap", [False, True])
def test_sitl_link_gap_is_not_charged_to_the_battery(tmp_path, model_stubs, reader_saw_gap):
    """Run9 replay: a 20 s host stall lost PX4 telemetry from 354.016 to 361.096 s.

    The last battery (SYS_STATUS) was at 353.5 s; the first post-gap pose made
    it 7.7 s old and the mission returned.  With the reader's gap the battery is
    judged from link recovery; if it then stays silent it still goes stale.
    """
    policy = _construct(tmp_path, {"mission_package": document()})
    start = policy.supervisor.started
    try:
        steps = [
            (start + 1.0, 353.6, [_battery_sample(start + 0.9, 353.5)]),
            (start + 24.0, 361.168, []),
            # The battery never reports again after recovery.
            (start + 40.0, 366.3, []),
        ]
        gaps = [[354.016, 361.096]] if reader_saw_gap else None
        estimates = _supervised_steps(policy, steps, gaps_s=gaps)
        assert estimates[1][1]["battery_age_s"] == pytest.approx(7.668)
        if not reader_saw_gap:
            assert estimates[1][0] == "return_now"
            assert policy.supervisor.reason == "battery telemetry stale; return while feasible"
            return
        assert [result for result, _ in estimates[:2]] == ["continue", "continue"]
        assert estimates[1][1]["link_gap_excluded_s"] == pytest.approx(7.08)
        assert [
            record["gap_s"]
            for record in map(json.loads, policy.supervisor.log.path.read_text().splitlines())
            if record["kind"] == "vehicle_telemetry_gap"
        ] == [pytest.approx(7.08)]
        # Stale once 5 s of live link have passed without a battery report.
        assert estimates[2][0] == "return_now"
        assert policy.supervisor.reason == "battery telemetry stale; return while feasible"
    finally:
        policy.close()


@pytest.mark.parametrize("stamped", [False, True])
def test_flight_battery_freshness_stays_on_host_receipt_time(tmp_path, model_stubs, stamped):
    policy = _construct(tmp_path, {"mission_package": document()})
    # A real aircraft: a stall really ages the data, so host receipt time rules
    # even if a reader also stamped the vehicle clock.
    policy.supervisor._flight_mode = True
    start = policy.supervisor.started
    try:
        steps = [
            (
                start + 1.0,
                201.0,
                [_battery_sample(start + 0.9, 200.9 if stamped else None)],
            ),
            (start + 7.0, 201.1, []),
        ]
        estimates = _supervised_steps(policy, steps)
        assert estimates[0][0] == "continue"
        assert estimates[1][0] == "return_now"
        assert policy.supervisor.reason == "battery telemetry stale; return while feasible"
        assert estimates[1][1]["battery_age_s"] == pytest.approx(6.1)
        assert not policy.supervisor.energy_estimator.clock_bound
    finally:
        policy.close()
