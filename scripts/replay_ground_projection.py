"""Offline CPU replay of recorded track pixels against scenario truth.

Only explicitly provided episode roots are read. Segmentation supplies image-space
association when available; projected truth boxes are the documented fallback.
No detector, model, simulator, or live aircraft is started.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from multidetect.autonomy.ground_model import DemGround, FlatGround  # noqa: E402
from multidetect.simulator.camera_projection import (  # noqa: E402
    CAMERA_MOUNT_PITCH_RAD,
    camera_center_ned_up,
    pixel_ground_point,
    project_target_bbox,
)
from multidetect.terrain_dem import LocalDemGrid, load_local_dem_grid  # noqa: E402
from scripts.score_onboard_loop import _iou, truth_for_episode  # noqa: E402


def error_statistics(values: list[float]) -> dict[str, float | int | None]:
    """Use nearest-rank p95; empty samples remain explicit instead of zero."""
    if not values:
        return dict(count=0, mean_m=None, median_m=None, rmse_m=None, p95_m=None, max_m=None)
    ordered = sorted(values)
    return {
        "count": len(values),
        "mean_m": statistics.mean(values),
        "median_m": statistics.median(values),
        "rmse_m": math.sqrt(statistics.mean(value * value for value in values)),
        "p95_m": ordered[math.ceil(0.95 * len(ordered)) - 1],
        "max_m": ordered[-1],
    }


def _source(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _truth_path(world: str, scenarios: Path | None) -> Path:
    roots = [scenarios] if scenarios else []
    roots.extend(
        ROOT / "configs/scenarios" / name
        for name in (
            "generated-count-site",
            "generated-confusers",
            "generated-v2-appearance",
            "generated-v2",
        )
    )
    return next(
        root / f"{world}.truth.json" for root in roots if (root / f"{world}.truth.json").is_file()
    )


def _projected_boxes(observation: dict, targets: dict[str, dict]) -> dict[str, list[float]]:
    info = observation["camera_info"]
    boxes = {}
    for target_id, target in targets.items():
        pose = target["pose"]
        box = project_target_bbox(
            target_ned=(pose["y_north_m"], pose["x_east_m"], pose["z_up_m"]),
            pose=observation,
            camera_info=info,
            image_size=(int(info["width"]), int(info["height"])),
            target_size_m=tuple(target.get("size_m", (3.0, 1.8, 1.5))),
            target_yaw_rad=pose.get("yaw_rad", 0.0),
            target_bottom_offset_m=target.get("bottom_offset_m"),
        )
        if box is not None:
            boxes[target_id] = list(box)
    return boxes


def associate_box(box: list[float], truth_boxes: dict[str, list[float]], minimum_iou: float):
    """Choose identity solely in image space, independent of ground estimates."""
    if not truth_boxes:
        return None
    target_id = max(truth_boxes, key=lambda key: _iou(box, truth_boxes[key]))
    overlap = _iou(box, truth_boxes[target_id])
    return (target_id, overlap) if overlap >= minimum_iou else None


def replay_episodes(
    episodes: list[Path],
    *,
    terrain_dir: Path,
    scenarios: Path | None = None,
    minimum_iou: float = 0.1,
    include_hits: bool = False,
) -> dict[str, Any]:
    flat_errors, dem_errors, deltas = [], [], []
    skip = Counter()
    associations = Counter()
    results, hits = [], []
    default_checked = default_equal = stored_checked = stored_equal = 0
    stored_differences = []
    for episode in sorted(set(episodes)):
        steps_path = episode / "steps.jsonl"
        if not steps_path.is_file():
            skip["episode_missing_steps"] += 1
            continue
        world, truth = truth_for_episode(episode, scenarios)
        if world is None or truth is None:
            skip["episode_missing_truth"] += 1
            continue
        dem_path = terrain_dir / f"{world}.dem.json"
        if not dem_path.is_file():
            skip["episode_missing_dem"] += 1
            continue
        grid = load_local_dem_grid(dem_path)
        dem = DemGround(grid)
        targets = {str(target["target_id"]): target for target in truth["targets"]}
        rows = [
            json.loads(line)
            for line in steps_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        ef, ed = [], []
        segmentation_sources = {}
        episode_tracks = 0
        for row in rows:
            observation = row["obs_before"]
            tracks = (row.get("labels", {}).get("onboard") or {}).get("tracks", [])
            if not tracks:
                continue
            info = observation["camera_info"]
            size = (int(info["width"]), int(info["height"]))
            segmentation = observation.get("segmentation_path")
            segmentation_path = (
                (episode / segmentation.replace("\\", "/")) if segmentation else None
            )
            if (
                segmentation_path is not None
                and segmentation_path.is_file()
                and truth.get("segmentation_labels")
            ):
                from multidetect.sim_teacher.segmentation import (
                    boxes_from_segmentation,
                    load_labels_map,
                )

                boxes = boxes_from_segmentation(
                    load_labels_map(segmentation_path), truth, min_area_px=4, min_box_px=2
                )
                boxes = {key: list(value["box"]) for key, value in boxes.items() if key in targets}
                association_source = "segmentation"
                segmentation_sources[str(segmentation_path)] = _source(segmentation_path)
            else:
                boxes = _projected_boxes(observation, targets)
                association_source = "projected_truth_bbox"
            for track in tracks:
                episode_tracks += 1
                box = track["bbox"]
                kwargs = dict(pose=observation, camera_info=info, image_size=size)
                u, v = (box[0] + box[2]) / 2, box[3]
                flat = pixel_ground_point(u, v, **kwargs, ground_model=FlatGround())
                legacy = pixel_ground_point(u, v, **kwargs)
                default_checked += 1
                default_equal += flat == legacy
                if track.get("ground_ne") is not None and flat is not None:
                    stored_checked += 1
                    stored = tuple(track["ground_ne"])
                    stored_equal += flat == stored
                    stored_differences.append(math.dist(flat, stored))
                association = associate_box(box, boxes, minimum_iou)
                if association is None:
                    skip["track_no_image_association"] += 1
                    continue
                associations[association_source] += 1
                target_id, overlap = association
                terrain = pixel_ground_point(u, v, **kwargs, ground_model=dem)
                if flat is None or terrain is None:
                    skip["associated_flat_miss" if flat is None else "associated_dem_miss"] += 1
                    continue
                target = targets[target_id]["pose"]
                target_ne = (float(target["y_north_m"]), float(target["x_east_m"]))
                ferr, derr = math.dist(flat, target_ne), math.dist(terrain, target_ne)
                flat_errors.append(ferr)
                dem_errors.append(derr)
                deltas.append(math.dist(flat, terrain))
                ef.append(ferr)
                ed.append(derr)
                if include_hits:
                    hits.append(
                        dict(
                            episode=str(episode),
                            step=row.get("step_index"),
                            track_id=track.get("id"),
                            target_id=target_id,
                            association_source=association_source,
                            iou=overlap,
                            flat_ne=flat,
                            dem_ne=terrain,
                            truth_ne=target_ne,
                            flat_error_m=ferr,
                            dem_error_m=derr,
                        )
                    )
        seg_sources = sorted(segmentation_sources.values(), key=lambda item: item["path"])
        results.append(
            {
                "episode": str(episode),
                "world": world,
                "steps": len(rows),
                "track_boxes": episode_tracks,
                "flat": error_statistics(ef),
                "dem": error_statistics(ed),
                "dem_local_height_extent_m": [
                    min(map(min, grid.elevations_m)) - grid.origin_elevation_m,
                    max(map(max, grid.elevations_m)) - grid.origin_elevation_m,
                ],
                "sources": {
                    "episode": _source(episode / "episode.json"),
                    "steps": _source(steps_path),
                    "truth": _source(_truth_path(world, scenarios)),
                    "dem": _source(dem_path),
                    "segmentation_files": len(seg_sources),
                    "segmentation_manifest_sha256": hashlib.sha256(
                        json.dumps(seg_sources, sort_keys=True).encode()
                    ).hexdigest(),
                },
            }
        )
    result = {
        "association": {
            "minimum_iou": minimum_iou,
            "sources": dict(associations),
            "method": "maximum image-space IoU to visible scenario object bbox; no ground matching",
            "fallback_limitation": (
                "Projected boxes ignore rendered occlusion; "
                "ambiguous overlapping boxes can misassociate."
            ),
        },
        "flat": error_statistics(flat_errors),
        "dem": error_statistics(dem_errors),
        "flat_dem_position_difference": error_statistics(deltas),
        "skip_counts": dict(skip),
        "default_equivalence": {"checked": default_checked, "exact_equal": default_equal},
        "stored_ground_ne_equivalence": {
            "checked": stored_checked,
            "exact_equal": stored_equal,
            "difference": error_statistics(stored_differences),
            "limitation": (
                "Recorded bboxes may be rounded; ground_ne may preserve "
                "unrounded or smoothed tracks. Differences are not compatibility failures."
            ),
        },
        "episodes": results,
    }
    if include_hits:
        result["hits"] = hits
    return result


def synthetic_slopes() -> list[dict[str, Any]]:
    """Exact contact pixels isolate terrain bias from detection/object-extent bias."""
    result = []
    reference_msl = 135.0
    axis = (-200.0, 0.0, 200.0)
    observation = {"pose": {"local_ned_m": [0.0, 0.0, -30.0]}, "attitude": {}}
    info = dict(fx=540.0, fy=540.0, cx=640.0, cy=480.0, width=1280, height=960)
    camera = camera_center_ned_up(observation)
    assert camera is not None
    cp, sp = math.cos(CAMERA_MOUNT_PITCH_RAD), math.sin(CAMERA_MOUNT_PITCH_RAD)
    for north_slope, east_slope in ((0.14, 0.05), (-0.08, 0.10)):
        grid = LocalDemGrid(
            scenario_id="synthetic-slope",
            origin_latitude_deg=1.3521,
            origin_longitude_deg=103.8198,
            origin_elevation_m=reference_msl,
            north_origin_m=axis[0],
            east_origin_m=axis[0],
            north_spacing_m=200.0,
            east_spacing_m=200.0,
            elevations_m=tuple(
                tuple(reference_msl + north_slope * n + east_slope * e for e in axis) for n in axis
            ),
            vertical_sigma_m=0.02,
            slope_sigma_m_per_m=0.0001,
            source_sha256="0" * 64,
        )
        flat_errors, dem_errors, points = [], [], []
        for north in (40.0, 80.0, 120.0):
            for east in (-20.0, 0.0, 20.0):
                up = north_slope * north + east_slope * east
                x, y, z = north - camera[0], east - camera[1], camera[2] - up
                optical_x, optical_z = cp * x + sp * z, -sp * x + cp * z
                u = info["cx"] + info["fx"] * y / optical_x
                v = info["cy"] + info["fy"] * optical_z / optical_x
                kwargs = dict(pose=observation, camera_info=info, image_size=(1280, 960))
                flat = pixel_ground_point(u, v, **kwargs, ground_model=FlatGround())
                terrain = pixel_ground_point(u, v, **kwargs, ground_model=DemGround(grid))
                assert flat is not None and terrain is not None
                flat_errors.append(math.dist(flat, (north, east)))
                dem_errors.append(math.dist(terrain, (north, east)))
                points.append(dict(truth_neu=[north, east, up], pixel=[u, v]))
        result.append(
            dict(
                reference_elevation_msl_m=reference_msl,
                north_slope_m_per_m=north_slope,
                east_slope_m_per_m=east_slope,
                aircraft_up_m=30.0,
                flat=error_statistics(flat_errors),
                dem=error_statistics(dem_errors),
                exact_contact_samples=points,
            )
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "episodes", type=Path, nargs="*", help="Episode directories or recursive roots"
    )
    parser.add_argument("--terrain-dir", type=Path, default=ROOT / "simulation/terrain")
    parser.add_argument("--scenarios", type=Path)
    parser.add_argument("--minimum-iou", type=float, default=0.1)
    parser.add_argument("--include-hits", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not 0.0 < args.minimum_iou <= 1.0:
        parser.error("--minimum-iou must lie in (0, 1]")
    episodes = []
    for path in args.episodes:
        if (path / "episode.json").is_file():
            episodes.append(path)
        else:
            episodes.extend(meta.parent for meta in path.rglob("episode.json"))
    report = {
        "schema_version": 1,
        "execution": "offline CPU geometry only",
        "source": _source(Path(__file__)),
        "geometry_sources": [
            _source(ROOT / "src/multidetect/autonomy/ground_model.py"),
            _source(ROOT / "src/multidetect/simulator/camera_projection.py"),
            _source(ROOT / "src/multidetect/terrain_dem.py"),
        ],
        "semantics": (
            "Per-observation bbox bottom-centre ground hit versus object truth N/E; "
            "not flight-policy counterfactual."
        ),
        "replay": replay_episodes(
            episodes,
            terrain_dir=args.terrain_dir,
            scenarios=args.scenarios,
            minimum_iou=args.minimum_iou,
            include_hits=args.include_hits,
        ),
        "synthetic_slopes": synthetic_slopes(),
    }
    encoded = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
        print(
            json.dumps(
                {
                    "output": str(args.output),
                    "flat": report["replay"]["flat"],
                    "dem": report["replay"]["dem"],
                }
            )
        )
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
