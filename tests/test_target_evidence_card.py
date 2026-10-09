from __future__ import annotations

import copy
import json
from pathlib import Path
from uuid import uuid4

import pytest

from multidetect.autonomy.serialization import AutonomyContractError
from multidetect.target_event_replay import (
    TargetEventInput,
    TargetEventReplayBuilder,
    TargetGpsEvidence,
    TargetMediaInput,
    TargetRangeEvidence,
)
from multidetect.target_evidence_card import TargetEvidenceCard, TargetEvidenceCardError
from multidetect.target_geolocation import TargetGeolocation


def _bundle_event(tmp_path: Path, *, qualified: bool = True):
    gps = (
        TargetGpsEvidence(
            status="qualified",
            latitude_deg=1.3521,
            longitude_deg=103.8198,
            horizontal_sigma_m=2.4,
            fix_type=3,
            satellites_visible=12,
            horizontal_accuracy_m=3.1,
            vertical_accuracy_m=4.6,
            observed_at_s=10.0,
        )
        if qualified
        else TargetGpsEvidence(status="unqualified")
    )
    event = TargetEventInput(
        event_id="event-1",
        frame_id="frame-1",
        target_id="target-1",
        target_label="person",
        captured_at_s=10.5,
        gps=gps,
        ranging=TargetRangeEvidence(
            validity="VALID",
            range_m=18.25,
            relative_bearing_deg=-12.5,
            range_kind="ground",
        ),
        video=TargetMediaInput(kind="video_frame", payload=b"frame", encoding="jpeg"),
        source_frame_id="camera-frame-1",
    )
    root = tmp_path / "bundle"
    builder = TargetEventReplayBuilder(root, session_id=str(uuid4()))
    return builder.append(event)


def _geolocation() -> TargetGeolocation:
    return TargetGeolocation(
        latitude_deg=1.3522,
        longitude_deg=103.8200,
        horizontal_sigma_m=4.1,
    )


def test_qualified_event_builds_hashable_read_only_card(tmp_path: Path) -> None:
    event = _bundle_event(tmp_path)
    card = TargetEvidenceCard.from_event(event, _geolocation(), run_identity_sha256="a" * 64)

    assert card.available is True
    assert card.unavailable_reason is None
    assert card.latitude_deg == pytest.approx(1.3522)
    assert card.range_m == pytest.approx(18.25)
    assert card.gps_freshness == "fresh"
    mapping = card.to_mapping()
    assert mapping["claims"] == {
        "read_only_evidence": True,
        "contains_flight_control": False,
        "contains_truth": False,
    }
    assert len(card.canonical_hash) == 64
    assert TargetEvidenceCard.from_mapping(json.loads(json.dumps(mapping))) == card


def test_package_lazy_export_resolves_card_type() -> None:
    import multidetect

    assert multidetect.TargetEvidenceCard is TargetEvidenceCard


def test_unqualified_event_never_leaks_a_coordinate(tmp_path: Path) -> None:
    event = _bundle_event(tmp_path, qualified=False)
    card = TargetEvidenceCard.from_event(event, _geolocation())

    assert card.available is False
    assert card.unavailable_reason == "gps_unqualified"
    assert card.latitude_deg is None
    assert card.longitude_deg is None
    assert card.horizontal_sigma_m is None


def test_unavailable_range_and_missing_geolocation_are_explicit(tmp_path: Path) -> None:
    event = _bundle_event(tmp_path)
    no_geolocation = TargetEvidenceCard.from_event(event, None)
    assert no_geolocation.available is False
    assert no_geolocation.unavailable_reason == "geolocation_unavailable"

    unavailable_range = TargetEventInput(
        event_id="event-2",
        frame_id="frame-2",
        target_id="target-1",
        target_label="person",
        captured_at_s=11.0,
        gps=event.gps,
        ranging=TargetRangeEvidence(validity="UNAVAILABLE", range_kind="unknown"),
        video=TargetMediaInput(kind="video_frame", payload=b"frame-2", encoding="jpeg"),
    )
    builder = TargetEventReplayBuilder(tmp_path / "bundle-2", session_id=str(uuid4()))
    range_event = builder.append(unavailable_range)
    card = TargetEvidenceCard.from_event(range_event, _geolocation())
    assert card.available is False
    assert card.unavailable_reason == "range_unavailable"
    assert card.latitude_deg is None


def test_legacy_unknown_gps_age_is_not_treated_as_fresh(tmp_path: Path) -> None:
    event = _bundle_event(tmp_path)
    # A legacy replay may omit the observation timestamp.  That remains
    # readable, but it cannot produce a current map point.
    legacy_gps = TargetGpsEvidence(
        status="qualified",
        latitude_deg=event.gps.latitude_deg,
        longitude_deg=event.gps.longitude_deg,
        horizontal_sigma_m=event.gps.horizontal_sigma_m,
        fix_type=event.gps.fix_type,
        satellites_visible=event.gps.satellites_visible,
        horizontal_accuracy_m=event.gps.horizontal_accuracy_m,
        vertical_accuracy_m=event.gps.vertical_accuracy_m,
    )
    legacy_input = TargetEventInput(
        event_id="event-legacy",
        frame_id="frame-legacy",
        target_id=event.target_id,
        target_label=event.target_label,
        captured_at_s=11.0,
        gps=legacy_gps,
        ranging=event.ranging,
        video=TargetMediaInput(kind="video_frame", payload=b"legacy", encoding="jpeg"),
    )
    builder = TargetEventReplayBuilder(tmp_path / "bundle-legacy", session_id=str(uuid4()))
    legacy_event = builder.append(legacy_input)
    card = TargetEvidenceCard.from_event(legacy_event, _geolocation())
    assert card.available is False
    assert card.unavailable_reason == "gps_freshness_unknown"
    assert card.latitude_deg is None


def test_card_loader_rejects_noncanonical_or_unsafe_mutation(tmp_path: Path) -> None:
    event = _bundle_event(tmp_path)
    card = TargetEvidenceCard.from_event(event, _geolocation())
    raw = card.to_mapping()

    tampered = copy.deepcopy(raw)
    tampered["latitude_deg"] = 90.0
    with pytest.raises(
        (AutonomyContractError, TargetEvidenceCardError), match="pole|canonical|latitude"
    ):
        TargetEvidenceCard.from_mapping(tampered)

    unsafe = copy.deepcopy(raw)
    unsafe["claims"]["contains_flight_control"] = True
    with pytest.raises(TargetEvidenceCardError, match="claims"):
        TargetEvidenceCard.from_mapping(unsafe)
