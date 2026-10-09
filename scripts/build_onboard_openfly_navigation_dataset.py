#!/usr/bin/env python3
"""Build audited planned-navigation samples from original OpenFly raw steps.

No aggregate annotation action is a training label. The original instruction
and current/most-recent-past RGB are actor inputs; poses and future steps are
offline checks only. These are source plans, not PX4 execution receipts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.onboard_navigation_supervision import (  # noqa: E402
    NavigationSourceValidationSession,
    NavigationSupervisionSample,
    serialize_navigation_supervision_messages,
    serialize_navigation_supervision_target,
    validate_navigation_episode_samples,
)
from multidetect.autonomy.serialization import (  # noqa: E402
    canonical_json,
    strict_json_loads,
)
from scripts.fetch_openfly_navigation_pilot import (  # noqa: E402
    ANNOTATION_BYTES,
    ANNOTATION_SHA256,
    CODE_PATHS,
    CODE_REVISION,
    DATASET,
    MAX_ANNOTATION_BYTES,
    REVISION,
    align_episode,
    iter_annotation_records,
    parse_pose,
)

# Fixed before reading model outputs; failures are not repaired/rescaled.
POSITION_TOLERANCE_M = 1e-4
YAW_TOLERANCE_RAD = 1e-6
MAX_RECORD_BYTES = 2 * 1024 * 1024


class NavigationDatasetError(ValueError):
    """Source, metric or partition admission failed."""


def binding(path: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": str(path.resolve()), "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def resolve_binding(value: dict, root: Path, *, cap=MAX_ANNOTATION_BYTES) -> Path:
    if not isinstance(value, dict) or not {"path", "bytes", "sha256"} <= value.keys():
        raise NavigationDatasetError("missing original file binding")
    path = Path(value["path"])
    resolved = (path if path.is_absolute() else root / path).resolve(strict=True)
    if not resolved.is_file() or not resolved.is_relative_to(root.resolve(strict=True)):
        raise NavigationDatasetError("source escapes artifact root")
    if type(value["bytes"]) is not int or not 0 < value["bytes"] <= cap:
        raise NavigationDatasetError("source exceeds byte budget")
    actual = binding(resolved)
    if (actual["bytes"], actual["sha256"]) != (value["bytes"], value["sha256"]):
        raise NavigationDatasetError("source actual size/SHA differs")
    return resolved


def relative_binding(path: Path, root: Path) -> dict:
    result = binding(path)
    result["path"] = path.resolve(strict=True).relative_to(root.resolve(strict=True)).as_posix()
    return result


def create_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def create_json(path: Path, document: dict) -> None:
    create_bytes(path, (canonical_json(document) + "\n").encode("utf-8"))


def verify_published_sources(manifest: dict, root: Path) -> None:
    """Recompute publisher digests and bind each download to retained trees.

    Full train SHA is separately fixed in this program. Metadata integrity is
    recorded by the frozen source-manifest SHA, not an unverified filename.
    """
    hf_entries, code_entries = {}, {}
    for bound in manifest["metadata_records"]:
        path = resolve_binding(bound, root, cap=MAX_RECORD_BYTES)
        document = strict_json_loads(path.read_bytes())
        if isinstance(document, list):
            for entry in document:
                if isinstance(entry, dict) and entry.get("type") == "file":
                    key = entry["path"]
                    if key in hf_entries and hf_entries[key] != entry:
                        raise NavigationDatasetError("conflicting published dataset metadata")
                    hf_entries[key] = entry
        elif isinstance(document, dict) and "tree" in document:
            if document.get("sha") != CODE_REVISION or document.get("truncated") is not False:
                raise NavigationDatasetError("retained code tree is not the pinned complete tree")
            for entry in document["tree"]:
                if entry.get("type") == "blob":
                    code_entries[entry["path"]] = entry
    downloaded = [manifest["annotation"], *manifest["code_sources"], *manifest["license_sources"]]
    for episode in manifest["episodes"]:
        downloaded.extend([episode["pose"], *episode["images"]])
    hf_prefix = f"https://huggingface.co/datasets/{DATASET}/resolve/{REVISION}/"
    code_prefix = (
        f"https://raw.githubusercontent.com/SHAILAB-IPEC/OpenFly-Platform/{CODE_REVISION}/"
    )
    for record in downloaded:
        path = resolve_binding(record, root)
        url = record.get("source_url", "")
        if url.startswith(hf_prefix):
            repository_path = url[len(hf_prefix) :]
            metadata = hf_entries.get(repository_path)
            if record.get("repository_path") != repository_path or metadata is None:
                raise NavigationDatasetError(
                    "dataset download does not match retained original tree path"
                )
            if metadata.get("lfs") is not None:
                expected_kind, expected = "sha256-lfs", metadata["lfs"]["oid"]
                if metadata["lfs"]["size"] != record["bytes"]:
                    raise NavigationDatasetError("published LFS size differs")
            else:
                expected_kind, expected = "sha1-git-blob", metadata["oid"]
        elif url.startswith(code_prefix):
            repository_path = url[len(code_prefix) :]
            metadata = code_entries.get(repository_path)
            if metadata is None:
                raise NavigationDatasetError("code download does not match pinned tree path")
            expected_kind, expected = "sha1-git-blob", metadata["sha"]
        else:
            raise NavigationDatasetError("download does not use pinned official source URL")
        if metadata["size"] != record["bytes"] or (
            record.get("official_digest_kind"),
            record.get("official_digest"),
        ) != (expected_kind, expected):
            raise NavigationDatasetError(
                "download publisher digest/size differs from retained tree"
            )
        if expected_kind == "sha256-lfs":
            actual = binding(path)["sha256"]
        else:
            digest = hashlib.sha1(b"blob " + str(record["bytes"]).encode() + b"\0")
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            actual = digest.hexdigest()
        if actual != expected:
            raise NavigationDatasetError("actual bytes differ from published digest")


def wrapped(value: float) -> float:
    return math.atan2(math.sin(value), math.cos(value))


def derive_primitive(actions: list[dict], index: int) -> tuple[dict, dict]:
    """Validate raw action i against raw pose i+1; emit body FRD units.

    Source world XY is not geographic North/East. Source Z is up and yaw is
    counter-clockwise. No clock/dt is inferred, and STOP cannot mean LAND.
    """
    current = actions[index]
    if current["imageid"] != index:
        raise NavigationDatasetError("raw imageid differs from original record order")
    kind, value = current["type"], current["value"]
    if type(value) not in (int, float) or not math.isfinite(value):
        raise NavigationDatasetError("nonfinite raw primitive value")
    if kind == "stop":
        if index != len(actions) - 1 or value != 0:
            raise NavigationDatasetError("only original terminal zero STOP is admitted")
        return {"kind": "stop", "translation_m": None, "yaw_delta_rad": None}, {
            "imageid": index,
            "next_imageid": None,
            "terminal_source_stop": True,
            "landing_or_task_completion_inferred": False,
        }
    if index + 1 >= len(actions) or not 0 < value <= 500:
        raise NavigationDatasetError(
            "nonterminal primitive requires bounded positive value and next pose"
        )
    next_pose = actions[index + 1]
    if next_pose["imageid"] != index + 1:
        raise NavigationDatasetError("raw successor must be exactly next original step")
    if any(
        type(item) not in (int, float) or not math.isfinite(item)
        for pose in (current, next_pose)
        for item in (*pose["pos"], pose["yaw"])
    ):
        raise NavigationDatasetError("nonfinite source pose or yaw")
    delta = [b - a for a, b in zip(current["pos"], next_pose["pos"], strict=True)]
    if len(delta) != 3 or not all(math.isfinite(x) for x in delta):
        raise NavigationDatasetError("invalid original pose delta")
    yaw = current["yaw"]
    body = [
        math.cos(yaw) * delta[0] + math.sin(yaw) * delta[1],
        math.sin(yaw) * delta[0] - math.cos(yaw) * delta[1],
        -delta[2],
    ]
    source_yaw_delta = wrapped(next_pose["yaw"] - yaw)
    expected_body = [0.0, 0.0, 0.0]
    expected_source_yaw = 0.0
    target = {"kind": "translate", "translation_m": None, "yaw_delta_rad": None}
    if kind == "go straight":
        expected_body[0] = float(value)
    elif kind == "go up":
        expected_body[2] = -float(value)
    elif kind == "go down":
        expected_body[2] = float(value)
    elif kind in {"turn left", "turn right"}:
        if value > 180:
            raise NavigationDatasetError("raw turn is outside unambiguous angular range")
        expected_source_yaw = math.radians(value) * (1 if kind == "turn left" else -1)
        target = {
            "kind": "turn_yaw",
            "translation_m": None,
            "yaw_delta_rad": -expected_source_yaw,
        }
    else:
        raise NavigationDatasetError("unknown raw primitive cannot be relabelled")
    position_error = [abs(a - b) for a, b in zip(body, expected_body, strict=True)]
    yaw_error = abs(wrapped(source_yaw_delta - expected_source_yaw))
    if max(position_error) > POSITION_TOLERANCE_M or yaw_error > YAW_TOLERANCE_RAD:
        raise NavigationDatasetError(
            f"raw primitive/next-pose disagreement at original step {index}"
        )
    if target["kind"] == "translate":
        # The original metric primitive is retained after checking, not fitted
        # to a conflicting pose delta or to an aggregated annotation code.
        target["translation_m"] = expected_body
    return target, {
        "imageid": index,
        "next_imageid": index + 1,
        "raw_type": kind,
        "raw_value": value,
        "observed_world_delta": delta,
        "observed_body_frd_delta_m": body,
        "expected_body_frd_delta_m": expected_body,
        "observed_source_yaw_delta_rad": source_yaw_delta,
        "expected_source_yaw_delta_rad": expected_source_yaw,
        "position_abs_error_m": position_error,
        "yaw_abs_error_rad": yaw_error,
    }


def rgb_identity(path: Path) -> dict:
    from PIL import Image

    with Image.open(path) as image:
        if image.format != "PNG" or image.mode != "RGB" or image.width * image.height > 4_194_304:
            raise NavigationDatasetError("original image is not bounded single RGB PNG")
        if getattr(image, "n_frames", 1) != 1:
            raise NavigationDatasetError("animated images are not source steps")
        image.load()
        rgb_hash = hashlib.sha256(
            f"RGB:{image.width}:{image.height}:".encode("ascii") + image.tobytes()
        ).hexdigest()
        gray = list(image.convert("L").resize((9, 8)).getdata())
        dhash = sum(
            int(gray[row * 9 + col] > gray[row * 9 + col + 1]) << (row * 8 + col)
            for row in range(8)
            for col in range(8)
        )
        mean_rgb = list(image.resize((1, 1)).getpixel((0, 0)))
        return {
            "rgb_sha256": rgb_hash,
            "dhash64": f"{dhash:016x}",
            "mean_rgb": mean_rgb,
            "width": image.width,
            "height": image.height,
        }


def assign_partitions(episodes: list[dict]) -> tuple[dict[str, str], dict]:
    """Whole scene/episode/visual-duplicate groups, never random frame splits."""
    parents = {episode["episode_id"]: episode["episode_id"] for episode in episodes}

    def find(key):
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    def union(a, b):
        left, right = find(a), find(b)
        if left != right:
            parents[max(left, right)] = min(left, right)

    same_scene_edges, duplicate_edges = [], []
    for i, left in enumerate(episodes):
        for right in episodes[i + 1 :]:
            if (
                left["scene_id"] == right["scene_id"]
                or left["source_episode"] == right["source_episode"]
            ):
                union(left["episode_id"], right["episode_id"])
                same_scene_edges.append([left["episode_id"], right["episode_id"]])
            for lf in left["visual_identities"]:
                for rf in right["visual_identities"]:
                    exact = lf["rgb_sha256"] == rf["rgb_sha256"]
                    near = (
                        int(lf["dhash64"], 16) ^ int(rf["dhash64"], 16)
                    ).bit_count() <= 3 and max(
                        abs(a - b) for a, b in zip(lf["mean_rgb"], rf["mean_rgb"], strict=True)
                    ) <= 10
                    if exact or near:
                        union(left["episode_id"], right["episode_id"])
                        duplicate_edges.append(
                            {
                                "episodes": [left["episode_id"], right["episode_id"]],
                                "exact_rgb": exact,
                            }
                        )
                        break
                else:
                    continue
                break
    groups = {}
    for episode in episodes:
        groups.setdefault(find(episode["episode_id"]), []).append(episode["episode_id"])
    ordered = sorted(groups)
    assignment = {key: "train" for key in parents}
    if len(ordered) >= 3:
        for group, partition in zip(ordered[-2:], ("dev-validation", "dev-test"), strict=True):
            for key in groups[group]:
                assignment[key] = partition
    return assignment, {
        "method": "whole-source-scene-episode-and-visual-connected-components",
        "groups": [sorted(groups[key]) for key in ordered],
        "group_count": len(groups),
        "scene_edges": same_scene_edges,
        "visual_duplicate_edges": duplicate_edges,
        "near_duplicate_rule": {"dhash64_hamming_max": 3, "mean_rgb_component_difference_max": 10},
        "independent_development_holdout_available": len(groups) >= 3,
        "insufficient_groups_behavior": "all-train-no-generalization-claim",
        "assignment": assignment,
    }


def _frame(image: dict, episode_id: str, scene: str, root: Path) -> dict:
    path = resolve_binding(image, root, cap=8 * 1024 * 1024)
    rgb = image["rgb"]
    return {
        "frame_id": hashlib.sha256((episode_id + ":" + str(image["imageid"])).encode()).hexdigest(),
        "episode_id": episode_id,
        "scene_id": scene,
        "step_index": image["imageid"],
        **relative_binding(path, root),
        "width": rgb["width"],
        "height": rgb["height"],
        "encoding": rgb["encoding"],
        "source_clock_id": None,
        "source_stamp": None,
        "stamp_unit": None,
    }


def build_dataset(
    source_manifest: Path, output_dir: Path, *, artifact_root=ROOT, source_sha256=None
) -> dict:
    implementation_paths = [
        Path(__file__).resolve(),
        ROOT / "src/multidetect/autonomy/onboard_navigation_supervision.py",
        ROOT / "src/multidetect/autonomy/serialization.py",
        ROOT / "scripts/fetch_openfly_navigation_pilot.py",
        ROOT / "tests/test_build_onboard_openfly_navigation_dataset.py",
        ROOT / "tests/test_onboard_navigation_supervision.py",
        ROOT / "tests/test_fetch_openfly_navigation_pilot.py",
    ]
    implementation_before = [binding(path) for path in implementation_paths]
    root = Path(artifact_root).resolve(strict=True)
    manifest_path = source_manifest.resolve(strict=True)
    manifest_binding = relative_binding(manifest_path, root)
    if source_sha256 is not None and manifest_binding["sha256"] != source_sha256:
        raise NavigationDatasetError("source manifest differs from frozen expected SHA")
    if manifest_binding["bytes"] > MAX_RECORD_BYTES:
        raise NavigationDatasetError("pilot manifest too large")
    manifest = strict_json_loads(manifest_path.read_bytes())
    if (
        manifest.get("contract") != "multidetect.openfly-navigation-pilot-v1"
        or manifest["dataset"]["repository"] != DATASET
        or manifest["dataset"]["revision"] != REVISION
        or manifest["dataset"]["split"] != "train"
        or manifest["code"]["revision"] != CODE_REVISION
        or manifest["source_planned_not_executed"] is not True
        or manifest["seen_unseen_downloaded"] is not False
        or manifest["weights_downloaded"] is not False
    ):
        raise NavigationDatasetError("only pinned original OpenFly train pilot is admitted")
    out = output_dir.absolute()
    if out.exists() or not out.resolve().is_relative_to(root):
        raise NavigationDatasetError("output must be a new directory inside artifact root")
    if (manifest["annotation"]["bytes"], manifest["annotation"]["sha256"]) != (
        ANNOTATION_BYTES,
        ANNOTATION_SHA256,
    ):
        raise NavigationDatasetError("original full train annotation identity differs")
    all_sources = [
        manifest["annotation"],
        *manifest["code_sources"],
        *manifest["license_sources"],
        *manifest["metadata_records"],
        *manifest["source_files"],
    ]
    for episode in manifest["episodes"]:
        all_sources.extend([episode["annotation_record"], episode["pose"], *episode["images"]])
    before = []
    for bound in all_sources:
        path = resolve_binding(bound, root)
        before.append(relative_binding(path, root))
    verify_published_sources(manifest, root)
    annotation_path = resolve_binding(manifest["annotation"], root)
    selected_indices = {episode["annotation_index"] for episode in manifest["episodes"]}
    if len(selected_indices) != len(manifest["episodes"]) or not 1 <= len(selected_indices) <= 3:
        raise NavigationDatasetError("pilot must contain 1..3 distinct original records")
    originals, original_count = {}, 0
    for index, record in iter_annotation_records(annotation_path):
        original_count += 1
        if index in selected_indices:
            originals[index] = record
    if (
        set(originals) != selected_indices
        or original_count != manifest["selection"]["annotation_record_count"]
    ):
        raise NavigationDatasetError("selected original record membership differs")
    if sum(len(ep["images"]) for ep in manifest["episodes"]) > 256:
        raise NavigationDatasetError("pilot image count exceeds frozen cap")
    if (
        sum(
            ep["pose"]["bytes"] + sum(im["bytes"] for im in ep["images"])
            for ep in manifest["episodes"]
        )
        > 128 * 1024 * 1024
    ):
        raise NavigationDatasetError("pilot RGB/pose size exceeds frozen cap")
    code_paths = [resolve_binding(item, root) for item in manifest["code_sources"]]
    for required in CODE_PATHS:
        if len([path for path in code_paths if path.as_posix().endswith("/" + required)]) != 1:
            raise NavigationDatasetError("generation/conversion source missing")
    license_paths = [resolve_binding(item, root) for item in manifest["license_sources"]]
    cards = [p for p in license_paths if p.name == "README.md"]
    if len(cards) != 1 or "license: apache-2.0" not in cards[0].read_text(encoding="utf-8"):
        raise NavigationDatasetError("original dataset license card absent")
    prepared, rejected = [], []
    for ep in manifest["episodes"]:
        record = originals[ep["annotation_index"]]
        extracted = strict_json_loads(resolve_binding(ep["annotation_record"], root).read_bytes())
        if (
            canonical_json(record) != canonical_json(extracted)
            or record["image_path"] != ep["image_path"]
        ):
            raise NavigationDatasetError(
                "extracted original instruction/episode differs from full source"
            )
        actions, _ = parse_pose(resolve_binding(ep["pose"], root).read_bytes())
        scene = record["image_path"].split("/")[0]
        # This revision's AirSim transform is reviewed only with unit scale.
        # Other source environments are quarantined rather than silently scaled.
        configs = [p for p in code_paths if p.name == scene + ".yaml"]
        if len(configs) != 1:
            raise NavigationDatasetError("selected scene's generation configuration missing")
        import yaml

        scene_config = yaml.safe_load(configs[0].read_text(encoding="utf-8"))
        map_config = scene_config.get("traj_map") if isinstance(scene_config, dict) else None
        if (
            not scene.startswith("env_airsim_")
            or not isinstance(map_config, dict)
            or any(
                type(map_config.get(name)) not in (int, float) or map_config[name] != 1
                for name in ("traj_scale_ratio", "pcd_scale_ratio")
            )
        ):
            rejected.append(
                {
                    "annotation_index": ep["annotation_index"],
                    "reason": "unreviewed_source_scene_scale",
                }
            )
            continue
        if len(actions) != len(ep["images"]) or [im["imageid"] for im in ep["images"]] != list(
            range(len(actions))
        ):
            raise NavigationDatasetError(
                "all raw RGB and pose steps must exist in exact source order"
            )
        image_entries = [(im["imageid"], {"path": im["repository_path"]}) for im in ep["images"]]
        source_alignment = align_episode(record, actions, image_entries)
        if source_alignment["annotation_pose_yaw_exact_match"] is not True:
            rejected.append(
                {
                    "annotation_index": ep["annotation_index"],
                    "reason": "annotation_raw_pose_mismatch",
                }
            )
            continue
        try:
            metrics = [derive_primitive(actions, index) for index in range(len(actions))]
        except NavigationDatasetError as exc:
            rejected.append({"annotation_index": ep["annotation_index"], "reason": str(exc)})
            continue
        episode_id = hashlib.sha256((REVISION + ":" + ep["image_path"]).encode()).hexdigest()
        visuals = [rgb_identity(resolve_binding(im, root)) for im in ep["images"]]
        prepared.append(
            {
                "episode_id": episode_id,
                "scene_id": scene,
                "source_episode": ep["image_path"],
                "entry": ep,
                "record": record,
                "actions": actions,
                "metrics": metrics,
                "visual_identities": visuals,
                "source_alignment": source_alignment,
                "config": relative_binding(configs[0], root),
            }
        )
    if not prepared:
        raise NavigationDatasetError("no complete raw episode passed metric admission")
    assignment, partition_report = assign_partitions(prepared)
    out.mkdir(parents=True, exist_ok=False)
    implementation_snapshots = []
    for path in implementation_paths:
        destination = out / "implementation-snapshot" / path.relative_to(ROOT)
        create_bytes(destination, path.read_bytes())
        implementation_snapshots.append(relative_binding(destination, root))
    # Retain exact new converter bytes inside the artifact, not a rewritten copy.
    converter_path = out / "conversion/build_onboard_openfly_navigation_dataset.py"
    create_bytes(converter_path, Path(__file__).read_bytes())
    converter = relative_binding(converter_path, root)
    unit_path = out / "unit-verification.json"
    create_json(
        unit_path,
        {
            "contract": "multidetect.openfly-source-units-review-v1",
            "code_revision": CODE_REVISION,
            "generation_code": [relative_binding(p, root) for p in code_paths],
            "selected_scene_configs": [ep["config"] for ep in prepared],
            "source_coordinate_frame": "openfly-world-xy-z-up-not-geographic",
            "position_unit": "m",
            "source_yaw_unit": "rad",
            "raw_turn_value_unit": "deg",
            "target_translation_frame": "body_frd",
            "target_yaw_convention": "clockwise_right_positive",
            "body_transform": ["cos(yaw)*dx+sin(yaw)*dy", "sin(yaw)*dx-cos(yaw)*dy", "-dz"],
            "yaw_transform": "-atan2(sin(next_yaw-yaw),cos(next_yaw-yaw))",
            "scale_scope": "only_selected_airsim_scenes_with_traj_and_pcd_scale_one",
            "pose_origin": "planned_pose",
            "dt_s": None,
            "physical_calibration_or_execution_evidence": False,
        },
    )
    unit = relative_binding(unit_path, root)
    samples, episode_reports = [], []
    annotation_source_binding = relative_binding(annotation_path, root)
    for ep in prepared:
        entry, actions, record = ep["entry"], ep["actions"], ep["record"]
        alignment_path = out / f"alignment/{ep['episode_id']}.json"
        create_json(
            alignment_path,
            {
                "contract": "multidetect.openfly-raw-step-alignment-v1",
                "pose": relative_binding(resolve_binding(entry["pose"], root), root),
                "annotation": manifest_binding,
                "annotation_index": entry["annotation_index"],
                "position_tolerance_m": POSITION_TOLERANCE_M,
                "yaw_tolerance_rad": YAW_TOLERANCE_RAD,
                "transitions": [audit for _, audit in ep["metrics"]],
                "annotation_selected_pose_alignment": ep["source_alignment"],
                "aggregate_annotation_actions_excluded_from_gold": True,
                "rgb_i_is_current_pose_i": True,
                "action_i_is_planned_pose_i_to_i_plus_1": True,
                "no_successor_rgb_in_actor_input": True,
                "dt_s": None,
            },
        )
        alignment = relative_binding(alignment_path, root)
        frames = [_frame(im, ep["episode_id"], ep["scene_id"], root) for im in entry["images"]]
        pose_source_binding = relative_binding(resolve_binding(entry["pose"], root), root)
        for index, (action, (target, _)) in enumerate(zip(actions, ep["metrics"], strict=True)):
            sample = NavigationSupervisionSample.from_mapping(
                {
                    "schema_version": 1,
                    "contract": "multidetect.onboard-navigation-supervision-v1",
                    "sample_id": hashlib.sha256(
                        (ep["episode_id"] + ":" + str(index)).encode()
                    ).hexdigest(),
                    "episode_id": ep["episode_id"],
                    "scene_id": ep["scene_id"],
                    "step_index": index,
                    "source": {
                        "repository_id": DATASET,
                        "revision": REVISION,
                        "original_split": "train",
                        "annotation_record_id": str(entry["annotation_index"]),
                        "annotation": annotation_source_binding,
                        "pose": pose_source_binding,
                        "conversion": converter,
                        "license": relative_binding(cards[0], root),
                        "review_receipt": None,
                        "unit_verification": unit,
                        "alignment_verification": alignment,
                    },
                    "prompt": {
                        "text_original": record["gpt_instruction"],
                        "utf8_sha256": hashlib.sha256(
                            record["gpt_instruction"].encode()
                        ).hexdigest(),
                        "source_record_sha256": manifest["annotation"]["sha256"],
                        "source_pointer": {
                            "format": "json",
                            "record_index": None,
                            "json_pointer": f"/{entry['annotation_index']}/gpt_instruction",
                        },
                    },
                    "frames": frames[max(0, index - 1) : index + 1],
                    "pose": {
                        "position_xyz": action["pos"],
                        "yaw": action["yaw"],
                        "coordinate_frame": "openfly-world-xy-z-up-not-geographic",
                        "length_unit": "m",
                        "yaw_unit": "rad",
                        "origin": "planned_pose",
                        "source_record_sha256": entry["pose"]["sha256"],
                    },
                    "planned_action": {
                        "raw": action,
                        "raw_record_sha256": entry["pose"]["sha256"],
                        **target,
                        "unit_verification_sha256": None
                        if target["kind"] == "stop"
                        else unit["sha256"],
                        "raw_source_pointer": {
                            "format": "json_sequence",
                            "record_index": index,
                            "json_pointer": "/action",
                        },
                    },
                    "alignment": {
                        "observation_step": index,
                        "action_step": index,
                        "next_step": None if target["kind"] == "stop" else index + 1,
                        "evidence_sha256": alignment["sha256"],
                        "dt_s": None,
                    },
                    "partition": assignment[ep["episode_id"]],
                    "label_provenance": "external_planned_navigation_supervision",
                }
            )
            samples.append(sample)
        episode_reports.append(
            {
                "episode_id": ep["episode_id"],
                "scene_id": ep["scene_id"],
                "annotation_index": entry["annotation_index"],
                "steps": len(actions),
                "partition": assignment[ep["episode_id"]],
                "raw_primitive_counts": dict(Counter(a["type"] for a in actions)),
                "visual_identities": ep["visual_identities"],
            }
        )
    samples = validate_navigation_episode_samples(tuple(samples))
    # Exercise exactly the source-only actor serializer/independent gold path.
    # Successful context exit rehashes shared sources after all image decoding.
    with NavigationSourceValidationSession(root) as session:
        session.prime(samples)
        for sample in samples:
            messages = serialize_navigation_supervision_messages(sample, root, session=session)
            serialize_navigation_supervision_target(sample, root, session=session)
            if messages[1]["content"][0]["text"] != sample.prompt.text_original:
                raise NavigationDatasetError("actor original prompt was changed")
    if not session.verified:
        raise NavigationDatasetError("source validation session did not seal")
    after = [relative_binding(resolve_binding(value, root), root) for value in before]
    if before != after or relative_binding(manifest_path, root) != manifest_binding:
        raise NavigationDatasetError("source changed before dataset sealing")
    if binding(converter_path)["sha256"] != binding(Path(__file__))["sha256"]:
        raise NavigationDatasetError("converter source changed during build")
    if implementation_before != [binding(path) for path in implementation_paths]:
        raise NavigationDatasetError("contract/converter/dependency source changed during build")
    dataset_path = out / "samples.jsonl"
    create_bytes(
        dataset_path, b"".join((canonical_json(s.to_mapping()) + "\n").encode() for s in samples)
    )
    report = {
        "contract": "multidetect.openfly-navigation-dataset-report-v1",
        "source_manifest": manifest_binding,
        "dataset": relative_binding(dataset_path, root),
        "converter": converter,
        "implementation_working_source_bindings": implementation_before,
        "implementation_source_snapshots": implementation_snapshots,
        "implementation_sources_unchanged": True,
        "source_bindings": before,
        "sources_unchanged": True,
        "unit_verification": unit,
        "episodes": episode_reports,
        "excluded_episodes": rejected,
        "admitted_episode_count": len(prepared),
        "admitted_sample_count": len(samples),
        "primitive_counts": dict(Counter(s.planned_action.kind for s in samples)),
        "partition_counts": dict(Counter(s.partition for s in samples)),
        "partitions": partition_report,
        "position_tolerance_m": POSITION_TOLERANCE_M,
        "yaw_tolerance_rad": YAW_TOLERANCE_RAD,
        "aggregate_annotation_actions_used_as_gold": False,
        "actor_input": "original_prompt_and_actual_current_plus_at_most_one_past_rgb",
        "pose_future_truth_or_label_in_actor_input": False,
        "dt_available": False,
        "source_planned_not_executed": True,
        "training_ready": False,
        "external_image_color_orientation_live_camera_parity_verified": False,
        "optimizer_performed": False,
        "candidate_promoted": False,
        "physical_performance_claimed": False,
        "hil_real_evidence": False,
        "outdoor_gnss_evidence": False,
        "final_release_allowed": False,
    }
    create_json(out / "report.json", report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--source-manifest-sha256")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = build_dataset(
            args.source_manifest, args.output_dir, source_sha256=args.source_manifest_sha256
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "navigation_dataset_rejected",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "training_ready": False,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 1
    print(
        json.dumps(
            {
                "event": "navigation_dataset_sealed",
                "report": binding(args.output_dir / "report.json"),
                "samples": report["admitted_sample_count"],
                "partition_counts": report["partition_counts"],
                "training_ready": False,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
