from __future__ import annotations

from pathlib import Path

import pytest

from multidetect.domain import SensorKind
from multidetect.replay import (
    ReplayRecording,
    frame_from_mapping,
    iter_jsonl_replay,
    load_jsonl_replay,
)

ROOT = Path(__file__).resolve().parents[1]
AIRFRAME_HASH = "a" * 64


def test_demo_replay_loads_and_normalizes_fire_label() -> None:
    frames = load_jsonl_replay(ROOT / "examples/fire_mission_replay.jsonl")

    assert len(frames) == 4
    assert frames[0].detections[0].label == "flame"
    assert len(frames[0].detections) == 1
    assert frames[0].detections[0].sensor is SensorKind.RGB
    assert frames[0].detections[0].metadata["independent_rgb_corroborated"] is True


def test_replay_rejects_duplicate_frame() -> None:
    line = (
        '{"frame_id":"same","captured_at_s":1,"telemetry":'
        '{"altitude_agl_m":10,"roll_deg":0,"pitch_deg":0,"ground_speed_mps":0,'
        '"in_allowed_zone":true,"geofence_healthy":true,"position_healthy":true,'
        '"link_healthy":true,"flight_mode_allows_deploy":true,'
        '"release_zone_clear":true},"detections":[]}\n'
    )

    with pytest.raises(ValueError, match="duplicate"):
        tuple(iter_jsonl_replay([line, line]))


def test_replay_maps_extended_read_only_flight_status() -> None:
    frame = frame_from_mapping(
        {
            "frame_id": "telemetry-frame",
            "captured_at_s": 10.0,
            "telemetry": {
                "altitude_agl_m": 30.0,
                "roll_deg": 1.0,
                "pitch_deg": -2.0,
                "ground_speed_mps": 12.0,
                "armed": True,
                "flight_mode": "AUTO",
                "mission_sequence": 6,
            },
            "detections": [],
        }
    )

    assert frame.telemetry.armed is True
    assert frame.telemetry.flight_mode == "AUTO"
    assert frame.telemetry.mission_sequence == 6


def test_replay_canonical_mapping_omits_absent_mission_feedback_metadata() -> None:
    frame = frame_from_mapping(
        {
            "frame_id": "legacy-mission-cursor",
            "captured_at_s": 10.0,
            "telemetry": {
                "altitude_agl_m": 30.0,
                "roll_deg": 1.0,
                "pitch_deg": -2.0,
                "ground_speed_mps": 12.0,
                "mission_sequence": 6,
            },
            "detections": [],
        }
    )

    mapping = ReplayRecording(
        recording_id="legacy-mission-cursor",
        frames=(frame,),
        airframe_profile_hash=AIRFRAME_HASH,
    ).to_mapping()
    telemetry = mapping["frames"][0]["telemetry"]

    assert telemetry["mission_sequence"] == 6
    assert "mission_sequence_observed_at_s" not in telemetry
    assert "mission_item_reached_sequence" not in telemetry
    assert "mission_item_reached_observed_at_s" not in telemetry


def test_replay_preserves_every_navigation_and_gps_quality_field() -> None:
    frame = frame_from_mapping(
        {
            "frame_id": "full-navigation-telemetry",
            "captured_at_s": 25.0,
            "telemetry": {
                "altitude_agl_m": 42.5,
                "roll_deg": 3.0,
                "pitch_deg": -1.5,
                "ground_speed_mps": 19.0,
                "latitude_deg": 1.30042,
                "longitude_deg": 103.80118,
                "heading_deg": 87.0,
                "battery_remaining_pct": 73.0,
                "satellites_visible": 18,
                "gps_fix_type": 3,
                "gps_horizontal_accuracy_m": 0.7,
                "gps_vertical_accuracy_m": 1.2,
                "gps_observed_at_s": 24.9,
                "gps_absolute_altitude_m": 47.75,
                "attitude_observed_at_s": 24.95,
                "position_observed_at_s": 24.9,
                "velocity_north_mps": 1.25,
                "velocity_east_mps": 18.75,
                "airspeed_mps": 20.0,
                "wind_north_mps": -0.5,
                "wind_east_mps": 1.0,
                "velocity_observed_at_s": 24.92,
                "airspeed_observed_at_s": 24.91,
                "wind_observed_at_s": 24.8,
                "altitude_observed_at_s": 24.94,
                "altitude_reference": "local_ned",
                "local_north_m": 125.0,
                "local_east_m": -18.0,
                "local_down_m": -42.5,
                "local_position_observed_at_s": 24.93,
            },
            "detections": [],
        }
    )

    telemetry = frame.telemetry
    assert telemetry.gps_fix_type == 3
    assert telemetry.gps_horizontal_accuracy_m == pytest.approx(0.7)
    assert telemetry.gps_vertical_accuracy_m == pytest.approx(1.2)
    assert telemetry.gps_observed_at_s == pytest.approx(24.9)
    assert telemetry.gps_absolute_altitude_m == pytest.approx(47.75)
    assert telemetry.attitude_observed_at_s == pytest.approx(24.95)
    assert telemetry.position_observed_at_s == pytest.approx(24.9)
    assert telemetry.altitude_observed_at_s == pytest.approx(24.94)
    assert telemetry.altitude_reference == "local_ned"
    assert telemetry.local_north_m == pytest.approx(125.0)
    assert telemetry.local_east_m == pytest.approx(-18.0)
    assert telemetry.local_down_m == pytest.approx(-42.5)
    assert telemetry.local_position_observed_at_s == pytest.approx(24.93)

    mapping = ReplayRecording(
        recording_id="full-navigation-telemetry",
        frames=(frame,),
        airframe_profile_hash=AIRFRAME_HASH,
    ).to_mapping()
    assert mapping["frames"][0]["telemetry"]["gps_absolute_altitude_m"] == pytest.approx(47.75)


def test_replay_rejects_nonfinite_frame_timestamp() -> None:
    with pytest.raises(ValueError, match="finite"):
        frame_from_mapping(
            {
                "frame_id": "invalid-time",
                "captured_at_s": float("nan"),
                "telemetry": {
                    "altitude_agl_m": 10.0,
                    "roll_deg": 0.0,
                    "pitch_deg": 0.0,
                    "ground_speed_mps": 1.0,
                },
                "detections": [],
            }
        )


def test_replay_recording_loads_the_same_frame_observation_contract() -> None:
    recording = ReplayRecording.from_jsonl(
        ROOT / "examples/fire_mission_replay.jsonl",
        airframe_profile_hash=AIRFRAME_HASH,
    )
    legacy_frames = load_jsonl_replay(ROOT / "examples/fire_mission_replay.jsonl")

    assert recording.recording_id == "fire_mission_replay"
    assert tuple(frame.frame_id for frame in recording.frames) == tuple(
        frame.frame_id for frame in legacy_frames
    )
    assert tuple(frame.captured_at_s for frame in recording.frames) == tuple(
        frame.captured_at_s for frame in legacy_frames
    )


def test_replay_recording_rejects_duplicate_frames() -> None:
    frame = load_jsonl_replay(ROOT / "examples/fire_mission_replay.jsonl")[0]

    with pytest.raises(ValueError, match="duplicate"):
        ReplayRecording("duplicate", (frame, frame), AIRFRAME_HASH)


def test_replay_recording_is_deeply_immutable_and_content_hashed() -> None:
    frame = load_jsonl_replay(ROOT / "examples/fire_mission_replay.jsonl")[0]
    mutable_metadata = frame.detections[0].metadata
    source = [frame]
    recording = ReplayRecording(" immutable ", source, AIRFRAME_HASH.upper())
    observed_hash = recording.recording_hash

    source.clear()
    mutable_metadata["later_mutation"] = True  # type: ignore[index]

    assert recording.recording_id == "immutable"
    assert recording.airframe_profile_hash == AIRFRAME_HASH
    assert len(recording.frames) == 1
    assert "later_mutation" not in recording.frames[0].detections[0].metadata
    assert recording.recording_hash == observed_hash
    with pytest.raises(TypeError):
        recording.frames[0].detections[0].metadata["blocked"] = True  # type: ignore[index]
