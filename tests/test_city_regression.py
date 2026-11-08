from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.realworld import prepare_city_regression as cities


def test_city_cache_must_match_the_actual_origin_and_extent() -> None:
    configured = {"lat": 31.24, "lon": 121.5, "size_m": 2000}
    cities.check_origin("site", configured, dict(configured))
    for key, difference in (("lat", 0.001), ("lon", 0.001), ("size_m", 1000)):
        actual = {**configured, key: configured[key] + difference}
        with pytest.raises(ValueError, match=key):
            cities.check_origin("site", configured, actual)


def test_city_registration_never_silently_omits_a_missing_world(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cities, "ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        cities.write_index()
    directory = tmp_path / "configs/scenarios/realworld"
    directory.mkdir(parents=True)
    for site in cities.SITES:
        (directory / f"{site}__{cities.WEATHER}.scenario.json").write_text("{}")
    data = json.loads(cities.write_index().read_text())
    assert data["count"] == 3
    assert {row["site"] for row in data["worlds"]} == set(cities.SITES)
    assert all(row["split"] == "test" for row in data["worlds"])


def test_texture_profile_preserves_aspect_ratio_and_reports_decoded_memory(tmp_path) -> None:
    image = pytest.importorskip("PIL.Image")
    model = tmp_path / "model"
    textures = model / "textures"
    textures.mkdir(parents=True)
    image.new("RGB", (512, 256)).save(textures / "ground.jpg")
    image.new("RGBA", (32, 32)).save(textures / "inst_tree.png")
    scene_path = tmp_path / "scene.json"
    scene_path.write_text(json.dumps({"stats": {}, "raw_dir": "/unaltered/raw"}))
    cities.limit_model_textures(model, scene_path, 128)
    with image.open(textures / "ground.jpg") as result:
        assert result.size == (128, 64)
    with image.open(textures / "inst_tree.png") as result:
        assert result.size == (32, 32)
        assert result.mode == "RGBA"
    scene = json.loads(scene_path.read_text())
    assert scene["raw_dir"] == "/unaltered/raw"
    assert scene["stats"]["decoded_texture_bytes"] == (128 * 64 + 32 * 32) * 4
    assert scene["stats"]["decoded_prop_texture_bytes"] == 32 * 32 * 4


def test_city_suite_uses_active_models_pregate_and_distinct_geographic_sites() -> None:
    root = Path(__file__).resolve().parents[1]
    suite = json.loads((root / "configs/onboard/suite-cities-r21idr3-r24plan.json").read_text())
    active = json.loads((root / "configs/onboard/sim-loop-v3-r21idr3-r24plan-defaults.json").read_text())
    sites = json.loads((root / "configs/sim-assets/realworld-sites.json").read_text())["sites"]
    for key in ("gguf", "planning_model", "planning_pregate", "yoloe_weights"):
        assert suite["defaults"][key] == active[key]
    assert len(suite["missions"]) == 3
    assert {mission["world"] for mission in suite["missions"]} == {
        f"{site}__{cities.WEATHER}" for site in cities.SITES
    }
    for mission in suite["missions"]:
        assert mission["expect"]["skills"] == ["survey", "return_home"]
        assert mission["expect"]["min_survey_coverage"] == 0.7
    for site in cities.SITES[:2]:
        assert 31.20 < sites[site]["lat"] < 31.25
        assert 121.49 < sites[site]["lon"] < 121.57
    marina = sites["rw_singapore_marina_bay"]
    assert 1.27 < marina["lat"] < 1.30
    assert 103.84 < marina["lon"] < 103.87
