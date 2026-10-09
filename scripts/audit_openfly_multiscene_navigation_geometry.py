"""Audit the extracted original82 RGB/pose geometry, not a training dataset.

Reuses the previously reviewed primitive conversion and visual duplicate rule.
Only the three frozen original TRAIN routes are admitted. Future poses remain
offline supervision checks, never actor observations. No model, network, GPU,
partition promotion or flight authority is created.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = "artifacts/training/openfly-multiscene-embedded-images-20261008-r1/receipt.json"
RECEIPT_SHA = "095eea5eed5136a07e89986bd971632392359f1d1d05f6b731b4bba0ec910a04"
PLAN_SHA = "b047040485da2a1cd5fd73f69e671fd73fc555a309d1113edac29d704c873740"
CONTRACT = "multidetect.openfly-original82-geometry-visual-audit-v1"
ROUTES = ((139, "env_airsim_26", 23), (220, "env_airsim_16", 17), (90, "env_airsim_23", 42))
FALSE_CLAIMS = (
    "model_loaded",
    "GPU_used",
    "optimizer_performed",
    "training_ready",
    "samples_created",
    "independent_holdout_verified",
    "generalization_verified",
    "old_dataset_merged_or_modified",
    "candidate_promoted",
    "execution_authority",
    "physical_performance_claimed",
    "hil_real_evidence",
    "outdoor_gnss_evidence",
    "final_release_allowed",
)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")


def record(path):
    path = Path(path).resolve(strict=True)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def verified(row, *, cap=128 * 1024 * 1024):
    path = Path(row["path"])
    path = (path if path.is_absolute() else ROOT / path).resolve(strict=True)
    if not path.is_relative_to(ROOT) or not path.is_file():
        raise ValueError("Audit source escaped the repository")
    if type(row["bytes"]) is not int or not 0 < row["bytes"] <= cap:
        raise ValueError("Audit source exceeded its byte bound")
    actual = record(path)
    if (actual["bytes"], actual["sha256"]) != (row["bytes"], row["sha256"]):
        raise ValueError("Audit source bytes differ from frozen extraction")
    return path


def read_small(row):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError(f"Nonfinite JSON constant: {value}")

    return json.loads(
        verified(row, cap=2 * 1024 * 1024).read_bytes(),
        object_pairs_hook=pairs,
        parse_constant=constant,
    )


def metric_targets(actions, metadata, derive):
    """Require all metadata rows and every immediate original successor."""
    if len(actions) != len(metadata) or not actions:
        raise ValueError("Raw pose and metadata lengths differ")
    for index, (action, row) in enumerate(zip(actions, metadata, strict=True)):
        if (
            type(row["frame_index"]) is not int
            or row["frame_index"] != index
            or action["imageid"] != index
            or row["pos"] != action["pos"]
            or row["yaw"] != action["yaw"]
            or row["action_type"] != action["type"]
            or row["action_value"] != action["value"]
        ):
            raise ValueError("Raw pose/primitive differs from original shard metadata")
    return [derive(actions, index) for index in range(len(actions))]


def visual_edges(groups):
    """Exact RGB plus the frozen dHash/mean rule; not universal scene identity."""
    edges = []
    for index, left in enumerate(groups):
        for right in groups[index + 1 :]:
            matches = []
            for lf in left["frames"]:
                for rf in right["frames"]:
                    exact = lf["rgb_sha256"] == rf["rgb_sha256"]
                    distance = (int(lf["dhash64"], 16) ^ int(rf["dhash64"], 16)).bit_count()
                    mean = max(
                        abs(a - b) for a, b in zip(lf["mean_rgb"], rf["mean_rgb"], strict=True)
                    )
                    if exact or distance <= 3 and mean <= 10:
                        matches.append(
                            {
                                "left_path": lf["path"],
                                "right_path": rf["path"],
                                "exact_rgb": exact,
                                "dhash_distance": distance,
                                "max_mean_rgb_difference": mean,
                            }
                        )
            if matches:
                edges.append({"scenes": [left["scene_id"], right["scene_id"]], "matches": matches})
    return edges


def audit(output):
    output = Path(output).absolute().resolve(strict=False)
    if output.parent != (ROOT / "artifacts/diagnostics").resolve(strict=True):
        raise ValueError("Use a fresh direct artifacts/diagnostics directory")
    output.mkdir()  # Never overwrite earlier observations.
    receipt_binding = record(ROOT / RECEIPT)
    if receipt_binding["sha256"] != RECEIPT_SHA:
        raise ValueError("Frozen original82 extraction receipt changed")
    receipt = read_small(receipt_binding)
    plan = read_small(receipt["plan"])
    if (
        receipt["plan"]["sha256"] != PLAN_SHA
        or receipt["native_exit_code"] != 0
        or type(receipt["native_exit_code"]) is not int
        or receipt["sources_unchanged"] is not True
        or receipt["all_82_original_images_verified"] is not True
        or receipt["old_dataset_merged_or_modified"] is not False
        or [
            (r["annotation_index"], r["scene_id"], r["required_frame_count"])
            for r in plan["routes"]
        ]
        != list(ROUTES)
        or len(receipt["images"]) != 82
    ):
        raise ValueError("Only the sealed original82 TRAIN extraction is admitted")
    dependencies = (
        "scripts/build_onboard_openfly_navigation_dataset.py",
        "scripts/fetch_openfly_navigation_pilot.py",
        "src/multidetect/autonomy/onboard_navigation_supervision.py",
        "src/multidetect/autonomy/serialization.py",
    )
    used = [
        receipt_binding,
        record(verified(receipt["plan"])),
        record(__file__),
        record(ROOT / "tests/test_audit_openfly_multiscene_navigation_geometry.py"),
    ]
    for name in dependencies:
        rows = [r for r in receipt["sources_before"] if r["path"].replace("\\", "/").endswith(name)]
        if len(rows) != 1:
            raise ValueError("Exactly one frozen implementation binding required")
        used.append(record(verified(rows[0], cap=2 * 1024 * 1024)))
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]
    import yaml

    from scripts import build_onboard_openfly_navigation_dataset as builder
    from scripts.fetch_openfly_navigation_pilot import parse_pose

    routes, groups = [], []
    for route in plan["routes"]:
        index, scene = route["annotation_index"], route["scene_id"]
        paths = {}
        for role, suffix in (
            ("pose", f"Image/{route['image_path']}/pose.jsonl"),
            ("config", f"configs/{scene}.yaml"),
        ):
            rows = [
                r
                for r in receipt["sources_before"]
                if r["path"].replace("\\", "/").endswith(suffix)
            ]
            if len(rows) != 1:
                raise ValueError("One exact pose and scene configuration required")
            paths[role] = verified(rows[0])
            used.append(record(paths[role]))
        config = yaml.safe_load(paths["config"].read_text(encoding="utf-8"))
        if any(
            type(config["traj_map"].get(k)) not in (int, float) or config["traj_map"][k] != 1
            for k in ("traj_scale_ratio", "pcd_scale_ratio")
        ):
            raise ValueError("Only reviewed unit-scale AirSim geometry is admitted")
        actions, _ = parse_pose(paths["pose"].read_bytes())
        targets = metric_targets(actions, route["metadata_rows"], builder.derive_primitive)
        images = [r for r in receipt["images"] if r["annotation_index"] == index]
        if len(images) != route["required_frame_count"]:
            raise ValueError("Complete original image sequence required")
        frames = []
        for step, (image, metadata) in enumerate(zip(images, route["metadata_rows"], strict=True)):
            if (
                image["frame_index"] != step
                or image["parquet_row_index"] != step
                or image["image_id"] != metadata["image_id"]
                or image["scene_id"] != scene
                or image["image_path"] != route["image_path"]
                or image["original_encoded_bytes_preserved"] is not True
            ):
                raise ValueError("Extracted RGB and original metadata order differ")
            path = verified(image["file"], cap=8 * 1024 * 1024)
            used.append(record(path))
            frames.append({"path": str(path), **builder.rgb_identity(path)})
        groups.append({"scene_id": scene, "frames": frames})
        routes.append(
            {
                "annotation_index": index,
                "scene_id": scene,
                "proposed_partition_not_promoted": route["development_partition"],
                "original_split": "train",
                "frame_count": len(frames),
                "raw_action_counts": dict(Counter(a["type"] for a in actions)),
                "raw_action_values": sorted({(a["type"], a["value"]) for a in actions}),
                "targets_and_geometry_checks": [{"target": t, "check": c} for t, c in targets],
            }
        )
    old_images = [
        r
        for r in receipt["sources_before"]
        if "openfly-navigation-source-pilot-20261007-r2/raw/Image/env_airsim_18/"
        in r["path"].replace("\\", "/")
        and r["path"].endswith(".png")
    ]
    if len(old_images) != 86 or len({r["path"] for r in old_images}) != 86:
        raise ValueError("Exactly the old86 original RGB files must be compared")
    old_frames = []
    for row in old_images:
        path = verified(row, cap=8 * 1024 * 1024)
        used.append(record(path))
        old_frames.append({"path": str(path), **builder.rgb_identity(path)})
    groups.append({"scene_id": "env_airsim_18", "frames": old_frames})
    edges = visual_edges(groups)
    unique = {r["path"]: r for r in used}
    before = [unique[k] for k in sorted(unique)]
    after = [record(r["path"]) for r in before]
    if canonical(before) != canonical(after):
        raise ValueError("Audited sources changed during geometry/visual checks")
    report = {
        "schema_version": 1,
        "contract": CONTRACT,
        "native_exit_code": 0,
        "scope": "offline_planned_geometry_and_visual_heuristic_not_samples_or_holdout_acceptance",
        "original82_pose_successor_geometry_verified": True,
        "old86_RGB_compared": True,
        "routes": routes,
        "visual_groups": groups,
        "cross_scene_visual_edges": edges,
        "visual_rule": {"dhash64_hamming_max": 3, "mean_rgb_component_difference_max": 10},
        "heuristic_cross_scene_duplicate_detected": bool(edges),
        "sources_before": before,
        "sources_after": after,
        "sources_unchanged": True,
        "future_pose_or_label_sent_to_actor": False,
        **dict.fromkeys(FALSE_CLAIMS, False),
    }
    with (output / "report.json").open("xb") as stream:
        stream.write(canonical(report) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    print(
        json.dumps(
            {
                "event": "original82_geometry_audit_complete",
                "native_exit_code": 0,
                "report": record(output / "report.json"),
                "routes": len(routes),
                "images": 82,
                "old_images": 86,
                "cross_scene_edges": len(edges),
            }
        ),
        flush=True,
    )
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    return audit(args.output_dir)


if __name__ == "__main__":
    raise SystemExit(main())
