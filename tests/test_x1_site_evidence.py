import hashlib
import json
from types import SimpleNamespace

import pytest

from multidetect.sim_teacher.x1_live_evidence_builder import build_live_evidence
from scripts.realworld import x1_site_evidence, x1_site_obstacles


def test_deployed_triangles_are_enclosed_with_enu_to_msl_conversion(tmp_path):
    obj = tmp_path / "mesh.obj"
    obj.write_text(
        "v -10 1 0\nv -8 3 2\nv -11 1 3\nv 18 20 -2\nv 20 22 0\nv 18 24 1\nf 1 2 3\nf 4 5 6\n"
    )
    boxes, triangles = x1_site_obstacles._mesh_boxes(obj, "buildings", 318.23)
    assert triangles == 2
    assert len(boxes) == 2
    for box, (north, east, elevation) in zip(
        boxes, [(1, -11, 321.23), (24, 20, 316.23)], strict=True
    ):
        assert box[0] < north < box[1]
        assert box[2] < east < box[3]
        assert box[4] < elevation < box[5]
    assert boxes[0][3] < boxes[1][2]  # Don't turn disjoint buildings into one occupied box.


def test_canopy_batch_across_home_keeps_triangles_in_separate_boxes(tmp_path):
    obj = tmp_path / "canopy.obj"
    obj.write_text(
        "v -84 1 0\nv -83 3 2\nv -82 1 3\nv 85 1 0\nv 86 3 2\nv 87 1 3\nf 1 2 3\nf 4 5 6\n"
    )
    boxes, triangles = x1_site_obstacles._mesh_boxes(obj, "canopy", 318.23)
    assert triangles == len(boxes) == 2
    assert boxes[0][2] < -84 < -82 < boxes[0][3] < -40
    assert 40 < boxes[1][2] < 85 < 87 < boxes[1][3]


@pytest.mark.parametrize("record", ["f 1 2 4", "f 0 2 3", "f 1 2 3 4", "l 1 2"])
def test_unhandled_or_invalid_obj_fails_closed(tmp_path, record):
    obj = tmp_path / "mesh.obj"
    obj.write_text(f"v 0 1 0\nv -8 3 2\nv -11 1 3\n{record}\n")
    with pytest.raises(ValueError):
        x1_site_obstacles._mesh_boxes(obj, "buildings", 318.23)


@pytest.fixture
def site_inputs(tmp_path, monkeypatch):
    scene = tmp_path / "scene.json"
    scene.write_bytes(b"scene")
    models = tmp_path / "models"
    models.mkdir()
    world = tmp_path / "world.sdf"
    world.write_bytes(b"world")
    world_sha = x1_site_obstacles.sha256(world)
    ground = models / x1_site_obstacles.WORLD_MODEL / "meshes" / "ground_collision.obj"
    ground.parent.mkdir(parents=True)
    ground.write_bytes(b"ground")
    paths = [ground, *(models / f"test{index}.obj" for index in range(356))]
    for path in paths[1:]:
        path.write_text(path.name)
    inventory = {
        "world_sha256": world_sha,
        "dem_sha256": "d" * 64,
        "scene_sha256": x1_site_obstacles.sha256(scene),
        "scene_path": str(scene),
        "models_root": str(models),
        "mesh_sha256": {
            path.relative_to(models).as_posix(): x1_site_obstacles.sha256(path) for path in paths
        },
        "obstacle_coverage": [-20, 20, -20, 20, 300, 600],
        "obstacles": [[2, 3, 4, 5, 310, 320]],
    }
    monkeypatch.setattr(x1_site_obstacles, "WORLD_SHA", world_sha)
    monkeypatch.setattr(x1_site_obstacles, "SCENE_SHA", inventory["scene_sha256"])
    monkeypatch.setattr(x1_site_obstacles, "GROUND_SHA", x1_site_obstacles.sha256(ground))
    result = {
        "world": x1_site_obstacles.SITE,
        "scenario": f"/scenario/{x1_site_obstacles.SCENARIO}.scenario.json",
        "prepared": {key: inventory[key] for key in ("world_sha256", "dem_sha256")},
        "flight": {"status": "plan_only"},
        "validator": {"verdict": "accepted"},
        "contingency_scenario": {
            "motion_scope_sha256": "a" * 64,
            "live_evidence": {"base_mission_sha256": "b" * 64, "host_bounds_path": None},
        },
    }
    return SimpleNamespace(
        result=result,
        inventory=inventory,
        scene=scene,
        models=models,
        ground=ground,
        world=world,
        path=tmp_path / "wind.site-bounds.json",
    )


def verify(inputs, **overrides):
    return x1_site_evidence.verify_site_bounds(
        inputs.path,
        **{
            "world_path": inputs.world,
            "world_sha256": inputs.inventory["world_sha256"],
            "dem_sha256": inputs.inventory["dem_sha256"],
            "models_root": str(inputs.models),
            "rtf_model_root": "",
            **overrides,
        },
    )


def test_site_bounds_deterministic_and_accepted_in_existing_schema(site_inputs):
    i = site_inputs
    x1_site_evidence.write_bounds(i.path, i.result, i.inventory)
    provenance = x1_site_evidence._provenance_path(i.path)
    first = (i.path.read_bytes(), provenance.read_bytes())
    x1_site_evidence.write_bounds(i.path, i.result, i.inventory)
    assert first == (i.path.read_bytes(), provenance.read_bytes())
    bound = verify(i)
    assert json.loads(first[1])["bounds_sha256"] == hashlib.sha256(first[0]).hexdigest()
    evidence, missing = build_live_evidence(
        SimpleNamespace(document={"budget": {}}),
        terrain=SimpleNamespace(source_sha256=bound["dem_sha256"]),
        scope_sha256=bound["scope_sha256"],
        base_mission_sha256=bound["base_mission_sha256"],
        surveyed_bounds=bound,
    )
    assert set(evidence) == {"scope_sha256", "dem_sha256", "obstacle_coverage", "obstacles"}
    assert set(missing) == {
        "dem_error_m",
        "landing_pad",
        "battery",
        "suffixes",
        "budget.battery_capacity_wh",
    }


@pytest.mark.parametrize(
    "change", ["bounds", "scene", "mesh", "dem", "world", "no_companion", "model_override"]
)
def test_stale_or_incomplete_site_provenance_is_rejected(site_inputs, change):
    i = site_inputs
    x1_site_evidence.write_bounds(i.path, i.result, i.inventory)
    kwargs = {}
    if change == "bounds":
        i.path.write_text("{}")
    elif change == "scene":
        i.scene.write_text("stale")
    elif change == "mesh":
        i.ground.write_text("changed deployed asset")
    elif change == "dem":
        kwargs["dem_sha256"] = "e" * 64
    elif change == "world":
        kwargs["world_sha256"] = "f" * 64
    elif change == "model_override":
        kwargs["rtf_model_root"] = str(i.models)
    else:
        x1_site_evidence._provenance_path(i.path).unlink()
    with pytest.raises(ValueError):
        verify(i, **kwargs)


def test_plan_only_binding_does_not_publish_on_wrong_site_hash(site_inputs):
    i = site_inputs
    i.result["prepared"]["dem_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="exact audited inputs"):
        x1_site_evidence.write_bounds(i.path, i.result, i.inventory)
    assert not i.path.exists()
