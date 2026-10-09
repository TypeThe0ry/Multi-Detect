"""Bounded publisher metadata probe, not RGB acquisition or model evaluation.

First diagnose the three originally selected routes. A separate explicit plan
may inspect seven more distinct original-annotation routes per scene. Only
pinned episode trees and one pinned official README are fetched; no payloads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.openfly-multiscene-metadata-probe-plan-v1"
REPORT_CONTRACT = "multidetect.openfly-multiscene-metadata-probe-v1"
INITIAL_PLAN = "configs/training/openfly-multiscene-download-20261008-r1.json"
FAILED_ATTEMPT = "artifacts/training/openfly-multiscene-source-20261008-r1/receipt.json"
ORIGINAL_OUTPUT = "artifacts/diagnostics/openfly-multiscene-original-metadata-20261008-r1"
AVAILABILITY_OUTPUT = "artifacts/diagnostics/openfly-multiscene-availability-20261008-r1"
SOURCE_FILES = (
    "scripts/probe_openfly_multiscene_availability.py",
    "tests/test_probe_openfly_multiscene_availability.py",
)
BUDGET = {
    "max_routes_per_scene": 8,
    "max_tree_requests": 24,
    "max_total_metadata_bytes": 8 * 1024 * 1024,
    "max_tree_bytes": 2 * 1024 * 1024,
    "max_document_bytes": 64 * 1024,
}


def helpers():
    original = sys.path.copy()
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from scripts import fetch_openfly_multiscene_navigation as acquisition
    finally:
        sys.path[:] = original
    return acquisition, *acquisition.helpers()


def select_candidates(initial, manifest, mode):
    _acquisition, planner, fetch, _builder = helpers()
    wanted = {row["scene_id"]: row for row in initial["routes"]}
    selected = {scene: [] for scene in wanted}
    paths = {scene: {row["image_path"]} for scene, row in wanted.items()}
    originals, inspected = {}, 0
    for index, annotation in fetch.iter_annotation_records(
        planner.local_path(manifest["annotation"]["path"])
    ):
        inspected += 1
        scene = str(annotation.get("image_path", "")).split("/")[0]
        if scene not in wanted:
            continue
        if index == wanted[scene]["annotation_index"]:
            originals[scene] = (index, annotation)
        elif mode == "availability" and len(selected[scene]) < BUDGET["max_routes_per_scene"] - 1:
            if annotation["image_path"] not in paths[scene]:
                selected[scene].append((index, annotation))
                paths[scene].add(annotation["image_path"])
    candidates = []
    for scene, original in wanted.items():
        if scene not in originals:
            raise ValueError("Originally selected annotation is absent")
        for index, annotation in [originals[scene], *selected[scene]]:
            ids = fetch.validate_annotation(annotation)
            if ids[-1] >= planner.BUDGET["max_raw_images"]:
                raise ValueError("Candidate original terminal frame exceeds probe frame cap")
            if index == original["annotation_index"] and (
                annotation["image_path"] != original["image_path"]
                or hashlib.sha256(planner.canonical(annotation)).hexdigest()
                != original["annotation_record_sha256"]
            ):
                raise ValueError("Original candidate identity differs")
            candidates.append(
                {
                    "annotation_index": index,
                    "scene_id": scene,
                    "image_path": annotation["image_path"],
                    "originally_selected_route": index == original["annotation_index"],
                    "annotation_record_sha256": hashlib.sha256(
                        planner.canonical(annotation)
                    ).hexdigest(),
                    "annotation_terminal_frame_count": ids[-1] + 1,
                    "annotation_image_ids": ids,
                    "annotation_filenames": annotation["index_list"],
                    "cached_tree": None,
                }
            )
    return candidates, inspected


def build_plan(mode):
    acquisition, planner, fetch, _builder = helpers()
    if mode not in ("original", "availability"):
        raise ValueError("Unknown metadata-only probe mode")
    initial_path = planner.local_path(INITIAL_PLAN)
    initial, initial_sources = acquisition.admit_plan(
        initial_path, planner.binding(initial_path)["sha256"]
    )
    attempt_path = planner.local_path(FAILED_ATTEMPT)
    if attempt_path.stat().st_size > BUDGET["max_tree_bytes"]:
        raise ValueError("Attempt receipt exceeds cap")
    attempt = planner.strict_json(attempt_path.read_bytes())
    if (
        attempt.get("contract") != acquisition.RECEIPT_CONTRACT
        or type(attempt.get("native_exit_code")) is not int
        or attempt["native_exit_code"] != 1
        or attempt.get("status") != "metadata_failed"
        or attempt.get("verified_rgb_count") != 0
        or attempt.get("verified_pose_count") != 0
        or attempt.get("verified_scene_config_count") != 0
        or attempt.get("sources_unchanged") is not True
    ):
        raise ValueError("Only the retained failed metadata attempt is admitted")
    manifest = planner.load_manifest(initial["source_manifest"]["path"])
    candidates, inspected = select_candidates(initial, manifest, mode)
    extra = [planner.binding(attempt_path)] + [
        planner.binding(ROOT / name) for name in SOURCE_FILES
    ]
    cached = {}
    for row in attempt["metadata"]:
        record = {key: row[key] for key in ("path", "bytes", "sha256")}
        planner.verify_binding(record, cap=BUDGET["max_tree_bytes"])
        extra.append(record)
        cached[Path(record["path"]).name] = record
    if mode == "availability":
        original_report_path = planner.local_path(ORIGINAL_OUTPUT + "/report.json")
        original_report = planner.strict_json(original_report_path.read_bytes())
        if (
            original_report.get("contract") != REPORT_CONTRACT
            or original_report.get("mode") != "original"
            or type(original_report.get("native_exit_code")) is not int
            or original_report["native_exit_code"] != 0
            or original_report.get("sources_unchanged") is not True
            or len(original_report.get("routes", [])) != 3
        ):
            raise ValueError("Original-route diagnostic must complete first")
        extra.append(planner.binding(original_report_path))
        for row in original_report["routes"]:
            record = row["tree"]
            planner.verify_binding(record, cap=BUDGET["max_tree_bytes"])
            extra.append(record)
            cached[Path(record["path"]).name] = record
    for candidate in candidates:
        candidate["cached_tree"] = cached.get(f"episode-{candidate['annotation_index']}-tree.json")
    if mode == "availability" and any(
        row["originally_selected_route"] and row["cached_tree"] is None for row in candidates
    ):
        raise ValueError("All original route trees must be retained before expansion")
    code_tree = planner.strict_json(planner.verify_binding(initial["code_tree"]).read_bytes())
    if code_tree.get("sha") != fetch.CODE_REVISION or code_tree.get("truncated") is not False:
        raise ValueError("Official code tree is incomplete")
    documents = [row for row in code_tree["tree"] if row.get("path") == "README.md"]
    if len(documents) != 1:
        raise ValueError("Official pinned README entry is missing")
    declared = acquisition.metadata_record(documents[0], cap=BUDGET["max_document_bytes"])
    sources = planner.merge_source_records(initial_sources + extra)
    for row in sources.values():
        planner.verify_binding(row)
    return {
        "contract": CONTRACT,
        "mode": mode,
        "dataset_repository": fetch.DATASET,
        "dataset_revision": fetch.REVISION,
        "code_repository": fetch.CODE_REPOSITORY,
        "code_revision": fetch.CODE_REVISION,
        "initial_acquisition_plan": planner.binding(initial_path),
        "failed_attempt": planner.binding(attempt_path),
        "output_directory": ORIGINAL_OUTPUT if mode == "original" else AVAILABILITY_OUTPUT,
        "candidates": candidates,
        "annotation_records_inspected": inspected,
        "selection_method": (
            "original_routes_then_first_seven_distinct_paths_per_scene_in_train_file_order"
        ),
        "selection_not_conditioned_on": ["model_results", "action_classes", "prompt_targets"],
        "official_code_readme": {
            "url": f"https://raw.githubusercontent.com/{fetch.CODE_REPOSITORY}/{fetch.CODE_REVISION}/README.md",
            **declared,
        },
        "budget": BUDGET.copy(),
        "sources": [sources[key] for key in sorted(sources)],
        "payload_download_authorized": False,
        "optimizer_authorized": False,
        "claims": acquisition.GATES.copy(),
    }


def classify_tree(tree, candidate):
    acquisition, _planner, fetch, _builder = helpers()
    prefix = "Image/" + fetch._repository_path(candidate["image_path"]) + "/"
    files, seen = [], set()
    for entry in tree:
        if not isinstance(entry, dict) or entry.get("type") != "file":
            raise ValueError("Unexpected episode metadata entry")
        path = fetch._repository_path(entry.get("path"))
        if not path.startswith(prefix) or "/" in path[len(prefix) :] or path in seen:
            raise ValueError("Tree escapes candidate or duplicates a file")
        seen.add(path)
        files.append({"repository_path": path, **acquisition.metadata_record(entry, cap=2**63 - 1)})
    pose = [row for row in files if row["repository_path"].endswith("/pose.jsonl")]
    png = [row for row in files if row["repository_path"].endswith(".png")]
    result = {
        "publisher_rgb_file_count": len(png),
        "publisher_pose_file_count": len(pose),
        "publisher_payload_bytes": sum(row["bytes"] for row in files),
        "publisher_files": files,
        "complete_rgb_pose_tree": False,
        "annotation_frame_alignment": False,
        "raw_pose_alignment_verified": False,
        "scene_units_verified": False,
        "status": "incomplete_rgb_pose_tree",
    }
    if len(files) == len(pose) == 1:
        result["status"] = "pose_only"
        return result
    try:
        _pose, images = fetch.episode_metadata(tree, candidate["image_path"])
    except fetch.PilotError as exc:
        result["metadata_alignment_error"] = str(exc)
        return result
    aligned = len(images) == candidate["annotation_terminal_frame_count"] and all(
        imageid < len(images) and images[imageid][1]["path"].rsplit("/", 1)[1] == filename + ".png"
        for imageid, filename in zip(
            candidate["annotation_image_ids"], candidate["annotation_filenames"], strict=True
        )
    )
    result.update(
        complete_rgb_pose_tree=True,
        annotation_frame_alignment=aligned,
        status="complete_rgb_pose_tree_not_downloaded"
        if aligned
        else "complete_tree_annotation_mismatch",
    )
    return result


def read_document(client, plan, out):
    acquisition, planner, _fetch, _builder = helpers()
    declaration = plan["official_code_readme"]
    response, trail = client.stream(declaration["url"])
    try:
        raw = bytearray()
        for block in response.iter_content(65536):
            raw.extend(block)
            if len(raw) > declaration["bytes"] or len(raw) > BUDGET["max_document_bytes"]:
                raise ValueError("Official README exceeds publisher size/cap")
    finally:
        response.close()
    payload = bytes(raw)
    actual_digest = hashlib.sha1(f"blob {len(payload)}\0".encode() + payload).hexdigest()
    if len(payload) != declaration["bytes"] or actual_digest != declaration["digest"]:
        raise ValueError("Official README differs from pinned publisher metadata")
    record = acquisition.create_bytes(out / "documentation/OpenFly-Platform-README.md", payload)
    return {**record, "url": declaration["url"], "response_chain": trail}


def bounded_client():
    """Require declared API length before reading, with a shared metadata cap."""
    _acquisition, planner, fetch, _builder = helpers()

    class MetadataClient(fetch.OfficialClient):
        def __init__(self):
            super().__init__()
            self.remaining_metadata_bytes = BUDGET["max_total_metadata_bytes"]

        def metadata(self, url):
            response, trail = self.stream(url)
            try:
                length = response.headers.get("Content-Length")
                if (
                    not isinstance(length, str)
                    or not length.isdigit()
                    or not 0
                    < int(length)
                    <= min(BUDGET["max_tree_bytes"], self.remaining_metadata_bytes)
                    or response.headers.get("Content-Encoding", "identity") != "identity"
                ):
                    raise ValueError("Metadata length/encoding exceeds remaining budget")
                pieces, count = [], 0
                for block in response.iter_content(65536):
                    count += len(block)
                    if count > int(length):
                        raise ValueError("Metadata body exceeds publisher Content-Length")
                    pieces.append(block)
                if count != int(length):
                    raise ValueError("Metadata body differs from Content-Length")
                raw = b"".join(pieces)
                return planner.strict_json(raw), raw, trail
            finally:
                response.close()

    return MetadataClient()


def run_probe(path, expected_sha, *, client=None):
    acquisition, planner, fetch, _builder = helpers()
    path = planner.local_path(path)
    admitted = planner.binding(path)
    if admitted["bytes"] > BUDGET["max_tree_bytes"] or admitted["sha256"] != expected_sha:
        raise ValueError("Explicit metadata probe plan SHA/size differs")
    plan = planner.strict_json(path.read_bytes())
    if planner.canonical(plan) != planner.canonical(build_plan(plan.get("mode"))):
        raise ValueError("Probe plan differs from actual admitted sources/candidate authority")
    sources = planner.merge_source_records(plan["sources"] + [admitted])
    before = [sources[key] for key in sorted(sources)]
    for row in before:
        planner.verify_binding(row)
    out = acquisition.guarded_path(plan["output_directory"], fresh=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    acquisition.guarded_path(plan["output_directory"], fresh=True)
    out.mkdir(exist_ok=False)
    client_owned = client is None
    client = client or bounded_client()
    results, requests, metadata_bytes, document, failure = [], 0, 0, None, None
    native_exit = 0
    try:
        if plan["mode"] == "availability":
            document = read_document(client, plan, out)
            metadata_bytes += document["bytes"]
        for candidate in plan["candidates"]:
            result = {**candidate, "tree": None, "status": "unknown"}
            try:
                cache = candidate["cached_tree"]
                if cache is not None:
                    raw = planner.verify_binding(cache, cap=BUDGET["max_tree_bytes"]).read_bytes()
                    tree = planner.strict_json(raw)
                    record = cache
                    trail = []
                    acquisition_method = "retained_bound_tree_no_request"
                else:
                    requests += 1
                    if requests > BUDGET["max_tree_requests"]:
                        raise ValueError("Tree request budget exhausted")
                    if client_owned:
                        client.remaining_metadata_bytes = (
                            BUDGET["max_total_metadata_bytes"] - metadata_bytes
                        )
                    tree, raw, trail = client.tree("Image/" + candidate["image_path"])
                    if not isinstance(raw, bytes) or not 0 < len(raw) <= BUDGET["max_tree_bytes"]:
                        raise ValueError("One metadata tree exceeds cap")
                    if metadata_bytes + len(raw) > BUDGET["max_total_metadata_bytes"]:
                        raise ValueError("Total retained metadata exceeds 8 MiB cap")
                    record = acquisition.create_bytes(
                        out / f"metadata/episode-{candidate['annotation_index']}-tree.json", raw
                    )
                    acquisition_method = "pinned_publisher_tree_request"
                metadata_bytes += len(raw)
                if metadata_bytes > BUDGET["max_total_metadata_bytes"]:
                    raise ValueError("Total retained metadata exceeds 8 MiB cap")
                if planner.canonical(planner.strict_json(raw)) != planner.canonical(tree):
                    raise ValueError("Tree response differs from retained original JSON")
                result.update(
                    tree=record,
                    response_chain=trail,
                    acquisition_method=acquisition_method,
                )
                result.update(classify_tree(tree, candidate))
            except Exception as exc:
                native_exit = 1
                result["error_type"] = type(exc).__name__
                result["error"] = (
                    str(exc)
                    if isinstance(exc, ValueError)
                    else "metadata retrieval failed; status unknown"
                )
            results.append(result)
            print(
                json.dumps(
                    {
                        "event": "metadata_route_diagnosed",
                        "scene": candidate["scene_id"],
                        "annotation_index": candidate["annotation_index"],
                        "status": result["status"],
                    }
                ),
                flush=True,
            )
            if metadata_bytes >= BUDGET["max_total_metadata_bytes"]:
                native_exit = 1
                failure = {"type": "ValueError", "reason": "Metadata budget exhausted"}
                break
        for row in results:
            if row["tree"] is not None:
                planner.verify_binding(row["tree"], cap=BUDGET["max_tree_bytes"])
        if document is not None:
            planner.verify_binding({key: document[key] for key in ("path", "bytes", "sha256")})
    except Exception as exc:
        native_exit = 1
        failure = {
            "type": type(exc).__name__,
            "reason": str(exc) if isinstance(exc, ValueError) else "metadata-only probe failed",
        }
    finally:
        if client_owned:
            client.session.close()
    after, source_errors = [], []
    for row in before:
        try:
            after.append(planner.binding(row["path"]))
        except Exception as exc:
            source_errors.append({"path": row["path"], "error_type": type(exc).__name__})
    unchanged = planner.canonical(before) == planner.canonical(after)
    if not unchanged:
        native_exit = 1
    report = {
        "contract": REPORT_CONTRACT,
        "mode": plan["mode"],
        "plan": admitted,
        "native_exit_code": native_exit,
        "failure": failure,
        "metadata_probe_completed": native_exit == 0 and len(results) == len(plan["candidates"]),
        "tree_requests_performed": requests,
        "actual_retained_metadata_bytes": metadata_bytes,
        "official_code_readme": document,
        "routes": results,
        "sources_before": before,
        "sources_after": after,
        "sources_unchanged": unchanged,
        "source_errors": source_errors,
        "complete_metadata_routes": [
            row["annotation_index"]
            for row in results
            if row.get("complete_rgb_pose_tree") is True
            and row.get("annotation_frame_alignment") is True
        ],
        "pose_only_routes": [
            row["annotation_index"] for row in results if row["status"] == "pose_only"
        ],
        "unknown_routes": [
            row["annotation_index"] for row in results if row["status"] == "unknown"
        ],
        "independent_holdout_available": False,
        "payload_download_performed": False,
        **acquisition.GATES,
    }
    record = acquisition.create_bytes(out / "report.json", acquisition.json_bytes(report))
    return report, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit-plan", choices=("original", "availability"))
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--plan-sha256")
    args = parser.parse_args(argv)
    try:
        if args.emit_plan is not None:
            if args.plan is not None or args.plan_sha256 is not None:
                raise ValueError("Emission never runs a metadata probe")
            print(
                json.dumps(build_plan(args.emit_plan), ensure_ascii=False, sort_keys=True, indent=2)
            )
            return 0
        if args.plan is None or args.plan_sha256 is None:
            raise ValueError("Explicit metadata-only plan and SHA are required")
        report, record = run_probe(args.plan, args.plan_sha256)
        print(
            json.dumps(
                {
                    "event": "metadata_probe_finished",
                    "native_exit_code": report["native_exit_code"],
                    "report": record,
                    "tree_requests_performed": report["tree_requests_performed"],
                    "complete_metadata_routes": report["complete_metadata_routes"],
                    "pose_only_routes": report["pose_only_routes"],
                    "unknown_routes": report["unknown_routes"],
                    "payload_download_performed": False,
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
                    "event": "metadata_probe_admission_failed",
                    "native_exit_code": 1,
                    "error_type": type(exc).__name__,
                    "reason": str(exc)
                    if isinstance(exc, ValueError)
                    else "metadata-only probe admission failed",
                    "training_ready": False,
                }
            ),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
