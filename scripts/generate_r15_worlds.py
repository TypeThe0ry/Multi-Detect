#!/usr/bin/env python3
"""Generate the r15 Fuel-appearance train/val world set."""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from multidetect.autonomy.serialization import canonical_sha256  # noqa: E402
from multidetect.camera_defaults import DEFAULT_CAMERA_INFO  # noqa: E402
from multidetect.sim_teacher.spawn_clearance import (  # noqa: E402
    spawn_clearance_report,
    teacher_intent_clearance_altitude,
    teacher_intent_clearance_route_points,
)
from multidetect.simulator.task_semantics import fuel_task_texts  # noqa: E402
from multidetect.simulator.world_gen import (  # noqa: E402
    FIXED_WING_SENSORS,
    build_teacher_actuator_state,
    generate_fuel_worlds,  # noqa: E402
)


def r21id_r4_templates():
    designs = {
        "pickup": [("8-12", 10), ("12-16", 14), ("8-12", 10), ("12-16", 14), ("24-32", 28), (">=32", 40)],
        "suv": [("8-12", 10), ("12-16", 14), ("8-12", 10), ("12-16", 14), ("24-32", 28), (">=32", 40)],
        "postbox": [("8-12", 10), ("12-16", 14), ("24-32", 28), (">=32", 40)],
        "person": [("24-32", 28), (">=32", 40), ("24-32", 28), (">=32", 40)],
        "ambulance": [("24-32", 28), (">=32", 40), ("24-32", 28), (">=32", 40)],
    }
    groups = (
        ("pickup", ["suv"], 18), ("suv", ["pickup"], 24),
        ("postbox", ["hatchback"], 1), ("walking_person", ["postbox"], 1.5),
        ("ambulance", ["suv"], 24),
    )
    val_positions = {(0, 5), (1, 5), (2, 3), (3, 3), (4, 3), (1, 4)}
    rows = []
    for group, (target, confusers, altitude) in enumerate(groups):
        key = "person" if target == "walking_person" else target
        for i, (size_bin, px) in enumerate(designs[key]):
            rows.append({
                "case": "identity_multiview", "target": target,
                "confusers": confusers, "forced_distractors": ["electrical_box"],
                "appearance": "morning_low_sun" if key == "postbox" else ("noon_clear", "overcast_flat", "haze", "evening_low_sun", "dusk_dim", "morning_low_sun")[i],
                "height_m": altitude if i < 2 or group >= 2 else (32 if group < 3 else altitude),
                "target_short_side_px": size_bin, "target_short_side_px_value": px,
                "split": "val" if (group, i) in val_positions else "train",
                "task_pair": key in {"pickup", "suv"},
                "task_override": "suv" if key == "ambulance" else None,
                "geometry_reference": {"pickup": "w0002/w0001", "suv": "w0010/w0009", "postbox": "w0026"}.get(key),
            })
    return rows


def r21id_r5_templates():
    """Train/val identity worlds for the four r21id-r5 hard-reject directions."""
    directions = (
        ("suv", "postbox"),
        ("walking_person", "jersey_barrier"),
        ("stop_sign", "jersey_barrier"),
        ("hatchback", "postbox"),
    )
    bands = (("8-12", 10), ("12-16", 13), ("16-24", 16))
    appearances = ("noon_clear", "overcast_flat", "morning_low_sun", "haze", "evening_low_sun")
    rows = []
    for direction_index, (target, confuser) in enumerate(directions):
        for band_index, (size_bin, px) in enumerate(bands):
            height = (
                (18.0, 24.0, 30.0)[band_index]
                if target in {"suv", "hatchback"}
                else (12.0, 10.0, 6.0)[band_index]
            )
            rows.append({
                "case": "identity_multiview",
                "target": target,
                "confusers": [confuser],
                "forced_distractors": ["electrical_box"],
                "appearance": appearances[(direction_index + band_index) % len(appearances)],
                "height_m": height,
                "target_short_side_px": size_bin,
                "target_short_side_px_value": px,
                "split": "val" if band_index == 2 else "train",
                "r5_direction": f"{target}->{confuser}",
                "geometry_reference": None,
            })
    return rows


def r21id_r7_templates():
    """Diverse train/val identity worlds for the r21id-r7 hard-reject families."""
    bands = (("8-12", 10), ("12-16", 13), ("16-24", 19), ("24-32", 28), ("12-16", 14))
    appearances = ("noon_clear", "overcast_flat", "morning_low_sun", "haze", "evening_low_sun")
    rows = []
    families = (
        # ``generate_fuel_worlds`` keys confusers by target slug, so each
        # repeated target slug keeps one stable paired model.  The two families
        # deliberately use the catalog's postbox and mailbox models.
        ("suv", ("postbox",) * 5, (18.0, 22.0, 26.0, 30.0, 20.0), "suv->postbox"),
        ("hatchback", ("mailbox",) * 5, (16.0, 20.0, 24.0, 28.0, 18.0), "car->postbox"),
        ("walking_person", ("jersey_barrier",) * 5, (6.0, 8.0, 10.0, 12.0, 7.0), "person->jersey_barrier"),
    )
    target_variants = {
        "suv": ("suv",) * 5,
        "hatchback": ("hatchback", "hatchback_blue", "hatchback_red", "hatchback", "hatchback_blue"),
        "walking_person": ("walking_person", "male_visitor", "walking_person", "male_visitor", "walking_person"),
    }
    occlusions = ("building", "guardrail", "pipe_line", "none", "building")
    for family_index, (family, confusers, heights, direction) in enumerate(families):
        for i, (band, px) in enumerate(bands):
            rows.append({
                "case": "identity_multiview",
                "target": target_variants[family][i],
                "confusers": [confusers[i]],
                "forced_distractors": ["electrical_box"],
                "appearance": appearances[i],
                "height_m": heights[i],
                "target_short_side_px": band,
                "target_short_side_px_value": px,
                "split": "val" if i == 4 else "train",
                "r7_family": direction,
                "occlusion": occlusions[(family_index + i) % len(occlusions)],
                "geometry_reference": None,
            })
    return rows


def r21id_r8_templates():
    """Replacement and focused hard-reject worlds selected after the r7 audit."""
    rows = []
    specs = (
        ("male_visitor", "jersey_barrier", 8.0, "12-16", 13, "morning_low_sun", "person->jersey_barrier"),
        ("hatchback", "mailbox", 20.0, "12-16", 14, "overcast_flat", "car->mailbox"),
        ("suv", "postbox", 24.0, "16-24", 19, "haze", "suv->postbox"),
    )
    for target, confuser, height, band, px, appearance, direction in specs:
        rows.append({
            "case": "identity_multiview",
            "target": target,
            "confusers": [confuser],
            "forced_distractors": ["electrical_box"],
            "appearance": appearance,
            "height_m": height,
            "target_short_side_px": band,
            "target_short_side_px_value": px,
            "split": "train",
            "r8_family": direction,
            "occlusion": "none",
            "geometry_reference": None,
        })
    return rows


def projected_short_edge_px(asset, ground_distance_m, altitude_m, *, visible_face: bool = False):
    dimensions = [float(value) for value in asset["measured_size_m"]]
    # The r5 stop-sign face is bounded by X/Z; preserve the historical X/Y
    # default for every existing profile and caller.
    short_edge_m = min(dimensions[0], dimensions[2]) if visible_face else min(dimensions[0], dimensions[1])
    return DEFAULT_CAMERA_INFO["fx"] * short_edge_m / math.hypot(ground_distance_m, altitude_m)


def _translate_sdf_scene(world_path: str | Path, east_m: float, north_m: float) -> None:
    root = ET.parse(world_path)
    world = root.find("world")
    if world is None:
        raise ValueError(f"world SDF has no <world>: {world_path}")
    for model in world.findall("model"):
        name = model.get("name", "")
        if name == "ground_plane" or name.startswith(("px4_", "x500_", "rc_cessna")):
            continue
        pose = model.find("pose")
        if pose is None:
            continue
        values = pose.text.split() if pose.text else []
        if len(values) < 2:
            continue
        values[0] = f"{float(values[0]) + east_m:.3f}"
        values[1] = f"{float(values[1]) + north_m:.3f}"
        pose.text = " ".join(values)
    Path(world_path).write_text(
        ET.tostring(root.getroot(), encoding="unicode") + "\n",
        encoding="utf-8",
        newline="\n",
    )


def r21id_r4_scene_shift(
    world_path: str | Path,
    *,
    target_east_m: float,
    target_north_m: float,
    observation: Mapping[str, object],
    step_m: float = 0.5,
) -> float:
    """Find the smallest +target-ray scene translation accepted at origin spawn."""
    if step_m <= 0 or not math.isfinite(step_m):
        raise ValueError("step_m must be finite and positive")
    target_radius = math.hypot(float(target_east_m), float(target_north_m))
    if target_radius <= 0:
        raise ValueError("r21id-r4 target must be away from the spawn origin")
    altitude = teacher_intent_clearance_altitude(observation)
    if altitude is None:
        raise ValueError("r21id-r4 observation must declare camera_altitude_m")
    route_points = teacher_intent_clearance_route_points(observation)
    radial_east = float(target_east_m) / target_radius
    radial_north = float(target_north_m) / target_radius

    with tempfile.TemporaryDirectory() as temporary:
        for index in range(401):
            shift = index * step_m
            candidate = Path(temporary) / Path(world_path).name
            candidate.write_bytes(Path(world_path).read_bytes())
            _translate_sdf_scene(candidate, shift * radial_east, shift * radial_north)
            shifted_route = [
                {
                    **point,
                    "x_east_m": float(point.get("x_east_m", 0.0)) + shift * radial_east,
                    "y_north_m": float(point.get("y_north_m", 0.0)) + shift * radial_north,
                }
                for point in route_points
            ]
            report = spawn_clearance_report(
                candidate,
                {"x_east_m": 0.0, "y_north_m": 0.0, "z_up_m": 0.2},
                climb_altitude_m=altitude,
                route_points=shifted_route,
            )
            if report["clear"]:
                return round(shift, 3)
    raise ValueError(f"{Path(world_path).stem}: no spawn-clearance shift found")


def _shift_truth_scene(truth: dict, east_m: float, north_m: float) -> None:
    latitude0 = 1.352100
    longitude0 = 103.819800
    longitude_scale = 111_320.0 * math.cos(math.radians(latitude0))
    for target in truth.get("targets", []):
        pose = target.get("pose")
        if not isinstance(pose, dict):
            continue
        pose["x_east_m"] = float(pose.get("x_east_m", 0.0)) + east_m
        pose["y_north_m"] = float(pose.get("y_north_m", 0.0)) + north_m
        position = target.get("position")
        if not isinstance(position, dict):
            continue
        position["latitude_deg"] = round(
            float(position.get("latitude_deg", latitude0)) + north_m / 111_132.0,
            8,
        )
        position["longitude_deg"] = round(
            float(position.get("longitude_deg", longitude0)) + east_m / longitude_scale,
            8,
        )


def _shift_observation_views(observation: dict, east_m: float, north_m: float) -> None:
    for view in observation.get("views", []):
        point = view.get("observation_waypoint") if isinstance(view, dict) else None
        if not isinstance(point, dict):
            continue
        point["x_east_m"] = round(float(point.get("x_east_m", 0.0)) + east_m, 3)
        point["y_north_m"] = round(float(point.get("y_north_m", 0.0)) + north_m, 3)


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
        "--gimbal-sidecar",
        action="store_true",
        help="write a sidecar and index reference; teacher-intent profiles also link it",
    )
    p.add_argument("--gimbal-yaw-rad", type=float)
    p.add_argument("--gimbal-roll-rad", type=float)
    p.add_argument(
        "--stabilization-mode",
        choices=("follow-body", "lock-horizon", "follow-yaw"),
    )
    p.add_argument(
        "--propeller-spin-rad-s",
        type=float,
        nargs=4,
        metavar=("R1", "R2", "R3", "R4"),
        help="four signed rotor rates in rad/s, in generator rotor order",
    )
    p.add_argument(
        "--profile",
        choices=("default", "r20", "r21-identity", "r21id-r3", "r21id-r4", "r21id-r5", "r21id-r7", "r21id-r8"),
        default="default",
        help="optional hard-case profile; default preserves the r15 config",
    )
    a = p.parse_args(argv)
    sidecar_overrides = (
        a.gimbal_yaw_rad,
        a.gimbal_roll_rad,
        a.stabilization_mode,
        a.propeller_spin_rad_s,
    )
    if not a.gimbal_sidecar and any(value is not None for value in sidecar_overrides):
        p.error("gimbal sidecar values require --gimbal-sidecar")
    actuator_state = None
    if a.gimbal_sidecar:
        try:
            actuator_state = build_teacher_actuator_state(
                gimbal_yaw_rad=0.0 if a.gimbal_yaw_rad is None else a.gimbal_yaw_rad,
                gimbal_roll_rad=0.0 if a.gimbal_roll_rad is None else a.gimbal_roll_rad,
                stabilization_mode=a.stabilization_mode or "lock-horizon",
                propeller_spin_rad_s=a.propeller_spin_rad_s or (0.0, 0.0, 0.0, 0.0),
            )
        except ValueError as exc:
            p.error(str(exc))
    cfg = json.loads(a.config.read_text(encoding="utf-8"))
    if a.profile in ("r20", "r21-identity", "r21id-r3", "r21id-r4", "r21id-r5", "r21id-r7", "r21id-r8"):
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
                    "height_m": layers[i % len(layers)][0], "distance_m": layers[i % len(layers)][1],
                    "target_short_side_px": layers[i % len(layers)][2],
                    "split": "train" if i < 6 else ("val" if i == 6 else "test"),
                }
                for cls in classes for i in range(8)
            )
        elif a.profile == "r21id-r4":
            templates = tuple(r21id_r4_templates())
        elif a.profile == "r21id-r5":
            templates = tuple(r21id_r5_templates())
        elif a.profile == "r21id-r7":
            templates = tuple(r21id_r7_templates())
        elif a.profile == "r21id-r8":
            templates = tuple(r21id_r8_templates())
        elif a.profile == "r21id-r3":
            # Six independently seeded worlds per identity class. The final ten
            # entries are held-out combinations from the frozen identity set.
            classes = (
                ("pickup", "suv"), ("suv", "pickup"),
                ("walking_person", "postbox"),
                ("postbox", "hatchback"), ("mailbox", "walking_person"),
                ("hatchback", "postbox"), ("fire_hydrant", "backpack"),
                ("backpack", "fire_hydrant"), ("construction_cone", "backpack"),
            )
            layers = ((18, 20, [13, 20]), (24, 30, [21, 32]), (32, 40, [13, 20]),
                      (12, 20, [8, 12]), (18, 30, [13, 20]), (24, 40, [21, 32]))
            templates = tuple(
                {
                    "case": "identity_multiview", "target": target,
                    "confusers": [confuser], "forced_distractors": ["electrical_box"],
                    "appearance": ("noon_clear", "overcast_flat", "morning_low_sun")[i % 3],
                    "height_m": layers[i % len(layers)][0], "distance_m": layers[i % len(layers)][1],
                    "target_short_side_px": layers[i % len(layers)][2],
                    "view_mix": ["front", "oblique", "edge_truncated", "recovery"],
                    "split": ("train" if i < 4 else ("val" if i == 4 else "test")),
                }
                for target, confuser in classes for i in range(8)
            )
            # Keep ten test worlds while retaining at least one test example for
            # each confusable direction used by the identity eval.
            heldout_targets = {"pickup", "suv", "walking_person", "postbox"}
            test_indices = {
                i for i, template in enumerate(templates)
                if template["target"] in heldout_targets and (i % 8) < 3
            }
            val_indices = {i for i in range(len(templates)) if i not in test_indices and (i % 8) == 4}
            # Exactly four validation worlds, with the remainder in train.
            val_indices = set(sorted(val_indices)[:4])
            for i, template in enumerate(templates):
                template["split"] = "test" if i in test_indices else ("val" if i in val_indices else "train")
        cfg.update(
            {
                "world_prefix": "gen_r21id_r8_w" if a.profile == "r21id-r8" else ("gen_r21id_r7_w" if a.profile == "r21id-r7" else ("gen_r21id_r5_w" if a.profile == "r21id-r5" else ("gen_r21id_r4_w" if a.profile == "r21id-r4" else ("gen_r21id_r3_w" if a.profile == "r21id-r3" else ("gen_r21id_w" if a.profile == "r21-identity" else "gen_r20_w"))))),
                "seed": 20261122 if a.profile == "r21id-r8" else (20261107 if a.profile == "r21id-r7" else (20261102 if a.profile == "r21id-r5" else (20261031 if a.profile == "r21id-r4" else (20261028 if a.profile == "r21id-r3" else (20261025 if a.profile == "r21-identity" else 20261024))))),
                "count": len(templates),
                "split_ratios": [1.0, 0.0, 0.0] if a.profile == "r21id-r8" else ([12 / 15, 3 / 15, 0.0] if a.profile == "r21id-r7" else ([8 / 12, 4 / 12, 0.0] if a.profile == "r21id-r5" else ([18 / 24, 6 / 24, 0.0] if a.profile == "r21id-r4" else ([56 / 72, 4 / 72, 12 / 72] if a.profile == "r21id-r3" else ([0.75, 0.125, 0.125] if a.profile == "r21-identity" else [0.8, 0.2, 0.0]))))),
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
        summary = {
            "count": cfg["count"],
            "seed": cfg["seed"],
            "world_prefix": cfg["world_prefix"],
            "splits": {
                s: sum(w["split"] == s for w in cfg["worlds"])
                for s in ("train", "val", "test")
            },
        }
        if actuator_state is not None:
            summary["actuator_state_sidecar"] = actuator_state
        print(json.dumps(summary, ensure_ascii=False))
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
        actuator_state=actuator_state,
    )
    if a.profile in ("r20", "r21-identity", "r21id-r3", "r21id-r4", "r21id-r5", "r21id-r7", "r21id-r8"):
        doc["profile"] = a.profile
        for entry, world in zip(doc["worlds"], cfg["worlds"], strict=True):
            entry["split"] = world.get("split", entry.get("split", "train"))
            entry["hard_case_profile"] = world["case"]
            entry["paired_confusers"] = list(world.get("confusers", ()))
            entry["view_geometry"] = {"paired_same_world": True}
            if a.profile in ("r21-identity", "r21id-r3", "r21id-r4", "r21id-r5", "r21id-r7", "r21id-r8"):
                entry["hard_case_profile"] = "identity_multiview"
                if a.profile not in ("r21id-r4", "r21id-r5", "r21id-r7", "r21id-r8"):
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
            if a.profile in ("r21id-r4", "r21id-r5", "r21id-r7", "r21id-r8"):
                catalog = json.loads((a.catalog or ROOT / cfg["catalog"]).read_text(encoding="utf-8"))
                asset = next(x for x in catalog["assets"] if x["slug"] == target["asset_slug"])
                dimensions = [float(value) for value in asset["measured_size_m"]]
                short_edge = min(dimensions[0], dimensions[2]) if asset.get("slug") == "stop_sign" else min(dimensions[0], dimensions[1])
                altitude = float(world["height_m"])
                slant = DEFAULT_CAMERA_INFO["fx"] * short_edge / float(world["target_short_side_px_value"])
                tx, ty, tz, angle = math.sqrt(max(1.0, slant * slant - altitude * altitude)), 0.0, 0.0, -0.42
                if a.profile in ("r21id-r5", "r21id-r7", "r21id-r8"):
                    # Point each teacher station at the ground target centre;
                    # r4's fixed pitch is too steep for the larger r5 standoffs.
                    angle = -math.atan2(altitude, max(tx, 1e-6))
                entry["view_geometry"].update({
                    "camera_altitude_m": altitude,
                    "ground_distance_m": round(tx, 3),
                    "slant_distance_m": round(math.hypot(tx, altitude), 3),
                    "target_short_side_px": world["target_short_side_px"],
                "target_short_side_px_estimate": round(
                    projected_short_edge_px(
                        asset, tx, altitude,
                        visible_face=(
                            a.profile in ("r21id-r5", "r21id-r7", "r21id-r8")
                            and target["asset_slug"] == "stop_sign"
                        ),
                    ), 2
                ),
                    "camera_intrinsics": {k: DEFAULT_CAMERA_INFO[k] for k in ("width", "height", "fx", "fy", "cx", "cy")},
                    "camera_pitch_rad": angle,
                    "geometry_reference": world["geometry_reference"],
                    "visible_fraction": [0.45, 0.65, 0.85, 1.0],
                    "occlusion_types": ["none", "building", "guardrail", "pipe_line"],
                    "view_angles_deg": [0, 35, 90, 145],
                    "identity_sequence": ["front", "oblique", "edge_truncated", "recovery"],
                    "target_id": entry["true_target_id"],
                })
            elif a.profile in ("r21-identity", "r21id-r3"):
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
                tx, ty, tz, altitude, angle = 60.0, 35.0, 0.0, 30.0, -0.18
                entry["view_geometry"] = {
                    "fixed_wing": True,
                    "slant_range_m": 91,
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
            # The generated target is placed at the requested ground-distance
            # ring from home.  Observation waypoints are camera positions, so
            # they must stay on that ring instead of being the target pose.
            waypoint_east = 0.0
            waypoint_north = 0.0
            observation_intent = {
                "camera_altitude_m": altitude,
                "camera_pitch_rad": angle,
                "observation_waypoint": {
                    "x_east_m": waypoint_east,
                    "y_north_m": waypoint_north,
                    "z_up_m": 0.0,
                },
            }
            if case == "fixed_wing_oblique":
                observation_intent["orbit_radius_m"] = 80.0
                heading = math.atan2(tx, ty)
                center_north = ty - 80.0 * math.sin(heading)
                center_east = tx + 80.0 * math.cos(heading)
                observation_intent["observation_waypoint"] = {
                    "x_east_m": round(center_east, 3),
                    "y_north_m": round(center_north, 3),
                    "z_up_m": 0.0,
                }
                observation_intent["ground_distance_m"] = 80.0
            if a.profile in ("r21-identity", "r21id-r3", "r21id-r4", "r21id-r5", "r21id-r7", "r21id-r8"):
                target_radius = math.hypot(tx, ty)
                radial = (tx / target_radius, ty / target_radius) if target_radius else (1.0, 0.0)
                perpendicular = (-radial[1], radial[0])
                views = []
                for view_name, angle_deg in zip(
                    ("front", "oblique", "edge_truncated", "recovery"), (180.0, 145.0, 90.0, 35.0), strict=True
                ):
                    angle_rad = math.radians(angle_deg)
                    offset_e = target_radius * (math.cos(angle_rad) * radial[0] + math.sin(angle_rad) * perpendicular[0])
                    offset_n = target_radius * (math.cos(angle_rad) * radial[1] + math.sin(angle_rad) * perpendicular[1])
                    views.append({
                        "view": view_name,
                        "observation_waypoint": {
                            "x_east_m": round(tx - offset_e, 3),
                            "y_north_m": round(ty - offset_n, 3),
                            "z_up_m": 0.0,
                        },
                    })
                observation_intent["observation_frames"] = 8
                if (
                    a.profile in ("r21id-r5", "r21id-r7", "r21id-r8")
                    or target["asset_slug"] in {"pickup", "suv"}
                    and world["target_short_side_px"] in {"8-12", "12-16"}
                ):
                    # These small r4 targets are beyond the expert teacher's
                    # historical 80 m prior. Align its search gate with design.
                    observation_intent["max_detection_range_m"] = round(
                        math.hypot(tx, altitude) + 5.0, 3
                    )
                observation_intent["views"] = views
            if a.profile in ("r21id-r4", "r21id-r5", "r21id-r7", "r21id-r8"):
                runtime["vehicle"]["spawn_pose"].update({
                    "x_east_m": 0.0, "y_north_m": 0.0, "z_up_m": 0.2,
                    "roll_rad": 0.0, "pitch_rad": 0.0, "yaw_rad": 0.0,
                })
                if world.get("task_pair"):
                    texts = {
                        "pickup": ("找到皮卡并记录位置", "Find the pickup and record its position."),
                        "suv": ("找到 SUV 并记录位置", "Find the SUV and record its position."),
                    }
                    requested = target["asset_slug"]
                    other = "suv" if requested == "pickup" else "pickup"
                    entry["task_text_variants"] = [
                        {"requested_category": slug, "task_text": texts[slug][0], "task_text_en": texts[slug][1]}
                        for slug in (requested, other)
                    ]
                if a.profile in ("r21id-r5", "r21id-r7", "r21id-r8"):
                    confuser_slug = world["confusers"][0]
                    confuser = next(
                        item for item in truth["targets"]
                        if item.get("confuser") and item.get("asset_slug") == confuser_slug
                    )
                    target_text, target_text_en = fuel_task_texts(target)
                    if target["asset_slug"] == "suv":
                        target_text, target_text_en = "找到 SUV 并记录位置", "Find the SUV and record its position."
                    confuser_text, confuser_text_en = fuel_task_texts(confuser)
                    entry["task_text"] = target_text
                    entry["task_text_en"] = target_text_en
                    entry["find_task_text"] = target_text
                    entry["find_task_text_en"] = target_text_en
                    entry["task_text_variants"] = [
                        {
                            "requested_category": target["asset_slug"],
                            "task_text": target_text,
                            "task_text_en": target_text_en,
                        },
                        {
                            "requested_category": confuser_slug,
                            "task_text": confuser_text,
                            "task_text_en": confuser_text_en,
                        },
                    ]
                if world.get("task_override"):
                    entry["task_text"] = "找到 SUV 并记录位置"
                    entry["task_text_en"] = "Find the SUV and record its position."
                    entry["find_task_text"] = entry["task_text"]
                    entry["find_task_text_en"] = entry["task_text_en"]
                    entry["task_text_variants"] = [{
                        "requested_category": "suv", "task_text": entry["task_text"],
                        "task_text_en": entry["task_text_en"],
                    }]
                entry["hard_case_profile"] = "identity_hard_negative"
                entry["paired_confusers"] = list(world.get("confusers", ()))
                if a.profile == "r21id-r7":
                    entry["r7_family"] = world["r7_family"]
                    entry["occlusion_profile"] = world["occlusion"]
                if a.profile == "r21id-r8":
                    entry["r8_family"] = world["r8_family"]
                    entry["occlusion_profile"] = world["occlusion"]
            teacher_intent = {
                "kind": "find_and_observe",
                "observation": observation_intent,
            }
            if "actuator_state_sidecar" in entry:
                teacher_intent["actuator_state_sidecar"] = dict(
                    entry["actuator_state_sidecar"]
                )
            spawn_pose = {
                "x_east_m": 0.0,
                "y_north_m": 0.0,
                "z_up_m": 0.2,
                "roll_rad": 0.0,
                "pitch_rad": 0.0,
                "yaw_rad": 0.0,
            }
            root = ET.parse(a.out_worlds / f"{wid}.sdf")
            model = root.find(f".//model[@name='{target['target_id']}']")
            if model is None:
                raise RuntimeError(f"{wid}: target SDF model not found")
            model.find(
                "pose"
            ).text = f"{tx:.3f} {ty:.3f} {tz + float(target['size_m'][2]) / 2:.3f} 0 0 0"
            if a.profile in ("r21id-r3", "r21id-r4", "r21id-r5", "r21id-r7", "r21id-r8") and world.get("confusers"):
                pair_slug = world.get("confusers", [None])[0]
                pair = next((row for row in truth["targets"] if row.get("confuser") and row["asset_slug"] == pair_slug), None)
                if pair is not None:
                    pair_x, pair_y = tx + 3.5, ty
                    pair["pose"].update({"x_east_m": pair_x, "y_north_m": pair_y, "z_up_m": 0.0})
                    pair["position"].update({"altitude_m": 0.0, "latitude_deg": round(1.352100 + pair_y / 111_132.0, 8), "longitude_deg": round(103.819800 + pair_x / (111_320.0 * math.cos(math.radians(1.352100))), 8)})
                    pair_model = root.find(".//model[@name='" + pair["target_id"] + "']")
                    if pair_model is None:
                        raise RuntimeError(f"{wid}: paired {pair_slug} SDF model not found")
                    pair_model.find("pose").text = f"{pair_x:.3f} {pair_y:.3f} {float(pair['size_m'][2]) / 2:.3f} 0 0 0"
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
            if case == "hydrant_building_occlusion_truncation" or a.profile in ("r21-identity", "r21id-r3", "r21id-r4", "r21id-r5", "r21id-r7", "r21id-r8"):
                # Keep the occluder on the target ray while placing its nearest
                # footprint edge outside the multirotor takeoff clearance ring.
                # The old midpoint (tx/2, ty/2) put the 12x8 m building over
                # the home position for close r21 identity views (for example
                # target=(12, 0) -> building=(6, 0)).
                # r21 identity views occupy a full ring around the target.  A
                # building at twice the target radius lands on the front view
                # waypoint; keep its near footprint beyond every view's 15 m
                # clearance ring while remaining on the target ray.
                target_radius = math.hypot(tx, ty)
                if a.profile in ("r21-identity", "r21id-r3", "r21id-r4", "r21id-r5", "r21id-r7", "r21id-r8"):
                    building_radius = 2.0 * target_radius + 24.0
                    scale = building_radius / max(target_radius, 1e-6)
                else:
                    scale = 2.0
                building_x, building_y = (scale * tx, scale * ty)
                occlusion = world.get("occlusion", "building") if a.profile in ("r21id-r7", "r21id-r8") else "building"
                if occlusion == "none":
                    occluder_xml = ""
                elif occlusion == "guardrail":
                    occluder_xml = (
                        f'<model name="{a.profile.replace("-", "_")}_guardrail_occluder"><static>true</static>'
                        f'<pose>{building_x:.3f} {building_y:.3f} 0.9 0 0 0.25</pose><link name="body">'
                        '<collision name="collision"><geometry><box><size>12 0.6 1.8</size></box></geometry></collision>'
                        '<visual name="visual"><geometry><box><size>12 0.6 1.8</size></box></geometry>'
                        '<material><ambient>0.48 0.48 0.18 1</ambient><diffuse>0.62 0.62 0.22 1</diffuse></material></visual>'
                        '</link></model>'
                    )
                elif occlusion == "pipe_line":
                    occluder_xml = (
                        f'<model name="{a.profile.replace("-", "_")}_pipe_line_occluder"><static>true</static>'
                        f'<pose>{building_x:.3f} {building_y:.3f} 5 0 0 0</pose><link name="body">'
                        '<collision name="collision"><geometry><cylinder><radius>0.45</radius><length>10</length></cylinder></geometry></collision>'
                        '<visual name="visual"><geometry><cylinder><radius>0.45</radius><length>10</length></cylinder></geometry>'
                        '<material><ambient>0.24 0.28 0.30 1</ambient><diffuse>0.30 0.34 0.36 1</diffuse></material></visual>'
                        '</link></model>'
                    )
                else:
                    occluder_xml = (
                        f'<model name="{a.profile.replace("-", "_")}_building_occluder"><static>true</static>'
                        f"<pose>{building_x:.3f} {building_y:.3f} 6 0 0 0</pose><link name=\"body\">"
                        '<collision name="collision"><geometry><box><size>12 8 12</size></box></geometry></collision>'
                        '<visual name="visual"><geometry><box><size>12 8 12</size></box></geometry>'
                        '<material><ambient>0.35 0.35 0.38 1</ambient><diffuse>0.42 0.42 0.45 1</diffuse></material></visual>'
                        '</link></model>'
                    )
                if occluder_xml:
                    root.find("world").append(ET.fromstring(occluder_xml))
            sdf_path = a.out_worlds / f"{wid}.sdf"
            with sdf_path.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(ET.tostring(root.getroot(), encoding="unicode") + "\n")
            scene_shift_m = 0.0
            if a.profile in ("r21id-r4", "r21id-r5", "r21id-r7", "r21id-r8"):
                target_radius = math.hypot(tx, ty)
                radial_east = tx / target_radius
                radial_north = ty / target_radius
                scene_shift_m = r21id_r4_scene_shift(
                    sdf_path,
                    target_east_m=tx,
                    target_north_m=ty,
                    observation=observation_intent,
                )
                shift_east = scene_shift_m * radial_east
                shift_north = scene_shift_m * radial_north
                if scene_shift_m:
                    _translate_sdf_scene(sdf_path, shift_east, shift_north)
                    _shift_truth_scene(truth, shift_east, shift_north)
                    _shift_observation_views(observation_intent, shift_east, shift_north)
                    shifted_target_radius = math.hypot(tx + shift_east, ty + shift_north)
                    geometry = entry["view_geometry"]
                    old_slant = geometry.get("slant_distance_m")
                    geometry["ground_distance_m"] = round(shifted_target_radius, 3)
                    geometry["slant_distance_m"] = round(
                        math.hypot(shifted_target_radius, altitude), 3
                    )
                    if "max_detection_range_m" in observation_intent and old_slant is not None:
                        margin = float(observation_intent["max_detection_range_m"]) - float(old_slant)
                        observation_intent["max_detection_range_m"] = round(
                            geometry["slant_distance_m"] + margin, 3
                        )
                if scene_shift_m:
                    entry["view_geometry"]["scene_shift_m"] = scene_shift_m
                final_report = spawn_clearance_report(
                    sdf_path,
                    spawn_pose,
                    climb_altitude_m=teacher_intent_clearance_altitude(observation_intent),
                    route_points=teacher_intent_clearance_route_points(observation_intent),
                )
                if not final_report["clear"]:
                    raise RuntimeError(f"{wid}: final SDF spawn clearance failed: {final_report}")
            if case == "fixed_wing_oblique":
                runtime["vehicle"] = {
                    "model": "px4_fixed_wing_standard",
                    "airframe_profile_id": "px4_fixed_wing_gazebo_v1",
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
                teacher_intent["airframe"] = "rc_cessna_runway"
                teacher_intent["takeoff_method"] = "runway"
            else:
                runtime["vehicle"]["spawn_pose"].update(
                    spawn_pose
                )
            # Teacher geometry is deliberately a sidecar.  It is consumed by
            # the collector only and must not widen the strict runtime-input
            # contract or change its provenance hash.
            (sc / f"{wid}.teacher_intent.json").write_text(
                json.dumps(teacher_intent, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
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
