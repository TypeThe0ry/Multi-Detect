from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from multidetect.px4_mission import Px4MissionError

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "vtol_sitl_mission_acceptance.py"
MANIFEST = ROOT / "configs" / "scenarios" / "river_rescue_vtol_001.scenario.json"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
CONTAINER_ID = "d" * 64


@pytest.mark.parametrize("returncode,stdout", [(0, "FW_R_LIM: 50"), (1, ""), (0, "x" * 5000)])
def test_dynamics_snapshot_is_read_only_and_preserves_missing_output(
    monkeypatch, returncode, stdout
):
    from types import SimpleNamespace

    module = _module()
    collector = object()
    calls = []

    def prepare(actual_collector, run, docker, command, **kwargs):
        assert actual_collector is collector
        assert run is module._run
        assert docker == "docker-test"
        assert kwargs == {"timeout_s": 10.0, "check": False}
        calls.append(command)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="raw diagnostic")

    monkeypatch.setattr(module, "_prepare_while_collecting", prepare)
    result = module.read_sim_dynamics_parameters(collector, "docker-test", CONTAINER_ID)
    assert len(calls) == 11
    assert calls[-1] == ["exec", CONTAINER_ID, "/opt/px4-gazebo/bin/px4-ver", "all"]
    assert all(
        command[:4]
        == [
            "exec",
            CONTAINER_ID,
            "/opt/px4-gazebo/bin/px4-param",
            "show",
        ]
        for command in calls[:-1]
    )
    assert {command[-1] for command in calls[:-1]} == set(result["parameters"])
    version = result["firmware_version_observation"]
    assert version["returncode"] == returncode
    assert version["stdout"] == stdout[:4096]
    assert version["output_truncated"] == (len(stdout) > 4096)
    assert version["reproducible_binary_source_binding_proven"] is False
    assert result["changes_parameters"] is False
    assert result["numeric_values_validated"] is False
    assert result["airframe_capability_proven"] is False
    assert result["parameters"]["FW_R_LIM"]["returncode"] == returncode
    assert result["parameters"]["FW_R_LIM"]["stdout"] == stdout[:4096]
    assert result["parameters"]["FW_R_LIM"]["output_truncated"] == (len(stdout) > 4096)


def test_both_sim_state_streams_configured_on_exact_instance(monkeypatch):
    from types import SimpleNamespace

    module = _module()
    calls = []

    def prepare(collector, run, docker, command, **kwargs):
        calls.append(command)
        assert kwargs == {"timeout_s": 10.0}
        return SimpleNamespace(returncode=0, stdout="configured")

    monkeypatch.setattr(module, "_prepare_while_collecting", prepare)
    result = module.configure_sim_state_streams(object(), "docker", CONTAINER_ID, 18570)
    assert [c[7] for c in calls] == [
        "HEARTBEAT",
        "VFR_HUD",
        "EXTENDED_SYS_STATE",
        "NAV_CONTROLLER_OUTPUT",
        "ATTITUDE_TARGET",
        "GLOBAL_POSITION_INT",
    ]
    for call in calls:
        assert call[:3] == ["exec", CONTAINER_ID, "/opt/px4-gazebo/bin/px4-mavlink"]
        assert call[3:7] == ["stream", "-u", "18570", "-s"]
        assert call[8:] == ["-r", "10"]
    assert module.MAXIMUM_FLIGHT_STATE_AGE_S == 1.5
    for observation in result.values():
        assert observation["freshness_threshold_changed"] is False
        assert observation["host_receive_rate_proven"] is False


@pytest.mark.parametrize("code,output", [(1, ""), (0, "ERROR"), (0, "not running")])
def test_sim_state_stream_rejection_is_not_success(monkeypatch, code, output):
    from types import SimpleNamespace

    module = _module()
    monkeypatch.setattr(
        module,
        "_prepare_while_collecting",
        lambda *a, **kw: SimpleNamespace(returncode=code, stdout=output),
    )
    with pytest.raises(module.VtolMissionAcceptanceError, match="stream configuration"):
        module.configure_sim_state_streams(object(), "docker", CONTAINER_ID, 18570)


def _energy_cli_args():
    return [
        "--evidence",
        "unused.json",
        "--container-name",
        "test",
        "--expected-container-id",
        CONTAINER_ID,
        "--evaluator-port",
        "14691",
        "--airframe-profile-sha256",
        HASH_A,
        "--runtime-descriptor-sha256",
        HASH_B,
        "--runtime-provenance-sha256",
        HASH_C,
    ]


@pytest.mark.parametrize("options", [
    {"policy_artifact": Path("absent.json")},
    {"policy_artifact_file_sha256": HASH_A},
    {"policy_artifact": Path("absent.json"), "policy_artifact_file_sha256": HASH_A},
])
def test_outer_policy_rejects_invalid_scope_before_side_effects(tmp_path, monkeypatch, options):
    from scripts import run_vtol_mission_sensor_capture as outer

    monkeypatch.setattr(outer, "_resolve_docker_path", lambda *a: pytest.fail("Docker touched"))
    with pytest.raises(outer.VtolMissionSensorCaptureError, match="policy artifact"):
        outer.run_vtol_mission_sensor_capture(
            evidence_path=tmp_path / "evidence.json",
            run_directory=tmp_path / "run", execute=True, **options,
        )
    assert not list(tmp_path.iterdir())


def test_outer_cli_forwards_policy_identity(tmp_path, monkeypatch):
    from scripts import run_vtol_mission_sensor_capture as outer

    calls = []
    monkeypatch.setattr(outer, "run_vtol_mission_sensor_capture",
                        lambda *a, **kw: calls.append(kw) or {"gate_passed": False})
    policy = tmp_path / "policy.json"
    assert outer.main(["--campaign-file", str(tmp_path / "campaign.json"),
                       "--policy-artifact", str(policy),
                       "--policy-artifact-file-sha256", HASH_A]) == 0
    assert calls[0]["policy_artifact"] == policy
    assert calls[0]["policy_artifact_file_sha256"] == HASH_A


def test_energy_cli_legacy_dispatch(monkeypatch):
    module = _module()
    calls = []
    monkeypatch.setattr(module, "_run_cli", lambda args, **kw: calls.append(kw) or 0)
    assert module.main(_energy_cli_args()) == 0
    assert calls == [{}]


@pytest.mark.parametrize("ack", [False, True])
def test_energy_cli_partial_configuration_rejected(monkeypatch, ack):
    module = _module()
    monkeypatch.setattr(
        module, "MissionTelemetrySession", lambda **kw: pytest.fail("opened socket")
    )
    args = _energy_cli_args() + ["--energy-capacity-wh", "100"]
    if ack:
        args += ["--acknowledge-owned-disposable-sitl"]
    with pytest.raises(SystemExit, match="2"):
        module.main(args)


@pytest.mark.parametrize("ack", [False, True])
def test_energy_cli_explicit_session_lifecycle(monkeypatch, ack):
    module = _module()
    events = []

    class Session:
        def __init__(self, **kw):
            events.append(kw)

        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, *exc):
            events.append("close")

    def run(args, *, telemetry_session):
        assert isinstance(telemetry_session, Session)
        raise RuntimeError("dispatch failed")

    monkeypatch.setattr(module, "MissionTelemetrySession", Session)
    monkeypatch.setattr(module, "_run_cli", run)
    args = _energy_cli_args() + [
        "--energy-capacity-wh",
        "100",
        "--energy-avionics-power-w",
        "36",
        "--energy-propulsion-max-power-w",
        "324",
        "--energy-initial-remaining-wh",
        "90",
        "--energy-provenance",
        "synthetic-test",
    ]
    if not ack:
        with pytest.raises(SystemExit, match="2"):
            module.main(args)
        assert events == []
    else:
        with pytest.raises(RuntimeError, match="dispatch failed"):
            module.main(args + ["--acknowledge-owned-disposable-sitl"])
        assert events[0]["energy_profile"].capacity_wh == 100
        assert events[0]["initial_remaining_wh"] == 90
        assert events[1:] == ["enter", "close"]


def _module() -> ModuleType:
    scripts = str(SCRIPT.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    name = "vtol_sitl_mission_acceptance_test_module"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("fault", [None, "source", "equal_boundary", "future", "invalid"])
def test_terminal_tail_preserves_completion_and_qualifies_sample(monkeypatch, fault):
    module = _module()
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    class Collector:
        calls = 0

        def pump(self, timeout):
            self.calls += 1
            clock[0] += timeout

        def post_mission_observation(self, *, now_s):
            sample = {
                "type": "GLOBAL_POSITION_INT",
                "field": "time_boot_ms",
                "value": 1000,
                "at_s": now_s - 90,
                "source_system": 1,
                "source_component": 1,
                "field_valid": True,
            }
            if fault == "source":
                sample["source_system"] = 2
            elif fault == "equal_boundary":
                sample["at_s"] = 10.0
            elif fault == "future":
                sample["at_s"] += 10
            elif fault == "invalid":
                sample["field_valid"] = False
            return {
                "observed_at_s": now_s,
                "energy_input_history": {
                    "receive_clock": "host_monotonic_since_collector_start",
                    "receive_origin_host_monotonic_s": 90,
                    "samples": [sample],
                },
            }

    collector = Collector()
    observation, evidence = module.collect_terminal_observation_tail(
        collector, completed_at_s=100, timeout_s=0.5
    )
    assert evidence["completed_host_monotonic_s"] == 100
    assert evidence["end_position_sample_received"] is (fault is None)
    assert not evidence["full_energy_coverage_claimed"]
    assert not evidence["authorizes_next_task"]
    assert observation["observed_at_s"] > 100
    assert 1 <= collector.calls <= 4


def test_terminal_tail_missing_adapter_is_explicit():
    observation, evidence = _module().collect_terminal_observation_tail(
        object(), completed_at_s=100
    )
    assert not observation["telemetry_complete_and_fresh"]
    assert evidence["reason"] == "collector_observation_adapter_unavailable"
    assert evidence["pump_calls"] == 0


def test_terminal_tail_frozen_clock_is_bounded(monkeypatch):
    module = _module()
    monkeypatch.setattr(module.time, "monotonic", lambda: 100)

    class Collector:
        def pump(self, timeout):
            pass

        def post_mission_observation(self, *, now_s):
            return {"observed_at_s": now_s, "energy_input_history": {}}

    _, evidence = module.collect_terminal_observation_tail(Collector(), completed_at_s=100)
    assert evidence["pump_calls"] == 256
    assert not evidence["end_position_sample_received"]


class _Message:
    def __init__(
        self,
        message_type: str,
        *,
        source_system: int = 1,
        source_component: int = 1,
        **fields: Any,
    ) -> None:
        self._message_type = message_type
        self._source_system = source_system
        self._source_component = source_component
        for name, value in fields.items():
            setattr(self, name, value)

    def get_type(self) -> str:
        return self._message_type

    def get_srcSystem(self) -> int:
        return self._source_system

    def get_srcComponent(self) -> int:
        return self._source_component


class _MavRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def mission_count_send(self, *args: Any) -> None:
        self.calls.append(("MISSION_COUNT", args))

    def mission_item_int_send(self, *args: Any) -> None:
        self.calls.append(("MISSION_ITEM_INT", args))

    def mission_request_list_send(self, *args: Any) -> None:
        self.calls.append(("MISSION_REQUEST_LIST", args))

    def mission_request_int_send(self, *args: Any) -> None:
        self.calls.append(("MISSION_REQUEST_INT", args))


class _Connection:
    def __init__(self, messages: list[_Message | None]) -> None:
        self.mav = _MavRecorder()
        self._messages = iter(messages)

    def recv_match(self, **_kwargs: Any) -> _Message | None:
        return next(self._messages)


def _live_context() -> dict[str, Any]:
    return {
        "captured_at_s": 12.5,
        "latitude_deg": 1.3000,
        "longitude_deg": 103.8000,
        "gps_fix_type": 3,
        "satellites_visible": 12,
        "gps_horizontal_accuracy_m": 0.8,
        "gps_accuracy_source": "GPS_RAW_INT.h_acc",
        "gps_estimator_disagreement_m": 0.2,
        "battery_remaining_pct": 93.0,
        "estimated_flight_time_s": 1800.0,
        "ground_speed_mps": 0.0,
        "airspeed_mps": 0.0,
        "source": "live MAVLink telemetry only",
    }


def _documents(
    module: ModuleType,
    *,
    telemetry=None,
    maximum_duration_s=600.0,
    projection=None,
    home=None,
    provider=None,
    receive_clock=None,
    reference_profile="lifecycle-smoke",
    original_prompt=None,
    require_original_prompt=False,
):
    runtime_input = module.load_runtime_scenario(MANIFEST, mode=module.RuntimeMode.SIM)
    now = datetime.now(timezone.utc)
    return module.build_acceptance_documents(
        runtime_input=runtime_input,
        live_telemetry=_live_context() if telemetry is None else telemetry,
        container_id=CONTAINER_ID,
        airframe_profile_hash=HASH_A,
        runtime_descriptor_hash=HASH_B,
        runtime_provenance_hash=HASH_C,
        now_utc=now,
        maximum_duration_s=maximum_duration_s,
        energy_projection=projection,
        planning_home=home,
        selection_provider=provider,
        receive_clock=receive_clock,
        reference_profile=reference_profile,
        original_prompt=original_prompt,
        require_original_prompt=require_original_prompt,
    )


def _prompt(module: ModuleType, text: str = "execute the original route"):
    return module.PromptEnvelope(
        prompt_id="prompt-original-001",
        text=text,
        language="en",
        issued_at="2026-01-01T00:00:00+00:00",
        operator_id="operator-fixture",
        aircraft_id="aircraft-fixture",
    )


def test_response_reference_reuses_compiler_and_unchanged_safety_policy():
    module = _module()
    smoke = _documents(module)
    response = _documents(module, reference_profile="fixed-wing-response")
    assert response.policy == smoke.policy
    assert [i.command for i in response.compiled.wire_items] == [
        i.command for i in smoke.compiled.wire_items
    ]
    assert response.compiled.wire_items != smoke.compiled.wire_items
    geometry = module.build_live_runtime_geometry(
        home_latitude_deg=47.0,
        home_longitude_deg=8.0,
        reference_profile="fixed-wing-response",
    ).to_mapping()
    assert geometry["mission_geometry"]["loiter_time_s"] == 30
    assert geometry["mission_geometry"]["loiter_radius_m"] == 50
    assert response.policy.maximum_mission_radius_m == 250
    assert response.policy.geofence_radius_m == 350


def test_response_reference_rejects_unknown_and_campaign_override():
    module = _module()
    with pytest.raises(module.VtolMissionAcceptanceError, match="unknown"):
        _documents(module, reference_profile="typo")
    with pytest.raises(module.VtolMissionAcceptanceError, match="override"):
        _documents(module, reference_profile="fixed-wing-response", provider=lambda _: None)


def _transaction(module: ModuleType):
    documents = _documents(module)
    now = datetime.now(timezone.utc)
    transaction = module.Px4MissionTransaction(
        transaction_id=documents.operator_grant.transaction_id,
        compiled=documents.compiled,
        validation_report=documents.validation_report,
        operator_grant=documents.operator_grant,
        bindings=documents.bindings,
        now_utc=now,
    )
    upload_plan = transaction.begin_upload(now_utc=now)
    return documents, transaction, upload_plan


def _upload_messages(upload_plan: Any, *, legacy_sequence: int | None = None) -> list[_Message]:
    result: list[_Message] = []
    for sequence in range(upload_plan.item_count):
        message_type = "MISSION_REQUEST" if legacy_sequence == sequence else "MISSION_REQUEST_INT"
        result.append(
            _Message(
                message_type,
                seq=sequence,
                mission_type=0,
                target_system=250,
                target_component=191,
            )
        )
    result.append(
        _Message(
            "MISSION_ACK",
            type=0,
            mission_type=0,
            target_system=250,
            target_component=191,
        )
    )
    return result


def _readback_messages(upload_plan: Any, *, change_sequence: int | None = None) -> list[_Message]:
    messages = [
        _Message(
            "MISSION_COUNT",
            count=upload_plan.item_count,
            mission_type=0,
            target_system=250,
            target_component=191,
        )
    ]
    for item in upload_plan.items:
        mapping = item.to_mapping()
        x = int(mapping["x"]) + (1 if item.sequence == change_sequence else 0)
        messages.append(
            _Message(
                "MISSION_ITEM_INT",
                seq=item.sequence,
                frame=item.frame,
                command=item.command,
                current=item.current,
                autocontinue=item.autocontinue,
                param1=item.param1,
                param2=item.param2,
                param3=item.param3,
                param4=item.param4,
                x=x,
                y=item.y,
                z=item.z,
                mission_type=item.mission_type,
                target_system=250,
                target_component=191,
            )
        )
    return messages


@pytest.mark.parametrize(
    "value,age,source,expected",
    [
        (1800, 1, (1, 1), 1800),
        (1800, 2, (1, 1), None),
        (1800, -1, (1, 1), None),
        (1800, 0, (2, 1), None),
        (0, 0, (1, 1), None),
        (True, 0, (1, 1), None),
    ],
)
def test_observed_endurance_requires_fresh_bound_battery(value, age, source, expected):
    module = _module()
    collector = module.TelemetryCollector(object())
    collector.last_battery_time_remaining_s = value
    collector.last_battery_observed_at_monotonic_s = 10 - age
    collector.observation_sources["BATTERY_STATUS"] = source
    assert module._qualified_observed_endurance(collector, 10) == expected


def test_external_documents_use_exact_shared_planner():
    module = _module()
    documents = _documents(module)
    assert documents.plan == module.plan_task(documents.task_spec, documents.context, "vtol")
    assert documents.context.estimated_flight_time_s == 1800
    assert documents.energy_evidence is None


@pytest.mark.parametrize("port,valid", [(38573, True), (14550, False), (0, False)])
def test_heartbeat_stream_port_comes_from_inspected_container(port, valid):
    module = _module()
    record = {
        "Config": {"Env": [f"PX4_MAVLINK_UDP_PORT={port}"]},
        "HostConfig": {"PortBindings": {f"{port}/udp": []}},
    }
    if valid:
        assert module.owned_mavlink_control_port(record) == port
    else:
        with pytest.raises(module.VtolMissionAcceptanceError):
            module.owned_mavlink_control_port(record)


@pytest.mark.parametrize("fault", [None, "armed", "airborne", "stale", "source"])
def test_uploaded_cursor_reset_requires_fresh_ground_state(monkeypatch, fault):
    from types import SimpleNamespace

    module = _module()
    sent = []
    connection = SimpleNamespace(
        mav=SimpleNamespace(mission_set_current_send=lambda *args: sent.append(args))
    )
    monkeypatch.setattr(module.time, "monotonic", lambda: 10)
    collector = module.TelemetryCollector(connection)
    collector.last_heartbeat = {"base_mode": 128 if fault == "armed" else 0}
    collector.last_landed_state = 2 if fault == "airborne" else 1
    collector.last_heartbeat_observed_at_monotonic_s = 7 if fault == "stale" else 10
    collector.last_extended_state_observed_at_monotonic_s = 10
    collector.observation_sources = {
        "HEARTBEAT": (2, 1) if fault == "source" else (1, 1),
        "EXTENDED_SYS_STATE": (1, 1),
    }
    monkeypatch.setattr(collector, "pump", lambda timeout: _Message("MISSION_CURRENT", seq=0))
    if fault:
        with pytest.raises(module.VtolMissionAcceptanceError):
            module.reset_uploaded_mission_current(collector)
        assert sent == []
    else:
        module.reset_uploaded_mission_current(collector)
        assert sent == [(1, 1, 0)]


@pytest.mark.parametrize("with_policy", [False, True])
def test_outer_campaign_command_omits_camera_barriers(tmp_path, with_policy):
    from types import SimpleNamespace

    from scripts import run_vtol_mission_sensor_capture as outer

    paths = SimpleNamespace(
        **{
            name: tmp_path / name
            for name in (
                "mission_evidence",
                "flight_ready",
                "flight_capture_complete",
                "mission_journal",
            )
        }
    )
    args = outer._process_arguments(
        paths=paths,
        scenario=MANIFEST,
        container_name="test",
        container_id=CONTAINER_ID,
        evaluator_port=19001,
        airframe_profile_sha256=HASH_A,
        runtime_descriptor_sha256=HASH_B,
        runtime_provenance_sha256=HASH_C,
        mission_timeout_s=600,
        docker_executable="docker",
        campaign_file=tmp_path / "input.json",
        **({"policy_artifact": tmp_path / "policy.json",
            "policy_artifact_file_sha256": HASH_C} if with_policy else {}),
    )
    assert args[1:3] == ["-m", "scripts.vtol_selected_task_campaign"]
    assert "--campaign-file" in args
    assert "--campaign-output-directory" in args
    assert "--flight-capture-ready-file" not in args
    assert "--flight-capture-complete-file" not in args
    assert "--mission-journal" not in args
    if with_policy:
        assert args[args.index("--policy-artifact") + 1] == str(tmp_path / "policy.json")
        assert args[args.index("--policy-artifact-file-sha256") + 1] == HASH_C
    else:
        assert "--policy-artifact" not in args


@pytest.mark.parametrize("tamper", [None, "identity", "order"])
@pytest.mark.parametrize("clock_binding", [None, "same-parent", "wrong-parent"])
@pytest.mark.parametrize("energy_covered", [False, True])
@pytest.mark.parametrize("policy_mode", ["legacy", "shared", "tampered", "unsupplied"])
def test_parent_campaign_recomputes_candidate_ranking(
    tmp_path, monkeypatch, tamper, clock_binding, energy_covered, policy_mode
):
    from multidetect.autonomy.task_ranker import TaskRanker
    from multidetect.clock import HighResolutionSystemClock
    from multidetect.mission_planning import compile_planned_task_mission_from_runtime
    from scripts import audit_mission_energy_window as energy_audit
    from scripts import run_vtol_mission_sensor_capture as outer

    module = _module()
    original = _documents(module)
    ranker = TaskRanker((-1, 0, 0), "untrained parent audit fixture")
    home = original.context.home_position
    geometry = module.build_live_runtime_geometry(
        home_latitude_deg=home.latitude_deg, home_longitude_deg=home.longitude_deg
    )
    runtime = {
        "schema_version": 1,
        **geometry.to_mapping(),
        "safety_policy": original.policy.to_mapping(),
    }
    tasks = [replace(original.task_spec, task_id=name) for name in ("low", "high")]
    campaign_file = tmp_path / "input.json"
    campaign_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ranker": ranker.to_mapping(),
                "tasks": [
                    {"task": task.to_mapping(), "priority": index, "runtime": runtime}
                    for index, task in enumerate(tasks)
                ],
            }
        ),
        encoding="utf-8",
    )
    directory = tmp_path / "campaign-missions"
    directory.mkdir()
    completed = []
    for number, task in enumerate(tasks, 1):
        mission_id = f"fixture-{number}"
        compilation = compile_planned_task_mission_from_runtime(
            task,
            original.context,
            "vtol",
            runtime=runtime,
            airframe_profile_hash=HASH_A,
            mission_id=mission_id,
        )
        mission_hash = compilation.executable_mission_hash
        path = directory / f"mission-{number:03d}.json"
        path.write_text(
            json.dumps(
                {
                    "gate_passed": True,
                    "container": {"id": CONTAINER_ID},
                    "documents": {
                        "context_snapshot": {"document": original.context.to_mapping()},
                        "executable_mission": {
                            "document": {"mission_id": mission_id},
                            "sha256": mission_hash,
                        },
                    },
                    "shared_outcome_shadow": {
                        "outcome": {"status": "completed", "mission_hash": mission_hash}
                    },
                    "transaction": {"id": "fixture-transaction"},
                }
            ),
            encoding="utf-8",
        )
        path.with_suffix(".transaction.jsonl").write_text("fixture", encoding="utf-8")
        completed.append(
            {
                "task_id": task.task_id,
                "evidence_path": str(path),
                "mission_hash": mission_hash,
                "ranker_sha256": ranker.canonical_hash,
            }
        )
    if tamper == "identity":
        completed[0]["ranker_sha256"] = HASH_B
    elif tamper == "order":
        completed[0]["task_id"] = "high"
    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps(
            {
                "status": "all_tasks_completed",
                "session_id": CONTAINER_ID,
                "completed": completed,
                "ranker_sha256": ranker.canonical_hash,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(outer, "validate_mission_journal", lambda *a, **k: {"fixture": True})
    kwargs = dict(
        campaign_file=campaign_file,
        expected_input_hash=outer._sha256_path(campaign_file),
        container_id=CONTAINER_ID,
        airframe_profile_hash=HASH_A,
        energy_configuration={
            "capacity_wh": 100,
            "avionics_power_w": 36,
            "propulsion_max_power_w": 324,
            "initial_remaining_wh": 100,
            "provenance": "fixture only",
        },
    )
    monkeypatch.setattr(
        energy_audit,
        "audit_window",
        lambda *args: {
            "receive_window_bracketed_by_valid_energy_prefix": energy_covered,
        },
    )
    if clock_binding is not None:
        identity = HighResolutionSystemClock(session_id="parent-fixture").identity()
        for path in directory.glob("mission-*.json"):
            report = json.loads(path.read_text(encoding="utf-8"))
            report["mission_receive_window"] = {
                "clock": "host_perf_counter_since_collector_start",
                "host_clock_identity": identity,
            }
            report["flight"] = {
                "host_clock_identity": identity,
                "post_mission_observation": {
                    "host_clock_identity": identity,
                    "energy_input_history": {
                        "receive_clock": "host_perf_counter_since_collector_start",
                        "host_clock_identity": identity,
                    },
                },
            }
            path.write_text(json.dumps(report), encoding="utf-8")
        kwargs["host_clock_identity"] = dict(identity)
        if clock_binding == "wrong-parent":
            kwargs["host_clock_identity"]["session_id"] = "other-parent"
    if policy_mode != "legacy":
        from multidetect.autonomy.policy_artifact import PolicyArtifact
        from multidetect.autonomy.policy_contract import SharedTaskPolicy
        from scripts.vtol_selected_task_campaign import load_campaign_definition

        policy = SharedTaskPolicy(ranker, PolicyArtifact.for_ranker(
            ranker, policy_id="audit-fixture", policy_version="0.3.0-pre.8",
            airframe_profile_hash=HASH_A, mission_contract_hash=HASH_B,
            safety_policy_hash=HASH_C,
        ))
        pending, _, _ = load_campaign_definition(campaign_file)
        saved = json.loads(summary.read_text())
        saved.update(shared_policy_used=True, planner_revalidated=True,
                     policy_artifact_sha256=policy.artifact_sha256)
        for number, entry in enumerate(saved["completed"], 1):
            decision, selected = policy.decide(pending, original.context, "vtol")
            policy_path = directory / f"mission-{number:03d}.policy.json"
            payload = {"decision": decision.to_mapping(),
                       "ranker_sha256": ranker.canonical_hash,
                       "mission_hash": entry["mission_hash"],
                       "policy_applied_to_actuator": False}
            if policy_mode == "tampered":
                payload["decision"]["observation_sha256"] = HASH_C
            policy_path.write_text(json.dumps(payload), encoding="utf-8")
            entry.update(policy_artifact_sha256=policy.artifact_sha256,
                         planner_revalidated=True, policy_evidence_path=str(policy_path))
            pending = [c for c in pending if c.task.task_id != selected.candidate.task.task_id]
        summary.write_text(json.dumps(saved), encoding="utf-8")
        if policy_mode != "unsupplied":
            kwargs["shared_policy"] = policy
    if (tamper or clock_binding == "wrong-parent" or not energy_covered
            or policy_mode in {"tampered", "unsupplied"}):
        with pytest.raises(outer.VtolMissionSensorCaptureError):
            outer.validate_campaign_result(summary, **kwargs)
    else:
        assert len(outer.validate_campaign_result(summary, **kwargs)["verified_sorties"]) == 2


def test_campaign_verification_rejects_changed_input_before_reports(tmp_path):
    from scripts import run_vtol_mission_sensor_capture as outer

    path = tmp_path / "input.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(outer.VtolMissionSensorCaptureError, match="input changed"):
        outer.validate_campaign_result(
            tmp_path / "missing.json",
            campaign_file=path,
            expected_input_hash=HASH_A,
            container_id=CONTAINER_ID,
            airframe_profile_hash=HASH_B,
        )


def test_campaign_prompt_loader_preserves_original_prompt_envelope(tmp_path):
    module = _module()
    from scripts import vtol_selected_task_campaign as campaign

    prompt = _prompt(module, "load this exact campaign instruction")
    path = tmp_path / "campaign.json"
    path.write_text(
        json.dumps({"schema_version": 1, "tasks": [], "prompt": prompt.to_mapping()}),
        encoding="utf-8",
    )
    loaded = campaign.load_campaign_prompt(path)
    assert loaded == prompt
    assert loaded.canonical_hash == prompt.canonical_hash


def test_campaign_cli_rejects_invalid_original_prompt_before_session(tmp_path, monkeypatch):
    _module()
    from scripts import vtol_selected_task_campaign as campaign

    campaign_file = tmp_path / "campaign.json"
    campaign_file.write_text(
        json.dumps({"schema_version": 1, "tasks": [], "prompt": {"schema_version": 1}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(campaign, "load_campaign_definition", lambda _path: ([], {}, None))
    monkeypatch.setattr(campaign, "_cli_host_clock", lambda *args: None)
    opened = []

    class Session:
        def __init__(self, **_kwargs):
            opened.append(True)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(campaign, "MissionTelemetrySession", Session)
    args = _energy_cli_args()
    args[1] = str(tmp_path / "result.json")
    args += [
        "--acknowledge-owned-disposable-sitl",
        "--campaign-file", str(campaign_file),
        "--campaign-output-directory", str(tmp_path / "sorties"),
        "--energy-capacity-wh", "100", "--energy-avionics-power-w", "10",
        "--energy-propulsion-max-power-w", "100",
        "--energy-initial-remaining-wh", "90", "--energy-provenance", "fixture",
    ]
    with pytest.raises(SystemExit) as exc:
        campaign.main(args)
    assert exc.value.code == 2
    assert opened == []


@pytest.mark.parametrize("fault", [None, "empty", "duplicate", "schema", "extra", "priority"])
def test_campaign_file_loads_explicit_tasks_and_runtime(tmp_path, fault):
    from scripts.vtol_selected_task_campaign import load_campaign

    module = _module()
    original = _documents(module)
    home = original.context.home_position
    geometry = module.build_live_runtime_geometry(
        home_latitude_deg=home.latitude_deg,
        home_longitude_deg=home.longitude_deg,
    )
    entry = {
        "task": original.task_spec.to_mapping(),
        "priority": 5,
        "runtime": {
            "schema_version": 1,
            **geometry.to_mapping(),
            "safety_policy": original.policy.to_mapping(),
        },
    }
    raw = {"schema_version": 1, "tasks": [entry]}
    if fault == "empty":
        raw["tasks"] = []
    elif fault == "duplicate":
        raw["tasks"] = [entry, entry]
    elif fault == "schema":
        raw["schema_version"] = True
    elif fault == "extra":
        raw["truth"] = {}
    elif fault == "priority":
        entry["priority"] = -1
    path = tmp_path / "campaign.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    if fault:
        with pytest.raises(ValueError):
            load_campaign(path)
    else:
        tasks, runtimes = load_campaign(path)
        assert tasks[0].task == original.task_spec
        assert tasks[0].priority == 5
        assert runtimes[original.task_spec.task_id] == entry["runtime"]


@pytest.mark.parametrize("vehicle_class", ["vtol", "fixed-wing", "multirotor"])
def test_campaign_definition_reuses_vehicle_neutral_runtime_parser(tmp_path, vehicle_class):
    from scripts.run_px4_airframe_transaction_acceptance import _compile_fixture
    from scripts.vtol_selected_task_campaign import load_campaign_definition

    source = ROOT / "configs" / "scenarios" / "vtol-ranked-routes.synthetic-campaign.json"
    task_mapping = json.loads(source.read_text(encoding="utf-8"))["tasks"][0]["task"]
    _, _, geometry, policy = _compile_fixture(vehicle_class)
    runtime = {
        "schema_version": 1,
        **geometry.to_mapping(),
        "safety_policy": policy.to_mapping(),
    }
    path = tmp_path / f"{vehicle_class}.campaign.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tasks": [{"task": task_mapping, "priority": 1, "runtime": runtime}],
            }
        ),
        encoding="utf-8",
    )

    candidates, runtimes, ranker = load_campaign_definition(
        path, vehicle_class=vehicle_class
    )
    assert len(candidates) == 1
    assert ranker is None
    assert runtimes[candidates[0].task.task_id] == runtime


@pytest.mark.parametrize(
    "failure", [None, "outcome", "hash", "gate", "exception", "battery", "endurance"]
)
@pytest.mark.parametrize("use_ranker", [False, True, "shared", "mismatch"])
def test_campaign_selects_again_only_after_bound_completion(
    tmp_path, monkeypatch, failure, use_ranker
):
    from multidetect.autonomy.task_ranker import TaskRanker
    from multidetect.autonomy.task_selection import TaskCandidate
    from scripts import vtol_selected_task_campaign as campaign

    module = _module()
    original = _documents(module)
    session = _telemetry_session(module, monkeypatch)
    home = original.context.home_position
    geometry = module.build_live_runtime_geometry(
        home_latitude_deg=home.latitude_deg,
        home_longitude_deg=home.longitude_deg,
    )
    runtime = {
        "schema_version": 1,
        **geometry.to_mapping(),
        "safety_policy": original.policy.to_mapping(),
    }
    candidates = [
        TaskCandidate(replace(original.task_spec, task_id="low"), 1),
        TaskCandidate(replace(original.task_spec, task_id="high"), 10),
    ]
    calls = []

    def execute(**kwargs):
        assert kwargs["telemetry_session"] is session
        context = replace(
            original.context,
            snapshot_id=f"observed-{len(calls)}",
            battery_remaining_pct=98 - len(calls),
            captured_at_s=10 + len(calls),
        )
        if calls and failure == "battery":
            context = replace(context, battery_remaining_pct=4)
        if calls and failure == "endurance":
            context = replace(context, estimated_flight_time_s=1)
        selected = kwargs["selection_provider"](context)
        calls.append((selected.task.task_id, kwargs["evidence_path"], context))
        if failure == "exception":
            raise RuntimeError("mission failed")
        mission_hash = selected.compilation.executable_mission_hash
        return {
            "gate_passed": failure != "gate",
            "shared_outcome_shadow": {
                "outcome": {
                    "status": "running" if failure == "outcome" else "completed",
                    "mission_hash": HASH_C if failure == "hash" else mission_hash,
                }
            },
            "documents": {"executable_mission": {"sha256": mission_hash}},
        }

    monkeypatch.setattr(campaign, "run_mission_acceptance", execute)
    kwargs = dict(
        session=session,
        ranker=TaskRanker((-1, 0, 0), "untrained fixture") if use_ranker else None,
        candidates=candidates,
        runtimes={"low": runtime, "high": runtime},
        output_directory=tmp_path / "campaign",
        mission_options={
            "expected_container_id": CONTAINER_ID,
            "acknowledge_owned_disposable_sitl": True,
            "airframe_profile_sha256": HASH_A,
        },
    )
    if use_ranker in {"shared", "mismatch"}:
        from multidetect.autonomy.policy_artifact import PolicyArtifact
        from multidetect.autonomy.policy_contract import SharedTaskPolicy

        ranker = kwargs.pop("ranker")
        kwargs["shared_policy"] = SharedTaskPolicy(
            ranker,
            PolicyArtifact.for_ranker(
                ranker,
                policy_id="campaign-fixture",
                policy_version="0.3.0-pre.8",
                airframe_profile_hash=HASH_B if use_ranker == "mismatch" else HASH_A,
                mission_contract_hash=HASH_B,
                safety_policy_hash=HASH_C,
            ),
        )
        if use_ranker == "mismatch":
            with pytest.raises(campaign.VtolMissionAcceptanceError, match="airframe"):
                campaign.run_selected_task_campaign(**kwargs)
            assert not calls
            assert not kwargs["output_directory"].exists()
            return
    if failure:
        with pytest.raises((campaign.VtolMissionAcceptanceError, RuntimeError)) as caught:
            campaign.run_selected_task_campaign(**kwargs)
        assert len(calls) == 1
        if failure in {"battery", "endurance"}:
            assert isinstance(caught.value, campaign.TaskSelectionRejected)
            evidence = caught.value.evidence
            rejected_context = module.ContextSnapshot.from_mapping(evidence["context"])
            assert rejected_context.snapshot_id == "observed-1"
            assert rejected_context.canonical_hash == evidence["context_sha256"]
            assert rejected_context.canonical_hash != calls[0][2].canonical_hash
            assert [item["task_id"] for item in evidence["rejected"]] == (
                ["high"] if use_ranker else ["low"]
            )
            assert all(item["reason"] for item in evidence["rejected"])
    else:
        result = campaign.run_selected_task_campaign(**kwargs)
        assert result["status"] == "all_tasks_completed"
        assert [call[0] for call in calls] == (["low", "high"] if use_ranker else ["high", "low"])
        expected_ranker_hash = (
            kwargs["shared_policy"].ranker.canonical_hash if use_ranker == "shared"
            else kwargs["ranker"].canonical_hash if use_ranker else None
        )
        assert result["shared_policy_used"] is (use_ranker == "shared")
        assert result["planner_revalidated"] is True
        if use_ranker == "shared":
            assert result["policy_artifact_sha256"] == kwargs["shared_policy"].artifact_sha256
            for item in result["completed"]:
                evidence = json.loads(Path(item["policy_evidence_path"]).read_text())
                assert evidence["decision"]["planner_revalidated"] is True
                assert evidence["mission_hash"] == item["mission_hash"]
                assert evidence["ranker_sha256"] == expected_ranker_hash
                assert evidence["policy_artifact_sha256"] == kwargs["shared_policy"].artifact_sha256
                assert evidence["planner_revalidated"] is True
                assert evidence["shared_policy_used"] is True
                assert item["shared_policy_used"] is True
        assert result["ranker_sha256"] == expected_ranker_hash
        assert all(item["ranker_sha256"] == expected_ranker_hash for item in result["completed"])
        assert calls[0][1] != calls[1][1]
        assert calls[0][2].canonical_hash != calls[1][2].canonical_hash
        assert not session.closed


def test_campaign_passes_capture_barriers_only_to_first_sortie(tmp_path, monkeypatch):
    """The outer capture handshake belongs to the first campaign sortie only."""

    from multidetect.autonomy.task_selection import TaskCandidate
    from scripts import vtol_selected_task_campaign as campaign

    module = _module()
    original = _documents(module)
    session = _telemetry_session(module, monkeypatch)
    home = original.context.home_position
    geometry = module.build_live_runtime_geometry(
        home_latitude_deg=home.latitude_deg,
        home_longitude_deg=home.longitude_deg,
    )
    runtime = {
        "schema_version": 1,
        **geometry.to_mapping(),
        "safety_policy": original.policy.to_mapping(),
    }
    candidates = [
        TaskCandidate(replace(original.task_spec, task_id="first"), 2),
        TaskCandidate(replace(original.task_spec, task_id="second"), 1),
    ]
    calls: list[dict[str, Any]] = []

    def execute(**kwargs):
        selected = kwargs["selection_provider"](
            replace(original.context, snapshot_id=f"barrier-{len(calls)}")
        )
        calls.append(
            {
                "ready": kwargs["flight_capture_ready_file"],
                "complete": kwargs["flight_capture_complete_file"],
                "deadline": kwargs["flight_capture_deadline_s"],
                "task": selected.task.task_id,
            }
        )
        mission_hash = selected.compilation.executable_mission_hash
        return {
            "gate_passed": True,
            "shared_outcome_shadow": {
                "outcome": {"status": "completed", "mission_hash": mission_hash}
            },
            "documents": {"executable_mission": {"sha256": mission_hash}},
        }

    monkeypatch.setattr(campaign, "run_mission_acceptance", execute)
    ready = tmp_path / "flight-ready.json"
    complete = tmp_path / "flight-complete.json"
    result = campaign.run_selected_task_campaign(
        session=session,
        candidates=candidates,
        runtimes={"first": runtime, "second": runtime},
        output_directory=tmp_path / "campaign",
        mission_options={
            "expected_container_id": CONTAINER_ID,
            "acknowledge_owned_disposable_sitl": True,
            "airframe_profile_sha256": HASH_A,
        },
        flight_capture_ready_file=ready,
        flight_capture_complete_file=complete,
        flight_capture_deadline_s=30.0,
    )

    assert result["status"] == "all_tasks_completed"
    assert [item["task"] for item in calls] == ["first", "second"]
    assert calls[0]["ready"] == ready
    assert calls[0]["complete"] == complete
    assert calls[0]["deadline"] == 30.0
    assert calls[1]["ready"] is None
    assert calls[1]["complete"] is None
    assert calls[1]["deadline"] is None


@pytest.mark.parametrize(
    "tamper",
    [None, "prompt_document", "prompt_hash", "transaction", "grant_hash", "grant_prompt_hash"],
)
def test_campaign_reuses_original_prompt_and_rejects_report_binding_tamper(
    tmp_path, monkeypatch, tamper
):
    from multidetect.autonomy.task_selection import TaskCandidate
    _module()
    from scripts import vtol_selected_task_campaign as campaign

    module = _module()
    original = _documents(module)
    prompt = _prompt(module, "the one prompt shared by every sortie")
    session = _telemetry_session(module, monkeypatch)
    geometry = module.build_live_runtime_geometry(
        home_latitude_deg=original.context.home_position.latitude_deg,
        home_longitude_deg=original.context.home_position.longitude_deg,
    )
    runtime = {
        "schema_version": 1,
        **geometry.to_mapping(),
        "safety_policy": original.policy.to_mapping(),
    }
    candidates = [
        TaskCandidate(replace(original.task_spec, task_id="first"), 2),
        TaskCandidate(replace(original.task_spec, task_id="second"), 1),
    ]
    observed = []

    def execute(**kwargs):
        context = replace(
            original.context,
            snapshot_id=f"prompt-sortie-{len(observed)}",
            captured_at_s=12.5 + len(observed),
        )
        selected = kwargs["selection_provider"](context)
        assert kwargs["original_prompt"] == prompt
        assert selected.prompt == prompt
        documents = module.build_selected_acceptance_documents(
            selection=selected.compilation,
            task_spec=selected.task,
            context=context,
            geometry=selected.geometry,
            policy=selected.policy,
            runtime_input=module.load_runtime_scenario(MANIFEST, mode=module.RuntimeMode.SIM),
            container_id=CONTAINER_ID,
            airframe_profile_hash=HASH_A,
            runtime_descriptor_hash=HASH_B,
            runtime_provenance_hash=HASH_C,
            now_utc=datetime.now(timezone.utc),
            now_receive_s=context.captured_at_s,
            prompt=selected.prompt,
            require_original_prompt=True,
        )
        evidence = module._document_evidence(documents)
        report = {
            "gate_passed": True,
            "documents": evidence,
            "transaction": {
                "id": documents.operator_grant.transaction_id,
                "operator_grant_sha256": documents.operator_grant.canonical_hash,
            },
            "shared_outcome_shadow": {
                "outcome": {
                    "status": "completed",
                    "mission_hash": selected.compilation.executable_mission_hash,
                }
            },
        }
        if tamper and not observed:
            if tamper == "prompt_document":
                report["documents"]["prompt"]["document"]["text"] = "tampered"
            elif tamper == "prompt_hash":
                report["documents"]["prompt"]["sha256"] = HASH_A
            elif tamper == "transaction":
                report["transaction"]["id"] = "transaction-tampered"
            elif tamper == "grant_hash":
                report["documents"]["operator_grant"]["sha256"] = HASH_A
                report["transaction"]["operator_grant_sha256"] = HASH_A
            elif tamper == "grant_prompt_hash":
                report["documents"]["operator_grant"]["document"]["prompt_hash"] = HASH_A
                grant_hash = module.canonical_sha256(
                    report["documents"]["operator_grant"]["document"]
                )
                report["documents"]["operator_grant"]["sha256"] = grant_hash
                report["transaction"]["operator_grant_sha256"] = grant_hash
        observed.append((selected, report))
        return report

    monkeypatch.setattr(campaign, "run_mission_acceptance", execute)
    kwargs = dict(
        session=session,
        candidates=candidates,
        runtimes={"first": runtime, "second": runtime},
        output_directory=tmp_path / "campaign",
        mission_options={
            "expected_container_id": CONTAINER_ID,
            "acknowledge_owned_disposable_sitl": True,
            "airframe_profile_sha256": HASH_A,
        },
        prompt=prompt,
    )
    if tamper:
        with pytest.raises(
            campaign.VtolMissionAcceptanceError, match="(?i)prompt|transaction|grant"
        ):
            campaign.run_selected_task_campaign(**kwargs)
        assert len(observed) == 1
    else:
        result = campaign.run_selected_task_campaign(**kwargs)
        assert len(observed) == 2
        assert result["prompt"] == prompt.to_mapping()
        assert result["prompt_sha256"] == prompt.canonical_hash
        assert result["prompt_provenance_bound"] is True
        assert all(item[0].prompt == prompt for item in observed)
        assert all(
            item[1]["documents"]["prompt"]["sha256"] == prompt.canonical_hash
            for item in observed
        )
        assert all(
            item[1]["documents"]["bindings"]["prompt_hash"] == prompt.canonical_hash
            for item in observed
        )
        assert all(
            item[1]["documents"]["operator_grant"]["document"]["prompt_hash"]
            == prompt.canonical_hash
            for item in observed
        )


def test_prompt_shared_policy_campaign_executes_both_sorties_through_onboard_executor(
    tmp_path, monkeypatch
):
    """A Prompt must drive both sorties through the real shared OnboardAI path.

    The outer PX4 lifecycle is stubbed at its existing acceptance seam, but
    selection, PromptTaskResolver, OnboardAIExecutor, planner and Mission
    compiler all run unmodified.  This catches drift between the mission hash
    prepared by OnboardAI and the hash subsequently bound by the campaign,
    as well as a resolver that accidentally retains completed tasks on sortie
    two.
    """

    from multidetect.autonomy.policy_artifact import PolicyArtifact
    from multidetect.autonomy.policy_contract import SharedTaskPolicy
    from multidetect.autonomy.task_ranker import TaskRanker
    from multidetect.autonomy.task_selection import TaskCandidate
    from scripts import vtol_selected_task_campaign as campaign

    module = _module()
    original = _documents(module)
    # Prompt aircraft identity is intentionally the live airframe profile ID;
    # a different identity is covered by the fail-closed test below.
    prompt = replace(
        _prompt(module, "execute the approved VTOL route"),
        prompt_id="prompt-vtol-shared-policy-east-west-v1",
        aircraft_id=original.context.airframe_profile_id,
    )
    session = _telemetry_session(module, monkeypatch)
    geometry = module.build_live_runtime_geometry(
        home_latitude_deg=original.context.home_position.latitude_deg,
        home_longitude_deg=original.context.home_position.longitude_deg,
    )
    runtime = {
        "schema_version": 1,
        **geometry.to_mapping(),
        "safety_policy": original.policy.to_mapping(),
    }
    candidates = [
        TaskCandidate(replace(original.task_spec, task_id="prompt-first"), 2),
        TaskCandidate(replace(original.task_spec, task_id="prompt-second"), 1),
    ]
    ranker = TaskRanker((-1, 0, 0), "prompt campaign integration fixture")
    policy = SharedTaskPolicy(
        ranker,
        PolicyArtifact.for_ranker(
            ranker,
            policy_id="prompt-campaign-integration",
            policy_version="0.3.0-pre.8",
            airframe_profile_hash=HASH_A,
            mission_contract_hash=HASH_B,
            safety_policy_hash=HASH_C,
        ),
    )
    contexts = []

    def execute(**kwargs):
        context = replace(
            original.context,
            snapshot_id=f"prompt-onboard-{len(contexts)}",
            captured_at_s=20.0 + len(contexts),
        )
        selected = kwargs["selection_provider"](context)
        contexts.append(context)
        documents = module.build_selected_acceptance_documents(
            selection=selected.compilation,
            task_spec=selected.task,
            context=context,
            geometry=selected.geometry,
            policy=selected.policy,
            runtime_input=module.load_runtime_scenario(MANIFEST, mode=module.RuntimeMode.SIM),
            container_id=CONTAINER_ID,
            airframe_profile_hash=HASH_A,
            runtime_descriptor_hash=HASH_B,
            runtime_provenance_hash=HASH_C,
            now_utc=datetime.now(timezone.utc),
            now_receive_s=context.captured_at_s,
            prompt=selected.prompt,
            require_original_prompt=True,
        )
        return {
            "gate_passed": True,
            "documents": module._document_evidence(documents),
            "transaction": {
                "id": documents.operator_grant.transaction_id,
                "operator_grant_sha256": documents.operator_grant.canonical_hash,
            },
            "shared_outcome_shadow": {
                "outcome": {
                    "status": "completed",
                    "mission_hash": selected.compilation.executable_mission_hash,
                }
            },
        }

    monkeypatch.setattr(campaign, "run_mission_acceptance", execute)
    result = campaign.run_selected_task_campaign(
        session=session,
        candidates=candidates,
        runtimes={"prompt-first": runtime, "prompt-second": runtime},
        output_directory=tmp_path / "prompt-onboard-campaign",
        mission_options={
            "expected_container_id": CONTAINER_ID,
            "acknowledge_owned_disposable_sitl": True,
            "airframe_profile_sha256": HASH_A,
        },
        shared_policy=policy,
        prompt=prompt,
    )

    assert result["status"] == "all_tasks_completed"
    assert result["onboard_ai_used"] is True
    assert result["onboard_ai_prompt_resolution_bound"] is True
    assert result["prompt_sha256"] == prompt.canonical_hash
    assert result["prompt_provenance_bound"] is True
    assert {item["task_id"] for item in result["completed"]} == {
        "prompt-first",
        "prompt-second",
    }
    assert len(contexts) == 2
    for item, context in zip(result["completed"], contexts, strict=True):
        onboard = item["onboard_decision"]
        assert onboard["response"] == "execute"
        assert onboard["prompt_sha256"] == prompt.canonical_hash
        assert onboard["context_sha256"] == context.canonical_hash
        assert onboard["selected_task_id"] == item["task_id"]
        assert onboard["planner_revalidated"] is True
        assert onboard["mission_bound"] is True
        assert onboard["policy_applied_to_actuator"] is False
        binding = onboard["policy_runtime_binding"]
        assert binding["plan_sha256"] == item["policy_runtime_binding"]["plan_sha256"]
        assert binding["executable_mission_sha256"] == item["mission_hash"]
        assert binding["wire_hash"] == item["policy_runtime_binding"]["wire_hash"]


def test_prompt_shared_policy_airframe_identity_mismatch_fails_closed_before_mission(
    tmp_path, monkeypatch
):
    """A Prompt for another airframe cannot enter the Mission lifecycle."""

    from multidetect.autonomy.policy_artifact import PolicyArtifact
    from multidetect.autonomy.policy_contract import SharedTaskPolicy
    from multidetect.autonomy.task_ranker import TaskRanker
    from multidetect.autonomy.task_selection import TaskCandidate
    from scripts import vtol_selected_task_campaign as campaign

    module = _module()
    original = _documents(module)
    prompt = replace(
        _prompt(module, "execute on a different aircraft"),
        prompt_id="prompt-vtol-shared-policy-east-west-v1",
    )
    session = _telemetry_session(module, monkeypatch)
    geometry = module.build_live_runtime_geometry(
        home_latitude_deg=original.context.home_position.latitude_deg,
        home_longitude_deg=original.context.home_position.longitude_deg,
    )
    runtime = {
        "schema_version": 1,
        **geometry.to_mapping(),
        "safety_policy": original.policy.to_mapping(),
    }
    candidate = TaskCandidate(replace(original.task_spec, task_id="identity-gated"), 1)
    ranker = TaskRanker((-1, 0, 0), "prompt identity mismatch fixture")
    policy = SharedTaskPolicy(
        ranker,
        PolicyArtifact.for_ranker(
            ranker,
            policy_id="prompt-identity-mismatch",
            policy_version="0.3.0-pre.8",
            airframe_profile_hash=HASH_A,
            mission_contract_hash=HASH_B,
            safety_policy_hash=HASH_C,
        ),
    )
    called = []
    def execute(**kwargs):
        called.append(kwargs)
        kwargs["selection_provider"](original.context)
        pytest.fail("airframe-mismatched Prompt reached Mission lifecycle")

    monkeypatch.setattr(campaign, "run_mission_acceptance", execute)

    with pytest.raises(campaign.VtolMissionAcceptanceError, match="Prompt aircraft"):
        campaign.run_selected_task_campaign(
            session=session,
            candidates=[candidate],
            runtimes={"identity-gated": runtime},
            output_directory=tmp_path / "identity-mismatch-campaign",
            mission_options={
                "expected_container_id": CONTAINER_ID,
                "acknowledge_owned_disposable_sitl": True,
                "airframe_profile_sha256": HASH_A,
            },
            shared_policy=policy,
            prompt=prompt,
            require_registered_prompt=True,
        )

    assert len(called) == 1
    assert session.missions_started == 0


def test_campaign_rejects_unpaired_capture_barrier_before_mission_start(tmp_path, monkeypatch):
    from multidetect.autonomy.task_selection import TaskCandidate
    from scripts import vtol_selected_task_campaign as campaign

    module = _module()
    original = _documents(module)
    session = _telemetry_session(module, monkeypatch)
    home = original.context.home_position
    geometry = module.build_live_runtime_geometry(
        home_latitude_deg=home.latitude_deg,
        home_longitude_deg=home.longitude_deg,
    )
    runtime = {
        "schema_version": 1,
        **geometry.to_mapping(),
        "safety_policy": original.policy.to_mapping(),
    }
    candidate = TaskCandidate(replace(original.task_spec, task_id="only"), 1)
    monkeypatch.setattr(
        campaign,
        "run_mission_acceptance",
        lambda **_kwargs: pytest.fail("unpaired barrier reached Mission lifecycle"),
    )

    with pytest.raises(campaign.VtolMissionAcceptanceError, match="provided together"):
        campaign.run_selected_task_campaign(
            session=session,
            candidates=[candidate],
            runtimes={"only": runtime},
            output_directory=tmp_path / "campaign",
            mission_options={
                "expected_container_id": CONTAINER_ID,
                "acknowledge_owned_disposable_sitl": True,
                "airframe_profile_sha256": HASH_A,
            },
            flight_capture_ready_file=tmp_path / "only-ready.json",
        )

    assert not (tmp_path / "campaign").exists()
    assert session.missions_started == 0


def test_shared_policy_abstain_on_unqualified_gps_never_enters_mission_transaction(
    tmp_path, monkeypatch
):
    from multidetect.autonomy.policy_artifact import PolicyArtifact
    from multidetect.autonomy.policy_contract import SharedTaskPolicy
    from multidetect.autonomy.task_ranker import TaskRanker
    from multidetect.autonomy.task_selection import TaskCandidate
    from scripts import vtol_selected_task_campaign as campaign

    module = _module()
    original = _documents(module)
    session = _telemetry_session(module, monkeypatch)
    home = original.context.home_position
    geometry = module.build_live_runtime_geometry(
        home_latitude_deg=home.latitude_deg,
        home_longitude_deg=home.longitude_deg,
    )
    runtime = {
        "schema_version": 1,
        **geometry.to_mapping(),
        "safety_policy": original.policy.to_mapping(),
    }
    candidates = [
        TaskCandidate(replace(original.task_spec, task_id="gps-gated"), 10),
    ]
    ranker = TaskRanker((-1, 0, 0), "unqualified GPS fail-closed fixture")
    policy = SharedTaskPolicy(
        ranker,
        PolicyArtifact.for_ranker(
            ranker,
            policy_id="gps-gated-fixture",
            policy_version="0.3.0-pre.8",
            airframe_profile_hash=HASH_A,
            mission_contract_hash=HASH_B,
            safety_policy_hash=HASH_C,
        ),
    )
    callback_calls = []

    def execute(**kwargs):
        callback_calls.append(kwargs)
        unqualified = replace(
            original.context,
            snapshot_id="gps-unqualified",
            gps_fix_type=1,
            captured_at_s=20.0,
        )
        # The real acceptance wrapper would only acquire/upload after this
        # callback returns.  A rejection here must therefore be observable
        # without fabricating a Mission transaction or policy evidence file.
        kwargs["selection_provider"](unqualified)
        pytest.fail("unqualified GPS unexpectedly selected a mission")

    monkeypatch.setattr(campaign, "run_mission_acceptance", execute)
    output = tmp_path / "gps-gated-campaign"
    with pytest.raises(campaign.TaskSelectionRejected) as caught:
        campaign.run_selected_task_campaign(
            session=session,
            candidates=candidates,
            runtimes={"gps-gated": runtime},
            output_directory=output,
            mission_options={
                "expected_container_id": CONTAINER_ID,
                "acknowledge_owned_disposable_sitl": True,
                "airframe_profile_sha256": HASH_A,
            },
            shared_policy=policy,
        )

    assert len(callback_calls) == 1
    assert caught.value.evidence["context"]["gps_fix_type"] == 1
    assert caught.value.evidence["rejected"] == [
        {"task_id": "gps-gated", "reason": "no_feasible_task"}
    ]
    assert output.exists()
    assert list(output.iterdir()) == []
    assert session.missions_started == 0
    assert not session.closed


def test_campaign_binds_unique_sensor_frame_to_each_mission_evidence(tmp_path, monkeypatch):
    from multidetect.autonomy.serialization import canonical_sha256
    from multidetect.autonomy.task_selection import TaskCandidate
    from multidetect.sensor_transport_binding import load_sensor_transport_manifest
    from scripts import vtol_selected_task_campaign as campaign

    module = _module()
    original = _documents(module)
    session = _telemetry_session(module, monkeypatch)
    home = original.context.home_position
    geometry = module.build_live_runtime_geometry(
        home_latitude_deg=home.latitude_deg,
        home_longitude_deg=home.longitude_deg,
    )
    runtime = {
        "schema_version": 1,
        **geometry.to_mapping(),
        "safety_policy": original.policy.to_mapping(),
    }
    candidates = [
        TaskCandidate(replace(original.task_spec, task_id="west"), 2),
        TaskCandidate(replace(original.task_spec, task_id="east"), 1),
    ]
    sensor_root = tmp_path / "sensor"
    sensor_root.mkdir()
    frame_records = []
    for sequence, (frame_id, captured_at_s, payload) in enumerate(
        (("sensor-west", 1.0, b"west"), ("sensor-east", 2.0, b"east"))
    ):
        file_name = f"{frame_id}.bgr24"
        (sensor_root / file_name).write_bytes(payload)
        frame_hash = hashlib.sha256(payload).hexdigest()
        frame_records.append(
            {
                "frame_id": frame_id,
                "sequence": sequence,
                "file": file_name,
                "bytes": len(payload),
                "sha256": frame_hash,
                "wire_payload_sha256": frame_hash,
                "raw_bytes_sha256_matches_wire_payload": True,
                "provenance": {
                    "captured_at_s": captured_at_s,
                    "captured_at_clock": "host_monotonic_since_collector_start",
                    "frame_telemetry": {},
                    "wire_telemetry": {},
                },
            }
        )
    manifest_document = {
        "schema": "multidetect.gazebo-bgr24-frame-set",
        "schema_version": 2,
        "wire_version": 3,
        "pixel_format": "BGR24",
        "scenario_id": "river_rescue_vtol_001",
        "sensor_binding_hash": HASH_C,
        "frame_count": 2,
        "frames": frame_records,
        "per_frame_sensor_evidence": {
            "record_count": 2,
            "derived_ned_values_recomputed_before_seal": True,
            "gps_and_pose_values_covered_by_wire_record_sha256": True,
        },
        "source_attribution": {"truth_loaded_by_runtime": False},
        "claims": {"per_frame_sensor_geolocation_input": "passed"},
    }
    manifest_document["manifest_sha256"] = canonical_sha256(manifest_document)
    manifest_path = sensor_root / "sensor-manifest.json"
    manifest_path.write_text(json.dumps(manifest_document), encoding="utf-8")
    manifest = load_sensor_transport_manifest(manifest_path)
    calls = []

    def execute(**kwargs):
        selected = kwargs["selection_provider"](
            replace(original.context, snapshot_id=f"observed-{len(calls)}")
        )
        provenance = kwargs["sensor_transport_binding"]
        assert provenance["task_id"] == selected.task.task_id
        assert provenance["frame_id"] == {"west": "sensor-west", "east": "sensor-east"}[
            selected.task.task_id
        ]
        calls.append((selected.task.task_id, provenance))
        mission_hash = selected.compilation.executable_mission_hash
        return {
            "gate_passed": True,
            "sensor_transport": {
                **provenance,
                "mission_receive_window": {
                    "clock": "host_monotonic_since_collector_start",
                    "arm_request_host_monotonic_s": 0.0,
                    "completed_host_monotonic_s": 3.0,
                },
            },
            "shared_outcome_shadow": {
                "outcome": {"status": "completed", "mission_hash": mission_hash}
            },
            "documents": {"executable_mission": {"sha256": mission_hash}},
        }

    monkeypatch.setattr(campaign, "run_mission_acceptance", execute)
    result = campaign.run_selected_task_campaign(
        session=session,
        candidates=candidates,
        runtimes={"west": runtime, "east": runtime},
        output_directory=tmp_path / "campaign",
        mission_options={
            "expected_container_id": CONTAINER_ID,
            "acknowledge_owned_disposable_sitl": True,
            "airframe_profile_sha256": HASH_A,
        },
        sensor_transport_manifest=manifest,
        sensor_frame_ids={"west": "sensor-west", "east": "sensor-east"},
    )
    assert result["sensor_transport_used"] is True
    assert result["sensor_transport"]["manifest_sha256"] == manifest.manifest_sha256
    assert [task_id for task_id, _ in calls] == ["west", "east"]
    assert [item["sensor_transport"]["frame_id"] for item in result["completed"]] == [
        "sensor-west",
        "sensor-east",
    ]


def test_campaign_rejects_sensor_frame_map_reuse_or_missing_task(tmp_path):
    from types import SimpleNamespace

    from multidetect.autonomy.serialization import canonical_sha256
    from multidetect.sensor_transport_binding import load_sensor_transport_manifest
    from scripts import vtol_selected_task_campaign as campaign

    sensor_root = tmp_path / "sensor"
    sensor_root.mkdir()
    payload = b"frame"
    (sensor_root / "frame.bin").write_bytes(payload)
    frame_hash = hashlib.sha256(payload).hexdigest()
    manifest_document = {
        "schema": "multidetect.gazebo-bgr24-frame-set",
        "schema_version": 2,
        "wire_version": 3,
        "pixel_format": "BGR24",
        "frame_count": 1,
        "frames": [
            {
                "frame_id": "frame-0",
                "sequence": 0,
                "file": "frame.bin",
                "bytes": len(payload),
                "sha256": frame_hash,
                "wire_payload_sha256": frame_hash,
                "raw_bytes_sha256_matches_wire_payload": True,
                "provenance": {
                    "captured_at_s": 0.0,
                    "captured_at_clock": "host_monotonic_since_collector_start",
                    "frame_telemetry": {},
                    "wire_telemetry": {},
                },
            }
        ],
        "per_frame_sensor_evidence": {
            "record_count": 1,
            "derived_ned_values_recomputed_before_seal": True,
            "gps_and_pose_values_covered_by_wire_record_sha256": True,
        },
        "source_attribution": {"truth_loaded_by_runtime": False},
        "claims": {"per_frame_sensor_geolocation_input": "passed"},
    }
    manifest_document["manifest_sha256"] = canonical_sha256(manifest_document)
    manifest_path = sensor_root / "sensor-manifest.json"
    manifest_path.write_text(json.dumps(manifest_document), encoding="utf-8")
    manifest = load_sensor_transport_manifest(manifest_path)
    only = SimpleNamespace(task=SimpleNamespace(task_id="only"))
    other = SimpleNamespace(task=SimpleNamespace(task_id="other"))
    with pytest.raises(ValueError, match="exactly match"):
        campaign._validate_sensor_frame_map(manifest, [only], {"other": "frame-0"})
    with pytest.raises(ValueError, match="unique frame"):
        campaign._validate_sensor_frame_map(
            manifest,
            [only, other],
            {"only": "frame-0", "other": "frame-0"},
        )


def test_sensor_transport_binding_requires_same_clock_and_mission_window():
    module = _module()
    binding = {
        "manifest_file_sha256": HASH_A,
        "manifest_sha256": HASH_B,
        "schema_version": 2,
        "wire_version": 3,
        "pixel_format": "BGR24",
        "frame_id": "sensor-0",
        "frame_sequence": 0,
        "frame_sha256": HASH_C,
        "frame_bytes": 4,
        "frame_captured_at_s": 2.0,
        "frame_captured_at_clock": "host_monotonic_since_collector_start",
        "raw_bytes_sha256_verified": True,
        "truth_free": True,
    }
    window = {
        "available": True,
        "clock": "host_monotonic_since_collector_start",
        "arm_request_host_monotonic_s": 1.0,
        "completed_host_monotonic_s": 3.0,
    }
    result = module._validate_sensor_transport_binding_window(binding, window)
    assert result["mission_receive_window"]["clock"] == window["clock"]
    absolute_binding = dict(binding)
    absolute_binding.update(
        {
            "frame_captured_at_clock": "host_monotonic",
            "frame_captured_at_s": 2.0,
        }
    )
    absolute_window = dict(
        window,
        clock_domain="host_monotonic",
        clock_values_semantics="absolute_collector_clock",
    )
    absolute_result = module._validate_sensor_transport_binding_window(
        absolute_binding, absolute_window
    )
    assert absolute_result["mission_receive_window"]["clock_domain"] == "host_monotonic"
    mismatched_domain = dict(absolute_binding, frame_captured_at_clock="host_perf_counter")
    with pytest.raises(ValueError, match="different clock domains"):
        module._validate_sensor_transport_binding_window(mismatched_domain, absolute_window)
    for field, value in (
        ("frame_captured_at_clock", "host_monotonic"),
        ("frame_captured_at_s", 4.0),
        ("raw_bytes_sha256_verified", False),
    ):
        tampered = dict(binding)
        tampered[field] = value
        with pytest.raises(ValueError):
            module._validate_sensor_transport_binding_window(tampered, window)


def test_sensor_transport_binding_accepts_loaded_host_monotonic_manifest(tmp_path):
    """A sealed sensor manifest uses the Mission's absolute host monotonic domain."""
    from types import SimpleNamespace

    from multidetect.autonomy.serialization import canonical_sha256
    from multidetect.sensor_transport_binding import load_sensor_transport_manifest
    from scripts.vtol_selected_task_campaign import (
        _sensor_transport_for_task,
        _validate_sensor_manifest_scenario_binding,
    )

    module = _module()
    sensor_root = tmp_path / "sensor"
    sensor_root.mkdir()
    payload = b"host-monotonic-frame"
    frame_path = sensor_root / "frame.bgr24"
    frame_path.write_bytes(payload)
    frame_hash = hashlib.sha256(payload).hexdigest()
    scenario_path = tmp_path / "scenario.json"
    scenario_document = {
        "schema_version": 1,
        "scenario_id": "host-monotonic",
        "scenario_version": 1,
        "channels": {
            "runtime_input": {"path": "runtime.json", "sha256": "1" * 64},
            "truth": {"path": "truth.json", "sha256": "2" * 64},
            "expectations": {"path": "expectations.json", "sha256": "3" * 64},
        },
    }
    scenario_path.write_text(json.dumps(scenario_document), encoding="utf-8")
    scenario_hash = canonical_sha256(scenario_document)
    document = {
        "schema": "multidetect.gazebo-bgr24-frame-set",
        "schema_version": 2,
        "wire_version": 3,
        "pixel_format": "BGR24",
        "scenario_manifest_hash": scenario_hash,
        "frame_count": 1,
        "frames": [
            {
                "frame_id": "frame-0",
                "sequence": 0,
                "file": frame_path.name,
                "bytes": len(payload),
                "sha256": frame_hash,
                "wire_payload_sha256": frame_hash,
                "raw_bytes_sha256_matches_wire_payload": True,
                "provenance": {
                    "captured_at_s": 2.0,
                    "captured_at_clock": "host_monotonic",
                    "frame_telemetry": {},
                    "wire_telemetry": {},
                },
            }
        ],
        "per_frame_sensor_evidence": {
            "record_count": 1,
            "derived_ned_values_recomputed_before_seal": True,
            "gps_and_pose_values_covered_by_wire_record_sha256": True,
        },
        "source_attribution": {"truth_loaded_by_runtime": False},
        "claims": {"per_frame_sensor_geolocation_input": "passed"},
    }
    document["manifest_sha256"] = canonical_sha256(document)
    manifest_path = sensor_root / "manifest.json"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    manifest = load_sensor_transport_manifest(manifest_path)
    assert manifest.scenario_manifest_hash == scenario_hash
    _validate_sensor_manifest_scenario_binding(manifest, scenario_path)
    other_scenario_path = tmp_path / "other-scenario.json"
    other_document = dict(scenario_document)
    other_document["scenario_id"] = "other"
    other_scenario_path.write_text(json.dumps(other_document), encoding="utf-8")
    with pytest.raises(ValueError, match="scenario manifest identity mismatch"):
        _validate_sensor_manifest_scenario_binding(manifest, other_scenario_path)

    binding = _sensor_transport_for_task(manifest, {"task-0": "frame-0"}, "task-0")
    assert binding is not None
    collector = SimpleNamespace(clock=module.SystemClock())
    window = module._mission_receive_window(1.0, 2.0, 3.0, collector=collector)
    assert window["clock_domain"] == "host_monotonic"
    assert window["clock_values_semantics"] == "absolute_collector_clock"
    result = module._validate_sensor_transport_binding_window(binding, window)
    assert result["frame_captured_at_clock"] == "host_monotonic"
    assert result["mission_receive_window"]["clock_domain"] == "host_monotonic"


def test_outer_campaign_audit_reopens_sensor_manifest_before_validation(tmp_path, monkeypatch):
    from multidetect.autonomy.serialization import canonical_sha256
    from multidetect.autonomy.task_ranker import TaskRanker
    from multidetect.mission_planning import compile_planned_task_mission_from_runtime
    from multidetect.sensor_transport_binding import load_sensor_transport_manifest
    from scripts import run_vtol_mission_sensor_capture as outer

    module = _module()
    original = _documents(module)
    ranker = TaskRanker((0, 0, 0), "outer sensor audit fixture")
    geometry = module.build_live_runtime_geometry(
        home_latitude_deg=original.context.home_position.latitude_deg,
        home_longitude_deg=original.context.home_position.longitude_deg,
    )
    runtime = {
        "schema_version": 1,
        **geometry.to_mapping(),
        "safety_policy": original.policy.to_mapping(),
    }
    task = original.task_spec
    campaign = tmp_path / "campaign.json"
    campaign.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ranker": ranker.to_mapping(),
                "tasks": [{"task": task.to_mapping(), "priority": 1, "runtime": runtime}],
            }
        ),
        encoding="utf-8",
    )
    sensor_root = tmp_path / "sensor"
    sensor_root.mkdir()
    payload = b"outer-audit-frame"
    frame_path = sensor_root / "frame.bgr24"
    frame_path.write_bytes(payload)
    frame_hash = hashlib.sha256(payload).hexdigest()
    manifest_document = {
        "schema": "multidetect.gazebo-bgr24-frame-set",
        "schema_version": 2,
        "wire_version": 3,
        "pixel_format": "BGR24",
        "frame_count": 1,
        "frames": [
            {
                "frame_id": "frame-0",
                "sequence": 0,
                "file": frame_path.name,
                "bytes": len(payload),
                "sha256": frame_hash,
                "wire_payload_sha256": frame_hash,
                "raw_bytes_sha256_matches_wire_payload": True,
                "provenance": {
                    "captured_at_s": 1.0,
                    "captured_at_clock": "host_monotonic_since_collector_start",
                    "frame_telemetry": {},
                    "wire_telemetry": {},
                },
            }
        ],
        "per_frame_sensor_evidence": {
            "record_count": 1,
            "derived_ned_values_recomputed_before_seal": True,
            "gps_and_pose_values_covered_by_wire_record_sha256": True,
        },
        "source_attribution": {"truth_loaded_by_runtime": False},
        "claims": {"per_frame_sensor_geolocation_input": "passed"},
    }
    manifest_document["manifest_sha256"] = canonical_sha256(manifest_document)
    manifest_path = sensor_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_document), encoding="utf-8")
    manifest = load_sensor_transport_manifest(manifest_path)
    compilation = compile_planned_task_mission_from_runtime(
        task,
        original.context,
        "vtol",
        runtime=runtime,
        airframe_profile_hash=HASH_A,
        mission_id="outer-sensor-audit",
    )
    sensor = manifest.selection_mapping(["frame-0"])
    sensor.update(
        {
            "task_id": task.task_id,
            "frame_id": "frame-0",
            "frame_sequence": 0,
            "frame_sha256": frame_hash,
            "frame_bytes": len(payload),
            "frame_captured_at_s": 1.0,
            "frame_captured_at_clock": "host_monotonic_since_collector_start",
            "mission_receive_window": {
                "available": True,
                "clock": "host_monotonic_since_collector_start",
                "arm_request_host_monotonic_s": 0.0,
                "completed_host_monotonic_s": 2.0,
            },
        }
    )
    report_path = tmp_path / "campaign-missions" / "mission-001.json"
    report_path.parent.mkdir()
    report_path.write_text(
        json.dumps(
            {
                "gate_passed": True,
                "container": {"id": CONTAINER_ID},
                "documents": {
                    "context_snapshot": {"document": original.context.to_mapping()},
                    "executable_mission": {
                        "document": {"mission_id": "outer-sensor-audit"},
                        "sha256": compilation.executable_mission_hash,
                    },
                },
                "mission_receive_window": sensor["mission_receive_window"],
                "sensor_transport": sensor,
                "shared_outcome_shadow": {
                    "outcome": {
                        "status": "completed",
                        "mission_hash": compilation.executable_mission_hash,
                    }
                },
                "transaction": {"id": "outer-sensor-transaction"},
            }
        ),
        encoding="utf-8",
    )
    report_path.with_suffix(".transaction.jsonl").write_text("fixture", encoding="utf-8")
    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps(
            {
                "status": "all_tasks_completed",
                "session_id": CONTAINER_ID,
                "ranker_sha256": ranker.canonical_hash,
                "sensor_transport_used": True,
                "sensor_transport": {
                    "manifest_path": str(manifest.path),
                    "manifest_file_sha256": manifest.file_sha256,
                    "manifest_sha256": manifest.manifest_sha256,
                    "frame_count": 1,
                    "truth_free": True,
                },
                "completed": [
                    {
                        "task_id": task.task_id,
                        "mission_hash": compilation.executable_mission_hash,
                        "ranker_sha256": ranker.canonical_hash,
                        "evidence_path": str(report_path),
                        "sensor_transport": sensor,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(outer, "validate_mission_journal", lambda *args, **kwargs: {"ok": True})
    kwargs = {
        "campaign_file": campaign,
        "expected_input_hash": outer._sha256_path(campaign),
        "container_id": CONTAINER_ID,
        "airframe_profile_hash": HASH_A,
        "sensor_transport_manifest": manifest,
        "sensor_frame_ids": {task.task_id: "frame-0"},
    }
    assert outer.validate_campaign_result(summary, **kwargs)["sensor_transport_used"] is True

    frame_path.write_bytes(b"tampered-after-load")
    with pytest.raises(outer.VtolMissionSensorCaptureError, match="manifest audit failed"):
        outer.validate_campaign_result(summary, **kwargs)


@pytest.mark.parametrize("tamper", [None, "hash", "upload", "command", "cleanup", "reason"])
def test_campaign_rejection_audit_recomputes_and_rejects_tampering(tamper):
    from multidetect.autonomy.task_selection import TaskCandidate, select_next_task
    from scripts.audit_campaign_rejection import verify

    module = _module()
    original = _documents(module)
    candidates = [
        TaskCandidate(replace(original.task_spec, task_id="high"), 10),
        TaskCandidate(replace(original.task_spec, task_id="low"), 1),
    ]
    context = original.context
    later = replace(
        context,
        snapshot_id="later",
        captured_at_s=context.captured_at_s + 100,
        estimated_flight_time_s=1,
    )
    decision = select_next_task(candidates[1:], later, "vtol")
    first = {
        "gate_passed": True,
        "documents": {
            "context_snapshot": {
                "document": context.to_mapping(),
                "sha256": context.canonical_hash,
            },
            "task_spec": {"document": {"task_id": "high"}},
            "executable_mission": {"sha256": HASH_A},
        },
        "shared_outcome_shadow": {"outcome": {"status": "completed", "mission_hash": HASH_A}},
        "container": {"id": CONTAINER_ID},
        "telemetry_session": {"collector_origin_host_monotonic_s": 1000},
    }
    second = {
        "gate_passed": False,
        "selection_rejection": {
            "context": later.to_mapping(),
            "context_sha256": later.canonical_hash,
            "rejected": [
                {"task_id": item.task_id, "reason": item.reason} for item in decision.rejected
            ],
        },
        "transaction": {"final_state": "not_created", "state_events": []},
        "mission_journal": {"exists": False, "record_count": 0},
        "protocol": {"mission_item_count": 0, "upload_acknowledged": False},
        "mavlink": {"control_command_events": []},
        "container": first["container"],
        "telemetry_session": first["telemetry_session"],
    }
    outer = {
        "container": {
            "cleanup": {
                "exact_id": CONTAINER_ID,
                "cleanup_succeeded": True,
                "post_remove_absence_proven": True,
            }
        }
    }
    if tamper == "hash":
        second["selection_rejection"]["context_sha256"] = HASH_B
    elif tamper == "upload":
        second["protocol"]["mission_item_count"] = 1
    elif tamper == "command":
        second["mavlink"]["control_command_events"] = [{"command": "arm"}]
    elif tamper == "cleanup":
        outer["container"]["cleanup"]["post_remove_absence_proven"] = False
    elif tamper == "reason":
        second["selection_rejection"]["rejected"][0]["reason"] = "invented"
    if tamper:
        with pytest.raises(ValueError):
            verify(first, second, outer, candidates)
    else:
        assert verify(first, second, outer, candidates)["report_level_negative_verified"]


@pytest.mark.parametrize("tamper", [None, "consumption", "prefix", "context", "missing"])
def test_rejection_energy_audit_replays_cumulative_prefixes(tamper):
    from multidetect.simulation.energy_replay import CumulativeSimulationEnergy
    from multidetect.simulator.flight_model import SimulationEnergyProfile
    from scripts.audit_campaign_rejection import verify_energy

    original = _documents(_module()).context
    profile = SimulationEnergyProfile(100, 36, 324, "explicit test assumption")
    state = CumulativeSimulationEnergy(profile, session_id=CONTAINER_ID, initial_remaining_wh=33.5)
    samples = []
    reports = []
    for i in range(3):
        for kind, field, value in (
            ("HEARTBEAT", "base_mode", 128),
            ("VFR_HUD", "throttle", 100),
            ("GLOBAL_POSITION_INT", "time_boot_ms", 1000 + i * 500),
        ):
            samples.append(
                dict(
                    type=kind,
                    field=field,
                    value=value,
                    at_s=i * 0.5,
                    source_system=1,
                    source_component=1,
                )
            )
        if i == 0:
            continue
        history = dict(
            samples=[dict(s) for s in samples],
            samples_seen=len(samples),
            samples_dropped=0,
            receive_origin_host_monotonic_s=1000,
            receive_clock="host_monotonic_since_collector_start",
        )
        energy = state.observe(history, session_id=CONTAINER_ID)
        context = replace(original, captured_at_s=i * 0.5, snapshot_id=f"sample-{i}")
        projected = replace(
            context,
            battery_remaining_pct=energy["battery_remaining_pct"],
            estimated_flight_time_s=energy["estimated_flight_time_s"],
        )
        energy.update(
            input_context_hash=context.canonical_hash,
            projected_context_hash=projected.canonical_hash,
        )
        reports.append(
            {
                "planning_energy": energy,
                "planning_energy_input": {
                    "history": history,
                    "input_context": context.to_mapping(),
                },
                "container": {"id": CONTAINER_ID},
                "telemetry_session": {"collector_origin_host_monotonic_s": 1000},
                "documents": {"context_snapshot": {"sha256": projected.canonical_hash}},
                "selection_rejection": {"context_sha256": projected.canonical_hash}
                if i == 2
                else None,
            }
        )
    if tamper == "consumption":
        reports[1]["planning_energy"]["consumed_wh"] = 0
    elif tamper == "prefix":
        reports[1]["planning_energy_input"]["history"]["samples"][0]["value"] = 0
    elif tamper == "context":
        reports[1]["selection_rejection"]["context_sha256"] = HASH_B
    elif tamper == "missing":
        reports[1].pop("planning_energy_input")
    if tamper:
        with pytest.raises((ValueError, KeyError)):
            verify_energy(*reports)
    else:
        replayed = verify_energy(*reports)
        assert replayed[1]["consumed_wh"] == pytest.approx(0.1)
        assert replayed[1]["newly_consumed_wh"] == pytest.approx(0.05)


def test_diverse_campaign_produces_distinct_wire_geometry():
    from multidetect.mission_planning import compile_planned_task_mission_from_runtime
    from scripts.vtol_selected_task_campaign import load_campaign

    module = _module()
    original = _documents(module)
    home = module.GeoPoint(1.3, 103.8005)
    context = replace(original.context, home_position=home, aircraft_position=home)
    candidates, runtimes = load_campaign(
        ROOT / "configs/scenarios/vtol-diverse-routes.synthetic-campaign.json"
    )
    compilations = [
        compile_planned_task_mission_from_runtime(
            candidate.task,
            context,
            "vtol",
            runtime=runtimes[candidate.task.task_id],
            airframe_profile_hash=HASH_A,
        )
        for candidate in candidates
    ]
    coordinates = [
        tuple((item.x, item.y) for item in result.compiled_mission.wire_items)
        for result in compilations
    ]
    assert coordinates[0] != coordinates[1]
    assert compilations[0].wire_hash != compilations[1].wire_hash
    for candidate in candidates:
        assert candidate.task.geometry.centerline[-1].longitude_deg == home.longitude_deg


@pytest.mark.parametrize("fault", [None, "policy", "budget", "clock"])
def test_shared_selection_provider_enters_existing_document_path(fault):
    from multidetect.autonomy.task_selection import TaskCandidate, select_next_task
    from multidetect.mission_planning import compile_planned_task_mission

    module = _module()
    original = _documents(module)
    seen = []

    def projection(context):
        return replace(context, battery_remaining_pct=95), {"test_projection": True}

    def provider(context):
        assert context.battery_remaining_pct == 95
        preferred = replace(original.task_spec, task_id="preferred")
        if fault == "budget":
            preferred = replace(
                preferred, constraints=replace(preferred.constraints, maximum_duration_s=601)
            )
        decision = select_next_task(
            [
                TaskCandidate(replace(original.task_spec, task_id="other"), 0),
                TaskCandidate(preferred, 10),
            ],
            context,
            "vtol",
        )
        task = decision.selected.candidate.task
        assert task.task_id == "preferred"
        geometry = module.build_live_runtime_geometry(
            home_latitude_deg=context.home_position.latitude_deg,
            home_longitude_deg=context.home_position.longitude_deg,
        )
        policy = original.policy
        if fault == "policy":
            policy = replace(policy, geofence_radius_m=400)
        compilation = compile_planned_task_mission(
            task,
            context,
            "vtol",
            geometry=geometry,
            policy=policy,
            airframe_profile_hash=HASH_A,
            mission_id="chosen-preferred",
        )
        seen.append(compilation)
        return module.SelectedMissionInputs(task, compilation, geometry, policy)

    kwargs = dict(
        provider=provider,
        projection=projection,
        receive_clock=None if fault == "clock" else lambda: _live_context()["captured_at_s"],
    )
    if fault:
        with pytest.raises(module.VtolMissionAcceptanceError):
            _documents(module, **kwargs)
    else:
        result = _documents(module, **kwargs)
        assert result.task_spec.task_id == "preferred"
        assert result.compiled is seen[0].compiled_mission
        assert result.energy_evidence == {"test_projection": True}
        transaction = module.Px4MissionTransaction(
            transaction_id=result.operator_grant.transaction_id,
            compiled=result.compiled,
            validation_report=result.validation_report,
            operator_grant=result.operator_grant,
            bindings=result.bindings,
            now_utc=datetime.now(timezone.utc),
        )
        assert transaction is not None


@pytest.mark.parametrize("mutation", [None, "stale", "future", "context", "profile", "geometry"])
def test_selected_compilation_binding_preserves_exact_artifacts(mutation):
    from multidetect.mission_planning import compile_planned_task_mission

    module = _module()
    original = _documents(module)
    context = original.context
    geometry = module.build_live_runtime_geometry(
        home_latitude_deg=context.home_position.latitude_deg,
        home_longitude_deg=context.home_position.longitude_deg,
    )
    selected = compile_planned_task_mission(
        original.task_spec,
        context,
        "vtol",
        geometry=geometry,
        policy=original.policy,
        airframe_profile_hash=HASH_A,
        mission_id="selected-mission-1",
    )
    kwargs = dict(
        selection=selected,
        task_spec=original.task_spec,
        context=context,
        geometry=geometry,
        policy=original.policy,
        runtime_input=module.load_runtime_scenario(MANIFEST, mode=module.RuntimeMode.SIM),
        container_id=CONTAINER_ID,
        airframe_profile_hash=HASH_A,
        runtime_descriptor_hash=HASH_B,
        runtime_provenance_hash=HASH_C,
        now_utc=datetime.now(timezone.utc),
        now_receive_s=context.captured_at_s,
    )
    if mutation == "stale":
        kwargs["now_receive_s"] += 2
    elif mutation == "future":
        kwargs["now_receive_s"] -= 1
    elif mutation == "context":
        kwargs["context"] = replace(context, battery_remaining_pct=90)
    elif mutation == "profile":
        kwargs["airframe_profile_hash"] = HASH_B
    elif mutation == "geometry":
        kwargs["geometry"] = module.build_live_runtime_geometry(
            home_latitude_deg=context.home_position.latitude_deg + 0.00001,
            home_longitude_deg=context.home_position.longitude_deg,
        )
    if mutation is not None:
        with pytest.raises((ValueError, module.VtolMissionAcceptanceError, Px4MissionError)):
            module.build_selected_acceptance_documents(**kwargs)
    else:
        result = module.build_selected_acceptance_documents(**kwargs)
        assert result.compiled is selected.compiled_mission
        assert result.plan is selected.plan
        assert result.context is context
        assert result.bindings.context_snapshot_hash == context.canonical_hash
        assert result.operator_grant.executable_mission_hash == selected.executable_mission_hash


def test_original_prompt_is_bound_to_documents_evidence_and_hashes():
    module = _module()
    prompt = _prompt(module, "search the original corridor and land")
    documents = _documents(
        module,
        original_prompt=prompt,
        require_original_prompt=True,
    )

    assert documents.prompt == prompt
    assert documents.bindings.prompt_hash == prompt.canonical_hash
    assert documents.operator_grant.prompt_hash == prompt.canonical_hash
    evidence = module._document_evidence(documents)
    assert evidence["prompt"] == {
        "document": prompt.to_mapping(),
        "sha256": prompt.canonical_hash,
    }
    assert evidence["bindings"]["prompt_hash"] == prompt.canonical_hash
    assert evidence["operator_grant"]["document"]["prompt_hash"] == prompt.canonical_hash


def test_original_prompt_is_required_when_requested():
    module = _module()
    with pytest.raises(module.VtolMissionAcceptanceError, match="original_prompt"):
        _documents(module, require_original_prompt=True)


def test_selected_provider_cannot_replace_original_prompt():
    from multidetect.mission_planning import compile_planned_task_mission

    module = _module()
    original = _documents(module)
    original_prompt = _prompt(module, "the prompt that entered the campaign")
    replacement_prompt = _prompt(module, "a provider-generated replacement")

    def provider(context):
        geometry = module.build_live_runtime_geometry(
            home_latitude_deg=context.home_position.latitude_deg,
            home_longitude_deg=context.home_position.longitude_deg,
        )
        policy = replace(original.policy, geofence_center=context.home_position)
        compilation = compile_planned_task_mission(
            original.task_spec,
            context,
            "vtol",
            geometry=geometry,
            policy=policy,
            airframe_profile_hash=HASH_A,
            mission_id="selected-prompt-mismatch",
        )
        return module.SelectedMissionInputs(
            original.task_spec,
            compilation,
            geometry,
            policy,
            replacement_prompt,
        )

    with pytest.raises(module.VtolMissionAcceptanceError, match="(?i)prompt"):
        _documents(
            module,
            provider=provider,
            receive_clock=lambda: original.context.captured_at_s,
            original_prompt=original_prompt,
            require_original_prompt=True,
        )


def test_selected_documents_reject_missing_prompt_when_required():
    from multidetect.mission_planning import compile_planned_task_mission

    module = _module()
    original = _documents(module)
    context = original.context
    geometry = module.build_live_runtime_geometry(
        home_latitude_deg=context.home_position.latitude_deg,
        home_longitude_deg=context.home_position.longitude_deg,
    )
    selection = compile_planned_task_mission(
        original.task_spec,
        context,
        "vtol",
        geometry=geometry,
        policy=original.policy,
        airframe_profile_hash=HASH_A,
        mission_id="selected-prompt-required",
    )
    with pytest.raises(module.VtolMissionAcceptanceError, match="(?i)prompt"):
        module.build_selected_acceptance_documents(
            selection=selection,
            task_spec=original.task_spec,
            context=context,
            geometry=geometry,
            policy=original.policy,
            runtime_input=module.load_runtime_scenario(MANIFEST, mode=module.RuntimeMode.SIM),
            container_id=CONTAINER_ID,
            airframe_profile_hash=HASH_A,
            runtime_descriptor_hash=HASH_B,
            runtime_provenance_hash=HASH_C,
            now_utc=datetime.now(timezone.utc),
            now_receive_s=context.captured_at_s,
            require_original_prompt=True,
        )


def test_session_home_survives_sortie_without_freezing_aircraft_position(monkeypatch):
    module = _module()
    session = _telemetry_session(module, monkeypatch)
    first = module.GeoPoint(_live_context()["latitude_deg"], _live_context()["longitude_deg"])
    session.acquire(container_id=CONTAINER_ID, endpoint=session.endpoint)
    assert session.bind_planning_home(first) == first
    session.release(succeeded=True)
    session.acquire(container_id=CONTAINER_ID, endpoint=session.endpoint)
    live = {**_live_context(), "latitude_deg": first.latitude_deg + 0.000001}
    moved = module.GeoPoint(live["latitude_deg"], live["longitude_deg"])
    home = session.bind_planning_home(moved)
    documents = _documents(module, telemetry=live, home=home)
    assert documents.context.home_position == first
    assert documents.context.aircraft_position == moved
    assert documents.context.aircraft_position != documents.context.home_position
    assert documents.policy.geofence_center == first
    assert documents.plan == module.plan_task(documents.task_spec, documents.context, "vtol")
    session.close()


@pytest.mark.parametrize("state", ["idle", "failed", "closed"])
def test_home_binding_requires_healthy_active_session(monkeypatch, state):
    module = _module()
    session = _telemetry_session(module, monkeypatch)
    if state == "failed":
        session.acquire(container_id=CONTAINER_ID, endpoint=session.endpoint)
        session.release(succeeded=False)
    elif state == "closed":
        session.close()
    with pytest.raises(module.VtolMissionAcceptanceError, match="active healthy"):
        session.bind_planning_home(module.GeoPoint(1.3, 103.8))


@pytest.mark.parametrize("endurance", [None, 0, 1])
def test_external_planning_does_not_substitute_timeout_for_endurance(endurance):
    module = _module()
    telemetry = {**_live_context(), "estimated_flight_time_s": endurance}
    with pytest.raises(ValueError, match="energy budget|flight time must be positive"):
        _documents(module, telemetry=telemetry)


def test_external_shared_planner_keeps_task_duration_limit():
    module = _module()
    with pytest.raises(ValueError, match="maximum_duration"):
        _documents(module, maximum_duration_s=10)


def test_energy_projection_precedes_shared_plan_and_hash_binding():
    module = _module()
    state = module.CumulativeSimulationEnergy(
        module.SimulationEnergyProfile(100, 36, 324, "test fixture"),
        session_id=CONTAINER_ID,
        initial_remaining_wh=100,
    )
    samples = []
    for at, boot in ((11.5, 1000), (12.5, 2000)):
        for kind, field, value in (
            ("HEARTBEAT", "base_mode", 0),
            ("VFR_HUD", "throttle", 0),
            ("GLOBAL_POSITION_INT", "time_boot_ms", boot),
        ):
            samples.append(
                dict(
                    type=kind,
                    field=field,
                    value=value,
                    at_s=at,
                    source_system=1,
                    source_component=1,
                )
            )
    history = dict(
        samples=samples,
        samples_seen=len(samples),
        samples_dropped=0,
        receive_origin_host_monotonic_s=100,
        receive_clock="host_monotonic_since_collector_start",
    )

    def project(context):
        clock = module.ManualClock(
            context.captured_at_s, datetime.fromisoformat(context.captured_at_utc)
        )
        return state.project_context(
            context,
            history,
            session_id=CONTAINER_ID,
            clock=clock,
            runtime_mode=module.RuntimeMode.SIM,
        )

    docs = _documents(
        module, projection=project, telemetry={**_live_context(), "estimated_flight_time_s": None}
    )
    assert docs.context.battery_remaining_pct == pytest.approx(99.99)
    assert docs.context.estimated_flight_time_s == pytest.approx(999.9)
    assert docs.plan == module.plan_task(docs.task_spec, docs.context, "vtol")
    assert docs.bindings.context_snapshot_hash == docs.context.canonical_hash
    assert docs.energy_evidence["projected_context_hash"] == docs.context.canonical_hash


def test_live_geometry_and_documents_are_runtime_bound_complete_vtol() -> None:
    module = _module()
    documents = _documents(module)
    mission = documents.compiled.executable_mission

    assert documents.context.home_position.latitude_deg == _live_context()["latitude_deg"]
    assert documents.context.home_position.longitude_deg == _live_context()["longitude_deg"]
    assert documents.context.gps_fix_type == 3
    assert documents.context.map_cache_available is True
    assert documents.context.dem_available is True
    assert documents.bindings.prompt_hash == documents.prompt.canonical_hash
    assert documents.bindings.task_spec_hash == documents.task_spec.canonical_hash
    assert documents.bindings.plan_hash == documents.plan.canonical_hash
    assert documents.operator_grant.operator_id == (
        "test-only-acceptance-fixture-not-human-approval"
    )
    assert [item.command.value for item in mission.items] == [
        "MAV_CMD_NAV_VTOL_TAKEOFF",
        "MAV_CMD_DO_VTOL_TRANSITION",
        "MAV_CMD_NAV_WAYPOINT",
        "MAV_CMD_NAV_WAYPOINT",
        "MAV_CMD_NAV_LOITER_TIME",
        "MAV_CMD_DO_VTOL_TRANSITION",
        "MAV_CMD_NAV_VTOL_LAND",
    ]
    assert all(item.mission_type == 0 for item in mission.items)
    assert documents.compiled.wire_hash == module.px4_wire_hash(documents.compiled.wire_items)
    evidence = module._document_evidence(documents)
    assert evidence["operator_grant"]["human_operator_approval_claimed"] is False
    assert evidence["mission_dispatcher"] == {
        "name": "compile_executable_mission",
        "vehicle_class_input": "vtol",
        "vehicle_class_normalized": "vtol",
    }


def test_live_context_clamps_negative_sitl_speed_without_fabricating_position() -> None:
    module = _module()

    class _Collector:
        elapsed_s = 4.2000000000123
        last_position = {
            "latitude_deg": 1.3,
            "longitude_deg": 103.8,
            "relative_altitude_m": 0.0,
        }
        last_battery_remaining = 97.0

        def __init__(self) -> None:
            self._messages = iter(
                [
                    _Message(
                        "VFR_HUD",
                        groundspeed=-0.04,
                        airspeed=-0.78,
                    ),
                    _Message(
                        "GPS_RAW_INT",
                        fix_type=3,
                        lat=13_000_000,
                        lon=1_038_000_000,
                        satellites_visible=10,
                        h_acc=900,
                    ),
                ]
            )

        def pump(self, _timeout_s: float) -> _Message | None:
            try:
                return next(self._messages)
            except StopIteration:
                return None

    context = module._collect_live_context(_Collector(), timeout_s=0.1)
    assert context["captured_at_s"] == _Collector.elapsed_s
    assert context["latitude_deg"] == 1.3
    assert context["longitude_deg"] == 103.8
    assert context["ground_speed_mps"] == 0.0
    assert context["airspeed_mps"] == 0.0
    assert context["raw_ground_speed_mps"] == -0.04
    assert context["raw_airspeed_mps"] == -0.78
    assert context["nonnegative_speed_clamp_applied"] is True


def test_telemetry_collector_records_monotonic_freshness_for_flight_state() -> None:
    module = _module()
    collector = module.TelemetryCollector(object())

    collector._record(
        _Message(
            "HEARTBEAT",
            base_mode=module.mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED,
            custom_mode=module.PX4_AUTO_MISSION_CUSTOM_MODE,
            system_status=4,
            type=2,
        )
    )
    collector._record(
        _Message(
            "GLOBAL_POSITION_INT",
            lat=13_000_000,
            lon=1_038_000_000,
            relative_alt=20_000,
            hdg=9_000,
        )
    )
    collector._record(
        _Message(
            "EXTENDED_SYS_STATE",
            vtol_state=module.MAV_VTOL_STATE_FW,
            landed_state=module.mavutil.mavlink.MAV_LANDED_STATE_IN_AIR,
        )
    )

    observed = (
        collector.last_heartbeat_observed_at_monotonic_s,
        collector.last_position_observed_at_monotonic_s,
        collector.last_extended_state_observed_at_monotonic_s,
    )
    assert all(isinstance(value, float) and value > 0.0 for value in observed)
    assert observed == tuple(sorted(observed))


@pytest.mark.parametrize("endurance", (None, 0, -1, True, 240))
def test_post_mission_observation_never_substitutes_endurance(endurance) -> None:
    module = _module()
    collector = module.TelemetryCollector(object())
    for message in (
        _Message("HEARTBEAT", base_mode=0, custom_mode=0, system_status=3, type=2),
        _Message(
            "GPS_RAW_INT", fix_type=3, satellites_visible=12, lat=13000000, lon=1038000000, alt=5000
        ),
        _Message("GLOBAL_POSITION_INT", lat=13000000, lon=1038000000, relative_alt=0, hdg=0),
        _Message("EXTENDED_SYS_STATE", vtol_state=3, landed_state=1),
        _Message("BATTERY_STATUS", battery_remaining=76, time_remaining=endurance),
    ):
        collector._record(message)
    sample = collector.post_mission_observation(now_s=module.time.monotonic())
    assert sample["battery_remaining_pct"] == 76
    assert sample["authorizes_next_task"] is False
    assert sample["telemetry_complete_and_fresh"] == (endurance == 240)
    assert sample["observed_endurance_s"] == (240 if endurance == 240 else None)
    stale = collector.post_mission_observation(now_s=module.time.monotonic() + 2)
    assert stale["telemetry_complete_and_fresh"] is False
    assert "BATTERY_STATUS:missing_or_stale" in stale["missing_reasons"]
    collector._record(
        _Message("BATTERY_STATUS", battery_remaining=76, time_remaining=240, source_system=2)
    )
    wrong = collector.post_mission_observation(now_s=module.time.monotonic())
    assert "BATTERY_STATUS:source_not_px4" in wrong["missing_reasons"]


def test_post_mission_observation_unknown_battery_is_not_zero() -> None:
    module = _module()
    collector = module.TelemetryCollector(object())
    collector._record(_Message("BATTERY_STATUS", battery_remaining=-1))
    result = collector.post_mission_observation(now_s=module.time.monotonic())
    assert result["battery_remaining_pct"] is None
    assert result["observed_endurance_s"] is None
    assert result["telemetry_complete_and_fresh"] is False


@pytest.mark.parametrize("remaining", (True, 1.5, "50", None, -1, 101))
def test_battery_history_does_not_coerce_invalid_remaining(remaining) -> None:
    module = _module()
    collector = module.TelemetryCollector(object())
    collector._record(_Message("BATTERY_STATUS", battery_remaining=remaining))
    assert collector.last_battery_remaining is None
    assert collector.battery_samples[-1]["battery_remaining_pct"] is None


def test_battery_history_preserves_identity_units_sentinels_and_truncation(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module.TelemetryCollector, "BATTERY_HISTORY_LIMIT", 2)
    collector = module.TelemetryCollector(object())
    for battery_id in (0, 1, 2):
        collector._record(
            _Message(
                "BATTERY_STATUS",
                id=battery_id,
                source_system=2,
                battery_remaining=50,
                current_battery=-1,
                current_consumed=-1,
                energy_consumed=-1,
                time_remaining=0,
            )
        )
    result = collector.post_mission_observation(now_s=module.time.monotonic())
    history = result["battery_history"]
    assert history["samples_seen"] == 3
    assert history["samples_dropped"] == 1
    assert [s["battery_id"] for s in history["samples"]] == [1, 2]
    assert all(s["source_system"] == 2 for s in history["samples"])
    assert history["samples"][0]["current_battery_cA_raw"] == -1
    assert history["samples"][0]["current_consumed_mAh_raw"] == -1
    assert history["samples"][0]["energy_consumed_hJ_raw"] == -1
    assert history["samples"][0]["time_remaining_s_raw"] == 0
    assert "BATTERY_STATUS:source_not_px4" in result["missing_reasons"]
    assert result["observed_endurance_s"] is None
    assert result["authorizes_next_task"] is False
    history["samples"][0]["battery_id"] = 99
    assert collector.battery_samples[0]["battery_id"] == 1


def test_battery_history_preserves_reported_energy_without_estimating_endurance() -> None:
    module = _module()
    collector = module.TelemetryCollector(object())
    collector._record(
        _Message(
            "BATTERY_STATUS",
            id=0,
            battery_remaining=72,
            current_battery=250,
            current_consumed=1200,
            energy_consumed=4320,
            time_remaining=0,
        )
    )
    sample = collector.battery_samples[0]
    assert sample["current_battery_cA_raw"] == 250
    assert sample["current_consumed_mAh_raw"] == 1200
    assert sample["energy_consumed_hJ_raw"] == 4320
    assert collector.last_battery_time_remaining_s is None


@pytest.mark.parametrize("value", (True, "50", 1.5, -1, 101, None))
def test_energy_input_history_rejects_invalid_throttle(value) -> None:
    module = _module()
    collector = module.TelemetryCollector(object())
    collector._record(_Message("VFR_HUD", throttle=value))
    sample = collector.energy_input_samples[-1]
    assert sample["field_valid"] is False
    assert sample["value"] is None


def test_energy_input_history_keeps_clock_reset_foreign_source_and_truncation(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module.TelemetryCollector, "ENERGY_INPUT_HISTORY_LIMIT", 3)
    collector = module.TelemetryCollector(object())
    for message in (
        _Message("VFR_HUD", throttle=50),
        _Message("SYSTEM_TIME", time_boot_ms=10000),
        _Message("SYSTEM_TIME", time_boot_ms=0),
        _Message("VFR_HUD", throttle=75, source_system=2),
    ):
        collector._record(message)
    history = collector.post_mission_observation(now_s=module.time.monotonic())[
        "energy_input_history"
    ]
    assert history["samples_seen"] == 4
    assert history["samples_dropped"] == 1
    assert [s["value"] for s in history["samples"]] == [10000, 0, 75]
    assert history["samples"][-1]["source_is_expected_px4"] is False
    assert history["synchronization_claimed"] is False
    assert history["clock_continuity_proven"] is False
    assert history["energy_integration_performed"] is False
    history["samples"][0]["value"] = 99
    assert collector.energy_input_samples[0]["value"] == 10000


@pytest.mark.parametrize("stamp", (True, -1, 2**32, float("nan")))
def test_energy_input_boot_time_requires_uint32(stamp) -> None:
    module = _module()
    collector = module.TelemetryCollector(object())
    collector._record(_Message("SYSTEM_TIME", time_boot_ms=stamp))
    assert collector.energy_input_samples[-1]["field_valid"] is False


def test_mission_window_binds_only_host_receive_time() -> None:
    module = _module()
    window = module._mission_receive_window(100.0, 103.25, 190.5)
    assert window["available"] is True
    assert window["start_receive_s"] == 3.25
    assert window["end_receive_s"] == 90.5
    assert window["boot_time_mapping_claimed"] is False
    assert window["energy_coverage_claimed"] is False
    assert window["authorizes_next_task"] is False


@pytest.mark.parametrize(
    "values",
    [
        (None, 2, 3),
        (1, None, 3),
        (1, 2, None),
        (3, 2, 4),
        (1, 3, 2),
        (True, 2, 3),
        (1, float("nan"), 3),
        (-1, 2, 3),
    ],
)
def test_mission_window_does_not_invent_missing_or_invalid_boundaries(values) -> None:
    module = _module()
    window = module._mission_receive_window(*values)
    assert window["available"] is False
    assert window["start_receive_s"] is None
    assert window["end_receive_s"] is None


def test_energy_history_exports_its_actual_host_origin() -> None:
    module = _module()
    collector = module.TelemetryCollector(object())
    history = collector.post_mission_observation(now_s=collector.started)["energy_input_history"]
    assert history["receive_origin_host_monotonic_s"] == collector.started


def test_preparation_keeps_receiver_on_caller_and_returns_result() -> None:
    module = _module()
    pumped = threading.Event()
    caller = threading.get_ident()
    receive_threads = []
    worker_threads = []

    class Collector:
        def pump(self, timeout):
            assert timeout == 0.05
            receive_threads.append(threading.get_ident())
            pumped.set()

    def prepare(value, *, extra):
        worker_threads.append(threading.get_ident())
        assert pumped.wait(2.0)
        return value + extra

    assert module._prepare_while_collecting(Collector(), prepare, 2, extra=3) == 5
    assert receive_threads and set(receive_threads) == {caller}
    assert worker_threads[0] != caller


def test_preparation_failure_propagates_after_telemetry() -> None:
    module = _module()
    pumped = threading.Event()

    class Collector:
        def pump(self, timeout):
            pumped.set()

    def prepare():
        assert pumped.wait(2.0)
        raise RuntimeError("preparation failed")

    with pytest.raises(RuntimeError, match="preparation failed"):
        module._prepare_while_collecting(Collector(), prepare)


def test_receiver_failure_joins_preparation_before_returning() -> None:
    module = _module()
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class Collector:
        def pump(self, timeout):
            assert started.wait(2.0)
            release.set()
            raise RuntimeError("receiver failed")

    def prepare():
        started.set()
        assert release.wait(2.0)
        finished.set()

    with pytest.raises(RuntimeError, match="receiver failed"):
        module._prepare_while_collecting(Collector(), prepare)
    assert finished.is_set()


def test_protocol_waits_record_interleaved_energy_inputs_once() -> None:
    module = _module()
    _docs, transaction, plan = _transaction(module)
    wire = [*_upload_messages(plan), *_readback_messages(plan)]
    interleaved = []
    for message in wire:
        interleaved.extend([_Message("SYSTEM_TIME", time_boot_ms=100), message])
    connection = _Connection(interleaved)
    collector = module.TelemetryCollector(connection)
    collector.send_gcs_heartbeat = lambda: None
    upload = module.upload_int_only_mission(connection, transaction, plan, receive=collector.pump)
    readback = module.readback_int_only_mission(
        connection, transaction, plan, receive=collector.pump
    )
    assert upload["idle_retry_count"] == 0
    assert readback["hash_match"] is True
    assert len(collector.energy_input_samples) == len(wire)
    assert all(row["value"] == 100 for row in collector.energy_input_samples)
    assert transaction.state is module.MissionTransactionState.READBACK_VERIFIED


def test_unrelated_telemetry_does_not_extend_protocol_deadline(monkeypatch) -> None:
    module = _module()
    now = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    timeouts = []

    def receive(timeout):
        timeouts.append(timeout)
        now[0] += 0.5
        return _Message("SYSTEM_TIME", time_boot_ms=100)

    assert module._receive_protocol_message(None, ("MISSION_ACK",), 1.5, receive) is None
    assert timeouts == [1.5, 1.0, 0.5]


def test_collected_legacy_request_still_denies_upload() -> None:
    module = _module()
    _docs, transaction, plan = _transaction(module)
    connection = _Connection(
        [_Message("SYSTEM_TIME", time_boot_ms=100), *_upload_messages(plan, legacy_sequence=0)]
    )
    collector = module.TelemetryCollector(connection)
    collector.send_gcs_heartbeat = lambda: None
    with pytest.raises(Px4MissionError):
        module.upload_int_only_mission(connection, transaction, plan, receive=collector.pump)
    assert len(collector.energy_input_samples) == 1


def test_fake_int_upload_readback_commit_and_permit_exact_auto_mission() -> None:
    module = _module()
    _docs, transaction, upload_plan = _transaction(module)
    foreign_qgc_request = _Message(
        "MISSION_REQUEST_INT",
        seq=0,
        mission_type=0,
        target_system=255,
        target_component=190,
    )
    upload_connection = _Connection([foreign_qgc_request, *_upload_messages(upload_plan)])

    upload = module.upload_int_only_mission(upload_connection, transaction, upload_plan)
    assert upload["acknowledged"] is True
    assert upload["int_only"] is True
    assert upload["ignored_foreign_target_messages"] == 1
    assert transaction.state is module.MissionTransactionState.UPLOADED
    assert [name for name, _args in upload_connection.mav.calls] == [
        "MISSION_COUNT",
        *("MISSION_ITEM_INT" for _ in range(upload_plan.item_count)),
    ]

    foreign_qgc_count = _Message(
        "MISSION_COUNT",
        count=0,
        mission_type=0,
        target_system=255,
        target_component=190,
    )
    readback_connection = _Connection([foreign_qgc_count, *_readback_messages(upload_plan)])
    readback = module.readback_int_only_mission(readback_connection, transaction, upload_plan)
    assert readback["hash_match"] is True
    assert readback["ignored_foreign_target_messages"] == 1
    assert transaction.state is module.MissionTransactionState.READBACK_VERIFIED
    assert transaction.commit() == upload_plan.protocol_hash
    permit = transaction.permit_start()
    assert transaction.state is module.MissionTransactionState.START_PERMITTED
    assert permit.to_set_mode_args() == (
        1,
        module.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
        module.PX4_AUTO_MISSION_CUSTOM_MODE,
    )
    assert [name for name, _args in readback_connection.mav.calls] == [
        "MISSION_REQUEST_LIST",
        *("MISSION_REQUEST_INT" for _ in range(upload_plan.item_count)),
    ]


def test_legacy_mission_request_denies_transaction_before_any_item_send() -> None:
    module = _module()
    _docs, transaction, upload_plan = _transaction(module)
    connection = _Connection(_upload_messages(upload_plan, legacy_sequence=0))

    with pytest.raises(Px4MissionError, match="MISSION_REQUEST_INT"):
        module.upload_int_only_mission(connection, transaction, upload_plan)
    assert transaction.state is module.MissionTransactionState.DENIED
    assert [name for name, _args in connection.mav.calls] == ["MISSION_COUNT"]


def test_readback_coordinate_mismatch_denies_before_commit_or_permit() -> None:
    module = _module()
    _docs, transaction, upload_plan = _transaction(module)
    module.upload_int_only_mission(
        _Connection(_upload_messages(upload_plan)), transaction, upload_plan
    )

    with pytest.raises(Px4MissionError, match="protocol hash mismatch"):
        module.readback_int_only_mission(
            _Connection(_readback_messages(upload_plan, change_sequence=2)),
            transaction,
            upload_plan,
        )
    assert transaction.state is module.MissionTransactionState.DENIED
    with pytest.raises(Px4MissionError, match="does not permit"):
        transaction.commit()


def _container_record(module: ModuleType) -> dict[str, Any]:
    return {
        "Id": CONTAINER_ID,
        "Name": "/combined-sim-test",
        "Config": {
            "Image": module.PINNED_IMAGE,
            "Labels": {"multidetect.purpose": module.EXPECTED_CONTAINER_LABEL},
            "Env": [
                "PX4_SIM_MODEL=gz_standard_vtol_mono_cam",
                "PX4_SYS_AUTOSTART=4004",
            ],
            "Cmd": ["-c", "mavlink start -x -u 18570 -o 18774"],
        },
        "HostConfig": {
            "Privileged": False,
            "NetworkMode": "bridge",
            "Devices": [],
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "PortBindings": {"18570/udp": [{"HostIp": "127.0.0.1", "HostPort": "18774"}]},
        },
        "State": {"Running": True},
    }


def _telemetry_session(module, monkeypatch):
    class Connection:
        closed_count = 0

        def close(self):
            self.closed_count += 1

    connection = Connection()
    monkeypatch.setattr(module.mavutil, "mavlink_connection", lambda *a, **k: connection)
    return module.MissionTelemetrySession(container_id=CONTAINER_ID, evaluator_port=19100)


def test_session_reuses_collector_clock_and_closes_only_at_owner_boundary(monkeypatch):
    module = _module()
    session = _telemetry_session(module, monkeypatch)
    collector = session.collector
    origin = collector.started
    with session:
        for _ in range(2):
            session.acquire(container_id=CONTAINER_ID, endpoint=session.endpoint)
            assert session.collector is collector
            assert collector.started == origin
            session.release(succeeded=True)
            assert session.connection.closed_count == 0
    assert session.missions_started == 2
    assert session.connection.closed_count == 1
    session.close()
    assert session.connection.closed_count == 1


@pytest.mark.parametrize("failure", ["container", "endpoint", "active", "failed", "closed"])
def test_session_rejects_invalid_reuse(monkeypatch, failure):
    module = _module()
    session = _telemetry_session(module, monkeypatch)
    container_id, endpoint = CONTAINER_ID, session.endpoint
    if failure == "container":
        container_id = HASH_A
    elif failure == "endpoint":
        endpoint = "udpin:127.0.0.1:19101"
    elif failure in ("active", "failed"):
        session.acquire(container_id=container_id, endpoint=endpoint)
        if failure == "failed":
            session.release(succeeded=False)
    else:
        session.close()
    try:
        with pytest.raises(module.VtolMissionAcceptanceError):
            session.acquire(container_id=container_id, endpoint=endpoint)
    finally:
        session.close()


@pytest.mark.parametrize("selection_rejected", [False, True])
def test_failed_mission_leaves_session_connection_for_owner_cleanup(
    monkeypatch, tmp_path, selection_rejected
):
    module = _module()
    session = _telemetry_session(module, monkeypatch)
    monkeypatch.setattr(session.collector, "pump", lambda timeout: None)
    monkeypatch.setattr(module, "_container_inspect", lambda *a: _container_record(module))

    def no_heartbeat(timeout):
        if selection_rejected:
            raise module.TaskSelectionRejected(
                _documents(module).context,
                [{"task_id": "fixture-task", "reason": "fixture insufficient energy"}],
            )
        raise RuntimeError("fixture heartbeat failure")

    session.collector.wait_for_heartbeat = no_heartbeat
    try:
        failure = pytest.raises(
            SystemExit,
            module.run_mission_acceptance,
            MANIFEST,
            tmp_path / "mission.json",
            docker="unused",
            container_name="combined-sim-test",
            expected_container_id=CONTAINER_ID,
            expected_container_label=module.EXPECTED_CONTAINER_LABEL,
            evaluator_port=19100,
            airframe_profile_sha256=HASH_A,
            runtime_descriptor_sha256=HASH_B,
            runtime_provenance_sha256=HASH_C,
            mission_active_ready_file=None,
            mission_timeout_s=120,
            source_system=250,
            source_component=191,
            acknowledge_owned_disposable_sitl=True,
            telemetry_session=session,
        )
        assert failure.value.code == 1
        report = json.loads((tmp_path / "mission.json").read_text(encoding="utf-8"))
        assert report["gate_passed"] is False
        if selection_rejected:
            assert "TaskSelectionRejected" in report["failure"]
            evidence = report["selection_rejection"]
            restored = module.ContextSnapshot.from_mapping(evidence["context"])
            assert restored.canonical_hash == evidence["context_sha256"]
            assert evidence["rejected"] == [
                {"task_id": "fixture-task", "reason": "fixture insufficient energy"}
            ]
        else:
            assert "fixture heartbeat failure" in report["failure"]
            assert report["selection_rejection"] is None
        assert report["telemetry_session"]["externally_owned"] is True
        assert session.failed and not session.active
        assert session.connection.closed_count == 0
    finally:
        session.close()
    assert session.connection.closed_count == 1


def test_report_publication_keeps_pumping_and_holds_mission_lease(monkeypatch, tmp_path):
    module = _module()
    session = _telemetry_session(module, monkeypatch)
    session.acquire(container_id=CONTAINER_ID, endpoint=session.endpoint)
    pumped = threading.Event()
    caller = threading.get_ident()
    recorded = []

    def pump(timeout):
        assert session.active
        assert threading.get_ident() == caller
        with pytest.raises(module.VtolMissionAcceptanceError):
            session.acquire(container_id=CONTAINER_ID, endpoint=session.endpoint)
        recorded.append(timeout)
        pumped.set()

    def write(path, payload, *, overwrite):
        assert threading.get_ident() != caller
        assert pumped.wait(2)
        assert overwrite is False
        path.write_text(json.dumps(payload), encoding="utf-8")

    session.collector.pump = pump
    monkeypatch.setattr(module, "_atomic_write_json", write)
    try:
        path = tmp_path / "report.json"
        session.publish_completed(path, {"gate_passed": True})
        assert recorded
        assert json.loads(path.read_text()) == {"gate_passed": True}
        assert not session.active and not session.failed
        assert session.connection.closed_count == 0
    finally:
        session.close()


@pytest.mark.parametrize("source", ["write", "receiver"])
def test_publication_failure_blocks_next_mission(monkeypatch, tmp_path, source):
    module = _module()
    session = _telemetry_session(module, monkeypatch)
    session.acquire(container_id=CONTAINER_ID, endpoint=session.endpoint)
    pumped = threading.Event()
    finished = threading.Event()

    def pump(timeout):
        pumped.set()
        if source == "receiver":
            raise RuntimeError("receiver failure")

    def write(*args, **kwargs):
        assert pumped.wait(2)
        finished.set()
        if source == "write":
            raise OSError("write failure")

    session.collector.pump = pump
    monkeypatch.setattr(module, "_atomic_write_json", write)
    try:
        with pytest.raises((RuntimeError, OSError), match="failure"):
            session.publish_completed(tmp_path / "report.json", {"gate_passed": True})
        assert finished.is_set()
        assert session.failed and not session.active
        with pytest.raises(module.VtolMissionAcceptanceError):
            session.acquire(container_id=CONTAINER_ID, endpoint=session.endpoint)
    finally:
        session.close()


def test_explicit_session_history_capacity_and_overflow_are_evidenced(monkeypatch):
    module = _module()
    session = _telemetry_session(module, monkeypatch)
    try:
        assert session.collector.energy_input_samples.maxlen == 65536
        assert module.TelemetryCollector(object()).energy_input_samples.maxlen == 8192
        collector = module.TelemetryCollector(object(), energy_history_limit=2)
        for boot in (100, 200, 300):
            collector._record(_Message("SYSTEM_TIME", time_boot_ms=boot))
        history = collector.post_mission_observation(now_s=collector.started)[
            "energy_input_history"
        ]
        assert history["capacity"] == 2
        assert history["samples_seen"] == 3
        assert history["samples_dropped"] == 1
        assert [s["value"] for s in history["samples"]] == [200, 300]
    finally:
        session.close()


@pytest.mark.parametrize("limit", [True, 0, -1, 131073, 2.5])
def test_invalid_session_capacity_rejected_before_opening_connection(monkeypatch, limit):
    module = _module()

    def unexpected(*args, **kwargs):
        pytest.fail("invalid capacity opened a connection")

    monkeypatch.setattr(module.mavutil, "mavlink_connection", unexpected)
    with pytest.raises(module.VtolMissionAcceptanceError):
        module.MissionTelemetrySession(
            container_id=CONTAINER_ID, evaluator_port=19100, energy_history_limit=limit
        )
    with pytest.raises(RuntimeError, match="energy history limit"):
        module.TelemetryCollector(object(), energy_history_limit=limit)


def test_container_gate_requires_exact_id_label_and_loopback_without_14550() -> None:
    module = _module()
    record = _container_record(module)
    checks = module.validate_owned_container_record(
        record,
        container_name="combined-sim-test",
        expected_container_id=CONTAINER_ID,
        expected_label=module.EXPECTED_CONTAINER_LABEL,
    )
    assert all(checks.values())

    record["HostConfig"]["PortBindings"]["18570/udp"][0]["HostIp"] = "0.0.0.0"
    with pytest.raises(module.VtolMissionAcceptanceError, match="loopback_only"):
        module.validate_owned_container_record(
            record,
            container_name="combined-sim-test",
            expected_container_id=CONTAINER_ID,
            expected_label=module.EXPECTED_CONTAINER_LABEL,
        )


def test_ready_json_is_atomic_refuses_stale_file_and_keeps_visual_boundary(
    tmp_path: Path,
) -> None:
    module = _module()
    ready = tmp_path / "mission-active.ready.json"
    payload = {
        "transaction_state": "start_permitted",
        "auto_mission_confirmed": True,
        "armed_after_permit": True,
        "mission_activity": {"message_type": "MISSION_CURRENT", "sequence": 0},
        "operator_visual_acceptance": "not_claimed",
    }
    module._atomic_write_json(ready, payload, overwrite=False)
    assert ready.is_file()
    assert not list(tmp_path.glob("*.tmp"))
    assert module.json.loads(ready.read_text(encoding="utf-8")) == payload
    with pytest.raises(module.VtolMissionAcceptanceError, match="stale ready"):
        module._atomic_write_json(ready, payload, overwrite=False)


@pytest.mark.parametrize("high_resolution", [False, True])
def test_observer_writes_ready_only_after_armed_activity_and_proves_full_lifecycle(
    tmp_path: Path,
    high_resolution,
) -> None:
    module = _module()

    class FakeFlightCollector:
        def __init__(self) -> None:
            if high_resolution:
                self.clock = module.HighResolutionSystemClock(session_id="flight-return-regression")
                self.now_s = self.clock.monotonic
            observed_at = module._mission_now(self)
            self.last_position = {
                "latitude_deg": 1.3,
                "longitude_deg": 103.8,
                "relative_altitude_m": 0.0,
            }
            self.last_landed_state = module.mavutil.mavlink.MAV_LANDED_STATE_ON_GROUND
            self.last_vtol_state = module.MAV_VTOL_STATE_MC
            self.last_heartbeat = {
                "base_mode": (
                    module.mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
                    | module.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
                ),
                "custom_mode": module.PX4_AUTO_MISSION_CUSTOM_MODE,
            }
            self.last_heartbeat_observed_at_monotonic_s = observed_at
            self.last_position_observed_at_monotonic_s = observed_at
            self.last_extended_state_observed_at_monotonic_s = observed_at
            self._steps = iter(
                [
                    (
                        {},
                        _Message("MISSION_CURRENT", seq=0),
                    ),
                    (
                        {},
                        _Message(
                            "GLOBAL_POSITION_INT",
                            time_boot_ms=1234,
                            lat=13000000,
                            lon=1038000000,
                            relative_alt=2500,
                            vx=1,
                            vy=2,
                            vz=30,
                        ),
                    ),
                    (
                        {},
                        _Message(
                            "ATTITUDE",
                            time_boot_ms=1235,
                            roll=0.25,
                            pitch=0,
                            yaw=1,
                            rollspeed=0,
                            pitchspeed=0,
                            yawspeed=0,
                        ),
                    ),
                    ({}, _Message("VFR_HUD", airspeed=15, groundspeed=16, alt=20, climb=0)),
                    (
                        {},
                        _Message(
                            "NAV_CONTROLLER_OUTPUT",
                            nav_roll=30,
                            nav_pitch=2,
                            alt_error=-1,
                            aspd_error=0.5,
                            xtrack_error=3,
                        ),
                    ),
                    (
                        {
                            "last_landed_state": module.mavutil.mavlink.MAV_LANDED_STATE_IN_AIR,
                            "last_vtol_state": module.MAV_VTOL_STATE_FW,
                            "last_position": {
                                "latitude_deg": 1.30063,
                                "longitude_deg": 103.8,
                                "relative_altitude_m": 20.0,
                            },
                        },
                        _Message("EXTENDED_SYS_STATE"),
                    ),
                    (
                        {"last_vtol_state": module.MAV_VTOL_STATE_MC},
                        _Message("EXTENDED_SYS_STATE", vtol_state=module.MAV_VTOL_STATE_MC),
                    ),
                    ({}, _Message("MISSION_CURRENT", seq=6)),
                    (
                        {
                            "last_landed_state": module.mavutil.mavlink.MAV_LANDED_STATE_ON_GROUND,
                            "last_heartbeat": {"base_mode": 0},
                        },
                        _Message("MISSION_ITEM_REACHED", seq=6),
                    ),
                ]
            )

        def pump(self, _timeout_s: float) -> _Message | None:
            try:
                changes, message = next(self._steps)
            except StopIteration:
                return None
            for name, value in changes.items():
                setattr(self, name, value)
            return message

    ready = tmp_path / "active.json"
    events: list[dict[str, Any]] = []
    shadow_evidence: dict[str, Any] = {}
    shadow_mission = _documents(module).compiled.executable_mission
    collector = FakeFlightCollector()
    result = module.observe_mission_flight(
        collector,
        executable_mission=shadow_mission,
        outcome_evidence=shadow_evidence,
        item_count=7,
        home_latitude_deg=1.3,
        home_longitude_deg=103.8,
        mission_timeout_s=10.0,
        mission_active_ready_file=ready,
        container_id=CONTAINER_ID,
        transaction_state="start_permitted",
        control_command_events=events,
    )

    assert result["host_clock_identity"] == module._mission_clock_identity(collector)
    assert result["mission_current_sequences"] == [0, 6]
    assert shadow_evidence["mode"] == "shadow_receive_only"
    assert shadow_evidence["affects_acceptance_gate"] is False
    assert shadow_evidence["outcome"]["mission_hash"] == shadow_mission.canonical_hash
    # This legacy fixture updates collector caches, but omits landed_state on
    # the actual wire message. Shadow must not inherit that cached success.
    assert shadow_evidence["outcome"]["status"] == "failed"
    assert shadow_evidence["events"][0]["message"]["message_type"] == "MISSION_CURRENT"
    positions = result["phase_position_observations"]
    assert result["trajectory_tracking_quality_proven"] is False
    assert result["acceptance_scope"] == "mission_lifecycle_and_capture_not_tracking_error_bounds"
    assert positions["samples_seen"] == 1
    assert positions["truncated"] is False
    assert positions["samples"][0]["time_boot_ms"] == 1234
    assert positions["samples"][0]["vz"] == 30
    dynamics = result["phase_dynamics_observations"]
    assert dynamics["streams"]["ATTITUDE"]["samples_seen"] == 1
    assert dynamics["streams"]["ATTITUDE"]["samples"][0]["roll"] == 0.25
    assert dynamics["streams"]["VFR_HUD"]["samples_seen"] == 1
    assert dynamics["streams"]["VFR_HUD"]["samples"][0]["airspeed"] == 15
    assert "time_boot_ms" not in dynamics["streams"]["VFR_HUD"]["samples"][0]
    navigation = dynamics["streams"]["NAV_CONTROLLER_OUTPUT"]
    assert navigation["samples_seen"] == 1
    assert navigation["samples"][0]["nav_roll"] == 30
    assert navigation["samples"][0]["xtrack_error"] == 3
    assert "time_boot_ms" not in navigation["samples"][0]
    assert dynamics["true_airspeed_semantics_proven"] is False
    assert any(
        event["message"].get("vtol_state") == module.MAV_VTOL_STATE_MC
        for event in shadow_evidence["events"]
    )
    assert any(not event["accepted"] for event in shadow_evidence["events"])
    assert result["mission_item_reached_sequences"] == [6]
    assert result["maximum_distance_from_home_m"] > 35.0
    assert result["maximum_relative_altitude_m"] == 20.0
    assert result["in_air_observed"] is True
    assert result["fixed_wing_observed"] is True
    assert result["multicopter_observed"] is True
    assert result["landed_observed"] is True
    assert result["terminal_disarmed"] is True
    assert events == []
    ready_payload = module.json.loads(ready.read_text(encoding="utf-8"))
    assert ready_payload["transaction_state"] == "start_permitted"
    assert ready_payload["auto_mission_confirmed"] is True
    assert ready_payload["auto_mission_active"] is True
    assert ready_payload["auto_mission_custom_mode"] == module.PX4_AUTO_MISSION_CUSTOM_MODE
    assert ready_payload["armed_after_permit"] is True
    assert ready_payload["in_air_observed"] is True
    assert ready_payload["in_air_at_ready"] is True
    assert ready_payload["mission_activity"]["message_type"] == "MISSION_CURRENT"
    assert ready_payload["mission_activity"]["sequence"] == 0
    assert ready_payload["mission_activity"]["at_s"] >= 0.0
    assert ready_payload["mission_progress"]["observed"] is True
    assert ready_payload["mission_progress"]["current_sequences"] == [0]
    assert ready_payload["operator_visual_acceptance"] == "not_claimed"


def test_observer_never_writes_active_ready_for_ground_arm_and_activity(
    tmp_path: Path,
) -> None:
    module = _module()

    class GroundOnlyCollector:
        def __init__(self) -> None:
            self.last_position = {
                "latitude_deg": 1.3,
                "longitude_deg": 103.8,
                "relative_altitude_m": 0.0,
            }
            self.last_landed_state = module.mavutil.mavlink.MAV_LANDED_STATE_ON_GROUND
            self.last_vtol_state = module.MAV_VTOL_STATE_MC
            self.last_heartbeat = {
                "base_mode": (
                    module.mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
                    | module.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
                ),
                "custom_mode": module.PX4_AUTO_MISSION_CUSTOM_MODE,
            }
            self._activity_sent = False

        def pump(self, _timeout_s: float) -> _Message | None:
            if self._activity_sent:
                return None
            self._activity_sent = True
            return _Message("MISSION_CURRENT", seq=0)

    ready = tmp_path / "ground-must-not-be-ready.json"
    with pytest.raises(module.VtolMissionAcceptanceError, match="did not prove progress"):
        module.observe_mission_flight(
            GroundOnlyCollector(),
            item_count=7,
            home_latitude_deg=1.3,
            home_longitude_deg=103.8,
            mission_timeout_s=0.01,
            mission_active_ready_file=ready,
            container_id=CONTAINER_ID,
            transaction_state="start_permitted",
            control_command_events=[],
        )
    assert not ready.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("transaction_state", "committed"),
        ("mission_progress_observed", False),
        ("auto_mission_active", False),
        ("armed", False),
        ("in_air_at_ready", False),
        ("fixed_wing_observed", False),
        ("current_vtol_state", None),
        ("maximum_distance_m", 34.999),
        ("maximum_altitude_m", 9.999),
        ("telemetry_fresh", False),
    ],
)
def test_flight_capture_ready_gate_requires_every_strict_flight_fact(
    field: str,
    value: Any,
) -> None:
    module = _module()
    facts: dict[str, Any] = {
        "transaction_state": "start_permitted",
        "mission_progress_observed": True,
        "auto_mission_active": True,
        "armed": True,
        "in_air_at_ready": True,
        "fixed_wing_observed": True,
        "current_vtol_state": module.MAV_VTOL_STATE_FW,
        "maximum_distance_m": 35.0,
        "maximum_altitude_m": 10.0,
        "telemetry_fresh": True,
    }
    assert module._flight_capture_ready_gate(**facts) is True

    facts[field] = value
    assert module._flight_capture_ready_gate(**facts) is False


def test_flight_capture_ready_rejects_stale_or_aliased_target_before_flight(
    tmp_path: Path,
) -> None:
    module = _module()
    stale = tmp_path / "stale-flight-capture.json"
    stale.write_text('{"sentinel":"preserve"}\n', encoding="utf-8")

    with pytest.raises(module.VtolMissionAcceptanceError, match="stale flight-capture"):
        module._validate_ready_file_targets(None, stale)
    assert stale.read_text(encoding="utf-8") == '{"sentinel":"preserve"}\n'

    target = tmp_path / "same-target.json"
    with pytest.raises(module.VtolMissionAcceptanceError, match="must be distinct"):
        module._validate_ready_file_targets(target, target)
    assert not target.exists()

    complete = tmp_path / "stale-complete.json"
    complete.write_text('{"sentinel":"preserve"}\n', encoding="utf-8")
    with pytest.raises(module.VtolMissionAcceptanceError, match="stale flight-capture complete"):
        module._validate_ready_file_targets(None, tmp_path / "ready.json", complete)
    assert complete.read_text(encoding="utf-8") == '{"sentinel":"preserve"}\n'


def test_campaign_capture_binding_descriptor_and_deadline_are_fail_closed() -> None:
    module = _module()
    binding = {
        "schema": "multidetect.vtol-campaign-flight-capture-binding",
        "schema_version": 1,
        "task_id": "synthetic-east",
        "context_sha256": HASH_A,
        "plan_sha256": HASH_B,
        "executable_mission_sha256": HASH_C,
        "wire_hash": HASH_A,
        "airframe_profile_sha256": HASH_B,
        "transaction_id": None,
        "transaction_binding": "ready_marker_and_mission_evidence",
        "policy_applied_to_actuator": False,
        "truth_free": True,
    }
    assert all(module.validate_flight_capture_binding_descriptor(binding).values())
    invalid_binding = dict(binding, schema_version=True)
    with pytest.raises(module.VtolMissionAcceptanceError, match="schema_version"):
        module.validate_flight_capture_binding_descriptor(invalid_binding)

    complete = {
        "schema": "multidetect.vtol-sensor-capture-complete",
        "schema_version": 1,
        "clock": "host_monotonic",
        "capture_started_at_monotonic_s": 10.0,
        "capture_completed_at_monotonic_s": 15.0,
        "capture_deadline_monotonic_s": 20.0,
        "container_id": CONTAINER_ID,
        "transaction_id": "tx-binding",
        "committed_protocol_sha256": HASH_A,
        "scenario_id": "river_rescue_vtol_001",
        "scenario_manifest_hash": HASH_B,
        "frame_count": 3,
        "sensor_binding_hash": HASH_B,
        "frame_set_manifest_sha256": HASH_C,
        "ground_truth_used_for_runtime_or_capture": False,
        "task_id": binding["task_id"],
        "plan_sha256": binding["plan_sha256"],
        "executable_mission_sha256": binding["executable_mission_sha256"],
        "wire_hash": binding["wire_hash"],
    }
    checks = module.validate_flight_capture_complete_marker(
        complete,
        flight_ready_monotonic_s=5.0,
        container_id=CONTAINER_ID,
        transaction_id="tx-binding",
        committed_protocol_sha256=HASH_A,
        scenario_id="river_rescue_vtol_001",
        scenario_manifest_hash=HASH_B,
        expected_task_binding=binding,
        capture_deadline_monotonic_s=20.0,
    )
    assert all(checks.values())
    invalid_schema = dict(complete, schema_version=True)
    with pytest.raises(module.VtolMissionAcceptanceError, match="schema_version"):
        module.validate_flight_capture_complete_marker(
            invalid_schema,
            flight_ready_monotonic_s=5.0,
            container_id=CONTAINER_ID,
            transaction_id="tx-binding",
            committed_protocol_sha256=HASH_A,
            scenario_id="river_rescue_vtol_001",
            scenario_manifest_hash=HASH_B,
            expected_task_binding=binding,
            capture_deadline_monotonic_s=20.0,
        )
    late = dict(complete)
    late["capture_completed_at_monotonic_s"] = 21.0
    with pytest.raises(module.VtolMissionAcceptanceError, match="capture_deadline"):
        module.validate_flight_capture_complete_marker(
            late,
            flight_ready_monotonic_s=5.0,
            container_id=CONTAINER_ID,
            transaction_id="tx-binding",
            committed_protocol_sha256=HASH_A,
            scenario_id="river_rescue_vtol_001",
            scenario_manifest_hash=HASH_B,
            expected_task_binding=binding,
            capture_deadline_monotonic_s=20.0,
        )
    tampered = dict(binding, task_id="other-task")
    with pytest.raises(module.VtolMissionAcceptanceError, match="task_binding"):
        module.validate_flight_capture_complete_marker(
            complete,
            flight_ready_monotonic_s=5.0,
            container_id=CONTAINER_ID,
            transaction_id="tx-binding",
            committed_protocol_sha256=HASH_A,
            scenario_id="river_rescue_vtol_001",
            scenario_manifest_hash=HASH_B,
            expected_task_binding=tampered,
            capture_deadline_monotonic_s=20.0,
        )


@pytest.mark.parametrize(
    ("ready", "complete"),
    (
        ("ready.json", None),
        (None, "complete.json"),
    ),
)
def test_flight_capture_ready_and_complete_targets_are_required_as_a_pair(
    tmp_path: Path,
    ready: str | None,
    complete: str | None,
) -> None:
    module = _module()

    with pytest.raises(module.VtolMissionAcceptanceError, match="supplied together"):
        module._validate_ready_file_targets(
            None,
            tmp_path / ready if ready is not None else None,
            tmp_path / complete if complete is not None else None,
        )


@pytest.mark.parametrize("high_resolution", [False, True])
def test_observer_writes_strict_flight_capture_ready_once_with_bound_facts(
    tmp_path: Path,
    high_resolution: bool,
) -> None:
    module = _module()
    mission_active_ready = tmp_path / "mission-active.json"
    flight_capture_ready = tmp_path / "flight-capture.json"
    flight_capture_complete = tmp_path / "flight-capture-complete.json"

    class StrictFlightCollector:
        def __init__(self) -> None:
            if high_resolution:
                self.clock = module.HighResolutionSystemClock(session_id="capture-observer")
            observed_at = self.now_s()
            self.last_position = {
                "latitude_deg": 1.3,
                "longitude_deg": 103.8,
                "relative_altitude_m": 0.0,
            }
            self.last_landed_state = module.mavutil.mavlink.MAV_LANDED_STATE_ON_GROUND
            self.last_vtol_state = module.MAV_VTOL_STATE_MC
            self.last_heartbeat = {
                "base_mode": (
                    module.mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
                    | module.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
                ),
                "custom_mode": module.PX4_AUTO_MISSION_CUSTOM_MODE,
            }
            self.last_heartbeat_observed_at_monotonic_s = observed_at
            self.last_position_observed_at_monotonic_s = observed_at
            self.last_extended_state_observed_at_monotonic_s = observed_at
            self.calls = 0
            self.flight_ready_bytes: bytes | None = None
            self._steps = iter(
                [
                    ({}, _Message("MISSION_CURRENT", seq=0)),
                    (
                        {
                            "last_landed_state": module.mavutil.mavlink.MAV_LANDED_STATE_IN_AIR,
                            "last_vtol_state": module.MAV_VTOL_STATE_FW,
                            "last_position": {
                                "latitude_deg": 1.3002,
                                "longitude_deg": 103.8,
                                "relative_altitude_m": 10.0,
                            },
                        },
                        _Message("EXTENDED_SYS_STATE"),
                    ),
                    (
                        {
                            "last_position": {
                                "latitude_deg": 1.3004,
                                "longitude_deg": 103.8,
                                "relative_altitude_m": 10.0,
                            }
                        },
                        _Message("GLOBAL_POSITION_INT"),
                    ),
                    (
                        {
                            "last_position": {
                                "latitude_deg": 1.30063,
                                "longitude_deg": 103.8,
                                "relative_altitude_m": 20.0,
                            }
                        },
                        _Message("GLOBAL_POSITION_INT"),
                    ),
                    (
                        {"last_vtol_state": module.MAV_VTOL_STATE_MC},
                        _Message("EXTENDED_SYS_STATE"),
                    ),
                    ({}, _Message("MISSION_CURRENT", seq=6)),
                    (
                        {
                            "last_landed_state": module.mavutil.mavlink.MAV_LANDED_STATE_ON_GROUND,
                            "last_heartbeat": {"base_mode": 0},
                        },
                        _Message("MISSION_ITEM_REACHED", seq=6),
                    ),
                ]
            )

        def now_s(self):
            return self.clock.monotonic() if high_resolution else module.time.monotonic()

        def pump(self, _timeout_s: float) -> _Message | None:
            self.calls += 1
            if self.calls == 3:
                assert mission_active_ready.is_file()
                assert not flight_capture_ready.exists()
            if self.calls == 4:
                assert flight_capture_ready.is_file()
                self.flight_ready_bytes = flight_capture_ready.read_bytes()
                ready_payload = module.json.loads(flight_capture_ready.read_text(encoding="utf-8"))
                ready_monotonic_s = float(ready_payload["captured_at_monotonic_s"])
                capture_completed_monotonic_s = self.now_s()
                module._atomic_write_json(
                    flight_capture_complete,
                    {
                        "schema": "multidetect.vtol-sensor-capture-complete",
                        "schema_version": 1,
                        "generated_at_utc": "2026-09-05T00:00:00Z",
                        "clock": module._mission_clock_label(self),
                        "host_clock_identity": module._mission_clock_identity(self),
                        "capture_started_at_monotonic_s": ready_monotonic_s,
                        "capture_completed_at_monotonic_s": capture_completed_monotonic_s,
                        "container_id": CONTAINER_ID,
                        "transaction_id": "tx-v56-flight-capture",
                        "committed_protocol_sha256": HASH_A,
                        "scenario_id": "river_rescue_vtol_001",
                        "scenario_manifest_hash": HASH_B,
                        "frame_count": 3,
                        "sensor_binding_hash": HASH_B,
                        "frame_set_manifest_sha256": HASH_C,
                        "ground_truth_used_for_runtime_or_capture": False,
                    },
                    overwrite=False,
                )
            if self.calls > 4 and self.flight_ready_bytes is not None:
                assert flight_capture_ready.read_bytes() == self.flight_ready_bytes
            try:
                changes, message = next(self._steps)
            except StopIteration:
                return None
            for name, value in changes.items():
                setattr(self, name, value)
            return message

    collector = StrictFlightCollector()
    transaction_id = "tx-v56-flight-capture"
    result = module.observe_mission_flight(
        collector,
        item_count=7,
        home_latitude_deg=1.3,
        home_longitude_deg=103.8,
        mission_timeout_s=10.0,
        mission_active_ready_file=mission_active_ready,
        container_id=CONTAINER_ID,
        transaction_state="start_permitted",
        control_command_events=[],
        flight_capture_ready_file=flight_capture_ready,
        flight_capture_complete_file=flight_capture_complete,
        transaction_id=transaction_id,
        committed_protocol_sha256=HASH_A,
        scenario_id="river_rescue_vtol_001",
        scenario_manifest_hash=HASH_B,
    )

    assert result["mission_active_ready_written"] is True
    assert result["flight_capture_ready_written"] is True
    assert result["flight_capture_complete_observed"] is True
    timing = result["loop_timing"]
    assert timing["host_clock_identity"] == module._mission_clock_identity(collector)
    assert timing["phases"]["pump"]["count"] == collector.calls
    assert timing["phases"]["shadow"]["count"] == collector.calls
    assert timing["phases"]["other"]["count"] == collector.calls
    assert all(
        row["elapsed_s"] >= 0 for phase in timing["phases"].values() for row in phase["slowest"]
    )
    assert result["flight_capture_complete_file"] == str(flight_capture_complete)
    assert (
        result["flight_capture_complete_file_sha256"]
        == hashlib.sha256(flight_capture_complete.read_bytes()).hexdigest()
    )
    assert all(result["flight_capture_complete_checks"].values())
    assert result["flight_capture_interval_sample_count"] > 0
    assert result["flight_capture_interval_violation"] is False
    assert result["flight_capture_interval_qualified"] is True
    assert all(
        sample["maximum_state_age_s"] == module.MAXIMUM_FLIGHT_STATE_AGE_S
        and 0.0 <= sample["heartbeat_age_s"] <= module.MAXIMUM_FLIGHT_STATE_AGE_S
        and 0.0 <= sample["position_age_s"] <= module.MAXIMUM_FLIGHT_STATE_AGE_S
        and 0.0 <= sample["extended_system_state_age_s"] <= module.MAXIMUM_FLIGHT_STATE_AGE_S
        and sample["telemetry_fresh"] is True
        and sample["valid"] is True
        for sample in result["flight_capture_interval_samples"]
    )
    assert collector.flight_ready_bytes == flight_capture_ready.read_bytes()
    payload = module.json.loads(flight_capture_ready.read_text(encoding="utf-8"))
    assert payload["schema"] == "multidetect.px4-vtol-flight-capture-ready"
    assert payload["container"] == {"exact_id_bound": True, "id": CONTAINER_ID}
    assert payload["transaction"] == {
        "committed_protocol_sha256": HASH_A,
        "id": transaction_id,
        "start_permitted": True,
        "state": "start_permitted",
    }
    assert payload["flight"]["auto_mission_active"] is True
    assert payload["flight"]["auto_mission_custom_mode"] == (module.PX4_AUTO_MISSION_CUSTOM_MODE)
    assert payload["flight"]["armed"] is True
    assert payload["flight"]["in_air_at_ready"] is True
    assert payload["flight"]["fixed_wing_observed"] is True
    assert payload["flight"]["maximum_distance_from_home_m"] >= 35.0
    assert payload["flight"]["maximum_relative_altitude_m"] >= 10.0
    assert payload["flight"]["mission_progress"]["observed"] is True
    assert payload["flight"]["mission_progress"]["current_sequences"] == [0]
    assert payload["flight"]["mission_progress"]["first_activity"]["at_s"] >= 0.0
    assert "captured_at_s" not in payload["flight"]["mission_progress"]["first_activity"]
    assert payload["flight"]["telemetry_fresh"] is True
    assert payload["flight"]["maximum_telemetry_age_s"] == module.MAXIMUM_FLIGHT_STATE_AGE_S
    assert all(
        0.0 <= age <= module.MAXIMUM_FLIGHT_STATE_AGE_S
        for age in payload["flight"]["telemetry_ages_s"].values()
    )
    assert (
        payload["flight"]["maximum_distance_from_home_m"] < (result["maximum_distance_from_home_m"])
    )
    assert payload["operator_visual_acceptance"] == "not_claimed"


def test_observer_rejects_valid_complete_marker_after_frozen_telemetry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    flight_capture_ready = tmp_path / "flight-capture-ready.json"
    flight_capture_complete = tmp_path / "flight-capture-complete.json"

    class FakeClock:
        def __init__(self) -> None:
            self.value = 100.0

        def monotonic(self) -> float:
            return self.value

    clock = FakeClock()
    monkeypatch.setattr(module.time, "monotonic", clock.monotonic)

    class FrozenCollector:
        def __init__(self) -> None:
            self.last_position = {
                "latitude_deg": 1.3,
                "longitude_deg": 103.8,
                "relative_altitude_m": 0.0,
            }
            self.last_landed_state = module.mavutil.mavlink.MAV_LANDED_STATE_ON_GROUND
            self.last_vtol_state = module.MAV_VTOL_STATE_MC
            self.last_heartbeat = {
                "base_mode": (
                    module.mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
                    | module.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
                ),
                "custom_mode": module.PX4_AUTO_MISSION_CUSTOM_MODE,
            }
            self.last_heartbeat_observed_at_monotonic_s = clock.value
            self.last_position_observed_at_monotonic_s = clock.value
            self.last_extended_state_observed_at_monotonic_s = clock.value
            self.calls = 0

        def pump(self, _timeout_s: float) -> _Message | None:
            self.calls += 1
            if self.calls == 1:
                return _Message("MISSION_CURRENT", seq=0)
            if self.calls == 2:
                self.last_position = {
                    "latitude_deg": 1.30063,
                    "longitude_deg": 103.8,
                    "relative_altitude_m": 20.0,
                }
                self.last_landed_state = module.mavutil.mavlink.MAV_LANDED_STATE_IN_AIR
                self.last_vtol_state = module.MAV_VTOL_STATE_FW
                self.last_extended_state_observed_at_monotonic_s = clock.value
                return _Message("EXTENDED_SYS_STATE")
            if self.calls == 3:
                ready_payload = module.json.loads(flight_capture_ready.read_text(encoding="utf-8"))
                ready_time = float(ready_payload["captured_at_monotonic_s"])
                module._atomic_write_json(
                    flight_capture_complete,
                    {
                        "schema": "multidetect.vtol-sensor-capture-complete",
                        "schema_version": 1,
                        "generated_at_utc": "2026-09-05T00:00:00Z",
                        "clock": "host_monotonic",
                        "capture_started_at_monotonic_s": ready_time,
                        "capture_completed_at_monotonic_s": ready_time + 1.0,
                        "container_id": CONTAINER_ID,
                        "transaction_id": "tx-v56-frozen-telemetry",
                        "committed_protocol_sha256": HASH_A,
                        "scenario_id": "river_rescue_vtol_001",
                        "scenario_manifest_hash": HASH_B,
                        "frame_count": 3,
                        "sensor_binding_hash": HASH_B,
                        "frame_set_manifest_sha256": HASH_C,
                        "ground_truth_used_for_runtime_or_capture": False,
                    },
                    overwrite=False,
                )
                clock.value = ready_time + module.MAXIMUM_FLIGHT_STATE_AGE_S + 0.1
                return None
            if self.calls == 4:
                self.last_position_observed_at_monotonic_s = clock.value
                return _Message("GLOBAL_POSITION_INT")
            if self.calls == 5:
                self.last_landed_state = module.mavutil.mavlink.MAV_LANDED_STATE_ON_GROUND
                self.last_vtol_state = module.MAV_VTOL_STATE_MC
                self.last_extended_state_observed_at_monotonic_s = clock.value
                return _Message("EXTENDED_SYS_STATE")
            if self.calls == 6:
                self.last_heartbeat = {"base_mode": 0, "custom_mode": 0}
                self.last_heartbeat_observed_at_monotonic_s = clock.value
                return _Message("HEARTBEAT")
            if self.calls == 7:
                return _Message("MISSION_CURRENT", seq=6)
            if self.calls == 8:
                return _Message("MISSION_ITEM_REACHED", seq=6)
            clock.value += 10.0
            return None

    diagnostics = {}
    buffers = {}
    with pytest.raises(
        module.VtolMissionAcceptanceError,
        match="valid flight-capture completion marker.*left current FW/armed/in-air/AUTO.MISSION",
    ):
        module.observe_mission_flight(
            FrozenCollector(),
            capture_diagnostics=diagnostics,
            observation_buffers=buffers,
            item_count=7,
            home_latitude_deg=1.3,
            home_longitude_deg=103.8,
            mission_timeout_s=10.0,
            mission_active_ready_file=None,
            container_id=CONTAINER_ID,
            transaction_state="start_permitted",
            control_command_events=[],
            flight_capture_ready_file=flight_capture_ready,
            flight_capture_complete_file=flight_capture_complete,
            transaction_id="tx-v56-frozen-telemetry",
            committed_protocol_sha256=HASH_A,
            scenario_id="river_rescue_vtol_001",
            scenario_manifest_hash=HASH_B,
        )
    assert flight_capture_ready.is_file()
    assert flight_capture_complete.is_file()
    first = diagnostics["first_invalid_sample"]
    assert first["armed"] and first["auto_mission_active"] and first["in_air"]
    assert first["vtol_state"] == module.MAV_VTOL_STATE_FW
    assert not first["telemetry_fresh"]
    assert first["heartbeat_age_s"] > module.MAXIMUM_FLIGHT_STATE_AGE_S
    assert diagnostics["invalid_sample_count"] > 1
    partial = module.snapshot_failed_observer(buffers)
    assert partial["capture_complete"] is False
    assert partial["training_label_eligible"] is False
    assert partial["phase_position_observations"]["samples_seen"] == 1
    assert partial["phase_position_observations"]["samples"][0]["missing_integer_fields"]
    assert partial["position_summary"]["mission_progress_not_proven"] is True
    assert partial["position_summary"]["valid_position_samples"] == 0
    assert "NAV_CONTROLLER_OUTPUT" in partial["phase_dynamics_observations"]["streams"]
    assert module.snapshot_failed_observer({}) is None
    module.json.dumps(partial, allow_nan=False)
    assert diagnostics["sample_count"] > diagnostics["invalid_sample_count"]
    assert diagnostics["last_sample"] != first
    assert "left current" in diagnostics["last_marker_error"]
    assert not diagnostics["affects_acceptance_gate"]
    assert diagnostics["loop_timing_at_first_invalid"]["phases"]


def test_observer_fails_closed_when_requested_flight_capture_ready_never_occurs(
    tmp_path: Path,
) -> None:
    module = _module()

    class NeverExactAutoCollector:
        def __init__(self) -> None:
            observed_at = module.time.monotonic()
            self.last_position = {
                "latitude_deg": 1.3,
                "longitude_deg": 103.8,
                "relative_altitude_m": 0.0,
            }
            self.last_landed_state = module.mavutil.mavlink.MAV_LANDED_STATE_ON_GROUND
            self.last_vtol_state = module.MAV_VTOL_STATE_MC
            self.last_heartbeat = {
                "base_mode": (
                    module.mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
                    | module.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
                ),
                "custom_mode": module.PX4_AUTO_MISSION_CUSTOM_MODE + 1,
            }
            self.last_heartbeat_observed_at_monotonic_s = observed_at
            self.last_position_observed_at_monotonic_s = observed_at
            self.last_extended_state_observed_at_monotonic_s = observed_at
            self._steps = iter(
                [
                    ({}, _Message("MISSION_CURRENT", seq=0)),
                    (
                        {
                            "last_landed_state": module.mavutil.mavlink.MAV_LANDED_STATE_IN_AIR,
                            "last_vtol_state": module.MAV_VTOL_STATE_FW,
                            "last_position": {
                                "latitude_deg": 1.30063,
                                "longitude_deg": 103.8,
                                "relative_altitude_m": 20.0,
                            },
                        },
                        _Message("EXTENDED_SYS_STATE"),
                    ),
                    (
                        {"last_vtol_state": module.MAV_VTOL_STATE_MC},
                        _Message("EXTENDED_SYS_STATE"),
                    ),
                    ({}, _Message("MISSION_CURRENT", seq=6)),
                    (
                        {
                            "last_landed_state": module.mavutil.mavlink.MAV_LANDED_STATE_ON_GROUND,
                            "last_heartbeat": {"base_mode": 0},
                        },
                        _Message("MISSION_ITEM_REACHED", seq=6),
                    ),
                ]
            )

        def pump(self, _timeout_s: float) -> _Message | None:
            try:
                changes, message = next(self._steps)
            except StopIteration:
                return None
            for name, value in changes.items():
                setattr(self, name, value)
            return message

    ready = tmp_path / "must-not-exist.json"
    complete = tmp_path / "complete-must-not-exist.json"
    with pytest.raises(
        module.VtolMissionAcceptanceError,
        match="without the flight-capture ready barrier",
    ):
        module.observe_mission_flight(
            NeverExactAutoCollector(),
            item_count=7,
            home_latitude_deg=1.3,
            home_longitude_deg=103.8,
            mission_timeout_s=10.0,
            mission_active_ready_file=None,
            container_id=CONTAINER_ID,
            transaction_state="start_permitted",
            control_command_events=[],
            flight_capture_ready_file=ready,
            flight_capture_complete_file=complete,
            transaction_id="tx-v56-never-ready",
            committed_protocol_sha256=HASH_A,
            scenario_id="river_rescue_vtol_001",
            scenario_manifest_hash=HASH_B,
        )
    assert not ready.exists()
    assert not complete.exists()


def test_cli_forwards_paired_flight_capture_ready_and_complete_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    observed: dict[str, Any] = {}

    def fake_run(*args: Any, **kwargs: Any) -> dict[str, Any]:
        observed["args"] = args
        observed["kwargs"] = kwargs
        return {"gate_passed": True}

    monkeypatch.setattr(module, "_resolve_docker_cli", lambda value: f"resolved:{value}")
    monkeypatch.setattr(module, "run_mission_acceptance", fake_run)
    ready = tmp_path / "ready.json"
    complete = tmp_path / "complete.json"
    code = module.main(
        [
            "--scenario-manifest",
            str(MANIFEST),
            "--evidence",
            str(tmp_path / "mission-evidence.json"),
            "--container-name",
            "multidetect-vtol-cli-test",
            "--expected-container-id",
            CONTAINER_ID,
            "--evaluator-port",
            "51005",
            "--airframe-profile-sha256",
            HASH_A,
            "--runtime-descriptor-sha256",
            HASH_B,
            "--runtime-provenance-sha256",
            HASH_C,
            "--flight-capture-ready-file",
            str(ready),
            "--flight-capture-complete-file",
            str(complete),
            "--docker",
            "docker-test",
            "--acknowledge-owned-disposable-sitl",
        ]
    )

    assert code == 0
    assert observed["kwargs"]["docker"] == "resolved:docker-test"
    assert observed["kwargs"]["flight_capture_ready_file"] == ready.resolve()
    assert observed["kwargs"]["flight_capture_complete_file"] == complete.resolve()
    assert observed["kwargs"]["acknowledge_owned_disposable_sitl"] is True


def test_source_has_no_container_lifecycle_or_evaluator_truth_loader() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    lower = text.lower()

    assert "from multidetect.simulation import load_runtime_scenario" in text
    assert "load_scenario_manifest" not in text
    assert '"created_by_this_helper": False' in text
    assert '"stopped_by_this_helper": False' in text
    assert '"operator_visual_acceptance": "not_claimed"' in text
    assert '"real_pixhawk_contacted": False' in text
    assert "docker run" not in lower
    assert "docker stop" not in lower
    assert "docker rm" not in lower
    assert "MISSION_REQUEST_INT" in text
    assert "MISSION_ITEM_INT" in text
    assert "permit_start" in text
    assert "mission_journal.resume_plan().to_mapping()" in text
    assert "PX4_AUTO_MISSION_CUSTOM_MODE" in text
    assert "--flight-capture-ready-file" in text
    assert "--flight-capture-complete-file" in text
    assert "--acknowledge-owned-disposable-sitl" in text
    assert "_container_inspect(docker, expected_container_id)" in text
    assert '"exec",\n                    expected_container_id,' in text
    assert '"id": (documents.operator_grant.transaction_id' in text


@pytest.mark.parametrize(
    "fault", [None, "missing_hash", "missing_path", "hash", "ranker", "airframe", "no_ranker"]
)
def test_campaign_cli_loads_policy_before_session(tmp_path, monkeypatch, fault):
    from multidetect.autonomy.policy_artifact import PolicyArtifact
    from multidetect.autonomy.task_ranker import TaskRanker
    from scripts import vtol_selected_task_campaign as campaign

    ranker = TaskRanker((-1, 0, 0), "CLI policy fixture")
    artifact = PolicyArtifact.for_ranker(
        ranker, policy_id="cli-fixture", policy_version="0.3.0-pre.8",
        airframe_profile_hash=HASH_B if fault == "airframe" else HASH_A,
        mission_contract_hash=HASH_B, safety_policy_hash=HASH_C,
    )
    raw = (json.dumps(artifact.to_mapping(), indent=2) + "\n").encode()
    artifact_path = tmp_path / "policy.json"
    artifact_path.write_bytes(raw)
    expected = HASH_C if fault == "hash" else hashlib.sha256(raw).hexdigest()
    loaded_ranker = None if fault == "no_ranker" else (
        TaskRanker((1, 0, 0), "different ranker") if fault == "ranker" else ranker
    )
    monkeypatch.setattr(campaign, "load_campaign_definition", lambda p: ([], {}, loaded_ranker))
    # The campaign CLI now resolves the original PromptEnvelope through its
    # dedicated loader before opening a telemetry session.  Keep this wiring
    # fixture focused on policy ordering without teaching production code to
    # tolerate a missing campaign file.
    monkeypatch.setattr(campaign, "load_campaign_prompt", lambda p: None)
    opened = []
    calls = []

    class Session:
        def __init__(self, **kwargs):
            opened.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(campaign, "MissionTelemetrySession", Session)
    monkeypatch.setattr(campaign, "run_selected_task_campaign", lambda **kw: calls.append(kw) or {})
    # Avoid dependency on hardware/clock setup in this CLI wiring fixture.
    monkeypatch.setattr(campaign, "_cli_host_clock", lambda *a: None)
    args = _energy_cli_args()
    evidence = tmp_path / "result.json"
    args[1] = str(evidence)
    args += [
        "--acknowledge-owned-disposable-sitl",
        "--campaign-file", str(tmp_path / "campaign.json"),
        "--campaign-output-directory", str(tmp_path / "sorties"),
        "--energy-capacity-wh", "100", "--energy-avionics-power-w", "10",
        "--energy-propulsion-max-power-w", "100",
        "--energy-initial-remaining-wh", "90", "--energy-provenance", "fixture",
    ]
    if fault != "missing_path":
        args += ["--policy-artifact", str(artifact_path)]
    if fault != "missing_hash":
        args += ["--policy-artifact-file-sha256", expected]
    if fault:
        with pytest.raises(SystemExit) as exc:
            campaign.main(args)
        assert exc.value.code == 2
        assert not opened
        assert not calls
        assert not evidence.exists()
    else:
        assert campaign.main(args) == 0
        assert len(opened) == len(calls) == 1
        assert calls[0]["ranker"] is None
        assert calls[0]["shared_policy"].artifact == artifact
        saved = json.loads(evidence.read_text())
        assert saved["policy_artifact_input"]["file_sha256"] == expected
        assert saved["policy_artifact_input"]["canonical_sha256"] == artifact.canonical_hash
