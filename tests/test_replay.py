from __future__ import annotations

from pathlib import Path

import pytest

from multidetect.domain import SensorKind
from multidetect.replay import frame_from_mapping, iter_jsonl_replay, load_jsonl_replay

ROOT = Path(__file__).resolve().parents[1]


def test_demo_replay_loads_and_normalizes_fire_label() -> None:
    frames = load_jsonl_replay(ROOT / "examples/fire_mission_replay.jsonl")

    assert len(frames) == 4
    assert frames[0].detections[0].label == "flame"
    assert frames[0].detections[1].sensor is SensorKind.THERMAL


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
