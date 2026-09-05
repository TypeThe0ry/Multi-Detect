from __future__ import annotations

import json
import struct
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.qgc_readonly_bridge import MavlinkFrame
from scripts import run_simulator_docker_ai_acceptance as module
from scripts.run_simulator_docker import build_docker_simulator_plan

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "configs" / "scenarios" / "river_rescue_vtol_001.scenario.json"


def _completed(argv: list[str], *, code: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr=stderr)


class _Message:
    def __init__(
        self,
        kind: str,
        *,
        source_system: int = 1,
        source_component: int = 1,
        **fields: object,
    ) -> None:
        self.kind = kind
        self.source_system = source_system
        self.source_component = source_component
        self.__dict__.update(fields)

    def get_type(self) -> str:
        return self.kind

    def get_srcSystem(self) -> int:
        return self.source_system

    def get_srcComponent(self) -> int:
        return self.source_component


class _Mav:
    def __init__(self) -> None:
        self.manual_control: list[tuple[int, ...]] = []
        self.command_long: list[tuple[object, ...]] = []
        self.set_modes: list[tuple[int, ...]] = []
        self.heartbeats = 0

    def manual_control_send(self, *args: int) -> None:
        self.manual_control.append(tuple(int(value) for value in args))

    def heartbeat_send(self, *_args: object) -> None:
        self.heartbeats += 1

    def command_long_send(self, *args: object) -> None:
        self.command_long.append(tuple(args))

    def set_mode_send(self, *args: int) -> None:
        self.set_modes.append(tuple(int(value) for value in args))


class _Connection:
    def __init__(self, messages: tuple[object, ...]) -> None:
        self.messages = iter(messages)
        self.mav = _Mav()
        self.closed = False

    def recv_match(self, *, blocking: bool = True, timeout: float = 0.2):  # noqa: ARG002
        try:
            return next(self.messages)
        except StopIteration:
            return None

    def close(self) -> None:
        self.closed = True


class _AdvancingConnection(_Connection):
    def __init__(
        self,
        messages: tuple[object | None, ...],
        clock_state: list[float],
        *,
        step_s: float = 0.01,
    ) -> None:
        super().__init__(messages)
        self.clock_state = clock_state
        self.step_s = step_s

    def recv_match(self, *, blocking: bool = True, timeout: float = 0.2):  # noqa: ARG002
        self.clock_state[0] += self.step_s
        return super().recv_match(blocking=blocking, timeout=timeout)


def _mavutil() -> object:
    return SimpleNamespace(
        mavlink=SimpleNamespace(
            MAV_TYPE_GCS=6,
            MAV_AUTOPILOT_INVALID=8,
            MAV_CMD_COMPONENT_ARM_DISARM=400,
            MAV_COMP_ID_AUTOPILOT1=1,
        )
    )


def _messages() -> tuple[object, ...]:
    return (
        _Message(
            "HEARTBEAT",
            base_mode=128,
            type=2,
            autopilot=3,
            custom_mode=0,
            system_status=4,
        ),
        _Message(
            "GPS_RAW_INT",
            fix_type=3,
            satellites_visible=13,
            lat=13_000_000,
            lon=1_038_005_000,
            alt=5000,
            eph=120,
            epv=180,
        ),
        _Message(
            "GLOBAL_POSITION_INT",
            lat=13_000_000,
            lon=1_038_005_000,
            relative_alt=5000,
            hdg=9000,
        ),
    )


def _probe_with_fakes(
    endpoint: str,
    *,
    control_port: int = 24540,
    wait_for_armed_heartbeat: bool = False,
    exercise_arm: bool = False,
    exercise_target: bool = False,
    exercise_flight_duration_s: float = 0.0,
    minimum_flight_displacement_m: float = (
        module.DEFAULT_MINIMUM_FLIGHT_DISPLACEMENT_M
    ),
    clock: Callable[[], float] | None = None,
    px4_component_ids: set[int] | frozenset[int] | None = None,
    minimum_gps_fix_type: int = 3,
    maximum_frames: int = module.DEFAULT_MAXIMUM_TELEMETRY_FRAMES,
) -> module.DockerAiTelemetryProbe:
    telemetry = _Connection(_messages())
    control = _Connection(())
    probe = module.DockerAiTelemetryProbe(
        endpoint,
        control_endpoint=f"udpout:127.0.0.1:{control_port}",
        connection_factory=lambda *_args, **_kwargs: telemetry,
        control_connection_factory=lambda *_args, **_kwargs: control,
        mavutil_module=_mavutil(),
        wait_for_armed_heartbeat=wait_for_armed_heartbeat,
        exercise_arm=exercise_arm,
        exercise_target=exercise_target,
        exercise_flight_duration_s=exercise_flight_duration_s,
        minimum_flight_displacement_m=minimum_flight_displacement_m,
        **({} if clock is None else {"clock": clock}),
        # Keep the fixture aligned with the production default: only the PX4
        # autopilot component is allowed to satisfy the live identity gate.
        px4_component_ids=px4_component_ids
        if px4_component_ids is not None
        else module.DEFAULT_PX4_COMPONENT_IDS,
        minimum_gps_fix_type=minimum_gps_fix_type,
        maximum_frames=maximum_frames,
    )
    return probe


@pytest.mark.parametrize(
    "endpoint",
    (
        "udpin:localhost:24541",
        "udpin:0.0.0.0:24541",
        "udpin:192.0.2.10:24541",
        "udpin:10.0.0.1:24541",
        "serial:/dev/ttyUSB0",
        "udpout:127.0.0.1:24541",
        "udp://127.0.0.1:24541",
        "udpin:127.0.0.1:0",
        "udpin:127.0.0.1:65536",
        " udpin:127.0.0.1:24541",
        "udpin:127.0.0.1:24541 ",
    ),
)
def test_probe_rejects_non_literal_loopback_telemetry_endpoints(endpoint: str) -> None:
    """Telemetry must never resolve a hostname or open a REAL/wildcard URI."""

    with pytest.raises(module.DockerAiAcceptanceError, match="literal loopback"):
        module.DockerAiTelemetryProbe(
            endpoint,
            control_endpoint="udpout:127.0.0.1:24540",
            connection_factory=lambda *_args, **_kwargs: _Connection(()),
            control_connection_factory=lambda *_args, **_kwargs: _Connection(()),
            mavutil_module=_mavutil(),
        )


def test_probe_accepts_literal_ipv4_and_ipv6_loopback_for_both_legs() -> None:
    assert module.parse_literal_loopback_udp_endpoint(
        "udpin:127.0.0.1:24541", scheme="udpin"
    ) == ("127.0.0.1", 24541)
    assert module.parse_literal_loopback_udp_endpoint(
        "udpin:::1:24541", scheme="udpin"
    ) == ("::1", 24541)
    assert module.parse_literal_loopback_udp_endpoint(
        "udpin:[::1]:24541", scheme="udpin"
    ) == ("::1", 24541)
    assert module.parse_literal_loopback_udp_endpoint(
        "udpout:[::1]:24540", scheme="udpout"
    ) == ("::1", 24540)
    with pytest.raises(module.DockerAiAcceptanceError, match="udpout"):
        module.parse_literal_loopback_udp_endpoint(
            "udpin:127.0.0.1:24541", scheme="udpout"
        )


def test_dry_run_validates_generated_loopback_ports_without_opening_io(tmp_path: Path) -> None:
    """Invalid port choices fail before the dry-run fixture or Docker path."""

    with pytest.raises(module.DockerAiAcceptanceError, match="endpoint port"):
        module.run_acceptance(
            SCENARIO,
            tmp_path / "invalid-port.json",
            execute=False,
            control_port=0,
        )


def _inspect_record(plan: object) -> dict[str, object]:
    model_root = Path(plan.model_root).resolve()  # type: ignore[attr-defined]
    world_path = Path(plan.world_path).resolve()  # type: ignore[attr-defined]
    scripts_root = Path(plan.scripts_root).resolve()  # type: ignore[attr-defined]
    environment = [
        f"{key}={value}"
        for key, value in plan.environment  # type: ignore[attr-defined]
    ]
    return {
        "Id": "container-id-ai",
        "Name": f"/{plan.container_name}",  # type: ignore[attr-defined]
        "Config": {
            "Image": plan.image,  # type: ignore[attr-defined]
            "Labels": {"multidetect.purpose": "simulator-docker"},
            "Env": environment,
        },
        "HostConfig": {
            "NetworkMode": "bridge",
            "Privileged": False,
            "PortBindings": {
                f"{plan.host_control_port}/udp": [  # type: ignore[attr-defined]
                    {
                        "HostIp": "127.0.0.1",
                        "HostPort": str(plan.host_control_port),  # type: ignore[attr-defined]
                    }
                ],
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": str(model_root),
                    "Target": "/opt/multidetect-gazebo-models",
                    "ReadOnly": True,
                },
                {
                    "Type": "bind",
                    "Source": str(world_path),
                    "Target": "/opt/px4-gazebo/share/gz/worlds/default.sdf",
                    "ReadOnly": True,
                },
                {
                    "Type": "bind",
                    "Source": str(scripts_root),
                    "Target": "/workspace/scripts",
                    "ReadOnly": True,
                },
            ],
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": str(model_root),
                "Destination": "/opt/multidetect-gazebo-models",
                "RW": False,
            },
            {
                "Type": "bind",
                "Source": str(world_path),
                "Destination": "/opt/px4-gazebo/share/gz/worlds/default.sdf",
                "RW": False,
            },
            {
                "Type": "bind",
                "Source": str(scripts_root),
                "Destination": "/workspace/scripts",
                "RW": False,
            },
        ],
        "State": {"Running": True},
    }


def test_dry_run_executes_only_fixture_and_never_runner(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: object):
        calls.append(argv)
        return _completed(argv, code=99, stderr="runner must not be called")

    output = tmp_path / "dry-ai.json"
    result = module.run_acceptance(SCENARIO, output, runner=runner)
    assert result["gate_passed"] is True
    assert result["execution_requested"] is False
    assert result["ai_policy_gate_passed"] is True
    assert result["ai_steps"] > 0
    assert result["frames"]["observed"] > 0
    assert result["safety"]["real_pixhawk_contacted"] is False
    assert result["claims"]["px4_actuator_response"] == "not_claimed"
    assert result["container_runtime"]["docker_invoked"] is False
    assert calls == []
    assert json.loads(output.read_text(encoding="utf-8"))["gate_passed"] is True


def test_disarmed_live_run_reports_not_exercised_not_failed() -> None:
    result = module._augment_payload(
        {
            "gate_passed": True,
            "runtime_gate_passed": True,
            "claims": {},
        },
        ai_evidence={
            "armed_observed": False,
            "startup_gate": "waiting_for_armed_heartbeat",
            "ai_steps": 0,
            "ai_accepted": 0,
            "ai_rejected": 0,
            "truth_free": True,
            "rejections": [],
        },
        fixture=None,
        execution_requested=True,
    )

    assert result["runtime_gate_passed"] is True
    assert result["gate_passed"] is False
    assert result["ai_policy_gate_passed"] is False
    assert result["ai_policy_steps"] == 0
    assert result["ai_policy_gate_status"] == (
        "not_exercised_waiting_for_armed_heartbeat"
    )
    assert result["claims"]["sim_ai_policy"] == (
        "not_exercised_waiting_for_armed_heartbeat"
    )
    assert result["claims"]["sim_ai_control_output"] == "none"
    assert result["claims"]["real_control_acceptance"] == "not_claimed"


def test_disarmed_failsafe_step_is_not_an_ai_policy_pass() -> None:
    result = module._augment_payload(
        {
            "gate_passed": True,
            "runtime_gate_passed": True,
            "claims": {},
        },
        ai_evidence={
            "armed_observed": False,
            "startup_gate": "released",
            "ai_steps": 1,
            "ai_accepted": 0,
            "ai_rejected": 0,
            "failsafe_steps": 1,
            "watchdog_failsafe_steps": 0,
            "truth_free": True,
            "rejections": [],
        },
        fixture=None,
        execution_requested=True,
    )

    assert result["runtime_gate_passed"] is True
    assert result["ai_steps"] == 1
    assert result["ai_policy_steps"] == 0
    assert result["ai_policy_gate_passed"] is False
    assert result["ai_policy_gate_status"] == (
        "not_exercised_waiting_for_armed_heartbeat"
    )
    assert result["gate_passed"] is False


def test_bridge_maps_observed_messages_and_emits_shared_ai_frames() -> None:
    probe = _probe_with_fakes("udpin:127.0.0.1:24541")
    probe.bridge.enable_ai()
    for index, message in enumerate(_messages(), start=1):
        probe._record(message, float(index))
    evidence = probe.bridge.evidence()
    assert evidence["truth_free"] is True
    assert evidence["frames_observed"] == 3
    assert evidence["frames_rejected"] == 0
    assert evidence["ai_steps"] >= 1
    assert evidence["ai_accepted"] >= 1
    snapshot = evidence["last_snapshot"]
    assert snapshot["gps_fix_type"] == 3
    assert snapshot["longitude_deg"] == 103.8005
    assert snapshot["gps_horizontal_accuracy_m"] == 1.2
    assert probe.adapter.evidence()["messages_sent"] >= 1
    probe.adapter.close()


def test_bridge_rejects_wrong_identity_and_unknown_message() -> None:
    probe = _probe_with_fakes("udpin:127.0.0.1:24541")
    probe.bridge.enable_ai()
    probe._record(
        _Message("HEARTBEAT", source_system=42, base_mode=128),
        1.0,
    )
    probe._record(object(), 1.1)
    evidence = probe.bridge.evidence()
    assert evidence["frames_observed"] == 2
    assert evidence["frames_rejected"] == 2
    reasons = [item["reason"] for item in evidence["rejections"]]
    assert "source_system_mismatch" in reasons
    assert "unsupported_or_truncated_message" in reasons
    probe.adapter.close()


def test_production_probe_defaults_to_armed_startup_gate() -> None:
    telemetry = _Connection(())
    control = _Connection(())
    probe = module.DockerAiTelemetryProbe(
        "udpin:127.0.0.1:24541",
        control_endpoint="udpout:127.0.0.1:24540",
        connection_factory=lambda *_args, **_kwargs: telemetry,
        control_connection_factory=lambda *_args, **_kwargs: control,
        mavutil_module=_mavutil(),
    )

    evidence = probe.bridge.evidence()
    assert evidence["wait_for_armed_heartbeat"] is True
    assert evidence["startup_gate"] == "waiting_for_armed_heartbeat"
    probe.adapter.close()


def test_exercise_arm_requires_the_armed_startup_gate() -> None:
    with pytest.raises(ValueError, match="wait_for_armed_heartbeat"):
        module.DockerAiTelemetryProbe(
            "udpin:127.0.0.1:24541",
            control_endpoint="udpout:127.0.0.1:24540",
            connection_factory=lambda *_args, **_kwargs: _Connection(()),
            control_connection_factory=lambda *_args, **_kwargs: _Connection(()),
            mavutil_module=_mavutil(),
            wait_for_armed_heartbeat=False,
            exercise_arm=True,
        )


def test_exercise_arm_requires_execute_mode(tmp_path: Path) -> None:
    with pytest.raises(module.DockerAiAcceptanceError, match="requires --execute"):
        module.run_acceptance(
            SCENARIO,
            tmp_path / "arm-without-execute.json",
            exercise_arm=True,
        )


def test_exercise_target_requires_explicit_arm_and_execute(tmp_path: Path) -> None:
    with pytest.raises(module.DockerAiAcceptanceError, match="exercise-arm"):
        module.run_acceptance(
            SCENARIO,
            tmp_path / "target-without-arm.json",
            execute=True,
            exercise_target=True,
        )
    with pytest.raises(module.DockerAiAcceptanceError, match="--execute"):
        module.run_acceptance(
            SCENARIO,
            tmp_path / "target-without-execute.json",
            exercise_target=True,
            exercise_arm=True,
        )


def test_exercise_flight_requires_target_arm_and_execute(tmp_path: Path) -> None:
    with pytest.raises(module.DockerAiAcceptanceError, match="exercise-target"):
        module.run_acceptance(
            SCENARIO,
            tmp_path / "flight-without-target.json",
            execute=True,
            exercise_arm=True,
            exercise_flight_duration_s=2.0,
        )
    with pytest.raises(module.DockerAiAcceptanceError, match="--execute"):
        module.run_acceptance(
            SCENARIO,
            tmp_path / "flight-without-execute.json",
            exercise_arm=True,
            exercise_target=True,
            exercise_flight_duration_s=2.0,
        )


@pytest.mark.parametrize("duration", (-1.0, 0.5, 31.0, float("inf")))
def test_exercise_flight_duration_is_bounded(duration: float) -> None:
    with pytest.raises(ValueError, match="1..30"):
        _probe_with_fakes(
            "udpin:127.0.0.1:24541",
            wait_for_armed_heartbeat=True,
            exercise_arm=True,
            exercise_target=True,
            exercise_flight_duration_s=duration,
        )


def test_joystick_preflight_is_scoped_only_to_the_flight_exercise() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: object):
        calls.append(argv)
        return _completed(argv)

    arm_only = module._prepare_disposable_sitl_for_arm(
        "docker",
        "arm-only",
        runner=runner,
        enable_joystick_control=False,
    )
    flight = module._prepare_disposable_sitl_for_arm(
        "docker",
        "flight",
        runner=runner,
        enable_joystick_control=True,
    )

    arm_parameters = [row["parameter"] for row in arm_only["parameters"]]
    flight_parameters = [row["parameter"] for row in flight["parameters"]]
    assert arm_parameters == [
        "COM_ARM_HFLT_CHK",
        "COM_MODE_ARM_CHK",
        "CBRK_VTOLARMING",
        "NAV_DLL_ACT",
    ]
    assert flight_parameters == [*arm_parameters, "COM_RC_IN_MODE"]
    assert calls[-1][-2:] == ["COM_RC_IN_MODE", "1"]


def test_none_polls_do_not_consume_the_real_frame_limit() -> None:
    now = [10.0]
    telemetry = _AdvancingConnection(
        (None, None, *_messages()),
        now,
        step_s=0.01,
    )
    control = _Connection(())
    probe = module.DockerAiTelemetryProbe(
        "udpin:127.0.0.1:24541",
        control_endpoint="udpout:127.0.0.1:24540",
        connection_factory=lambda *_args, **_kwargs: telemetry,
        control_connection_factory=lambda *_args, **_kwargs: control,
        mavutil_module=_mavutil(),
        clock=lambda: now[0],
        minimum_ai_steps=10,
        maximum_frames=3,
        wait_for_armed_heartbeat=False,
    )

    result = probe.observe(1.0)

    assert result["frames_observed"] == 3
    assert result["poll_iterations"] == 5
    assert result["maximum_frames"] == 3
    assert result["frame_limit_reached"] is True
    assert result["observe_exit_reason"] == "frame_limit"
    assert "maximum telemetry frame limit 3 reached" in result["error"]
    assert result["status"] == "failed"


def test_failed_flight_elapsed_freezes_and_target_refresh_survives_empty_polls() -> None:
    now = [20.0]
    messages = (
        _Message(
            "HEARTBEAT",
            base_mode=129,
            type=2,
            autopilot=3,
            custom_mode=0,
            system_status=4,
        ),
        _messages()[1],
        _messages()[2],
        _Message(
            "HEARTBEAT",
            base_mode=129,
            type=2,
            autopilot=3,
            custom_mode=module.PX4_POSCTL_CUSTOM_MODE,
            system_status=4,
        ),
    )
    telemetry = _AdvancingConnection(messages, now, step_s=0.1)
    control = _Connection(())
    probe = module.DockerAiTelemetryProbe(
        "udpin:127.0.0.1:24541",
        control_endpoint="udpout:127.0.0.1:24540",
        connection_factory=lambda *_args, **_kwargs: telemetry,
        control_connection_factory=lambda *_args, **_kwargs: control,
        mavutil_module=_mavutil(),
        clock=lambda: now[0],
        wait_for_armed_heartbeat=True,
        exercise_arm=True,
        exercise_target=True,
        exercise_flight_duration_s=1.0,
        minimum_flight_displacement_m=0.5,
    )

    result = probe.observe(1.8)
    before = result["flight_exercise"]
    assert result["observe_exit_reason"] == "timeout"
    assert before["gate_passed"] is False
    assert before["target_refresh_gate_passed"] is True
    assert before["target_updates_accepted"] >= 10
    assert before["maximum_target_refresh_gap_s"] <= 0.5

    now[0] += 20.0
    after = probe.evidence()["flight_exercise"]
    assert after["elapsed_duration_s"] == before["elapsed_duration_s"]
    assert after["target_refresh_coverage_s"] == before["target_refresh_coverage_s"]


def test_exercise_arm_sends_arm_and_cleanup_disarm_on_shared_socket() -> None:
    probe = _probe_with_fakes(
        "udpin:127.0.0.1:24541",
        wait_for_armed_heartbeat=True,
        exercise_arm=True,
    )
    result = probe.observe(1.0)
    assert result["qualified"] is True
    assert result["exercise_arm"] is True
    arm = result["arm_exercise"]
    assert arm["arm_requested"] is True
    assert arm["arm_command_sent"] is True
    assert arm["disarm_requested"] is True
    assert arm["disarm_command_sent"] is True
    commands = arm["commands"]
    assert [item["action"] for item in commands] == ["arm", "disarm"]
    assert commands[0]["param2"] == 21196.0
    assert commands[0]["force_arm_sentinel"] is True
    control = probe.adapter.evidence()
    # MANUAL_CONTROL policy output and command_long arm/disarm share the same
    # fake connection; no second control socket is opened.
    assert control["messages_sent"] >= 1


def test_exercise_target_injects_truth_free_perception_and_requires_track() -> None:
    probe = _probe_with_fakes(
        "udpin:127.0.0.1:24541",
        wait_for_armed_heartbeat=True,
        exercise_arm=True,
        exercise_target=True,
    )
    result = probe.observe(1.0)
    assert result["qualified"] is True
    assert result["exercise_arm"] is True
    assert result["exercise_target"] is True
    target = result["target_exercise"]
    assert target["requested"] is True
    assert target["sent"] is True
    assert target["accepted"] is True
    assert target["observation"]["source"] == "synthetic_perception_fixture"
    assert target["observation"]["truth_free"] is True
    assert target["decision"]["mode"] == "track"
    assert target["decision"]["accepted"] is True
    assert target["visual_accuracy"] == "not_claimed"
    assert result["ai"]["target_exercise"]["decision"]["mode"] == "track"
    probe.adapter.close()


def test_flight_exercise_requires_sustained_track_and_actual_wgs84_motion() -> None:
    now = [100.0]
    probe = _probe_with_fakes(
        "udpin:127.0.0.1:24541",
        wait_for_armed_heartbeat=True,
        exercise_arm=True,
        exercise_target=True,
        exercise_flight_duration_s=1.0,
        minimum_flight_displacement_m=0.5,
        clock=lambda: now[0],
    )
    probe.bridge.enable_ai()
    probe._record(_messages()[0], 99.7)
    probe._record(_messages()[1], 99.8)
    probe._record(_messages()[2], 99.9)
    assert probe._request_flight_mode() is True
    probe._record(
        _Message(
            "HEARTBEAT",
            base_mode=129,
            type=2,
            autopilot=3,
            custom_mode=module.PX4_POSCTL_CUSTOM_MODE,
            system_status=4,
        ),
        now[0],
    )

    decision = probe._exercise_target_observation()
    assert decision is not None
    assert decision.mode.value == "track"
    for stamp in (100.2, 100.4, 100.6, 100.8):
        now[0] = stamp
        refreshed = probe._exercise_target_observation()
        assert refreshed is not None
        assert refreshed.mode.value == "track"

    now[0] = 101.2
    probe._record(
        _Message(
            "GLOBAL_POSITION_INT",
            lat=13_000_000,
            lon=1_038_005_100,
            relative_alt=5200,
            hdg=9000,
        ),
        now[0],
    )
    probe._exercise_target_observation()
    flight = probe.evidence()["flight_exercise"]
    assert flight["gate_passed"] is True
    assert flight["target_updates_accepted"] == 6
    assert flight["target_refresh_gate_passed"] is True
    assert flight["flight_mode_gate_passed"] is True
    assert flight["flight_mode"]["px4_main_mode"] == "POSCTL"
    assert flight["flight_mode"]["observed"] is True
    assert flight["heartbeat_timeout_s"] == pytest.approx(3.0)
    assert flight["maximum_horizontal_displacement_m"] >= 0.5
    assert flight["final_relative_altitude_delta_m"] == pytest.approx(0.2)
    assert flight["position_source"] == "MAVLink GLOBAL_POSITION_INT"
    assert flight["gazebo_pose_injection_used"] is False
    assert flight["gazebo_truth_consumed"] is False
    assert flight["px4_actuator_response"] == "not_claimed"
    assert "SimulatorAiExecutor" in flight["control_path"]
    probe.adapter.close()


def test_heartbeat_dropout_cannot_be_hidden_by_later_recovery() -> None:
    now = [100.0]
    probe = _probe_with_fakes(
        "udpin:127.0.0.1:24541",
        wait_for_armed_heartbeat=True,
        exercise_arm=True,
        exercise_target=True,
        exercise_flight_duration_s=1.0,
        minimum_flight_displacement_m=0.5,
        clock=lambda: now[0],
    )
    probe.bridge.enable_ai()
    probe._record(_messages()[0], 99.7)
    probe._record(_messages()[1], 99.8)
    probe._record(_messages()[2], 99.9)
    assert probe._request_flight_mode() is True
    probe._record(
        _Message(
            "HEARTBEAT",
            base_mode=129,
            type=2,
            autopilot=3,
            custom_mode=module.PX4_POSCTL_CUSTOM_MODE,
            system_status=4,
        ),
        now[0],
    )
    assert probe._exercise_target_observation() is not None
    for stamp in (100.2, 100.4, 100.6, 100.8):
        now[0] = stamp
        probe._exercise_target_observation()
    now[0] = 101.2
    probe._record(
        _Message(
            "GLOBAL_POSITION_INT",
            lat=13_000_000,
            lon=1_038_005_100,
            relative_alt=5200,
            hdg=9000,
        ),
        now[0],
    )
    probe._exercise_target_observation()
    assert probe.evidence()["flight_exercise"]["gate_passed"] is True

    now[0] = 103.2
    stale_decision = probe._exercise_target_observation()
    assert stale_decision is not None
    assert stale_decision.mode.value == "failsafe"
    stale = probe.evidence()["flight_exercise"]
    assert stale["gate_passed"] is False
    assert stale["heartbeat_continuity_gate_passed"] is False

    now[0] = 103.3
    probe._record(
        _Message(
            "HEARTBEAT",
            base_mode=129,
            type=2,
            autopilot=3,
            custom_mode=module.PX4_POSCTL_CUSTOM_MODE,
            system_status=4,
        ),
        now[0],
    )
    recovered = probe.evidence()["flight_exercise"]
    assert recovered["gate_passed"] is False
    assert recovered["target_updates_rejected"] > 0
    assert probe.bridge.evidence()["failsafe_steps"] > 0
    probe.adapter.close()


def test_flight_response_is_independent_from_ai_policy_gate() -> None:
    result = module._augment_payload(
        {
            "gate_passed": True,
            "runtime_gate_passed": True,
            "claims": {},
        },
        ai_evidence={
            "armed_observed": True,
            "startup_gate": "released",
            "ai_steps": 2,
            "ai_accepted": 2,
            "ai_rejected": 0,
            "failsafe_steps": 0,
            "watchdog_failsafe_steps": 0,
            "truth_free": True,
            "rejections": [],
            "exercise_flight": True,
                "flight_exercise": {
                    "requested": True,
                    "gate_passed": False,
                    "completed": True,
                    "target_updates_accepted": 10,
                    "target_updates_rejected": 0,
                    "target_refresh_gate_passed": True,
                    "flight_mode_gate_passed": True,
                    "heartbeat_continuity_gate_passed": True,
                    "gazebo_pose_injection_used": False,
                "gazebo_truth_consumed": False,
                "position_source": "MAVLink GLOBAL_POSITION_INT",
            },
        },
        fixture=None,
        execution_requested=True,
    )

    assert result["runtime_gate_passed"] is True
    assert result["ai_policy_gate_passed"] is True
    assert result["flight_response_gate_passed"] is False
    assert result["flight_response_gate_status"] == "position_response_not_observed"
    assert result["gate_passed"] is False
    assert result["claims"]["px4_sim_vehicle_motion"] == "not_observed"


def test_command_ack_is_recorded_and_attached_to_arm_request() -> None:
    probe = _probe_with_fakes(
        "udpin:127.0.0.1:24541",
        wait_for_armed_heartbeat=True,
        exercise_arm=True,
    )
    assert probe._request_arm() is True
    probe._record(
        _Message(
            "COMMAND_ACK",
            command=module.MAV_CMD_COMPONENT_ARM_DISARM,
            result=0,
            progress=100,
        ),
        1.0,
    )
    arm = probe._arm_evidence()
    assert arm["acks"][0]["result_name"] == "accepted"
    assert arm["acks"][0]["result_param2"] == 0
    assert arm["commands"][0]["ack_result_name"] == "accepted"
    probe.adapter.close()


def test_raw_mavlink_command_ack_uses_wire_identity_and_command_id() -> None:
    probe = _probe_with_fakes(
        "udpin:127.0.0.1:24541",
        wait_for_armed_heartbeat=True,
        exercise_arm=True,
    )
    assert probe._request_arm() is True
    # COMMAND_ACK's common payload prefix is command, result, progress,
    # result_param2, target_system and target_component.
    frame = MavlinkFrame(
        encoded=b"",
        version=1,
        message_id=module.MSG_COMMAND_ACK,
        source_system_id=module.PX4_SYSTEM_ID,
        source_component_id=module.PX4_AUTOPILOT_COMPONENT,
        payload=struct.pack("<HBBiBB", 400, 0, 100, 0, 245, 190),
    )
    probe._record(frame, 2.0)
    arm = probe._arm_evidence()
    assert arm["command_acks_observed"] == 1
    assert arm["command_acks_accepted"] == 1
    assert arm["command_acks_rejected"] == 0
    assert arm["acks"][0]["identity_valid"] is True
    assert arm["commands"][0]["ack_result_name"] == "accepted"
    probe.adapter.close()


def test_probe_propagates_live_gps_and_px4_identity_configuration() -> None:
    probe = _probe_with_fakes(
        "udpin:127.0.0.1:24541",
        px4_component_ids={1, 191},
        minimum_gps_fix_type=4,
    )
    assert probe.minimum_gps_fix_type == 4
    assert probe.px4_component_ids == frozenset({1, 191})
    evidence = probe.bridge.evidence()
    assert evidence["minimum_gps_fix_type"] == 4
    assert evidence["px4_identity"] == {
        "system_id": module.PX4_SYSTEM_ID,
        "component_ids": [1, 191],
    }
    probe.adapter.close()


def test_probe_qualification_uses_configured_gps_fix_threshold() -> None:
    probe = _probe_with_fakes(
        "udpin:127.0.0.1:24541",
        minimum_gps_fix_type=4,
    )
    for index, message in enumerate(_messages(), start=1):
        probe._record(message, float(index))
    # The fixture has a 3-D fix.  A live run configured for RTK/float quality
    # (fix type >= 4) must remain unqualified even though coordinates exist.
    assert probe._qualified() is False
    assert probe.bridge.evidence()["minimum_gps_fix_type"] == 4
    probe.adapter.close()


def test_set_mode_wire_contract_uses_the_shared_control_socket() -> None:
    probe = _probe_with_fakes(
        "udpin:127.0.0.1:24541",
        wait_for_armed_heartbeat=True,
        exercise_arm=True,
        exercise_target=True,
        exercise_flight_duration_s=1.0,
    )

    assert probe._request_flight_mode() is True
    control = probe._control_connection
    assert control is not None
    assert control.mav.set_modes == [
        (
            module.PX4_SYSTEM_ID,
            module.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
            module.PX4_POSCTL_CUSTOM_MODE,
        )
    ]
    probe.adapter.close()


def test_posctl_confirmation_is_current_armed_and_loss_is_fail_closed() -> None:
    probe = _probe_with_fakes(
        "udpin:127.0.0.1:24541",
        wait_for_armed_heartbeat=True,
        exercise_arm=True,
        exercise_target=True,
        exercise_flight_duration_s=1.0,
    )
    probe.bridge.enable_ai()
    assert probe._request_flight_mode() is True

    probe._record(
        _Message(
            "HEARTBEAT",
            source_system=42,
            base_mode=129,
            type=2,
            autopilot=3,
            custom_mode=module.PX4_POSCTL_CUSTOM_MODE,
            system_status=4,
        ),
        0.9,
    )
    assert probe._flight_mode_evidence()["observed"] is False

    probe._record(
        _Message(
            "HEARTBEAT",
            base_mode=129,
            type=2,
            autopilot=3,
            custom_mode=module.PX4_POSCTL_CUSTOM_MODE,
            system_status=4,
        ),
        1.0,
    )
    assert probe._flight_mode_evidence()["observed"] is True

    probe._record(
        _Message(
            "HEARTBEAT",
            base_mode=129,
            type=2,
            autopilot=3,
            custom_mode=4 << 16,
            system_status=4,
        ),
        1.1,
    )
    mode = probe._flight_mode_evidence()
    assert mode["observed"] is False
    assert mode["loss_events"] == 1
    assert mode["clean_throughout"] is False

    probe._record(
        _Message(
            "HEARTBEAT",
            base_mode=1,
            type=2,
            autopilot=3,
            custom_mode=module.PX4_POSCTL_CUSTOM_MODE,
            system_status=4,
        ),
        1.2,
    )
    assert probe._flight_mode_evidence()["observed"] is False
    probe.adapter.close()


def test_first_rejected_target_does_not_capture_the_flight_position_baseline() -> None:
    now = [100.0]
    probe = _probe_with_fakes(
        "udpin:127.0.0.1:24541",
        wait_for_armed_heartbeat=True,
        exercise_arm=True,
        exercise_target=True,
        exercise_flight_duration_s=1.0,
        clock=lambda: now[0],
    )
    probe.bridge.enable_ai()
    for offset, message in enumerate(_messages(), start=1):
        probe._record(message, 99.6 + offset / 10.0)
    assert probe._request_flight_mode() is True
    probe._record(
        _Message(
            "HEARTBEAT",
            base_mode=129,
            type=2,
            autopilot=3,
            custom_mode=module.PX4_POSCTL_CUSTOM_MODE,
            system_status=4,
        ),
        now[0],
    )

    original_ingest = probe.bridge.ingest_perception
    probe.bridge.ingest_perception = lambda *_args, **_kwargs: None
    assert probe._exercise_target_observation() is None
    assert probe.flight_initial_position is None

    moved_latitude = 1.3000100
    now[0] += 0.2
    probe._record(
        _Message(
            "GLOBAL_POSITION_INT",
            lat=round(moved_latitude * 10_000_000),
            lon=1_038_005_000,
            relative_alt=5000,
            hdg=9000,
        ),
        now[0],
    )
    probe.bridge.ingest_perception = original_ingest
    assert probe._exercise_target_observation() is not None
    assert probe.flight_initial_position is not None
    assert probe.flight_initial_position["latitude_deg"] == pytest.approx(moved_latitude)
    probe.adapter.close()


@pytest.mark.parametrize(
    ("override", "value"),
    (
        ("ai_rejected", 1),
        ("failsafe_steps", 1),
        ("watchdog_failsafe_steps", 1),
        ("target_updates_rejected", 1),
    ),
)
def test_flight_acceptance_rejects_any_recorded_safety_fault(
    override: str, value: int
) -> None:
    flight = {
        "requested": True,
        "gate_passed": True,
        "status": "passed",
        "completed": True,
        "target_updates_accepted": 20,
        "target_updates_rejected": 0,
        "target_refresh_gate_passed": True,
        "flight_mode_gate_passed": True,
        "heartbeat_continuity_gate_passed": True,
        "gazebo_pose_injection_used": False,
        "gazebo_truth_consumed": False,
        "position_source": "MAVLink GLOBAL_POSITION_INT",
    }
    ai = {
        "armed_observed": True,
        "startup_gate": "released",
        "ai_steps": 20,
        "ai_accepted": 20,
        "ai_rejected": 0,
        "failsafe_steps": 0,
        "watchdog_failsafe_steps": 0,
        "truth_free": True,
        "rejections": [],
        "exercise_target": True,
        "target_exercise": {
            "accepted": True,
            "decision": {"mode": "track", "accepted": True},
        },
        "exercise_flight": True,
        "flight_exercise": flight,
    }
    if override == "target_updates_rejected":
        flight[override] = value
    else:
        ai[override] = value

    result = module._augment_payload(
        {"gate_passed": True, "runtime_gate_passed": True, "claims": {}},
        ai_evidence=ai,
        fixture=None,
        execution_requested=True,
    )

    assert result["ai_policy_gate_passed"] is True
    assert result["flight_response_gate_passed"] is False
    assert result["flight_response_gate_status"] == "safety_fault_observed"
    assert result["gate_passed"] is False


@pytest.mark.parametrize(
    ("message_kwargs", "reason"),
    (
        (
            {"source_system": 42, "source_component": 1, "command": 400},
            "command_ack_source_system_mismatch",
        ),
        (
            {"source_system": 1, "source_component": 42, "command": 400},
            "command_ack_source_component_mismatch",
        ),
        (
            {"source_system": 1, "source_component": 1, "command": 211},
            "command_ack_command_mismatch",
        ),
    ),
)
def test_command_ack_identity_gate_rejects_spoofed_or_unrelated_ack(
    message_kwargs: dict[str, int], reason: str
) -> None:
    probe = _probe_with_fakes(
        "udpin:127.0.0.1:24541",
        wait_for_armed_heartbeat=True,
        exercise_arm=True,
    )
    assert probe._request_arm() is True
    probe._record(
        _Message("COMMAND_ACK", result=0, progress=100, **message_kwargs),
        2.0,
    )
    arm = probe._arm_evidence()
    assert arm["command_acks_observed"] == 1
    assert arm["command_acks_accepted"] == 0
    assert arm["command_acks_rejected"] == 1
    assert arm["acks"][0]["identity_valid"] is False
    assert arm["acks"][0]["accepted_for_arm_exercise"] is False
    assert arm["acks"][0]["rejection_reason"] == reason
    assert "ack_result" not in arm["commands"][0]
    rejection = probe.rejections[-1]
    assert rejection["reason"] == reason
    assert rejection["command_id"] == message_kwargs["command"]
    probe.adapter.close()


def test_bridge_waits_for_first_armed_heartbeat_before_ai_failsafe() -> None:
    probe = _probe_with_fakes(
        "udpin:127.0.0.1:24541", wait_for_armed_heartbeat=True
    )
    probe.bridge.enable_ai()
    # PX4 startup commonly emits one or more disarmed heartbeats.  They must
    # not poison the terminal authority state before the first arm event.
    startup = _Message("HEARTBEAT", base_mode=0)
    assert probe.bridge.ingest(startup, 1.0) is None
    waiting = probe.bridge.evidence()
    assert waiting["startup_disarmed_wait"] == 1
    assert waiting["startup_gate"] == "waiting_for_armed_heartbeat"
    assert waiting["ai_steps"] == 0
    armed = _Message("HEARTBEAT", base_mode=128)
    decision = probe.bridge.ingest(armed, 1.1)
    assert decision is not None
    assert decision.mode.value == "hover"
    assert probe.bridge.evidence()["armed_observed"] is True
    disarmed = probe.bridge.ingest(_Message("HEARTBEAT", base_mode=0), 1.2)
    assert disarmed is not None
    assert disarmed.mode.value == "failsafe"
    probe.adapter.close()


def test_docker_probe_runs_truth_free_policy_while_px4_is_disarmed() -> None:
    probe = _probe_with_fakes("udpin:127.0.0.1:24541")
    # The live Docker gate observes a safe, disarmed PX4.  It must still
    # exercise the shared policy path so this gate does not depend on arming.
    probe.bridge.enable_ai()
    for index, message in enumerate(_messages(), start=1):
        if getattr(message, "kind", "") == "HEARTBEAT":
            message.base_mode = 0
        probe._record(message, float(index))
    evidence = probe.bridge.evidence()
    assert evidence["wait_for_armed_heartbeat"] is False
    assert evidence["startup_gate"] == "released"
    assert evidence["armed_observed"] is False
    assert evidence["ai_steps"] >= 1
    assert evidence["ai_accepted"] == 0
    assert probe.adapter.evidence()["messages_sent"] == 1
    assert probe.adapter.evidence()["authority_state"] == "failsafe"
    probe.adapter.close()


def test_bridge_accepts_raw_mavlink_frame_and_positional_timestamp() -> None:
    payload = struct.pack("<IBBBBB", 0, 2, 3, 128, 4, 3)
    frame = MavlinkFrame(
        encoded=b"",
        version=1,
        message_id=0,
        source_system_id=1,
        source_component_id=1,
        payload=payload,
    )
    probe = _probe_with_fakes("udpin:127.0.0.1:24541")
    probe.bridge.enable_ai()
    decision = probe.bridge(frame, 3.0)
    assert decision is not None
    assert decision.mode.value == "hover"
    assert probe.bridge.evidence()["frames_accepted"] == 1
    probe.adapter.close()


def test_execute_runs_container_probe_and_reclaims_it(tmp_path: Path) -> None:
    container_name = "sim-ai-test-container"
    control_port = 24560
    # Scenario manifests currently bind the production telemetry leg to the
    # fixed loopback 14541 endpoint; the planner rejects arbitrary ports.
    telemetry_port = 14541
    plan = build_docker_simulator_plan(
        SCENARIO,
        repository_root=ROOT,
        job_dir=tmp_path / "job",
        container_name=container_name,
        control_port=control_port,
        telemetry_port=telemetry_port,
    )
    calls: list[list[str]] = []
    inspect_calls = 0

    def runner(argv: list[str], **_kwargs: object):
        nonlocal inspect_calls
        calls.append(argv)
        args = argv[1:]
        if args[:3] == ["container", "inspect", plan.container_name]:
            inspect_calls += 1
            if inspect_calls == 1:
                return _completed(argv, code=1, stderr="No such container")
            return _completed(argv, stdout=json.dumps([_inspect_record(plan)]))
        if args[:3] == ["image", "inspect", plan.image]:
            return _completed(argv, stdout="sha256:image-ai\n")
        if args[:1] == ["run"]:
            return _completed(argv, stdout="container-id-ai\n")
        if args[:1] == ["exec"]:
            return _completed(argv, stdout="param set\n")
        if args[:1] in (["logs"], ["stop"], ["rm"]):
            return _completed(argv, stdout="PX4 AI startup\n")
        raise AssertionError(f"unexpected docker call: {argv}")

    def factory(endpoint: str) -> module.DockerAiTelemetryProbe:
        return _probe_with_fakes(
            endpoint,
            control_port=control_port,
            wait_for_armed_heartbeat=True,
            exercise_arm=True,
        )

    output = tmp_path / "execute-ai.json"
    result = module.run_acceptance(
        SCENARIO,
        output,
        execute=True,
        timeout_s=5.0,
        docker="docker-fake",
        container_name=container_name,
        control_port=control_port,
        telemetry_port=telemetry_port,
        runner=runner,
        telemetry_probe_factory=factory,
        exercise_arm=True,
    )
    assert result["gate_passed"] is True
    assert result["runtime_gate_passed"] is True
    assert result["telemetry"]["qualified"] is True
    assert result["ai_steps"] >= 1
    assert result["ai"]["truth_free"] is True
    assert result["telemetry"]["arm_exercise"]["arm_command_sent"] is True
    assert result["telemetry"]["arm_exercise"]["disarm_command_sent"] is True
    assert result["container_runtime"]["arm_preflight"]["applied"] is True
    assert result["safety"]["flight_control_commands_sent"] >= 1
    assert result["safety"]["real_pixhawk_contacted"] is False
    assert result["claims"]["real_control_acceptance"] == "not_claimed"
    assert result["container_runtime"]["cleanup"]["removed"] is True
    assert any(call[1] == "run" for call in calls)
    assert any(call[1] == "stop" for call in calls)
    assert any(call[1] == "rm" for call in calls)
