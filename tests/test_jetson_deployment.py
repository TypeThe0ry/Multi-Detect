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
    assert "${ALERT_HMAC_KEY}" not in service
    assert "--simulate-payload-cycle" not in service
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
    assert "BEGIN PRIVATE KEY" not in environment
    assert "api_key" not in environment.lower()
