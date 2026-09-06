#!/usr/bin/env python3
"""Execute one hash-bound PX4 VTOL mission on the owned combined-SIM endpoint.

This adapter is intentionally narrower than a general mission uploader:

* it attaches to an already-running, exact-ID Docker container owned by the
  combined acceptance orchestrator and never creates, stops, or removes it;
* it loads only the scenario runtime channel and derives home from qualified
  live ``GPS_RAW_INT`` telemetry (truth/expectation channels are never loaded);
* it accepts only the MAVLink 2 ``MISSION_REQUEST_INT`` / ``MISSION_ITEM_INT``
  protocol, verifies an exact INT readback hash, commits the transaction, and
  obtains ``permit_start`` before changing mode or arming;
* the only selectable mode is the exact PX4 ``AUTO.MISSION`` custom mode; and
* every claim remains SIM-only.  A machine-created grant fixture is explicitly
  not a human approval and visual operator acceptance remains unclaimed.

The existing mission-active ready file is written atomically only after
readback, commit, permit, current ``AUTO.MISSION`` heartbeat confirmation,
post-permit ARM, an in-air state, and bounded mission progress.  Its semantics
remain intentionally unchanged.  A separate optional flight-capture ready file
adds observed fixed-wing flight, at least 35 m of horizontal displacement, and
at least 10 m of relative altitude.  The stricter barrier gives a headless
sensor consumer a race-free, transaction-bound point for airborne capture and
cannot be satisfied by a vehicle that was merely armed on the ground.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from vtol_sitl_scenario_acceptance import (
    GCS_SOURCE_COMPONENT,
    GCS_SOURCE_SYSTEM,
    PINNED_IMAGE,
    TelemetryCollector,
    _container_inspect,
    _docker_path,
    _haversine_m,
    _run,
    _utc_now,
    mavutil,
)

from multidetect.autonomy import (
    ContextSnapshot,
    GeoPoint,
    OperatorGrant,
    PlanAction,
    PlanStep,
    PlatformNeutralPlan,
    PromptEnvelope,
    ResolvedTaskSpec,
    TaskConstraints,
    TaskGeometry,
    TaskType,
    ValidationFinding,
    ValidationReport,
    ValidationVerdict,
)
from multidetect.autonomy.serialization import canonical_sha256
from multidetect.mission_journal import (
    DurableMissionJournal,
    PersistentPx4MissionTransaction,
)
from multidetect.product_version import PRODUCT_VERSION
from multidetect.px4_mission import (
    MAV_MISSION_TYPE_MISSION,
    MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
    MAV_VTOL_STATE_FW,
    MAV_VTOL_STATE_MC,
    PX4_AUTO_MISSION_CUSTOM_MODE,
    PX4_AUTOPILOT_COMPONENT_ID,
    PX4_SYSTEM_ID,
    MissionTransactionBindings,
    MissionTransactionState,
    MissionUploadPlan,
    Px4MissionItemInt,
    Px4MissionSafetyPolicy,
    Px4MissionTransaction,
    VtolMissionGeometry,
    compile_executable_mission,
    normalize_px4_vehicle_class,
    px4_wire_hash,
)
from multidetect.runtime import RuntimeMode
from multidetect.simulation import load_runtime_scenario
from multidetect.simulator.stack import resolve_simulator_assets

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "configs" / "scenarios" / "river_rescue_vtol_001.scenario.json"
EXPECTED_CONTAINER_LABEL = "combined-px4-gazebo-qgc-sim-acceptance"
PROTECTED_GROUND_STATION_PORT = 14550
DEFAULT_MISSION_TIMEOUT_S = 240.0
MAXIMUM_MISSION_TIMEOUT_S = 600.0
MAXIMUM_FLIGHT_STATE_AGE_S = 1.5
RELEASE_STATUS = (
    f"v{PRODUCT_VERSION.release_label}; final v{PRODUCT_VERSION.numeric} remains blocked"
)
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_DOCKER_ID_RE = re.compile(r"^[0-9a-f]{64}$")


class VtolMissionAcceptanceError(RuntimeError):
    """A fail-closed owned-SITL mission acceptance invariant failed."""


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_sha256(value: str, name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise VtolMissionAcceptanceError(f"{name} must be exactly 64 hexadecimal characters")
    return value.lower()


def _sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_write_json(path: Path, payload: Mapping[str, Any], *, overwrite: bool) -> None:
    """Write durable same-directory JSON and never expose a partial ready file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise VtolMissionAcceptanceError(f"refusing to overwrite stale ready file: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            temporary.replace(path)
        else:
            try:
                # A same-directory hard link atomically publishes the already
                # flushed inode while retaining no-clobber semantics.  Unlike
                # check-then-replace, a competing stale writer cannot be
                # silently overwritten between the existence check and publish.
                os.link(temporary, path)
            except FileExistsError as exc:
                raise VtolMissionAcceptanceError(
                    f"refusing to overwrite stale ready file: {path}"
                ) from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def _validate_ready_file_targets(
    mission_active_ready_file: Path | None,
    flight_capture_ready_file: Path | None,
    flight_capture_complete_file: Path | None = None,
) -> None:
    """Reject stale or aliased synchronization targets before flight work starts."""

    targets = {
        "mission-active ready": mission_active_ready_file,
        "flight-capture ready": flight_capture_ready_file,
        "flight-capture complete": flight_capture_complete_file,
    }
    for label, target in targets.items():
        if target is not None and target.exists():
            raise VtolMissionAcceptanceError(f"stale {label} file already exists: {target}")
    selected = [target for target in targets.values() if target is not None]
    if len(set(selected)) != len(selected):
        raise VtolMissionAcceptanceError(
            "mission-active, flight-capture ready, and flight-capture complete files "
            "must be distinct"
        )
    if (flight_capture_ready_file is None) != (flight_capture_complete_file is None):
        raise VtolMissionAcceptanceError(
            "flight-capture ready and complete files must be supplied together"
        )


def _flight_capture_ready_gate(
    *,
    transaction_state: str,
    mission_progress_observed: bool,
    auto_mission_active: bool,
    armed: bool,
    in_air_at_ready: bool,
    fixed_wing_observed: bool,
    current_vtol_state: int | None,
    maximum_distance_m: float,
    maximum_altitude_m: float,
    telemetry_fresh: bool,
) -> bool:
    """Return true only for the strict airborne sensor-capture barrier."""

    return bool(
        transaction_state == MissionTransactionState.START_PERMITTED.value
        and mission_progress_observed
        and auto_mission_active
        and armed
        and in_air_at_ready
        and fixed_wing_observed
        and current_vtol_state == MAV_VTOL_STATE_FW
        and maximum_distance_m >= 35.0
        and maximum_altitude_m >= 10.0
        and telemetry_fresh
    )


def _observation_age_s(now_monotonic_s: float, observed_at_monotonic_s: object) -> float | None:
    if isinstance(observed_at_monotonic_s, bool):
        return None
    try:
        observed = float(observed_at_monotonic_s)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    age = now_monotonic_s - observed
    if not math.isfinite(now_monotonic_s) or not math.isfinite(observed) or age < 0.0:
        return None
    return age


def validate_flight_capture_complete_marker(
    payload: Mapping[str, Any],
    *,
    flight_ready_monotonic_s: float,
    container_id: str,
    transaction_id: str,
    committed_protocol_sha256: str,
    scenario_id: str,
    scenario_manifest_hash: str,
) -> dict[str, bool]:
    """Bind the parent capture completion marker to this live mission interval."""

    try:
        started = float(payload.get("capture_started_at_monotonic_s"))
        completed = float(payload.get("capture_completed_at_monotonic_s"))
    except (TypeError, ValueError, OverflowError):
        started = math.nan
        completed = math.nan
    checks = {
        "schema": payload.get("schema") == "multidetect.vtol-sensor-capture-complete",
        "schema_version": payload.get("schema_version") == 1,
        "host_monotonic_clock": payload.get("clock") == "host_monotonic"
        and math.isfinite(flight_ready_monotonic_s)
        and flight_ready_monotonic_s > 0.0
        and math.isfinite(started)
        and math.isfinite(completed),
        "capture_interval": flight_ready_monotonic_s <= started <= completed,
        "exact_container_id": _DOCKER_ID_RE.fullmatch(container_id) is not None
        and payload.get("container_id") == container_id,
        "transaction_id": bool(transaction_id) and payload.get("transaction_id") == transaction_id,
        "committed_protocol_sha256": payload.get("committed_protocol_sha256")
        == _require_sha256(committed_protocol_sha256, "committed_protocol_sha256"),
        "scenario_binding": payload.get("scenario_id") == scenario_id
        and payload.get("scenario_manifest_hash")
        == _require_sha256(scenario_manifest_hash, "scenario_manifest_hash"),
        "frame_set_binding": isinstance(payload.get("frame_count"), int)
        and not isinstance(payload.get("frame_count"), bool)
        and int(payload.get("frame_count")) > 0
        and _SHA256_RE.fullmatch(str(payload.get("sensor_binding_hash"))) is not None
        and _SHA256_RE.fullmatch(str(payload.get("frame_set_manifest_sha256"))) is not None,
        "truth_free": payload.get("ground_truth_used_for_runtime_or_capture") is False,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise VtolMissionAcceptanceError(
            "flight-capture complete marker checks failed: " + ", ".join(failed)
        )
    return checks


def validate_owned_container_record(
    record: Mapping[str, Any],
    *,
    container_name: str,
    expected_container_id: str,
    expected_label: str,
) -> dict[str, bool]:
    """Validate the exact combined-SIM container without trusting its name alone."""

    if _DOCKER_ID_RE.fullmatch(expected_container_id) is None:
        raise VtolMissionAcceptanceError(
            "expected_container_id must be the exact 64-character lowercase Docker ID"
        )
    config = record.get("Config") or {}
    host = record.get("HostConfig") or {}
    state = record.get("State") or {}
    labels = config.get("Labels") or {}
    bindings = host.get("PortBindings") or {}
    environment = tuple(str(item) for item in (config.get("Env") or ()))
    command_text = " ".join(str(item) for item in (config.get("Cmd") or ()))

    host_ports: list[int] = []
    loopback_bindings = True
    for container_port, rows in bindings.items():
        if str(container_port).split("/", maxsplit=1)[0] == str(PROTECTED_GROUND_STATION_PORT):
            loopback_bindings = False
        for row in rows or ():
            host_ip = str((row or {}).get("HostIp") or "")
            host_port = str((row or {}).get("HostPort") or "")
            if host_ip != "127.0.0.1" or not host_port.isdecimal():
                loopback_bindings = False
                continue
            host_ports.append(int(host_port))

    checks = {
        "exact_container_id": str(record.get("Id") or "") == expected_container_id,
        "exact_container_name": str(record.get("Name") or "") == f"/{container_name}",
        "exact_owned_label": labels.get("multidetect.purpose") == expected_label,
        "running": state.get("Running") is True,
        "pinned_image": config.get("Image") == PINNED_IMAGE,
        "not_privileged": host.get("Privileged") is False,
        "bridge_network": host.get("NetworkMode") in {"bridge", "default"},
        "no_devices": not host.get("Devices"),
        "all_capabilities_dropped": "ALL" in tuple(host.get("CapDrop") or ()),
        "no_new_privileges": "no-new-privileges:true" in tuple(host.get("SecurityOpt") or ()),
        "loopback_only_host_bindings": bool(bindings) and loopback_bindings,
        "protected_udp_14550_not_bound": PROTECTED_GROUND_STATION_PORT not in host_ports,
        "protected_udp_14550_not_targeted": "14550" not in command_text,
        "expected_vtol_model": "PX4_SIM_MODEL=gz_standard_vtol_mono_cam" in environment,
        "expected_vtol_autostart": "PX4_SYS_AUTOSTART=4004" in environment,
    }
    failed = tuple(name for name, passed in checks.items() if not passed)
    if failed:
        raise VtolMissionAcceptanceError(
            "owned combined-SIM container checks failed: " + ", ".join(failed)
        )
    return checks


def _source_identity(message: Any) -> tuple[int, int]:
    try:
        return int(message.get_srcSystem()), int(message.get_srcComponent())
    except (AttributeError, TypeError, ValueError) as exc:
        raise VtolMissionAcceptanceError("MAVLink message has no source identity") from exc


def _require_px4_source(message: Any, what: str) -> tuple[int, int]:
    source = _source_identity(message)
    if source != (PX4_SYSTEM_ID, PX4_AUTOPILOT_COMPONENT_ID):
        raise VtolMissionAcceptanceError(
            f"{what} source identity mismatch: observed {source[0]}/{source[1]}"
        )
    return source


def _mission_type(message: Any) -> int:
    return int(getattr(message, "mission_type", MAV_MISSION_TYPE_MISSION))


def _targets_evaluator(message: Any) -> bool:
    try:
        target = int(message.target_system), int(message.target_component)
    except (AttributeError, TypeError, ValueError) as exc:
        raise VtolMissionAcceptanceError(
            f"{message.get_type()} has no MAVLink target identity"
        ) from exc
    return target == (GCS_SOURCE_SYSTEM, GCS_SOURCE_COMPONENT)


def upload_int_only_mission(
    connection: Any,
    transaction: Px4MissionTransaction,
    upload_plan: MissionUploadPlan,
    *,
    message_timeout_s: float = 1.5,
    maximum_duration_s: float = 30.0,
    maximum_idle_retries: int = 6,
) -> dict[str, Any]:
    """Run ``MISSION_COUNT -> REQUEST_INT -> ITEM_INT -> ACK`` exactly once."""

    connection.mav.mission_count_send(*upload_plan.count_send_args())
    requested_sequences: list[int] = []
    request_types: list[str] = []
    transmitted_item_count = 0
    idle_retries = 0
    total_idle_retries = 0
    ignored_foreign_target_messages = 0
    deadline = time.monotonic() + maximum_duration_s
    while time.monotonic() < deadline:
        message = connection.recv_match(
            type=["MISSION_REQUEST_INT", "MISSION_REQUEST", "MISSION_ACK"],
            blocking=True,
            timeout=message_timeout_s,
        )
        if message is None:
            idle_retries += 1
            total_idle_retries += 1
            if idle_retries > maximum_idle_retries:
                raise VtolMissionAcceptanceError(
                    "MISSION upload timed out after bounded idle retries"
                )
            connection.mav.mission_count_send(*upload_plan.count_send_args())
            continue
        idle_retries = 0
        message_type = str(message.get_type())
        source_system, source_component = _require_px4_source(message, message_type)
        if not _targets_evaluator(message):
            ignored_foreign_target_messages += 1
            continue
        observed_mission_type = _mission_type(message)
        if observed_mission_type != MAV_MISSION_TYPE_MISSION:
            raise VtolMissionAcceptanceError(
                f"{message_type} carried non-primary mission_type={observed_mission_type}"
            )
        if message_type in {"MISSION_REQUEST_INT", "MISSION_REQUEST"}:
            sequence = int(message.seq)
            # The transaction itself enters DENIED for a legacy request.
            item = transaction.mission_item_for_request(sequence, request_message_type=message_type)
            connection.mav.mission_item_int_send(*item.to_send_args())
            requested_sequences.append(sequence)
            request_types.append(message_type)
            transmitted_item_count += 1
            continue
        if message_type == "MISSION_ACK":
            acknowledgement = int(message.type)
            transaction.accept_upload_ack(
                ack_type=acknowledgement,
                mission_type=observed_mission_type,
                source_system=source_system,
                source_component=source_component,
            )
            return {
                "acknowledged": True,
                "acknowledgement_code": acknowledgement,
                "request_sequences": requested_sequences,
                "request_message_types": request_types,
                "transmitted_item_count": transmitted_item_count,
                "idle_retry_count": total_idle_retries,
                "ignored_foreign_target_messages": ignored_foreign_target_messages,
                "int_only": all(item == "MISSION_REQUEST_INT" for item in request_types),
            }
    raise VtolMissionAcceptanceError("MISSION upload exceeded its maximum duration")


def _wire_item_from_int_message(message: Any) -> Px4MissionItemInt:
    if str(message.get_type()) != "MISSION_ITEM_INT":
        raise VtolMissionAcceptanceError(
            "legacy MISSION_ITEM readback is forbidden; MISSION_ITEM_INT is required"
        )
    _require_px4_source(message, "MISSION_ITEM_INT")
    if not _targets_evaluator(message):
        raise VtolMissionAcceptanceError("MISSION_ITEM_INT target identity mismatch")
    return Px4MissionItemInt(
        target_system=PX4_SYSTEM_ID,
        target_component=PX4_AUTOPILOT_COMPONENT_ID,
        sequence=int(message.seq),
        frame=int(message.frame),
        command=int(message.command),
        current=int(message.current),
        autocontinue=int(message.autocontinue),
        param1=float(message.param1),
        param2=float(message.param2),
        param3=float(message.param3),
        param4=float(message.param4),
        x=int(message.x),
        y=int(message.y),
        z=float(message.z),
        mission_type=_mission_type(message),
    )


def readback_int_only_mission(
    connection: Any,
    transaction: Px4MissionTransaction,
    upload_plan: MissionUploadPlan,
    *,
    message_timeout_s: float = 1.5,
    maximum_retries_per_item: int = 5,
) -> dict[str, Any]:
    """Request the stored mission using only REQUEST_INT and verify its wire hash."""

    connection.mav.mission_request_list_send(
        PX4_SYSTEM_ID,
        PX4_AUTOPILOT_COMPONENT_ID,
        MAV_MISSION_TYPE_MISSION,
    )
    ignored_foreign_target_messages = 0
    count_deadline = time.monotonic() + max(3.0, message_timeout_s * 4.0)
    count_message: Any | None = None
    while time.monotonic() < count_deadline:
        candidate = connection.recv_match(
            type="MISSION_COUNT", blocking=True, timeout=message_timeout_s
        )
        if candidate is None:
            continue
        _require_px4_source(candidate, "MISSION_COUNT")
        if not _targets_evaluator(candidate):
            ignored_foreign_target_messages += 1
            continue
        count_message = candidate
        break
    if count_message is None:
        raise VtolMissionAcceptanceError("MISSION readback did not return bound MISSION_COUNT")
    if _mission_type(count_message) != MAV_MISSION_TYPE_MISSION:
        raise VtolMissionAcceptanceError("MISSION_COUNT readback has the wrong mission_type")
    count = int(count_message.count)
    if count != upload_plan.item_count:
        raise VtolMissionAcceptanceError(
            f"MISSION_COUNT readback mismatch: expected {upload_plan.item_count}, got {count}"
        )

    items: list[Px4MissionItemInt] = []
    retries = 0
    for expected_sequence in range(count):
        item: Px4MissionItemInt | None = None
        for attempt in range(maximum_retries_per_item + 1):
            connection.mav.mission_request_int_send(
                PX4_SYSTEM_ID,
                PX4_AUTOPILOT_COMPONENT_ID,
                expected_sequence,
                MAV_MISSION_TYPE_MISSION,
            )
            message = connection.recv_match(
                type=["MISSION_ITEM_INT", "MISSION_ITEM", "MISSION_ACK"],
                blocking=True,
                timeout=message_timeout_s,
            )
            if message is None:
                retries += 1
                continue
            message_type = str(message.get_type())
            _require_px4_source(message, message_type)
            if not _targets_evaluator(message):
                ignored_foreign_target_messages += 1
                retries += 1
                continue
            if message_type == "MISSION_ITEM":
                raise VtolMissionAcceptanceError(
                    "legacy MISSION_ITEM readback is forbidden; MISSION_ITEM_INT is required"
                )
            if message_type == "MISSION_ACK":
                raise VtolMissionAcceptanceError(
                    f"PX4 aborted MISSION readback with ACK type={int(message.type)}"
                )
            candidate = _wire_item_from_int_message(message)
            if candidate.sequence != expected_sequence:
                retries += 1
                if attempt >= maximum_retries_per_item:
                    raise VtolMissionAcceptanceError("MISSION_ITEM_INT readback sequence mismatch")
                continue
            item = candidate
            break
        if item is None:
            raise VtolMissionAcceptanceError(
                f"MISSION_ITEM_INT readback timed out for sequence {expected_sequence}"
            )
        items.append(item)

    readback_hash = px4_wire_hash(tuple(items))
    verified_hash = transaction.verify_readback(tuple(items))
    if verified_hash != upload_plan.protocol_hash or readback_hash != upload_plan.protocol_hash:
        raise VtolMissionAcceptanceError("MISSION readback hash did not match upload plan")
    return {
        "mission_count": count,
        "received_sequences": [item.sequence for item in items],
        "received_message_types": ["MISSION_ITEM_INT"] * len(items),
        "retry_count": retries,
        "ignored_foreign_target_messages": ignored_foreign_target_messages,
        "uploaded_protocol_sha256": upload_plan.protocol_hash,
        "readback_protocol_sha256": readback_hash,
        "hash_match": True,
        "items": [item.to_mapping() for item in items],
    }


def _offset_coordinate(
    latitude_deg: float, longitude_deg: float, *, north_m: float, east_m: float
) -> tuple[float, float]:
    radius_m = 6_371_008.8
    latitude = latitude_deg + math.degrees(north_m / radius_m)
    cosine = math.cos(math.radians(latitude_deg))
    if abs(cosine) < 1.0e-6:
        raise VtolMissionAcceptanceError("live GPS home is too close to a pole")
    longitude = longitude_deg + math.degrees(east_m / (radius_m * cosine))
    if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
        raise VtolMissionAcceptanceError("runtime-derived mission geometry is out of range")
    return latitude, longitude


def build_live_runtime_geometry(
    *, home_latitude_deg: float, home_longitude_deg: float
) -> VtolMissionGeometry:
    """Build a bounded route from live home only; no evaluator coordinate is accepted."""

    first = _offset_coordinate(home_latitude_deg, home_longitude_deg, north_m=70.0, east_m=0.0)
    second = _offset_coordinate(home_latitude_deg, home_longitude_deg, north_m=70.0, east_m=70.0)
    runtime_mapping = {
        "home_position": {
            "latitude_deg": home_latitude_deg,
            "longitude_deg": home_longitude_deg,
        },
        "mission_geometry": {
            "takeoff_altitude_m": 20.0,
            "loiter_time_s": 5.0,
            "loiter_radius_m": 30.0,
            "acceptance_radius_m": 8.0,
            "waypoints": [
                {
                    "latitude_deg": first[0],
                    "longitude_deg": first[1],
                    "altitude_m": 25.0,
                    "altitude_reference": "relative_home",
                },
                {
                    "latitude_deg": second[0],
                    "longitude_deg": second[1],
                    "altitude_m": 25.0,
                    "altitude_reference": "relative_home",
                },
            ],
            "landing": {
                "latitude_deg": home_latitude_deg,
                "longitude_deg": home_longitude_deg,
                "altitude_m": 0.0,
                "altitude_reference": "relative_home",
            },
        },
    }
    return VtolMissionGeometry.from_runtime_mapping(runtime_mapping)


@dataclass(frozen=True, slots=True)
class AcceptanceDocuments:
    prompt: PromptEnvelope
    task_spec: ResolvedTaskSpec
    context: ContextSnapshot
    plan: PlatformNeutralPlan
    compiled: Any
    policy: Px4MissionSafetyPolicy
    validation_report: ValidationReport
    bindings: MissionTransactionBindings
    operator_grant: OperatorGrant
    vehicle_identity_hash: str


def build_acceptance_documents(
    *,
    runtime_input: Any,
    live_telemetry: Mapping[str, Any],
    container_id: str,
    airframe_profile_hash: str,
    runtime_descriptor_hash: str,
    runtime_provenance_hash: str,
    now_utc: datetime,
    maximum_duration_s: float,
) -> AcceptanceDocuments:
    """Build every hash-bound autonomy document from runtime/live inputs."""

    try:
        runtime_assets = resolve_simulator_assets(runtime_input, repository_root=ROOT)
    except (OSError, TypeError, ValueError) as exc:
        raise VtolMissionAcceptanceError(f"runtime map/DEM asset binding failed: {exc}") from exc
    airframe_hash = _require_sha256(airframe_profile_hash, "airframe_profile_hash")
    descriptor_hash = _require_sha256(runtime_descriptor_hash, "runtime_descriptor_hash")
    provenance_hash = _require_sha256(runtime_provenance_hash, "runtime_provenance_hash")
    home = GeoPoint(
        float(live_telemetry["latitude_deg"]),
        float(live_telemetry["longitude_deg"]),
    )
    geometry = build_live_runtime_geometry(
        home_latitude_deg=home.latitude_deg,
        home_longitude_deg=home.longitude_deg,
    )
    route_points = (home, *geometry.waypoints, geometry.landing)
    route_length = sum(
        _haversine_m(
            route_points[index].latitude_deg,
            route_points[index].longitude_deg,
            route_points[index + 1].latitude_deg,
            route_points[index + 1].longitude_deg,
        )
        for index in range(len(route_points) - 1)
    )
    timestamp = _utc_iso(now_utc)
    run_token = f"{runtime_input.scenario_id}-{int(now_utc.timestamp())}"
    aircraft_id = f"owned-sitl-{container_id[:12]}"
    prompt = PromptEnvelope(
        prompt_id=f"prompt-{run_token}",
        text=(
            "Test-only owned disposable SITL acceptance fixture: execute the bounded "
            "runtime-derived VTOL search route and land at live GPS home."
        ),
        language="en",
        issued_at=timestamp,
        operator_id="test-only-acceptance-fixture",
        aircraft_id=aircraft_id,
    )
    task_spec = ResolvedTaskSpec(
        task_id=f"task-{run_token}",
        task_type=TaskType.CORRIDOR_VISUAL_SEARCH,
        geometry=TaskGeometry(
            kind="runtime-derived-live-gps-corridor",
            resolved_feature_id=f"live-home-{container_id[:12]}",
            direction="north_then_east_then_live_home",
            length_m=route_length,
            centerline=route_points,
            resolution_confidence=1.0,
            buffer_m=40.0,
        ),
        target_classes=("simulated_search_subject",),
        on_detection=(PlanAction.CAPTURE_EVIDENCE, PlanAction.REPORT_GEOLOCATION),
        constraints=TaskConstraints(
            minimum_return_battery_pct=5.0,
            maximum_duration_s=maximum_duration_s,
            remain_inside_geofence=True,
        ),
        completion_action=PlanAction.LAND,
    )
    camera_available = any(sensor.kind == "rgb_camera" for sensor in runtime_input.sensors)
    context = ContextSnapshot(
        snapshot_id=f"context-{run_token}",
        captured_at_s=float(live_telemetry["captured_at_s"]),
        captured_at_utc=timestamp,
        aircraft_position=home,
        home_position=home,
        airframe_profile_id=runtime_input.vehicle.airframe_profile_id,
        battery_remaining_pct=float(live_telemetry["battery_remaining_pct"]),
        estimated_flight_time_s=maximum_duration_s,
        ground_speed_mps=float(live_telemetry.get("ground_speed_mps", 0.0)),
        airspeed_mps=float(live_telemetry.get("airspeed_mps", 0.0)),
        wind_north_mps=runtime_input.world.wind_north_mps,
        wind_east_mps=runtime_input.world.wind_east_mps,
        gps_fix_type=int(live_telemetry["gps_fix_type"]),
        gps_horizontal_accuracy_m=float(live_telemetry["gps_horizontal_accuracy_m"]),
        geofence_valid=True,
        camera_available=camera_available,
        perception_available=camera_available,
        map_cache_available=runtime_assets.map_path.is_file(),
        dem_available=runtime_assets.dem_path.is_file(),
    )
    plan = PlatformNeutralPlan(
        plan_id=f"plan-{run_token}",
        task_spec_hash=task_spec.canonical_hash,
        context_snapshot_hash=context.canonical_hash,
        steps=(
            PlanStep(0, PlanAction.TAKEOFF),
            PlanStep(1, PlanAction.TRANSIT),
            PlanStep(2, PlanAction.SEARCH_CORRIDOR),
            PlanStep(3, PlanAction.LAND),
        ),
    )
    policy = Px4MissionSafetyPolicy(
        geofence_center=home,
        geofence_radius_m=350.0,
        allowed_airframe_profile_ids=(runtime_input.vehicle.airframe_profile_id,),
        minimum_relative_altitude_m=10.0,
        maximum_relative_altitude_m=60.0,
        maximum_mission_radius_m=250.0,
        maximum_loiter_radius_m=60.0,
        maximum_loiter_time_s=30.0,
        maximum_landing_offset_m=5.0,
        home_match_tolerance_m=1.0,
    )
    compiled = compile_executable_mission(
        vehicle_class="vtol",
        platform_plan=plan,
        context_snapshot=context,
        geometry=geometry,
        policy=policy,
        airframe_profile_hash=airframe_hash,
        mission_id=f"mission-{run_token}",
    )
    report = ValidationReport(
        report_id=f"validation-{run_token}",
        executable_mission_hash=compiled.executable_mission.canonical_hash,
        verdict=ValidationVerdict.PASS,
        findings=(
            ValidationFinding("runtime-channel-only", ValidationVerdict.PASS, "truth not loaded"),
            ValidationFinding("live-gps-home", ValidationVerdict.PASS, "qualified live GPS"),
            ValidationFinding("geofence", ValidationVerdict.PASS, "all items inside policy"),
            ValidationFinding("mission-item-int", ValidationVerdict.PASS, "INT-only wire contract"),
            ValidationFinding(
                "owned-sitl", ValidationVerdict.PASS, "exact container identity bound"
            ),
        ),
    )
    vehicle_identity_hash = canonical_sha256(
        {
            "container_id": container_id,
            "aircraft_id": aircraft_id,
            "vehicle_model": runtime_input.vehicle.model,
            "airframe_profile_id": runtime_input.vehicle.airframe_profile_id,
            "target_system": PX4_SYSTEM_ID,
            "target_component": PX4_AUTOPILOT_COMPONENT_ID,
            "sim_only": True,
        }
    )
    transaction_id = f"transaction-{run_token}"
    bindings = MissionTransactionBindings(
        prompt_hash=prompt.canonical_hash,
        task_spec_hash=task_spec.canonical_hash,
        plan_hash=plan.canonical_hash,
        context_snapshot_hash=context.canonical_hash,
        airframe_profile_hash=airframe_hash,
        geofence_hash=policy.geofence_hash,
        vehicle_identity_hash=vehicle_identity_hash,
        runtime_descriptor_hash=descriptor_hash,
        runtime_provenance_hash=provenance_hash,
    )
    grant = OperatorGrant(
        grant_id=f"test-only-acceptance-fixture-{run_token}",
        transaction_id=transaction_id,
        operator_id="test-only-acceptance-fixture-not-human-approval",
        prompt_hash=bindings.prompt_hash,
        task_spec_hash=bindings.task_spec_hash,
        plan_hash=bindings.plan_hash,
        executable_mission_hash=compiled.executable_mission.canonical_hash,
        validation_report_hash=report.canonical_hash,
        context_snapshot_hash=bindings.context_snapshot_hash,
        airframe_profile_hash=bindings.airframe_profile_hash,
        geofence_hash=bindings.geofence_hash,
        vehicle_identity_hash=bindings.vehicle_identity_hash,
        runtime_descriptor_hash=bindings.runtime_descriptor_hash,
        runtime_provenance_hash=bindings.runtime_provenance_hash,
        expires_at=_utc_iso(now_utc + timedelta(minutes=15)),
    )
    return AcceptanceDocuments(
        prompt=prompt,
        task_spec=task_spec,
        context=context,
        plan=plan,
        compiled=compiled,
        policy=policy,
        validation_report=report,
        bindings=bindings,
        operator_grant=grant,
        vehicle_identity_hash=vehicle_identity_hash,
    )


def _collect_live_context(
    collector: TelemetryCollector, *, timeout_s: float = 8.0
) -> dict[str, Any]:
    latest_gps: Any | None = None
    latest_vfr: Any | None = None
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        message = collector.pump(0.2)
        if message is not None:
            message_type = str(message.get_type())
            if message_type == "GPS_RAW_INT":
                _require_px4_source(message, message_type)
                latest_gps = message
            elif message_type == "VFR_HUD":
                _require_px4_source(message, message_type)
                latest_vfr = message
        if (
            latest_gps is not None
            and collector.last_position is not None
            and collector.last_battery_remaining is not None
        ):
            break
    if latest_gps is None or collector.last_position is None:
        raise VtolMissionAcceptanceError("live GPS/global-position context is incomplete")
    if collector.last_battery_remaining is None:
        raise VtolMissionAcceptanceError("live battery context is unavailable")
    fix_type = int(latest_gps.fix_type)
    latitude_deg = int(latest_gps.lat) / 10_000_000.0
    longitude_deg = int(latest_gps.lon) / 10_000_000.0
    satellites = int(latest_gps.satellites_visible)
    if fix_type < 3 or not latitude_deg or not longitude_deg or satellites < 6:
        raise VtolMissionAcceptanceError("live GPS home is not qualified")
    h_acc_raw = int(getattr(latest_gps, "h_acc", 0))
    eph_raw = int(getattr(latest_gps, "eph", 65_535))
    if 0 < h_acc_raw < 4_294_967_295:
        horizontal_accuracy_m = h_acc_raw / 1000.0
        accuracy_source = "GPS_RAW_INT.h_acc"
    elif 0 < eph_raw < 65_535:
        horizontal_accuracy_m = eph_raw / 100.0
        accuracy_source = "GPS_RAW_INT.eph"
    else:
        raise VtolMissionAcceptanceError("live GPS horizontal accuracy is unavailable")
    global_position = collector.last_position
    disagreement_m = _haversine_m(
        latitude_deg,
        longitude_deg,
        float(global_position["latitude_deg"]),
        float(global_position["longitude_deg"]),
    )
    if disagreement_m > 25.0:
        raise VtolMissionAcceptanceError("GPS and estimator global position disagree")
    # PX4's SITL VFR_HUD can report a small negative airspeed while the
    # estimator is still settling on the ground (for example -0.78 m/s in the
    # combined acceptance run).  Airspeed is a magnitude at this contract
    # boundary, so clamp only this physically impossible numerical undershoot
    # to zero instead of rejecting an otherwise qualified live GPS context.
    raw_ground_speed_mps = float(getattr(latest_vfr, "groundspeed", 0.0))
    raw_airspeed_mps = float(getattr(latest_vfr, "airspeed", 0.0))
    ground_speed_mps = max(0.0, raw_ground_speed_mps)
    airspeed_mps = max(0.0, raw_airspeed_mps)
    return {
        "captured_at_s": round(collector.elapsed_s, 6),
        "latitude_deg": latitude_deg,
        "longitude_deg": longitude_deg,
        "gps_fix_type": fix_type,
        "satellites_visible": satellites,
        "gps_horizontal_accuracy_m": horizontal_accuracy_m,
        "gps_accuracy_source": accuracy_source,
        "gps_estimator_disagreement_m": round(disagreement_m, 3),
        "battery_remaining_pct": float(collector.last_battery_remaining),
        "ground_speed_mps": ground_speed_mps,
        "airspeed_mps": airspeed_mps,
        "raw_ground_speed_mps": raw_ground_speed_mps,
        "raw_airspeed_mps": raw_airspeed_mps,
        "nonnegative_speed_clamp_applied": (
            ground_speed_mps != raw_ground_speed_mps or airspeed_mps != raw_airspeed_mps
        ),
        "source": "live MAVLink telemetry only",
    }


def _wait_for_disarmed_ground_state(
    collector: TelemetryCollector, *, timeout_s: float = 12.0
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        collector.pump(0.2)
        armed = bool(
            collector.last_heartbeat
            and int(collector.last_heartbeat["base_mode"])
            & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
        )
        if (
            not armed
            and collector.last_vtol_state == MAV_VTOL_STATE_MC
            and collector.last_landed_state == mavutil.mavlink.MAV_LANDED_STATE_ON_GROUND
        ):
            return
    raise VtolMissionAcceptanceError(
        "mission upload requires an explicitly disarmed, multicopter, on-ground state"
    )


def _wait_for_exact_auto_mission(
    collector: TelemetryCollector, *, timeout_s: float = 10.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        message = collector.pump(0.2)
        if message is None or str(message.get_type()) != "HEARTBEAT":
            continue
        _require_px4_source(message, "HEARTBEAT")
        heartbeat = collector.last_heartbeat or {}
        base_mode = int(heartbeat.get("base_mode", 0))
        custom_mode = int(heartbeat.get("custom_mode", -1))
        if (
            custom_mode == PX4_AUTO_MISSION_CUSTOM_MODE
            and base_mode & MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
        ):
            return {
                "base_mode": base_mode,
                "custom_mode": custom_mode,
                "mode": "PX4_AUTO.MISSION",
            }
    raise VtolMissionAcceptanceError("PX4 did not confirm the exact AUTO.MISSION custom mode")


def _send_command_long_exact(
    collector: TelemetryCollector, command: int, *parameters: float, timeout_s: float = 10.0
) -> int:
    values = list(parameters) + [0.0] * (7 - len(parameters))
    collector.connection.mav.command_long_send(
        PX4_SYSTEM_ID,
        PX4_AUTOPILOT_COMPONENT_ID,
        command,
        0,
        *values[:7],
    )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        message = collector.pump(0.2)
        if message is None or str(message.get_type()) != "COMMAND_ACK":
            continue
        if int(message.command) != command:
            continue
        _require_px4_source(message, "COMMAND_ACK")
        result = int(message.result)
        if result != mavutil.mavlink.MAV_RESULT_ACCEPTED:
            raise VtolMissionAcceptanceError(
                f"PX4 rejected command {command} with MAV_RESULT={result}"
            )
        return result
    raise VtolMissionAcceptanceError(f"PX4 COMMAND_ACK timeout for command {command}")


def _wait_for_armed(collector: TelemetryCollector, *, timeout_s: float = 8.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        message = collector.pump(0.2)
        if message is None or str(message.get_type()) != "HEARTBEAT":
            continue
        _require_px4_source(message, "HEARTBEAT")
        if (
            collector.last_heartbeat
            and int(collector.last_heartbeat["base_mode"])
            & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
        ):
            return
    raise VtolMissionAcceptanceError("post-permit ARM was accepted but armed state was absent")


def observe_mission_flight(
    collector: TelemetryCollector,
    *,
    item_count: int,
    home_latitude_deg: float,
    home_longitude_deg: float,
    mission_timeout_s: float,
    mission_active_ready_file: Path | None,
    container_id: str,
    transaction_state: str,
    control_command_events: list[dict[str, Any]],
    flight_capture_ready_file: Path | None = None,
    flight_capture_complete_file: Path | None = None,
    transaction_id: str | None = None,
    committed_protocol_sha256: str | None = None,
    scenario_id: str | None = None,
    scenario_manifest_hash: str | None = None,
) -> dict[str, Any]:
    """Observe real PX4 mission progress, VTOL lifecycle, landing, and disarm."""

    current_sequences: list[int] = []
    reached_sequences: list[int] = []
    maximum_distance_m = 0.0
    maximum_altitude_m = 0.0
    in_air_observed = False
    fixed_wing_observed = False
    multicopter_observed = False
    landed_observed = False
    terminal_disarm_command_sent = False
    first_activity: dict[str, Any] | None = None
    ready_written = False
    flight_capture_ready_written = False
    flight_capture_ready_monotonic_s: float | None = None
    flight_capture_complete_observed = False
    flight_capture_complete_document: dict[str, Any] | None = None
    flight_capture_complete_checks: dict[str, bool] | None = None
    flight_capture_complete_file_sha256: str | None = None
    flight_capture_complete_last_error: str | None = None
    flight_capture_interval_samples: list[dict[str, Any]] = []
    flight_capture_interval_violation = False
    flight_capture_protocol_hash: str | None = None
    if flight_capture_ready_file is not None:
        if not isinstance(transaction_id, str) or not transaction_id.strip():
            raise VtolMissionAcceptanceError(
                "flight-capture ready file requires a nonempty transaction_id"
            )
        flight_capture_protocol_hash = _require_sha256(
            committed_protocol_sha256 or "", "committed_protocol_sha256"
        )
        if flight_capture_complete_file is None:
            raise VtolMissionAcceptanceError(
                "flight-capture ready file requires a completion marker file"
            )
        if not isinstance(scenario_id, str) or not scenario_id:
            raise VtolMissionAcceptanceError(
                "flight-capture completion binding requires a scenario_id"
            )
        scenario_manifest_hash = _require_sha256(
            scenario_manifest_hash or "", "scenario_manifest_hash"
        )
    terminal_landed_at: float | None = None
    started = time.monotonic()
    deadline = started + mission_timeout_s
    while time.monotonic() < deadline:
        message = collector.pump(0.2)
        observed_now_monotonic_s = time.monotonic()
        now_elapsed = observed_now_monotonic_s - started
        if message is not None:
            message_type = str(message.get_type())
            if message_type in {
                "HEARTBEAT",
                "GLOBAL_POSITION_INT",
                "EXTENDED_SYS_STATE",
                "MISSION_CURRENT",
                "MISSION_ITEM_REACHED",
            }:
                _require_px4_source(message, message_type)
            if message_type == "MISSION_CURRENT":
                sequence = int(message.seq)
                if 0 <= sequence < item_count and sequence not in current_sequences:
                    current_sequences.append(sequence)
                    first_activity = first_activity or {
                        "message_type": message_type,
                        "sequence": sequence,
                        "at_s": round(now_elapsed, 6),
                    }
            elif message_type == "MISSION_ITEM_REACHED":
                sequence = int(message.seq)
                if 0 <= sequence < item_count and sequence not in reached_sequences:
                    reached_sequences.append(sequence)
                    first_activity = first_activity or {
                        "message_type": message_type,
                        "sequence": sequence,
                        "at_s": round(now_elapsed, 6),
                    }

        if collector.last_position is not None:
            position = collector.last_position
            maximum_distance_m = max(
                maximum_distance_m,
                _haversine_m(
                    home_latitude_deg,
                    home_longitude_deg,
                    float(position["latitude_deg"]),
                    float(position["longitude_deg"]),
                ),
            )
            maximum_altitude_m = max(maximum_altitude_m, float(position["relative_altitude_m"]))
        if collector.last_landed_state == mavutil.mavlink.MAV_LANDED_STATE_IN_AIR:
            in_air_observed = True
        in_air_at_ready = collector.last_landed_state == mavutil.mavlink.MAV_LANDED_STATE_IN_AIR
        if collector.last_vtol_state == MAV_VTOL_STATE_FW:
            fixed_wing_observed = True
        if fixed_wing_observed and collector.last_vtol_state == MAV_VTOL_STATE_MC:
            multicopter_observed = True
        if (
            in_air_observed
            and collector.last_landed_state == mavutil.mavlink.MAV_LANDED_STATE_ON_GROUND
        ):
            landed_observed = True
            terminal_landed_at = terminal_landed_at or time.monotonic()
        armed = bool(
            collector.last_heartbeat
            and int(collector.last_heartbeat["base_mode"])
            & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
        )
        auto_mission_active = bool(
            collector.last_heartbeat
            and int(collector.last_heartbeat["base_mode"]) & MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
            and int(collector.last_heartbeat.get("custom_mode", -1)) == PX4_AUTO_MISSION_CUSTOM_MODE
        )
        mission_progress_observed = bool(
            first_activity is not None and (current_sequences or reached_sequences)
        )
        heartbeat_age_s = _observation_age_s(
            observed_now_monotonic_s,
            getattr(collector, "last_heartbeat_observed_at_monotonic_s", None),
        )
        position_age_s = _observation_age_s(
            observed_now_monotonic_s,
            getattr(collector, "last_position_observed_at_monotonic_s", None),
        )
        extended_state_age_s = _observation_age_s(
            observed_now_monotonic_s,
            getattr(collector, "last_extended_state_observed_at_monotonic_s", None),
        )
        telemetry_fresh = bool(
            heartbeat_age_s is not None
            and heartbeat_age_s <= MAXIMUM_FLIGHT_STATE_AGE_S
            and position_age_s is not None
            and position_age_s <= MAXIMUM_FLIGHT_STATE_AGE_S
            and extended_state_age_s is not None
            and extended_state_age_s <= MAXIMUM_FLIGHT_STATE_AGE_S
        )

        if (
            mission_progress_observed
            and auto_mission_active
            and armed
            and in_air_at_ready
            and telemetry_fresh
            and mission_active_ready_file is not None
            and not ready_written
        ):
            ready_payload = {
                "schema": "multidetect.px4-vtol-mission-active-ready",
                "schema_version": 1,
                "generated_at_utc": _utc_now(),
                "container_id": container_id,
                "transaction_state": transaction_state,
                "auto_mission_confirmed": True,
                "auto_mission_active": auto_mission_active,
                "auto_mission_custom_mode": PX4_AUTO_MISSION_CUSTOM_MODE,
                "armed_after_permit": armed,
                "in_air_observed": in_air_observed,
                "in_air_at_ready": in_air_at_ready,
                "mission_activity": first_activity,
                "mission_progress": {
                    "observed": mission_progress_observed,
                    "current_sequences": list(current_sequences),
                    "reached_sequences": list(reached_sequences),
                },
                "telemetry_fresh": True,
                "maximum_telemetry_age_s": MAXIMUM_FLIGHT_STATE_AGE_S,
                "telemetry_ages_s": {
                    "heartbeat": round(heartbeat_age_s or 0.0, 6),
                    "position": round(position_age_s or 0.0, 6),
                    "extended_system_state": round(extended_state_age_s or 0.0, 6),
                },
                "operator_visual_acceptance": "not_claimed",
                "real_pixhawk_contacted": False,
                "real_jetson_contacted": False,
                "real_camera_contacted": False,
            }
            if (
                transaction_state != MissionTransactionState.START_PERMITTED.value
                or not auto_mission_active
                or not armed
                or not in_air_at_ready
                or not mission_progress_observed
                or not telemetry_fresh
            ):
                raise VtolMissionAcceptanceError(
                    "mission ready barrier reached without permit, exact AUTO.MISSION, "
                    "armed, in-air, progress, and fresh telemetry state"
                )
            _atomic_write_json(mission_active_ready_file, ready_payload, overwrite=False)
            ready_written = True

        flight_capture_barrier_reached = _flight_capture_ready_gate(
            transaction_state=transaction_state,
            mission_progress_observed=mission_progress_observed,
            auto_mission_active=auto_mission_active,
            armed=armed,
            in_air_at_ready=in_air_at_ready,
            fixed_wing_observed=fixed_wing_observed,
            current_vtol_state=collector.last_vtol_state,
            maximum_distance_m=maximum_distance_m,
            maximum_altitude_m=maximum_altitude_m,
            telemetry_fresh=telemetry_fresh,
        )
        if (
            flight_capture_ready_file is not None
            and not flight_capture_ready_written
            and flight_capture_barrier_reached
        ):
            if collector.last_position is None:
                raise VtolMissionAcceptanceError(
                    "flight-capture ready barrier has no current vehicle position"
                )
            position_at_ready = {
                "latitude_deg": float(collector.last_position["latitude_deg"]),
                "longitude_deg": float(collector.last_position["longitude_deg"]),
                "relative_altitude_m": float(collector.last_position["relative_altitude_m"]),
            }
            # The external capture process can only bind to the timestamp
            # serialized in the ready marker. Keep the in-memory comparison
            # value identical to that six-decimal contract value; otherwise a
            # round-down by less than one microsecond can reject an exact echo.
            flight_capture_ready_monotonic_s = round(time.monotonic(), 6)
            flight_capture_payload = {
                "schema": "multidetect.px4-vtol-flight-capture-ready",
                "schema_version": 1,
                "generated_at_utc": _utc_now(),
                "captured_at_clock": "host_monotonic",
                "captured_at_monotonic_s": flight_capture_ready_monotonic_s,
                "container": {
                    "id": container_id,
                    "exact_id_bound": True,
                },
                "transaction": {
                    "id": transaction_id,
                    "state": transaction_state,
                    "start_permitted": True,
                    "committed_protocol_sha256": flight_capture_protocol_hash,
                },
                "flight": {
                    "auto_mission_active": True,
                    "auto_mission_custom_mode": PX4_AUTO_MISSION_CUSTOM_MODE,
                    "armed": True,
                    "in_air_at_ready": True,
                    "fixed_wing_observed": True,
                    "current_vtol_state": collector.last_vtol_state,
                    "maximum_distance_from_home_m": round(maximum_distance_m, 3),
                    "minimum_required_distance_from_home_m": 35.0,
                    "maximum_relative_altitude_m": round(maximum_altitude_m, 3),
                    "minimum_required_relative_altitude_m": 10.0,
                    "position_at_ready": position_at_ready,
                    "mission_progress": {
                        "observed": True,
                        "first_activity": first_activity,
                        "current_sequences": list(current_sequences),
                        "reached_sequences": list(reached_sequences),
                    },
                    "telemetry_fresh": True,
                    "maximum_telemetry_age_s": MAXIMUM_FLIGHT_STATE_AGE_S,
                    "telemetry_ages_s": {
                        "heartbeat": round(heartbeat_age_s or 0.0, 6),
                        "position": round(position_age_s or 0.0, 6),
                        "extended_system_state": round(extended_state_age_s or 0.0, 6),
                    },
                },
                "operator_visual_acceptance": "not_claimed",
                "real_pixhawk_contacted": False,
                "real_jetson_contacted": False,
                "real_camera_contacted": False,
            }
            _atomic_write_json(
                flight_capture_ready_file,
                flight_capture_payload,
                overwrite=False,
            )
            flight_capture_ready_written = True

        if flight_capture_ready_written and not flight_capture_complete_observed:
            sample_monotonic_s = time.monotonic()
            interval_state_valid = bool(
                auto_mission_active
                and armed
                and in_air_at_ready
                and collector.last_vtol_state == MAV_VTOL_STATE_FW
                and telemetry_fresh
            )
            flight_capture_interval_samples.append(
                {
                    "captured_at_clock": "host_monotonic",
                    "captured_at_monotonic_s": round(sample_monotonic_s, 6),
                    "auto_mission_active": auto_mission_active,
                    "armed": armed,
                    "in_air": in_air_at_ready,
                    "vtol_state": collector.last_vtol_state,
                    "maximum_state_age_s": MAXIMUM_FLIGHT_STATE_AGE_S,
                    "heartbeat_age_s": (
                        round(heartbeat_age_s, 6) if heartbeat_age_s is not None else None
                    ),
                    "position_age_s": (
                        round(position_age_s, 6) if position_age_s is not None else None
                    ),
                    "extended_system_state_age_s": (
                        round(extended_state_age_s, 6) if extended_state_age_s is not None else None
                    ),
                    "telemetry_fresh": telemetry_fresh,
                    "valid": interval_state_valid,
                }
            )
            if not interval_state_valid:
                flight_capture_interval_violation = True
            if flight_capture_complete_file is not None and flight_capture_complete_file.is_file():
                try:
                    marker_value = json.loads(
                        flight_capture_complete_file.read_text(encoding="utf-8")
                    )
                    if not isinstance(marker_value, dict):
                        raise VtolMissionAcceptanceError(
                            "flight-capture complete marker must be one JSON object"
                        )
                    if flight_capture_ready_monotonic_s is None:
                        raise VtolMissionAcceptanceError(
                            "flight-capture complete marker was observed before ready time"
                        )
                    marker_checks = validate_flight_capture_complete_marker(
                        marker_value,
                        flight_ready_monotonic_s=flight_capture_ready_monotonic_s,
                        container_id=container_id,
                        transaction_id=transaction_id or "",
                        committed_protocol_sha256=flight_capture_protocol_hash or "",
                        scenario_id=scenario_id or "",
                        scenario_manifest_hash=scenario_manifest_hash or "",
                    )
                    if not interval_state_valid or flight_capture_interval_violation:
                        raise VtolMissionAcceptanceError(
                            "vehicle left current FW/armed/in-air/AUTO.MISSION state during capture"
                        )
                    flight_capture_complete_document = marker_value
                    flight_capture_complete_checks = marker_checks
                    flight_capture_complete_file_sha256 = _sha256_path(flight_capture_complete_file)
                    flight_capture_complete_observed = True
                    flight_capture_complete_last_error = None
                except (
                    OSError,
                    UnicodeError,
                    json.JSONDecodeError,
                    VtolMissionAcceptanceError,
                ) as exc:
                    flight_capture_complete_last_error = f"{type(exc).__name__}: {exc}"

        terminal_item_reached = item_count - 1 in reached_sequences
        terminal_item_current = item_count - 1 in current_sequences
        if (
            terminal_item_reached
            and landed_observed
            and armed
            and terminal_landed_at is not None
            and not terminal_disarm_command_sent
            and time.monotonic() - terminal_landed_at >= 8.0
        ):
            result = _send_command_long_exact(
                collector,
                mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
                0.0,
                21196.0,
            )
            control_command_events.append(
                {
                    "command": "MAV_CMD_COMPONENT_ARM_DISARM",
                    "command_id": int(mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM),
                    "arm": False,
                    "result": result,
                    "reason": "post-VTOL_LAND on-ground terminal completion",
                    "after_permit": True,
                }
            )
            terminal_disarm_command_sent = True
            continue
        if (
            terminal_item_reached
            and terminal_item_current
            and in_air_observed
            and fixed_wing_observed
            and multicopter_observed
            and landed_observed
            and not armed
            and heartbeat_age_s is not None
            and heartbeat_age_s <= MAXIMUM_FLIGHT_STATE_AGE_S
            and extended_state_age_s is not None
            and extended_state_age_s <= MAXIMUM_FLIGHT_STATE_AGE_S
            and position_age_s is not None
            and position_age_s <= MAXIMUM_FLIGHT_STATE_AGE_S
        ):
            if maximum_distance_m < 35.0:
                raise VtolMissionAcceptanceError(
                    "mission completed without enough real horizontal movement"
                )
            if maximum_altitude_m < 10.0:
                raise VtolMissionAcceptanceError(
                    "mission completed without enough real relative altitude"
                )
            if mission_active_ready_file is not None and not ready_written:
                raise VtolMissionAcceptanceError(
                    "mission completed without the active ready barrier"
                )
            if flight_capture_ready_file is not None and not flight_capture_ready_written:
                raise VtolMissionAcceptanceError(
                    "mission completed without the flight-capture ready barrier"
                )
            if flight_capture_complete_file is not None and not flight_capture_complete_observed:
                detail = (
                    f": {flight_capture_complete_last_error}"
                    if flight_capture_complete_last_error
                    else ""
                )
                raise VtolMissionAcceptanceError(
                    "mission completed without a valid flight-capture completion marker" + detail
                )
            if flight_capture_interval_violation:
                raise VtolMissionAcceptanceError(
                    "vehicle left current FW/armed/in-air/AUTO.MISSION state during capture"
                )
            return {
                "auto_mission_confirmed": True,
                "armed_after_permit": True,
                "mission_current_sequences": current_sequences,
                "mission_item_reached_sequences": reached_sequences,
                "maximum_distance_from_home_m": round(maximum_distance_m, 3),
                "maximum_relative_altitude_m": round(maximum_altitude_m, 3),
                "in_air_observed": in_air_observed,
                "fixed_wing_observed": fixed_wing_observed,
                "multicopter_observed": multicopter_observed,
                "landed_observed": landed_observed,
                "terminal_disarmed": True,
                "terminal_vtol_land_sequence": item_count - 1,
                "terminal_disarm_command_sent": terminal_disarm_command_sent,
                "mission_active_ready_written": ready_written,
                "mission_active_ready_file": (
                    str(mission_active_ready_file) if mission_active_ready_file else None
                ),
                "flight_capture_ready_written": flight_capture_ready_written,
                "flight_capture_ready_file": (
                    str(flight_capture_ready_file) if flight_capture_ready_file else None
                ),
                "flight_capture_complete_observed": flight_capture_complete_observed,
                "flight_capture_complete_file": (
                    str(flight_capture_complete_file) if flight_capture_complete_file else None
                ),
                "flight_capture_complete_file_sha256": flight_capture_complete_file_sha256,
                "flight_capture_complete_document": flight_capture_complete_document,
                "flight_capture_complete_checks": flight_capture_complete_checks,
                "flight_capture_interval_state_source": "live MAVLink state observer",
                "flight_capture_interval_samples": flight_capture_interval_samples,
                "flight_capture_interval_sample_count": len(flight_capture_interval_samples),
                "flight_capture_interval_violation": flight_capture_interval_violation,
                "flight_capture_interval_qualified": bool(
                    flight_capture_complete_observed
                    and flight_capture_interval_samples
                    and not flight_capture_interval_violation
                    and all(sample["valid"] is True for sample in flight_capture_interval_samples)
                ),
                "duration_s": round(now_elapsed, 6),
            }
    raise VtolMissionAcceptanceError(
        "VTOL mission did not prove progress, movement, flight, FW/MC, VTOL_LAND, and disarm "
        f"within {mission_timeout_s:.1f}s"
    )


def _document_evidence(documents: AcceptanceDocuments) -> dict[str, Any]:
    return {
        "prompt": {
            "document": documents.prompt.to_mapping(),
            "sha256": documents.prompt.canonical_hash,
        },
        "task_spec": {
            "document": documents.task_spec.to_mapping(),
            "sha256": documents.task_spec.canonical_hash,
        },
        "context_snapshot": {
            "document": documents.context.to_mapping(),
            "sha256": documents.context.canonical_hash,
        },
        "platform_neutral_plan": {
            "document": documents.plan.to_mapping(),
            "sha256": documents.plan.canonical_hash,
        },
        "executable_mission": {
            "document": documents.compiled.executable_mission.to_mapping(),
            "sha256": documents.compiled.executable_mission.canonical_hash,
        },
        "mission_dispatcher": {
            "name": "compile_executable_mission",
            "vehicle_class_input": "vtol",
            "vehicle_class_normalized": normalize_px4_vehicle_class("vtol"),
        },
        "validation_report": {
            "document": documents.validation_report.to_mapping(),
            "sha256": documents.validation_report.canonical_hash,
        },
        "operator_grant": {
            "document": documents.operator_grant.to_mapping(),
            "sha256": documents.operator_grant.canonical_hash,
            "classification": "test-only owned-SITL acceptance fixture",
            "human_operator_approval_claimed": False,
        },
        "bindings": {
            "prompt_hash": documents.bindings.prompt_hash,
            "task_spec_hash": documents.bindings.task_spec_hash,
            "plan_hash": documents.bindings.plan_hash,
            "context_snapshot_hash": documents.bindings.context_snapshot_hash,
            "airframe_profile_hash": documents.bindings.airframe_profile_hash,
            "geofence_hash": documents.bindings.geofence_hash,
            "vehicle_identity_hash": documents.bindings.vehicle_identity_hash,
            "runtime_descriptor_hash": documents.bindings.runtime_descriptor_hash,
            "runtime_provenance_hash": documents.bindings.runtime_provenance_hash,
        },
    }


def run_mission_acceptance(
    manifest_path: Path,
    evidence_path: Path,
    *,
    docker: str,
    container_name: str,
    expected_container_id: str,
    expected_container_label: str,
    evaluator_port: int,
    airframe_profile_sha256: str,
    runtime_descriptor_sha256: str,
    runtime_provenance_sha256: str,
    mission_active_ready_file: Path | None,
    mission_journal_path: Path | None = None,
    mission_timeout_s: float,
    source_system: int,
    source_component: int,
    acknowledge_owned_disposable_sitl: bool,
    flight_capture_ready_file: Path | None = None,
    flight_capture_complete_file: Path | None = None,
) -> dict[str, Any]:
    """Execute and evidence the real attached PX4/Gazebo VTOL mission gate."""

    if not acknowledge_owned_disposable_sitl:
        raise VtolMissionAcceptanceError(
            "--acknowledge-owned-disposable-sitl is required for the evaluator writer"
        )
    if not 1024 <= evaluator_port <= 65535 or evaluator_port == PROTECTED_GROUND_STATION_PORT:
        raise VtolMissionAcceptanceError(
            "evaluator port must be in 1024..65535 and cannot be protected UDP 14550"
        )
    if not 10.0 <= mission_timeout_s <= MAXIMUM_MISSION_TIMEOUT_S:
        raise VtolMissionAcceptanceError("mission timeout must be in 10..600 seconds")
    if (source_system, source_component) != (GCS_SOURCE_SYSTEM, GCS_SOURCE_COMPONENT):
        raise VtolMissionAcceptanceError(
            "mission evaluator requires the exact bound MAVLink identity 250/191"
        )
    airframe_hash = _require_sha256(airframe_profile_sha256, "airframe_profile_sha256")
    descriptor_hash = _require_sha256(runtime_descriptor_sha256, "runtime_descriptor_sha256")
    provenance_hash = _require_sha256(runtime_provenance_sha256, "runtime_provenance_sha256")
    source = manifest_path.resolve(strict=True)
    # This is the sole scenario loader imported by this adapter.  It verifies
    # and reads runtime_input only; evaluator truth and expectations stay closed.
    runtime_input = load_runtime_scenario(source, mode=RuntimeMode.SIM)
    if runtime_input.vehicle.model != "px4_vtol_standard":
        raise VtolMissionAcceptanceError("mission gate requires px4_vtol_standard")
    # Inspect and operate on the immutable ID supplied by the lifecycle owner.
    # The human-readable name remains an independently validated metadata
    # field, never the execution authority after ownership is established.
    record = _container_inspect(docker, expected_container_id)
    ownership_checks = validate_owned_container_record(
        record,
        container_name=container_name,
        expected_container_id=expected_container_id,
        expected_label=expected_container_label,
    )
    _validate_ready_file_targets(
        mission_active_ready_file,
        flight_capture_ready_file,
        flight_capture_complete_file,
    )
    resolved_journal_path = (
        (evidence_path.parent / f"{evidence_path.stem}.mission.jsonl")
        if mission_journal_path is None
        else mission_journal_path.resolve()
    )
    if resolved_journal_path.exists():
        raise VtolMissionAcceptanceError(
            f"stale mission journal already exists: {resolved_journal_path}"
        )

    endpoint = f"udpin:127.0.0.1:{evaluator_port}"
    connection: Any | None = None
    collector: TelemetryCollector | None = None
    documents: AcceptanceDocuments | None = None
    transaction: Any | None = None
    mission_journal: DurableMissionJournal | None = None
    transaction_events: list[dict[str, Any]] = []
    control_command_events: list[dict[str, Any]] = []
    parameter_overrides: dict[str, Any] = {}
    protocol: dict[str, Any] = {
        "upload_acknowledged": False,
        "readback_hash_match": False,
        "mission_item_count": 0,
    }
    flight: dict[str, Any] = {
        "auto_mission_confirmed": False,
        "armed_after_permit": False,
        "mission_current_sequences": [],
        "mission_item_reached_sequences": [],
        "maximum_distance_from_home_m": 0.0,
        "maximum_relative_altitude_m": 0.0,
        "in_air_observed": False,
        "fixed_wing_observed": False,
        "multicopter_observed": False,
        "landed_observed": False,
        "terminal_disarmed": False,
        "mission_active_ready_written": False,
        "mission_active_ready_file": (
            str(mission_active_ready_file) if mission_active_ready_file else None
        ),
        "flight_capture_ready_written": False,
        "flight_capture_ready_file": (
            str(flight_capture_ready_file) if flight_capture_ready_file else None
        ),
        "flight_capture_complete_observed": False,
        "flight_capture_complete_file": (
            str(flight_capture_complete_file) if flight_capture_complete_file else None
        ),
        "flight_capture_interval_qualified": False,
    }
    live_context: dict[str, Any] | None = None
    failure: str | None = None
    started_at = time.monotonic()
    try:
        connection = mavutil.mavlink_connection(
            endpoint,
            source_system=source_system,
            source_component=source_component,
            autoreconnect=False,
        )
        collector = TelemetryCollector(connection)
        collector.wait_for_heartbeat(45.0)
        raw_heartbeat = (getattr(connection, "messages", {}) or {}).get("HEARTBEAT")
        if raw_heartbeat is None:
            raise VtolMissionAcceptanceError("raw PX4 HEARTBEAT identity is unavailable")
        _require_px4_source(raw_heartbeat, "HEARTBEAT")
        if int(getattr(connection, "target_system", 0)) != PX4_SYSTEM_ID:
            raise VtolMissionAcceptanceError("connected MAVLink vehicle identity is not PX4 1/1")
        collector.wait_for_gps_fix(25.0)
        collector.wait_for_navigation_settle()
        _wait_for_disarmed_ground_state(collector)
        live_context = _collect_live_context(collector)

        # Test-only PX4 parameters are scoped to the exact owned disposable
        # container.  They do not traverse the product runtime or a hardware link.
        for parameter, value in (
            ("COM_ARM_HFLT_CHK", "0"),
            ("COM_MODE_ARM_CHK", "0"),
            ("CBRK_VTOLARMING", "22027"),
            ("NAV_DLL_ACT", "0"),
        ):
            result = _run(
                docker,
                [
                    "exec",
                    expected_container_id,
                    "/opt/px4-gazebo/bin/px4-param",
                    "set",
                    parameter,
                    value,
                ],
                timeout_s=10.0,
                check=True,
            )
            parameter_overrides[parameter] = {
                "value": value,
                "stdout": result.stdout.strip(),
                "scope": "exact owned disposable combined PX4/Gazebo SITL only",
            }

        now_utc = datetime.now(timezone.utc)
        documents = build_acceptance_documents(
            runtime_input=runtime_input,
            live_telemetry=live_context,
            container_id=expected_container_id,
            airframe_profile_hash=airframe_hash,
            runtime_descriptor_hash=descriptor_hash,
            runtime_provenance_hash=provenance_hash,
            now_utc=now_utc,
            maximum_duration_s=mission_timeout_s,
        )
        transaction = Px4MissionTransaction(
            transaction_id=documents.operator_grant.transaction_id,
            compiled=documents.compiled,
            validation_report=documents.validation_report,
            operator_grant=documents.operator_grant,
            bindings=documents.bindings,
            now_utc=now_utc,
        )
        mission_journal = DurableMissionJournal.create(
            resolved_journal_path,
            identity=transaction,
            now_utc=now_utc,
        )
        transaction = PersistentPx4MissionTransaction(transaction, mission_journal)
        transaction_events.append(
            {"state": transaction.state.value, "at_s": round(time.monotonic() - started_at, 6)}
        )
        upload_plan = transaction.begin_upload()
        transaction_events.append(
            {"state": transaction.state.value, "at_s": round(time.monotonic() - started_at, 6)}
        )
        upload_evidence = upload_int_only_mission(connection, transaction, upload_plan)
        transaction_events.append(
            {"state": transaction.state.value, "at_s": round(time.monotonic() - started_at, 6)}
        )
        readback_evidence = readback_int_only_mission(connection, transaction, upload_plan)
        transaction_events.append(
            {"state": transaction.state.value, "at_s": round(time.monotonic() - started_at, 6)}
        )
        committed_hash = transaction.commit()
        transaction_events.append(
            {"state": transaction.state.value, "at_s": round(time.monotonic() - started_at, 6)}
        )
        permit = transaction.permit_start()
        transaction_events.append(
            {"state": transaction.state.value, "at_s": round(time.monotonic() - started_at, 6)}
        )
        protocol = {
            "upload_acknowledged": bool(upload_evidence["acknowledged"]),
            "readback_hash_match": bool(readback_evidence["hash_match"]),
            "mission_item_count": upload_plan.item_count,
            "int_only": bool(upload_evidence["int_only"]),
            "mission_type": MAV_MISSION_TYPE_MISSION,
            "uploaded_protocol_sha256": upload_plan.protocol_hash,
            "readback_protocol_sha256": readback_evidence["readback_protocol_sha256"],
            "committed_protocol_sha256": committed_hash,
            "request_sequences": upload_evidence["request_sequences"],
            "request_message_types": upload_evidence["request_message_types"],
            "upload_ignored_foreign_target_messages": upload_evidence[
                "ignored_foreign_target_messages"
            ],
            "readback_sequences": readback_evidence["received_sequences"],
            "readback_message_types": readback_evidence["received_message_types"],
            "readback_ignored_foreign_target_messages": readback_evidence[
                "ignored_foreign_target_messages"
            ],
            "wire_items": [item.to_mapping() for item in upload_plan.items],
        }

        # Exact mode first, then ARM; both are downstream of permit_start.
        connection.mav.set_mode_send(*permit.to_set_mode_args())
        mode_evidence = _wait_for_exact_auto_mission(collector)
        control_command_events.append(
            {
                "message": "SET_MODE",
                "mode": "PX4_AUTO.MISSION",
                "base_mode": MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                "custom_mode": PX4_AUTO_MISSION_CUSTOM_MODE,
                "confirmed": True,
                "after_permit": True,
            }
        )
        flight["auto_mission_confirmed"] = True
        arm_result = _send_command_long_exact(
            collector,
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
            1.0,
            21196.0,
        )
        _wait_for_armed(collector)
        control_command_events.append(
            {
                "command": "MAV_CMD_COMPONENT_ARM_DISARM",
                "command_id": int(mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM),
                "arm": True,
                "force_magic": 21196,
                "result": arm_result,
                "after_permit": True,
            }
        )
        flight["armed_after_permit"] = True
        flight = observe_mission_flight(
            collector,
            item_count=upload_plan.item_count,
            home_latitude_deg=float(live_context["latitude_deg"]),
            home_longitude_deg=float(live_context["longitude_deg"]),
            mission_timeout_s=mission_timeout_s,
            mission_active_ready_file=mission_active_ready_file,
            container_id=expected_container_id,
            transaction_state=transaction.state.value,
            control_command_events=control_command_events,
            flight_capture_ready_file=flight_capture_ready_file,
            flight_capture_complete_file=flight_capture_complete_file,
            transaction_id=documents.operator_grant.transaction_id,
            committed_protocol_sha256=committed_hash,
            scenario_id=runtime_input.scenario_id,
            scenario_manifest_hash=runtime_input.scenario_manifest_hash,
        )
        flight["mode_confirmation"] = mode_evidence
        if transaction.state is not MissionTransactionState.START_PERMITTED:
            raise VtolMissionAcceptanceError("transaction state changed after start permit")
        if not (
            protocol["upload_acknowledged"]
            and protocol["readback_hash_match"]
            and protocol["int_only"]
            and flight["auto_mission_confirmed"]
            and flight["armed_after_permit"]
            and flight["in_air_observed"]
            and flight["fixed_wing_observed"]
            and flight["multicopter_observed"]
            and flight["landed_observed"]
            and flight["terminal_disarmed"]
            and (flight_capture_ready_file is None or flight["flight_capture_ready_written"])
            and (
                flight_capture_complete_file is None or flight["flight_capture_interval_qualified"]
            )
        ):
            raise VtolMissionAcceptanceError("one or more mission acceptance gates are false")
        gate_passed = True
    except Exception as exc:  # noqa: BLE001 - evidence every fail-closed adapter error
        failure = f"{type(exc).__name__}: {exc}"
        gate_passed = False
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        if mission_journal is not None:
            try:
                mission_journal.close()
            except Exception:
                pass

    transaction_final = transaction.state.value if transaction is not None else "not_created"
    if transaction is not None and (
        not transaction_events or transaction_events[-1]["state"] != transaction.state.value
    ):
        transaction_events.append(
            {
                "state": transaction.state.value,
                "at_s": round(time.monotonic() - started_at, 6),
                "failure_transition": True,
            }
        )
    payload: dict[str, Any] = {
        "schema": "multidetect.px4-vtol-sitl-mission-acceptance",
        "schema_version": 1,
        "generated_at_utc": _utc_now(),
        "gate_passed": gate_passed,
        "failure": failure,
        "release_status": RELEASE_STATUS,
        "scenario": {
            "manifest": str(source),
            "manifest_sha256": _sha256_path(source),
            "scenario_id": runtime_input.scenario_id,
            "scenario_version": runtime_input.scenario_version,
            "runtime_input_sha256": runtime_input.canonical_hash,
            "truth_loaded_by_runtime": False,
            "expectations_loaded_by_runtime": False,
            "mission_geometry_source": "qualified live GPS home plus bounded offsets",
        },
        "container": {
            "name": container_name,
            "id": expected_container_id,
            "purpose_label": expected_container_label,
            "ownership_checks": ownership_checks,
            "lifecycle_owner": "scripts/run_vtol_mission_sensor_capture.py",
            "created_by_this_helper": False,
            "stopped_by_this_helper": False,
        },
        "live_context": live_context,
        "documents": _document_evidence(documents) if documents is not None else None,
        "transaction": {
            "id": (documents.operator_grant.transaction_id if documents is not None else None),
            "final_state": transaction_final,
            "state_events": transaction_events,
            "denial_reason": transaction.denial_reason if transaction is not None else None,
            "test_only_acceptance_fixture_grant": True,
            "human_operator_approval_claimed": False,
        },
        "mission_journal": (
            {
                "path": str(resolved_journal_path),
                "exists": resolved_journal_path.exists(),
                "recovery_required": mission_journal.recovery_required,
                "recovery_reason": mission_journal.recovery_reason,
                "state": mission_journal.state.value,
                "record_count": len(mission_journal.records),
                "requested_sequences": list(mission_journal.requested_sequences),
                "identity_hash": mission_journal.identity.canonical_hash,
                "resume_plan": mission_journal.resume_plan().to_mapping(),
                "file_sha256": _sha256_path(resolved_journal_path),
            }
            if mission_journal is not None
            else {
                "path": str(resolved_journal_path),
                "exists": resolved_journal_path.exists(),
                "recovery_required": False,
                "recovery_reason": None,
                "state": "not_created",
                "record_count": 0,
                "requested_sequences": [],
                "identity_hash": None,
                "resume_plan": None,
                "file_sha256": (
                    _sha256_path(resolved_journal_path) if resolved_journal_path.is_file() else None
                ),
            }
        ),
        "protocol": protocol,
        "flight": flight,
        "mavlink": {
            "endpoint": endpoint,
            "loopback_only": True,
            "source_system": source_system,
            "source_component": source_component,
            "target_system": PX4_SYSTEM_ID,
            "target_component": PX4_AUTOPILOT_COMPONENT_ID,
            "control_command_events": control_command_events,
            "protected_udp_14550_used": False,
            "channel": "test-only combined-SIM evaluator",
        },
        "parameter_overrides": parameter_overrides,
        "safety": {
            "operator_visual_acceptance": "not_claimed",
            "qgc_fly_view_visual_acceptance": "not_claimed",
            "ground_truth_used_for_mission": False,
            "expectations_used_for_mission": False,
            "test_only_acceptance_fixture_grant": True,
            "human_operator_approval_claimed": False,
            "product_runtime_flight_control_enabled": False,
            "product_runtime_actuator_output_enabled": False,
            "real_pixhawk_contacted": False,
            "real_jetson_contacted": False,
            "real_camera_contacted": False,
            "hardware_control_enabled": False,
            "container_lifecycle_changed_by_helper": False,
        },
        "telemetry_tail": collector.messages[-300:] if collector is not None else [],
    }
    _atomic_write_json(evidence_path, payload, overwrite=False)
    print(json.dumps(payload, sort_keys=True))
    if not gate_passed:
        raise SystemExit(1)
    return payload


def _resolve_docker_cli(explicit: str | None) -> str:
    if explicit is None:
        return _docker_path()
    token = explicit.strip()
    if not token:
        raise VtolMissionAcceptanceError("--docker must not be empty")
    candidate = Path(token).expanduser()
    if candidate.is_absolute():
        resolved = candidate.resolve(strict=False)
        if not resolved.is_file():
            raise VtolMissionAcceptanceError(f"Docker CLI does not exist: {resolved}")
        return str(resolved)
    found = shutil.which(token)
    if found is None:
        raise VtolMissionAcceptanceError(f"Docker CLI is not available on PATH: {token}")
    return found


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario-manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--container-name", required=True)
    parser.add_argument("--expected-container-id", required=True)
    parser.add_argument("--expected-container-label", default=EXPECTED_CONTAINER_LABEL)
    parser.add_argument("--evaluator-port", type=int, required=True)
    parser.add_argument("--airframe-profile-sha256", required=True)
    parser.add_argument("--runtime-descriptor-sha256", required=True)
    parser.add_argument("--runtime-provenance-sha256", required=True)
    parser.add_argument("--mission-active-ready-file", type=Path)
    parser.add_argument("--flight-capture-ready-file", type=Path)
    parser.add_argument("--flight-capture-complete-file", type=Path)
    parser.add_argument(
        "--docker",
        help="exact Docker CLI selected by the parent lifecycle owner",
    )
    parser.add_argument(
        "--mission-journal",
        type=Path,
        help="path for the append-only PX4 Mission transaction journal",
    )
    parser.add_argument("--mission-timeout-seconds", type=float, default=DEFAULT_MISSION_TIMEOUT_S)
    parser.add_argument("--source-system", type=int, default=GCS_SOURCE_SYSTEM)
    parser.add_argument("--source-component", type=int, default=GCS_SOURCE_COMPONENT)
    parser.add_argument("--acknowledge-owned-disposable-sitl", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    run_mission_acceptance(
        args.scenario_manifest.resolve(),
        args.evidence.resolve(),
        docker=_resolve_docker_cli(args.docker),
        container_name=args.container_name,
        expected_container_id=args.expected_container_id,
        expected_container_label=args.expected_container_label,
        evaluator_port=args.evaluator_port,
        airframe_profile_sha256=args.airframe_profile_sha256,
        runtime_descriptor_sha256=args.runtime_descriptor_sha256,
        runtime_provenance_sha256=args.runtime_provenance_sha256,
        mission_active_ready_file=(
            args.mission_active_ready_file.resolve()
            if args.mission_active_ready_file is not None
            else None
        ),
        flight_capture_ready_file=(
            args.flight_capture_ready_file.resolve()
            if args.flight_capture_ready_file is not None
            else None
        ),
        flight_capture_complete_file=(
            args.flight_capture_complete_file.resolve()
            if args.flight_capture_complete_file is not None
            else None
        ),
        mission_journal_path=(
            args.mission_journal.resolve() if args.mission_journal is not None else None
        ),
        mission_timeout_s=args.mission_timeout_seconds,
        source_system=args.source_system,
        source_component=args.source_component,
        acknowledge_owned_disposable_sitl=args.acknowledge_owned_disposable_sitl,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
