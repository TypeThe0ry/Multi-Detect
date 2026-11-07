import json
from pathlib import Path

import pytest

from scripts.collect_sim_teacher_episodes import CollectionPlan, docker_command


@pytest.mark.parametrize("directory", ["generated-r20", "generated-r21id"])
def test_generated_worlds_build_collector_stack_plan_without_docker(directory):
    root = Path(__file__).resolve().parents[1]
    scenario_dir = root / "configs" / "scenarios" / directory
    scenarios = sorted(scenario_dir.glob("*.scenario.json"))
    assert scenarios
    for scenario in scenarios:
        document = json.loads(scenario.read_text(encoding="utf-8"))
        runtime = scenario_dir / document["channels"]["runtime_input"]["path"]
        truth = scenario_dir / document["channels"]["truth"]["path"]
        runtime_doc = json.loads(runtime.read_text(encoding="utf-8"))
        vehicle = runtime_doc["vehicle"]
        fixed_wing = "fixed_wing" in vehicle["model"]
        airframe = "rc_cessna_runway" if fixed_wing else "x500_mono_cam"
        plan = CollectionPlan(
            str(scenario), str(runtime), str(truth), "expert_search", 1, 120, 20261016,
            str(root / "_scratch" / "stack-preflight"), airframe=airframe,
            takeoff_method="runway" if fixed_wing else None,
        )
        command = docker_command(plan, name=f"offline-{scenario.stem}")
        assert command and command[0] == "docker"


def test_collector_host_network_plan_uses_worker_instance_and_partition(tmp_path):
    root = Path(__file__).resolve().parents[1]
    scenario = root / "configs/scenarios/river_rescue_vtol_001.scenario.json"
    document = json.loads(scenario.read_text(encoding="utf-8"))
    runtime = (scenario.parent / document["channels"]["runtime_input"]["path"]).resolve()
    truth = (scenario.parent / document["channels"]["truth"]["path"]).resolve()
    command = docker_command(
        CollectionPlan(
            str(scenario), str(runtime), str(truth), "expert_search", 1, 1, 0,
            str(tmp_path), sim_network="host", sim_instance=3,
        ),
        name="r15-worker-3-episode-0",
    )
    assert "--network" in command and command[command.index("--network") + 1] == "host"
    assert "--publish" not in command and "--add-host" not in command
    env = {
        command[index + 1].split("=", 1)[0]: command[index + 1].split("=", 1)[1]
        for index, token in enumerate(command)
        if token == "--env"
    }
    assert env["GZ_PARTITION"] == "r15-worker-3-episode-0"
    assert "px4 -i 3" in command[-1]
