from __future__ import annotations

import json
from pathlib import Path

import pytest

from multidetect.simulator.__main__ import build_parser, main

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "configs" / "scenarios" / "river_rescue_vtol_001.scenario.json"


def test_stack_plan_parser_exposes_required_options_and_aliases() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "stack-plan",
            "--scenario",
            str(SCENARIO),
            "--repo-root",
            str(ROOT),
            "--gazebo",
            "gazebo",
            "--px4",
            "px4",
            "--flavor",
            "gazebo",
            "--gui",
            "--no-realtime",
            "--px4-port",
            "14542",
        ]
    )

    assert args.command == "stack-plan"
    assert args.scenario == SCENARIO
    assert args.repository_root == ROOT
    assert args.gazebo_executable == "gazebo"
    assert args.px4_executable == "px4"
    assert args.gazebo_flavor == "gazebo"
    assert args.headless is False
    assert args.realtime is False
    assert args.px4_control_port == 14542


def test_stack_plan_parser_requires_scenario() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as error:
        parser.parse_args(["stack-plan"])
    assert error.value.code == 2


def test_stack_plan_cli_prints_dry_run_json(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(
        [
            "stack-plan",
            "--scenario",
            str(SCENARIO),
            "--repository-root",
            str(ROOT),
        ]
    )

    assert exit_code == 0
    document = json.loads(capsys.readouterr().out)
    assert document["event"] == "simulator_stack_plan"
    assert document["scenario"]["id"] == "river_rescue_vtol_001"
    assert document["vehicle"]["class"] == "vtol"
    assert document["runtime"]["dry_run"] is True
    assert document["repository"]["truth_loaded"] is False
    assert document["ports"]["loopback_only"] is True
    assert document["validation"]["gate_passed"] is True
    assert len(document["sha256"]) == 64


def test_stack_plan_cli_writes_same_hash_bound_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_path = tmp_path / "stack-plan.json"
    exit_code = main(
        [
            "stack-plan",
            "--scenario",
            str(SCENARIO),
            "--repo-root",
            str(ROOT),
            "--out",
            str(output_path),
            "--gui",
            "--no-realtime",
        ]
    )

    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert written == printed
    assert written["runtime"]["headless"] is False
    assert written["runtime"]["realtime"] is False
    assert written["sha256"]


def test_stack_plan_cli_reports_invalid_repository(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(
        [
            "stack-plan",
            "--scenario",
            str(SCENARIO),
            "--repository-root",
            str(SCENARIO.parent),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err.startswith("simulator stack-plan:")
