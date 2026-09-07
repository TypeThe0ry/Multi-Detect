from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from multidetect.adaptive_ranging import AdaptiveRangingConfig, assess_gps_quality
from multidetect.autonomy.serialization import AutonomyContractError
from multidetect.domain import VehicleTelemetry
from multidetect.target_event_replay import (
    GPS_QUALIFIED_FIX_TYPE,
    GPS_QUALIFIED_MAX_AGE_S,
    GPS_QUALIFIED_MAX_HORIZONTAL_ACCURACY_M,
    GPS_QUALIFIED_MAX_VERTICAL_ACCURACY_M,
    GPS_QUALIFIED_MIN_SATELLITES,
    TargetEventInput,
    TargetEventReplayBuilder,
    TargetGpsEvidence,
    TargetMediaInput,
    TargetRangeEvidence,
    load_target_event_replay_manifest,
)
from scripts.create_target_event_replay import main as create_target_event_replay
from scripts.inspect_target_event_replay import main as inspect_target_event_replay


def _gps(*, status: str = "qualified", observed_at_s: float | None = None) -> TargetGpsEvidence:
    if status != "qualified":
        return TargetGpsEvidence(status=status, observed_at_s=observed_at_s)
    return TargetGpsEvidence(
        status="qualified",
        latitude_deg=1.3521,
        longitude_deg=103.8198,
        horizontal_sigma_m=2.4,
        fix_type=3,
        satellites_visible=12,
        horizontal_accuracy_m=3.1,
        vertical_accuracy_m=4.6,
        observed_at_s=observed_at_s,
    )


def _gps_telemetry(**updates: object) -> VehicleTelemetry:
    """Build a live telemetry sample equivalent to :func:`_gps` defaults."""

    values: dict[str, object] = {
        "altitude_agl_m": 40.0,
        "roll_deg": 0.0,
        "pitch_deg": 0.0,
        "ground_speed_mps": 0.0,
        "in_allowed_zone": None,
        "geofence_healthy": None,
        "position_healthy": True,
        "link_healthy": True,
        "flight_mode_allows_deploy": None,
        "release_zone_clear": None,
        "latitude_deg": 1.3521,
        "longitude_deg": 103.8198,
        "satellites_visible": 12,
        "gps_fix_type": 3,
        "gps_horizontal_accuracy_m": 3.1,
        "gps_vertical_accuracy_m": 4.6,
        "gps_observed_at_s": 99.0,
    }
    values.update(updates)
    return VehicleTelemetry(**values)


def _event(
    captured_at_s: float,
    *,
    target_id: str = "target-1",
    event_id: str | None = None,
) -> TargetEventInput:
    return TargetEventInput(
        event_id=event_id or f"event-{captured_at_s}",
        frame_id=f"frame-{captured_at_s}",
        target_id=target_id,
        target_label="person",
        captured_at_s=captured_at_s,
        gps=_gps(),
        ranging=TargetRangeEvidence(
            validity="VALID",
            range_m=18.25,
            relative_bearing_deg=-12.5,
            range_kind="ground",
        ),
        video=TargetMediaInput(
            kind="video_frame",
            payload=f"video-{captured_at_s}".encode("ascii"),
            encoding="jpeg",
            width=1920,
            height=1080,
        ),
        depth=(
            TargetMediaInput(
                kind="depth_map",
                payload=f"depth-{captured_at_s}".encode("ascii"),
                encoding="raw",
            )
            if captured_at_s == 1.0
            else None
        ),
        source_frame_id=f"camera-frame-{captured_at_s}",
        vehicle_altitude_agl_m=42.0,
        vehicle_heading_deg=271.0,
    )


def _camera_calibration(path: Path, *, fx_px: float = 540.0) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "calibration": {
                    "calibration_id": "camera-main-001",
                    "width_px": 1280,
                    "height_px": 960,
                    "fx_px": fx_px,
                    "fy_px": 540.0,
                    "cx_px": 640.0,
                    "cy_px": 480.0,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_target_event_replay_round_trip_filters_and_binds_media(tmp_path: Path) -> None:
    root = tmp_path / "target-event-bundle"
    session_id = str(uuid4())
    builder = TargetEventReplayBuilder(root, session_id=session_id)

    first = builder.append(_event(1.0))
    second = builder.append(_event(2.0, target_id="target-2"))
    bundle = builder.finalize()

    assert first.sequence == 0
    assert second.sequence == 1
    assert bundle.event_count == 2
    assert bundle.media_count == 3
    assert bundle.locatable_event_count == 2
    assert bundle.ranged_event_count == 2
    assert bundle.media_event_count == 2
    assert bundle.unique_target_count == 2
    assert bundle.capture_duration_s == pytest.approx(1.0)
    assert bundle.media_bytes == 27
    assert bundle.session_id == session_id
    assert bundle.summary()["claims"] == {
        "read_only_evidence": True,
        "contains_flight_control": False,
        "contains_truth": False,
    }
    assert [event.target_id for event in bundle.iter_events(target_id="target-1")] == ["target-1"]
    assert [event.sequence for event in bundle.iter_events(start_s=1.5, end_s=2.0)] == [1]
    assert bundle.events[0].video is not None
    assert bundle.events[0].video.read_verified_bytes() == b"video-1.0"
    assert bundle.events[0].depth is not None
    assert bundle.events[0].depth.read_verified_bytes() == b"depth-1.0"

    manifest_path = root / "manifest.json"
    manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    loaded = load_target_event_replay_manifest(
        manifest_path,
        expected_manifest_sha256=manifest_hash,
    )
    assert loaded.summary() == bundle.summary()


def test_target_event_replay_contains_and_rehashes_camera_calibration(tmp_path: Path) -> None:
    calibration = _camera_calibration(tmp_path / "camera.json")
    root = tmp_path / "target-event-calibrated"
    builder = TargetEventReplayBuilder(
        root,
        session_id=str(uuid4()),
        camera_calibration_path=calibration,
    )
    builder.append(_event(1.0))
    bundle = builder.finalize()
    assert bundle.camera_calibration_id == "camera-main-001"
    assert bundle.camera_calibration_sha256 == hashlib.sha256(calibration.read_bytes()).hexdigest()
    assert bundle.camera_calibration_path == (root / "camera-calibration.json").resolve()

    bundle.camera_calibration_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(AutonomyContractError, match="camera calibration.*mismatch"):
        load_target_event_replay_manifest(root / "manifest.json")


def test_target_event_replay_rejects_mutated_media_and_event_stream(tmp_path: Path) -> None:
    root = tmp_path / "target-event-bundle"
    builder = TargetEventReplayBuilder(root, session_id=str(uuid4()))
    builder.append(_event(1.0))
    bundle = builder.finalize()
    assert bundle.events[0].video is not None

    video_path = bundle.events[0].video.resolved_path
    video_path.write_bytes(b"tamperxxx")
    with pytest.raises(AutonomyContractError, match="mismatch"):
        load_target_event_replay_manifest(root / "manifest.json")

    # Restore the media and mutate the separately hashed JSONL stream instead.
    video_path.write_bytes(b"video-1.0")
    (root / "media" / "unreferenced.bin").write_bytes(b"not-bound")
    with pytest.raises(AutonomyContractError, match="unreferenced"):
        load_target_event_replay_manifest(root / "manifest.json")
    (root / "media" / "unreferenced.bin").unlink()

    events_path = root / "events.jsonl"
    events_path.write_bytes(events_path.read_bytes() + b"\n")
    with pytest.raises(AutonomyContractError, match="mismatch"):
        load_target_event_replay_manifest(root / "manifest.json")


def test_target_event_replay_rejects_manifest_tampering_and_unsafe_roots(
    tmp_path: Path,
) -> None:
    root = tmp_path / "target-event-bundle"
    builder = TargetEventReplayBuilder(root, session_id=str(uuid4()))
    builder.append(_event(1.0))
    builder.finalize()

    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["bundle_id"] = "tampered"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(AutonomyContractError, match="bundle_hash"):
        load_target_event_replay_manifest(manifest_path)

    with pytest.raises(AutonomyContractError, match="not empty"):
        TargetEventReplayBuilder(root, session_id=str(uuid4()), overwrite=True)


def test_target_event_replay_quality_and_order_gates_fail_closed(tmp_path: Path) -> None:
    assert TargetGpsEvidence(status="unavailable").qualified is False
    assert TargetGpsEvidence(status="unqualified").qualified is False

    with pytest.raises(AutonomyContractError, match="quality gate"):
        TargetGpsEvidence(
            status="qualified",
            latitude_deg=1.0,
            longitude_deg=2.0,
            horizontal_sigma_m=2.0,
            fix_type=2,
            satellites_visible=5,
            horizontal_accuracy_m=20.0,
        )
    with pytest.raises(AutonomyContractError, match="cannot contain measurements"):
        TargetGpsEvidence(status="unavailable", latitude_deg=1.0, longitude_deg=2.0)
    with pytest.raises(AutonomyContractError, match="requires range_m"):
        TargetRangeEvidence(validity="VALID")
    with pytest.raises(AutonomyContractError, match="cannot contain range_m"):
        TargetRangeEvidence(validity="UNAVAILABLE", range_m=1.0)
    with pytest.raises(AutonomyContractError, match="relative_bearing_deg"):
        TargetRangeEvidence(validity="UNAVAILABLE", relative_bearing_deg=0.0)

    root = tmp_path / "target-event-bundle"
    builder = TargetEventReplayBuilder(root, session_id=str(uuid4()))
    with pytest.raises(AutonomyContractError, match="video frame or depth map"):
        builder.append(
            TargetEventInput(
                event_id="event-no-media",
                frame_id="frame-no-media",
                target_id="target-1",
                target_label="person",
                captured_at_s=1.0,
                gps=TargetGpsEvidence(status="unavailable"),
                ranging=TargetRangeEvidence(),
            )
        )
    builder.append(_event(2.0))
    with pytest.raises(AutonomyContractError, match="event_id values must be unique"):
        builder.append(_event(3.0, event_id="event-2.0"))
    with pytest.raises(AutonomyContractError, match="strictly increasing"):
        builder.append(_event(2.0, target_id="target-2", event_id="event-duplicate-time"))


def test_target_event_replay_gps_thresholds_follow_live_policy_defaults() -> None:
    """Replay qualification must use the same defaults as live GPS gating."""

    config = AdaptiveRangingConfig()
    assert GPS_QUALIFIED_FIX_TYPE == config.minimum_gps_fix_type
    assert GPS_QUALIFIED_MIN_SATELLITES == config.minimum_gps_satellites
    assert GPS_QUALIFIED_MAX_HORIZONTAL_ACCURACY_M == config.maximum_gps_horizontal_accuracy_m
    assert GPS_QUALIFIED_MAX_VERTICAL_ACCURACY_M == config.maximum_gps_vertical_accuracy_m
    assert GPS_QUALIFIED_MAX_AGE_S == config.maximum_gps_age_s


@pytest.mark.parametrize(
    ("vertical_accuracy_m", "error_match"),
    (
        (None, "quality gate"),
        (GPS_QUALIFIED_MAX_VERTICAL_ACCURACY_M + 0.01, "quality gate"),
        (-0.01, "at least 0"),
        (math.nan, "finite"),
    ),
)
def test_target_event_replay_vertical_accuracy_matches_live_gate(
    vertical_accuracy_m: float | None,
    error_match: str,
) -> None:
    """A replay cannot claim a fix the live assessor would reject on vAcc."""

    telemetry = _gps_telemetry(gps_vertical_accuracy_m=vertical_accuracy_m)
    live_assessment = assess_gps_quality(telemetry, now_s=100.0)
    assert live_assessment.qualified is False
    with pytest.raises(AutonomyContractError, match=error_match):
        TargetGpsEvidence(
            status="qualified",
            latitude_deg=telemetry.latitude_deg,
            longitude_deg=telemetry.longitude_deg,
            horizontal_sigma_m=2.0,
            fix_type=telemetry.gps_fix_type,
            satellites_visible=telemetry.satellites_visible,
            horizontal_accuracy_m=telemetry.gps_horizontal_accuracy_m,
            vertical_accuracy_m=vertical_accuracy_m,
            observed_at_s=telemetry.gps_observed_at_s,
        )


def test_target_event_replay_accepts_live_vertical_accuracy_boundary() -> None:
    """The exact configured vAcc boundary is accepted by both paths."""

    telemetry = _gps_telemetry(
        gps_vertical_accuracy_m=GPS_QUALIFIED_MAX_VERTICAL_ACCURACY_M,
    )
    live_assessment = assess_gps_quality(telemetry, now_s=100.0)
    assert live_assessment.qualified is True
    evidence = TargetGpsEvidence(
        status="qualified",
        latitude_deg=telemetry.latitude_deg,
        longitude_deg=telemetry.longitude_deg,
        horizontal_sigma_m=2.0,
        fix_type=telemetry.gps_fix_type,
        satellites_visible=telemetry.satellites_visible,
        horizontal_accuracy_m=telemetry.gps_horizontal_accuracy_m,
        vertical_accuracy_m=telemetry.gps_vertical_accuracy_m,
        observed_at_s=telemetry.gps_observed_at_s,
    )
    assert evidence.qualified is True


@pytest.mark.parametrize("latitude_deg", (-90.0, 90.0))
def test_target_event_replay_rejects_pole_coordinates_like_live_gate(
    latitude_deg: float,
) -> None:
    telemetry = _gps_telemetry(latitude_deg=latitude_deg)
    assert assess_gps_quality(telemetry, now_s=100.0).reason == "gps_coordinates_invalid"
    with pytest.raises(AutonomyContractError, match="quality gate"):
        TargetGpsEvidence(
            status="qualified",
            latitude_deg=latitude_deg,
            longitude_deg=telemetry.longitude_deg,
            horizontal_sigma_m=2.0,
            fix_type=telemetry.gps_fix_type,
            satellites_visible=telemetry.satellites_visible,
            horizontal_accuracy_m=telemetry.gps_horizontal_accuracy_m,
            vertical_accuracy_m=telemetry.gps_vertical_accuracy_m,
        )


def test_target_event_replay_schema_uses_mavlink_fix_and_satellite_bounds() -> None:
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "configs/schemas/target-event-replay-manifest.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    gps = schema["$defs"]["gps"]
    assert gps["properties"]["fix_type"]["maximum"] == 8
    qualified = gps["allOf"][0]["then"]["properties"]
    assert qualified["fix_type"]["maximum"] == 8
    assert qualified["satellites_visible"]["maximum"] == 254
    assert qualified["latitude_deg"]["exclusiveMinimum"] == -90
    assert qualified["latitude_deg"]["exclusiveMaximum"] == 90


def test_target_event_replay_binds_gps_observation_time_and_freshness(tmp_path: Path) -> None:
    fresh_gps = TargetGpsEvidence(
        status="qualified",
        latitude_deg=1.0,
        longitude_deg=2.0,
        horizontal_sigma_m=1.0,
        fix_type=3,
        satellites_visible=8,
        horizontal_accuracy_m=2.0,
        vertical_accuracy_m=3.0,
        observed_at_s=9.0,
    )
    event = TargetEventInput(
        event_id="fresh-event",
        frame_id="fresh-frame",
        target_id="target-fresh",
        target_label="person",
        captured_at_s=10.0,
        gps=fresh_gps,
        ranging=TargetRangeEvidence(validity="VALID", range_m=4.0),
        video=TargetMediaInput(kind="video_frame", payload=b"frame", encoding="jpeg"),
    )
    assert fresh_gps.age_at(event.captured_at_s) == pytest.approx(1.0)

    builder = TargetEventReplayBuilder(tmp_path / "fresh", session_id=str(uuid4()))
    materialized = builder.append(event)
    assert materialized.gps_age_s == pytest.approx(1.0)
    assert materialized.gps_freshness == "fresh"
    mapping = materialized.to_mapping()
    assert mapping["gps"]["observed_at_s"] == pytest.approx(9.0)
    assert mapping["gps_age_s"] == pytest.approx(1.0)
    assert mapping["gps_freshness"] == "fresh"


def test_target_event_replay_rejects_future_and_stale_qualified_gps(tmp_path: Path) -> None:
    base = dict(
        status="qualified",
        latitude_deg=1.0,
        longitude_deg=2.0,
        horizontal_sigma_m=1.0,
        fix_type=3,
        satellites_visible=8,
        horizontal_accuracy_m=2.0,
        vertical_accuracy_m=3.0,
    )
    with pytest.raises(AutonomyContractError, match="newer than target capture"):
        TargetEventInput(
            event_id="future-event",
            frame_id="future-frame",
            target_id="target-future",
            target_label="person",
            captured_at_s=10.0,
            gps=TargetGpsEvidence(**base, observed_at_s=10.1),
            ranging=TargetRangeEvidence(validity="VALID", range_m=4.0),
            video=TargetMediaInput(kind="video_frame", payload=b"frame", encoding="jpeg"),
        )
    with pytest.raises(AutonomyContractError, match="stale"):
        TargetEventReplayBuilder(tmp_path / "stale", session_id=str(uuid4())).append(
            TargetEventInput(
                event_id="stale-event",
                frame_id="stale-frame",
                target_id="target-stale",
                target_label="person",
                captured_at_s=10.0,
                gps=TargetGpsEvidence(**base, observed_at_s=10.0 - GPS_QUALIFIED_MAX_AGE_S - 0.01),
                ranging=TargetRangeEvidence(validity="VALID", range_m=4.0),
                video=TargetMediaInput(kind="video_frame", payload=b"frame", encoding="jpeg"),
            )
        )


def test_target_event_replay_rejects_gps_observation_time_regression(
    tmp_path: Path,
) -> None:
    """Keep replay evidence chronological across event and GPS source clocks."""

    first = TargetEventInput(
        event_id="gps-order-1",
        frame_id="gps-frame-1",
        target_id="target-gps-order",
        target_label="person",
        captured_at_s=10.0,
        gps=_gps(observed_at_s=9.8),
        ranging=TargetRangeEvidence(validity="VALID", range_m=4.0),
        video=TargetMediaInput(kind="video_frame", payload=b"frame-1", encoding="jpeg"),
    )
    second = TargetEventInput(
        event_id="gps-order-2",
        frame_id="gps-frame-2",
        target_id="target-gps-order",
        target_label="person",
        captured_at_s=11.0,
        # The frame is newer, but the attached GPS observation regressed.  A
        # replay must reject this rather than silently presenting time-travel
        # location evidence.
        gps=_gps(observed_at_s=9.7),
        ranging=TargetRangeEvidence(validity="VALID", range_m=4.1),
        video=TargetMediaInput(kind="video_frame", payload=b"frame-2", encoding="jpeg"),
    )

    builder = TargetEventReplayBuilder(tmp_path / "gps-order", session_id=str(uuid4()))
    builder.append(first)
    with pytest.raises(AutonomyContractError, match="non-decreasing"):
        builder.append(second)

    # A legacy event without an observation timestamp does not reset the
    # chronology check; a later source that moves behind the last known sample
    # remains invalid when the finalized bundle is materialized.
    legacy_gap = TargetEventInput(
        event_id="gps-order-gap",
        frame_id="gps-frame-gap",
        target_id="target-gps-order",
        target_label="person",
        captured_at_s=10.5,
        gps=_gps(),
        ranging=TargetRangeEvidence(validity="VALID", range_m=4.2),
        video=TargetMediaInput(kind="video_frame", payload=b"frame-gap", encoding="jpeg"),
    )
    builder.append(legacy_gap)
    later_regression = TargetEventInput(
        event_id="gps-order-3",
        frame_id="gps-frame-3",
        target_id="target-gps-order",
        target_label="person",
        captured_at_s=11.0,
        gps=_gps(observed_at_s=9.75),
        ranging=TargetRangeEvidence(validity="VALID", range_m=4.3),
        video=TargetMediaInput(kind="video_frame", payload=b"frame-3", encoding="jpeg"),
    )
    with pytest.raises(AutonomyContractError, match="non-decreasing"):
        builder.append(later_regression)


def test_target_event_replay_rejects_tampered_derived_freshness(tmp_path: Path) -> None:
    root = tmp_path / "tampered-freshness"
    builder = TargetEventReplayBuilder(root, session_id=str(uuid4()))
    builder.append(
        TargetEventInput(
            event_id="fresh-event",
            frame_id="fresh-frame",
            target_id="target-fresh",
            target_label="person",
            captured_at_s=10.0,
            gps=TargetGpsEvidence(
                status="qualified",
                latitude_deg=1.0,
                longitude_deg=2.0,
                horizontal_sigma_m=1.0,
                fix_type=3,
                satellites_visible=8,
                horizontal_accuracy_m=2.0,
                vertical_accuracy_m=3.0,
                observed_at_s=9.0,
            ),
            ranging=TargetRangeEvidence(validity="VALID", range_m=4.0),
            video=TargetMediaInput(kind="video_frame", payload=b"frame", encoding="jpeg"),
        )
    )
    builder.finalize()
    event_line = json.loads((root / "events.jsonl").read_text(encoding="utf-8"))
    event_line["gps_age_s"] = 999.0
    with pytest.raises(AutonomyContractError, match="gps_age_s"):
        from multidetect.target_event_replay import TargetEvent

        TargetEvent.from_mapping(event_line, asset_root=root)


def test_target_event_replay_inspector_filters_without_exposing_absolute_paths(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "target-event-bundle"
    builder = TargetEventReplayBuilder(root, session_id=str(uuid4()))
    builder.append(_event(1.0))
    builder.append(_event(2.0, target_id="target-2"))
    builder.finalize()

    assert (
        inspect_target_event_replay(
            [
                str(root / "manifest.json"),
                "--target-id",
                "target-2",
                "--start-s",
                "1.5",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["summary"]["event_count"] == 2
    assert output["summary"]["locatable_event_count"] == 2
    assert output["summary"]["ranged_event_count"] == 2
    assert output["summary"]["media_event_count"] == 2
    assert output["summary"]["unique_target_count"] == 2
    assert output["summary"]["capture_duration_s"] == pytest.approx(1.0)
    assert output["summary"]["media_bytes"] == 27
    assert [item["target_id"] for item in output["events"]] == ["target-2"]
    assert output["events"][0]["gps_age_s"] is None
    assert output["events"][0]["gps_freshness"] == "unknown"
    assert str(root) not in captured.out


def test_target_event_replay_schema_matches_manifest_shape() -> None:
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "configs/schemas/target-event-replay-manifest.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["$schema"].endswith("draft/2020-12/schema")
    assert set(schema["required"]) == {
        "schema",
        "schema_version",
        "kind",
        "bundle_id",
        "session_id",
        "started_at_utc",
        "events",
        "media",
        "claims",
        "bundle_hash",
    }
    assert schema["$defs"]["claims"]["properties"]["contains_flight_control"] == {"const": False}
    assert schema["$defs"]["cameraCalibration"]["properties"]["path"] == {
        "const": "camera-calibration.json"
    }
    assert schema["$defs"]["targetEvent"]["properties"]["video"]["anyOf"]
    assert schema["$defs"]["gps"]["properties"]["observed_at_s"]["type"] == [
        "number",
        "null",
    ]
    assert schema["$defs"]["targetEvent"]["properties"]["gps_freshness"]["enum"] == [
        "fresh",
        "stale",
        "unknown",
    ]
    unavailable_range = schema["$defs"]["ranging"]["allOf"][1]["then"]["properties"]
    assert unavailable_range["relative_bearing_deg"] == {"type": "null"}


def test_target_event_replay_creator_copies_capture_media_and_emits_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    video_path = tmp_path / "capture.jpg"
    depth_path = tmp_path / "capture.npy"
    video_path.write_bytes(b"captured-video")
    depth_path.write_bytes(b"captured-depth")
    capture_manifest = tmp_path / "capture.json"
    calibration_path = _camera_calibration(tmp_path / "camera.json")
    capture_manifest.write_text(
        json.dumps(
            {
                "session_id": str(uuid4()),
                "bundle_id": "bundle-from-capture",
                "started_at_utc": "2026-08-30T08:00:00Z",
                "events": [
                    {
                        "event_id": "capture-event-0",
                        "frame_id": "camera-frame-0",
                        "target_id": "target-42",
                        "target_label": "person",
                        "captured_at_s": 4.25,
                        "gps": {
                            "status": "qualified",
                            "latitude_deg": 1.3521,
                            "longitude_deg": 103.8198,
                            "horizontal_sigma_m": 2.0,
                            "fix_type": 3,
                            "satellites_visible": 10,
                            "horizontal_accuracy_m": 3.0,
                            "vertical_accuracy_m": 4.0,
                        },
                        "ranging": {
                            "validity": "VALID",
                            "range_m": 25.0,
                            "relative_bearing_deg": 8.0,
                            "range_kind": "slant",
                        },
                        "video": {
                            "path": "capture.jpg",
                            "encoding": "jpeg",
                            "width": 1280,
                            "height": 720,
                        },
                        "depth": {"path": "capture.npy", "encoding": "npy"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output_root = tmp_path / "bundle"

    assert (
        create_target_event_replay(
            [
                str(capture_manifest),
                str(output_root),
                "--camera-calibration",
                str(calibration_path),
            ]
        )
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["bundle_id"] == "bundle-from-capture"
    assert summary["event_count"] == 1
    assert summary["media_count"] == 2
    assert summary["camera_calibration_id"] == "camera-main-001"
    loaded = load_target_event_replay_manifest(output_root / "manifest.json")
    assert (
        loaded.camera_calibration_sha256
        == hashlib.sha256(calibration_path.read_bytes()).hexdigest()
    )
    assert loaded.events[0].video is not None
    assert loaded.events[0].video.read_verified_bytes() == b"captured-video"
    assert loaded.events[0].depth is not None
    assert loaded.events[0].depth.read_verified_bytes() == b"captured-depth"


@pytest.mark.parametrize(
    "script_name",
    ("create_target_event_replay.py", "inspect_target_event_replay.py"),
)
def test_target_event_replay_scripts_run_without_pythonpath(script_name: str) -> None:
    """The documented replay helpers work from a clean checkout."""

    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, str(root / "scripts" / script_name), "--help"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert "--help" in completed.stdout
