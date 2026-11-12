import json
from pathlib import Path

import pytest

pytest.importorskip("cv2")
from scripts import build_sim_detector_dataset as builder  # noqa: E402


def test_world_id_accepts_rw_path_and_world_sdf(tmp_path):
    episode = tmp_path / "hover" / "rw_city_tokyo__clear_noon_clu6m" / "episodes" / "episode-0000"
    episode.mkdir(parents=True)
    assert builder._world_id(episode) == "rw_city_tokyo__clear_noon_clu6m"
    flat = tmp_path / "ep"
    flat.mkdir()
    (flat / "episode.json").write_text(json.dumps({"world_sdf": "/x/worlds/rw_a__b_clu3m.sdf"}))
    assert builder._world_id(flat) == "rw_a__b_clu3m"


def test_find_truth_in_realworld_clusters(tmp_path):
    root = Path(__file__).resolve().parents[1]
    world = json.loads((root / "configs/scenarios/realworld-clusters/index.json").read_text())["worlds"][0]["world_id"]
    assert builder._find_truth(world, [tmp_path]) is not None


def test_target_models_carry_label_plugin():
    pytest.importorskip("shapely")
    from scripts.realworld import make_world

    on = make_world.labelled_model("t", "fire_hydrant", 0, 0, 0, 0, 10, emit_label=True)
    off = make_world.labelled_model("t", "fire_hydrant", 0, 0, 0, 0, 10)
    assert "<label>10</label>" in on and "gz-sim-label-system" not in off
