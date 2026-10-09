"""Audit actual repository static geometry and wind; never qualify flight."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from multidetect.autonomy.onboard_executed_trajectory import EvidenceBinding  # noqa: E402
from multidetect.autonomy.sim_static_route_clearance import (  # noqa: E402
    inspect_static_gazebo_scene,
)
from multidetect.autonomy.sim_world_inputs import qualify_configured_sim_world_wind  # noqa: E402
from scripts.run_onboard_teacher_gazebo_observation_v2 import _json_new  # noqa: E402


def _file(relative):
    path = ROOT / relative
    before = path.stat()
    raw = path.read_bytes()
    after = path.stat()
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or len(raw) != after.st_size
        or not 0 < len(raw) <= 1024 * 1024
    ):
        raise ValueError("bounded stable static source required")
    return EvidenceBinding(relative, len(raw), hashlib.sha256(raw).hexdigest(), relative, None)


def inspect_static_inputs(output_directory):
    output = Path(output_directory).resolve()
    if not output.is_relative_to(ROOT / "artifacts/diagnostics") or output.exists():
        raise ValueError("fresh contained audit directory required")
    world = _file("simulation/gazebo/worlds/urban_inspection_multirotor_001.sdf")
    runtime = _file("configs/scenarios/urban_inspection_multirotor_001.runtime.json")
    target = _file("simulation/gazebo/models/multidetect_search_target_v1/model.sdf")
    wind = qualify_configured_sim_world_wind(
        artifact_root=ROOT, world_source=world, runtime_source=runtime
    )
    scene = inspect_static_gazebo_scene(
        artifact_root=ROOT,
        world_source=world,
        include_model_sources={"model://multidetect_search_target_v1": target},
    )
    output.mkdir(exist_ok=False)
    receipt = {
        "contract": "multidetect.teacher-static-input-audit-v1",
        "scope": (
            "retained repository geometry/configuration only; "
            "not live loaded-asset or route approval"
        ),
        "scene": scene.to_mapping(),
        "scene_sha256": scene.canonical_hash,
        "configured_wind": wind.to_mapping(),
        "configured_wind_sha256": wind.canonical_hash,
        "collision_primitive_count": len(scene.collisions),
        "world_configured_wind_north_mps": wind.wind_north_mps,
        "world_configured_wind_east_mps": wind.wind_east_mps,
        "runtime_wind_direction_matches_world": wind.runtime_direction_matches,
        "claims": {
            name: False
            for name in (
                "live_runtime_verified",
                "origin_qualified",
                "route_collision_clearance_passed",
                "full_map_DEM_resources_qualified",
                "static_geometry_sent_to_actor",
                "Mission_approved",
                "Mission_transferred",
                "ARM_sent",
                "flight_executed",
                "new_training_performed",
                "candidate_promoted",
                "physical_performance_claimed",
                "hil_real_evidence",
                "outdoor_gnss_evidence",
                "final_release_allowed",
            )
        },
    }
    binding = _json_new(output / "static-input-audit.json", receipt)
    return {"collision_primitive_count": len(scene.collisions), "receipt": binding}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(inspect_static_inputs(args.output_directory)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
