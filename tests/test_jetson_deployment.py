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
    assert "FIRE_FLAME_CONFIDENCE_THRESHOLD=0.25" in environment
    assert "FIRE_SMOKE_CONFIDENCE_THRESHOLD=0.30" in environment
    assert "FIRE_CANDIDATE_STABILITY_FRAMES=3" in environment
    assert "PIXHAWK_HARDWARE_PROFILE=holybro_pixhawk_jetson_baseboard" in environment
    assert "PIXHAWK_ENDPOINT=udp:0.0.0.0:14550" in environment
    assert "PIXHAWK_BAUD=921600" in environment
    assert "PIXHAWK_SYSTEM_ID=1" in environment
    assert "PIXHAWK_EXPECTED_AUTOPILOT=px4" in environment
    assert "PIXHAWK_EXPECTED_VEHICLE_TYPE=fixed_wing" in environment
    assert "BEGIN PRIVATE KEY" not in environment
    assert "api_key" not in environment.lower()


def test_jetson_bench_launcher_uses_rtsp_tensorrt_and_gated_v6x_control() -> None:
    launcher = (ROOT / "scripts/run_jetson_fire_patrol.sh").read_text(encoding="utf-8")

    assert "--source-env CAMERA_SOURCE" in launcher
    assert 'METRIC_DEPTH_CALIBRATION_SCALE="${METRIC_DEPTH_CALIBRATION_SCALE:-0.282}"' in launcher
    assert "outdoor-field-scale-0.282-20260721" in launcher
    assert "--rtsp-codec h265" in launcher
    assert "--gstreamer-hardware-decode" in launcher
    assert 'GSTREAMER_LATENCY_MS="${GSTREAMER_LATENCY_MS:-50}"' in launcher
    assert 'CAPTURE_QUEUE_FRAMES="${CAPTURE_QUEUE_FRAMES:-1}"' in launcher
    assert 'FIRE_FLAME_CONFIDENCE_THRESHOLD="${FIRE_FLAME_CONFIDENCE_THRESHOLD:-0.25}"' in launcher
    assert 'FIRE_SMOKE_CONFIDENCE_THRESHOLD="${FIRE_SMOKE_CONFIDENCE_THRESHOLD:-0.30}"' in launcher
    assert 'FIRE_CANDIDATE_STABILITY_FRAMES="${FIRE_CANDIDATE_STABILITY_FRAMES:-3}"' in launcher
    assert 'PRIMARY_FIRE_MODEL_FRAME_STRIDE="${PRIMARY_FIRE_MODEL_FRAME_STRIDE:-2}"' in launcher
    assert 'PRIMARY_FIRE_MODEL_FRAME_PHASE="${PRIMARY_FIRE_MODEL_FRAME_PHASE:-0}"' in launcher
    assert 'LOCK_MODEL_FORCE_EVERY_FRAME="${LOCK_MODEL_FORCE_EVERY_FRAME:-0}"' in launcher
    assert 'MONOCULAR_AVOIDANCE_ENABLED="${MONOCULAR_AVOIDANCE_ENABLED:-1}"' in launcher
    assert 'UNIFIED_TARGET_POOL_ENABLED="${UNIFIED_TARGET_POOL_ENABLED:-1}"' in launcher
    assert "UNIFIED_TARGET_POOL_PRIORITY_MINIMUM_NEW_TRACK_CONFIDENCE" in launcher
    assert 'PATROL_ADVISORY_ENABLED="${PATROL_ADVISORY_ENABLED:-1}"' in launcher
    assert 'COMMON_OBJECT_DETECTOR_ENABLED="${COMMON_OBJECT_DETECTOR_ENABLED:-auto}"' in launcher
    assert 'PERSON_REID_ENABLED="${PERSON_REID_ENABLED:-0}"' in launcher
    assert 'VEHICLE_REID_ENABLED="${VEHICLE_REID_ENABLED:-0}"' in launcher
    assert 'SEMANTIC_CONTEXT_ENABLED="${SEMANTIC_CONTEXT_ENABLED:-0}"' in launcher
    assert 'SHORT_TERM_TRACKING_ENABLED="${SHORT_TERM_TRACKING_ENABLED:-1}"' in launcher
    assert 'MULTIMODAL_RANGING_ENABLED="${MULTIMODAL_RANGING_ENABLED:-0}"' in launcher
    assert 'MODE3_CONFIRMATION_ENABLED="${MODE3_CONFIRMATION_ENABLED:-0}"' in launcher
    assert 'MODE3_AIM_CONTROL_ENABLED="${MODE3_AIM_CONTROL_ENABLED:-0}"' in launcher
    assert 'RANGING_CALIBRATION_PATH="${RANGING_CALIBRATION_PATH:-}"' in launcher
    assert '--gstreamer-latency-ms "${GSTREAMER_LATENCY_MS}"' in launcher
    assert '--capture-queue-frames "${CAPTURE_QUEUE_FRAMES}"' in launcher
    assert '--primary-model-frame-stride "${PRIMARY_FIRE_MODEL_FRAME_STRIDE}"' in launcher
    assert '--primary-model-frame-phase "${PRIMARY_FIRE_MODEL_FRAME_PHASE}"' in launcher
    assert '--flame-confidence-threshold "${FIRE_FLAME_CONFIDENCE_THRESHOLD}"' in launcher
    assert '--smoke-confidence-threshold "${FIRE_SMOKE_CONFIDENCE_THRESHOLD}"' in launcher
    assert '--candidate-stability-frames "${FIRE_CANDIDATE_STABILITY_FRAMES}"' in launcher
    assert "--monocular-avoidance" in launcher
    assert '--avoidance-analysis-width "${AVOIDANCE_ANALYSIS_WIDTH:-320}"' in launcher
    assert "MONOCULAR_AVOIDANCE_ENABLED must be 0 or 1" in launcher
    assert "--unified-target-pool" in launcher
    assert '--identity-tracking-log-out "${identity_tracking_out}"' in launcher
    assert '--identity-tracking-session-id "${tracking_evidence_session_id}"' in launcher
    assert "import uuid; print(uuid.uuid4())" in launcher
    assert (
        'identity_tracking_out="${EVIDENCE_DIR}/jetson-live-${timestamp}.identity-tracks.jsonl"'
        in launcher
    )
    assert (
        '--unified-target-pool-maximum-tracks "${UNIFIED_TARGET_POOL_MAXIMUM_TRACKS:-64}"'
        in launcher
    )
    assert "--unified-target-pool-minimum-association-confidence" in launcher
    assert "--unified-target-pool-minimum-new-track-confidence" in launcher
    assert "--unified-target-pool-high-confidence-threshold" in launcher
    assert "--unified-target-pool-kalman-process-noise" in launcher
    assert "--unified-target-pool-kalman-measurement-noise" in launcher
    assert "--unified-target-pool-kalman-gate-sigma" in launcher
    assert "--unified-target-pool-kalman-maximum-horizon-seconds" in launcher
    assert "PERSON_REID_ENABLED=1 requires UNIFIED_TARGET_POOL_ENABLED=1" in launcher
    assert "PATROL_ADVISORY_ENABLED=1 requires UNIFIED_TARGET_POOL_ENABLED=1" in launcher
    assert "--patrol-advisory" in launcher
    assert '--patrol-maximum-bank-angle-deg "${PATROL_MAXIMUM_BANK_ANGLE_DEG:-25.0}"' in launcher
    assert '--safety-onnx-model "${COMMON_OBJECT_MODEL_PATH}"' in launcher
    assert '--safety-model-manifest "${COMMON_OBJECT_MODEL_MANIFEST}"' in launcher
    assert 'COMMON_OBJECT_ENGINE_PROVENANCE="${COMMON_OBJECT_ENGINE_PROVENANCE:-' in launcher
    assert "--safety-model-coco80" in launcher
    assert "--safety-model-format ultralytics_raw" in launcher
    assert '--safety-model-iou-threshold "${COMMON_OBJECT_IOU_THRESHOLD}"' in launcher
    assert '--safety-model-maximum-detections "${COMMON_OBJECT_MAXIMUM_DETECTIONS}"' in launcher
    assert '--safety-model-frame-stride "${COMMON_OBJECT_FRAME_STRIDE}"' in launcher
    assert '--safety-model-frame-phase "${COMMON_OBJECT_FRAME_PHASE}"' in launcher
    assert 'if [[ "${COMMON_OBJECT_DETECTOR_ENABLED}" == "auto" ]]' in launcher
    assert "yolo26n-traditional.b1.fp16.trt86.engine" in launcher
    assert '--safety-confidence-threshold "${COMMON_OBJECT_CONFIDENCE_THRESHOLD}"' in launcher
    assert "COMMON_OBJECT_PRIORITY_CONFIDENCE_THRESHOLD" in launcher
    assert "COMMON_OBJECT_FALLBACK_CONFIDENCE_THRESHOLD" in launcher
    assert "COMMON_OBJECT_TILE_CONFIDENCE_THRESHOLD" in launcher
    assert "COMMON_OBJECT_TILE_MAXIMUM_BOX_AREA" in launcher
    assert "COMMON_OBJECT_TILE_FUSION_IOU_THRESHOLD" in launcher
    assert '--safety-tile-labels "${COMMON_OBJECT_TILE_LABELS}"' in launcher
    assert "--no-lock-model-force-every-frame" in launcher
    assert "COMMON_OBJECT_DETECTOR_ENABLED must be auto, 0 or 1" in launcher
    assert '--person-reid-engine "${PERSON_REID_ENGINE_PATH}"' in launcher
    assert '--person-reid-frame-stride "${PERSON_REID_FRAME_STRIDE:-2}"' in launcher
    assert "resnet50_market1501_aicity156.b1-b10.fp16.trt86.engine" in launcher
    assert 'sha256sum --check --status "${PERSON_REID_ENGINE_SHA256_PATH}"' in launcher
    assert 'PERSON_REID_ENGINE_PROVENANCE="${PERSON_REID_ENGINE_PROVENANCE:-' in launcher
    assert "VEHICLE_REID_ENABLED=1 requires UNIFIED_TARGET_POOL_ENABLED=1" in launcher
    assert '--vehicle-reid-onnx "${VEHICLE_REID_ONNX_PATH}"' in launcher
    assert "--allow-nonrealtime-reid" not in launcher
    assert '--vehicle-reid-engine "${VEHICLE_REID_ENGINE_PATH}"' in launcher
    assert '--vehicle-reid-frame-stride "${VEHICLE_REID_FRAME_STRIDE:-2}"' in launcher
    assert '--reid-maximum-interval-seconds "${REID_MAXIMUM_INTERVAL_SECONDS:-0.1}"' in launcher
    assert 'sha256sum --check --status "${VEHICLE_REID_ENGINE_SHA256_PATH}"' in launcher
    assert 'VEHICLE_REID_ENGINE_PROVENANCE="${VEHICLE_REID_ENGINE_PROVENANCE:-' in launcher
    assert "osnet_ain_x1_0_vehicle_reid.b1-b8.fp16.trt86.engine" in launcher
    assert "ENVIRONMENT_RISK_DETECTOR_ENABLED=1 requires UNIFIED_TARGET_POOL_ENABLED=1" in launcher
    assert '--environment-onnx-model "${ENVIRONMENT_ENGINE_PATH}"' in launcher
    assert '--environment-model-manifest "${ENVIRONMENT_MODEL_MANIFEST}"' in launcher
    assert '--environment-class-names "${ENVIRONMENT_CLASS_NAMES}"' in launcher
    assert "power_line,flammable_tank" in launcher
    assert '--semantic-context-onnx-model "${SEMANTIC_CONTEXT_ONNX_PATH}"' in launcher
    assert '--semantic-context-model-manifest "${SEMANTIC_CONTEXT_MODEL_MANIFEST}"' in launcher
    assert '--semantic-context-engine "${SEMANTIC_CONTEXT_ENGINE_PATH}"' in launcher
    assert (
        '--semantic-context-engine-provenance "${SEMANTIC_CONTEXT_ENGINE_PROVENANCE}"' in launcher
    )
    assert 'sha256sum --check --status "${SEMANTIC_CONTEXT_ENGINE_SHA256_PATH}"' in launcher
    assert "multidetect.engine_provenance verify" in launcher
    assert "SEMANTIC_CONTEXT_ENABLED must be 0 or 1" in launcher
    assert "--short-term-tracking" in launcher
    assert '--short-term-maximum-tracks "${SHORT_TERM_MAXIMUM_TRACKS:-16}"' in launcher
    assert '--short-term-analysis-width "${SHORT_TERM_ANALYSIS_WIDTH:-320}"' in launcher
    assert '--short-term-minimum-box-size-px "${SHORT_TERM_MINIMUM_BOX_SIZE_PX:-8}"' in launcher
    assert '--short-term-frame-stride "${SHORT_TERM_FRAME_STRIDE:-1}"' in launcher
    assert '--short-term-search-expansion "${SHORT_TERM_SEARCH_EXPANSION:-2.5}"' in launcher
    assert "SHORT_TERM_OCCLUDED_SEARCH_MULTIPLIER:-1.5" in launcher
    assert "SHORT_TERM_REACQUIRING_SEARCH_MULTIPLIER:-2.0" in launcher
    assert "SHORT_TERM_MAXIMUM_SEARCH_EXPANSION:-6.0" in launcher
    assert "SHORT_TERM_MAXIMUM_RETAINED_TEMPLATE_AGE_SECONDS:-2.0" in launcher
    assert "SHORT_TERM_TRACKING_ENABLED=1 requires UNIFIED_TARGET_POOL_ENABLED=1" in launcher
    assert "MULTIMODAL_RANGING_ENABLED=1 requires UNIFIED_TARGET_POOL_ENABLED=1" in launcher
    assert "RANGING_CALIBRATION_PATH must name an existing calibrated camera" in launcher
    assert "--multimodal-ranging" in launcher
    assert '--ranging-calibration "${RANGING_CALIBRATION_PATH}"' in launcher
    assert "MODE3_CONFIRMATION_ENABLED=1 requires OPERATOR_UDP_ENABLED=1" in launcher
    assert "MODE3_AIM_CONTROL_ENABLED=1 requires MODE3_CONFIRMATION_ENABLED=1" in launcher
    assert "--mode3-aim" in launcher
    assert "--fixed-wing-aim-control" in launcher
    assert '--aim-control-mode "${AIM_CONTROL_MODE:-OFFBOARD}"' in launcher
    assert '--aim-return-mode "${AIM_RETURN_MODE:-AUTO}"' in launcher
    assert '--aim-rc-input-rate-hz "${AIM_RC_INPUT_RATE_HZ:-20.0}"' in launcher
    assert (
        '--aim-rc-input-maximum-age-seconds "${AIM_RC_INPUT_MAXIMUM_AGE_SECONDS:-0.30}"'
        in launcher
    )
    assert '--aim-rc-cancel-threshold-us "${AIM_RC_CANCEL_THRESHOLD_US:-50}"' in launcher
    assert '--reconnect-attempts "${CAMERA_RECONNECT_ATTEMPTS}"' in launcher
    assert '--reconnect-delay-seconds "${CAMERA_RECONNECT_DELAY_SECONDS}"' in launcher
    assert "best.opset17.fp16.engine" in launcher
    assert "--pixhawk-endpoint udp:0.0.0.0:14550" in launcher
    assert "--pixhawk-baud 921600" in launcher
    assert "--pixhawk-expected-autopilot px4" in launcher
    assert "--pixhawk-expected-vehicle-type fixed_wing" in launcher
    assert "--no-display" in launcher


def test_multimodal_ranging_deployer_stages_hashes_before_service_restart() -> None:
    deployer = (ROOT / "scripts/deploy_jetson_multimodal_ranging.ps1").read_text(
        encoding="utf-8"
    )

    assert "src/multidetect/adaptive_ranging.py" in deployer
    assert "src/multidetect/rgb_slam_range.py" in deployer
    assert "src/multidetect/operator_protocol.py" in deployer
    assert "from multidetect.operator_protocol import OperatorTunnelCodec" in deployer
    assert "source-sha256.txt" in deployer
    assert "[System.IO.File]::WriteAllText" in deployer
    assert '($manifest -join "`n") + "`n"' in deployer
    assert "[System.Text.Encoding]::ASCII" in deployer
    assert "sha256sum --check source-sha256.txt" in deployer
    assert "-m compileall -q src scripts" in deployer
    assert "bash -n scripts/run_jetson_fire_patrol.sh" in deployer
    assert 'chmod 755 "`$root/scripts/run_jetson_fire_patrol.sh"' in deployer
    assert 'test -x "`$root/scripts/run_jetson_fire_patrol.sh"' in deployer
    assert "RgbSlamRangeEstimator().config.minimum_range_m == 0.4" in deployer
    assert "RgbSlamRangeEstimator().config.maximum_range_m == 800.0" in deployer
    assert "sudo -n systemctl restart" in deployer
    assert "QGC source and build artifacts stay local" in deployer


def test_jetson_rtsp_evidence_recorder_is_independent_redacted_stream_copy() -> None:
    recorder = (ROOT / "scripts/record_jetson_rtsp_evidence.sh").read_text(encoding="utf-8")
    launcher = (ROOT / "scripts/run_jetson_fire_patrol.sh").read_text(encoding="utf-8")

    assert "record-rtsp-evidence" in recorder
    assert '--source-env "${SOURCE_ENV_NAME}"' in recorder
    assert '--out-video "${video_out}"' in recorder
    assert '--manifest-out "${manifest_out}"' in recorder
    assert '--session-id "${tracking_evidence_session_id}"' in recorder
    assert 'tracking_evidence_session_id="${TRACKING_EVIDENCE_SESSION_ID:-}"' in recorder
    assert "must match the live tracker session" in recorder
    assert 'SOURCE_ENV_NAME="${SOURCE_ENV_NAME:-CAMERA_SOURCE}"' in recorder
    assert "${CAMERA_SOURCE}" not in recorder
    assert "live-camera" not in recorder
    assert "pixhawk" not in recorder.lower()
    assert "payload" in recorder.lower()
    assert "--simulate-payload-cycle" not in recorder
    assert "--simulate-payload-cycle" not in launcher
    assert "--auto-simulate-payload-cycle" not in launcher
    assert 'if [[ "${OPERATOR_UDP_ENABLED}" == "1" ]]' in launcher
    assert 'OPERATOR_UDP_ENABLED="${OPERATOR_UDP_ENABLED:-auto}"' in launcher
    assert 'if [[ "${OPERATOR_UDP_ENABLED}" == "auto" ]]' in launcher
    assert 'MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX:-' in launcher
    assert "OPERATOR_UDP_ENABLED must be auto, 0 or 1" in launcher
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


def test_common_object_candidate_records_contract_hash_and_coverage_gap() -> None:
    descriptor = json.loads(
        (ROOT / "configs/models/ultralytics_yolo26n_coco80.json").read_text(encoding="utf-8")
    )

    assert descriptor["model_role"] == "safety_object_evidence"
    assert descriptor["artifact_sha256"] == (
        "cd89b16b1497b4c1cda0b57404f6582f99aa3ba8b9bc3f79b3350a081528b85f"
    )
    assert descriptor["output"]["format"] == "post_nms_Nx6"
    assert {"person", "car", "bus", "truck"}.issubset(descriptor["validated_labels"])
    assert set(descriptor["missing_required_labels"]) == {"power_line", "flammable_tank"}
    assert set(descriptor["missing_semantic_context_labels"]) == {"building", "road"}
    assert descriptor["production_approved"] is False
    assert descriptor["flight_control_enabled"] is False
    assert descriptor["physical_release_enabled"] is False


def test_environment_detector_contract_keeps_missing_coverage_explicit() -> None:
    descriptor = json.loads(
        (ROOT / "configs/models/environment_risk_detector_contract.json").read_text(
            encoding="utf-8"
        )
    )
    assert descriptor["model_role"] == "environment_risk_evidence"
    assert descriptor["status"] == "required_not_supplied"
    assert set(descriptor["required_labels"]) == {"power_line", "flammable_tank"}
    assert descriptor["runtime_contract"]["output_format"] == "post_nms_Nx6"
    assert descriptor["production_approved"] is False
    assert descriptor["flight_control_enabled"] is False
    assert descriptor["physical_release_enabled"] is False


def test_common_object_engine_builder_is_hash_pinned_and_refuses_live_build() -> None:
    builder = (ROOT / "scripts/build_jetson_common_detector_engine.sh").read_text(encoding="utf-8")

    assert 'hash_sidecar="${ONNX}.sha256"' in builder
    assert "COMMON_OBJECT_EXPECTED_SHA256" in builder
    assert "expected SHA-256 is malformed" in builder
    assert "pgrep -f 'multidetect live-camera'" in builder
    assert "refusing a concurrent TensorRT engine build" in builder
    assert "--fp16" in builder
    assert "--skipInference" in builder
    assert "--timingCacheFile" in builder
    assert "yolo26n-traditional.onnx" in builder
    assert "--safety-model-format ultralytics_raw" in builder
    assert "--native-output-format ultralytics_raw_xywh_class_scores" in builder
    assert '--model-artifact "${ENGINE}"' in builder
    assert "COMMON_OBJECT_MODEL_MANIFEST" in builder
    assert "Hash-bound runtime manifest written" in builder
    assert "multidetect.engine_provenance write" in builder
    assert "command_long" not in builder.lower()
    assert "mission_item" not in builder.lower()


def test_priority_object_engine_builder_is_hash_pinned_and_refuses_live_build() -> None:
    builder = (ROOT / "scripts/build_jetson_priority_detector_engine.sh").read_text(
        encoding="utf-8"
    )

    assert 'hash_sidecar="${ONNX}.sha256"' in builder
    assert "PRIORITY_OBJECT_EXPECTED_SHA256" in builder
    assert "pgrep -f 'multidetect live-camera'" in builder
    assert "refusing a concurrent TensorRT engine build" in builder
    assert "--fp16" in builder
    assert "--skipInference" in builder
    assert "--timingCacheFile" in builder
    assert "best.onnx" in builder
    assert "images:1x3x960x960" in builder
    assert "ultralytics_raw_xywh_class_scores" in builder
    assert "multidetect.engine_provenance write" in builder
    assert "model-manifest-init" in builder


def test_jetson_launcher_autoloads_priority_object_detector() -> None:
    launcher = (ROOT / "scripts/run_jetson_fire_patrol.sh").read_text(encoding="utf-8")

    assert (
        'PRIORITY_OBJECT_DETECTOR_ENABLED="${PRIORITY_OBJECT_DETECTOR_ENABLED:-auto}"'
        in launcher
    )
    assert "models/visdrone-yolo26n-e30-960/best.onnx" in launcher
    assert "best.b1.fp16.trt86.engine" in launcher
    assert "pedestrian=person,people=person,van=car" in launcher
    assert '--priority-onnx-model "${PRIORITY_OBJECT_MODEL_PATH}"' in launcher
    assert '--priority-model-manifest "${PRIORITY_OBJECT_MODEL_MANIFEST}"' in launcher
    assert '--priority-confidence-threshold "${PRIORITY_OBJECT_CONFIDENCE_THRESHOLD}"' in launcher
    assert (
        '--priority-person-confidence-threshold '
        '"${PRIORITY_OBJECT_PERSON_CONFIDENCE_THRESHOLD}"' in launcher
    )
    assert (
        '--priority-vehicle-confidence-threshold '
        '"${PRIORITY_OBJECT_VEHICLE_CONFIDENCE_THRESHOLD}"' in launcher
    )
    assert (
        'PRIORITY_OBJECT_CAR_SINGLE_SOURCE_CONFIDENCE_THRESHOLD='
        '"${PRIORITY_OBJECT_CAR_SINGLE_SOURCE_CONFIDENCE_THRESHOLD:-0.80}"' in launcher
    )
    assert (
        '--car-single-source-confidence-threshold '
        '"${PRIORITY_OBJECT_CAR_SINGLE_SOURCE_CONFIDENCE_THRESHOLD}"' in launcher
    )
    expected_label_thresholds = (
        'PRIORITY_OBJECT_LABEL_CONFIDENCE_THRESHOLDS='
        '"${PRIORITY_OBJECT_LABEL_CONFIDENCE_THRESHOLDS:-truck=0.80}"'
    )
    assert expected_label_thresholds in launcher
    assert (
        '--priority-label-confidence-thresholds '
        '"${PRIORITY_OBJECT_LABEL_CONFIDENCE_THRESHOLDS}"' in launcher
    )
    assert (
        '--priority-vehicle-stability-frames '
        '"${PRIORITY_OBJECT_VEHICLE_STABILITY_FRAMES}"' in launcher
    )
    assert '--priority-model-frame-stride "${PRIORITY_OBJECT_FRAME_STRIDE}"' in launcher
    assert '--priority-model-frame-phase "${PRIORITY_OBJECT_FRAME_PHASE}"' in launcher
    assert 'PRIORITY_OBJECT_FRAME_STRIDE="${PRIORITY_OBJECT_FRAME_STRIDE:-8}"' in launcher
    assert "PRIORITY_OBJECT_DETECTOR_ENABLED must be auto, 0 or 1" in launcher


def test_trt86_raw_common_object_contract_has_no_embedded_topk() -> None:
    descriptor = json.loads(
        (ROOT / "configs/models/ultralytics_yolo26n_coco80_trt86_raw.json").read_text(
            encoding="utf-8"
        )
    )

    assert descriptor["source_checkpoint_sha256"] == (
        "9b09cc8bf347f0fc8a5f7657480587f25db09b34bf33b0652110fb03a8ad4fef"
    )
    assert descriptor["artifact_sha256"] == (
        "0e54c190865025af36c8e71ba1e7966fb4e11d7e07df9e9a2d1b4ec0999c119f"
    )
    assert descriptor["output"]["shape"] == [1, 84, 8400]
    assert descriptor["output"]["embedded_nms"] is False
    assert descriptor["output"]["topk_nodes"] == 0
    assert descriptor["export"]["opset"] == 17
    assert descriptor["export"]["end2end"] is False


def test_jetson_launcher_uses_the_trt86_raw_common_object_paths() -> None:
    launcher = (ROOT / "scripts/run_jetson_fire_patrol.sh").read_text(encoding="utf-8")

    assert "models/coco-yolo26n-traditional/yolo26n-traditional.onnx" in launcher
    assert "yolo26n-traditional.b1.fp16.trt86.engine" in launcher
    assert "models/coco-yolo26n/coco-yolo26n.onnx" not in launcher


def test_semantic_context_engine_builder_is_hash_pinned_and_refuses_live_build() -> None:
    builder = (ROOT / "scripts/build_jetson_semantic_context_engine.sh").read_text(encoding="utf-8")

    assert "94ace62e250ed0a3122a46df8573950510b60a90c1b511e53c40dbca2bea21fb" in builder
    assert "pgrep -f 'multidetect live-camera'" in builder
    assert "Refusing a concurrent TensorRT engine build" in builder
    assert "--fp16" in builder
    assert "--skipInference" in builder
    assert "input:1x3x1024x1820" in builder
    assert 'expected_model_role="semantic_scene_context"' in builder
    assert 'expected_output_format="categorical_H_W_1"' in builder
    assert "multidetect.engine_provenance write" in builder
    assert "Flight control and physical release remain disabled" in builder
    assert "command_long" not in builder.lower()
    assert "mission_item" not in builder.lower()


def test_all_new_jetson_engines_write_target_bound_provenance() -> None:
    for name in (
        "build_jetson_common_detector_engine.sh",
        "build_jetson_reid_engine.sh",
        "build_jetson_vehicle_reid_engine.sh",
    ):
        builder = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "multidetect.engine_provenance write" in builder
        assert '--trtexec "${TRTEXEC}"' in builder
        assert "flight control remains disabled" in builder or "flight-control" in builder


def test_perception_engine_maintenance_requires_stopped_ground_window_and_gates_reid() -> None:
    script = (ROOT / "scripts/run_jetson_perception_engine_maintenance.sh").read_text(
        encoding="utf-8"
    )

    assert "recognition-stopped-ground-maintenance-only" in script
    assert "pgrep -f 'multidetect live-camera'" in script
    assert "flock -n 9" in script
    assert "build_jetson_common_detector_engine.sh" in script
    assert "build_jetson_reid_engine.sh" in script
    assert "build_jetson_vehicle_reid_engine.sh" in script
    assert "reid-tensorrt-bench" in script
    assert "--realtime-frame-budget-ms" in script
    assert "Recognition remains stopped" in script
    assert "does not stop or restart" in script
    assert "ALLOW_CONCURRENT_ENGINE_BUILD" not in script
    assert "systemctl" not in script
    assert "pkill" not in script
    assert "kill " not in script
    assert "command_long" not in script.lower()
    assert "actuator" in script.lower()
    assert "physical payload release remain disabled" in script


def test_jetson_launcher_rejects_engine_runtime_drift() -> None:
    launcher = (ROOT / "scripts/run_jetson_fire_patrol.sh").read_text(encoding="utf-8")
    assert launcher.count("multidetect.engine_provenance verify") == 6
    assert launcher.count('--provenance "${') == 6
