import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import build_plan_sft  # noqa: E402
import generate_r15_worlds  # noqa: E402
from scripts.collect_sim_teacher_episodes import _airframe_profile  # noqa: E402

from multidetect.autonomy.plan_intent import check_plan_intent  # noqa: E402


def test_r20_profile_dry_run_isolated(capsys):
    assert generate_r15_worlds.main(["--profile", "r20", "--dry-run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 20
    assert payload["world_prefix"] == "gen_r20_w"


def test_r20_committed_worlds_have_no_heldout_true_targets_and_real_cases():
    index = json.loads(
        (ROOT / "configs/scenarios/generated-r20/index.json").read_text(encoding="utf-8")
    )
    held = json.loads(
        (ROOT / "configs/scenarios/generated-v2/index.json").read_text(encoding="utf-8")
    )["held_out_target_combos"]
    held_keys = {(x["slug"], x["category"], x.get("color")) for x in held}
    cases = {}
    paired_counts = {}
    assert index["count"] == 20
    assert sum(x["split"] == "train" for x in index["worlds"]) == 16
    assert sum(x["split"] == "val" for x in index["worlds"]) == 4
    assert not any(x["split"] == "test" for x in index["worlds"])
    assert index["held_out_target_combos"] == held
    for entry in index["worlds"]:
        wid = entry["world_id"]
        runtime = json.loads(
            (ROOT / "configs/scenarios/generated-r20" / f"{wid}.runtime_input.json").read_text(
                encoding="utf-8"
            )
        )
        spawn = runtime["vehicle"]["spawn_pose"]
        assert spawn["z_up_m"] < 1.0
        assert spawn["pitch_rad"] == 0.0
        intent = json.loads(
            (ROOT / "configs/scenarios/generated-r20" / f"{wid}.teacher_intent.json").read_text(encoding="utf-8")
        )["observation"]
        assert intent["camera_altitude_m"] > 1.0
        assert "camera_pitch_rad" in intent
        assert set(intent["observation_waypoint"]) >= {"x_east_m", "y_north_m", "z_up_m"}
        truth = json.loads(
            (ROOT / "configs/scenarios/generated-r20" / f"{wid}.truth.json").read_text(
                encoding="utf-8"
            )
        )
        target = next(x for x in truth["targets"] if x.get("true_target"))
        assert (target["asset_slug"], target["class"], target.get("color")) not in held_keys
        assert entry["target_combo"]["slug"] == target["asset_slug"]
        cases[entry["hard_case_profile"]] = cases.get(entry["hard_case_profile"], 0) + 1
        if entry["hard_case_profile"] == "paired_confusers":
            confuser = next(x for x in truth["targets"] if x.get("confuser"))
            pair = (target["asset_slug"], confuser["asset_slug"])
            paired_counts[pair] = paired_counts.get(pair, 0) + 1
            assert confuser["pose"]["x_east_m"] - target["pose"]["x_east_m"] == 3.5 or pair != (
                "construction_cone",
                "jersey_barrier",
            )
            if pair == ("construction_cone", "jersey_barrier"):
                assert target["pose"]["x_east_m"] == 20.0
                assert target["pose"]["y_north_m"] == confuser["pose"]["y_north_m"] == 12.0
                assert entry["view_geometry"]["target_short_side_px"] == [16, 20]
        sdf = (ROOT / "simulation/gazebo/worlds" / entry["world_file"]).read_text(encoding="utf-8")
        if entry["hard_case_profile"] == "hydrant_building_occlusion_truncation":
            assert (
                "r20_building_occluder" in sdf and entry["view_geometry"]["image_bottom_truncation"]
            )
        if entry["hard_case_profile"] == "fixed_wing_oblique":
            assert runtime["vehicle"]["airframe_profile_id"] == "px4_fixed_wing_gazebo_v1"
            teacher_intent = json.loads(
                (ROOT / "configs/scenarios/generated-r20" / f"{wid}.teacher_intent.json").read_text(encoding="utf-8")
            )
            assert teacher_intent["airframe"] == "rc_cessna_runway"
            assert teacher_intent["takeoff_method"] == "runway"
            assert _airframe_profile(teacher_intent["airframe"], teacher_intent["takeoff_method"]) is not None
            assert entry["view_geometry"]["slant_range_m"] >= 80
    assert cases == {
        "paired_confusers": 6,
        "fixed_wing_oblique": 5,
        "backpack_small_oblique": 5,
        "hydrant_building_occlusion_truncation": 4,
    }
    assert paired_counts == {
        ("construction_cone", "jersey_barrier"): 4,
        ("hatchback_blue", "ambulance"): 1,
        ("ambulance", "bus"): 1,
    }


def test_r20_intent_pairs_cover_correction_and_unknown_place(tmp_path):
    args = type("Args", (), {"prompt_variant": "default"})()
    rows = build_plan_sft.r20_intent_rows(args, set(), {})
    assert {row["family"] for row in rows} == {
        "bad_return",
        "good_return",
        "bad_unknown_place",
        "good_return_alias",
    }
    refs = ("poi:gate (大门 / the gate)", "poi:pad (起降坪 / the pad)")
    for row in rows:
        findings = check_plan_intent(row["assistant_target"], row["task_text"], refs)
        if row["family"].startswith("bad_"):
            assert findings
        else:
            assert findings == []
