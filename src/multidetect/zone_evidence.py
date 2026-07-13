from __future__ import annotations

import hashlib
import hmac
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .domain import VehicleTelemetry

ZONE_EVIDENCE_PROTOCOL_VERSION = 1
_EARTH_RADIUS_M = 6_371_008.8


@dataclass(frozen=True, slots=True)
class ZoneEvidenceSnapshot:
    """Authenticated, position-bound safety predicates from an independent source."""

    observed_at_s: float
    source_id: str
    mission_id: str
    sequence: int
    key_id: str
    authenticated: bool
    latitude_deg: float
    longitude_deg: float
    in_allowed_zone: bool
    geofence_healthy: bool
    release_zone_clear: bool
    protocol_version: int = ZONE_EVIDENCE_PROTOCOL_VERSION


@dataclass(frozen=True, slots=True)
class ZoneEvidenceVerification:
    valid: bool
    source_id: str
    reasons: tuple[str, ...]
    position_delta_m: float | None


class ZoneEvidenceProvider(Protocol):
    def snapshot(self, *, now_s: float) -> ZoneEvidenceSnapshot: ...


class FileZoneEvidenceProvider:
    """Reads signed zone evidence without writing to avionics or an actuator."""

    def __init__(
        self,
        path: str | Path,
        *,
        hmac_key: bytes,
        expected_key_id: str,
    ) -> None:
        if not hmac_key:
            raise ValueError("zone evidence HMAC key cannot be empty")
        if not isinstance(expected_key_id, str) or not expected_key_id.strip():
            raise ValueError("zone evidence expected key ID cannot be empty")
        self.path = Path(path)
        self.hmac_key = hmac_key
        self.expected_key_id = expected_key_id.strip()
        self._highest_sequence: int | None = None
        self._highest_sequence_digest: str | None = None

    def snapshot(self, *, now_s: float) -> ZoneEvidenceSnapshot:
        del now_s
        document = _load_zone_evidence_document(self.path)
        snapshot = _snapshot_from_document(
            document,
            hmac_key=self.hmac_key,
            expected_key_id=self.expected_key_id,
        )
        document_digest = hashlib.sha256(_canonical_report_bytes(document)).hexdigest()
        if self._highest_sequence is not None and snapshot.sequence < self._highest_sequence:
            raise ValueError("zone evidence sequence moved backwards")
        if (
            self._highest_sequence is not None
            and snapshot.sequence == self._highest_sequence
            and document_digest != self._highest_sequence_digest
        ):
            raise ValueError("zone evidence content changed without a new sequence")
        if self._highest_sequence is None or snapshot.sequence > self._highest_sequence:
            self._highest_sequence = snapshot.sequence
            self._highest_sequence_digest = document_digest
        return snapshot


def load_zone_evidence_snapshot(
    path: str | Path,
    *,
    hmac_key: bytes,
    expected_key_id: str,
) -> ZoneEvidenceSnapshot:
    document = _load_zone_evidence_document(Path(path))
    return _snapshot_from_document(
        document,
        hmac_key=hmac_key,
        expected_key_id=expected_key_id,
    )


def verify_zone_evidence(
    snapshot: ZoneEvidenceSnapshot,
    telemetry: VehicleTelemetry,
    *,
    mission_id: str,
    now_s: float,
    maximum_age_s: float,
    maximum_position_delta_m: float,
) -> ZoneEvidenceVerification:
    _validate_verification_inputs(
        mission_id=mission_id,
        now_s=now_s,
        maximum_age_s=maximum_age_s,
        maximum_position_delta_m=maximum_position_delta_m,
    )
    reasons: list[str] = []
    position_delta_m: float | None = None
    if not isinstance(snapshot.source_id, str) or not snapshot.source_id.strip():
        reasons.append("zone evidence source identity is not confirmed")
    if snapshot.protocol_version != ZONE_EVIDENCE_PROTOCOL_VERSION:
        reasons.append("zone evidence protocol version is not supported")
    if snapshot.authenticated is not True:
        reasons.append("zone evidence report is not authenticated")
    if not isinstance(snapshot.key_id, str) or not snapshot.key_id.strip():
        reasons.append("zone evidence key identity is not confirmed")
    if snapshot.mission_id != mission_id:
        reasons.append("zone evidence mission ID does not match")
    if (
        isinstance(snapshot.sequence, bool)
        or not isinstance(snapshot.sequence, int)
        or snapshot.sequence < 0
    ):
        reasons.append("zone evidence sequence is invalid")
    for field_name, value in (
        ("allowed-area", snapshot.in_allowed_zone),
        ("geofence-health", snapshot.geofence_healthy),
        ("release-zone", snapshot.release_zone_clear),
    ):
        if not isinstance(value, bool):
            reasons.append(f"zone evidence {field_name} predicate is invalid")
    if not math.isfinite(snapshot.observed_at_s) or snapshot.observed_at_s < 0:
        reasons.append("zone evidence timestamp is invalid")
    else:
        age_s = now_s - snapshot.observed_at_s
        if age_s < 0 or age_s > maximum_age_s:
            reasons.append("zone evidence is stale")

    evidence_position_valid = _valid_lat_lon(snapshot.latitude_deg, snapshot.longitude_deg)
    vehicle_position_valid = _valid_lat_lon(telemetry.latitude_deg, telemetry.longitude_deg)
    if not evidence_position_valid:
        reasons.append("zone evidence position is invalid")
    if telemetry.position_healthy is not True:
        reasons.append("vehicle position health is not confirmed")
    if not vehicle_position_valid:
        reasons.append("vehicle position is invalid")
    if evidence_position_valid and vehicle_position_valid:
        position_delta_m = _haversine_distance_m(
            snapshot.latitude_deg,
            snapshot.longitude_deg,
            telemetry.latitude_deg,
            telemetry.longitude_deg,
        )
        if position_delta_m > maximum_position_delta_m:
            reasons.append("zone evidence position does not match the vehicle position")

    return ZoneEvidenceVerification(
        valid=not reasons,
        source_id=snapshot.source_id,
        reasons=tuple(reasons),
        position_delta_m=position_delta_m,
    )


def sign_zone_evidence_document(document: dict[str, object], *, hmac_key: bytes) -> str:
    if not hmac_key:
        raise ValueError("zone evidence HMAC key cannot be empty")
    return hmac.new(hmac_key, _canonical_report_bytes(document), hashlib.sha256).hexdigest()


def unavailable_zone_evidence_verification(
    *, source_id: str, reason: str
) -> ZoneEvidenceVerification:
    return ZoneEvidenceVerification(
        valid=False,
        source_id=source_id,
        reasons=(reason,),
        position_delta_m=None,
    )


def _load_zone_evidence_document(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError("zone evidence report must be a JSON object")
    return raw


def _snapshot_from_document(
    document: dict[str, object],
    *,
    hmac_key: bytes,
    expected_key_id: str,
) -> ZoneEvidenceSnapshot:
    _verify_report_signature(
        document,
        hmac_key=hmac_key,
        expected_key_id=expected_key_id,
    )
    try:
        sequence = document["sequence"]
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise ValueError("sequence must be an integer")
        return ZoneEvidenceSnapshot(
            observed_at_s=float(document["observed_at_s"]),
            source_id=_strict_string(document["source_id"], "source_id"),
            mission_id=_strict_string(document["mission_id"], "mission_id"),
            sequence=sequence,
            key_id=_strict_string(document["key_id"], "key_id"),
            authenticated=True,
            latitude_deg=float(document["latitude_deg"]),
            longitude_deg=float(document["longitude_deg"]),
            in_allowed_zone=_strict_bool(document["in_allowed_zone"]),
            geofence_healthy=_strict_bool(document["geofence_healthy"]),
            release_zone_clear=_strict_bool(document["release_zone_clear"]),
            protocol_version=int(document["protocol_version"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid zone evidence report: {exc}") from exc


def _verify_report_signature(
    document: dict[str, object],
    *,
    hmac_key: bytes,
    expected_key_id: str,
) -> None:
    signature = document.get("signature_hmac_sha256")
    key_id = document.get("key_id")
    if not isinstance(signature, str) or len(signature) != 64:
        raise ValueError("zone evidence signature is missing or invalid")
    if key_id != expected_key_id:
        raise ValueError("zone evidence key ID does not match")
    expected = sign_zone_evidence_document(document, hmac_key=hmac_key)
    if not hmac.compare_digest(signature.lower(), expected):
        raise ValueError("zone evidence HMAC verification failed")


def _canonical_report_bytes(document: dict[str, object]) -> bytes:
    unsigned = {key: value for key, value in document.items() if key != "signature_hmac_sha256"}
    return json.dumps(
        unsigned,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _strict_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("zone evidence boolean fields must be true or false")
    return value


def _strict_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _validate_verification_inputs(
    *,
    mission_id: str,
    now_s: float,
    maximum_age_s: float,
    maximum_position_delta_m: float,
) -> None:
    if not isinstance(mission_id, str) or not mission_id.strip():
        raise ValueError("zone evidence mission ID cannot be empty")
    for name, value in (
        ("now_s", now_s),
        ("maximum_age_s", maximum_age_s),
        ("maximum_position_delta_m", maximum_position_delta_m),
    ):
        if isinstance(value, bool) or not math.isfinite(value) or value < 0:
            raise ValueError(f"zone evidence {name} must be finite and non-negative")


def _valid_lat_lon(latitude_deg: float, longitude_deg: float) -> bool:
    return (
        math.isfinite(latitude_deg)
        and math.isfinite(longitude_deg)
        and -90.0 <= latitude_deg <= 90.0
        and -180.0 <= longitude_deg <= 180.0
    )


def _haversine_distance_m(
    latitude_a_deg: float,
    longitude_a_deg: float,
    latitude_b_deg: float,
    longitude_b_deg: float,
) -> float:
    latitude_a = math.radians(latitude_a_deg)
    latitude_b = math.radians(latitude_b_deg)
    delta_latitude = latitude_b - latitude_a
    delta_longitude = math.radians(longitude_b_deg - longitude_a_deg)
    haversine = (
        math.sin(delta_latitude / 2.0) ** 2
        + math.cos(latitude_a) * math.cos(latitude_b) * math.sin(delta_longitude / 2.0) ** 2
    )
    return 2.0 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(haversine)))


__all__ = [
    "FileZoneEvidenceProvider",
    "ZONE_EVIDENCE_PROTOCOL_VERSION",
    "ZoneEvidenceProvider",
    "ZoneEvidenceSnapshot",
    "ZoneEvidenceVerification",
    "load_zone_evidence_snapshot",
    "sign_zone_evidence_document",
    "unavailable_zone_evidence_verification",
    "verify_zone_evidence",
]
