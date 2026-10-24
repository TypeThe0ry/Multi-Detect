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
