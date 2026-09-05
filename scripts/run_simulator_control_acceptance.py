"""Deterministic acceptance for the shared SIM manual/AI control path."""

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
    ControlAuthorityError,
    ControlAuthorityState,
    SimulatorControlAuthority,
    SimulatorControlGateway,
    SimulatorControlGatewayConfig,
    SimulatorMavlinkControlAdapter,
)
from multidetect.simulator.control_gateway import build_manual_control_frame


class _FakeMav:
    def __init__(self) -> None:
        self.messages: list[tuple[int, int, int, int, int, int]] = []

    def manual_control_send(
        self, target: int, x: int, y: int, z: int, r: int, buttons: int
    ) -> None:
        self.messages.append((target, x, y, z, r, buttons))


class _FakeConnection:
    def __init__(self) -> None:
        self.mav = _FakeMav()

    def close(self) -> None:
        return None


def run_acceptance() -> dict[str, object]:
    authority = SimulatorControlAuthority()
    connection = _FakeConnection()
    adapter = SimulatorMavlinkControlAdapter(authority, connection=connection)
    gateway = SimulatorControlGateway(
        adapter,
        config=SimulatorControlGatewayConfig(
            qgc_input_port=14656,
            px4_output_endpoint=adapter.config.endpoint,
        ),
    )

    authority.enable_ai()
    authority.release_manual_control()
    ai_receipt = adapter.submit_ai(
        roll=0.20,
        pitch=-0.10,
        yaw=0.15,
        throttle=0.55,
        timestamp_s=1.0,
    )
    manual_receipts = gateway.process_datagram(
        build_manual_control_frame(
            pitch=0.25,
            roll=-0.30,
            yaw=-0.05,
            throttle=0.60,
        ),
        source=("127.0.0.1", 50100),
        timestamp_s=1.1,
    )
    manual_took_over = authority.state is ControlAuthorityState.MANUAL
    try:
        adapter.submit_ai(roll=0, pitch=0, yaw=0, throttle=0.5, timestamp_s=1.2)
    except ControlAuthorityError:
        ai_blocked_after_manual = True
    else:
        ai_blocked_after_manual = False

    authority.release_manual_control()
    adapter.submit_ai(roll=0.10, pitch=0, yaw=0, throttle=0.50, timestamp_s=2.0)
    watchdog_receipt = gateway.poll_watchdog(now_s=2.5)
    watchdog_failsafe = (
        watchdog_receipt is not None and authority.state is ControlAuthorityState.FAILSAFE
    )
    evidence = gateway.evidence()
    result = {
        "schema_version": 1,
        "event": "simulator_control_acceptance_finished",
        "gate_passed": bool(
            ai_receipt.source == "ai"
            and len(manual_receipts) == 1
            and manual_took_over
            and ai_blocked_after_manual
            and watchdog_failsafe
            and connection.mav.messages[-1] == (1, 0, 0, 500, 0, 0)
        ),
        "manual_took_over": manual_took_over,
        "ai_blocked_after_manual": ai_blocked_after_manual,
        "watchdog_failsafe": watchdog_failsafe,
        "mavlink_messages": len(connection.mav.messages),
        "last_mavlink_message": connection.mav.messages[-1],
        "gateway": evidence,
        "three_d_runtime_visual_acceptance": "not_claimed",
        "hardware_acceptance": "not_claimed",
    }
    adapter.close()
    return result


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
