from __future__ import annotations

import json
from pathlib import Path

import pytest

from multidetect.simulator.camera_projection import pixel_ground_point
from scripts.replay_ground_projection import (
    associate_box,
    error_statistics,
    replay_episodes,
    synthetic_slopes,
)


def _episode(tmp_path: Path):
    episode = tmp_path / "episode"
    episode.mkdir()
    terrain = tmp_path / "terrain"
    terrain.mkdir()
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    (episode / "episode.json").write_text(json.dumps({"world_sdf": "test.sdf"}))
    truth = {
        "targets": [
            {
                "target_id": "object",
                "pose": {
                    "y_north_m": 40.0,
                    "x_east_m": 0.0,
                    "z_up_m": 1.0,
                },
                "size_m": [4.0, 2.0, 2.0],
            }
        ],
        "segmentation_labels": {"7": {"target_id": "object"}},
    }
    (scenarios / "test.truth.json").write_text(json.dumps(truth))
    (terrain / "test.dem.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scenario_id": "test",
                "coordinate_reference": "WGS84",
                "vertical_datum": "MSL",
                "origin": {
                    "latitude_deg": 1.3521,
                    "longitude_deg": 103.8198,
                    "elevation_m": 135.0,
                },
                "north_origin_m": -200.0,
                "east_origin_m": -200.0,
                "north_spacing_m": 200.0,
                "east_spacing_m": 200.0,
                "elevation_m": [[135.0] * 3 for _ in range(3)],
                "vertical_sigma_m": 0.02,
                "slope_sigma_m_per_m": 0.001,
            }
        )
    )
    observation = {
        "pose": {"local_ned_m": [0.0, 0.0, -30.0]},
        "attitude": {},
        "camera_info": {
            "fx": 540.0,
            "fy": 540.0,
            "cx": 640.0,
            "cy": 480.0,
            "width": 1280,
            "height": 960,
        },
    }
    return episode, terrain, scenarios, observation


def test_image_association_rejects_low_overlap_and_does_not_use_ground():
    assert associate_box([0, 0, 10, 10], {"near": [20, 20, 30, 30]}, 0.1) is None
    assert associate_box([0, 0, 10, 10], {}, 0.1) is None
    assert associate_box(
        [0, 0, 10, 10],
        {
            "wrong": [5, 5, 15, 15],
            "correct": [0, 0, 10, 10],
        },
        0.1,
    ) == ("correct", 1.0)


def test_replay_prefers_segmentation_and_records_default_equivalence(tmp_path, monkeypatch):
    from PIL import Image

    import scripts.replay_ground_projection as replay

    episode, terrain, scenarios, observation = _episode(tmp_path)
    labels = Image.new("RGB", (1280, 960))
    # A visible bbox can differ from projected full-object bounds due to occlusion.
    labels.paste((0, 0, 7), (600, 500, 680, 600))
    labels.save(episode / "seg.png")
    observation["segmentation_path"] = "seg.png"
    ground = pixel_ground_point(
        640, 600, pose=observation, camera_info=observation["camera_info"], image_size=(1280, 960)
    )
    (episode / "steps.jsonl").write_text(
        json.dumps(
            {
                "step_index": 0,
                "obs_before": observation,
                "labels": {
                    "onboard": {
                        "tracks": [
                            {"id": "t1", "bbox": [600, 500, 680, 600], "ground_ne": ground},
                            {"id": "t2", "bbox": [0, 0, 10, 10]},
                        ]
                    }
                },
            }
        )
        + "\n"
    )

    def forbid_projection(*args):
        raise AssertionError("segmentation is available")

    monkeypatch.setattr(replay, "_projected_boxes", forbid_projection)
    result = replay_episodes([episode], terrain_dir=terrain, scenarios=scenarios, include_hits=True)
    assert result["association"]["sources"] == {"segmentation": 1}
    assert result["default_equivalence"] == {"checked": 2, "exact_equal": 2}
    assert result["stored_ground_ne_equivalence"]["checked"] == 1
    assert result["stored_ground_ne_equivalence"]["exact_equal"] == 1
    assert result["skip_counts"] == {"track_no_image_association": 1}
    assert result["flat_dem_position_difference"]["max_m"] < 1e-10
    assert result["hits"][0]["target_id"] == "object"
    assert result["episodes"][0]["sources"]["segmentation_files"] == 1
    assert len(result["episodes"][0]["sources"]["steps"]["sha256"]) == 64


def test_replay_projection_fallback_and_missing_episode(tmp_path):
    import scripts.replay_ground_projection as replay

    episode, terrain, scenarios, observation = _episode(tmp_path)
    truth = json.loads((scenarios / "test.truth.json").read_text())
    boxes = replay._projected_boxes(observation, {"object": truth["targets"][0]})
    (episode / "steps.jsonl").write_text(
        json.dumps(
            {
                "obs_before": observation,
                "labels": {"onboard": {"tracks": [{"bbox": boxes["object"]}]}},
            }
        )
    )
    result = replay_episodes(
        [episode, tmp_path / "missing"], terrain_dir=terrain, scenarios=scenarios
    )
    assert result["association"]["sources"] == {"projected_truth_bbox": 1}
    assert result["skip_counts"] == {"episode_missing_steps": 1}
    assert result["flat"]["count"] == result["dem"]["count"] == 1
    assert "hits" not in result


def test_synthetic_contact_rays_recover_lateral_slopes_at_nonzero_home_elevation():
    cases = synthetic_slopes()
    assert len(cases) == 2
    for case in cases:
        assert case["reference_elevation_msl_m"] == 135.0
        assert case["north_slope_m_per_m"] != 0 and case["east_slope_m_per_m"] != 0
        assert case["flat"]["count"] == case["dem"]["count"] == 9
        assert case["flat"]["mean_m"] > 10.0
        assert case["dem"]["max_m"] < 1e-9


def test_statistics_use_nearest_rank_and_explicit_empty_samples():
    assert error_statistics([])["mean_m"] is None
    result = error_statistics([1.0, 2.0, 3.0, 4.0])
    assert result["p95_m"] == 4.0
    assert result["rmse_m"] == pytest.approx((30 / 4) ** 0.5)
