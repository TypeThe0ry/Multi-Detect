"""Replay recorded count confirmations against their frame camera-pose validity."""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from multidetect.simulator.camera_projection import pixel_camera_ray  # noqa: E402
from scripts.score_onboard_loop import episode_camera_pitch_rad  # noqa: E402


def replay_episode(episode: Path) -> dict[str, Any]:
    """Keep the recorded flight and decisions; reject count events lacking a frame ray."""
    rows_path = episode / "steps.jsonl"
    rows = [
        json.loads(line)
        for line in rows_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not rows:
        raise ValueError(f"empty episode: {episode}")

    count_by_step: dict[str, int] = {}
    previous: dict[str, int] = {}
    events: list[dict[str, Any]] = []
    pitch = episode_camera_pitch_rad(episode)
    for row in rows:
        onboard = ((row.get("labels") or {}).get("onboard") or {})
        mission = onboard.get("mission") or {}
        counts = mission.get("counts") or {}
        for step_id, value in counts.items():
            current = int(value)
            count_by_step[step_id] = max(count_by_step.get(step_id, 0), current)
            if current <= previous.get(step_id, 0):
                continue
            if current != previous.get(step_id, 0) + 1:
                raise ValueError(f"non-unit count increment at step {row.get('step_index')}")
            previous[step_id] = current

            confirmation = onboard.get("confirmed") or {}
            observation = row.get("obs_before") or {}
            bbox = confirmation.get("bbox")
            position = confirmation.get("position_estimate_ne")
            ray = None
            angle_deg = None
            if isinstance(bbox, list) and len(bbox) == 4:
                camera_info = observation.get("camera_info") or {}
                image_size = (
                    int(camera_info.get("width", 1280)),
                    int(camera_info.get("height", 960)),
                )
                ray = pixel_camera_ray(
                    (float(bbox[0]) + float(bbox[2])) / 2.0,
                    float(bbox[3]),
                    pose=observation,
                    camera_info=camera_info,
                    image_size=image_size,
                    camera_pitch_rad=pitch,
                )
                if ray is not None:
                    direction = ray[1]
                    angle_deg = math.degrees(
                        math.atan2(direction[2], math.hypot(direction[0], direction[1]))
                    )
            events.append(
                {
                    "step": int(row["step_index"]),
                    "track_id": confirmation.get("track_id"),
                    "position_ne": position,
                    "camera_ray_available": ray is not None,
                    "position_available": position is not None,
                    "accepted_after_pose_gate": ray is not None and position is not None,
                    "ground_ray_below_horizon_deg": angle_deg,
                    "sim_skew_ms": observation.get("sim_skew_ms"),
                    "gimbal_feedback_age_s": (observation.get("gimbal_state") or {}).get("age_s"),
                    "gimbal_feedback_fresh": (observation.get("gimbal_state") or {}).get("fresh"),
                    "camera_pose_present": isinstance(observation.get("camera_pose"), dict),
                }
            )

    return {
        "episode": str(episode),
        "recorded_count": count_by_step,
        "recorded_confirmation_events": len(events),
        "pose_gated_count": sum(event["accepted_after_pose_gate"] for event in events),
        "events": events,
        "method": "fixed recorded path; replay logged count confirmations through current camera projection; no detector, action, or track association rerun",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episodes", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = {"episodes": [replay_episode(path) for path in args.episodes]}
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
