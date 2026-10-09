"""Index a bounded prefix of the retained official OpenFly TRAIN annotation.

This is metadata discovery, not pose-action/RGB validation or training admission.
The consumed prefix SHA is not a current whole-file SHA on a partial scan.
No network, images, weights, live WAL, models, or seen/unseen annotations are read.
"""

from __future__ import annotations

import argparse
import codecs
import hashlib
import importlib.util
import io
import json
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY = "scripts/build_openfly_bounded_annotation_index.py"
TEST = "tests/test_build_openfly_bounded_annotation_index.py"
PARSER = "scripts/fetch_openfly_navigation_pilot.py"
PARSER_PIN = (34972, "3e1315b1ad17ad752b1b8624076561272fb526fd00e54e566fad79f27a4920c7")
DEFAULT_MANIFEST = "artifacts/training/openfly-navigation-source-pilot-20261007-r2/manifest.json"
MANIFEST_PIN = (142534, "b8a4b318678bf1e2a47a5340bafe47811f61a25266084cdad888c4691db9706c")
ANNOTATION_PIN = (405815796, "1a4c5a5ed1561ea5b3b79b6c25da7648ce4085b7d8489d62022a9bd1244100df")
REVISION = "a12316d56a4e35a32ad626fb725ed7089937a1c4"
CONTRACT = "multidetect.openfly-bounded-annotation-index-v1"
SMALL_CAP = 2 * 1024 * 1024
PROMPT_CAP = 16 * 1024
ROLES = {
    "env_airsim_18": "train",
    "env_airsim_26": "train",
    "env_airsim_16": "dev-validation",
    "env_airsim_23": "dev-test",
}


@dataclass(frozen=True)
class ScanBudget:
    max_bytes: int = 64 * 1024 * 1024
    max_records: int = 20000
    max_routes: int = 128
    max_frames_per_route: int = 128
    max_candidate_frames: int = 4096

    def validate(self):
        ceilings = asdict(ScanBudget())
        if any(type(v) is not int or not 0 < v <= ceilings[k] for k, v in asdict(self).items()):
            raise ValueError(
                "Positive integer budgets may not exceed the fixed first-batch ceilings"
            )


class ByteBudgetReached(Exception):
    """Intentional partial-prefix boundary, not a malformed JSON success."""


def canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def bad(_value):
        raise ValueError("Nonfinite JSON constant")

    result = json.loads(raw, object_pairs_hook=pairs, parse_constant=bad)
    canonical(result)
    return result


def local_path(value, *, exists=True):
    if not isinstance(value, (str, Path)):
        raise ValueError("Local evidence path must be text")
    original = ROOT / value
    path = original.resolve(strict=exists)
    if not path.is_relative_to(ROOT.resolve()):
        raise ValueError("Evidence path escapes repository")
    cursor = original.absolute()
    while cursor.is_relative_to(ROOT.absolute()) and cursor != ROOT.absolute():
        if cursor.is_symlink():
            raise ValueError("Symlink evidence paths are not admitted")
        cursor = cursor.parent
    return path


def small_binding(value, *, cap=SMALL_CAP):
    path = local_path(value)
    size = path.stat().st_size
    if not path.is_file() or not 0 < size <= cap:
        raise ValueError("Small metadata/source byte cap exceeded")
    raw = path.read_bytes()
    if len(raw) != size or path.stat().st_size != size:
        raise ValueError("Small source changed during read")
    return {"path": str(path), "bytes": size, "sha256": hashlib.sha256(raw).hexdigest()}


def file_stat(value):
    path = local_path(value)
    if not path.is_file():
        raise ValueError("Annotation must be a regular file")
    stat = path.stat()
    return {
        "path": str(path),
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "device": stat.st_dev,
        "inode": stat.st_ino,
    }


def build_plan(source_manifest=DEFAULT_MANIFEST, *, budget=None):
    budget = budget or ScanBudget()
    budget.validate()
    manifest_binding = small_binding(source_manifest)
    if (manifest_binding["bytes"], manifest_binding["sha256"]) != MANIFEST_PIN:
        raise ValueError("Retained historical TRAIN manifest differs")
    manifest = strict_json(Path(manifest_binding["path"]).read_bytes())
    if (
        manifest.get("contract") != "multidetect.openfly-navigation-pilot-v1"
        or manifest.get("seen_unseen_downloaded") is not False
        or manifest["dataset"]["repository"] != "IPEC-COMMUNITY/OpenFly"
        or manifest["dataset"]["revision"] != REVISION
        or manifest["dataset"]["split"] != "train"
    ):
        raise ValueError("Only the sealed original TRAIN source is allowed")
    annotation = manifest["annotation"]
    path = local_path(annotation["path"])
    expected = Path(manifest_binding["path"]).parent / "raw/Annotation/train.json"
    if (
        path != expected
        or annotation["repository_path"] != "Annotation/train.json"
        or type(annotation["bytes"]) is not int
        or (annotation["bytes"], annotation["sha256"]) != ANNOTATION_PIN
    ):
        raise ValueError("Historical annotation declaration/path differs")
    observed = file_stat(path)
    if observed["bytes"] != annotation["bytes"]:
        raise ValueError("Current annotation size differs from historical receipt")
    sources = [small_binding(name) for name in (ENTRY, TEST, PARSER)]
    if (sources[-1]["bytes"], sources[-1]["sha256"]) != PARSER_PIN:
        raise ValueError("Frozen annotation parser differs")
    plan = {
        "contract": CONTRACT + "-plan",
        "dataset_revision": REVISION,
        "source_manifest": manifest_binding,
        "historical_annotation_binding": {k: annotation[k] for k in ("path", "bytes", "sha256")},
        "annotation_stat": observed,
        "source_files": sources,
        "budget": asdict(budget),
        "locked_scene_roles": ROLES.copy(),
        "scan_scope": "first_bounded_prefix_in_original_train_order_not_exhaustive_or_random",
        "official_seen_unseen_reserved_untouched": True,
    }
    if (
        small_binding(source_manifest) != manifest_binding
        or [small_binding(row["path"]) for row in sources] != sources
        or file_stat(path) != observed
    ):
        raise ValueError("Small plan sources or annotation stat changed before emission")
    return plan


def load_parser():
    row = small_binding(PARSER)
    if (row["bytes"], row["sha256"]) != PARSER_PIN:
        raise ValueError("Frozen parser changed before import")
    spec = importlib.util.spec_from_file_location("_bounded_openfly_frozen_parser", ROOT / PARSER)
    if spec is None or spec.loader is None:
        raise ValueError("Cannot load frozen metadata parser")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if small_binding(PARSER) != row:
        raise ValueError("Frozen parser changed during import")
    return module


class LimitedRawReader(io.RawIOBase):
    """Count/hash actual binary reads below the UTF-8 decoder's read-ahead."""

    def __init__(self, path, max_bytes, source_size):
        self.stream = local_path(path).open("rb", buffering=0)
        self.max_bytes, self.source_size = max_bytes, source_size
        self.bytes_read = 0
        self.sha256 = hashlib.sha256()

    def readable(self):
        return True

    def readinto(self, target):
        if self.bytes_read == self.source_size:
            return 0
        remaining = self.max_bytes - self.bytes_read
        if not remaining:
            raise ByteBudgetReached()
        count = self.stream.readinto(
            memoryview(target)[: min(remaining, self.source_size - self.bytes_read)]
        )
        if not count:
            raise ValueError("Annotation ended before its admitted size")
        self.bytes_read += count
        self.sha256.update(memoryview(target)[:count])
        return count

    def close(self):
        try:
            self.stream.close()
        finally:
            super().close()


class BudgetedAnnotationPath:
    """The frozen iterator only requires path.open; no global monkeypatch."""

    def __init__(self, path, budget, source_size):
        self.path, self.budget, self.source_size = path, budget, source_size
        self.raw = None

    def open(self, mode, *, encoding):
        if mode != "r" or encoding != "utf-8" or self.raw is not None:
            raise ValueError("Exactly one UTF-8 metadata stream is allowed")
        self.raw = LimitedRawReader(self.path, self.budget.max_bytes, self.source_size)
        return BudgetedTextReader(self.raw)


class BudgetedTextReader:
    """Return decoded chunks without losing completed text at a hard byte cap."""

    def __init__(self, raw):
        self.raw = raw
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")

    def __enter__(self):
        return self

    def __exit__(self, *_error):
        self.raw.close()

    def read(self, size):
        if type(size) is not int or size <= 0:
            raise ValueError("Only positive bounded text reads are allowed")
        while True:
            data = bytearray(min(size, 65536))
            count = self.raw.readinto(data)
            text = self.decoder.decode(memoryview(data)[:count], final=count == 0)
            if text or not count:
                return text


def validate_index_record(parser, record):
    """Validate ordinary records; quarantine recognized padding without labels.

    The disposable action copy below is only a schema-validation aid: it lets
    the unchanged strict parser reach its non-action pose/index checks. It is
    never indexed, counted as original actions, or emitted as a route/target.
    """
    if len(canonical(record)) > SMALL_CAP:
        raise ValueError("Decoded annotation record exceeds metadata cap")
    try:
        return parser.validate_annotation(record)
    except parser.PilotError as error:
        if str(error) != "unknown original action code":
            raise
        actions = record["action"]
        if not (
            all(type(code) is int and code in (*range(10), -1, -2) for code in actions)
            and any(code in (-1, -2) for code in actions)
        ):
            raise
        non_action_schema = {**record, "action": [1] * (len(actions) - 1) + [0]}
        parser.validate_annotation(non_action_schema)
        return None


def scan_index(plan, *, plan_binding=None):
    budget = ScanBudget(**plan["budget"])
    authentic = build_plan(plan["source_manifest"]["path"], budget=budget)
    if canonical(authentic) != canonical(plan):
        raise ValueError("Bound index plan differs from admitted sources/roles/budgets")
    before = plan["source_files"] + [plan["source_manifest"]]
    if plan_binding is not None:
        before += [plan_binding]
    parser = load_parser()
    proxy = BudgetedAnnotationPath(
        plan["annotation_stat"]["path"], budget, plan["annotation_stat"]["bytes"]
    )
    iterator = parser.iter_annotation_records(proxy)
    routes, seen, skipped = [], set(), Counter()
    codes, scene_records = Counter(), Counter()
    inspected = candidate_frames = 0
    stop = "end_of_array"
    complete = False
    try:
        for index, record in iterator:
            ids = validate_index_record(parser, record)
            inspected += 1
            scene = record["image_path"].split("/")[0]
            codes.update(str(code) for code in record["action"])
            scene_records[scene] += 1
            reason = None
            frames = 0 if ids is None else ids[-1] + 1
            if ids is None:
                reason = "unsupported_padded_action_codes"
            elif not re.fullmatch(r"env_airsim_[A-Za-z0-9_]+", scene):
                reason = "non_airsim_unreviewed_renderer"
            elif record["image_path"] in seen:
                reason = "duplicate_route_first_original_record_retained"
            elif frames > budget.max_frames_per_route:
                reason = "route_frame_cap"
            elif len(record["gpt_instruction"].encode("utf-8")) > PROMPT_CAP:
                reason = "prompt_byte_cap"
            elif candidate_frames + frames > budget.max_candidate_frames:
                stop = "candidate_frame_limit"
                break
            if reason:
                skipped[reason] += 1
            else:
                role = ROLES.get(scene, "unassigned_no_training_admission")
                routes.append(
                    {
                        "annotation_index": index,
                        "scene_id": scene,
                        "image_path": record["image_path"],
                        "parquet_path": "traj/" + record["image_path"] + ".parquet",
                        "pose_path": "Image/" + record["image_path"] + "/pose.jsonl",
                        "annotation_record_sha256": hashlib.sha256(canonical(record)).hexdigest(),
                        "prompt_original": record["gpt_instruction"],
                        "prompt_sha256": hashlib.sha256(
                            record["gpt_instruction"].encode("utf-8")
                        ).hexdigest(),
                        "annotation_image_ids": ids,
                        "annotation_filenames": record["index_list"],
                        "candidate_frame_count_from_terminal_id": frames,
                        "annotation_action_code_counts": dict(
                            sorted(Counter(str(c) for c in record["action"]).items())
                        ),
                        "raw_pose_primitive_counts": None,
                        "raw_pose_actions_verified": False,
                        "locked_development_role": role,
                        "historically_trained_scene": scene == "env_airsim_18",
                        "may_enter_train_without_new_admission": False,
                        "original_annotation_split": "train",
                    }
                )
                seen.add(record["image_path"])
                candidate_frames += frames
                if len(routes) == budget.max_routes:
                    stop = "route_limit"
                    break
                if candidate_frames == budget.max_candidate_frames:
                    stop = "candidate_frame_limit"
                    break
            if inspected == budget.max_records:
                stop = "record_limit"
                break
        else:
            complete = True
    except ByteBudgetReached:
        stop = "byte_limit"
    finally:
        iterator.close()
    if proxy.raw is None or not proxy.raw.closed:
        raise ValueError("Annotation reader did not close before source admission")
    observed_after = file_stat(plan["annotation_stat"]["path"])
    after = [small_binding(row["path"]) for row in before]
    if observed_after != plan["annotation_stat"] or after != before:
        raise ValueError("Source stat or small source bytes changed before sealing")
    prefix = {
        "bytes": proxy.raw.bytes_read,
        "sha256": proxy.raw.sha256.hexdigest(),
        "scope": "actual_binary_bytes_read_including_parser_read_ahead_not_record_boundary",
    }
    all_bytes = prefix["bytes"] == plan["historical_annotation_binding"]["bytes"]
    if all_bytes and prefix["sha256"] != plan["historical_annotation_binding"]["sha256"]:
        raise ValueError("Fully consumed small source differs from its historical SHA")
    scene_prompts, scene_codes = {}, {}
    for route in routes:
        scene = route["scene_id"]
        scene_prompts.setdefault(scene, set()).add(route["prompt_sha256"])
        scene_codes.setdefault(scene, Counter()).update(route["annotation_action_code_counts"])
    return {
        "contract": CONTRACT,
        "schema_version": 1,
        "budget": asdict(budget),
        "historical_annotation_binding": plan["historical_annotation_binding"],
        "annotation_stat_before": plan["annotation_stat"],
        "annotation_stat_after": observed_after,
        "annotation_stat_unchanged": True,
        "consumed_prefix": prefix,
        "full_source_bytes_consumed": all_bytes,
        "whole_annotation_sha256_verified_by_this_scan": all_bytes,
        "complete_array_syntax_validated": complete,
        "scan_complete": complete,
        "stop_reason": stop,
        "records_inspected": inspected,
        "unique_route_count": len(routes),
        "candidate_frame_count": candidate_frames,
        "skipped_record_counts": dict(sorted(skipped.items())),
        "scanned_annotation_action_code_counts": dict(sorted(codes.items())),
        "scanned_scene_record_counts": dict(sorted(scene_records.items())),
        "retained_scene_route_counts": dict(sorted(Counter(r["scene_id"] for r in routes).items())),
        "retained_unique_prompt_count": len({r["prompt_sha256"] for r in routes}),
        "retained_scene_unique_prompt_counts": {
            scene: len(values) for scene, values in sorted(scene_prompts.items())
        },
        "retained_scene_annotation_action_code_counts": {
            scene: dict(sorted(values.items())) for scene, values in sorted(scene_codes.items())
        },
        "routes": routes,
        "source_bindings_before": before,
        "source_bindings_after": after,
        "small_sources_unchanged": True,
        "action_histogram_scope": (
            "aggregated_annotation_codes_not_raw_pose_step_primitives_or_FRD_gold"
        ),
        "unsupported_padding_scope": (
            "typed_minus1_minus2_records_quarantined_without_routes_or_labels;"
            "schema_only_action_copy_not_original_codes_or_targets"
        ),
        "source_closure_scope": (
            "small_sources_full_SHA_plus_annotation_stat_and_consumed_prefix_not_unread_suffix_SHA"
        ),
        "selection_scope": (
            "first_distinct_eligible_routes_in_bounded_train_prefix_not_balanced_or_exhaustive"
        ),
        "official_seen_unseen_reserved_untouched": True,
        **dict.fromkeys(
            (
                "rgb_payload_available_verified",
                "raw_pose_geometry_verified",
                "scene_units_verified",
                "independent_holdout_verified",
                "training_ready",
                "optimizer_authorized",
                "model_loaded",
                "GPU_used",
                "download_performed",
                "quality_claim",
                "candidate_promoted",
                "execution_authority",
                "final_release_allowed",
            ),
            False,
        ),
    }


def output_target(value):
    path = local_path(value, exists=False)
    if (
        path.parent != ROOT / "artifacts/diagnostics"
        or not path.name.startswith("openfly-bounded-annotation-index-")
        or path.exists()
    ):
        raise ValueError("Fresh create-only bounded index diagnostics directory required")
    return path


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    mode = cli.add_mutually_exclusive_group(required=True)
    mode.add_argument("--emit-plan", action="store_true")
    mode.add_argument("--plan")
    cli.add_argument("--plan-sha256")
    cli.add_argument("--output-dir")
    cli.add_argument("--source-manifest", default=DEFAULT_MANIFEST)
    args = cli.parse_args(argv)
    try:
        if args.emit_plan:
            if args.plan_sha256 or args.output_dir:
                raise ValueError("Plan emission does not scan or publish an index")
            print(canonical(build_plan(args.source_manifest)).decode("utf-8"))
            return 0
        if not args.plan_sha256 or not args.output_dir or args.source_manifest != DEFAULT_MANIFEST:
            raise ValueError(
                "Explicit plan SHA and fresh output required; source comes from bound plan"
            )
        output = output_target(args.output_dir)
        row = small_binding(args.plan)
        if not re.fullmatch(r"[0-9a-f]{64}", args.plan_sha256) or row["sha256"] != args.plan_sha256:
            raise ValueError("Explicit plan SHA differs")
        report = scan_index(strict_json(Path(row["path"]).read_bytes()), plan_binding=row)
        raw = canonical(report) + b"\n"
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("Bounded index report byte cap exceeded")
        output.mkdir(parents=True, exist_ok=False)
        with (output / "report.json").open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        print(
            json.dumps(
                {
                    "event": "bounded_annotation_index_finished",
                    "native_exit_code": 0,
                    "report": small_binding(output / "report.json", cap=8 * 1024 * 1024),
                }
            )
        )
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "event": "bounded_annotation_index_rejected",
                    "native_exit_code": 1,
                    "failure_type": type(error).__name__,
                    "failure": str(error)[:2048],
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
