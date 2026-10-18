"""Camera-model and report wiring checks; all perception backends are inert."""

from __future__ import annotations

import io
import json
import math
from types import SimpleNamespace

import pytest
from PIL import Image

from multidetect.autonomy.mission_executor import MissionExecutor, plan_for_find_task
from multidetect.autonomy.mission_plan import validate_mission_plan
from multidetect.autonomy.mission_summary import summarize_mission
from multidetect.autonomy.onboard_loop import Detection, OnboardLoop, Track
from multidetect.autonomy.onboard_mission_log import DownlinkQueue, OnboardMissionLog
from multidetect.autonomy.step_triangulation import PositionEstimate
from multidetect.onboard_runtime.camera import CameraCalibration
from multidetect.onboard_runtime.projection import GroundProjector
from multidetect.simulator.camera_projection import pixel_camera_ray, pixel_ground_point
from scripts.onboard_policy import OnboardPolicy

CAMERA = {"fx": 270.0, "fy": 280.0, "cx": 320.0, "cy": 240.0}


def policy(tmp_path, config, **kwargs):
    return OnboardPolicy(
        task_text="Find a car",
        world_path=None,
        altitude_m=20,
        camera_info=CAMERA,
        image_size=(640, 480),
        work_dir=tmp_path,
        config=config,
        proposer=lambda image: [],
        slow_runner=SimpleNamespace(close=lambda: None),
        **kwargs,
    )


def observation():
    image = io.BytesIO()
    Image.new("RGB", (640, 480)).save(image, format="PNG")
    return {
        "rgb": image.getvalue(),
        "camera_info": CAMERA,
        "pose": {"local_ned_m": [3, 4, -20]},
        "attitude": {"roll_deg": 3, "pitch_deg": -2, "yaw_deg": 20},
        "pose_sim_stamp_ns": 12_500_000_000,
    }


@pytest.mark.parametrize("mission", [None, "find"])
@pytest.mark.parametrize("enabled", [False, True])
def test_matching_camera_ray_and_sensor_timestamp_reach_all_loops(
    tmp_path,
    monkeypatch,
    mission,
    enabled,
):
    subject = policy(tmp_path, {"mission": mission, "step_triangulation": enabled})
    seen = []

    def capture(self, detections, **kwargs):
        seen.append(kwargs)
        return []

    monkeypatch.setattr(OnboardLoop, "update_tracks", capture)
    obs = observation()
    subject.act(obs, pose_ned=(3, 4, -20), yaw_deg=20)
    assert seen
    for kwargs in seen:
        assert ("camera_ray" in kwargs) is enabled
        assert ("timestamp_s" in kwargs) is enabled
        if enabled:
            assert kwargs["timestamp_s"] == 12.5
            expected = pixel_camera_ray(
                320,
                300,
                pose=obs,
                camera_info=CAMERA,
                image_size=(640, 480),
            )
            assert kwargs["camera_ray"](320, 300) == expected
    subject.close()


def test_custom_ground_model_requires_its_own_matching_ray(tmp_path, monkeypatch):
    sentinel = ((3, 4, -20), (0.6, 0, 0.8))

    class Projector:
        def __call__(self, u, v, obs):
            return (18, 4)

        def ray(self, u, v, obs):
            return sentinel

    for projector, expected in ((Projector(), sentinel), (lambda *args: (18, 4), None)):
        subject = policy(tmp_path, {"step_triangulation": True}, ground_projector=projector)
        seen = []

        def capture(self, detections, *, captured=seen, **kwargs):
            captured.append(kwargs["camera_ray"](320, 300))
            return []

        monkeypatch.setattr(OnboardLoop, "update_tracks", capture)
        subject.act(observation(), pose_ned=(3, 4, -20), yaw_deg=20)
        assert seen == [expected]
        subject.close()


@pytest.mark.parametrize(
    "key,value",
    [
        ("step_triangulation", "false"),
        ("step_triangulation", 1),
        ("step_triangulation_max_views", 3.5),
        ("step_triangulation_max_views", True),
        ("step_triangulation_max_views", "8"),
        ("step_triangulation_max_views", 1),
        ("ground_height_sigma_m", True),
        ("ground_height_sigma_m", "1"),
        ("ground_height_sigma_m", -1),
        ("ground_height_sigma_m", math.nan),
        ("step_triangulation_sigma_scale", 0.5),
        ("step_triangulation_sigma_scale", math.inf),
        ("step_triangulation_ground_gate_m", -1),
        ("step_triangulation_ground_gate_m", math.nan),
        ("step_triangulation_ground_gate_sigma", -1),
        ("step_triangulation_ground_gate_sigma", True),
        ("step_triangulation_parallax_deg_per_m", 0),
        ("step_triangulation_parallax_deg_per_m", math.inf),
        ("step_triangulation_min_ray_angle_deg", -1),
        ("step_triangulation_min_ray_angle_deg", math.nan),
    ],
)
def test_policy_passes_config_types_to_strict_validation(tmp_path, key, value):
    with pytest.raises(ValueError, match=key):
        policy(tmp_path, {key: value})


def test_simulator_unit_ray_uses_same_mount_attitude_and_intrinsics_as_ground():
    obs = observation()
    centre, ray = pixel_camera_ray(
        350,
        400,
        pose=obs,
        camera_info=CAMERA,
        image_size=(640, 480),
    )
    assert math.sqrt(sum(value * value for value in ray)) == pytest.approx(1)
    scale = -centre[2] / ray[2]
    expected = (centre[0] + scale * ray[0], centre[1] + scale * ray[1])
    assert pixel_ground_point(
        350,
        400,
        pose=obs,
        camera_info=CAMERA,
        image_size=(640, 480),
    ) == pytest.approx(expected, abs=1e-12)


def test_real_unit_ray_uses_distortion_extrinsics_and_camera_lever_arm():
    calibration = CameraCalibration(
        640,
        480,
        270,
        280,
        320,
        240,
        distortion=(0.01, -0.02, 0.001, 0.002, 0),
        extrinsics={
            "mount_pitch_down_deg": 45,
            "mount_yaw_right_deg": 10,
            "mount_roll_clockwise_deg": 2,
            "camera_offset_forward_m": 0.3,
            "camera_offset_right_m": 0.1,
            "camera_offset_down_m": 0.2,
        },
    )
    projector = GroundProjector(calibration, ground_down_m=1)
    centre, ray = projector.ray(350, 300, observation())
    assert math.sqrt(sum(value * value for value in ray)) == pytest.approx(1)
    assert centre != (3, 4, -20)
    scale = (1 - centre[2]) / ray[2]
    assert projector(350, 300, observation()) == (
        centre[0] + scale * ray[0],
        centre[1] + scale * ray[1],
    )


def test_finding_summary_keeps_optional_uncertainty_without_changing_legacy(tmp_path):
    subject = policy(tmp_path, {"mission": "find", "step_triangulation": True})
    executor = subject.executor
    record = {
        "track_id": 1,
        "bbox": (1, 2, 3, 4),
        "rule": "confirmed",
        "position_estimate_ne": (12.3, 45.6),
        "position_source": "fused",
        "position_sigma_m": 2.4,
        "position_view_count": 3,
        "position_baseline_m": 2.0,
        "position_ray_angle_deg": 1.5,
        "position_consistent": True,
        "position_consistency_note": None,
    }
    executor._add_finding(executor._run, record, "frame.png")
    finding = executor.findings[-1]
    metadata = {
        key: value
        for key, value in record.items()
        if key.startswith("position_") and key != "position_estimate_ne"
    }
    assert all(finding[key] == value for key, value in metadata.items())
    summary, _, _ = summarize_mission(
        {"findings": executor.findings},
        plan_for_find_task("car"),
        "Find a car",
    )
    assert all(summary["findings"][0][key] == value for key, value in metadata.items())
    assert executor.report().positions == ("12.3,45.6",)
    legacy, _, _ = summarize_mission(
        {"findings": [{"position_ne": (12.3, 45.6)}]},
        plan_for_find_task("car"),
        "Find a car",
    )
    assert set(legacy["findings"][0]) == {"step", "query", "position_ne", "frame_id"}
    subject.close()


def test_multistep_find_position_drives_inspect_summary_and_persisted_downlink(tmp_path):
    subject = policy(
        tmp_path,
        {
            "mission": "find_and_inspect",
            "step_triangulation": True,
            "ground_height_sigma_m": 100.0,
        },
    )
    subject._slow_identity = lambda *args: {"decision": "confirm_target", "confidence": 0.99}
    detection = Detection((290, 350, 350, 410), 0.9)
    for index, east in enumerate((0.0, 10.0)):
        camera = (0.0, east, -20.0)
        vector = (30.0, -east, 20.0)
        norm = math.hypot(*vector)
        ray = tuple(value / norm for value in vector)
        _, labels = subject._act_mission(
            tmp_path / f"frame-{index}.png",
            [detection],
            0,
            pose_ned=camera,
            yaw_deg=0,
            ground_point=lambda u, v: (35.0, 0.0),
            camera_ray=lambda u, v, c=camera, r=ray: (c, r),
            timestamp_s=float(index),
        )
    executor = subject.executor
    assert executor.current_step.skill == "inspect"
    finding = executor.findings[0]
    position = finding["position_ne"]
    assert position[0] == pytest.approx(30, abs=0.1)
    assert position != (35.0, 0.0)
    assert finding["position_source"] == "fused"
    assert finding["position_view_count"] == 2
    assert labels["onboard"]["confirmed"]["position_sigma_m"] == finding["position_sigma_m"]
    executor._plan_views(executor._run, (0, 2, -20))
    assert executor._run.centre_ne == position
    for viewpoint in executor._run.viewpoints:
        assert math.dist(viewpoint, position) == pytest.approx(
            executor.current_step.params["standoff_m"],
        )
    subject._write_mission_summary()
    saved = json.loads((tmp_path / "mission_summary.json").read_text(encoding="utf-8"))
    assert saved["findings"][0]["position_ne"] == pytest.approx(position, abs=0.05)
    assert saved["findings"][0]["position_view_count"] == 2
    # Supervisor execution and final-summary events enqueue this structured
    # finding verbatim. Read the actual durable outbox without sending data.
    log = OnboardMissionLog(tmp_path / "outbox", "triangulation-test")
    log.append("execution", 2.0, findings=executor.findings)
    queue = DownlinkQueue(log, sender=None)
    try:
        pending, _ = queue._next_record()
        assert pending["findings"][0]["position_ne"] == list(position)
        assert pending["findings"][0]["position_sigma_m"] == finding["position_sigma_m"]
    finally:
        queue.close()
        subject.close()


@pytest.mark.parametrize("enabled", [False, True])
def test_alert_report_position_does_not_shift_alert_association_anchor(tmp_path, enabled):
    subject = policy(tmp_path, {"mission": "find", "step_triangulation": enabled})
    executor = subject.executor
    track = Track("target", (290, 350, 350, 410), 0, 1, ground=(35, 0))
    track.position_estimate = PositionEstimate((30, 1, 0), 2, 1, "fused", view_count=2)
    anchor = (34, 0)
    executor._emit_alert(executor._run, track, anchor)
    finding = executor.findings[-1]
    assert executor._run.alert_observations[-1]["position"] == anchor
    assert executor._run.alert_positions[-1] == anchor
    assert finding["position_ne"] == ((30, 1) if enabled else anchor)
    assert ("position_source" in finding) is enabled
    subject.close()


def test_follow_ignores_static_estimate_inherited_from_completed_search(tmp_path):
    subject = policy(tmp_path, {"mission": "find", "step_triangulation": True})
    plan = validate_mission_plan(
        {
            "plan_version": 1,
            "goal": "find and follow car",
            "report": ["positions"],
            "steps": [
                {
                    "id": "search",
                    "skill": "area_search",
                    "params": {"query": "car", "region": "zone:all"},
                    "on_success": "follow",
                    "on_fail": "home",
                },
                {
                    "id": "follow",
                    "skill": "follow",
                    "params": {"query": "car", "track_ref": "track:1"},
                    "on_success": "home",
                    "on_fail": "home",
                },
                {
                    "id": "home",
                    "skill": "return_home",
                    "params": {},
                    "on_success": None,
                    "on_fail": None,
                },
            ],
        }
    )
    executor = MissionExecutor(plan, loop_factory=subject.executor.loop_factory)
    old_loop = executor.active_loop
    track = Track("target", (290, 350, 350, 410), 0, 0, ground=(35, 0))
    track.position_estimate = PositionEstimate((30, 1, 0), 2, 1, "fused", view_count=2)
    old_loop.tracks[track.track_id] = track
    executor._add_finding(
        executor._run,
        {
            "track_id": track.track_id,
            "position_estimate_ne": (30, 1),
            "bbox": track.bbox,
            **old_loop.position_metadata(track),
        },
        None,
    )
    executor._finish("succeeded", "confirmed")
    assert executor.current_step.skill == "follow"
    assert executor.active_loop is old_loop
    assert not old_loop.config.step_triangulation
    assert old_loop.target_position(track) == track.ground
    executor.observe(
        [Detection(track.bbox, 0.9)],
        pose_ned=(0, 0, -20),
        yaw_deg=0,
        ground_point=lambda u, v: (35, 0),
        timestamp_s=2,
        camera_ray=lambda u, v: pytest.fail("moving target must not accumulate static rays"),
    )
    assert old_loop.target_position(track) == track.ground
    subject.close()


def test_step_triangulation_is_on_by_default_with_an_honest_ground_prior(tmp_path):
    """Default since 2026-10-18 (closed loop, ground believed 2 m high: median
    position error 3.8 -> 2.0 m); explicit config still turns it off."""
    default = policy(tmp_path / "default", {})
    assert default.loop.config.step_triangulation is True
    assert default.loop.config.ground_height_sigma_m == 5.0
    default.close()
    off = policy(tmp_path / "off", {"step_triangulation": False, "ground_height_sigma_m": 1.0})
    assert off.loop.config.step_triangulation is False
    assert off.loop.config.ground_height_sigma_m == 1.0
    off.close()
