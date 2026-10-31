"""Offline contract checks for the r21id-r4 generated identity worlds."""

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import generate_r15_worlds  # noqa: E402


def test_r21id_r4_dry_run_profile(capsys):
    assert generate_r15_worlds.main(["--profile", "r21id-r4", "--dry-run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 24
    assert payload["world_prefix"] == "gen_r21id_r4_w"
    assert payload["splits"] == {"train": 18, "val": 6, "test": 0}


def test_generated_r21id_r4_offline_contract():
    base = ROOT / "configs/scenarios/generated-r21id-r4"
    index = json.loads((base / "index.json").read_text(encoding="utf-8"))
    catalog = json.loads((ROOT / "configs/sim-assets/fuel-catalog.json").read_text(encoding="utf-8"))
    assets = {row["slug"]: row for row in catalog["assets"]}
    assert index["count"] == 24
    assert index["profile"] == "r21id-r4"
    assert not any(row["split"] == "test" for row in index["worlds"])
    assert {slug: sum(row["target_combo"]["slug"] == slug for row in index["worlds"])
            for slug in ("pickup", "suv", "postbox", "walking_person", "ambulance")} == {
                "pickup": 6, "suv": 6, "postbox": 4, "walking_person": 4, "ambulance": 4,
            }

    small_counts = {"pickup": 0, "suv": 0, "postbox": 0}
    for row in index["worlds"]:
        wid = row["world_id"]
        truth = json.loads((base / f"{wid}.truth.json").read_text(encoding="utf-8"))
        runtime = json.loads((base / f"{wid}.runtime_input.json").read_text(encoding="utf-8"))
        intent = json.loads((base / f"{wid}.teacher_intent.json").read_text(encoding="utf-8"))["observation"]
        target = next(obj for obj in truth["targets"] if obj["true_target"])
        assert all(obj["asset_slug"] in assets for obj in truth["targets"])
        assert runtime["vehicle"]["spawn_pose"] == {
            "x_east_m": 0.0, "y_north_m": 0.0, "z_up_m": 0.2,
            "roll_rad": 0.0, "pitch_rad": 0.0, "yaw_rad": 0.0,
        }
        assert runtime["vehicle"]["airframe_profile_id"] == "px4_multirotor_gazebo_v1"
        assert intent["observation_frames"] == 8
        assert [view["view"] for view in intent["views"]] == ["front", "oblique", "edge_truncated", "recovery"]
        geom = row["view_geometry"]
        slant = math.hypot(geom["ground_distance_m"], geom["camera_altitude_m"])
        assert math.isclose(geom["slant_distance_m"], slant, abs_tol=0.002)
        assert math.isclose(target["pose"]["x_east_m"], geom["ground_distance_m"], abs_tol=0.002)
        assert target["pose"]["y_north_m"] == 0.0
        assert intent["camera_altitude_m"] == geom["camera_altitude_m"]
        computed = generate_r15_worlds.projected_short_edge_px(
            assets[target["asset_slug"]], geom["ground_distance_m"], geom["camera_altitude_m"]
        )
        assert math.isclose(computed, geom["target_short_side_px_estimate"], abs_tol=0.02)
        band = geom["target_short_side_px"]
        assert (8 <= computed < 12 if band == "8-12" else
                12 <= computed < 16 if band == "12-16" else
                24 <= computed < 32 if band == "24-32" else computed >= 32)
        if target["asset_slug"] in small_counts and band in {"8-12", "12-16"}:
            small_counts[target["asset_slug"]] += 1
        if target["asset_slug"] in {"pickup", "suv"}:
            assert any(obj.get("confuser") and obj["asset_slug"] == ("suv" if target["asset_slug"] == "pickup" else "pickup")
                       for obj in truth["targets"])
            assert len(row["task_text_variants"]) == 2
        if target["asset_slug"] == "postbox":
            assert any(obj["asset_slug"] == "hatchback" for obj in truth["targets"])
        if target["asset_slug"] == "walking_person":
            assert any(obj["asset_slug"] == "postbox" for obj in truth["targets"])
        if target["asset_slug"] == "ambulance":
            assert "SUV" in row["task_text_en"]
            assert row["find_task_text_en"] == row["task_text_en"]
            assert any(obj.get("confuser") and obj["asset_slug"] == "suv" for obj in truth["targets"])
            assert row["split"] in {"train", "val"}
        sdf = (ROOT / "simulation/gazebo/worlds" / row["world_file"]).read_text(encoding="utf-8")
        assert f'<model name="{target["target_id"]}">' in sdf
        assert "https://" not in sdf and "http://" not in sdf
    assert small_counts == {"pickup": 4, "suv": 4, "postbox": 2}
