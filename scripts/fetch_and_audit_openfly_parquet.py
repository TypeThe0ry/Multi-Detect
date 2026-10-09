"""Acquire three exact original TRAIN shards and audit CPU metadata columns.

This create-only phase never extracts/transcodes images, reads official
seen/unseen data, merges datasets, loads a model, or authorizes training.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import re
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.openfly-parquet-acquisition-plan-v1"
REPORT_CONTRACT = "multidetect.openfly-parquet-acquisition-metadata-audit-v1"
OUTPUT = "artifacts/training/openfly-multiscene-parquet-source-20261008-r1"
EXACT_REPORT = "artifacts/diagnostics/openfly-parquet-exact-routes-20261008-r1/report.json"
SOURCE_FILES = (
    "scripts/fetch_and_audit_openfly_parquet.py",
    "tests/test_fetch_and_audit_openfly_parquet.py",
)
BUDGET = {
    "max_payload_files": 9,
    "max_http_attempts_per_file": 6,
    "max_http_attempts_total_upper_bound": 54,
    "max_total_download_bytes": 128 * 1024 * 1024,
    "max_small_file_bytes": 65536,
    "max_plan_report_bytes": 2 * 1024 * 1024,
    "max_rows_per_shard": 42,
    "max_uncompressed_parquet_bytes": 128 * 1024 * 1024,
    "max_footer_string_bytes": 8 * 1024 * 1024,
    "max_footer_container_items": 100000,
    "metadata_batch_size": 1,
}
METADATA_COLUMNS = (
    "env_id",
    "traj_id",
    "image_id",
    "frame_index",
    "pos",
    "yaw",
    "action_type",
    "action_value",
)


def helpers():
    original = sys.path.copy()
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from scripts import bind_openfly_parquet_route_metadata as exact
    finally:
        sys.path[:] = original
    return exact, *exact.helpers()


def absolute_binding(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file() or path.is_symlink():
        raise ValueError("Runtime source must be a regular installed file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def runtime_information():
    import pyarrow
    import pyarrow._parquet
    import pyarrow.parquet
    import pyarrow.parquet.core
    import yaml

    files = (
        pyarrow.__file__,
        pyarrow._parquet.__file__,
        pyarrow.parquet.__file__,
        pyarrow.parquet.core.__file__,
        yaml.__file__,
    )
    return {
        "versions": {
            "pyarrow": importlib.metadata.version("pyarrow"),
            "PyYAML": importlib.metadata.version("PyYAML"),
        },
        "sources": [absolute_binding(path) for path in files],
        "source_inventory_scope": (
            "listed_package_entrypoints_and_parquet_extension_not_all_transitive_DLLs"
        ),
    }


def forbidden_imports():
    prefixes = ("torch", "transformers", "peft", "accelerate", "datasets")
    return sorted(name for name in sys.modules if name.split(".", 1)[0] in prefixes)


def route_payloads(route, fetch):
    result = []
    for role, key in (
        ("pose", "pose_publisher"),
        ("config", "config_publisher"),
        ("shard", "shard_publisher"),
    ):
        row = route[key]
        path = fetch._repository_path(row["repository_path"])
        if role == "shard":
            if (
                path != "traj/" + route["image_path"] + ".parquet"
                or row["digest_kind"] != "sha256-lfs"
            ):
                raise ValueError(
                    "Shard must be the exact original TRAIN episode with publisher LFS SHA"
                )
            url = f"https://huggingface.co/datasets/{fetch.DATASET}/resolve/{fetch.REVISION}/{quote(path)}"
        elif role == "pose":
            if path != "Image/" + route["image_path"] + "/pose.jsonl":
                raise ValueError("Pose must belong to the exact original TRAIN episode")
            url = fetch.dataset_url(path)
        else:
            if path != f"configs/{route['scene_id']}.yaml":
                raise ValueError("Scene config does not match the original annotation")
            url = f"https://raw.githubusercontent.com/{fetch.CODE_REPOSITORY}/{fetch.CODE_REVISION}/{path}"
        result.append(
            {"role": role, "annotation_index": route["annotation_index"], "url": url, **row}
        )
    return result


def build_plan():
    exact, _leaf, _publication, _probe, acquisition, planner, fetch, _builder = helpers()
    path = planner.local_path(EXACT_REPORT)
    if path.stat().st_size > BUDGET["max_plan_report_bytes"]:
        raise ValueError("Exact TRAIN route report exceeds cap")
    report = planner.strict_json(path.read_bytes())
    if (
        report.get("contract") != exact.REPORT_CONTRACT
        or type(report.get("native_exit_code")) is not int
        or report["native_exit_code"] != 0
        or report.get("dataset_repository") != fetch.DATASET
        or report.get("dataset_revision") != fetch.REVISION
        or report.get("all_three_train_episode_paths_exist") is not True
        or report.get("original_train_annotation_membership_bound") is not True
        or report.get("reserved_seen_unseen_annotation_read") is not False
        or report.get("payload_downloaded") is not False
        or report.get("sources_unchanged") is not True
        or planner.canonical(report["sources_before"]) != planner.canonical(report["sources_after"])
    ):
        raise ValueError("Only the sealed exact original TRAIN metadata phase is admitted")
    previous_plan_path = planner.verify_binding(report["plan"])
    previous_plan = planner.strict_json(previous_plan_path.read_bytes())
    if planner.canonical(previous_plan) != planner.canonical(exact.build_plan()):
        raise ValueError("Exact route plan differs from current original TRAIN authority")
    metadata_path = planner.verify_binding(report["metadata"])
    routes = exact.exact_rows(
        planner.strict_json(metadata_path.read_bytes()), previous_plan["routes"]
    )
    if planner.canonical(routes) != planner.canonical(report["routes"]):
        raise ValueError("Actual retained publisher rows and the report differ")
    payloads = [row for route in routes for row in route_payloads(route, fetch)]
    if len(payloads) != 9 or len({row["url"] for row in payloads}) != 9:
        raise ValueError("Exactly three shards and their three original pose/config pairs required")
    total = sum(row["bytes"] for row in payloads)
    if total > BUDGET["max_total_download_bytes"]:
        raise ValueError("All exact payloads must fit 128 MiB")
    sources = planner.merge_source_records(
        report["sources_before"]
        + [planner.binding(path), planner.binding(metadata_path)]
        + [planner.binding(ROOT / name) for name in SOURCE_FILES]
    )
    for record in sources.values():
        planner.verify_binding(record)
    if forbidden_imports():
        raise ValueError("This CPU data phase must not import model libraries")
    return {
        "contract": CONTRACT,
        "exact_train_metadata_report": planner.binding(path),
        "dataset_repository": fetch.DATASET,
        "dataset_revision": fetch.REVISION,
        "code_repository": fetch.CODE_REPOSITORY,
        "code_revision": fetch.CODE_REVISION,
        "original_train_annotation": report["original_train_annotation"],
        "routes": routes,
        "payloads": payloads,
        "expected_total_download_bytes": total,
        "sources": [sources[key] for key in sorted(sources)],
        "runtime": runtime_information(),
        "budget": BUDGET.copy(),
        "output_directory": OUTPUT,
        "download_authorized": True,
        "metadata_columns_audited": list(METADATA_COLUMNS),
        "embedded_image_column_materialized_or_extracted": False,
        "source_units": "native_only_not_inferred_or_rescaled",
        "claims": acquisition.GATES.copy(),
    }


def download_payload(client, row, output, allowlist):
    _exact, _leaf, _publication, _probe, acquisition, planner, fetch, _builder = helpers()
    if row["url"] not in allowlist or planner.canonical(row) != planner.canonical(
        allowlist[row["url"]]
    ):
        raise ValueError("Initial payload path/size/digest is outside the exact approved allowlist")
    cap = (
        BUDGET["max_total_download_bytes"]
        if row["role"] == "shard"
        else BUDGET["max_small_file_bytes"]
    )
    size = row["bytes"]
    kind = row["digest_kind"]
    expected = row["digest"]
    if type(size) is not int or not 0 < size <= cap or kind not in ("sha256-lfs", "sha1-git-blob"):
        raise ValueError("Publisher file type/size exceeds the exact acquisition contract")
    if (
        not isinstance(expected, str)
        or re.fullmatch(r"[0-9a-f]{64}" if kind == "sha256-lfs" else r"[0-9a-f]{40}", expected)
        is None
    ):
        raise ValueError("Publisher digest is malformed")
    relative = f"{output.relative_to(ROOT).as_posix()}/payload/{row['repository_path']}"
    target = acquisition.guarded_path(relative, fresh=True)
    partial = acquisition.guarded_path(relative + ".partial", fresh=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    acquisition.guarded_path(relative, fresh=True)
    acquisition.guarded_path(relative + ".partial", fresh=True)
    digest = hashlib.sha256() if kind == "sha256-lfs" else hashlib.sha1()
    if kind == "sha1-git-blob":
        digest.update(f"blob {size}\0".encode("ascii"))
    response = None
    count = 0
    print(
        json.dumps(
            {
                "event": "parquet_source_started",
                "role": row["role"],
                "annotation_index": row["annotation_index"],
                "expected_bytes": size,
            }
        ),
        flush=True,
    )
    try:
        response, trail = client.stream(row["url"])
        if response.status_code != 200 or response.request.headers.get("Authorization"):
            raise ValueError("Exact public full-file download requires unauthenticated HTTP 200")
        length = response.headers.get("Content-Length")
        if length is not None and (not length.isdigit() or int(length) != size):
            raise ValueError("Payload Content-Length differs from publisher bytes")
        if response.headers.get("Content-Encoding", "identity") != "identity":
            raise ValueError("Payload encoding must preserve original published bytes")
        with partial.open("xb") as stream:
            for block in response.iter_content(1024 * 1024):
                count += len(block)
                if count > size or count > cap:
                    raise ValueError("Payload exceeds exact publisher/authorized byte cap")
                digest.update(block)
                stream.write(block)
            stream.flush()
            os.fsync(stream.fileno())
        if count != size or digest.hexdigest() != expected:
            raise ValueError("Original publisher bytes or digest do not match")
        acquisition.guarded_path(relative, fresh=True)
        os.link(partial, target)
        partial.unlink()
        record = planner.binding(target)
        print(
            json.dumps(
                {
                    "event": "parquet_source_verified",
                    "role": row["role"],
                    "annotation_index": row["annotation_index"],
                    "bytes": count,
                }
            ),
            flush=True,
        )
        return {**row, "file": record, "response_chain": trail, "publisher_digest_verified": True}
    finally:
        if response is not None:
            response.close()


def expected_schema():
    import pyarrow as pa

    return pa.schema(
        [
            ("env_id", pa.string()),
            ("traj_id", pa.string()),
            ("image_id", pa.string()),
            ("frame_index", pa.int32()),
            ("image", pa.struct([("bytes", pa.binary()), ("path", pa.string())])),
            ("pos", pa.list_(pa.float64(), 3)),
            ("yaw", pa.float64()),
            ("action_type", pa.string()),
            ("action_value", pa.int32()),
        ]
    )


def checked_parquet(path, required_rows):
    import pyarrow.parquet as pq

    if type(required_rows) is not int or not 0 < required_rows <= BUDGET["max_rows_per_shard"]:
        raise ValueError("Original TRAIN terminal frame count exceeds audit cap")
    parquet = pq.ParquetFile(
        path,
        memory_map=False,
        buffer_size=0,
        pre_buffer=False,
        thrift_string_size_limit=BUDGET["max_footer_string_bytes"],
        thrift_container_size_limit=BUDGET["max_footer_container_items"],
    )
    try:
        actual_schema = parquet.schema_arrow
        if not actual_schema.remove_metadata().equals(expected_schema()):
            raise ValueError(
                "Actual Parquet schema differs from exact declared publisher fields/types"
            )
        metadata = parquet.metadata
        if metadata.num_rows != required_rows or not 0 < metadata.num_row_groups <= required_rows:
            raise ValueError(
                "Actual Parquet row/row-group count differs from original terminal episode"
            )
        uncompressed = sum(
            metadata.row_group(i).total_byte_size for i in range(metadata.num_row_groups)
        )
        if not 0 < uncompressed <= BUDGET["max_uncompressed_parquet_bytes"]:
            raise ValueError("Parquet declared uncompressed bytes exceed CPU expansion cap")
        return parquet, {
            "schema": str(actual_schema),
            "schema_without_metadata_sha256": hashlib.sha256(
                actual_schema.remove_metadata().serialize().to_pybytes()
            ).hexdigest(),
            "rows": metadata.num_rows,
            "row_groups": metadata.num_row_groups,
            "declared_uncompressed_bytes": uncompressed,
        }
    except Exception:
        parquet.close()
        raise


def audit_metadata(route, records, annotation):
    _exact, _leaf, _publication, _probe, _acquisition, planner, fetch, _builder = helpers()
    import yaml

    by_role = {
        row["role"]: row for row in records if row["annotation_index"] == route["annotation_index"]
    }
    if set(by_role) != {"pose", "config", "shard"}:
        raise ValueError("Exact three original files must be verified before metadata audit")
    paths = {key: planner.verify_binding(row["file"]) for key, row in by_role.items()}
    actions, _offline_aim = fetch.parse_pose(paths["pose"].read_bytes())
    if len(actions) != route["required_frame_count"]:
        raise ValueError("Original pose terminal count differs from pinned TRAIN declaration")
    config = yaml.safe_load(paths["config"].read_bytes())
    scales = config.get("traj_map") if isinstance(config, dict) else None
    if not isinstance(scales, dict) or any(
        type(scales.get(key)) not in (int, float)
        or not math.isfinite(scales[key])
        or scales[key] != 1
        for key in ("traj_scale_ratio", "pcd_scale_ratio")
    ):
        raise ValueError("Original scene scales require separate review; no units will be guessed")
    parquet, summary = checked_parquet(paths["shard"], len(actions))
    rows, images = [], []
    try:
        for batch in parquet.iter_batches(
            batch_size=BUDGET["metadata_batch_size"],
            columns=list(METADATA_COLUMNS),
            use_threads=False,
        ):
            for row in batch.to_pylist():
                index = len(rows)
                if index >= len(actions) or any(row.get(key) is None for key in METADATA_COLUMNS):
                    raise ValueError("Missing/null/extra Parquet metadata rows are not repairable")
                action = actions[index]
                if (
                    row["env_id"] != PurePosixPath(route["image_path"]).parent.as_posix()
                    or row["traj_id"] != PurePosixPath(route["image_path"]).name
                    or type(row["frame_index"]) is not int
                    or row["frame_index"] != index
                    or not isinstance(row["image_id"], str)
                    or (match := re.fullmatch(r"[0-9]{8}_[0-9]{6}_([0-9]+)", row["image_id"]))
                    is None
                    or int(match[1]) != index
                    or not isinstance(row["pos"], list)
                    or len(row["pos"]) != 3
                    or any(
                        type(value) is not float or not math.isfinite(value) for value in row["pos"]
                    )
                    or type(row["yaw"]) is not float
                    or not math.isfinite(row["yaw"])
                    or type(row["action_value"]) is not int
                    or row["pos"] != action["pos"]
                    or row["yaw"] != action["yaw"]
                    or row["action_type"] != action["type"]
                    or row["action_value"] != action["value"]
                ):
                    raise ValueError(
                        f"Parquet metadata differs from original pose/episode at frame {index}"
                    )
                rows.append(row)
                images.append(
                    (
                        index,
                        {"path": "Image/" + route["image_path"] + "/" + row["image_id"] + ".png"},
                    )
                )
        if len(rows) != len(actions):
            raise ValueError("Parquet metadata row count differs from original pose")
        alignment = fetch.align_episode(annotation, actions, images)
        if alignment["annotation_pose_yaw_exact_match"] is not True:
            raise ValueError("Original TRAIN annotation and raw pose do not match exactly")
        return {
            "annotation_index": route["annotation_index"],
            "scene_id": route["scene_id"],
            "original_annotation_split": "train",
            "development_partition": route["development_partition"],
            "schema_metadata": summary,
            "metadata_rows": rows,
            "raw_pose_matches_all_metadata_rows": True,
            "annotation_sparse_filename_pose_yaw_match": True,
            "filename_alignment_scope": "embedded_image_id_only_not_publisher_Image_tree_evidence",
            "scene_scale_ratios_reviewed_as_one": True,
            "source_units_inferred_or_rescaled": False,
            "embedded_image_column_materialized_or_extracted": False,
            "embedded_image_validity_verified": False,
            "timestamps_s": None,
            "dt_s": None,
        }
    finally:
        parquet.close()


def acquire(path, expected_sha, *, client=None):
    _exact, _leaf, _publication, _probe, acquisition, planner, fetch, _builder = helpers()
    path = planner.local_path(path)
    admitted = planner.binding(path)
    if admitted["bytes"] > BUDGET["max_plan_report_bytes"] or admitted["sha256"] != expected_sha:
        raise ValueError("Explicit NEW acquisition plan SHA/bytes differ")
    plan = planner.strict_json(path.read_bytes())
    if planner.canonical(plan) != planner.canonical(build_plan()):
        raise ValueError(
            "Acquisition plan differs from actual exact TRAIN metadata/source authority"
        )
    declarations = planner.merge_source_records(plan["sources"] + [admitted])
    before = [declarations[key] for key in sorted(declarations)]
    for record in before:
        planner.verify_binding(record)
    runtime_before = plan["runtime"]["sources"]
    if runtime_information() != plan["runtime"] or forbidden_imports():
        raise ValueError("Installed CPU runtime/source changed or model libraries imported")
    annotations, _inspected = planner.selected_annotations(
        plan["original_train_annotation"]["path"],
        {row["annotation_index"] for row in plan["routes"]},
    )
    out = acquisition.guarded_path(plan["output_directory"], fresh=True)
    if out != ROOT / OUTPUT:
        raise ValueError("Only the fresh NEW parquet source root is writable")
    out.parent.mkdir(parents=True, exist_ok=True)
    acquisition.guarded_path(plan["output_directory"], fresh=True)
    out.mkdir(exist_ok=False)
    own_client = client is None
    if own_client:
        client = fetch.OfficialClient()
        client.session.auth = lambda request: request
    records, audits, failures = [], [], []
    initial_payload_requests_attempted = 0
    allowlist = {row["url"]: row for row in plan["payloads"]}
    try:
        for row in plan["payloads"]:
            initial_payload_requests_attempted += 1
            records.append(download_payload(client, row, out, allowlist))
        for route in plan["routes"]:
            try:
                audits.append(
                    audit_metadata(route, records, annotations[route["annotation_index"]])
                )
            except Exception as exc:
                failures.append(
                    {
                        "stage": "CPU_metadata_audit",
                        "annotation_index": route["annotation_index"],
                        "type": type(exc).__name__,
                        "reason": str(exc)
                        if isinstance(exc, ValueError)
                        else "CPU Parquet/pose audit failed",
                    }
                )
    except Exception as exc:
        failures.append(
            {
                "stage": "acquisition",
                "type": type(exc).__name__,
                "reason": str(exc)
                if isinstance(exc, ValueError)
                else "Exact public payload acquisition failed",
            }
        )
    finally:
        if own_client:
            try:
                client.session.close()
            except Exception:
                failures.append(
                    {
                        "stage": "client_cleanup",
                        "type": "RuntimeError",
                        "reason": "CPU data session cleanup failed",
                    }
                )
    after = []
    for record in before:
        try:
            after.append(planner.binding(record["path"]))
        except Exception:
            pass
    runtime_after = []
    for record in runtime_before:
        try:
            runtime_after.append(absolute_binding(record["path"]))
        except Exception:
            failures.append(
                {
                    "stage": "runtime_source_recheck",
                    "type": "ValueError",
                    "reason": "Admitted runtime source is missing or unreadable",
                }
            )
    unchanged = (
        planner.canonical(before) == planner.canonical(after) and runtime_before == runtime_after
    )
    published_outputs_unchanged = True
    for record in records:
        try:
            planner.verify_binding(record["file"])
        except Exception:
            published_outputs_unchanged = False
            failures.append(
                {
                    "stage": "published_output_recheck",
                    "type": "ValueError",
                    "reason": "Verified output bytes changed before receipt",
                }
            )
    imports = forbidden_imports()
    native_exit = int(
        bool(failures) or not unchanged or bool(imports) or len(records) != 9 or len(audits) != 3
    )
    partial = [planner.binding(path) for path in sorted(out.rglob("*.partial"))]
    report = {
        "contract": REPORT_CONTRACT,
        "native_exit_code": native_exit,
        "plan": admitted,
        "dataset_repository": fetch.DATASET,
        "dataset_revision": fetch.REVISION,
        "original_train_annotation": plan["original_train_annotation"],
        "routes": plan["routes"],
        "verified_payloads": records,
        "verified_payload_count": len(records),
        "initial_payload_requests_attempted": initial_payload_requests_attempted,
        "successful_payload_http_response_count": sum(
            len(row["response_chain"]) for row in records
        ),
        "http_total_count_unknown_after_transport_failure": any(
            row["stage"] == "acquisition" for row in failures
        ),
        "http_attempts_total_upper_bound": BUDGET["max_http_attempts_total_upper_bound"],
        "verified_payload_bytes": sum(row["file"]["bytes"] for row in records),
        "all_exact_original_payloads_verified": (
            len(records) == 9 and not partial and published_outputs_unchanged
        ),
        "published_outputs_unchanged": published_outputs_unchanged,
        "metadata_audits": audits,
        "all_metadata_rows_pose_aligned": len(audits) == 3 and native_exit == 0,
        "failures": failures,
        "partial_files": partial,
        "sources_before": before,
        "sources_after": after,
        "runtime_sources_before": runtime_before,
        "runtime_sources_after": runtime_after,
        "sources_unchanged": unchanged,
        "forbidden_model_imports": imports,
        "full_shard_payloads_downloaded_and_byte_hashed": (
            sum(row["role"] == "shard" for row in records) == 3
        ),
        "embedded_image_column_materialized_or_extracted": False,
        "embedded_image_validity_verified": False,
        "next_required_gate": (
            "separately_sealed_original_embedded_image_extraction_and_visual_split_audit"
        ),
        "reserved_seen_unseen_read": False,
        "old_dataset_merged_or_modified": False,
        "model_loaded": False,
        "GPU_used": False,
        "optimizer_updates": 0,
        **acquisition.GATES,
    }
    record = acquisition.create_bytes(out / "receipt.json", acquisition.json_bytes(report))
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
                raise ValueError("Plan emission does not download payloads")
            print(json.dumps(build_plan(), ensure_ascii=False, sort_keys=True, indent=2))
            return 0
        if args.plan is None or args.plan_sha256 is None:
            raise ValueError("Explicit NEW acquisition plan and SHA required")
        report, record = acquire(args.plan, args.plan_sha256)
        print(
            json.dumps(
                {
                    "event": "parquet_acquisition_cpu_metadata_finished",
                    "native_exit_code": report["native_exit_code"],
                    "receipt": record,
                    "verified_payload_count": report["verified_payload_count"],
                    "verified_payload_bytes": report["verified_payload_bytes"],
                    "metadata_audited_routes": len(report["metadata_audits"]),
                    "failures": report["failures"],
                    "embedded_image_column_materialized_or_extracted": False,
                    "training_ready": False,
                }
            ),
            flush=True,
        )
        return report["native_exit_code"]
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "parquet_acquisition_admission_failed",
                    "native_exit_code": 1,
                    "error_type": type(exc).__name__,
                    "reason": str(exc)
                    if isinstance(exc, ValueError)
                    else "NEW acquisition admission failed",
                    "training_ready": False,
                }
            ),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
