"""Immutable, read-only target evidence cards.

The QGC card is a presentation of one already-qualified observation.  This
module provides the matching typed artifact for SIM/REPLAY and field tooling:
it binds the target event metadata to the geolocation result that was produced
by the canonical WGS-84 projector, while making an unavailable result explicit
instead of inventing a coordinate.

Evidence cards never contain mission, actuator, permit, or truth fields and
therefore cannot be used as a flight-control input.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .autonomy.serialization import (
    AutonomyContractError,
    canonical_sha256,
    optional_real,
    optional_text,
    require_bool,
    require_int,
    require_real,
    require_schema_version,
    require_sha256,
    require_text,
    strict_object,
)
from .target_event_replay import GPS_QUALIFIED_MAX_AGE_S, TargetEvent
from .target_geolocation import TargetGeolocation

SCHEMA = "multidetect.target-evidence-card.v1"
SCHEMA_VERSION = 1
_VALID_RANGE = frozenset({"VALID", "DEGRADED", "UNAVAILABLE"})
_VALID_GPS = frozenset({"qualified", "unqualified", "unavailable"})
_VALID_FRESHNESS = frozenset({"fresh", "stale", "unknown"})


class TargetEvidenceCardError(AutonomyContractError):
    """Raised when a target evidence card is incomplete or tampered with."""


def _optional_sha(value: object, name: str) -> str | None:
    if value is None:
        return None
    return require_sha256(value, name)


@dataclass(frozen=True, slots=True)
class TargetEvidenceCard:
    """One deterministic target card suitable for map display or local save."""

    card_id: str
    event_id: str
    frame_id: str
    target_id: str
    target_label: str
    captured_at_s: float
    available: bool
    unavailable_reason: str | None
    latitude_deg: float | None
    longitude_deg: float | None
    horizontal_sigma_m: float | None
    gps_status: str
    gps_freshness: str
    gps_fix_type: int | None
    gps_satellites_visible: int | None
    gps_horizontal_accuracy_m: float | None
    gps_vertical_accuracy_m: float | None
    range_validity: str
    range_m: float | None
    relative_bearing_deg: float | None
    source_frame_id: str | None = None
    run_identity_sha256: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", require_schema_version(self.schema_version))
        for value, name in (
            (self.card_id, "card_id"),
            (self.event_id, "event_id"),
            (self.frame_id, "frame_id"),
            (self.target_id, "target_id"),
            (self.target_label, "target_label"),
        ):
            require_text(value, name)
        object.__setattr__(
            self, "captured_at_s", require_real(self.captured_at_s, "captured_at_s", minimum=0.0)
        )
        object.__setattr__(self, "available", require_bool(self.available, "available"))
        if self.unavailable_reason is not None:
            object.__setattr__(
                self,
                "unavailable_reason",
                require_text(self.unavailable_reason, "unavailable_reason"),
            )
        if self.available:
            if self.unavailable_reason is not None:
                raise TargetEvidenceCardError("available card cannot contain unavailable_reason")
            for value, name in (
                (self.latitude_deg, "latitude_deg"),
                (self.longitude_deg, "longitude_deg"),
                (self.horizontal_sigma_m, "horizontal_sigma_m"),
            ):
                if value is None:
                    raise TargetEvidenceCardError(f"available card requires {name}")
            latitude = require_real(self.latitude_deg, "latitude_deg", minimum=-90.0, maximum=90.0)
            if not -90.0 < latitude < 90.0:
                raise TargetEvidenceCardError("available card latitude cannot be a pole")
            object.__setattr__(self, "latitude_deg", latitude)
            object.__setattr__(
                self,
                "longitude_deg",
                require_real(self.longitude_deg, "longitude_deg", minimum=-180.0, maximum=180.0),
            )
            object.__setattr__(
                self,
                "horizontal_sigma_m",
                require_real(
                    self.horizontal_sigma_m, "horizontal_sigma_m", minimum=0.0, maximum=100_000.0
                ),
            )
        elif any(
            value is not None
            for value in (self.latitude_deg, self.longitude_deg, self.horizontal_sigma_m)
        ):
            raise TargetEvidenceCardError("unavailable card must not contain coordinates")
        for name, value in (
            ("gps_status", self.gps_status),
            ("gps_freshness", self.gps_freshness),
            ("range_validity", self.range_validity),
        ):
            normalized = (
                require_text(value, name).lower()
                if name != "range_validity"
                else require_text(value, name).upper()
            )
            allowed = (
                _VALID_GPS
                if name == "gps_status"
                else _VALID_FRESHNESS
                if name == "gps_freshness"
                else _VALID_RANGE
            )
            if normalized not in allowed:
                raise TargetEvidenceCardError(f"{name} has an unsupported value")
            object.__setattr__(self, name, normalized)
        if self.gps_fix_type is not None:
            object.__setattr__(
                self,
                "gps_fix_type",
                require_int(self.gps_fix_type, "gps_fix_type", minimum=0, maximum=8),
            )
        if self.gps_satellites_visible is not None:
            object.__setattr__(
                self,
                "gps_satellites_visible",
                require_int(
                    self.gps_satellites_visible, "gps_satellites_visible", minimum=0, maximum=255
                ),
            )
        for name, value in (
            ("gps_horizontal_accuracy_m", self.gps_horizontal_accuracy_m),
            ("gps_vertical_accuracy_m", self.gps_vertical_accuracy_m),
            ("range_m", self.range_m),
            ("relative_bearing_deg", self.relative_bearing_deg),
        ):
            if value is not None:
                maximum = (
                    1_000_000.0
                    if name == "range_m"
                    else 100_000.0
                    if name.startswith("gps_")
                    else 360.0
                )
                minimum = -360.0 if name == "relative_bearing_deg" else 0.0
                object.__setattr__(
                    self, name, require_real(value, name, minimum=minimum, maximum=maximum)
                )
        if self.range_validity in {"VALID", "DEGRADED"} and self.range_m is None:
            raise TargetEvidenceCardError("usable range requires range_m")
        if self.range_validity == "UNAVAILABLE" and (
            self.range_m is not None or self.relative_bearing_deg is not None
        ):
            raise TargetEvidenceCardError("unavailable range cannot contain distance or bearing")
        if self.source_frame_id is not None:
            object.__setattr__(
                self, "source_frame_id", require_text(self.source_frame_id, "source_frame_id")
            )
        object.__setattr__(
            self,
            "run_identity_sha256",
            _optional_sha(self.run_identity_sha256, "run_identity_sha256"),
        )

    @classmethod
    def from_event(
        cls,
        event: TargetEvent,
        geolocation: TargetGeolocation | None,
        *,
        run_identity_sha256: str | None = None,
        card_id: str | None = None,
    ) -> TargetEvidenceCard:
        """Build a card without ever fabricating a point for a bad sample."""

        if not isinstance(event, TargetEvent):
            raise TargetEvidenceCardError("event must be TargetEvent")
        if geolocation is not None and not isinstance(geolocation, TargetGeolocation):
            raise TargetEvidenceCardError("geolocation must be TargetGeolocation or None")
        available = (
            event.gps.qualified
            and event.gps_freshness == "fresh"
            and event.ranging.validity in {"VALID", "DEGRADED"}
            and geolocation is not None
        )
        reason = None if available else _unavailable_reason(event, geolocation)
        return cls(
            card_id=card_id or f"{event.event_id}-card",
            event_id=event.event_id,
            frame_id=event.frame_id,
            target_id=event.target_id,
            target_label=event.target_label,
            captured_at_s=event.captured_at_s,
            available=available,
            unavailable_reason=reason,
            latitude_deg=geolocation.latitude_deg
            if available and geolocation is not None
            else None,
            longitude_deg=geolocation.longitude_deg
            if available and geolocation is not None
            else None,
            horizontal_sigma_m=geolocation.horizontal_sigma_m
            if available and geolocation is not None
            else None,
            gps_status=event.gps.status,
            gps_freshness=event.gps_freshness,
            gps_fix_type=event.gps.fix_type,
            gps_satellites_visible=event.gps.satellites_visible,
            gps_horizontal_accuracy_m=event.gps.horizontal_accuracy_m,
            gps_vertical_accuracy_m=event.gps.vertical_accuracy_m,
            range_validity=event.ranging.validity,
            range_m=event.ranging.range_m,
            relative_bearing_deg=event.ranging.relative_bearing_deg,
            source_frame_id=event.source_frame_id,
            run_identity_sha256=run_identity_sha256,
        )

    @classmethod
    def from_live_observation(
        cls,
        details: Mapping[str, object],
        *,
        captured_at_s: float,
        target_label: str,
        run_identity_sha256: str | None = None,
        card_id: str | None = None,
    ) -> TargetEvidenceCard:
        """Materialize one card from the shared live ranging audit record.

        The audit record is deliberately treated as untrusted input.  A
        missing or incomplete GPS quality section therefore produces an
        unavailable card, even if a nested coordinate happens to be present.
        """

        if not isinstance(details, Mapping):
            raise TargetEvidenceCardError("live observation details must be a mapping")
        target_id = require_text(details.get("target_id"), "target_id")
        frame_id = require_text(details.get("frame_id"), "frame_id")
        geo = details.get("target_geolocation")
        geo_mapping = geo if isinstance(geo, Mapping) else {}
        gps_quality = details.get("gps_quality")
        gps_quality_mapping = gps_quality if isinstance(gps_quality, Mapping) else {}
        gps_sample = details.get("gps_sample")
        gps_sample_mapping = gps_sample if isinstance(gps_sample, Mapping) else {}
        gps_qualified = gps_quality_mapping.get("qualified") is True
        age_value = gps_quality_mapping.get("age_s")
        gps_age = (
            float(age_value)
            if isinstance(age_value, (int, float))
            and not isinstance(age_value, bool)
            and age_value >= 0.0
            else None
        )
        gps_freshness = (
            "fresh"
            if gps_age is not None and gps_age <= GPS_QUALIFIED_MAX_AGE_S
            else "stale"
            if gps_age is not None
            else "unknown"
        )
        range_validity = require_text(details.get("validity"), "validity").upper()
        range_m = optional_real(details.get("ground_range_m"), "ground_range_m", minimum=0.0)
        bearing = optional_real(
            details.get("relative_bearing_deg"),
            "relative_bearing_deg",
            minimum=-360.0,
            maximum=360.0,
        )
        geo_available = geo_mapping.get("available") is True
        latitude = optional_real(
            geo_mapping.get("latitude_deg"),
            "target_geolocation.latitude_deg",
            minimum=-90.0,
            maximum=90.0,
        )
        longitude = optional_real(
            geo_mapping.get("longitude_deg"),
            "target_geolocation.longitude_deg",
            minimum=-180.0,
            maximum=180.0,
        )
        sigma = optional_real(
            geo_mapping.get("horizontal_sigma_m"),
            "target_geolocation.horizontal_sigma_m",
            minimum=0.0,
            maximum=100_000.0,
        )
        available = (
            gps_qualified
            and gps_freshness == "fresh"
            and range_validity in {"VALID", "DEGRADED"}
            and range_m is not None
            and geo_available
            and latitude is not None
            and longitude is not None
            and sigma is not None
        )
        if available:
            reason = None
        elif not gps_qualified:
            reason = "gps_unqualified"
        elif gps_freshness == "stale":
            reason = "gps_stale"
        elif gps_freshness == "unknown":
            reason = "gps_freshness_unknown"
        elif range_validity == "UNAVAILABLE" or range_m is None:
            reason = "range_unavailable"
        elif not geo_available or latitude is None or longitude is None or sigma is None:
            reason = "geolocation_unavailable"
        else:
            reason = "geolocation_rejected"
        return cls(
            card_id=card_id or f"{target_id}-{frame_id}-card",
            event_id=f"live-{frame_id}",
            frame_id=frame_id,
            target_id=target_id,
            target_label=target_label,
            captured_at_s=captured_at_s,
            available=available,
            unavailable_reason=reason,
            latitude_deg=latitude if available else None,
            longitude_deg=longitude if available else None,
            horizontal_sigma_m=sigma if available else None,
            gps_status="qualified" if gps_qualified else "unqualified",
            gps_freshness=gps_freshness,
            gps_fix_type=(
                gps_sample_mapping.get("fix_type")
                if isinstance(gps_sample_mapping.get("fix_type"), int)
                and not isinstance(gps_sample_mapping.get("fix_type"), bool)
                else None
            ),
            gps_satellites_visible=(
                gps_sample_mapping.get("satellites_visible")
                if isinstance(gps_sample_mapping.get("satellites_visible"), int)
                and not isinstance(gps_sample_mapping.get("satellites_visible"), bool)
                else None
            ),
            gps_horizontal_accuracy_m=optional_real(
                gps_sample_mapping.get("h_acc_m"), "gps_sample.h_acc_m", minimum=0.0
            ),
            gps_vertical_accuracy_m=optional_real(
                gps_sample_mapping.get("v_acc_m"), "gps_sample.v_acc_m", minimum=0.0
            ),
            range_validity=range_validity,
            range_m=range_m,
            relative_bearing_deg=bearing,
            source_frame_id=frame_id,
            run_identity_sha256=run_identity_sha256,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "schema_version": self.schema_version,
            "kind": "target_evidence_card",
            "card_id": self.card_id,
            "event_id": self.event_id,
            "frame_id": self.frame_id,
            "target_id": self.target_id,
            "target_label": self.target_label,
            "captured_at_s": self.captured_at_s,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "latitude_deg": self.latitude_deg,
            "longitude_deg": self.longitude_deg,
            "horizontal_sigma_m": self.horizontal_sigma_m,
            "gps_status": self.gps_status,
            "gps_freshness": self.gps_freshness,
            "gps_fix_type": self.gps_fix_type,
            "gps_satellites_visible": self.gps_satellites_visible,
            "gps_horizontal_accuracy_m": self.gps_horizontal_accuracy_m,
            "gps_vertical_accuracy_m": self.gps_vertical_accuracy_m,
            "range_validity": self.range_validity,
            "range_m": self.range_m,
            "relative_bearing_deg": self.relative_bearing_deg,
            "source_frame_id": self.source_frame_id,
            "run_identity_sha256": self.run_identity_sha256,
            "claims": {
                "read_only_evidence": True,
                "contains_flight_control": False,
                "contains_truth": False,
            },
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @classmethod
    def from_mapping(cls, raw: object) -> TargetEvidenceCard:
        value = strict_object(
            raw,
            name="TargetEvidenceCard",
            required=frozenset(
                {
                    "schema",
                    "schema_version",
                    "kind",
                    "card_id",
                    "event_id",
                    "frame_id",
                    "target_id",
                    "target_label",
                    "captured_at_s",
                    "available",
                    "unavailable_reason",
                    "latitude_deg",
                    "longitude_deg",
                    "horizontal_sigma_m",
                    "gps_status",
                    "gps_freshness",
                    "gps_fix_type",
                    "gps_satellites_visible",
                    "gps_horizontal_accuracy_m",
                    "gps_vertical_accuracy_m",
                    "range_validity",
                    "range_m",
                    "relative_bearing_deg",
                    "source_frame_id",
                    "run_identity_sha256",
                    "claims",
                }
            ),
        )
        if (
            require_text(value["schema"], "schema") != SCHEMA
            or require_text(value["kind"], "kind") != "target_evidence_card"
        ):
            raise TargetEvidenceCardError("target evidence card schema identifier is invalid")
        claims = strict_object(
            value["claims"],
            name="claims",
            required=frozenset({"read_only_evidence", "contains_flight_control", "contains_truth"}),
        )
        if (
            claims["read_only_evidence"] is not True
            or claims["contains_flight_control"] is not False
            or claims["contains_truth"] is not False
        ):
            raise TargetEvidenceCardError("target evidence card claims are unsafe")
        card = cls(
            card_id=require_text(value["card_id"], "card_id"),
            event_id=require_text(value["event_id"], "event_id"),
            frame_id=require_text(value["frame_id"], "frame_id"),
            target_id=require_text(value["target_id"], "target_id"),
            target_label=require_text(value["target_label"], "target_label"),
            captured_at_s=require_real(value["captured_at_s"], "captured_at_s", minimum=0.0),
            available=require_bool(value["available"], "available"),
            unavailable_reason=optional_text(value["unavailable_reason"], "unavailable_reason"),
            latitude_deg=optional_real(
                value["latitude_deg"], "latitude_deg", minimum=-90.0, maximum=90.0
            ),
            longitude_deg=optional_real(
                value["longitude_deg"], "longitude_deg", minimum=-180.0, maximum=180.0
            ),
            horizontal_sigma_m=optional_real(
                value["horizontal_sigma_m"], "horizontal_sigma_m", minimum=0.0, maximum=100_000.0
            ),
            gps_status=require_text(value["gps_status"], "gps_status"),
            gps_freshness=require_text(value["gps_freshness"], "gps_freshness"),
            gps_fix_type=(
                require_int(value["gps_fix_type"], "gps_fix_type", minimum=0, maximum=8)
                if value["gps_fix_type"] is not None
                else None
            ),
            gps_satellites_visible=(
                require_int(
                    value["gps_satellites_visible"],
                    "gps_satellites_visible",
                    minimum=0,
                    maximum=255,
                )
                if value["gps_satellites_visible"] is not None
                else None
            ),
            gps_horizontal_accuracy_m=optional_real(
                value["gps_horizontal_accuracy_m"],
                "gps_horizontal_accuracy_m",
                minimum=0.0,
                maximum=100_000.0,
            ),
            gps_vertical_accuracy_m=optional_real(
                value["gps_vertical_accuracy_m"],
                "gps_vertical_accuracy_m",
                minimum=0.0,
                maximum=100_000.0,
            ),
            range_validity=require_text(value["range_validity"], "range_validity"),
            range_m=optional_real(value["range_m"], "range_m", minimum=0.0, maximum=1_000_000.0),
            relative_bearing_deg=optional_real(
                value["relative_bearing_deg"], "relative_bearing_deg", minimum=-360.0, maximum=360.0
            ),
            source_frame_id=optional_text(value["source_frame_id"], "source_frame_id"),
            run_identity_sha256=_optional_sha(value["run_identity_sha256"], "run_identity_sha256"),
            schema_version=require_schema_version(value["schema_version"]),
        )
        if card.to_mapping() != dict(value):
            raise TargetEvidenceCardError("target evidence card is not canonical")
        return card


def _unavailable_reason(event: TargetEvent, geolocation: TargetGeolocation | None) -> str:
    if not event.gps.qualified:
        return "gps_unqualified"
    if event.gps_freshness == "stale":
        return "gps_stale"
    if event.gps_freshness == "unknown":
        return "gps_freshness_unknown"
    if event.ranging.validity == "UNAVAILABLE":
        return "range_unavailable"
    if geolocation is None:
        return "geolocation_unavailable"
    return "geolocation_rejected"


__all__ = ["SCHEMA", "SCHEMA_VERSION", "TargetEvidenceCard", "TargetEvidenceCardError"]
