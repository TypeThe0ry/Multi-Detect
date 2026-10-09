"""Plan local OpenFly scene acquisition and an anchored development split.

This entry never downloads, loads a model, trains, or creates samples. Missing
raw assets remain missing. A completed preflight is not a dataset approval.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.openfly-multiscene-acquisition-plan-v1"
REPORT_CONTRACT = "multidetect.openfly-multiscene-acquisition-preflight-v1"
HISTORICAL_SCENE = "env_airsim_18"
HISTORICAL_INDICES = (0, 7, 8)
SOURCE_FILES = (
    "scripts/plan_openfly_multiscene_acquisition.py",
    "tests/test_plan_openfly_multiscene_acquisition.py",
    "scripts/fetch_openfly_navigation_pilot.py",
    "scripts/build_onboard_openfly_navigation_dataset.py",
    "src/multidetect/autonomy/onboard_navigation_supervision.py",
    "src/multidetect/autonomy/serialization.py",
)
BUDGET = {
    "max_routes": 12,
    "max_raw_images": 1024,
    "max_file_bytes": 8 * 1024 * 1024,
    "max_total_payload_bytes": 256 * 1024 * 1024,
    "max_tree_bytes": 2 * 1024 * 1024,
    "max_scene_config_bytes": 64 * 1024,
}
CLAIMS = dict.fromkeys(
    (
        "download_performed",
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
    ),
    False,
)


def strict_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("Duplicate JSON key")
            value[key] = item
        return value

    def nonfinite(_):
        raise ValueError("Nonfinite JSON constant")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def local_path(path, *, exists=True):
    value = Path(path)
    resolved = (value if value.is_absolute() else ROOT / value).resolve(strict=exists)
    if not resolved.is_relative_to(ROOT.resolve(strict=True)):
        raise ValueError("Input/output must stay inside the repository")
    return resolved


def binding(path):
    path = local_path(path)
    if not path.is_file():
        raise ValueError("Binding must name an actual file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {
        "path": path.relative_to(ROOT).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def verify_binding(row, *, cap=None):
    if (
        type(row) is not dict
        or set(row) != {"path", "bytes", "sha256"}
        or type(row["bytes"]) is not int
        or row["bytes"] <= 0
        or type(row["sha256"]) is not str
        or not re.fullmatch(r"[a-f0-9]{64}", row["sha256"])
        or cap is not None
        and row["bytes"] > cap
    ):
        raise ValueError("Invalid bounded file binding")
    actual = binding(row["path"])
    if canonical(actual) != canonical(row):
        raise ValueError("Bound source bytes changed")
    return local_path(row["path"])


def helpers():
    original = sys.path.copy()
    try:
        for path in (ROOT, ROOT / "src"):
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))
        from scripts import build_onboard_openfly_navigation_dataset as builder
        from scripts import fetch_openfly_navigation_pilot as fetch
    finally:
        sys.path[:] = original

    return fetch, builder


def load_manifest(path):
    fetch, _builder = helpers()
    path = local_path(path)
    if path.stat().st_size > BUDGET["max_tree_bytes"]:
        raise ValueError("Source manifest exceeds budget")
    value = strict_json(path.read_bytes())
    if (
        value.get("contract") != "multidetect.openfly-navigation-pilot-v1"
        or value["dataset"]["repository"] != fetch.DATASET
        or value["dataset"]["revision"] != fetch.REVISION
        or value["dataset"]["split"] != "train"
        or value["code"]["revision"] != fetch.CODE_REVISION
        or value["seen_unseen_downloaded"] is not False
        or value["source_planned_not_executed"] is not True
        or (value["annotation"]["bytes"], value["annotation"]["sha256"])
        != (fetch.ANNOTATION_BYTES, fetch.ANNOTATION_SHA256)
    ):
        raise ValueError("Only the retained pinned train source is admitted")
    return value


def source_records(manifest):
    declarations = [manifest["annotation"]]
    for name in ("code_sources", "license_sources", "metadata_records", "source_files"):
        declarations.extend(manifest[name])
    for episode in manifest["episodes"]:
        declarations.extend([episode["annotation_record"], episode["pose"], *episode["images"]])
    result = {}
    for row in declarations:
        path = local_path(row["path"])
        value = {
            "path": path.relative_to(ROOT).as_posix(),
            "bytes": row["bytes"],
            "sha256": row["sha256"],
        }
        key = value["path"]
        if key in result and canonical(result[key]) != canonical(value):
            raise ValueError("Conflicting source binding")
        result[key] = value
    return [result[key] for key in sorted(result)]


def merge_source_records(declarations):
    """Do not let an actual/new binding erase a retained source declaration."""
    result, identities = {}, {}
    for row in declarations:
        path = local_path(row["path"]).relative_to(ROOT).as_posix()
        normalized = {"path": path, "bytes": row["bytes"], "sha256": row["sha256"]}
        identity = path.casefold() if os.name == "nt" else path
        if identity in identities:
            original = result[identities[identity]]
            if canonical({**normalized, "path": original["path"]}) != canonical(original):
                raise ValueError("Conflicting canonical source declaration")
        else:
            identities[identity] = path
            result[path] = normalized
    return result


def parse_scene_route(value):
    match = re.fullmatch(r"(env_airsim_[A-Za-z0-9_]+):([0-9]+)", value)
    if match is None:
        raise ValueError("Scene route must be env_airsim_NAME:original_index")
    return match[1], int(match[2])


def selected_annotations(annotation, indices):
    fetch, _ = helpers()
    found = {}
    total = 0
    for index, row in fetch.iter_annotation_records(local_path(annotation)):
        total += 1
        if index in indices:
            fetch.validate_annotation(row)
            found[index] = row
    if set(found) != set(indices):
        raise ValueError("Requested original annotation index is absent")
    return found, total


def build_plan(source_manifest, extra_train, dev_validation, dev_test, acquisition_root):
    fetch, _ = helpers()
    manifest = load_manifest(source_manifest)
    historical = manifest["episodes"]
    if sorted(ep["annotation_index"] for ep in historical) != list(HISTORICAL_INDICES) or any(
        ep["scene"] != HISTORICAL_SCENE for ep in historical
    ):
        raise ValueError("Historical trained scene must be explicitly anchored")
    requested = [(ep["scene"], ep["annotation_index"], "train") for ep in historical]
    requested += [(scene, index, "train") for scene, index in extra_train]
    requested += [(scene, index, "dev-validation") for scene, index in dev_validation]
    requested += [(scene, index, "dev-test") for scene, index in dev_test]
    if not 1 <= len(requested) <= BUDGET["max_routes"]:
        raise ValueError("Complete route count exceeds budget")
    assignment = {HISTORICAL_SCENE: "train"}
    for scene, _index, partition in requested:
        if scene in assignment and assignment[scene] != partition:
            raise ValueError("A scene cannot cross partitions or move historical data to holdout")
        assignment[scene] = partition
    if (
        sum(v == "train" for v in assignment.values()) < 2
        or "dev-validation" not in assignment.values()
        or "dev-test" not in assignment.values()
    ):
        raise ValueError("Require two train scenes and distinct validation/test scenes")
    indices = [index for _scene, index, _part in requested]
    if len(set(indices)) != len(indices):
        raise ValueError("Original route index repeated")
    rows, total = selected_annotations(manifest["annotation"]["path"], indices)
    source_dir = local_path(source_manifest).parent
    destination = local_path(acquisition_root, exists=False)
    if (
        destination.parent != ROOT / "artifacts/training"
        or not destination.name.startswith("openfly-multiscene-source-")
        or destination == source_dir
        or destination.is_relative_to(source_dir)
    ):
        raise ValueError("New acquisition must not write inside the sealed source directory")
    historical_indices = {ep["annotation_index"] for ep in historical}
    routes = []
    for scene, index, partition in requested:
        row = rows[index]
        if row["image_path"].split("/")[0] != scene:
            raise ValueError("Explicit scene differs from original annotation")
        old = index in historical_indices
        parent = source_dir if old else destination
        tree = parent / "metadata" / f"episode-{index}-tree.json"
        config = parent / "code/configs" / (scene + ".yaml")
        routes.append(
            {
                "annotation_index": index,
                "scene_id": scene,
                "partition": partition,
                "historical_training_route": old,
                "image_path": row["image_path"],
                "annotation_record_sha256": hashlib.sha256(canonical(row)).hexdigest(),
                "raw_root": (parent / "raw").relative_to(ROOT).as_posix(),
                "episode_tree": binding(tree) if tree.is_file() else None,
                "scene_config": binding(config) if config.is_file() else None,
            }
        )
    return {
        "schema_version": 1,
        "contract": CONTRACT,
        "dataset_repository": fetch.DATASET,
        "dataset_revision": fetch.REVISION,
        "code_revision": fetch.CODE_REVISION,
        "source_manifest": binding(source_manifest),
        "historical_train_scene": HISTORICAL_SCENE,
        "scene_assignment": assignment,
        "acquisition_root": destination.relative_to(ROOT).as_posix(),
        "annotation_records_inspected": total,
        "routes": routes,
        "budget": BUDGET.copy(),
        "source_files": [binding(ROOT / name) for name in SOURCE_FILES],
        "claims": CLAIMS.copy(),
        "split_policy": "explicit_scenes_historical_and_visual_train_anchor_no_hash_reassignment",
        "official_seen_unseen_reserved_untouched": True,
        "download_authorized": False,
        "optimizer_authorized": False,
    }


def anchored_visual_split(episodes):
    """Reuse duplicate grouping, not the old hash-sorted partition assignment."""
    _, builder = helpers()
    _old_assignment, groups = builder.assign_partitions(episodes)
    by_id = {ep["episode_id"]: ep for ep in episodes}
    assignment, overlaps = {}, []
    for group in groups["groups"]:
        parts = {by_id[key]["declared_partition"] for key in group}
        anchor = any(by_id[key]["scene_id"] == HISTORICAL_SCENE for key in group)
        if anchor or "train" in parts:
            effective = "train"
        elif len(parts) == 1:
            effective = next(iter(parts))
        else:
            effective = "quarantine-visual-overlap"
        for key in group:
            assignment[key] = effective
            if effective != by_id[key]["declared_partition"]:
                overlaps.append(key)
    return {"assignment": assignment, "overlap_routes": overlaps, "grouping": groups}


def verify_payload(path, entry, cap):
    fetch, _ = helpers()
    size, expected, kind = fetch._metadata_entry(entry)
    path = local_path(path)
    if size > cap or path.stat().st_size != size:
        raise ValueError("Payload size differs from publisher metadata/budget")
    digest = hashlib.sha256() if kind == "sha256-lfs" else hashlib.sha1()
    if kind == "sha1-git-blob":
        digest.update(f"blob {size}\0".encode())
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise ValueError("Actual payload differs from publisher digest")
    return binding(path)


def inspect_route(route, annotation, budget):
    fetch, builder = helpers()
    ids = fetch.validate_annotation(annotation)
    if ids[-1] >= budget["max_raw_images"]:
        raise ValueError("One complete raw episode exceeds the image budget")
    required_ids = list(range(ids[-1] + 1))
    prefix = "Image/" + route["image_path"] + "/"
    sparse = dict(zip(ids, annotation["index_list"], strict=True))
    result = {
        **route,
        "status": "not_acquired",
        "required_raw_image_ids": required_ids,
        "expected_pose_repository_path": prefix + "pose.jsonl",
        "missing": [],
        "publisher_payloads": [],
        "actual_payloads": [],
        "primitive_alignment_verified": False,
        "eligible_step_count": 0,
        "visual_identities": [],
        "complete_frame_filenames_known": False,
    }
    if route["episode_tree"] is None:
        result["missing"].append("episode_tree_not_acquired")
        if route["scene_config"] is None:
            result["missing"].append("scene_config_not_acquired")
        result["required_frames"] = [
            {
                "image_id": i,
                "repository_path": prefix + sparse[i] + ".png" if i in sparse else None,
                "publisher_digest": None,
                "bytes": None,
                "status": "not_acquired",
            }
            for i in required_ids
        ]
        result["required_payload_downloads"] = None
        return result
    tree_path = verify_binding(route["episode_tree"], cap=budget["max_tree_bytes"])
    pose, images = fetch.episode_metadata(strict_json(tree_path.read_bytes()), route["image_path"])
    if [i for i, _ in images] != required_ids:
        raise ValueError("Tree raw image IDs differ from original terminal annotation")
    payloads = [pose, *(entry for _index, entry in images)]
    result["complete_frame_filenames_known"] = True
    for entry in payloads:
        size, digest, kind = fetch._metadata_entry(entry)
        if size > budget["max_file_bytes"]:
            raise ValueError("Publisher file exceeds per-file cap")
        result["publisher_payloads"].append(
            {"repository_path": entry["path"], "bytes": size, "digest": digest, "digest_kind": kind}
        )
        path = local_path(Path(route["raw_root"]) / entry["path"], exists=False)
        if not path.is_file():
            result["missing"].append(entry["path"])
        else:
            result["actual_payloads"].append(verify_payload(path, entry, budget["max_file_bytes"]))
    if (
        sum(entry["bytes"] for entry in result["publisher_payloads"])
        > budget["max_total_payload_bytes"]
    ):
        raise ValueError("Complete route payload exceeds acquisition cap")
    if route["scene_config"] is None:
        result["missing"].append("scene_config_not_acquired")
    result["required_payload_downloads"] = [
        entry
        for entry in result["publisher_payloads"]
        if entry["repository_path"] in result["missing"]
    ]
    if result["missing"]:
        return result
    import yaml

    config_path = verify_binding(route["scene_config"], cap=budget["max_scene_config_bytes"])
    config = yaml.safe_load(config_path.read_bytes())
    ratios = config.get("traj_map") if isinstance(config, dict) else None
    if not isinstance(ratios, dict) or any(
        type(ratios.get(name)) not in (int, float)
        or not math.isfinite(ratios[name])
        or ratios[name] != 1
        for name in ("traj_scale_ratio", "pcd_scale_ratio")
    ):
        result["status"] = "quarantined-unreviewed-scene-scale"
        return result
    actions, _offline_aim = fetch.parse_pose(
        local_path(Path(route["raw_root"]) / pose["path"]).read_bytes()
    )
    aligned = fetch.align_episode(annotation, actions, images)
    if aligned["annotation_pose_yaw_exact_match"] is not True:
        raise ValueError("Original annotation and raw pose disagree")
    for index in range(len(actions)):
        builder.derive_primitive(actions, index)
    result["visual_identities"] = [
        builder.rgb_identity(local_path(Path(route["raw_root"]) / entry["path"]))
        for _index, entry in images
    ]
    result.update(
        status="local_raw_verified_not_training_approved",
        primitive_alignment_verified=True,
        eligible_step_count=len(actions),
    )
    return result


def preflight(plan_path, expected_sha):
    fetch, builder = helpers()
    plan_path = local_path(plan_path)
    if plan_path.stat().st_size > BUDGET["max_tree_bytes"]:
        raise ValueError("Plan exceeds byte cap")
    admitted_plan = binding(plan_path)
    if admitted_plan["sha256"] != expected_sha:
        raise ValueError("Explicit plan SHA differs")
    plan = strict_json(plan_path.read_bytes())
    if (
        plan["contract"] != CONTRACT
        or type(plan["schema_version"]) is not int
        or plan["schema_version"] != 1
        or plan["historical_train_scene"] != HISTORICAL_SCENE
        or plan["scene_assignment"].get(HISTORICAL_SCENE) != "train"
        or canonical(plan["budget"]) != canonical(BUDGET)
        or canonical(plan["claims"]) != canonical(CLAIMS)
        or plan["download_authorized"] is not False
        or plan["optimizer_authorized"] is not False
        or plan["official_seen_unseen_reserved_untouched"] is not True
        or plan["dataset_repository"] != fetch.DATASET
        or plan["dataset_revision"] != fetch.REVISION
        or plan["code_revision"] != fetch.CODE_REVISION
    ):
        raise ValueError("Explicit non-training acquisition plan differs")
    manifest_path = verify_binding(plan["source_manifest"], cap=BUDGET["max_tree_bytes"])
    manifest = load_manifest(manifest_path)
    expected = build_plan(
        manifest_path,
        [
            (r["scene_id"], r["annotation_index"])
            for r in plan["routes"]
            if r["partition"] == "train" and r["historical_training_route"] is False
        ],
        [
            (r["scene_id"], r["annotation_index"])
            for r in plan["routes"]
            if r["partition"] == "dev-validation"
        ],
        [
            (r["scene_id"], r["annotation_index"])
            for r in plan["routes"]
            if r["partition"] == "dev-test"
        ],
        plan["acquisition_root"],
    )
    if canonical(plan) != canonical(expected):
        raise ValueError("Plan must retain complete historical routes and exact declared sources")
    builder.verify_published_sources(manifest, ROOT)
    declarations = (
        source_records(manifest) + plan["source_files"] + [plan["source_manifest"], admitted_plan]
    )
    for route in plan["routes"]:
        declarations += [v for v in (route["episode_tree"], route["scene_config"]) if v is not None]
    before = merge_source_records(declarations)
    code_tree_binding = next(
        row for row in manifest["metadata_records"] if Path(row["path"]).name == "code-tree.json"
    )
    code_tree = strict_json(local_path(code_tree_binding["path"]).read_bytes())
    if code_tree["sha"] != fetch.CODE_REVISION or code_tree["truncated"] is not False:
        raise ValueError("Pinned code tree must be complete")
    for row in before.values():
        verify_binding(row)
    for route in plan["routes"]:
        if route["scene_config"] is not None:
            repository_path = "configs/" + route["scene_id"] + ".yaml"
            matches = [row for row in code_tree["tree"] if row["path"] == repository_path]
            if len(matches) != 1 or matches[0]["type"] != "blob":
                raise ValueError("Scene config is absent from pinned code tree")
            entry = matches[0]
            verify_payload(
                verify_binding(route["scene_config"]),
                {"size": entry["size"], "oid": entry["sha"]},
                BUDGET["max_scene_config_bytes"],
            )
    rows, total = selected_annotations(
        manifest["annotation"]["path"], {r["annotation_index"] for r in plan["routes"]}
    )
    inspected = [
        inspect_route(route, rows[route["annotation_index"]], BUDGET) for route in plan["routes"]
    ]
    for route in inspected:
        repository_path = "configs/" + route["scene_id"] + ".yaml"
        config_entries = [row for row in code_tree["tree"] if row["path"] == repository_path]
        if len(config_entries) != 1 or config_entries[0]["type"] != "blob":
            raise ValueError("Required scene config is absent from pinned official code tree")
        entry = config_entries[0]
        route["scene_config_requirement"] = {
            "repository": fetch.CODE_REPOSITORY,
            "revision": fetch.CODE_REVISION,
            "repository_path": repository_path,
            "bytes": entry["size"],
            "digest": entry["sha"],
            "digest_kind": "sha1-git-blob",
            "local_binding": route["scene_config"],
            "status": "acquired" if route["scene_config"] is not None else "not_acquired",
        }
        route["episode_tree_requirement"] = {
            "repository": fetch.DATASET,
            "revision": fetch.REVISION,
            "repository_path": "Image/" + route["image_path"],
            "local_binding": route["episode_tree"],
            "status": "acquired" if route["episode_tree"] is not None else "not_acquired",
        }
    for route in inspected:
        for row in route["actual_payloads"]:
            before = merge_source_records([*before.values(), row])
    if sum(len(r["required_raw_image_ids"]) for r in inspected) > BUDGET["max_raw_images"]:
        raise ValueError("Total complete raw image count exceeds acquisition budget")
    payload_bytes = sum(sum(p["bytes"] for p in r["actual_payloads"]) for r in inspected)
    if payload_bytes > BUDGET["max_total_payload_bytes"]:
        raise ValueError("Total existing payload exceeds acquisition budget")
    verified = [r for r in inspected if r["primitive_alignment_verified"]]
    grouped = anchored_visual_split(
        [
            {
                "episode_id": str(r["annotation_index"]),
                "scene_id": r["scene_id"],
                "source_episode": r["image_path"],
                "declared_partition": r["partition"],
                "visual_identities": r["visual_identities"],
            }
            for r in verified
        ]
    )
    after = {key: binding(key) for key in before}
    if canonical(before) != canonical(after):
        raise ValueError("Sources changed during preflight")
    steps = dict.fromkeys(("train", "dev-validation", "dev-test", "quarantine-visual-overlap"), 0)
    for route in verified:
        steps[grouped["assignment"][str(route["annotation_index"])]] += route["eligible_step_count"]
    complete = len(verified) == len(inspected)
    train_groups = sum(
        any(grouped["assignment"][key] == "train" for key in group)
        for group in grouped["grouping"]["groups"]
    )
    return {
        "contract": REPORT_CONTRACT,
        "native_exit_code": 0,
        "status": "dry_run_complete_missing_assets"
        if not complete
        else "local_raw_preflight_complete_not_training_approved",
        "plan": binding(plan_path),
        "dataset_repository": fetch.DATASET,
        "dataset_revision": fetch.REVISION,
        "code_revision": fetch.CODE_REVISION,
        "annotation_records_inspected": total,
        "sources_before": [before[key] for key in sorted(before)],
        "sources_after": [after[key] for key in sorted(after)],
        "sources_unchanged": True,
        "routes": inspected,
        "visual_split": grouped,
        "complete_route_count": len(verified),
        "verified_steps_by_effective_partition": steps,
        "missing_route_count": len(inspected) - len(verified),
        "planned_required_raw_image_count": sum(
            len(r["required_raw_image_ids"]) for r in inspected
        ),
        "actual_train_visual_groups": train_groups,
        "actual_existing_payload_bytes": payload_bytes,
        "all_raw_assets_acquired_and_verified": complete,
        "independent_holdout_available": complete
        and train_groups >= 2
        and steps["dev-validation"] > 0
        and steps["dev-test"] > 0
        and not grouped["overlap_routes"],
        "visual_split_complete": complete,
        "missing_assets_are_not_samples": True,
        "official_seen_unseen_reserved_untouched": True,
        **CLAIMS,
    }


def write_report(path, value):
    path = local_path(path, exists=False)
    allowed = ROOT / "artifacts/preflight-plan"
    if path.parent != allowed or path.exists() or path.is_symlink():
        raise ValueError("Report must be a new direct child of artifacts/preflight-plan")
    allowed.mkdir(exist_ok=True)
    with path.open("xb") as stream:
        stream.write(canonical(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit-plan", action="store_true")
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--extra-train", action="append", default=[])
    parser.add_argument("--dev-validation", action="append", default=[])
    parser.add_argument("--dev-test", action="append", default=[])
    parser.add_argument("--acquisition-root", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--plan-sha256")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.emit_plan:
            if (
                args.plan
                or args.plan_sha256
                or args.report
                or not args.source_manifest
                or not args.acquisition_root
            ):
                raise ValueError(
                    "Plan emission needs source manifest and separate acquisition root only"
                )
            result = build_plan(
                args.source_manifest,
                *[
                    [parse_scene_route(v) for v in values]
                    for values in (args.extra_train, args.dev_validation, args.dev_test)
                ],
                args.acquisition_root,
            )
        else:
            if (
                args.source_manifest
                or args.acquisition_root
                or args.extra_train
                or args.dev_validation
                or args.dev_test
                or not args.plan
                or not args.plan_sha256
            ):
                raise ValueError("Preflight requires an explicit plan and SHA only")
            result = preflight(args.plan, args.plan_sha256)
            if args.report:
                write_report(args.report, result)
                result = {
                    "contract": REPORT_CONTRACT,
                    "native_exit_code": 0,
                    "status": result["status"],
                    "report": binding(args.report),
                    "independent_holdout_available": result["independent_holdout_available"],
                    **CLAIMS,
                }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "contract": REPORT_CONTRACT,
                    "native_exit_code": 1,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    **CLAIMS,
                },
                ensure_ascii=False,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
