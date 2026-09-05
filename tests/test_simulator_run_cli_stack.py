from __future__ import annotations

import json
from pathlib import Path

import pytest

import multidetect.simulator.__main__ as simulator_cli
from multidetect.simulator.__main__ import build_parser

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "configs" / "scenarios" / "river_rescue_vtol_001.scenario.json"


def test_run_parser_exposes_stack_and_external_launch_options() -> None:
    parser = build_parser()
    defaults = parser.parse_args(["run", "--scenario", str(SCENARIO)])
    assert defaults.stack_plan_enabled is True
    assert defaults.stack_repository_root is None
    assert defaults.stack_px4_executable is None
    assert defaults.stack_gazebo_executable is None
    assert defaults.stack_gazebo_flavor is None
    assert defaults.stack_px4_workdir is None
    assert defaults.stack_host is None
    assert defaults.stack_px4_control_port is None
    assert defaults.stack_px4_telemetry_port is None
    assert defaults.stack_qgc_output_port is None
    assert defaults.stack_qgc_input_port is None
    assert defaults.stack_metadata_port is None
    assert defaults.stack_rtsp_port is None
    assert defaults.readiness_timeout is None
    assert defaults.readiness_require_heartbeat is None
    assert defaults.readiness_strict is None

    args = parser.parse_args(
        [
            "run",
            "--scenario",
            str(SCENARIO),
            "--no-stack-plan",
            "--stack-repository-root",
            str(ROOT),
            "--stack-px4-executable",
            "px4-sitl",
            "--stack-gazebo-executable",
            "gazebo",
            "--stack-gazebo-flavor",
            "gazebo",
            "--stack-px4-workdir",
            str(ROOT),
            "--stack-host",
            "127.0.0.1",
            "--stack-px4-control-port",
            "24540",
            "--stack-px4-telemetry-port",
            "24541",
            "--stack-qgc-output-port",
            "24549",
            "--stack-qgc-input-port",
            "24550",
            "--stack-metadata-port",
            "24560",
            "--stack-rtsp-port",
            "24554",
        ]
    )
    assert args.stack_plan_enabled is False
    assert args.stack_repository_root == ROOT
    assert args.stack_px4_executable == "px4-sitl"
    assert args.stack_gazebo_executable == "gazebo"
    assert args.stack_gazebo_flavor == "gazebo"
    assert args.stack_px4_workdir == ROOT
    assert args.stack_host == "127.0.0.1"
    assert args.stack_px4_control_port == 24540
    assert args.stack_px4_telemetry_port == 24541
    assert args.stack_qgc_output_port == 24549
    assert args.stack_qgc_input_port == 24550
    assert args.stack_metadata_port == 24560
    assert args.stack_rtsp_port == 24554


def test_scenario_parser_exposes_compiled_mission_progress_options() -> None:
    parser = build_parser()
    mission = ROOT / "compiled-mission.json"
    args = parser.parse_args(
        [
            "scenario",
            "--scenario",
            str(SCENARIO),
            "--mission",
            str(mission),
            "--start-mission",
        ]
    )
    assert args.mission == mission
    assert args.start_mission is True


def test_run_parser_exposes_explicit_readiness_switches() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            "--scenario",
            str(SCENARIO),
            "--readiness-timeout",
            "1.5",
            "--readiness-require-heartbeat",
            "--readiness-strict",
        ]
    )
    assert args.readiness_timeout == 1.5
    assert args.readiness_require_heartbeat is True
    assert args.readiness_strict is True


def test_run_cli_forwards_explicit_readiness_switches(monkeypatch, capsys) -> None:
    captured: dict[str, object] = {}

    class _Result:
        status = "planned"

        def to_mapping(self) -> dict[str, object]:
            return {"status": self.status}

    class _FakeOrchestrator:
        def __init__(self, _scenario: Path, **kwargs: object) -> None:
            captured.update(kwargs)

        def start(self) -> _Result:
            return _Result()

    monkeypatch.setattr(simulator_cli, "SimulatorOrchestrator", _FakeOrchestrator)
    code = simulator_cli.main(
        [
            "run",
            "--scenario",
            str(SCENARIO),
            "--dry-run",
            "--readiness-timeout",
            "2.25",
            "--readiness-require-heartbeat",
            "--readiness-strict",
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"status": "planned"}
    assert captured["readiness_timeout_s"] == 2.25
    assert captured["readiness_require_heartbeat"] is True
    assert captured["readiness_strict"] is True


def test_run_cli_forwards_manual_watchdog_override(monkeypatch, capsys) -> None:
    captured: dict[str, object] = {}

    class _Result:
        status = "planned"

        def to_mapping(self) -> dict[str, object]:
            return {"status": self.status}

    class _FakeOrchestrator:
        def __init__(self, _scenario: Path, **kwargs: object) -> None:
            captured.update(kwargs)

        def start(self) -> _Result:
            return _Result()

    monkeypatch.setattr(simulator_cli, "SimulatorOrchestrator", _FakeOrchestrator)
    code = simulator_cli.main(
        [
            "run",
            "--scenario",
            str(SCENARIO),
            "--dry-run",
            "--manual-watchdog-seconds",
            "0.75",
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"status": "planned"}
    assert captured["manual_watchdog_s"] == 0.75


def test_run_cli_forwards_stack_configuration_to_orchestrator(monkeypatch, capsys) -> None:
    captured: dict[str, object] = {}

    class _Result:
        status = "planned"

        def to_mapping(self) -> dict[str, object]:
            return {"status": self.status}

    class _FakeOrchestrator:
        def __init__(self, scenario: Path, **kwargs: object) -> None:
            captured["scenario"] = scenario
            captured.update(kwargs)

        def start(self) -> _Result:
            return _Result()

    monkeypatch.setattr(simulator_cli, "SimulatorOrchestrator", _FakeOrchestrator)
    code = simulator_cli.main(
        [
            "run",
            "--scenario",
            str(SCENARIO),
            "--dry-run",
            "--no-stack-plan",
            "--stack-repository-root",
            str(ROOT),
            "--stack-px4-executable",
            "px4-sitl",
            "--stack-gazebo-executable",
            "gazebo",
            "--stack-gazebo-flavor",
            "gazebo",
            "--stack-px4-workdir",
            str(ROOT),
            "--stack-host",
            "127.0.0.1",
            "--stack-px4-control-port",
            "24540",
            "--stack-px4-telemetry-port",
            "24541",
            "--stack-qgc-output-port",
            "24549",
            "--stack-qgc-input-port",
            "24550",
            "--stack-metadata-port",
            "24560",
            "--stack-rtsp-port",
            "24554",
            "--qgc-control-port",
            "24550",
            "--qgc-output-port",
            "24549",
            "--px4-telemetry-port",
            "24541",
            "--px4-control-endpoint",
            "udpout:127.0.0.1:24540",
        ]
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"status": "planned"}
    assert captured["scenario"] == SCENARIO
    assert captured["stack_plan_enabled"] is False
    assert captured["stack_repository_root"] == ROOT
    assert captured["stack_px4_executable"] == "px4-sitl"
    assert captured["stack_gazebo_executable"] == "gazebo"
    assert captured["stack_gazebo_flavor"] == "gazebo"
    assert captured["stack_px4_workdir"] == ROOT
    ports = captured["stack_ports"]
    assert ports is not None
    assert ports.host == "127.0.0.1"
    assert ports.px4_control_port == 24540
    assert ports.px4_telemetry_port == 24541
    assert ports.qgc_output_port == 24549
    assert ports.qgc_input_port == 24550
    assert ports.metadata_port == 24560
    assert ports.rtsp_port == 24554


def test_run_cli_stack_alias_must_match_existing_endpoint(capsys) -> None:
    code = simulator_cli.main(
        [
            "run",
            "--scenario",
            str(SCENARIO),
            "--dry-run",
            "--stack-px4-control-port",
            "24540",
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "stack-px4-control-port" in captured.err
    assert "conflicts" in captured.err


def test_run_cli_rejects_live_ai_without_interactive_router(capsys) -> None:
    """Live PX4 AI telemetry must not silently degrade to a headless run."""

    code = simulator_cli.main(
        [
            "run",
            "--scenario",
            str(SCENARIO),
            "--dry-run",
            "--ai-live",
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "live_ai requires the interactive QGC/PX4 telemetry router" in captured.err


def test_run_parser_accepts_bracketed_ipv6_stack_endpoint() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            "--scenario",
            str(SCENARIO),
            "--dry-run",
            "--stack-metadata-port",
            "24560",
            "--px4-control-endpoint",
            "udpout:[::1]:24540",
        ]
    )
    host, port = simulator_cli._parse_run_loopback_udpout(args.px4_control_endpoint)
    assert (host, port) == ("::1", 24540)


def test_run_parser_rejects_conflicting_stack_mode_flags() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as error:
        parser.parse_args(
            [
                "run",
                "--scenario",
                str(SCENARIO),
                "--stack-plan",
                "--no-stack-plan",
            ]
        )
    assert error.value.code == 2


def test_px4_launch_plan_parser_exposes_preflight_options() -> None:
    parser = build_parser()
    defaults = parser.parse_args(["px4-launch-plan", "--scenario", str(SCENARIO)])
    assert defaults.command == "px4-launch-plan"
    assert defaults.dry_run is True
    assert defaults.write_evidence is True
    assert defaults.px4_executable == "px4"
    assert defaults.px4_control_port == 14540
    assert defaults.px4_telemetry_port == 14541

    args = parser.parse_args(
        [
            "px4-launch-plan",
            "--scenario",
            str(SCENARIO),
            "--repo-root",
            str(ROOT),
            "--job-dir",
            "artifacts/evaluation/px4-cli",
            "--px4-workdir",
            str(ROOT),
            "--px4",
            "px4-sitl",
            "--host",
            "::1",
            "--control-port",
            "24540",
            "--telemetry-port",
            "24541",
            "--strict",
            "--no-write-evidence",
            "--out",
            "artifacts/evaluation/px4-cli/evidence.json",
        ]
    )
    assert args.repository_root == ROOT
    assert args.px4_executable == "px4-sitl"
    assert args.px4_workdir == ROOT
    assert args.host == "::1"
    assert args.px4_control_port == 24540
    assert args.px4_telemetry_port == 24541
    assert args.dry_run is False
    assert args.write_evidence is False


def test_px4_launch_plan_cli_writes_hash_bound_dry_run_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job = tmp_path / "px4-job"
    out = job / "evidence.json"
    code = simulator_cli.main(
        [
            "px4-launch-plan",
            "--scenario",
            str(SCENARIO),
            "--repository-root",
            str(ROOT),
            "--job-dir",
            str(job),
            "--out",
            str(out),
        ]
    )
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    written = json.loads(out.read_text(encoding="utf-8"))
    assert printed == written
    assert printed["event"] == "simulator_px4_launch_plan"
    assert printed["sha256"]
    assert printed["ports"]["control_port"] == 14540
    assert printed["ports"]["telemetry_port"] == 14541
    assert printed["runtime"]["external_process_started"] is False
    assert printed["runtime"]["external_io_opened"] is False
    assert printed["artifacts"]["source_checkout_modified"] is False
    assert Path(printed["artifacts"]["wrapper"]).is_file()
    assert Path(printed["artifacts"]["generated_rc_mavlink"]).is_file()


def test_px4_launch_plan_cli_strict_mode_reports_missing_px4(capsys) -> None:
    code = simulator_cli.main(
        [
            "px4-launch-plan",
            "--scenario",
            str(SCENARIO),
            "--repository-root",
            str(ROOT),
            "--strict",
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "px4-launch-plan" in captured.err


def test_run_cli_forwards_px4_launch_switches(monkeypatch, capsys) -> None:
    captured: dict[str, object] = {}

    class _Result:
        status = "planned"

        def to_mapping(self) -> dict[str, object]:
            return {"status": self.status}

    class _FakeOrchestrator:
        def __init__(self, _scenario: Path, **kwargs: object) -> None:
            captured.update(kwargs)

        def start(self) -> _Result:
            return _Result()

    monkeypatch.setattr(simulator_cli, "SimulatorOrchestrator", _FakeOrchestrator)
    code = simulator_cli.main(
        [
            "run",
            "--scenario",
            str(SCENARIO),
            "--dry-run",
            "--px4-launch",
            "--px4-launch-strict",
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"status": "planned"}
    assert captured["px4_launch_enabled"] is True
    assert captured["px4_launch_strict"] is True


def test_run_cli_forwards_no_px4_launch(monkeypatch, capsys) -> None:
    captured: dict[str, object] = {}

    class _Result:
        status = "planned"

        def to_mapping(self) -> dict[str, object]:
            return {"status": self.status}

    class _FakeOrchestrator:
        def __init__(self, _scenario: Path, **kwargs: object) -> None:
            captured.update(kwargs)

        def start(self) -> _Result:
            return _Result()

    monkeypatch.setattr(simulator_cli, "SimulatorOrchestrator", _FakeOrchestrator)
    code = simulator_cli.main(
        [
            "run",
            "--scenario",
            str(SCENARIO),
            "--dry-run",
            "--no-px4-launch",
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"status": "planned"}
    assert captured["px4_launch_enabled"] is False
    assert captured["px4_launch_strict"] is None


def test_run_cli_px4_launch_dry_run_persists_wrapper_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = simulator_cli.main(
        [
            "run",
            "--scenario",
            str(SCENARIO),
            "--headless",
            "--dry-run",
            "--px4-launch",
            "--job-root",
            str(tmp_path),
        ]
    )
    assert code == 0
    document = json.loads(capsys.readouterr().out)
    assert document["status"] == "planned"
    assert document["job_dir"]
    assert document["px4_launch"]["event"] == "simulator_px4_launch_plan"
    assert document["px4_launch"]["runtime"]["external_process_started"] is False
    launch_path = Path(document["job_dir"]) / "px4-launch.json"
    assert launch_path.is_file()
    launch_document = json.loads(launch_path.read_text(encoding="utf-8"))
    assert launch_document["sha256"] == document["px4_launch"]["sha256"]
