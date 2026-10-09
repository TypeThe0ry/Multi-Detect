"""Create an auditable train-only draw schedule, never authorize or run training."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import random
import sys
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAN_SHA256 = "f4d76902c220812201f2ca4e961b0f427e4378ba6ee5449bf96e86be23999c53"
CONTRACT = "multidetect.onboard-navigation-balanced-draw-schedule-v1"
CLASSES = ("forward", "left", "right", "stop")
SOURCE_COUNTS = {"forward": 70, "left": 6, "right": 7, "stop": 3}
DRAWS_PER_CLASS = 70


def canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def binding(path, root):
    path = Path(path).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Source file escaped the repository")
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def verify_sources(rows, root):
    unique = {}
    for row in rows:
        if set(row) != {"path", "bytes", "sha256"}:
            raise ValueError("Source binding fields differ")
        path = (root / row["path"]).resolve(strict=True)
        actual = binding(path, root)
        if (actual["bytes"], actual["sha256"]) != (row["bytes"], row["sha256"]):
            raise ValueError("Source bytes or SHA changed")
        if path in unique and unique[path] != actual:
            raise ValueError("A source has conflicting bindings")
        unique[path] = actual
    return list(unique.values())


@contextmanager
def frozen_pilot(plan):
    """The pinned source imports remain standard-library-only; restore local paths."""
    root = Path(plan["artifact_root"]).resolve(strict=True)
    verify_sources(plan["source_files"], root)
    original_path = sys.path[:]
    try:
        for path in (root, root / "src"):
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))
        pilot = importlib.import_module("scripts.train_onboard_vlm_navigation_pilot")
        if Path(pilot.__file__).resolve() != root / "scripts/train_onboard_vlm_navigation_pilot.py":
            raise ValueError("Frozen plan loader import originated outside the bound source")
        yield pilot
    finally:
        sys.path[:] = original_path


def build_draws(samples, targets, expected_ids, *, seed, classify):
    """Sample IDs only: the labels used for balancing never enter actor messages."""
    if type(seed) is not int or seed != 20261007:
        raise ValueError("Only the declared original seed is admitted")
    ids = [sample.sample_id for sample in samples]
    if len(ids) != 86 or len(set(ids)) != 86 or ids != list(expected_ids):
        raise ValueError("All 86 unique original source IDs must be preserved in source order")
    if set(targets) != set(ids):
        raise ValueError("Targets must cover exactly the original source IDs")
    pools = {name: [] for name in CLASSES}
    for sample in samples:
        if sample.partition != "train" or sample.source.original_split != "train":
            raise ValueError("Non-train or original holdout data cannot enter this schedule")
        action_class = classify(targets[sample.sample_id])
        if action_class not in pools:
            raise ValueError("Unknown action class cannot be balanced by guessing")
        pools[action_class].append(sample.sample_id)
    if {name: len(pool) for name, pool in pools.items()} != SOURCE_COUNTS:
        raise ValueError("Original forward/left/right/stop source counts differ")
    if len({sample.scene_id for sample in samples}) != 1:
        raise ValueError("This original pilot contains exactly one scene")
    rng = random.Random(seed)
    queues = {name: [] for name in CLASSES}
    for action_class in CLASSES:
        cycle = 0
        while len(queues[action_class]) < DRAWS_PER_CLASS:
            cycle += 1
            pool = pools[action_class][:]
            rng.shuffle(pool)
            for offset, sample_id in enumerate(pool, 1):
                if len(queues[action_class]) == DRAWS_PER_CLASS:
                    break
                queues[action_class].append((sample_id, cycle, offset))
    class_slots = [name for name in CLASSES for _ in range(DRAWS_PER_CLASS)]
    rng.shuffle(class_slots)
    class_draws, exposures = Counter(), Counter()
    draws = []
    for draw_index, action_class in enumerate(class_slots, 1):
        sample_id, cycle, offset = queues[action_class][class_draws[action_class]]
        class_draws[action_class] += 1
        exposures[sample_id] += 1
        draws.append(
            {
                "draw_index": draw_index,
                "action_class": action_class,
                "sample_id": sample_id,
                "class_draw_index": class_draws[action_class],
                "class_cycle": cycle,
                "class_cycle_offset": offset,
                "per_sample_exposure": exposures[sample_id],
            }
        )
    if set(exposures) != set(ids):
        raise ValueError("Balanced scheduling must retain every unique original sample")
    return draws, dict(sorted(exposures.items()))


def build_schedule(plan_path, plan_sha256, output_directory):
    if plan_sha256 != PLAN_SHA256:
        raise ValueError("Only the exact sealed original pilot plan is admitted")
    raw = Path(plan_path).read_bytes()
    if len(raw) > 2 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != PLAN_SHA256:
        raise ValueError("Plan SHA or size changed")
    original = json.loads(raw)
    root = Path(original["artifact_root"]).resolve(strict=True)
    if root != ROOT:
        raise ValueError("Original artifact_root differs from this repository")
    output = Path(output_directory).resolve()
    training_root = (root / "artifacts/training").resolve(strict=True)
    if output == training_root or not output.is_relative_to(training_root):
        raise ValueError("Schedule output must be a new directory within artifacts/training")
    if output.exists():
        raise FileExistsError("Existing output directories are never reused or overwritten")
    if not output.parent.is_dir():
        raise ValueError("Output parent must already exist")
    with frozen_pilot(original) as pilot:
        plan = pilot.load_plan(plan_path, PLAN_SHA256)
        records = pilot.plan_bindings(plan)
        records += [binding(plan_path, root)]
        records += [
            binding(__file__, root),
            binding(root / "tests/test_build_onboard_navigation_balanced_schedule.py", root),
        ]
        dataset = pilot.verify_binding(plan["dataset_samples"], root)
        if dataset.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("Source sample file exceeds the bounded pilot cap")
        with dataset.open("rb") as stream:
            samples = tuple(
                pilot.navigation.NavigationSupervisionSample.from_json(line) for line in stream
            )
        pilot.navigation.validate_navigation_episode_samples(samples)
        targets = {}
        for sample in samples:
            action = sample.planned_action
            targets[sample.sample_id] = {
                "schema_version": 1,
                "contract": pilot.navigation.TARGET_CONTRACT,
                "kind": action.kind,
                "translation_frame": "body_frd",
                "yaw_convention": "clockwise_right_positive",
                "translation_m": None
                if action.translation_m is None
                else list(action.translation_m),
                "yaw_delta_rad": action.yaw_delta_rad,
            }
            for value in sample.source.to_mapping().values():
                if isinstance(value, dict) and set(value) == {"path", "bytes", "sha256"}:
                    records.append(value)
            records += [
                {"path": frame.path, "bytes": frame.bytes, "sha256": frame.sha256}
                for frame in sample.frames
            ]
        source_bindings = verify_sources(records, root)
        draws, exposures = build_draws(
            samples,
            targets,
            plan["train_sample_ids"],
            seed=plan["optimization"]["seed"],
            classify=pilot.action_class,
        )
        report = pilot.read_bound_json(plan["dataset_report"], root)
        review = pilot.read_bound_json(plan["review_receipt"], root)
        if (
            report["dataset"]["sha256"] != plan["dataset_samples"]["sha256"]
            or report["admitted_sample_count"] != 86
            or review["dataset_samples_sha256"] != plan["dataset_samples"]["sha256"]
            or review["dataset_report_sha256"] != plan["dataset_report"]["sha256"]
            or review["allowed_training_use"]
            != "planned_navigation_train_only_resubstitution_pilot"
            or review["independent_quality_evidence"] is not False
        ):
            raise ValueError("Original source review and report do not bind this train-only pilot")
        if verify_sources(source_bindings, root) != source_bindings:
            raise ValueError("Source bindings changed before publication")
        document = {
            "schema_version": 1,
            "contract": CONTRACT,
            "status": "balanced_draw_schedule_prepared_not_optimizer_authorized_or_executed",
            "source_plan": binding(plan_path, root),
            "dataset_samples": plan["dataset_samples"],
            "source_bindings": source_bindings,
            "source_bindings_reverified_before_publication": True,
            "seed": plan["optimization"]["seed"],
            "sampling_algorithm": "seeded_class_cycles_then_shuffled_class_slots_v1",
            "source_class_counts": SOURCE_COUNTS,
            "draw_class_counts": dict(Counter(row["action_class"] for row in draws)),
            "source_unique_samples": 86,
            "draw_count": len(draws),
            "unique_draw_samples": len(exposures),
            "per_sample_exposure": exposures,
            "scene_count": 1,
            "scene_ids": sorted({sample.scene_id for sample in samples}),
            "episode_count": len({sample.episode_id for sample in samples}),
            "partition_counts": {"train": 86},
            "original_single_complete_epoch": False,
            "original_optimizer_budget": plan["optimization"],
            "draw_count_exceeds_original_max_optimizer_steps": len(draws)
            > plan["optimization"]["max_optimizer_steps"],
            "new_training_plan_required": True,
            "resubstitution_only": True,
            "independent_holdout_available": False,
            "source_planned_not_executed": True,
            "source_byte_bindings_and_structure_verified": True,
            "original_source_semantic_revalidation_performed": False,
            "image_decoding_performed": False,
            "actor_input": "unchanged_original_prompt_current_and_at_most_latest_past_rgb",
            "schedule_is_actor_input": False,
            "schedule_classes_visible_to_actor": False,
            "target_labels_in_actor_input": False,
            "draws": draws,
            "optimizer_authorized": False,
            "optimizer_performed": False,
            "training_completed": False,
            "model_loaded": False,
            "quality_claim": False,
            "candidate_promoted": False,
            "physical_performance_claimed": False,
            "hil_real_evidence": False,
            "outdoor_gnss_evidence": False,
            "final_release_allowed": False,
            "execution_authority": False,
            "transport_created": False,
            "mission_created": False,
        }
        payload = canonical_bytes(document) + b"\n"
        output.mkdir(exist_ok=False)
        with (output / "schedule.json").open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    return document, binding(output / "schedule.json", root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        document, output = build_schedule(args.plan, args.plan_sha256, args.output_dir)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "balanced_schedule_rejected",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            ),
            flush=True,
        )
        return 1
    print(
        json.dumps(
            {
                "event": "balanced_schedule_created",
                "output": output,
                "draw_count": document["draw_count"],
                "draw_class_counts": document["draw_class_counts"],
                "unique_draw_samples": document["unique_draw_samples"],
                "optimizer_authorized": False,
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
