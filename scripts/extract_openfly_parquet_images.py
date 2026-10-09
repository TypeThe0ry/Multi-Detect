"""Extract original embedded PNG bytes from three verified TRAIN route shards.

CPU-only, offline, create-only. This phase does not transcode, merge datasets,
derive flight labels, claim independent holdout, train, or promote a model.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.openfly-embedded-png-extraction-plan-v1"
REPORT_CONTRACT = "multidetect.openfly-original-embedded-png-evidence-v1"
ACQUISITION_RECEIPT = (
    "artifacts/training/openfly-multiscene-parquet-source-20261008-r1/receipt.json"
)
OUTPUT = "artifacts/training/openfly-multiscene-embedded-images-20261008-r1"
SOURCE_FILES = (
    "scripts/extract_openfly_parquet_images.py",
    "tests/test_extract_openfly_parquet_images.py",
)
BUDGET = {
    "max_shards": 3,
    "max_images": 82,
    "max_encoded_image_bytes": 8 * 1024 * 1024,
    "max_total_extracted_bytes": 128 * 1024 * 1024,
    "max_decoded_pixels_per_image": 16777216,
    "max_png_chunks_per_image": 4096,
    "max_plan_report_bytes": 2 * 1024 * 1024,
    "batch_size": 1,
}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
DEFLATE_BLOCK_BYTES = 65536


def expected_scanline_bytes(width, height, interlace):
    if interlace == 0:
        return (1 + 3 * width) * height
    total = 0
    for x, y, dx, dy in (
        (0, 0, 8, 8),
        (4, 0, 8, 8),
        (0, 4, 4, 8),
        (2, 0, 4, 4),
        (0, 2, 2, 4),
        (1, 0, 2, 2),
        (0, 1, 1, 2),
    ):
        columns = (width - x + dx - 1) // dx if width > x else 0
        rows = (height - y + dy - 1) // dy if height > y else 0
        if columns and rows:
            total += (1 + 3 * columns) * rows
    return total


def count_deflate_bytes(inflater, data, expected, decoded):
    """Incrementally verify one original zlib stream without retaining pixels."""
    for cursor in range(0, len(data), DEFLATE_BLOCK_BYTES):
        pending = data[cursor : cursor + DEFLATE_BLOCK_BYTES]
        while len(pending):
            try:
                block = inflater.decompress(
                    pending, min(DEFLATE_BLOCK_BYTES, expected - decoded + 1)
                )
            except zlib.error as exc:
                raise ValueError("Original PNG IDAT zlib stream is invalid") from exc
            decoded += len(block)
            if decoded > expected or inflater.unused_data:
                raise ValueError(
                    "PNG IDAT has extra decoded pixels, trailing data, or multiple zlib streams"
                )
            pending = inflater.unconsumed_tail
            del block
    return decoded


def helpers():
    original = sys.path.copy()
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from scripts import fetch_and_audit_openfly_parquet as acquisition
    finally:
        sys.path[:] = original
    return acquisition, *acquisition.helpers()


def runtime_information():
    acquisition, *_other = helpers()
    import PIL
    from PIL import Image, ImageFile, PngImagePlugin

    runtime = acquisition.runtime_information()
    records = runtime["sources"] + [
        acquisition.absolute_binding(path)
        for path in (
            PIL.__file__,
            Image.__file__,
            ImageFile.__file__,
            PngImagePlugin.__file__,
            Image.core.__file__,
        )
    ]
    mapped = {}
    for record in records:
        if record["path"] in mapped and mapped[record["path"]] != record:
            raise ValueError("Installed CPU runtime source declarations conflict")
        mapped[record["path"]] = record
    return {
        "versions": {**runtime["versions"], "Pillow": PIL.__version__},
        "sources": [mapped[key] for key in sorted(mapped)],
        "source_inventory_scope": "listed_package_entrypoints_extensions_not_all_transitive_DLLs",
    }


def build_plan():
    acquisition, _exact, _leaf, _publication, _probe, io_helper, planner, fetch, _builder = (
        helpers()
    )
    path = planner.local_path(ACQUISITION_RECEIPT)
    if path.stat().st_size > BUDGET["max_plan_report_bytes"]:
        raise ValueError("Sealed Parquet acquisition receipt exceeds cap")
    report = planner.strict_json(path.read_bytes())
    if (
        report.get("contract") != acquisition.REPORT_CONTRACT
        or type(report.get("native_exit_code")) is not int
        or report["native_exit_code"] != 0
        or report.get("dataset_repository") != fetch.DATASET
        or report.get("dataset_revision") != fetch.REVISION
        or report.get("all_exact_original_payloads_verified") is not True
        or report.get("all_metadata_rows_pose_aligned") is not True
        or report.get("published_outputs_unchanged") is not True
        or report.get("sources_unchanged") is not True
        or report.get("reserved_seen_unseen_read") is not False
        or report.get("old_dataset_merged_or_modified") is not False
        or report.get("embedded_image_column_materialized_or_extracted") is not False
        or report.get("model_loaded") is not False
        or report.get("GPU_used") is not False
        or report.get("forbidden_model_imports") != []
        or report.get("failures") != []
        or report.get("partial_files") != []
        or planner.canonical(report["sources_before"]) != planner.canonical(report["sources_after"])
        or report.get("runtime_sources_before") != report.get("runtime_sources_after")
        or any(report.get(key) is not False for key in io_helper.GATES)
    ):
        raise ValueError(
            "Only the sealed successful original TRAIN acquisition/metadata phase is admitted"
        )
    old_plan_path = planner.verify_binding(report["plan"])
    old_plan = planner.strict_json(old_plan_path.read_bytes())
    if planner.canonical(old_plan) != planner.canonical(acquisition.build_plan()):
        raise ValueError(
            "Sealed acquisition plan differs from exact original TRAIN/source authority"
        )
    if planner.canonical(report["routes"]) != planner.canonical(old_plan["routes"]):
        raise ValueError(
            "Stage1 receipt route identity differs from its admitted original TRAIN plan"
        )
    records = report["verified_payloads"]
    if len(records) != len(old_plan["payloads"]):
        raise ValueError("Stage1 verified payload count differs from its exact admitted plan")
    for actual, declared in zip(records, old_plan["payloads"], strict=True):
        if (
            set(actual) != set(declared) | {"file", "response_chain", "publisher_digest_verified"}
            or planner.canonical({key: actual[key] for key in declared})
            != planner.canonical(declared)
            or actual["publisher_digest_verified"] is not True
        ):
            raise ValueError(
                "Stage1 verified payload declaration differs from its admitted publisher plan"
            )
    routes = []
    for route in report["routes"]:
        matching = [
            row
            for row in report["verified_payloads"]
            if row["annotation_index"] == route["annotation_index"] and row["role"] == "shard"
        ]
        audits = [
            row
            for row in report["metadata_audits"]
            if row["annotation_index"] == route["annotation_index"]
        ]
        if len(matching) != 1 or len(audits) != 1 or route["original_annotation_split"] != "train":
            raise ValueError(
                "Each exact original TRAIN route requires one actual shard and one metadata audit"
            )
        shard, audit = matching[0], audits[0]
        planner.verify_binding(shard["file"])
        if (
            shard["publisher_digest_verified"] is not True
            or shard["file"]["sha256"] != route["shard_publisher"]["digest"]
            or shard["file"]["bytes"] != route["shard_publisher"]["bytes"]
            or len(audit["metadata_rows"]) != route["required_frame_count"]
            or audit["raw_pose_matches_all_metadata_rows"] is not True
            or audit["annotation_sparse_filename_pose_yaw_match"] is not True
            or audit["embedded_image_validity_verified"] is not False
        ):
            raise ValueError("Actual shard bytes/metadata row identity is incomplete")
        routes.append(
            {
                **route,
                "shard_file": shard["file"],
                "metadata_rows": audit["metadata_rows"],
                "schema_metadata": audit["schema_metadata"],
            }
        )
    if (
        len(routes) != BUDGET["max_shards"]
        or sum(row["required_frame_count"] for row in routes) != BUDGET["max_images"]
    ):
        raise ValueError("Only three exact TRAIN shards with 82 original rows admitted")
    sources = planner.merge_source_records(
        report["sources_before"]
        + [planner.binding(path)]
        + [row["file"] for row in report["verified_payloads"]]
        + [planner.binding(ROOT / name) for name in SOURCE_FILES]
    )
    for record in sources.values():
        planner.verify_binding(record)
    runtime = runtime_information()
    actual_runtime = {row["path"]: row for row in runtime["sources"]}
    for record in report["runtime_sources_before"]:
        if actual_runtime.get(record["path"]) != record:
            raise ValueError("Stage1 CPU runtime must remain unchanged during extraction")
    if acquisition.forbidden_imports():
        raise ValueError("Offline PNG evidence phase must not import model libraries")
    return {
        "contract": CONTRACT,
        "acquisition_receipt": planner.binding(path),
        "dataset_repository": fetch.DATASET,
        "dataset_revision": fetch.REVISION,
        "original_train_annotation": report["original_train_annotation"],
        "routes": routes,
        "expected_images": BUDGET["max_images"],
        "sources": [sources[key] for key in sorted(sources)],
        "runtime": runtime,
        "budget": BUDGET.copy(),
        "output_directory": OUTPUT,
        "original_embedded_bytes_extraction_authorized": True,
        "network_authorized": False,
        "transcode_authorized": False,
        "merge_old_dataset_authorized": False,
        "flight_label_derivation_authorized": False,
        "independent_holdout_claim_allowed": False,
        "claims": io_helper.GATES.copy(),
    }


def check_png(raw):
    """Verify every PNG chunk CRC and bounded RGB8 header before real decode."""
    if (
        type(raw) is not bytes
        or not 0 < len(raw) <= BUDGET["max_encoded_image_bytes"]
        or not raw.startswith(PNG_SIGNATURE)
    ):
        raise ValueError(
            "Require bounded original embedded PNG bytes; path-only fallback forbidden"
        )
    view = memoryview(raw)
    cursor, chunks, idats = 8, 0, 0
    dimensions = None
    seen_iend = False
    idat_closed = False
    seen_plte = False
    inflater = zlib.decompressobj()
    expected_decoded, decoded = None, 0
    while cursor < len(view):
        chunks += 1
        if chunks > BUDGET["max_png_chunks_per_image"] or len(view) - cursor < 12:
            raise ValueError("PNG chunk count or remaining chunk size is invalid")
        length = int.from_bytes(view[cursor : cursor + 4], "big")
        kind = bytes(view[cursor + 4 : cursor + 8])
        end = cursor + 12 + length
        if (
            end > len(view)
            or not all(65 <= letter <= 90 or 97 <= letter <= 122 for letter in kind)
            or kind[2] & 32
        ):
            raise ValueError("Malformed PNG chunk boundary/type/reserved bit")
        data = view[cursor + 8 : cursor + 8 + length]
        crc = zlib.crc32(data, zlib.crc32(kind)) & 0xFFFFFFFF
        if crc != int.from_bytes(view[end - 4 : end], "big"):
            raise ValueError("Original embedded PNG chunk CRC mismatch")
        if chunks == 1 and kind != b"IHDR":
            raise ValueError("PNG must begin with original IHDR")
        if kind == b"IHDR":
            if dimensions is not None or chunks != 1 or length != 13:
                raise ValueError("Duplicate or malformed PNG IHDR")
            width, height, bit_depth, color, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", data
            )
            if (
                not width
                or not height
                or width * height > BUDGET["max_decoded_pixels_per_image"]
                or bit_depth != 8
                or color != 2
                or compression != 0
                or filtering != 0
                or interlace not in (0, 1)
            ):
                raise ValueError(
                    "Original PNG must be bounded RGB8; no color conversion/rescaling allowed"
                )
            dimensions = width, height
            expected_decoded = expected_scanline_bytes(width, height, interlace)
        elif kind == b"PLTE":
            if seen_plte or idats or not 0 < length <= 768 or length % 3:
                raise ValueError("Malformed/out-of-order PNG palette")
            seen_plte = True
        elif kind == b"IDAT":
            if idat_closed:
                raise ValueError("PNG IDAT chunks must be consecutive")
            idats += 1
            decoded = count_deflate_bytes(inflater, data, expected_decoded, decoded)
        elif kind == b"IEND":
            if length or not idats or end != len(view):
                raise ValueError("PNG IEND/trailing bytes or image data invalid")
            seen_iend = True
        elif not kind[0] & 32:
            raise ValueError("Unknown critical PNG chunk is not admitted")
        elif idats:
            idat_closed = True
        cursor = end
    if dimensions is None or not seen_iend or not idats:
        raise ValueError("Incomplete original embedded PNG")
    if (
        not inflater.eof
        or inflater.unused_data
        or inflater.unconsumed_tail
        or decoded != expected_decoded
    ):
        raise ValueError(
            "Original PNG IDAT must be one complete zlib stream with exact scanline bytes"
        )
    from PIL import Image, ImageFile

    if ImageFile.LOAD_TRUNCATED_IMAGES is not False:
        raise ValueError("Decoder must not admit truncated original PNGs")

    with Image.open(io.BytesIO(raw)) as image:
        if (
            image.format != "PNG"
            or image.mode != "RGB"
            or image.size != dimensions
            or getattr(image, "is_animated", False)
        ):
            raise ValueError("Actual image decode must be one original RGB PNG with matching IHDR")
        image.load()
        result = {
            "encoding": "PNG",
            "mode": "RGB",
            "width": image.width,
            "height": image.height,
            "decoded_pixels": image.width * image.height,
            "bit_depth": 8,
            "png_chunk_count": chunks,
            "all_chunk_CRCs_verified": True,
            "actual_Pillow_decode_succeeded": True,
            "complete_single_zlib_stream_verified": True,
            "original_scanline_bytes_verified": decoded,
            "interlace": interlace,
            "transcoded": False,
        }
    return result


def extract_route(route, out, *, written):
    acquisition, _exact, _leaf, _publication, _probe, io_helper, planner, _fetch, _builder = (
        helpers()
    )
    path = planner.verify_binding(route["shard_file"])
    reader, schema = acquisition.checked_parquet(path, route["required_frame_count"])
    if planner.canonical(schema) != planner.canonical(route["schema_metadata"]):
        reader.close()
        raise ValueError("Actual Parquet schema/row-group metadata changed since Stage1")
    index = 0
    try:
        for batch in reader.iter_batches(batch_size=BUDGET["batch_size"], use_threads=False):
            if batch.num_rows != 1:
                raise ValueError("Original embedded PNG processing requires one row per batch")
            row = batch.to_pylist()[0]
            if index >= route["required_frame_count"]:
                raise ValueError("Extra embedded image row is not allowed")
            metadata = {key: row[key] for key in acquisition.METADATA_COLUMNS}
            if planner.canonical(metadata) != planner.canonical(route["metadata_rows"][index]):
                raise ValueError(
                    "Image row metadata differs from exact Stage1 original pose/episode identity"
                )
            image = row.get("image")
            if (
                type(image) is not dict
                or set(image) != {"bytes", "path"}
                or type(image["bytes"]) is not bytes
            ):
                raise ValueError(
                    "Original embedded image bytes required; NULL/path-only fallback forbidden"
                )
            raw = image["bytes"]
            encoded_sha = hashlib.sha256(raw).hexdigest()
            info = check_png(raw)
            if (
                len(written) >= BUDGET["max_images"]
                or sum(item["file"]["bytes"] for item in written) + len(raw)
                > BUDGET["max_total_extracted_bytes"]
            ):
                raise ValueError("Original image count/total encoded byte cap exceeded")
            image_name = metadata["image_id"] + ".png"
            relative = (
                f"{out.relative_to(ROOT).as_posix()}/images/{route['image_path']}/{image_name}"
            )
            target = io_helper.guarded_path(relative, fresh=True)
            record = io_helper.create_bytes(target, raw)
            if record["bytes"] != len(raw) or record["sha256"] != encoded_sha:
                raise ValueError(
                    "Extracted file must exactly equal original embedded encoded bytes"
                )
            written.append(
                {
                    "annotation_index": route["annotation_index"],
                    "scene_id": route["scene_id"],
                    "development_partition_proposed_not_accepted": route["development_partition"],
                    "original_annotation_split": "train",
                    "image_path": route["image_path"],
                    "parquet_row_index": index,
                    "frame_index": metadata["frame_index"],
                    "image_id": metadata["image_id"],
                    "source_shard": route["shard_file"],
                    "publisher_shard": route["shard_publisher"],
                    "embedded_encoded_bytes": len(raw),
                    "embedded_encoded_sha256": encoded_sha,
                    "file": record,
                    "png": info,
                    "original_encoded_bytes_preserved": True,
                    "opaque_embedded_path_used_as_source": False,
                    "metadata_identity_matches_stage1": True,
                }
            )
            index += 1
            # The receipt retains hashes/metadata only, not encoded image buffers.
            del raw, image, row, batch
        if index != route["required_frame_count"]:
            raise ValueError("Missing embedded original image rows")
        return {
            "annotation_index": route["annotation_index"],
            "original_images_extracted": index,
            "source_shard": route["shard_file"],
        }
    finally:
        reader.close()


def extract(path, expected_sha):
    acquisition, _exact, _leaf, _publication, _probe, io_helper, planner, fetch, _builder = (
        helpers()
    )
    path = planner.local_path(path)
    admitted = planner.binding(path)
    if admitted["bytes"] > BUDGET["max_plan_report_bytes"] or admitted["sha256"] != expected_sha:
        raise ValueError("Explicit NEW extraction plan SHA/bytes differ")
    plan = planner.strict_json(path.read_bytes())
    if planner.canonical(plan) != planner.canonical(build_plan()):
        raise ValueError("Image extraction plan differs from exact sealed original TRAIN sources")
    declarations = planner.merge_source_records(plan["sources"] + [admitted])
    before = [declarations[key] for key in sorted(declarations)]
    for record in before:
        planner.verify_binding(record)
    runtime_before = plan["runtime"]["sources"]
    if runtime_information() != plan["runtime"] or acquisition.forbidden_imports():
        raise ValueError("Installed CPU decoder/source changed or forbidden model imports observed")
    out = io_helper.guarded_path(plan["output_directory"], fresh=True)
    if out != ROOT / OUTPUT:
        raise ValueError("Only the create-only NEW original-image root is writable")
    out.parent.mkdir(parents=True, exist_ok=True)
    io_helper.guarded_path(plan["output_directory"], fresh=True)
    out.mkdir(exist_ok=False)
    images, completed, failures = [], [], []
    for route in plan["routes"]:
        print(
            json.dumps(
                {
                    "event": "original_png_route_started",
                    "annotation_index": route["annotation_index"],
                    "expected_images": route["required_frame_count"],
                }
            ),
            flush=True,
        )
        try:
            completed.append(extract_route(route, out, written=images))
        except Exception as exc:
            failures.append(
                {
                    "stage": "original_png_extraction",
                    "annotation_index": route["annotation_index"],
                    "type": type(exc).__name__,
                    "reason": str(exc)
                    if isinstance(exc, ValueError)
                    else "Original embedded PNG validation/extraction failed",
                }
            )
    after, runtime_after = [], []
    for record in before:
        try:
            after.append(planner.binding(record["path"]))
        except Exception:
            pass
    for record in runtime_before:
        try:
            runtime_after.append(acquisition.absolute_binding(record["path"]))
        except Exception:
            pass
    unchanged = (
        planner.canonical(before) == planner.canonical(after) and runtime_before == runtime_after
    )
    reverified = 0
    for image in images:
        try:
            planner.verify_binding(image["file"])
            reverified += 1
        except Exception:
            failures.append(
                {
                    "stage": "original_png_output_recheck",
                    "type": "ValueError",
                    "reason": "Published original PNG bytes changed before receipt",
                }
            )
    known_files = {image["file"]["path"] for image in images}
    unverified = [
        planner.binding(file)
        for file in sorted(out.rglob("*.png"))
        if file.relative_to(ROOT).as_posix() not in known_files
    ]
    imports = acquisition.forbidden_imports()
    native_exit = int(
        bool(failures)
        or not unchanged
        or bool(imports)
        or len(images) != plan["expected_images"]
        or len(completed) != BUDGET["max_shards"]
        or bool(unverified)
    )
    report = {
        "contract": REPORT_CONTRACT,
        "native_exit_code": native_exit,
        "plan": admitted,
        "acquisition_receipt": plan["acquisition_receipt"],
        "dataset_repository": fetch.DATASET,
        "dataset_revision": fetch.REVISION,
        "original_train_annotation": plan["original_train_annotation"],
        "completed_routes": completed,
        "images": images,
        "PNG_decode_validated_count_at_extraction": len(images),
        "current_output_files_reverified_count": reverified,
        "original_encoded_bytes_extracted": sum(image["file"]["bytes"] for image in images),
        "all_82_original_images_verified": native_exit == 0,
        "all_source_shard_row_to_encoded_byte_bindings_verified": native_exit == 0,
        "sources_before": before,
        "sources_after": after,
        "runtime_sources_before": runtime_before,
        "runtime_sources_after": runtime_after,
        "sources_unchanged": unchanged,
        "failures": failures,
        "unverified_output_files": unverified,
        "forbidden_model_imports": imports,
        "network_io_in_phase": False,
        "transcoded_images": 0,
        "opaque_path_fallback_used": False,
        "old_dataset_merged_or_modified": False,
        "reserved_seen_unseen_read": False,
        "model_loaded": False,
        "GPU_used": False,
        "optimizer_updates": 0,
        "independent_holdout_verified": False,
        "FRD_flight_labels_verified": False,
        "next_required_gate": "separately_reviewed_visual_split_and_navigation_dataset_plan",
        **io_helper.GATES,
    }
    record = io_helper.create_bytes(out / "receipt.json", io_helper.json_bytes(report))
    return report, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit-plan", action="store_true")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--plan-sha256")
    args = parser.parse_args(argv)
    try:
        if args.emit_plan:
            if args.plan is not None or args.plan_sha256 is not None:
                raise ValueError("Plan emission does not extract images")
            print(json.dumps(build_plan(), ensure_ascii=False, sort_keys=True, indent=2))
            return 0
        if args.plan is None or args.plan_sha256 is None:
            raise ValueError("Explicit NEW extraction plan and SHA required")
        report, record = extract(args.plan, args.plan_sha256)
        print(
            json.dumps(
                {
                    "event": "original_embedded_png_extraction_finished",
                    "native_exit_code": report["native_exit_code"],
                    "receipt": record,
                    "original_images": report["PNG_decode_validated_count_at_extraction"],
                    "bytes": report["original_encoded_bytes_extracted"],
                    "failures": report["failures"],
                    "training_ready": False,
                    "independent_holdout_verified": False,
                }
            ),
            flush=True,
        )
        return report["native_exit_code"]
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "original_png_extraction_admission_failed",
                    "native_exit_code": 1,
                    "error_type": type(exc).__name__,
                    "reason": str(exc)
                    if isinstance(exc, ValueError)
                    else "Original PNG extraction admission failed",
                    "training_ready": False,
                }
            ),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
