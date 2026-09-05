"""Deterministic, observation-only progress tracking for compiled missions.

The PX4 mission compiler already produces one strict ``ExecutableMission``
contract for VTOL, fixed-wing and multirotor vehicles.  This module binds that
contract to a truth-free telemetry sample and reports which item is active.
It deliberately does not emit MAVLink or actuator output: a caller still has
to use the existing transaction, authority and safety paths to start or
abort a mission.

The same tracker can consume the deterministic SIM telemetry and an adapter's
HIL/REAL telemetry as long as the object exposes the fields in
``TelemetryLike``.  All coordinate checks use the shared WGS-84 inverse
geodesic, and stale/weak GPS is never allowed to advance a coordinate item.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from numbers import Real
from typing import Any, Protocol

from .autonomy.contracts import ExecutableMission, MissionCommand, MissionItem
from .autonomy.serialization import (
    AutonomyContractError,
    canonical_sha256,
    optional_real,
    require_array,
    require_bool,
    require_int,
    require_real,
    require_schema_version,
    require_sha256,
    require_text,
    strict_object,
)
from .compat import StrEnum
from .geolocation_accuracy import wgs84_distance_m
from .mavlink_gps import (
    GNSS_CANONICAL_MAX_GPS_AGE_S,
    GNSS_CANONICAL_MAX_HORIZONTAL_ACCURACY_M,
    GNSS_CANONICAL_MAX_VERTICAL_ACCURACY_M,
    MAVLINK_GPS_FIX_TYPE_MAX,
    MAVLINK_QUALIFIED_FIX_TYPE_MIN,
    MAVLINK_QUALIFIED_SATELLITES_MIN,
    MAVLINK_SATELLITES_MAX,
)
from .px4_mission_feedback import Px4MissionFeedbackSnapshot

MISSION_PROGRESS_SCHEMA_VERSION = 1
MAXIMUM_PROGRESS_ITEMS = 64


class MissionProgressError(AutonomyContractError):
    """Raised when a mission progress input would make the result ambiguous."""


class MissionProgressPhase(StrEnum):
    """Observable state of the current mission item."""

    READY = "ready"
    ACTIVE = "active"
    WAITING_FOR_LINK = "waiting_for_link"
    WAITING_FOR_GPS = "waiting_for_gps"
    BLOCKED = "blocked"
    COMPLETE = "complete"


class TelemetryLike(Protocol):
    """Minimum read-only telemetry surface consumed by the tracker."""

    observed_at_s: float
    latitude_deg: float
    longitude_deg: float
    altitude_agl_m: float
    gps_fix_type: int | None
    gps_horizontal_accuracy_m: float
    gps_vertical_accuracy_m: float
    satellites_visible: int | None
    gps_observed_at_s: float | None
    link_healthy: bool | None
    armed: bool | None
    flight_mode: str
    mission_started: bool


@dataclass(frozen=True, slots=True)
class MissionProgressConfig:
    """Bounded acceptance and freshness policy shared by all runtimes."""

    waypoint_radius_m: float = 5.0
    altitude_tolerance_m: float = 3.0
    gps_max_age_s: float = GNSS_CANONICAL_MAX_GPS_AGE_S
    require_px4_feedback: bool = False

    def __post_init__(self) -> None:
        for name in ("waypoint_radius_m", "altitude_tolerance_m", "gps_max_age_s"):
            value = _finite(getattr(self, name), name)
            if value <= 0.0:
                raise MissionProgressError(f"{name} must be greater than zero")
        if self.gps_max_age_s > GNSS_CANONICAL_MAX_GPS_AGE_S:
            raise MissionProgressError(
                "gps_max_age_s cannot exceed the canonical GPS freshness budget"
            )
        if not isinstance(self.require_px4_feedback, bool):
            raise MissionProgressError("require_px4_feedback must be bool")


@dataclass(frozen=True, slots=True)
class MissionProgressSnapshot:
    """Canonical, hashable progress evidence for one telemetry observation."""

    mission_id: str
    mission_hash: str
    phase: MissionProgressPhase
    observed_at_s: float
    mission_item_count: int
    current_sequence: int | None
    completed_sequences: tuple[int, ...]
    active_command: str | None
    distance_to_active_m: float | None
    altitude_error_m: float | None
    dwell_elapsed_s: float
    progress_fraction: float
    mission_started: bool
    armed: bool | None
    link_healthy: bool | None
    gps_quality_valid: bool
    gps_age_s: float | None
    reason: str
    px4_feedback: Px4MissionFeedbackSnapshot | None = None
    schema_version: int = MISSION_PROGRESS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", require_schema_version(self.schema_version))
        object.__setattr__(self, "mission_id", require_text(self.mission_id, "mission_id"))
        object.__setattr__(self, "mission_hash", require_sha256(self.mission_hash, "mission_hash"))
        if not isinstance(self.phase, MissionProgressPhase):
            raise MissionProgressError("phase must be a MissionProgressPhase")
        object.__setattr__(
            self, "observed_at_s", require_real(self.observed_at_s, "observed_at_s", minimum=0.0)
        )
        object.__setattr__(
            self,
            "mission_item_count",
            require_int(
                self.mission_item_count,
                "mission_item_count",
                minimum=1,
                maximum=MAXIMUM_PROGRESS_ITEMS,
            ),
        )
        if self.current_sequence is not None:
            object.__setattr__(
                self,
                "current_sequence",
                require_int(
                    self.current_sequence,
                    "current_sequence",
                    minimum=0,
                    maximum=self.mission_item_count - 1,
                ),
            )
        completed = tuple(self.completed_sequences)
        if any(
            isinstance(item, bool)
            or not isinstance(item, int)
            or item < 0
            or item >= self.mission_item_count
            for item in completed
        ):
            raise MissionProgressError("completed_sequences contains an invalid sequence")
        if completed != tuple(sorted(set(completed))):
            raise MissionProgressError("completed_sequences must be sorted and unique")
        if self.current_sequence is not None and self.current_sequence in completed:
            raise MissionProgressError("current_sequence is already completed")
        object.__setattr__(self, "completed_sequences", completed)
        if self.active_command is not None:
            object.__setattr__(
                self, "active_command", require_text(self.active_command, "active_command")
            )
        elif self.current_sequence is not None:
            raise MissionProgressError("active_command is required while a sequence is active")
        for name in ("distance_to_active_m", "altitude_error_m", "gps_age_s"):
            object.__setattr__(self, name, optional_real(getattr(self, name), name, minimum=0.0))
        object.__setattr__(
            self,
            "dwell_elapsed_s",
            require_real(self.dwell_elapsed_s, "dwell_elapsed_s", minimum=0.0),
        )
        fraction = require_real(
            self.progress_fraction, "progress_fraction", minimum=0.0, maximum=1.0
        )
        expected_fraction = len(completed) / self.mission_item_count
        if not math.isclose(fraction, expected_fraction, rel_tol=0.0, abs_tol=1.0e-12):
            raise MissionProgressError("progress_fraction does not match completed_sequences")
        object.__setattr__(self, "progress_fraction", fraction)
        object.__setattr__(
            self, "mission_started", require_bool(self.mission_started, "mission_started")
        )
        for name in ("armed", "link_healthy"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, require_bool(value, name))
        object.__setattr__(
            self, "gps_quality_valid", require_bool(self.gps_quality_valid, "gps_quality_valid")
        )
        object.__setattr__(self, "reason", require_text(self.reason, "reason"))
        feedback = self.px4_feedback
        if feedback is not None:
            if not isinstance(feedback, Px4MissionFeedbackSnapshot):
                raise MissionProgressError(
                    "px4_feedback must be a Px4MissionFeedbackSnapshot or None"
                )
            if (
                feedback.mission_id != self.mission_id
                or feedback.mission_hash != self.mission_hash
                or feedback.mission_item_count != self.mission_item_count
            ):
                raise MissionProgressError(
                    "px4_feedback does not match the bound Mission identity"
                )
        if self.phase is MissionProgressPhase.COMPLETE:
            if len(completed) != self.mission_item_count or self.current_sequence is not None:
                raise MissionProgressError("complete progress must contain every sequence")
            if not math.isclose(fraction, 1.0, rel_tol=0.0, abs_tol=1.0e-12):
                raise MissionProgressError("complete progress must have fraction 1")
        if (
            self.phase is not MissionProgressPhase.COMPLETE
            and len(completed) == self.mission_item_count
        ):
            raise MissionProgressError("all sequences completed requires phase=complete")

    @classmethod
    def from_mapping(cls, raw: object) -> MissionProgressSnapshot:
        value = strict_object(
            raw,
            name="MissionProgressSnapshot",
            required=frozenset(
                {
                    "schema_version",
                    "mission_id",
                    "mission_hash",
                    "phase",
                    "observed_at_s",
                    "mission_item_count",
                    "current_sequence",
                    "completed_sequences",
                    "active_command",
                    "distance_to_active_m",
                    "altitude_error_m",
                    "dwell_elapsed_s",
                    "progress_fraction",
                    "mission_started",
                    "armed",
                    "link_healthy",
                    "gps_quality_valid",
                    "gps_age_s",
                    "reason",
                }
            ),
            optional=frozenset({"px4_feedback"}),
        )
        try:
            phase = MissionProgressPhase(require_text(value["phase"], "phase"))
        except ValueError as exc:
            raise MissionProgressError(f"unsupported progress phase: {exc}") from exc
        if "px4_feedback" in value and value["px4_feedback"] is None:
            raise MissionProgressError("px4_feedback must be an object when present")
        return cls(
            mission_id=require_text(value["mission_id"], "mission_id"),
            mission_hash=require_sha256(value["mission_hash"], "mission_hash"),
            phase=phase,
            observed_at_s=require_real(value["observed_at_s"], "observed_at_s", minimum=0.0),
            mission_item_count=require_int(
                value["mission_item_count"], "mission_item_count", minimum=1
            ),
            current_sequence=(
                None
                if value["current_sequence"] is None
                else require_int(value["current_sequence"], "current_sequence", minimum=0)
            ),
            completed_sequences=tuple(
                require_int(item, "completed_sequences item", minimum=0)
                for item in require_array(value["completed_sequences"], "completed_sequences")
            ),
            active_command=(
                None
                if value["active_command"] is None
                else require_text(value["active_command"], "active_command")
            ),
            distance_to_active_m=optional_real(
                value["distance_to_active_m"], "distance_to_active_m", minimum=0.0
            ),
            altitude_error_m=optional_real(
                value["altitude_error_m"], "altitude_error_m", minimum=0.0
            ),
            dwell_elapsed_s=require_real(value["dwell_elapsed_s"], "dwell_elapsed_s", minimum=0.0),
            progress_fraction=require_real(
                value["progress_fraction"], "progress_fraction", minimum=0.0, maximum=1.0
            ),
            mission_started=require_bool(value["mission_started"], "mission_started"),
            armed=None if value["armed"] is None else require_bool(value["armed"], "armed"),
            link_healthy=None
            if value["link_healthy"] is None
            else require_bool(value["link_healthy"], "link_healthy"),
            gps_quality_valid=require_bool(value["gps_quality_valid"], "gps_quality_valid"),
            gps_age_s=optional_real(value["gps_age_s"], "gps_age_s", minimum=0.0),
            reason=require_text(value["reason"], "reason"),
            px4_feedback=(
                None
                if "px4_feedback" not in value
                else Px4MissionFeedbackSnapshot.from_mapping(value["px4_feedback"])
            ),
            schema_version=require_schema_version(value["schema_version"]),
        )

    def to_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "mission_id": self.mission_id,
            "mission_hash": self.mission_hash,
            "phase": self.phase.value,
            "observed_at_s": self.observed_at_s,
            "mission_item_count": self.mission_item_count,
            "current_sequence": self.current_sequence,
            "completed_sequences": list(self.completed_sequences),
            "active_command": self.active_command,
            "distance_to_active_m": self.distance_to_active_m,
            "altitude_error_m": self.altitude_error_m,
            "dwell_elapsed_s": self.dwell_elapsed_s,
            "progress_fraction": self.progress_fraction,
            "mission_started": self.mission_started,
            "armed": self.armed,
            "link_healthy": self.link_healthy,
            "gps_quality_valid": self.gps_quality_valid,
            "gps_age_s": self.gps_age_s,
            "reason": self.reason,
        }
        if self.px4_feedback is not None:
            result["px4_feedback"] = self.px4_feedback.to_mapping()
        return result

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @property
    def sha256(self) -> str:
        return self.canonical_hash


class MissionProgressTracker:
    """Advance an immutable mission cursor from telemetry observations."""

    def __init__(
        self,
        mission: ExecutableMission,
        *,
        config: MissionProgressConfig | None = None,
        home_latitude_deg: float | None = None,
        home_longitude_deg: float | None = None,
    ) -> None:
        if not isinstance(mission, ExecutableMission):
            raise MissionProgressError("mission must be an ExecutableMission")
        if len(mission.items) > MAXIMUM_PROGRESS_ITEMS:
            raise MissionProgressError("mission exceeds the bounded progress item budget")
        self.mission = mission
        if config is not None and not isinstance(config, MissionProgressConfig):
            raise MissionProgressError("config must be a MissionProgressConfig or None")
        self.config = config or MissionProgressConfig()
        self._validate_mission_items(mission.items)
        first_coordinate = next((item for item in mission.items if _has_coordinates(item)), None)
        if (home_latitude_deg is None) != (home_longitude_deg is None):
            raise MissionProgressError(
                "home_latitude_deg and home_longitude_deg must be supplied together"
            )
        if home_latitude_deg is None and home_longitude_deg is None:
            if first_coordinate is None:
                raise MissionProgressError("mission needs a coordinate item to derive home")
            home_latitude_deg = first_coordinate.latitude_deg
            home_longitude_deg = first_coordinate.longitude_deg
        self.home_latitude_deg = _coordinate(home_latitude_deg, "home_latitude_deg", latitude=True)
        self.home_longitude_deg = _coordinate(
            home_longitude_deg, "home_longitude_deg", latitude=False
        )
        self._started = False
        self._current_sequence = 0
        self._completed: list[int] = []
        self._dwell_started_at_s: float | None = None
        self._last_observed_at_s: float | None = None
        self._last_snapshot: MissionProgressSnapshot | None = None

    @property
    def snapshot(self) -> MissionProgressSnapshot | None:
        return self._last_snapshot

    @property
    def current_sequence(self) -> int | None:
        return None if self._is_complete else self._current_sequence

    @property
    def completed_sequences(self) -> tuple[int, ...]:
        return tuple(self._completed)

    @property
    def is_complete(self) -> bool:
        return self._is_complete

    @property
    def _is_complete(self) -> bool:
        return len(self._completed) == len(self.mission.items)

    def reset(self) -> None:
        self._started = False
        self._current_sequence = 0
        self._completed.clear()
        self._dwell_started_at_s = None
        self._last_observed_at_s = None
        self._last_snapshot = None

    def start(self) -> None:
        """Mark the mission start permit as observed; no control is emitted."""

        self._started = True

    def stop(self) -> None:
        """Stop progress evaluation while retaining the completed cursor."""

        self._started = False
        self._dwell_started_at_s = None

    def update(
        self,
        telemetry: TelemetryLike | Mapping[str, Any],
        *,
        now_s: Real | None = None,
        mission_started: bool | None = None,
        px4_feedback: Px4MissionFeedbackSnapshot | Mapping[str, Any] | None = None,
    ) -> MissionProgressSnapshot:
        observed_at_s = _telemetry_number(telemetry, "observed_at_s", alias="timestamp_s")
        if self._last_observed_at_s is not None and observed_at_s < self._last_observed_at_s:
            raise MissionProgressError("telemetry observed_at_s moved backwards")
        self._last_observed_at_s = observed_at_s
        now = observed_at_s if now_s is None else _finite(now_s, "now_s")
        if now < observed_at_s:
            raise MissionProgressError("now_s cannot precede telemetry observed_at_s")
        feedback = _coerce_px4_feedback(px4_feedback)
        if feedback is not None:
            if (
                feedback.mission_id != self.mission.mission_id
                or feedback.mission_hash != self.mission.canonical_hash
                or feedback.mission_item_count != len(self.mission.items)
            ):
                raise MissionProgressError(
                    "px4_feedback does not match the bound Mission identity"
                )
            if not math.isclose(
                feedback.evaluated_at_s,
                now,
                rel_tol=0.0,
                abs_tol=1.0e-9,
            ):
                raise MissionProgressError(
                    "px4_feedback must be evaluated at the current progress time"
                )

        telemetry_started = _telemetry_optional_bool(telemetry, "mission_started")
        if mission_started is not None:
            if not isinstance(mission_started, bool):
                raise MissionProgressError("mission_started must be bool or None")
            self._started = mission_started
        elif telemetry_started is True:
            # Direct MAVLink-shaped sinks may set the model flag without
            # going through the closed-loop convenience method.
            self._started = True

        link_healthy = _telemetry_optional_bool(telemetry, "link_healthy")
        armed = _telemetry_optional_bool(telemetry, "armed")
        gps_quality, gps_age, gps_reason = _gps_quality(telemetry, now, self.config.gps_max_age_s)

        phase = MissionProgressPhase.READY
        reason = "mission_not_started"
        distance: float | None = None
        altitude_error: float | None = None
        dwell_elapsed = 0.0

        if self._started:
            phase = MissionProgressPhase.ACTIVE
            reason = "mission_active"
            if armed is not True:
                phase = MissionProgressPhase.BLOCKED
                reason = "vehicle_disarmed"
            elif link_healthy is not True:
                phase = MissionProgressPhase.WAITING_FOR_LINK
                reason = "link_not_healthy"
            elif feedback is not None and not feedback.qualified:
                phase = MissionProgressPhase.BLOCKED
                reason = f"px4_{feedback.reason}"
            elif self.config.require_px4_feedback and feedback is None:
                phase = MissionProgressPhase.BLOCKED
                reason = "px4_mission_feedback_required"
            elif self._is_complete:
                phase = MissionProgressPhase.COMPLETE
                reason = "mission_complete"
            else:
                # A bounded loop permits several zero-distance lifecycle
                # items (for example a transition followed by a landing) to
                # settle in one telemetry tick without unbounded work.
                for _ in range(len(self.mission.items)):
                    item = self.mission.items[self._current_sequence]
                    state, distance, altitude_error, dwell_elapsed, reason = self._evaluate_item(
                        item,
                        telemetry,
                        now,
                        gps_quality=gps_quality,
                        gps_age_s=gps_age,
                        gps_reason=gps_reason,
                    )
                    if state == "waiting_for_gps":
                        phase = MissionProgressPhase.WAITING_FOR_GPS
                        break
                    if state != "complete":
                        phase = MissionProgressPhase.ACTIVE
                        break
                    self._completed.append(self._current_sequence)
                    self._current_sequence += 1
                    self._dwell_started_at_s = None
                    distance = None
                    altitude_error = None
                    dwell_elapsed = 0.0
                    if self._is_complete:
                        phase = MissionProgressPhase.COMPLETE
                        reason = "mission_complete"
                        break
                else:  # pragma: no cover - bounded loop always settles
                    raise MissionProgressError("mission progress loop did not settle")

        current_item = None if self._is_complete else self.mission.items[self._current_sequence]
        active_command = None if current_item is None else current_item.command.value
        progress = len(self._completed) / len(self.mission.items)
        snapshot = MissionProgressSnapshot(
            mission_id=self.mission.mission_id,
            mission_hash=self.mission.canonical_hash,
            phase=phase,
            observed_at_s=observed_at_s,
            mission_item_count=len(self.mission.items),
            current_sequence=None if self._is_complete else self._current_sequence,
            completed_sequences=tuple(self._completed),
            active_command=active_command,
            distance_to_active_m=distance,
            altitude_error_m=altitude_error,
            dwell_elapsed_s=dwell_elapsed,
            progress_fraction=progress,
            mission_started=self._started,
            armed=armed,
            link_healthy=link_healthy,
            gps_quality_valid=gps_quality,
            gps_age_s=gps_age,
            reason=reason,
            px4_feedback=feedback,
        )
        self._last_snapshot = snapshot
        return snapshot

    def _evaluate_item(
        self,
        item: MissionItem,
        telemetry: TelemetryLike | Mapping[str, Any],
        now_s: float,
        *,
        gps_quality: bool,
        gps_age_s: float | None,
        gps_reason: str,
    ) -> tuple[str, float | None, float | None, float, str]:
        command = item.command
        if command is MissionCommand.VTOL_TRANSITION:
            flight_mode = _telemetry_text(telemetry, "flight_mode").upper().replace("-", "_")
            expected = "FW" if math.isclose(item.param1, 4.0, abs_tol=1.0e-6) else "MC"
            if flight_mode in {expected, "FIXED_WING" if expected == "FW" else "MULTIROTOR"}:
                return "complete", None, None, 0.0, "vtol_transition_observed"
            return "active", None, None, 0.0, f"awaiting_vtol_{expected.lower()}_transition"

        if command is MissionCommand.RETURN_TO_LAUNCH:
            if not gps_quality:
                return "waiting_for_gps", None, None, 0.0, gps_reason
            latitude = _telemetry_number(telemetry, "latitude_deg")
            longitude = _telemetry_number(telemetry, "longitude_deg")
            distance = wgs84_distance_m(
                latitude1_deg=latitude,
                longitude1_deg=longitude,
                latitude2_deg=self.home_latitude_deg,
                longitude2_deg=self.home_longitude_deg,
            )
            if distance <= self.config.waypoint_radius_m:
                return "complete", distance, None, 0.0, "rtl_home_reached"
            return "active", distance, None, 0.0, "returning_to_home"

        if not _has_coordinates(item):
            raise MissionProgressError(f"unsupported mission item {command.value}")
        if not gps_quality:
            return "waiting_for_gps", None, None, 0.0, gps_reason
        latitude = _telemetry_number(telemetry, "latitude_deg")
        longitude = _telemetry_number(telemetry, "longitude_deg")
        altitude = _telemetry_number(telemetry, "altitude_agl_m")
        distance = wgs84_distance_m(
            latitude1_deg=latitude,
            longitude1_deg=longitude,
            latitude2_deg=float(item.latitude_deg),
            longitude2_deg=float(item.longitude_deg),
        )
        altitude_error = abs(altitude - float(item.altitude_m))
        radius = self._acceptance_radius(item)
        if distance > radius or altitude_error > self.config.altitude_tolerance_m:
            self._dwell_started_at_s = None
            return "active", distance, altitude_error, 0.0, "approaching_active_item"
        if command is MissionCommand.LOITER_TIME:
            duration = max(0.0, float(item.param1))
            if duration <= 0.0:
                return "complete", distance, altitude_error, 0.0, "loiter_complete"
            if self._dwell_started_at_s is None:
                self._dwell_started_at_s = now_s
            dwell_elapsed = max(0.0, now_s - self._dwell_started_at_s)
            if dwell_elapsed + 1.0e-12 >= duration:
                return "complete", distance, altitude_error, dwell_elapsed, "loiter_complete"
            return "active", distance, altitude_error, dwell_elapsed, "loitering"
        return "complete", distance, altitude_error, 0.0, "acceptance_radius_reached"

    def _acceptance_radius(self, item: MissionItem) -> float:
        if item.command is MissionCommand.LOITER_TIME and item.param3 > 0.0:
            return float(item.param3)
        if item.command is MissionCommand.WAYPOINT and item.param2 > 0.0:
            return float(item.param2)
        return self.config.waypoint_radius_m

    @staticmethod
    def _validate_mission_items(items: tuple[MissionItem, ...]) -> None:
        for index, item in enumerate(items):
            if item.sequence != index:
                raise MissionProgressError("mission item sequence is not contiguous")
            if _has_coordinates(item):
                if (
                    item.latitude_deg is None
                    or item.longitude_deg is None
                    or item.altitude_m is None
                ):
                    raise MissionProgressError("coordinate mission item is incomplete")
                if not -90.0 < item.latitude_deg < 90.0:
                    raise MissionProgressError("mission progress rejects exact-pole coordinates")
                if not -180.0 <= item.longitude_deg <= 180.0 or item.altitude_m < 0.0:
                    raise MissionProgressError(
                        "mission item coordinate is outside the progress domain"
                    )


def _has_coordinates(item: MissionItem) -> bool:
    return (
        item.latitude_deg is not None
        or item.longitude_deg is not None
        or item.altitude_m is not None
    )


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
        raise MissionProgressError(f"{name} must be finite numeric")
    return float(value)


def _coordinate(value: object, name: str, *, latitude: bool) -> float:
    result = _finite(value, name)
    if latitude and not -90.0 < result < 90.0:
        raise MissionProgressError(f"{name} must be strictly within (-90, 90)")
    if not latitude and not -180.0 <= result <= 180.0:
        raise MissionProgressError(f"{name} must be within [-180, 180]")
    return result


def _telemetry_value(
    telemetry: TelemetryLike | Mapping[str, Any], name: str, *, alias: str | None = None
) -> Any:
    if isinstance(telemetry, Mapping):
        if name in telemetry:
            return telemetry[name]
        if alias is not None and alias in telemetry:
            return telemetry[alias]
        raise MissionProgressError(f"telemetry is missing {name}")
    if hasattr(telemetry, name):
        return getattr(telemetry, name)
    if alias is not None and hasattr(telemetry, alias):
        return getattr(telemetry, alias)
    raise MissionProgressError(f"telemetry is missing {name}")


def _telemetry_number(
    telemetry: TelemetryLike | Mapping[str, Any], name: str, *, alias: str | None = None
) -> float:
    return _finite(_telemetry_value(telemetry, name, alias=alias), f"telemetry.{name}")


def _telemetry_optional_bool(
    telemetry: TelemetryLike | Mapping[str, Any], name: str
) -> bool | None:
    try:
        value = _telemetry_value(telemetry, name)
    except MissionProgressError:
        return None
    if value is None:
        return None
    if type(value) is not bool:
        raise MissionProgressError(f"telemetry.{name} must be bool or None")
    return value


def _telemetry_text(telemetry: TelemetryLike | Mapping[str, Any], name: str) -> str:
    value = _telemetry_value(telemetry, name)
    if not isinstance(value, str) or not value.strip():
        raise MissionProgressError(f"telemetry.{name} must be a non-empty string")
    return value.strip()


def _gps_quality(
    telemetry: TelemetryLike | Mapping[str, Any], now_s: float, maximum_age_s: float
) -> tuple[bool, float | None, str]:
    try:
        fix_type = _telemetry_value(telemetry, "gps_fix_type")
        satellites = _telemetry_value(telemetry, "satellites_visible")
        latitude = _telemetry_number(telemetry, "latitude_deg")
        longitude = _telemetry_number(telemetry, "longitude_deg")
        horizontal_accuracy = _telemetry_number(telemetry, "gps_horizontal_accuracy_m")
        vertical_accuracy = _telemetry_number(telemetry, "gps_vertical_accuracy_m")
        gps_observed = _telemetry_value(telemetry, "gps_observed_at_s")
    except MissionProgressError:
        return False, None, "gps_fields_unavailable"
    if gps_observed is None:
        return False, None, "gps_timestamp_unavailable"
    try:
        gps_timestamp = _finite(gps_observed, "telemetry.gps_observed_at_s")
    except MissionProgressError:
        return False, None, "gps_timestamp_invalid"
    age = now_s - gps_timestamp
    if age < 0.0:
        return False, None, "gps_timestamp_in_future"
    if age > maximum_age_s:
        return False, age, "gps_sample_stale"
    if (
        isinstance(fix_type, bool)
        or not isinstance(fix_type, int)
        or not MAVLINK_QUALIFIED_FIX_TYPE_MIN <= fix_type <= MAVLINK_GPS_FIX_TYPE_MAX
    ):
        return False, age, "gps_fix_unqualified"
    if (
        isinstance(satellites, bool)
        or not isinstance(satellites, int)
        or not MAVLINK_QUALIFIED_SATELLITES_MIN <= satellites <= MAVLINK_SATELLITES_MAX
    ):
        return False, age, "gps_satellites_unqualified"
    if not -90.0 < latitude < 90.0 or not -180.0 <= longitude <= 180.0:
        return False, age, "gps_coordinate_invalid"
    if not 0.0 <= horizontal_accuracy <= GNSS_CANONICAL_MAX_HORIZONTAL_ACCURACY_M:
        return False, age, "gps_horizontal_accuracy_unqualified"
    if not 0.0 <= vertical_accuracy <= GNSS_CANONICAL_MAX_VERTICAL_ACCURACY_M:
        return False, age, "gps_vertical_accuracy_unqualified"
    return True, age, "gps_qualified"


def _coerce_px4_feedback(
    value: Px4MissionFeedbackSnapshot | Mapping[str, Any] | None,
) -> Px4MissionFeedbackSnapshot | None:
    if value is None:
        return None
    if isinstance(value, Px4MissionFeedbackSnapshot):
        return value
    if isinstance(value, Mapping):
        return Px4MissionFeedbackSnapshot.from_mapping(value)
    raise MissionProgressError(
        "px4_feedback must be a Px4MissionFeedbackSnapshot, mapping, or None"
    )


__all__ = [
    "MAXIMUM_PROGRESS_ITEMS",
    "MISSION_PROGRESS_SCHEMA_VERSION",
    "MissionProgressConfig",
    "MissionProgressError",
    "MissionProgressPhase",
    "MissionProgressSnapshot",
    "MissionProgressTracker",
    "TelemetryLike",
]
