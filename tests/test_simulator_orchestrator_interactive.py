from __future__ import annotations

import json
import random
import socket
import struct
import threading
import time
from pathlib import Path

import pytest

from multidetect.simulator import (
    ControlAuthorityState,
    SimulatorInteractiveRouterConfig,
    SimulatorOrchestrator,
    SimulatorOrchestratorError,
    SimulatorProcessSpec,
)
from multidetect.simulator.__main__ import build_parser

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "configs/scenarios/river_rescue_vtol_001.scenario.json"


class _FakeProcess:
    _next_pid = 7000

    def __init__(self) -> None:
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, _signal: int) -> None:
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.returncode = -9


def _ports() -> tuple[int, int, int, int]:
    # Windows can reserve parts of the ephemeral range for Hyper-V/WSL.  Pick
    # a deterministic set that is free for both TCP (the orchestrator probe)
    # and UDP (the actual router bind), rather than trusting port 0.
    selected: list[int] = []
    # Start from a randomized high-user-port window instead of a fixed tuple.
    # The orchestrator validates and binds these endpoints after this helper
    # returns; randomization prevents unrelated local services or a parallel
    # pytest worker from repeatedly colliding with the same 30000-series port.
    rng = random.SystemRandom()
    candidates = iter(rng.sample(range(30000, 60000), 256))
    for candidate in candidates:
        if len(selected) == 4:
            break
        if candidate in selected:
            continue
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            tcp.bind(("127.0.0.1", candidate))
            udp.bind(("127.0.0.1", candidate))
        except OSError:
            continue
        finally:
            tcp.close()
            udp.close()
        selected.append(candidate)
    assert len(selected) == 4
    return tuple(selected)  # type: ignore[return-value]


def test_interactive_dry_run_persists_router_and_ai_evidence(tmp_path: Path) -> None:
    qgc_input, qgc_output, px4_input, px4_output = _ports()
    orchestrator = SimulatorOrchestrator(
        SCENARIO,
        dry_run=True,
        interactive=True,
        job_root=tmp_path,
        qgc_control_port=qgc_input,
        qgc_output_port=qgc_output,
        px4_telemetry_port=px4_input,
        px4_control_endpoint=f"udpout:127.0.0.1:{px4_output}",
    )
    result = orchestrator.start()

    assert result.status == "planned"
    assert result.interactive_router["enabled"] is True
    # An actual native Fly View session must have the explicit bounded flight
    # policy enabled; the standalone acceptance fixture remains read-only.
    assert result.interactive_router["qgc_control_mode"] == "sim_flight"
    assert result.interactive_router["endpoints"]["qgc_input"] == f"127.0.0.1:{qgc_input}"
    assert result.ai["enabled"] is False
    assert orchestrator.control_adapter.config.manual_watchdog_s == pytest.approx(0.5)
    assert (result.job_dir / "interactive-router.json").is_file()
    assert (result.job_dir / "ai.json").is_file()
    job = json.loads((result.job_dir / "job.json").read_text(encoding="utf-8"))
    assert job["interactive"] is True
    assert job["interactive_router"]["qgc_output"] == f"127.0.0.1:{qgc_output}"
    assert job["qgc_control_mode"] == "sim_flight"


def test_interactive_orchestrator_can_explicitly_force_read_only(tmp_path: Path) -> None:
    qgc_input, qgc_output, px4_input, px4_output = _ports()
    orchestrator = SimulatorOrchestrator(
        SCENARIO,
        dry_run=True,
        interactive=True,
        qgc_control_mode="read_only",
        job_root=tmp_path,
        qgc_control_port=qgc_input,
        qgc_output_port=qgc_output,
        px4_telemetry_port=px4_input,
        px4_control_endpoint=f"udpout:127.0.0.1:{px4_output}",
    )
    assert orchestrator.interactive_router_config.qgc_control_mode == "read_only"
    result = orchestrator.start()
    assert result.interactive_router["qgc_control_mode"] == "read_only"


def test_interactive_router_mode_override_must_match_custom_config(tmp_path: Path) -> None:
    qgc_input, qgc_output, px4_input, px4_output = _ports()
    config = SimulatorInteractiveRouterConfig(
        qgc_input_port=qgc_input,
        qgc_output_port=qgc_output,
        px4_input_port=px4_input,
        px4_output_endpoint=f"udpout:127.0.0.1:{px4_output}",
        qgc_control_mode="read_only",
    )
    with pytest.raises(SimulatorOrchestratorError, match="qgc_control_mode"):
        SimulatorOrchestrator(
            SCENARIO,
            dry_run=True,
            interactive=True,
            interactive_router_config=config,
            qgc_control_mode="sim_flight",
            job_root=tmp_path,
            px4_control_endpoint=config.px4_output_endpoint,
        )


def test_simulator_cli_exposes_qgc_control_mode() -> None:
    parser = build_parser()
    args = parser.parse_args(["run", "--scenario", str(SCENARIO)])
    assert args.qgc_control_mode is None
    args = parser.parse_args(
        ["run", "--scenario", str(SCENARIO), "--qgc-control-mode", "read_only"]
    )
    assert args.qgc_control_mode == "read_only"


def test_simulator_cli_exposes_live_ai_switch() -> None:
    parser = build_parser()
    args = parser.parse_args(["run", "--scenario", str(SCENARIO)])
    assert args.live_ai is False
    args = parser.parse_args(["run", "--scenario", str(SCENARIO), "--ai-live"])
    assert args.live_ai is True


def test_simulator_cli_exposes_manual_control_watchdog_override() -> None:
    parser = build_parser()
    args = parser.parse_args(["run", "--scenario", str(SCENARIO)])
    assert args.manual_watchdog_seconds is None
    args = parser.parse_args(
        [
            "run",
            "--scenario",
            str(SCENARIO),
            "--manual-watchdog-seconds",
            "0.75",
        ]
    )
    assert args.manual_watchdog_seconds == pytest.approx(0.75)


def test_noninteractive_orchestrator_keeps_manual_watchdog_disabled_by_default(
    tmp_path: Path,
) -> None:
    orchestrator = SimulatorOrchestrator(
        SCENARIO,
        dry_run=True,
        interactive=False,
        job_root=tmp_path,
    )
    assert orchestrator.control_adapter.config.manual_watchdog_s is None


def test_interactive_qgc_command_uses_native_isolated_hil_ports(tmp_path: Path) -> None:
    qgc_input, qgc_output, px4_input, px4_output = _ports()
    seen: list[list[str]] = []

    def factory(command: list[str], **_kwargs: object) -> _FakeProcess:
        seen.append(command)
        return _FakeProcess()

    orchestrator = SimulatorOrchestrator(
        SCENARIO,
        interactive=True,
        job_root=tmp_path,
        qgc_control_port=qgc_input,
        qgc_output_port=qgc_output,
        px4_telemetry_port=px4_input,
        px4_control_endpoint=f"udpout:127.0.0.1:{px4_output}",
        process_factory=factory,
        process_specs=(
            SimulatorProcessSpec("px4", ("fake-px4",)),
            SimulatorProcessSpec("gazebo", ("fake-gz",)),
            SimulatorProcessSpec("qgc", ("fake-qgc",)),
        ),
    )
    result = orchestrator.start()
    try:
        assert result.status == "running"
        qgc = next(item for item in seen if item[0] == "fake-qgc")
        assert "--isolated-hil" in qgc
        assert qgc[qgc.index("--isolated-hil-px4-udp-port") + 1] == str(qgc_output)
        assert qgc[qgc.index("--isolated-hil-px4-target-port") + 1] == str(qgc_input)
        assert orchestrator.interactive_router is not None
        assert orchestrator.interactive_router.ready_event.is_set()
    finally:
        orchestrator.stop()

    assert orchestrator.last_result is not None
    assert orchestrator.last_result.status == "stopped"
    assert (orchestrator.job_dir / "interactive-router.json").is_file()


def test_custom_interactive_router_throttle_mode_must_match_adapter(tmp_path: Path) -> None:
    qgc_input, qgc_output, px4_input, px4_output = _ports()
    config = SimulatorInteractiveRouterConfig(
        qgc_input_port=qgc_input,
        qgc_output_port=qgc_output,
        px4_input_port=px4_input,
        px4_output_endpoint=f"udpout:127.0.0.1:{px4_output}",
        throttle_wire_mode="signed",
    )
    with pytest.raises(SimulatorOrchestratorError, match="throttle_wire_mode"):
        SimulatorOrchestrator(
            SCENARIO,
            dry_run=True,
            interactive=True,
            job_root=tmp_path,
            interactive_router_config=config,
            # The default adapter mode is qgc_legacy, so a concrete signed
            # router mode must be rejected instead of silently diverging.
            px4_control_endpoint=config.px4_output_endpoint,
        )


def test_custom_auto_router_mode_accepts_concrete_adapter_and_owns_ports(
    tmp_path: Path,
) -> None:
    qgc_input, qgc_output, px4_input, px4_output = _ports()
    config = SimulatorInteractiveRouterConfig(
        qgc_input_port=qgc_input,
        qgc_output_port=qgc_output,
        px4_input_port=px4_input,
        px4_output_endpoint=f"udpout:127.0.0.1:{px4_output}",
        throttle_wire_mode="auto",
    )
    orchestrator = SimulatorOrchestrator(
        SCENARIO,
        dry_run=True,
        interactive=True,
        throttle_wire_mode="signed",
        job_root=tmp_path,
        interactive_router_config=config,
        px4_control_endpoint=config.px4_output_endpoint,
    )
    assert orchestrator.qgc_output_port == qgc_output
    assert orchestrator.px4_telemetry_port == px4_input
    assert orchestrator.control_adapter.config.throttle_wire_mode == "signed"
    assert orchestrator.interactive_router_config.throttle_wire_mode == "auto"
    assert orchestrator.control_gateway.config.throttle_wire_mode == "signed"
    result = orchestrator.start()
    assert result.status == "planned"
    assert result.interactive_router["throttle_wire_mode"] == "auto"


def test_custom_interactive_router_port_conflict_is_rejected(tmp_path: Path) -> None:
    qgc_input, qgc_output, px4_input, px4_output = _ports()
    config = SimulatorInteractiveRouterConfig(
        qgc_input_port=qgc_input,
        qgc_output_port=qgc_output,
        px4_input_port=px4_input,
        px4_output_endpoint=f"udpout:127.0.0.1:{px4_output}",
    )
    with pytest.raises(SimulatorOrchestratorError, match="qgc_input_port"):
        SimulatorOrchestrator(
            SCENARIO,
            dry_run=True,
            interactive=True,
            job_root=tmp_path,
            interactive_router_config=config,
            qgc_control_port=qgc_input + 10,
            px4_control_endpoint=config.px4_output_endpoint,
        )


def test_legacy_control_gateway_remains_default_without_qgc(tmp_path: Path) -> None:
    qgc_input, _, _, px4_output = _ports()
    orchestrator = SimulatorOrchestrator(
        SCENARIO,
        job_root=tmp_path,
        qgc_control_port=qgc_input,
        px4_control_endpoint=f"udpout:127.0.0.1:{px4_output}",
        process_factory=lambda *_args, **_kwargs: _FakeProcess(),
        process_specs=(SimulatorProcessSpec("fake", ("fake",)),),
    )
    assert orchestrator.interactive is False
    assert orchestrator.interactive_router is None
    result = orchestrator.start()
    try:
        assert result.status == "running"
        assert result.interactive_router["enabled"] is False
    finally:
        orchestrator.stop()


def test_ai_step_uses_shared_executor_and_stop_neutralizes(tmp_path: Path) -> None:
    qgc_input, _, _, px4_output = _ports()
    orchestrator = SimulatorOrchestrator(
        SCENARIO,
        dry_run=True,
        job_root=tmp_path,
        qgc_control_port=qgc_input,
        px4_control_endpoint=f"udpout:127.0.0.1:{px4_output}",
    )
    orchestrator.prepare()
    orchestrator.enable_ai()
    decision = orchestrator.step_ai(
        {
            "observed_at_s": 1.0,
            "altitude_agl_m": 30.0,
            "link_healthy": True,
            "armed": True,
        },
        now_s=1.0,
    )
    # The policy and adapter are the same in dry-run; a local pymavlink
    # transport may be available, so either an emitted hover or a fail-closed
    # transport error is a valid deterministic result.
    assert decision.mode.value == "hover"
    assert decision.command is not None
    orchestrator.stop()
    assert orchestrator.last_result is not None
    assert (orchestrator.job_dir / "ai.json").is_file()
    control = json.loads((orchestrator.job_dir / "control.json").read_text(encoding="utf-8"))
    assert control["dry_run"] is True
    assert control["external_io_opened"] is False
    assert control["transport_sink"] == "in_process_recording_mavlink"


def test_live_ai_bridge_consumes_px4_telemetry_through_router(tmp_path: Path) -> None:
    qgc_input, qgc_output, px4_input, px4_output = _ports()
    orchestrator = SimulatorOrchestrator(
        SCENARIO,
        dry_run=True,
        interactive=True,
        live_ai=True,
        job_root=tmp_path,
        qgc_control_port=qgc_input,
        qgc_output_port=qgc_output,
        px4_telemetry_port=px4_input,
        px4_control_endpoint=f"udpout:127.0.0.1:{px4_output}",
    )
    assert orchestrator.live_ai_bridge is not None
    assert orchestrator.interactive_router is not None
    assert orchestrator.interactive_router.telemetry_sink is not None
    assert orchestrator.interactive_router.watchdog_sink is not None
    orchestrator.prepare()
    orchestrator.enable_ai()
    # PX4 HEARTBEAT wire payload: custom_mode, type, autopilot, base mode
    # (armed), system status and MAVLink version.  The router/bridge sees
    # this as observed telemetry; no simulator truth is injected.
    heartbeat = (
        bytes([0xFE, 9, 0, 1, 1, 0]) + struct.pack("<IBBBBB", 0, 2, 12, 128, 4, 3) + b"\x00\x00"
    )
    assert orchestrator.interactive_router is not None
    assert (
        orchestrator.interactive_router.process_px4_datagram(
            heartbeat, source=("127.0.0.1", 14540), timestamp_s=10.0
        )
        == 1
    )
    orchestrator.stop()
    evidence = json.loads((orchestrator.job_dir / "ai.json").read_text(encoding="utf-8"))
    bridge = evidence["live_bridge"]
    assert evidence["live_ai_enabled"] is True
    assert bridge["frames_observed"] == 1
    assert bridge["heartbeat_seen"] is True
    assert bridge["snapshots"] == 1
    assert bridge["ai_steps"] >= 1
    router_evidence = json.loads(
        (orchestrator.job_dir / "interactive-router.json").read_text(encoding="utf-8")
    )
    assert router_evidence["telemetry_sink_enabled"] is True
    assert router_evidence["px4_telemetry_sink_frames"] == 1


def test_live_ai_watchdog_sink_drives_bridge_freshness_and_persists_evidence(
    tmp_path: Path,
) -> None:
    qgc_input, qgc_output, px4_input, px4_output = _ports()
    orchestrator = SimulatorOrchestrator(
        SCENARIO,
        dry_run=True,
        interactive=True,
        live_ai=True,
        stack_plan_enabled=False,
        job_root=tmp_path,
        qgc_control_port=qgc_input,
        qgc_output_port=qgc_output,
        px4_telemetry_port=px4_input,
        px4_control_endpoint=f"udpout:127.0.0.1:{px4_output}",
    )
    orchestrator.prepare()
    orchestrator.enable_ai()
    router = orchestrator.interactive_router
    bridge = orchestrator.live_ai_bridge
    assert router is not None and bridge is not None

    # One armed PX4 heartbeat releases the bridge's startup gate.  Calling
    # the router watchdog later than the bridge's default two-second lease
    # must take the same stale/failsafe path as a live router event loop.
    heartbeat = (
        bytes([0xFE, 9, 0, 1, 1, 0]) + struct.pack("<IBBBBB", 0, 2, 12, 128, 4, 3) + b"\x00\x00"
    )
    assert (
        router.process_px4_datagram(heartbeat, source=("127.0.0.1", 14540), timestamp_s=10.0) == 1
    )
    decision = router.poll_watchdog(now_s=13.0)
    assert decision is not None
    assert decision.mode.value == "failsafe"
    # The adapter watchdog is intentionally shorter than the bridge heartbeat
    # lease in the default orchestrator profile, so the shared executor may
    # report its canonical watchdog reason while the bridge records the
    # heartbeat stale transition.
    assert decision.reason == "ai_control_watchdog_expired"

    evidence = router.evidence()
    assert evidence["watchdog_sink_enabled"] is True
    assert evidence["watchdog_sink_calls"] == 1
    assert evidence["watchdog_sink_errors"] == 0
    assert evidence["watchdog_ticks"] == 1
    assert bridge.evidence()["heartbeat_stale"] is True
    ai_evidence = json.loads((orchestrator.job_dir / "ai.json").read_text(encoding="utf-8"))
    assert ai_evidence["live_bridge"]["heartbeat_stale"] is True

    orchestrator.stop()


def test_watchdog_evidence_write_failure_does_not_duplicate_neutral_output(
    tmp_path: Path,
) -> None:
    """A telemetry-evidence failure must not make router fallback emit twice."""

    qgc_input, qgc_output, px4_input, px4_output = _ports()
    orchestrator = SimulatorOrchestrator(
        SCENARIO,
        dry_run=True,
        interactive=True,
        live_ai=True,
        stack_plan_enabled=False,
        job_root=tmp_path,
        qgc_control_port=qgc_input,
        qgc_output_port=qgc_output,
        px4_telemetry_port=px4_input,
        px4_control_endpoint=f"udpout:127.0.0.1:{px4_output}",
    )
    orchestrator.prepare()
    orchestrator.enable_ai()
    router = orchestrator.interactive_router
    assert router is not None

    heartbeat = (
        bytes([0xFE, 9, 0, 1, 1, 0]) + struct.pack("<IBBBBB", 0, 2, 12, 128, 4, 3) + b"\x00\x00"
    )
    assert (
        router.process_px4_datagram(heartbeat, source=("127.0.0.1", 14540), timestamp_s=10.0) == 1
    )

    original_write_json = orchestrator._write_json
    failed = False

    def fail_once(name: str, value: object) -> None:
        nonlocal failed
        if name == "ai.json" and not failed:
            failed = True
            raise OSError("evidence storage unavailable")
        original_write_json(name, value)

    orchestrator._write_json = fail_once  # type: ignore[method-assign]
    decision = router.poll_watchdog(now_s=13.0)
    assert decision is None
    evidence = router.evidence()
    assert evidence["watchdog_sink_calls"] == 0
    assert evidence["watchdog_sink_errors"] == 1
    assert "evidence storage unavailable" in evidence["watchdog_sink_last_error"]
    # The bridge watchdog already consumed the adapter timestamp and emitted
    # the canonical neutral frame.  Router fallback must therefore be a no-op.
    assert orchestrator.control_adapter.evidence()["watchdog_neutral_count"] == 1
    assert orchestrator.control_adapter.evidence()["messages_sent"] == 2

    orchestrator._write_json = original_write_json  # type: ignore[method-assign]
    orchestrator.stop()


def test_live_ai_requires_interactive_router(tmp_path: Path) -> None:
    with pytest.raises(SimulatorOrchestratorError, match="live_ai requires"):
        SimulatorOrchestrator(SCENARIO, dry_run=True, live_ai=True, job_root=tmp_path)


def test_live_ai_evidence_writes_are_atomic_under_router_concurrency(
    tmp_path: Path,
) -> None:
    """A telemetry callback must never expose a half-written JSON document.

    ``_consume_live_telemetry`` runs on the router thread while lifecycle and
    explicit ``step_ai`` callers can publish evidence concurrently.  Exercise
    the same private writer with a reader racing several writers; an atomic
    same-directory replace is required for every observed document to remain
    parseable.
    """

    qgc_input, qgc_output, px4_input, px4_output = _ports()
    orchestrator = SimulatorOrchestrator(
        SCENARIO,
        dry_run=True,
        interactive=True,
        live_ai=True,
        stack_plan_enabled=False,
        job_root=tmp_path,
        qgc_control_port=qgc_input,
        qgc_output_port=qgc_output,
        px4_telemetry_port=px4_input,
        px4_control_endpoint=f"udpout:127.0.0.1:{px4_output}",
    )
    orchestrator.prepare()
    assert orchestrator.job_dir is not None
    evidence_path = orchestrator.job_dir / "ai.json"
    failures: list[str] = []
    writer_failures: list[str] = []
    done = threading.Event()

    def reader() -> None:
        while not done.is_set():
            try:
                json.loads(evidence_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except PermissionError:
                # Windows may briefly deny a read while the writer closes its
                # replacement handle; this is orthogonal to JSON atomicity.
                continue
            except json.JSONDecodeError as exc:
                failures.append(str(exc))
                # Keep sampling to catch multiple torn writes without making
                # the test depend on a particular scheduling interleaving.
            time.sleep(0.001)

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()

    def writer(index: int) -> None:
        try:
            for sequence in range(24):
                orchestrator._write_json(
                    "ai.json",
                    {
                        "writer": index,
                        "sequence": sequence,
                        # A payload larger than one filesystem sector makes a
                        # truncate-then-write implementation observably racy.
                        "payload": "x" * 32768,
                    },
                )
        except BaseException as exc:  # pragma: no cover - surfaced below
            writer_failures.append(f"writer {index}: {exc!r}")

    workers = [threading.Thread(target=writer, args=(index,)) for index in range(3)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10.0)
        assert not worker.is_alive()
    # Give the reader one final scheduling slice after the last replacement.
    time.sleep(0.02)
    done.set()
    reader_thread.join(timeout=2.0)
    assert not reader_thread.is_alive()
    assert writer_failures == []
    assert failures == []
    final = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert final["writer"] in {0, 1, 2}
    assert 0 <= final["sequence"] < 24
    assert not list(orchestrator.job_dir.glob(".ai.json.*.tmp"))
    orchestrator.stop()


def test_initial_ai_state_syncs_executor(tmp_path: Path) -> None:
    _, _, _, px4_output = _ports()
    orchestrator = SimulatorOrchestrator(
        SCENARIO,
        dry_run=True,
        initial_authority=ControlAuthorityState.AI,
        job_root=tmp_path,
        px4_control_endpoint=f"udpout:127.0.0.1:{px4_output}",
    )
    assert orchestrator.authority.state is ControlAuthorityState.AI
    assert orchestrator.ai_executor.enabled is True
