from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from multidetect.domain import VehicleTelemetry
from multidetect.telemetry import AuthenticatedZoneTelemetryProvider
from multidetect.zone_evidence import (
    FileZoneEvidenceProvider,
    ZoneEvidenceSnapshot,
    sign_zone_evidence_document,
    verify_zone_evidence,
)

MISSION_ID = "fire-fixed-wing-hil-001"
KEY_ID = "zone-key-v1"
HMAC_KEY = b"zone-evidence-test-key-32-bytes-minimum"


def _telemetry(**changes: object) -> VehicleTelemetry:
    base = VehicleTelemetry(
        altitude_agl_m=30.0,
        roll_deg=1.0,
        pitch_deg=-1.0,
        ground_speed_mps=18.0,
        in_allowed_zone=None,
        geofence_healthy=None,
        position_healthy=True,
        link_healthy=True,
        flight_mode_allows_deploy=None,
        release_zone_clear=None,
        latitude_deg=31.123456,
        longitude_deg=121.654321,
        flight_mode="AUTO",
    )
    return replace(base, **changes)


def _document(**changes: object) -> dict[str, object]:
    document: dict[str, object] = {
        "protocol_version": 1,
        "observed_at_s": 100.0,
        "source_id": "independent-zone-monitor",
        "mission_id": MISSION_ID,
        "sequence": 7,
        "key_id": KEY_ID,
        "latitude_deg": 31.123456,
        "longitude_deg": 121.654321,
        "in_allowed_zone": True,
        "geofence_healthy": True,
        "release_zone_clear": True,
    }
    document.update(changes)
    document["signature_hmac_sha256"] = sign_zone_evidence_document(document, hmac_key=HMAC_KEY)
    return document


def _write_report(path: Path, document: dict[str, object]) -> None:
    path.write_text(json.dumps(document), encoding="utf-8")


class _ConstantTelemetryProvider:
    def __init__(self, telemetry: VehicleTelemetry) -> None:
        self.telemetry = telemetry
        self.closed = False

    def snapshot(self, *, now_s: float) -> VehicleTelemetry:
        assert now_s >= 0
        return self.telemetry

    def close(self) -> None:
        self.closed = True


def _wrapper(
    path: Path, telemetry: VehicleTelemetry | None = None
) -> AuthenticatedZoneTelemetryProvider:
    return AuthenticatedZoneTelemetryProvider(
        _ConstantTelemetryProvider(telemetry or _telemetry()),
        FileZoneEvidenceProvider(path, hmac_key=HMAC_KEY, expected_key_id=KEY_ID),
        mission_id=MISSION_ID,
        maximum_age_s=1.0,
        maximum_position_delta_m=25.0,
    )


def test_signed_nearby_zone_evidence_augments_read_only_telemetry(tmp_path: Path) -> None:
    report = tmp_path / "zone.json"
    _write_report(report, _document())
    provider = _wrapper(report)

    snapshot = provider.snapshot(now_s=100.5)

    assert provider.is_read_only is True
    assert snapshot.in_allowed_zone is True
    assert snapshot.geofence_healthy is True
    assert snapshot.release_zone_clear is True
    assert snapshot.latitude_deg == pytest.approx(31.123456)
    assert provider.last_verification is not None
    assert provider.last_verification.valid is True
    assert provider.last_verification.position_delta_m == pytest.approx(0.0)


def test_zone_wrapper_closes_underlying_pixhawk_like_provider(tmp_path: Path) -> None:
    report = tmp_path / "zone.json"
    _write_report(report, _document())
    base = _ConstantTelemetryProvider(_telemetry())
    provider = AuthenticatedZoneTelemetryProvider(
        base,
        FileZoneEvidenceProvider(report, hmac_key=HMAC_KEY, expected_key_id=KEY_ID),
        mission_id=MISSION_ID,
        maximum_age_s=1.0,
        maximum_position_delta_m=25.0,
    )

    provider.close()

    assert base.closed is True


def test_authenticated_false_predicate_is_preserved_as_a_safety_denial(tmp_path: Path) -> None:
    report = tmp_path / "zone.json"
    _write_report(report, _document(release_zone_clear=False))

    snapshot = _wrapper(report).snapshot(now_s=100.5)

    assert snapshot.in_allowed_zone is True
    assert snapshot.geofence_healthy is True
    assert snapshot.release_zone_clear is False


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"observed_at_s": 90.0}, "stale"),
        ({"mission_id": "another-mission"}, "mission ID"),
        ({"latitude_deg": 32.0}, "does not match"),
    ],
)
def test_invalid_binding_causes_unknown_zone_predicates(
    tmp_path: Path, changes: dict[str, object], reason: str
) -> None:
    report = tmp_path / "zone.json"
    _write_report(report, _document(**changes))
    provider = _wrapper(report)

    snapshot = provider.snapshot(now_s=100.5)

    assert snapshot.in_allowed_zone is None
    assert snapshot.geofence_healthy is None
    assert snapshot.release_zone_clear is None
    assert snapshot.position_healthy is True
    assert provider.last_verification is not None
    assert any(reason in item for item in provider.last_verification.reasons)


def test_tampered_or_missing_report_fails_closed_and_preserves_vehicle_data(
    tmp_path: Path,
) -> None:
    report = tmp_path / "zone.json"
    document = _document()
    document["release_zone_clear"] = False
    _write_report(report, document)
    provider = _wrapper(report)

    snapshot = provider.snapshot(now_s=100.5)

    assert snapshot.in_allowed_zone is None
    assert snapshot.release_zone_clear is None
    assert snapshot.altitude_agl_m == 30.0
    report.unlink()
    snapshot = provider.snapshot(now_s=100.6)
    assert snapshot.geofence_healthy is None
    assert provider.last_verification is not None
    assert provider.last_verification.valid is False


def test_file_provider_rejects_sequence_rollback_and_changed_same_sequence(
    tmp_path: Path,
) -> None:
    report = tmp_path / "zone.json"
    provider = FileZoneEvidenceProvider(report, hmac_key=HMAC_KEY, expected_key_id=KEY_ID)
    _write_report(report, _document(sequence=8))
    provider.snapshot(now_s=100.0)

    _write_report(report, _document(sequence=7))
    with pytest.raises(ValueError, match="moved backwards"):
        provider.snapshot(now_s=100.1)

    _write_report(report, _document(sequence=8, release_zone_clear=False))
    with pytest.raises(ValueError, match="without a new sequence"):
        provider.snapshot(now_s=100.2)


def test_verification_requires_healthy_vehicle_position() -> None:
    snapshot = ZoneEvidenceSnapshot(
        observed_at_s=100.0,
        source_id="zone-monitor",
        mission_id=MISSION_ID,
        sequence=1,
        key_id=KEY_ID,
        authenticated=True,
        latitude_deg=31.123456,
        longitude_deg=121.654321,
        in_allowed_zone=True,
        geofence_healthy=True,
        release_zone_clear=True,
    )

    verification = verify_zone_evidence(
        snapshot,
        _telemetry(position_healthy=None),
        mission_id=MISSION_ID,
        now_s=100.5,
        maximum_age_s=1.0,
        maximum_position_delta_m=25.0,
    )

    assert verification.valid is False
    assert "vehicle position health is not confirmed" in verification.reasons
