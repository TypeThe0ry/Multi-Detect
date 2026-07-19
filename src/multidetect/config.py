from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from math import isfinite
from numbers import Real
from pathlib import Path
from typing import Any

from .compat import StrEnum
from .domain import ConfigurationError


def _require_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _require_bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise ConfigurationError(f"{field_name} must be a boolean")
    return value


def _require_real(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ConfigurationError(f"{field_name} must be a number")
    return float(value)


def _require_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{field_name} must be an integer")
    return value


def _require_string_sequence(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ConfigurationError(f"{field_name} must be an array of non-empty strings")
    return tuple(
        _require_string(item, f"{field_name}[{index}]").lower() for index, item in enumerate(value)
    )


class MissionType(StrEnum):
    FIRE_SUPPRESSION = "fire_suppression"
    RESCUE_SUPPLY = "rescue_supply"
    COMMUNICATION_RELAY = "communication_relay"
    ENVIRONMENTAL_SENSOR = "environmental_sensor"
    AGRICULTURE = "agriculture"
    INDUSTRIAL_SENSOR = "industrial_sensor"
    MULTIPOINT_LOGISTICS = "multipoint_logistics"
    SEARCH_AND_RESCUE = "search_and_rescue"


class PlatformMode(StrEnum):
    DISPOSABLE = "disposable"
    MULTI_DEPLOYMENT = "multi_deployment"


class CompletionPolicy(StrEnum):
    TERMINATE_AFTER_FIRST = "terminate_after_first"
    CONTINUE_WHILE_PAYLOAD_AVAILABLE = "continue_while_payload_available"


SAFE_PAYLOADS: Mapping[MissionType, frozenset[str]] = {
    MissionType.FIRE_SUPPRESSION: frozenset(
        {"fire_suppression_agent", "fire_suppression_ball", "fire_suppression_bag"}
    ),
    MissionType.RESCUE_SUPPLY: frozenset(
        {"first_aid_kit", "life_jacket", "rescue_rope", "emergency_radio"}
    ),
    MissionType.COMMUNICATION_RELAY: frozenset(
        {"communication_relay", "non_combustion_emergency_light"}
    ),
    MissionType.ENVIRONMENTAL_SENSOR: frozenset(
        {"environmental_sensor", "weather_sensor", "air_quality_sensor"}
    ),
    MissionType.AGRICULTURE: frozenset({"seed_packet", "pest_trap", "biological_control_payload"}),
    MissionType.INDUSTRIAL_SENSOR: frozenset(
        {"sensor_node", "location_marker", "inspection_device"}
    ),
    MissionType.MULTIPOINT_LOGISTICS: frozenset({"small_parcel"}),
    MissionType.SEARCH_AND_RESCUE: frozenset(
        {"first_aid_kit", "life_jacket", "emergency_radio", "communication_relay"}
    ),
}

PROHIBITED_TERMS = frozenset(
    {
        "weapon",
        "munition",
        "ammunition",
        "explosive",
        "incendiary",
        "attack",
        "bomb",
        "missile",
        "武器",
        "弹药",
        "爆炸物",
        "燃烧装置",
        "攻击",
        "导弹",
    }
)


@dataclass(frozen=True, slots=True)
class PayloadSpec:
    slot_id: str
    payload_type: str

    def __post_init__(self) -> None:
        _require_string(self.slot_id, "payload slot_id")
        _require_string(self.payload_type, "payload_type")


@dataclass(frozen=True, slots=True)
class SafetyLimits:
    minimum_altitude_agl_m: float = 8.0
    maximum_altitude_agl_m: float = 60.0
    maximum_abs_roll_deg: float = 12.0
    maximum_abs_pitch_deg: float = 12.0
    minimum_ground_speed_mps: float = 0.0
    maximum_ground_speed_mps: float = 3.0
    person_exclusion_margin_normalized: float = 0.08
    sensor_data_max_age_seconds: float = 1.0
    authorization_ttl_seconds: float = 10.0
    release_confirmation_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        numeric_limits = (
            self.minimum_altitude_agl_m,
            self.maximum_altitude_agl_m,
            self.maximum_abs_roll_deg,
            self.maximum_abs_pitch_deg,
            self.minimum_ground_speed_mps,
            self.maximum_ground_speed_mps,
            self.person_exclusion_margin_normalized,
            self.sensor_data_max_age_seconds,
            self.authorization_ttl_seconds,
            self.release_confirmation_timeout_seconds,
        )
        if any(isinstance(value, bool) or not isinstance(value, Real) for value in numeric_limits):
            raise ConfigurationError("safety limits must be numbers")
        if not all(isfinite(value) for value in numeric_limits):
            raise ConfigurationError("safety limits must be finite")
        if self.minimum_altitude_agl_m < 0:
            raise ConfigurationError("minimum altitude cannot be negative")
        if self.maximum_altitude_agl_m <= self.minimum_altitude_agl_m:
            raise ConfigurationError("maximum altitude must exceed minimum altitude")
        if self.maximum_abs_roll_deg <= 0 or self.maximum_abs_pitch_deg <= 0:
            raise ConfigurationError("attitude limits must be positive")
        if self.minimum_ground_speed_mps < 0:
            raise ConfigurationError("minimum ground speed cannot be negative")
        if self.maximum_ground_speed_mps < self.minimum_ground_speed_mps:
            raise ConfigurationError("maximum ground speed must not be below minimum ground speed")
        if not 0 <= self.person_exclusion_margin_normalized <= 0.5:
            raise ConfigurationError("person exclusion margin must be in [0, 0.5]")
        if (
            min(
                self.sensor_data_max_age_seconds,
                self.authorization_ttl_seconds,
                self.release_confirmation_timeout_seconds,
            )
            <= 0
        ):
            raise ConfigurationError("timeouts must be positive")


@dataclass(frozen=True, slots=True)
class FixedWingReleaseWindowConfig:
    """Multimodal ballistic release-window model for software HIL only."""

    calibration_id: str
    camera_horizontal_fov_deg: float
    camera_vertical_fov_deg: float
    camera_mount_down_angle_deg: float
    payload_descent_time_factor: float
    command_to_release_latency_seconds: float
    maximum_cross_track_error_m: float
    release_window_half_length_m: float
    minimum_depression_angle_deg: float = 5.0
    maximum_depression_angle_deg: float = 85.0
    gravity_mps2: float = 9.80665
    require_multimodal_range: bool = False
    maximum_range_age_s: float = 0.5
    minimum_range_target_iou: float = 0.5
    minimum_range_sensor_consistency: float = 0.65
    payload_mass_kg: float = 0.55
    payload_mass_sigma_kg: float = 0.03
    drag_coefficient: float = 0.85
    drag_coefficient_sigma: float = 0.10
    reference_area_m2: float = 0.018
    air_density_kg_m3: float = 1.225
    wind_sigma_mps: float = 0.8
    ground_velocity_sigma_mps: float = 0.4
    altitude_sigma_m: float = 0.8
    release_latency_sigma_s: float = 0.05
    maximum_air_data_disagreement_mps: float = 4.0
    maximum_error_ellipse_major_m: float = 12.0
    maximum_error_ellipse_minor_m: float = 8.0
    integration_step_seconds: float = 0.01
    maximum_flight_time_seconds: float = 15.0
    hil_only: bool = True

    def __post_init__(self) -> None:
        if not self.calibration_id.strip():
            raise ConfigurationError("fixed-wing release-window calibration_id cannot be empty")
        values = (
            self.camera_horizontal_fov_deg,
            self.camera_vertical_fov_deg,
            self.camera_mount_down_angle_deg,
            self.payload_descent_time_factor,
            self.command_to_release_latency_seconds,
            self.maximum_cross_track_error_m,
            self.release_window_half_length_m,
            self.minimum_depression_angle_deg,
            self.maximum_depression_angle_deg,
            self.gravity_mps2,
            self.maximum_range_age_s,
            self.minimum_range_target_iou,
            self.minimum_range_sensor_consistency,
            self.payload_mass_kg,
            self.payload_mass_sigma_kg,
            self.drag_coefficient,
            self.drag_coefficient_sigma,
            self.reference_area_m2,
            self.air_density_kg_m3,
            self.wind_sigma_mps,
            self.ground_velocity_sigma_mps,
            self.altitude_sigma_m,
            self.release_latency_sigma_s,
            self.maximum_air_data_disagreement_mps,
            self.maximum_error_ellipse_major_m,
            self.maximum_error_ellipse_minor_m,
            self.integration_step_seconds,
            self.maximum_flight_time_seconds,
        )
        if not all(isfinite(value) for value in values):
            raise ConfigurationError("fixed-wing release-window values must be finite")
        if not 0.0 < self.camera_horizontal_fov_deg < 180.0:
            raise ConfigurationError("camera horizontal FOV must be in (0, 180) degrees")
        if not 0.0 < self.camera_vertical_fov_deg < 180.0:
            raise ConfigurationError("camera vertical FOV must be in (0, 180) degrees")
        if not 0.0 < self.camera_mount_down_angle_deg < 90.0:
            raise ConfigurationError("camera mount down angle must be in (0, 90) degrees")
        if self.payload_descent_time_factor <= 0.0:
            raise ConfigurationError("payload descent time factor must be positive")
        if self.command_to_release_latency_seconds < 0.0:
            raise ConfigurationError("command-to-release latency cannot be negative")
        if self.maximum_cross_track_error_m <= 0.0:
            raise ConfigurationError("maximum cross-track error must be positive")
        if self.release_window_half_length_m <= 0.0:
            raise ConfigurationError("release window half-length must be positive")
        if not (0.0 < self.minimum_depression_angle_deg < self.maximum_depression_angle_deg < 90.0):
            raise ConfigurationError(
                "depression-angle limits must satisfy 0 < minimum < maximum < 90"
            )
        if self.gravity_mps2 <= 0.0:
            raise ConfigurationError("gravity must be positive")
        if not isinstance(self.require_multimodal_range, bool):
            raise ConfigurationError("require_multimodal_range must be a boolean")
        for name, value in (
            ("maximum_range_age_s", self.maximum_range_age_s),
            ("payload_mass_kg", self.payload_mass_kg),
            ("drag_coefficient", self.drag_coefficient),
            ("reference_area_m2", self.reference_area_m2),
            ("air_density_kg_m3", self.air_density_kg_m3),
            ("maximum_air_data_disagreement_mps", self.maximum_air_data_disagreement_mps),
            ("maximum_error_ellipse_major_m", self.maximum_error_ellipse_major_m),
            ("maximum_error_ellipse_minor_m", self.maximum_error_ellipse_minor_m),
            ("integration_step_seconds", self.integration_step_seconds),
            ("maximum_flight_time_seconds", self.maximum_flight_time_seconds),
        ):
            if value <= 0.0:
                raise ConfigurationError(f"{name} must be positive")
        for name, value in (
            ("payload_mass_sigma_kg", self.payload_mass_sigma_kg),
            ("drag_coefficient_sigma", self.drag_coefficient_sigma),
            ("wind_sigma_mps", self.wind_sigma_mps),
            ("ground_velocity_sigma_mps", self.ground_velocity_sigma_mps),
            ("altitude_sigma_m", self.altitude_sigma_m),
            ("release_latency_sigma_s", self.release_latency_sigma_s),
        ):
            if value < 0.0:
                raise ConfigurationError(f"{name} cannot be negative")
        if not 0.0 < self.minimum_range_target_iou <= 1.0:
            raise ConfigurationError("minimum_range_target_iou must be in (0, 1]")
        if not 0.0 <= self.minimum_range_sensor_consistency <= 1.0:
            raise ConfigurationError("minimum_range_sensor_consistency must be in [0, 1]")
        if self.payload_mass_sigma_kg >= self.payload_mass_kg:
            raise ConfigurationError("payload mass uncertainty must be below payload mass")
        if self.drag_coefficient_sigma >= self.drag_coefficient:
            raise ConfigurationError("drag uncertainty must be below drag coefficient")
        if self.integration_step_seconds > 0.05:
            raise ConfigurationError("integration_step_seconds must not exceed 0.05")
        if not self.hil_only:
            raise ConfigurationError("fixed-wing release-window model is restricted to HIL")


@dataclass(frozen=True, slots=True)
class MissionConfig:
    mission_id: str
    mission_type: MissionType
    platform_mode: PlatformMode
    payloads: tuple[PayloadSpec, ...]
    target_classes: tuple[str, ...]
    human_authorization_required: bool = True
    minimum_confidence: float = 0.82
    minimum_track_time_seconds: float = 3.0
    minimum_track_observations: int = 4
    maximum_track_gap_seconds: float = 1.0
    target_reengagement_cooldown_seconds: float = 300.0
    person_exclusion_enabled: bool = True
    require_independent_rgb_corroboration: bool = True
    require_thermal_corroboration: bool = False
    completion_policy: CompletionPolicy = CompletionPolicy.CONTINUE_WHILE_PAYLOAD_AVAILABLE
    safety: SafetyLimits = field(default_factory=SafetyLimits)
    fixed_wing_release_window: FixedWingReleaseWindowConfig | None = None
    person_labels: tuple[str, ...] = ("person", "firefighter")
    ruleset_version: str = "safety-rules-v1"

    def __post_init__(self) -> None:
        _require_string(self.mission_id, "mission_id")
        _require_string(self.ruleset_version, "ruleset_version")
        if not isinstance(self.mission_type, MissionType):
            raise ConfigurationError("mission_type must be a supported mission type")
        if not isinstance(self.platform_mode, PlatformMode):
            raise ConfigurationError("platform_mode must be a supported platform mode")
        if not isinstance(self.completion_policy, CompletionPolicy):
            raise ConfigurationError("completion_policy must be a supported policy")
        if not isinstance(self.safety, SafetyLimits):
            raise ConfigurationError("safety must be a SafetyLimits object")
        if self.fixed_wing_release_window is not None and not isinstance(
            self.fixed_wing_release_window, FixedWingReleaseWindowConfig
        ):
            raise ConfigurationError(
                "fixed_wing_release_window must be a FixedWingReleaseWindowConfig object"
            )
        if any(not isinstance(payload, PayloadSpec) for payload in self.payloads):
            raise ConfigurationError("payloads must contain PayloadSpec objects")
        if not self.target_classes:
            raise ConfigurationError("at least one target class is required")
        normalized_target_classes = tuple(
            _require_string(label, f"target_classes[{index}]").lower()
            for index, label in enumerate(self.target_classes)
        )
        normalized_person_labels = tuple(
            _require_string(label, f"person_labels[{index}]").lower()
            for index, label in enumerate(self.person_labels)
        )
        object.__setattr__(self, "target_classes", normalized_target_classes)
        object.__setattr__(self, "person_labels", normalized_person_labels)
        _require_bool(self.human_authorization_required, "human_authorization_required")
        _require_bool(self.person_exclusion_enabled, "person_exclusion_enabled")
        _require_bool(
            self.require_independent_rgb_corroboration,
            "require_independent_rgb_corroboration",
        )
        _require_bool(self.require_thermal_corroboration, "require_thermal_corroboration")
        if self.human_authorization_required is not True:
            raise ConfigurationError("MVP requires human_authorization_required=true")
        if self.person_exclusion_enabled and not self.person_labels:
            raise ConfigurationError(
                "person_labels cannot be empty when person exclusion is enabled"
            )
        numeric_values = (
            self.minimum_confidence,
            self.minimum_track_time_seconds,
            self.maximum_track_gap_seconds,
            self.target_reengagement_cooldown_seconds,
        )
        if any(isinstance(value, bool) or not isinstance(value, Real) for value in numeric_values):
            raise ConfigurationError("mission numeric settings must be numbers")
        if not 0.0 <= self.minimum_confidence <= 1.0:
            raise ConfigurationError("minimum_confidence must be in [0, 1]")
        track_timing = (
            self.minimum_track_time_seconds,
            self.maximum_track_gap_seconds,
            self.target_reengagement_cooldown_seconds,
        )
        if not all(isfinite(value) for value in track_timing):
            raise ConfigurationError("track timing values must be finite")
        if (
            self.minimum_track_time_seconds < 0
            or self.maximum_track_gap_seconds <= 0
            or self.target_reengagement_cooldown_seconds <= 0
        ):
            raise ConfigurationError("track timing values are invalid")
        if isinstance(self.minimum_track_observations, bool) or not isinstance(
            self.minimum_track_observations, int
        ):
            raise ConfigurationError("minimum_track_observations must be an integer")
        if self.minimum_track_observations < 2:
            raise ConfigurationError("minimum_track_observations must be at least 2")
        slot_ids = [payload.slot_id for payload in self.payloads]
        if len(slot_ids) != len(set(slot_ids)):
            raise ConfigurationError("payload slot_id values must be unique")
        allowed = SAFE_PAYLOADS[self.mission_type]
        for payload in self.payloads:
            lowered = payload.payload_type.lower()
            if any(term in lowered for term in PROHIBITED_TERMS):
                raise ConfigurationError(f"prohibited payload type: {payload.payload_type}")
            if payload.payload_type not in allowed:
                raise ConfigurationError(
                    f"payload {payload.payload_type!r} is not allowed for {self.mission_type.value}"
                )
        if self.fixed_wing_release_window is not None:
            if self.mission_type is not MissionType.FIRE_SUPPRESSION:
                raise ConfigurationError(
                    "fixed_wing_release_window is only supported for fire_suppression"
                )
            if not self.payloads:
                raise ConfigurationError(
                    "fixed_wing_release_window requires an installed fire-suppression payload"
                )
        if self.platform_mode is PlatformMode.DISPOSABLE:
            if len(self.payloads) != 1:
                raise ConfigurationError("disposable platform must contain exactly one payload")
            if self.completion_policy is not CompletionPolicy.TERMINATE_AFTER_FIRST:
                raise ConfigurationError(
                    "disposable platform requires completion_policy=terminate_after_first"
                )

    @property
    def payload_installed(self) -> bool:
        """Whether this flight configuration contains any task payload."""

        return bool(self.payloads)

    @property
    def deployment_capable(self) -> bool:
        """Whether the mission may enter the simulated deployment workflow."""

        return self.payload_installed

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> MissionConfig:
        if not isinstance(raw, Mapping):
            raise ConfigurationError("mission configuration must be an object")
        required_keys = {
            "mission_id",
            "mission_type",
            "platform_mode",
            "payloads",
            "target_classes",
            "human_authorization_required",
        }
        allowed_keys = required_keys | {
            "payload_count",
            "minimum_confidence",
            "minimum_track_time_seconds",
            "minimum_track_observations",
            "maximum_track_gap_seconds",
            "target_reengagement_cooldown_seconds",
            "person_exclusion_enabled",
            "person_labels",
            "require_independent_rgb_corroboration",
            "require_thermal_corroboration",
            "completion_policy",
            "ruleset_version",
            "safety",
            "fixed_wing_release_window",
        }
        missing_keys = sorted(required_keys - raw.keys())
        if missing_keys:
            raise ConfigurationError(f"missing required configuration key: {missing_keys[0]}")
        extra_keys = sorted(str(key) for key in raw.keys() - allowed_keys)
        if extra_keys:
            raise ConfigurationError(f"unknown configuration key: {extra_keys[0]}")

        payload_items = raw.get("payloads")
        if not isinstance(payload_items, Sequence) or isinstance(
            payload_items, (str, bytes, bytearray)
        ):
            raise ConfigurationError("payloads must be an array")
        payloads_list: list[PayloadSpec] = []
        for index, item in enumerate(payload_items):
            if not isinstance(item, Mapping):
                raise ConfigurationError(f"payloads[{index}] must be an object")
            try:
                payloads_list.append(PayloadSpec(**dict(item)))
            except (TypeError, ValueError) as exc:
                if isinstance(exc, ConfigurationError):
                    raise
                raise ConfigurationError(f"payloads[{index}]: {exc}") from exc
        payloads = tuple(payloads_list)
        declared_count = raw.get("payload_count")
        if declared_count is not None:
            declared_count = _require_int(declared_count, "payload_count")
            if declared_count < 0:
                raise ConfigurationError("payload_count cannot be negative")
            if declared_count != len(payloads):
                raise ConfigurationError("payload_count does not match payloads")
        safety_raw = raw.get("safety", {})
        if not isinstance(safety_raw, Mapping):
            raise ConfigurationError("safety must be an object")
        release_window_raw = raw.get("fixed_wing_release_window")
        if release_window_raw is not None and not isinstance(release_window_raw, Mapping):
            raise ConfigurationError("fixed_wing_release_window must be an object")
        try:
            safety = SafetyLimits(**dict(safety_raw))
            release_window = (
                FixedWingReleaseWindowConfig(**dict(release_window_raw))
                if release_window_raw is not None
                else None
            )
            return cls(
                mission_id=_require_string(raw["mission_id"], "mission_id"),
                mission_type=MissionType(_require_string(raw["mission_type"], "mission_type")),
                platform_mode=PlatformMode(_require_string(raw["platform_mode"], "platform_mode")),
                payloads=payloads,
                target_classes=_require_string_sequence(raw["target_classes"], "target_classes"),
                human_authorization_required=_require_bool(
                    raw["human_authorization_required"], "human_authorization_required"
                ),
                minimum_confidence=_require_real(
                    raw.get("minimum_confidence", 0.82), "minimum_confidence"
                ),
                minimum_track_time_seconds=_require_real(
                    raw.get("minimum_track_time_seconds", 3.0),
                    "minimum_track_time_seconds",
                ),
                minimum_track_observations=_require_int(
                    raw.get("minimum_track_observations", 4), "minimum_track_observations"
                ),
                maximum_track_gap_seconds=_require_real(
                    raw.get("maximum_track_gap_seconds", 1.0), "maximum_track_gap_seconds"
                ),
                target_reengagement_cooldown_seconds=_require_real(
                    raw.get("target_reengagement_cooldown_seconds", 300.0),
                    "target_reengagement_cooldown_seconds",
                ),
                person_exclusion_enabled=_require_bool(
                    raw.get("person_exclusion_enabled", True), "person_exclusion_enabled"
                ),
                require_independent_rgb_corroboration=_require_bool(
                    raw.get("require_independent_rgb_corroboration", True),
                    "require_independent_rgb_corroboration",
                ),
                require_thermal_corroboration=_require_bool(
                    raw.get("require_thermal_corroboration", False),
                    "require_thermal_corroboration",
                ),
                completion_policy=CompletionPolicy(
                    raw.get("completion_policy", "continue_while_payload_available")
                ),
                safety=safety,
                fixed_wing_release_window=release_window,
                person_labels=_require_string_sequence(
                    raw.get("person_labels", ("person", "firefighter")), "person_labels"
                ),
                ruleset_version=_require_string(
                    raw.get("ruleset_version", "safety-rules-v1"), "ruleset_version"
                ),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ConfigurationError):
                raise
            raise ConfigurationError(str(exc)) from exc

    @classmethod
    def from_json(cls, path: str | Path) -> MissionConfig:
        try:
            with Path(path).open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ConfigurationError(
                f"invalid mission configuration JSON at line {exc.lineno}, column {exc.colno}"
            ) from exc
        if not isinstance(raw, dict):
            raise ConfigurationError("mission configuration must be a JSON object")
        return cls.from_mapping(raw)
