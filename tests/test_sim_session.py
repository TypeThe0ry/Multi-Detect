from pathlib import Path
import json

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


@pytest.mark.parametrize("held,should_acquire", [(False, True), (True, False)])
def test_session_honors_inherited_sitl_lock_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, held: bool, should_acquire: bool
) -> None:
    events: list[str] = []

    class FakeLock:
        def __init__(self, *_args, **_kwargs):
            pass

        def acquire(self, *_args, **_kwargs):
            events.append("acquire")

        def release(self):
            events.append("release")

    class Result:
        returncode = 0
        stdout = "true\n"

    monkeypatch.setattr(session, "SitlLock", FakeLock)
    monkeypatch.setattr(session, "validate_runtime_prerequisites", lambda **_: None)
    monkeypatch.setattr(session, "ensure_viewer_image", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        session,
        "build_session_command",
        lambda **_kwargs: (["docker", "run"], type("Plan", (), {"docker_executable": "docker", "container_name": "c"})(), ROOT / "simulation/gazebo/worlds/gen_cnt2_w0000.sdf"),
    )
    monkeypatch.setattr(session, "_wait_world_ready", lambda *_args: (True, True, ""))
    monkeypatch.setattr(session.subprocess, "run", lambda *_args, **_kwargs: Result())
    if held:
        monkeypatch.setenv("MULTIDETECT_SITL_LOCK_HELD", "1")
    else:
        monkeypatch.delenv("MULTIDETECT_SITL_LOCK_HELD", raising=False)
    try:
        assert main([
            "--onboard-task", "test", "--exit-after-task",
            "--onboard-out", str(tmp_path / "out"), "--artifacts-root", str(tmp_path / "art"),
            "--sitl-lock-path", str(tmp_path / "lock"),
        ]) == 0
    finally:
        monkeypatch.delenv("MULTIDETECT_SITL_LOCK_HELD", raising=False)
    assert ("acquire" in events) is should_acquire
    assert ("release" in events) is should_acquire


def test_world_must_be_repository_owned() -> None:
    with pytest.raises(ValueError, match="world not found"):
        resolve_world("missing-world.sdf")


@pytest.mark.parametrize(
    ("airframe", "world", "model"),
    [
        ("x500", "urban_inspection_multirotor_001.sdf", "px4_multirotor_standard"),
        ("standard_vtol", "river_rescue_vtol_001.sdf", "px4_vtol_standard"),
        ("quadtailsitter", "quadtailsitter_mono_cam_001.sdf", "quadtailsitter_mono_cam"),
        ("delta_tailsitter", "delta_tailsitter_mono_cam_001.sdf", "delta_tailsitter_mono_cam"),
        ("delta_tailsitter_mono_cam", "delta_tailsitter_mono_cam_001.sdf", "delta_tailsitter_mono_cam"),
        ("delta_tailsitter_gimbal", "delta_tailsitter_gimbal_cam_001.sdf", "delta_tailsitter_gimbal_cam"),
        ("delta_tailsitter_gimbal_cam", "delta_tailsitter_gimbal_cam_001.sdf", "delta_tailsitter_gimbal_cam"),
        ("tiltrotor", "tiltrotor_mono_cam_001.sdf", "tiltrotor_mono_cam"),
        ("rc_cessna", "coastal_patrol_fixed_wing_001.sdf", "px4_fixed_wing_standard"),
    ],
)
def test_session_plan_selects_airframe_runtime(airframe: str, world: str, model: str) -> None:
    _, plan, selected_world = build_session_command(
        airframe=airframe, container_name=f"test-{airframe}"
    )
    assert selected_world.name == world
    assert plan.scenario_id
    assert plan.vehicle_model == model


@pytest.mark.parametrize(
    ("airframe", "world", "scenario"),
    [
        ("standard_vtol", "gen_v2_w0031", "vtol-v2/standard_vtol/gen_v2_w0031"),
        ("standard_vtol", "gen_v2_w0016", "vtol-v2/standard_vtol/gen_v2_w0016"),
        ("quadtailsitter", "gen_v2_w0016", "vtol-v2/quadtailsitter/gen_v2_w0016"),
        ("delta_tailsitter", "gen_v2_w0016", "vtol-v2/delta_tailsitter/gen_v2_w0016"),
        ("delta_tailsitter_gimbal", "gen_v2_w0016", "vtol-v2/delta_tailsitter_gimbal/gen_v2_w0016"),
        ("rc_cessna", "gen_v2_w0031", "fixed-wing-v2/gen_v2_w0031"),
        ("rc_cessna_hand", "gen_v2_w0016", "fixed-wing-v2/gen_v2_w0016"),
        ("x500", "gen_v2_w0031", "generated-v2/gen_v2_w0031"),
    ],
)
def test_generated_world_uses_matching_airframe_scenario(
    airframe: str, world: str, scenario: str
) -> None:
    resolved = session._scenario_for(resolve_world(world), airframe)
    assert resolved == ROOT / "configs/scenarios" / f"{scenario}.scenario.json"
    _, plan, _ = build_session_command(
        airframe=airframe, world=world, container_name=f"test-generated-{airframe}"
    )
    assert plan.scenario_id == world


@pytest.mark.parametrize(
    ("airframe", "world"),
    [
        ("tiltrotor", "gen_v2_w0031"),  # no tiltrotor derivation of the generated world
        ("rc_cessna", "urban_inspection_multirotor_001.sdf"),
        ("x500", "river_rescue_vtol_001.sdf"),
    ],
)
def test_known_world_without_an_airframe_scenario_is_rejected(airframe: str, world: str) -> None:
    with pytest.raises(ValueError, match="has no scenario for airframe"):
        build_session_command(airframe=airframe, world=world, container_name="mismatch")


@pytest.mark.parametrize(
    ("airframe", "requested", "expected"),
    [
        ("x500", None, None),
        ("standard_vtol", None, "vtol"),
        ("quadtailsitter_mono_cam", None, "vtol"),
        ("delta_tailsitter_mono_cam", None, "vtol"),
        ("tiltrotor", None, "vtol"),
        ("rc_cessna", None, "runway"),
        ("rc_cessna", "catapult", "catapult"),
        ("rc_cessna_hand", None, "hand"),
        ("rc_cessna_catapult", None, "catapult"),
    ],
)
def test_takeoff_method_follows_the_airframe(
    airframe: str, requested: str | None, expected: str | None
) -> None:
    assert session.resolve_takeoff_method(airframe, requested) == expected


@pytest.mark.parametrize(
    ("airframe", "requested"),
    [
        ("x500", "runway"),
        ("standard_vtol", "runway"),
        ("delta_tailsitter", "runway"),
        ("rc_cessna", "vtol"),
        ("rc_cessna_hand", "catapult"),
    ],
)
def test_takeoff_method_rejects_other_airframe_families(airframe: str, requested: str) -> None:
    with pytest.raises(ValueError, match="does not apply"):
        session.resolve_takeoff_method(airframe, requested)
    with pytest.raises(SystemExit):
        main(["--airframe", airframe, "--takeoff-method", requested])


@pytest.mark.parametrize(
    ("airframe", "world", "canonical", "takeoff"),
    [
        ("x500", "urban_inspection_multirotor_001.sdf", "x500_mono_cam", None),
        ("standard_vtol", "gen_v2_w0016", "standard_vtol", "vtol"),
        ("quadtailsitter", "quadtailsitter_mono_cam_001.sdf", "quadtailsitter", "vtol"),
        ("delta_tailsitter", "delta_tailsitter_mono_cam_001.sdf", "delta_tailsitter", "vtol"),
        ("tiltrotor", "tiltrotor_mono_cam_001.sdf", "tiltrotor", "vtol"),
        ("rc_cessna", "gen_v2_w0031", "rc_cessna_mono_cam", "runway"),
        ("rc_cessna_hand", "coastal_patrol_fixed_wing_001.sdf", "rc_cessna_mono_cam", "hand"),
    ],
)
def test_onboard_collector_command_carries_airframe_and_takeoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    airframe: str,
    world: str,
    canonical: str,
    takeoff: str | None,
) -> None:
    captured: list[list[str]] = []

    def fake_run(command, **kwargs):
        captured.append(command)
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(session.subprocess, "run", fake_run)
    world_path = resolve_world(world)
    session._run_task(
        "find target",
        world_path,
        14540,
        tmp_path,
        "sim",
        operator_link={"operator_port": 14580},
        airframe=airframe,
        artifacts_root=tmp_path / "art",
    )
    command = captured[0]
    assert command[command.index("--airframe") + 1] == canonical
    assert Path(command[command.index("--scenario") + 1]) == session._scenario_for(
        world_path, airframe
    )
    if takeoff:
        assert command[command.index("--takeoff-method") + 1] == takeoff
    else:
        assert "--takeoff-method" not in command
    # The SITL battery simulator is airframe-agnostic: keep it charged for every
    # operator-link session, alongside the session disk guard and step budget.
    assert command[command.index("--sim-param") + 1] == "SIM_BAT_MIN_PCT=95"
    assert command[command.index("--min-free-gb") + 1] == "6"
    assert command[command.index("--steps-per-episode") + 1] == "220"


def test_variant_world_resolves_its_own_scenario() -> None:
    from scripts.run_sim_session import _scenario_for

    variants = ROOT / "simulation/gazebo/worlds/variants"
    scenario = _scenario_for(variants / "gen_cnt2_w0000__wind6_gust3p4.sdf", "x500_mono_cam")
    assert (
        scenario == ROOT / "configs/scenarios/variants/gen_cnt2_w0000__wind6_gust3p4.scenario.json"
    )
    # A variant without its own index entry falls back to the world it derives from.
    unindexed = _scenario_for(
        variants / "gen_cnt2_w0000__wind6_gust3p4_dusk_actors2_procedural.sdf", "x500_mono_cam"
    )
    assert unindexed == ROOT / "configs/scenarios/generated-count-site/gen_cnt2_w0000.scenario.json"


def test_generated_world_resolves_through_its_scenario_index() -> None:
    from scripts.run_sim_session import _scenario_for

    worlds = ROOT / "simulation/gazebo/worlds"
    assert (
        _scenario_for(worlds / "gen_v2_w0038.sdf", "x500_mono_cam")
        == ROOT / "configs/scenarios/generated-v2/gen_v2_w0038.scenario.json"
    )
    assert (
        _scenario_for(worlds / "gen_v2_w0031.sdf", "x500_mono_cam")
        == ROOT / "configs/scenarios/generated-v2/gen_v2_w0031.scenario.json"
    )
    assert (
        _scenario_for(worlds / "gen_v2_w0031.sdf", "rc_cessna")
        == ROOT / "configs/scenarios/fixed-wing-v2/gen_v2_w0031.scenario.json"
    )


@pytest.mark.parametrize("family", ["delta_tailsitter", "delta_tailsitter_gimbal"])
def test_delta_tailsitter_vtol_scenario_index_is_complete(family: str) -> None:
    index_path = ROOT / "configs/scenarios" / "vtol-v2" / family / "index.json"
    document = json.loads(index_path.read_text(encoding="utf-8"))
    worlds = document["worlds"]
    assert len(worlds) == 2
    assert {entry["split"] for entry in worlds} == {"train", "val"}
    assert {entry["world_id"] for entry in worlds} == {"gen_v2_w0016", "gen_v2_w0031"}
    for entry in worlds:
        scenario = index_path.parent / f"{entry['world_id']}.scenario.json"
        assert scenario.is_file()


def test_run_task_uses_session_ports_and_repo_defaults(monkeypatch, tmp_path: Path) -> None:
    import json
    import subprocess

    from scripts import run_sim_session as session

    calls: list[list[str]] = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_: calls.append(command) or subprocess.CompletedProcess(command, 0),
    )
    artifacts = tmp_path / "art"
    code = session._run_task(
        "Count cones",
        ROOT / "simulation/gazebo/worlds/gen_cnt2_w0000.sdf",
        14640,
        tmp_path / "out",
        "c",
        telemetry_port=14641,
        artifacts_root=artifacts,
    )
    assert code == 0
    command = calls[0]
    assert command[command.index("--control-port") + 1] == "14640"
    assert command[command.index("--telemetry-port") + 1] == "14641"
    # Frames at the camera's native calibration size, as the mission suite flies:
    # the collector's 640x480 default halved every box below the verdict floor.
    calibration = json.loads(
        (ROOT / "simulation/gazebo/calibration/gazebo-multirotor-front-camera-v1.json").read_text(
            encoding="utf-8"
        )
    )["calibration"]
    assert command[command.index("--rgb-size") + 1] == (
        f"{calibration['width_px']}x{calibration['height_px']}"
    )
    config = json.loads((tmp_path / "out/onboard-config.json").read_text(encoding="utf-8"))
    assert config["mission"] == "from_text"
    for key in ("planning_model", "yoloe_weights", "gguf"):
        assert Path(config[key]).is_relative_to(artifacts), (key, config[key])
    assert "_scratch" not in json.dumps(config)


def test_run_task_reads_explicit_onboard_defaults(monkeypatch, tmp_path: Path) -> None:
    import json
    import subprocess

    from scripts import run_sim_session as session

    monkeypatch.setattr(
        subprocess, "run", lambda command, **_: subprocess.CompletedProcess(command, 0)
    )
    defaults = tmp_path / "defaults.json"
    defaults.write_text(
        json.dumps(
            {
                "defaults": {
                    "mission": "from_text",
                    "yoloe_weights": "artifacts/w.pt",
                    "gguf": "/abs/x.gguf",
                }
            }
        ),
        encoding="utf-8",
    )
    session._run_task(
        "t",
        ROOT / "simulation/gazebo/worlds/gen_cnt2_w0000.sdf",
        14540,
        tmp_path / "out",
        "c",
        onboard_defaults=defaults,
        artifacts_root=tmp_path / "a",
    )
    config = json.loads((tmp_path / "out/onboard-config.json").read_text(encoding="utf-8"))
    assert Path(config["yoloe_weights"]) == tmp_path / "a" / "w.pt"
    assert config["gguf"] == "/abs/x.gguf"


def test_session_defaults_are_repository_owned() -> None:
    import json

    from scripts.run_sim_session import DEFAULT_ARTIFACTS_ROOT, DEFAULT_ONBOARD_DEFAULTS

    assert DEFAULT_ONBOARD_DEFAULTS.is_relative_to(ROOT / "configs/onboard")
    defaults = json.loads(DEFAULT_ONBOARD_DEFAULTS.read_text(encoding="utf-8"))["defaults"]
    assert all(not str(value).startswith(("D:", "/")) for value in defaults.values())
    assert DEFAULT_ARTIFACTS_ROOT == ROOT / "artifacts"


def test_heartbeat_details_releases_the_gcs_port(monkeypatch) -> None:
    from pymavlink import mavutil

    from scripts.run_sim_session import _heartbeat_details

    closed: list[bool] = []

    class _Message:
        autopilot = 12
        type = 2

        def get_srcSystem(self) -> int:
            return 1

        def get_srcComponent(self) -> int:
            return 1

    class _Connection:
        def wait_heartbeat(self, timeout: float):
            return _Message()

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(mavutil, "mavlink_connection", lambda *a, **k: _Connection())
    assert _heartbeat_details(14560, 1.0)["sysid"] == 1
    assert closed == [True]


@pytest.mark.usefixtures("require_loopback_sockets")
def test_heartbeat_details_actually_frees_the_udp_port() -> None:
    import socket

    from scripts.run_sim_session import _heartbeat_details

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert _heartbeat_details(port, 0.2) is None
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as gcs:
        gcs.bind(("0.0.0.0", port))


def test_gcs_rc_append_is_idempotent(tmp_path: Path) -> None:
    import subprocess

    from scripts.run_sim_session import _gcs_rc_append

    command, _, _ = build_session_command(container_name="gcs-rc", gcs_port=14560)
    snippet = _gcs_rc_append(14560)
    assert snippet in command[-1]
    rc = tmp_path / "px4-rc.mavlink"
    rc.write_text("mavlink start -x -u $udp_gcs_port_local\n", encoding="utf-8")
    script = f"set -eu; RC={rc.as_posix()}; {snippet}"
    for _ in range(2):  # a container restart re-runs the start-up command
        subprocess.run(["sh", "-c", script], check=True)
    text = rc.read_text(encoding="utf-8")
    assert text.count("-u 14542 -o 14560") == 1
    assert "-t $DOCKER_HOST_IP" in text
