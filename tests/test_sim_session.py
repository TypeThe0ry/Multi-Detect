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
