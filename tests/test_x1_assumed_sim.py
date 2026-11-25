"""Isolation and reproducibility of the Iowa-only assumed bounds loader."""
import ast
import json
from types import SimpleNamespace

import pytest

from multidetect.autonomy.mission_package import load_mission_package
from scripts import generate_x1_assumed_sim_bounds as generator
from scripts import run_x1_autonomy_demo as demo
from test_x1_planner import _fixture

PATH = demo.ROOT / "configs/simulation/x1-assumed-sim-bounds-iowa.json"


def test_committed_assumptions_reproduce():
    generated = generator.generate(mode="sim", world=generator.WORLD, image=generator.IMAGE, profile_path=demo.PROFILE)
    assert PATH.read_text(encoding="utf-8") == json.dumps(generated, ensure_ascii=False, indent=2) + "\n"
    assert generated["dem_error_m"]["value"] >= 10
    assert generated["battery_capacity_wh"]["value"] == 72
    for entry in [generated[key] for key in ("dem_error_m", "landing_pad", "battery", "battery_capacity_wh")] + list(
            generated["suffixes"].values()):
        assert entry["basis"] == "ASSUMED_SIM" and len(entry["justification"].splitlines()) == 1


@pytest.mark.parametrize(("mode", "world", "image"), [
    (None, generator.WORLD, generator.IMAGE),
    ("flight", generator.WORLD, generator.IMAGE),
    ("real", generator.WORLD, generator.IMAGE),
    ("hil", generator.WORLD, generator.IMAGE),
    ("sim", "other_world", generator.IMAGE),
    ("sim", generator.WORLD, "vehicle-image"),
])
def test_generator_and_runner_reject_other_contexts(mode, world, image):
    with pytest.raises(ValueError, match="explicit sim mode"):
        generator.generate(mode=mode, world=world, image=image, profile_path=demo.PROFILE)
    with pytest.raises(ValueError, match="explicit sim mode"):
        demo.load_assumed_sim(PATH, package=SimpleNamespace(mode=mode), site=SimpleNamespace(scenario_id=world),
                              collector_plan=SimpleNamespace(image=image), faults=(), site_bounds=None,
                              scope=None, base_digest=None)


def test_vehicle_and_generic_package_loader_refuse_assumed_package():
    with pytest.raises(ValueError):
        load_mission_package(PATH, mode="flight", flight_mode=True, allow_assumed_sim=True)
    package = _fixture("Fly over the yard, return home and land", target=(320, 0))
    doc = {**package.document, "x1_live_evidence": {"basis": generator.BASIS}}
    with pytest.raises(ValueError, match="authorized SITL scenario runner"):
        load_mission_package(doc, mode="sim")
    with pytest.raises(ValueError):
        load_mission_package(doc, mode="flight", flight_mode=True, allow_assumed_sim=True)
    with pytest.raises(ValueError, match="unknown field"):
        from multidetect.sim_teacher.x1_live_evidence_builder import build_live_evidence

        bound = json.loads(PATH.read_text(encoding="utf-8"))
        build_live_evidence(package, terrain=SimpleNamespace(source_sha256=None), scope_sha256=None,
                            base_mission_sha256=None, surveyed_bounds=bound)


def test_production_import_and_cli_opt_in_are_confined_to_the_scenario_runner():
    # Scan project-owned Python entry points, including the vehicle code under src.
    references = {"generate_x1_assumed_sim_bounds": set(), "allow_assumed_sim": set(), "--assumed-sim-bounds": set()}
    for directory in ("scripts", "src", "deploy"):
        for path in (demo.ROOT / directory).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if not any(word in source for word in references):
                continue
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, (ast.Import, ast.ImportFrom)) and (
                    any(alias.name.endswith("generate_x1_assumed_sim_bounds") for alias in node.names)
                    or (getattr(node, "module", None) or "").endswith("generate_x1_assumed_sim_bounds")
                ):
                    references["generate_x1_assumed_sim_bounds"].add(path)
                if isinstance(node, ast.keyword) and node.arg == "allow_assumed_sim":
                    references["allow_assumed_sim"].add(path)
                if isinstance(node, ast.Constant) and node.value == "--assumed-sim-bounds":
                    references["--assumed-sim-bounds"].add(path)
    assert all(paths == {demo.ROOT / "scripts/run_x1_autonomy_demo.py"} for paths in references.values())
