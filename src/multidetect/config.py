from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from pathlib import Path
from typing import Any

from .domain import ConfigurationError


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
        if not self.slot_id.strip():
            raise ConfigurationError("payload slot_id cannot be empty")
        if not self.payload_type.strip():
            raise ConfigurationError("payload_type cannot be empty")


@dataclass(frozen=True, slots=True)
class SafetyLimits:
    minimum_altitude_agl_m: float = 8.0
    maximum_altitude_agl_m: float = 60.0
    maximum_abs_roll_deg: float = 12.0
    maximum_abs_pitch_deg: float = 12.0
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
            self.maximum_ground_speed_mps,
            self.person_exclusion_margin_normalized,
            self.sensor_data_max_age_seconds,
            self.authorization_ttl_seconds,
            self.release_confirmation_timeout_seconds,
        )
        if not all(isfinite(value) for value in numeric_limits):
            raise ConfigurationError("safety limits must be finite")
        if self.minimum_altitude_agl_m < 0:
            raise ConfigurationError("minimum altitude cannot be negative")
        if self.maximum_altitude_agl_m <= self.minimum_altitude_agl_m:
            raise ConfigurationError("maximum altitude must exceed minimum altitude")
        if self.maximum_abs_roll_deg <= 0 or self.maximum_abs_pitch_deg <= 0:
            raise ConfigurationError("attitude limits must be positive")
        if self.maximum_ground_speed_mps < 0:
            raise ConfigurationError("ground speed limit cannot be negative")
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
    require_thermal_corroboration: bool = True
    completion_policy: CompletionPolicy = CompletionPolicy.CONTINUE_WHILE_PAYLOAD_AVAILABLE
    safety: SafetyLimits = field(default_factory=SafetyLimits)
    person_labels: tuple[str, ...] = ("person", "firefighter")
    ruleset_version: str = "safety-rules-v1"

    def __post_init__(self) -> None:
        if not self.mission_id.strip():
            raise ConfigurationError("mission_id cannot be empty")
        if not self.target_classes:
            raise ConfigurationError("at least one target class is required")
        if not self.human_authorization_required:
            raise ConfigurationError("MVP requires human_authorization_required=true")
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
        payload_items = raw.get("payloads")
        if payload_items is None:
            raise ConfigurationError("payloads is required")
        payloads = tuple(PayloadSpec(**item) for item in payload_items)
        declared_count = raw.get("payload_count")
        if declared_count is not None and declared_count != len(payloads):
            raise ConfigurationError("payload_count does not match payloads")
        safety = SafetyLimits(**raw.get("safety", {}))
        try:
            return cls(
                mission_id=str(raw["mission_id"]),
                mission_type=MissionType(raw["mission_type"]),
                platform_mode=PlatformMode(raw.get("platform_mode", "multi_deployment")),
                payloads=payloads,
                target_classes=tuple(label.lower() for label in raw["target_classes"]),
                human_authorization_required=raw.get("human_authorization_required", True),
                minimum_confidence=float(raw.get("minimum_confidence", 0.82)),
                minimum_track_time_seconds=float(raw.get("minimum_track_time_seconds", 3.0)),
                minimum_track_observations=int(raw.get("minimum_track_observations", 4)),
                maximum_track_gap_seconds=float(raw.get("maximum_track_gap_seconds", 1.0)),
                target_reengagement_cooldown_seconds=float(
                    raw.get("target_reengagement_cooldown_seconds", 300.0)
                ),
                person_exclusion_enabled=bool(raw.get("person_exclusion_enabled", True)),
                require_thermal_corroboration=bool(raw.get("require_thermal_corroboration", True)),
                completion_policy=CompletionPolicy(
                    raw.get("completion_policy", "continue_while_payload_available")
                ),
                safety=safety,
                person_labels=tuple(raw.get("person_labels", ("person", "firefighter"))),
                ruleset_version=str(raw.get("ruleset_version", "safety-rules-v1")),
            )
        except KeyError as exc:
            raise ConfigurationError(f"missing required configuration key: {exc.args[0]}") from exc
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ConfigurationError):
                raise
            raise ConfigurationError(str(exc)) from exc

    @classmethod
    def from_json(cls, path: str | Path) -> MissionConfig:
        with Path(path).open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict):
            raise ConfigurationError("mission configuration must be a JSON object")
        return cls.from_mapping(raw)
