from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from scripts.polaris_sim.e2e import (
    GIMBAL_PITCH_TEST_TARGETS_DEG,
    Config,
    E2EFailure,
    PolarisE2E,
    assert_latency_increased,
    mean_absolute_frame_difference,
    parse_slots,
    takeoff_target_altitude_amsl_m,
    validate_ai_response,
    validate_discovery,
    validate_doctor,
    validate_event_order,
    validate_video_probe,
)


def _config(tmp_path: Path) -> Config:
    return Config(
        host="192.168.1.138",
        http_port=8780,
        mavlink_tcp_port=5770,
        mavlink_udp_port=14550,
        rtsp_port=8554,
        ai_port=8790,
        slots=(5, 6),
        world="rw_suburb_davis__clear_noon",
        airframe="x500_gimbal",
        steps=("doctor",),
        output_dir=tmp_path / "run",
        up_script=tmp_path / "up.sh",
        down_script=tmp_path / "down.sh",
        timeout_s=2.0,
        lifecycle_timeout_s=5.0,
        record_seconds=10.0,
        min_width=320,
        min_height=240,
        min_fps=1.0,
        max_video_startup_ms=15000.0,
        ai_query_file=None,
        ai_demo_script=tmp_path / "ai_client_demo.py",
        video_root=tmp_path / "video",
    )


def test_ai_response_accepts_spica_success_and_fail_closed_abstain() -> None:
    success = {
        "api_version": "1.0",
        "result": {
            "id": "q-1",
            "name": "query_target",
            "status": "ok",
            "latency_ms": 3,
            "data": {"schema_version": "1.0", "results": []},
        },
        "runtime": {"status": "ok"},
    }
    abstain = {
        "api_version": "1.0",
        "result": {"id": "q-2", "name": "query_target", "status": "unavailable", "data": None},
        "runtime": {"status": "disabled"},
    }

    assert validate_ai_response(success, "q-1")["status"] == "ok"
    assert validate_ai_response(abstain, "q-2")["status"] == "unavailable"


def test_ai_response_rejects_positive_envelope_without_spica_data() -> None:
    with pytest.raises(E2EFailure, match="schema_version"):
        validate_ai_response(
            {
                "result": {
                    "id": "q-1",
                    "name": "query_target",
                    "status": "ok",
                    "data": {"results": []},
                }
            },
            "q-1",
        )


def test_doctor_requires_all_reported_checks_to_pass() -> None:
    result = validate_doctor(
        {
            "status": "pass",
            "checks": [{"name": "gateway", "status": "PASS"}, {"name": "video", "ok": True}],
        }
    )
    assert len(result["checks"]) == 3
    # WARN is reported but does not fail the run; only FAIL/error does.
    validate_doctor({"checks": [{"name": "memory", "status": "WARN"}]})
    with pytest.raises(E2EFailure, match="FAIL"):
        validate_doctor({"checks": [{"name": "ai", "status": "FAIL"}]})


def test_sse_event_order_requires_full_ordered_pipeline() -> None:
    events = [
        {"event": name}
        for name in (
            "query.parsed",
            "vega.detected",
            "vlm.reviewed",
            "mizar.located",
            "tracking.updated",
            "published",
        )
    ]
    assert validate_event_order(events)[-1] == "published"
    with pytest.raises(E2EFailure, match="event order incomplete"):
        validate_event_order([{"event": "query.parsed"}, {"event": "published"}])


def test_video_probe_checks_h264_resolution_and_rate() -> None:
    document = {
        "streams": [
            {
                "codec_name": "h264",
                "width": 1280,
                "height": 720,
                "avg_frame_rate": "30/1",
                "r_frame_rate": "30/1",
            }
        ],
        "frames": [{"best_effort_timestamp_time": "0.0"}],
    }
    assert validate_video_probe(document, min_width=320, min_height=240, min_fps=1.0) == {
        "codec": "h264",
        "width": 1280,
        "height": 720,
        "fps": 30.0,
    }
    with pytest.raises(E2EFailure, match="H.264"):
        validate_video_probe(
            {
                "streams": [
                    {"codec_name": "hevc", "width": 1280, "height": 720, "avg_frame_rate": "30/1"}
                ]
            },
            min_width=320,
            min_height=240,
            min_fps=1.0,
        )


def test_gimbal_frame_difference_requires_equal_nonempty_frames() -> None:
    assert mean_absolute_frame_difference(b"\x00\x10", b"\x02\x18") == 5.0
    with pytest.raises(ValueError, match="equal lengths"):
        mean_absolute_frame_difference(b"\x00", b"\x00\x01")
    with pytest.raises(ValueError, match="non-empty"):
        mean_absolute_frame_difference(b"", b"")


def test_takeoff_target_uses_current_msl_altitude_plus_climb() -> None:
    assert takeoff_target_altitude_amsl_m(13.67) == pytest.approx(18.67)
    assert takeoff_target_altitude_amsl_m(125.0, 2.0) == pytest.approx(127.0)


def test_gimbal_motion_targets_include_a_textured_scene_angle() -> None:
    assert GIMBAL_PITCH_TEST_TARGETS_DEG == (-25.0, 0.0)


def test_gimbal_video_step_probes_only_real_gimbal_camera_and_checks_motion(tmp_path) -> None:
    runner = PolarisE2E(_config(tmp_path))
    probes = []
    motions = []
    runner._video_probe = lambda slot, camera: probes.append((slot, camera)) or {
        "slot": slot,
        "camera": camera,
    }
    runner._gimbal_video_motion = lambda slot, sysid: motions.append((slot, sysid)) or {
        "mean_abs_diff": 4.0
    }

    result = runner.video()

    assert probes == [(5, "gimbal"), (6, "gimbal")]
    assert motions == [(5, 1), (6, 2)]
    assert result == {
        "streams": [
            {"slot": 5, "camera": "gimbal"},
            {"slot": 6, "camera": "gimbal"},
        ],
        "gimbal_motion": [{"mean_abs_diff": 4.0}, {"mean_abs_diff": 4.0}],
    }


def test_discovery_document_exposes_expected_gateway_ports() -> None:
    document = {
        "service": "polaris-sim",
        "version": 1,
        "host": "192.168.1.138",
        "mavlink_tcp": 5770,
        "mavlink_udp": 14550,
        "http": 8780,
        "rtsp": 8554,
        "ai": 8790,
        "vehicles": [],
    }
    assert validate_discovery(
        document,
        http_port=8780,
        rtsp_port=8554,
        mavlink_tcp_port=5770,
        mavlink_udp_port=14550,
        ai_port=8790,
    )["service"] == "polaris-sim"
    with pytest.raises(E2EFailure, match="service contract"):
        validate_discovery(
            {**document, "http": 8000},
            http_port=8780,
            rtsp_port=8554,
            mavlink_tcp_port=5770,
            mavlink_udp_port=14550,
            ai_port=8790,
        )


def test_discovery_rejects_wrong_ai_and_mavlink_ports() -> None:
    document = {
        "service": "polaris-sim",
        "version": 1,
        "host": "192.168.1.138",
        "mavlink_tcp": 5770,
        "mavlink_udp": 14550,
        "http": 8780,
        "rtsp": 8554,
        "ai": 8791,
        "vehicles": [],
    }
    with pytest.raises(E2EFailure, match="service contract"):
        validate_discovery(
            document,
            http_port=8780,
            rtsp_port=8554,
            mavlink_tcp_port=5770,
            mavlink_udp_port=14550,
            ai_port=8790,
        )


def test_lifecycle_slots_must_be_contiguous_from_slot_five() -> None:
    assert parse_slots("5,6") == (5, 6)
    assert parse_slots("5,6,7") == (5, 6, 7)
    with pytest.raises(argparse.ArgumentTypeError, match="contiguous"):
        parse_slots("5,7")


def test_netem_latency_assertion_detects_change() -> None:
    assert_latency_increased(10.0, 120.0, 80.0)
    with pytest.raises(E2EFailure, match="did not measurably increase"):
        assert_latency_increased(10.0, 50.0, 80.0)


def test_cli_dry_run_writes_json_and_markdown_without_services(tmp_path: Path) -> None:
    runner = PolarisE2E(_config(tmp_path), dry_run=True)

    assert runner.execute() == 0
    report = json.loads((tmp_path / "run" / "report.json").read_text(encoding="utf-8"))
    markdown = (tmp_path / "run" / "report.md").read_text(encoding="utf-8")
    assert report["status"] == "DRY RUN"
    assert report["results"]["dry_run"]["details"]["slots"] == [5, 6]
    commands = report["results"]["dry_run"]["details"]["commands"]
    up = commands[0]
    assert "--vehicles" in up and "2" in up
    assert "--airframe" in up and "x500_gimbal" in up
    assert "--slot" not in up
    assert commands[-1] == ["bash", str(_config(tmp_path).down_script)]
    assert "未验证" in markdown
