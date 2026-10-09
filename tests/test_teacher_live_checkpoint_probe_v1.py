"""CPU wiring/ownership checks; none of these fixtures establish SIM flight."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_teacher_live_checkpoint_probe_v1 as worker
from scripts import supervise_teacher_live_checkpoint_probe_v1 as supervisor
from scripts.onboard_teacher_sim_operator_v2 import DisposableSimOperatorIntent
from scripts.run_simulator_docker import PINNED_IMAGE

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    diagnostics = tmp_path / "artifacts/diagnostics"
    diagnostics.mkdir(parents=True)
    monkeypatch.setattr(supervisor, "ROOT", tmp_path)
    scenario = tmp_path / "fixture.scenario.json"
    scenario.write_text("{}")
    scenario.with_name("fixture.runtime.json").write_text("{}")
    plan = SimpleNamespace(
        frame_count=2,
        source_manifest_path=scenario,
        docker_plan=SimpleNamespace(world_path=scenario),
        to_mapping=lambda: {"CPU_fixture": True},
    )
    monkeypatch.setattr(worker, "source_inventory", lambda *_: [])
    intent = DisposableSimOperatorIntent.from_mapping(
        json.loads((ROOT / "configs/simulation/teacher-sim-operator-20261008-r2.json").read_bytes())
    )
    return tmp_path, diagnostics, plan, intent


def test_plan_only_has_no_session_or_current_authority(fixture):
    root, diagnostics, plan, intent = fixture
    output = diagnostics / "worker"
    report = worker.run_probe(plan, output, operator_intent=intent, artifact_root=root)
    assert report["status"] == "planned"
    assert report["checkpoint_qualified_while_session_open"] is False
    assert report["external_SIM_observed_here"] is False
    assert set(report["claims"].values()) == {False}
    assert not output.exists()


@pytest.mark.parametrize("capture,collector", [(3, 3), (4, 1), (4, 2), (True, 1), (4, True)])
def test_mixed_resource_versions_refused_before_output(fixture, capture, collector):
    root, diagnostics, plan, intent = fixture
    with pytest.raises(ValueError, match="compatible"):
        worker.run_probe(
            plan,
            diagnostics / "worker",
            operator_intent=intent,
            artifact_root=root,
            capture_version=capture,
            collector_version=collector,
        )
    assert not (diagnostics / "worker").exists()


def test_new_capture_plan_binds_no_current_authority(fixture):
    root, diagnostics, plan, intent = fixture
    plan.session_timeout_s = 360
    report = worker.run_probe(
        plan,
        diagnostics / "worker",
        operator_intent=intent,
        artifact_root=root,
        capture_version=4,
        collector_version=3,
    )
    assert report["status"] == "planned"
    assert report["lifecycle_budget"] is None
    assert report["capture_version"] == 4 and report["collector_version"] == 3
    assert set(report["claims"].values()) == {False}
    assert not (diagnostics / "worker").exists()


def test_budget_is_bound_to_original_session_start_not_current_now():
    now = [140.0]
    session = SimpleNamespace(
        deadline_s=460,
        plan=SimpleNamespace(session_timeout_s=360),
        clock=SimpleNamespace(session_id="CPU-original-clock", monotonic=lambda: now[0]),
    )
    budget = worker.lifecycle_budget_for_probe(session, 512)
    assert budget.started_at_s == 100 and budget.deadline_s == 410
    assert budget.data_byte_limit < 2 * 1024**3
    assert budget.record_limit > 16384
    now[0] = 150
    assert worker.lifecycle_budget_for_probe(session, 512) == budget
    now[0] = 220
    with pytest.raises(ValueError, match="deadline exhausted"):
        worker.lifecycle_budget_for_probe(session, 512)


def test_budget_refuses_insufficient_queue_and_session():
    session = SimpleNamespace(
        deadline_s=340,
        plan=SimpleNamespace(session_timeout_s=240),
        clock=SimpleNamespace(session_id="CPU-clock", monotonic=lambda: 140),
    )
    with pytest.raises(ValueError, match="session cannot"):
        worker.lifecycle_budget_for_probe(session, 512)
    session.deadline_s, session.plan.session_timeout_s = 460, 360
    with pytest.raises(ValueError, match="Queue cannot"):
        worker.lifecycle_budget_for_probe(session, 16)


def test_supervisor_refuses_lifecycle_that_outlasts_its_process_budget(fixture):
    _, diagnostics, _, _ = fixture
    with pytest.raises(ValueError, match="entire NEW"):
        supervisor.supervise(
            diagnostics / "supervisor",
            diagnostics / "worker",
            capture_version=4,
            collector_version=3,
            timeout_s=300,
            session_timeout_s=360,
        )
    assert not (diagnostics / "supervisor").exists()


@pytest.mark.parametrize("execute", [1, "yes", None])
def test_non_boolean_execute_is_rejected_before_any_session(fixture, execute):
    root, diagnostics, plan, intent = fixture
    with pytest.raises(ValueError, match="boolean"):
        worker.run_probe(
            plan,
            diagnostics / "worker",
            operator_intent=intent,
            artifact_root=root,
            execute=execute,
        )


def test_wrong_intent_and_two_frame_scope_rejected(fixture):
    root, diagnostics, plan, intent = fixture
    with pytest.raises(ValueError, match="exact SIM"):
        worker.run_probe(plan, diagnostics / "worker", operator_intent={}, artifact_root=root)
    plan.frame_count = 4
    with pytest.raises(ValueError, match="two original"):
        worker.run_probe(plan, diagnostics / "worker", operator_intent=intent, artifact_root=root)


def test_output_is_fresh_and_contained(fixture):
    root, diagnostics, _, _ = fixture
    assert worker.fresh_output(diagnostics / "new", root) == diagnostics / "new"
    with pytest.raises(ValueError, match="contained"):
        worker.fresh_output(root / "other", root)
    with pytest.raises(ValueError, match="contained"):
        worker.fresh_output(diagnostics, root)
    existing = diagnostics / "existing"
    existing.mkdir()
    with pytest.raises(ValueError, match="contained"):
        worker.fresh_output(existing, root)


def test_xml_is_bound_as_whole_file_not_a_json_pointer(fixture):
    root, _, _, _ = fixture
    path = root / "model.sdf"
    path.write_bytes(b'<sdf version="1.9" />')
    source = worker.whole_file_source(path, root)
    assert source.json_pointer is None
    assert source.path == "model.sdf"
    assert source.bytes == len(path.read_bytes())
    assert worker._verify_source(source, root) == path.read_bytes()


def cleanup_fixture(root, diagnostics, *, token="a" * 32, cid="b" * 64):
    session = diagnostics.parent / (diagnostics.name + "-job") / "teacher-owned-session"
    session.mkdir(parents=True)
    (session / "intent.json").write_text(
        json.dumps(
            {
                "owner_token": token,
                "plan": {
                    "docker_plan": {
                        "docker_executable": "docker",
                        "container_name": "multidetect-teacher-gazebo",
                        "image": PINNED_IMAGE,
                    }
                },
            }
        )
    )
    (session / "native-container.cid").write_text(cid)
    return cid


def test_exact_absence_is_not_arbitrary_docker_failure(fixture, monkeypatch):
    root, diagnostics, _, _ = fixture
    worker_dir, output = diagnostics / "worker", diagnostics / "supervisor"
    output.mkdir()
    cid = cleanup_fixture(root, worker_dir)
    monkeypatch.setattr(supervisor.shutil, "which", lambda _: "docker")
    monkeypatch.setattr(
        supervisor,
        "_native",
        lambda argv, *_: (
            subprocess.CompletedProcess(argv, 1, b"[]", f"No such container: {cid}\n".encode()),
            {},
        ),
    )
    assert supervisor.verify_cleanup(worker_dir, output)["verified"] is True
    monkeypatch.setattr(
        supervisor,
        "_native",
        lambda argv, *_: (
            subprocess.CompletedProcess(argv, 1, b"[]", b"Docker daemon unavailable"),
            {},
        ),
    )
    assert supervisor.verify_cleanup(worker_dir, output)["verified"] is False


def test_foreign_container_never_stopped_or_removed(fixture, monkeypatch):
    root, diagnostics, _, _ = fixture
    worker_dir, output = diagnostics / "worker", diagnostics / "supervisor"
    output.mkdir()
    cid = cleanup_fixture(root, worker_dir)
    monkeypatch.setattr(supervisor.shutil, "which", lambda _: "docker")
    commands = []

    def native(argv, *_):
        commands.append(argv)
        payload = [
            {
                "Id": cid,
                "Name": "/multidetect-teacher-gazebo",
                "Config": {
                    "Image": PINNED_IMAGE,
                    "Labels": {"multidetect.teacher-session-owner": "foreign"},
                },
            }
        ]
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload).encode(), b""), {}

    monkeypatch.setattr(supervisor, "_native", native)
    report = supervisor.verify_cleanup(worker_dir, output)
    assert report["verified"] is False and "untouched" in report["error"]
    assert len(commands) == 1 and commands[0][1:3] == ["container", "inspect"]


def test_owned_recovery_targets_exact_cid_and_rechecks_absence(fixture, monkeypatch):
    root, diagnostics, _, _ = fixture
    worker_dir, output = diagnostics / "worker", diagnostics / "supervisor"
    output.mkdir()
    cid = cleanup_fixture(root, worker_dir)
    monkeypatch.setattr(supervisor.shutil, "which", lambda _: "docker")
    commands = []

    def native(argv, *_):
        commands.append(argv)
        if len(commands) == 1:
            payload = [
                {
                    "Id": cid,
                    "Name": "/multidetect-teacher-gazebo",
                    "Config": {
                        "Image": PINNED_IMAGE,
                        "Labels": {
                            "multidetect.teacher-session-owner": "a" * 32,
                            "multidetect.teacher-session-purpose": "truth-free-teacher-observation",
                            "multidetect.purpose": "simulator-docker",
                        },
                    },
                }
            ]
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload).encode(), b""), {}
        if len(commands) == 4:
            return subprocess.CompletedProcess(
                argv, 1, b"[]", f"No such object: {cid}".encode()
            ), {}
        return subprocess.CompletedProcess(argv, 0, cid.encode(), b""), {}

    monkeypatch.setattr(supervisor, "_native", native)
    assert supervisor.verify_cleanup(worker_dir, output)["verified"] is True
    assert commands[1] == ["docker", "stop", "--timeout", "2", cid]
    assert commands[2] == ["docker", "rm", cid]
    assert all("--force" not in command for command in commands)


def test_cleanup_original_executable_cannot_redirect(fixture, monkeypatch):
    root, diagnostics, _, _ = fixture
    worker_dir, output = diagnostics / "worker", diagnostics / "supervisor"
    output.mkdir()
    cleanup_fixture(root, worker_dir)
    monkeypatch.setattr(supervisor.shutil, "which", lambda _: "other-docker")
    with pytest.raises(ValueError, match="executable"):
        supervisor.verify_cleanup(worker_dir, output)


def test_stop_timeout_still_attempts_exact_rm_and_final_inspection(fixture, monkeypatch):
    root, diagnostics, _, _ = fixture
    worker_dir, output = diagnostics / "worker", diagnostics / "supervisor"
    output.mkdir()
    cid = cleanup_fixture(root, worker_dir)
    monkeypatch.setattr(supervisor.shutil, "which", lambda _: "docker")
    commands = []

    def native(argv, *_):
        commands.append(argv)
        if len(commands) == 1:
            record = {
                "Id": cid,
                "Name": "/multidetect-teacher-gazebo",
                "Config": {
                    "Image": PINNED_IMAGE,
                    "Labels": {
                        "multidetect.teacher-session-owner": "a" * 32,
                        "multidetect.teacher-session-purpose": "truth-free-teacher-observation",
                        "multidetect.purpose": "simulator-docker",
                    },
                },
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps([record]).encode(), b""), {}
        if argv[1] == "stop":
            raise subprocess.TimeoutExpired(argv, 15)
        if argv[1] == "rm":
            return subprocess.CompletedProcess(argv, 0, cid.encode(), b""), {}
        return subprocess.CompletedProcess(argv, 1, b"[]", f"No such container: {cid}".encode()), {}

    monkeypatch.setattr(supervisor, "_native", native)
    result = supervisor.verify_cleanup(worker_dir, output)
    assert commands[2] == ["docker", "rm", cid]
    assert commands[3] == ["docker", "container", "inspect", cid]
    assert result["absence_observed_after_cleanup"] is True
    assert result["verified"] is False  # Do not erase the timed-out operation.
    assert result["native_rows"][1]["native_exit_code"] is None
    assert "TimeoutExpired" in result["native_rows"][1]["error"]


def test_one_shot_origin_is_not_a_live_sensor_in_initial_ready_gate():
    ages = {name: 0.1 for name in (*worker.REQUIRED_MESSAGES, *worker.EXTRA_MESSAGES)}
    ages["GPS_GLOBAL_ORIGIN"] = 3.0

    def latest(kind, *, now_s, maximum_age_s):
        return {"received_at_s": now_s - ages[kind]} if ages[kind] <= maximum_age_s else None

    capture = SimpleNamespace(latest_message=latest)
    assert worker.observation_streams_ready(capture, 100.0) is True
    ages["HEARTBEAT"] = 2.1
    assert worker.observation_streams_ready(capture, 100.0) is False
    ages["HEARTBEAT"] = 0.1
    ages["GPS_GLOBAL_ORIGIN"] = 30.1
    assert worker.observation_streams_ready(capture, 100.0) is False


@pytest.mark.parametrize("duration", [True, 0, 541, float("nan")])
def test_invalid_total_watchdog_budget_before_launch(fixture, duration):
    _, diagnostics, _, _ = fixture
    with pytest.raises(ValueError):
        supervisor.supervise(diagnostics / "supervisor", diagnostics / "worker", timeout_s=duration)


def test_native_logs_include_original_empty_stderr(fixture, monkeypatch):
    _, diagnostics, _, _ = fixture
    output = diagnostics / "native"
    output.mkdir()
    monkeypatch.setattr(
        supervisor.subprocess,
        "run",
        lambda command, **_: subprocess.CompletedProcess(command, 1, b"[]", b""),
    )
    _, row = supervisor._native(["docker", "container", "inspect", "b" * 64], output, "inspect")
    assert row["streams"]["stderr"]["bytes"] == 0
    assert Path(row["streams"]["stderr"]["path"]).read_bytes() == b""


def reference_fixture(*, change_at=None, moving=False):
    clock = SimpleNamespace(value=100.0)
    clock.monotonic = lambda: clock.value
    seen = []

    def latest(kind, *, now_s, maximum_age_s):
        assert now_s == clock.value
        assert maximum_age_s == (30 if kind == "GPS_GLOBAL_ORIGIN" else 2)
        changed = change_at is not None and clock.value >= change_at
        value = 2 if changed else 1
        messages = {
            "HOME_POSITION": {
                "latitude": 10,
                "longitude": 20,
                "altitude": 30,
                "x": 0,
                "y": 0,
                "z": 0,
                "time_usec": value,
            },
            "GPS_GLOBAL_ORIGIN": {"latitude": 10, "longitude": 20, "altitude": 30, "time_usec": 1},
            "LOCAL_POSITION_NED": {"vx": 1 if moving else 0, "vy": 0, "vz": 0},
        }
        return {
            "message": messages[kind],
            "record_index": int(clock.value * 1000),
            "received_at_s": clock.value - 0.1,
        }

    def drain():
        clock.value += 0.1
        seen.append(clock.value)

    return SimpleNamespace(latest_message=latest), clock, drain, seen


def test_stability_wait_does_not_delete_startup_reference_changes():
    capture, clock, drain, seen = reference_fixture(change_at=101.5)
    report = worker.wait_reference_stability(capture, clock, drain, deadline_s=110, stable_s=2)
    assert len(report["observed_reference_changes"]) == 2
    assert report["finished_at_s"] - report["unchanged_since_s"] >= 2
    assert report["finished_at_s"] >= 103.5
    assert len(seen) > 30
    assert report["packets_discarded"] is False
    assert report["HOME_manually_set"] is False
    assert report["navigation_qualified"] is False


def test_stability_wait_is_bounded_and_does_not_accept_moving_ground():
    capture, clock, drain, _ = reference_fixture(moving=True)
    with pytest.raises(ValueError, match="stabilization"):
        worker.wait_reference_stability(capture, clock, drain, deadline_s=101, stable_s=2)
