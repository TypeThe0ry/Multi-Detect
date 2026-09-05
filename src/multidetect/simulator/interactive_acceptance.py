"""Deterministic acceptance fixture for the native QGC SIM control path.

The fixture drives the same loopback router that an interactive QGC/PX4 job
uses, but replaces the UDP sockets and PX4 process with the deterministic
flight-model connection.  This gives us a small, repeatable contract test for
the *interactive* path:

``QGC MANUAL_CONTROL -> interactive router -> shared adapter -> flight model``

and, independently,

``observed telemetry -> AI executor -> shared adapter -> flight model``.

It intentionally does not create a second UI.  The only operator-facing
surface represented by the fixture is the native QGC MANUAL_CONTROL frame.
The resulting evidence separates protocol/authority acceptance from desktop
visual, PX4/Gazebo, and hardware acceptance.
"""

from __future__ import annotations

import argparse
import json
import struct
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..autonomy.serialization import canonical_sha256
from ..clock import ManualClock
from ..qgc_readonly_bridge import (
    MSG_HEARTBEAT,
    MSG_PARAM_REQUEST_LIST,
)
from ..runtime import RuntimeDescriptor, RuntimeMode, RuntimeProfile
from .ai_executor import SimulatorAiMode
from .closed_loop import SimulatorClosedLoop
from .control import ControlAuthorityState
from .control_gateway import build_manual_control_frame
from .flight_model import FlightMode
from .interactive_router import (
    SimulatorInteractiveRouter,
    SimulatorInteractiveRouterConfig,
)
from .mavlink_control import SimulatorMavlinkControlConfig

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = ROOT / "artifacts/evaluation/simulator-interactive-acceptance.json"
MSG_MISSION_COUNT = 44


def _v1_frame(
    message_id: int,
    payload: bytes,
    *,
    source_system: int,
    source_component: int,
    sequence: int = 0,
) -> bytes:
    """Build the compact v1 fixture accepted by the repository decoder.

    The acceptance parser intentionally treats checksum bytes as opaque (the
    production pymavlink transport computes the real CRC).  Keeping the frame
    builder here makes the test independent of an optional pymavlink install.
    """

    if not 0 <= int(message_id) <= 255:
        raise ValueError("v1 fixture message_id must fit MAVLink v1")
    if len(payload) > 255:
        raise ValueError("v1 fixture payload is too large")
    return (
        bytes(
            [
                0xFE,
                len(payload),
                sequence & 0xFF,
                source_system & 0xFF,
                source_component & 0xFF,
                message_id,
            ]
        )
        + bytes(payload)
        + b"\x00\x00"
    )


def _heartbeat(*, source_system: int = 1, source_component: int = 1) -> bytes:
    # The router only needs the minimum common heartbeat payload length for
    # this byte-for-byte telemetry-forwarding assertion.
    return _v1_frame(
        MSG_HEARTBEAT,
        bytes(9),
        source_system=source_system,
        source_component=source_component,
    )


def _param_request_list() -> bytes:
    # PARAM_REQUEST_LIST payload: target_system, target_component.
    return _v1_frame(
        MSG_PARAM_REQUEST_LIST,
        bytes((1, 1)),
        source_system=255,
        source_component=190,
    )


def _mission_write() -> bytes:
    # MISSION_COUNT is a write-side mission message and is intentionally not
    # in the interactive router's QGC read-only allowlist.
    return _v1_frame(
        MSG_MISSION_COUNT,
        struct.pack("<HBB", 0, 1, 1),
        source_system=255,
        source_component=190,
    )


def _moved(before: Any, after: Any) -> bool:
    """Return whether an observable vehicle field changed by one tick."""

    fields = (
        "latitude_deg",
        "longitude_deg",
        "altitude_agl_m",
        "ground_speed_mps",
        "heading_deg",
        "roll_deg",
        "pitch_deg",
    )
    return any(float(getattr(before, name)) != float(getattr(after, name)) for name in fields)


def _ports(index: int) -> tuple[int, int, int, int]:
    """Return deterministic, non-protected fixture ports for one mode."""

    base = 14760 + (index * 10)
    # qgc input, qgc receive/output, PX4 telemetry input, PX4 command output
    return base, base + 1, base + 2, base + 3


def _run_mode(mode: FlightMode, *, index: int) -> dict[str, object]:
    qgc_input, qgc_output, px4_input, px4_output = _ports(index)
    qgc_source_port = 55100 + index
    px4_source_port = 55200 + index
    seed = 42017 + index
    control_config = SimulatorMavlinkControlConfig(
        endpoint=f"udpout:127.0.0.1:{px4_output}",
        throttle_wire_mode="qgc_legacy",
        # Match the production interactive orchestrator: a QGC MANUAL_CONTROL
        # stream is fail-closed after 500 ms without a fresh frame.  Headless
        # deterministic callers keep the adapter default disabled elsewhere.
        manual_watchdog_s=0.5,
    )
    # Keep this fixture on the same runtime identity contract as the
    # manifest-driven simulator.  The descriptor is intentionally bound to
    # the fixture's vehicle/seed inputs rather than to any host path or wall
    # clock, so repeated acceptance runs produce comparable evidence.
    runtime_descriptor = RuntimeDescriptor(
        mode=RuntimeMode.SIM,
        profile=RuntimeProfile.sim(),
        backend_id="multidetect.simulator.interactive_acceptance",
        airframe_profile_hash=canonical_sha256(
            {
                "model": "deterministic-flight-model-v1",
                "mode": mode.value,
                "wind_mps": [3.0, -1.0],
                "dt_s": 0.1,
            }
        ),
        scenario_id=f"interactive-acceptance-{mode.value}",
        scenario_manifest_hash=canonical_sha256(
            {
                "event": "simulator_interactive_acceptance",
                "mode": mode.value,
                "index": index,
            }
        ),
        scenario_seed=seed,
        deterministic=True,
    )
    clock = ManualClock()
    sim = SimulatorClosedLoop(
        mode,
        seed=seed,
        wind_mps=(3.0, -1.0),
        dt_s=0.1,
        control_config=control_config,
        clock=clock,
        runtime_descriptor=runtime_descriptor,
    )
    to_qgc: list[tuple[bytes, tuple[str, int]]] = []
    to_px4: list[tuple[bytes, tuple[str, int]]] = []
    router = SimulatorInteractiveRouter(
        sim.adapter,
        config=SimulatorInteractiveRouterConfig(
            qgc_input_port=qgc_input,
            qgc_output_port=qgc_output,
            px4_input_port=px4_input,
            px4_output_endpoint=control_config.endpoint,
            qgc_source_port=qgc_source_port,
            px4_source_port=px4_source_port,
            throttle_wire_mode="qgc_legacy",
        ),
        qgc_sender=lambda frame, destination: to_qgc.append((frame, destination)),
        px4_sender=lambda frame, destination: to_px4.append((frame, destination)),
        lifecycle_sink=sim.connection.mav,
        clock=clock.monotonic,
    )

    try:
        # AI runs first from an observed target measurement.  This is the
        # same telemetry contract a detector/ranging bridge supplies; no
        # scenario truth is passed into the executor.
        # Lifecycle is explicit even in the fixture: QGC/PX4 would emit an
        # ARM command before handing authority to the AI stream.
        sim.arm()
        sim.enable_ai()
        initial = sim.telemetry
        ai_observation = replace(
            initial,
            target_visible=True,
            target_bearing_deg=18.0,
            target_distance_m=90.0,
            target_confidence=0.86,
        )
        ai_step = sim.step_with_observation(ai_observation)
        after_ai = sim.telemetry

        # A native QGC joystick frame enters through the interactive router.
        # It is decoded once, then the shared adapter emits the canonical
        # MANUAL_CONTROL frame into the model sink.
        manual_before = sim.telemetry
        manual_receipts = router.process_qgc_datagram(
            build_manual_control_frame(
                pitch=0.10,
                roll=-0.20,
                yaw=0.08,
                throttle=0.65,
            ),
            source=("127.0.0.1", qgc_source_port),
            timestamp_s=sim.time_s,
        )
        manual_step = sim.step()
        manual_took_over = sim.authority.state is ControlAuthorityState.MANUAL

        # The exact same AI executor is still enabled, but manual authority
        # suppresses its decision until the operator explicitly releases it.
        manual_suppressed = (
            manual_step.decision is not None
            and manual_step.decision.mode is SimulatorAiMode.SUPPRESSED
            and not manual_step.decision.accepted
        )

        sim.release_manual_control()
        resumed_step = sim.step_with_observation(
            replace(
                sim.telemetry,
                target_visible=True,
                target_bearing_deg=-12.0,
                target_distance_m=70.0,
                target_confidence=0.90,
            )
        )
        ai_resumed_after_release = (
            resumed_step.decision is not None
            and resumed_step.decision.mode is SimulatorAiMode.TRACK
            and resumed_step.decision.accepted
            and sim.authority.state is ControlAuthorityState.AI
        )

        # Let the router own the watchdog tick, just as its socket event loop
        # does in the real interactive job.  The adapter must emit a neutral
        # legacy-throttle frame and transition the shared authority to
        # FAILSAFE.
        watchdog_now = sim.time_s + sim.adapter.config.watchdog_s + 0.01
        watchdog_receipt = router.poll_watchdog(now_s=watchdog_now)
        watchdog_failsafe = (
            watchdog_receipt is not None
            and watchdog_receipt.source == "watchdog"
            and sim.authority.state is ControlAuthorityState.FAILSAFE
        )

        # PX4 telemetry is forwarded byte-for-byte to QGC's native receive
        # endpoint, while a read-only QGC request is the only non-manual frame
        # allowed towards PX4.
        heartbeat = _heartbeat()
        forwarded_telemetry = router.process_px4_datagram(
            heartbeat,
            source=("127.0.0.1", px4_source_port),
        )
        readonly_frame = _param_request_list()
        readonly_receipts = router.process_qgc_datagram(
            readonly_frame,
            source=("127.0.0.1", qgc_source_port),
        )
        write_frame = _mission_write()
        blocked_write_receipts = router.process_qgc_datagram(
            write_frame,
            source=("127.0.0.1", qgc_source_port),
        )

        # Identity and isolation probes must fail closed without reaching the
        # adapter or either forwarding callback.
        router.process_qgc_datagram(
            build_manual_control_frame(pitch=0.1, roll=0.1, yaw=0.0, throttle=0.5),
            source=("192.0.2.10", qgc_source_port),
        )
        router.process_qgc_datagram(
            build_manual_control_frame(pitch=0.1, roll=0.1, yaw=0.0, throttle=0.5),
            source=("127.0.0.1", qgc_source_port + 1),
        )
        router.process_qgc_datagram(
            build_manual_control_frame(
                pitch=0.1,
                roll=0.1,
                yaw=0.0,
                throttle=0.5,
                source_system=42,
            ),
            source=("127.0.0.1", qgc_source_port),
        )
        router.process_qgc_datagram(
            build_manual_control_frame(
                pitch=0.1,
                roll=0.1,
                yaw=0.0,
                throttle=0.5,
                target_system=2,
            ),
            source=("127.0.0.1", qgc_source_port),
        )
        router.process_px4_datagram(
            heartbeat,
            source=("192.0.2.11", px4_source_port),
        )
        router.process_px4_datagram(
            _heartbeat(source_system=42),
            source=("127.0.0.1", px4_source_port),
        )

        loop_evidence = sim.evidence_with_hash()
        router_evidence = router.evidence()
        transport_evidence = sim.connection.evidence()
        last_message = transport_evidence["last_message"]
        gate = {
            "ai_track_accepted": (
                ai_step.decision is not None
                and ai_step.decision.mode is SimulatorAiMode.TRACK
                and ai_step.decision.accepted
            ),
            "ai_advanced_vehicle": _moved(initial, after_ai),
            "qgc_manual_accepted": len(manual_receipts) == 1,
            "manual_took_over": manual_took_over,
            "manual_advanced_vehicle": _moved(manual_before, manual_step.telemetry_after),
            "manual_suppressed_ai": manual_suppressed,
            "ai_resumed_after_release": ai_resumed_after_release,
            "watchdog_failsafe": watchdog_failsafe,
            "neutral_watchdog_frame": last_message == [1, 0, 0, 500, 0, 0],
            "telemetry_forwarded_byte_for_byte": (
                forwarded_telemetry == 1
                and bool(to_qgc)
                and to_qgc[-1] == (heartbeat, router.config.qgc_output_endpoint)
            ),
            "readonly_qgc_frame_forwarded": (
                not readonly_receipts
                and bool(to_px4)
                and to_px4[-1] == (readonly_frame, router.config.px4_output_address)
            ),
            "mission_write_blocked": not blocked_write_receipts,
            "truth_free_closed_loop": loop_evidence["truth_free"] is True,
            "loopback_and_identity_counters": (
                router_evidence["qgc_non_loopback_datagrams_blocked"] == 1
                and router_evidence["qgc_unexpected_source_datagrams_blocked"] == 1
                and router_evidence["px4_non_loopback_datagrams_blocked"] == 1
                and router_evidence["px4_unexpected_source_datagrams_blocked"] == 0
                and router_evidence["qgc_rejected_reasons"]["qgc_source_system_mismatch"] == 1
                and router_evidence["qgc_rejected_reasons"]["manual_control_target_mismatch"] == 1
                and router_evidence["qgc_rejected_reasons"]["qgc_message_not_allowed"] == 1
                and router_evidence["px4_rejected_reasons"]["px4_source_system_mismatch"] == 1
            ),
        }
        return {
            "mode": mode.value,
            "gate": gate,
            "gate_passed": all(bool(value) for value in gate.values()),
            "ai_step": ai_step.to_mapping(),
            "manual_step": manual_step.to_mapping(),
            "resumed_step": resumed_step.to_mapping(),
            "watchdog_receipt": _receipt_mapping(watchdog_receipt),
            "router": router_evidence,
            "transport": transport_evidence,
            "closed_loop": loop_evidence,
            "runtime_descriptor": runtime_descriptor.to_mapping(),
            "runtime_descriptor_hash": runtime_descriptor.canonical_hash,
            "claims": {
                "qgc_native_manual_control_path": True,
                "shared_ai_manual_authority": True,
                "truth_free_ai_input": True,
                "qgc_is_sole_operator_ui": True,
                "visual_acceptance": "not_claimed",
                "px4_gazebo_acceptance": "not_claimed",
                "pixhawk_jetson_camera_acceptance": "not_claimed",
                "real_flight_acceptance": "not_claimed",
            },
        }
    finally:
        # ``close`` is idempotent and leaves the evidence usable even when a
        # future assertion or transport probe raises.
        router.stop()
        sim.close()


def _receipt_mapping(receipt: object | None) -> dict[str, object] | None:
    if receipt is None:
        return None
    return {
        "source": receipt.source,
        "state": receipt.state,
        "endpoint": receipt.endpoint,
        "target_system": receipt.target_system,
        "x": receipt.x,
        "y": receipt.y,
        "z": receipt.z,
        "r": receipt.r,
        "timestamp_s": receipt.timestamp_s,
    }


def run_acceptance(*, mode: str = "both") -> dict[str, object]:
    """Run one or more deterministic vehicle modes.

    ``both`` preserves the historical VTOL/fixed-wing baseline.  ``all``
    adds the plain PX4 multirotor path so CI can exercise all supported
    airframes without changing the established baseline artifact shape.
    """

    normalized = mode.strip().lower().replace("_", "-") if isinstance(mode, str) else mode
    if normalized == "both":
        selected = [FlightMode.VTOL, FlightMode.FIXED_WING]
    elif normalized == "all":
        selected = [FlightMode.VTOL, FlightMode.FIXED_WING, FlightMode.MULTIROTOR]
    else:
        try:
            # Reuse the flight-model alias table (``quadcopter``, ``x500``,
            # ``multi_rotor`` and friends) instead of maintaining a second
            # list that can drift from the actual model parser.
            selected = [FlightMode.parse(normalized)]
        except (TypeError, ValueError) as exc:
            raise ValueError("mode must be both, all, vtol, fixed-wing or multirotor") from exc
    modes = [_run_mode(item, index=index) for index, item in enumerate(selected)]
    return {
        "schema_version": 1,
        "event": "simulator_interactive_acceptance_finished",
        "gate_passed": all(bool(item["gate_passed"]) for item in modes),
        "modes": modes,
        "claims": {
            "qgc_native_manual_control_path": True,
            "shared_ai_manual_authority": True,
            "loopback_only": True,
            "qgc_is_sole_operator_ui": True,
            "visual_acceptance": "not_claimed",
            "px4_gazebo_acceptance": "not_claimed",
            "pixhawk_jetson_camera_acceptance": "not_claimed",
            "real_flight_acceptance": "not_claimed",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("both", "all", "vtol", "fixed-wing", "multirotor"),
        default="both",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        result = run_acceptance(mode=args.mode)
    except (OSError, TypeError, ValueError, RuntimeError, KeyError) as exc:
        print(json.dumps({"event": "simulator_interactive_acceptance_failed", "error": str(exc)}))
        return 2
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out is not None:
        output = args.out if args.out.is_absolute() else ROOT / args.out
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["gate_passed"] else 1


__all__ = ["DEFAULT_OUTPUT", "main", "run_acceptance"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
