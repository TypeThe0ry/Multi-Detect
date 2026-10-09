"""Prepare development-only RGB/Prompt probes from sealed Blender train clips.

This does not infer, train, create flight labels, or replay a sensor recording.
Only prompt, exact RGB bytes and factual context missingness may enter a model.
All mask/label/scene provenance is isolated in offline evaluation metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from scripts import evaluate_onboard_visual_curriculum as evaluator  # noqa: E402
from scripts import render_onboard_sdf_blender_curriculum as curriculum  # noqa: E402
from scripts import render_onboard_sdf_blender_dataset as renderer  # noqa: E402
from scripts import replay_onboard_visual_clips as replay  # noqa: E402
from scripts import train_onboard_sdf_visual_candidate as trainer  # noqa: E402

CONTRACT = "multidetect.offline-vlm-rgb-shadow-cases-v1"
DEFAULT_MANIFEST = "artifacts/training/onboard-blender-sdf-clip-suite-20261007-r3/manifest.json"
DEFAULT_MANIFEST_SHA256 = "acc3ce0c867a78255fd7e2a1ef4c454d35da9fc97c6d2b8285520f6c8aac3f6e"
DEFAULT_OUTPUT = "artifacts/evaluation/onboard-vlm-shadow-cases-20261007-r1"
CLOCK_SEMANTICS = "declared_rendered_clip_sample_time_not_sensor_clock"
SOURCE_MODULES = {
    "scripts/evaluate_onboard_visual_curriculum.py": evaluator,
    "scripts/render_onboard_sdf_blender_curriculum.py": curriculum,
    "scripts/render_onboard_sdf_blender_dataset.py": renderer,
    "scripts/replay_onboard_visual_clips.py": replay,
    "scripts/train_onboard_sdf_visual_candidate.py": trainer,
}
SOURCE_PATHS = tuple(
    sorted(
        {
            "scripts/build_onboard_vlm_shadow_cases.py",
            *SOURCE_MODULES,
            *evaluator.SOURCE_PATHS,
            *replay.SOURCE_PATHS,
        }
    )
)
FALSE_FLAGS = (
    "training_ready",
    "gazebo_capture",
    "sensor_recording_available",
    "model_inference_performed",
    "training_performed",
    "flight_action_labels_created",
    "flight",
    "autonomous_flight_verified",
    "hardware_acceptance",
    "candidate_promoted",
    "onboard_policy_reference_changed",
    "final_release_allowed",
)
MISSING_FIELDS = (
    "telemetry",
    "aircraft_position_gps",
    "aircraft_position_ned",
    "aircraft_attitude",
    "aircraft_velocity",
    "battery_state",
    "camera_intrinsics",
    "camera_extrinsics",
    "depth",
    "geofence",
    "flight_authorization",
    "mission_execution_history",
)
FRAME_FIELDS = {
    "frame_id",
    "path",
    "sha256",
    "captured_at_s",
    "clock_domain",
    "source_clip_id",
    "width",
    "height",
    "encoding",
    "split",
}
CASE_FIELDS = {
    "case_id",
    "prompt",
    "prompt_issued_at",
    "frames",
    "context_missing_fields",
    "evaluation_metadata",
}


@dataclass(frozen=True)
class CasePlan:
    """Human-authored task requests, never action or navigation gold labels."""

    case_id: str
    clip_id: str
    frame_indices: tuple[int, ...]
    prompt: str
    comparison_group: str
    capability: str


OBSERVE_PROMPT = (
    "观察画面中低矮、带突起的实体与周围环境，报告能可靠看见的细节及不确定性。"
    "如果需要进一步观察，说明还缺什么视觉证据；不要输出飞控指令。"
)
HISTORY_PROMPT = (
    "持续观察低矮实体的可见性与变化，区分当前可见、历史证据与推测。"
    "不能确认的身份或位置请明确未知，并说明还缺什么观察；不要输出飞控指令。"
)
DEFAULT_CASE_PLANS = (
    CasePlan(
        "near-visible-observe",
        "train-suite-near-00000",
        (0,),
        OBSERVE_PROMPT,
        "same_rgb_different_prompt",
        "visible_entity_description",
    ),
    CasePlan(
        "near-visible-obstacle",
        "train-suite-near-00000",
        (0,),
        "检查视线中有哪些结构可能挡住观察。仅依据图像描述遮挡与观察局限，"
        "不要把二维空隙认定为可安全飞行的通道；说明确认通行还缺什么信息。",
        "same_rgb_different_prompt",
        "obstacle_and_observation_limits",
    ),
    CasePlan(
        "near-visible-location-unknown",
        "train-suite-near-00000",
        (0,),
        "任务需要记录被观察实体的位置。检查当前输入能支持哪种定位描述，"
        "分别说明画面内相对位置与真实 GPS/NED 或米制位置是否可确定，不能确定就明确未知。",
        "same_rgb_different_prompt",
        "metric_localization_abstention",
    ),
    CasePlan(
        "near-visible-unknown-person",
        "train-suite-near-00000",
        (0,),
        "检查是否有足够视觉证据可靠确认一名行人。描述支持或不足的证据；"
        "不要把几何色块直接当成真人，无法确认时请明确未知。",
        "same_rgb_different_prompt",
        "unconfirmed_semantic_target",
    ),
    CasePlan(
        "near-hidden-current-only",
        "train-suite-near-00000",
        (8,),
        HISTORY_PROMPT,
        "same_prompt_same_current_different_history",
        "current_only_visibility_uncertainty",
    ),
    CasePlan(
        "near-hidden-with-history",
        "train-suite-near-00000",
        (0, 3, 5, 8),
        HISTORY_PROMPT,
        "same_prompt_same_current_different_history",
        "ordered_history_and_occlusion",
    ),
    CasePlan(
        "near-reappeared-with-history",
        "train-suite-near-00000",
        (0, 3, 5, 8, 10, 13),
        HISTORY_PROMPT,
        "same_prompt_extended_history",
        "reappearance_without_identity_gold",
    ),
    CasePlan(
        "far-small-observe",
        "train-suite-far-00002",
        (0,),
        OBSERVE_PROMPT,
        "same_prompt_different_observation",
        "small_entity_and_visual_uncertainty",
    ),
)


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _strict_json(payload: bytes) -> dict:
    def pairs(items: list[tuple[str, Any]]) -> dict:
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def nonfinite(value: str) -> None:
        raise ValueError(f"nonfinite JSON number: {value}")

    def finite(value: Any) -> None:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("nonfinite JSON number")
        if isinstance(value, dict):
            for child in value.values():
                finite(child)
        elif isinstance(value, list):
            for child in value:
                finite(child)

    result = json.loads(payload.decode("utf-8"), object_pairs_hook=pairs, parse_constant=nonfinite)
    if not isinstance(result, dict):
        raise ValueError("JSON document must be an object")
    finite(result)
    return result


def _relative(path: Path) -> str:
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT.resolve()):
        raise ValueError("source and output files must remain inside the repository")
    return resolved.relative_to(ROOT.resolve()).as_posix()


def _snapshot(path: Path, snapshots: dict[Path, str], expected: str | None = None) -> str:
    path = path.resolve(strict=True)
    digest = trainer.sha256(path)
    if (expected is not None and digest != expected) or (
        path in snapshots and snapshots[path] != digest
    ):
        raise ValueError(f"file identity changed: {path}")
    snapshots[path] = digest
    return digest


def _verify_snapshots(snapshots: dict[Path, str]) -> None:
    for path, expected in snapshots.items():
        if not path.is_file() or trainer.sha256(path) != expected:
            raise ValueError(f"file identity changed during preparation: {path}")


def _source_snapshots() -> tuple[dict[str, str], dict[Path, str]]:
    snapshots: dict[Path, str] = {}
    sources = {relative: _snapshot(ROOT / relative, snapshots) for relative in SOURCE_PATHS}
    _snapshot(Path(__file__), snapshots, sources["scripts/build_onboard_vlm_shadow_cases.py"])
    for relative, module in SOURCE_MODULES.items():
        _snapshot(Path(module.__file__), snapshots, sources[relative])
    return sources, snapshots


def _normalise_frame(raw: dict) -> dict:
    frame = deepcopy(raw)
    model, measured = frame.get("target_model"), frame.get("visibility_measurement")
    if not isinstance(model, dict) or not isinstance(measured, dict):
        raise ValueError("selected clips require source-bound models and measured visibility")
    frame["curriculum"] = {
        "target_model_path": model.get("path"),
        "target_model_sha256": model.get("sha256"),
        "target_geometry_sha256": model.get("geometry_sha256"),
        **measured,
    }
    return frame


def _scope(frame: dict) -> tuple:
    model = frame.get("target_model", {})
    return (
        frame["clip_id"],
        frame["split"],
        frame["world_path"],
        frame["world_sha256"],
        model.get("path"),
        model.get("sha256"),
        model.get("geometry_sha256"),
        frame.get("timestamp_semantics"),
        tuple(sorted(frame["source_hashes"].items())),
    )


def _plans_and_frames(plans: tuple[CasePlan, ...], clips: dict[str, list[dict]]) -> list:
    if not 6 <= len(plans) <= 10 or len({plan.case_id for plan in plans}) != len(plans):
        raise ValueError("shadow probes require 6..10 uniquely identified cases")
    selections = []
    for plan in plans:
        if not plan.case_id or not plan.prompt.strip() or plan.clip_id not in clips:
            raise ValueError("case requires a prompt and an existing source clip")
        if (
            not 1 <= len(plan.frame_indices) <= 8
            or any(type(index) is not int or index < 0 for index in plan.frame_indices)
            or tuple(sorted(set(plan.frame_indices))) != plan.frame_indices
        ):
            raise ValueError("case frame indices must be unique, bounded and ordered")
        try:
            selected = [clips[plan.clip_id][index] for index in plan.frame_indices]
        except IndexError as exc:
            raise ValueError("case frame index exceeds source clip") from exc
        if any(frame["split"] != "train" for frame in selected):
            raise ValueError("development probe design may use only original train frames")
        if any(_scope(frame) != _scope(selected[0]) for frame in selected):
            raise ValueError("case cannot cross clip, source, geometry or clock scope")
        if selected[0].get("timestamp_semantics") != CLOCK_SEMANTICS:
            raise ValueError("rendered clip timing must not masquerade as a sensor clock")
        selections.append((plan, selected))
    return selections


def _validate_rgb(payload: bytes, frame: dict) -> None:
    from io import BytesIO

    from PIL import Image

    if (
        payload[:8] != b"\x89PNG\r\n\x1a\n"
        or payload[12:16] != b"IHDR"
        or payload[24:26] != bytes((8, 2))
    ):
        # Pillow can silently downconvert a true-color 16-bit PNG into RGB8.
        # The original encoded IHDR must itself declare RGB8, not a conversion.
        raise ValueError("probes require the original lossless RGB8 PNG payload")
    with Image.open(BytesIO(payload)) as image:
        image.verify()
    with Image.open(BytesIO(payload)) as image:
        image.load()
        if (
            image.format != "PNG"
            or image.mode != "RGB"
            or type(frame.get("width")) is not int
            or type(frame.get("height")) is not int
            or image.size != (frame["width"], frame["height"])
        ):
            raise ValueError("actual RGB mode or dimensions disagree with the bound frame")


def model_case_input(case: dict) -> dict:
    """Explicit allowlist: offline evaluation/provenance never goes to the VLM."""
    if set(case) != CASE_FIELDS or any(set(frame) != FRAME_FIELDS for frame in case["frames"]):
        raise ValueError("case contains unknown or model-visible truth fields")
    return {
        "prompt": case["prompt"],
        "frames": deepcopy(case["frames"]),
        "context_missing_fields": list(case["context_missing_fields"]),
    }


def validate_shadow_cases(manifest: Path, expected_sha256: str | None = None) -> dict:
    """Verify persisted case bytes and their original frame/split/time bindings."""
    payload = manifest.read_bytes()
    if expected_sha256 is not None and _digest(payload) != expected_sha256:
        raise ValueError("shadow manifest byte identity mismatch")
    document = _strict_json(payload)
    if (
        document.get("contract") != CONTRACT
        or document.get("path_resolution") != "repository_root"
        or Path(document.get("repository_root", "")).resolve() != ROOT.resolve()
        or document.get("model_input_allowlist")
        != ["actual_rgb_bytes", "prompt", "context_missing_fields"]
        or document.get("evaluation_metadata_model_visible") is not False
        or document.get("aircraft_context") is not None
        or document.get("timestamp_semantics") != CLOCK_SEMANTICS
        or document.get("prompt_issued_at_semantics") != "builder_host_utc_not_sensor_utc"
        or any(document.get(key) is not False for key in FALSE_FLAGS)
    ):
        raise ValueError("shadow contract must retain offline non-flight boundaries")
    reference = document["source_manifest"]
    source_path = trainer.bound_file(ROOT, reference["path"], reference["sha256"])
    source = _strict_json(source_path.read_bytes())
    originals = {frame["frame_id"]: frame for frame in source["frames"]}
    cases = document.get("cases")
    if (
        not isinstance(cases, list)
        or not 6 <= len(cases) <= 10
        or document.get("case_count") != len(cases)
        or len({case["case_id"] for case in cases}) != len(cases)
    ):
        raise ValueError("shadow case inventory mismatch")
    for case in cases:
        model_case_input(case)
        issued = datetime.fromisoformat(case["prompt_issued_at"])
        if issued.tzinfo is None or issued.utcoffset().total_seconds() != 0:
            raise ValueError("prompt_issued_at must be host UTC, not a sensor timestamp")
        if not isinstance(case["prompt"], str) or not case["prompt"].strip():
            raise ValueError("case prompt must be nonempty")
        if case["context_missing_fields"] != list(MISSING_FIELDS):
            raise ValueError("missing telemetry context must not be silently synthesized")
        metadata = case["evaluation_metadata"]
        if (
            metadata.get("development_probe_only") is not True
            or metadata.get("blind_holdout") is not False
            or any(
                key not in metadata or metadata[key] is not None
                for key in ("gold_flight_action", "gold_metric_position", "gold_entity_identity")
            )
        ):
            raise ValueError("development probes cannot manufacture flight or identity gold")
        frames = case["frames"]
        if not 1 <= len(frames) <= 8:
            raise ValueError("case must retain 1..8 ordered frames")
        original_frames, previous = [], None
        provenance = case["evaluation_metadata"]["source_provenance"]
        if len(provenance) != len(frames):
            raise ValueError("frame provenance inventory mismatch")
        for frame, bound in zip(frames, provenance, strict=True):
            raw = originals[frame["frame_id"]]
            expected_clock = f"offline-rendered:{reference['sha256']}:{raw['clip_id']}"
            if (
                frame["split"] != "train"
                or raw["split"] != "train"
                or bound["original_split"] != raw["split"]
                or bound["source_frame_id"] != raw["frame_id"]
                or bound["source_image_sha256"] != raw["image_sha256"]
                or bound["source_frame_index"] != raw["frame_index"]
                or bound["source_clip_id"] != raw["clip_id"]
                or bound["source_image_path"] != _relative(source_path.parent / raw["image_path"])
                or bound["source_sample_dt_s"] != raw["dt"]
                or bound["timestamp_semantics"] != raw["timestamp_semantics"]
                or bound["offline_recording_identity"] != f"{reference['sha256']}:{raw['clip_id']}"
                or bound["world"] != {"path": raw["world_path"], "sha256": raw["world_sha256"]}
                or bound["target_geometry"]
                != {key: raw["target_model"][key] for key in ("path", "sha256", "geometry_sha256")}
                or bound["measured_condition"]
                != raw["visibility_measurement"]["measured_condition"]
                or bound["visible_fraction"] != raw["visibility_measurement"]["visible_fraction"]
                or frame["sha256"] != raw["image_sha256"]
                or frame["source_clip_id"] != raw["clip_id"]
                or frame["clock_domain"] != expected_clock
                or frame["captured_at_s"] != raw["captured_at_s"]
                or type(frame["captured_at_s"]) not in (int, float)
                or frame["captured_at_s"] < 0
                or frame["encoding"] != "png"
                or (frame["width"], frame["height"]) != (raw["width"], raw["height"])
                or (previous is not None and frame["captured_at_s"] <= previous)
            ):
                raise ValueError("case altered its original RGB, split, scope or timestamp binding")
            previous = frame["captured_at_s"]
            path = trainer.bound_file(ROOT, frame["path"], frame["sha256"])
            if not path.is_relative_to(manifest.parent.resolve()):
                raise ValueError("RGB copies must remain in the exclusive shadow artifact")
            _validate_rgb(path.read_bytes(), frame)
            original_frames.append(raw)
        if any(_scope(frame) != _scope(original_frames[0]) for frame in original_frames):
            raise ValueError("case crosses original source scope")
    return document


def build_shadow_cases(
    manifest: Path,
    expected_manifest_sha256: str,
    output_directory: Path,
    *,
    plans: tuple[CasePlan, ...] = DEFAULT_CASE_PLANS,
    prompt_issued_at: str | None = None,
) -> dict:
    """Publish new immutable copied RGB cases, or leave only unsealed partials."""
    manifest = manifest.resolve(strict=True)
    output = output_directory.resolve()
    _relative(manifest)
    _relative(output)
    if output.exists():
        raise FileExistsError("refusing to overwrite an existing shadow artifact")
    if manifest.is_relative_to(output) or output.is_relative_to(manifest.parent):
        raise ValueError("shadow output must be independent of the source dataset")
    sources, snapshots = _source_snapshots()
    manifest_hash = _snapshot(manifest, snapshots, expected_manifest_sha256)
    parsed_bytes = manifest.read_bytes()
    if _digest(parsed_bytes) != manifest_hash:
        raise ValueError("source manifest changed before parsing")
    strict = _strict_json(parsed_bytes)
    verified, yaml = trainer.verify_dataset(manifest)
    if verified != strict:
        raise ValueError("source manifest changed between parsers")
    if any(key in strict and strict[key] is not False for key in FALSE_FLAGS):
        raise ValueError("source must retain offline Blender claim boundaries")
    groups = replay.validate_clips(strict)
    for clip, clip_frames in groups:
        if any(_scope(frame) != _scope(clip_frames[0]) for frame in clip_frames):
            raise ValueError("source clip mixes world, model, split or clock identities")
        if any(
            key in clip and clip[key] != clip_frames[0].get(key)
            for key in ("world_path", "world_sha256", "source_hashes", "timestamp_semantics")
        ):
            raise ValueError("source clip inventory disagrees with frame identity")
    _snapshot(yaml, snapshots, strict["dataset_yaml_sha256"])
    normalized = [_normalise_frame(frame) for frame in strict["frames"]]
    for frame in normalized:
        for relative, digest in frame["source_hashes"].items():
            _snapshot(trainer.bound_file(ROOT, relative, digest), snapshots, digest)
        for kind in ("image", "label", "mask", "amodal_mask"):
            _snapshot(
                trainer.bound_file(manifest.parent, frame[f"{kind}_path"], frame[f"{kind}_sha256"]),
                snapshots,
                frame[f"{kind}_sha256"],
            )
    geometry = evaluator.verify_geometry_provenance({"frames": normalized})
    selections = _plans_and_frames(plans, {clip["clip_id"]: frames for clip, frames in groups})
    issued_at = prompt_issued_at or datetime.now(timezone.utc).isoformat()
    issued = datetime.fromisoformat(issued_at)
    if issued.tzinfo is None or issued.utcoffset().total_seconds() != 0:
        raise ValueError("prompt_issued_at must be host UTC")

    import cv2

    inputs, measurements = {}, {}
    for _plan, selected in selections:
        for raw in selected:
            frame_id = raw["frame_id"]
            if frame_id in inputs:
                continue
            path = trainer.bound_file(manifest.parent, raw["image_path"], raw["image_sha256"])
            payload = path.read_bytes()
            if _digest(payload) != raw["image_sha256"]:
                raise ValueError("source RGB changed before decode")
            _validate_rgb(payload, raw)
            condition, fraction = evaluator.verify_visibility(
                _normalise_frame(raw), manifest.parent, cv2
            )
            if type(raw.get("target_visible")) is not bool or raw["target_visible"] != (
                raw["visible_target_pixels"] > 0
            ):
                raise ValueError("source visibility flag disagrees with actual mask")
            inputs[frame_id] = payload
            measurements[frame_id] = {"measured_condition": condition, "visible_fraction": fraction}
    _verify_snapshots(snapshots)
    output.mkdir(parents=True, exist_ok=False)
    copied = {}
    for index, (frame_id, payload) in enumerate(inputs.items()):
        path = output / "images" / f"rgb-{index:03d}.png"
        path.parent.mkdir(exist_ok=True)
        with path.open("xb") as stream:
            stream.write(payload)
        _snapshot(path, snapshots, _digest(payload))
        copied[frame_id] = _relative(path)
    reference = {"path": _relative(manifest), "sha256": manifest_hash, "bytes": len(parsed_bytes)}
    cases = []
    for plan, selected in selections:
        clock = f"offline-rendered:{manifest_hash}:{plan.clip_id}"
        frames, provenance = [], []
        for raw in selected:
            frames.append(
                {
                    "frame_id": raw["frame_id"],
                    "path": copied[raw["frame_id"]],
                    "sha256": raw["image_sha256"],
                    "captured_at_s": raw["captured_at_s"],
                    "clock_domain": clock,
                    "source_clip_id": raw["clip_id"],
                    "width": raw["width"],
                    "height": raw["height"],
                    "encoding": "png",
                    "split": raw["split"],
                }
            )
            provenance.append(
                {
                    "source_frame_id": raw["frame_id"],
                    "source_clip_id": raw["clip_id"],
                    "source_frame_index": raw["frame_index"],
                    "original_split": raw["split"],
                    "source_image_path": _relative(manifest.parent / raw["image_path"]),
                    "source_image_sha256": raw["image_sha256"],
                    "source_sample_dt_s": raw["dt"],
                    "timestamp_semantics": raw["timestamp_semantics"],
                    "offline_recording_identity": f"{manifest_hash}:{raw['clip_id']}",
                    "world": {"path": raw["world_path"], "sha256": raw["world_sha256"]},
                    "target_geometry": geometry[raw["target_model"]["path"]],
                    **measurements[raw["frame_id"]],
                }
            )
        cases.append(
            {
                "case_id": plan.case_id,
                "prompt": plan.prompt,
                "prompt_issued_at": issued_at,
                "frames": frames,
                "context_missing_fields": list(MISSING_FIELDS),
                "evaluation_metadata": {
                    "comparison_group": plan.comparison_group,
                    "capability": plan.capability,
                    "development_probe_only": True,
                    "blind_holdout": False,
                    "gold_flight_action": None,
                    "gold_metric_position": None,
                    "gold_entity_identity": None,
                    "source_provenance": provenance,
                },
            }
        )
    result = {
        "contract": CONTRACT,
        "path_resolution": "repository_root",
        "repository_root": ROOT.resolve().as_posix(),
        "source_manifest": reference,
        "runtime_sources": sources,
        "case_count": len(cases),
        "unique_rgb_count": len(copied),
        "source_split": "train",
        "timestamp_semantics": CLOCK_SEMANTICS,
        "prompt_issued_at_semantics": "builder_host_utc_not_sensor_utc",
        "model_input_allowlist": ["actual_rgb_bytes", "prompt", "context_missing_fields"],
        "evaluation_metadata_model_visible": False,
        "aircraft_context": None,
        "cases": cases,
        **dict.fromkeys(FALSE_FLAGS, False),
    }
    sealed_bytes = (
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    expected = _digest(sealed_bytes)
    pending = output / "manifest.pending.json"
    with pending.open("xb") as stream:
        stream.write(sealed_bytes)
    _snapshot(pending, snapshots, expected)
    _verify_snapshots(snapshots)
    if pending.read_bytes() != sealed_bytes:
        raise ValueError("pending manifest changed before verification")
    validate_shadow_cases(pending, expected)
    _verify_snapshots(snapshots)
    if pending.read_bytes() != sealed_bytes:
        raise ValueError("pending manifest changed after verification")
    # Exclusive publication copies the verified bytes, not a reread of a
    # mutable pending file. Existing files can never be overwritten.
    final = output / "manifest.json"
    with final.open("xb") as stream:
        stream.write(sealed_bytes)
    try:
        _snapshot(final, snapshots, expected)
        _verify_snapshots(snapshots)
    except Exception:
        # This is solely our newly created publication marker. Retain all
        # independent payloads/pending bytes for diagnosis, never a bad seal.
        final.unlink()
        raise
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / DEFAULT_MANIFEST)
    parser.add_argument("--manifest-sha256", default=DEFAULT_MANIFEST_SHA256)
    parser.add_argument("--output-directory", type=Path, default=ROOT / DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = build_shadow_cases(args.manifest, args.manifest_sha256, args.output_directory)
    output = args.output_directory.resolve() / "manifest.json"
    print(
        json.dumps(
            {
                "manifest": str(output),
                "sha256": trainer.sha256(output),
                "case_count": result["case_count"],
                "unique_rgb_count": result["unique_rgb_count"],
            }
        )
    )


if __name__ == "__main__":
    main()
