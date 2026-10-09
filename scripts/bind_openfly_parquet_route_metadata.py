"""Bind exactly three original TRAIN annotation episodes to publisher shards.

One public paths-info metadata request only. No parquet/image/pose/config
payloads, model loads, official seen/unseen annotations, or GPU operations.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.openfly-parquet-route-metadata-plan-v1"
REPORT_CONTRACT = "multidetect.openfly-parquet-route-metadata-v1"
OUTPUT = "artifacts/diagnostics/openfly-parquet-exact-routes-20261008-r1"
LEAF_REPORT = "artifacts/diagnostics/openfly-parquet-leaf-metadata-20261008-r1/report.json"
SOURCE_FILES = (
    "scripts/bind_openfly_parquet_route_metadata.py",
    "tests/test_bind_openfly_parquet_route_metadata.py",
)
BUDGET = {
    "max_api_requests": 1,
    "max_metadata_bytes": 65536,
    "max_payload_bytes_for_future_plan": 128 * 1024 * 1024,
}


def helpers():
    original = sys.path.copy()
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from scripts import probe_openfly_parquet_leaf_metadata as leaf
    finally:
        sys.path[:] = original
    return leaf, *leaf.helpers()


def build_plan():
    leaf, _publication, probe, acquisition, planner, fetch, _builder = helpers()
    initial_path = planner.local_path(probe.INITIAL_PLAN)
    initial, admitted_sources = acquisition.admit_plan(
        initial_path, planner.binding(initial_path)["sha256"]
    )
    leaf_path = planner.local_path(LEAF_REPORT)
    if leaf_path.stat().st_size > planner.BUDGET["max_tree_bytes"]:
        raise ValueError("Leaf metadata report exceeds cap")
    leaf_report = planner.strict_json(leaf_path.read_bytes())
    if (
        leaf_report.get("contract") != leaf.REPORT_CONTRACT
        or type(leaf_report.get("native_exit_code")) is not int
        or leaf_report["native_exit_code"] != 0
        or leaf_report.get("dataset_revision") != fetch.REVISION
        or leaf_report.get("sources_unchanged") is not True
        or leaf_report.get("parquet_payload_downloaded") is not False
        or planner.canonical(leaf_report["sources_before"])
        != planner.canonical(leaf_report["sources_after"])
    ):
        raise ValueError("Only the sealed metadata-only leaf result is admitted")
    manifest = planner.load_manifest(initial["source_manifest"]["path"])
    annotations, inspected = planner.selected_annotations(
        manifest["annotation"]["path"], {route["annotation_index"] for route in initial["routes"]}
    )
    code_tree = planner.strict_json(planner.verify_binding(initial["code_tree"]).read_bytes())
    original_path = planner.local_path(probe.ORIGINAL_OUTPUT + "/report.json")
    original = planner.strict_json(original_path.read_bytes())
    routes = []
    for route in initial["routes"]:
        index = route["annotation_index"]
        annotation = annotations[index]
        if (
            annotation["image_path"] != route["image_path"]
            or hashlib.sha256(planner.canonical(annotation)).hexdigest()
            != route["annotation_record_sha256"]
        ):
            raise ValueError("Original TRAIN annotation episode identity changed")
        config_path = f"configs/{route['scene_id']}.yaml"
        configs = [row for row in code_tree["tree"] if row.get("path") == config_path]
        raw_rows = [row for row in original["routes"] if row["annotation_index"] == index]
        if len(configs) != 1 or len(raw_rows) != 1:
            raise ValueError("Exact config/pose publisher metadata is absent")
        pose_path = f"Image/{annotation['image_path']}/pose.jsonl"
        poses = [
            row for row in raw_rows[0]["publisher_files"] if row["repository_path"] == pose_path
        ]
        if len(poses) != 1:
            raise ValueError("Exact original pose metadata is absent")
        routes.append(
            {
                "annotation_index": index,
                "scene_id": route["scene_id"],
                "development_partition": route["partition"],
                "original_annotation_split": "train",
                "original_annotation_record_sha256": route["annotation_record_sha256"],
                "image_path": annotation["image_path"],
                "parquet_path": "traj/" + annotation["image_path"] + ".parquet",
                "required_frame_count": route["required_frame_count"],
                "annotation_image_ids": fetch.validate_annotation(annotation),
                "annotation_filenames": annotation["index_list"],
                "pose_publisher": poses[0],
                "config_publisher": {
                    "repository_path": config_path,
                    **acquisition.metadata_record(configs[0], cap=65536),
                },
            }
        )
    specification = importlib.util.find_spec("huggingface_hub")
    if specification is None or specification.origin is None:
        raise ValueError("Existing official API source is unavailable")
    api_source = Path(specification.origin).with_name("hf_api.py")
    declarations = planner.merge_source_records(
        admitted_sources
        + leaf_report["sources_before"]
        + [planner.binding(leaf_path), planner.binding(original_path), planner.binding(api_source)]
        + leaf_report["retained_metadata"]
        + [planner.binding(ROOT / name) for name in SOURCE_FILES]
    )
    for row in declarations.values():
        planner.verify_binding(row)
    return {
        "contract": CONTRACT,
        "dataset_repository": fetch.DATASET,
        "dataset_revision": fetch.REVISION,
        "code_repository": fetch.CODE_REPOSITORY,
        "code_revision": fetch.CODE_REVISION,
        "source_manifest": initial["source_manifest"],
        "original_train_annotation": planner.binding(manifest["annotation"]["path"]),
        "annotation_records_inspected": inspected,
        "routes": routes,
        "metadata_endpoint": f"https://huggingface.co/api/datasets/{fetch.DATASET}/paths-info/{fetch.REVISION}",
        "request_method": "POST_read_only_paths_info",
        "authentication": False,
        "endpoint_reference": {
            "package": "huggingface_hub",
            "version": importlib.metadata.version("huggingface_hub"),
            "api_source": planner.binding(api_source),
            "function": "HfApi.get_paths_info",
        },
        "output_directory": OUTPUT,
        "budget": BUDGET.copy(),
        "sources": [declarations[key] for key in sorted(declarations)],
        "payload_download_authorized": False,
        "claims": acquisition.GATES.copy(),
    }


def exact_rows(document, routes):
    _leaf, _publication, _probe, acquisition, _planner, _fetch, _builder = helpers()
    if not isinstance(document, list) or len(document) != 3:
        raise ValueError("All three exact TRAIN episode shard paths must exist")
    wanted = {route["parquet_path"] for route in routes}
    mapped = {}
    for row in document:
        if not isinstance(row, dict) or row.get("path") not in wanted or row.get("path") in mapped:
            raise ValueError("Unexpected, duplicate, or unrequested publisher shard")
        declared = acquisition.metadata_record(row, cap=BUDGET["max_payload_bytes_for_future_plan"])
        if declared["digest_kind"] != "sha256-lfs":
            raise ValueError("Require actual LFS SHA-256 for the shard")
        mapped[row["path"]] = declared
    return [
        {
            **route,
            "shard_publisher": {
                "repository_path": route["parquet_path"],
                **mapped[route["parquet_path"]],
            },
        }
        for route in routes
    ]


def public_request(plan):
    _leaf, _publication, _probe, _acquisition, planner, fetch, _builder = helpers()
    client = fetch.OfficialClient()
    # A truthy identity auth handler suppresses netrc credentials; no token used.
    client.session.auth = lambda request: request
    response = None
    try:
        response = client.session.post(
            plan["metadata_endpoint"],
            data={"paths": [route["parquet_path"] for route in plan["routes"]], "expand": False},
            stream=True,
            allow_redirects=False,
            timeout=(15, 60),
            headers={"Accept-Encoding": "identity", "User-Agent": "Multi-Detect-metadata/1"},
        )
        if response.status_code != 200 or response.request.headers.get("Authorization"):
            raise ValueError("Public pinned metadata request requires unauthenticated HTTP 200")
        if response.headers.get("Content-Encoding", "identity") != "identity":
            raise ValueError("Unexpected metadata content encoding")
        length = response.headers.get("Content-Length")
        if length is not None and (
            not length.isdigit() or not 0 < int(length) <= BUDGET["max_metadata_bytes"]
        ):
            raise ValueError("Exact shard metadata exceeds bounded Content-Length")
        raw = bytearray()
        for block in response.iter_content(4096):
            raw.extend(block)
            if len(raw) > BUDGET["max_metadata_bytes"]:
                raise ValueError("Exact shard metadata exceeds 64 KiB cap")
        if length is not None and len(raw) != int(length):
            raise ValueError("Exact shard metadata differs from Content-Length")
        payload = bytes(raw)
        return planner.strict_json(payload), payload, fetch._response_headers(response.headers)
    finally:
        if response is not None:
            response.close()
        client.session.close()


def run_binding(path, expected_sha, *, requester=None):
    _leaf, _publication, _probe, acquisition, planner, _fetch, _builder = helpers()
    path = planner.local_path(path)
    admitted = planner.binding(path)
    if admitted["bytes"] > planner.BUDGET["max_tree_bytes"] or admitted["sha256"] != expected_sha:
        raise ValueError("Explicit exact-route metadata plan SHA/size differs")
    plan = planner.strict_json(path.read_bytes())
    if planner.canonical(plan) != planner.canonical(build_plan()):
        raise ValueError("Exact-route plan differs from actual TRAIN episode/source authority")
    declared = planner.merge_source_records(plan["sources"] + [admitted])
    before = [declared[key] for key in sorted(declared)]
    for row in before:
        planner.verify_binding(row)
    out = acquisition.guarded_path(plan["output_directory"], fresh=True)
    if out != ROOT / OUTPUT:
        raise ValueError("Only the new exact-route metadata output is writable")
    out.parent.mkdir(parents=True, exist_ok=True)
    acquisition.guarded_path(plan["output_directory"], fresh=True)
    out.mkdir(exist_ok=False)
    native_exit, failure, metadata, routes = 0, None, None, []
    try:
        document, raw, headers = (requester or public_request)(plan)
        if not isinstance(raw, bytes) or not 0 < len(raw) <= BUDGET["max_metadata_bytes"]:
            raise ValueError("Original exact-path metadata exceeds cap")
        metadata = acquisition.create_bytes(out / "paths-info.json", raw)
        if planner.canonical(planner.strict_json(raw)) != planner.canonical(document):
            raise ValueError("Returned metadata differs from retained original JSON")
        routes = exact_rows(document, plan["routes"])
        total = sum(route["shard_publisher"]["bytes"] for route in routes)
        total += sum(
            route["pose_publisher"]["bytes"] + route["config_publisher"]["bytes"]
            for route in routes
        )
        if total > BUDGET["max_payload_bytes_for_future_plan"]:
            raise ValueError(
                "Exact three shards plus original pose/config exceed future 128 MiB cap"
            )
        planner.verify_binding(metadata)
    except Exception as exc:
        native_exit = 1
        headers = {}
        failure = {
            "type": type(exc).__name__,
            "reason": str(exc)
            if isinstance(exc, ValueError)
            else "public exact-path metadata query failed",
        }
    after = []
    for row in before:
        try:
            after.append(planner.binding(row["path"]))
        except Exception:
            native_exit = 1
    unchanged = planner.canonical(before) == planner.canonical(after)
    if not unchanged:
        native_exit = 1
    report = {
        "contract": REPORT_CONTRACT,
        "native_exit_code": native_exit,
        "plan": admitted,
        "original_train_annotation": plan["original_train_annotation"],
        "dataset_repository": plan["dataset_repository"],
        "dataset_revision": plan["dataset_revision"],
        "metadata": metadata,
        "response_headers": headers,
        "failure": failure,
        "routes": routes,
        "shard_bytes_sum": sum(route["shard_publisher"]["bytes"] for route in routes),
        "pose_config_bytes_sum": sum(
            route["pose_publisher"]["bytes"] + route["config_publisher"]["bytes"]
            for route in routes
        ),
        "all_three_train_episode_paths_exist": len(routes) == 3 and native_exit == 0,
        "original_train_annotation_membership_bound": native_exit == 0,
        "shard_contents_or_schema_verified": False,
        "reserved_seen_unseen_annotation_read": False,
        "payload_downloaded": False,
        "sources_before": before,
        "sources_after": after,
        "sources_unchanged": unchanged,
        **acquisition.GATES,
    }
    record = acquisition.create_bytes(out / "report.json", acquisition.json_bytes(report))
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
                raise ValueError("Emission does not query metadata")
            print(json.dumps(build_plan(), ensure_ascii=False, sort_keys=True, indent=2))
            return 0
        if args.plan is None or args.plan_sha256 is None:
            raise ValueError("Explicit exact-route metadata plan and SHA required")
        report, record = run_binding(args.plan, args.plan_sha256)
        print(
            json.dumps(
                {
                    "event": "exact_train_route_metadata_finished",
                    "native_exit_code": report["native_exit_code"],
                    "report": record,
                    "routes": report["routes"],
                    "shard_bytes_sum": report["shard_bytes_sum"],
                    "pose_config_bytes_sum": report["pose_config_bytes_sum"],
                    "shard_contents_or_schema_verified": False,
                    "payload_downloaded": False,
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
                    "event": "exact_train_route_metadata_admission_failed",
                    "native_exit_code": 1,
                    "error_type": type(exc).__name__,
                    "reason": str(exc)
                    if isinstance(exc, ValueError)
                    else "Metadata-only admission failed",
                    "training_ready": False,
                }
            ),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
