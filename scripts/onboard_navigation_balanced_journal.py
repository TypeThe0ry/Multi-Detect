"""Bounded stdlib-only validation for the new 280-draw experiment.

These checks authenticate neither model updates nor external artifacts. The caller
must first bind the schedule's bytes and regenerate its seeded source ordering.
An incomplete durable journal remains incomplete, even after synchronization.
"""

from __future__ import annotations

import io
import json
import math
import os
from collections import Counter
from pathlib import Path, PurePosixPath

SCHEDULE_CONTRACT = "multidetect.onboard-navigation-balanced-draw-schedule-v1"
JOURNAL_CONTRACT = "multidetect.onboard-navigation-balanced-optimizer-journal-v1"
STAGES = ("attempted", "returned", "synchronized", "completed")
SOURCE_COUNTS = {"forward": 70, "left": 6, "right": 7, "stop": 3}
DRAW_COUNT = 280
DRAW_FIELDS = frozenset(
    {
        "draw_index",
        "sample_id",
        "action_class",
        "class_draw_index",
        "class_cycle",
        "class_cycle_offset",
        "per_sample_exposure",
    }
)
BALANCED_OPTIMIZATION = {
    "epochs": 1,
    "max_optimizer_steps": 280,
    "max_pixels_per_image": 50176,
    "max_input_tokens": 4096,
    "max_new_tokens": 192,
    "rank": 4,
    "alpha": 8,
    "learning_rate": 2e-5,
    "seed": 20261007,
    "gradient_checkpointing": True,
    "gradient_clip_norm": 1.0,
    "max_cuda_reserved_bytes": 8053063680,
}
FALSE_FLAGS = frozenset(
    {
        "original_single_complete_epoch",
        "independent_holdout_available",
        "original_source_semantic_revalidation_performed",
        "image_decoding_performed",
        "schedule_is_actor_input",
        "schedule_classes_visible_to_actor",
        "target_labels_in_actor_input",
        "optimizer_authorized",
        "optimizer_performed",
        "training_completed",
        "model_loaded",
        "quality_claim",
        "candidate_promoted",
        "physical_performance_claimed",
        "hil_real_evidence",
        "outdoor_gnss_evidence",
        "final_release_allowed",
        "execution_authority",
        "transport_created",
        "mission_created",
    }
)
TRUE_FLAGS = frozenset(
    {
        "source_bindings_reverified_before_publication",
        "draw_count_exceeds_original_max_optimizer_steps",
        "new_training_plan_required",
        "resubstitution_only",
        "source_planned_not_executed",
        "source_byte_bindings_and_structure_verified",
    }
)
SCHEDULE_FIELDS = (
    FALSE_FLAGS
    | TRUE_FLAGS
    | {
        "schema_version",
        "contract",
        "status",
        "source_plan",
        "dataset_samples",
        "source_bindings",
        "seed",
        "sampling_algorithm",
        "source_class_counts",
        "draw_class_counts",
        "source_unique_samples",
        "draw_count",
        "unique_draw_samples",
        "per_sample_exposure",
        "scene_count",
        "scene_ids",
        "episode_count",
        "partition_counts",
        "original_optimizer_budget",
        "actor_input",
        "draws",
    }
)
MEASUREMENT_FIELDS = {
    "attempted": frozenset({"loss", "gradient_norm"}),
    "returned": frozenset(),
    "synchronized": frozenset(),
    "completed": frozenset({"elapsed_s", "peak_allocated_bytes", "peak_reserved_bytes"}),
}
MAX_LINE_BYTES = 2048
MAX_EVENTS = DRAW_COUNT * len(STAGES)
MAX_JOURNAL_BYTES = MAX_LINE_BYTES * MAX_EVENTS


def _fields(value, expected, name):
    if type(value) is not dict or set(value) != expected:
        raise ValueError(f"{name} fields differ")


def _integer(value, minimum, maximum, name):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be a bounded integer, not bool")


def _number(value, maximum, name):
    if type(value) not in (int, float) or not 0 <= value <= maximum or not math.isfinite(value):
        raise ValueError(f"{name} must be a bounded finite nonnegative number, not bool")


def _identifier(value, name):
    if (
        type(value) is not str
        or not 1 <= len(value) <= 128
        or any(ord(c) < 33 or ord(c) > 126 for c in value)
    ):
        raise ValueError(f"{name} must be a bounded printable identifier")


def _draw(row):
    _fields(row, DRAW_FIELDS, "Draw")
    _identifier(row["sample_id"], "sample_id")
    if type(row["action_class"]) is not str or row["action_class"] not in SOURCE_COUNTS:
        raise ValueError("Unknown action class")
    for key in DRAW_FIELDS - {"sample_id", "action_class"}:
        _integer(row[key], 1, DRAW_COUNT, key)


def _draws(draws):
    if type(draws) is not list or len(draws) != DRAW_COUNT:
        raise ValueError("The bound schedule requires exactly 280 draws")
    class_counts, exposures = Counter(), Counter()
    class_pools = {name: set() for name in SOURCE_COUNTS}
    cycle_ids, sample_classes = {}, {}
    for index, row in enumerate(draws, 1):
        _draw(row)
        name, sample_id = row["action_class"], row["sample_id"]
        class_counts[name] += 1
        exposures[sample_id] += 1
        source_count = SOURCE_COUNTS[name]
        class_index = class_counts[name]
        cycle = (class_index - 1) // source_count + 1
        offset = (class_index - 1) % source_count + 1
        if (
            row["draw_index"] != index
            or row["class_draw_index"] != class_index
            or row["class_cycle"] != cycle
            or row["class_cycle_offset"] != offset
            or row["per_sample_exposure"] != exposures[sample_id]
        ):
            raise ValueError("Draw order, class cycle, or per-sample exposure differs")
        if sample_id in sample_classes and sample_classes[sample_id] != name:
            raise ValueError("One sample cannot change action class")
        sample_classes[sample_id] = name
        class_pools[name].add(sample_id)
        seen = cycle_ids.setdefault((name, cycle), set())
        if sample_id in seen:
            raise ValueError("A class cycle cannot repeat a sample")
        seen.add(sample_id)
    if dict(class_counts) != dict.fromkeys(SOURCE_COUNTS, 70):
        raise ValueError("Each action class requires exactly 70 draws")
    if {name: len(pool) for name, pool in class_pools.items()} != SOURCE_COUNTS:
        raise ValueError("The schedule must preserve all 86 original class identities")
    for pool in class_pools.values():
        values = [exposures[sample_id] for sample_id in pool]
        if max(values) - min(values) > 1:
            raise ValueError("Per-class sample exposures are unbalanced")
    return class_counts, exposures


def _budget(optimization, expected):
    _fields(optimization, set(expected), "Optimization budget")
    if any(
        type(optimization[key]) is not type(value) or optimization[key] != value
        for key, value in expected.items()
    ):
        raise ValueError("Only the exact declared balanced optimization values are admitted")


def validate_balanced_budget(optimization, draws):
    """Admit the NEW budget; never apply the old 100-step budget to 280 draws."""
    _budget(optimization, BALANCED_OPTIMIZATION)
    _draws(draws)
    return optimization


def _binding(row):
    _fields(row, {"path", "bytes", "sha256"}, "Source binding")
    path, digest = row["path"], row["sha256"]
    if (
        type(path) is not str
        or not 1 <= len(path) <= 1024
        or "\\" in path
        or ":" in path
        or PurePosixPath(path).is_absolute()
        or any(x in ("", ".", "..") for x in path.split("/"))
        or any(ord(c) < 32 for c in path)
    ):
        raise ValueError("Source binding requires a repository-relative path")
    _integer(row["bytes"], 1, 2**63 - 1, "Source bytes")
    if (
        type(digest) is not str
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise ValueError("Source binding requires a lowercase SHA-256")


def validate_balanced_schedule(schedule):
    """Validate frozen-schedule structure, identities, cycles and non-authority."""
    _fields(schedule, SCHEDULE_FIELDS, "Schedule")
    expected = {
        "schema_version": 1,
        "contract": SCHEDULE_CONTRACT,
        "status": "balanced_draw_schedule_prepared_not_optimizer_authorized_or_executed",
        "seed": 20261007,
        "sampling_algorithm": "seeded_class_cycles_then_shuffled_class_slots_v1",
        "source_unique_samples": 86,
        "draw_count": DRAW_COUNT,
        "unique_draw_samples": 86,
        "scene_count": 1,
        "episode_count": 3,
        "actor_input": "unchanged_original_prompt_current_and_at_most_latest_past_rgb",
        **dict.fromkeys(FALSE_FLAGS, False),
        **dict.fromkeys(TRUE_FLAGS, True),
    }
    for key, value in expected.items():
        if type(schedule[key]) is not type(value) or schedule[key] != value:
            raise ValueError(f"Schedule {key} differs")
    for key, expected_counts in (
        ("source_class_counts", SOURCE_COUNTS),
        ("draw_class_counts", dict.fromkeys(SOURCE_COUNTS, 70)),
        ("partition_counts", {"train": 86}),
    ):
        _fields(schedule[key], set(expected_counts), key)
        if any(
            type(schedule[key][k]) is not int or schedule[key][k] != v
            for k, v in expected_counts.items()
        ):
            raise ValueError(f"Schedule {key} differs")
    scenes = schedule["scene_ids"]
    if type(scenes) is not list or len(scenes) != 1:
        raise ValueError("The original schedule contains one scene")
    _identifier(scenes[0], "scene_id")
    _binding(schedule["source_plan"])
    _binding(schedule["dataset_samples"])
    bindings = schedule["source_bindings"]
    if type(bindings) is not list or not 1 <= len(bindings) <= 10000:
        raise ValueError("Source bindings must be a bounded nonempty list")
    by_path = {}
    for row in bindings:
        _binding(row)
        if row["path"] in by_path:
            raise ValueError("Duplicate source binding path")
        by_path[row["path"]] = row
    for name in ("source_plan", "dataset_samples"):
        row = schedule[name]
        if by_path.get(row["path"]) != row:
            raise ValueError("Plan and dataset must occur exactly in the source bindings")
    _budget(
        schedule["original_optimizer_budget"],
        {
            **BALANCED_OPTIMIZATION,
            "max_optimizer_steps": 100,
            "gradient_clip_norm": 1,
        },
    )
    _, exposures = _draws(schedule["draws"])
    _fields(schedule["per_sample_exposure"], set(exposures), "Exposure summary")
    if any(
        type(schedule["per_sample_exposure"][key]) is not int
        or schedule["per_sample_exposure"][key] != value
        for key, value in exposures.items()
    ):
        raise ValueError("Exposure summary differs from the bound draws")
    return schedule


def _event(row):
    if type(row) is not dict or type(row.get("stage")) is not str or row["stage"] not in STAGES:
        raise ValueError("Unknown journal stage")
    stage = row["stage"]
    _fields(
        row,
        DRAW_FIELDS | {"schema_version", "contract", "stage"} | MEASUREMENT_FIELDS[stage],
        "Journal event",
    )
    if type(row["schema_version"]) is not int or row["schema_version"] != 1:
        raise ValueError("Journal schema_version differs")
    if row["contract"] != JOURNAL_CONTRACT:
        raise ValueError("Journal contract differs")
    _draw({key: row[key] for key in DRAW_FIELDS})
    if stage == "attempted":
        _number(row["loss"], 1e6, "loss")
        _number(row["gradient_norm"], 1e12, "gradient_norm")
    elif stage == "completed":
        _number(row["elapsed_s"], 86400, "elapsed_s")
        for key in ("peak_allocated_bytes", "peak_reserved_bytes"):
            _integer(row[key], 0, BALANCED_OPTIMIZATION["max_cuda_reserved_bytes"], key)
        if row["peak_allocated_bytes"] > row["peak_reserved_bytes"]:
            raise ValueError("Allocated bytes exceed reserved bytes")


def append_journal_event(stream, draw, stage, **bounded_measurements):
    """Write one complete line and fsync it BEFORE the caller's next stage."""
    _draw(draw)
    if type(stage) is not str or stage not in STAGES:
        raise ValueError("Unknown journal stage")
    if set(bounded_measurements) != MEASUREMENT_FIELDS[stage]:
        raise ValueError("Journal measurement fields differ")
    row = {
        "schema_version": 1,
        "contract": JOURNAL_CONTRACT,
        **draw,
        "stage": stage,
        **bounded_measurements,
    }
    _event(row)
    payload = (
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    )
    encoded = payload.encode("utf-8")
    if len(encoded) > MAX_LINE_BYTES:
        raise ValueError("Journal line exceeds its bounded cap")
    binary = not isinstance(stream, io.TextIOBase) and "b" in getattr(stream, "mode", "")
    data = encoded if binary else payload
    if stream.write(data) != len(data):
        raise OSError("Journal event was not fully written")
    stream.flush()
    os.fsync(stream.fileno())
    return row


def _strict_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key in journal")
            result[key] = value
        return result

    def reject(value):
        raise ValueError(f"Non-finite JSON constant in journal: {value}")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=reject)


def verify_journal(path, draws, *, require_complete=False):
    """Verify a durable prefix of exact 4-stage draws; absent files are errors.

    Counters describe observed valid events only. Attempted-but-not-completed
    draws always retain update uncertainty; no missing measurement is invented.
    """
    _draws(draws)
    if type(require_complete) is not bool:
        raise ValueError("require_complete must be bool")
    path = Path(path)
    if path.stat().st_size > MAX_JOURNAL_BYTES:
        raise ValueError("Journal exceeds its bounded size cap")
    counts = dict.fromkeys(STAGES, 0)
    event_count, last_phase = 0, None
    with path.open("rb") as stream:
        while raw := stream.readline(MAX_LINE_BYTES + 1):
            if len(raw) > MAX_LINE_BYTES or not raw.endswith(b"\n"):
                raise ValueError("Journal line is oversized or truncated")
            if b"\r" in raw or not raw.strip():
                raise ValueError("Blank or non-LF journal line")
            if event_count >= MAX_EVENTS:
                raise ValueError("Journal contains events beyond its 280-draw budget")
            row = _strict_json(raw)
            _event(row)
            draw_index, phase = divmod(event_count, len(STAGES))
            if row["stage"] != STAGES[phase]:
                raise ValueError("Journal stage order differs")
            if {key: row[key] for key in DRAW_FIELDS} != draws[draw_index]:
                raise ValueError("Journal event differs from its exact bound scheduled draw")
            counts[row["stage"]] += 1
            event_count += 1
            last_phase = row["stage"]
    complete = all(value == DRAW_COUNT for value in counts.values())
    if require_complete and not complete:
        raise ValueError("Journal does not prove all 280 completed draws")
    return {
        **{f"optimizer_steps_{stage}": value for stage, value in counts.items()},
        "event_count": event_count,
        "last_phase": last_phase,
        "optimizer_update_uncertain": counts["attempted"] != counts["completed"],
        "complete": complete,
    }


read_journal = verify_journal
