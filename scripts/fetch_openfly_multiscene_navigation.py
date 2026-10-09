"""Acquire three explicitly admitted OpenFly routes, never models or training.

All input sources and the acquisition plan are bound before network access.
All three episode trees are checked before any payload download. Raw files,
partial failures, metadata and receipts are create-only in one new directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.openfly-multiscene-download-plan-v1"
PAYLOAD_CONTRACT = "multidetect.openfly-multiscene-payload-plan-v1"
RECEIPT_CONTRACT = "multidetect.openfly-multiscene-download-receipt-v1"
OUTPUT = "artifacts/training/openfly-multiscene-source-20261008-r1"
PREFLIGHT_PLAN = "configs/training/openfly-multiscene-acquisition-20261008-r2.json"
PREFLIGHT_REPORT = "artifacts/preflight-plan/openfly-multiscene-acquisition-20261008-r2.json"
SOURCE_FILES = (
    "scripts/fetch_openfly_multiscene_navigation.py",
    "tests/test_fetch_openfly_multiscene_navigation.py",
)
ROUTES = (
    (139, "env_airsim_26", "train", 23),
    (220, "env_airsim_16", "dev-validation", 17),
    (90, "env_airsim_23", "dev-test", 42),
)
BUDGET = {
    "max_total_download_bytes": 128 * 1024 * 1024,
    "max_payload_file_bytes": 8 * 1024 * 1024,
    "max_tree_bytes": 2 * 1024 * 1024,
    "max_scene_config_bytes": 64 * 1024,
    "exact_raw_images": 82,
    "exact_pose_files": 3,
    "exact_scene_configs": 3,
    "exact_episode_trees": 3,
}
GATES = dict.fromkeys(
    (
        "model_loaded",
        "gpu_used",
        "optimizer_performed",
        "samples_created",
        "training_ready",
        "action_class_coverage_claimed",
        "quality_claim",
        "candidate_promoted",
        "execution_authority",
        "physical_performance_claimed",
        "hil_real_evidence",
        "outdoor_gnss_evidence",
        "final_release_allowed",
        "weights_downloaded",
        "seen_unseen_downloaded",
    ),
    False,
)


def helpers():
    original = sys.path.copy()
    try:
        for path in (ROOT, ROOT / "src"):
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))
        from scripts import plan_openfly_multiscene_acquisition as planner
    finally:
        sys.path[:] = original
    fetch, builder = planner.helpers()
    return planner, fetch, builder


def guarded_path(relative, *, fresh=False):
    """Reject reparse/symlink ancestors, traversal and an existing final target."""
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("Only canonical repository-relative paths are admitted")
    if any(part in ("", ".", "..") for part in relative.split("/")):
        raise ValueError("Unsafe output path")
    path = ROOT / relative
    if Path(relative).is_absolute() or not path.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("Output escapes repository")
    cursor = ROOT
    for part in relative.split("/"):
        cursor = cursor / part
        if (
            cursor.is_symlink()
            or cursor.exists()
            and (getattr(cursor.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400)
        ):
            raise ValueError("Output contains a symlink/reparse point")
    if fresh and path.exists():
        raise FileExistsError("Create-only output already exists")
    return path


def create_bytes(path, raw):
    planner, _fetch, _builder = helpers()
    relative = path.relative_to(ROOT).as_posix()
    guarded_path(relative, fresh=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    guarded_path(relative, fresh=True)
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return planner.binding(path)


def json_bytes(value):
    planner, _fetch, _builder = helpers()
    return planner.canonical(value) + b"\n"


def build_plan():
    planner, fetch, _builder = helpers()
    preflight_path = planner.local_path(PREFLIGHT_PLAN)
    report_path = planner.local_path(PREFLIGHT_REPORT)
    if max(preflight_path.stat().st_size, report_path.stat().st_size) > BUDGET["max_tree_bytes"]:
        raise ValueError("Admission evidence exceeds cap")
    preflight = planner.strict_json(preflight_path.read_bytes())
    report = planner.strict_json(report_path.read_bytes())
    if (
        preflight.get("contract") != planner.CONTRACT
        or preflight.get("acquisition_root") != OUTPUT
        or preflight.get("dataset_repository") != fetch.DATASET
        or preflight.get("dataset_revision") != fetch.REVISION
        or preflight.get("code_revision") != fetch.CODE_REVISION
        or report.get("contract") != planner.REPORT_CONTRACT
        or type(report.get("native_exit_code")) is not int
        or report["native_exit_code"] != 0
        or report.get("sources_unchanged") is not True
        or planner.canonical(report["sources_before"]) != planner.canonical(report["sources_after"])
        or planner.canonical(report["plan"]) != planner.canonical(planner.binding(preflight_path))
        or preflight.get("historical_train_scene") != "env_airsim_18"
        or preflight.get("scene_assignment", {}).get("env_airsim_18") != "train"
    ):
        raise ValueError("Only the bound retained r2 preflight is admitted")
    manifest_path = planner.verify_binding(preflight["source_manifest"])
    manifest = planner.load_manifest(manifest_path)
    sources = planner.merge_source_records(
        planner.source_records(manifest)
        + report["sources_before"]
        + preflight["source_files"]
        + [planner.binding(preflight_path), planner.binding(report_path)]
        + [planner.binding(ROOT / name) for name in SOURCE_FILES]
    )
    declarations = [sources[key] for key in sorted(sources)]
    for row in declarations:
        planner.verify_binding(row)
    cards = [
        row
        for row in manifest["license_sources"]
        if row.get("repository") == fetch.DATASET and row.get("repository_path") == "README.md"
    ]
    if len(cards) != 1 or manifest["dataset"].get("license") != "apache-2.0":
        raise ValueError("Retained pinned dataset license is missing")
    if not re.fullmatch(
        rb"---\r?\nlicense: apache-2\.0\r?\n---\r?\n?",
        planner.local_path(cards[0]["path"]).read_bytes(),
    ):
        raise ValueError("Dataset license declaration changed")
    selected = [row for row in preflight["routes"] if row["historical_training_route"] is False]
    if len(selected) != len(ROUTES):
        raise ValueError("Unexpected additional acquisition routes")
    by_index = {row["annotation_index"]: row for row in selected}
    if len(by_index) != len(selected):
        raise ValueError("Repeated acquisition route")
    routes = []
    for index, scene, partition, frames in ROUTES:
        row = by_index.get(index)
        if (
            row is None
            or type(row["annotation_index"]) is not int
            or row["scene_id"] != scene
            or row["partition"] != partition
            or row["raw_root"] != OUTPUT + "/raw"
            or row["episode_tree"] is not None
            or row["scene_config"] is not None
        ):
            raise ValueError("Only the three original missing routes are authorized")
        routes.append({**row, "required_frame_count": frames})
    trees = [
        row for row in manifest["metadata_records"] if Path(row["path"]).name == "code-tree.json"
    ]
    if len(trees) != 1:
        raise ValueError("Retained official code tree is missing")
    code_tree = planner.binding(trees[0]["path"])
    return {
        "schema_version": 1,
        "contract": CONTRACT,
        "dataset_repository": fetch.DATASET,
        "dataset_revision": fetch.REVISION,
        "code_repository": fetch.CODE_REPOSITORY,
        "code_revision": fetch.CODE_REVISION,
        "preflight_plan": planner.binding(preflight_path),
        "preflight_report": planner.binding(report_path),
        "source_manifest": planner.binding(manifest_path),
        "code_tree": code_tree,
        "output_directory": OUTPUT,
        "routes": routes,
        "sources": declarations,
        "budget": BUDGET.copy(),
        "download_authorized": True,
        "optimizer_authorized": False,
        "official_seen_unseen_reserved_untouched": True,
        "source_action_semantics": "planned_not_executed",
        "claims": GATES.copy(),
    }


def admit_plan(path, expected_sha):
    planner, _fetch, _builder = helpers()
    path = planner.local_path(path)
    if (
        not re.fullmatch(r"[0-9a-f]{64}", expected_sha)
        or path.stat().st_size > BUDGET["max_tree_bytes"]
    ):
        raise ValueError("Invalid plan SHA or size")
    admitted = planner.binding(path)
    if admitted["sha256"] != expected_sha:
        raise ValueError("Acquisition plan SHA differs")
    plan = planner.strict_json(path.read_bytes())
    if planner.canonical(plan) != planner.canonical(build_plan()):
        raise ValueError("Acquisition plan does not exactly match current admitted sources")
    sources = planner.merge_source_records(plan["sources"] + [admitted])
    before = [sources[key] for key in sorted(sources)]
    for row in before:
        planner.verify_binding(row)
    return plan, before


def metadata_record(entry, *, cap):
    _planner, fetch, _builder = helpers()
    if not isinstance(entry, dict) or entry.get("type") not in ("file", "blob"):
        raise ValueError("Unexpected publisher entry type")
    adapted = (
        {"size": entry.get("size"), "oid": entry.get("sha")}
        if entry.get("type") == "blob"
        else entry
    )
    if adapted.get("lfs") is not None and (
        type(adapted["lfs"]) is not dict or type(adapted["lfs"].get("size")) is not int
    ):
        raise ValueError("Publisher LFS size must be an integer")
    size, digest, kind = fetch._metadata_entry(adapted)
    if size > cap:
        raise ValueError("Publisher payload exceeds per-file cap")
    return {"bytes": size, "digest": digest, "digest_kind": kind}


def payload_path(out, role, repository_path):
    _planner, fetch, _builder = helpers()
    fetch._repository_path(repository_path)
    prefix = "code" if role == "scene_config" else "raw"
    relative = f"{out.relative_to(ROOT).as_posix()}/{prefix}/{repository_path}"
    return guarded_path(relative, fresh=True)


def collect_payload_plan(client, plan, out, metadata_records):
    planner, fetch, _builder = helpers()
    manifest = planner.load_manifest(plan["source_manifest"]["path"])
    annotations, inspected = planner.selected_annotations(
        manifest["annotation"]["path"], {row["annotation_index"] for row in plan["routes"]}
    )
    code_tree_path = planner.verify_binding(plan["code_tree"], cap=BUDGET["max_tree_bytes"])
    code_tree = planner.strict_json(code_tree_path.read_bytes())
    if (
        not isinstance(code_tree, dict)
        or code_tree.get("sha") != fetch.CODE_REVISION
        or code_tree.get("truncated") is not False
        or not isinstance(code_tree.get("tree"), list)
    ):
        raise ValueError("Official pinned code tree is incomplete")
    routes, downloads = [], []
    for route in plan["routes"]:
        index = route["annotation_index"]
        annotation = annotations[index]
        ids = fetch.validate_annotation(annotation)
        if (
            annotation["image_path"] != route["image_path"]
            or hashlib.sha256(planner.canonical(annotation)).hexdigest()
            != route["annotation_record_sha256"]
            or ids[-1] + 1 != route["required_frame_count"]
        ):
            raise ValueError("Actual annotation differs from explicit route/frame authority")
        tree, raw, trail = client.tree("Image/" + route["image_path"])
        if not isinstance(raw, bytes) or not 0 < len(raw) <= BUDGET["max_tree_bytes"]:
            raise ValueError("Episode metadata exceeds cap")
        tree_binding = create_bytes(out / f"metadata/episode-{index}-tree.json", raw)
        metadata_records.append({**tree_binding, "response_chain": trail})
        if planner.canonical(planner.strict_json(raw)) != planner.canonical(tree):
            raise ValueError("Returned episode tree differs from retained raw JSON")
        pose, images = fetch.episode_metadata(tree, route["image_path"])
        if [i for i, _entry in images] != list(range(route["required_frame_count"])):
            raise ValueError("Complete publisher tree differs from terminal frame authority")
        for step, imageid in enumerate(ids):
            if (
                images[imageid][1]["path"].rsplit("/", 1)[1]
                != annotation["index_list"][step] + ".png"
            ):
                raise ValueError("Annotation filename differs from publisher imageid")
        for role, imageid, entry in [("pose", None, pose)] + [
            ("rgb", imageid, entry) for imageid, entry in images
        ]:
            declared = metadata_record(entry, cap=BUDGET["max_payload_file_bytes"])
            path = payload_path(out, role, entry["path"])
            downloads.append(
                {
                    "role": role,
                    "annotation_index": index,
                    "image_id": imageid,
                    "repository_path": entry["path"],
                    "output_path": path.relative_to(ROOT).as_posix(),
                    "url": fetch.dataset_url(entry["path"]),
                    **declared,
                }
            )
        config_name = f"configs/{route['scene_id']}.yaml"
        matches = [entry for entry in code_tree["tree"] if entry.get("path") == config_name]
        if len(matches) != 1:
            raise ValueError("Official pinned scene config missing or duplicated")
        declared = metadata_record(matches[0], cap=BUDGET["max_scene_config_bytes"])
        config_path = payload_path(out, "scene_config", config_name)
        downloads.append(
            {
                "role": "scene_config",
                "annotation_index": index,
                "image_id": None,
                "repository_path": config_name,
                "output_path": config_path.relative_to(ROOT).as_posix(),
                "url": f"https://raw.githubusercontent.com/{fetch.CODE_REPOSITORY}/{fetch.CODE_REVISION}/{config_name}",
                **declared,
            }
        )
        routes.append(
            {
                **route,
                "episode_tree": tree_binding,
                "scene_config_output": config_path.relative_to(ROOT).as_posix(),
            }
        )
    if len(downloads) != 88 or len({row["output_path"] for row in downloads}) != len(downloads):
        raise ValueError("Expected exactly 82 RGB, 3 pose and 3 config paths without duplicates")
    for role, count in (("rgb", 82), ("pose", 3), ("scene_config", 3)):
        if sum(row["role"] == role for row in downloads) != count:
            raise ValueError("Payload role count differs from exact authority")
    total = sum(row["bytes"] for row in downloads) + sum(row["bytes"] for row in metadata_records)
    if total > BUDGET["max_total_download_bytes"]:
        raise ValueError("Exact metadata plus payload exceeds total download cap")
    return {
        "contract": PAYLOAD_CONTRACT,
        "dataset_repository": fetch.DATASET,
        "dataset_revision": fetch.REVISION,
        "code_repository": fetch.CODE_REPOSITORY,
        "code_revision": fetch.CODE_REVISION,
        "annotation_records_inspected": inspected,
        "routes": routes,
        "downloads": downloads,
        "metadata": metadata_records,
        "total_download_bytes": total,
        "all_payload_metadata_verified_before_download": True,
        "claims": GATES.copy(),
    }, annotations


def verify_sources(before):
    planner, _fetch, _builder = helpers()
    after = [planner.binding(row["path"]) for row in before]
    if planner.canonical(after) != planner.canonical(before):
        raise ValueError("Source changed during acquisition")
    return after


def acquire(path, expected_sha, *, client=None):
    planner, fetch, _builder = helpers()
    plan, before = admit_plan(path, expected_sha)
    original_plan_binding = next(
        row
        for row in before
        if row["path"] == planner.local_path(path).relative_to(ROOT).as_posix()
    )
    out = guarded_path(plan["output_directory"], fresh=True)
    if out != ROOT / OUTPUT:
        raise ValueError("Only the explicitly authorized new acquisition directory is writable")
    out.parent.mkdir(parents=True, exist_ok=True)
    guarded_path(plan["output_directory"], fresh=True)
    out.mkdir(exist_ok=False)
    client_owned = client is None
    client = client or fetch.OfficialClient()
    metadata, verified, inspected_routes = [], [], []
    payload_plan_binding = None
    native_exit = 1
    status, failure = "metadata_failed", None
    sources_after, sources_unchanged = [], False
    try:
        payload_plan, annotations = collect_payload_plan(client, plan, out, metadata)
        verify_sources(before)
        payload_plan_binding = create_bytes(out / "payload-plan.json", json_bytes(payload_plan))
        payload_sources = [row for row in metadata] + [payload_plan_binding]
        status = "payload_download_failed"
        print(
            json.dumps(
                {
                    "event": "all_payload_metadata_admitted",
                    "payload_files": len(payload_plan["downloads"]),
                    "total_download_bytes": payload_plan["total_download_bytes"],
                    "payload_plan": payload_plan_binding,
                }
            ),
            flush=True,
        )
        for row in payload_plan["downloads"]:
            target = payload_path(out, row["role"], row["repository_path"])
            if target.relative_to(ROOT).as_posix() != row["output_path"]:
                raise ValueError("Payload output path differs from admitted path")
            record = fetch.download_file(
                client,
                row["url"],
                target,
                size=row["bytes"],
                expected=row["digest"],
                digest_kind=row["digest_kind"],
                cap=BUDGET["max_scene_config_bytes"]
                if row["role"] == "scene_config"
                else BUDGET["max_payload_file_bytes"],
            )
            verified.append(
                {
                    **row,
                    "actual": planner.binding(target),
                    "response_chain": record["response_chain"],
                    "status": "publisher_digest_and_size_verified",
                }
            )
        status = "post_download_validation_failed"
        for route in payload_plan["routes"]:
            complete_route = {
                key: value
                for key, value in route.items()
                if key not in ("required_frame_count", "scene_config_output")
            }
            complete_route["scene_config"] = planner.binding(route["scene_config_output"])
            result = planner.inspect_route(
                complete_route, annotations[route["annotation_index"]], planner.BUDGET
            )
            inspected_routes.append(result)
            if result["status"] != "local_raw_verified_not_training_approved":
                raise ValueError("Route raw validation or reviewed scene units did not pass")
        for row in verified:
            planner.verify_binding(row["actual"])
        for row in payload_sources:
            planner.verify_binding({key: row[key] for key in ("path", "bytes", "sha256")})
        sources_after = verify_sources(before)
        sources_unchanged = True
        manifest = {
            "contract": "multidetect.openfly-multiscene-raw-assets-v1",
            "acquisition_plan": original_plan_binding,
            "payload_plan": payload_plan_binding,
            "source_manifest": plan["source_manifest"],
            "dataset_repository": fetch.DATASET,
            "dataset_revision": fetch.REVISION,
            "code_repository": fetch.CODE_REPOSITORY,
            "code_revision": fetch.CODE_REVISION,
            "metadata": metadata,
            "payloads": verified,
            "routes": inspected_routes,
            "sources_before": before,
            "sources_after": sources_after,
            "sources_unchanged": True,
            "download_performed": True,
            "source_action_semantics": "planned_not_executed",
            "landmark_truth_model_input_allowed": False,
            **GATES,
        }
        create_bytes(out / "manifest.json", json_bytes(manifest))
        status, native_exit = "raw_assets_acquired_and_verified_not_training_approved", 0
    except Exception as exc:
        failure = {
            "type": type(exc).__name__,
            "reason": str(exc)
            if isinstance(exc, (ValueError, FileExistsError))
            else "network/acquisition failed; original partial state retained",
        }
    finally:
        if client_owned:
            client.session.close()
        try:
            sources_after = [planner.binding(row["path"]) for row in before]
            sources_unchanged = planner.canonical(sources_after) == planner.canonical(before)
        except Exception:
            sources_unchanged = False
        if not sources_unchanged:
            status, native_exit = "source_drift_failed", 1
        partials = [
            planner.binding(guarded_path(item.relative_to(ROOT).as_posix()))
            for item in sorted(out.rglob("*.partial"))
            if item.is_file()
        ]
        receipt = {
            "contract": RECEIPT_CONTRACT,
            "native_exit_code": native_exit,
            "status": status,
            "failure": failure,
            "acquisition_plan": original_plan_binding,
            "payload_plan": payload_plan_binding,
            "metadata": metadata,
            "verified_payloads": verified,
            "verified_rgb_count": sum(row["role"] == "rgb" for row in verified),
            "verified_pose_count": sum(row["role"] == "pose" for row in verified),
            "verified_scene_config_count": sum(row["role"] == "scene_config" for row in verified),
            "actual_downloaded_payload_bytes": sum(row["actual"]["bytes"] for row in verified),
            "partial_files": partials,
            "source_count": len(before),
            "sources_before": before,
            "sources_after": sources_after,
            "sources_unchanged": sources_unchanged,
            "download_performed": bool(metadata or verified or partials),
            "source_action_semantics": "planned_not_executed",
            "landmark_truth_model_input_allowed": False,
            **GATES,
        }
        receipt_binding = create_bytes(out / "receipt.json", json_bytes(receipt))
    return receipt, receipt_binding


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit-plan", action="store_true")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--plan-sha256")
    args = parser.parse_args(argv)
    try:
        if args.emit_plan:
            if args.plan is not None or args.plan_sha256 is not None:
                raise ValueError("Plan emission cannot acquire assets")
            print(json.dumps(build_plan(), ensure_ascii=False, sort_keys=True, indent=2))
            return 0
        if args.plan is None or args.plan_sha256 is None:
            raise ValueError("An explicit bound plan is required")
        receipt, record = acquire(args.plan, args.plan_sha256)
        print(
            json.dumps(
                {
                    "event": "openfly_multiscene_acquisition_finished",
                    "native_exit_code": receipt["native_exit_code"],
                    "status": receipt["status"],
                    "receipt": record,
                    "verified_rgb_count": receipt["verified_rgb_count"],
                    "verified_pose_count": receipt["verified_pose_count"],
                    "verified_scene_config_count": receipt["verified_scene_config_count"],
                    **GATES,
                }
            ),
            flush=True,
        )
        return receipt["native_exit_code"]
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "openfly_multiscene_admission_failed",
                    "error_type": type(exc).__name__,
                    "reason": str(exc)
                    if isinstance(exc, (ValueError, FileExistsError))
                    else "admission failed; no model or training started",
                    **GATES,
                }
            ),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
