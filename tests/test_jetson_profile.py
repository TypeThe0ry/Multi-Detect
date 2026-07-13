from __future__ import annotations

import json
from pathlib import Path

from multidetect.jetson_profile import jetson_static_preflight, load_environment_file

ROOT = Path(__file__).resolve().parents[1]


def test_example_jetson_profile_passes_static_placeholder_check_without_leaking() -> None:
    environment = ROOT / "deploy/jetson/runtime.env.example"

    report = jetson_static_preflight(
        ROOT / "configs/missions/fire_patrol.demo.json",
        environment,
        allow_placeholders=True,
    )

    assert report["valid"] is True
    assert report["mission_capability"] == "patrol_only"
    assert report["model_class_names"] == ("flame", "smoke")
    assert report["model_output_coordinates"] == "letterbox_xyxy_px"
    assert report["candidate_confidence_floor"] == 0.10
    assert report["flame_candidate_threshold"] == 0.72
    assert report["smoke_candidate_threshold"] == 0.60
    assert report["candidate_stability_frames"] == 6
    assert report["mission_minimum_confidence"] == 0.82
    assert report["pixhawk_read_only"] is True
    assert report["camera_opened"] is False
    assert report["model_loaded"] is False
    assert report["pixhawk_opened"] is False
    encoded = json.dumps(report)
    values = load_environment_file(environment)
    assert values["CAMERA_SOURCE"] not in encoded
    assert values["ALERT_HMAC_KEY"] not in encoded


def test_jetson_profile_rejects_short_secret_and_payload_mission(tmp_path: Path) -> None:
    source = (ROOT / "deploy/jetson/runtime.env.example").read_text(encoding="utf-8")
    environment = tmp_path / "runtime.env"
    environment.write_text(
        source.replace(
            "REPLACE_WITH_RANDOM_SECRET_OF_AT_LEAST_32_BYTES",
            "short",
        )
        .replace(
            "rtsp://USER:PASSWORD@CAMERA_HOST:554/STREAM",
            "rtsp://camera.local/live",
        )
        .replace("192.0.2.1", "ground.local"),
        encoding="utf-8",
    )

    report = jetson_static_preflight(
        ROOT / "configs/missions/fire_suppression.demo.json",
        environment,
    )

    assert report["valid"] is False
    assert any("must not declare payload" in error for error in report["errors"])
    assert any("at least 32 bytes" in error for error in report["errors"])
    assert "short" not in json.dumps(report)


def test_jetson_profile_rejects_thresholds_that_bypass_the_runtime_contract(
    tmp_path: Path,
) -> None:
    source = (ROOT / "deploy/jetson/runtime.env.example").read_text(encoding="utf-8")
    environment = tmp_path / "runtime.env"
    environment.write_text(
        source.replace("FIRE_CONFIDENCE_THRESHOLD=0.10", "FIRE_CONFIDENCE_THRESHOLD=0.70")
        .replace(
            "FIRE_SMOKE_CONFIDENCE_THRESHOLD=0.60",
            "FIRE_SMOKE_CONFIDENCE_THRESHOLD=0.50",
        )
        .replace(
            "FIRE_FLAME_CONFIDENCE_THRESHOLD=0.72",
            "FIRE_FLAME_CONFIDENCE_THRESHOLD=0.90",
        )
        .replace("FIRE_CANDIDATE_STABILITY_FRAMES=6", "FIRE_CANDIDATE_STABILITY_FRAMES=2"),
        encoding="utf-8",
    )

    report = jetson_static_preflight(
        ROOT / "configs/missions/fire_patrol.demo.json",
        environment,
        allow_placeholders=True,
    )

    assert report["valid"] is False
    assert any("cannot be below FIRE_CONFIDENCE_THRESHOLD" in error for error in report["errors"])
    assert any(
        "cannot exceed the mission minimum_confidence" in error for error in report["errors"]
    )
    assert any(
        "cannot be below mission minimum_track_observations" in error for error in report["errors"]
    )
