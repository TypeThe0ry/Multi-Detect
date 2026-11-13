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
    assert on.count("gz-sim-label-system") == 2  # outer model and nested mesh model


def test_cluster_worlds_ship_close_range_teacher_intent():
    root = Path(__file__).resolve().parents[1] / "configs/scenarios/realworld-clusters"
    worlds = json.loads((root / "index.json").read_text())["worlds"]
    for world in worlds:
        for folder in (root, root / "delta_tailsitter_gimbal"):
            intent = json.loads((folder / f"{world['world_id']}.teacher_intent.json").read_text())["observation"]
            assert intent["camera_altitude_m"] <= 24 and intent["vtol_hover_floor_m"] <= intent["camera_altitude_m"]
            assert len(intent["views"]) >= 6


def test_builder_can_salvage_unfinalised_episodes(tmp_path):
    import numpy as np
    import cv2

    ep = tmp_path / "rw_city_tokyo__clear_noon_clu6m" / "episodes" / "episode-0000"
    (ep / "rgb").mkdir(parents=True)
    cv2.imwrite(str(ep / "rgb/000000_before.png"), np.zeros((32, 32, 3), np.uint8) + 7)
    (ep / "episode.json").write_text(json.dumps({"world_sdf": "x/rw_city_tokyo__clear_noon_clu6m.sdf"}))
    (ep / "steps.jsonl").write_text(json.dumps({"obs_before": {"rgb_path": "rgb/000000_before.png"}}) + "\n")
    index = json.loads((Path(__file__).resolve().parents[1] / "configs/scenarios/realworld-clusters/index.json").read_text())
    for world in index["worlds"]:
        world["split"] = "train"
    (tmp_path / "idx.json").write_text(json.dumps(index))
    common = dict(index_path=tmp_path / "idx.json", label_source="projection")
    skipped = builder.build_dataset([tmp_path], tmp_path.parent / (tmp_path.name + "-out0"), **common)
    salvaged = builder.build_dataset([tmp_path], tmp_path.parent / (tmp_path.name + "-out1"), include_incomplete=True, **common)
    assert skipped["counts"]["frames"] == 0
    assert salvaged["dedupe"]["unique_sha"] == 1  # the frame was scanned


def test_gimbal_configure_is_resent_with_growing_windows_before_timing_out(monkeypatch):
    import scripts.collect_sim_teacher_episodes as collector

    sent, windows = [], []

    class Mav:
        def command_long_send(self, *args):
            sent.append(args)

    class Connection:
        mav = Mav()
        target_system = 1
        target_component = 1

    class Link:
        def __init__(self, *_):
            pass

        def wait_command_ack(self, command, timeout_s):
            windows.append(timeout_s)
            return None

    monkeypatch.setattr(collector, "_ReaderAckLink", Link)
    result = collector._command_and_wait_gimbal(Connection(), object(), {"yaw": 0.0, "roll": 0.0, "pitch": -45.0})
    assert result["status"] == "manager_configure_ack_timeout"
    assert len(sent) == 3 and windows == [5.0, 10.0, 20.0]


def test_cruise_preset_defaults_and_explicit_override():
    import argparse

    ns = argparse.Namespace(negative_ratio=None, preset="cruise", include_incomplete=False)
    assert builder.resolve_preset(ns).negative_ratio == 0.3 and ns.include_incomplete
    hover = builder.resolve_preset(argparse.Namespace(negative_ratio=None, preset="hover", include_incomplete=False))
    assert hover.negative_ratio == 0.15 and not hover.include_incomplete
    explicit = builder.resolve_preset(argparse.Namespace(negative_ratio=0.5, preset="cruise", include_incomplete=False))
    assert explicit.negative_ratio == 0.5
