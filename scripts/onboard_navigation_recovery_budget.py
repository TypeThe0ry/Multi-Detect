"""NEW coverage-first recovery accounting, never a trainer or execution grant.

The caller must admit real train-only sources, targets, classifier and the
sealed failed-attempt receipt before using this contract. Old failed13 updates
remain a separate spent ledger, not checkpoint state or copied journal events.
This schedules a fresh optimizer from the original warm adapter, not exact resume.
No artifact, tensor, processor, model, GPU, or optimizer is loaded here.
"""

from __future__ import annotations

import json
import random
import re
from collections import Counter
from dataclasses import asdict, dataclass, field

CONTRACT = "multidetect.onboard-navigation-recovery-coverage-first-schedule-v1"
CLASSES = ("forward", "left", "right", "stop")
SOURCE_COUNTS = dict(zip(CLASSES, (70, 6, 7, 3), strict=True))
CLASS_QUOTAS = dict(zip(CLASSES, (70, 66, 66, 65), strict=True))
FALSE_CLAIMS = (
    "exact_resume",
    "optimizer_state_restored",
    "scheduler_state_restored",
    "rng_state_restored",
    "prior_journal_copied",
    "schedule_is_actor_input",
    "target_labels_in_actor_input",
    "optimizer_authorized",
    "optimizer_performed",
    "training_completed",
    "model_loaded",
    "quality_claim",
    "candidate_promoted",
    "execution_authority",
    "final_release_allowed",
)


@dataclass(frozen=True)
class RecoveryTrainingBudget:
    spent_optimizer_attempts: int = 13
    campaign_attempt_cap: int = 280
    max_optimizer_steps: int = 267
    per_class_quotas: dict[str, int] = field(default_factory=lambda: CLASS_QUOTAS.copy())
    seed: int = 20261008

    def validate(self):
        for name, expected in (
            ("spent_optimizer_attempts", 13),
            ("campaign_attempt_cap", 280),
            ("max_optimizer_steps", 267),
            ("seed", 20261008),
        ):
            value = getattr(self, name)
            if type(value) is not int or value != expected:
                raise ValueError(f"Recovery {name} differs from the explicit budget")
        quotas = self.per_class_quotas
        if (
            type(quotas) is not dict
            or tuple(quotas) != CLASSES
            or any(
                type(quotas[key]) is not int or quotas[key] != CLASS_QUOTAS[key] for key in CLASSES
            )
        ):
            raise ValueError("Recovery per-class quotas differ from coverage-first 70/66/66/65")
        if sum(quotas.values()) != self.max_optimizer_steps or (
            self.spent_optimizer_attempts + self.max_optimizer_steps != self.campaign_attempt_cap
        ):
            raise ValueError("Recovery quota or cumulative budget accounting differs")
        return self

    def validate_attempts(self, new_attempts):
        """Count attempts conservatively, including an uncertain unfinished step."""
        self.validate()
        if type(new_attempts) is not int or not 0 <= new_attempts <= self.max_optimizer_steps:
            raise ValueError("NEW optimizer attempts exceed the remaining campaign budget")
        total = self.spent_optimizer_attempts + new_attempts
        if total > self.campaign_attempt_cap:
            raise ValueError("Cumulative optimizer attempts exceed the campaign cap")
        return total


def _sources(pools, targets, classify):
    if type(pools) is not dict or tuple(pools) != CLASSES:
        raise ValueError("Source pools require the exact ordered four-class mapping")
    sources, ids = {}, []
    for name in CLASSES:
        values = pools[name]
        if type(values) not in (list, tuple) or len(values) != SOURCE_COUNTS[name]:
            raise ValueError("Source pool counts must remain exactly 70/6/7/3")
        if any(
            type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in values
        ):
            raise ValueError("Every source sample ID must be a canonical lowercase SHA-256")
        sources[name] = list(values)
        ids.extend(values)
    if len(set(ids)) != 86:
        raise ValueError("All 86 source IDs must be unique across every class")
    if type(targets) is not dict or set(targets) != set(ids) or not callable(classify):
        raise ValueError("Complete exact targets and an explicit classifier are required")
    for name, values in sources.items():
        for sample_id in values:
            actual = classify(targets[sample_id])
            if type(actual) is not str or actual != name:
                raise ValueError("Declared source class differs from the supplied semantic target")
    return sources


def _budget(budget):
    if budget is None:
        budget = RecoveryTrainingBudget()
    if type(budget) is not RecoveryTrainingBudget:
        raise TypeError("An explicit RecoveryTrainingBudget is required")
    return budget.validate()


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def build_recovery_draws(pools, *, targets, classify, budget=None):
    """Return a NEW in-memory schedule with complete first-cycle source coverage.

    Pools preserve the caller's admitted semantic source order. Each class is
    cycled with seeded shuffles; rounds interleave forward/left/right/stop while
    skipping exhausted quotas. Local exposure never inherits failed-run state.
    """
    budget = _budget(budget)
    sources = _sources(pools, targets, classify)
    rng, queues = random.Random(budget.seed), {}
    for name in CLASSES:
        queue, cycle = [], 0
        while len(queue) < budget.per_class_quotas[name]:
            cycle += 1
            values = sources[name].copy()
            rng.shuffle(values)
            for offset, sample_id in enumerate(values, 1):
                if len(queue) == budget.per_class_quotas[name]:
                    break
                queue.append((sample_id, cycle, offset))
        queues[name] = queue
    draws, exposures = [], Counter()
    for round_index in range(max(budget.per_class_quotas.values())):
        for name in CLASSES:
            if round_index >= len(queues[name]):
                continue
            sample_id, cycle, offset = queues[name][round_index]
            exposures[sample_id] += 1
            draws.append(
                {
                    "local_draw_index": len(draws) + 1,
                    "sample_id": sample_id,
                    "action_class": name,
                    "class_draw_index": round_index + 1,
                    "class_cycle": cycle,
                    "class_cycle_offset": offset,
                    "local_exposure": exposures[sample_id],
                }
            )
    budget.validate_attempts(len(draws))
    return {
        "schema_version": 1,
        "contract": CONTRACT,
        "status": "coverage_first267_prepared_not_optimizer_authorized_or_executed",
        "continuation_kind": "original_warm_adapter_with_fresh_optimizer_not_exact_resume",
        "sampling_algorithm": "seeded_class_cycles_round_interleave_v1",
        "sources": sources,
        "source_class_counts": SOURCE_COUNTS.copy(),
        "optimization": asdict(budget),
        "draw_count": len(draws),
        "unique_draw_samples": len(exposures),
        "draw_class_counts": budget.per_class_quotas.copy(),
        "per_sample_exposure": dict(sorted(exposures.items())),
        "draws": draws,
        "old_failed_updates_accounted_separately": True,
        **dict.fromkeys(FALSE_CLAIMS, False),
    }


def validate_recovery_schedule(schedule, pools, *, targets, classify, budget=None):
    """Regenerate exact source order, fields, rounds, cycles and local exposure.

    This admits only the NEW267 contract, never a suffix of the old280 journal.
    No source/receipt byte authentication or optimizer execution is implied.
    """
    expected = build_recovery_draws(pools, targets=targets, classify=classify, budget=budget)
    if type(schedule) is not dict:
        raise ValueError("Recovery schedule must be the complete mapping")
    _sources(schedule.get("sources"), targets, classify)
    if _canonical(schedule) != _canonical(expected):
        raise ValueError("Recovery schedule differs from exact declared sources, order or contract")
    return schedule
