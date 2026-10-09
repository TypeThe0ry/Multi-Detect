"""Inspect pinned OpenFly traj publication metadata without reading shard bytes.

At most six API tree requests, two tree levels, eight MiB of metadata and two
small pinned publisher documentation files. No parquet, archive, RGB or model
payload is fetched. Source declarations and the explicit plan are rehashed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.openfly-parquet-publication-probe-plan-v1"
REPORT_CONTRACT = "multidetect.openfly-parquet-publication-metadata-v1"
OUTPUT = "artifacts/diagnostics/openfly-parquet-publication-20261008-r1"
AVAILABILITY_REPORT = (
    "artifacts/diagnostics/openfly-multiscene-availability-20261008-r1/report.json"
)
SOURCE_FILES = (
    "scripts/probe_openfly_parquet_publication.py",
    "tests/test_probe_openfly_parquet_publication.py",
)
BUDGET = {
    "max_tree_requests": 6,
    "max_tree_levels": 2,
    "max_total_metadata_bytes": 8 * 1024 * 1024,
    "max_tree_bytes": 2 * 1024 * 1024,
    "max_document_bytes": 64 * 1024,
}
DOCUMENT_NAMES = ("hf.py", "upload.sh")


def helpers():
    original = sys.path.copy()
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from scripts import probe_openfly_multiscene_availability as probe
    finally:
        sys.path[:] = original
    return probe, *probe.helpers()


def build_plan():
    probe, acquisition, planner, fetch, _builder = helpers()
    report_path = planner.local_path(AVAILABILITY_REPORT)
    if report_path.stat().st_size > BUDGET["max_tree_bytes"]:
        raise ValueError("Availability evidence exceeds cap")
    report = planner.strict_json(report_path.read_bytes())
    if (
        report.get("contract") != probe.REPORT_CONTRACT
        or report.get("mode") != "availability"
        or type(report.get("native_exit_code")) is not int
        or report["native_exit_code"] != 0
        or report.get("sources_unchanged") is not True
        or report.get("payload_download_performed") is not False
        or len(report.get("routes", [])) != 24
    ):
        raise ValueError("Admit only the sealed metadata-only availability result")
    initial_path = planner.local_path(probe.INITIAL_PLAN)
    initial, inputs = acquisition.admit_plan(initial_path, planner.binding(initial_path)["sha256"])
    manifest = planner.load_manifest(initial["source_manifest"]["path"])
    root_records = [
        row
        for row in manifest["metadata_records"]
        if Path(row["path"]).name == "dataset-root-tree.json"
    ]
    if len(root_records) != 1:
        raise ValueError("Pinned dataset root metadata is absent")
    root_path = planner.local_path(root_records[0]["path"])
    root_tree = planner.strict_json(root_path.read_bytes())
    traj = [entry for entry in root_tree if entry.get("path") == "traj"]
    if len(traj) != 1 or traj[0].get("type") != "directory":
        raise ValueError("Only the retained pinned traj directory is admitted")
    documents = []
    for name in DOCUMENT_NAMES:
        entries = [entry for entry in root_tree if entry.get("path") == name]
        if len(entries) != 1:
            raise ValueError("Pinned publisher documentation metadata is absent")
        documents.append(
            {
                "repository_path": name,
                "url": f"https://huggingface.co/datasets/{fetch.DATASET}/resolve/{fetch.REVISION}/{name}",
                **acquisition.metadata_record(entries[0], cap=BUDGET["max_document_bytes"]),
            }
        )
    bindings = planner.merge_source_records(
        inputs
        + report["sources_before"]
        + report["sources_after"]
        + [planner.binding(report_path)]
        + [planner.binding(ROOT / name) for name in SOURCE_FILES]
    )
    for row in bindings.values():
        planner.verify_binding(row)
    return {
        "contract": CONTRACT,
        "dataset_repository": fetch.DATASET,
        "dataset_revision": fetch.REVISION,
        "availability_report": planner.binding(report_path),
        "dataset_root_tree": planner.binding(root_path),
        "initial_tree_path": "traj",
        "child_selection": "up_to_first_five_direct_child_directories_in_publisher_order",
        "output_directory": OUTPUT,
        "budget": BUDGET.copy(),
        "documentation": documents,
        "sources": [bindings[key] for key in sorted(bindings)],
        "payload_download_authorized": False,
        "claims": acquisition.GATES.copy(),
    }


def validate_tree(document, directory):
    _probe, acquisition, _planner, fetch, _builder = helpers()
    if not isinstance(document, list) or len(document) > 1000:
        raise ValueError("Invalid bounded tree response")
    prefix, seen, files, directories = directory + "/", set(), [], []
    for entry in document:
        if not isinstance(entry, dict) or entry.get("type") not in ("directory", "file"):
            raise ValueError("Unexpected publication tree entry")
        path = fetch._repository_path(entry.get("path"))
        if not path.startswith(prefix) or "/" in path[len(prefix) :] or path in seen:
            raise ValueError("Tree escapes requested direct-child directory or duplicates a path")
        seen.add(path)
        if entry["type"] == "directory":
            directories.append(path)
        else:
            files.append(
                {"repository_path": path, **acquisition.metadata_record(entry, cap=2**63 - 1)}
            )
    return {
        "files": files,
        "directories": directories,
        "complete_without_pagination": len(document) < 1000,
    }


def read_document(client, declaration, out, remaining):
    _probe, acquisition, _planner, _fetch, _builder = helpers()
    if declaration["repository_path"] not in DOCUMENT_NAMES or declaration["bytes"] > remaining:
        raise ValueError("Documentation exceeds exact path/remaining byte authority")
    response, trail = client.stream(declaration["url"])
    try:
        size = declaration["bytes"]
        length = response.headers.get("Content-Length")
        if (
            length != str(size)
            or response.headers.get("Content-Encoding", "identity") != "identity"
        ):
            raise ValueError("Documentation length/encoding differs from publisher metadata")
        raw = bytearray()
        for block in response.iter_content(65536):
            raw.extend(block)
            if len(raw) > size:
                raise ValueError("Documentation body exceeds bounded size")
    finally:
        response.close()
    payload = bytes(raw)
    digest = hashlib.sha256() if declaration["digest_kind"] == "sha256-lfs" else hashlib.sha1()
    if declaration["digest_kind"] == "sha1-git-blob":
        digest.update(f"blob {size}\0".encode())
    digest.update(payload)
    if len(payload) != size or digest.hexdigest() != declaration["digest"]:
        raise ValueError("Documentation differs from publisher digest/size")
    record = acquisition.create_bytes(
        out / "documentation" / declaration["repository_path"], payload
    )
    return {**record, "url": declaration["url"], "response_chain": trail, "publisher": declaration}


def tree_metadata(client, url, remaining):
    """Preserve pagination as a boolean, never logging the Link URL itself."""
    _probe, _acquisition, planner, _fetch, _builder = helpers()
    response, trail = client.stream(url)
    try:
        length = response.headers.get("Content-Length")
        if (
            not isinstance(length, str)
            or not length.isdigit()
            or not 0 < int(length) <= min(BUDGET["max_tree_bytes"], remaining)
            or response.headers.get("Content-Encoding", "identity") != "identity"
        ):
            raise ValueError("API metadata Content-Length/encoding exceeds remaining cap")
        raw = bytearray()
        for block in response.iter_content(65536):
            raw.extend(block)
            if len(raw) > int(length):
                raise ValueError("API metadata exceeds declared Content-Length")
        if len(raw) != int(length):
            raise ValueError("API metadata differs from declared Content-Length")
        has_pagination = bool(response.headers.get("Link"))
    finally:
        response.close()
    payload = bytes(raw)
    return planner.strict_json(payload), payload, trail, has_pagination


def run_probe(path, expected_sha, *, client=None):
    probe, acquisition, planner, fetch, _builder = helpers()
    path = planner.local_path(path)
    admitted = planner.binding(path)
    if admitted["bytes"] > BUDGET["max_tree_bytes"] or admitted["sha256"] != expected_sha:
        raise ValueError("Explicit publication probe plan SHA/size differs")
    plan = planner.strict_json(path.read_bytes())
    if planner.canonical(plan) != planner.canonical(build_plan()):
        raise ValueError("Publication probe plan differs from exact current authority")
    declarations = planner.merge_source_records(plan["sources"] + [admitted])
    before = [declarations[key] for key in sorted(declarations)]
    for row in before:
        planner.verify_binding(row)
    out = acquisition.guarded_path(plan["output_directory"], fresh=True)
    if out != ROOT / OUTPUT:
        raise ValueError("Only the separate publication metadata output is writable")
    out.parent.mkdir(parents=True, exist_ok=True)
    acquisition.guarded_path(plan["output_directory"], fresh=True)
    out.mkdir(exist_ok=False)
    client_owned = client is None
    client = client or probe.bounded_client()
    trees, documents, files, errors, retained_metadata = [], [], [], [], []
    total_bytes, requests, native_exit = 0, 0, 0
    queue = ["traj"]
    try:
        while queue:
            directory = queue.pop(0)
            if requests >= BUDGET["max_tree_requests"]:
                raise ValueError("Publication tree request cap exhausted")
            requests += 1
            if client_owned:
                client.remaining_metadata_bytes = BUDGET["max_total_metadata_bytes"] - total_bytes
            url = f"https://huggingface.co/api/datasets/{fetch.DATASET}/tree/{fetch.REVISION}/{directory}?limit=1000"
            if client_owned:
                document, raw, trail, has_pagination = tree_metadata(
                    client, url, BUDGET["max_total_metadata_bytes"] - total_bytes
                )
            else:
                document, raw, trail = client.metadata(url)
                has_pagination = False
            if (
                not isinstance(raw, bytes)
                or not 0 < len(raw) <= BUDGET["max_tree_bytes"]
                or total_bytes + len(raw) > BUDGET["max_total_metadata_bytes"]
            ):
                raise ValueError("Publication tree exceeds remaining metadata cap")
            record = acquisition.create_bytes(out / f"metadata/tree-{requests}.json", raw)
            retained_metadata.append(record)
            total_bytes += len(raw)
            if planner.canonical(planner.strict_json(raw)) != planner.canonical(document):
                raise ValueError("Returned publication tree differs from retained original bytes")
            parsed = validate_tree(document, directory)
            parsed["pagination_link_present"] = has_pagination
            if has_pagination:
                parsed["complete_without_pagination"] = False
            trees.append(
                {"requested_path": directory, "tree": record, "response_chain": trail, **parsed}
            )
            files.extend(parsed["files"])
            if directory == "traj":
                queue.extend(parsed["directories"][: BUDGET["max_tree_requests"] - 1])
            print(
                json.dumps(
                    {
                        "event": "publication_tree_inspected",
                        "path": directory,
                        "files": len(parsed["files"]),
                        "directories": len(parsed["directories"]),
                        "complete_without_pagination": parsed["complete_without_pagination"],
                    }
                ),
                flush=True,
            )
        for declaration in plan["documentation"]:
            document = read_document(
                client, declaration, out, BUDGET["max_total_metadata_bytes"] - total_bytes
            )
            documents.append(document)
            total_bytes += document["bytes"]
        for tree in trees:
            planner.verify_binding(tree["tree"])
        for document in documents:
            planner.verify_binding({key: document[key] for key in ("path", "bytes", "sha256")})
    except Exception as exc:
        native_exit = 1
        errors.append(
            {
                "type": type(exc).__name__,
                "reason": str(exc)
                if isinstance(exc, ValueError)
                else "bounded publication metadata request failed",
            }
        )
    finally:
        if client_owned:
            client.session.close()
    after = []
    for row in before:
        try:
            after.append(planner.binding(row["path"]))
        except Exception as exc:
            errors.append(
                {"type": type(exc).__name__, "path": row["path"], "reason": "Input disappeared"}
            )
    unchanged = planner.canonical(before) == planner.canonical(after)
    if not unchanged:
        native_exit = 1
    shards = [row for row in files if row["repository_path"].endswith(".parquet")]
    minimum = min((row["bytes"] for row in shards), default=None)
    exhaustive = (
        bool(trees)
        and all(tree["complete_without_pagination"] for tree in trees)
        and all(not tree["directories"] for tree in trees[1:])
        and len(trees[0]["directories"]) <= BUDGET["max_tree_requests"] - 1
    )
    report = {
        "contract": REPORT_CONTRACT,
        "native_exit_code": native_exit,
        "plan": admitted,
        "dataset_repository": fetch.DATASET,
        "dataset_revision": fetch.REVISION,
        "tree_requests_performed": requests,
        "actual_metadata_bytes": total_bytes,
        "tree_depth_limit": 2,
        "trees": trees,
        "retained_metadata": retained_metadata,
        "documentation": documents,
        "errors": errors,
        "observed_parquet_shards": shards,
        "observed_parquet_shard_count": len(shards),
        "observed_parquet_bytes": sum(row["bytes"] for row in shards),
        "smallest_observed_shard_bytes": minimum,
        "smallest_observed_shards": [row for row in shards if row["bytes"] == minimum],
        "two_level_inventory_exhaustive": exhaustive and native_exit == 0,
        "rgb_parquet_schema_verified": False,
        "schema_source": None,
        "parquet_payload_downloaded": False,
        "viewer_rows_or_images_requested": False,
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
                raise ValueError("Emission does not fetch publication metadata")
            print(json.dumps(build_plan(), ensure_ascii=False, sort_keys=True, indent=2))
            return 0
        if args.plan is None or args.plan_sha256 is None:
            raise ValueError("Explicit publication probe plan and SHA required")
        report, record = run_probe(args.plan, args.plan_sha256)
        print(
            json.dumps(
                {
                    "event": "publication_metadata_probe_finished",
                    "native_exit_code": report["native_exit_code"],
                    "report": record,
                    "tree_requests_performed": report["tree_requests_performed"],
                    "metadata_bytes": report["actual_metadata_bytes"],
                    "observed_parquet_shards": report["observed_parquet_shard_count"],
                    "observed_parquet_bytes": report["observed_parquet_bytes"],
                    "smallest_observed_shard_bytes": report["smallest_observed_shard_bytes"],
                    "two_level_inventory_exhaustive": report["two_level_inventory_exhaustive"],
                    "rgb_parquet_schema_verified": False,
                    "parquet_payload_downloaded": False,
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
                    "event": "publication_metadata_admission_failed",
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
