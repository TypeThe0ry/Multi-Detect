#!/usr/bin/env python3
"""Run the real SIM teacher collector, then verify mover visibility offline."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

from multidetect.simulator.camera_projection import project_target_bbox
from scripts.score_onboard_loop import target_pose_at_time

ROOT = Path(__file__).resolve().parents[1]


def build_collector_command(
    world: str,
    scenario: Path,
    out: Path,
    duration_s: float,
    min_free_gb: float,
) -> list[str]:
    """Build the collector invocation without starting the simulator."""
    return [
        "python",
        "scripts/collect_sim_teacher_episodes.py",
        "--scenario",
        str(scenario),
        "--teacher",
        "hover",
        "--episodes",
        "1",
        "--steps-per-episode",
        str(max(8, int(duration_s / 2))),
        "--timeout-s",
        "600",
        "--max-episode-s",
        "600",
        "--segmentation",
        "--takeoff-altitude-m",
        "8",
        "--container-prefix",
        "codex-movers",
        "--out",
        str(out),
        "--min-free-gb",
        str(min_free_gb),
        "--control-port",
        "14600",
        "--telemetry-port",
        "14601",
    ]


MAX_STABLE_TILT_DEG = 3.0
MAX_MEDIAN_CENTROID_ERROR_PX = 25.0
# Yaw still settling after a turn shows up as drift across the hold step.
MAX_STABLE_YAW_DRIFT_DEG = 3.0


def postprocess_episode(episode: Path, truth: dict, *, window_px: int = 12) -> dict:
    target = next(t for t in truth["targets"] if t.get("true_target"))
    label = next(
        int(k)
        for k, v in truth["segmentation_labels"].items()
        if v.get("target_id") == target["target_id"]
    )
    frames = []
    skipped_maneuvering = 0
    for line in (episode / "steps.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        obs = row.get("obs_before") or {}
        stamp = obs.get("frame_sensor_stamp_ns")
        if stamp is None or not obs.get("segmentation_path") or not obs.get("rgb_path"):
            continue
        # Score only frames taken while the drone holds still.  Mid-turn frames are
        # exposed while yaw/roll change quickly, so the pose paired with the image no
        # longer matches it and the projected truth lands off the real target.
        attitude = obs.get("attitude") or {}
        after = (row.get("obs_after") or {}).get("attitude") or {}
        yaw_drift = abs(
            (
                float(after.get("yaw_deg", attitude.get("yaw_deg", 0.0)))
                - float(attitude.get("yaw_deg", 0.0))
                + 180.0
            )
            % 360.0
            - 180.0
        )
        if (
            (row.get("action") or {}).get("kind") != "hold"
            or max(
                abs(float(attitude.get("roll_deg", 0.0))),
                abs(float(attitude.get("pitch_deg", 0.0))),
            )
            > MAX_STABLE_TILT_DEG
            or yaw_drift > MAX_STABLE_YAW_DRIFT_DEG
        ):
            skipped_maneuvering += 1
            continue
        cam = obs.get("camera_info") or {}
        size = (int(cam.get("width", 640)), int(cam.get("height", 480)))
        pose = target_pose_at_time(target, float(stamp) / 1e9)
        bbox = project_target_bbox(
            target_ned=(pose["y_north_m"], pose["x_east_m"], pose.get("z_up_m", 0.0)),
            target_size_m=target.get("size_m") or target.get("measured_size_m"),
            target_yaw_rad=pose.get("yaw_rad", 0.0),
            target_bottom_offset_m=target.get("bottom_offset_m"),
            pose=obs.get("pose") or {},
            camera_info=cam,
            image_size=size,
        )
        if not bbox:
            continue
        x1, y1, x2, y2 = bbox
        seg = np.asarray(Image.open(episode / obs["segmentation_path"]))
        yy, xx = np.indices(seg.shape[:2])
        mask = (
            (seg[..., 2] == label)
            & (xx >= x1 - window_px)
            & (xx <= x2 + window_px)
            & (yy >= y1 - window_px)
            & (yy <= y2 + window_px)
        )
        ys, xs = np.nonzero(mask)
        center = ((x1 + x2) / 2, (y1 + y2) / 2)
        centroid = (float(xs.mean()), float(ys.mean())) if len(xs) else None
        rgb = np.asarray(Image.open(episode / obs["rgb_path"]).convert("RGB"))
        crop = rgb[
            max(0, int(y1) - window_px) : min(rgb.shape[0], int(y2) + window_px + 1),
            max(0, int(x1) - window_px) : min(rgb.shape[1], int(x2) + window_px + 1),
        ]
        border = (
            np.concatenate((crop[0], crop[-1], crop[:, 0], crop[:, -1]), axis=0)
            if crop.size
            else np.empty((0, 3))
        )
        diff = (
            float(np.mean(np.abs(crop.astype(np.float32) - np.median(border, axis=0))))
            if crop.size and len(border)
            else 0.0
        )
        frames.append(
            {
                "sim_time_s": float(stamp) / 1e9,
                "projected_bbox": [x1, y1, x2, y2],
                "segmentation_visible": bool(len(xs)),
                "centroid_error_px": (
                    ((centroid[0] - center[0]) ** 2 + (centroid[1] - center[1]) ** 2) ** 0.5
                    if centroid
                    else None
                ),
                "rgb_background_diff": diff,
            }
        )
    errors = [f["centroid_error_px"] for f in frames if f["centroid_error_px"] is not None]
    return {
        "frames": frames,
        "frame_count": len(frames),
        "skipped_maneuvering_frames": skipped_maneuvering,
        "max_centroid_error_px": max(errors, default=None),
        "median_centroid_error_px": float(np.median(errors)) if errors else None,
        "segmentation_visible_fraction": sum(f["segmentation_visible"] for f in frames)
        / len(frames)
        if frames
        else 0.0,
        "rgb_visible_fraction": sum(f["rgb_background_diff"] > 3 for f in frames) / len(frames)
        if frames
        else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", default="gen_mov_w0000")
    ap.add_argument("--duration-s", type=float, default=30.0)
    ap.add_argument("--out", type=Path, default=ROOT / "artifacts/verify-movers")
    # The collector's disk guard defaults to 15 GB; a 30 s verification needs far less.
    ap.add_argument("--min-free-gb", type=float, default=15.0)
    a = ap.parse_args()
    scenario = ROOT / "configs/scenarios/generated-movers" / f"{a.world}.scenario.json"
    a.out.mkdir(parents=True, exist_ok=True)
    prefix = "codex-movers"
    cmd = build_collector_command(a.world, scenario, a.out, a.duration_s, a.min_free_gb)
    try:
        run = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, check=False)
        (a.out / f"{a.world}.collector.stdout.log").write_text(run.stdout)
        (a.out / f"{a.world}.collector.stderr.log").write_text(run.stderr)
        # The newest episode that recorded steps; an earlier aborted run in the same
        # output directory may have left an episode without them.
        eps = sorted(
            (p for p in (a.out / "episodes").glob("episode-*") if (p / "steps.jsonl").is_file()),
            key=lambda p: (p / "steps.jsonl").stat().st_mtime,
        )
        truth = json.loads(
            (ROOT / "configs/scenarios/generated-movers" / f"{a.world}.truth.json").read_text()
        )
        verdict = postprocess_episode(eps[-1], truth) if eps else {"frame_count": 0}
        verdict.update({"world": a.world, "collector_returncode": run.returncode})
        verdict["pass"] = (
            run.returncode == 0
            and verdict.get("segmentation_visible_fraction", 0) > 0.8
            and verdict.get("rgb_visible_fraction", 0) > 0.8
            # The rendered target must sit where the sim-time truth says it is.
            and (verdict.get("median_centroid_error_px") or float("inf"))
            <= MAX_MEDIAN_CENTROID_ERROR_PX
        )
        (a.out / f"{a.world}.verdict.json").write_text(json.dumps(verdict, indent=2))
        print(json.dumps(verdict, indent=2))
        return 0 if verdict["pass"] else 1
    finally:
        subprocess.run(
            ["docker", "rm", "-f", f"{prefix}-episode-0000"],
            capture_output=True,
            text=True,
            check=False,
        )


if __name__ == "__main__":
    raise SystemExit(main())
