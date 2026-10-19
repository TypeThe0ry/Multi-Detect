"""A generated (x500) world re-targeted at a runway fixed-wing."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from jsonschema import validate

from multidetect.autonomy.serialization import canonical_sha256
from multidetect.simulator.stack import _vehicle_spec as _stack_vehicle_spec
from multidetect.simulator.world_gen import (
    derive_fixed_wing_scenario,
    generate_worlds,
    runway_corridor_clearance,
)

ROOT = Path(__file__).resolve().parents[1]


def _world(tmp_path: Path) -> tuple[Path, Path]:
    worlds = tmp_path / "simulation/gazebo/worlds"
    scenarios = tmp_path / "configs/scenarios/generated"
    generate_worlds(2, 20261010, out_worlds=worlds, out_scenarios=scenarios)
    return worlds, scenarios / "gen_w0000.scenario.json"


def test_corridor_clearance_measures_the_whole_line() -> None:
    # An object 30 m north of home, radius 5: an east-west runway clears it by
    # 25 m, a north-south one runs straight through it (either direction).
    footprints = [(30.0, 0.0, 5.0)]
    assert runway_corridor_clearance(footprints, 90.0) == pytest.approx(25.0)
    assert runway_corridor_clearance(footprints, 0.0) == pytest.approx(-5.0)
    assert runway_corridor_clearance(footprints, 180.0) == pytest.approx(-5.0)


def test_derived_runtime_spawns_a_fixed_wing_along_the_clearest_runway(tmp_path: Path) -> None:
    worlds, scenario = _world(tmp_path)
    out = tmp_path / "configs/scenarios/fw"
    entry = derive_fixed_wing_scenario(
        scenario, out, worlds_dir=worlds, corridor_half_width_m=0.0
    )
    world_id = "gen_w0000"
    runtime = json.loads((out / f"{world_id}.runtime_input.json").read_text(encoding="utf-8"))
    schema = json.loads(
        (ROOT / "configs/schemas/scenario-runtime-input.schema.json").read_text(encoding="utf-8")
    )
    validate(runtime, schema)
    vehicle = runtime["vehicle"]
    assert _stack_vehicle_spec(vehicle["model"], vehicle["airframe_profile_id"]) == (
        "fixed-wing",
        "rc_cessna_mono_cam",
    )
    heading = entry["runway_heading_deg"]
    # ENU yaw from east == 90 deg - NED heading from north.
    yaw = vehicle["spawn_pose"]["yaw_rad"]
    assert math.cos(yaw) == pytest.approx(math.sin(math.radians(heading)), abs=1e-5)
    assert math.sin(yaw) == pytest.approx(math.cos(math.radians(heading)), abs=1e-5)
    assert {s["calibration_id"] for s in runtime["sensors"] if "calibration_id" in s} == {
        "gazebo-fixed-wing-front-camera-v1",
        "gazebo-fixed-wing-depth-v1",
        "gazebo-fixed-wing-airspeed-v1",
    }
    # The world and truth stay the generated ones; the manifest hashes the new runtime.
    source_truth = scenario.parent / f"{world_id}.truth.json"
    assert (out / f"{world_id}.truth.json").read_text(encoding="utf-8") == source_truth.read_text(
        encoding="utf-8"
    )
    manifest = json.loads((out / f"{world_id}.scenario.json").read_text(encoding="utf-8"))
    assert manifest["channels"]["runtime_input"]["sha256"] == canonical_sha256(runtime)
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))
    (row,) = index["worlds"]
    assert row["world_id"] == world_id and row["task_text"] and row["runway_heading_deg"] == heading


def test_chosen_heading_is_the_clearest_and_a_blocked_runway_is_refused(tmp_path: Path) -> None:
    worlds, scenario = _world(tmp_path)
    out = tmp_path / "fw"
    best = derive_fixed_wing_scenario(scenario, out, worlds_dir=worlds, corridor_half_width_m=0.0)
    for heading in range(0, 180, 5):
        forced = derive_fixed_wing_scenario(
            scenario,
            out,
            worlds_dir=worlds,
            runway_heading_deg=heading,
            corridor_half_width_m=-1.0e9,
        )
        assert forced["runway_clearance_m"] <= best["runway_clearance_m"]
    with pytest.raises(ValueError, match="clears objects by"):
        derive_fixed_wing_scenario(
            scenario,
            out,
            worlds_dir=worlds,
            corridor_half_width_m=best["runway_clearance_m"] + 1.0,
        )
