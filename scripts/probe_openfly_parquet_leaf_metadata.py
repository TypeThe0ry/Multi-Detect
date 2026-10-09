"""Inspect one real published parquet category in each of three admitted scenes.

This is a six-request metadata-only phase. It never reads parquet payloads,
range/footer bytes, images, models, or official seen/unseen annotations.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.openfly-parquet-leaf-probe-plan-v1"
REPORT_CONTRACT = "multidetect.openfly-parquet-leaf-metadata-v1"
OUTPUT = "artifacts/diagnostics/openfly-parquet-leaf-metadata-20261008-r1"
PARENT_REPORT = "artifacts/diagnostics/openfly-parquet-publication-20261008-r1/report.json"
SCENES = ("env_airsim_16", "env_airsim_23", "env_airsim_26")
SOURCE_FILES = (
    "scripts/probe_openfly_parquet_leaf_metadata.py",
    "tests/test_probe_openfly_parquet_leaf_metadata.py",
)
BUDGET = {
    "max_tree_requests": 6,
    "max_total_metadata_bytes": 8 * 1024 * 1024,
    "max_tree_bytes": 2 * 1024 * 1024,
    "max_category_trees_per_scene": 1,
}


def helpers():
    original = sys.path.copy()
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from scripts import probe_openfly_parquet_publication as publication
    finally:
        sys.path[:] = original
    return publication, *publication.helpers()


def build_plan():
    publication, _probe, acquisition, planner, fetch, _builder = helpers()
    path = planner.local_path(PARENT_REPORT)
    if path.stat().st_size > BUDGET["max_tree_bytes"]:
        raise ValueError("Parent metadata report exceeds cap")
    parent = planner.strict_json(path.read_bytes())
    if (
        parent.get("contract") != publication.REPORT_CONTRACT
        or type(parent.get("native_exit_code")) is not int
        or parent["native_exit_code"] != 0
        or parent.get("dataset_repository") != fetch.DATASET
        or parent.get("dataset_revision") != fetch.REVISION
        or parent.get("sources_unchanged") is not True
        or parent.get("parquet_payload_downloaded") is not False
        or planner.canonical(parent["sources_before"]) != planner.canonical(parent["sources_after"])
    ):
        raise ValueError("Admit only the source-bound pinned parent publication metadata")
    base_directories = []
    for scene in SCENES:
        candidates = [tree for tree in parent["trees"] if tree["requested_path"] == "traj/" + scene]
        directory = f"traj/{scene}/astar_data"
        if len(candidates) != 1 or directory not in candidates[0]["directories"]:
            raise ValueError("Exact scene astar_data directory was not observed in parent metadata")
        base_directories.append(directory)
    declarations = planner.merge_source_records(
        parent["sources_before"]
        + [planner.binding(path)]
        + [planner.binding(ROOT / name) for name in SOURCE_FILES]
        + [tree["tree"] for tree in parent["trees"]]
        + [
            {key: row[key] for key in ("path", "bytes", "sha256")}
            for row in parent["documentation"]
        ]
    )
    for row in declarations.values():
        planner.verify_binding(row)
    return {
        "contract": CONTRACT,
        "dataset_repository": fetch.DATASET,
        "dataset_revision": fetch.REVISION,
        "parent_report": planner.binding(path),
        "base_directories": base_directories,
        "category_selection": "low_short_if_published_else_first_actual_child_directory",
        "output_directory": OUTPUT,
        "budget": BUDGET.copy(),
        "sources": [declarations[key] for key in sorted(declarations)],
        "payload_download_authorized": False,
        "claims": acquisition.GATES.copy(),
    }


def select_category(base, directories):
    preferred = base + "/low_short"
    if preferred in directories:
        return preferred, "published_low_short"
    return (
        (directories[0], "first_actual_publisher_directory")
        if directories
        else (None, "no_category_published")
    )


def run_probe(path, expected_sha, *, client=None):
    publication, probe, acquisition, planner, fetch, _builder = helpers()
    path = planner.local_path(path)
    admitted = planner.binding(path)
    if admitted["bytes"] > BUDGET["max_tree_bytes"] or admitted["sha256"] != expected_sha:
        raise ValueError("Explicit leaf probe plan SHA/size differs")
    plan = planner.strict_json(path.read_bytes())
    if planner.canonical(plan) != planner.canonical(build_plan()):
        raise ValueError("Leaf metadata plan differs from current exact authority")
    declarations = planner.merge_source_records(plan["sources"] + [admitted])
    before = [declarations[key] for key in sorted(declarations)]
    for row in before:
        planner.verify_binding(row)
    out = acquisition.guarded_path(plan["output_directory"], fresh=True)
    if out != ROOT / OUTPUT:
        raise ValueError("Only the new leaf metadata output is writable")
    out.parent.mkdir(parents=True, exist_ok=True)
    acquisition.guarded_path(plan["output_directory"], fresh=True)
    out.mkdir(exist_ok=False)
    client_owned = client is None
    client = client or probe.bounded_client()
    trees, retained, choices, errors = [], [], [], []
    requests, total_bytes, native_exit = 0, 0, 0

    def read_tree(directory):
        nonlocal requests, total_bytes
        if requests >= BUDGET["max_tree_requests"]:
            raise ValueError("Leaf tree request cap exhausted")
        requests += 1
        url = f"https://huggingface.co/api/datasets/{fetch.DATASET}/tree/{fetch.REVISION}/{directory}?limit=1000"
        if client_owned:
            document, raw, trail, pagination = publication.tree_metadata(
                client, url, BUDGET["max_total_metadata_bytes"] - total_bytes
            )
        else:
            document, raw, trail = client.metadata(url)
            pagination = False
        if (
            not isinstance(raw, bytes)
            or not 0 < len(raw) <= BUDGET["max_tree_bytes"]
            or total_bytes + len(raw) > BUDGET["max_total_metadata_bytes"]
        ):
            raise ValueError("Leaf tree exceeds remaining metadata budget")
        record = acquisition.create_bytes(out / f"metadata/tree-{requests}.json", raw)
        retained.append(record)
        total_bytes += len(raw)
        if planner.canonical(planner.strict_json(raw)) != planner.canonical(document):
            raise ValueError("Leaf response differs from retained original JSON")
        parsed = publication.validate_tree(document, directory)
        parsed["pagination_link_present"] = pagination
        parsed["complete_without_pagination"] = (
            parsed["complete_without_pagination"] and not pagination
        )
        result = {"requested_path": directory, "tree": record, "response_chain": trail, **parsed}
        trees.append(result)
        print(
            json.dumps(
                {
                    "event": "parquet_leaf_tree_inspected",
                    "path": directory,
                    "files": len(parsed["files"]),
                    "directories": len(parsed["directories"]),
                    "complete_without_pagination": parsed["complete_without_pagination"],
                }
            ),
            flush=True,
        )
        return result

    try:
        for base in plan["base_directories"]:
            tree = read_tree(base)
            selected, reason = select_category(base, tree["directories"])
            choices.append(
                {"base_directory": base, "selected_category": selected, "selection_reason": reason}
            )
        for choice in choices:
            if choice["selected_category"] is not None:
                read_tree(choice["selected_category"])
        for row in retained:
            planner.verify_binding(row)
    except Exception as exc:
        native_exit = 1
        errors.append(
            {
                "type": type(exc).__name__,
                "reason": str(exc)
                if isinstance(exc, ValueError)
                else "bounded leaf metadata request failed",
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
                {"path": row["path"], "type": type(exc).__name__, "reason": "Input disappeared"}
            )
    unchanged = planner.canonical(before) == planner.canonical(after)
    if not unchanged:
        native_exit = 1
    file_map = {}
    for tree in trees:
        for file in tree["files"]:
            identity = file["repository_path"]
            if identity in file_map and planner.canonical(file_map[identity]) != planner.canonical(
                file
            ):
                raise ValueError("Conflicting publisher shard declaration")
            file_map[identity] = file
    shards = [row for row in file_map.values() if row["repository_path"].endswith(".parquet")]
    minimum = min((row["bytes"] for row in shards), default=None)
    selected_paths = [choice["selected_category"] for choice in choices]
    category_trees = [tree for tree in trees if tree["requested_path"] in selected_paths]
    selected_complete = (
        native_exit == 0
        and len(choices) == 3
        and len(category_trees) == 3
        and all(
            tree["complete_without_pagination"] and not tree["directories"]
            for tree in category_trees
        )
    )
    report = {
        "contract": REPORT_CONTRACT,
        "native_exit_code": native_exit,
        "plan": admitted,
        "dataset_repository": fetch.DATASET,
        "dataset_revision": fetch.REVISION,
        "tree_requests_performed": requests,
        "actual_metadata_bytes": total_bytes,
        "choices": choices,
        "trees": trees,
        "retained_metadata": retained,
        "errors": errors,
        "observed_parquet_shards": shards,
        "observed_parquet_shard_count": len(shards),
        "observed_parquet_bytes": sum(row["bytes"] for row in shards),
        "smallest_observed_shard_bytes": minimum,
        "smallest_observed_shards": [row for row in shards if row["bytes"] == minimum],
        "selected_category_trees_complete": selected_complete,
        "whole_scene_inventory_exhaustive": False,
        "whole_dataset_inventory_exhaustive": False,
        "rgb_parquet_schema_verified": False,
        "parquet_payload_downloaded": False,
        "range_or_footer_requested": False,
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
                raise ValueError("Emission never requests metadata")
            print(json.dumps(build_plan(), ensure_ascii=False, sort_keys=True, indent=2))
            return 0
        if args.plan is None or args.plan_sha256 is None:
            raise ValueError("Explicit leaf metadata plan and SHA required")
        report, record = run_probe(args.plan, args.plan_sha256)
        print(
            json.dumps(
                {
                    "event": "parquet_leaf_metadata_probe_finished",
                    "native_exit_code": report["native_exit_code"],
                    "report": record,
                    "tree_requests_performed": report["tree_requests_performed"],
                    "metadata_bytes": report["actual_metadata_bytes"],
                    "observed_parquet_shards": report["observed_parquet_shard_count"],
                    "observed_parquet_bytes": report["observed_parquet_bytes"],
                    "smallest_observed_shard_bytes": report["smallest_observed_shard_bytes"],
                    "selected_category_trees_complete": report["selected_category_trees_complete"],
                    "parquet_payload_downloaded": False,
                    "rgb_parquet_schema_verified": False,
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
                    "event": "parquet_leaf_metadata_admission_failed",
                    "native_exit_code": 1,
                    "error_type": type(exc).__name__,
                    "reason": str(exc)
                    if isinstance(exc, ValueError)
                    else "Leaf metadata admission failed",
                    "training_ready": False,
                }
            ),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
