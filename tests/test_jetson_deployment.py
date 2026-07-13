from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_jetson_service_is_patrol_only_read_only_and_unprivileged() -> None:
    service = (ROOT / "deploy/jetson/multi-detect.service").read_text(encoding="utf-8")
    patrol = json.loads(
        (ROOT / "configs/missions/fire_patrol.demo.json").read_text(encoding="utf-8")
    )

    assert "User=multidetect" in service
    assert "--require-production-approved-models" in service
    assert "--observe-pixhawk-lifecycle" in service
    assert "--task-area-mission-sequence" in service
    assert "--source-env CAMERA_SOURCE" in service
    assert "--source ${CAMERA_SOURCE}" not in service
    assert "--alert-udp-host ${ALERT_RECEIVER_HOST}" in service
    assert "--alert-hmac-key-env ALERT_HMAC_KEY" in service
    assert "--class-names ${FIRE_MODEL_CLASS_NAMES}" in service
    assert "--output-coordinates ${FIRE_MODEL_OUTPUT_COORDINATES}" in service
    assert "--confidence-threshold ${FIRE_CONFIDENCE_THRESHOLD}" in service
    assert "--flame-confidence-threshold ${FIRE_FLAME_CONFIDENCE_THRESHOLD}" in service
    assert "--smoke-confidence-threshold ${FIRE_SMOKE_CONFIDENCE_THRESHOLD}" in service
    assert "--candidate-stability-frames ${FIRE_CANDIDATE_STABILITY_FRAMES}" in service
    assert "${ALERT_HMAC_KEY}" not in service
    assert "--simulate-payload-cycle" not in service
    assert "--auto-simulate-payload-cycle" not in service
    assert "--allow-synthetic-hil-model" not in service
    assert "--prediction-log-out" not in service
    assert "payload-inventory-check" not in service
    assert "GPIO" not in service
    assert patrol["payloads"] == []
    assert patrol["payload_count"] == 0


def test_jetson_environment_template_contains_only_placeholders() -> None:
    environment = (ROOT / "deploy/jetson/runtime.env.example").read_text(encoding="utf-8")

    assert "USER:PASSWORD" in environment
    assert "CAMERA_HOST" in environment
    assert "192.0.2.1" in environment
    assert "REPLACE_WITH_RANDOM_SECRET" in environment
    assert "FIRE_MODEL_CLASS_NAMES=flame,smoke" in environment
    assert "FIRE_MODEL_OUTPUT_COORDINATES=letterbox_xyxy_px" in environment
    assert "FIRE_CONFIDENCE_THRESHOLD=0.10" in environment
    assert "FIRE_FLAME_CONFIDENCE_THRESHOLD=0.72" in environment
    assert "FIRE_SMOKE_CONFIDENCE_THRESHOLD=0.60" in environment
    assert "FIRE_CANDIDATE_STABILITY_FRAMES=6" in environment
    assert "BEGIN PRIVATE KEY" not in environment
    assert "api_key" not in environment.lower()
