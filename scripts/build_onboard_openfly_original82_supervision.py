"""Create-only original82 planned supervision; not optimizer/official holdout authority.

The actual source-only CPU build reads/hashes the full original annotation and
decodes original PNGs. Run it only in the separately authorized resource window.
No processor, model, tensor, GPU, network client or legacy trainer is invoked.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.openfly-original82-supervision-bridge-v1"
ROUTES = (
    (139, "env_airsim_26", "train", 23),
    (220, "env_airsim_16", "dev-validation", 17),
    (90, "env_airsim_23", "dev-test", 42),
)
CONTROLS = {
    "extraction": (
        "artifacts/training/openfly-multiscene-embedded-images-20261008-r1/receipt.json",
        "095eea5eed5136a07e89986bd971632392359f1d1d05f6b731b4bba0ec910a04",
    ),
    "plan": (
        "configs/training/openfly-parquet-image-extraction-20261008-r2.json",
        "b047040485da2a1cd5fd73f69e671fd73fc555a309d1113edac29d704c873740",
    ),
    "geometry": (
        "artifacts/diagnostics/openfly-original82-geometry-20261008-r1/report.json",
        "a6543c62f7fee07e7eb8685d745c8cef635faba5bca8d7fedd95ce087db6a72c",
    ),
    "image_evidence": (
        "docs/evidence/openfly-parquet-original-images-20261008-r1.json",
        "a6739c17c2cdf2bc0505e350f861317a4b16f85f3127c61b34f5a7871bdbc29a",
    ),
    "geometry_evidence": (
        "docs/evidence/openfly-original82-geometry-visual-audit-20261008-r1.json",
        "93450b4e24b5b604cdc35cf47c473fa7f8605bd35c0dd23233e32d805c8284d9",
    ),
    "manifest": (
        "artifacts/training/openfly-navigation-source-pilot-20261007-r2/manifest.json",
        "b8a4b318678bf1e2a47a5340bafe47811f61a25266084cdad888c4691db9706c",
    ),
}
FALSE_CLAIMS = (
    "training_ready",
    "optimizer_authorized",
    "optimizer_performed",
    "model_loaded",
    "processor_used",
    "GPU_used",
    "network_io_performed",
    "quality_claim",
    "candidate_promoted",
    "default_model_changed",
    "execution_authority",
    "physical_performance_claimed",
    "hil_real_evidence",
    "outdoor_gnss_evidence",
    "final_release_allowed",
    "official_seen_unseen_holdout_verified",
    "generalization_verified",
    "compatible_with_legacy86_training_admission",
    "old_dataset_merged_or_modified",
    "live_camera_color_orientation_parity_verified",
)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")


def strict_json(raw):
    def pairs(rows):
        value = {}
        for key, item in rows:
            if key in value:
                raise ValueError("Duplicate JSON key")
            value[key] = item
        return value

    def constant(value):
        raise ValueError("Nonfinite JSON constant: " + value)

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    canonical(value)
    return value


def path_for(row, *, runtime=False):
    if not isinstance(row, dict) or not {"path", "bytes", "sha256"} <= row.keys():
        raise ValueError("A source byte binding is required")
    if type(row["bytes"]) is not int or not 0 < row["bytes"] <= 450 * 1024 * 1024:
        raise ValueError("Invalid source byte bound")
    digest = row["sha256"]
    if (
        type(digest) is not str
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise ValueError("Canonical source SHA-256 required")
    if type(row["path"]) is not str or not row["path"]:
        raise ValueError("A nonempty source path is required")
    path = (ROOT / row["path"]).resolve(strict=True)
    if not path.is_file() or (not runtime and not path.is_relative_to(ROOT)):
        raise ValueError("Repository source escaped the admitted root")
    return path


def record(path):
    path = Path(path).resolve(strict=True)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(block)
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def verify(row, *, runtime=False):
    path = path_for(row, runtime=runtime)
    if record(path) != {
        k: str(path) if k == "path" else row[k] for k in ("path", "bytes", "sha256")
    }:
        raise ValueError("Current source bytes differ from the sealed source binding")
    return path


def read_small(row):
    if row["bytes"] > 2 * 1024 * 1024:
        raise ValueError("Control JSON exceeds its byte cap")
    return strict_json(verify(row).read_bytes())


def relative(row):
    return {**row, "path": Path(row["path"]).relative_to(ROOT).as_posix()}


def exact_source(rows, suffix):
    found = [
        r
        for r in rows
        if r["path"].replace("\\", "/").endswith("/" + suffix)
        or r["path"].replace("\\", "/") == suffix
    ]
    unique = {}
    for row in found:
        path = str((ROOT / row["path"]).resolve(strict=False))
        identity = (row["bytes"], row["sha256"])
        if path in unique and unique[path][0] != identity:
            raise ValueError("Conflicting source bindings: " + suffix)
        unique[path] = (identity, row)
    if len(unique) != 1:
        raise ValueError("Exactly one source binding required: " + suffix)
    return next(iter(unique.values()))[1]


def validate_chain(extraction, plan, geometry):
    if (
        extraction.get("contract") != "multidetect.openfly-original-embedded-png-evidence-v1"
        or geometry.get("contract") != "multidetect.openfly-original82-geometry-visual-audit-v1"
        or plan.get("contract") != "multidetect.openfly-embedded-png-extraction-plan-v1"
        or any(
            type(r.get("native_exit_code")) is not int
            or r["native_exit_code"] != 0
            or r.get("sources_unchanged") is not True
            or canonical(r["sources_before"]) != canonical(r["sources_after"])
            for r in (extraction, geometry)
        )
        or extraction.get("all_82_original_images_verified") is not True
        or extraction.get("all_source_shard_row_to_encoded_byte_bindings_verified") is not True
        or type(extraction.get("transcoded_images")) is not int
        or extraction["transcoded_images"] != 0
        or extraction.get("reserved_seen_unseen_read") is not False
        or geometry.get("original82_pose_successor_geometry_verified") is not True
        or geometry.get("future_pose_or_label_sent_to_actor") is not False
        or geometry.get("cross_scene_visual_edges") != []
    ):
        raise ValueError("Successful original TRAIN extraction/geometry chain required")
    if [
        (
            r["annotation_index"],
            r["scene_id"],
            r["development_partition"],
            r["required_frame_count"],
        )
        for r in plan["routes"]
    ] != list(ROUTES):
        raise ValueError("Only the three exact preset original82 routes are allowed")
    groups = {r["scene_id"]: r for r in geometry["visual_groups"]}
    if len(groups) != len(geometry["visual_groups"]) or set(groups) != {
        "env_airsim_18",
        *(r[1] for r in ROUTES),
    }:
        raise ValueError("All four original visual scene groups must remain bound")
    if len(groups["env_airsim_18"]["frames"]) != 86:
        raise ValueError("Historical86 visual anchor coverage differs")
    images = extraction["images"]
    expected = [(index, step) for index, _, _, count in ROUTES for step in range(count)]
    if [(r["annotation_index"], r["frame_index"]) for r in images] != expected:
        raise ValueError("Original82 images must retain exact route/row order and coverage")
    for route in plan["routes"]:
        metadata = route["metadata_rows"]
        if (
            type(route["annotation_index"]) is not int
            or type(route["required_frame_count"]) is not int
            or route["original_annotation_split"] != "train"
            or len(metadata) != route["required_frame_count"]
        ):
            raise ValueError("Complete original TRAIN metadata required")
        shard, publisher = route["shard_file"], route["shard_publisher"]
        if (shard["sha256"], shard["bytes"]) != (
            publisher["digest"],
            publisher["bytes"],
        ) or publisher["digest_kind"] != "sha256-lfs":
            raise ValueError("Original shard differs from publisher LFS identity")
        selected = [r for r in images if r["annotation_index"] == route["annotation_index"]]
        for index, (image, row) in enumerate(zip(selected, metadata, strict=True)):
            png = image["png"]
            if (
                type(image["frame_index"]) is not int
                or type(image["parquet_row_index"]) is not int
                or image["frame_index"] != index
                or image["parquet_row_index"] != index
                or type(row["frame_index"]) is not int
                or row["frame_index"] != index
                or row["env_id"] != route["image_path"].rsplit("/", 1)[0]
                or row["traj_id"] != route["image_path"].rsplit("/", 1)[1]
                or image["image_id"] != row["image_id"]
                or image["scene_id"] != route["scene_id"]
                or image["image_path"] != route["image_path"]
                or image["original_annotation_split"] != "train"
                or image["source_shard"] != shard
                or image["publisher_shard"] != publisher
                or image["file"]["sha256"] != image["embedded_encoded_sha256"]
                or image["file"]["bytes"] != image["embedded_encoded_bytes"]
                or type(image["file"]["bytes"]) is not int
                or not 0 < image["file"]["bytes"] <= 8 * 1024 * 1024
                or image["original_encoded_bytes_preserved"] is not True
                or image["opaque_embedded_path_used_as_source"] is not False
                or image["metadata_identity_matches_stage1"] is not True
                or (png["width"], png["height"], png["mode"], png["encoding"])
                != (853, 480, "RGB", "PNG")
                or png["actual_Pillow_decode_succeeded"] is not True
                or png["transcoded"] is not False
            ):
                raise ValueError("Publisher shard/row/original PNG identity differs")
        if len(groups[route["scene_id"]]["frames"]) != len(selected):
            raise ValueError("Geometry visual frame coverage differs")
        for image, visual in zip(selected, groups[route["scene_id"]]["frames"], strict=True):
            if (ROOT / image["file"]["path"]).resolve(strict=False) != (
                ROOT / visual["path"]
            ).resolve(strict=False):
                raise ValueError("Visual evidence does not bind the original RGB order")
    if sum(r["file"]["bytes"] for r in images) != 56041231:
        raise ValueError("Original encoded82 total differs")


def admit_inputs():
    documents, bindings = {}, {}
    for name, (path, digest) in CONTROLS.items():
        binding = record(ROOT / path)
        if binding["sha256"] != digest:
            raise ValueError("Frozen bridge prerequisite changed: " + name)
        documents[name], bindings[name] = read_small(binding), binding
    e, p, g, m = (documents[k] for k in ("extraction", "plan", "geometry", "manifest"))
    validate_chain(e, p, g)
    if (
        e["plan"]["sha256"] != bindings["plan"]["sha256"]
        or e["original_train_annotation"] != p["original_train_annotation"]
    ):
        raise ValueError("Extraction plan/original full annotation binding differs")
    repo = [
        *g["sources_before"],
        *bindings.values(),
        p["original_train_annotation"],
        *[r["shard_file"] for r in p["routes"]],
        *m["code_sources"],
        *m["license_sources"],
    ]
    dependencies = (
        "scripts/build_onboard_openfly_navigation_dataset.py",
        "scripts/fetch_openfly_navigation_pilot.py",
        "scripts/plan_openfly_multiscene_acquisition.py",
        "src/multidetect/autonomy/onboard_navigation_supervision.py",
        "src/multidetect/autonomy/serialization.py",
    )
    for suffix in dependencies:
        row = exact_source([*e["sources_before"], *g["sources_before"]], suffix)
        if row["bytes"] > 2 * 1024 * 1024:
            raise ValueError("Dependency source exceeded its bound")
        verify(row)
        repo.append(row)
    for suffix, size, digest in (
        (
            "src/multidetect/autonomy/onboard_navigation_actor.py",
            13167,
            "eaefc34cd199295c43cab927d3c51182bfa728fd1753c7ac2e4150a1400a83e1",
        ),
        (
            "scripts/onboard_navigation_closed_sample_encoding.py",
            5165,
            "5f3bc0e7284c5a92bca1a4781898546eda2ec30c58626b1f321a023a179455b9",
        ),
    ):
        row = {"path": suffix, "bytes": size, "sha256": digest}
        verify(row)
        repo.append(row)
    runtime = [
        r
        for r in e["runtime_sources_before"]
        if "/PIL/" in r["path"].replace("\\", "/") or "/yaml/" in r["path"].replace("\\", "/")
    ]
    if len(runtime) != 6 or e["runtime_sources_before"] != e["runtime_sources_after"]:
        raise ValueError("Pinned source-only Pillow/YAML runtime changed")
    for row in runtime:
        verify(row, runtime=True)
    return SimpleNamespace(**documents, controls=bindings, sources=repo, runtime=runtime)


def apis():
    original = sys.path[:]
    try:
        sys.path[:0] = [str(ROOT), str(ROOT / "src")]
        import PIL
        import yaml
        from PIL import Image, ImageFile, PngImagePlugin, _imaging

        from multidetect.autonomy import onboard_navigation_actor as actor
        from multidetect.autonomy import onboard_navigation_supervision as navigation
        from scripts import build_onboard_openfly_navigation_dataset as builder
        from scripts import fetch_openfly_navigation_pilot as fetch
        from scripts import plan_openfly_multiscene_acquisition as planner
        from scripts.onboard_navigation_closed_sample_encoding import (
            ClosedNavigationSourceValidationSession,
        )
    finally:
        sys.path[:] = original
    return SimpleNamespace(
        b=builder,
        f=fetch,
        p=planner,
        n=navigation,
        a=actor,
        Session=ClosedNavigationSourceValidationSession,
        runtime_modules=(PIL, Image, ImageFile, PngImagePlugin, _imaging, yaml),
        yaml=yaml,
    )


def validate_imported_runtime(api, rows):
    expected = {path_for(row, runtime=True) for row in rows}
    actual = {Path(module.__file__).resolve(strict=True) for module in api.runtime_modules}
    if len(rows) != 6 or len(actual) != 6 or actual != expected:
        raise ValueError("Imported Pillow/YAML entrypoints differ from the sealed six bindings")
    snapshot(rows, runtime=True)


def select_annotations(path, fetch):
    selected, visited = {}, 0
    iterator = fetch.iter_annotation_records(path)
    try:
        for index, row in iterator:
            visited += 1
            if index in {r[0] for r in ROUTES}:
                fetch.validate_annotation(row)
                selected[index] = row
            if len(selected) == 3:
                break
    finally:
        iterator.close()
    if set(selected) != {r[0] for r in ROUTES}:
        raise ValueError("An original annotation selection is missing")
    return selected, visited


def fixed_partitions(episodes, planner):
    split = planner.anchored_visual_split(episodes)
    expected = {ep["episode_id"]: ep["declared_partition"] for ep in episodes}
    if split["overlap_routes"] or split["assignment"] != expected:
        raise ValueError("Preset scene roles overlap; no automatic reassignment or promotion")
    return split


def make_sample(route, annotation, action, target, frames, index, sources, navigation):
    return navigation.NavigationSupervisionSample.from_mapping(
        {
            "schema_version": 1,
            "contract": "multidetect.onboard-navigation-supervision-v1",
            "sample_id": frames[index]["frame_id"],
            "episode_id": frames[index]["episode_id"],
            "scene_id": route["scene_id"],
            "step_index": index,
            "source": sources,
            "prompt": {
                "text_original": annotation["gpt_instruction"],
                "utf8_sha256": hashlib.sha256(annotation["gpt_instruction"].encode()).hexdigest(),
                "source_record_sha256": sources["annotation"]["sha256"],
                "source_pointer": {
                    "format": "json",
                    "record_index": None,
                    "json_pointer": f"/{route['annotation_index']}/gpt_instruction",
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
                "source_record_sha256": sources["pose"]["sha256"],
            },
            "planned_action": {
                "raw": action,
                "raw_record_sha256": sources["pose"]["sha256"],
                **target,
                "unit_verification_sha256": None
                if target["kind"] == "stop"
                else sources["unit_verification"]["sha256"],
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
                "evidence_sha256": sources["alignment_verification"]["sha256"],
                "dt_s": None,
            },
            "partition": route["development_partition"],
            "label_provenance": "external_planned_navigation_supervision",
        }
    )


def snapshot(rows, *, runtime=False):
    unique = {}
    for row in rows:
        path = path_for(row, runtime=runtime)
        normalized = {k: str(path) if k == "path" else row[k] for k in ("path", "bytes", "sha256")}
        if str(path) in unique and unique[str(path)] != normalized:
            raise ValueError("Conflicting source bindings")
        unique[str(path)] = normalized
    for row in unique.values():
        verify(row, runtime=runtime)
    return [unique[k] for k in sorted(unique)]


def create_json(path, value, builder):
    builder.create_bytes(path, canonical(value) + b"\n")
    return relative(record(path))


def build_bridge(output_dir, *, entrypoint_sha256):
    entry = record(__file__)
    if entry["sha256"] != entrypoint_sha256:
        raise ValueError("Explicit NEW bridge entrypoint SHA differs")
    output = Path(output_dir).absolute().resolve(strict=False)
    if (
        output.parent != ROOT / "artifacts/training"
        or not output.name.startswith("onboard-openfly-original82-supervision-")
        or output.exists()
        or output.is_symlink()
    ):
        raise ValueError("Use one fresh independent original82 supervision directory")
    ctx = admit_inputs()  # All pinned code/controls admitted before lazy imports/writes.
    api = apis()
    validate_imported_runtime(api, ctx.runtime)
    for package in ("Pillow", "PyYAML"):
        if importlib.metadata.version(package) != ctx.plan["runtime"]["versions"][package]:
            raise ValueError("Pinned source-only CPU distribution differs")
    annotation = ctx.plan["original_train_annotation"]
    if (annotation["bytes"], annotation["sha256"]) != (
        api.f.ANNOTATION_BYTES,
        api.f.ANNOTATION_SHA256,
    ):
        raise ValueError("Original full TRAIN annotation identity differs")
    if (
        ctx.plan["dataset_repository"],
        ctx.plan["dataset_revision"],
        ctx.manifest["code"]["revision"],
    ) != (api.f.DATASET, api.f.REVISION, api.f.CODE_REVISION):
        raise ValueError("Original publisher dataset/code revision differs")
    ctx.sources += [
        entry,
        record(ROOT / "tests/test_build_onboard_openfly_original82_supervision.py"),
    ]
    before, runtime_before = snapshot(ctx.sources), snapshot(ctx.runtime, runtime=True)
    selected, visited = select_annotations(path_for(annotation), api.f)
    prepared, episodes = [], []
    for route in ctx.plan["routes"]:
        index, scene = route["annotation_index"], route["scene_id"]
        original = selected[index]
        if (
            original["image_path"] != route["image_path"]
            or hashlib.sha256(api.p.canonical(original)).hexdigest()
            != route["original_annotation_record_sha256"]
        ):
            raise ValueError("Exact original TRAIN instruction/episode record differs")
        pose = exact_source(
            ctx.geometry["sources_before"], f"Image/{route['image_path']}/pose.jsonl"
        )
        config = exact_source(ctx.geometry["sources_before"], f"configs/{scene}.yaml")
        for row, publisher in (
            (pose, route["pose_publisher"]),
            (config, route["config_publisher"]),
        ):
            raw = verify(row).read_bytes()
            blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
            if (len(raw), blob, publisher["digest_kind"]) != (
                publisher["bytes"],
                publisher["digest"],
                "sha1-git-blob",
            ):
                raise ValueError("Pinned publisher pose/config Git blob differs")
        mapping = api.yaml.safe_load(verify(config).read_text(encoding="utf-8"))["traj_map"]
        if any(
            type(mapping.get(k)) not in (int, float) or mapping[k] != 1
            for k in ("traj_scale_ratio", "pcd_scale_ratio")
        ):
            raise ValueError("Only previously reviewed unit-scale AirSim scenes are allowed")
        actions, _offline_landmark = api.f.parse_pose(verify(pose).read_bytes())
        images = [r for r in ctx.extraction["images"] if r["annotation_index"] == index]
        if len(actions) != len(images):
            raise ValueError("Complete pose/RGB sequence length differs")
        alignment = api.f.align_episode(
            original, actions, [(i, {"path": r["file"]["path"]}) for i, r in enumerate(images)]
        )
        if alignment["annotation_pose_yaw_exact_match"] is not True:
            raise ValueError("Original selected annotation and raw pose differ")
        metrics = [api.b.derive_primitive(actions, i) for i in range(len(actions))]
        observed = next(r for r in ctx.geometry["routes"] if r["annotation_index"] == index)
        if canonical(
            [{"target": target, "check": check} for target, check in metrics]
        ) != canonical(observed["targets_and_geometry_checks"]):
            raise ValueError("Recomputed planned geometry differs from sealed original82 audit")
        for action, row in zip(actions, route["metadata_rows"], strict=True):
            if canonical(
                [action["imageid"], action["pos"], action["yaw"], action["type"], action["value"]]
            ) != canonical(
                [
                    row["frame_index"],
                    row["pos"],
                    row["yaw"],
                    row["action_type"],
                    row["action_value"],
                ]
            ):
                raise ValueError("Original action/pose differs from Parquet row metadata")
        episode_id = hashlib.sha256(
            (api.f.REVISION + ":" + route["image_path"]).encode()
        ).hexdigest()
        visual = next(r for r in ctx.geometry["visual_groups"] if r["scene_id"] == scene)["frames"]
        episodes.append(
            {
                "episode_id": episode_id,
                "scene_id": scene,
                "source_episode": route["image_path"],
                "declared_partition": route["development_partition"],
                "visual_identities": visual,
            }
        )
        prepared.append(
            (route, original, actions, metrics, images, pose, config, alignment, episode_id)
        )
    old_visual = next(r for r in ctx.geometry["visual_groups"] if r["scene_id"] == "env_airsim_18")[
        "frames"
    ]
    episodes.append(
        {
            "episode_id": "historical-env18-visual-anchor-not-merged",
            "scene_id": "env_airsim_18",
            "source_episode": "historical-env18-not-merged",
            "declared_partition": "train",
            "visual_identities": old_visual,
        }
    )
    split = fixed_partitions(episodes, api.p)
    output.mkdir(parents=True, exist_ok=False)
    try:
        converter_path = output / "conversion" / Path(__file__).name
        api.b.create_bytes(converter_path, Path(__file__).read_bytes())
        converter = relative(record(converter_path))
        unit = create_json(
            output / "unit-verification.json",
            {
                "contract": "multidetect.openfly-source-units-review-v1",
                "code_revision": api.f.CODE_REVISION,
                "generation_code": [
                    {k: row[k] for k in ("path", "bytes", "sha256")}
                    for row in ctx.manifest["code_sources"]
                ],
                "selected_scene_configs": [row[6] for row in prepared],
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
                "sealed_geometry_evidence": ctx.controls["geometry_evidence"],
            },
            api.b,
        )
        samples = []
        license_row = exact_source(ctx.manifest["license_sources"], "license/dataset/README.md")
        if "license: apache-2.0" not in verify(license_row).read_text(encoding="utf-8"):
            raise ValueError("Original dataset license card absent")
        for (
            route,
            original,
            actions,
            metrics,
            images,
            pose,
            _config,
            alignment,
            episode_id,
        ) in prepared:
            proof = create_json(
                output / f"alignment/{episode_id}.json",
                {
                    "contract": "multidetect.openfly-raw-step-alignment-v1",
                    "pose": pose,
                    "annotation": annotation,
                    "annotation_index": route["annotation_index"],
                    "position_tolerance_m": api.b.POSITION_TOLERANCE_M,
                    "yaw_tolerance_rad": api.b.YAW_TOLERANCE_RAD,
                    "transitions": [check for _, check in metrics],
                    "annotation_selected_pose_alignment": alignment,
                    "aggregate_annotation_actions_excluded_from_gold": True,
                    "rgb_i_is_current_pose_i": True,
                    "action_i_is_planned_pose_i_to_i_plus_1": True,
                    "no_successor_rgb_in_actor_input": True,
                    "dt_s": None,
                    "original_embedded_png_extraction": ctx.controls["extraction"],
                },
                api.b,
            )
            frames = [
                {
                    "frame_id": hashlib.sha256((episode_id + ":" + str(i)).encode()).hexdigest(),
                    "episode_id": episode_id,
                    "scene_id": route["scene_id"],
                    "step_index": i,
                    **image["file"],
                    "width": 853,
                    "height": 480,
                    "encoding": "png",
                    "source_clock_id": None,
                    "source_stamp": None,
                    "stamp_unit": None,
                }
                for i, image in enumerate(images)
            ]
            sources = {
                "repository_id": api.f.DATASET,
                "revision": api.f.REVISION,
                "original_split": "train",
                "annotation_record_id": str(route["annotation_index"]),
                "annotation": annotation,
                "pose": {k: pose[k] for k in ("path", "bytes", "sha256")},
                "conversion": converter,
                "license": {k: license_row[k] for k in ("path", "bytes", "sha256")},
                "review_receipt": None,
                "unit_verification": unit,
                "alignment_verification": proof,
            }
            samples.extend(
                make_sample(route, original, action, target, frames, i, sources, api.n)
                for i, (action, (target, _)) in enumerate(zip(actions, metrics, strict=True))
            )
        samples = api.n.validate_navigation_episode_samples(tuple(samples))
        actors, body_completed = [], False
        with api.Session(ROOT) as session:
            session.prime(samples)
            for sample in samples:
                actor = api.a.NavigationActorInput.from_supervision_sample(
                    sample, ROOT, session=session
                )
                target = strict_json(
                    api.n.serialize_navigation_supervision_target(sample, ROOT, session=session)
                )
                identity = actor.identity_sha256
                messages = api.a.serialize_navigation_actor_messages(actor)
                try:
                    if messages[1]["content"][0] != {
                        "type": "text",
                        "text": sample.prompt.text_original,
                    }:
                        raise ValueError("Actor original prompt differs")
                    actors.append(
                        {
                            "sample_id": sample.sample_id,
                            "actor_identity_sha256": identity,
                            "target": target,
                            "ordered_frame_steps": [f.step_index for f in sample.frames],
                        }
                    )
                finally:
                    for message in messages:
                        for item in message["content"]:
                            if item["type"] == "image":
                                item["image"].close()
                    del actor, messages
            body_completed = True
        if not body_completed or len(actors) != 82 or session.verified is not True:
            raise ValueError("Original82 source session did not finish its final SHA check")
        after, runtime_after = snapshot(ctx.sources), snapshot(ctx.runtime, runtime=True)
        validate_imported_runtime(api, ctx.runtime)
        if before != after or runtime_before != runtime_after:
            raise ValueError("Bridge sources/runtime changed before sealing")
        payload = b"".join((api.b.canonical_json(s.to_mapping()) + "\n").encode() for s in samples)
        if len(payload) > 2 * 1024 * 1024:
            raise ValueError("Metadata-only sample output exceeds its cap")
        api.b.create_bytes(output / "samples.jsonl", payload)
        report = {
            "schema_version": 1,
            "contract": CONTRACT,
            "native_exit_code": 0,
            "sample_count": len(samples),
            "partition_counts": dict(Counter(s.partition for s in samples)),
            "dataset": relative(record(output / "samples.jsonl")),
            "converter": converter,
            "prerequisites": ctx.controls,
            "source_bindings_before": before,
            "source_bindings_after": after,
            "runtime_bindings_before": runtime_before,
            "runtime_bindings_after": runtime_after,
            "runtime_inventory_scope": (
                "six_sealed_Pillow_YAML_sources_not_all_transitive_modules_or_DLLs"
            ),
            "sources_unchanged": True,
            "source_validation_session_verified": True,
            "actor_input": "verbatim_prompt_and_current_plus_latest_past_original_RGB_only",
            "pose_future_truth_or_label_in_actor_input": False,
            "aggregate_annotation_actions_used_as_gold": False,
            "source_planned_not_executed": True,
            "dt_s": None,
            "partitions": split,
            "annotation_records_streamed_until_selections": visited,
            "sample_actor_target_bindings": actors,
            "memory_scope": (
                "bounded_source_stream_one_actor_at_a_time_no_full_RGB_or_tensor_cache_RSS_not_measured"
            ),
            "raw_primitive_counts": dict(
                Counter(s.to_mapping()["planned_action"]["raw"]["type"] for s in samples)
            ),
            **dict.fromkeys(FALSE_CLAIMS, False),
        }
        create_json(output / "report.json", report, api.b)
        return report
    except BaseException as error:
        create_json(
            output / "failure.json",
            {
                "contract": CONTRACT,
                "native_exit_code": 1,
                "failure_type": type(error).__name__,
                "failure": str(error)[:2048],
                "partial_outputs_preserved": True,
                **dict.fromkeys(FALSE_CLAIMS, False),
            },
            api.b,
        )
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--entrypoint-sha256", required=True)
    args = parser.parse_args(argv)
    report = build_bridge(args.output_dir, entrypoint_sha256=args.entrypoint_sha256)
    print(
        json.dumps(
            {
                "event": "original82_supervision_bridge_finished",
                "native_exit_code": 0,
                "dataset": report["dataset"],
                "partition_counts": report["partition_counts"],
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
