"""Versioned, immutable documents crossing the autonomy trust boundary.

Every document rejects unknown fields and hashes its canonical JSON form.  An
operator grant can therefore bind the exact prompt, context, task, plan,
executable mission, validation report, airframe, geofence, vehicle identity
and runtime descriptor instead of authorising a mutable in-memory object or
crossing a SIM/REAL boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..compat import StrEnum
from ..mavlink_gps import MAVLINK_GPS_FIX_TYPE_MAX
from .serialization import (
    AutonomyContractError,
    canonical_json,
    canonical_sha256,
    optional_real,
    optional_text,
    require_array,
    require_aware_timestamp,
    require_bool,
    require_int,
    require_real,
    require_schema_version,
    require_sha256,
    require_string_array,
    require_text,
    strict_object,
)


class TaskType(StrEnum):
    AREA_SEARCH = "area_search"
    CORRIDOR_VISUAL_SEARCH = "corridor_visual_search"
    TARGET_OBSERVATION = "target_observation"
    SURVEY = "survey"


class PlanAction(StrEnum):
    TAKEOFF = "takeoff"
    TRANSIT = "transit"
    SEARCH_AREA = "search_area"
    SEARCH_CORRIDOR = "search_corridor"
    SURVEY = "survey"
    OBSERVE_TARGET = "observe_target"
    CAPTURE_EVIDENCE = "capture_evidence"
    REPORT_GEOLOCATION = "report_geolocation"
    REVISIT = "revisit"
    RETURN = "return"
    DIVERT = "divert"
    LAND = "land"


class ValidationVerdict(StrEnum):
    PASS = "pass"
    DENY = "deny"


class EpisodeOutcome(StrEnum):
    COMPLETED = "completed"
    SAFE_RTL = "safe_rtl"
    ABORTED = "aborted"
    FAILED = "failed"


class MissionCommand(StrEnum):
    """Fail-closed v1 mission command allow-list.

    The list intentionally contains navigation/lifecycle commands only.  It
    excludes arm/disarm, servo/actuator, parameter and arbitrary ``DO_*``
    commands; those cannot enter an ``ExecutableMission`` contract.
    """

    TAKEOFF = "MAV_CMD_NAV_TAKEOFF"
    VTOL_TAKEOFF = "MAV_CMD_NAV_VTOL_TAKEOFF"
    WAYPOINT = "MAV_CMD_NAV_WAYPOINT"
    LOITER_TIME = "MAV_CMD_NAV_LOITER_TIME"
    LOITER_TO_ALT = "MAV_CMD_NAV_LOITER_TO_ALT"
    RETURN_TO_LAUNCH = "MAV_CMD_NAV_RETURN_TO_LAUNCH"
    LAND = "MAV_CMD_NAV_LAND"
    VTOL_LAND = "MAV_CMD_NAV_VTOL_LAND"
    VTOL_TRANSITION = "MAV_CMD_DO_VTOL_TRANSITION"


class MissionFrame(StrEnum):
    GLOBAL_INT = "MAV_FRAME_GLOBAL_INT"
    GLOBAL_RELATIVE_ALT_INT = "MAV_FRAME_GLOBAL_RELATIVE_ALT_INT"
    GLOBAL_TERRAIN_ALT_INT = "MAV_FRAME_GLOBAL_TERRAIN_ALT_INT"
    MISSION = "MAV_FRAME_MISSION"


_COORDINATE_MISSION_COMMANDS = frozenset(
    {
        MissionCommand.TAKEOFF,
        MissionCommand.VTOL_TAKEOFF,
        MissionCommand.WAYPOINT,
        MissionCommand.LOITER_TIME,
        MissionCommand.LOITER_TO_ALT,
        MissionCommand.LAND,
        MissionCommand.VTOL_LAND,
    }
)
_GLOBAL_MISSION_FRAMES = frozenset(
    {
        MissionFrame.GLOBAL_INT,
        MissionFrame.GLOBAL_RELATIVE_ALT_INT,
        MissionFrame.GLOBAL_TERRAIN_ALT_INT,
    }
)


def _typed_tuple(
    value: object,
    name: str,
    item_type: type[Any],
    *,
    minimum_items: int = 0,
) -> tuple[Any, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise AutonomyContractError(f"{name} must be an array")
    result = tuple(value)
    if len(result) < minimum_items:
        raise AutonomyContractError(f"{name} must contain at least {minimum_items} item(s)")
    if any(not isinstance(item, item_type) for item in result):
        raise AutonomyContractError(f"{name} must contain only {item_type.__name__} values")
    return result


class _CanonicalDocument:
    __slots__ = ()

    def to_mapping(self) -> dict[str, Any]:
        raise NotImplementedError

    @property
    def canonical_json(self) -> str:
        return canonical_json(self.to_mapping())

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @property
    def canonical_sha256(self) -> str:
        return self.canonical_hash

    @property
    def sha256(self) -> str:
        return self.canonical_hash


@dataclass(frozen=True, slots=True)
class GeoPoint(_CanonicalDocument):
    latitude_deg: float
    longitude_deg: float
    altitude_m: float | None = None
    altitude_reference: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "latitude_deg",
            require_real(self.latitude_deg, "latitude_deg", minimum=-90.0, maximum=90.0),
        )
        object.__setattr__(
            self,
            "longitude_deg",
            require_real(self.longitude_deg, "longitude_deg", minimum=-180.0, maximum=180.0),
        )
        object.__setattr__(self, "altitude_m", optional_real(self.altitude_m, "altitude_m"))
        if (self.altitude_m is None) != (self.altitude_reference is None):
            raise AutonomyContractError(
                "altitude_m and altitude_reference must be supplied together"
            )
        if self.altitude_reference is not None:
            object.__setattr__(
                self,
                "altitude_reference",
                require_text(self.altitude_reference, "altitude_reference"),
            )

    @classmethod
    def from_mapping(cls, raw: object) -> GeoPoint:
        value = strict_object(
            raw,
            name="GeoPoint",
            required=frozenset({"latitude_deg", "longitude_deg"}),
            optional=frozenset({"altitude_m", "altitude_reference"}),
        )
        return cls(
            latitude_deg=require_real(
                value["latitude_deg"], "latitude_deg", minimum=-90.0, maximum=90.0
            ),
            longitude_deg=require_real(
                value["longitude_deg"], "longitude_deg", minimum=-180.0, maximum=180.0
            ),
            altitude_m=optional_real(value.get("altitude_m"), "altitude_m"),
            altitude_reference=optional_text(value.get("altitude_reference"), "altitude_reference"),
        )

    def to_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "latitude_deg": self.latitude_deg,
            "longitude_deg": self.longitude_deg,
        }
        if self.altitude_m is not None:
            result["altitude_m"] = self.altitude_m
            result["altitude_reference"] = self.altitude_reference
        return result


@dataclass(frozen=True, slots=True)
class PromptEnvelope(_CanonicalDocument):
    prompt_id: str
    text: str
    language: str
    issued_at: str
    operator_id: str
    aircraft_id: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", require_schema_version(self.schema_version))
        for field_name in ("prompt_id", "text", "language", "operator_id", "aircraft_id"):
            object.__setattr__(
                self,
                field_name,
                require_text(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "issued_at", require_aware_timestamp(self.issued_at, "issued_at"))

    @classmethod
    def from_mapping(cls, raw: object) -> PromptEnvelope:
        value = strict_object(
            raw,
            name="PromptEnvelope",
            required=frozenset(
                {
                    "schema_version",
                    "prompt_id",
                    "text",
                    "language",
                    "issued_at",
                    "operator_id",
                    "aircraft_id",
                }
            ),
        )
        return cls(
            prompt_id=require_text(value["prompt_id"], "prompt_id"),
            text=require_text(value["text"], "text"),
            language=require_text(value["language"], "language"),
            issued_at=require_aware_timestamp(value["issued_at"], "issued_at"),
            operator_id=require_text(value["operator_id"], "operator_id"),
            aircraft_id=require_text(value["aircraft_id"], "aircraft_id"),
            schema_version=require_schema_version(value["schema_version"]),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "prompt_id": self.prompt_id,
            "text": self.text,
            "language": self.language,
            "issued_at": self.issued_at,
            "operator_id": self.operator_id,
            "aircraft_id": self.aircraft_id,
        }


@dataclass(frozen=True, slots=True)
class ContextSnapshot(_CanonicalDocument):
    snapshot_id: str
    captured_at_s: float
    captured_at_utc: str
    aircraft_position: GeoPoint
    home_position: GeoPoint
    airframe_profile_id: str
    battery_remaining_pct: float
    estimated_flight_time_s: float
    ground_speed_mps: float
    airspeed_mps: float
    wind_north_mps: float
    wind_east_mps: float
    gps_fix_type: int
    gps_horizontal_accuracy_m: float
    geofence_valid: bool
    camera_available: bool
    perception_available: bool
    map_cache_available: bool
    dem_available: bool
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", require_schema_version(self.schema_version))
        object.__setattr__(self, "snapshot_id", require_text(self.snapshot_id, "snapshot_id"))
        object.__setattr__(
            self,
            "captured_at_s",
            require_real(self.captured_at_s, "captured_at_s", minimum=0.0),
        )
        object.__setattr__(
            self,
            "captured_at_utc",
            require_aware_timestamp(self.captured_at_utc, "captured_at_utc"),
        )
        if not isinstance(self.aircraft_position, GeoPoint) or not isinstance(
            self.home_position, GeoPoint
        ):
            raise AutonomyContractError("context positions must be GeoPoint values")
        object.__setattr__(
            self,
            "airframe_profile_id",
            require_text(self.airframe_profile_id, "airframe_profile_id"),
        )
        object.__setattr__(
            self,
            "battery_remaining_pct",
            require_real(
                self.battery_remaining_pct,
                "battery_remaining_pct",
                minimum=0.0,
                maximum=100.0,
            ),
        )
        object.__setattr__(
            self,
            "estimated_flight_time_s",
            require_real(self.estimated_flight_time_s, "estimated_flight_time_s", minimum=0.0),
        )
        for name in (
            "ground_speed_mps",
            "airspeed_mps",
            "gps_horizontal_accuracy_m",
        ):
            object.__setattr__(self, name, require_real(getattr(self, name), name, minimum=0.0))
        for name in ("wind_north_mps", "wind_east_mps"):
            object.__setattr__(self, name, require_real(getattr(self, name), name))
        object.__setattr__(
            self,
            "gps_fix_type",
            require_int(
                self.gps_fix_type,
                "gps_fix_type",
                minimum=0,
                maximum=MAVLINK_GPS_FIX_TYPE_MAX,
            ),
        )
        for name in (
            "geofence_valid",
            "camera_available",
            "perception_available",
            "map_cache_available",
            "dem_available",
        ):
            require_bool(getattr(self, name), name)

    @classmethod
    def from_mapping(cls, raw: object) -> ContextSnapshot:
        names = {
            "schema_version",
            "snapshot_id",
            "captured_at_s",
            "captured_at_utc",
            "aircraft_position",
            "home_position",
            "airframe_profile_id",
            "battery_remaining_pct",
            "estimated_flight_time_s",
            "ground_speed_mps",
            "airspeed_mps",
            "wind_north_mps",
            "wind_east_mps",
            "gps_fix_type",
            "gps_horizontal_accuracy_m",
            "geofence_valid",
            "camera_available",
            "perception_available",
            "map_cache_available",
            "dem_available",
        }
        value = strict_object(raw, name="ContextSnapshot", required=frozenset(names))
        return cls(
            snapshot_id=require_text(value["snapshot_id"], "snapshot_id"),
            captured_at_s=require_real(value["captured_at_s"], "captured_at_s", minimum=0.0),
            captured_at_utc=require_aware_timestamp(value["captured_at_utc"], "captured_at_utc"),
            aircraft_position=GeoPoint.from_mapping(value["aircraft_position"]),
            home_position=GeoPoint.from_mapping(value["home_position"]),
            airframe_profile_id=require_text(value["airframe_profile_id"], "airframe_profile_id"),
            battery_remaining_pct=require_real(
                value["battery_remaining_pct"],
                "battery_remaining_pct",
                minimum=0.0,
                maximum=100.0,
            ),
            estimated_flight_time_s=require_real(
                value["estimated_flight_time_s"], "estimated_flight_time_s", minimum=0.0
            ),
            ground_speed_mps=require_real(
                value["ground_speed_mps"], "ground_speed_mps", minimum=0.0
            ),
            airspeed_mps=require_real(value["airspeed_mps"], "airspeed_mps", minimum=0.0),
            wind_north_mps=require_real(value["wind_north_mps"], "wind_north_mps"),
            wind_east_mps=require_real(value["wind_east_mps"], "wind_east_mps"),
            gps_fix_type=require_int(
                value["gps_fix_type"],
                "gps_fix_type",
                minimum=0,
                maximum=MAVLINK_GPS_FIX_TYPE_MAX,
            ),
            gps_horizontal_accuracy_m=require_real(
                value["gps_horizontal_accuracy_m"],
                "gps_horizontal_accuracy_m",
                minimum=0.0,
            ),
            geofence_valid=require_bool(value["geofence_valid"], "geofence_valid"),
            camera_available=require_bool(value["camera_available"], "camera_available"),
            perception_available=require_bool(
                value["perception_available"], "perception_available"
            ),
            map_cache_available=require_bool(value["map_cache_available"], "map_cache_available"),
            dem_available=require_bool(value["dem_available"], "dem_available"),
            schema_version=require_schema_version(value["schema_version"]),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "captured_at_s": self.captured_at_s,
            "captured_at_utc": self.captured_at_utc,
            "aircraft_position": self.aircraft_position.to_mapping(),
            "home_position": self.home_position.to_mapping(),
            "airframe_profile_id": self.airframe_profile_id,
            "battery_remaining_pct": self.battery_remaining_pct,
            "estimated_flight_time_s": self.estimated_flight_time_s,
            "ground_speed_mps": self.ground_speed_mps,
            "airspeed_mps": self.airspeed_mps,
            "wind_north_mps": self.wind_north_mps,
            "wind_east_mps": self.wind_east_mps,
            "gps_fix_type": self.gps_fix_type,
            "gps_horizontal_accuracy_m": self.gps_horizontal_accuracy_m,
            "geofence_valid": self.geofence_valid,
            "camera_available": self.camera_available,
            "perception_available": self.perception_available,
            "map_cache_available": self.map_cache_available,
            "dem_available": self.dem_available,
        }


@dataclass(frozen=True, slots=True)
class TaskGeometry(_CanonicalDocument):
    kind: str
    resolved_feature_id: str
    direction: str
    length_m: float
    centerline: tuple[GeoPoint, ...]
    resolution_confidence: float
    buffer_m: float = 0.0

    def __post_init__(self) -> None:
        for name in ("kind", "resolved_feature_id", "direction"):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        object.__setattr__(self, "length_m", require_real(self.length_m, "length_m", minimum=0.001))
        object.__setattr__(self, "buffer_m", require_real(self.buffer_m, "buffer_m", minimum=0.0))
        object.__setattr__(
            self,
            "resolution_confidence",
            require_real(
                self.resolution_confidence,
                "resolution_confidence",
                minimum=0.0,
                maximum=1.0,
            ),
        )
        object.__setattr__(
            self,
            "centerline",
            _typed_tuple(self.centerline, "centerline", GeoPoint, minimum_items=2),
        )

    @classmethod
    def from_mapping(cls, raw: object) -> TaskGeometry:
        value = strict_object(
            raw,
            name="TaskGeometry",
            required=frozenset(
                {
                    "kind",
                    "resolved_feature_id",
                    "direction",
                    "length_m",
                    "buffer_m",
                    "centerline",
                    "resolution_confidence",
                }
            ),
        )
        points = require_array(value["centerline"], "centerline")
        return cls(
            kind=require_text(value["kind"], "kind"),
            resolved_feature_id=require_text(value["resolved_feature_id"], "resolved_feature_id"),
            direction=require_text(value["direction"], "direction"),
            length_m=require_real(value["length_m"], "length_m", minimum=0.001),
            centerline=tuple(GeoPoint.from_mapping(point) for point in points),
            resolution_confidence=require_real(
                value["resolution_confidence"],
                "resolution_confidence",
                minimum=0.0,
                maximum=1.0,
            ),
            buffer_m=require_real(value["buffer_m"], "buffer_m", minimum=0.0),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "resolved_feature_id": self.resolved_feature_id,
            "direction": self.direction,
            "length_m": self.length_m,
            "buffer_m": self.buffer_m,
            "centerline": [point.to_mapping() for point in self.centerline],
            "resolution_confidence": self.resolution_confidence,
        }


@dataclass(frozen=True, slots=True)
class TaskConstraints(_CanonicalDocument):
    minimum_return_battery_pct: float
    maximum_duration_s: float
    remain_inside_geofence: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "minimum_return_battery_pct",
            require_real(
                self.minimum_return_battery_pct,
                "minimum_return_battery_pct",
                minimum=0.0,
                maximum=100.0,
            ),
        )
        object.__setattr__(
            self,
            "maximum_duration_s",
            require_real(self.maximum_duration_s, "maximum_duration_s", minimum=0.001),
        )
        require_bool(self.remain_inside_geofence, "remain_inside_geofence")

    @classmethod
    def from_mapping(cls, raw: object) -> TaskConstraints:
        value = strict_object(
            raw,
            name="TaskConstraints",
            required=frozenset(
                {
                    "minimum_return_battery_pct",
                    "maximum_duration_s",
                    "remain_inside_geofence",
                }
            ),
        )
        return cls(
            minimum_return_battery_pct=require_real(
                value["minimum_return_battery_pct"],
                "minimum_return_battery_pct",
                minimum=0.0,
                maximum=100.0,
            ),
            maximum_duration_s=require_real(
                value["maximum_duration_s"], "maximum_duration_s", minimum=0.001
            ),
            remain_inside_geofence=require_bool(
                value["remain_inside_geofence"], "remain_inside_geofence"
            ),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "minimum_return_battery_pct": self.minimum_return_battery_pct,
            "maximum_duration_s": self.maximum_duration_s,
            "remain_inside_geofence": self.remain_inside_geofence,
        }


@dataclass(frozen=True, slots=True)
class ResolvedTaskSpec(_CanonicalDocument):
    task_id: str
    task_type: TaskType
    geometry: TaskGeometry
    target_classes: tuple[str, ...]
    on_detection: tuple[PlanAction, ...]
    constraints: TaskConstraints
    completion_action: PlanAction
    unresolved_assumptions: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", require_schema_version(self.schema_version))
        object.__setattr__(self, "task_id", require_text(self.task_id, "task_id"))
        if not isinstance(self.task_type, TaskType):
            raise AutonomyContractError("task_type must be a TaskType")
        if not isinstance(self.geometry, TaskGeometry):
            raise AutonomyContractError("geometry must be a TaskGeometry")
        target_classes = require_string_array(self.target_classes, "target_classes")
        if not target_classes:
            raise AutonomyContractError("target_classes must not be empty")
        object.__setattr__(self, "target_classes", target_classes)
        object.__setattr__(
            self,
            "on_detection",
            _typed_tuple(self.on_detection, "on_detection", PlanAction),
        )
        if not isinstance(self.constraints, TaskConstraints):
            raise AutonomyContractError("constraints must be TaskConstraints")
        if self.completion_action not in {PlanAction.RETURN, PlanAction.LAND}:
            raise AutonomyContractError("completion_action must be return or land")
        object.__setattr__(
            self,
            "unresolved_assumptions",
            require_string_array(self.unresolved_assumptions, "unresolved_assumptions"),
        )

    @classmethod
    def from_mapping(cls, raw: object) -> ResolvedTaskSpec:
        value = strict_object(
            raw,
            name="ResolvedTaskSpec",
            required=frozenset(
                {
                    "schema_version",
                    "task_id",
                    "task_type",
                    "geometry",
                    "target_classes",
                    "on_detection",
                    "constraints",
                    "completion_action",
                    "unresolved_assumptions",
                }
            ),
        )
        action_values = require_array(value["on_detection"], "on_detection")
        try:
            task_type = TaskType(require_text(value["task_type"], "task_type"))
            actions = tuple(
                PlanAction(require_text(item, "on_detection item")) for item in action_values
            )
            completion = PlanAction(require_text(value["completion_action"], "completion_action"))
        except ValueError as exc:
            raise AutonomyContractError(f"unsupported task/action value: {exc}") from exc
        return cls(
            task_id=require_text(value["task_id"], "task_id"),
            task_type=task_type,
            geometry=TaskGeometry.from_mapping(value["geometry"]),
            target_classes=require_string_array(value["target_classes"], "target_classes"),
            on_detection=actions,
            constraints=TaskConstraints.from_mapping(value["constraints"]),
            completion_action=completion,
            unresolved_assumptions=require_string_array(
                value["unresolved_assumptions"], "unresolved_assumptions"
            ),
            schema_version=require_schema_version(value["schema_version"]),
        )

    @property
    def ready_for_planning(self) -> bool:
        return not self.unresolved_assumptions

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "task_type": self.task_type.value,
            "geometry": self.geometry.to_mapping(),
            "target_classes": list(self.target_classes),
            "on_detection": [action.value for action in self.on_detection],
            "constraints": self.constraints.to_mapping(),
            "completion_action": self.completion_action.value,
            "unresolved_assumptions": list(self.unresolved_assumptions),
        }


@dataclass(frozen=True, slots=True)
class PlanStep(_CanonicalDocument):
    sequence: int
    action: PlanAction
    reference_id: str | None = None
    quantity: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "sequence", require_int(self.sequence, "sequence", minimum=0))
        if not isinstance(self.action, PlanAction):
            raise AutonomyContractError("action must be a PlanAction")
        object.__setattr__(self, "reference_id", optional_text(self.reference_id, "reference_id"))
        object.__setattr__(self, "quantity", optional_real(self.quantity, "quantity", minimum=0.0))

    @classmethod
    def from_mapping(cls, raw: object) -> PlanStep:
        value = strict_object(
            raw,
            name="PlanStep",
            required=frozenset({"sequence", "action"}),
            optional=frozenset({"reference_id", "quantity"}),
        )
        try:
            action = PlanAction(require_text(value["action"], "action"))
        except ValueError as exc:
            raise AutonomyContractError(f"unsupported plan action: {exc}") from exc
        return cls(
            sequence=require_int(value["sequence"], "sequence", minimum=0),
            action=action,
            reference_id=optional_text(value.get("reference_id"), "reference_id"),
            quantity=optional_real(value.get("quantity"), "quantity", minimum=0.0),
        )

    def to_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {"sequence": self.sequence, "action": self.action.value}
        if self.reference_id is not None:
            result["reference_id"] = self.reference_id
        if self.quantity is not None:
            result["quantity"] = self.quantity
        return result


@dataclass(frozen=True, slots=True)
class PlatformNeutralPlan(_CanonicalDocument):
    plan_id: str
    task_spec_hash: str
    context_snapshot_hash: str
    steps: tuple[PlanStep, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", require_schema_version(self.schema_version))
        object.__setattr__(self, "plan_id", require_text(self.plan_id, "plan_id"))
        object.__setattr__(
            self, "task_spec_hash", _require_sha256(self.task_spec_hash, "task_spec_hash")
        )
        object.__setattr__(
            self,
            "context_snapshot_hash",
            _require_sha256(self.context_snapshot_hash, "context_snapshot_hash"),
        )
        object.__setattr__(
            self,
            "steps",
            _typed_tuple(self.steps, "steps", PlanStep, minimum_items=1),
        )
        if tuple(step.sequence for step in self.steps) != tuple(range(len(self.steps))):
            raise AutonomyContractError("plan step sequence must be contiguous and zero-based")

    @classmethod
    def from_mapping(cls, raw: object) -> PlatformNeutralPlan:
        value = strict_object(
            raw,
            name="PlatformNeutralPlan",
            required=frozenset(
                {"schema_version", "plan_id", "task_spec_hash", "context_snapshot_hash", "steps"}
            ),
        )
        steps = tuple(
            PlanStep.from_mapping(item) for item in require_array(value["steps"], "steps")
        )
        return cls(
            plan_id=require_text(value["plan_id"], "plan_id"),
            task_spec_hash=_require_sha256(value["task_spec_hash"], "task_spec_hash"),
            context_snapshot_hash=_require_sha256(
                value["context_snapshot_hash"], "context_snapshot_hash"
            ),
            steps=steps,
            schema_version=require_schema_version(value["schema_version"]),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "task_spec_hash": self.task_spec_hash,
            "context_snapshot_hash": self.context_snapshot_hash,
            "steps": [step.to_mapping() for step in self.steps],
        }


@dataclass(frozen=True, slots=True)
class MissionItem(_CanonicalDocument):
    sequence: int
    command: MissionCommand
    frame: MissionFrame
    latitude_deg: float | None = None
    longitude_deg: float | None = None
    altitude_m: float | None = None
    param1: float = 0.0
    param2: float = 0.0
    param3: float = 0.0
    param4: float = 0.0
    current: int | None = None
    autocontinue: bool = True
    mission_type: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "sequence", require_int(self.sequence, "sequence", minimum=0))
        if not isinstance(self.command, MissionCommand):
            raise AutonomyContractError("command must be an allowed MissionCommand")
        if not isinstance(self.frame, MissionFrame):
            raise AutonomyContractError("frame must be an allowed MissionFrame")
        object.__setattr__(
            self,
            "latitude_deg",
            optional_real(self.latitude_deg, "latitude_deg", minimum=-90.0, maximum=90.0),
        )
        object.__setattr__(
            self,
            "longitude_deg",
            optional_real(self.longitude_deg, "longitude_deg", minimum=-180.0, maximum=180.0),
        )
        object.__setattr__(self, "altitude_m", optional_real(self.altitude_m, "altitude_m"))
        for name in ("param1", "param2", "param3", "param4"):
            object.__setattr__(self, name, require_real(getattr(self, name), name))
        if self.current is None:
            object.__setattr__(self, "current", 1 if self.sequence == 0 else 0)
        else:
            object.__setattr__(
                self,
                "current",
                require_int(self.current, "current", minimum=0, maximum=1),
            )
        require_bool(self.autocontinue, "autocontinue")
        object.__setattr__(
            self,
            "mission_type",
            require_int(self.mission_type, "mission_type", minimum=0, maximum=0),
        )
        coordinates = (self.latitude_deg, self.longitude_deg, self.altitude_m)
        if any(item is None for item in coordinates) and any(
            item is not None for item in coordinates
        ):
            raise AutonomyContractError(
                "mission coordinates must be supplied as one complete triplet"
            )
        has_coordinates = all(item is not None for item in coordinates)
        if self.command in _COORDINATE_MISSION_COMMANDS:
            if self.frame not in _GLOBAL_MISSION_FRAMES:
                raise AutonomyContractError(
                    f"{self.command.value} requires an allowed global coordinate frame"
                )
            if not has_coordinates:
                raise AutonomyContractError(
                    f"{self.command.value} requires latitude, longitude and altitude"
                )
        elif self.command is MissionCommand.RETURN_TO_LAUNCH:
            if self.frame is not MissionFrame.MISSION:
                raise AutonomyContractError(
                    "MAV_CMD_NAV_RETURN_TO_LAUNCH requires MAV_FRAME_MISSION"
                )
            if has_coordinates:
                raise AutonomyContractError("MAV_CMD_NAV_RETURN_TO_LAUNCH cannot carry coordinates")
            if any(value != 0.0 for value in (self.param1, self.param2, self.param3, self.param4)):
                raise AutonomyContractError(
                    "MAV_CMD_NAV_RETURN_TO_LAUNCH cannot carry non-zero parameters"
                )
        elif self.command is MissionCommand.VTOL_TRANSITION:
            if self.frame is not MissionFrame.MISSION:
                raise AutonomyContractError("MAV_CMD_DO_VTOL_TRANSITION requires MAV_FRAME_MISSION")
            if has_coordinates:
                raise AutonomyContractError("MAV_CMD_DO_VTOL_TRANSITION cannot carry coordinates")
            if self.param1 not in {3.0, 4.0}:
                raise AutonomyContractError(
                    "MAV_CMD_DO_VTOL_TRANSITION param1 must request MC (3) or FW (4)"
                )
            if any(value != 0.0 for value in (self.param2, self.param3, self.param4)):
                raise AutonomyContractError("MAV_CMD_DO_VTOL_TRANSITION only permits param1")

    @classmethod
    def from_mapping(cls, raw: object) -> MissionItem:
        value = strict_object(
            raw,
            name="MissionItem",
            required=frozenset(
                {
                    "sequence",
                    "command",
                    "frame",
                    "param1",
                    "param2",
                    "param3",
                    "param4",
                    "current",
                    "autocontinue",
                    "mission_type",
                }
            ),
            optional=frozenset({"latitude_deg", "longitude_deg", "altitude_m"}),
        )
        try:
            command = MissionCommand(require_text(value["command"], "command"))
            frame = MissionFrame(require_text(value["frame"], "frame"))
        except ValueError as exc:
            raise AutonomyContractError(f"unsupported mission command/frame: {exc}") from exc
        return cls(
            sequence=require_int(value["sequence"], "sequence", minimum=0),
            command=command,
            frame=frame,
            latitude_deg=optional_real(
                value.get("latitude_deg"), "latitude_deg", minimum=-90.0, maximum=90.0
            ),
            longitude_deg=optional_real(
                value.get("longitude_deg"),
                "longitude_deg",
                minimum=-180.0,
                maximum=180.0,
            ),
            altitude_m=optional_real(value.get("altitude_m"), "altitude_m"),
            param1=require_real(value["param1"], "param1"),
            param2=require_real(value["param2"], "param2"),
            param3=require_real(value["param3"], "param3"),
            param4=require_real(value["param4"], "param4"),
            current=require_int(value["current"], "current", minimum=0, maximum=1),
            autocontinue=require_bool(value["autocontinue"], "autocontinue"),
            mission_type=require_int(value["mission_type"], "mission_type", minimum=0, maximum=0),
        )

    def to_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "sequence": self.sequence,
            "command": self.command.value,
            "frame": self.frame.value,
            "param1": self.param1,
            "param2": self.param2,
            "param3": self.param3,
            "param4": self.param4,
            "current": self.current,
            "autocontinue": self.autocontinue,
            "mission_type": self.mission_type,
        }
        if self.latitude_deg is not None:
            result.update(
                {
                    "latitude_deg": self.latitude_deg,
                    "longitude_deg": self.longitude_deg,
                    "altitude_m": self.altitude_m,
                }
            )
        return result


@dataclass(frozen=True, slots=True)
class ExecutableMission(_CanonicalDocument):
    mission_id: str
    platform_plan_hash: str
    airframe_profile_hash: str
    airframe_adapter_id: str
    airframe_adapter_hash: str
    items: tuple[MissionItem, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", require_schema_version(self.schema_version))
        object.__setattr__(self, "mission_id", require_text(self.mission_id, "mission_id"))
        object.__setattr__(
            self,
            "platform_plan_hash",
            _require_sha256(self.platform_plan_hash, "platform_plan_hash"),
        )
        object.__setattr__(
            self,
            "airframe_profile_hash",
            _require_sha256(self.airframe_profile_hash, "airframe_profile_hash"),
        )
        object.__setattr__(
            self,
            "airframe_adapter_id",
            require_text(self.airframe_adapter_id, "airframe_adapter_id"),
        )
        object.__setattr__(
            self,
            "airframe_adapter_hash",
            _require_sha256(self.airframe_adapter_hash, "airframe_adapter_hash"),
        )
        object.__setattr__(
            self,
            "items",
            _typed_tuple(self.items, "items", MissionItem, minimum_items=1),
        )
        if tuple(item.sequence for item in self.items) != tuple(range(len(self.items))):
            raise AutonomyContractError("mission item sequence must be contiguous and zero-based")
        if tuple(item.current for item in self.items) != (1, *([0] * (len(self.items) - 1))):
            raise AutonomyContractError("only mission sequence zero may be the current item")
        if any(not item.autocontinue for item in self.items):
            raise AutonomyContractError("all executable mission items must autocontinue")
        if any(item.mission_type != 0 for item in self.items):
            raise AutonomyContractError("executable mission supports mission_type=0 only")

    @classmethod
    def from_mapping(cls, raw: object) -> ExecutableMission:
        value = strict_object(
            raw,
            name="ExecutableMission",
            required=frozenset(
                {
                    "schema_version",
                    "mission_id",
                    "platform_plan_hash",
                    "airframe_profile_hash",
                    "airframe_adapter_id",
                    "airframe_adapter_hash",
                    "items",
                }
            ),
        )
        return cls(
            mission_id=require_text(value["mission_id"], "mission_id"),
            platform_plan_hash=_require_sha256(value["platform_plan_hash"], "platform_plan_hash"),
            airframe_profile_hash=_require_sha256(
                value["airframe_profile_hash"], "airframe_profile_hash"
            ),
            airframe_adapter_id=require_text(value["airframe_adapter_id"], "airframe_adapter_id"),
            airframe_adapter_hash=_require_sha256(
                value["airframe_adapter_hash"], "airframe_adapter_hash"
            ),
            items=tuple(
                MissionItem.from_mapping(item) for item in require_array(value["items"], "items")
            ),
            schema_version=require_schema_version(value["schema_version"]),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mission_id": self.mission_id,
            "platform_plan_hash": self.platform_plan_hash,
            "airframe_profile_hash": self.airframe_profile_hash,
            "airframe_adapter_id": self.airframe_adapter_id,
            "airframe_adapter_hash": self.airframe_adapter_hash,
            "items": [item.to_mapping() for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class ValidationFinding(_CanonicalDocument):
    rule_id: str
    verdict: ValidationVerdict
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "rule_id", require_text(self.rule_id, "rule_id"))
        object.__setattr__(self, "reason", require_text(self.reason, "reason"))
        if not isinstance(self.verdict, ValidationVerdict):
            raise AutonomyContractError("verdict must be a ValidationVerdict")

    @classmethod
    def from_mapping(cls, raw: object) -> ValidationFinding:
        value = strict_object(
            raw,
            name="ValidationFinding",
            required=frozenset({"rule_id", "verdict", "reason"}),
        )
        try:
            verdict = ValidationVerdict(require_text(value["verdict"], "verdict"))
        except ValueError as exc:
            raise AutonomyContractError(f"unsupported validation verdict: {exc}") from exc
        return cls(
            rule_id=require_text(value["rule_id"], "rule_id"),
            verdict=verdict,
            reason=require_text(value["reason"], "reason"),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {"rule_id": self.rule_id, "verdict": self.verdict.value, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class ValidationReport(_CanonicalDocument):
    report_id: str
    executable_mission_hash: str
    verdict: ValidationVerdict
    findings: tuple[ValidationFinding, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", require_schema_version(self.schema_version))
        object.__setattr__(self, "report_id", require_text(self.report_id, "report_id"))
        object.__setattr__(
            self,
            "executable_mission_hash",
            _require_sha256(self.executable_mission_hash, "executable_mission_hash"),
        )
        object.__setattr__(
            self,
            "findings",
            _typed_tuple(self.findings, "findings", ValidationFinding, minimum_items=1),
        )
        if not isinstance(self.verdict, ValidationVerdict):
            raise AutonomyContractError("verdict must be a ValidationVerdict")
        expected = (
            ValidationVerdict.DENY
            if any(item.verdict is ValidationVerdict.DENY for item in self.findings)
            else ValidationVerdict.PASS
        )
        if self.verdict is not expected:
            raise AutonomyContractError("validation verdict does not match its findings")

    @classmethod
    def from_mapping(cls, raw: object) -> ValidationReport:
        value = strict_object(
            raw,
            name="ValidationReport",
            required=frozenset(
                {
                    "schema_version",
                    "report_id",
                    "executable_mission_hash",
                    "verdict",
                    "findings",
                }
            ),
        )
        try:
            verdict = ValidationVerdict(require_text(value["verdict"], "verdict"))
        except ValueError as exc:
            raise AutonomyContractError(f"unsupported validation verdict: {exc}") from exc
        return cls(
            report_id=require_text(value["report_id"], "report_id"),
            executable_mission_hash=_require_sha256(
                value["executable_mission_hash"], "executable_mission_hash"
            ),
            verdict=verdict,
            findings=tuple(
                ValidationFinding.from_mapping(item)
                for item in require_array(value["findings"], "findings")
            ),
            schema_version=require_schema_version(value["schema_version"]),
        )

    @property
    def accepted(self) -> bool:
        return self.verdict is ValidationVerdict.PASS

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "report_id": self.report_id,
            "executable_mission_hash": self.executable_mission_hash,
            "verdict": self.verdict.value,
            "findings": [finding.to_mapping() for finding in self.findings],
        }


@dataclass(frozen=True, slots=True)
class OperatorGrant(_CanonicalDocument):
    grant_id: str
    transaction_id: str
    operator_id: str
    prompt_hash: str
    task_spec_hash: str
    plan_hash: str
    executable_mission_hash: str
    validation_report_hash: str
    context_snapshot_hash: str
    airframe_profile_hash: str
    geofence_hash: str
    vehicle_identity_hash: str
    runtime_descriptor_hash: str
    runtime_provenance_hash: str
    expires_at: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", require_schema_version(self.schema_version))
        for name in ("grant_id", "transaction_id", "operator_id"):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        for name in (
            "prompt_hash",
            "task_spec_hash",
            "plan_hash",
            "executable_mission_hash",
            "validation_report_hash",
            "context_snapshot_hash",
            "airframe_profile_hash",
            "geofence_hash",
            "vehicle_identity_hash",
            "runtime_descriptor_hash",
            "runtime_provenance_hash",
        ):
            object.__setattr__(self, name, _require_sha256(getattr(self, name), name))
        object.__setattr__(
            self, "expires_at", require_aware_timestamp(self.expires_at, "expires_at")
        )

    @classmethod
    def from_mapping(cls, raw: object) -> OperatorGrant:
        names = {
            "schema_version",
            "grant_id",
            "transaction_id",
            "operator_id",
            "prompt_hash",
            "task_spec_hash",
            "plan_hash",
            "executable_mission_hash",
            "validation_report_hash",
            "context_snapshot_hash",
            "airframe_profile_hash",
            "geofence_hash",
            "vehicle_identity_hash",
            "runtime_descriptor_hash",
            "runtime_provenance_hash",
            "expires_at",
        }
        value = strict_object(raw, name="OperatorGrant", required=frozenset(names))
        return cls(
            grant_id=require_text(value["grant_id"], "grant_id"),
            transaction_id=require_text(value["transaction_id"], "transaction_id"),
            operator_id=require_text(value["operator_id"], "operator_id"),
            prompt_hash=_require_sha256(value["prompt_hash"], "prompt_hash"),
            task_spec_hash=_require_sha256(value["task_spec_hash"], "task_spec_hash"),
            plan_hash=_require_sha256(value["plan_hash"], "plan_hash"),
            executable_mission_hash=_require_sha256(
                value["executable_mission_hash"], "executable_mission_hash"
            ),
            validation_report_hash=_require_sha256(
                value["validation_report_hash"], "validation_report_hash"
            ),
            context_snapshot_hash=_require_sha256(
                value["context_snapshot_hash"], "context_snapshot_hash"
            ),
            airframe_profile_hash=_require_sha256(
                value["airframe_profile_hash"], "airframe_profile_hash"
            ),
            geofence_hash=_require_sha256(value["geofence_hash"], "geofence_hash"),
            vehicle_identity_hash=_require_sha256(
                value["vehicle_identity_hash"], "vehicle_identity_hash"
            ),
            runtime_descriptor_hash=_require_sha256(
                value["runtime_descriptor_hash"], "runtime_descriptor_hash"
            ),
            runtime_provenance_hash=_require_sha256(
                value["runtime_provenance_hash"], "runtime_provenance_hash"
            ),
            expires_at=require_aware_timestamp(value["expires_at"], "expires_at"),
            schema_version=require_schema_version(value["schema_version"]),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "grant_id": self.grant_id,
            "transaction_id": self.transaction_id,
            "operator_id": self.operator_id,
            "prompt_hash": self.prompt_hash,
            "task_spec_hash": self.task_spec_hash,
            "plan_hash": self.plan_hash,
            "executable_mission_hash": self.executable_mission_hash,
            "validation_report_hash": self.validation_report_hash,
            "context_snapshot_hash": self.context_snapshot_hash,
            "airframe_profile_hash": self.airframe_profile_hash,
            "geofence_hash": self.geofence_hash,
            "vehicle_identity_hash": self.vehicle_identity_hash,
            "runtime_descriptor_hash": self.runtime_descriptor_hash,
            "runtime_provenance_hash": self.runtime_provenance_hash,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class EpisodeArtifact(_CanonicalDocument):
    role: str
    path: str
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", require_text(self.role, "role"))
        object.__setattr__(self, "path", require_text(self.path, "path"))
        object.__setattr__(self, "sha256", _require_sha256(self.sha256, "sha256"))

    @classmethod
    def from_mapping(cls, raw: object) -> EpisodeArtifact:
        value = strict_object(
            raw,
            name="EpisodeArtifact",
            required=frozenset({"role", "path", "sha256"}),
        )
        return cls(
            role=require_text(value["role"], "role"),
            path=require_text(value["path"], "path"),
            sha256=_require_sha256(value["sha256"], "sha256"),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {"role": self.role, "path": self.path, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class EpisodeBundle(_CanonicalDocument):
    episode_id: str
    runtime_descriptor_hash: str
    runtime_provenance_hash: str
    task_spec_hash: str
    plan_hash: str
    executable_mission_hash: str
    validation_report_hash: str
    operator_grant_hash: str
    outcome: EpisodeOutcome
    artifacts: tuple[EpisodeArtifact, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", require_schema_version(self.schema_version))
        object.__setattr__(self, "episode_id", require_text(self.episode_id, "episode_id"))
        for name in (
            "runtime_descriptor_hash",
            "runtime_provenance_hash",
            "task_spec_hash",
            "plan_hash",
            "executable_mission_hash",
            "validation_report_hash",
            "operator_grant_hash",
        ):
            object.__setattr__(self, name, _require_sha256(getattr(self, name), name))
        if not isinstance(self.outcome, EpisodeOutcome):
            raise AutonomyContractError("outcome must be an EpisodeOutcome")
        object.__setattr__(
            self,
            "artifacts",
            _typed_tuple(self.artifacts, "artifacts", EpisodeArtifact, minimum_items=1),
        )

    @classmethod
    def from_mapping(cls, raw: object) -> EpisodeBundle:
        value = strict_object(
            raw,
            name="EpisodeBundle",
            required=frozenset(
                {
                    "schema_version",
                    "episode_id",
                    "runtime_descriptor_hash",
                    "runtime_provenance_hash",
                    "task_spec_hash",
                    "plan_hash",
                    "executable_mission_hash",
                    "validation_report_hash",
                    "operator_grant_hash",
                    "outcome",
                    "artifacts",
                }
            ),
        )
        try:
            outcome = EpisodeOutcome(require_text(value["outcome"], "outcome"))
        except ValueError as exc:
            raise AutonomyContractError(f"unsupported episode outcome: {exc}") from exc
        return cls(
            episode_id=require_text(value["episode_id"], "episode_id"),
            runtime_descriptor_hash=_require_sha256(
                value["runtime_descriptor_hash"], "runtime_descriptor_hash"
            ),
            runtime_provenance_hash=_require_sha256(
                value["runtime_provenance_hash"], "runtime_provenance_hash"
            ),
            task_spec_hash=_require_sha256(value["task_spec_hash"], "task_spec_hash"),
            plan_hash=_require_sha256(value["plan_hash"], "plan_hash"),
            executable_mission_hash=_require_sha256(
                value["executable_mission_hash"], "executable_mission_hash"
            ),
            validation_report_hash=_require_sha256(
                value["validation_report_hash"], "validation_report_hash"
            ),
            operator_grant_hash=_require_sha256(
                value["operator_grant_hash"], "operator_grant_hash"
            ),
            outcome=outcome,
            artifacts=tuple(
                EpisodeArtifact.from_mapping(item)
                for item in require_array(value["artifacts"], "artifacts")
            ),
            schema_version=require_schema_version(value["schema_version"]),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "episode_id": self.episode_id,
            "runtime_descriptor_hash": self.runtime_descriptor_hash,
            "runtime_provenance_hash": self.runtime_provenance_hash,
            "task_spec_hash": self.task_spec_hash,
            "plan_hash": self.plan_hash,
            "executable_mission_hash": self.executable_mission_hash,
            "validation_report_hash": self.validation_report_hash,
            "operator_grant_hash": self.operator_grant_hash,
            "outcome": self.outcome.value,
            "artifacts": [artifact.to_mapping() for artifact in self.artifacts],
        }


def _require_sha256(value: object, name: str) -> str:
    return require_sha256(value, name)


__all__ = [
    "AutonomyContractError",
    "ContextSnapshot",
    "EpisodeArtifact",
    "EpisodeBundle",
    "EpisodeOutcome",
    "ExecutableMission",
    "GeoPoint",
    "MissionCommand",
    "MissionFrame",
    "MissionItem",
    "OperatorGrant",
    "PlanAction",
    "PlanStep",
    "PlatformNeutralPlan",
    "PromptEnvelope",
    "ResolvedTaskSpec",
    "TaskConstraints",
    "TaskGeometry",
    "TaskType",
    "ValidationFinding",
    "ValidationReport",
    "ValidationVerdict",
]
