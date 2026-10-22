from pathlib import Path

import pytest

import scripts.run_sim_session as session
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


@pytest.mark.parametrize(
    ("airframe", "world", "model"),
    [
        ("x500", "urban_inspection_multirotor_001.sdf", "px4_multirotor_standard"),
        ("standard_vtol", "river_rescue_vtol_001.sdf", "px4_vtol_standard"),
        ("quadtailsitter", "quadtailsitter_mono_cam_001.sdf", "quadtailsitter_mono_cam"),
        ("tiltrotor", "tiltrotor_mono_cam_001.sdf", "tiltrotor_mono_cam"),
        ("rc_cessna", "coastal_patrol_fixed_wing_001.sdf", "px4_fixed_wing_standard"),
    ],
)
def test_session_plan_selects_airframe_runtime(
    airframe: str, world: str, model: str
) -> None:
    _, plan, selected_world = build_session_command(
        airframe=airframe, container_name=f"test-{airframe}"
    )
    assert selected_world.name == world
    assert plan.scenario_id
    assert plan.vehicle_model == model


@pytest.mark.parametrize(
    ("airframe", "scenario_fragment"),
    [
        ("standard_vtol", "configs/scenarios/vtol-v2/standard_vtol/gen_v2_w0031.scenario.json"),
        ("quadtailsitter", "configs/scenarios/vtol-v2/quadtailsitter/gen_v2_w0016.scenario.json"),
        ("rc_cessna", "configs/scenarios/fixed-wing-v2/gen_v2_w0031.scenario.json"),
    ],
)
def test_generated_world_uses_matching_airframe_scenario(airframe: str, scenario_fragment: str) -> None:
    _, plan, _ = build_session_command(
        airframe=airframe,
        world=scenario_fragment.replace(".scenario.json", ".sdf").replace(
            "configs/scenarios/vtol-v2/standard_vtol/", "simulation/gazebo/worlds/"
        ).replace("configs/scenarios/vtol-v2/quadtailsitter/", "simulation/gazebo/worlds/").replace(
            "configs/scenarios/fixed-wing-v2/", "simulation/gazebo/worlds/"
        ),
        container_name=f"test-generated-{airframe}",
    )
    assert plan.scenario_id == "gen_v2_w0031" if "w0031" in scenario_fragment else plan.scenario_id == "gen_v2_w0016"


@pytest.mark.parametrize(
    ("airframe", "takeoff"),
    [("x500", None), ("standard_vtol", "vtol"), ("quadtailsitter", "vtol"),
     ("tiltrotor", "vtol"), ("rc_cessna", "runway")],
)
def test_onboard_collector_command_carries_airframe_and_takeoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, airframe: str, takeoff: str | None
) -> None:
    captured: list[list[str]] = []

    def fake_run(command, **kwargs):
        captured.append(command)
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(session.subprocess, "run", fake_run)
    world = resolve_world("urban_inspection_multirotor_001.sdf")
    session._run_task("find target", world, 14540, tmp_path, "sim", airframe)
    command = captured[0]
    assert command[command.index("--airframe") + 1] in {
        "x500_mono_cam", "standard_vtol", "quadtailsitter", "tiltrotor", "rc_cessna_mono_cam"
    }
    if takeoff:
        assert command[command.index("--takeoff-method") + 1] == takeoff
    else:
        assert "--takeoff-method" not in command
