from pathlib import Path

import pytest

from multidetect.sim_teacher.spawn_clearance import require_spawn_clearance, spawn_clearance_report


def _write_world(path: Path, *, east: float, north: float, height: float = 4.0):
    path.write_text(
        f'''<sdf version="1.9"><world name="default">
        <model name="obstacle_fence"><pose>{east} {north} {height / 2} 0 0 0</pose>
        <link name="l"><collision name="c"><geometry><box><size>2 2 {height}</size></box></geometry></collision></link></model>
        </world></sdf>''',
        encoding="utf-8",
    )


def test_spawn_clearance_rejects_object_inside_radius_and_vertical_climb(tmp_path):
    world = tmp_path / "world.sdf"
    _write_world(world, east=8, north=0)
    report = spawn_clearance_report(world, {"z_up_m": 0.2}, climb_altitude_m=12)
    assert not report["clear"]
    assert [x["name"] for x in report["blockers"]] == ["obstacle_fence"]
    assert [x["name"] for x in report["climb_blockers"]] == ["obstacle_fence"]
    with pytest.raises(ValueError, match="obstacle_fence"):
        require_spawn_clearance(world, {"z_up_m": 0.2}, climb_altitude_m=12)


def test_spawn_clearance_accepts_objects_beyond_safety_radius(tmp_path):
    world = tmp_path / "world.sdf"
    _write_world(world, east=30, north=0)
    assert spawn_clearance_report(world, {"z_up_m": 0.2}, climb_altitude_m=12)["clear"]


def test_generated_r21id_spawn_clearance_all_worlds():
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    index = json.loads((root / "configs/scenarios/generated-r21id/index.json").read_text())
    for entry in index["worlds"]:
        intent = json.loads(
            (root / "configs/scenarios/generated-r21id" / f"{entry['world_id']}.teacher_intent.json").read_text()
        )
        report = spawn_clearance_report(
            root / "simulation/gazebo/worlds" / entry["world_file"],
            {"x_east_m": 0, "y_north_m": 0, "z_up_m": 0.2},
            climb_altitude_m=max(12.0, float(intent["observation"]["camera_altitude_m"])),
        )
        assert report["clear"], (entry["world_id"], report)


def test_generated_r20_spawn_clearance_all_worlds():
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    index = json.loads((root / "configs/scenarios/generated-r20/index.json").read_text(encoding="utf-8"))
    for entry in index["worlds"]:
        intent = json.loads(
            (root / "configs/scenarios/generated-r20" / f"{entry['world_id']}.teacher_intent.json").read_text(encoding="utf-8")
        )
        report = spawn_clearance_report(
            root / "simulation/gazebo/worlds" / entry["world_file"],
            {"x_east_m": 0, "y_north_m": 0, "z_up_m": 0.2},
            climb_altitude_m=float(intent["observation"]["camera_altitude_m"]),
        )
        assert report["clear"], (entry["world_id"], report)
