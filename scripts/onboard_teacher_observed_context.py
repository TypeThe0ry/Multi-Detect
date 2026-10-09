"""Source-qualified, grounded x500 context from the NEW filtered raw capture.

No receive/cache fallback, default GPS, synthesized pose or endurance override.
BATTERY_STATUS.time_remaining must be a positive actual reported estimate;
shared planner reserve/margin is still deducted separately. GPS eph is unitless
HDOP, NOT horizontal accuracy in metres: only the actual h_acc mm field is used.
SIM-configured wind and caller-observed resource flags are explicitly bound,
not relabelled as measured wind or authenticated authority. This module reads
small evidence files only and creates no Grant, Permit, command, Actor or label.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from multidetect.autonomy.contracts import ContextSnapshot, GeoPoint
from multidetect.autonomy.onboard_executed_trajectory import EvidenceBinding
from multidetect.autonomy.serialization import (
    canonical_json,
    canonical_sha256,
    require_array,
    require_aware_timestamp,
    require_bool,
    require_int,
    require_real,
    require_schema_version,
    require_sha256,
    require_text,
    strict_json_loads,
    strict_object,
)
from multidetect.geolocation_accuracy import wgs84_distance_m
from multidetect.simulator.stack import airframe_profile_hash
from scripts.onboard_teacher_telemetry_capture import FALSE_CLAIMS as RAW_FALSE_CLAIMS

CONTRACT = "multidetect.onboard-teacher-observed-context-v1"
REQUIRED_MESSAGES = (
    "HEARTBEAT",
    "EXTENDED_SYS_STATE",
    "HOME_POSITION",
    "GPS_RAW_INT",
    "GLOBAL_POSITION_INT",
    "LOCAL_POSITION_NED",
    "BATTERY_STATUS",
    "SYS_STATUS",
    "VFR_HUD",
)
RESOURCE_FLAGS = (
    "geofence_valid",
    "camera_available",
    "perception_available",
    "map_cache_available",
    "dem_available",
)
MAXIMUM_AGE_S = 2.0
MAXIMUM_OBSERVER_SKEW_S = 1.0
MAXIMUM_BOOT_SKEW_MS = 500
MAXIMUM_GPS_ACCURACY_M = 2.0
MAXIMUM_HOME_OFFSET_M = 2.0
MAXIMUM_GROUND_SPEED_MPS = 0.3
MAXIMUM_RELATIVE_ALTITUDE_M = 0.5
MAXIMUM_SOURCE_BYTES = 65536
REQUIRED_SENSOR_BITS = 1 | 2 | 32 | 33554432  # gyro, accel, GPS, battery (MAVLink common)


def _identity(stat):
    return stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino


def _source_bytes(root, source, cap, observations):
    if type(source) is not EvidenceBinding:
        raise TypeError("source must be an EvidenceBinding")
    if source.bytes > cap:
        raise ValueError("evidence source exceeds bounded read cap")
    path = root / source.path
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
        raise ValueError("evidence source must be a repository-contained regular file")
    before = path.stat()
    if before.st_size != source.bytes:
        raise ValueError("evidence source byte size differs")
    with path.open("rb") as stream:
        raw = stream.read(cap + 1)
    after = path.stat()
    if (
        _identity(before) != _identity(after)
        or len(raw) != source.bytes
        or hashlib.sha256(raw).hexdigest() != source.sha256
    ):
        raise ValueError("evidence source identity/bytes/SHA differ")
    previous = observations.setdefault(source.path, _identity(after))
    if previous != _identity(after):
        raise ValueError("evidence source identity changed during context qualification")
    return raw


def _projection(root, source, observations):
    if source.json_pointer is None:
        raise ValueError("JSON evidence requires an explicit RFC6901 pointer")
    value = strict_json_loads(_source_bytes(root, source, MAXIMUM_SOURCE_BYTES, observations))
    for token in source.json_pointer.split("/")[1:] if source.json_pointer else ():
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, Mapping):
            if key not in value:
                raise ValueError("evidence JSON pointer is missing")
            value = value[key]
        elif type(value) is list and key.isdecimal() and str(int(key)) == key:
            index = int(key)
            if index >= len(value):
                raise ValueError("evidence JSON pointer index is missing")
            value = value[index]
        else:
            raise ValueError("evidence JSON pointer cannot be resolved")
    return value


def _capture_binding(root, value, kind, role):
    binding = strict_object(
        value, name=f"{kind}.{role}", required=frozenset({"path", "bytes", "sha256"})
    )
    path = Path(require_text(binding["path"], "capture binding path"))
    if not path.is_absolute() or not path.resolve().is_relative_to(root):
        raise ValueError("raw capture binding must have an absolute contained path")
    return EvidenceBinding(
        path.relative_to(root).as_posix(),
        binding["bytes"],
        binding["sha256"],
        f"teacher-raw-{kind}-{role}",
        "" if role == "envelope" else None,
    )


def _admit_row(root, row, kind, *, now, age, clock_id, transaction_id, origin, observations):
    value = strict_object(
        row,
        name=f"captured {kind}",
        required=frozenset(
            {
                "record_index",
                "received_at_s",
                "clock_id",
                "transaction_id",
                "local_origin_id",
                "system_id",
                "component_id",
                "packet_sequence",
                "packet_sha256",
                "source_accepted",
                "message",
                "claims",
                "dispatch_ready_at_s",
                "envelope",
                "packet",
            }
        ),
    )
    require_int(value["record_index"], "record_index", minimum=0, maximum=8191)
    require_int(value["packet_sequence"], "packet_sequence", minimum=0, maximum=255)
    for field, expected in (("system_id", 1), ("component_id", 1)):
        require_int(value[field], field, minimum=expected, maximum=expected)
    if value["source_accepted"] is not True:
        raise ValueError("foreign/unaccepted source cannot qualify context")
    for field, expected in (
        ("clock_id", clock_id),
        ("transaction_id", transaction_id),
        ("local_origin_id", origin),
    ):
        if value[field] != expected:
            raise ValueError(f"raw capture {field} mismatch")
    received = require_real(value["received_at_s"], "received_at_s", minimum=0)
    ready = require_real(value["dispatch_ready_at_s"], "dispatch_ready_at_s", minimum=received)
    if not 0 <= now - received <= age or ready > now:
        raise ValueError(f"{kind} is stale/future or not ready at qualification")
    if canonical_json(value["claims"]) != canonical_json(RAW_FALSE_CLAIMS):
        raise ValueError("raw capture claim scope was changed")
    envelope = _capture_binding(root, value["envelope"], kind, "envelope")
    packet = _capture_binding(root, value["packet"], kind, "packet")
    if envelope.bytes > 16384 or packet.bytes > 4096:
        raise ValueError("raw capture source exceeds original bounded size")
    saved = _projection(root, envelope, observations)
    projected = {
        k: v for k, v in value.items() if k not in {"dispatch_ready_at_s", "envelope", "packet"}
    }
    if canonical_json(saved) != canonical_json(projected):
        raise ValueError("raw capture projection differs from persisted envelope")
    _source_bytes(root, packet, 4096, observations)
    if require_sha256(value["packet_sha256"], "packet_sha256") != packet.sha256:
        raise ValueError("raw capture packet SHA differs from envelope")
    message = value["message"]
    if not isinstance(message, Mapping) or any(
        message.get(key) != expected
        for key, expected in (
            ("mavpackettype", kind),
            ("message_type", kind),
            ("source_system_id", 1),
            ("source_component_id", 1),
        )
    ):
        raise ValueError("decoded message kind/source differs from qualified envelope")
    require_int(message["source_system_id"], "message source system", minimum=1, maximum=1)
    require_int(message["source_component_id"], "message source component", minimum=1, maximum=1)
    return dict(message), received, (envelope, packet)


def _integer(message, field, low, high):
    if field not in message:
        raise ValueError(f"required telemetry field {field} is missing")
    return require_int(message[field], field, minimum=low, maximum=high)


def _number(message, field, low=-1e6, high=1e6):
    if field not in message:
        raise ValueError(f"required telemetry field {field} is missing")
    return require_real(message[field], field, minimum=low, maximum=high)


def _geo(message, lat="lat", lon="lon"):
    return GeoPoint(
        _integer(message, lat, -900000000, 900000000) / 1e7,
        _integer(message, lon, -1800000000, 1800000000) / 1e7,
    )


def _distance(a, b):
    return wgs84_distance_m(
        latitude1_deg=a.latitude_deg,
        longitude1_deg=a.longitude_deg,
        latitude2_deg=b.latitude_deg,
        longitude2_deg=b.longitude_deg,
    )


@dataclass(frozen=True, slots=True)
class ObservedTeacherContext:
    """Read-qualified telemetry + source-bound resource declarations, no authority."""

    context: ContextSnapshot
    source_references: tuple[EvidenceBinding, ...]
    _details_json: str

    @property
    def qualification_details(self):
        return strict_json_loads(self._details_json)

    def to_mapping(self):
        return {
            "contract": CONTRACT,
            "schema_version": 1,
            "context": self.context.to_mapping(),
            "context_sha256": self.context.canonical_hash,
            "source_references": [s.to_mapping() for s in self.source_references],
            "qualification_details": self.qualification_details,
            "claims": {
                name: False
                for name in (
                    "source_authenticated",
                    "decoded_projection_independently_verified",
                    "airframe_identity_observed_on_vehicle",
                    "resource_issuer_authenticated",
                    "wind_measured",
                    "UTC_clock_mapping_independently_verified",
                    "EKF_origin_mapping_verified",
                    "route_clearance_verified",
                    "mission_approved",
                    "mission_submitted",
                    "mission_executed",
                    "teacher_expert_verified",
                    "train_admitted",
                    "execution_authority",
                    "hardware_control_enabled",
                    "final_release_allowed",
                )
            },
        }

    @property
    def canonical_hash(self):
        return canonical_sha256(self.to_mapping())


def build_observed_teacher_context(
    rows,
    *,
    artifact_root,
    now_s,
    captured_at_utc,
    clock_id,
    transaction_id,
    local_origin_id,
    profile_source,
    profile_id,
    resource_source,
    maximum_age_s=MAXIMUM_AGE_S,
):
    """Read actual capture bindings then qualify a grounded/disarmed plain x500.

    ``rows`` maps all REQUIRED_MESSAGES to detached NEW raw capture return/latest
    rows. ``profile_source`` points at the stable stack aircraft projection;
    ``resource_source`` points at explicit same-session resource/wind observations.
    The caller provides a UTC anchor corresponding to ``now_s``; no external clock
    synchronization is claimed. Missing/unknown battery remaining time rejects.
    """
    if not isinstance(rows, Mapping) or set(rows) != set(REQUIRED_MESSAGES):
        raise ValueError("exact required raw message set is incomplete or contains unknown entries")
    root = Path(artifact_root).resolve()
    if not root.is_dir():
        raise ValueError("existing artifact_root required")
    now = require_real(now_s, "now_s", minimum=0)
    age = require_real(maximum_age_s, "maximum_age_s", minimum=0.01, maximum=MAXIMUM_AGE_S)
    clock_id = require_text(clock_id, "clock_id")
    transaction_id = require_text(transaction_id, "transaction_id")
    origin = require_text(local_origin_id, "local_origin_id")
    utc = datetime.fromisoformat(
        require_aware_timestamp(captured_at_utc, "UTC anchor").replace("Z", "+00:00")
    )
    observations, messages, times, sources = {}, {}, {}, []
    for kind in REQUIRED_MESSAGES:
        message, received, bindings = _admit_row(
            root,
            rows[kind],
            kind,
            now=now,
            age=age,
            clock_id=clock_id,
            transaction_id=transaction_id,
            origin=origin,
            observations=observations,
        )
        messages[kind], times[kind] = message, received
        sources.extend(bindings)
    if len({rows[k]["record_index"] for k in REQUIRED_MESSAGES}) != len(REQUIRED_MESSAGES):
        raise ValueError("different telemetry kinds cannot share one raw record index")
    if max(times.values()) - min(times.values()) > MAXIMUM_OBSERVER_SKEW_S:
        raise ValueError("required telemetry observer-time skew exceeds qualification bound")
    profile = strict_object(
        _projection(root, profile_source, observations),
        name="airframe projection",
        required=frozenset(
            {
                "schema_version",
                "airframe_profile_id",
                "vehicle_model",
                "model_name",
                "stack_plan_sha256",
            }
        ),
    )
    require_schema_version(profile["schema_version"])
    require_sha256(profile["stack_plan_sha256"], "stack_plan_sha256")
    if (
        profile["airframe_profile_id"] != profile_id
        or profile["vehicle_model"] != "px4_multirotor_standard"
        or profile["model_name"] != "x500_mono_cam"
    ):
        raise ValueError("source-bound airframe projection is not the requested plain x500 profile")
    profile_hash = airframe_profile_hash(profile)
    resources = strict_object(
        _projection(root, resource_source, observations),
        name="observed resources",
        required=frozenset(
            {
                "clock_id",
                "transaction_id",
                "observed_at_s",
                "airframe_profile_hash",
                "wind_north_mps",
                "wind_east_mps",
                "wind_source",
                *RESOURCE_FLAGS,
            }
        ),
    )
    if resources["clock_id"] != clock_id or resources["transaction_id"] != transaction_id:
        raise ValueError("resource clock/transaction differs from captured telemetry")
    resource_at = require_real(resources["observed_at_s"], "resource observed_at_s", minimum=0)
    if not 0 <= now - resource_at <= age:
        raise ValueError("resource observations are stale/future")
    if resources["airframe_profile_hash"] != profile_hash:
        raise ValueError("resource airframe profile hash mismatch")
    if resources["wind_source"] != "software-SIM-configured":
        raise ValueError(
            "explicit software-SIM-configured wind source required, never default zero"
        )
    flags = {name: require_bool(resources[name], name) for name in RESOURCE_FLAGS}
    wind_north = _number(resources, "wind_north_mps", -100, 100)
    wind_east = _number(resources, "wind_east_mps", -100, 100)
    sources.extend((profile_source, resource_source))
    heartbeat = messages["HEARTBEAT"]
    if (_integer(heartbeat, "type", 0, 255), _integer(heartbeat, "autopilot", 0, 255)) != (2, 12):
        raise ValueError("ordinary QUADROTOR/PX4 HEARTBEAT is required, not VTOL lifecycle")
    if _integer(heartbeat, "base_mode", 0, 255) & 128:
        raise ValueError("context qualification requires actual disarmed HEARTBEAT")
    if _integer(heartbeat, "system_status", 0, 255) != 3:
        raise ValueError("context qualification requires actual STANDBY HEARTBEAT")
    extended = messages["EXTENDED_SYS_STATE"]
    if (
        _integer(extended, "landed_state", 0, 255) != 1
        or _integer(extended, "vtol_state", 0, 255) != 0
    ):
        raise ValueError("plain multirotor ON_GROUND/undefined VTOL state is required")
    status = messages["SYS_STATUS"]
    for field in (
        "onboard_control_sensors_present",
        "onboard_control_sensors_enabled",
        "onboard_control_sensors_health",
    ):
        if _integer(status, field, 0, 2**32 - 1) & REQUIRED_SENSOR_BITS != REQUIRED_SENSOR_BITS:
            raise ValueError("required gyro/accel/GPS/battery sensor status is not qualified")
    for field in (
        "errors_comm",
        "errors_count1",
        "errors_count2",
        "errors_count3",
        "errors_count4",
    ):
        if _integer(status, field, 0, 65535):
            raise ValueError("reported SYS_STATUS error counters prevent qualification")
    gps, global_position, local = (
        messages[k] for k in ("GPS_RAW_INT", "GLOBAL_POSITION_INT", "LOCAL_POSITION_NED")
    )
    fix = _integer(gps, "fix_type", 3, 8)
    satellites = _integer(gps, "satellites_visible", 6, 254)
    _integer(
        gps, "time_usec", 1, 2**64 - 1
    )  # preserved opaque; do not subtract from host or boot time
    accuracy = _integer(gps, "h_acc", 1, 2**32 - 2) / 1000
    if accuracy > MAXIMUM_GPS_ACCURACY_M:
        raise ValueError("actual GPS h_acc exceeds horizontal qualification bound")
    gps_point, global_point = _geo(gps), _geo(global_position)
    home_message = messages["HOME_POSITION"]
    home = _geo(home_message, "latitude", "longitude")
    disagreement = _distance(gps_point, global_point)
    home_offset = _distance(global_point, home)
    if disagreement > MAXIMUM_HOME_OFFSET_M or home_offset > MAXIMUM_HOME_OFFSET_M:
        raise ValueError("actual GPS/global/home positions disagree beyond ground bound")
    global_boot = _integer(global_position, "time_boot_ms", 1, 2**32 - 1)
    local_boot = _integer(local, "time_boot_ms", 1, 2**32 - 1)
    if abs(global_boot - local_boot) > MAXIMUM_BOOT_SKEW_MS:
        raise ValueError("LOCAL/GLOBAL boot-time skew prevents qualification")
    relative_altitude = _integer(global_position, "relative_alt", -(2**31), 2**31 - 1) / 1000
    home_altitude = _integer(home_message, "altitude", -(2**31), 2**31 - 1) / 1000
    global_altitude = _integer(global_position, "alt", -(2**31), 2**31 - 1) / 1000
    gps_altitude = _integer(gps, "alt", -(2**31), 2**31 - 1) / 1000
    if (
        abs(relative_altitude) > MAXIMUM_RELATIVE_ALTITUDE_M
        or abs(global_altitude - home_altitude - relative_altitude) > 1
        or abs(gps_altitude - global_altitude) > 3
    ):
        raise ValueError("observed global/GPS/home altitude does not qualify grounded context")
    xyz = tuple(_number(local, k) for k in ("x", "y", "z"))
    home_xyz = tuple(_number(home_message, k) for k in ("x", "y", "z"))
    if (
        math.hypot(xyz[0] - home_xyz[0], xyz[1] - home_xyz[1]) > MAXIMUM_HOME_OFFSET_M
        or abs(xyz[2] - home_xyz[2] + relative_altitude) > MAXIMUM_RELATIVE_ALTITUDE_M
    ):
        raise ValueError("actual local/home NED position does not qualify grounded context")
    local_velocity = tuple(_number(local, k) for k in ("vx", "vy", "vz"))
    global_velocity = tuple(
        _integer(global_position, k, -32768, 32767) / 100 for k in ("vx", "vy", "vz")
    )
    if (
        math.sqrt(sum(v * v for v in local_velocity)) > MAXIMUM_GROUND_SPEED_MPS
        or math.sqrt(sum(v * v for v in global_velocity)) > MAXIMUM_GROUND_SPEED_MPS
    ):
        raise ValueError("actual LOCAL/GLOBAL velocity does not qualify ground state")
    vfr = messages["VFR_HUD"]
    ground_speed = _number(vfr, "groundspeed", 0, MAXIMUM_GROUND_SPEED_MPS)
    airspeed = _number(vfr, "airspeed", 0, 100)  # no inherited negative-speed repair/clamp
    battery = messages["BATTERY_STATUS"]
    if _integer(battery, "id", 0, 255) != 0 or _integer(battery, "battery_function", 0, 255) != 1:
        raise ValueError("primary all-flight-systems battery id=0 is required")
    if (
        _integer(battery, "charge_state", 0, 255) != 1
        or _integer(battery, "fault_bitmask", 0, 2**32 - 1) != 0
    ):
        raise ValueError("battery charge/fault status is not qualified")
    remaining = _integer(battery, "battery_remaining", 1, 100)
    status_remaining = _integer(status, "battery_remaining", 1, 100)
    if abs(remaining - status_remaining) > 5:
        raise ValueError("BATTERY_STATUS/SYS_STATUS remaining percentages disagree")
    seconds = _integer(battery, "time_remaining", -(2**31), 2**31 - 1)
    if not 1 <= seconds <= 604800:
        raise ValueError(
            "BATTERY_STATUS.time_remaining requires a positive bounded estimate; no override"
        )
    voltages = require_array(battery.get("voltages"), "battery voltages")
    if len(voltages) != 10:
        raise ValueError("actual ten-cell/total battery voltage field is required")
    millivolts = tuple(
        require_int(v, "battery cell mV", minimum=1, maximum=65535) for v in voltages
    )
    if millivolts[0] == 65535:
        raise ValueError("primary/total battery voltage is unavailable")
    _integer(battery, "current_battery", 0, 32767)
    endurance = seconds - (now - times["BATTERY_STATUS"])
    if endurance <= 0:
        raise ValueError("reported remaining battery time expired; no endurance override")
    oldest = min(*times.values(), resource_at)
    last_record_index = max(rows[k]["record_index"] for k in REQUIRED_MESSAGES)
    context = ContextSnapshot(
        snapshot_id=f"{transaction_id}:source-qualified:{last_record_index}",
        captured_at_s=oldest,
        captured_at_utc=(utc - timedelta(seconds=now - oldest)).isoformat(),
        aircraft_position=GeoPoint(
            global_point.latitude_deg,
            global_point.longitude_deg,
            relative_altitude,
            "relative_home",
        ),
        home_position=home,
        airframe_profile_id=profile_id,
        airframe_profile_hash=profile_hash,
        battery_remaining_pct=remaining,
        estimated_flight_time_s=endurance,
        ground_speed_mps=ground_speed,
        airspeed_mps=airspeed,
        wind_north_mps=wind_north,
        wind_east_mps=wind_east,
        gps_fix_type=fix,
        gps_horizontal_accuracy_m=accuracy,
        **flags,
    )
    # Qualification is provisional until every source still matches after all derivation.
    for source in sources:
        _source_bytes(root, source, MAXIMUM_SOURCE_BYTES, observations)
    details = {
        "source_file_bytes_SHA_verified": True,
        "source_identity_before_after_equal": True,
        "accepted_system_component": [1, 1],
        "clock_id": clock_id,
        "transaction_id": transaction_id,
        "local_origin_id": origin,
        "message_received_at_s": times,
        "oldest_observation_at_s": oldest,
        "resource_observed_at_s": resource_at,
        "maximum_age_s": age,
        "utc_anchor": {"observer_at_s": now, "utc": utc.isoformat(), "source": "caller UTC anchor"},
        "qualified_plain_MC_disarmed_on_ground": True,
        "gps_accuracy_source": "GPS_RAW_INT.h_acc_mm",
        "satellites_visible": satellites,
        "gps_global_disagreement_m": disagreement,
        "global_home_offset_m": home_offset,
        "local_global_boot_skew_ms": abs(global_boot - local_boot),
        "endurance_source": "BATTERY_STATUS.time_remaining_seconds",
        "reported_time_remaining_s": seconds,
        "conservative_receive_age_deduction_s": now - times["BATTERY_STATUS"],
        "endurance_reserve_already_deducted": False,
        "wind_source": resources["wind_source"],
        "resource_flags": flags,
        "resource_semantics": "source-bound caller observations; no issuer authority",
        "limitations": (
            "snapshot not exact simultaneity, sender authentication, "
            "EKF-origin proof or flight clearance"
        ),
    }
    return ObservedTeacherContext(context, tuple(sources), canonical_json(details))
