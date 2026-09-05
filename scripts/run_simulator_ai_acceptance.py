"""Deterministic SIM AI reasoning/execution gate.

This fixture drives the same production path used by an interactive job:
observed telemetry -> explainable policy -> shared authority -> MAVLink
``MANUAL_CONTROL`` adapter.  A QGC frame is injected between AI steps to prove
that human takeover suppresses AI, then AI is explicitly released again.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    _repository_source = Path(__file__).resolve().parents[1] / "src"
    if _repository_source.is_dir() and str(_repository_source) not in sys.path:
        sys.path.insert(0, str(_repository_source))

from multidetect.simulator import (
    ControlAuthorityState,
    SimulatorAiExecutor,
    SimulatorControlAuthority,
    SimulatorInteractiveRouter,
    SimulatorInteractiveRouterConfig,
    SimulatorMavlinkControlAdapter,
    SimulatorTelemetrySnapshot,
)
from multidetect.simulator.control_gateway import build_manual_control_frame
from multidetect.simulator.interactive_router import DEFAULT_PX4_INPUT_PORT


class _FakeMav:
    def __init__(self) -> None:
        self.messages: list[tuple[int, int, int, int, int, int]] = []

    def manual_control_send(self, *args: int) -> None:
        self.messages.append(tuple(args))


class _FakeConnection:
    def __init__(self) -> None:
        self.mav = _FakeMav()

    def close(self) -> None:
        return None


def _snapshot(**overrides: object) -> SimulatorTelemetrySnapshot:
    values: dict[str, object] = {
        "observed_at_s": 1.0,
        "altitude_agl_m": 30.0,
        "ground_speed_mps": 8.0,
        "heading_deg": 0.0,
        "link_healthy": True,
        "armed": True,
        # GPS quality is an observed contract.  A missing satellite count is
        # deliberately unknown and must fail the return gate, so the fixture
        # supplies a concrete healthy value for its positive path.
        "satellites_visible": 12,
        "gps_observed_at_s": 1.0,
    }
    values.update(overrides)
    return SimulatorTelemetrySnapshot(**values)


def _ports() -> tuple[int, int, int, int]:
    # Keep fixture endpoints deterministic and independent from the normal
    # gateway defaults.  No sockets are opened by this acceptance gate.
    return (14670, 14669, 14671, DEFAULT_PX4_INPUT_PORT + 30)


def run_acceptance() -> dict[str, object]:
    authority = SimulatorControlAuthority()
    connection = _FakeConnection()
    adapter = SimulatorMavlinkControlAdapter(authority, connection=connection)
    executor = SimulatorAiExecutor(adapter)
    qgc_input, qgc_output, px4_input, px4_output = _ports()
    router = SimulatorInteractiveRouter(
        adapter,
        config=SimulatorInteractiveRouterConfig(
            qgc_input_port=qgc_input,
            qgc_output_port=qgc_output,
            px4_input_port=px4_input,
            px4_output_endpoint=f"udpout:127.0.0.1:{px4_output}",
        ),
    )

    executor.enable_ai()
    hover = executor.step(_snapshot(), now_s=1.0)
    track = executor.step(
        _snapshot(
            observed_at_s=1.1,
            target_visible=True,
            target_bearing_deg=18.0,
            target_distance_m=90.0,
            target_confidence=0.86,
        ),
        now_s=1.1,
    )

    manual = router.process_qgc_datagram(
        build_manual_control_frame(
            pitch=0.05,
            roll=-0.15,
            yaw=0.02,
            throttle=0.60,
        ),
        source=("127.0.0.1", 50123),
        timestamp_s=1.2,
    )
    suppressed = executor.step(_snapshot(observed_at_s=1.3), now_s=1.3)

    executor.release_manual_control()
    returned = executor.step(
        _snapshot(
            observed_at_s=1.4,
            return_requested=True,
            home_bearing_deg=-25.0,
            home_distance_m=120.0,
            latitude_deg=1.3001,
            longitude_deg=103.8001,
            gps_fix_type=3,
            gps_horizontal_accuracy_m=2.5,
            gps_vertical_accuracy_m=2.5,
            gps_observed_at_s=1.4,
        ),
        now_s=1.4,
    )
    # An unhealthy link must enter the same adapter neutral/failsafe path.
    fault = executor.step(
        _snapshot(observed_at_s=1.5, link_healthy=False),
        now_s=1.5,
    )

    evidence = {
        "schema_version": 1,
        "event": "simulator_ai_acceptance_finished",
        "gate_passed": bool(
            hover.mode.value == "hover"
            and hover.accepted
            and track.mode.value == "track"
            and track.accepted
            and len(manual) == 1
            and suppressed.mode.value == "suppressed"
            and not suppressed.accepted
            and returned.mode.value == "return"
            and returned.accepted
            and fault.mode.value == "failsafe"
            and authority.state is ControlAuthorityState.FAILSAFE
            and connection.mav.messages[-1] == (1, 0, 0, 500, 0, 0)
        ),
        "decision_sequence": [
            hover.to_mapping(),
            track.to_mapping(),
            suppressed.to_mapping(),
            returned.to_mapping(),
            fault.to_mapping(),
        ],
        "executor": executor.evidence(),
        "interactive_router": router.evidence(),
        "mavlink_messages": len(connection.mav.messages),
        "last_mavlink_message": connection.mav.messages[-1],
        "claims": {
            "reasoned_policy_used": True,
            "shared_manual_ai_authority": True,
            "truth_free_input": True,
            "qgc_is_sole_operator_ui": True,
            "visual_acceptance": "not_claimed",
            "pixhawk_hardware_acceptance": "not_claimed",
        },
    }
    adapter.close()
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    result = run_acceptance()
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
