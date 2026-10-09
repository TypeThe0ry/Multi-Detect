"""Replay actual RGB clips through the existing detector and target pool.

This is an offline synthetic visual evaluation, not a task-policy training
set, a Gazebo capture, or flight evidence. All predictions complete before
the evaluator reads visible/amodal masks and labels. The runtime boundary
accepts only image pixels and dt; it never receives a manifest or scene truth.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from multidetect.domain import BoundingBox  # noqa: E402
from multidetect.unified_tracking import (  # noqa: E402
    TargetObservation,
    UnifiedTargetPool,
)
from multidetect.vision import OnnxRawYoloConfig, OnnxRawYoloDetector  # noqa: E402
from scripts.evaluate_onboard_visual_curriculum import verify_model_contract  # noqa: E402
from scripts.render_onboard_sdf_blender_dataset import visible_bbox, yolo_label  # noqa: E402
from scripts.train_onboard_sdf_visual_candidate import (  # noqa: E402
    bound_file,
    sha256,
    verify_dataset,
)

CONTRACT = "multidetect.onboard-synthetic-visual-clip-replay-v1"
SOURCE_PATHS = (
    "scripts/replay_onboard_visual_clips.py",
    "scripts/evaluate_onboard_visual_curriculum.py",
    "scripts/train_onboard_sdf_visual_candidate.py",
    "scripts/render_onboard_sdf_blender_dataset.py",
    "src/multidetect/unified_tracking.py",
    "src/multidetect/assignment.py",
    "src/multidetect/vision.py",
    "src/multidetect/domain.py",
)


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite numeric timing")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return result


def validate_clips(document: dict) -> list[tuple[dict, list[dict]]]:
    """Validate every split and ordered clip, including unselected partitions."""
    clips = document.get("clips")
    if not isinstance(clips, list) or not clips:
        raise ValueError("continuous replay requires a nonempty clips inventory")
    inventory: dict[str, dict] = {}
    groups: dict[str, list[dict]] = {}
    for clip in clips:
        if not isinstance(clip, dict):
            raise ValueError("clip inventory entries must be objects")
        clip_id = clip.get("clip_id")
        if not isinstance(clip_id, str) or not clip_id or clip_id in inventory:
            raise ValueError("clip IDs must be nonempty and unique")
        if clip.get("split") not in {"train", "val", "test"}:
            raise ValueError("clip split is invalid")
        inventory[clip_id], groups[clip_id] = clip, []
    frame_ids: set[str] = set()
    for frame in document["frames"]:
        clip_id = frame.get("clip_id")
        if clip_id not in inventory:
            raise ValueError("frame clip_id is absent from the clips inventory")
        if frame["split"] != inventory[clip_id]["split"]:
            raise ValueError("a clip cannot cross dataset splits")
        frame_id = frame.get("frame_id")
        if not isinstance(frame_id, str) or not frame_id or frame_id in frame_ids:
            raise ValueError("frame IDs must be nonempty and unique")
        frame_ids.add(frame_id)
        groups[clip_id].append(frame)
    result = []
    for clip_id, clip in inventory.items():
        frames = groups[clip_id]
        count = clip.get("frame_count")
        if type(count) is not int or count < 2 or count != len(frames):
            raise ValueError("clip frame_count must match at least two ordered frames")
        if clip.get("frame_ids") != [frame["frame_id"] for frame in frames]:
            raise ValueError("clip frame_ids must exactly match persisted frame order")
        fps = _finite(clip.get("fps"), "fps")
        if not 0 < fps <= 240:
            raise ValueError("clip fps must be positive and bounded")
        previous = None
        for index, frame in enumerate(frames):
            if type(frame.get("frame_index")) is not int or frame["frame_index"] != index:
                raise ValueError("frame_index must start at zero and increase continuously")
            timestamp = _finite(frame.get("captured_at_s"), "captured_at_s")
            dt = _finite(frame.get("dt"), "dt")
            if index == 0:
                if timestamp != 0 or dt != 0:
                    raise ValueError("first clip timestamp and dt must be zero")
            elif (
                timestamp <= previous
                or not math.isclose(timestamp - previous, dt, rel_tol=1e-8, abs_tol=1e-9)
                or not math.isclose(dt, 1 / fps, rel_tol=1e-8, abs_tol=1e-9)
            ):
                raise ValueError("clip dt must match increasing timestamps and declared fps")
            previous = timestamp
            if frame.get("rgb_payload_sha256", frame["image_sha256"]) != frame["image_sha256"]:
                raise ValueError("RGB pixel source identity does not match image identity")
        if not math.isclose(
            _finite(clip.get("captured_at_start_s"), "captured_at_start_s"), 0, abs_tol=1e-9
        ) or not math.isclose(
            _finite(clip.get("captured_at_end_s"), "captured_at_end_s"), previous, abs_tol=1e-9
        ):
            raise ValueError("clip timing inventory does not match frame timestamps")
        result.append((clip, frames))
    return result


class VisualClipRuntime:
    """Pixels + dt only; a new instance resets the real tracker per clip."""

    def __init__(self, detector: Any, tracker: UnifiedTargetPool | None = None) -> None:
        if tuple(detector.class_names) != ("search_target",):
            raise ValueError("runtime must retain the true search_target class mapping")
        self.detector = detector
        self.tracker = tracker if tracker is not None else UnifiedTargetPool()
        if self.tracker.last_frame_time_s is not None:
            raise ValueError("each clip requires a fresh tracker without prior frame state")
        self.elapsed_s = 0.0
        self.frame_index = 0

    def predict(self, image_bgr: Any, dt: float) -> dict:
        dt = _finite(dt, "runtime dt")
        if (self.frame_index == 0 and dt != 0) or (self.frame_index > 0 and dt <= 0):
            raise ValueError("runtime dt must be zero initially and positive thereafter")
        self.elapsed_s += dt
        detections = tuple(self.detector.detect(image_bgr))
        if any(item.label != "search_target" for item in detections):
            raise ValueError("runtime detector emitted an unexpected or aliased label")
        update = self.tracker.update(
            frame_id=f"rgb-{self.frame_index:06d}",
            captured_at_s=self.elapsed_s,
            observations=tuple(TargetObservation.from_detection(item) for item in detections),
        )
        self.frame_index += 1
        return {
            "runtime_elapsed_s": self.elapsed_s,
            "input_fields": ["image_bgr", "dt"],
            "detector_input_fields": ["image_bgr"],
            "tracker_input_fields": ["RGB-derived detections", "dt-derived elapsed time"],
            "detections": [
                {
                    "label": item.label,
                    "confidence": item.confidence,
                    "bbox_xyxy_normalized": item.bbox.rounded(9),
                    "model_version": item.model_version,
                }
                for item in detections
            ],
            "tracks": [
                {
                    "track_id": track.track_id,
                    "label": track.label,
                    "state": track.state.value,
                    "bbox_xyxy_normalized": track.bbox.rounded(9),
                    "predicted_bbox_xyxy_normalized": track.predicted_bbox.rounded(9),
                    "last_seen_at_s": track.last_seen_at_s,
                    "missed_frame_count": track.missed_frame_count,
                    "observation_count": track.observation_count,
                    "confidence": track.confidence,
                }
                for track in update.tracks
            ],
            "created_track_ids": update.created_track_ids,
            "recovered_track_ids": update.recovered_track_ids,
            "lost_track_ids": update.lost_track_ids,
            "removed_track_ids": update.removed_track_ids,
            "dropped_observation_count": update.dropped_observation_count,
        }


def _read_rgb(path: Path, frame: dict) -> Any:
    import cv2

    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None or image.shape != (frame["height"], frame["width"], 3):
        raise ValueError("RGB dimensions do not match the persisted frame")
    return image


def _offline_truth(base: Path, frame: dict) -> dict:
    """Evaluation only; called after every runtime prediction has completed."""
    import cv2
    import numpy as np

    height, width = frame["height"], frame["width"]

    def read_mask(kind: str) -> Any:
        path = bound_file(base, frame[f"{kind}_path"], frame[f"{kind}_sha256"])
        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if image is None or image.shape != (height, width) or not np.isin(image, [0, 255]).all():
            raise ValueError("evaluation masks must be binary and match RGB geometry")
        return image

    mask = read_mask("mask")
    box = visible_bbox(mask.reshape(-1).tobytes(), width, height)
    label = bound_file(base, frame["label_path"], frame["label_sha256"]).read_text("utf-8")
    if label != yolo_label(box, width, height):
        raise ValueError("offline class-zero labels do not match the actual visible mask")
    present = frame.get("target_present")
    if type(present) is not bool or (not present and box is not None):
        raise ValueError("offline target presence conflicts with visible mask evidence")
    amodal = read_mask("amodal_mask")
    amodal_count = int(np.count_nonzero(amodal))
    visible_count = int(np.count_nonzero(mask))
    intersection = int(np.count_nonzero((mask > 0) & (amodal > 0)))
    if visible_count != intersection:
        raise ValueError("visible pixels outside amodal reference: inconsistent evaluation mask")
    if not present and amodal_count:
        raise ValueError("visible/amodal evaluation mask evidence is inconsistent")
    normalized = (
        None if box is None else [box[0] / width, box[1] / height, box[2] / width, box[3] / height]
    )
    return {
        "bbox_xyxy_normalized": normalized,
        "center_xy_normalized": None
        if normalized is None
        else [(normalized[0] + normalized[2]) / 2, (normalized[1] + normalized[3]) / 2],
        "visible_pixels": visible_count,
        "amodal_pixels": amodal_count,
        "visibility_fraction": intersection / amodal_count if amodal_count else None,
        "fully_occluded": bool(present and amodal_count > 0 and visible_count == 0),
        "absent": not present,
        "out_of_frame": bool(present and amodal_count == 0),
        "source": "offline_visible_and_amodal_3d_mask_pixels",
    }


def evaluate_clip(frames: list[dict], predictions: list[dict], truths: list[dict]) -> dict:
    counters: Counter = Counter()
    rows, occlusion_events = [], []
    previous_track_id = None
    ever_matched = False
    visible_gap = False
    pending_occlusion = None
    was_occluded = False
    for frame, prediction, truth in zip(frames, predictions, truths, strict=True):
        timestamp = float(frame["captured_at_s"])
        box = truth["bbox_xyxy_normalized"]
        gt = BoundingBox(*box) if box is not None else None
        fresh_tracks = [
            track
            for track in prediction["tracks"]
            if track["missed_frame_count"] == 0
            and math.isclose(track["last_seen_at_s"], prediction["runtime_elapsed_s"], abs_tol=1e-9)
        ]
        detection_iou = (
            max(
                (
                    gt.iou(BoundingBox(*item["bbox_xyxy_normalized"]))
                    for item in prediction["detections"]
                ),
                default=0,
            )
            if gt
            else 0
        )
        matched_tracks = (
            []
            if gt is None
            else [
                (gt.iou(BoundingBox(*track["bbox_xyxy_normalized"])), track["track_id"])
                for track in fresh_tracks
            ]
        )
        best = max(matched_tracks, default=(0, None))
        matched_id = best[1] if best[0] >= 0.5 else None
        detection_tp = int(gt is not None and detection_iou >= 0.5)
        track_tp = int(matched_id is not None)
        counters.update(
            {
                "frames": 1,
                "detector_detections": len(prediction["detections"]),
                "currently_observed_tracks": len(fresh_tracks),
                "visible_gt_frames": int(gt is not None),
                "detection_tp": detection_tp,
                "detection_fp": len(prediction["detections"]) - detection_tp,
                "detection_fn": int(gt is not None) - detection_tp,
                "track_matched_frames": track_tp,
                "track_fp": len(fresh_tracks) - track_tp,
                "track_missed_visible_frames": int(gt is not None) - track_tp,
                "fully_occluded_frames": int(truth["fully_occluded"]),
                "absent_frames": int(truth["absent"]),
                "out_of_frame_frames": int(truth["out_of_frame"]),
                "partial_occlusion_frames": int(
                    gt is not None and truth["visibility_fraction"] < 0.95
                ),
            }
        )
        if matched_id is not None:
            if previous_track_id is not None and previous_track_id != matched_id:
                counters["id_switches"] += 1
            if ever_matched and visible_gap:
                counters["fragmentations"] += 1
            previous_track_id, ever_matched, visible_gap = matched_id, True, False
        elif gt is not None and ever_matched:
            visible_gap = True
        if truth["fully_occluded"] and not was_occluded:
            if pending_occlusion is not None:
                pending_occlusion["status"] = "another_occlusion_before_reacquisition"
            pending_occlusion = {
                "occlusion_start_s": timestamp,
                "reappearance_at_s": None,
                "reacquired_at_s": None,
                "reacquisition_delay_s": None,
                "pre_occlusion_track_id": previous_track_id,
                "reacquired_track_id": None,
                "same_identity_recovered": None,
                "status": "awaiting_reappearance",
            }
            occlusion_events.append(pending_occlusion)
        if pending_occlusion is not None and (truth["absent"] or truth["out_of_frame"]):
            pending_occlusion["status"] = (
                "target_absent_before_reacquisition"
                if truth["absent"]
                else "target_left_image_before_reacquisition"
            )
            pending_occlusion = None
        if pending_occlusion is not None and gt is not None:
            if pending_occlusion["reappearance_at_s"] is None:
                pending_occlusion["reappearance_at_s"] = timestamp
                pending_occlusion["status"] = "awaiting_reacquisition"
            if matched_id is not None:
                pending_occlusion.update(
                    {
                        "reacquired_at_s": timestamp,
                        "reacquisition_delay_s": timestamp - pending_occlusion["reappearance_at_s"],
                        "reacquired_track_id": matched_id,
                        "same_identity_recovered": (
                            matched_id == pending_occlusion["pre_occlusion_track_id"]
                            if pending_occlusion["pre_occlusion_track_id"] is not None
                            else None
                        ),
                        "status": "reacquired",
                    }
                )
                pending_occlusion = None
        was_occluded = truth["fully_occluded"]
        rows.append(
            {
                "frame_id": frame["frame_id"],
                "frame_index": frame["frame_index"],
                "captured_at_s": timestamp,
                "dt": frame["dt"],
                "RGB_sha256": frame["image_sha256"],
                "prediction": prediction,
                "offline_evaluation": {
                    **truth,
                    "matched_track_id": matched_id,
                    "matched_track_iou": best[0],
                    "detector_best_iou": detection_iou,
                },
            }
        )
    for key in ("id_switches", "fragmentations"):
        counters.setdefault(key, 0)
    counters["occlusion_events"] = len(occlusion_events)
    counters["reacquired_occlusion_events"] = sum(
        event["reacquired_at_s"] is not None for event in occlusion_events
    )
    counters["occlusion_events_with_preexisting_track"] = sum(
        event["pre_occlusion_track_id"] is not None for event in occlusion_events
    )
    counters["same_identity_recovered_events"] = sum(
        event["same_identity_recovered"] is True for event in occlusion_events
    )
    if pending_occlusion is not None:
        pending_occlusion["status"] = (
            "clip_ended_before_reappearance"
            if pending_occlusion["reappearance_at_s"] is None
            else "not_reacquired_after_reappearance"
        )
    return {
        "observed_counters": dict(counters),
        "occlusion_reacquisition": occlusion_events,
        "frames": rows,
    }


def _detector(model: Path, digest: str) -> OnnxRawYoloDetector:
    verify_model_contract(model)
    return OnnxRawYoloDetector(
        OnnxRawYoloConfig(
            model_path=model,
            class_names=("search_target",),
            input_width=640,
            input_height=640,
            confidence_threshold=0.25,
            providers=("CPUExecutionProvider",),
            model_version=f"offline-synthetic-search-target:{digest}",
        )
    )


def replay(
    manifest: Path,
    model: Path,
    output: Path,
    *,
    split: str = "test",
    model_sha256: str | None = None,
    detector_factory: Callable = _detector,
    tracker_factory: Callable = UnifiedTargetPool,
) -> dict:
    if output.exists():
        raise ValueError("refusing to overwrite an existing replay report")
    if split not in {"train", "val", "test"}:
        raise ValueError("invalid replay split")
    manifest, model = manifest.resolve(strict=True), model.resolve(strict=True)
    manifest_hash, model_hash = sha256(manifest), sha256(model)
    if model_sha256 is not None and model_hash != model_sha256:
        raise ValueError("model SHA-256 does not match the explicit expected identity")
    document, _yaml = verify_dataset(manifest)
    clips = validate_clips(document)
    for frame in document["frames"]:
        bound_file(manifest.parent, frame["amodal_mask_path"], frame["amodal_mask_sha256"])
    selected = [(clip, frames) for clip, frames in clips if clip["split"] == split]
    if not selected:
        raise ValueError("requested split contains no clips")
    sources = {path: sha256(ROOT / path) for path in SOURCE_PATHS}
    base = manifest.parent
    detector = detector_factory(model, model_hash)
    predicted = []
    tracker_config = None
    for clip, frames in selected:
        tracker = tracker_factory()
        tracker_config = asdict(tracker.config)
        runtime = VisualClipRuntime(detector, tracker)
        predictions = []
        for frame in frames:
            path = bound_file(base, frame["image_path"], frame["image_sha256"])
            image = _read_rgb(path, frame)
            predictions.append(runtime.predict(image, frame["dt"]))
        predicted.append((clip, frames, predictions))
    # No labels, visible masks, amodal masks or scene parameters were parsed
    # by the runtime above. Evaluation begins only after its final prediction.
    results = []
    totals: Counter = Counter()
    for clip, frames, predictions in predicted:
        truths = [_offline_truth(base, frame) for frame in frames]
        result = evaluate_clip(frames, predictions, truths)
        totals.update(result["observed_counters"])
        results.append(
            {"clip_id": clip["clip_id"], "split": clip["split"], "tracker_reset": True, **result}
        )
    verify_dataset(manifest)
    for frame in document["frames"]:
        bound_file(base, frame["amodal_mask_path"], frame["amodal_mask_sha256"])
    if sha256(manifest) != manifest_hash or sha256(model) != model_hash:
        raise ValueError("model or dataset changed during replay; no report sealed")
    if any(sha256(ROOT / path) != digest for path, digest in sources.items()):
        raise ValueError("runtime source changed during replay; no report sealed")
    report = {
        "contract": CONTRACT,
        "scope": "offline_synthetic_visual_temporal_evaluation",
        "manifest": {"path": str(manifest), "sha256": manifest_hash},
        "model": {
            "path": str(model),
            "sha256": model_hash,
            "expected_sha256_supplied": model_sha256 is not None,
            "class_names": ["search_target"],
            "label_aliases": {},
            "native_output_contract": "raw_YOLO_batch1_4plus1_features",
            "configured_input_size": [640, 640],
            "configured_confidence_threshold": 0.25,
            "configured_providers": ["CPUExecutionProvider"],
        },
        "runtime_sources": sources,
        "dataset_source_identities": dict(
            sorted(
                {
                    path: digest
                    for frame in document["frames"]
                    for path, digest in frame["source_hashes"].items()
                }.items()
            )
        ),
        "input_fields": ["image_bgr", "dt"],
        "truth_into_runtime": False,
        "predictions_completed_before_truth_evaluation": True,
        "tracker": {
            "implementation": "multidetect.unified_tracking.UnifiedTargetPool",
            "configuration": tracker_config,
            "appearance_encoder_used": False,
            "camera_motion_truth_used": False,
            "reset_per_clip": True,
        },
        "timestamp_semantics": "declared_rendered_clip_sample_time_not_sensor_clock",
        "metric_definition": {
            "matching": "single visible mask-derived GT; current observation tracks only; IoU>=0.5",
            "partial_occlusion_frames": (
                "visible GT and measured visible_fraction<0.95; renderer's any-pixel partial "
                "category may count additional near-clear frames"
            ),
            "id_switches": "matched identity changes within one clip, including across gaps",
            "fragmentations": "matched/unmatched/matched runs while GT is visible",
            "reacquisition": (
                "first matched frame minus first visible frame after measured full occlusion"
            ),
            "zero_event_counts": "absence of measured events is not robustness evidence",
        },
        "split": split,
        "clip_count": len(results),
        "observed_counters": dict(totals),
        "clips": results,
        "training_ready": False,
        "task_policy_training": False,
        "gazebo_capture": False,
        "autonomous_flight_verified": False,
        "candidate_promoted": False,
        "hil_real_evidence": False,
        "jetson_hardware_evidence": False,
        "outdoor_gnss_evidence": False,
        "final_release_allowed": False,
        "limitations": [
            "offline compute replay is not realtime latency or flight acceptance",
            "one designated mask-derived GT instance per clip",
            "generic search_target motion profile; no implicit vehicle semantics",
            "no appearance encoder or camera-motion compensation in this replay",
            "zero measured occlusions cannot establish occlusion robustness",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2, allow_nan=False) + "\n")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--split", choices=("train", "val", "test"), default="test")
    parser.add_argument("--model-sha256", help="Optional external expected model identity")
    args = parser.parse_args(argv)
    report = replay(
        args.manifest, args.model, args.output, split=args.split, model_sha256=args.model_sha256
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "clip_count": report["clip_count"],
                "observed_counters": report["observed_counters"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
