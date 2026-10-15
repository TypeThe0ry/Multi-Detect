"""CPU-only tests for the collector observation's simulation clock wiring."""

import io
from types import SimpleNamespace

import pytest
from PIL import Image

from multidetect.autonomy.mission_executor import MissionReport
from scripts.onboard_policy import OnboardPolicy


def _policy(tmp_path):
    received = []
    executor = SimpleNamespace(
        findings=[],
        current_tracks=(),
        current_step=None,
        current_task_text=None,
        history=[],
        counts={},
        observe=lambda *args, **kwargs: [],
        next_action=lambda **kwargs: (received.append(kwargs) or None, {}),
        report=MissionReport,
    )
    policy = OnboardPolicy.__new__(OnboardPolicy)
    policy._step = 0
    policy.work_dir = tmp_path
    policy.proposer = lambda frame: []
    policy.executor = executor
    policy.task_text = "Monitor the zone."
    policy.mission_plan_report = None
    return policy, received


def _observation(**timestamps):
    payload = io.BytesIO()
    Image.new("RGB", (2, 2)).save(payload, format="PNG")
    return {"rgb": payload.getvalue(), "camera_info": {}, **timestamps}


@pytest.mark.parametrize(
    ("timestamps", "expected"),
    [
        (
            {
                "pose_time_boot_ms": 12_345,
                "pose_sim_stamp_ns": 99_000_000_000,
                "frame_sensor_stamp_ns": 98_000_000_000,
                "pose_received_monotonic_ns": 9_999_000_000_000,
            },
            12.345,
        ),
        ({"pose_time_boot_ms": 0, "frame_sensor_stamp_ns": 3_000_000_000}, 0.0),
        ({"pose_time_boot_ms": None, "pose_sim_stamp_ns": 4_250_000_000}, 4.25),
        ({"frame_sensor_stamp_ns": 6_750_000_000}, 6.75),
        ({"pose_received_monotonic_ns": 9_999_000_000_000}, None),
        ({"pose_time_boot_ms": True, "frame_sensor_stamp_ns": 8_000_000_000}, 8.0),
        ({"pose_time_boot_ms": float("nan")}, None),
        ({"pose_time_boot_ms": -1}, None),
    ],
)
def test_act_passes_observation_simulation_seconds_to_executor(tmp_path, timestamps, expected):
    policy, received = _policy(tmp_path)

    policy.act(_observation(**timestamps), pose_ned=(0.0, 0.0, -20.0), yaw_deg=0.0)

    assert received[0]["timestamp_s"] == expected


def test_direct_mission_call_without_timestamp_keeps_estimate_fallback(tmp_path):
    policy, received = _policy(tmp_path)

    policy._act_mission(
        tmp_path / "frame.png",
        [],
        0.01,
        pose_ned=(0.0, 0.0, -20.0),
        yaw_deg=0.0,
        ground_point=None,
    )

    assert received[0]["timestamp_s"] is None
