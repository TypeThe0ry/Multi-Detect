from pathlib import Path

import pytest

from scripts.run_sim_session import build_session_command, resolve_world

ROOT = Path(__file__).resolve().parents[1]


def test_session_plan_enables_gui_and_wslg_mounts() -> None:
    command, plan, world = build_session_command(container_name="test-sim-session")
    assert world.name == "urban_inspection_multirotor_001.sdf"
    assert "HEADLESS=0" in command
    assert "DISPLAY=:0" in command
    assert any("target=/tmp/.X11-unix" in token for token in command)
    assert any("target=/mnt/wslg" in token for token in command)
    assert plan.container_name == "test-sim-session"
    assert "14550/udp" not in command


def test_session_plan_can_publish_gcs_port() -> None:
    command, _, _ = build_session_command(
        world="gen_cnt2_w0000.sdf",
        airframe="x500_mono_cam",
        gcs_port=14550,
        container_name="gcs-session",
    )
    assert "127.0.0.1:14550:14550/udp" in command
    assert any("mavlink start -x -u 14550" in token for token in command)


def test_world_must_be_repository_owned() -> None:
    with pytest.raises(ValueError, match="world not found"):
        resolve_world("missing-world.sdf")
