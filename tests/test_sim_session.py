from pathlib import Path

import pytest

from scripts.run_sim_session import VIEWER_IMAGE, build_session_command, main, resolve_world

ROOT = Path(__file__).resolve().parents[1]


def test_session_plan_enables_browser_viewer() -> None:
    command, plan, world = build_session_command(container_name="test-sim-session", gcs_port=14560)
    assert world.name == "urban_inspection_multirotor_001.sdf"
    assert VIEWER_IMAGE in command
    assert "HEADLESS=1" in command
    assert "DISPLAY=:99" in command
    assert "127.0.0.1:6080:6080/tcp" in command
    assert any("Xvfb :99" in token for token in command)
    assert plan.container_name == "test-sim-session"
    assert "14550/udp" not in command


def test_session_plan_sends_gcs_to_host_without_publishing_port() -> None:
    command, _, _ = build_session_command(
        world="gen_cnt2_w0000.sdf",
        airframe="x500_mono_cam",
        gcs_port=14560,
        container_name="gcs-session",
    )
    assert "127.0.0.1:14560:14560/udp" not in command
    assert any("-t $DOCKER_HOST_IP -u 14542 -o 14560" in token for token in command)


def test_session_options_reject_invalid_snapshot_interval() -> None:
    with pytest.raises(SystemExit):
        main(["--snapshot-interval", "0"])


def test_world_must_be_repository_owned() -> None:
    with pytest.raises(ValueError, match="world not found"):
        resolve_world("missing-world.sdf")
