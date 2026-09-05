from __future__ import annotations

import json
from pathlib import Path

from multidetect.simulator.__main__ import build_parser, main

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "configs" / "scenarios" / "river_rescue_vtol_001.scenario.json"


def test_parser_exposes_external_plan_command() -> None:
    args = build_parser().parse_args(["external-plan", "--scenario", str(SCENARIO)])
    assert args.command == "external-plan"
    assert args.dry_run is True
    assert args.px4_launch_enabled is False
    assert args.px4_launch_strict is None


def test_external_plan_parser_exposes_px4_launch_switches() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "external-plan",
            "--scenario",
            str(SCENARIO),
            "--px4-launch",
            "--px4-launch-strict",
        ]
    )
    assert args.px4_launch_enabled is True
    assert args.px4_launch_strict is True


def test_external_plan_cli_writes_hash_bound_json_without_starting_processes(
    tmp_path: Path, capsys
) -> None:
    out = tmp_path / "evidence.json"
    job = tmp_path / "job"
    code = main(
        [
            "external-plan",
            "--scenario",
            str(SCENARIO),
            "--repository-root",
            str(ROOT),
            "--job-dir",
            str(job),
            "--out",
            str(out),
            "--env",
            "MD_SIM_OPERATOR_LABEL=cli-fixture",
        ]
    )
    assert code == 0
    stdout = capsys.readouterr().out
    printed = json.loads(stdout)
    written = json.loads(out.read_text(encoding="utf-8"))
    assert printed == written
    assert printed["event"] == "simulator_external_stack_plan"
    assert printed["sha256"]
    assert printed["process_order"] == ["px4", "gazebo"]
    assert printed["processes"][0]["env"]["MD_SIM_OPERATOR_LABEL"] == "cli-fixture"
    assert printed["runtime"]["external_processes_started"] is False
    assert printed["runtime"]["external_io_opened"] is False
    assert (job / "logs").is_dir()


def test_external_plan_cli_rejects_bound_environment_override(
    tmp_path: Path, capsys
) -> None:
    code = main(
        [
            "external-plan",
            "--scenario",
            str(SCENARIO),
            "--repository-root",
            str(ROOT),
            "--job-dir",
            str(tmp_path / "job"),
            "--env",
            "PX4_MAVLINK_UDP_PORT=19999",
        ]
    )
    assert code == 2
    assert "bound key" in capsys.readouterr().err


def test_external_plan_cli_strict_mode_returns_error_when_tools_missing(
    tmp_path: Path, capsys
) -> None:
    code = main(
        [
            "external-plan",
            "--scenario",
            str(SCENARIO),
            "--repository-root",
            str(ROOT),
            "--job-dir",
            str(tmp_path / "strict-job"),
            "--strict",
        ]
    )
    assert code == 2
    assert "external-plan" in capsys.readouterr().err


def test_external_plan_cli_supports_fixed_wing_and_gui_flags(tmp_path: Path, capsys) -> None:
    scenario = ROOT / "configs" / "scenarios" / "coastal_patrol_fixed_wing_001.scenario.json"
    code = main(
        [
            "external-plan",
            "--scenario",
            str(scenario),
            "--repository-root",
            str(ROOT),
            "--job-dir",
            str(tmp_path / "fixed-wing"),
            "--gui",
            "--no-realtime",
        ]
    )
    assert code == 0
    document = json.loads(capsys.readouterr().out)
    assert document["source_stack_plan"]["vehicle"]["class"] == "fixed-wing"
    assert document["source_stack_plan"]["runtime"]["headless"] is False
    assert document["source_stack_plan"]["runtime"]["realtime"] is False


def test_external_plan_cli_includes_optional_px4_launch_wrapper(
    tmp_path: Path, capsys
) -> None:
    job = tmp_path / "px4-wrapper-job"
    code = main(
        [
            "external-plan",
            "--scenario",
            str(SCENARIO),
            "--repository-root",
            str(ROOT),
            "--job-dir",
            str(job),
            "--px4-launch",
        ]
    )
    assert code == 0
    document = json.loads(capsys.readouterr().out)
    assert document["px4_launch"] is not None
    assert document["px4_launch"]["event"] == "simulator_px4_launch_plan"
    assert document["px4_launch_sha256"]
    assert document["processes"][0]["command"] == [str(job / "px4-sim-launch.sh")]
    assert document["validation"]["px4_launch_ready"] is False
    assert document["runtime"]["external_processes_started"] is False


def test_external_plan_cli_px4_launch_strict_reports_unready_wrapper(
    tmp_path: Path, capsys
) -> None:
    code = main(
        [
            "external-plan",
            "--scenario",
            str(SCENARIO),
            "--repository-root",
            str(ROOT),
            "--job-dir",
            str(tmp_path / "strict-px4"),
            "--px4-launch",
            "--px4-launch-strict",
        ]
    )
    assert code == 2
    assert "external-plan" in capsys.readouterr().err
