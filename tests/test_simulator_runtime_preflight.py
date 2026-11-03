from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from multidetect.simulator import (
    build_runtime_preflight_document,
    write_runtime_preflight_document,
)
from multidetect.simulator.isolation import ToolCapability


def _capabilities(*, native: bool, docker_daemon: bool) -> dict[str, ToolCapability]:
    return {
        "px4": ToolCapability(
            "px4",
            "px4" if native else None,
            native,
            version="PX4 v1.15" if native else None,
            reason=None if native else "executable not found on PATH",
        ),
        "gazebo": ToolCapability(
            "gazebo",
            "gz" if native else None,
            native,
            version="Gazebo Sim 8" if native else None,
            reason=None if native else "executable not found on PATH",
        ),
        "docker": ToolCapability(
            "docker",
            "docker.exe",
            True,
            version="Docker version 28.4.0",
            daemon_available=docker_daemon,
            daemon_version="28.4.0" if docker_daemon else None,
            daemon_reason=None if docker_daemon else "Cannot connect to daemon",
        ),
    }


def test_runtime_preflight_is_explicit_when_all_external_runtimes_are_unavailable() -> None:
    document = build_runtime_preflight_document(
        capabilities=_capabilities(native=False, docker_daemon=False),
        wsl={"available": True, "distros": ["docker-desktop"], "running_distros": 1},
        generated_at=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc),
    )

    assert document["event"] == "simulator_runtime_preflight"
    assert document["readiness"] == {
        "native_px4_gazebo_ready": False,
        "docker_daemon_ready": False,
        "ready_to_attempt": False,
        "execution_mode": "unavailable",
        "reason": (
            "PX4: executable not found on PATH; Gazebo: executable not found on PATH; "
            "Docker daemon: Cannot connect to daemon"
        ),
    }
    assert document["claims"]["process_started"] is False
    assert document["claims"]["telemetry_observed"] is False
    assert document["claims"]["visual_acceptance"] == "not_claimed"
    assert document["truth_free"] is True
    assert len(document["sha256"]) == 64


def test_runtime_preflight_prefers_native_when_both_native_and_docker_are_ready() -> None:
    document = build_runtime_preflight_document(
        capabilities=_capabilities(native=True, docker_daemon=True),
        wsl={"available": False, "distros": [], "running_distros": 0},
        generated_at=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc),
    )

    readiness = document["readiness"]
    assert readiness["native_px4_gazebo_ready"] is True
    assert readiness["docker_daemon_ready"] is True
    assert readiness["ready_to_attempt"] is True
    assert readiness["execution_mode"] == "native"


def test_runtime_preflight_uses_docker_when_native_tools_are_missing() -> None:
    document = build_runtime_preflight_document(
        capabilities=_capabilities(native=False, docker_daemon=True),
        wsl={"available": True, "distros": ["docker-desktop"], "running_distros": 1},
        generated_at=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc),
    )

    readiness = document["readiness"]
    assert readiness["ready_to_attempt"] is True
    assert readiness["execution_mode"] == "docker"
    assert readiness["reason"] == "Docker client and daemon are responsive"


def test_runtime_preflight_exposes_external_gate_and_explicit_fallback_boundary() -> None:
    document = build_runtime_preflight_document(
        capabilities=_capabilities(native=False, docker_daemon=False),
        wsl={"available": False, "distros": [], "running_distros": 0},
        generated_at=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc),
    )

    contract = document["external_runtime"]
    assert contract["schema_version"] == 1
    assert contract["required_for_real_3d"] is True
    assert contract["candidate"] == "unavailable"
    assert contract["ready_to_attempt"] is False
    assert contract["gate_passed"] is False
    assert contract["external_runtime_gate_passed"] is False
    assert contract["process_started"] is False
    assert contract["telemetry_observed"] is False
    assert contract["fallback"] == {
        "policy": "deterministic_flight_model",
        "automatic": False,
        "used": False,
        "reason": (
            "runtime-preflight never starts or silently switches runtimes; "
            "an orchestrator must record an explicit deterministic fallback"
        ),
    }
    assert document["asset_preflight"]["status"] == "not_requested"
    assert document["px4_workdir_preflight"]["status"] == "not_requested"


def test_runtime_preflight_binds_repository_owned_scenario_assets(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    scenario_root = repo / "configs" / "scenarios"
    source_root = Path(__file__).resolve().parents[1]
    scenario_root.mkdir(parents=True)
    # Keep the test independent of the host checkout while reusing the
    # repository-owned scenario and asset graph as immutable input fixtures.
    import shutil

    shutil.copytree(source_root / "simulation", repo / "simulation")
    shutil.copy2(
        source_root / "configs" / "scenarios" / "river_rescue_vtol_001.scenario.json",
        scenario_root / "river_rescue_vtol_001.scenario.json",
    )
    for sibling in (
        "river_rescue_vtol_001.runtime.json",
        "river_rescue_vtol_001.truth.json",
        "river_rescue_vtol_001.expectations.json",
    ):
        shutil.copy2(source_root / "configs" / "scenarios" / sibling, scenario_root / sibling)

    document = build_runtime_preflight_document(
        capabilities=_capabilities(native=False, docker_daemon=True),
        wsl={"available": False, "distros": [], "running_distros": 0},
        repository_root=repo,
        scenario=scenario_root / "river_rescue_vtol_001.scenario.json",
        generated_at=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc),
    )

    assets = document["asset_preflight"]
    assert assets["status"] == "ready"
    assert assets["asset_gate_passed"] is True
    assert assets["scenario"]["scenario_id"] == "river_rescue_vtol_001"
    assert assets["scenario"]["manifest_sha256"]
    assert assets["stack_plan_sha256"]
    assert assets["assets"]["asset_sha256"]["world"]
    contract = document["external_runtime"]
    assert contract["candidate"] == "docker"
    assert contract["asset_preflight_requested"] is True
    assert contract["asset_preflight_passed"] is True
    assert contract["ready_to_attempt"] is True
    assert contract["gate_passed"] is False


def test_runtime_preflight_blocks_candidate_when_requested_scenario_is_invalid(
    tmp_path: Path,
) -> None:
    document = build_runtime_preflight_document(
        capabilities=_capabilities(native=False, docker_daemon=True),
        wsl={"available": False, "distros": [], "running_distros": 0},
        repository_root=tmp_path,
        scenario=tmp_path / "missing.scenario.json",
        generated_at=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc),
    )

    assets = document["asset_preflight"]
    assert assets["status"] == "failed"
    assert assets["asset_gate_passed"] is False
    contract = document["external_runtime"]
    assert contract["candidate"] == "docker"
    assert contract["ready_to_attempt"] is False
    assert "asset preflight failed" in contract["reason"]


def test_runtime_preflight_writer_persists_hash_bound_document(tmp_path: Path) -> None:
    target = write_runtime_preflight_document(
        tmp_path / "runtime-preflight.json",
        capabilities=_capabilities(native=False, docker_daemon=False),
        wsl={"available": False, "distros": [], "running_distros": 0},
        generated_at=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc),
    )

    payload = json.loads(target.read_text(encoding="utf-8"))
    digest = payload.pop("sha256")
    from multidetect.autonomy.serialization import canonical_sha256

    assert target.is_file()
    assert digest == canonical_sha256(payload)


def test_runtime_preflight_accepts_explicit_docker_diagnostics_without_host_io() -> None:
    diagnostics = {
        "schema_version": 1,
        "status": "log_read",
        "log_path": "C:/diagnostics/com.docker.backend.exe.log",
        "matched_error_lines": ["backend crashed: stale dockerInference entry"],
        "likely_cause": "stale dockerInference reparse/socket entry blocks backend startup",
        "recommended_action": "Exit Docker Desktop completely and remove only the stale entry",
    }
    document = build_runtime_preflight_document(
        capabilities=_capabilities(native=False, docker_daemon=False),
        wsl={"available": False, "distros": [], "running_distros": 0},
        docker_diagnostics=diagnostics,
        generated_at=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc),
    )

    assert document["docker_diagnostics"] == diagnostics
    payload = dict(document)
    digest = payload.pop("sha256")
    from multidetect.autonomy.serialization import canonical_sha256

    assert digest == canonical_sha256(payload)


def test_docker_desktop_diagnostics_reports_stale_socket_from_bounded_log(
    tmp_path: Path, monkeypatch
) -> None:
    from multidetect.simulator import runtime_preflight

    log_dir = tmp_path / "Docker" / "log" / "host"
    log_dir.mkdir(parents=True)
    log_path = log_dir / "com.docker.backend.exe.log"
    log_path.write_text(
        "info startup\n"
        "backend crashed: initializing Inference manager: "
        "remove dockerInference: The file cannot be accessed by the system.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    # Exercise Windows log parsing while keeping pathlib native to the host.
    from types import SimpleNamespace

    monkeypatch.setattr(
        runtime_preflight, "os", SimpleNamespace(name="nt", environ=runtime_preflight.os.environ)
    )

    diagnostics = runtime_preflight._probe_docker_desktop_diagnostics()

    assert diagnostics["status"] == "log_read"
    assert diagnostics["log_path"] == str(log_path)
    assert diagnostics["matched_error_lines"]
    assert diagnostics["likely_cause"] == (
        "stale dockerInference reparse/socket entry blocks backend startup"
    )
    assert "remove only the stale dockerInference entry" in diagnostics["recommended_action"]


def test_runtime_preflight_cli_writes_document_without_starting_runtime(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import multidetect.simulator.__main__ as simulator_cli

    expected = build_runtime_preflight_document(
        capabilities=_capabilities(native=False, docker_daemon=False),
        wsl={"available": False, "distros": [], "running_distros": 0},
        generated_at=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc),
    )
    monkeypatch.setattr(simulator_cli, "build_runtime_preflight_document", lambda: expected)

    target = tmp_path / "cli-runtime-preflight.json"
    assert simulator_cli.main(["runtime-preflight", "--out", str(target)]) == 0
    captured = json.loads(capsys.readouterr().out)
    persisted = json.loads(target.read_text(encoding="utf-8"))
    assert captured == expected
    assert persisted == expected


def test_runtime_preflight_cli_escapes_unicode_for_legacy_windows_stdout(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import multidetect.simulator.__main__ as simulator_cli

    expected = build_runtime_preflight_document(
        capabilities=_capabilities(native=False, docker_daemon=False),
        wsl={"available": False, "distros": [], "running_distros": 0},
        docker_diagnostics={
            "schema_version": 1,
            "status": "log_read",
            "log_path": "C:/diagnostics/backend.log",
            "matched_error_lines": ["duration µs"],
            "likely_cause": "unicode diagnostic",
            "recommended_action": "rerun",
        },
        generated_at=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc),
    )
    monkeypatch.setattr(simulator_cli, "build_runtime_preflight_document", lambda: expected)

    target = tmp_path / "unicode-runtime-preflight.json"
    assert simulator_cli.main(["runtime-preflight", "--out", str(target)]) == 0

    # The output is valid JSON even when the host console cannot encode ``µ``;
    # JSON parsing restores the original character in the value.
    captured = json.loads(capsys.readouterr().out)
    assert captured == expected
