from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.run_onboard_3d_training_preflight import (
    DEFAULT_MANIFEST,
    DEFAULT_REFERENCE,
    build_preflight,
)

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.usefixtures("require_current_onboard_model")


def _missing_tools(_name: str) -> str | None:
    return None


def _docker_ok(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["docker", "info"], 0, "27.0.0\n", "")


def test_preflight_uses_hash_bound_reference_and_blocks_without_runtime() -> None:
    report = build_preflight(
        reference_path=DEFAULT_REFERENCE,
        manifest_path=DEFAULT_MANIFEST,
        which=_missing_tools,
        run=_docker_ok,
    )

    assert report["contract"] == "multidetect.onboard-3d-training-preflight"
    assert report["status"] == "blocked"
    assert report["training_ready"] is False
    assert report["claim_boundary"]["external_gazebo_runtime"] == "not_executed"
    assert report["onboard_model_reference"]["model_id"] == (
        "onboard-policy-r33-balanced-bearing-3d-dev"
    )
    assert report["gazebo_3d_manifest"]["scenario_count"] == 3
    assert len(report["capture_matrix"]) == 3
    assert all(item["not_executed_by_preflight"] is True for item in report["capture_matrix"])
    assert [item["status"] for item in report["capture_matrix"]] == [
        "ready_to_execute",
        "ready_to_execute",
        "ready_to_execute_sensor_only",
    ]
    vtol = report["capture_matrix"][1]
    assert vtol["runner"] == "scripts/run_vtol_mission_sensor_capture.py"
    assert vtol["capture_mode"] == "autonomous_mission_sensor_capture"
    assert "--frame-output-directory" in vtol["command"]
    assert "--depth-output-directory" in vtol["command"]
    assert vtol["command"][vtol["command"].index("--sim-energy-profile") + 1] == (
        "vtol-3d-capture-v1"
    )
    assert "--execute" in vtol["command"]
    fixed_wing = report["capture_matrix"][2]
    assert fixed_wing["runner"] == "scripts/run_simulator_docker_acceptance.py"
    assert fixed_wing["capture_mode"] == "passive_sensor_capture"
    assert fixed_wing["autonomous_flight_opt_in"] is False
    assert "--sensor-depth-output-dir" in fixed_wing["command"]
    assert "--execute" in fixed_wing["command"]
    for item in report["capture_matrix"]:
        command_text = " ".join(item["command"])
        assert "red_car" not in command_text
        assert "red_green" not in command_text
        assert "truth.json" not in command_text
    assert any("Docker daemon" in reason for reason in report["reasons"])
    inventory = report["training_input_inventory"]
    rendered = next(item for item in inventory if item["kind"] == "rendered_3d")
    assert rendered["target_visibility_gate"]["contract"] == (
        "multidetect.onboard-3d-target-visibility-gate-v1"
    )
    assert rendered["target_visibility_gate"]["passed"] is False
    assert rendered["target_visibility_gate"]["target_visibility_rows"] == 0
    assert any(item["kind"] == "rendered_3d" for item in inventory)
    assert any(item["kind"] == "trajectory" for item in inventory)
    assert all(item["eligible_for_real_3d_training"] is False for item in inventory)
    assert report["capture_inventory"]["contract"] == (
        "multidetect.onboard-3d-capture-inventory-v1"
    )
    # Current workspaces may contain real mainline sensor captures.  Their
    # presence must not make the strict training gate pass; eligibility here
    # only means the capture is bound to the canonical 3-D scenario hash.
    assert report["capture_inventory"]["capture_eligible_count"] >= 1
    assert any("training_ready remains false" in reason for reason in report["reasons"])
    assert any("target-visibility gate" in reason for reason in report["reasons"])


def test_preflight_reports_ready_to_execute_only_when_docker_server_is_reachable() -> None:
    def _which(name: str) -> str | None:
        return f"C:/tools/{name}.exe"

    report = build_preflight(
        reference_path=DEFAULT_REFERENCE,
        manifest_path=DEFAULT_MANIFEST,
        which=_which,
        run=_docker_ok,
    )

    assert report["status"] == "ready_to_execute"
    assert report["tools"]["docker"]["server_available"] is True
    assert report["tools"]["gazebo"]["local_cli_available"] is True
    assert report["tools"]["px4"]["local_cli_available"] is True
    assert report["training_ready"] is False
    assert "--model-reference" in report["capture_matrix"][0]["command"]
    assert "--use-actual-detector" in report["capture_matrix"][0]["command"]
    assert "--capture-plan" in report["capture_matrix"][0]["command"]
    assert report["capture_plan"]["target_pose_used_for_offline_route_only"] is False
    assert report["capture_plan"]["target_pose_used_for_capture_route"] is False
    assert all(
        "--execute-mission-transactions" in item["command"]
        for item in report["capture_matrix"][:1]
    )
    assert all(
        "--execute-autonomous-flight" in item["command"]
        for item in report["capture_matrix"][:1]
    )
    assert "--sensor-depth-output-dir" in report["capture_matrix"][0]["command"]
    assert "--execute" in report["capture_matrix"][0]["command"]
    assert report["capture_matrix"][1]["status"] == "ready_to_execute"
    assert report["capture_matrix"][2]["status"] == "ready_to_execute_sensor_only"
    assert report["detector_reference"]["production_approved"] is False
    assert all(
        item["kind"] in {"rendered_3d", "trajectory"}
        for item in report["training_input_inventory"]
    )
    assert report["capture_inventory"]["mainline_manifest"]["scenario_ids"]


def test_preflight_report_is_json_serializable() -> None:
    report = build_preflight(
        reference_path=DEFAULT_REFERENCE,
        manifest_path=DEFAULT_MANIFEST,
        which=_missing_tools,
        run=_docker_ok,
    )
    encoded = json.dumps(report, ensure_ascii=False)
    assert "multidetect.onboard-3d-training-preflight" in encoded
    assert "multidetect.onboard-3d-capture-inventory-v1" in encoded


def test_preflight_classifies_docker_desktop_stale_socket(tmp_path: Path) -> None:
    app_data = tmp_path / "LocalAppData"
    docker_dir = app_data / "Docker"
    docker_dir.mkdir(parents=True)
    (docker_dir / "backend.error.json").write_text(
        json.dumps(
            {
                "error": "initializing Ingest server: "
                "rename sailor-ingest.sock sailor-ingest.sock.stale: "
                "The file cannot be accessed by the system"
            }
        ),
        encoding="utf-8",
    )

    report = build_preflight(
        reference_path=DEFAULT_REFERENCE,
        manifest_path=DEFAULT_MANIFEST,
        which=_missing_tools,
        run=_docker_ok,
        local_app_data=str(app_data),
    )

    diagnostic = report["tools"]["docker"]["backend_diagnostic"]
    assert diagnostic["failure_kind"] == "docker_desktop_stale_socket"
    assert diagnostic["stale_socket_name"] == "sailor-ingest.sock"
    assert diagnostic["stale_socket_path"].replace("\\", "/").endswith(
        "Docker/run/sailor-ingest.sock"
    )
    assert any("stale sailor-ingest.sock" in reason for reason in report["reasons"])


def test_preflight_extracts_non_ingest_docker_desktop_socket(tmp_path: Path) -> None:
    app_data = tmp_path / "LocalAppData"
    docker_dir = app_data / "Docker"
    docker_dir.mkdir(parents=True)
    socket_path = app_data / "docker-secrets-engine" / "engine.sock"
    (docker_dir / "backend.error.json").write_text(
        json.dumps(
            {
                "error": (
                    "initializing Secrets Engine: listening on unix://"
                    f"{socket_path.as_posix()}: rename {socket_path.name} "
                    f"{socket_path.name}.stale: The file cannot be accessed by the system"
                )
            }
        ),
        encoding="utf-8",
    )

    report = build_preflight(
        reference_path=DEFAULT_REFERENCE,
        manifest_path=DEFAULT_MANIFEST,
        which=_missing_tools,
        run=_docker_ok,
        local_app_data=str(app_data),
    )

    diagnostic = report["tools"]["docker"]["backend_diagnostic"]
    assert diagnostic["failure_kind"] == "docker_desktop_stale_socket"
    assert diagnostic["stale_socket_name"] == "engine.sock"
    assert diagnostic["stale_socket_path"].replace("\\", "/").endswith(
        "docker-secrets-engine/engine.sock"
    )
    assert any("stale engine.sock" in reason for reason in report["reasons"])


def test_preflight_keeps_all_reported_stale_socket_paths(tmp_path: Path) -> None:
    app_data = tmp_path / "LocalAppData"
    docker_dir = app_data / "Docker"
    docker_dir.mkdir(parents=True)
    (docker_dir / "backend.error.json").write_text(
        json.dumps(
            {
                "error": (
                    "listening on unix://C:/Users/test/AppData/Local/Docker/run/"
                    "sailor-ingest.sock: rename sailor-ingest.sock "
                    "sailor-ingest.sock.stale: The file cannot be accessed by the system; "
                    "then listening on unix://C:/Users/test/AppData/Local/docker-secrets-engine/"
                    "engine.sock: rename engine.sock engine.sock.stale: "
                    "The file cannot be accessed by the system"
                )
            }
        ),
        encoding="utf-8",
    )

    report = build_preflight(
        reference_path=DEFAULT_REFERENCE,
        manifest_path=DEFAULT_MANIFEST,
        which=_missing_tools,
        run=_docker_ok,
        local_app_data=str(app_data),
    )

    diagnostic = report["tools"]["docker"]["backend_diagnostic"]
    assert [path.replace("\\", "/") for path in diagnostic["stale_socket_paths"]] == [
        "C:/Users/test/AppData/Local/Docker/run/sailor-ingest.sock",
        "C:/Users/test/AppData/Local/docker-secrets-engine/engine.sock",
    ]
    assert "all reported stale reparse points" in diagnostic["remediation"]
