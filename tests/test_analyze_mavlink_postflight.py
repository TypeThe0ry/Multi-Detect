import json
from pathlib import Path

from scripts.analyze_mavlink_postflight import analyze_episode, main


def _row(kind, stamp, **values):
    return {"message_type": kind, "receive_monotonic_ns": stamp, **values}


def _episode(path: Path, shift=0):
    path.mkdir(parents=True)
    rows = [
        _row("HEARTBEAT", shift + 1_000_000_000, base_mode=1, custom_mode=50593792),
        _row("EXTENDED_SYS_STATE", shift + 1_100_000_000, landed_state=1),
        _row("HEARTBEAT", shift + 2_000_000_000, base_mode=129, custom_mode=50593792),
        _row("EXTENDED_SYS_STATE", shift + 2_100_000_000, landed_state=2),
        _row("ATTITUDE", shift + 2_200_000_000, roll=0.1, pitch=-0.2, yaw=0.3),
        _row("LOCAL_POSITION_NED", shift + 2_300_000_000, x=2.0, y=3.0, z=-5.0),
        _row("POSITION_TARGET_LOCAL_NED", shift + 2_310_000_000, type_mask=0, x=1.0, y=1.0, z=-4.0),
        _row("STATUSTEXT", shift + 2_400_000_000, severity=4, text="test warning"),
        _row("GIMBAL_DEVICE_ATTITUDE_STATUS", shift + 2_500_000_000, q=[1, 0, 0, 0]),
        _row("GIMBAL_DEVICE_SET_ATTITUDE", shift + 2_600_000_000, q=[1, 0, 0, 0]),
        _row("SERVO_OUTPUT_RAW", shift + 2_700_000_000, servo1_raw=1200),
        _row("HEARTBEAT", shift + 5_500_000_000, base_mode=1, custom_mode=393216),
        _row("EXTENDED_SYS_STATE", shift + 5_600_000_000, landed_state=1),
    ]
    (path / "mavlink.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    (path / "steps.jsonl").write_text(json.dumps({"step_index": 0}) + "\n")


def test_analyze_episode_reports_mavlink_postflight_facts(tmp_path):
    episode = tmp_path / "episode-1"
    _episode(episode)

    report = analyze_episode(episode)

    assert report["timeline"]["armed_at_s"] == 1.0
    assert report["timeline"]["airborne_at_s"] == 1.1
    assert report["timeline"]["landed_at_s"] == 4.6
    assert [change["mode"] for change in report["timeline"]["flight_mode_changes"]] == ["AUTO.LOITER", "OFFBOARD"]
    assert report["altitude_m"] == {"min": 5.0, "max": 5.0}
    assert report["setpoint_comparison"]["position_error_m"]["samples"] == 1
    assert report["setpoint_comparison"]["position_error_m"]["max_abs"] == 2.449
    assert report["attitude_extremes_deg"]["roll_deg"]["max"] == 5.7296
    assert report["timeline"]["status_text"][0]["severity_name"] == "WARNING"
    assert report["gimbal"]["finite_quaternion_commands"] == 1
    assert report["servo_output_raw"]["channel_extremes_raw"]["servo1_raw"]["max"] == 1200


def test_campaign_text_and_json_output(tmp_path, capsys):
    _episode(tmp_path / "campaign" / "episode-a")
    _episode(tmp_path / "campaign" / "episode-b", shift=10_000_000_000)
    campaign = tmp_path / "campaign"

    assert main([str(campaign)]) == 0
    assert "ULog-only data absent here" in capsys.readouterr().out
    assert main([str(campaign), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 2
    assert all("timeline" in episode for episode in payload)
