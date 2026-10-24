#!/usr/bin/env python3
"""Generate the r15 Fuel-appearance train/val world set."""

from __future__ import annotations

import argparse
import json
import math
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from multidetect.autonomy.serialization import canonical_sha256  # noqa: E402
from multidetect.simulator.world_gen import (  # noqa: E402
    FIXED_WING_SENSORS,
    generate_fuel_worlds,  # noqa: E402
)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument(
        "--config", type=Path, default=ROOT / "configs/scenarios/generated-r15/r15-world-set.json"
    )
    p.add_argument("--out-worlds", type=Path, default=ROOT / "simulation/gazebo/worlds")
    p.add_argument("--out-scenarios", type=Path, default=ROOT / "configs/scenarios/generated-r15")
    p.add_argument("--world-prefix", help="override the generated world id prefix")
    p.add_argument("--seed", type=int, help="override the configured generation seed")
    p.add_argument("--count", type=int, help="override the configured world count")
    p.add_argument("--catalog", type=Path, help="override asset catalog path")
    p.add_argument("--resolved-root", type=Path, help="override resolved asset root")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--profile",
        choices=("default", "r20", "r21-identity"),
        default="default",
        help="optional hard-case profile; default preserves the r15 config",
    )
    a = p.parse_args(argv)
    cfg = json.loads(a.config.read_text(encoding="utf-8"))
    if a.profile in ("r20", "r21-identity"):
        # Four deterministic hard-case families. All true-target slugs are
        # outside generated-v2's frozen holdout. Cone/barrier is the primary
        # paired hard case; hatchbacks provide non-held-out vehicle lookalikes.
        templates = (
            *[
                {
                    "case": "paired_confusers",
                    "target": "construction_cone",
                    "confusers": ["jersey_barrier"],
                    "appearance": "noon_clear",
                }
                for _ in range(4)
            ],
            *[
                {
                    "case": "paired_confusers",
                    "target": "hatchback_blue",
                    "confusers": ["ambulance"],
                    "appearance": "noon_clear",
                }
            ],
            *[
                {
                    "case": "paired_confusers",
                    "target": "ambulance",
                    "confusers": ["bus"],
                    "appearance": "noon_clear",
                }
            ],
            *[
                {
                    "case": "fixed_wing_oblique",
                    "target": "fire_truck",
                    "confusers": ["ambulance"],
                    "appearance": "overcast_flat",
                }
                for _ in range(5)
            ],
            *[
                {
                    "case": "backpack_small_oblique",
                    "target": "backpack",
                    "confusers": ["cardboard_box"],
                    "forced_distractors": ["fire_hydrant"],
                    "appearance": "haze",
                }
                for _ in range(5)
            ],
            *[
                {
                    "case": "hydrant_building_occlusion_truncation",
                    "target": "fire_hydrant",
                    "confusers": ["cardboard_box"],
                    "appearance": "morning_low_sun",
                }
                for _ in range(4)
            ],
        )
        if a.profile == "r21-identity":
            classes = ("fire_hydrant", "backpack", "construction_cone")
            layers = ((12, 12, [8, 12]), (18, 20, [13, 20]), (24, 30, [21, 32]), (32, 40, [13, 20]),
                      (18, 12, [8, 12]), (24, 20, [13, 20]), (32, 30, [21, 32]), (12, 40, [8, 12]))
            templates = tuple(
                {
                    "case": "identity_multiview", "target": cls,
                    "confusers": ["cardboard_box" if cls != "construction_cone" else "jersey_barrier"],
                    "forced_distractors": ["electrical_box"],
                    "appearance": ("noon_clear", "overcast_flat", "morning_low_sun")[i % 3],
                    "height_m": layers[i][0], "distance_m": layers[i][1],
                    "target_short_side_px": layers[i][2],
                    "split": "train" if i < 6 else ("val" if i == 6 else "test"),
                }
                for cls in classes for i in range(8)
            )
        cfg.update(
            {
                "world_prefix": "gen_r21id_w" if a.profile == "r21-identity" else "gen_r20_w",
                "seed": 20261025 if a.profile == "r21-identity" else 20261024,
                "count": len(templates),
                "split_ratios": [0.75, 0.125, 0.125] if a.profile == "r21-identity" else [0.8, 0.2, 0.0],
                "spread_m": [80.0, 120.0],
                "count_copies": 1,
                "confuser_copies": 1,
                "segmentation_labels": True,
                "unambiguous_tasks": True,
            }
        )
        cfg["worlds"] = [
            dict(t, split=t.get("split", "train" if i < 16 else "val")) for i, t in enumerate(templates)
        ]
    if a.dry_run:
        print(
            json.dumps(
                {
                    "count": cfg["count"],
                    "seed": cfg["seed"],
                    "world_prefix": cfg["world_prefix"],
                    "splits": {
                        s: sum(w["split"] == s for w in cfg["worlds"])
                        for s in ("train", "val", "test")
                    },
                },
                ensure_ascii=False,
            )
        )
        return 0
    if a.world_prefix:
        cfg["world_prefix"] = a.world_prefix
    if a.seed is not None:
        cfg["seed"] = a.seed
    if a.count is not None:
        cfg["count"] = a.count
        cfg["worlds"] = [
            dict(
                cfg["worlds"][i % len(cfg["worlds"])],
                world_id=f"{cfg['world_prefix']}{i:04d}",
            )
            for i in range(a.count)
        ]
    targets = tuple(w["target"] for w in cfg["worlds"])
    conf = {}
    forced = {}
    for w in cfg["worlds"]:
        if w.get("confusers"):
            conf[w["target"]] = tuple(w["confusers"])
        forced[w["target"]] = tuple(
            dict.fromkeys((*forced.get(w["target"], ()), *w.get("forced_distractors", ())))
        )
    # Existing generated-r15 outputs are intentionally removed before regeneration;
    # only this prefix is written.
    doc = generate_fuel_worlds(
        count=cfg["count"],
        seed=cfg["seed"],
        split_ratios=tuple(cfg["split_ratios"]),
        out_worlds=a.out_worlds,
        out_scenarios=a.out_scenarios,
        catalog_path=(a.catalog or (ROOT / cfg["catalog"])),
        resolved_root=(a.resolved_root or (ROOT / cfg["resolved_root"])),
        segmentation_labels=cfg["segmentation_labels"],
        unambiguous_tasks=cfg["unambiguous_tasks"],
        world_prefix=cfg["world_prefix"],
        count_copies=cfg["count_copies"],
        count_slugs=targets,
        spread_m=tuple(cfg["spread_m"]),
        confusers=conf,
        confuser_copies=cfg["confuser_copies"],
        appearance=tuple(w["appearance"] for w in cfg["worlds"]),
        forced_distractors=forced,
    )
    if a.profile in ("r20", "r21-identity"):
        doc["profile"] = a.profile
        for entry, world in zip(doc["worlds"], cfg["worlds"], strict=True):
            entry["hard_case_profile"] = world["case"]
            entry["paired_confusers"] = list(world.get("confusers", ()))
            entry["view_geometry"] = {"paired_same_world": True}
            if a.profile == "r21-identity":
                entry["hard_case_profile"] = "identity_multiview"
                entry["view_geometry"].update({
                    "camera_altitude_m": world["height_m"], "ground_distance_m": world["distance_m"],
                    "target_short_side_px": world["target_short_side_px"],
                    "visible_fraction": [0.45, 0.65, 0.85, 1.0],
                    "occlusion_types": ["none", "building", "guardrail", "pipe_line"],
                    "view_angles_deg": [0, 35, 90, 145],
                    "identity_sequence": ["front", "oblique", "edge_truncated", "recovery"],
                    "target_id": entry["true_target_id"],
                })
            wid = entry["world_id"]
            sc = a.out_scenarios
            scenario = json.loads((sc / f"{wid}.scenario.json").read_text(encoding="utf-8"))
            channels = {
                n: json.loads((sc / f"{wid}.{n}.json").read_text(encoding="utf-8"))
                for n in ("runtime_input", "truth", "expectations")
            }
            truth, runtime = channels["truth"], channels["runtime_input"]
            target = next(t for t in truth["targets"] if t.get("true_target"))
            # Place the target in a controlled camera-relative position and write
            # the same pose into SDF and truth, preserving asset and label identity.
            case = entry["hard_case_profile"]
            if a.profile == "r21-identity":
                tx, ty, tz, altitude, angle = float(world["distance_m"]), 0.0, 0.0, float(world["height_m"]), -0.42
            elif case == "backpack_small_oblique":
                tx, ty, tz, altitude, angle = 21.0 + (int(wid[-4:]) % 2), 18.0, 0.0, 19.0, -0.42
                entry["view_geometry"] = {
                    "slant_range_m": 32,
                    "altitude_m": altitude,
                    "target_short_side_px": [10, 13],
                    "camera_pitch_rad": angle,
                }
            elif case == "hydrant_building_occlusion_truncation":
                tx, ty, tz, altitude, angle = 28.0, 22.0, 0.0, 18.0, -0.48
                entry["view_geometry"] = {
                    "building_occluder": True,
                    "camera_altitude_m": altitude,
                    "image_bottom_truncation": True,
                    "camera_pitch_rad": angle,
                }
            elif case in ("fixed_wing_oblique",):
                tx, ty, tz, altitude, angle = 60.0, 35.0, 0.0, 75.0, -0.18
                entry["view_geometry"] = {
                    "fixed_wing": True,
                    "slant_range_m": 101,
                    "camera_altitude_m": altitude,
                    "camera_pitch_rad": angle,
                    "target_short_side_px": [20, 40],
                }
            else:
                tx, ty, tz, altitude, angle = 35.0, 28.0, 0.0, 30.0, -0.25
            if case == "paired_confusers" and target["asset_slug"] == "construction_cone":
                # 1.09 m cone at ~28 m slant range is about 18 px tall at
                # 480 px / 15 m nominal focal length; barrier stays nearby.
                tx, ty, tz, altitude, angle = 20.0, 12.0, 0.0, 22.0, -0.35
                entry["view_geometry"].update(
                    {
                        "paired_same_world": True,
                        "pair_separation_m": 3.5,
                        "target_short_side_px": [16, 20],
                        "camera_altitude_m": altitude,
                        "camera_pitch_rad": angle,
                    }
                )
            target["pose"].update({"x_east_m": tx, "y_north_m": ty, "z_up_m": tz, "yaw_rad": 0.0})
            target["position"].update(
                {
                    "altitude_m": tz,
                    "latitude_deg": round(1.352100 + ty / 111_132.0, 8),
                    "longitude_deg": round(
                        103.819800 + tx / (111_320.0 * math.cos(math.radians(1.352100))), 8
                    ),
                }
            )
            # Observation geometry is a mission/teacher input, never a vehicle
            # spawn pose.  Keep the vehicle on the ground in a neutral attitude;
            # PX4 rejects airborne or tilted initial states during arming.
            observation_intent = {
                "camera_altitude_m": altitude,
                "camera_pitch_rad": angle,
                "observation_waypoint": {
                    "x_east_m": tx,
                    "y_north_m": ty,
                    "z_up_m": 0.0,
                },
            }
            runtime["task_intent"] = {
                "kind": "find_and_observe",
                "observation": observation_intent,
            }
            root = ET.parse(a.out_worlds / f"{wid}.sdf")
            model = root.find(f".//model[@name='{target['target_id']}']")
            if model is None:
                raise RuntimeError(f"{wid}: target SDF model not found")
            model.find(
                "pose"
            ).text = f"{tx:.3f} {ty:.3f} {tz + float(target['size_m'][2]) / 2:.3f} 0 0 0"
            if case == "paired_confusers" and target["asset_slug"] == "construction_cone":
                pair = next(
                    row
                    for row in truth["targets"]
                    if row.get("confuser") and row["asset_slug"] == "jersey_barrier"
                )
                pair["pose"].update(
                    {"x_east_m": tx + 3.5, "y_north_m": ty, "z_up_m": 0.0}
                )
                pair["position"].update(
                    {
                        "altitude_m": 0.0,
                        "latitude_deg": round(1.352100 + ty / 111_132.0, 8),
                        "longitude_deg": round(
                            103.819800
                            + (tx + 3.5)
                            / (111_320.0 * math.cos(math.radians(1.352100))),
                            8,
                        ),
                    }
                )
                pair_model = root.find(".//model[@name='" + pair["target_id"] + "']")
                if pair_model is None:
                    raise RuntimeError(f"{wid}: paired jersey barrier SDF model not found")
                pair_model.find("pose").text = (
                    f"{tx + 3.5:.3f} {ty:.3f} {float(pair['size_m'][2]) / 2:.3f} 0 0 0"
                )
            if case == "hydrant_building_occlusion_truncation" or a.profile == "r21-identity":
                building_xml = (
                    f'<model name="{a.profile.replace("-", "_")}_building_occluder"><static>true</static>'
                    f"<pose>{tx / 2:.3f} {ty / 2:.3f} 6 0 0 0</pose><link name=\"body\">"
                    '<collision name="collision"><geometry><box><size>12 8 12</size>'
                    "</box></geometry></collision>"
                    '<visual name="visual"><geometry><box><size>12 8 12</size></box>'
                    "</geometry><material><ambient>0.35 0.35 0.38 1</ambient>"
                    "<diffuse>0.42 0.42 0.45 1</diffuse></material></visual>"
                    "</link></model>"
                )
                building = ET.fromstring(building_xml)
                root.find("world").append(building)
            sdf_path = a.out_worlds / f"{wid}.sdf"
            with sdf_path.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(ET.tostring(root.getroot(), encoding="unicode") + "\n")
            if case == "fixed_wing_oblique":
                runtime["vehicle"] = {
                    "model": "px4_fixed_wing_standard",
                    "airframe_profile_id": "px4_fixed_wing_gazebo_v1",
                    "airframe": "rc_cessna_runway",
                    "takeoff_method": "runway",
                    "spawn_id": f"{wid}_runway",
                    "spawn_pose": {
                        "x_east_m": 0.0,
                        "y_north_m": 0.0,
                        "z_up_m": 0.2,
                        "roll_rad": 0.0,
                        "pitch_rad": 0.0,
                        "yaw_rad": 0.0,
                    },
                }
                telemetry = [s for s in runtime["sensors"] if s["kind"] == "mavlink_telemetry"]
                runtime["sensors"] = (
                    [
                        {
                            "sensor_id": sid,
                            "kind": kind,
                            "stream_uri": f"gazebo://{wid}/{stream}",
                            "calibration_id": cal,
                        }
                        for sid, kind, stream, cal in FIXED_WING_SENSORS[:2]
                    ]
                    + telemetry
                    + [
                        {
                            "sensor_id": sid,
                            "kind": kind,
                            "stream_uri": f"gazebo://{wid}/{stream}",
                            "calibration_id": cal,
                        }
                        for sid, kind, stream, cal in FIXED_WING_SENSORS[2:]
                    ]
                )
            else:
                runtime["vehicle"]["spawn_pose"].update(
                    {
                        "x_east_m": 0.0,
                        "y_north_m": 0.0,
                        "z_up_m": 0.2,
                        "pitch_rad": 0.0,
                        "yaw_rad": 0.0,
                    }
                )
            for name, value in channels.items():
                (sc / f"{wid}.{name}.json").write_text(
                    json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            scenario["channels"] = {
                name: {"path": f"{wid}.{name}.json", "sha256": canonical_sha256(value)}
                for name, value in channels.items()
            }
            (sc / f"{wid}.scenario.json").write_text(
                json.dumps(scenario, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            entry["target_combo"] = {
                "slug": target["asset_slug"],
                "category": target["class"],
                "color": target.get("color"),
            }
        held_index = json.loads(
            (ROOT / "configs/scenarios/generated-v2/index.json").read_text(encoding="utf-8")
        )
        doc["held_out_target_combos"] = held_index["held_out_target_combos"]
        (a.out_scenarios / "index.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    # Preserve the human-authored world table and add a machine-generated summary beside it.
    summary = {
        "schema_version": 1,
        "config": a.config.name,
        "generated_index": "index.json",
        "count": doc["count"],
        "seed": doc["seed"],
        "world_prefix": cfg["world_prefix"],
        "split_counts": {
            s: sum(e["split"] == s for e in doc["worlds"]) for s in ("train", "val", "test")
        },
    }
    (a.out_scenarios / "generation-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
