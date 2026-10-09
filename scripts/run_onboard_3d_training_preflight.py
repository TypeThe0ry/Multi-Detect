"""Prepare the real 3-D Gazebo -> OnboardAI training path.

This command is deliberately a preflight, not a simulator runner.  It verifies
the hash-bound current OnboardAI reference and the repository-owned 3-D scene
manifest, probes the local/container prerequisites, and emits a concrete
capture matrix.  It never starts Docker, Gazebo, PX4, QGC, or a flight.

The output separates three states that are often incorrectly conflated:

* ``blocked``: the host cannot currently start the pinned external simulator;
* ``ready_to_execute``: prerequisites are present, but no capture was run;
* ``training_ready``: always false here; this requires sealed RGB + Depth +
  telemetry episodes and an independent holdout, produced by later commands.

No target truth, class colour, billboard, or hand-written image is used by this
preflight.  The only approved source is the entity-based 3-D scene manifest.
Vehicle-specific runners remain explicit: multirotor keeps the full
autonomous path, VTOL uses the existing mission sensor-capture owner, and
fixed-wing is currently limited to passive RGB-D/telemetry capture.  A passive
capture is never reported as autonomous flight.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REFERENCE = ROOT / "configs" / "training" / "onboard-policy-current-reference-v1.json"
DEFAULT_MANIFEST = ROOT / "configs" / "scenarios" / "gazebo-3d-mainline-v1.manifest.json"
DEFAULT_DETECTOR_MODEL = (
    ROOT / "artifacts" / "models" / "coco-yolo26n-traditional" / "yolo26n-traditional.onnx"
)
DEFAULT_OUTPUT = ROOT / "artifacts" / "evaluation" / "onboard-3d-training-preflight.json"
DEFAULT_EVALUATION_ROOT = ROOT / "artifacts" / "evaluation"
DEFAULT_RENDERED_DATASETS = (
    ROOT / "artifacts" / "training" / "onboard-rendered-3d-depth-v3.json",
    ROOT / "artifacts" / "training" / "onboard-rendered-3d-mainline-v1.json",
)
DEFAULT_TRAJECTORY_DATASETS = (
    ROOT / "artifacts" / "training" / "onboard-trajectory-3d-staging-20261005.json",
)
RENDERED_DATASET_CONTRACTS = frozenset(
    {
        "multidetect.onboard-rendered-3d-dataset-v1",
        "multidetect.onboard-rendered-3d-dataset-merge-v1",
    }
)
CONTRACT = "multidetect.onboard-3d-training-preflight"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.onboard_model_reference import (  # noqa: E402
    OnboardModelReference,
    load_onboard_model_reference,
)
from scripts.audit_onboard_3d_capture_inventory import (  # noqa: E402
    audit_inventory,
)
from scripts.gazebo_capture_plan import load_capture_plan  # noqa: E402
from scripts.verify_gazebo_3d_mainline_manifest import verify_manifest  # noqa: E402


class PreflightError(ValueError):
    """Raised when the repository-owned training inputs are invalid."""


Which = Callable[[str], str | None]
Run = Callable[..., subprocess.CompletedProcess[str]]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repo_relative(path: Path) -> str:
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def _docker_backend_diagnostic(
    *,
    local_app_data: str | None = None,
) -> dict[str, Any]:
    """Classify a local Docker Desktop backend failure without repairing it.

    Docker Desktop can leave a Unix-socket reparse point behind after a backend
    crash.  The generic ``docker info`` error only says that the engine pipe is
    missing, which hides the actionable cause.  This read-only diagnostic
    reports the known stale-socket failure and a narrowly scoped remediation;
    it never removes files or restarts Docker.
    """

    app_data = local_app_data or os.environ.get("LOCALAPPDATA")
    if not isinstance(app_data, str) or not app_data.strip():
        return {"available": False, "reason": "LOCALAPPDATA is not set"}
    error_path = Path(app_data) / "Docker" / "backend.error.json"
    result: dict[str, Any] = {
        "available": False,
        "path": str(error_path),
    }
    if not error_path.is_file():
        result["reason"] = "Docker Desktop backend.error.json is not present"
        return result
    result["available"] = True
    result["sha256"] = _sha256(error_path)
    try:
        raw = error_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        result["reason"] = f"backend error report cannot be read: {exc}"
        return result
    # Docker Desktop has used several AF_UNIX endpoints over time.  Do not
    # hard-code one endpoint: backend.error.json contains the exact socket
    # that blocked startup, and the remediation must name that path.
    socket_match = re.search(
        r"listening on unix://(?P<path>[^\r\n]+?):\s*rename\s+",
        raw,
        flags=re.IGNORECASE,
    )
    rename_match = re.search(
        r"rename\s+(?P<name>[^\s:]+)\s+(?P<stale>[^\s:]+):\s*"
        r"The file cannot be accessed by the system",
        raw,
        flags=re.IGNORECASE,
    )
    if (socket_match or rename_match) and "file cannot be accessed by the system" in raw:
        if socket_match:
            socket_path = socket_match.group("path")
            if socket_path.startswith("/") and re.match(r"/[A-Za-z]:/", socket_path):
                socket_path = socket_path[1:]
            socket_path = socket_path.replace("/", os.sep)
        else:
            socket_path = str(
                Path(app_data) / "Docker" / "run" / rename_match.group("name")
            )
        socket_name = Path(socket_path).name
        related_paths: list[str] = []
        for match in re.finditer(
            r"listening on unix://(?P<path>[^\r\n]+?):\s*rename\s+",
            raw,
            flags=re.IGNORECASE,
        ):
            related_path = match.group("path")
            if related_path.startswith("/") and re.match(r"/[A-Za-z]:/", related_path):
                related_path = related_path[1:]
            related_path = related_path.replace("/", os.sep)
            if related_path not in related_paths:
                related_paths.append(related_path)
        if socket_path not in related_paths:
            related_paths.insert(0, socket_path)
        result.update(
            {
                "failure_kind": "docker_desktop_stale_socket",
                "stale_socket_name": socket_name,
                "stale_socket_path": socket_path,
                "stale_socket_paths": related_paths,
                "remediation": (
                    f"stop Docker Desktop, release all reported stale reparse points "
                    f"({', '.join(related_paths)}), then start Docker Desktop; "
                    "do not reset factory data"
                ),
            }
        )
    else:
        result["failure_kind"] = "docker_desktop_backend_failure_unclassified"
    return result


def _probe_tools(
    *,
    which: Which = shutil.which,
    run: Run = subprocess.run,
    local_app_data: str | None = None,
) -> dict[str, Any]:
    """Probe executable visibility and Docker daemon reachability only."""

    docker = which("docker")
    gz = which("gz")
    gazebo = which("gazebo")
    px4 = which("px4")
    docker_server = False
    docker_error: str | None = None
    if docker:
        try:
            completed = run(
                [docker, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                timeout=8.0,
                check=False,
            )
            docker_server = completed.returncode == 0
            if not docker_server:
                docker_error = (completed.stderr or completed.stdout or "docker info failed")[-600:]
        except (OSError, subprocess.SubprocessError) as exc:
            docker_error = str(exc)[-600:]
    else:
        docker_error = "docker executable not found on PATH"
    return {
        "docker": {
            "path": docker,
            "client_available": docker is not None,
            "server_available": docker_server,
            "error": docker_error,
            "backend_diagnostic": _docker_backend_diagnostic(local_app_data=local_app_data),
        },
        "gazebo": {
            "gz_path": gz,
            "gazebo_path": gazebo,
            "local_cli_available": gz is not None or gazebo is not None,
        },
        "px4": {"path": px4, "local_cli_available": px4 is not None},
        "probe_scope": "visibility_and_docker_info_only",
    }


def _capture_command(
    reference: OnboardModelReference,
    reference_path: Path,
    scenario_path: Path,
    scenario_id: str,
    detector_model_path: Path,
    vehicle_class: str,
    capture_plan_path: Path,
) -> dict[str, Any]:
    """Return an explicit, non-executed capture command for one 3-D scene."""

    root = Path("artifacts/evaluation") / f"onboard-3d-capture-{scenario_id}"
    required_outputs = [
        (root / "rgb" / "gazebo-bgr24-frame-set.manifest.json").as_posix(),
        (root / "depth" / "gazebo-depth-companion-set.manifest.json").as_posix(),
        (root / "acceptance.json").as_posix(),
    ]
    if vehicle_class == "vtol":
        command = [
            "python",
            "scripts/run_vtol_mission_sensor_capture.py",
            "--scenario",
            _repo_relative(scenario_path),
            "--evidence",
            (root / "acceptance.json").as_posix(),
            "--run-directory",
            (root / "run").as_posix(),
            "--frame-output-directory",
            (root / "rgb").as_posix(),
            "--depth-output-directory",
            (root / "depth").as_posix(),
            "--frame-count",
            "100",
            "--sim-energy-profile",
            "vtol-3d-capture-v1",
            "--execute",
        ]
        return {
            "scenario_id": scenario_id,
            "scenario_manifest": _repo_relative(scenario_path),
            "vehicle_class": vehicle_class,
            "runner": "scripts/run_vtol_mission_sensor_capture.py",
            "status": "ready_to_execute",
            "capture_mode": "autonomous_mission_sensor_capture",
            "command": command,
            "purpose": (
                "reuse the existing disposable PX4/Gazebo VTOL lifecycle and capture "
                "actual RGB + FLOAT32 depth + wire-bound telemetry from the neutral "
                "3-D scene; do not use truth.json"
            ),
            "autonomous_flight_opt_in": True,
            "detector_model": _repo_relative(detector_model_path),
            "not_executed_by_preflight": True,
            "required_outputs": required_outputs,
        }
    if vehicle_class == "fixed-wing":
        command = [
            "python",
            "scripts/run_simulator_docker_acceptance.py",
            "--scenario-manifest",
            _repo_relative(scenario_path),
            "--evidence",
            (root / "acceptance.json").as_posix(),
            "--sensor-frames",
            "100",
            "--sensor-frame-output-dir",
            (root / "rgb").as_posix(),
            "--sensor-depth-output-dir",
            (root / "depth").as_posix(),
            "--execute",
        ]
        return {
            "scenario_id": scenario_id,
            "scenario_manifest": _repo_relative(scenario_path),
            "vehicle_class": vehicle_class,
            "runner": "scripts/run_simulator_docker_acceptance.py",
            "status": "ready_to_execute_sensor_only",
            "capture_mode": "passive_sensor_capture",
            "command": command,
            "purpose": (
                "capture actual fixed-wing RGB + FLOAT32 depth + PX4 telemetry for "
                "3-D OnboardAI data preparation; the passive helper sends no ARM, "
                "Mission, actuator, or autonomous-flight command"
            ),
            "autonomous_flight_opt_in": False,
            "detector_model": _repo_relative(detector_model_path),
            "not_executed_by_preflight": True,
            "required_outputs": required_outputs,
        }
    if vehicle_class != "multirotor":
        return {
            "scenario_id": scenario_id,
            "scenario_manifest": _repo_relative(scenario_path),
            "vehicle_class": vehicle_class,
            "runner": None,
            "status": "blocked_unsupported_vehicle_runner",
            "capture_mode": "unavailable",
            "command": [],
            "purpose": (
                "do not capture this scene yet: no vehicle-specific RGB-D capture "
                "owner is registered for this airframe class"
            ),
            "autonomous_flight_opt_in": False,
            "detector_model": _repo_relative(detector_model_path),
            "not_executed_by_preflight": True,
            "required_outputs": [],
        }
    command = [
        "python",
        "scripts/run_external_onboard_model_acceptance.py",
        "--scenario",
        _repo_relative(scenario_path),
        "--model-reference",
        _repo_relative(reference_path),
        "--detector-model",
        _repo_relative(detector_model_path),
        "--capture-plan",
        _repo_relative(capture_plan_path),
        "--use-actual-detector",
        "--execute-mission-transactions",
        "--execute-autonomous-flight",
        "--sensor-frames",
        "100",
        "--sensor-frame-output-dir",
        (root / "rgb").as_posix(),
        "--sensor-depth-output-dir",
        (root / "depth").as_posix(),
        "--evidence",
        (root / "acceptance.json").as_posix(),
        "--execute",
    ]
    return {
        "scenario_id": scenario_id,
        "scenario_manifest": _repo_relative(scenario_path),
        "vehicle_class": vehicle_class,
        "runner": "scripts/run_external_onboard_model_acceptance.py",
        "status": "ready_to_execute",
        "capture_mode": "autonomous_mission_sensor_capture",
        "command": command,
        "purpose": (
            "run the disposable SITL Mission/arm/takeoff gate, then capture actual "
            "Gazebo RGB + FLOAT32 depth + wire-bound telemetry with the existing "
            "YOLO detector; do not use truth.json"
        ),
        "autonomous_flight_opt_in": True,
        "detector_model": _repo_relative(detector_model_path),
        "not_executed_by_preflight": True,
        "required_outputs": required_outputs,
    }


def _training_input_inventory(
    paths: Sequence[Path], *, kind: str
) -> list[dict[str, Any]]:
    """Summarize existing training artifacts without promoting any of them."""

    inventory: list[dict[str, Any]] = []
    for path in paths:
        resolved = path.expanduser().resolve()
        record: dict[str, Any] = {
            "kind": kind,
            "path": _repo_relative(resolved),
            "exists": resolved.is_file(),
            "training_ready": False,
            "eligible_for_real_3d_training": False,
        }
        if not resolved.is_file():
            record["reason"] = "artifact is not present"
            inventory.append(record)
            continue
        try:
            document = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            record["reason"] = f"artifact cannot be parsed: {exc}"
            inventory.append(record)
            continue
        if not isinstance(document, dict):
            record["reason"] = "artifact root is not an object"
            inventory.append(record)
            continue
        record.update(
            {
                "file_sha256": _sha256(resolved),
                "contract": document.get("contract"),
                "schema": document.get("schema"),
                "truth_free": document.get("truth_free") is True,
                "truth_injected": document.get("truth_injected") is True,
                "declared_training_ready": document.get("training_ready") is True,
            }
        )
        if kind == "rendered_3d":
            rows = document.get("rows")
            source_manifests = document.get("source_manifests")
            rows = rows if isinstance(rows, list) else []
            sources = [
                item.get("source")
                for item in rows
                if isinstance(item, dict) and isinstance(item.get("source"), dict)
            ]
            annotation_supervision_rows = sum(
                1
                for item in sources
                if item.get("annotation_frame_bound") is True
            )
            annotation_manifest_count = sum(
                1
                for item in (source_manifests if isinstance(source_manifests, list) else [])
                if isinstance(item, dict)
                and isinstance(item.get("annotation_frame_set"), dict)
            )
            record.update(
                {
                    "merge_dataset": document.get("contract")
                    == "multidetect.onboard-rendered-3d-dataset-merge-v1",
                    "row_count": len(rows),
                    "source_manifest_count": (
                        len(source_manifests) if isinstance(source_manifests, list) else 0
                    ),
                    "scenario_ids": sorted(
                        {
                            str(item.get("scenario_id"))
                            for item in sources
                            if isinstance(item.get("scenario_id"), str)
                        }
                    ),
                    "depth_measurement_rows": int(document.get("depth_measurement_rows", 0) or 0),
                    "calibrated_geometry_rows": int(
                        document.get("calibrated_geometry_rows", 0) or 0
                    ),
                    "detector_hits": sum(
                        1
                        for item in sources
                        if isinstance(item.get("detector_count"), int)
                        and item["detector_count"] > 0
                    ),
                    "annotation_supervision_rows": annotation_supervision_rows,
                    "annotation_manifest_count": annotation_manifest_count,
                }
            )
            target_visibility_rows = sum(
                1
                for item in sources
                if isinstance(item.get("detector_count"), int)
                and item["detector_count"] > 0
                and isinstance(item.get("depth_median"), (int, float))
                and math.isfinite(float(item["depth_median"]))
                and float(item["depth_median"]) > 0.0
                and item.get("geometry_source") == "camera_calibration_depth_pose_ned"
            )
            record["target_visibility_gate"] = {
                "contract": "multidetect.onboard-3d-target-visibility-gate-v1",
                "minimum_rows": 100,
                "target_visibility_rows": target_visibility_rows,
                "requires_detector_hit": True,
                "requires_float32_depth_measurement": True,
                "requires_calibrated_camera_depth_pose_geometry": True,
                "passed": target_visibility_rows >= 100,
            }
            record["annotation_supervision_gate"] = {
                "contract": "multidetect.onboard-3d-annotation-supervision-gate-v1",
                "minimum_rows": 100,
                "annotation_supervision_rows": annotation_supervision_rows,
                "annotation_manifest_count": annotation_manifest_count,
                "requires_exact_rgb_frame_binding": True,
                "requires_persisted_labels_map_payload": True,
                "passed": (
                    document.get("annotation_supervision_only") is True
                    and annotation_manifest_count >= 1
                    and annotation_supervision_rows >= 100
                ),
            }
            reasons = []
            if document.get("contract") not in RENDERED_DATASET_CONTRACTS:
                reasons.append("rendered dataset contract is not an approved capture contract")
            if document.get("training_ready") is not True:
                reasons.append("dataset does not declare training_ready=true")
            if int(document.get("depth_measurement_rows", 0) or 0) < 100:
                reasons.append("fewer than 100 usable depth rows")
            if int(document.get("calibrated_geometry_rows", 0) or 0) < 100:
                reasons.append("fewer than 100 calibrated camera/depth/pose NED rows")
            if record["detector_hits"] == 0:
                reasons.append("no detector hits in persisted source rows")
            if target_visibility_rows == 0:
                reasons.append(
                    "no target-visible RGB/Depth rows with detector hit, depth measurement, "
                    "and calibrated camera/depth/pose geometry"
                )
            elif target_visibility_rows < 100:
                reasons.append(
                    "fewer than 100 target-visible RGB/Depth rows with calibrated geometry"
                )
            if document.get("annotation_supervision_only") is not True:
                reasons.append("dataset is not marked annotation_supervision_only")
            if annotation_manifest_count < 1:
                reasons.append("no hash-bound Gazebo 3-D annotation frame set")
            if annotation_supervision_rows < 100:
                reasons.append("fewer than 100 rows have exact 3-D annotation supervision")
            if len(record["scenario_ids"]) < 2:
                reasons.append("fewer than two approved 3-D scenarios")
            record["reason"] = "; ".join(reasons) or "rendered dataset passes its declared checks"
        else:
            episodes = document.get("episodes")
            episodes = episodes if isinstance(episodes, list) else []
            transitions = [
                transition
                for episode in episodes
                if isinstance(episode, dict)
                for transition in (episode.get("transitions") or [])
                if isinstance(transition, dict)
            ]
            recording_bound = document.get("recording_bound") is True and all(
                episode.get("recording_bound") is True
                for episode in episodes
                if isinstance(episode, dict)
            )
            record.update(
                {
                    "episode_count": len(episodes),
                    "transition_count": len(transitions),
                    "recording_bound": recording_bound,
                    "materialized_sources": document.get("materialized_sources") is True,
                    "scenario_ids": sorted(
                        {
                            str(episode.get("scene_id"))
                            for episode in episodes
                            if isinstance(episode, dict)
                            and isinstance(episode.get("scene_id"), str)
                        }
                    ),
                }
            )
            reasons = []
            if document.get("training_ready") is not True:
                reasons.append("trajectory does not declare training_ready=true")
            if not recording_bound:
                reasons.append("recording-bound telemetry is missing")
            if document.get("materialized_sources") is not True:
                reasons.append("raw RGB/Depth sources are not materialized for review")
            if len(transitions) < 100:
                reasons.append("fewer than 100 trajectory transitions")
            record["reason"] = "; ".join(reasons) or "trajectory dataset passes its declared checks"
        record["training_ready"] = record["declared_training_ready"]
        record["eligible_for_real_3d_training"] = (
            record["training_ready"]
            and record["truth_free"]
            and not record["truth_injected"]
            and (
                kind != "trajectory"
                or (record.get("recording_bound") and record.get("materialized_sources"))
            )
            and (
                kind != "rendered_3d"
                or record.get("annotation_supervision_gate", {}).get("passed") is True
            )
        )
        inventory.append(record)
    return inventory


def build_preflight(
    *,
    reference_path: Path = DEFAULT_REFERENCE,
    manifest_path: Path = DEFAULT_MANIFEST,
    detector_model_path: Path = DEFAULT_DETECTOR_MODEL,
    capture_plan_path: Path = ROOT / "configs" / "scenarios" / "gazebo-3d-capture-plan-v1.json",
    rendered_datasets: Sequence[Path] = DEFAULT_RENDERED_DATASETS,
    trajectory_datasets: Sequence[Path] = DEFAULT_TRAJECTORY_DATASETS,
    evaluation_root: Path = DEFAULT_EVALUATION_ROOT,
    which: Which = shutil.which,
    run: Run = subprocess.run,
    local_app_data: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic preflight report without starting any runtime."""

    reference = load_onboard_model_reference(reference_path, repository_root=ROOT)
    manifest = verify_manifest(manifest_path, repository_root=ROOT)
    capture_plan_path = capture_plan_path.expanduser().resolve()
    if not capture_plan_path.is_file():
        raise PreflightError(
            "the offline 3-D capture plan is missing; regenerate it from the current "
            f"mainline manifest before capture: {capture_plan_path}"
        )
    detector_model_path = detector_model_path.expanduser().resolve()
    if not detector_model_path.is_file():
        raise PreflightError(
            "the existing YOLO detector model is missing; a 3-D capture must not "
            "silently fall back to structured or truth-injected observations: "
            f"{detector_model_path}"
        )
    tools = _probe_tools(which=which, run=run, local_app_data=local_app_data)
    capture_inventory = audit_inventory(
        evaluation_root=evaluation_root,
        mainline_manifest=manifest_path,
        minimum_frames=100,
    )
    scenarios = []
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    for raw in document["scenarios"]:
        scenario_id = str(raw["scenario_id"])
        vehicle_class = str(raw.get("vehicle", {}).get("class", "unknown"))
        if vehicle_class == "multirotor":
            try:
                loaded_plan = load_capture_plan(
                    capture_plan_path,
                    scenario_id=scenario_id,
                    vehicle_class=vehicle_class,
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                raise PreflightError(
                    f"capture plan is invalid for {scenario_id}: {exc}"
                ) from exc
            if loaded_plan.get("manifest_sha256") != _sha256(manifest_path):
                raise PreflightError(
                    f"capture plan manifest hash does not match current mainline: {scenario_id}"
                )
        scenario_path = ROOT / str(raw["scenario_manifest"]["path"])
        scenarios.append(
            _capture_command(
                reference,
                reference_path,
                scenario_path,
                scenario_id,
                detector_model_path,
                vehicle_class,
                capture_plan_path,
            )
        )

    training_input_inventory = [
        *_training_input_inventory(rendered_datasets, kind="rendered_3d"),
        *_training_input_inventory(trajectory_datasets, kind="trajectory"),
    ]
    docker_ready = bool(tools["docker"]["server_available"])
    reasons: list[str] = []
    if not docker_ready:
        reasons.append("Docker daemon is not reachable; external Gazebo/PX4 capture cannot start")
        backend_diagnostic = tools["docker"].get("backend_diagnostic", {})
        if backend_diagnostic.get("failure_kind") == "docker_desktop_stale_socket":
            socket_name = backend_diagnostic.get("stale_socket_name", "the reported socket")
            reasons.append(
                f"Docker Desktop backend is blocked by stale {socket_name}; release the "
                "reported socket and restart Docker Desktop before capture"
            )
    if not tools["gazebo"]["local_cli_available"]:
        reasons.append(
            "local Gazebo CLI (gz or gazebo) is not installed; "
            "the pinned container route may supply it"
        )
    if not tools["px4"]["local_cli_available"]:
        reasons.append("local PX4 CLI is not installed; the pinned container route may supply it")
    if docker_ready:
        reasons.append("prerequisites are visible, but no 3-D capture has been executed")
    reasons.append(
        "training_ready remains false until sealed RGB/Depth/telemetry episodes "
        "and an independent holdout exist"
    )
    if capture_inventory["capture_eligible_count"] == 0:
        reasons.append(
            "no current-mainline RGB + FLOAT32 depth capture is eligible for OnboardAI training"
        )
    rendered_visibility_gates = [
        item.get("target_visibility_gate")
        for item in training_input_inventory
        if item.get("kind") == "rendered_3d"
        and isinstance(item.get("target_visibility_gate"), dict)
    ]
    if rendered_visibility_gates and not any(
        gate.get("passed") is True for gate in rendered_visibility_gates
    ):
        reasons.append(
            "no rendered 3-D dataset currently passes the target-visibility gate; capture must "
            "contain detector hit, FLOAT32 depth measurement, and calibrated "
            "camera/depth/pose geometry"
        )
    rendered_annotation_gates = [
        item.get("annotation_supervision_gate")
        for item in training_input_inventory
        if item.get("kind") == "rendered_3d"
        and isinstance(item.get("annotation_supervision_gate"), dict)
    ]
    if rendered_annotation_gates and not any(
        gate.get("passed") is True for gate in rendered_annotation_gates
    ):
        reasons.append(
            "no rendered 3-D dataset currently passes the annotation-supervision gate; "
            "capture-only 3-D labels-map payloads must be hash-bound to every RGB frame"
        )

    return {
        "schema": 1,
        "contract": CONTRACT,
        "status": "ready_to_execute" if docker_ready else "blocked",
        "training_ready": False,
        "generated_by": "scripts/run_onboard_3d_training_preflight.py",
        "claim_boundary": {
            "repository_3d_asset_audit": True,
            "external_gazebo_runtime": "not_executed",
            "jetson_evidence": "not_claimed",
            "hil_real_evidence": "not_claimed",
            "outdoor_gnss_evidence": "not_claimed",
            "final_release": "not_claimed",
        },
        "onboard_model_reference": {
            "path": _repo_relative(reference_path),
            "file_sha256": _sha256(reference_path),
            "model_id": reference.model_id,
            "model_version": reference.model_version,
            "model_path": _repo_relative(reference.model_path),
            "model_sha256": reference.model_sha256,
            "feature_width": reference.feature_width,
            "status": reference.status,
            "same_reference_for_sim_and_real": True,
        },
        "detector_reference": {
            "path": _repo_relative(detector_model_path),
            "file_sha256": _sha256(detector_model_path),
            "role": "existing_yolo_detector_for_3d_sensor_capture",
            "production_approved": False,
        },
        "gazebo_3d_manifest": {
            "path": _repo_relative(manifest_path),
            "file_sha256": _sha256(manifest_path),
            "manifest_id": manifest["manifest_id"],
            "scenario_count": manifest["scenario_count"],
            "scenario_ids": [item["scenario_id"] for item in manifest["scenarios"]],
            "billboard_perception_fixtures_excluded": True,
        },
        "capture_plan": {
            "path": _repo_relative(capture_plan_path),
            "file_sha256": _sha256(capture_plan_path),
            "target_pose_excluded_from_runtime_model_input": True,
            "target_pose_used_for_offline_route_only": False,
            "target_pose_used_for_capture_route": False,
        },
        "tools": tools,
        "training_input_inventory": training_input_inventory,
        "capture_inventory": capture_inventory,
        "reasons": reasons,
        "capture_matrix": scenarios,
        "next_stage": {
            "capture": "execute one generated command only after Docker/PX4/Gazebo is available",
            "dataset": (
                "scripts/build_onboard_rendered_3d_dataset.py with RGB and paired "
                "FLOAT32 depth manifests"
            ),
            "inventory": (
                "resolve capture_inventory before training; do not train from historical, "
                "stale, truth-injected, or RGB-only captures"
            ),
            "training": (
                "scripts/train_onboard_model.py using the sealed rendered-3-D "
                "dataset and independent holdout"
            ),
            "promotion": (
                "not allowed from preflight; retain current reference until "
                "objective holdout improves"
            ),
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, default=DEFAULT_REFERENCE)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--detector-model", type=Path, default=DEFAULT_DETECTOR_MODEL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--evaluation-root", type=Path, default=DEFAULT_EVALUATION_ROOT)
    parser.add_argument("--rendered-dataset", type=Path, action="append", default=[])
    parser.add_argument("--trajectory-dataset", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    try:
        report = build_preflight(
            reference_path=args.reference,
            manifest_path=args.manifest,
            detector_model_path=args.detector_model,
            rendered_datasets=(args.rendered_dataset or list(DEFAULT_RENDERED_DATASETS)),
            trajectory_datasets=(args.trajectory_dataset or list(DEFAULT_TRAJECTORY_DATASETS)),
            evaluation_root=args.evaluation_root,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {"schema": 1, "contract": CONTRACT, "status": "error", "error": str(exc)},
                ensure_ascii=False,
            )
        )
        return 2
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
