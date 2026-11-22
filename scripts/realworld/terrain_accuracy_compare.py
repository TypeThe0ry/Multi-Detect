#!/usr/bin/env python3
"""Read-only V0/V6 terrain-proxy comparison for completed RTF outputs.

The two positional arguments are the primary V0 and V6 result directories.
Use ``--v0-run`` and ``--v6-run`` repeatedly to add more completed rounds.
Observations are matched by geographic N/E grid cells, never by timestamps.
The primary proxy is aircraft relative height minus DEM relative elevation.
Saved depth arrays are reported as validity/saturation diagnostics only because
the captured forward-depth stream is dominated by its 65.535 m saturation value
and the episode does not contain an independent terrain estimate.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np


@dataclass(frozen=True)
class Dem:
    path: Path
    origin_elevation_m: float
    north_origin_m: float
    east_origin_m: float
    north_spacing_m: float
    east_spacing_m: float
    elevation_m: np.ndarray

    @classmethod
    def load(cls, path: Path) -> "Dem":
        body = json.loads(path.read_text(encoding="utf-8"))
        origin = body.get("origin", {})
        grid = np.asarray(body["elevation_m"], dtype=float)
        if grid.ndim != 2 or min(grid.shape) < 2:
            raise ValueError(f"DEM grid must be 2-D with at least 2 cells: {path}")
        return cls(path.resolve(), float(origin["elevation_m"]), float(body["north_origin_m"]),
                   float(body["east_origin_m"]), float(body["north_spacing_m"]),
                   float(body["east_spacing_m"]), grid)

    def sample(self, north_m: float, east_m: float) -> float | None:
        row = (north_m - self.north_origin_m) / self.north_spacing_m
        col = (east_m - self.east_origin_m) / self.east_spacing_m
        if row < 0 or col < 0 or row > self.elevation_m.shape[0] - 1 or col > self.elevation_m.shape[1] - 1:
            return None
        r0 = min(int(math.floor(row)), self.elevation_m.shape[0] - 2)
        c0 = min(int(math.floor(col)), self.elevation_m.shape[1] - 2)
        dr, dc = row - r0, col - c0
        a, b = self.elevation_m[r0, c0], self.elevation_m[r0, c0 + 1]
        c, d = self.elevation_m[r0 + 1, c0], self.elevation_m[r0 + 1, c0 + 1]
        return float((1 - dr) * (1 - dc) * a + (1 - dr) * dc * b + dr * (1 - dc) * c + dr * dc * d)


@dataclass
class Sample:
    campaign: str
    run_name: str
    attempt: str
    episode: str
    step_index: int
    side: str
    obs: dict[str, Any]
    north_m: float
    east_m: float
    down_m: float
    height_rel_m: float
    sim_s: float | None
    dem_elevation_m: float
    dem_relative_m: float
    height_residual_m: float
    depth_path: Path | None = None
    depth_valid_fraction: float | None = None
    depth_saturation_fraction: float | None = None


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _attempt_dirs(root: Path) -> list[Path]:
    root = root.resolve()
    if (root / "result.json").is_file() or (root / "steps.jsonl").is_file():
        return [root]
    return sorted(p.parent for p in root.glob("attempt-*/result.json"))


def _select_attempt(root: Path) -> tuple[Path | None, list[str]]:
    attempts = _attempt_dirs(root)
    notes: list[str] = []
    if not attempts:
        return None, [f"未找到 attempt-*/result.json 或 steps.jsonl: {root}"]
    valid: list[Path] = []
    for attempt in attempts:
        result_path = attempt / "result.json"
        if not result_path.is_file():
            valid.append(attempt)
            continue
        try:
            result = _json(result_path)
        except (OSError, json.JSONDecodeError) as exc:
            notes.append(f"{attempt.name}: result.json 读取失败: {exc}")
            continue
        if result.get("valid_round") is True:
            valid.append(attempt)
        else:
            notes.append(f"{root.name}/{attempt.name}: 跳过 valid_round={result.get('valid_round')!r}, "
                         f"measurement_complete={result.get('measurement_complete')!r}, "
                         f"errors={result.get('round_errors') or result.get('errors') or []}")
    if not valid:
        return None, notes or [f"没有完成轮次: {root}"]
    if len(valid) > 1:
        notes.append(f"{root.name}: 发现多个有效 attempt，使用最后一个 {valid[-1].name}")
    return valid[-1], notes


def _episode_dir(attempt: Path) -> Path | None:
    if (attempt / "steps.jsonl").is_file():
        return attempt
    episodes = [p for p in sorted(attempt.glob("flight/episodes/*")) if (p / "steps.jsonl").is_file()]
    return episodes[0] if episodes else None


def _pose_local_ned(obs: dict[str, Any]) -> tuple[float, float, float] | None:
    local = (obs.get("pose") or {}).get("local_ned_m")
    if isinstance(local, list) and len(local) >= 3 and all(x is not None for x in local[:3]):
        return float(local[0]), float(local[1]), float(local[2])
    return None


def _sim_seconds(obs: dict[str, Any]) -> float | None:
    if obs.get("pose_sim_stamp_ns") is not None:
        return float(obs["pose_sim_stamp_ns"]) / 1e9
    if obs.get("pose_time_boot_ms") is not None:
        return float(obs["pose_time_boot_ms"]) / 1e3
    return None


def _iter_observations(episode: Path, side: str) -> Iterable[tuple[int, str, dict[str, Any]]]:
    for line in (episode / "steps.jsonl").open(encoding="utf-8"):
        item = json.loads(line)
        sides = ("before", "after") if side == "both" else (side,)
        for selected_side in sides:
            obs = item.get(f"obs_{selected_side}")
            if isinstance(obs, dict):
                yield int(item.get("step_index", -1)), selected_side, obs


def _stats(values: Iterable[float]) -> dict[str, float | int | None]:
    values = list(values)
    if not values:
        return {"n": 0, "mean": None, "median": None, "p05": None, "p95": None, "mad": None, "std": None}
    arr = np.asarray(values, dtype=float)
    median = float(np.median(arr))
    return {"n": int(arr.size), "mean": float(np.mean(arr)), "median": median,
            "p05": float(np.percentile(arr, 5)), "p95": float(np.percentile(arr, 95)),
            "mad": float(np.median(np.abs(arr - median))),
            "std": float(np.std(arr, ddof=1)) if arr.size > 1 else 0.0}


def _numbers(samples: Iterable[Sample], field: str) -> list[float]:
    values = []
    for sample in samples:
        value = getattr(sample, field)
        if value is not None and math.isfinite(float(value)):
            values.append(float(value))
    return values


def _summarize_depth(sample: Sample) -> None:
    if sample.depth_path is None or not sample.depth_path.is_file():
        return
    try:
        array = np.asarray(np.load(sample.depth_path), dtype=float)
    except Exception:
        return
    info = sample.obs.get("depth_camera_info") or {}
    minimum = float(info.get("minimum_depth_m", 0.1))
    maximum = float(info.get("maximum_depth_m", 65.535))
    finite = np.isfinite(array)
    valid = finite & (array > minimum) & (array < maximum - 1e-3)
    saturated = finite & (array >= maximum - 1e-3)
    sample.depth_valid_fraction = float(valid.mean())
    sample.depth_saturation_fraction = float(saturated.mean())


def _load_campaign(root: Path, campaign: str, *, side: str, dem: Dem, low_altitude_m: float,
                   max_seconds: float | None) -> tuple[list[Sample], dict[str, Any], list[str]]:
    attempt, notes = _select_attempt(root)
    if attempt is None:
        return [], {"run": root.name, "status": "missing", "sample_count": 0}, notes
    episode = _episode_dir(attempt)
    if episode is None:
        message = f"{attempt}: 未找到 flight/episodes/*/steps.jsonl"
        return [], {"run": root.name, "status": "missing", "sample_count": 0}, notes + [message]

    prepared: list[tuple[int, str, dict[str, Any], float, float, float, float, float | None]] = []
    for step_index, selected_side, obs in _iter_observations(episode, side):
        pose = _pose_local_ned(obs)
        if pose is None:
            notes.append(f"{root.name}/{episode.name} step {step_index}/{selected_side}: 缺少 local_ned_m")
            continue
        north_m, east_m, down_m = pose
        prepared.append((step_index, selected_side, obs, north_m, east_m, down_m, -down_m, _sim_seconds(obs)))
    t0 = next((row[-1] for row in prepared if row[-1] is not None), None)
    samples: list[Sample] = []
    for step_index, selected_side, obs, north_m, east_m, down_m, height_rel_m, sim_s in prepared:
        if height_rel_m >= low_altitude_m:
            continue
        if max_seconds is not None and (sim_s is None or t0 is None or sim_s - t0 > max_seconds):
            continue
        dem_elevation = dem.sample(north_m, east_m)
        if dem_elevation is None:
            notes.append(f"{root.name}/{episode.name} step {step_index}/{selected_side}: 位姿落在 DEM 外")
            continue
        sample = Sample(campaign, root.name, attempt.name, episode.name, step_index, selected_side, obs,
                        north_m, east_m, down_m, height_rel_m, sim_s, dem_elevation,
                        dem_elevation - dem.origin_elevation_m,
                        height_rel_m - (dem_elevation - dem.origin_elevation_m),
                        (episode / obs["depth_path"]).resolve() if obs.get("depth_path") else None)
        _summarize_depth(sample)
        samples.append(sample)
    episode_json = _json(episode / "episode.json") if (episode / "episode.json").is_file() else {}
    metadata = {
        "run": root.name, "attempt": attempt.name, "episode": episode.name, "status": "ok",
        "sample_count": len(samples), "raw_observation_count": len(prepared),
        "world_sdf": episode_json.get("world_sdf"),
        "height_residual_m": _stats(_numbers(samples, "height_residual_m")),
        "depth_valid_fraction": _stats(_numbers(samples, "depth_valid_fraction")),
        "depth_saturation_fraction": _stats(_numbers(samples, "depth_saturation_fraction")),
        "depth_frame_count": sum(sample.depth_path is not None for sample in samples),
    }
    return samples, metadata, notes


def _geo_bin_stats(samples: list[Sample], field: str, bin_m: float, dem: Dem) -> dict[tuple[int, int], dict[str, Any]]:
    grouped: dict[tuple[int, int], list[float]] = {}
    for sample in samples:
        value = getattr(sample, field)
        if value is None or not math.isfinite(float(value)):
            continue
        north_bin = int(math.floor((sample.north_m - dem.north_origin_m) / bin_m))
        east_bin = int(math.floor((sample.east_m - dem.east_origin_m) / bin_m))
        grouped.setdefault((north_bin, east_bin), []).append(float(value))
    return {key: _stats(values) for key, values in sorted(grouped.items())}


def _paired_geo_bins(v0: list[Sample], v6: list[Sample], field: str, bin_m: float, dem: Dem) -> dict[str, Any]:
    left = _geo_bin_stats(v0, field, bin_m, dem)
    right = _geo_bin_stats(v6, field, bin_m, dem)
    rows: list[dict[str, Any]] = []
    deltas: list[float] = []
    for north_bin, east_bin in sorted(set(left) & set(right)):
        a, b = left[(north_bin, east_bin)], right[(north_bin, east_bin)]
        if a["median"] is None or b["median"] is None:
            continue
        delta = float(b["median"] - a["median"])
        deltas.append(delta)
        rows.append({"north_bin": north_bin, "east_bin": east_bin,
                     "north_start_m": dem.north_origin_m + north_bin * bin_m,
                     "east_start_m": dem.east_origin_m + east_bin * bin_m,
                     "v0_median_m": a["median"], "v6_median_m": b["median"],
                     "delta_v6_minus_v0_m": delta, "v0_n": a["n"], "v6_n": b["n"]})
    return {"bin_m": bin_m, "matched_bins": len(rows), "delta_stats": _stats(deltas), "rows": rows}


def compare(v0_roots: list[Path], v6_roots: list[Path], dem: Dem, *, side: str,
            low_altitude_m: float, max_seconds: float | None, geo_bin_m: float) -> dict[str, Any]:
    samples_by_campaign: dict[str, list[Sample]] = {"V0": [], "V6": []}
    run_rows: list[dict[str, Any]] = []
    notes: list[str] = []
    for campaign, roots in (("V0", v0_roots), ("V6", v6_roots)):
        for root in roots:
            samples, metadata, run_notes = _load_campaign(root, campaign, side=side, dem=dem,
                                                           low_altitude_m=low_altitude_m,
                                                           max_seconds=max_seconds)
            samples_by_campaign[campaign].extend(samples)
            run_rows.append({"campaign": campaign, **metadata})
            notes.extend(run_notes)
    campaigns = {}
    for campaign in ("V0", "V6"):
        samples = samples_by_campaign[campaign]
        campaigns[campaign] = {
            "sample_count": len(samples),
            "height_residual_m": _stats(_numbers(samples, "height_residual_m")),
            "depth_valid_fraction": _stats(_numbers(samples, "depth_valid_fraction")),
            "depth_saturation_fraction": _stats(_numbers(samples, "depth_saturation_fraction")),
            "depth_frame_count": sum(sample.depth_path is not None for sample in samples),
        }
    return {
        "schema_version": 1,
        "inputs": {"v0": [str(path.resolve()) for path in v0_roots],
                   "v6": [str(path.resolve()) for path in v6_roots]},
        "dem": {"path": str(dem.path), "shape": list(dem.elevation_m.shape),
                "origin_elevation_m": dem.origin_elevation_m,
                "north_origin_m": dem.north_origin_m, "east_origin_m": dem.east_origin_m,
                "north_spacing_m": dem.north_spacing_m, "east_spacing_m": dem.east_spacing_m},
        "selection": {"side": side, "low_altitude_m": low_altitude_m,
                       "max_seconds": max_seconds, "geographic_bin_m": geo_bin_m,
                       "proxy": "-local_ned_m[2] - (DEM_elevation_m - DEM_origin_elevation_m)"},
        "runs": run_rows,
        "campaigns": campaigns,
        "paired_geographic_bins": _paired_geo_bins(samples_by_campaign["V0"], samples_by_campaign["V6"],
                                                    "height_residual_m", geo_bin_m, dem),
        "notes": notes,
    }


def _default_dem() -> Path:
    root = Path(__file__).resolve().parents[2]
    candidates = [root / "simulation/terrain/rw_farm_iowa_w6km__clear_noon.dem.json",
                  root / ".tmp/rtf-speedup-run/variants/V6/assets/terrain/rw_farm_iowa_w6km__rtf_v6__clear_noon.dem.json"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("v0_root", type=Path, help="第一个 V0 measurement 结果目录")
    parser.add_argument("v6_root", type=Path, help="第一个 V6 measurement 结果目录")
    parser.add_argument("--v0-run", dest="v0_extra", type=Path, action="append", default=[],
                        help="附加 V0 结果目录；可重复")
    parser.add_argument("--v6-run", dest="v6_extra", type=Path, action="append", default=[],
                        help="附加 V6 结果目录；可重复")
    parser.add_argument("--dem", type=Path, default=None, help="DEM JSON；默认使用 6 km 默认 DEM")
    parser.add_argument("--side", choices=("before", "after", "both"), default="after")
    parser.add_argument("--low-altitude-m", type=float, default=150.0)
    parser.add_argument("--max-seconds", type=float, default=None)
    parser.add_argument("--geo-bin-m", type=float, default=100.0, help="N/E 地理位置分箱边长")
    args = parser.parse_args(argv)
    if args.geo_bin_m <= 0:
        parser.error("--geo-bin-m must be positive")
    dem_path = (args.dem or _default_dem()).resolve()
    try:
        result = compare([args.v0_root, *args.v0_extra], [args.v6_root, *args.v6_extra],
                         Dem.load(dem_path), side=args.side, low_altitude_m=args.low_altitude_m,
                         max_seconds=args.max_seconds, geo_bin_m=args.geo_bin_m)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"terrain_accuracy_compare: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
