#!/usr/bin/env python3
"""Execute (or only preflight) the repository-owned Docker SIM runtime.

``run_simulator_docker.py`` is deliberately a planner.  This companion is the
small, explicit process boundary for a real Docker Desktop run: it consumes
the hash-bound planner output, starts the pinned PX4/Gazebo image only when
``--execute`` is present, observes loopback MAVLink telemetry, validates the
container contract, and always stops/removes the disposable container.

The evidence intentionally keeps four claims separate:

* ``preflight`` — scenario/assets/ports/command are structurally valid;
* ``container_runtime`` — Docker actually created and ran the container;
* ``telemetry`` — a real PX4 heartbeat and qualified GPS message arrived;
* ``visual``/``hardware`` — QGC desktop rendering and physical hardware are
  not inferred from a headless Docker run and therefore remain ``not_claimed``.

The helper is passive with respect to flight control.  It sends at most a GCS
heartbeat to keep MAVLink streams alive and never sends ARM, mission, actuator,
or transition commands.  The generated planner command remains the source of
truth for image, mounts, environment, and loopback ports.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    _repository_source = Path(__file__).resolve().parents[1] / "src"
    if _repository_source.is_dir() and str(_repository_source) not in sys.path:
        sys.path.insert(0, str(_repository_source))

from multidetect.autonomy.serialization import canonical_sha256
from multidetect.backends.gazebo_sim import (
    GAZEBO_SIM_ATTITUDE_UNCERTAINTY_SOURCE,
    GAZEBO_SIM_CAMERA_INTRINSICS_SEMANTIC_VALIDATION,
    GAZEBO_SIM_DEFAULT_MINIMUM_HEADING_SIGMA_DEG,
    GAZEBO_SIM_DEFAULT_MINIMUM_PITCH_SIGMA_DEG,
    GAZEBO_SIM_DEFAULT_MINIMUM_ROLL_SIGMA_DEG,
    GAZEBO_SIM_SOURCE_ROW_ORDER,
    GAZEBO_SIM_TERRAIN_AGL_REFERENCE,
    GAZEBO_SIM_VERTICAL_FLIP_APPLIED,
    GAZEBO_SIM_WIRE_ROW_ORDER,
    GazeboSimBackend,
    GazeboSimFrameProvenance,
    GazeboSimWireTelemetryProvenance,
    gazebo_sim_attitude_sigmas_deg,
    gazebo_sim_ned_frd_kinematics,
    reconstruct_gazebo_sim_wire_frame,
)
from multidetect.gazebo_depth import GazeboDepthFrame
from multidetect.gazebo_fault_verification import (
    SensorFaultDiagnosticError,
    verify_sensor_fault_diagnostic,
)
from multidetect.multimodal_ranging import CameraCalibration, load_camera_calibration
from multidetect.product_version import PRODUCT_VERSION
from multidetect.sim_network import sim_listen_host, sim_udpin_endpoint
from multidetect.runtime import RuntimeMode
from multidetect.simulation import load_runtime_scenario
from multidetect.simulator.stack import airframe_profile_hash as stable_airframe_profile_hash
from multidetect.simulator.stack import resolve_repository_model_asset_bundle
from multidetect.terrain_dem import load_local_dem_grid
from multidetect.vision import CapturedFrame

try:
    # Importing the module is optional: dry-run planning must work on a host
    # without the pixhawk extra installed.  Tests replace this object with a
    # deterministic fake MAVLink module.
    from pymavlink import mavutil as _pymavlink_mavutil
except ImportError:  # pragma: no cover - depends on the local environment
    _pymavlink_mavutil = None

try:  # pytest imports ``scripts`` as a namespace package
    from scripts.run_simulator_docker import (
        DEFAULT_SCENARIO,
        PINNED_IMAGE,
        DockerSimulatorPlan,
        DockerSimulatorPlanError,
        build_docker_simulator_plan,
    )
except ModuleNotFoundError:  # direct ``python scripts/...`` invocation
    from run_simulator_docker import (  # type: ignore[no-redef]
        DEFAULT_SCENARIO,
        PINNED_IMAGE,
        DockerSimulatorPlan,
        DockerSimulatorPlanError,
        build_docker_simulator_plan,
    )

try:  # pytest imports ``scripts`` as a namespace package
    from scripts.gazebo_depth_wire import build_depth_wire_record
except ModuleNotFoundError:  # direct ``python scripts/...`` invocation
    from gazebo_depth_wire import build_depth_wire_record  # type: ignore[no-redef]


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE = ROOT / "artifacts" / "evaluation" / "simulator-docker-acceptance.json"
DEFAULT_CONTAINER_NAME = "multidetect-sim-acceptance"
DEFAULT_CONTROL_PORT = 14540
DEFAULT_TELEMETRY_PORT = 14541
PROTECTED_PORT = 14550
GCS_SOURCE_SYSTEM = 250
GCS_SOURCE_COMPONENT = 191
RELEASE_STATUS = (
    f"v{PRODUCT_VERSION.release_label}; final v{PRODUCT_VERSION.numeric} remains blocked"
)


class DockerSimulatorAcceptanceError(RuntimeError):
    """A bounded Docker SIM runtime gate failed."""


SimulatorDockerAcceptanceError = DockerSimulatorAcceptanceError

GAZEBO_BGR24_FRAME_SET_MANIFEST = "gazebo-bgr24-frame-set.manifest.json"
GAZEBO_BGR24_FRAME_SET_SCHEMA_VERSION = 2
GAZEBO_DEPTH_FRAME_SET_MANIFEST = "gazebo-depth-companion-set.manifest.json"
GAZEBO_DEPTH_FRAME_SET_SCHEMA_VERSION = 1
GAZEBO_SENSOR_FAULT_SOURCES = ("image", "navsat", "odometry")


def _validate_sensor_fault_window(
    *,
    frame_count: int,
    fault_source: str | None,
    fault_start_image: int,
    fault_duration_images: int,
) -> None:
    if fault_source is None:
        if fault_start_image != 0 or fault_duration_images != 0:
            raise DockerSimulatorAcceptanceError(
                "sensor fault window options require sensor_fault_source"
            )
        return
    if fault_source not in GAZEBO_SENSOR_FAULT_SOURCES:
        raise DockerSimulatorAcceptanceError(
            "sensor_fault_source must be image, navsat, or odometry"
        )
    minimum_frames = 2 if fault_source == "image" else 4
    if frame_count < minimum_frames:
        raise DockerSimulatorAcceptanceError(
            "sensor fault exercise requires at least "
            f"{minimum_frames} frames for {fault_source} recovery"
        )
    if (
        type(fault_start_image) is not int
        or fault_start_image < 1
        or type(fault_duration_images) is not int
        or fault_duration_images < 1
    ):
        raise DockerSimulatorAcceptanceError(
            "sensor fault start and duration must be positive integer image counts"
        )


def _verified_sensor_fault_diagnostic(
    stderr_tail: str,
    *,
    fault_source: str,
    fault_start_image: int,
    fault_duration_images: int,
) -> dict[str, object]:
    """Compatibility entry point; validation lives in the installed core."""

    try:
        return verify_sensor_fault_diagnostic(
            stderr_tail,
            fault_source=fault_source,
            fault_start_image=fault_start_image,
            fault_duration_images=fault_duration_images,
        )
    except SensorFaultDiagnosticError as exc:
        raise DockerSimulatorAcceptanceError(str(exc)) from exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _model_assets_provenance(
    model_root: Path,
    model_name: str,
    *,
    repository_root: Path,
) -> dict[str, object]:
    """Bind every byte exposed by the repository-owned Gazebo model mount."""

    model_directory = (model_root / model_name).resolve()
    if not model_directory.is_dir() or model_directory.parent != model_root.resolve():
        raise DockerSimulatorAcceptanceError(
            f"Gazebo model directory is unavailable or escaped its root: {model_directory}"
        )
    active_files = sorted(path for path in model_directory.rglob("*") if path.is_file())
    if not active_files:
        raise DockerSimulatorAcceptanceError(f"Gazebo model directory is empty: {model_directory}")
    bundle = resolve_repository_model_asset_bundle(
        model_root,
        repository_root=repository_root,
    )
    return {
        **bundle.portable_mapping(),
        "active_model_name": model_name,
        "active_model_file_count": len(active_files),
    }


def _model_assets_sha256(
    model_root: Path,
    model_name: str,
    *,
    repository_root: Path | None = None,
) -> str:
    """Compatibility helper returning the complete mounted-model digest."""

    repository = repository_root or model_root.resolve().parents[2]
    provenance = _model_assets_provenance(
        model_root,
        model_name,
        repository_root=repository,
    )
    return str(provenance["assets_sha256"])


def _camera_calibration_provenance(
    *,
    plan: DockerSimulatorPlan,
    scenario_input: Any,
    expected_width: int,
    expected_height: int,
) -> dict[str, object]:
    """Load and hash-bind the scenario camera calibration used for ranging."""

    rgb_sensors = tuple(sensor for sensor in scenario_input.sensors if sensor.kind == "rgb_camera")
    if len(rgb_sensors) != 1 or rgb_sensors[0].calibration_id is None:
        raise DockerSimulatorAcceptanceError(
            "Gazebo frame persistence requires exactly one calibration-bound RGB camera"
        )
    calibration_id = rgb_sensors[0].calibration_id
    repository_root = plan.repository_root.resolve()
    calibration_root = (repository_root / "simulation/gazebo/calibration").resolve()
    calibration_path = (calibration_root / f"{calibration_id}.json").resolve()
    if (
        calibration_path.parent != calibration_root
        or calibration_path.is_symlink()
        or not calibration_path.is_file()
    ):
        raise DockerSimulatorAcceptanceError(
            f"Gazebo camera calibration is unavailable or escaped its root: {calibration_path}"
        )
    try:
        calibration = load_camera_calibration(calibration_path)
    except ValueError as exc:
        raise DockerSimulatorAcceptanceError(str(exc)) from exc
    if calibration.calibration_id != calibration_id:
        raise DockerSimulatorAcceptanceError(
            "Gazebo camera calibration ID does not match the scenario RGB sensor"
        )
    if (calibration.width_px, calibration.height_px) != (
        expected_width,
        expected_height,
    ):
        raise DockerSimulatorAcceptanceError(
            "Gazebo camera calibration geometry does not match the persisted BGR24 frames: "
            f"{calibration.width_px}x{calibration.height_px} != "
            f"{expected_width}x{expected_height}"
        )
    return {
        "schema_version": 1,
        "calibration_id": calibration.calibration_id,
        "repository_path": calibration_path.relative_to(repository_root).as_posix(),
        "file_sha256": _sha256(calibration_path),
        "calibration": asdict(calibration),
        "source": "repository_calibration_hash_bound",
        "camera_info_intrinsics_semantic_validation": ("pending_k_p_distortion_validation"),
    }


def _terrain_dem_provenance(
    *,
    plan: DockerSimulatorPlan,
    terrain_dem: Any,
) -> dict[str, object]:
    """Bind the exact MSL DEM needed to reproduce every frame's AGL."""

    repository_root = plan.repository_root.resolve()
    dem_path = plan.dem_path.resolve()
    try:
        repository_path = dem_path.relative_to(repository_root).as_posix()
    except ValueError as exc:
        raise DockerSimulatorAcceptanceError(
            f"Gazebo terrain DEM escaped the repository: {dem_path}"
        ) from exc
    if dem_path.is_symlink() or not dem_path.is_file():
        raise DockerSimulatorAcceptanceError(
            f"Gazebo terrain DEM is not a regular repository file: {dem_path}"
        )
    if _sha256(dem_path) != terrain_dem.source_sha256:
        raise DockerSimulatorAcceptanceError("Gazebo terrain DEM changed after validation")
    return {
        "schema_version": 1,
        "scenario_id": terrain_dem.scenario_id,
        "repository_path": repository_path,
        "file_sha256": terrain_dem.source_sha256,
        "coordinate_reference": "WGS84",
        "vertical_datum": "MSL",
        "altitude_reference": GAZEBO_SIM_TERRAIN_AGL_REFERENCE,
        "odometry_local_up_used_as_agl": False,
        "origin": {
            "latitude_deg": terrain_dem.origin_latitude_deg,
            "longitude_deg": terrain_dem.origin_longitude_deg,
            "elevation_m": terrain_dem.origin_elevation_m,
        },
        "grid": {
            "north_origin_m": terrain_dem.north_origin_m,
            "east_origin_m": terrain_dem.east_origin_m,
            "north_spacing_m": terrain_dem.north_spacing_m,
            "east_spacing_m": terrain_dem.east_spacing_m,
            "rows": len(terrain_dem.elevations_m),
            "columns": len(terrain_dem.elevations_m[0]),
            "vertical_sigma_m": terrain_dem.vertical_sigma_m,
            "slope_sigma_m_per_m": terrain_dem.slope_sigma_m_per_m,
        },
    }


def _empty_sensor_transport(
    *,
    execute: bool,
    frame_count: int,
    frame_output_directory: Path | None,
    depth_output_directory: Path | None,
    fault_source: str | None = None,
    fault_start_image: int = 0,
    fault_duration_images: int = 0,
) -> dict[str, object]:
    requested = frame_count > 0
    persistence_requested = frame_output_directory is not None
    return {
        "status": (
            "not_requested"
            if not requested
            else "not_started"
            if execute
            else "planned_not_executed"
        ),
        "requested": requested,
        "requested_frame_count": frame_count,
        "qualified": False,
        "wire_version": 3,
        "source_row_order": GAZEBO_SIM_SOURCE_ROW_ORDER,
        "wire_row_order": GAZEBO_SIM_WIRE_ROW_ORDER,
        "vertical_flip_applied": None,
        "odometry_required": True,
        "full_odometry_pose_velocity_fusion_claimed": False,
        "terrain_agl_reference": GAZEBO_SIM_TERRAIN_AGL_REFERENCE,
        "odometry_local_up_used_as_agl": False,
        "truth_free": True,
        "pixel_persistence": {
            "requested": persistence_requested,
            "status": (
                "disabled"
                if not persistence_requested
                else "not_started"
                if execute
                else "planned_not_executed"
            ),
            "output_directory": (
                str(frame_output_directory) if frame_output_directory is not None else None
            ),
            "persisted_frame_count": 0,
            "raw_bytes_sha256_matches_wire_payload": None,
            "manifest": None,
            "actual_gazebo_pixels_persisted": False,
            "qgc_rendering": "not_claimed",
            "manual_visual_acceptance": "not_claimed",
            "hardware": "not_claimed",
        },
        "depth_persistence": {
            "requested": depth_output_directory is not None,
            "status": (
                "disabled"
                if depth_output_directory is None and not requested
                else "not_started"
                if execute
                else "planned_not_executed"
            ),
            "output_directory": (
                str(depth_output_directory) if depth_output_directory is not None else None
            ),
            "persisted_frame_count": 0,
            "total_payload_bytes": 0,
            "raw_bytes_sha256_matches_wire_payload": None,
            "manifest": None,
            "actual_gazebo_depth_persisted": False,
            "metric_depth_model_calibration": "not_claimed",
            "outdoor_calibration": "not_claimed",
            "hardware": "not_claimed",
            "hil": "not_claimed",
            "real": "not_claimed",
        },
        "fault_injection": {
            "enabled": fault_source is not None,
            "source": fault_source,
            "start_image": fault_start_image if fault_source is not None else None,
            "duration_images": fault_duration_images if fault_source is not None else 0,
            "window_key": "received_image_index",
            "recovery_observed": None,
        },
    }


def _resolve_frame_output_directory(
    value: str | Path | None,
    *,
    evidence_path: Path,
    frame_count: int,
) -> Path | None:
    """Validate the optional exclusive raw-frame destination without creating it."""

    if value is None:
        return None
    if frame_count <= 0:
        raise DockerSimulatorAcceptanceError(
            "sensor_frame_output_dir requires sensor_frame_count > 0"
        )
    destination = Path(value).expanduser().resolve()
    if destination == evidence_path or destination in evidence_path.parents:
        raise DockerSimulatorAcceptanceError(
            "sensor_frame_output_dir must not contain the acceptance evidence file"
        )
    if destination.exists():
        if not destination.is_dir():
            raise DockerSimulatorAcceptanceError(
                f"sensor frame output path is not a directory: {destination}"
            )
        try:
            first_entry = next(destination.iterdir(), None)
        except OSError as exc:
            raise DockerSimulatorAcceptanceError(
                f"sensor frame output directory cannot be inspected: {destination}"
            ) from exc
        if first_entry is not None:
            raise DockerSimulatorAcceptanceError(
                f"sensor frame output directory must be empty: {destination}"
            )
    return destination


def _resolve_depth_output_directory(
    value: str | Path | None,
    *,
    evidence_path: Path,
    frame_count: int,
    frame_output_directory: Path | None,
) -> Path | None:
    """Validate the opt-in exclusive raw-depth destination.

    Depth persistence is intentionally a sibling output contract.  Requiring
    the RGB frame set at the same time guarantees that a later metric-depth
    replay can reproduce the exact RGB/Depth pair instead of retaining only a
    sensor payload with no image companion.
    """

    if value is None:
        return None
    if frame_count <= 0:
        raise DockerSimulatorAcceptanceError(
            "sensor_depth_output_dir requires sensor_frame_count > 0"
        )
    if frame_output_directory is None:
        raise DockerSimulatorAcceptanceError(
            "sensor_depth_output_dir requires sensor_frame_output_dir"
        )
    destination = Path(value).expanduser().resolve()
    if destination == evidence_path or destination in evidence_path.parents:
        raise DockerSimulatorAcceptanceError(
            "sensor_depth_output_dir must not contain the acceptance evidence file"
        )
    if destination == frame_output_directory or destination in frame_output_directory.parents:
        raise DockerSimulatorAcceptanceError(
            "sensor_depth_output_dir must be separate from sensor_frame_output_dir"
        )
    if frame_output_directory == destination or frame_output_directory in destination.parents:
        raise DockerSimulatorAcceptanceError(
            "sensor_depth_output_dir must be separate from sensor_frame_output_dir"
        )
    if destination.exists():
        if not destination.is_dir():
            raise DockerSimulatorAcceptanceError(
                f"sensor depth output path is not a directory: {destination}"
            )
        try:
            first_entry = next(destination.iterdir(), None)
        except OSError as exc:
            raise DockerSimulatorAcceptanceError(
                f"sensor depth output directory cannot be inspected: {destination}"
            ) from exc
        if first_entry is not None:
            raise DockerSimulatorAcceptanceError(
                f"sensor depth output directory must be empty: {destination}"
            )
    return destination


def _prepare_frame_output_directory(destination: Path) -> None:
    """Create the exclusive frame-set directory and recheck it immediately."""

    destination.mkdir(parents=True, exist_ok=True)
    if not destination.is_dir():  # pragma: no cover - guarded by mkdir
        raise DockerSimulatorAcceptanceError(
            f"sensor frame output path is not a directory: {destination}"
        )
    try:
        first_entry = next(destination.iterdir(), None)
    except OSError as exc:
        raise DockerSimulatorAcceptanceError(
            f"sensor frame output directory cannot be inspected: {destination}"
        ) from exc
    if first_entry is not None:
        raise DockerSimulatorAcceptanceError(
            f"sensor frame output directory must be empty: {destination}"
        )


def _frame_byte_view(frame: CapturedFrame, provenance: GazeboSimFrameProvenance) -> memoryview:
    """Return the existing contiguous BGR payload view without copying pixels."""

    if frame.frame_id != provenance.frame_id:
        raise DockerSimulatorAcceptanceError("Gazebo frame/provenance frame_id mismatch")
    if (frame.width, frame.height) != (provenance.width, provenance.height):
        raise DockerSimulatorAcceptanceError("Gazebo frame/provenance geometry mismatch")
    try:
        view = memoryview(frame.image_bgr)
    except TypeError as exc:
        raise DockerSimulatorAcceptanceError("Gazebo frame pixels do not expose a buffer") from exc
    if not view.c_contiguous:
        raise DockerSimulatorAcceptanceError("Gazebo BGR24 pixels must be C-contiguous")
    try:
        byte_view = view.cast("B")
    except (TypeError, ValueError) as exc:
        raise DockerSimulatorAcceptanceError(
            "Gazebo frame pixels are not byte-addressable"
        ) from exc
    if byte_view.nbytes != provenance.payload_bytes:
        raise DockerSimulatorAcceptanceError("Gazebo frame/provenance payload size mismatch")
    if provenance.stride != provenance.width * 3:
        raise DockerSimulatorAcceptanceError("Gazebo provenance BGR24 stride is invalid")
    observed_sha256 = hashlib.sha256(byte_view).hexdigest()
    if observed_sha256 != provenance.payload_sha256:
        raise DockerSimulatorAcceptanceError(
            "Gazebo frame pixels no longer match the validated wire payload"
        )
    return byte_view


def _write_exclusive_bytes(path: Path, payload: memoryview) -> None:
    """Write one bounded payload without replacing any existing artifact."""

    try:
        with path.open("xb") as stream:
            offset = 0
            while offset < payload.nbytes:
                written = stream.write(payload[offset:])
                if written is None or written <= 0:
                    raise OSError("raw frame write made no progress")
                offset += written
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise DockerSimulatorAcceptanceError(
            f"refusing to overwrite persisted Gazebo frame: {path}"
        ) from exc


def _persist_gazebo_frame(
    *,
    output_directory: Path,
    frame: CapturedFrame,
    provenance: GazeboSimFrameProvenance,
) -> dict[str, object]:
    """Persist and immediately re-hash one exact raw BGR24 payload."""

    payload = _frame_byte_view(frame, provenance)
    file_name = f"frame-{provenance.sequence:06d}.bgr24"
    frame_path = output_directory / file_name
    _write_exclusive_bytes(frame_path, payload)
    observed_bytes = frame_path.stat().st_size
    observed_sha256 = _sha256(frame_path)
    if observed_bytes != provenance.payload_bytes:
        raise DockerSimulatorAcceptanceError(
            f"persisted Gazebo frame byte count mismatch: {frame_path}"
        )
    if observed_sha256 != provenance.payload_sha256:
        raise DockerSimulatorAcceptanceError(
            f"persisted Gazebo frame SHA-256 mismatch: {frame_path}"
        )
    return {
        "frame_id": provenance.frame_id,
        "sequence": provenance.sequence,
        "file": file_name,
        "bytes": observed_bytes,
        "sha256": observed_sha256,
        "wire_payload_sha256": provenance.payload_sha256,
        "raw_bytes_sha256_matches_wire_payload": True,
        "wire_record_sha256": provenance.record_sha256,
        "provenance": provenance.to_mapping(),
    }


def _depth_file_suffix(encoding: str) -> str:
    suffixes = {"FLOAT32_M": "float32_m", "UINT16_MM": "uint16_mm"}
    try:
        return suffixes[encoding]
    except KeyError as exc:
        raise DockerSimulatorAcceptanceError(
            f"unsupported persisted Gazebo depth encoding: {encoding!r}"
        ) from exc


def _depth_wire_metadata(
    *,
    depth: GazeboDepthFrame,
    provenance: GazeboSimFrameProvenance,
) -> dict[str, object]:
    """Rebuild the exact MDGZDEP1 metadata from the validated host objects."""

    if depth.frame_id != provenance.frame_id:
        raise DockerSimulatorAcceptanceError("Gazebo depth/RGB frame_id binding mismatch")
    if provenance.depth_record_sha256 is None or provenance.depth_payload_sha256 is None:
        raise DockerSimulatorAcceptanceError("Gazebo depth provenance is incomplete")
    if provenance.depth_source_stamp_ns != depth.source_stamp_ns:
        raise DockerSimulatorAcceptanceError("Gazebo depth source timestamp binding mismatch")
    if depth.frame_sensor_stamp_ns != provenance.sensor_stamp_ns:
        raise DockerSimulatorAcceptanceError("Gazebo depth/RGB sensor timestamp mismatch")
    if provenance.depth_source_skew_ns != (provenance.sensor_stamp_ns - depth.source_stamp_ns):
        raise DockerSimulatorAcceptanceError("Gazebo depth source skew binding mismatch")
    if provenance.depth_topic != depth.source_topic:
        raise DockerSimulatorAcceptanceError("Gazebo depth source topic binding mismatch")
    if provenance.depth_calibration_id != depth.calibration_id:
        raise DockerSimulatorAcceptanceError("Gazebo depth calibration ID binding mismatch")
    if provenance.depth_geometry != (
        depth.width,
        depth.height,
        depth.stride_bytes,
    ):
        raise DockerSimulatorAcceptanceError("Gazebo depth geometry binding mismatch")
    if provenance.depth_encoding != depth.encoding:
        raise DockerSimulatorAcceptanceError("Gazebo depth encoding binding mismatch")
    if provenance.depth_distance_semantics != depth.distance_semantics:
        raise DockerSimulatorAcceptanceError("Gazebo depth semantics binding mismatch")
    calibration_sha256 = provenance.depth_calibration_sha256
    if calibration_sha256 is None:
        raise DockerSimulatorAcceptanceError("Gazebo depth calibration digest is missing")
    return {
        "sequence": provenance.sequence,
        "rgb_sensor_stamp_ns": provenance.sensor_stamp_ns,
        "rgb_record_sha256": provenance.record_sha256,
        "source_stamp_ns": depth.source_stamp_ns,
        "source_topic": depth.source_topic,
        "calibration_id": depth.calibration_id,
        "calibration_sha256": calibration_sha256,
        "registration_mode": "registered_to_rgb",
        "coordinate_space": "rgb_pixel_aligned",
        "encoding": depth.encoding,
        "distance_semantics": depth.distance_semantics,
        "width": depth.width,
        "height": depth.height,
        "stride_bytes": depth.stride_bytes,
        "minimum_depth_m": depth.minimum_depth_m,
        "maximum_depth_m": depth.maximum_depth_m,
        "fx_px": depth.fx_px,
        "fy_px": depth.fy_px,
        "cx_px": depth.cx_px,
        "cy_px": depth.cy_px,
    }


def _persist_gazebo_depth_frame(
    *,
    output_directory: Path,
    depth: GazeboDepthFrame,
    provenance: GazeboSimFrameProvenance,
) -> dict[str, object]:
    """Persist one exact MDGZDEP1 payload and bind it to the RGB record."""

    if not isinstance(depth, GazeboDepthFrame):
        raise DockerSimulatorAcceptanceError("Gazebo depth companion has an invalid type")
    try:
        payload = memoryview(depth.payload).cast("B")
    except (TypeError, ValueError) as exc:
        raise DockerSimulatorAcceptanceError(
            "Gazebo depth payload is not byte-addressable"
        ) from exc
    if hashlib.sha256(payload).hexdigest() != depth.payload_sha256:
        raise DockerSimulatorAcceptanceError("Gazebo depth payload no longer matches its SHA-256")
    if provenance.depth_payload_sha256 != depth.payload_sha256:
        raise DockerSimulatorAcceptanceError("Gazebo depth payload/RGB provenance digest mismatch")
    metadata = _depth_wire_metadata(depth=depth, provenance=provenance)
    try:
        wire_record = build_depth_wire_record(metadata=metadata, payload=payload)
    except (TypeError, ValueError) as exc:
        raise DockerSimulatorAcceptanceError(
            f"Gazebo depth companion failed MDGZDEP1 reconstruction: {exc}"
        ) from exc
    # The digest is the signed header digest, not sha256(record bytes).  Build
    # returns the exact bytes, so decode the fixed header field without making
    # the payload itself part of the manifest a second time.
    import struct

    digest = struct.unpack_from("<8sHHQQQIIIII32s", wire_record, 0)[-1].hex()
    if digest != provenance.depth_record_sha256:
        raise DockerSimulatorAcceptanceError("Gazebo depth wire digest reconstruction mismatch")
    file_name = f"depth-{provenance.sequence:06d}.{_depth_file_suffix(depth.encoding)}"
    depth_path = output_directory / file_name
    _write_exclusive_bytes(depth_path, payload)
    observed_bytes = depth_path.stat().st_size
    observed_sha256 = _sha256(depth_path)
    if observed_bytes != len(payload) or observed_sha256 != depth.payload_sha256:
        raise DockerSimulatorAcceptanceError(
            f"persisted Gazebo depth payload failed integrity verification: {depth_path}"
        )
    return {
        "frame_id": provenance.frame_id,
        "sequence": provenance.sequence,
        "file": file_name,
        "bytes": observed_bytes,
        "sha256": observed_sha256,
        "wire_payload_sha256": depth.payload_sha256,
        "raw_bytes_sha256_matches_wire_payload": True,
        "wire_record_sha256": provenance.depth_record_sha256,
        "rgb_sensor_stamp_ns": provenance.sensor_stamp_ns,
        "rgb_record_sha256": provenance.record_sha256,
        "source_stamp_ns": depth.source_stamp_ns,
        "source_topic": depth.source_topic,
        "calibration_id": depth.calibration_id,
        "calibration_sha256": provenance.depth_calibration_sha256,
        "registration_mode": metadata["registration_mode"],
        "coordinate_space": metadata["coordinate_space"],
        "encoding": depth.encoding,
        "distance_semantics": depth.distance_semantics,
        "width": depth.width,
        "height": depth.height,
        "stride_bytes": depth.stride_bytes,
        "minimum_depth_m": depth.minimum_depth_m,
        "maximum_depth_m": depth.maximum_depth_m,
        "fx_px": depth.fx_px,
        "fy_px": depth.fy_px,
        "cx_px": depth.cx_px,
        "cy_px": depth.cy_px,
    }


def _verify_persisted_gazebo_depth(
    output_directory: Path,
    record: Mapping[str, object],
) -> None:
    """Re-hash a persisted depth payload and reconstruct its signed wire digest."""

    file_name = record.get("file")
    expected_bytes = record.get("bytes")
    expected_sha256 = record.get("sha256")
    if (
        not isinstance(file_name, str)
        or Path(file_name).name != file_name
        or isinstance(expected_bytes, bool)
        or not isinstance(expected_bytes, int)
        or expected_bytes <= 0
        or not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        raise DockerSimulatorAcceptanceError("persisted Gazebo depth record is invalid")
    depth_path = output_directory / file_name
    if depth_path.is_symlink() or not depth_path.is_file():
        raise DockerSimulatorAcceptanceError(
            f"persisted Gazebo depth payload is missing: {depth_path}"
        )
    payload = depth_path.read_bytes()
    if len(payload) != expected_bytes or hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise DockerSimulatorAcceptanceError(
            f"persisted Gazebo depth payload failed post-write integrity verification: {depth_path}"
        )
    metadata = {
        key: record.get(key)
        for key in (
            "sequence",
            "rgb_sensor_stamp_ns",
            "rgb_record_sha256",
            "source_stamp_ns",
            "source_topic",
            "calibration_id",
            "calibration_sha256",
            "registration_mode",
            "coordinate_space",
            "encoding",
            "distance_semantics",
            "width",
            "height",
            "stride_bytes",
            "minimum_depth_m",
            "maximum_depth_m",
            "fx_px",
            "fy_px",
            "cx_px",
            "cy_px",
        )
    }
    try:
        wire_record = build_depth_wire_record(metadata=metadata, payload=payload)
        import struct

        digest = struct.unpack_from("<8sHHQQQIIIII32s", wire_record, 0)[-1].hex()
    except (TypeError, ValueError, struct.error) as exc:
        raise DockerSimulatorAcceptanceError(
            f"persisted Gazebo depth wire record is invalid: {depth_path}: {exc}"
        ) from exc
    if digest != record.get("wire_record_sha256"):
        raise DockerSimulatorAcceptanceError(
            f"persisted Gazebo depth wire digest mismatch: {depth_path}"
        )


def _verify_persisted_gazebo_frame(
    output_directory: Path,
    record: dict[str, object],
) -> None:
    """Fail closed if a persisted raw frame was changed after capture."""

    file_name = record.get("file")
    expected_bytes = record.get("bytes")
    expected_sha256 = record.get("sha256")
    if (
        not isinstance(file_name, str)
        or Path(file_name).name != file_name
        or isinstance(expected_bytes, bool)
        or not isinstance(expected_bytes, int)
        or expected_bytes <= 0
        or not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
    ):
        raise DockerSimulatorAcceptanceError("persisted Gazebo frame record is invalid")
    frame_path = output_directory / file_name
    if frame_path.is_symlink() or not frame_path.is_file():
        raise DockerSimulatorAcceptanceError(f"persisted Gazebo frame is missing: {frame_path}")
    if frame_path.stat().st_size != expected_bytes or _sha256(frame_path) != expected_sha256:
        raise DockerSimulatorAcceptanceError(
            f"persisted Gazebo frame failed post-write integrity verification: {frame_path}"
        )
    provenance = record.get("provenance")
    if not isinstance(provenance, dict):
        raise DockerSimulatorAcceptanceError("persisted Gazebo frame provenance is invalid")
    wire_fields = (
        provenance.get("wire_unsigned_header_hex"),
        provenance.get("wire_telemetry"),
        provenance.get("frame_telemetry"),
    )
    if all(value is None for value in wire_fields):
        return
    if any(value is None for value in wire_fields):
        raise DockerSimulatorAcceptanceError(
            "persisted Gazebo per-frame telemetry evidence is incomplete"
        )
    sequence = provenance.get("sequence")
    width = provenance.get("width")
    height = provenance.get("height")
    if any(
        isinstance(value, bool) or not isinstance(value, int) for value in (sequence, width, height)
    ):
        raise DockerSimulatorAcceptanceError("persisted Gazebo per-frame wire geometry is invalid")
    try:
        wire = reconstruct_gazebo_sim_wire_frame(
            unsigned_header_hex=str(provenance["wire_unsigned_header_hex"]),
            payload_bgr24=frame_path.read_bytes(),
            expected_sequence=sequence,
            expected_width=width,
            expected_height=height,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise DockerSimulatorAcceptanceError(
            f"persisted Gazebo wire record failed reconstruction: {frame_path}: {exc}"
        ) from exc
    if wire.record_sha256 != record.get("wire_record_sha256"):
        raise DockerSimulatorAcceptanceError(
            f"persisted Gazebo wire record SHA-256 mismatch: {frame_path}"
        )
    expected_wire_telemetry = GazeboSimWireTelemetryProvenance.from_wire(wire).to_mapping()
    if provenance.get("wire_telemetry") != expected_wire_telemetry:
        raise DockerSimulatorAcceptanceError(
            f"persisted Gazebo human-readable wire telemetry changed: {frame_path}"
        )
    frame_telemetry = provenance.get("frame_telemetry")
    if not isinstance(frame_telemetry, dict):
        raise DockerSimulatorAcceptanceError("persisted Gazebo derived frame telemetry is invalid")
    for key, expected in (
        ("latitude_deg", wire.latitude_deg),
        ("longitude_deg", wire.longitude_deg),
        ("gps_absolute_altitude_m", wire.altitude_m),
        ("local_north_m", wire.position_north_m),
        ("local_east_m", wire.position_east_m),
        ("local_down_m", -wire.position_up_m),
    ):
        if frame_telemetry.get(key) != expected:
            raise DockerSimulatorAcceptanceError(
                f"persisted Gazebo derived frame telemetry {key} changed: {frame_path}"
            )
    roll, pitch, heading, velocity_north, velocity_east, _velocity_down = (
        gazebo_sim_ned_frd_kinematics(wire)
    )
    for key, expected in (
        ("roll_deg", roll),
        ("pitch_deg", pitch),
        ("heading_deg", heading),
        ("velocity_north_mps", velocity_north),
        ("velocity_east_mps", velocity_east),
    ):
        if frame_telemetry.get(key) != expected:
            raise DockerSimulatorAcceptanceError(
                f"persisted Gazebo derived frame telemetry {key} changed: {frame_path}"
            )
    roll_sigma, pitch_sigma, heading_sigma = gazebo_sim_attitude_sigmas_deg(wire)
    for key, expected in (
        (
            "attitude_roll_sigma_deg",
            max(roll_sigma, GAZEBO_SIM_DEFAULT_MINIMUM_ROLL_SIGMA_DEG),
        ),
        (
            "attitude_pitch_sigma_deg",
            max(pitch_sigma, GAZEBO_SIM_DEFAULT_MINIMUM_PITCH_SIGMA_DEG),
        ),
        (
            "attitude_heading_sigma_deg",
            max(heading_sigma, GAZEBO_SIM_DEFAULT_MINIMUM_HEADING_SIGMA_DEG),
        ),
        ("attitude_uncertainty_source", GAZEBO_SIM_ATTITUDE_UNCERTAINTY_SOURCE),
    ):
        if frame_telemetry.get(key) != expected:
            raise DockerSimulatorAcceptanceError(
                f"persisted Gazebo derived frame telemetry {key} changed: {frame_path}"
            )


def _write_frame_set_manifest(
    *,
    output_directory: Path,
    plan: DockerSimulatorPlan,
    sensor_binding_hash: str,
    model_assets: dict[str, object],
    records: list[dict[str, object]],
    camera_calibration: dict[str, object] | None = None,
    terrain_dem: dict[str, object] | None = None,
    host_clock_identity: dict[str, object] | None = None,
) -> dict[str, object]:
    """Seal the exclusive raw-frame set with a canonical, self-declared hash."""

    for record in records:
        _verify_persisted_gazebo_frame(output_directory, record)
    wire_records = tuple(
        all(
            key in record.get("provenance", {})
            for key in (
                "wire_unsigned_header_hex",
                "wire_telemetry",
                "frame_telemetry",
            )
        )
        for record in records
    )
    if any(wire_records) and not all(wire_records):
        raise DockerSimulatorAcceptanceError(
            "Gazebo frame-set cannot mix legacy and per-frame telemetry records"
        )
    per_frame_telemetry_bound = bool(wire_records and all(wire_records))
    if per_frame_telemetry_bound and (
        not isinstance(camera_calibration, dict) or not isinstance(terrain_dem, dict)
    ):
        raise DockerSimulatorAcceptanceError(
            "Gazebo per-frame telemetry evidence requires calibration and DEM provenance"
        )
    expected_files = {str(record["file"]) for record in records}
    observed_files = {entry.name for entry in output_directory.iterdir()}
    if observed_files != expected_files:
        raise DockerSimulatorAcceptanceError(
            "Gazebo frame output directory contains an unexpected or missing entry"
        )
    source_attribution: dict[str, object] = {
        "pixels": "actual_gazebo_camera_sensor_output",
        "environment": "software_simulation",
        "wire": "MDGZSIM3",
        "source_row_order": GAZEBO_SIM_SOURCE_ROW_ORDER,
        "wire_row_order": GAZEBO_SIM_WIRE_ROW_ORDER,
        "vertical_flip_applied": GAZEBO_SIM_VERTICAL_FLIP_APPLIED,
        "repository_model_assets_sha256": model_assets["assets_sha256"],
        "repository_model_asset_file_count": model_assets["file_count"],
        "truth_loaded_by_runtime": False,
    }
    claims: dict[str, object] = {
        "actual_gazebo_pixels_persisted": True,
        "qgc_rendering": "not_claimed",
        "manual_visual_acceptance": "not_claimed",
        "hardware": "not_claimed",
        "detector_evaluation": "not_claimed",
        "production_model_approval": "not_claimed",
    }
    if per_frame_telemetry_bound:
        source_attribution.update(
            {
                "per_frame_wire_telemetry_persisted": True,
                "wire_record_recomputed_from_persisted_bytes": True,
                "camera_intrinsics_source": "repository_calibration_hash_bound",
                "camera_info_intrinsics_semantic_validation": camera_calibration[
                    "camera_info_intrinsics_semantic_validation"
                ],
                "terrain_source": "repository_dem_hash_bound",
            }
        )
        claims.update(
            {
                "per_frame_sensor_geolocation_input": "passed",
                "target_geolocation_evaluation": "not_claimed",
            }
        )
    manifest_without_hash: dict[str, object] = {
        "schema": "multidetect.gazebo-bgr24-frame-set",
        "schema_version": (
            GAZEBO_BGR24_FRAME_SET_SCHEMA_VERSION if per_frame_telemetry_bound else 1
        ),
        "generated_at_utc": _utc_now(),
        "scenario_id": plan.scenario_id,
        "scenario_manifest_hash": plan.scenario_manifest_hash,
        "sensor_binding_hash": sensor_binding_hash,
        "wire_version": 3,
        "pixel_format": "BGR24",
        "frame_count": len(records),
        "total_payload_bytes": sum(int(record["bytes"]) for record in records),
        "frames": records,
        "source_attribution": source_attribution,
        "repository_model_assets": model_assets,
            # The rendered-data builder compares this binding with the pinned
            # repository mainline before a capture can enter OnboardAI training.
            # It is provenance only; the runtime still exposes no world truth.
            "mainline_asset_binding": {
                "world_sdf_sha256": plan.assets.world.sha256,
                "vehicle_model_sha256": plan.assets.model.sha256,
                "vehicle_config_sha256": plan.assets.model_config.sha256,
                "stack_assets_sha256": plan.assets.aggregate_sha256,
                "model_root_assets_sha256": plan.assets.model_root.aggregate_sha256,
            },
        "claims": claims,
    }
    if per_frame_telemetry_bound:
        nonzero_covariance_records = sum(
            any(
                abs(float(value)) > 0.0
                for value in record["provenance"]["wire_telemetry"]["attitude_covariance_enu_rad2"]
            )
            for record in records
        )
        manifest_without_hash["camera_calibration"] = camera_calibration
        manifest_without_hash["terrain_dem"] = terrain_dem
        manifest_without_hash["per_frame_sensor_evidence"] = {
            "schema_version": 1,
            "record_count": len(records),
            "wire_header_encoding": "lowercase_hex_zero_digest_field",
            "wire_record_hash_algorithm": "sha256(zero_digest_header || raw_bgr24)",
            "source_association": "nearest_source_at_or_before_image",
            "maximum_source_skew_ns": 1_500_000_000,
            "gps_and_pose_values_covered_by_wire_record_sha256": True,
            "derived_ned_values_recomputed_before_seal": True,
            "attitude_covariance_convention": "gazebo_pose_fixed_axis_xyz_enu_rad2",
            "attitude_covariance_record_count": len(records),
            "nonzero_attitude_covariance_record_count": nonzero_covariance_records,
            "derived_attitude_sigmas_persisted": True,
            "attitude_uncertainty_source": GAZEBO_SIM_ATTITUDE_UNCERTAINTY_SOURCE,
            "attitude_uncertainty_floor_deg": {
                "roll": GAZEBO_SIM_DEFAULT_MINIMUM_ROLL_SIGMA_DEG,
                "pitch": GAZEBO_SIM_DEFAULT_MINIMUM_PITCH_SIGMA_DEG,
                "heading": GAZEBO_SIM_DEFAULT_MINIMUM_HEADING_SIGMA_DEG,
            },
            "agl_source": GAZEBO_SIM_TERRAIN_AGL_REFERENCE,
        }
    from multidetect.backends.gazebo_sim import validate_frame_set_clock
    from multidetect.clock import validate_host_identity

    try:
        if host_clock_identity is not None:
            manifest_without_hash["captured_at_clock"] = "host_perf_counter"
            manifest_without_hash["host_clock_identity"] = validate_host_identity(
                host_clock_identity
            )
        validate_frame_set_clock(manifest_without_hash)
    except (ValueError, TypeError) as exc:
        raise DockerSimulatorAcceptanceError(
            f"Gazebo frame-set clock binding failed: {exc}"
        ) from exc
    manifest_sha256 = canonical_sha256(manifest_without_hash)
    manifest = {**manifest_without_hash, "manifest_sha256": manifest_sha256}
    manifest_path = output_directory / GAZEBO_BGR24_FRAME_SET_MANIFEST
    try:
        with manifest_path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise DockerSimulatorAcceptanceError(
            f"refusing to overwrite Gazebo frame-set manifest: {manifest_path}"
        ) from exc
    observed = json.loads(manifest_path.read_text(encoding="utf-8"))
    observed_claim = observed.pop("manifest_sha256", None)
    if observed_claim != manifest_sha256 or canonical_sha256(observed) != manifest_sha256:
        raise DockerSimulatorAcceptanceError("Gazebo frame-set manifest hash verification failed")
    sealed_files = {entry.name for entry in output_directory.iterdir()}
    if sealed_files != expected_files | {manifest_path.name}:
        raise DockerSimulatorAcceptanceError(
            "Gazebo frame-set directory changed while the manifest was sealed"
        )
    return {
        "path": str(manifest_path),
        "file": manifest_path.name,
        "bytes": manifest_path.stat().st_size,
        "file_sha256": _sha256(manifest_path),
        "manifest_sha256": manifest_sha256,
    }


def _write_depth_companion_manifest(
    *,
    output_directory: Path,
    plan: DockerSimulatorPlan,
    sensor_binding_hash: str,
    records: list[dict[str, object]],
    rgb_records: list[dict[str, object]] | None,
    rgb_manifest: dict[str, object] | None = None,
) -> dict[str, object]:
    """Seal persisted MDGZDEP1 payloads without changing the RGB manifest contract."""

    if rgb_records is None or rgb_manifest is None:
        raise DockerSimulatorAcceptanceError(
            "Gazebo depth manifest requires the paired RGB frame-set manifest"
        )
    if len(rgb_records) != len(records):
        raise DockerSimulatorAcceptanceError("Gazebo RGB/depth frame counts do not match")
    if not records:
        raise DockerSimulatorAcceptanceError("Gazebo depth manifest cannot contain zero frames")
    encodings = {record.get("encoding") for record in records}
    if len(encodings) != 1 or None in encodings:
        raise DockerSimulatorAcceptanceError("Gazebo depth manifest encodings are inconsistent")
    encoding = next(iter(encodings))
    pair_binding: list[dict[str, object]] = []
    for index, (depth_record, rgb_record) in enumerate(zip(records, rgb_records, strict=True)):
        rgb_provenance = rgb_record.get("provenance")
        if not isinstance(rgb_provenance, dict):
            raise DockerSimulatorAcceptanceError(
                f"Gazebo RGB provenance is missing for depth frame {index}"
            )
        expected = {
            "frame_id": rgb_record.get("frame_id"),
            "sequence": rgb_record.get("sequence"),
            "rgb_sensor_stamp_ns": rgb_provenance.get("sensor_stamp_ns"),
            "rgb_record_sha256": rgb_record.get("wire_record_sha256"),
        }
        observed = {
            key: depth_record.get(key)
            for key in ("frame_id", "sequence", "rgb_sensor_stamp_ns", "rgb_record_sha256")
        }
        if observed != expected:
            raise DockerSimulatorAcceptanceError(
                f"Gazebo RGB/depth pair binding mismatch at frame {index}"
            )
        pair_binding.append(
            {**observed, "depth_record_sha256": depth_record.get("wire_record_sha256")}
        )
    manifest_sha = rgb_manifest.get("manifest_sha256")
    manifest_file_sha = rgb_manifest.get("file_sha256")
    if (
        not isinstance(manifest_sha, str)
        or len(manifest_sha) != 64
        or any(character not in "0123456789abcdef" for character in manifest_sha)
        or not isinstance(manifest_file_sha, str)
        or len(manifest_file_sha) != 64
        or any(character not in "0123456789abcdef" for character in manifest_file_sha)
    ):
        raise DockerSimulatorAcceptanceError("Gazebo RGB manifest binding is incomplete")
    rgb_manifest_path_value = rgb_manifest.get("path")
    if not isinstance(rgb_manifest_path_value, str):
        raise DockerSimulatorAcceptanceError("Gazebo RGB manifest path binding is missing")
    rgb_manifest_path = Path(rgb_manifest_path_value).expanduser().resolve()
    if rgb_manifest_path.is_symlink() or not rgb_manifest_path.is_file():
        raise DockerSimulatorAcceptanceError("Gazebo RGB manifest file is missing")
    if _sha256(rgb_manifest_path) != manifest_file_sha:
        raise DockerSimulatorAcceptanceError("Gazebo RGB manifest file digest changed")
    try:
        rgb_manifest_document = json.loads(rgb_manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DockerSimulatorAcceptanceError("Gazebo RGB manifest is not valid JSON") from exc
    if not isinstance(rgb_manifest_document, dict):
        raise DockerSimulatorAcceptanceError("Gazebo RGB manifest must be an object")
    observed_rgb_manifest_sha = rgb_manifest_document.pop("manifest_sha256", None)
    if (
        observed_rgb_manifest_sha != manifest_sha
        or canonical_sha256(rgb_manifest_document) != manifest_sha
    ):
        raise DockerSimulatorAcceptanceError("Gazebo RGB manifest canonical digest changed")
    for record in records:
        _verify_persisted_gazebo_depth(output_directory, record)
    expected_files = {str(record["file"]) for record in records}
    observed_files = {entry.name for entry in output_directory.iterdir()}
    manifest_name = GAZEBO_DEPTH_FRAME_SET_MANIFEST
    if observed_files != expected_files:
        raise DockerSimulatorAcceptanceError(
            "Gazebo depth output directory contains an unexpected or missing entry"
        )
    manifest_without_hash: dict[str, object] = {
        "schema": "multidetect.gazebo-depth-companion-set",
        "schema_version": GAZEBO_DEPTH_FRAME_SET_SCHEMA_VERSION,
        "generated_at_utc": _utc_now(),
        "release_label": PRODUCT_VERSION.release_label,
        "scenario_id": plan.scenario_id,
        "scenario_manifest_hash": plan.scenario_manifest_hash,
        "sensor_binding_hash": sensor_binding_hash,
        "wire": "MDGZDEP1",
        "encoding": encoding,
        "frame_count": len(records),
        "total_payload_bytes": sum(int(record["bytes"]) for record in records),
        "frames": records,
        "source_attribution": {
            "depth": "actual_gazebo_depth_sensor_output",
            "environment": "software_simulation",
            "rgb_binding": "MDGZSIM3_record_sha256",
            "truth_loaded_by_runtime": False,
        },
        "claims": {
            "actual_gazebo_depth_persisted": True,
            "metric_depth_model_calibration": "not_claimed",
            "outdoor_calibration": "not_claimed",
            "hardware": "not_claimed",
            "hil": "not_claimed",
            "real": "not_claimed",
            "final_release": "not_claimed",
        },
        "rgb_frame_set_manifest": rgb_manifest,
        "rgb_depth_pair_binding_sha256": canonical_sha256(pair_binding),
        "rgb_depth_pair_binding": pair_binding,
    }
    manifest_sha256 = canonical_sha256(manifest_without_hash)
    manifest = {**manifest_without_hash, "manifest_sha256": manifest_sha256}
    manifest_path = output_directory / manifest_name
    try:
        with manifest_path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise DockerSimulatorAcceptanceError(
            f"refusing to overwrite existing Gazebo depth manifest: {manifest_path}"
        ) from exc
    observed = json.loads(manifest_path.read_text(encoding="utf-8"))
    observed_claim = observed.pop("manifest_sha256", None)
    if observed_claim != manifest_sha256 or canonical_sha256(observed) != manifest_sha256:
        raise DockerSimulatorAcceptanceError("Gazebo depth manifest hash verification failed")
    sealed_files = {entry.name for entry in output_directory.iterdir()}
    if sealed_files != expected_files | {manifest_path.name}:
        raise DockerSimulatorAcceptanceError(
            "Gazebo depth output directory changed while the manifest was sealed"
        )
    return {
        "path": str(manifest_path),
        "file": manifest_path.name,
        "bytes": manifest_path.stat().st_size,
        "file_sha256": _sha256(manifest_path),
        "manifest_sha256": manifest_sha256,
    }


def _capture_gazebo_sensor_transport(
    *,
    plan: DockerSimulatorPlan,
    manifest_path: Path,
    docker_executable: str,
    frame_count: int,
    expected_width: int,
    expected_height: int,
    timeout_s: float,
    frame_output_directory: Path | None = None,
    depth_output_directory: Path | None = None,
    clock=None,
    fault_source: str | None = None,
    fault_start_image: int = 0,
    fault_duration_images: int = 0,
    frame_callback: Callable[[CapturedFrame, GazeboSimFrameProvenance, object | None], None]
    | None = None,
) -> dict[str, object]:
    """Capture real Gazebo pixels/NavSat/odometry through production wire v2."""

    from multidetect.clock import HighResolutionSystemClock

    if clock is not None and not isinstance(clock, HighResolutionSystemClock):
        raise DockerSimulatorAcceptanceError("capture clock must be HighResolutionSystemClock")
    scenario_input = load_runtime_scenario(manifest_path, mode=RuntimeMode.SIM)
    terrain_dem = load_local_dem_grid(plan.dem_path)
    camera_calibration = _camera_calibration_provenance(
        plan=plan,
        scenario_input=scenario_input,
        expected_width=expected_width,
        expected_height=expected_height,
    )
    camera_calibration_profile = CameraCalibration(**camera_calibration["calibration"])
    terrain_dem_provenance = _terrain_dem_provenance(
        plan=plan,
        terrain_dem=terrain_dem,
    )
    stream_script = plan.scripts_root / "gazebo_sensor_stream.py"
    stream_script_sha256 = _sha256(stream_script)
    model_assets = _model_assets_provenance(
        plan.model_root,
        plan.model_name,
        repository_root=plan.repository_root,
    )
    model_assets_sha256 = str(model_assets["assets_sha256"])
    airframe_profile_sha256 = stable_airframe_profile_hash(
        {
            "schema_version": 1,
            "airframe_profile_id": plan.airframe_profile_id,
            "vehicle_model": plan.vehicle_model,
            "model_name": plan.model_name,
            "stack_plan_sha256": plan.stack_plan_sha256,
        }
    )
    backend = GazeboSimBackend(
        scenario_input=scenario_input,
        **({"clock": clock} if clock is not None else {}),
        camera_calibration=camera_calibration_profile,
        docker_executable=Path(docker_executable).resolve(),
        container_name=plan.container_name,
        frame_count=frame_count,
        airframe_profile_hash=airframe_profile_sha256,
        container_image_sha256=plan.image.rsplit("@sha256:", 1)[-1],
        stream_script_sha256=stream_script_sha256,
        model_assets_sha256=model_assets_sha256,
        terrain_dem=terrain_dem,
        model_instance=f"{plan.model_name}_0",
        world="default",
        timeout_s=timeout_s,
        expected_width=expected_width,
        expected_height=expected_height,
        fault_source=fault_source,
        fault_start_image=fault_start_image,
        fault_duration_images=fault_duration_images,
    )
    if frame_output_directory is not None:
        _prepare_frame_output_directory(frame_output_directory)
    if depth_output_directory is not None:
        if frame_output_directory is None:
            raise DockerSimulatorAcceptanceError(
                "depth output requires the paired RGB frame output directory"
            )
        _prepare_frame_output_directory(depth_output_directory)
    frames: list[dict[str, object]] = []
    persisted_records: list[dict[str, object]] = []
    persisted_depth_records: list[dict[str, object]] = []
    telemetry = None
    stats = None
    capture_error: Exception | None = None
    try:
        backend.frame_source.open()
        for _ in range(frame_count):
            frame = backend.frame_source.read()
            provenance = backend.frame_source.latest_frame_provenance
            if frame.captured_at_s != provenance.captured_at_s:
                raise DockerSimulatorAcceptanceError(
                    "Gazebo frame/provenance host capture time mismatch"
                )
            persisted_record = None
            if frame_output_directory is not None:
                persisted_record = _persist_gazebo_frame(
                    output_directory=frame_output_directory,
                    frame=frame,
                    provenance=provenance,
                )
                persisted_records.append(persisted_record)
            if depth_output_directory is not None:
                depth_frame = backend.frame_source.latest_depth_frame
                if depth_frame is None:
                    raise DockerSimulatorAcceptanceError(
                        "depth output was requested but no validated Gazebo depth "
                        "companion was bound"
                    )
                if (
                    depth_frame.width != expected_width
                    or depth_frame.height != expected_height
                    or depth_frame.stride_bytes != expected_width * 4
                    or depth_frame.encoding != "FLOAT32_M"
                ):
                    raise DockerSimulatorAcceptanceError(
                        "Gazebo depth geometry/encoding does not match the RGB contract"
                    )
                persisted_depth_records.append(
                    _persist_gazebo_depth_frame(
                        output_directory=depth_output_directory,
                        depth=depth_frame,
                        provenance=provenance,
                    )
                )
            if frame_callback is not None:
                frame_callback(frame, provenance, backend.frame_source.latest_depth_frame)
            frame_record = {
                **provenance.to_mapping(),
                "bgr_shape": list(frame.image_bgr.shape),
                "persisted": persisted_record is not None,
                "persisted_file": (
                    persisted_record["file"] if persisted_record is not None else None
                ),
                "persisted_bytes": (
                    persisted_record["bytes"] if persisted_record is not None else None
                ),
                "persisted_sha256": (
                    persisted_record["sha256"] if persisted_record is not None else None
                ),
                "raw_bytes_sha256_matches_wire_payload": (
                    persisted_record["raw_bytes_sha256_matches_wire_payload"]
                    if persisted_record is not None
                    else None
                ),
            }
            frames.append(frame_record)
        telemetry = backend.telemetry_provider.snapshot(now_s=backend.clock.monotonic())
        backend.frame_source.close()
        stats = backend.frame_source.require_complete()
    except Exception as exc:
        capture_error = exc
    finally:
        backend.close()
    if capture_error is not None:
        failed_stats = backend.frame_source.stats
        raise DockerSimulatorAcceptanceError(
            "Gazebo sensor transport failed: "
            f"{capture_error}; helper_exit={failed_stats.helper_exit_code}; "
            f"helper_stderr={failed_stats.stderr_tail!r}"
        ) from capture_error
    if telemetry is None or stats is None:  # pragma: no cover - guarded by require_complete
        raise DockerSimulatorAcceptanceError("Gazebo sensor transport completed without telemetry")
    camera_calibration["camera_info_intrinsics_semantic_validation"] = (
        stats.camera_info_intrinsics_semantic_validation
    )
    qualified = bool(
        stats.complete
        and stats.received_frame_count == frame_count
        and stats.odometry_timestamp_frame_count == frame_count
        and stats.odometry_required
        and stats.full_odometry_pose_velocity_fusion_claimed
        and stats.source_row_order == GAZEBO_SIM_SOURCE_ROW_ORDER
        and stats.wire_row_order == GAZEBO_SIM_WIRE_ROW_ORDER
        and stats.vertical_flip_applied is GAZEBO_SIM_VERTICAL_FLIP_APPLIED
        and stats.camera_info_intrinsics_semantic_validation
        == GAZEBO_SIM_CAMERA_INTRINSICS_SEMANTIC_VALIDATION
        and math.isfinite(telemetry.altitude_agl_m)
        and telemetry.altitude_agl_m > 0.0
        and telemetry.altitude_reference == GAZEBO_SIM_TERRAIN_AGL_REFERENCE
    )
    if not qualified:
        raise DockerSimulatorAcceptanceError(
            "Gazebo sensor transport did not satisfy wire-v3 gates"
        )
    fault_evidence = None
    if fault_source is not None:
        fault_evidence = _verified_sensor_fault_diagnostic(
            stats.stderr_tail,
            fault_source=fault_source,
            fault_start_image=fault_start_image,
            fault_duration_images=fault_duration_images,
        )
    if len(backend.frame_source.frame_provenance) != frame_count:
        raise DockerSimulatorAcceptanceError(
            "Gazebo sensor transport provenance count did not match the frame count"
        )
    if frame_output_directory is None:
        pixel_persistence: dict[str, object] = {
            "requested": False,
            "status": "disabled",
            "output_directory": None,
            "persisted_frame_count": 0,
            "total_payload_bytes": 0,
            "raw_bytes_sha256_matches_wire_payload": None,
            "manifest": None,
            "files": [],
            "actual_gazebo_pixels_persisted": False,
            "qgc_rendering": "not_claimed",
            "manual_visual_acceptance": "not_claimed",
            "hardware": "not_claimed",
        }
        rgb_manifest_record = None
    else:
        if len(persisted_records) != frame_count:
            raise DockerSimulatorAcceptanceError(
                "persisted Gazebo frame count did not match the requested frame count"
            )
        manifest_record = _write_frame_set_manifest(
            output_directory=frame_output_directory,
            plan=plan,
            sensor_binding_hash=backend.sensor_binding_hash,
            model_assets=model_assets,
            records=persisted_records,
            camera_calibration=camera_calibration,
            terrain_dem=terrain_dem_provenance,
            host_clock_identity=clock.identity() if clock is not None else None,
        )
        pixel_persistence = {
            "requested": True,
            "status": "passed",
            "output_directory": str(frame_output_directory),
            "persisted_frame_count": len(persisted_records),
            "total_payload_bytes": sum(int(record["bytes"]) for record in persisted_records),
            "raw_bytes_sha256_matches_wire_payload": all(
                record["raw_bytes_sha256_matches_wire_payload"] is True
                and record["sha256"] == record["wire_payload_sha256"]
                for record in persisted_records
            ),
            "manifest": manifest_record,
            "files": persisted_records,
            "actual_gazebo_pixels_persisted": True,
            "qgc_rendering": "not_claimed",
            "manual_visual_acceptance": "not_claimed",
            "hardware": "not_claimed",
        }
        rgb_manifest_record = manifest_record
    if depth_output_directory is None:
        depth_persistence: dict[str, object] = {
            "requested": False,
            "status": "disabled",
            "output_directory": None,
            "persisted_frame_count": 0,
            "total_payload_bytes": 0,
            "raw_bytes_sha256_matches_wire_payload": None,
            "manifest": None,
            "actual_gazebo_depth_persisted": False,
            "metric_depth_model_calibration": "not_claimed",
            "outdoor_calibration": "not_claimed",
            "hardware": "not_claimed",
            "hil": "not_claimed",
            "real": "not_claimed",
        }
    else:
        if len(persisted_depth_records) != frame_count:
            raise DockerSimulatorAcceptanceError(
                "persisted Gazebo depth count did not match the requested frame count"
            )
        depth_manifest_record = _write_depth_companion_manifest(
            output_directory=depth_output_directory,
            plan=plan,
            sensor_binding_hash=backend.sensor_binding_hash,
            records=persisted_depth_records,
            rgb_records=persisted_records,
            rgb_manifest=rgb_manifest_record,
        )
        depth_persistence = {
            "requested": True,
            "status": "passed",
            "output_directory": str(depth_output_directory),
            "persisted_frame_count": len(persisted_depth_records),
            "total_payload_bytes": sum(int(record["bytes"]) for record in persisted_depth_records),
            "raw_bytes_sha256_matches_wire_payload": all(
                record["raw_bytes_sha256_matches_wire_payload"] is True
                and record["sha256"] == record["wire_payload_sha256"]
                for record in persisted_depth_records
            ),
            "manifest": depth_manifest_record,
            "actual_gazebo_depth_persisted": True,
            "metric_depth_model_calibration": "not_claimed",
            "outdoor_calibration": "not_claimed",
            "hardware": "not_claimed",
            "hil": "not_claimed",
            "real": "not_claimed",
        }
    return {
        "status": "passed",
        "requested": True,
        "requested_frame_count": frame_count,
        "qualified": True,
        "wire_version": 3,
        "source_row_order": GAZEBO_SIM_SOURCE_ROW_ORDER,
        "wire_row_order": GAZEBO_SIM_WIRE_ROW_ORDER,
        "vertical_flip_applied": GAZEBO_SIM_VERTICAL_FLIP_APPLIED,
        "odometry_required": True,
        "full_odometry_pose_velocity_fusion_claimed": True,
        "truth_free": True,
        "fault_injection": {
            "enabled": fault_source is not None,
            "source": fault_source,
            "start_image": fault_start_image if fault_source is not None else None,
            "duration_images": fault_duration_images if fault_source is not None else 0,
            "window_key": "received_image_index",
            "recovery_observed": (
                fault_evidence["recovery_observed"] if fault_evidence is not None else None
            ),
            **({"diagnostic": fault_evidence} if fault_evidence is not None else {}),
        },
        "sensor_binding_hash": backend.sensor_binding_hash,
        "stream_script_sha256": stream_script_sha256,
        "model_assets_sha256": model_assets_sha256,
        "repository_model_assets": model_assets,
        "terrain_dem_sha256": terrain_dem.source_sha256,
        "camera_calibration": camera_calibration,
        "terrain_dem": terrain_dem_provenance,
        "terrain_agl_reference": GAZEBO_SIM_TERRAIN_AGL_REFERENCE,
        "odometry_local_up_used_as_agl": False,
        "airframe_profile_sha256": airframe_profile_sha256,
        "frames": frames,
        "pixel_persistence": pixel_persistence,
        "depth_persistence": depth_persistence,
        "transport": stats.to_mapping(),
        "latest_telemetry": {
            "latitude_deg": telemetry.latitude_deg,
            "longitude_deg": telemetry.longitude_deg,
            "gps_absolute_altitude_m": telemetry.gps_absolute_altitude_m,
            "altitude_agl_m": telemetry.altitude_agl_m,
            "local_north_m": telemetry.local_north_m,
            "local_east_m": telemetry.local_east_m,
            "local_down_m": telemetry.local_down_m,
            "roll_deg": telemetry.roll_deg,
            "pitch_deg": telemetry.pitch_deg,
            "heading_deg": telemetry.heading_deg,
            "attitude_roll_sigma_deg": telemetry.attitude_roll_sigma_deg,
            "attitude_pitch_sigma_deg": telemetry.attitude_pitch_sigma_deg,
            "attitude_heading_sigma_deg": telemetry.attitude_heading_sigma_deg,
            "attitude_uncertainty_source": telemetry.attitude_uncertainty_source,
            "velocity_north_mps": telemetry.velocity_north_mps,
            "velocity_east_mps": telemetry.velocity_east_mps,
            "gps_fix_type": telemetry.gps_fix_type,
            "satellites_visible": telemetry.satellites_visible,
            "gps_horizontal_accuracy_m": telemetry.gps_horizontal_accuracy_m,
            "gps_vertical_accuracy_m": telemetry.gps_vertical_accuracy_m,
            "altitude_reference": telemetry.altitude_reference,
        },
    }


def _docker_path(explicit: str | Path | None = None) -> str | None:
    """Resolve Docker without opening it (used by dry-run and tests)."""

    if explicit is not None:
        raw = os.fspath(explicit)
        if not isinstance(raw, str) or not raw.strip():
            return None
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            return str(candidate.resolve()) if candidate.is_file() else None
        # Keep a bare explicit token intact.  This is useful for callers that
        # inject a command runner (tests, CI wrappers, or a managed Docker
        # shim); when no runner is supplied ``subprocess.run`` will report the
        # ordinary executable-not-found error rather than silently converting
        # an execution request into a no-op.
        return shutil.which(raw) or raw
    found = shutil.which("docker")
    if found:
        return found
    windows = Path(r"C:\Program Files\Docker\Docker\resources\bin\docker.exe")
    return str(windows) if windows.is_file() else None


def _result_returncode(result: object) -> int:
    value = getattr(result, "returncode", 1)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


def _result_text(result: object, field: str) -> str:
    value = getattr(result, field, "")
    return value if isinstance(value, str) else str(value or "")


def _run(
    docker: str,
    arguments: Sequence[str],
    *,
    timeout_s: float = 30.0,
    check: bool = False,
    runner: Callable[..., object] | None = None,
) -> object:
    """Run one Docker CLI command with bounded output.

    ``runner`` is intentionally injectable so unit tests can exercise the
    complete lifecycle without starting Docker.  Its preferred signature is
    ``runner(argv, timeout_s=...)``; a small fallback accepts ``runner(argv)``.
    """

    argv = [docker, *[str(item) for item in arguments]]
    if runner is None:
        result: object = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    else:
        try:
            result = runner(argv, timeout_s=timeout_s)
        except TypeError:
            result = runner(argv)
    if check and _result_returncode(result) != 0:
        detail = (_result_text(result, "stderr") or _result_text(result, "stdout")).strip()
        short = " ".join(str(item) for item in arguments[:3])
        raise DockerSimulatorAcceptanceError(
            f"docker {short} failed ({_result_returncode(result)}): {detail}"
        )
    return result


def _json_object(result: object, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(_result_text(result, "stdout"))
    except json.JSONDecodeError as exc:
        raise DockerSimulatorAcceptanceError(f"{label} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise DockerSimulatorAcceptanceError(f"{label} returned a non-object")
    return payload


def _container_inspect(
    docker: str,
    name: str,
    *,
    runner: Callable[..., object] | None,
) -> dict[str, Any]:
    result = _run(
        docker,
        ["container", "inspect", name],
        timeout_s=10.0,
        runner=runner,
    )
    if _result_returncode(result) != 0:
        detail = (_result_text(result, "stderr") or _result_text(result, "stdout")).strip()
        raise DockerSimulatorAcceptanceError(
            f"container inspect failed for {name}: {detail or 'container not found'}"
        )
    try:
        records = json.loads(_result_text(result, "stdout"))
    except json.JSONDecodeError as exc:
        raise DockerSimulatorAcceptanceError("docker inspect returned invalid JSON") from exc
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise DockerSimulatorAcceptanceError("docker inspect returned an unexpected container set")
    return records[0]


def _norm_path(value: object) -> str:
    try:
        raw = os.fspath(value)
    except TypeError:
        return ""
    if not isinstance(raw, str) or not raw:
        return ""
    return os.path.normcase(os.path.abspath(os.path.normpath(raw)))


def _env_map(values: object) -> dict[str, str]:
    if not isinstance(values, (list, tuple)):
        return {}
    result: dict[str, str] = {}
    for item in values:
        if not isinstance(item, str) or "=" not in item:
            continue
        key, value = item.split("=", 1)
        result[key] = value
    return result


def _expected_mounts(plan: DockerSimulatorPlan) -> dict[str, tuple[str, bool]]:
    expected = {
        "/opt/multidetect-gazebo-models": (_norm_path(plan.model_root), True),
        "/opt/px4-gazebo/share/gz/worlds/default.sdf": (_norm_path(plan.world_path), True),
        "/workspace/scripts": (_norm_path(plan.scripts_root), True),
    }
    # The planner bind-mounts the resolved Fuel model cache (real-appearance
    # worlds) read-only when the checkout has one, and records it on the plan.
    fuel_root = getattr(plan, "fuel_resolved_root", None)
    if fuel_root is not None:
        expected["/opt/multidetect-gz-fuel-resolved"] = (_norm_path(fuel_root), True)
    return expected


def _validate_owned_container(
    record: dict[str, Any],
    plan: DockerSimulatorPlan,
) -> dict[str, bool]:
    """Validate every host/container boundary emitted by the planner."""

    config = record.get("Config") or {}
    host = record.get("HostConfig") or {}
    state = record.get("State") or {}
    labels = config.get("Labels") or {}
    env = _env_map(config.get("Env"))
    checks: dict[str, bool] = {
        "running": state.get("Running") is True,
        "name": record.get("Name") == f"/{plan.container_name}",
        "pinned_image": config.get("Image") == plan.image,
        "purpose_label": labels.get("multidetect.purpose") == "simulator-docker",
        "bridge_network": host.get("NetworkMode") in {
            plan.container_network_mode,
            "default" if plan.container_network_mode == "bridge" else plan.container_network_mode,
        },
        "not_privileged": host.get("Privileged") is not True,
    }

    requested = host.get("Mounts") or []
    runtime = record.get("Mounts") or []
    expected = _expected_mounts(plan)
    requested_by_target = {
        item.get("Target"): item
        for item in requested
        if isinstance(item, dict) and isinstance(item.get("Target"), str)
    }
    runtime_by_target = {
        item.get("Destination"): item
        for item in runtime
        if isinstance(item, dict) and isinstance(item.get("Destination"), str)
    }
    checks["exact_mount_count"] = len(requested) == len(expected) and len(runtime) == len(expected)
    for target, (source, readonly) in expected.items():
        requested_mount = requested_by_target.get(target) or {}
        runtime_mount = runtime_by_target.get(target) or {}
        key = target.rsplit("/", 1)[-1].replace(".", "_")
        checks[f"mount_{key}_source"] = (
            _norm_path(requested_mount.get("Source")) == source
            and _norm_path(runtime_mount.get("Source")) == source
        )
        checks[f"mount_{key}_readonly"] = requested_mount.get(
            "ReadOnly"
        ) is readonly and runtime_mount.get("RW") is (not readonly)

    bindings = host.get("PortBindings") or {}
    # Only the control leg is published into the container.  PX4 emits
    # telemetry to the host-facing ``host.docker.internal:<telemetry>`` target
    # from its own UDP socket; publishing that same port on Docker Desktop
    # creates a host-port hairpin and can swallow the outbound stream.  The
    # planner therefore treats telemetry as an outbound-only host target.
    expected_bindings = {
        f"{plan.host_control_port}/udp": [
            {"HostIp": "127.0.0.1", "HostPort": str(plan.host_control_port)}
        ],
    }
    # Docker can represent an explicitly loopback-bound port with an empty
    # HostIp on older Desktop versions.  Accept that representation only when
    # the published host port still exactly matches and record it separately.
    strict_bindings = bindings == expected_bindings
    legacy_loopback_bindings = {
        key: [{"HostIp": "", "HostPort": value[0]["HostPort"]}]
        for key, value in expected_bindings.items()
    }
    checks["loopback_port_bindings"] = strict_bindings or bindings == legacy_loopback_bindings
    checks["telemetry_port_not_published"] = f"{plan.host_telemetry_port}/udp" not in bindings
    checks["protected_port_untouched"] = f"{PROTECTED_PORT}/udp" not in bindings

    expected_env = dict(plan.environment)
    checks["planned_environment_present"] = all(
        env.get(key) == value for key, value in expected_env.items()
    )
    checks["control_port_environment"] = env.get("PX4_MAVLINK_UDP_PORT") == str(
        plan.host_control_port
    )
    checks["telemetry_port_environment"] = env.get("PX4_MAVLINK_TELEMETRY_PORT") == str(
        plan.host_telemetry_port
    )
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise DockerSimulatorAcceptanceError("owned Docker SIM checks failed: " + ", ".join(failed))
    return checks


def _message_type(message: object) -> str:
    getter = getattr(message, "get_type", None)
    if callable(getter):
        try:
            return str(getter())
        except Exception:
            return ""
    if isinstance(message, dict):
        value = message.get("type") or message.get("message_type")
        return str(value) if value is not None else ""
    return str(getattr(message, "_type", ""))


def _field(message: object, name: str, default: object = None) -> object:
    if isinstance(message, dict):
        return message.get(name, default)
    return getattr(message, name, default)


def _int_field(message: object, name: str, default: int = 0) -> int:
    try:
        return int(_field(message, name, default))
    except (TypeError, ValueError):
        return default


class DockerTelemetryProbe:
    """Passive MAVLink heartbeat/GPS observer with a hard deadline."""

    def __init__(
        self,
        endpoint: str,
        *,
        connection_factory: Callable[..., Any] | None = None,
        mavutil_module: object | None = None,
        source_system: int = GCS_SOURCE_SYSTEM,
        source_component: int = GCS_SOURCE_COMPONENT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.endpoint = endpoint
        self.connection_factory = connection_factory
        self.mavutil = _pymavlink_mavutil if mavutil_module is None else mavutil_module
        self.source_system = source_system
        self.source_component = source_component
        self.clock = clock
        self.messages: list[dict[str, object]] = []
        self.heartbeat: dict[str, object] | None = None
        self.gps: dict[str, object] | None = None
        self.position: dict[str, object] | None = None
        self.gcs_heartbeat_sent = 0

    def _connect(self) -> Any:
        if self.connection_factory is not None:
            try:
                return self.connection_factory(
                    self.endpoint,
                    source_system=self.source_system,
                    source_component=self.source_component,
                    autoreconnect=False,
                )
            except TypeError:
                return self.connection_factory(self.endpoint)
        module = self.mavutil
        if module is None or not hasattr(module, "mavlink_connection"):
            raise DockerSimulatorAcceptanceError(
                "pymavlink is unavailable; install the pixhawk extra for --execute"
            )
        return module.mavlink_connection(
            self.endpoint,
            source_system=self.source_system,
            source_component=self.source_component,
            autoreconnect=False,
        )

    def _send_heartbeat(self, connection: Any) -> None:
        mav = getattr(connection, "mav", None)
        sender = getattr(mav, "heartbeat_send", None)
        module = self.mavutil
        constants = getattr(module, "mavlink", None) if module is not None else None
        if not callable(sender) or constants is None:
            return
        try:
            sender(
                getattr(constants, "MAV_TYPE_GCS", 6),
                getattr(constants, "MAV_AUTOPILOT_INVALID", 8),
                0,
                0,
                0,
            )
            self.gcs_heartbeat_sent += 1
        except Exception:
            # A heartbeat is a liveness hint, not a gate claim.  Keep
            # receiving telemetry even if a fake/minimal connection lacks it.
            return

    def _record(self, message: object) -> None:
        kind = _message_type(message)
        if not kind:
            return
        row: dict[str, object] = {"type": kind}
        if kind == "HEARTBEAT":
            self.heartbeat = {
                "vehicle_type": _int_field(message, "type"),
                "autopilot": _int_field(message, "autopilot"),
                "base_mode": _int_field(message, "base_mode"),
                "custom_mode": _int_field(message, "custom_mode"),
                "system_status": _int_field(message, "system_status"),
            }
            row.update(self.heartbeat)
        elif kind == "GPS_RAW_INT":
            self.gps = {
                "fix_type": _int_field(message, "fix_type"),
                "satellites_visible": _int_field(message, "satellites_visible"),
                "lat": _int_field(message, "lat"),
                "lon": _int_field(message, "lon"),
                "alt_mm": _int_field(message, "alt"),
            }
            row.update(self.gps)
        elif kind == "GLOBAL_POSITION_INT":
            try:
                lat = _int_field(message, "lat") / 10_000_000.0
                lon = _int_field(message, "lon") / 10_000_000.0
                altitude = _int_field(message, "relative_alt") / 1000.0
                if math.isfinite(lat) and math.isfinite(lon) and math.isfinite(altitude):
                    self.position = {
                        "latitude_deg": lat,
                        "longitude_deg": lon,
                        "relative_altitude_m": altitude,
                        "heading_cdeg": _int_field(message, "hdg"),
                    }
                    row.update(self.position)
            except (TypeError, ValueError):
                pass
        if kind in {"HEARTBEAT", "GPS_RAW_INT", "GLOBAL_POSITION_INT"}:
            row["at_monotonic"] = self.clock()
            self.messages.append(row)

    def observe(self, timeout_s: float) -> dict[str, object]:
        if not math.isfinite(timeout_s) or timeout_s <= 0.0:
            raise DockerSimulatorAcceptanceError("telemetry timeout must be finite and positive")
        connection: Any | None = None
        started = self.clock()
        deadline = started + timeout_s
        error: str | None = None
        error_code: int | None = None
        try:
            connection = self._connect()
            next_heartbeat = started
            while self.clock() < deadline:
                now = self.clock()
                if now >= next_heartbeat:
                    self._send_heartbeat(connection)
                    next_heartbeat = now + 0.5
                remaining = max(0.01, min(0.25, deadline - now))
                receiver = getattr(connection, "recv_match", None)
                if not callable(receiver):
                    raise DockerSimulatorAcceptanceError("MAVLink connection has no recv_match")
                try:
                    message = receiver(blocking=True, timeout=remaining)
                except TypeError:
                    message = receiver(timeout=remaining)
                if message is not None:
                    self._record(message)
                if self.heartbeat is not None and self.gps is not None:
                    if (
                        int(self.gps.get("fix_type", 0)) >= 3
                        and int(self.gps.get("lat", 0)) != 0
                        and int(self.gps.get("lon", 0)) != 0
                    ):
                        # One qualified sample is enough for this transport
                        # liveness helper; position quality is evaluated by
                        # the full VTOL/mission gates.
                        break
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            error = str(exc)
            error_code = getattr(exc, "winerror", None)
            if error_code is None:
                error_code = getattr(exc, "errno", None)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
        gps = self.gps or {}
        qualified = bool(
            self.heartbeat is not None
            and int(gps.get("fix_type", 0)) >= 3
            and int(gps.get("lat", 0)) != 0
            and int(gps.get("lon", 0)) != 0
        )
        result: dict[str, object] = {
            "status": "passed" if qualified else "failed",
            "endpoint": self.endpoint,
            "requested_endpoint": self.endpoint,
            "bind_endpoint": self.endpoint,
            "loopback_publication": self.endpoint.startswith("udpin:127.0.0.1:"),
            "docker_bridge_publication": self.endpoint.startswith(
                f"udpin:{sim_listen_host()}:"
            ),
            "wildcard_bind_fallback": False,
            "heartbeat_observed": self.heartbeat is not None,
            "heartbeat": self.heartbeat,
            "gps_observed": self.gps is not None,
            "gps": self.gps,
            "position": self.position,
            "qualified": qualified,
            "message_count": len(self.messages),
            "gcs_heartbeat_sent": self.gcs_heartbeat_sent,
            "duration_s": round(max(0.0, self.clock() - started), 6),
            "truth_free": True,
        }
        if error_code is not None:
            result["error_code"] = error_code
        if error:
            result["error"] = error
        if not qualified and "error" not in result:
            result["error"] = "qualified PX4 heartbeat and GPS fix did not arrive before timeout"
        result["messages_tail"] = self.messages[-100:]
        return result


def _empty_runtime(*, status: str, plan: DockerSimulatorPlan) -> dict[str, object]:
    return {
        "status": status,
        "docker_invoked": False,
        "container_created": False,
        "container_running_observed": False,
        "container_id": None,
        "inspect_checks": {},
        "image": plan.image,
        "container_name": plan.container_name,
        "planned_cleanup_commands": [list(item) for item in plan.cleanup_commands],
        "cleanup": {
            "attempted": False,
            "stop_exit_code": None,
            "remove_exit_code": None,
            "errors": [],
        },
        "external_process_started": False,
        "external_io_opened": False,
    }


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def run_acceptance(
    manifest_path: str | Path = DEFAULT_SCENARIO,
    evidence_path: str | Path = DEFAULT_EVIDENCE,
    *,
    execute: bool = False,
    timeout_s: float = 90.0,
    docker: str | Path | None = None,
    container_name: str = DEFAULT_CONTAINER_NAME,
    control_port: int = DEFAULT_CONTROL_PORT,
    telemetry_port: int = DEFAULT_TELEMETRY_PORT,
    image: str | None = None,
    sensor_frame_count: int = 0,
    sensor_width: int = 1280,
    sensor_height: int = 960,
    sensor_frame_output_dir: str | Path | None = None,
    sensor_depth_output_dir: str | Path | None = None,
    sensor_fault_source: str | None = None,
    sensor_fault_start_image: int = 0,
    sensor_fault_duration_images: int = 0,
    runner: Callable[..., object] | None = None,
    telemetry_probe_factory: Callable[[str], DockerTelemetryProbe] | None = None,
    mission_transaction_runner: Callable[..., Mapping[str, object]] | None = None,
    sensor_frame_callback: Callable[[CapturedFrame, GazeboSimFrameProvenance, object | None], None]
    | None = None,
    sensor_capture_concurrent: bool = False,
    sensor_capture_start_event: threading.Event | None = None,
) -> dict[str, object]:
    """Run the Docker SIM gate, or emit a no-I/O preflight evidence record."""

    if isinstance(execute, bool) is False:
        raise DockerSimulatorAcceptanceError("execute must be bool")
    if not isinstance(sensor_capture_concurrent, bool):
        raise DockerSimulatorAcceptanceError("sensor_capture_concurrent must be bool")
    if sensor_capture_start_event is not None and not isinstance(
        sensor_capture_start_event, threading.Event
    ):
        raise DockerSimulatorAcceptanceError("sensor_capture_start_event must be a threading.Event")
    if sensor_capture_concurrent and sensor_frame_count <= 0:
        raise DockerSimulatorAcceptanceError(
            "concurrent sensor capture requires sensor_frame_count > 0"
        )
    if not math.isfinite(timeout_s) or not 1.0 <= timeout_s <= 3600.0:
        raise DockerSimulatorAcceptanceError("timeout_s must be finite in [1, 3600]")
    for value, name, minimum, maximum in (
        (sensor_frame_count, "sensor_frame_count", 0, 10_000),
        (sensor_width, "sensor_width", 1, 16_384),
        (sensor_height, "sensor_height", 1, 16_384),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise DockerSimulatorAcceptanceError(
                f"{name} must be an integer in [{minimum}, {maximum}]"
            )
    _validate_sensor_fault_window(
        frame_count=sensor_frame_count,
        fault_source=sensor_fault_source,
        fault_start_image=sensor_fault_start_image,
        fault_duration_images=sensor_fault_duration_images,
    )
    evidence = Path(evidence_path).expanduser().resolve()
    source = Path(manifest_path).expanduser().resolve()
    frame_output_directory = _resolve_frame_output_directory(
        sensor_frame_output_dir,
        evidence_path=evidence,
        frame_count=sensor_frame_count,
    )
    depth_output_directory = _resolve_depth_output_directory(
        sensor_depth_output_dir,
        evidence_path=evidence,
        frame_count=sensor_frame_count,
        frame_output_directory=frame_output_directory,
    )
    started = time.monotonic()
    plan = build_docker_simulator_plan(
        source,
        repository_root=ROOT,
        job_dir=evidence.parent / f"{container_name}-job",
        container_name=container_name,
        control_port=control_port,
        telemetry_port=telemetry_port,
        image=image,
        dry_run=not execute,
    )
    plan_mapping = plan.to_mapping_with_hash()
    validation = plan_mapping.get("validation")
    validation = validation if isinstance(validation, dict) else {}
    preflight_passed = bool(validation.get("gate_passed"))
    runtime = _empty_runtime(status="not_requested" if not execute else "not_started", plan=plan)
    telemetry: dict[str, object] = {
        "status": "not_requested" if not execute else "not_started",
        "endpoint": sim_udpin_endpoint(telemetry_port),
        "heartbeat_observed": False,
        "gps_observed": False,
        "qualified": False,
        "truth_free": True,
    }
    mission_transaction: dict[str, object] = {
        "status": "not_requested" if mission_transaction_runner is None else "not_started",
        "requested": mission_transaction_runner is not None,
        "gate_passed": None,
        "flight_started": False,
        "physical_output_enabled": False,
    }
    sensor_transport = _empty_sensor_transport(
        execute=execute,
        frame_count=sensor_frame_count,
        frame_output_directory=frame_output_directory,
        depth_output_directory=depth_output_directory,
        fault_source=sensor_fault_source,
        fault_start_image=sensor_fault_start_image,
        fault_duration_images=sensor_fault_duration_images,
    )
    failure: str | None = None
    container_started = False
    # The name is claimed only after the preflight inspect proves it is absent.
    # A failed `docker run` can still leave a Created container (for example
    # when network programming fails), so keep the claim separate from the
    # parsed container ID and clean that name in the finally-equivalent path.
    container_claimed = False
    docker_executable = _docker_path(docker)
    container_log = evidence.with_suffix(".container.log")

    if execute:
        if docker_executable is None:
            failure = "Docker Desktop executable not found"
            runtime["status"] = "failed"
        else:
            deadline = started + timeout_s
            try:
                existing = _run(
                    docker_executable,
                    ["container", "inspect", container_name],
                    timeout_s=min(10.0, max(1.0, deadline - time.monotonic())),
                    runner=runner,
                )
                if _result_returncode(existing) == 0:
                    raise DockerSimulatorAcceptanceError(
                        f"container already exists: {container_name}"
                    )
                from scripts.run_simulator_docker import freeze_runtime_plan
                plan = freeze_runtime_plan(plan, docker=docker_executable,
                    runner=lambda argv, **kw: _run(docker_executable, argv[1:], timeout_s=kw["timeout"], runner=runner))
                container_claimed = True
                image_check = _run(
                    docker_executable,
                    ["image", "inspect", plan.image, "--format", "{{.Id}}"],
                    timeout_s=min(20.0, max(1.0, deadline - time.monotonic())),
                    runner=runner,
                )
                image_pulled = False
                if _result_returncode(image_check) != 0:
                    pull = _run(
                        docker_executable,
                        ["pull", plan.image],
                        timeout_s=min(600.0, max(1.0, deadline - time.monotonic())),
                        check=True,
                        runner=runner,
                    )
                    image_pulled = _result_returncode(pull) == 0
                    image_check = _run(
                        docker_executable,
                        ["image", "inspect", plan.image, "--format", "{{.Id}}"],
                        timeout_s=min(20.0, max(1.0, deadline - time.monotonic())),
                        check=True,
                        runner=runner,
                    )
                image_id = _result_text(image_check, "stdout").strip().splitlines()[-1]
                runtime["image_id"] = image_id
                runtime["image_pulled"] = image_pulled
                command = [docker_executable, *plan.command[1:]]
                # Record the attempted command before invoking it.  This keeps
                # failure evidence truthful and ensures a partially-created
                # named container is reclaimed below.
                runtime.update({"docker_invoked": True, "command": command})
                container_started = container_claimed
                launch = _run(
                    docker_executable,
                    plan.command[1:],
                    timeout_s=min(60.0, max(1.0, deadline - time.monotonic())),
                    check=True,
                    runner=runner,
                )
                # A detached `docker run` has created the container even if a
                # malformed/fake CLI response omits its ID.  Mark it before
                # parsing stdout so the finally-equivalent cleanup below
                # still issues stop/rm using the owned name.
                # A detached `docker run` has now returned; retain the claim
                # even when stdout is malformed so cleanup still runs.
                container_started = True
                container_id = _result_text(launch, "stdout").strip().splitlines()[-1]
                if not container_id:
                    raise DockerSimulatorAcceptanceError("docker run returned no container ID")
                runtime.update(
                    {
                        "status": "started",
                        "container_created": True,
                        "container_id": container_id,
                    }
                )
                # Wait for a running state, not merely a successful `docker
                # run -d`; PX4 can exit immediately on an invalid world.
                inspect_record: dict[str, Any] | None = None
                while time.monotonic() < deadline:
                    inspect_record = _container_inspect(
                        docker_executable,
                        container_name,
                        runner=runner,
                    )
                    if (inspect_record.get("State") or {}).get("Running") is True:
                        break
                    time.sleep(0.25)
                if inspect_record is None or (
                    (inspect_record.get("State") or {}).get("Running") is not True
                ):
                    raise DockerSimulatorAcceptanceError(
                        "Docker SIM container did not remain running"
                    )
                checks = _validate_owned_container(inspect_record, plan)
                runtime["inspect_checks"] = checks
                runtime["container_running_observed"] = True
                runtime["external_process_started"] = True
                runtime["external_io_opened"] = True
                runtime["status"] = "running"
                # A normal sensor gate is intentionally batch-oriented.  The
                # autonomous actual-detector path opts into the concurrent
                # variant so the telemetry probe can open a start gate only
                # after the vehicle is armed and at the qualified altitude.
                # This prevents preflight RGB/Depth frames from being replayed
                # as if they were flight-time observations.
                if telemetry_probe_factory is not None:
                    probe = telemetry_probe_factory(sim_udpin_endpoint(telemetry_port))
                else:
                    probe = DockerTelemetryProbe(sim_udpin_endpoint(telemetry_port))
                sensor_thread: threading.Thread | None = None
                sensor_thread_error: list[str] = []

                def capture_sensor_transport() -> None:
                    nonlocal sensor_transport
                    if (
                        sensor_capture_start_event is not None
                        and not sensor_capture_start_event.wait(
                            max(1.0, deadline - time.monotonic())
                        )
                    ):
                        sensor_thread_error.append(
                            "sensor capture start gate was not opened by qualified live telemetry"
                        )
                        return
                    try:
                        sensor_transport = _capture_gazebo_sensor_transport(
                            plan=plan,
                            manifest_path=source,
                            docker_executable=docker_executable,
                            frame_count=sensor_frame_count,
                            expected_width=sensor_width,
                            expected_height=sensor_height,
                            timeout_s=max(1.0, deadline - time.monotonic()),
                            frame_output_directory=frame_output_directory,
                            depth_output_directory=depth_output_directory,
                            fault_source=sensor_fault_source,
                            fault_start_image=sensor_fault_start_image,
                            fault_duration_images=sensor_fault_duration_images,
                            frame_callback=sensor_frame_callback,
                        )
                    except (OSError, RuntimeError, TypeError, ValueError) as exc:
                        sensor_transport = {
                            **sensor_transport,
                            "status": "failed",
                            "qualified": False,
                            "error": str(exc),
                        }
                        sensor_thread_error.append(str(exc))

                if sensor_frame_count > 0 and sensor_capture_concurrent:
                    sensor_thread = threading.Thread(
                        target=capture_sensor_transport,
                        name=f"{container_name}-sensor-capture",
                        daemon=True,
                    )
                    sensor_thread.start()
                elif sensor_frame_count > 0:
                    capture_sensor_transport()
                    if sensor_thread_error:
                        raise DockerSimulatorAcceptanceError(sensor_thread_error[0])

                telemetry = probe.observe(max(1.0, deadline - time.monotonic()))
                if sensor_thread is not None:
                    sensor_thread.join(max(0.0, deadline - time.monotonic()))
                    if sensor_thread.is_alive():
                        sensor_thread_error.append(
                            "sensor capture worker did not stop before deadline"
                        )
                    if sensor_thread_error:
                        raise DockerSimulatorAcceptanceError(sensor_thread_error[0])
                if telemetry.get("qualified") is not True:
                    raise DockerSimulatorAcceptanceError(
                        str(telemetry.get("error") or "Docker SIM telemetry was not qualified")
                    )
                if mission_transaction_runner is not None:
                    # The hook runs only after the exact container contract and
                    # truth-free MAVLink telemetry have passed.  It receives
                    # the live inspect record and endpoint so the caller can
                    # construct ExternalRuntimeAttestation and execute the
                    # shared MISSION_ITEM_INT transaction on that same
                    # disposable runtime.  The hook is forbidden from
                    # silently turning this gate into an arm/takeoff path.
                    mission_transaction["status"] = "running"
                    transaction_result = mission_transaction_runner(
                        plan=plan,
                        inspect_record=inspect_record,
                        telemetry=telemetry,
                        control_endpoint=f"udp:127.0.0.1:{control_port}",
                        telemetry_endpoint=sim_udpin_endpoint(telemetry_port),
                        evidence_path=evidence,
                    )
                    if not isinstance(transaction_result, Mapping):
                        raise DockerSimulatorAcceptanceError(
                            "Mission transaction runner returned a non-object"
                        )
                    mission_transaction = {
                        **dict(transaction_result),
                        "status": "passed"
                        if transaction_result.get("gate_passed") is True
                        else "failed",
                        "requested": True,
                    }
                    if transaction_result.get("gate_passed") is not True:
                        raise DockerSimulatorAcceptanceError(
                            "external Mission transaction gate was not passed"
                        )
                    if transaction_result.get("flight_started") is not False:
                        raise DockerSimulatorAcceptanceError(
                            "Mission transaction runner must not start flight"
                        )
                    if transaction_result.get("physical_output_enabled") is not False:
                        raise DockerSimulatorAcceptanceError(
                            "Mission transaction runner enabled physical output"
                        )
                runtime["status"] = "passed"
            except Exception as exc:
                failure = str(exc)
                runtime["status"] = "failed"
                if telemetry.get("status") == "not_started":
                    telemetry["status"] = "not_run"
                if mission_transaction_runner is not None and mission_transaction.get("status") in {
                    "not_started",
                    "running",
                }:
                    if mission_transaction["status"] == "running":
                        mission_transaction["status"] = "failed"
                        mission_transaction["gate_passed"] = False
                        mission_transaction["error"] = failure
                    else:
                        mission_transaction["status"] = "not_run"

    cleanup_errors: list[str] = []
    if container_started and docker_executable is not None:
        runtime_cleanup = runtime.get("cleanup")
        cleanup = runtime_cleanup if isinstance(runtime_cleanup, dict) else {}
        cleanup["attempted"] = True
        try:
            logs = _run(
                docker_executable,
                ["logs", "--tail", "1000", container_name],
                timeout_s=30.0,
                runner=runner,
            )
            log_text = _result_text(logs, "stdout") + _result_text(logs, "stderr")
            if len(log_text) > 2_000_000:
                log_text = log_text[-2_000_000:]
            container_log.parent.mkdir(parents=True, exist_ok=True)
            container_log.write_text(log_text, encoding="utf-8")
            runtime["container_log"] = str(container_log)
        except Exception as exc:  # preserve cleanup and evidence even on logs failure
            cleanup_errors.append(f"logs: {exc}")
        try:
            stop = _run(
                docker_executable,
                ["stop", "--timeout", "2", container_name],
                timeout_s=20.0,
                runner=runner,
            )
            cleanup["stop_exit_code"] = _result_returncode(stop)
            if _result_returncode(stop) != 0:
                cleanup_errors.append("docker stop returned non-zero")
        except Exception as exc:
            cleanup_errors.append(f"stop: {exc}")
        try:
            remove = _run(
                docker_executable,
                ["rm", container_name],
                timeout_s=20.0,
                runner=runner,
            )
            cleanup["remove_exit_code"] = _result_returncode(remove)
            if _result_returncode(remove) != 0:
                cleanup_errors.append("docker rm returned non-zero")
        except Exception as exc:
            cleanup_errors.append(f"rm: {exc}")
        cleanup["errors"] = cleanup_errors
        cleanup["removed"] = not cleanup_errors
        runtime["cleanup"] = cleanup
    elif execute:
        cleanup = runtime.get("cleanup")
        if isinstance(cleanup, dict):
            cleanup["errors"] = cleanup_errors
            runtime["cleanup"] = cleanup

    cleanup_record = runtime.get("cleanup")
    cleanup_passed = (
        not execute
        or not container_started
        or (isinstance(cleanup_record, dict) and cleanup_record.get("removed") is True)
    )
    depth_persistence_record = sensor_transport.get("depth_persistence")
    depth_gate_passed = (
        depth_output_directory is None
        or not execute
        or (
            isinstance(depth_persistence_record, dict)
            and depth_persistence_record.get("status") == "passed"
            and depth_persistence_record.get("actual_gazebo_depth_persisted") is True
        )
    )
    sensor_gate_passed = (
        sensor_frame_count == 0 or sensor_transport.get("qualified") is True
    ) and depth_gate_passed
    mission_gate_passed = (
        mission_transaction_runner is None
        or mission_transaction.get("status") == "passed"
        and mission_transaction.get("gate_passed") is True
    )
    runtime_gate_passed = (not execute) or (
        failure is None
        and runtime.get("status") == "passed"
        and sensor_gate_passed
        and mission_gate_passed
        and cleanup_passed
    )
    payload: dict[str, object] = {
        "schema": "multidetect.simulator-docker-acceptance",
        "schema_version": 1,
        "generated_at_utc": _utc_now(),
        "gate_passed": bool(preflight_passed and runtime_gate_passed),
        "preflight_gate_passed": preflight_passed,
        "runtime_gate_passed": runtime_gate_passed if execute else None,
        "sensor_transport_gate_passed": sensor_gate_passed if execute else None,
        "execution_requested": execute,
        "failure": failure,
        "release_status": RELEASE_STATUS,
        "scenario": {
            "manifest": str(source),
            "manifest_sha256": _sha256(source) if source.is_file() else None,
            "scenario_id": plan.scenario_id,
            "scenario_manifest_hash": plan.scenario_manifest_hash,
            "vehicle_model": plan.vehicle_model,
            "model_name": plan.model_name,
            "airframe_profile_id": plan.airframe_profile_id,
            "seed": dict(plan.environment).get("MD_SIM_SEED"),
            "truth_loaded_by_runtime": False,
            "expectations_loaded_by_runtime": False,
        },
        "plan": plan_mapping,
        "docker": runtime,
        "container_runtime": runtime,
        "telemetry": telemetry,
        "mission_transaction": mission_transaction,
        "mission_transaction_gate_passed": mission_gate_passed if execute else None,
        "sensor_transport": sensor_transport,
        "sensor_capture": {
            "concurrent": sensor_capture_concurrent,
            "start_gate_requested": sensor_capture_start_event is not None,
            "start_gate_opened": (
                sensor_capture_start_event.is_set()
                if sensor_capture_start_event is not None
                else None
            ),
            "flight_time_capture_contract": sensor_capture_concurrent,
        },
        "pixel_persistence": sensor_transport.get("pixel_persistence"),
        "depth_persistence": depth_persistence_record,
        "visual": {
            "status": "not_claimed",
            "qgc_fly_view_visual_acceptance": "not_claimed",
            "desktop_gpu_rendering": "not_claimed",
        },
        "hardware": {
            "status": "not_claimed",
            "real_pixhawk_contacted": False,
            "real_camera_contacted": False,
            "jetson_contacted": False,
            "hardware_control_enabled": False,
        },
        "safety": {
            "flight_control_output_enabled": False,
            "flight_control_commands_sent": 0,
            "gcs_heartbeat_only": mission_transaction_runner is None,
            "real_pixhawk_contacted": False,
            "hardware_control_enabled": False,
            "truth_free": True,
        },
        "claims": {
            "container_runtime": (
                "passed" if runtime.get("status") == "passed" else runtime.get("status")
            ),
            "telemetry": "passed" if telemetry.get("qualified") else telemetry.get("status"),
            "mission_transaction": (
                "passed"
                if mission_transaction.get("status") == "passed"
                else mission_transaction.get("status")
            ),
            "sensor_transport": (
                "passed" if sensor_transport.get("qualified") else sensor_transport.get("status")
            ),
            "visual": "not_claimed",
            "hardware": "not_claimed",
            "external_process_acceptance": (
                "passed"
                if runtime.get("status") == "passed"
                else "not_claimed"
                if not execute
                else "failed"
            ),
        },
        "duration_s": round(max(0.0, time.monotonic() - started), 6),
    }
    _write_json(evidence, payload)
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario-manifest", "--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--evidence", "--out", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--container-name", default=DEFAULT_CONTAINER_NAME)
    parser.add_argument("--control-port", type=int, default=DEFAULT_CONTROL_PORT)
    parser.add_argument("--telemetry-port", type=int, default=DEFAULT_TELEMETRY_PORT)
    parser.add_argument("--image", default=None)
    parser.add_argument(
        "--sensor-frames",
        type=int,
        default=0,
        help="capture this many real camera/NavSat/full-odometry/covariance wire-v3 frames",
    )
    parser.add_argument("--sensor-width", type=int, default=1280)
    parser.add_argument("--sensor-height", type=int, default=960)
    parser.add_argument(
        "--sensor-frame-output-dir",
        type=Path,
        help=(
            "explicit empty directory for exact raw BGR24 frames and a hash-bound manifest; "
            "disabled by default"
        ),
    )
    parser.add_argument(
        "--sensor-depth-output-dir",
        type=Path,
        help=(
            "explicit empty sibling directory for exact raw FLOAT32_M Gazebo depth payloads "
            "and a hash-bound MDGZDEP1 companion manifest; requires --sensor-frame-output-dir"
        ),
    )
    parser.add_argument(
        "--sensor-fault-source",
        choices=GAZEBO_SENSOR_FAULT_SOURCES,
        help="drop one live Gazebo sensor source for a bounded recovery exercise",
    )
    parser.add_argument("--sensor-fault-start-image", type=int, default=0)
    parser.add_argument("--sensor-fault-duration-images", type=int, default=0)
    parser.add_argument("--docker", type=Path)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="actually start the pinned container, observe telemetry, and clean it up",
    )
    parser.add_argument(
        "--timeout",
        "--timeout-seconds",
        dest="timeout_s",
        type=float,
        default=90.0,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = run_acceptance(
            args.scenario_manifest,
            args.evidence,
            execute=args.execute,
            timeout_s=args.timeout_s,
            docker=args.docker,
            container_name=args.container_name,
            control_port=args.control_port,
            telemetry_port=args.telemetry_port,
            image=args.image,
            sensor_frame_count=args.sensor_frames,
            sensor_width=args.sensor_width,
            sensor_height=args.sensor_height,
            sensor_frame_output_dir=args.sensor_frame_output_dir,
            sensor_depth_output_dir=args.sensor_depth_output_dir,
            sensor_fault_source=args.sensor_fault_source,
            sensor_fault_start_image=args.sensor_fault_start_image,
            sensor_fault_duration_images=args.sensor_fault_duration_images,
        )
    except (
        DockerSimulatorAcceptanceError,
        DockerSimulatorPlanError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"simulator docker-acceptance: {exc}", file=os.sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("gate_passed") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DEFAULT_EVIDENCE",
    "DEFAULT_SCENARIO",
    "DockerSimulatorAcceptanceError",
    "DockerSimulatorPlanError",
    "DockerTelemetryProbe",
    "GAZEBO_BGR24_FRAME_SET_MANIFEST",
    "GCS_SOURCE_COMPONENT",
    "GCS_SOURCE_SYSTEM",
    "PINNED_IMAGE",
    "SimulatorDockerAcceptanceError",
    "_container_inspect",
    "_docker_path",
    "_run",
    "_validate_owned_container",
    "main",
    "run_acceptance",
]
