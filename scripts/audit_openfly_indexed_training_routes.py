"""Create-only CPU source audit for six acquired original TRAIN episodes.

Only the authenticated 786432-byte annotation prefix is read. A derived JSON
selection preserves original records/index keys; it is not the official full
annotation. No model, processor, training authority or independent holdout is
provided. PNGs retain original encoded bytes and measured dimensions. The
largest input allocation is a single bounded Parquet row group, not a whole
dataset tensor/image cache; process RSS and decoder DLL residency are not
measured by this program.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = "artifacts/training/openfly-indexed-training-supervision-20261008-r1"
CONTRACT = "multidetect.openfly-indexed-training-supervision-audit-v1"
PLAN_CONTRACT = "multidetect.openfly-indexed-training-supervision-plan-v1"
ROUTES = ((143, 15), (150, 29), (153, 17), (154, 25), (148, 15), (144, 21))
PREFIX_BYTES = 786432
PREFIX_SHA = "221068e7506630ac33cedca7c922e069b35ae77372dc73f9054fee3d94f2e653"
REPORT = "artifacts/training/openfly-indexed-route-payloads-20261008-r1/report.json"
ACQUISITION_PLAN = "configs/training/openfly-indexed-route-payloads-20261008-r1.json"
INDEX = "artifacts/diagnostics/openfly-bounded-annotation-index-20261008-r1/report.json"
CONFIG = (
    "artifacts/training/openfly-multiscene-parquet-source-20261008-r1/"
    "payload/configs/env_airsim_26.yaml"
)
PINS = {
    REPORT: (20498, "e822b412d22743cc655dedb601861ca9261ea34f5f17efb3b1f6cce0006c9d31"),
    ACQUISITION_PLAN: (12407, "91f8439716baf3845019841b3916f787f7e1d17934d47e7b2d672fa030840853"),
    INDEX: (189868, "74af3937f614ee78400a3c42cd4087c01a79d8a8d4fd712c57319e3bade25f1c"),
    CONFIG: (773, "82ed8317535f84b83f95fdaae8c29c18b07aa860d56a210a760d6cae93a2ad73"),
    "scripts/extract_openfly_parquet_images.py": (
        26806,
        "fb071ac675892d97dabf0dd7781ca7397daf04c3a5306c90769b550906775b36",
    ),
    "scripts/audit_openfly_multiscene_navigation_geometry.py": (
        12526,
        "f6d4a72140cd57d0862f2e0471f2cbb058f6cd9c309e10dc8ae856987f0d23b6",
    ),
    "scripts/build_onboard_openfly_original82_supervision.py": (
        34827,
        "655113d9a27bd893f5de3617fbc7ce47433e705895c1c96591eb7ab64d6c7d59",
    ),
    "scripts/onboard_navigation_closed_sample_encoding.py": (
        5165,
        "5f3bc0e7284c5a92bca1a4781898546eda2ec30c58626b1f321a023a179455b9",
    ),
    "src/multidetect/autonomy/onboard_navigation_supervision.py": (
        52186,
        "bd1adc75a6954c2eb2453ae358536cae2db65305dd0c3afcf57d24a1711bb14e",
    ),
    "src/multidetect/autonomy/onboard_navigation_actor.py": (
        13167,
        "eaefc34cd199295c43cab927d3c51182bfa728fd1753c7ac2e4150a1400a83e1",
    ),
    "src/multidetect/autonomy/onboard_navigation_inference.py": (
        18843,
        "3838a9ee4ee1eef58a680369076157263cfe824bc75c6dcbfe265a977564954f",
    ),
}
FALSE_CLAIMS = (
    "training_ready",
    "optimizer_authorized",
    "optimizer_performed",
    "model_loaded",
    "GPU_used",
    "processor_used",
    "network_used",
    "quality_claim",
    "candidate_promoted",
    "default_changed",
    "independent_holdout_verified",
    "execution_authority",
    "physical_calibration_verified",
    "HIL_verified",
    "hardware_verified",
    "final_release_allowed",
    "old_dataset_merged_or_modified",
    "compatible_with_legacy86_training_admission",
    "whole_annotation_sha256_verified",
    "full_annotation_array_syntax_verified",
    "live_camera_parity_verified",
)
RUNTIME_MODULES = (
    "PIL",
    "PIL.Image",
    "PIL.ImageFile",
    "PIL.PngImagePlugin",
    "PIL._imaging",
    "yaml",
    "pyarrow",
    "pyarrow._parquet",
    "pyarrow.parquet",
    "pyarrow.parquet.core",
)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def constant(_value):
        raise ValueError("Nonfinite JSON")

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    canonical(value)
    return value


def local(path, *, exists=True):
    value = ROOT / path
    if value.is_symlink():
        raise ValueError("Symlink sources/outputs are forbidden")
    value = value.resolve(strict=exists)
    if not value.is_relative_to(ROOT):
        raise ValueError("Path escapes repository")
    return value


def binding(path, *, runtime=False):
    path = Path(path).resolve(strict=True) if runtime else local(path)
    if not path.is_file():
        raise ValueError("Regular file required")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return {
        "path": str(path) if runtime else path.relative_to(ROOT).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def verify(row, *, runtime=False):
    if (
        type(row.get("bytes")) is not int
        or row["bytes"] <= 0
        or type(row.get("sha256")) is not str
        or len(row["sha256"]) != 64
        or any(c not in "0123456789abcdef" for c in row["sha256"])
    ):
        raise ValueError("Strict file binding required")
    observed = binding(row["path"], runtime=runtime)
    if observed != {k: row[k] for k in ("path", "bytes", "sha256")}:
        raise ValueError("Bound source bytes changed")
    return Path(observed["path"]) if runtime else local(observed["path"])


def read_bound(row):
    if row["bytes"] > 2 * 1024 * 1024:
        raise ValueError("Small control JSON exceeds cap")
    return strict_json(verify(row).read_bytes())


def pin(path):
    row = binding(path)
    if (row["bytes"], row["sha256"]) != PINS[path]:
        raise ValueError("Frozen prerequisite identity differs: " + path)
    return row


def merge(rows):
    result = {}
    for row in rows:
        path = local(row["path"])
        normalized = {
            "path": path.relative_to(ROOT).as_posix(),
            "bytes": row["bytes"],
            "sha256": row["sha256"],
        }
        if normalized["path"] in result and result[normalized["path"]] != normalized:
            raise ValueError("Conflicting source identities")
        result[normalized["path"]] = normalized
    return [result[key] for key in sorted(result)]


def snapshot(rows, *, runtime=False):
    for row in rows:
        verify(row, runtime=runtime)
    return [binding(row["path"], runtime=runtime) for row in rows]


def forbidden_imports():
    return sorted(
        n
        for n in sys.modules
        if n.split(".", 1)[0] in {"torch", "transformers", "peft", "accelerate", "datasets"}
    )


def apis(sources):
    # Authenticate all inherited imports before executing frozen helper code.
    snapshot(sources)
    original = sys.path.copy()
    try:
        sys.path[:0] = [str(ROOT), str(ROOT / "src")]
        from multidetect.autonomy import onboard_navigation_actor as actor
        from multidetect.autonomy import onboard_navigation_supervision as navigation
        from scripts import audit_openfly_multiscene_navigation_geometry as geometry
        from scripts import build_onboard_openfly_navigation_dataset as builder
        from scripts import build_onboard_openfly_original82_supervision as bridge
        from scripts import build_openfly_bounded_annotation_index as index
        from scripts import extract_openfly_parquet_images as extractor
        from scripts import fetch_and_audit_openfly_parquet as acquisition
        from scripts import fetch_openfly_navigation_pilot as fetch
        from scripts.onboard_navigation_closed_sample_encoding import (
            ClosedNavigationSourceValidationSession,
        )
    finally:
        sys.path[:] = original
    imported = {
        "scripts/audit_openfly_multiscene_navigation_geometry.py": geometry,
        "scripts/build_onboard_openfly_navigation_dataset.py": builder,
        "scripts/build_onboard_openfly_original82_supervision.py": bridge,
        "scripts/build_openfly_bounded_annotation_index.py": index,
        "scripts/extract_openfly_parquet_images.py": extractor,
        "scripts/fetch_and_audit_openfly_parquet.py": acquisition,
        "scripts/fetch_openfly_navigation_pilot.py": fetch,
        "src/multidetect/autonomy/onboard_navigation_actor.py": actor,
        "src/multidetect/autonomy/onboard_navigation_supervision.py": navigation,
    }
    for expected, module in imported.items():
        if Path(module.__file__).resolve() != local(expected):
            raise ValueError("Imported helper is not the authenticated repository file")
    snapshot(sources)
    if forbidden_imports():
        raise ValueError("Model imports are forbidden in this CPU source audit")
    return SimpleNamespace(
        g=geometry,
        b=builder,
        bridge=bridge,
        index=index,
        e=extractor,
        acq=acquisition,
        f=fetch,
        a=actor,
        n=navigation,
        Session=ClosedNavigationSourceValidationSession,
    )


def runtime_information():
    modules = [importlib.import_module(name) for name in RUNTIME_MODULES]
    return {
        "versions": {
            name: importlib.metadata.version(name) for name in ("Pillow", "PyYAML", "pyarrow")
        },
        "modules": {
            name: binding(module.__file__, runtime=True)
            for name, module in zip(RUNTIME_MODULES, modules, strict=True)
        },
        "scope": "ten_listed_CPU_entrypoints_and_extensions_not_all_transitive_modules_or_DLLs",
    }


def annotation_stat(path):
    path = local(path)
    stat = path.stat()
    return {
        "path": str(path),
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "device": stat.st_dev,
        "inode": stat.st_ino,
    }


def validate_acquisition(report, plan, index):
    if (
        report.get("contract") != "multidetect.openfly-indexed-training-route-payloads-v1"
        or type(report.get("native_exit_code")) is not int
        or report["native_exit_code"] != 0
        or report.get("all_twelve_payloads_verified") is not True
        or report.get("payload_files_verified") != 12
        or report.get("verified_payload_bytes") != 83692236
        or report.get("small_sources_unchanged") is not True
        or canonical(report["sources_before"]) != canonical(report["sources_after"])
        or canonical(report["runtime_sources_before"]) != canonical(report["runtime_sources_after"])
        or canonical(report["routes"]) != canonical(plan["routes"])
        or any(value is not False for value in report["claims"].values())
    ):
        raise ValueError("Closed exact acquisition success required")
    routes = report["routes"]
    if [
        (r["annotation_index"], r["candidate_frame_count_from_terminal_id"]) for r in routes
    ] != list(ROUTES):
        raise ValueError("Exact six route order/count declarations required")
    for route in routes:
        if (
            route["scene_id"] != "env_airsim_26"
            or route["original_annotation_split"] != "train"
            or route["locked_development_role"] != "train"
            or not route["image_path"].startswith("env_airsim_26/astar_data/low_short/")
        ):
            raise ValueError("Only fixed original TRAIN env26 episodes allowed")
        original = [
            r for r in index["routes"] if r["annotation_index"] == route["annotation_index"]
        ]
        if len(original) != 1 or canonical({k: original[0][k] for k in route}) != canonical(route):
            raise ValueError("Route is not the original indexed declaration")
    if (index["consumed_prefix"]["bytes"], index["consumed_prefix"]["sha256"]) != (
        PREFIX_BYTES,
        PREFIX_SHA,
    ):
        raise ValueError("Exact bounded original annotation prefix required")
    expected = [(r["annotation_index"], role) for r in routes for role in ("shard", "pose")]
    if [(r["annotation_index"], r["role"]) for r in report["payloads"]] != expected:
        raise ValueError("Exact twelve publisher payloads required")
    for row in report["payloads"]:
        route = next(r for r in routes if r["annotation_index"] == row["annotation_index"])
        path = (
            "traj/" + route["image_path"] + ".parquet"
            if row["role"] == "shard"
            else "Image/" + route["image_path"] + "/pose.jsonl"
        )
        if (
            row["repository_path"] != path
            or row.get("publisher_digest_verified") is not True
            or row["bytes"] != row["file"]["bytes"]
            or row["digest_kind"] != ("sha256-lfs" if row["role"] == "shard" else "sha1-git-blob")
        ):
            raise ValueError("Publisher-to-local payload declaration differs")


def build_plan():
    fixed = [pin(path) for path in PINS]
    report, acquired, index = (read_bound(pin(path)) for path in (REPORT, ACQUISITION_PLAN, INDEX))
    validate_acquisition(report, acquired, index)
    manifest_row = next(r for r in report["sources_before"] if r["path"].endswith("/manifest.json"))
    manifest = read_bound(manifest_row)
    sources = merge(
        fixed
        + report["sources_before"]
        + manifest["code_sources"]
        + manifest["license_sources"]
        + [binding(__file__), binding("tests/test_audit_openfly_indexed_training_routes.py")]
    )
    snapshot(sources)
    apis(sources)
    annotation = index["historical_annotation_binding"]
    if annotation_stat(annotation["path"]) != index["annotation_stat_after"]:
        raise ValueError("Historical annotation stat differs from bounded scan")
    result = {
        "schema_version": 1,
        "contract": PLAN_CONTRACT,
        "output_directory": OUTPUT,
        "acquisition_report": pin(REPORT),
        "acquisition_plan": pin(ACQUISITION_PLAN),
        "index_report": pin(INDEX),
        "config": pin(CONFIG),
        "annotation_declared_not_rehashed": annotation,
        "annotation_stat": annotation_stat(annotation["path"]),
        "annotation_prefix": index["consumed_prefix"],
        "dataset_repository": report["dataset_repository"],
        "dataset_revision": report["dataset_revision"],
        "routes": report["routes"],
        "payloads": report["payloads"],
        "sources": sources,
        "runtime": runtime_information(),
        "license": merge(manifest["license_sources"])[0],
        "generation_code": merge(manifest["code_sources"]),
        "budget": {
            "episodes": 6,
            "images": 122,
            "successors": 116,
            "max_encoded_bytes": 128 * 1024 * 1024,
            "batch_size": 1,
            "max_png_bytes": 8 * 1024 * 1024,
            "max_png_pixels": 16777216,
            "max_row_group_uncompressed_bytes": 128 * 1024 * 1024,
        },
        "claims": dict.fromkeys(FALSE_CLAIMS, False),
    }
    # The dataset card, not the code license, supplies the typed dataset license.
    result["license"] = next(
        r
        for r in merge(manifest["license_sources"])
        if r["path"].endswith("license/dataset/README.md")
    )
    snapshot(sources)
    return result


def select_annotations(plan, api):
    expected = {r["annotation_index"]: r for r in plan["routes"]}
    path = plan["annotation_declared_not_rehashed"]["path"]
    before = annotation_stat(path)
    if before != plan["annotation_stat"]:
        raise ValueError("Annotation stat changed before prefix read")
    proxy = api.index.BudgetedAnnotationPath(
        path, SimpleNamespace(max_bytes=PREFIX_BYTES), before["bytes"]
    )
    selected, visited = {}, 0
    try:
        for position, original in api.f.iter_annotation_records(proxy):
            visited += 1
            if position in expected:
                api.f.validate_annotation(original)  # No padding repair or pose reconstruction.
                route = expected[position]
                if (
                    original["image_path"] != route["image_path"]
                    or hashlib.sha256(api.index.canonical(original)).hexdigest()
                    != route["annotation_record_sha256"]
                ):
                    raise ValueError("Original annotation record identity differs")
                selected[position] = original
    except api.index.ByteBudgetReached:
        pass
    finally:
        if proxy.raw is not None:
            proxy.raw.close()
    if (
        proxy.raw is None
        or proxy.raw.bytes_read != PREFIX_BYTES
        or proxy.raw.sha256.hexdigest() != PREFIX_SHA
        or set(selected) != set(expected)
        or annotation_stat(path) != before
    ):
        raise ValueError("Authenticated prefix/selected record coverage changed")
    return selected, visited


def verify_prefix(plan):
    """Rehash only the admitted byte range, including parser read-ahead."""
    path = plan["annotation_declared_not_rehashed"]["path"]
    if annotation_stat(path) != plan["annotation_stat"]:
        raise ValueError("Annotation stat changed before bounded prefix recheck")
    digest, size = hashlib.sha256(), 0
    with local(path).open("rb") as stream:
        while size < PREFIX_BYTES:
            block = stream.read(min(65536, PREFIX_BYTES - size))
            if not block:
                raise ValueError("Original annotation prefix ended early")
            size += len(block)
            digest.update(block)
    if digest.hexdigest() != PREFIX_SHA or annotation_stat(path) != plan["annotation_stat"]:
        raise ValueError("Original annotation prefix changed")
    return {
        "bytes": size,
        "sha256": digest.hexdigest(),
        "scope": "actual_binary_bytes_read_including_parser_read_ahead_not_record_boundary",
    }


def create_bytes(path, raw):
    path = local(path, exists=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return binding(path)


def create_json(path, value):
    return create_bytes(path, canonical(value) + b"\n")


def extract_images(route, metadata, output, api, written):
    reader, schema = api.acq.checked_parquet(
        verify(route["shard_file"]), route["required_frame_count"]
    )
    if canonical(schema) != canonical(metadata["schema_metadata"]):
        reader.close()
        raise ValueError("Parquet schema changed between metadata and image pass")
    images = []
    try:
        for batch in reader.iter_batches(batch_size=1, use_threads=False):
            if batch.num_rows != 1:
                raise ValueError("One original row per batch required")
            row = batch.to_pylist()[0]
            step = len(images)
            if step >= route["required_frame_count"] or canonical(
                {k: row[k] for k in api.acq.METADATA_COLUMNS}
            ) != canonical(metadata["metadata_rows"][step]):
                raise ValueError("Original Parquet row/episode metadata differs")
            image = row.get("image")
            if (
                type(image) is not dict
                or set(image) != {"bytes", "path"}
                or type(image["bytes"]) is not bytes
            ):
                raise ValueError("Original embedded PNG bytes required; no NULL/path fallback")
            raw = image["bytes"]
            info = api.e.check_png(raw)
            if (
                len(written) >= 122
                or sum(r["file"]["bytes"] for r in written) + len(raw) > 128 * 1024 * 1024
            ):
                raise ValueError("Original encoded image count/byte budget exceeded")
            file = create_bytes(
                output / "images" / route["image_path"] / (row["image_id"] + ".png"), raw
            )
            if (file["bytes"], file["sha256"]) != (len(raw), hashlib.sha256(raw).hexdigest()):
                raise ValueError("Original PNG byte publication differs")
            record = {
                "annotation_index": route["annotation_index"],
                "image_path": route["image_path"],
                "parquet_row_index": step,
                "image_id": row["image_id"],
                "source_shard": route["shard_file"],
                "publisher_shard": route["shard_publisher"],
                "file": file,
                "png": info,
                "original_encoded_bytes_preserved": True,
                "opaque_image_path_used_as_source": False,
            }
            images.append(record)
            written.append(record)
            del row, image, raw, batch
        if len(images) != route["required_frame_count"]:
            raise ValueError("Incomplete original image sequence")
        return images
    finally:
        reader.close()


def make_samples(route, annotation, actions, metrics, images, sources, api):
    episode = hashlib.sha256((api.f.REVISION + ":" + route["image_path"]).encode()).hexdigest()
    frames = [
        {
            "frame_id": hashlib.sha256((episode + ":" + str(i)).encode()).hexdigest(),
            "episode_id": episode,
            "scene_id": route["scene_id"],
            "step_index": i,
            **image["file"],
            "width": image["png"]["width"],
            "height": image["png"]["height"],
            "encoding": "png",
            "source_clock_id": None,
            "source_stamp": None,
            "stamp_unit": None,
        }
        for i, image in enumerate(images)
    ]
    return [
        api.bridge.make_sample(route, annotation, action, target, frames, i, sources, api.n)
        for i, (action, (target, _check)) in enumerate(zip(actions, metrics, strict=True))
    ]


def verify_actors(samples, api):
    bindings, body_completed = [], False
    with api.Session(ROOT) as session:
        session.prime(samples)
        for sample in samples:
            actor = api.a.NavigationActorInput.from_supervision_sample(
                sample, ROOT, session=session
            )
            messages = None
            try:
                messages = api.a.serialize_navigation_actor_messages(actor)
                target = strict_json(
                    api.n.serialize_navigation_supervision_target(sample, ROOT, session=session)
                )
                if messages[1]["content"][0] != {
                    "type": "text",
                    "text": sample.prompt.text_original,
                }:
                    raise ValueError("Verbatim actor prompt differs")
                bindings.append(
                    {
                        "sample_id": sample.sample_id,
                        "episode_id": sample.episode_id,
                        "actor_identity_sha256": actor.identity_sha256,
                        "ordered_frame_steps": [f.step_index for f in sample.frames],
                        "target": target,
                    }
                )
            finally:
                if messages is not None:
                    for message in messages:
                        for item in message["content"]:
                            if item["type"] == "image":
                                item["image"].close()
                del actor, messages
        body_completed = True
    if not body_completed or len(bindings) != len(samples) or session.verified is not True:
        raise ValueError("Normal complete verified source session required")
    return bindings


def audit(plan_path, expected_sha):
    plan_row = binding(plan_path)
    if plan_row["sha256"] != expected_sha:
        raise ValueError("Explicit audit plan SHA differs")
    plan = read_bound(plan_row)
    if canonical(plan) != canonical(build_plan()):
        raise ValueError("Audit plan differs from fixed source/CPU runtime contract")
    output = local(plan["output_directory"], exists=False)
    if output != ROOT / OUTPUT or output.exists():
        raise ValueError("Fresh fixed create-only audit output required")
    declared = merge(plan["sources"] + [plan_row] + [r["file"] for r in plan["payloads"]])
    before = snapshot(declared)
    api = apis(plan["sources"])
    if runtime_information() != plan["runtime"]:
        raise ValueError("Actual imported CPU entrypoint identity differs")
    selected, visited = select_annotations(plan, api)
    prefix_before = verify_prefix(plan)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(exist_ok=False)
    images, route_audits, samples = [], [], []
    try:
        selection = create_json(
            output / "annotation-selection.json", {str(k): v for k, v in selected.items()}
        )
        review = create_json(
            output / "annotation-selection-provenance.json",
            {
                "contract": "multidetect.openfly-prefix-derived-original-selection-v1",
                "dataset_repository": plan["dataset_repository"],
                "dataset_revision": plan["dataset_revision"],
                "original_declared_not_rehashed": plan["annotation_declared_not_rehashed"],
                "actual_prefix": plan["annotation_prefix"],
                "derived_selection": selection,
                "original_record_canonical_bindings": [
                    {
                        "annotation_index": r["annotation_index"],
                        "sha256": r["annotation_record_sha256"],
                    }
                    for r in plan["routes"]
                ],
                "whole_annotation_sha256_verified": False,
                "full_array_syntax_verified": False,
                "selection_values_unchanged": True,
                "selection_is_official_full_annotation": False,
            },
        )
        converter = create_bytes(
            output / "conversion" / Path(__file__).name, Path(__file__).read_bytes()
        )
        unit = create_json(
            output / "unit-verification.json",
            {
                "contract": "multidetect.openfly-source-units-review-v1",
                "code_revision": api.f.CODE_REVISION,
                "generation_code": plan["generation_code"],
                "selected_scene_configs": [plan["config"]],
                "source_coordinate_frame": "openfly-world-xy-z-up-not-geographic",
                "position_unit": "m",
                "source_yaw_unit": "rad",
                "raw_turn_value_unit": "deg",
                "target_translation_frame": "body_frd",
                "target_yaw_convention": "clockwise_right_positive",
                "body_transform": ["cos(yaw)*dx+sin(yaw)*dy", "sin(yaw)*dx-cos(yaw)*dy", "-dz"],
                "yaw_transform": "-atan2(sin(next_yaw-yaw),cos(next_yaw-yaw))",
                "pose_origin": "planned_pose",
                "physical_calibration_or_execution_evidence": False,
                "dt_s": None,
            },
        )
        if "license: apache-2.0" not in verify(plan["license"]).read_text(encoding="utf-8"):
            raise ValueError("Original dataset license card required")
        for original_route in plan["routes"]:
            route = {
                **original_route,
                "required_frame_count": original_route["candidate_frame_count_from_terminal_id"],
                "development_partition": "train",
            }
            records = [
                r for r in plan["payloads"] if r["annotation_index"] == route["annotation_index"]
            ]
            records += [
                {
                    "annotation_index": route["annotation_index"],
                    "role": "config",
                    "file": plan["config"],
                }
            ]
            for record in records:
                raw_file = verify(record["file"])
                if record["role"] == "shard":
                    if record["digest"] != record["file"]["sha256"]:
                        raise ValueError("Publisher LFS SHA differs")
                elif record["role"] == "pose":
                    raw = raw_file.read_bytes()
                    if (
                        hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
                        != record["digest"]
                    ):
                        raise ValueError("Publisher pose Git blob differs")
            shard, pose = (
                next(r for r in records if r["role"] == role) for role in ("shard", "pose")
            )
            route.update(
                shard_file=shard["file"],
                shard_publisher={
                    k: shard[k] for k in ("repository_path", "bytes", "digest", "digest_kind")
                },
            )
            original = selected[route["annotation_index"]]
            metadata = api.acq.audit_metadata(route, records, original)
            actions, _offline_landmark = api.f.parse_pose(verify(pose["file"]).read_bytes())
            metrics = api.g.metric_targets(
                actions, metadata["metadata_rows"], api.b.derive_primitive
            )
            extracted = extract_images(route, metadata, output, api, images)
            proof = create_json(
                output / f"alignment/{route['annotation_index']}.json",
                {
                    "contract": "multidetect.openfly-raw-step-alignment-v1",
                    "pose": pose["file"],
                    "annotation": selection,
                    "annotation_index": route["annotation_index"],
                    "position_tolerance_m": api.b.POSITION_TOLERANCE_M,
                    "yaw_tolerance_rad": api.b.YAW_TOLERANCE_RAD,
                    "transitions": [check for _, check in metrics],
                    "annotation_selected_pose_alignment": metadata,
                    "aggregate_annotation_actions_excluded_from_gold": True,
                    "no_successor_rgb_in_actor_input": True,
                    "dt_s": None,
                },
            )
            sources = {
                "repository_id": api.f.DATASET,
                "revision": api.f.REVISION,
                "original_split": "train",
                "annotation_record_id": str(route["annotation_index"]),
                "annotation": selection,
                "pose": pose["file"],
                "conversion": converter,
                "license": plan["license"],
                "review_receipt": review,
                "unit_verification": unit,
                "alignment_verification": proof,
            }
            samples.extend(make_samples(route, original, actions, metrics, extracted, sources, api))
            route_audits.append(
                {
                    "annotation_index": route["annotation_index"],
                    "image_path": route["image_path"],
                    "metadata": metadata,
                    "targets_and_geometry_checks": [
                        {"target": target, "check": check} for target, check in metrics
                    ],
                }
            )
            print(
                json.dumps(
                    {
                        "event": "indexed_episode_verified",
                        "annotation_index": route["annotation_index"],
                        "images": len(extracted),
                    }
                ),
                flush=True,
            )
        samples = api.n.validate_navigation_episode_samples(tuple(samples))
        if (
            len(samples) != 122
            or len({s.episode_id for s in samples}) != 6
            or sum(s.planned_action.kind == "stop" for s in samples) != 6
        ):
            raise ValueError(
                "Actual complete six episodes/122 samples/116 successors/6 STOP required"
            )
        actors = verify_actors(samples, api)
        after = snapshot(declared)
        for image in images:
            verify(image["file"])
        if (
            before != after
            or runtime_information() != plan["runtime"]
            or forbidden_imports()
            or annotation_stat(plan["annotation_declared_not_rehashed"]["path"])
            != plan["annotation_stat"]
        ):
            raise ValueError("Sources/CPU runtime/prefix stat changed before publication")
        prefix_after = verify_prefix(plan)
        if prefix_before != prefix_after:
            raise ValueError("Admitted annotation prefix changed before publication")
        dataset = create_bytes(
            output / "samples.jsonl",
            b"".join((api.b.canonical_json(s.to_mapping()) + "\n").encode() for s in samples),
        )
        report = {
            "schema_version": 1,
            "contract": CONTRACT,
            "native_exit_code": 0,
            "external_native_status_verification_required": True,
            "plan": plan_row,
            "sample_count": len(samples),
            "episode_count": 6,
            "partition_counts": {"train": 122},
            "successor_count": 116,
            "terminal_stop_count": 6,
            "dataset": dataset,
            "images": images,
            "original_encoded_image_bytes": sum(r["file"]["bytes"] for r in images),
            "routes": route_audits,
            "annotation_selection": selection,
            "annotation_provenance": review,
            "annotation_records_inspected_in_prefix": visited,
            "source_bindings_before": before,
            "annotation_prefix_before": prefix_before,
            "annotation_prefix_after": prefix_after,
            "source_bindings_after": after,
            "source_bindings_unchanged": True,
            "runtime": plan["runtime"],
            "source_validation_session_verified": True,
            "sample_actor_target_bindings": actors,
            "actor_input": "verbatim_prompt_current_plus_latest_past_original_RGB_only",
            "pose_future_truth_or_label_in_actor_input": False,
            "raw_primitive_counts": dict(
                Counter(s.to_mapping()["planned_action"]["raw"]["type"] for s in samples)
            ),
            "planned_not_executed": True,
            "dt_s": None,
            "memory_scope": (
                "one_bounded_row_group_one_row_and_one_actor_no_full_RGB_cache_RSS_unmeasured"
            ),
            **dict.fromkeys(FALSE_CLAIMS, False),
        }
        create_json(output / "report.json", report)
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
                "images_published_before_failure": len(images),
                **dict.fromkeys(FALSE_CLAIMS, False),
            },
        )
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit-plan")
    parser.add_argument("--plan")
    parser.add_argument("--plan-sha256")
    args = parser.parse_args(argv)
    if args.emit_plan and not args.plan and not args.plan_sha256:
        print(json.dumps(create_json(args.emit_plan, build_plan())))
    elif args.plan and args.plan_sha256 and not args.emit_plan:
        result = audit(args.plan, args.plan_sha256)
        print(
            json.dumps(
                {
                    "event": "indexed_training_source_audit_finished",
                    "native_exit_code": 0,
                    "sample_count": result["sample_count"],
                }
            )
        )
    else:
        parser.error("Use --emit-plan PATH or --plan PATH --plan-sha256 SHA")


if __name__ == "__main__":
    main()
