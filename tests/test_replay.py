from __future__ import annotations

from pathlib import Path

import pytest

from multidetect.domain import SensorKind
from multidetect.replay import iter_jsonl_replay, load_jsonl_replay

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
