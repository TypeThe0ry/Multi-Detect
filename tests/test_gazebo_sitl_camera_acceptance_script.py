from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_gazebo_sitl_camera_acceptance.ps1"
CAPTURE = ROOT / "scripts" / "gazebo_camera_capture.py"


def test_gazebo_camera_acceptance_is_digest_pinned_and_headless() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert (
        "px4io/px4-sitl-gazebo@sha256:"
        "bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8" in text
    )
    assert '"-e", "HEADLESS=1"' in text
    assert '"-e", "PX4_SIM_MODEL=gz_x500_mono_cam"' in text
    assert '"-e", "PX4_GZ_WORLD=default"' in text
    assert '"--rm", "-d", "--name", $ContainerName' in text
    assert '"--label", "multidetect.purpose=gazebo-sitl-camera-acceptance"' in text


def test_gazebo_camera_acceptance_keeps_hardware_and_qgc_boundaries_explicit() -> None:
    text = SCRIPT.read_text(encoding="utf-8").upper()
    assert "REAL_CAMERA_CONTACTED = $FALSE" in text
    assert "REAL_PIXHAWK_CONTACTED = $FALSE" in text
    assert "HARDWARE_CONTROL_ENABLED = $FALSE" in text
    assert 'OPERATOR_VISUAL_ACCEPTANCE = "NOT_CLAIMED"' in text
    assert "COUNTED_AS_QGC_E4_GATE = $FALSE" in text
    assert '"STOP", "--TIME", "2", $CONTAINERNAME' in text
    assert "DOCKER RM -F" not in text


def test_capture_script_uses_real_gazebo_topics_and_never_ground_truth() -> None:
    text = CAPTURE.read_text(encoding="utf-8")
    assert "gz.transport13" in text
    assert "gz.msgs10.image_pb2" in text
    assert "/sensor/camera/image" in text
    assert "/sensor/navsat_sensor/navsat" in text
    assert "odometry_with_covariance" in text
    assert "odometry_with_covariance_pb2" in text
    assert "ground_truth" not in text.lower()
    assert '"schema": "multidetect.gazebo-camera-capture"' in text
