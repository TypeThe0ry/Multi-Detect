"""Fail-closed PX4 fixed-wing/VTOL mission compilers and transaction core.

This module is deliberately headless.  It consumes runtime-channel geometry,
never evaluator truth, emits only ``MISSION_ITEM_INT`` contracts, and keeps
network I/O in the acceptance adapter which owns the MAVLink connection.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .autonomy import (
    ContextSnapshot,
    ExecutableMission,
    GeoPoint,
    MissionCommand,
    MissionFrame,
    MissionItem,
    OperatorGrant,
    PlanAction,
    PlatformNeutralPlan,
    ValidationReport,
    ValidationVerdict,
)
from .autonomy.mission_context_admission import ContextV2MissionAdmission
from .autonomy.serialization import (
    AutonomyContractError,
    canonical_sha256,
    require_array,
    require_aware_timestamp,
    require_int,
    require_real,
    require_sha256,
    require_string_array,
    require_text,
    strict_object,
)
from .compat import StrEnum

PX4_SYSTEM_ID = 1
PX4_AUTOPILOT_COMPONENT_ID = 1
MAV_MISSION_TYPE_MISSION = 0
MAXIMUM_MISSION_ITEMS = 64

MAV_FRAME_MISSION = 2
MAV_FRAME_GLOBAL_INT = 5
MAV_FRAME_GLOBAL_RELATIVE_ALT_INT = 6
MAV_FRAME_GLOBAL_TERRAIN_ALT_INT = 11

MAV_CMD_NAV_WAYPOINT = 16
MAV_CMD_NAV_LOITER_TIME = 19
MAV_CMD_NAV_RETURN_TO_LAUNCH = 20
MAV_CMD_NAV_LAND = 21
MAV_CMD_NAV_TAKEOFF = 22
MAV_CMD_NAV_LOITER_TO_ALT = 31
MAV_CMD_NAV_VTOL_TAKEOFF = 84
MAV_CMD_NAV_VTOL_LAND = 85
MAV_CMD_DO_VTOL_TRANSITION = 3000

MAV_VTOL_STATE_MC = 3
MAV_VTOL_STATE_FW = 4

MAV_MODE_FLAG_CUSTOM_MODE_ENABLED = 1
PX4_CUSTOM_MAIN_MODE_AUTO = 4
PX4_CUSTOM_SUB_MODE_AUTO_MISSION = 4
PX4_AUTO_MISSION_CUSTOM_MODE = (PX4_CUSTOM_MAIN_MODE_AUTO << 16) | (
    PX4_CUSTOM_SUB_MODE_AUTO_MISSION << 24
)

PX4_VTOL_ADAPTER_ID = "px4-vtol-mission-int-v1"
PX4_FIXED_WING_ADAPTER_ID = "px4-fixed-wing-mission-int-v1"
PX4_MULTIROTOR_ADAPTER_ID = "px4-multirotor-mission-int-v1"
RELATIVE_HOME_ALTITUDE_REFERENCE = "relative_home"

_FRAME_IDS = {
    MissionFrame.MISSION: MAV_FRAME_MISSION,
    MissionFrame.GLOBAL_INT: MAV_FRAME_GLOBAL_INT,
    MissionFrame.GLOBAL_RELATIVE_ALT_INT: MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
    MissionFrame.GLOBAL_TERRAIN_ALT_INT: MAV_FRAME_GLOBAL_TERRAIN_ALT_INT,
}
_COMMAND_IDS = {
    MissionCommand.WAYPOINT: MAV_CMD_NAV_WAYPOINT,
    MissionCommand.LOITER_TIME: MAV_CMD_NAV_LOITER_TIME,
    MissionCommand.RETURN_TO_LAUNCH: MAV_CMD_NAV_RETURN_TO_LAUNCH,
    MissionCommand.LAND: MAV_CMD_NAV_LAND,
    MissionCommand.TAKEOFF: MAV_CMD_NAV_TAKEOFF,
    MissionCommand.LOITER_TO_ALT: MAV_CMD_NAV_LOITER_TO_ALT,
    MissionCommand.VTOL_TAKEOFF: MAV_CMD_NAV_VTOL_TAKEOFF,
    MissionCommand.VTOL_LAND: MAV_CMD_NAV_VTOL_LAND,
    MissionCommand.VTOL_TRANSITION: MAV_CMD_DO_VTOL_TRANSITION,
}
_COORDINATE_COMMAND_IDS = frozenset(
    {
        MAV_CMD_NAV_WAYPOINT,
        MAV_CMD_NAV_LOITER_TIME,
        MAV_CMD_NAV_LAND,
        MAV_CMD_NAV_TAKEOFF,
        MAV_CMD_NAV_LOITER_TO_ALT,
        MAV_CMD_NAV_VTOL_TAKEOFF,
        MAV_CMD_NAV_VTOL_LAND,
    }
)
_GLOBAL_FRAME_IDS = frozenset(
    {
        MAV_FRAME_GLOBAL_INT,
        MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
        MAV_FRAME_GLOBAL_TERRAIN_ALT_INT,
    }
)

_ADAPTER_MANIFEST: Mapping[str, Any] = {
    "adapter_id": PX4_VTOL_ADAPTER_ID,
    "protocol": "MAVLink2/MISSION_ITEM_INT",
    "target_system": PX4_SYSTEM_ID,
    "target_component": PX4_AUTOPILOT_COMPONENT_ID,
    "mission_type": MAV_MISSION_TYPE_MISSION,
    "maximum_items": MAXIMUM_MISSION_ITEMS,
    "frame_ids": {item.value: value for item, value in _FRAME_IDS.items()},
    "command_ids": {item.value: value for item, value in _COMMAND_IDS.items()},
    "lifecycle": [
        MissionCommand.VTOL_TAKEOFF.value,
        MissionCommand.VTOL_TRANSITION.value,
        MissionCommand.WAYPOINT.value,
        MissionCommand.LOITER_TIME.value,
        MissionCommand.VTOL_TRANSITION.value,
        MissionCommand.VTOL_LAND.value,
    ],
}
PX4_VTOL_ADAPTER_HASH = canonical_sha256(_ADAPTER_MANIFEST)

_FIXED_WING_ADAPTER_MANIFEST: Mapping[str, Any] = {
    "adapter_id": PX4_FIXED_WING_ADAPTER_ID,
    "protocol": "MAVLink2/MISSION_ITEM_INT",
    "target_system": PX4_SYSTEM_ID,
    "target_component": PX4_AUTOPILOT_COMPONENT_ID,
    "mission_type": MAV_MISSION_TYPE_MISSION,
    "maximum_items": MAXIMUM_MISSION_ITEMS,
    "frame_ids": {item.value: value for item, value in _FRAME_IDS.items()},
    "command_ids": {item.value: value for item, value in _COMMAND_IDS.items()},
    "lifecycle": [
        MissionCommand.TAKEOFF.value,
        MissionCommand.WAYPOINT.value,
        MissionCommand.WAYPOINT.value,
        MissionCommand.LAND.value,
    ],
    "safety_contract": [
        "geofence",
        "energy_reserve",
        "minimum_turn_radius",
        "landing_alignment",
        "landing_glide_slope",
        "crosswind_tailwind",
    ],
}
PX4_FIXED_WING_ADAPTER_HASH = canonical_sha256(_FIXED_WING_ADAPTER_MANIFEST)

_MULTIROTOR_ADAPTER_MANIFEST: Mapping[str, Any] = {
    "adapter_id": PX4_MULTIROTOR_ADAPTER_ID,
    "protocol": "MAVLink2/MISSION_ITEM_INT",
    "target_system": PX4_SYSTEM_ID,
    "target_component": PX4_AUTOPILOT_COMPONENT_ID,
    "mission_type": MAV_MISSION_TYPE_MISSION,
    "maximum_items": MAXIMUM_MISSION_ITEMS,
    "frame_ids": {item.value: value for item, value in _FRAME_IDS.items()},
    "command_ids": {item.value: value for item, value in _COMMAND_IDS.items()},
    "lifecycle": [
        MissionCommand.TAKEOFF.value,
        MissionCommand.WAYPOINT.value,
        MissionCommand.LOITER_TIME.value,
        MissionCommand.LAND.value,
    ],
    "safety_contract": [
        "geofence",
        "relative_altitude",
        "home_match",
        "loiter_envelope",
        "landing_offset",
    ],
}
PX4_MULTIROTOR_ADAPTER_HASH = canonical_sha256(_MULTIROTOR_ADAPTER_MANIFEST)

_PX4_VEHICLE_CLASS_ALIASES = {
    "vtol": "vtol",
    "v-tol": "vtol",
    "vertical-takeoff-and-landing": "vtol",
    "vertical-takeoff-landing": "vtol",
    "fixed-wing": "fixed-wing",
    "fixedwing": "fixed-wing",
    "fixed_wing": "fixed-wing",
    "plane": "fixed-wing",
    "airplane": "fixed-wing",
    "multirotor": "multirotor",
    "multi-rotor": "multirotor",
    "multicopter": "multirotor",
    "quadcopter": "multirotor",
    "quad-copter": "multirotor",
    "quad": "multirotor",
    "copter": "multirotor",
    "x500": "multirotor",
}
PX4_VEHICLE_CLASSES = ("vtol", "fixed-wing", "multirotor")


class Px4MissionError(AutonomyContractError):
    """Raised when compilation or a mission transaction fails closed."""


def normalize_px4_vehicle_class(value: object) -> str:
    """Return the canonical PX4 mission vehicle class for a user/runtime token.

    The dispatcher is intentionally strict about the resulting class while
    accepting the aliases already used by the simulator and field tooling.
    Keeping this normalization next to the mission compiler prevents a
    caller from accidentally selecting a VTOL lifecycle for a plain x500 (or
    a fixed-wing envelope for a rotorcraft) because two layers used different
    spelling rules.
    """

    if not isinstance(value, str) or not value.strip():
        raise Px4MissionError("vehicle_class must be one of 'vtol', 'fixed-wing' or 'multirotor'")
    normalized = "-".join(
        part
        for part in value.strip().lower().replace("_", "-").replace(" ", "-").split("-")
        if part
    )
    try:
        return _PX4_VEHICLE_CLASS_ALIASES[normalized]
    except KeyError as exc:
        raise Px4MissionError(
            "vehicle_class must be one of 'vtol', 'fixed-wing' or 'multirotor'"
        ) from exc


class MissionTransactionState(StrEnum):
    AUTHORIZED = "authorized"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    READBACK_VERIFIED = "readback_verified"
    COMMITTED = "committed"
    START_PERMITTED = "start_permitted"
    DENIED = "denied"


def _reject_truth_channel(value: object, path: str = "runtime") -> None:
    """Reject evaluator-only fields rather than accidentally compiling from truth."""

    prohibited = {"truth", "ground_truth", "expectations", "expected_outcome"}
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if isinstance(key, str) and key.lower() in prohibited:
                raise Px4MissionError(
                    f"{path}.{key} is evaluator-only and cannot enter mission compile"
                )
            _reject_truth_channel(nested, f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, nested in enumerate(value):
            _reject_truth_channel(nested, f"{path}[{index}]")


def _point_from_runtime(value: object, name: str, *, require_altitude: bool) -> GeoPoint:
    raw = strict_object(
        value,
        name=name,
        required=frozenset({"latitude_deg", "longitude_deg"}),
        optional=frozenset({"altitude_m", "altitude_reference"}),
    )
    normalized = dict(raw)
    if require_altitude:
        if "altitude_m" not in normalized:
            raise Px4MissionError(f"{name}.altitude_m is required")
        normalized.setdefault("altitude_reference", RELATIVE_HOME_ALTITUDE_REFERENCE)
    point = GeoPoint.from_mapping(normalized)
    if require_altitude and point.altitude_reference != RELATIVE_HOME_ALTITUDE_REFERENCE:
        raise Px4MissionError(
            f"{name}.altitude_reference must be {RELATIVE_HOME_ALTITUDE_REFERENCE!r}"
        )
    return point


@dataclass(frozen=True, slots=True)
class VtolMissionGeometry:
    """Runtime-channel geometry for a bounded VTOL mission."""

    home: GeoPoint
    waypoints: tuple[GeoPoint, ...]
    takeoff_altitude_m: float
    loiter_time_s: float
    loiter_radius_m: float
    acceptance_radius_m: float
    landing: GeoPoint | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.home, GeoPoint):
            raise Px4MissionError("home must be a GeoPoint")
        waypoints = tuple(self.waypoints)
        if not waypoints or any(not isinstance(point, GeoPoint) for point in waypoints):
            raise Px4MissionError("waypoints must contain at least one GeoPoint")
        if len(waypoints) > MAXIMUM_MISSION_ITEMS - 5:
            raise Px4MissionError("waypoints exceed the bounded PX4 mission item budget")
        for index, point in enumerate(waypoints):
            if point.altitude_m is None:
                raise Px4MissionError(f"waypoints[{index}] requires altitude_m")
            if point.altitude_reference != RELATIVE_HOME_ALTITUDE_REFERENCE:
                raise Px4MissionError(
                    f"waypoints[{index}] altitude must be relative to runtime home"
                )
        object.__setattr__(self, "waypoints", waypoints)
        object.__setattr__(
            self,
            "takeoff_altitude_m",
            require_real(self.takeoff_altitude_m, "takeoff_altitude_m", minimum=0.001),
        )
        object.__setattr__(
            self,
            "loiter_time_s",
            require_real(self.loiter_time_s, "loiter_time_s", minimum=0.001),
        )
        object.__setattr__(
            self,
            "loiter_radius_m",
            require_real(self.loiter_radius_m, "loiter_radius_m", minimum=0.001),
        )
        object.__setattr__(
            self,
            "acceptance_radius_m",
            require_real(self.acceptance_radius_m, "acceptance_radius_m", minimum=0.001),
        )
        landing = self.landing
        if landing is None:
            landing = GeoPoint(
                self.home.latitude_deg,
                self.home.longitude_deg,
                0.0,
                RELATIVE_HOME_ALTITUDE_REFERENCE,
            )
        if not isinstance(landing, GeoPoint) or landing.altitude_m is None:
            raise Px4MissionError("landing must be a GeoPoint with relative altitude")
        if landing.altitude_reference != RELATIVE_HOME_ALTITUDE_REFERENCE:
            raise Px4MissionError("landing altitude must be relative to runtime home")
        object.__setattr__(self, "landing", landing)

    def to_mapping(self) -> dict[str, Any]:
        """Return the strict runtime geometry shape used by the compiler CLI."""

        return {
            "home_position": self.home.to_mapping(),
            "mission_geometry": {
                "waypoints": [point.to_mapping() for point in self.waypoints],
                "takeoff_altitude_m": self.takeoff_altitude_m,
                "loiter_time_s": self.loiter_time_s,
                "loiter_radius_m": self.loiter_radius_m,
                "acceptance_radius_m": self.acceptance_radius_m,
                "landing": self.landing.to_mapping(),
            },
        }

    @classmethod
    def from_mapping(cls, raw: object) -> VtolMissionGeometry:
        """Re-open a saved VTOL geometry document without evaluator fields."""

        value = strict_object(
            raw,
            name="VtolMissionGeometry",
            required=frozenset({"home_position", "mission_geometry"}),
        )
        return cls.from_runtime_mapping(value)

    @classmethod
    def from_runtime_mapping(cls, raw: object) -> VtolMissionGeometry:
        """Read only ``home_position`` and ``mission_geometry`` from runtime input."""

        _reject_truth_channel(raw)
        if not isinstance(raw, Mapping):
            raise Px4MissionError("runtime input must be an object")
        home_raw = raw.get("home_position", raw.get("home"))
        mission_raw = raw.get("mission_geometry")
        if home_raw is None:
            raise Px4MissionError("runtime input is missing home_position")
        if not isinstance(mission_raw, Mapping):
            raise Px4MissionError("runtime input is missing mission_geometry")
        mission = strict_object(
            mission_raw,
            name="mission_geometry",
            required=frozenset(
                {
                    "waypoints",
                    "takeoff_altitude_m",
                    "loiter_time_s",
                    "loiter_radius_m",
                    "acceptance_radius_m",
                }
            ),
            optional=frozenset({"landing"}),
        )
        points = require_array(mission["waypoints"], "mission_geometry.waypoints")
        landing_raw = mission.get("landing")
        return cls(
            home=_point_from_runtime(home_raw, "home_position", require_altitude=False),
            waypoints=tuple(
                _point_from_runtime(point, f"waypoints[{index}]", require_altitude=True)
                for index, point in enumerate(points)
            ),
            takeoff_altitude_m=require_real(
                mission["takeoff_altitude_m"], "takeoff_altitude_m", minimum=0.001
            ),
            loiter_time_s=require_real(mission["loiter_time_s"], "loiter_time_s", minimum=0.001),
            loiter_radius_m=require_real(
                mission["loiter_radius_m"], "loiter_radius_m", minimum=0.001
            ),
            acceptance_radius_m=require_real(
                mission["acceptance_radius_m"],
                "acceptance_radius_m",
                minimum=0.001,
            ),
            landing=(
                _point_from_runtime(landing_raw, "landing", require_altitude=True)
                if landing_raw is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class MultirotorMissionGeometry(VtolMissionGeometry):
    """Runtime-channel geometry for a plain PX4 multirotor mission.

    Multirotors use the same bounded home/waypoint/loiter/land geometry as the
    VTOL multicopter phase, but their wire lifecycle has no VTOL transition
    items.  Keeping a distinct type prevents a VTOL plan from silently being
    compiled for an ``x500`` airframe while still reusing the exact coordinate
    validation and runtime-only parsing rules.
    """

    @classmethod
    def from_runtime_mapping(cls, raw: object) -> MultirotorMissionGeometry:
        """Parse runtime geometry and keep evaluator truth out of the path."""

        parsed = VtolMissionGeometry.from_runtime_mapping(raw)
        return cls(
            home=parsed.home,
            waypoints=parsed.waypoints,
            takeoff_altitude_m=parsed.takeoff_altitude_m,
            loiter_time_s=parsed.loiter_time_s,
            loiter_radius_m=parsed.loiter_radius_m,
            acceptance_radius_m=parsed.acceptance_radius_m,
            landing=parsed.landing,
        )


@dataclass(frozen=True, slots=True)
class FixedWingMissionGeometry:
    """Runtime-channel geometry for a bounded fixed-wing mission.

    A fixed-wing aircraft cannot stop over the landing point or turn in place.
    The geometry therefore makes the approach leg explicit instead of hiding
    it in a generic ``LAND`` item.  All altitudes are relative to the runtime
    home and the terminal landing altitude is exactly zero.
    """

    home: GeoPoint
    waypoints: tuple[GeoPoint, ...]
    landing_approach: GeoPoint
    landing: GeoPoint
    takeoff_altitude_m: float
    acceptance_radius_m: float
    takeoff_pitch_deg: float = 12.0
    takeoff_heading_deg: float | None = None
    landing_heading_deg: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.home, GeoPoint):
            raise Px4MissionError("home must be a GeoPoint")
        waypoints = tuple(self.waypoints)
        if not waypoints or any(not isinstance(point, GeoPoint) for point in waypoints):
            raise Px4MissionError("waypoints must contain at least one GeoPoint")
        if len(waypoints) + 3 > MAXIMUM_MISSION_ITEMS:
            raise Px4MissionError("waypoints exceed the bounded PX4 mission item budget")
        for index, point in enumerate(waypoints):
            name = f"waypoints[{index}]"
            if point.altitude_m is None:
                raise Px4MissionError(f"{name} requires altitude_m")
            if point.altitude_reference != RELATIVE_HOME_ALTITUDE_REFERENCE:
                raise Px4MissionError(f"{name} altitude must be relative to runtime home")
        if not isinstance(self.landing_approach, GeoPoint):
            raise Px4MissionError("landing_approach must be a GeoPoint")
        if self.landing_approach.altitude_m is None:
            raise Px4MissionError("landing_approach requires altitude_m")
        if self.landing_approach.altitude_reference != RELATIVE_HOME_ALTITUDE_REFERENCE:
            raise Px4MissionError("landing_approach altitude must be relative to runtime home")
        if not isinstance(self.landing, GeoPoint) or self.landing.altitude_m is None:
            raise Px4MissionError("landing must be a GeoPoint with relative altitude")
        if self.landing.altitude_reference != RELATIVE_HOME_ALTITUDE_REFERENCE:
            raise Px4MissionError("landing altitude must be relative to runtime home")
        if float(self.landing.altitude_m) != 0.0:
            raise Px4MissionError("fixed-wing landing altitude must be exactly zero")
        object.__setattr__(self, "waypoints", waypoints)
        object.__setattr__(
            self,
            "takeoff_altitude_m",
            require_real(self.takeoff_altitude_m, "takeoff_altitude_m", minimum=0.001),
        )
        object.__setattr__(
            self,
            "acceptance_radius_m",
            require_real(self.acceptance_radius_m, "acceptance_radius_m", minimum=0.001),
        )
        for name in ("takeoff_pitch_deg", "takeoff_heading_deg", "landing_heading_deg"):
            value = getattr(self, name)
            if value is not None:
                normalized = require_real(value, name)
                if name == "takeoff_pitch_deg" and not 0.0 < normalized <= 30.0:
                    raise Px4MissionError("takeoff_pitch_deg must be in (0, 30]")
                if name != "takeoff_pitch_deg" and not 0.0 <= normalized < 360.0:
                    raise Px4MissionError(f"{name} must be in [0, 360)")
                object.__setattr__(self, name, normalized)

    def to_mapping(self) -> dict[str, Any]:
        """Return the strict runtime geometry shape used by the compiler CLI."""

        mission_geometry: dict[str, Any] = {
            "waypoints": [point.to_mapping() for point in self.waypoints],
            "landing_approach": self.landing_approach.to_mapping(),
            "landing": self.landing.to_mapping(),
            "takeoff_altitude_m": self.takeoff_altitude_m,
            "acceptance_radius_m": self.acceptance_radius_m,
            "takeoff_pitch_deg": self.takeoff_pitch_deg,
            "takeoff_heading_deg": self.takeoff_heading_deg,
            "landing_heading_deg": self.landing_heading_deg,
        }
        return {
            "home_position": self.home.to_mapping(),
            "mission_geometry": mission_geometry,
        }

    @classmethod
    def from_mapping(cls, raw: object) -> FixedWingMissionGeometry:
        """Re-open a saved fixed-wing geometry document strictly."""

        value = strict_object(
            raw,
            name="FixedWingMissionGeometry",
            required=frozenset({"home_position", "mission_geometry"}),
        )
        return cls.from_runtime_mapping(value)

    @classmethod
    def from_runtime_mapping(cls, raw: object) -> FixedWingMissionGeometry:
        """Read fixed-wing geometry from runtime input and reject truth fields."""

        _reject_truth_channel(raw)
        if not isinstance(raw, Mapping):
            raise Px4MissionError("runtime input must be an object")
        home_raw = raw.get("home_position", raw.get("home"))
        mission_raw = raw.get("mission_geometry")
        if home_raw is None:
            raise Px4MissionError("runtime input is missing home_position")
        if not isinstance(mission_raw, Mapping):
            raise Px4MissionError("runtime input is missing mission_geometry")
        mission = strict_object(
            mission_raw,
            name="mission_geometry",
            required=frozenset(
                {
                    "waypoints",
                    "landing_approach",
                    "landing",
                    "takeoff_altitude_m",
                    "acceptance_radius_m",
                }
            ),
            optional=frozenset({"takeoff_pitch_deg", "takeoff_heading_deg", "landing_heading_deg"}),
        )
        points = require_array(mission["waypoints"], "mission_geometry.waypoints")
        return cls(
            home=_point_from_runtime(home_raw, "home_position", require_altitude=False),
            waypoints=tuple(
                _point_from_runtime(point, f"waypoints[{index}]", require_altitude=True)
                for index, point in enumerate(points)
            ),
            landing_approach=_point_from_runtime(
                mission["landing_approach"], "landing_approach", require_altitude=True
            ),
            landing=_point_from_runtime(mission["landing"], "landing", require_altitude=True),
            takeoff_altitude_m=require_real(
                mission["takeoff_altitude_m"], "takeoff_altitude_m", minimum=0.001
            ),
            acceptance_radius_m=require_real(
                mission["acceptance_radius_m"], "acceptance_radius_m", minimum=0.001
            ),
            takeoff_pitch_deg=require_real(
                mission.get("takeoff_pitch_deg", 12.0), "takeoff_pitch_deg"
            ),
            takeoff_heading_deg=(
                None
                if mission.get("takeoff_heading_deg") is None
                else require_real(mission["takeoff_heading_deg"], "takeoff_heading_deg")
            ),
            landing_heading_deg=(
                None
                if mission.get("landing_heading_deg") is None
                else require_real(mission["landing_heading_deg"], "landing_heading_deg")
            ),
        )


@dataclass(frozen=True, slots=True)
class Px4MissionSafetyPolicy:
    """Geometry and airframe limits required before wire compilation."""

    geofence_center: GeoPoint
    geofence_radius_m: float
    allowed_airframe_profile_ids: tuple[str, ...]
    minimum_relative_altitude_m: float = 5.0
    maximum_relative_altitude_m: float = 120.0
    maximum_mission_radius_m: float = 3_000.0
    maximum_loiter_radius_m: float = 150.0
    maximum_loiter_time_s: float = 600.0
    maximum_landing_offset_m: float = 20.0
    home_match_tolerance_m: float = 2.0
    maximum_items: int = MAXIMUM_MISSION_ITEMS

    def __post_init__(self) -> None:
        if not isinstance(self.geofence_center, GeoPoint):
            raise Px4MissionError("geofence_center must be a GeoPoint")
        object.__setattr__(
            self,
            "allowed_airframe_profile_ids",
            require_string_array(self.allowed_airframe_profile_ids, "allowed_airframe_profile_ids"),
        )
        if not self.allowed_airframe_profile_ids:
            raise Px4MissionError("allowed_airframe_profile_ids must not be empty")
        for name in (
            "geofence_radius_m",
            "minimum_relative_altitude_m",
            "maximum_relative_altitude_m",
            "maximum_mission_radius_m",
            "maximum_loiter_radius_m",
            "maximum_loiter_time_s",
            "maximum_landing_offset_m",
            "home_match_tolerance_m",
        ):
            object.__setattr__(self, name, require_real(getattr(self, name), name, minimum=0.001))
        object.__setattr__(
            self,
            "maximum_items",
            require_int(
                self.maximum_items,
                "maximum_items",
                minimum=6,
                maximum=MAXIMUM_MISSION_ITEMS,
            ),
        )
        if self.minimum_relative_altitude_m > self.maximum_relative_altitude_m:
            raise Px4MissionError("minimum altitude cannot exceed maximum altitude")
        if self.maximum_mission_radius_m > self.geofence_radius_m:
            raise Px4MissionError("mission radius cannot exceed geofence radius")

    def to_mapping(self) -> dict[str, Any]:
        """Return every immutable policy field in its canonical input shape."""

        return {
            "geofence_center": self.geofence_center.to_mapping(),
            "geofence_radius_m": self.geofence_radius_m,
            "allowed_airframe_profile_ids": list(self.allowed_airframe_profile_ids),
            "minimum_relative_altitude_m": self.minimum_relative_altitude_m,
            "maximum_relative_altitude_m": self.maximum_relative_altitude_m,
            "maximum_mission_radius_m": self.maximum_mission_radius_m,
            "maximum_loiter_radius_m": self.maximum_loiter_radius_m,
            "maximum_loiter_time_s": self.maximum_loiter_time_s,
            "maximum_landing_offset_m": self.maximum_landing_offset_m,
            "home_match_tolerance_m": self.home_match_tolerance_m,
            "maximum_items": self.maximum_items,
        }

    @classmethod
    def from_mapping(cls, raw: object) -> Px4MissionSafetyPolicy:
        """Re-open a VTOL/multirotor policy with defaults and strict fields."""

        value = strict_object(
            raw,
            name="Px4MissionSafetyPolicy",
            required=frozenset(
                {"geofence_center", "geofence_radius_m", "allowed_airframe_profile_ids"}
            ),
            optional=frozenset(
                {
                    "minimum_relative_altitude_m",
                    "maximum_relative_altitude_m",
                    "maximum_mission_radius_m",
                    "maximum_loiter_radius_m",
                    "maximum_loiter_time_s",
                    "maximum_landing_offset_m",
                    "home_match_tolerance_m",
                    "maximum_items",
                }
            ),
        )
        return cls(
            geofence_center=GeoPoint.from_mapping(value["geofence_center"]),
            geofence_radius_m=require_real(value["geofence_radius_m"], "geofence_radius_m"),
            allowed_airframe_profile_ids=require_string_array(
                value["allowed_airframe_profile_ids"], "allowed_airframe_profile_ids"
            ),
            minimum_relative_altitude_m=require_real(
                value.get("minimum_relative_altitude_m", 5.0),
                "minimum_relative_altitude_m",
            ),
            maximum_relative_altitude_m=require_real(
                value.get("maximum_relative_altitude_m", 120.0),
                "maximum_relative_altitude_m",
            ),
            maximum_mission_radius_m=require_real(
                value.get("maximum_mission_radius_m", 3_000.0),
                "maximum_mission_radius_m",
            ),
            maximum_loiter_radius_m=require_real(
                value.get("maximum_loiter_radius_m", 150.0), "maximum_loiter_radius_m"
            ),
            maximum_loiter_time_s=require_real(
                value.get("maximum_loiter_time_s", 600.0), "maximum_loiter_time_s"
            ),
            maximum_landing_offset_m=require_real(
                value.get("maximum_landing_offset_m", 20.0), "maximum_landing_offset_m"
            ),
            home_match_tolerance_m=require_real(
                value.get("home_match_tolerance_m", 2.0), "home_match_tolerance_m"
            ),
            maximum_items=require_int(
                value.get("maximum_items", MAXIMUM_MISSION_ITEMS), "maximum_items"
            ),
        )

    @property
    def geofence_hash(self) -> str:
        return canonical_sha256(
            {
                "center": self.geofence_center.to_mapping(),
                "radius_m": self.geofence_radius_m,
            }
        )

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(
            {
                "geofence_center": self.geofence_center.to_mapping(),
                "geofence_radius_m": self.geofence_radius_m,
                "allowed_airframe_profile_ids": list(self.allowed_airframe_profile_ids),
                "minimum_relative_altitude_m": self.minimum_relative_altitude_m,
                "maximum_relative_altitude_m": self.maximum_relative_altitude_m,
                "maximum_mission_radius_m": self.maximum_mission_radius_m,
                "maximum_loiter_radius_m": self.maximum_loiter_radius_m,
                "maximum_loiter_time_s": self.maximum_loiter_time_s,
                "maximum_landing_offset_m": self.maximum_landing_offset_m,
                "home_match_tolerance_m": self.home_match_tolerance_m,
                "maximum_items": self.maximum_items,
            }
        )


@dataclass(frozen=True, slots=True)
class Px4MultirotorSafetyPolicy(Px4MissionSafetyPolicy):
    """Named multirotor view of the shared bounded mission policy.

    The policy fields intentionally remain identical to the VTOL hover-phase
    policy so SIM, HIL and REAL use one geofence/altitude/loiter contract.  A
    separate type and adapter hash make the selected vehicle explicit in the
    mission grant and audit chain.
    """


@dataclass(frozen=True, slots=True)
class Px4FixedWingSafetyPolicy:
    """Fixed-wing flight-envelope checks required before mission compilation.

    The policy is intentionally explicit: a generic waypoint list is not
    enough for a fixed-wing aircraft because it has a minimum airspeed,
    finite turn radius and a constrained landing approach.  Values are
    immutable and included in the canonical policy hash used by the mission
    grant/audit chain.
    """

    geofence_center: GeoPoint
    geofence_radius_m: float
    allowed_airframe_profile_ids: tuple[str, ...]
    minimum_relative_altitude_m: float = 20.0
    maximum_relative_altitude_m: float = 180.0
    maximum_mission_radius_m: float = 5_000.0
    minimum_turn_radius_m: float = 45.0
    minimum_leg_length_m: float = 25.0
    minimum_landing_approach_m: float = 120.0
    maximum_landing_approach_m: float = 1_000.0
    maximum_glide_slope_deg: float = 12.0
    maximum_landing_offset_m: float = 500.0
    maximum_landing_alignment_deg: float = 20.0
    home_match_tolerance_m: float = 5.0
    cruise_airspeed_mps: float = 17.0
    minimum_airspeed_mps: float = 12.0
    maximum_tailwind_mps: float = 8.0
    maximum_crosswind_mps: float = 10.0
    maximum_gps_accuracy_m: float = 25.0
    minimum_battery_reserve_pct: float = 30.0
    reserve_time_s: float = 120.0
    maximum_items: int = MAXIMUM_MISSION_ITEMS
    maximum_corner_angle_deg: float = 165.0

    def __post_init__(self) -> None:
        if not isinstance(self.geofence_center, GeoPoint):
            raise Px4MissionError("geofence_center must be a GeoPoint")
        object.__setattr__(
            self,
            "allowed_airframe_profile_ids",
            require_string_array(self.allowed_airframe_profile_ids, "allowed_airframe_profile_ids"),
        )
        if not self.allowed_airframe_profile_ids:
            raise Px4MissionError("allowed_airframe_profile_ids must not be empty")
        positive = (
            "geofence_radius_m",
            "minimum_relative_altitude_m",
            "maximum_relative_altitude_m",
            "maximum_mission_radius_m",
            "minimum_turn_radius_m",
            "minimum_leg_length_m",
            "minimum_landing_approach_m",
            "maximum_landing_approach_m",
            "cruise_airspeed_mps",
            "minimum_airspeed_mps",
            "home_match_tolerance_m",
            "reserve_time_s",
        )
        for name in positive:
            object.__setattr__(self, name, require_real(getattr(self, name), name, minimum=0.001))
        for name in (
            "maximum_glide_slope_deg",
            "maximum_tailwind_mps",
            "maximum_crosswind_mps",
            "maximum_gps_accuracy_m",
            "minimum_battery_reserve_pct",
            "maximum_landing_alignment_deg",
        ):
            object.__setattr__(self, name, require_real(getattr(self, name), name, minimum=0.0))
        object.__setattr__(
            self,
            "maximum_corner_angle_deg",
            require_real(self.maximum_corner_angle_deg, "maximum_corner_angle_deg", minimum=1.0),
        )
        object.__setattr__(
            self,
            "maximum_items",
            require_int(
                self.maximum_items,
                "maximum_items",
                minimum=4,
                maximum=MAXIMUM_MISSION_ITEMS,
            ),
        )
        if self.minimum_relative_altitude_m > self.maximum_relative_altitude_m:
            raise Px4MissionError("minimum altitude cannot exceed maximum altitude")
        if self.maximum_mission_radius_m > self.geofence_radius_m:
            raise Px4MissionError("mission radius cannot exceed geofence radius")
        if self.minimum_landing_approach_m > self.maximum_landing_approach_m:
            raise Px4MissionError("minimum landing approach cannot exceed maximum")
        if self.minimum_airspeed_mps > self.cruise_airspeed_mps:
            raise Px4MissionError("minimum airspeed cannot exceed cruise airspeed")
        if self.maximum_glide_slope_deg >= 90.0:
            raise Px4MissionError("maximum_glide_slope_deg must be below 90 degrees")
        if self.maximum_landing_alignment_deg > 90.0:
            raise Px4MissionError("maximum_landing_alignment_deg must be at most 90 degrees")
        if self.maximum_corner_angle_deg >= 180.0:
            raise Px4MissionError("maximum_corner_angle_deg must be below 180 degrees")
        if self.minimum_battery_reserve_pct > 100.0:
            raise Px4MissionError("minimum_battery_reserve_pct must be at most 100")

    def to_mapping(self) -> dict[str, Any]:
        """Return every immutable fixed-wing policy field in canonical order."""

        return {
            "geofence_center": self.geofence_center.to_mapping(),
            "geofence_radius_m": self.geofence_radius_m,
            "allowed_airframe_profile_ids": list(self.allowed_airframe_profile_ids),
            "minimum_relative_altitude_m": self.minimum_relative_altitude_m,
            "maximum_relative_altitude_m": self.maximum_relative_altitude_m,
            "maximum_mission_radius_m": self.maximum_mission_radius_m,
            "minimum_turn_radius_m": self.minimum_turn_radius_m,
            "minimum_leg_length_m": self.minimum_leg_length_m,
            "minimum_landing_approach_m": self.minimum_landing_approach_m,
            "maximum_landing_approach_m": self.maximum_landing_approach_m,
            "maximum_glide_slope_deg": self.maximum_glide_slope_deg,
            "maximum_landing_offset_m": self.maximum_landing_offset_m,
            "maximum_landing_alignment_deg": self.maximum_landing_alignment_deg,
            "home_match_tolerance_m": self.home_match_tolerance_m,
            "cruise_airspeed_mps": self.cruise_airspeed_mps,
            "minimum_airspeed_mps": self.minimum_airspeed_mps,
            "maximum_tailwind_mps": self.maximum_tailwind_mps,
            "maximum_crosswind_mps": self.maximum_crosswind_mps,
            "maximum_gps_accuracy_m": self.maximum_gps_accuracy_m,
            "minimum_battery_reserve_pct": self.minimum_battery_reserve_pct,
            "reserve_time_s": self.reserve_time_s,
            "maximum_items": self.maximum_items,
            "maximum_corner_angle_deg": self.maximum_corner_angle_deg,
        }

    @classmethod
    def from_mapping(cls, raw: object) -> Px4FixedWingSafetyPolicy:
        """Re-open a fixed-wing envelope with defaults and strict fields."""

        value = strict_object(
            raw,
            name="Px4FixedWingSafetyPolicy",
            required=frozenset(
                {"geofence_center", "geofence_radius_m", "allowed_airframe_profile_ids"}
            ),
            optional=frozenset(
                {
                    "minimum_relative_altitude_m",
                    "maximum_relative_altitude_m",
                    "maximum_mission_radius_m",
                    "minimum_turn_radius_m",
                    "minimum_leg_length_m",
                    "minimum_landing_approach_m",
                    "maximum_landing_approach_m",
                    "maximum_glide_slope_deg",
                    "maximum_landing_offset_m",
                    "maximum_landing_alignment_deg",
                    "home_match_tolerance_m",
                    "cruise_airspeed_mps",
                    "minimum_airspeed_mps",
                    "maximum_tailwind_mps",
                    "maximum_crosswind_mps",
                    "maximum_gps_accuracy_m",
                    "minimum_battery_reserve_pct",
                    "reserve_time_s",
                    "maximum_items",
                    "maximum_corner_angle_deg",
                }
            ),
        )
        return cls(
            geofence_center=GeoPoint.from_mapping(value["geofence_center"]),
            geofence_radius_m=require_real(value["geofence_radius_m"], "geofence_radius_m"),
            allowed_airframe_profile_ids=require_string_array(
                value["allowed_airframe_profile_ids"], "allowed_airframe_profile_ids"
            ),
            minimum_relative_altitude_m=require_real(
                value.get("minimum_relative_altitude_m", 20.0),
                "minimum_relative_altitude_m",
            ),
            maximum_relative_altitude_m=require_real(
                value.get("maximum_relative_altitude_m", 180.0),
                "maximum_relative_altitude_m",
            ),
            maximum_mission_radius_m=require_real(
                value.get("maximum_mission_radius_m", 5_000.0),
                "maximum_mission_radius_m",
            ),
            minimum_turn_radius_m=require_real(
                value.get("minimum_turn_radius_m", 45.0), "minimum_turn_radius_m"
            ),
            minimum_leg_length_m=require_real(
                value.get("minimum_leg_length_m", 25.0), "minimum_leg_length_m"
            ),
            minimum_landing_approach_m=require_real(
                value.get("minimum_landing_approach_m", 120.0),
                "minimum_landing_approach_m",
            ),
            maximum_landing_approach_m=require_real(
                value.get("maximum_landing_approach_m", 1_000.0),
                "maximum_landing_approach_m",
            ),
            maximum_glide_slope_deg=require_real(
                value.get("maximum_glide_slope_deg", 12.0), "maximum_glide_slope_deg"
            ),
            maximum_landing_offset_m=require_real(
                value.get("maximum_landing_offset_m", 500.0), "maximum_landing_offset_m"
            ),
            maximum_landing_alignment_deg=require_real(
                value.get("maximum_landing_alignment_deg", 20.0),
                "maximum_landing_alignment_deg",
            ),
            home_match_tolerance_m=require_real(
                value.get("home_match_tolerance_m", 5.0), "home_match_tolerance_m"
            ),
            cruise_airspeed_mps=require_real(
                value.get("cruise_airspeed_mps", 17.0), "cruise_airspeed_mps"
            ),
            minimum_airspeed_mps=require_real(
                value.get("minimum_airspeed_mps", 12.0), "minimum_airspeed_mps"
            ),
            maximum_tailwind_mps=require_real(
                value.get("maximum_tailwind_mps", 8.0), "maximum_tailwind_mps"
            ),
            maximum_crosswind_mps=require_real(
                value.get("maximum_crosswind_mps", 10.0), "maximum_crosswind_mps"
            ),
            maximum_gps_accuracy_m=require_real(
                value.get("maximum_gps_accuracy_m", 25.0), "maximum_gps_accuracy_m"
            ),
            minimum_battery_reserve_pct=require_real(
                value.get("minimum_battery_reserve_pct", 30.0),
                "minimum_battery_reserve_pct",
            ),
            reserve_time_s=require_real(value.get("reserve_time_s", 120.0), "reserve_time_s"),
            maximum_items=require_int(
                value.get("maximum_items", MAXIMUM_MISSION_ITEMS), "maximum_items"
            ),
            maximum_corner_angle_deg=require_real(
                value.get("maximum_corner_angle_deg", 165.0), "maximum_corner_angle_deg"
            ),
        )

    @property
    def geofence_hash(self) -> str:
        return canonical_sha256(
            {
                "center": self.geofence_center.to_mapping(),
                "radius_m": self.geofence_radius_m,
            }
        )

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(
            {
                "geofence_center": self.geofence_center.to_mapping(),
                "geofence_radius_m": self.geofence_radius_m,
                "allowed_airframe_profile_ids": list(self.allowed_airframe_profile_ids),
                "minimum_relative_altitude_m": self.minimum_relative_altitude_m,
                "maximum_relative_altitude_m": self.maximum_relative_altitude_m,
                "maximum_mission_radius_m": self.maximum_mission_radius_m,
                "minimum_turn_radius_m": self.minimum_turn_radius_m,
                "minimum_leg_length_m": self.minimum_leg_length_m,
                "minimum_landing_approach_m": self.minimum_landing_approach_m,
                "maximum_landing_approach_m": self.maximum_landing_approach_m,
                "maximum_glide_slope_deg": self.maximum_glide_slope_deg,
                "maximum_landing_offset_m": self.maximum_landing_offset_m,
                "maximum_landing_alignment_deg": self.maximum_landing_alignment_deg,
                "home_match_tolerance_m": self.home_match_tolerance_m,
                "cruise_airspeed_mps": self.cruise_airspeed_mps,
                "minimum_airspeed_mps": self.minimum_airspeed_mps,
                "maximum_tailwind_mps": self.maximum_tailwind_mps,
                "maximum_crosswind_mps": self.maximum_crosswind_mps,
                "maximum_gps_accuracy_m": self.maximum_gps_accuracy_m,
                "minimum_battery_reserve_pct": self.minimum_battery_reserve_pct,
                "reserve_time_s": self.reserve_time_s,
                "maximum_items": self.maximum_items,
                "maximum_corner_angle_deg": self.maximum_corner_angle_deg,
            }
        )


def _haversine_distance_m(left: GeoPoint, right: GeoPoint) -> float:
    radius_m = 6_371_008.8
    lat1 = math.radians(left.latitude_deg)
    lat2 = math.radians(right.latitude_deg)
    delta_lat = lat2 - lat1
    delta_lon = math.radians(right.longitude_deg - left.longitude_deg)
    value = (
        math.sin(delta_lat / 2.0) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2.0) ** 2
    )
    return 2.0 * radius_m * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1.0 - value)))


def _initial_bearing_deg(left: GeoPoint, right: GeoPoint) -> float:
    """Return the clockwise WGS-84 initial bearing from ``left`` to ``right``."""

    lat1 = math.radians(left.latitude_deg)
    lat2 = math.radians(right.latitude_deg)
    delta_lon = math.radians(right.longitude_deg - left.longitude_deg)
    y = math.sin(delta_lon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lon)
    if abs(x) < 1e-15 and abs(y) < 1e-15:
        raise Px4MissionError("bearing is undefined for coincident mission points")
    return math.degrees(math.atan2(y, x)) % 360.0


def _heading_delta_deg(left: float, right: float) -> float:
    """Smallest absolute angular difference in degrees."""

    return abs((float(left) - float(right) + 180.0) % 360.0 - 180.0)


@dataclass(frozen=True, slots=True)
class Px4MissionItemInt:
    """Canonical payload fields for MAVLink ``MISSION_ITEM_INT``."""

    target_system: int
    target_component: int
    sequence: int
    frame: int
    command: int
    current: int
    autocontinue: int
    param1: float
    param2: float
    param3: float
    param4: float
    x: int
    y: int
    z: float
    mission_type: int = MAV_MISSION_TYPE_MISSION

    def __post_init__(self) -> None:
        for name, expected in (
            ("target_system", PX4_SYSTEM_ID),
            ("target_component", PX4_AUTOPILOT_COMPONENT_ID),
            ("mission_type", MAV_MISSION_TYPE_MISSION),
        ):
            value = require_int(getattr(self, name), name, minimum=expected, maximum=expected)
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "sequence",
            require_int(self.sequence, "sequence", minimum=0, maximum=MAXIMUM_MISSION_ITEMS - 1),
        )
        object.__setattr__(self, "frame", require_int(self.frame, "frame", minimum=0))
        object.__setattr__(self, "command", require_int(self.command, "command", minimum=0))
        expected_current = 1 if self.sequence == 0 else 0
        object.__setattr__(
            self,
            "current",
            require_int(
                self.current,
                "current",
                minimum=expected_current,
                maximum=expected_current,
            ),
        )
        object.__setattr__(
            self,
            "autocontinue",
            require_int(self.autocontinue, "autocontinue", minimum=1, maximum=1),
        )
        for name in ("param1", "param2", "param3", "param4", "z"):
            object.__setattr__(self, name, require_real(getattr(self, name), name))
        object.__setattr__(
            self, "x", require_int(self.x, "x", minimum=-900_000_000, maximum=900_000_000)
        )
        object.__setattr__(
            self,
            "y",
            require_int(self.y, "y", minimum=-1_800_000_000, maximum=1_800_000_000),
        )
        if self.command not in set(_COMMAND_IDS.values()):
            raise Px4MissionError(f"wire command {self.command} is not allow-listed")
        if self.command in _COORDINATE_COMMAND_IDS:
            if self.frame not in _GLOBAL_FRAME_IDS:
                raise Px4MissionError("coordinate wire item requires an INT global frame")
        elif self.command == MAV_CMD_NAV_RETURN_TO_LAUNCH:
            if self.frame != MAV_FRAME_MISSION or any(
                value != 0
                for value in (
                    self.x,
                    self.y,
                    self.z,
                    self.param1,
                    self.param2,
                    self.param3,
                    self.param4,
                )
            ):
                raise Px4MissionError("RTL wire item must be an empty MAV_FRAME_MISSION item")
        elif self.command == MAV_CMD_DO_VTOL_TRANSITION:
            if (
                self.frame != MAV_FRAME_MISSION
                or self.param1 not in {float(MAV_VTOL_STATE_MC), float(MAV_VTOL_STATE_FW)}
                or any(
                    value != 0
                    for value in (self.x, self.y, self.z, self.param2, self.param3, self.param4)
                )
            ):
                raise Px4MissionError("VTOL transition wire item is not canonical")

    def to_mapping(self) -> dict[str, int | float]:
        return {
            "target_system": self.target_system,
            "target_component": self.target_component,
            "sequence": self.sequence,
            "frame": self.frame,
            "command": self.command,
            "current": self.current,
            "autocontinue": self.autocontinue,
            "param1": self.param1,
            "param2": self.param2,
            "param3": self.param3,
            "param4": self.param4,
            "x": self.x,
            "y": self.y,
            "z": self.z,
            "mission_type": self.mission_type,
        }

    @classmethod
    def from_mapping(cls, raw: object) -> Px4MissionItemInt:
        """Re-open one canonical ``MISSION_ITEM_INT`` payload strictly.

        The wire item is an audit/replay artifact, not a second source of
        mission truth.  Reconstructing it through the same constructor keeps
        command/frame allow-lists, sequence rules and coordinate invariants
        active when a saved compilation is loaded again.
        """

        value = strict_object(
            raw,
            name="Px4MissionItemInt",
            required=frozenset(
                {
                    "target_system",
                    "target_component",
                    "sequence",
                    "frame",
                    "command",
                    "current",
                    "autocontinue",
                    "param1",
                    "param2",
                    "param3",
                    "param4",
                    "x",
                    "y",
                    "z",
                    "mission_type",
                }
            ),
        )
        return cls(
            target_system=require_int(
                value["target_system"],
                "target_system",
                minimum=PX4_SYSTEM_ID,
                maximum=PX4_SYSTEM_ID,
            ),
            target_component=require_int(
                value["target_component"],
                "target_component",
                minimum=PX4_AUTOPILOT_COMPONENT_ID,
                maximum=PX4_AUTOPILOT_COMPONENT_ID,
            ),
            sequence=require_int(value["sequence"], "sequence", minimum=0),
            frame=require_int(value["frame"], "frame", minimum=0),
            command=require_int(value["command"], "command", minimum=0),
            current=require_int(value["current"], "current", minimum=0, maximum=1),
            autocontinue=require_int(value["autocontinue"], "autocontinue", minimum=1, maximum=1),
            param1=require_real(value["param1"], "param1"),
            param2=require_real(value["param2"], "param2"),
            param3=require_real(value["param3"], "param3"),
            param4=require_real(value["param4"], "param4"),
            x=require_int(value["x"], "x"),
            y=require_int(value["y"], "y"),
            z=require_real(value["z"], "z"),
            mission_type=require_int(
                value["mission_type"],
                "mission_type",
                minimum=MAV_MISSION_TYPE_MISSION,
                maximum=MAV_MISSION_TYPE_MISSION,
            ),
        )

    def to_send_args(self) -> tuple[int | float, ...]:
        """Arguments for ``connection.mav.mission_item_int_send``."""

        return (
            self.target_system,
            self.target_component,
            self.sequence,
            self.frame,
            self.command,
            self.current,
            self.autocontinue,
            self.param1,
            self.param2,
            self.param3,
            self.param4,
            self.x,
            self.y,
            self.z,
            self.mission_type,
        )


def compile_px4_mission_items(mission: ExecutableMission) -> tuple[Px4MissionItemInt, ...]:
    """Deterministically map a bound ``ExecutableMission`` to INT wire items."""

    if not isinstance(mission, ExecutableMission):
        raise Px4MissionError("mission must be an ExecutableMission")
    if not mission.items or len(mission.items) > MAXIMUM_MISSION_ITEMS:
        raise Px4MissionError("mission item count is outside the protocol bound")
    result: list[Px4MissionItemInt] = []
    for item in mission.items:
        has_coordinates = item.latitude_deg is not None
        result.append(
            Px4MissionItemInt(
                target_system=PX4_SYSTEM_ID,
                target_component=PX4_AUTOPILOT_COMPONENT_ID,
                sequence=item.sequence,
                frame=_FRAME_IDS[item.frame],
                command=_COMMAND_IDS[item.command],
                current=int(item.current),
                autocontinue=1 if item.autocontinue else 0,
                param1=item.param1,
                param2=item.param2,
                param3=item.param3,
                param4=item.param4,
                x=int(round(float(item.latitude_deg) * 10_000_000)) if has_coordinates else 0,
                y=int(round(float(item.longitude_deg) * 10_000_000)) if has_coordinates else 0,
                z=float(item.altitude_m) if has_coordinates else 0.0,
                mission_type=item.mission_type,
            )
        )
    return tuple(result)


def px4_wire_hash(items: Sequence[Px4MissionItemInt]) -> str:
    normalized = tuple(items)
    if not normalized or len(normalized) > MAXIMUM_MISSION_ITEMS:
        raise Px4MissionError("wire mission must contain 1..64 items")
    if any(not isinstance(item, Px4MissionItemInt) for item in normalized):
        raise Px4MissionError("wire mission contains a non-MISSION_ITEM_INT value")
    if tuple(item.sequence for item in normalized) != tuple(range(len(normalized))):
        raise Px4MissionError("wire mission sequence must be contiguous and zero-based")
    canonical_items: list[dict[str, int | float]] = []
    for item in normalized:
        payload = item.to_mapping()
        # PX4's mission store applies command-specific normalization before
        # serving MISSION_ITEM_INT readback.  TAKEOFF's optional pitch is not
        # retained, and LAND yaw is persisted at millidegree precision.  Keep
        # the hash bound to the values PX4 can actually round-trip while still
        # uploading the planner's original item (the safety geometry remains
        # validated before compilation).
        if item.command == MAV_CMD_NAV_TAKEOFF:
            payload["param1"] = 0.0
        if item.command == MAV_CMD_NAV_LAND:
            payload["param4"] = round(float(item.param4), 3)
        # MAVLink MISSION_ITEM_INT encodes the parameter and altitude fields
        # as IEEE-754 float32 values.  The planner may hold Python float64
        # values while PX4 returns the quantized float32 representation during
        # readback.  Hash the actual wire precision so a legitimate PX4
        # round-trip is not rejected as a mission replacement fault.
        for field in ("param1", "param2", "param3", "param4", "z"):
            payload[field] = struct.unpack("<f", struct.pack("<f", float(payload[field])))[0]
        canonical_items.append(payload)
    return canonical_sha256(
        {
            "protocol": "MAVLink2/MISSION_ITEM_INT",
            "items": canonical_items,
        }
    )


@dataclass(frozen=True, slots=True)
class CompiledPx4Mission:
    executable_mission: ExecutableMission
    wire_items: tuple[Px4MissionItemInt, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.executable_mission, ExecutableMission):
            raise Px4MissionError("executable_mission has the wrong type")
        items = tuple(self.wire_items)
        if items != compile_px4_mission_items(self.executable_mission):
            raise Px4MissionError("wire items do not match the executable mission")
        object.__setattr__(self, "wire_items", items)

    @property
    def wire_hash(self) -> str:
        return px4_wire_hash(self.wire_items)

    @property
    def protocol_hash(self) -> str:
        return self.wire_hash

    @classmethod
    def from_mapping(cls, raw: object) -> CompiledPx4Mission:
        """Re-open a compiled mission and re-derive its wire binding."""

        value = strict_object(
            raw,
            name="CompiledPx4Mission",
            required=frozenset({"executable_mission", "wire_items"}),
        )
        executable = ExecutableMission.from_mapping(value["executable_mission"])
        wire_items = tuple(
            Px4MissionItemInt.from_mapping(item)
            for item in require_array(value["wire_items"], "wire_items")
        )
        return cls(executable_mission=executable, wire_items=wire_items)

    def to_mapping(self) -> dict[str, Any]:
        """Return a strict JSON-compatible compilation artifact."""

        return {
            "executable_mission": self.executable_mission.to_mapping(),
            "wire_items": [item.to_mapping() for item in self.wire_items],
        }


def _validate_plan_and_geometry(
    platform_plan: PlatformNeutralPlan,
    context_snapshot: ContextSnapshot,
    geometry: VtolMissionGeometry,
    policy: Px4MissionSafetyPolicy,
    *,
    vehicle_label: str = "VTOL",
    lifecycle_overhead_items: int = 5,
    context_admission: ContextV2MissionAdmission | None = None,
    admission_now_s: float | None = None,
    admission_clock_id: str | None = None,
    airframe_profile_hash: str | None = None,
) -> None:
    if context_snapshot.schema_version != 1:
        if vehicle_label != "multirotor" or not isinstance(
            context_admission, ContextV2MissionAdmission
        ):
            raise Px4MissionError(
                "v2 context is observation/planning-only until Mission admission "
                "binds an actual SIM/HIL/REAL runtime scope"
            )
        try:
            context_admission.validate_for(
                context=context_snapshot,
                plan=platform_plan,
                geometry_mapping=geometry.to_mapping(),
                safety_policy_mapping=policy.to_mapping(),
                airframe_profile_sha256=airframe_profile_hash,
                vehicle_class="multirotor",
                now_s=admission_now_s,
                clock_id=admission_clock_id,
            )
        except (TypeError, ValueError) as exc:
            raise Px4MissionError(f"v2 Mission compilation admission rejected: {exc}") from exc
    elif (
        context_admission is not None
        or admission_now_s is not None
        or admission_clock_id is not None
    ):
        raise Px4MissionError("v1 context must not consume a v2 Mission compilation admission")
    if platform_plan.context_snapshot_hash != context_snapshot.canonical_hash:
        raise Px4MissionError("platform plan context hash does not match runtime context")
    actions = tuple(step.action for step in platform_plan.steps)
    if not actions or actions[0] is not PlanAction.TAKEOFF:
        raise Px4MissionError("platform plan must begin with TAKEOFF")
    if actions[-1] is not PlanAction.LAND or actions.count(PlanAction.LAND) != 1:
        raise Px4MissionError("platform plan must end with exactly one LAND")
    if actions.count(PlanAction.TAKEOFF) != 1:
        raise Px4MissionError("platform plan must contain exactly one TAKEOFF")
    if not context_snapshot.geofence_valid:
        raise Px4MissionError("runtime context reports an invalid geofence")
    if context_snapshot.airframe_profile_id not in policy.allowed_airframe_profile_ids:
        raise Px4MissionError(f"runtime airframe profile is not an allowed {vehicle_label} profile")
    if (
        _haversine_distance_m(context_snapshot.home_position, geometry.home)
        > policy.home_match_tolerance_m
    ):
        raise Px4MissionError("mission home does not match runtime-channel home")
    if not (
        policy.minimum_relative_altitude_m
        <= geometry.takeoff_altitude_m
        <= policy.maximum_relative_altitude_m
    ):
        raise Px4MissionError("takeoff altitude is outside the policy envelope")
    if geometry.loiter_radius_m > policy.maximum_loiter_radius_m:
        raise Px4MissionError("loiter radius exceeds the policy limit")
    if geometry.loiter_time_s > policy.maximum_loiter_time_s:
        raise Px4MissionError("loiter duration exceeds the policy limit")
    if lifecycle_overhead_items < 1:
        raise Px4MissionError("mission lifecycle overhead must be positive")
    if len(geometry.waypoints) + lifecycle_overhead_items > policy.maximum_items:
        raise Px4MissionError("compiled mission exceeds the policy item limit")
    coordinate_points = (geometry.home, *geometry.waypoints, geometry.landing)
    for index, point in enumerate(coordinate_points):
        if _haversine_distance_m(policy.geofence_center, point) > policy.geofence_radius_m:
            raise Px4MissionError(f"mission coordinate {index} is outside the geofence")
        if _haversine_distance_m(geometry.home, point) > policy.maximum_mission_radius_m:
            raise Px4MissionError(f"mission coordinate {index} exceeds the home radius")
    for index, point in enumerate(geometry.waypoints):
        altitude = float(point.altitude_m)
        if not (
            policy.minimum_relative_altitude_m <= altitude <= policy.maximum_relative_altitude_m
        ):
            raise Px4MissionError(f"waypoint {index} altitude is outside the policy envelope")
    if float(geometry.landing.altitude_m) != 0.0:
        raise Px4MissionError(f"{vehicle_label} landing terminal altitude must be exactly zero")
    if _haversine_distance_m(geometry.home, geometry.landing) > policy.maximum_landing_offset_m:
        raise Px4MissionError(f"{vehicle_label} landing point is too far from runtime home")


def _validate_vtol_lifecycle(mission: ExecutableMission) -> None:
    commands = tuple(item.command for item in mission.items)
    if len(commands) < 6:
        raise Px4MissionError("VTOL mission is missing lifecycle items")
    if commands[0:2] != (
        MissionCommand.VTOL_TAKEOFF,
        MissionCommand.VTOL_TRANSITION,
    ):
        raise Px4MissionError("VTOL mission must start with takeoff then FW transition")
    if commands[-3:] != (
        MissionCommand.LOITER_TIME,
        MissionCommand.VTOL_TRANSITION,
        MissionCommand.VTOL_LAND,
    ):
        raise Px4MissionError("VTOL mission must end with loiter, MC transition and VTOL land")
    if any(command is not MissionCommand.WAYPOINT for command in commands[2:-3]):
        raise Px4MissionError("VTOL mission body must contain waypoints only")
    if mission.items[1].param1 != float(MAV_VTOL_STATE_FW):
        raise Px4MissionError("first VTOL transition must request fixed-wing state")
    if mission.items[-2].param1 != float(MAV_VTOL_STATE_MC):
        raise Px4MissionError("terminal VTOL transition must request multicopter state")
    if commands.count(MissionCommand.VTOL_LAND) != 1:
        raise Px4MissionError("VTOL mission must contain exactly one terminal land")
    if any(item.mission_type != MAV_MISSION_TYPE_MISSION for item in mission.items):
        raise Px4MissionError("mission_type must be MAV_MISSION_TYPE_MISSION")


def compile_vtol_executable_mission(
    *,
    platform_plan: PlatformNeutralPlan,
    context_snapshot: ContextSnapshot,
    geometry: VtolMissionGeometry,
    policy: Px4MissionSafetyPolicy,
    airframe_profile_hash: str,
    mission_id: str | None = None,
) -> CompiledPx4Mission:
    """Compile runtime home/geometry into a complete, hash-bound VTOL mission."""

    if not isinstance(platform_plan, PlatformNeutralPlan):
        raise Px4MissionError("platform_plan has the wrong type")
    if not isinstance(context_snapshot, ContextSnapshot):
        raise Px4MissionError("context_snapshot has the wrong type")
    if not isinstance(geometry, VtolMissionGeometry):
        raise Px4MissionError("geometry has the wrong type")
    if not isinstance(policy, Px4MissionSafetyPolicy):
        raise Px4MissionError("policy has the wrong type")
    _validate_plan_and_geometry(platform_plan, context_snapshot, geometry, policy)
    profile_hash = require_sha256(airframe_profile_hash, "airframe_profile_hash")
    resolved_mission_id = (
        require_text(mission_id, "mission_id")
        if mission_id is not None
        else f"{platform_plan.plan_id}-px4-vtol"
    )
    items: list[MissionItem] = [
        MissionItem(
            sequence=0,
            command=MissionCommand.VTOL_TAKEOFF,
            frame=MissionFrame.GLOBAL_RELATIVE_ALT_INT,
            latitude_deg=geometry.home.latitude_deg,
            longitude_deg=geometry.home.longitude_deg,
            altitude_m=geometry.takeoff_altitude_m,
        ),
        MissionItem(
            sequence=1,
            command=MissionCommand.VTOL_TRANSITION,
            frame=MissionFrame.MISSION,
            param1=float(MAV_VTOL_STATE_FW),
        ),
    ]
    for point in geometry.waypoints:
        items.append(
            MissionItem(
                sequence=len(items),
                command=MissionCommand.WAYPOINT,
                frame=MissionFrame.GLOBAL_RELATIVE_ALT_INT,
                latitude_deg=point.latitude_deg,
                longitude_deg=point.longitude_deg,
                altitude_m=point.altitude_m,
                param2=geometry.acceptance_radius_m,
            )
        )
    loiter_point = geometry.waypoints[-1]
    items.extend(
        (
            MissionItem(
                sequence=len(items),
                command=MissionCommand.LOITER_TIME,
                frame=MissionFrame.GLOBAL_RELATIVE_ALT_INT,
                latitude_deg=loiter_point.latitude_deg,
                longitude_deg=loiter_point.longitude_deg,
                altitude_m=loiter_point.altitude_m,
                param1=geometry.loiter_time_s,
                param3=geometry.loiter_radius_m,
            ),
            MissionItem(
                sequence=len(items) + 1,
                command=MissionCommand.VTOL_TRANSITION,
                frame=MissionFrame.MISSION,
                param1=float(MAV_VTOL_STATE_MC),
            ),
            MissionItem(
                sequence=len(items) + 2,
                command=MissionCommand.VTOL_LAND,
                frame=MissionFrame.GLOBAL_RELATIVE_ALT_INT,
                latitude_deg=geometry.landing.latitude_deg,
                longitude_deg=geometry.landing.longitude_deg,
                altitude_m=geometry.landing.altitude_m,
            ),
        )
    )
    mission = ExecutableMission(
        mission_id=resolved_mission_id,
        platform_plan_hash=platform_plan.canonical_hash,
        airframe_profile_hash=profile_hash,
        airframe_adapter_id=PX4_VTOL_ADAPTER_ID,
        airframe_adapter_hash=PX4_VTOL_ADAPTER_HASH,
        items=tuple(items),
    )
    _validate_vtol_lifecycle(mission)
    return CompiledPx4Mission(
        executable_mission=mission,
        wire_items=compile_px4_mission_items(mission),
    )


def _validate_multirotor_lifecycle(mission: ExecutableMission) -> None:
    """Validate the plain multirotor TAKEOFF→...→LAND lifecycle."""

    commands = tuple(item.command for item in mission.items)
    if len(commands) < 4:
        raise Px4MissionError("multirotor mission is missing lifecycle items")
    if commands[0] is not MissionCommand.TAKEOFF:
        raise Px4MissionError("multirotor mission must start with TAKEOFF")
    if commands[-1] is not MissionCommand.LAND:
        raise Px4MissionError("multirotor mission must end with LAND")
    if commands.count(MissionCommand.TAKEOFF) != 1 or commands.count(MissionCommand.LAND) != 1:
        raise Px4MissionError("multirotor mission must contain exactly one TAKEOFF and LAND")
    if commands.count(MissionCommand.LOITER_TIME) != 1:
        raise Px4MissionError("multirotor mission must contain exactly one LOITER_TIME")
    if any(
        command not in {MissionCommand.WAYPOINT, MissionCommand.LOITER_TIME}
        for command in commands[1:-1]
    ):
        raise Px4MissionError("multirotor mission body must contain waypoints and loiter only")
    if any(item.mission_type != MAV_MISSION_TYPE_MISSION for item in mission.items):
        raise Px4MissionError("mission_type must be MAV_MISSION_TYPE_MISSION")


def compile_multirotor_executable_mission(
    *,
    platform_plan: PlatformNeutralPlan,
    context_snapshot: ContextSnapshot,
    geometry: MultirotorMissionGeometry,
    policy: Px4MultirotorSafetyPolicy,
    airframe_profile_hash: str,
    mission_id: str | None = None,
    context_admission: ContextV2MissionAdmission | None = None,
    admission_now_s: float | None = None,
    admission_clock_id: str | None = None,
) -> CompiledPx4Mission:
    """Compile a hash-bound, bounded PX4 multirotor mission.

    This is deliberately the same ``ExecutableMission`` →
    ``MISSION_ITEM_INT`` transaction path used by VTOL and fixed-wing.  Only
    the lifecycle commands differ: a plain multirotor has no VTOL transition
    items and lands at the runtime-provided home/landing point.

    A v2 context additionally requires an exact-scope compilation admission and
    caller's current monotonic time/clock. This does not issue a ValidationReport,
    OperatorGrant or start permit and never enables a physical transport.
    """

    if not isinstance(platform_plan, PlatformNeutralPlan):
        raise Px4MissionError("platform_plan has the wrong type")
    if not isinstance(context_snapshot, ContextSnapshot):
        raise Px4MissionError("context_snapshot has the wrong type")
    if not isinstance(geometry, MultirotorMissionGeometry):
        raise Px4MissionError("geometry has the wrong type")
    if not isinstance(policy, Px4MultirotorSafetyPolicy):
        raise Px4MissionError("policy has the wrong type")
    _validate_plan_and_geometry(
        platform_plan,
        context_snapshot,
        geometry,
        policy,
        vehicle_label="multirotor",
        lifecycle_overhead_items=3,
        context_admission=context_admission,
        admission_now_s=admission_now_s,
        admission_clock_id=admission_clock_id,
        airframe_profile_hash=airframe_profile_hash,
    )
    profile_hash = require_sha256(airframe_profile_hash, "airframe_profile_hash")
    resolved_mission_id = (
        require_text(mission_id, "mission_id")
        if mission_id is not None
        else f"{platform_plan.plan_id}-px4-multirotor"
    )
    items: list[MissionItem] = [
        MissionItem(
            sequence=0,
            command=MissionCommand.TAKEOFF,
            frame=MissionFrame.GLOBAL_RELATIVE_ALT_INT,
            latitude_deg=geometry.home.latitude_deg,
            longitude_deg=geometry.home.longitude_deg,
            altitude_m=geometry.takeoff_altitude_m,
        )
    ]
    for point in geometry.waypoints:
        items.append(
            MissionItem(
                sequence=len(items),
                command=MissionCommand.WAYPOINT,
                frame=MissionFrame.GLOBAL_RELATIVE_ALT_INT,
                latitude_deg=point.latitude_deg,
                longitude_deg=point.longitude_deg,
                altitude_m=point.altitude_m,
                param2=geometry.acceptance_radius_m,
            )
        )
    loiter_point = geometry.waypoints[-1]
    items.append(
        MissionItem(
            sequence=len(items),
            command=MissionCommand.LOITER_TIME,
            frame=MissionFrame.GLOBAL_RELATIVE_ALT_INT,
            latitude_deg=loiter_point.latitude_deg,
            longitude_deg=loiter_point.longitude_deg,
            altitude_m=loiter_point.altitude_m,
            param1=geometry.loiter_time_s,
            param3=geometry.loiter_radius_m,
        )
    )
    items.append(
        MissionItem(
            sequence=len(items),
            command=MissionCommand.LAND,
            frame=MissionFrame.GLOBAL_RELATIVE_ALT_INT,
            latitude_deg=geometry.landing.latitude_deg,
            longitude_deg=geometry.landing.longitude_deg,
            altitude_m=geometry.landing.altitude_m,
        )
    )
    mission = ExecutableMission(
        mission_id=resolved_mission_id,
        platform_plan_hash=platform_plan.canonical_hash,
        airframe_profile_hash=profile_hash,
        airframe_adapter_id=PX4_MULTIROTOR_ADAPTER_ID,
        airframe_adapter_hash=PX4_MULTIROTOR_ADAPTER_HASH,
        items=tuple(items),
    )
    _validate_multirotor_lifecycle(mission)
    return CompiledPx4Mission(
        executable_mission=mission,
        wire_items=compile_px4_mission_items(mission),
    )


def _wind_components_for_course(
    bearing_deg: float, *, wind_north_mps: float, wind_east_mps: float
) -> tuple[float, float]:
    """Return (tailwind, crosswind) for a clockwise course bearing.

    Positive tailwind means the air mass moves in the direction of travel;
    positive crosswind is only used as an absolute magnitude by the policy.
    """

    course_north = math.cos(math.radians(bearing_deg))
    course_east = math.sin(math.radians(bearing_deg))
    tailwind = wind_north_mps * course_north + wind_east_mps * course_east
    crosswind = -wind_north_mps * course_east + wind_east_mps * course_north
    return tailwind, crosswind


def _validate_fixed_wing_plan_and_geometry(
    platform_plan: PlatformNeutralPlan,
    context_snapshot: ContextSnapshot,
    geometry: FixedWingMissionGeometry,
    policy: Px4FixedWingSafetyPolicy,
) -> tuple[float, float]:
    """Validate fixed-wing geometry and return resolved takeoff/landing headings."""

    if context_snapshot.schema_version != 1:
        raise Px4MissionError(
            "v2 context is observation/planning-only until Mission admission "
            "binds an actual SIM/HIL/REAL runtime scope"
        )
    if platform_plan.context_snapshot_hash != context_snapshot.canonical_hash:
        raise Px4MissionError("platform plan context hash does not match runtime context")
    actions = tuple(step.action for step in platform_plan.steps)
    if not actions or actions[0] is not PlanAction.TAKEOFF:
        raise Px4MissionError("platform plan must begin with TAKEOFF")
    if actions[-1] is not PlanAction.LAND or actions.count(PlanAction.LAND) != 1:
        raise Px4MissionError("platform plan must end with exactly one LAND")
    if actions.count(PlanAction.TAKEOFF) != 1:
        raise Px4MissionError("platform plan must contain exactly one TAKEOFF")
    if PlanAction.RETURN not in actions and PlanAction.DIVERT not in actions:
        raise Px4MissionError("fixed-wing plan must contain RETURN or DIVERT before LAND")
    if not context_snapshot.geofence_valid:
        raise Px4MissionError("runtime context reports an invalid geofence")
    if context_snapshot.airframe_profile_id not in policy.allowed_airframe_profile_ids:
        raise Px4MissionError("runtime airframe profile is not an allowed fixed-wing profile")
    if context_snapshot.gps_fix_type < 3:
        raise Px4MissionError("runtime GPS fix is insufficient for fixed-wing mission")
    if context_snapshot.gps_horizontal_accuracy_m > policy.maximum_gps_accuracy_m:
        raise Px4MissionError("runtime GPS accuracy exceeds fixed-wing policy")
    if context_snapshot.battery_remaining_pct < policy.minimum_battery_reserve_pct:
        raise Px4MissionError("runtime battery is below the fixed-wing reserve")
    if (
        _haversine_distance_m(context_snapshot.home_position, geometry.home)
        > policy.home_match_tolerance_m
    ):
        raise Px4MissionError("mission home does not match runtime-channel home")
    if not (
        policy.minimum_relative_altitude_m
        <= geometry.takeoff_altitude_m
        <= policy.maximum_relative_altitude_m
    ):
        raise Px4MissionError("takeoff altitude is outside the fixed-wing policy envelope")
    if len(geometry.waypoints) + 3 > policy.maximum_items:
        raise Px4MissionError("compiled fixed-wing mission exceeds the policy item limit")

    points = (geometry.home, *geometry.waypoints, geometry.landing_approach, geometry.landing)
    for index, point in enumerate(points):
        if _haversine_distance_m(policy.geofence_center, point) > policy.geofence_radius_m:
            raise Px4MissionError(f"mission coordinate {index} is outside the geofence")
        if _haversine_distance_m(geometry.home, point) > policy.maximum_mission_radius_m:
            raise Px4MissionError(f"mission coordinate {index} exceeds the home radius")
    for index, point in enumerate((*geometry.waypoints, geometry.landing_approach)):
        altitude = float(point.altitude_m)
        if not (
            policy.minimum_relative_altitude_m <= altitude <= policy.maximum_relative_altitude_m
        ):
            raise Px4MissionError(f"fixed-wing waypoint {index} altitude is outside the policy")
    if _haversine_distance_m(geometry.home, geometry.waypoints[0]) < policy.minimum_leg_length_m:
        raise Px4MissionError(
            "takeoff-to-first-waypoint leg is too short for fixed-wing turn radius"
        )
    if _haversine_distance_m(geometry.home, geometry.landing) > policy.maximum_landing_offset_m:
        raise Px4MissionError("fixed-wing landing point is too far from runtime home")

    leg_distances = tuple(
        _haversine_distance_m(points[index], points[index + 1]) for index in range(len(points) - 1)
    )
    approach_distance = leg_distances[-1]
    if not (
        policy.minimum_landing_approach_m <= approach_distance <= policy.maximum_landing_approach_m
    ):
        raise Px4MissionError("landing approach distance is outside the policy envelope")
    approach_altitude = float(geometry.landing_approach.altitude_m)
    if approach_altitude <= 0.0:
        raise Px4MissionError("landing approach altitude must be positive")
    glide_slope_deg = math.degrees(math.atan2(approach_altitude, approach_distance))
    if glide_slope_deg > policy.maximum_glide_slope_deg:
        raise Px4MissionError("landing glide slope exceeds the fixed-wing policy")

    landing_course_deg = _initial_bearing_deg(geometry.landing_approach, geometry.landing)
    landing_heading_deg = (
        landing_course_deg
        if geometry.landing_heading_deg is None
        else float(geometry.landing_heading_deg)
    )
    if (
        _heading_delta_deg(landing_heading_deg, landing_course_deg)
        > policy.maximum_landing_alignment_deg
    ):
        raise Px4MissionError("landing heading is not aligned with the approach leg")
    takeoff_heading_deg = (
        _initial_bearing_deg(geometry.home, geometry.waypoints[0])
        if geometry.takeoff_heading_deg is None
        else float(geometry.takeoff_heading_deg)
    )

    # Every leg must leave enough room for the declared minimum turn radius.
    # The tangent distance formula is conservative and deterministic; it
    # rejects hairpin turns instead of silently asking PX4 to violate the
    # aircraft's envelope.
    bearings = tuple(
        _initial_bearing_deg(points[index], points[index + 1]) for index in range(len(points) - 1)
    )
    for index in range(1, len(points) - 1):
        turn_deg = _heading_delta_deg(bearings[index - 1], bearings[index])
        if turn_deg > policy.maximum_corner_angle_deg:
            raise Px4MissionError(
                f"corner {index} exceeds maximum fixed-wing turn angle ({turn_deg:.2f} deg)"
            )
        tangent_distance = policy.minimum_turn_radius_m * math.tan(math.radians(turn_deg) / 2.0)
        required_leg = max(policy.minimum_leg_length_m, tangent_distance)
        if leg_distances[index - 1] < required_leg or leg_distances[index] < required_leg:
            raise Px4MissionError(
                f"corner {index} does not provide the minimum turn-radius clearance"
            )

    # Crosswind/tailwind are checked on the landing approach, where a bad
    # wind vector is most dangerous.  The same wind is included in the energy
    # estimate below for every route leg.
    tailwind, crosswind = _wind_components_for_course(
        landing_course_deg,
        wind_north_mps=context_snapshot.wind_north_mps,
        wind_east_mps=context_snapshot.wind_east_mps,
    )
    if tailwind > policy.maximum_tailwind_mps:
        raise Px4MissionError("landing tailwind exceeds the fixed-wing policy")
    if abs(crosswind) > policy.maximum_crosswind_mps:
        raise Px4MissionError("landing crosswind exceeds the fixed-wing policy")

    estimated_flight_time_s = 0.0
    for distance, bearing in zip(leg_distances, bearings, strict=True):
        leg_tailwind, _ = _wind_components_for_course(
            bearing,
            wind_north_mps=context_snapshot.wind_north_mps,
            wind_east_mps=context_snapshot.wind_east_mps,
        )
        ground_speed = max(1.0, policy.cruise_airspeed_mps + leg_tailwind)
        estimated_flight_time_s += distance / ground_speed
    if context_snapshot.estimated_flight_time_s < estimated_flight_time_s + policy.reserve_time_s:
        raise Px4MissionError("runtime energy estimate cannot cover mission plus reserve")
    return takeoff_heading_deg, landing_heading_deg


def _validate_fixed_wing_lifecycle(mission: ExecutableMission) -> None:
    commands = tuple(item.command for item in mission.items)
    if len(commands) < 4:
        raise Px4MissionError("fixed-wing mission is missing lifecycle items")
    if commands[0] is not MissionCommand.TAKEOFF:
        raise Px4MissionError("fixed-wing mission must start with TAKEOFF")
    if commands[-1] is not MissionCommand.LAND:
        raise Px4MissionError("fixed-wing mission must end with LAND")
    if any(command is not MissionCommand.WAYPOINT for command in commands[1:-1]):
        raise Px4MissionError("fixed-wing mission body must contain waypoints only")
    if commands.count(MissionCommand.TAKEOFF) != 1 or commands.count(MissionCommand.LAND) != 1:
        raise Px4MissionError("fixed-wing mission must contain exactly one TAKEOFF and LAND")
    if any(item.mission_type != MAV_MISSION_TYPE_MISSION for item in mission.items):
        raise Px4MissionError("mission_type must be MAV_MISSION_TYPE_MISSION")


def compile_fixed_wing_executable_mission(
    *,
    platform_plan: PlatformNeutralPlan,
    context_snapshot: ContextSnapshot,
    geometry: FixedWingMissionGeometry,
    policy: Px4FixedWingSafetyPolicy,
    airframe_profile_hash: str,
    mission_id: str | None = None,
) -> CompiledPx4Mission:
    """Compile a hash-bound, envelope-checked PX4 fixed-wing mission."""

    if not isinstance(platform_plan, PlatformNeutralPlan):
        raise Px4MissionError("platform_plan has the wrong type")
    if not isinstance(context_snapshot, ContextSnapshot):
        raise Px4MissionError("context_snapshot has the wrong type")
    if not isinstance(geometry, FixedWingMissionGeometry):
        raise Px4MissionError("geometry has the wrong type")
    if not isinstance(policy, Px4FixedWingSafetyPolicy):
        raise Px4MissionError("policy has the wrong type")
    takeoff_heading_deg, landing_heading_deg = _validate_fixed_wing_plan_and_geometry(
        platform_plan, context_snapshot, geometry, policy
    )
    profile_hash = require_sha256(airframe_profile_hash, "airframe_profile_hash")
    resolved_mission_id = (
        require_text(mission_id, "mission_id")
        if mission_id is not None
        else f"{platform_plan.plan_id}-px4-fixed-wing"
    )
    items: list[MissionItem] = [
        MissionItem(
            sequence=0,
            command=MissionCommand.TAKEOFF,
            frame=MissionFrame.GLOBAL_RELATIVE_ALT_INT,
            latitude_deg=geometry.home.latitude_deg,
            longitude_deg=geometry.home.longitude_deg,
            altitude_m=geometry.takeoff_altitude_m,
            param1=geometry.takeoff_pitch_deg,
            param4=takeoff_heading_deg,
        )
    ]
    for point in (*geometry.waypoints, geometry.landing_approach):
        items.append(
            MissionItem(
                sequence=len(items),
                command=MissionCommand.WAYPOINT,
                frame=MissionFrame.GLOBAL_RELATIVE_ALT_INT,
                latitude_deg=point.latitude_deg,
                longitude_deg=point.longitude_deg,
                altitude_m=point.altitude_m,
                param2=geometry.acceptance_radius_m,
            )
        )
    items.append(
        MissionItem(
            sequence=len(items),
            command=MissionCommand.LAND,
            frame=MissionFrame.GLOBAL_RELATIVE_ALT_INT,
            latitude_deg=geometry.landing.latitude_deg,
            longitude_deg=geometry.landing.longitude_deg,
            altitude_m=geometry.landing.altitude_m,
            param4=landing_heading_deg,
        )
    )
    mission = ExecutableMission(
        mission_id=resolved_mission_id,
        platform_plan_hash=platform_plan.canonical_hash,
        airframe_profile_hash=profile_hash,
        airframe_adapter_id=PX4_FIXED_WING_ADAPTER_ID,
        airframe_adapter_hash=PX4_FIXED_WING_ADAPTER_HASH,
        items=tuple(items),
    )
    _validate_fixed_wing_lifecycle(mission)
    return CompiledPx4Mission(
        executable_mission=mission,
        wire_items=compile_px4_mission_items(mission),
    )


def compile_executable_mission(
    *,
    vehicle_class: str,
    platform_plan: PlatformNeutralPlan,
    context_snapshot: ContextSnapshot,
    geometry: VtolMissionGeometry | FixedWingMissionGeometry | MultirotorMissionGeometry,
    policy: Px4MissionSafetyPolicy | Px4FixedWingSafetyPolicy | Px4MultirotorSafetyPolicy,
    airframe_profile_hash: str,
    mission_id: str | None = None,
) -> CompiledPx4Mission:
    """Compile one platform-neutral plan through the canonical PX4 path.

    ``vehicle_class`` is the only dispatch input.  The selected geometry and
    policy must be the matching concrete types; a subclass is not silently
    accepted for a different lifecycle.  All three branches return the same
    :class:`CompiledPx4Mission`, whose wire items are subsequently uploaded,
    read back and committed by :class:`Px4MissionTransaction`.
    """

    normalized_class = normalize_px4_vehicle_class(vehicle_class)
    if normalized_class == "multirotor":
        if type(geometry) is not MultirotorMissionGeometry:
            raise Px4MissionError("multirotor vehicle_class requires MultirotorMissionGeometry")
        if type(policy) is not Px4MultirotorSafetyPolicy:
            raise Px4MissionError("multirotor vehicle_class requires Px4MultirotorSafetyPolicy")
        return compile_multirotor_executable_mission(
            platform_plan=platform_plan,
            context_snapshot=context_snapshot,
            geometry=geometry,
            policy=policy,
            airframe_profile_hash=airframe_profile_hash,
            mission_id=mission_id,
        )
    if normalized_class == "vtol":
        if type(geometry) is not VtolMissionGeometry:
            raise Px4MissionError("vtol vehicle_class requires VtolMissionGeometry")
        if type(policy) is not Px4MissionSafetyPolicy:
            raise Px4MissionError("vtol vehicle_class requires Px4MissionSafetyPolicy")
        return compile_vtol_executable_mission(
            platform_plan=platform_plan,
            context_snapshot=context_snapshot,
            geometry=geometry,
            policy=policy,
            airframe_profile_hash=airframe_profile_hash,
            mission_id=mission_id,
        )
    if type(geometry) is not FixedWingMissionGeometry:
        raise Px4MissionError("fixed-wing vehicle_class requires FixedWingMissionGeometry")
    if type(policy) is not Px4FixedWingSafetyPolicy:
        raise Px4MissionError("fixed-wing vehicle_class requires Px4FixedWingSafetyPolicy")
    return compile_fixed_wing_executable_mission(
        platform_plan=platform_plan,
        context_snapshot=context_snapshot,
        geometry=geometry,
        policy=policy,
        airframe_profile_hash=airframe_profile_hash,
        mission_id=mission_id,
    )


@dataclass(frozen=True, slots=True)
class MissionTransactionBindings:
    prompt_hash: str
    task_spec_hash: str
    plan_hash: str
    context_snapshot_hash: str
    airframe_profile_hash: str
    geofence_hash: str
    vehicle_identity_hash: str
    runtime_descriptor_hash: str
    runtime_provenance_hash: str

    def __post_init__(self) -> None:
        for name in (
            "prompt_hash",
            "task_spec_hash",
            "plan_hash",
            "context_snapshot_hash",
            "airframe_profile_hash",
            "geofence_hash",
            "vehicle_identity_hash",
            "runtime_descriptor_hash",
            "runtime_provenance_hash",
        ):
            object.__setattr__(self, name, require_sha256(getattr(self, name), name))


@dataclass(frozen=True, slots=True)
class MissionUploadPlan:
    items: tuple[Px4MissionItemInt, ...]
    protocol_hash: str

    @property
    def item_count(self) -> int:
        return len(self.items)

    def count_send_args(self) -> tuple[int, int, int, int]:
        """Arguments for ``connection.mav.mission_count_send``."""

        return (
            PX4_SYSTEM_ID,
            PX4_AUTOPILOT_COMPONENT_ID,
            self.item_count,
            MAV_MISSION_TYPE_MISSION,
        )


@dataclass(frozen=True, slots=True)
class MissionStartPermit:
    transaction_id: str
    executable_mission_hash: str
    protocol_hash: str

    def to_set_mode_args(self) -> tuple[int, int, int]:
        """Arguments for ``connection.mav.set_mode_send`` (PX4 AUTO.MISSION only)."""

        return (
            PX4_SYSTEM_ID,
            MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
            PX4_AUTO_MISSION_CUSTOM_MODE,
        )


def _aware_datetime(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise Px4MissionError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: str, name: str) -> datetime:
    text = require_aware_timestamp(value, name)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    return datetime.fromisoformat(normalized).astimezone(timezone.utc)


class Px4MissionTransaction:
    """Pure upload/readback/commit gate for a test-only ``OperatorGrant``.

    Network code may only send the values returned here.  A protocol readback
    hash must match before ``permit_start`` can return the exact PX4
    ``set_mode_send`` arguments.
    """

    def __init__(
        self,
        *,
        transaction_id: str,
        compiled: CompiledPx4Mission,
        validation_report: ValidationReport,
        operator_grant: OperatorGrant,
        bindings: MissionTransactionBindings,
        now_utc: datetime,
    ) -> None:
        self._transaction_id = require_text(transaction_id, "transaction_id")
        if not isinstance(compiled, CompiledPx4Mission):
            raise Px4MissionError("compiled has the wrong type")
        if not isinstance(validation_report, ValidationReport):
            raise Px4MissionError("validation_report has the wrong type")
        if not isinstance(operator_grant, OperatorGrant):
            raise Px4MissionError("operator_grant has the wrong type")
        if not isinstance(bindings, MissionTransactionBindings):
            raise Px4MissionError("bindings has the wrong type")
        self._compiled = compiled
        self._validation_report = validation_report
        self._operator_grant = operator_grant
        self._bindings = bindings
        self._expires_at = _parse_timestamp(operator_grant.expires_at, "expires_at")
        self._state = MissionTransactionState.AUTHORIZED
        self._denial_reason: str | None = None
        self._requested_sequences: set[int] = set()
        try:
            self._validate_authorization(_aware_datetime(now_utc, "now_utc"))
        except Px4MissionError as exc:
            self._state = MissionTransactionState.DENIED
            self._denial_reason = str(exc)
            raise

    @property
    def state(self) -> MissionTransactionState:
        return self._state

    @property
    def denial_reason(self) -> str | None:
        return self._denial_reason

    def _deny(self, reason: str) -> None:
        self._state = MissionTransactionState.DENIED
        self._denial_reason = reason
        raise Px4MissionError(reason)

    def _require_state(self, expected: MissionTransactionState) -> None:
        if self._state is not expected:
            self._deny(f"transaction state {self._state.value} does not permit {expected.value}")

    def _check_expiry(self, now_utc: datetime | None) -> None:
        now = datetime.now(timezone.utc) if now_utc is None else _aware_datetime(now_utc, "now_utc")
        if now >= self._expires_at:
            self._deny("operator grant is expired")

    def _validate_authorization(self, now_utc: datetime) -> None:
        if now_utc >= self._expires_at:
            raise Px4MissionError("operator grant is expired")
        mission_hash = self._compiled.executable_mission.canonical_hash
        report_hash = self._validation_report.canonical_hash
        if self._validation_report.verdict is not ValidationVerdict.PASS:
            raise Px4MissionError("validation report verdict must be PASS")
        if self._validation_report.executable_mission_hash != mission_hash:
            raise Px4MissionError("validation report is bound to another executable mission")
        expected = {
            "transaction_id": self._transaction_id,
            "prompt_hash": self._bindings.prompt_hash,
            "task_spec_hash": self._bindings.task_spec_hash,
            "plan_hash": self._bindings.plan_hash,
            "executable_mission_hash": mission_hash,
            "validation_report_hash": report_hash,
            "context_snapshot_hash": self._bindings.context_snapshot_hash,
            "airframe_profile_hash": self._bindings.airframe_profile_hash,
            "geofence_hash": self._bindings.geofence_hash,
            "vehicle_identity_hash": self._bindings.vehicle_identity_hash,
            "runtime_descriptor_hash": self._bindings.runtime_descriptor_hash,
            "runtime_provenance_hash": self._bindings.runtime_provenance_hash,
        }
        for name, value in expected.items():
            if getattr(self._operator_grant, name) != value:
                raise Px4MissionError(f"operator grant {name} binding mismatch")
        mission = self._compiled.executable_mission
        if mission.platform_plan_hash != self._bindings.plan_hash:
            raise Px4MissionError("executable mission plan hash binding mismatch")
        if mission.airframe_profile_hash != self._bindings.airframe_profile_hash:
            raise Px4MissionError("executable mission airframe hash binding mismatch")

    def begin_upload(self, *, now_utc: datetime | None = None) -> MissionUploadPlan:
        self._require_state(MissionTransactionState.AUTHORIZED)
        self._check_expiry(now_utc)
        self._state = MissionTransactionState.UPLOADING
        return MissionUploadPlan(self._compiled.wire_items, self._compiled.wire_hash)

    def mission_item_for_request(
        self,
        sequence: int,
        *,
        request_message_type: str = "MISSION_REQUEST_INT",
    ) -> Px4MissionItemInt:
        self._require_state(MissionTransactionState.UPLOADING)
        if request_message_type != "MISSION_REQUEST_INT":
            self._deny("legacy MISSION_REQUEST is not accepted; MISSION_REQUEST_INT is required")
        requested = require_int(
            sequence,
            "sequence",
            minimum=0,
            maximum=len(self._compiled.wire_items) - 1,
        )
        self._requested_sequences.add(requested)
        return self._compiled.wire_items[requested]

    def accept_upload_ack(
        self,
        *,
        ack_type: int = 0,
        mission_type: int = MAV_MISSION_TYPE_MISSION,
        source_system: int = PX4_SYSTEM_ID,
        source_component: int = PX4_AUTOPILOT_COMPONENT_ID,
        now_utc: datetime | None = None,
    ) -> None:
        self._require_state(MissionTransactionState.UPLOADING)
        self._check_expiry(now_utc)
        if self._requested_sequences != set(range(len(self._compiled.wire_items))):
            self._deny("upload ACK arrived before every mission item was requested")
        if ack_type != 0 or mission_type != MAV_MISSION_TYPE_MISSION:
            self._deny("PX4 did not accept the primary mission upload")
        if source_system != PX4_SYSTEM_ID or source_component != PX4_AUTOPILOT_COMPONENT_ID:
            self._deny("mission ACK source identity mismatch")
        self._state = MissionTransactionState.UPLOADED

    def verify_readback(
        self,
        items: Sequence[Px4MissionItemInt],
        *,
        source_system: int = PX4_SYSTEM_ID,
        source_component: int = PX4_AUTOPILOT_COMPONENT_ID,
        now_utc: datetime | None = None,
    ) -> str:
        self._require_state(MissionTransactionState.UPLOADED)
        self._check_expiry(now_utc)
        if source_system != PX4_SYSTEM_ID or source_component != PX4_AUTOPILOT_COMPONENT_ID:
            self._deny("mission readback source identity mismatch")
        try:
            actual_hash = px4_wire_hash(tuple(items))
        except (AutonomyContractError, TypeError, ValueError) as exc:
            self._deny(f"mission readback is invalid: {exc}")
        if actual_hash != self._compiled.wire_hash:
            self._deny("mission readback protocol hash mismatch")
        self._state = MissionTransactionState.READBACK_VERIFIED
        return actual_hash

    def commit(self, *, now_utc: datetime | None = None) -> str:
        self._require_state(MissionTransactionState.READBACK_VERIFIED)
        self._check_expiry(now_utc)
        self._state = MissionTransactionState.COMMITTED
        return self._compiled.wire_hash

    def permit_start(self, *, now_utc: datetime | None = None) -> MissionStartPermit:
        self._require_state(MissionTransactionState.COMMITTED)
        self._check_expiry(now_utc)
        self._state = MissionTransactionState.START_PERMITTED
        return MissionStartPermit(
            transaction_id=self._transaction_id,
            executable_mission_hash=self._compiled.executable_mission.canonical_hash,
            protocol_hash=self._compiled.wire_hash,
        )


__all__ = [
    "CompiledPx4Mission",
    "MAV_MISSION_TYPE_MISSION",
    "MAXIMUM_MISSION_ITEMS",
    "MissionStartPermit",
    "MissionTransactionBindings",
    "MissionTransactionState",
    "MissionUploadPlan",
    "PX4_AUTO_MISSION_CUSTOM_MODE",
    "PX4_FIXED_WING_ADAPTER_HASH",
    "PX4_FIXED_WING_ADAPTER_ID",
    "PX4_MULTIROTOR_ADAPTER_HASH",
    "PX4_MULTIROTOR_ADAPTER_ID",
    "PX4_VEHICLE_CLASSES",
    "PX4_VTOL_ADAPTER_HASH",
    "PX4_VTOL_ADAPTER_ID",
    "Px4MissionError",
    "Px4MissionItemInt",
    "Px4MissionSafetyPolicy",
    "Px4MultirotorSafetyPolicy",
    "Px4FixedWingSafetyPolicy",
    "Px4MissionTransaction",
    "RELATIVE_HOME_ALTITUDE_REFERENCE",
    "VtolMissionGeometry",
    "MultirotorMissionGeometry",
    "FixedWingMissionGeometry",
    "compile_px4_mission_items",
    "compile_executable_mission",
    "compile_fixed_wing_executable_mission",
    "compile_multirotor_executable_mission",
    "compile_vtol_executable_mission",
    "normalize_px4_vehicle_class",
    "px4_wire_hash",
]
