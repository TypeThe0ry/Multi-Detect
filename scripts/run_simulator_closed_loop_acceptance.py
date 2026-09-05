"""Run the deterministic game-like SIM closed-loop acceptance fixture.

The fixture exercises the historical VTOL and fixed-wing dynamics, feeds an observed
target measurement into the same truth-free AI executor, proves manual
takeover/release, and records a canonical evidence bundle.  It does not open
Gazebo, QGC, a serial port, or a network socket; the production interactive
job uses the same adapter with PX4 SITL as its transport sink.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    _repository_source = Path(__file__).resolve().parents[1] / "src"
    if _repository_source.is_dir() and str(_repository_source) not in sys.path:
        sys.path.insert(0, str(_repository_source))

from multidetect.simulator import (
    FlightMode,
    SimulatorClosedLoop,
    SimulatorClosedLoopStep,
)

ROOT = Path(__file__).resolve().parents[1]


def _run_mode(mode: FlightMode) -> dict[str, Any]:
    sim = SimulatorClosedLoop(
        mode,
        seed=42017,
        wind_mps=(3.0, -1.0),
        dt_s=0.1,
    )
    try:
        # Explicit lifecycle transition mirrors the QGC/PX4 ARM command and
        # keeps the model's constructor safely disarmed.
        sim.arm()
        sim.enable_ai()
        hover = sim.step()
        # This is a sensor-side observation, not scenario truth.  It uses the
        # current measured vehicle fields and adds the detector/ranging output
        # that a real bridge would provide.
        target_observation = replace(
            sim.telemetry,
            target_visible=True,
            target_bearing_deg=18.0,
            target_distance_m=90.0,
            target_confidence=0.86,
        )
        track = sim.step_with_observation(target_observation)
        manual_receipt = sim.submit_manual(
            roll=-0.20,
            pitch=0.10,
            yaw=0.08,
            throttle=0.65,
        )
        suppressed = sim.step()
        sim.release_manual_control()
        resumed = sim.step()
        # A bounded run after the hand-back demonstrates that the physics state
        # continues to evolve from the same AI/MAVLink command path.
        tail = sim.run(8)
        before_close = sim.evidence_with_hash()
        return {
            "mode": mode.value,
            "gate": {
                "hover_accepted": _accepted_mode(hover, "hover"),
                "track_accepted": _accepted_mode(track, "track"),
                "manual_takeover": manual_receipt.source == "manual",
                "manual_suppressed_ai": _accepted_mode(suppressed, "suppressed", accepted=False),
                "ai_resumed": _accepted_mode(resumed, "hover"),
                "tail_steps": len(tail),
                "truth_free": before_close["truth_free"] is True,
            },
            "decision_sequence": [
                _step_mapping(hover),
                _step_mapping(track),
                _step_mapping(suppressed),
                _step_mapping(resumed),
            ],
            "evidence": before_close,
        }
    finally:
        sim.close()


def _accepted_mode(
    step: SimulatorClosedLoopStep,
    mode: str,
    *,
    accepted: bool = True,
) -> bool:
    return (
        step.decision is not None
        and step.decision.mode.value == mode
        and step.decision.accepted is accepted
    )


def _step_mapping(step: SimulatorClosedLoopStep) -> dict[str, object]:
    # Keep the fixture compact while retaining the complete decision and final
    # telemetry contracts in the mode-level evidence document.
    return {
        "index": step.index,
        "mode": None if step.decision is None else step.decision.mode.value,
        "reason": None if step.decision is None else step.decision.reason,
        "accepted": None if step.decision is None else step.decision.accepted,
        "altitude_agl_m": step.telemetry_after.altitude_agl_m,
        "airspeed_mps": step.telemetry_after.airspeed_mps,
    }


def run_acceptance() -> dict[str, object]:
    modes = [_run_mode(FlightMode.VTOL), _run_mode(FlightMode.FIXED_WING)]
    gate_passed = all(
        all(bool(value) for value in item["gate"].values() if isinstance(value, bool))
        and item["gate"]["tail_steps"] == 8
        for item in modes
    )
    return {
        "schema_version": 1,
        "event": "simulator_closed_loop_acceptance_finished",
        "gate_passed": gate_passed,
        "modes": modes,
        "claims": {
            "shared_ai_manual_mavlink_path": True,
            "truth_free_ai_input": True,
            "qgc_is_sole_operator_ui": True,
            "network_or_serial_io_opened": False,
            "visual_acceptance": "not_claimed",
            "pixhawk_jetson_camera_acceptance": "not_claimed",
            "real_flight_acceptance": "not_claimed",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    result = run_acceptance()
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.out is not None:
        output = args.out if args.out.is_absolute() else ROOT / args.out
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
