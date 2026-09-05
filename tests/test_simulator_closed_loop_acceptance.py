from __future__ import annotations

from multidetect.simulator.__main__ import build_parser
from scripts.run_simulator_closed_loop_acceptance import run_acceptance


def test_closed_loop_acceptance_covers_vtol_and_fixed_wing() -> None:
    result = run_acceptance()
    assert result["gate_passed"] is True
    assert [item["mode"] for item in result["modes"]] == ["vtol", "fixed-wing"]
    for item in result["modes"]:
        gate = item["gate"]
        assert gate["hover_accepted"] is True
        assert gate["track_accepted"] is True
        assert gate["manual_takeover"] is True
        assert gate["manual_suppressed_ai"] is True
        assert gate["ai_resumed"] is True
        assert gate["truth_free"] is True
    assert result["claims"]["shared_ai_manual_mavlink_path"] is True
    assert result["claims"]["network_or_serial_io_opened"] is False


def test_simulator_cli_exposes_closed_loop_subcommand() -> None:
    args = build_parser().parse_args(
        ["closed-loop", "--mode", "fixed-wing", "--steps", "3", "--dt", "0.2"]
    )
    assert args.command == "closed-loop"
    assert args.mode == "fixed-wing"
    assert args.steps == 3
