"""Create-only acquisition of six fixed original TRAIN payload pairs.

Historical paths-info observation6a877f is metadata, not payload GET evidence.
Only the pinned index report and small source entrypoints are reopened; the
405MB annotation, Parquet schema/columns, images and models are never read.
Downloads narrowly reuse the frozen exact-allowlist downloader/redirect rules.
This does not admit these payloads as samples or grant optimizer authority.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.openfly-indexed-training-route-acquisition-plan-v1"
REPORT_CONTRACT = "multidetect.openfly-indexed-training-route-payloads-v1"
OUTPUT = "artifacts/training/openfly-indexed-route-payloads-20261008-r1"
DATASET = "IPEC-COMMUNITY/OpenFly"
REVISION = "a12316d56a4e35a32ad626fb725ed7089937a1c4"
INDEX_PATH = "artifacts/diagnostics/openfly-bounded-annotation-index-20261008-r1/report.json"
INDEX_BYTES = 189868
INDEX_SHA = "74af3937f614ee78400a3c42cd4087c01a79d8a8d4fd712c57319e3bade25f1c"
SOURCE_FILES = (
    "scripts/fetch_openfly_indexed_training_routes.py",
    "tests/test_fetch_openfly_indexed_training_routes.py",
)
PINNED_HELPERS = {
    "fetch_and_audit_openfly_parquet": (
        27895,
        "0b5312f57a1e5813c1b7cef0e37de762d1c94e90376bd3d55000e727d862f6e9",
    ),
    "bind_openfly_parquet_route_metadata": (
        15665,
        "a47585a2aaabeef2753dd5ffe6bd00a33a55ee0fc6ed47ce9855db3a19564573",
    ),
    "probe_openfly_parquet_leaf_metadata": (
        13228,
        "71391d64d406af69667e965fd7dd1db5224d9797fcd09584c209d479448a5cb8",
    ),
    "probe_openfly_parquet_publication": (
        17481,
        "605303f0bb857419b95d8c373f34e7fd254ff8e0c34b058617bb3d788ea8e7e5",
    ),
    "probe_openfly_multiscene_availability": (
        22592,
        "25263370ca4ae9fbb68c65452b22cfff537cf22d4eb1f40fe9f3eb47d5ccb085",
    ),
    "fetch_openfly_multiscene_navigation": (
        24889,
        "d86e2fd419edcafdc3abc6934aea7e830e548b72157faed85e63005909821439",
    ),
    "plan_openfly_multiscene_acquisition": (
        28565,
        "b20930141a7746bf9ada7f102baec69c3d6fd72b4a1beb4e77006029fa783618",
    ),
    "fetch_openfly_navigation_pilot": (
        34972,
        "3e1315b1ad17ad752b1b8624076561272fb526fd00e54e566fad79f27a4920c7",
    ),
    "build_onboard_openfly_navigation_dataset": (
        35770,
        "508cf1b255cc57dc12e51d4345ab4b50faf14458bd00cc593732d3fa6ebf45f4",
    ),
}
# index, route basename, terminal-derived candidate frames, annotation digest,
# parquet bytes/LFS SHA256, pose bytes/Git blob SHA1: actual6a877f observation.
PUBLISHED = (
    (
        143,
        "2025-1-1_15-10-50_1232056856",
        15,
        "8176a60a7fdec417b3ccf5560400ad9d29a9529be32fe9a3d64b2fd4a1b1a94a",
        11722348,
        "8173627adf5093fdf5f84fec532abc57ff29b870192b171e18b6fe797df792b3",
        4106,
        "9c9234309ae1915ce3142f2940f7626a65152c97",
    ),
    (
        150,
        "2025-1-1_16-53-12_610506582",
        29,
        "784d275cae29063bf1c2f6642c7dbe18de5dd00b2936dbd0e8bdf408857d9a15",
        17700019,
        "22af71853998bd278be693433dfaebc927fe49f79185921ebbe15469a82aff06",
        7416,
        "fa919d4998a5578d975bf60cecb119c8d5212f5c",
    ),
    (
        153,
        "2025-1-1_14-35-7_1795519125",
        17,
        "417ef45fa9957f01590b46b07908fe56136f7e19e54d8e9a1b866bd92bcb9153",
        12149310,
        "d3073859dbb353e098d4c8267fc4be1d1caba676026d716fcef8ba04c586302d",
        4579,
        "6a1bf677bf29b503f6cf95e6e9303c8a70dfb92c",
    ),
    (
        154,
        "2025-1-1_15-3-52_1426819080",
        25,
        "3479b78c27984a4267b88966452f863ada403c455c02a75e909e51b7ed450c15",
        15157033,
        "0669ecd0047ef7b6c25a31c5b659eab4c8d97688523758cbb5252094f9a04bc7",
        6656,
        "07be138b3b18032714134a8897177ed9e2a200b3",
    ),
    (
        148,
        "2025-1-1_15-45-7_76593093",
        15,
        "182d724c4e46c90605583fc3c823d4274fafc79db3738f8f1e2142a5c7710836",
        10369152,
        "2187fc291e5b97f69d5c6975d57960b78887c8eb6d8bcd4dc4010027ced505b3",
        4128,
        "de02fe0d26bff036b52fec681acf7caf8af9d232",
    ),
    (
        144,
        "2025-1-1_18-18-56_1683932587",
        21,
        "4b355b4eb573f781bddfb94edc047ccecaad518f070ec0b5572fbbc730518f76",
        16561799,
        "9f1beff7d84821388c63969a24050f94284ccdbbb5b81d70414a57f20ad1b79e",
        5690,
        "f597aac1b3b96100236644f3b1e2e1f49c7eed40",
    ),
)
BUDGET = {
    "max_payload_files": 12,
    "max_total_payload_bytes": 128 * 1024 * 1024,
    "exact_payload_bytes": 83692236,
    "max_http_responses_per_file": 6,
    "max_http_responses_total_upper_bound": 72,
    "max_small_source_bytes": 2 * 1024 * 1024,
}
GATES = dict.fromkeys(
    (
        "schema_read",
        "images_extracted",
        "images_decoded",
        "pose_geometry_verified",
        "frd_targets_verified",
        "samples_created",
        "training_ready",
        "model_loaded",
        "gpu_used",
        "optimizer_authorized",
        "optimizer_performed",
        "quality_claim",
        "independent_holdout_verified",
        "candidate_promoted",
        "execution_authority",
        "physical_performance_claimed",
        "final_release_allowed",
    ),
    False,
)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    def nonfinite(_):
        raise ValueError("nonfinite JSON constant")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)


def local_path(value, *, fresh=False):
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    root = ROOT.resolve(strict=True)
    if not path.resolve().is_relative_to(root) or any(part == ".." for part in path.parts):
        raise ValueError("path escapes repository")
    cursor = root
    for part in path.relative_to(ROOT).parts:
        cursor = cursor / part
        if (
            cursor.is_symlink()
            or cursor.exists()
            and (getattr(cursor.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400)
        ):
            raise ValueError("symlink/reparse source or output denied")
    if fresh and path.exists():
        raise FileExistsError("create-only target exists")
    return path


def binding(path, *, runtime=False, cap=BUDGET["max_small_source_bytes"]):
    path = Path(path).resolve(strict=True) if runtime else local_path(path)
    if not path.is_file() or path.is_symlink() or type(cap) is not int or path.stat().st_size > cap:
        raise ValueError("regular file exceeds binding cap")
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
        type(row) is not dict
        or set(row) != {"path", "bytes", "sha256"}
        or (
            type(row["bytes"]) is not int
            or not 0 < row["bytes"] <= BUDGET["max_small_source_bytes"]
            or type(row["sha256"]) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
        )
    ):
        raise ValueError("invalid small source binding")
    actual = binding(row["path"], runtime=runtime)
    if canonical(actual) != canonical(row):
        raise ValueError("small source identity changed")
    return actual


def helpers():
    for name, (size, sha) in PINNED_HELPERS.items():
        verify({"path": f"scripts/{name}.py", "bytes": size, "sha256": sha})
    original = sys.path.copy()
    try:
        sys.path.insert(0, str(ROOT))
        downloader = importlib.import_module("scripts.fetch_and_audit_openfly_parquet")
        *_others, acquisition, planner, fetch, _builder = downloader.helpers()
    finally:
        sys.path[:] = original
    for name in PINNED_HELPERS:
        module = sys.modules[f"scripts.{name}"]
        if Path(module.__file__).resolve() != (ROOT / f"scripts/{name}.py").resolve():
            raise ValueError("imported helper is not the admitted source")
    if fetch.DATASET != DATASET or fetch.REVISION != REVISION:
        raise ValueError("pinned publisher differs")
    return downloader, acquisition, planner, fetch


def runtime_sources():
    # Four installed requests entrypoints, not all urllib3/TLS/transitive code.
    return [
        binding(sys.modules[name].__file__, runtime=True)
        for name in ("requests", "requests.sessions", "requests.models", "requests.adapters")
    ]


def no_heavy_imports():
    if any(
        name.split(".")[0] in {"torch", "transformers", "peft", "PIL", "pyarrow", "datasets"}
        for name in sys.modules
    ):
        raise ValueError("payload acquisition must not import ML/image/schema libraries")


def build_plan():
    local_path(OUTPUT, fresh=True)
    index_binding = {"path": INDEX_PATH, "bytes": INDEX_BYTES, "sha256": INDEX_SHA}
    verify(index_binding)
    index = strict_json(local_path(INDEX_PATH).read_bytes())
    if (
        index.get("contract") != "multidetect.openfly-bounded-annotation-index-v1"
        or any(
            index.get(name) is not True
            for name in ("small_sources_unchanged", "annotation_stat_unchanged")
        )
        or canonical(index["source_bindings_before"]) != canonical(index["source_bindings_after"])
    ):
        raise ValueError("only unchanged bound index metadata is admitted")
    declarations = {INDEX_PATH: index_binding}
    for row in index["source_bindings_before"]:
        relative = local_path(row["path"]).relative_to(ROOT).as_posix()
        admitted = verify({**row, "path": relative})
        declarations[relative] = admitted
    routes, payloads = [], []
    for (
        number,
        name,
        frames,
        annotation_sha,
        shard_size,
        shard_sha,
        pose_size,
        pose_sha,
    ) in PUBLISHED:
        rows = [row for row in index["routes"] if row["annotation_index"] == number]
        if len(rows) != 1:
            raise ValueError("fixed selected TRAIN route absent/duplicate")
        row = rows[0]
        image_path = f"env_airsim_26/astar_data/low_short/{name}"
        if (
            row["scene_id"] != "env_airsim_26"
            or row["locked_development_role"] != "train"
            or row["original_annotation_split"] != "train"
            or row["image_path"] != image_path
            or row["annotation_record_sha256"] != annotation_sha
            or type(row["candidate_frame_count_from_terminal_id"]) is not int
            or row["candidate_frame_count_from_terminal_id"] != frames
            or row["raw_pose_actions_verified"] is not False
        ):
            raise ValueError("fixed selected TRAIN route metadata differs")
        paths = ("traj/" + image_path + ".parquet", "Image/" + image_path + "/pose.jsonl")
        if (row["parquet_path"], row["pose_path"]) != paths:
            raise ValueError("fixed publisher paths differ")
        routes.append(
            {
                key: row[key]
                for key in (
                    "annotation_index",
                    "scene_id",
                    "image_path",
                    "locked_development_role",
                    "original_annotation_split",
                    "annotation_record_sha256",
                    "candidate_frame_count_from_terminal_id",
                    "annotation_action_code_counts",
                )
            }
        )
        for role, path, size, digest, kind in (
            ("shard", paths[0], shard_size, shard_sha, "sha256-lfs"),
            ("pose", paths[1], pose_size, pose_sha, "sha1-git-blob"),
        ):
            payloads.append(
                {
                    "role": role,
                    "annotation_index": number,
                    "repository_path": path,
                    "bytes": size,
                    "digest": digest,
                    "digest_kind": kind,
                    "url": f"https://huggingface.co/datasets/{DATASET}/resolve/{REVISION}/{path}",
                }
            )
    if (
        len(payloads) != 12
        or sum(row["bytes"] for row in payloads) != BUDGET["exact_payload_bytes"]
    ):
        raise ValueError("fixed twelve payload budget differs")
    helpers()
    for name in (*SOURCE_FILES, *(f"scripts/{name}.py" for name in PINNED_HELPERS)):
        declarations[name] = binding(name)
    no_heavy_imports()
    plan = {
        "schema_version": 1,
        "contract": CONTRACT,
        "dataset_repository": DATASET,
        "dataset_revision": REVISION,
        "index_report": index_binding,
        "routes": routes,
        "payloads": payloads,
        "output_directory": OUTPUT,
        "budget": BUDGET.copy(),
        "metadata_observation": {
            "tool_chunk": "6a877f",
            "http_status": 200,
            "bytes": 3145,
            "sha256": "5cebc051cfe57cbc2ce9a972f26bb07868a8921c8423632bbe94a8ceebdc9793",
            "scope": "historical_paths_info_constants_not_fresh_payload_GET",
        },
        "sources": [declarations[key] for key in sorted(declarations)],
        "runtime_sources": runtime_sources(),
        "source_inventory_scope": (
            "listed_source_and_requests_entrypoints_not_all_transitive_runtime"
        ),
        "claims": GATES.copy(),
    }
    for source in plan["sources"]:
        verify(source)
    return plan


def create_json(path, value):
    path = local_path(path, fresh=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    local_path(path, fresh=True)
    with path.open("xb") as stream:
        stream.write(canonical(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    return binding(path)


def acquire(path, expected_sha, *, client=None):
    plan_binding = binding(path)
    if plan_binding["sha256"] != expected_sha:
        raise ValueError("explicit source plan SHA differs")
    plan = strict_json(local_path(path).read_bytes())
    if canonical(plan) != canonical(build_plan()):
        raise ValueError("plan differs from fixed publisher/index/source admission")
    downloader, _acquisition, _planner, fetch = helpers()
    sources = plan["sources"] + [plan_binding]
    before = [verify(row) for row in sources]
    runtime_before = [verify(row, runtime=True) for row in plan["runtime_sources"]]
    output = local_path(OUTPUT, fresh=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    local_path(OUTPUT, fresh=True)
    output.mkdir(exist_ok=False)
    owned_client = client is None
    records, failures, attempts = [], [], 0
    try:
        if owned_client:
            client = fetch.OfficialClient()
            client.session.auth = lambda request: request  # suppress netrc/token Authorization
        allowlist = {row["url"]: row for row in plan["payloads"]}
        for row in plan["payloads"]:
            attempts += 1
            result = downloader.download_payload(client, row, output, allowlist)
            file = binding(
                output / "payload" / row["repository_path"], cap=BUDGET["max_total_payload_bytes"]
            )
            if (
                result["file"] != file
                or file["bytes"] != row["bytes"]
                or (result["publisher_digest_verified"] is not True)
            ):
                raise ValueError("downloaded payload binding differs")
            trail = []
            for hop in result["response_chain"]:
                host = hop["host"]
                fetch._allowed_url("https://" + host)
                trail.append({"host": host})  # never persist URLs/headers/signed query credentials
            if len(trail) > 6:
                raise ValueError("response chain exceeds existing redirect budget")
            records.append(
                {**row, "file": file, "response_chain": trail, "publisher_digest_verified": True}
            )
    except Exception as exc:
        failures.append(
            {
                "stage": "payload_acquisition",
                "type": type(exc).__name__,
                "reason": "original payload acquisition failed; partial bytes preserved",
            }
        )
    finally:
        if owned_client and client is not None:
            try:
                client.session.close()
            except Exception as exc:
                failures.append(
                    {
                        "stage": "client_cleanup",
                        "type": type(exc).__name__,
                        "reason": "official HTTP session cleanup failed",
                    }
                )
    after = runtime_after = None
    try:
        after = [verify(row) for row in sources]
        runtime_after = [verify(row, runtime=True) for row in plan["runtime_sources"]]
        if runtime_sources() != runtime_before:
            raise ValueError("imported HTTP runtime entrypoint changed")
        for record in records:
            if (
                binding(record["file"]["path"], cap=BUDGET["max_total_payload_bytes"])
                != record["file"]
            ):
                raise ValueError("published payload changed before seal")
        no_heavy_imports()
    except Exception as exc:
        failures.append(
            {
                "stage": "final_identity_check",
                "type": type(exc).__name__,
                "reason": "small source or payload verification failed/incomplete",
            }
        )
    complete = not failures and len(records) == 12
    report = {
        "schema_version": 1,
        "contract": REPORT_CONTRACT,
        "native_exit_code": 0 if complete else 1,
        "plan": plan_binding,
        "dataset_repository": DATASET,
        "dataset_revision": REVISION,
        "metadata_observation": plan["metadata_observation"],
        "routes": plan["routes"],
        "payloads": records,
        "payload_files_verified": len(records),
        "verified_payload_bytes": sum(row["file"]["bytes"] for row in records),
        "initial_payload_requests_attempted": attempts,
        "http_responses_observed": sum(len(row["response_chain"]) for row in records),
        "http_response_count_scope": "verified_files_only_failed_chain_count_unknown",
        "client_scope": "official_client"
        if owned_client
        else "injected_client_not_HTTP_acceptance",
        "all_twelve_payloads_verified": complete,
        "failures": failures,
        "sources_before": before,
        "sources_after": after,
        "runtime_sources_before": runtime_before,
        "runtime_sources_after": runtime_after,
        "small_sources_unchanged": after == before and runtime_after == runtime_before,
        "source_inventory_scope": plan["source_inventory_scope"],
        "claims": GATES.copy(),
        "external_native_status_verification_required": True,
    }
    create_json(output / "report.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--emit-plan", metavar="CREATE_ONLY_PATH")
    mode.add_argument("--plan")
    parser.add_argument("--plan-sha256")
    args = parser.parse_args(argv)
    try:
        if args.emit_plan:
            if args.plan_sha256 is not None:
                raise ValueError("plan SHA is only for acquisition")
            print(json.dumps(create_json(args.emit_plan, build_plan())))
            return 0
        if not args.plan_sha256:
            raise ValueError("explicit plan SHA required")
        report = acquire(args.plan, args.plan_sha256)
        print(
            json.dumps(
                {
                    "event": "indexed_route_payload_acquisition_finished",
                    "native_exit_code": report["native_exit_code"],
                    "payload_files_verified": report["payload_files_verified"],
                }
            )
        )
        return report["native_exit_code"]
    except Exception as exc:
        # Exceptions from signed redirect requests may contain credential URLs.
        print(
            json.dumps(
                {
                    "event": "indexed_route_payload_admission_failed",
                    "type": type(exc).__name__,
                    "reason": "admission/acquisition failed",
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
