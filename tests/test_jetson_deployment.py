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
    assert "--provider" not in service
    assert "--trt-engine-cache" not in service
    assert "--pixhawk-system-id ${PIXHAWK_SYSTEM_ID}" in service
    assert "--pixhawk-expected-autopilot ${PIXHAWK_EXPECTED_AUTOPILOT}" in service
    assert "--pixhawk-expected-vehicle-type ${PIXHAWK_EXPECTED_VEHICLE_TYPE}" in service
    assert "--require-pixhawk-operational-state" in service
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
    assert "FIRE_MODEL_PATH=/opt/multi-detect/models/fire-smoke-nms.engine" in environment
    assert (
        "FIRE_MODEL_MANIFEST=/opt/multi-detect/models/fire-smoke-nms.engine.manifest.json"
        in environment
    )
    assert "FIRE_MODEL_OUTPUT_COORDINATES=letterbox_xyxy_px" in environment
    assert "FIRE_CONFIDENCE_THRESHOLD=0.10" in environment
    assert "FIRE_FLAME_CONFIDENCE_THRESHOLD=0.72" in environment
    assert "FIRE_SMOKE_CONFIDENCE_THRESHOLD=0.60" in environment
    assert "FIRE_CANDIDATE_STABILITY_FRAMES=6" in environment
    assert "PIXHAWK_HARDWARE_PROFILE=holybro_pixhawk_jetson_baseboard" in environment
    assert "PIXHAWK_ENDPOINT=udp:0.0.0.0:14550" in environment
    assert "PIXHAWK_BAUD=921600" in environment
    assert "PIXHAWK_SYSTEM_ID=1" in environment
    assert "PIXHAWK_EXPECTED_AUTOPILOT=px4" in environment
    assert "PIXHAWK_EXPECTED_VEHICLE_TYPE=fixed_wing" in environment
    assert "BEGIN PRIVATE KEY" not in environment
    assert "api_key" not in environment.lower()


def test_jetson_bench_launcher_uses_rtsp_tensorrt_and_read_only_v6x() -> None:
    launcher = (ROOT / "scripts/run_jetson_fire_patrol.sh").read_text(encoding="utf-8")

    assert "--source-env CAMERA_SOURCE" in launcher
    assert "--rtsp-codec h265" in launcher
    assert "--gstreamer-hardware-decode" in launcher
    assert '--reconnect-attempts "${CAMERA_RECONNECT_ATTEMPTS}"' in launcher
    assert '--reconnect-delay-seconds "${CAMERA_RECONNECT_DELAY_SECONDS}"' in launcher
    assert "best.opset17.fp16.engine" in launcher
    assert "--pixhawk-endpoint udp:0.0.0.0:14550" in launcher
    assert "--pixhawk-baud 921600" in launcher
    assert "--pixhawk-expected-autopilot px4" in launcher
    assert "--pixhawk-expected-vehicle-type fixed_wing" in launcher
    assert "--no-display" in launcher
    assert "--simulate-payload-cycle" not in launcher
    assert "--auto-simulate-payload-cycle" not in launcher
    assert 'if [[ "${OPERATOR_UDP_ENABLED}" == "1" ]]' in launcher
    assert '--operator-udp-port "${OPERATOR_UDP_PORT:-14580}"' in launcher
    assert "--operator-udp-bind-host" in launcher
    assert "--operator-hmac-key-env MULTIDETECT_OPERATOR_KEY" in launcher
    assert "--mavlink-signing-key-hex-env MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX" in launcher
    assert "--operator-local-system-id 1" in launcher
    assert "--operator-local-component-id 191" in launcher
    assert "--operator-remote-system-id 255" in launcher
    assert "--operator-remote-component-id 190" in launcher
    assert "MULTIDETECT_OPERATOR_KEY=" not in launcher
    assert "MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX=" not in launcher
    assert "command_long" not in launcher.lower()
    assert "mission_item" not in launcher.lower()
