"""Synthetic SHA IDs/targets exercise software contracts, not actual training."""

from __future__ import annotations

import copy
import hashlib
import json
import random
import subprocess
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import onboard_navigation_recovery_budget as recovery


def fixtures():
    pools, targets = {}, {}
    for name, count in recovery.SOURCE_COUNTS.items():
        pools[name] = [
            hashlib.sha256(f"fixture:{name}:{i}".encode()).hexdigest() for i in range(count)
        ]
        targets.update({sample_id: {"fixture_class": name} for sample_id in pools[name]})
    return pools, targets


def classify(target):
    return target["fixture_class"]


def build(pools=None, targets=None, **options):
    if pools is None:
        pools, targets = fixtures()
    return recovery.build_recovery_draws(pools, targets=targets, classify=classify, **options)


def test_exact_recovery_budget_and_conservative_cumulative_attempts():
    budget = recovery.RecoveryTrainingBudget()
    assert budget.validate() is budget
    assert [budget.validate_attempts(n) for n in (0, 1, 13, 266, 267)] == [13, 14, 26, 279, 280]
    assert budget.per_class_quotas == {"forward": 70, "left": 66, "right": 66, "stop": 65}
    second = recovery.RecoveryTrainingBudget()
    second.per_class_quotas["forward"] = 69
    assert budget.per_class_quotas["forward"] == 70
    with pytest.raises(ValueError):
        second.validate()


@pytest.mark.parametrize(
    "changes",
    [
        {"spent_optimizer_attempts": 0},
        {"spent_optimizer_attempts": 12},
        {"spent_optimizer_attempts": 14},
        {"spent_optimizer_attempts": True},
        {"spent_optimizer_attempts": 13.0},
        {"campaign_attempt_cap": 281},
        {"campaign_attempt_cap": 293},
        {"campaign_attempt_cap": 280.0},
        {"max_optimizer_steps": 268},
        {"max_optimizer_steps": 280},
        {"max_optimizer_steps": True},
        {"seed": 20261007},
        {"seed": 20261008.0},
        {"seed": True},
    ],
)
def test_budget_rejects_reset_extra_steps_and_type_confusion(changes):
    budget = replace(recovery.RecoveryTrainingBudget(), **changes)
    with pytest.raises(ValueError):
        build(budget=budget)


@pytest.mark.parametrize("attempts", [-1, 268, 280, True, 13.0, "13", None])
def test_no_extra_or_unknown_new_optimizer_attempt_is_admitted(attempts):
    with pytest.raises(ValueError):
        recovery.RecoveryTrainingBudget().validate_attempts(attempts)


@pytest.mark.parametrize("change", ["missing", "extra", "order", "bool", "near_balanced", "list"])
def test_only_declared_coverage_first_quota_mapping_is_admitted(change):
    quotas = recovery.CLASS_QUOTAS.copy()
    if change == "missing":
        del quotas["stop"]
    elif change == "extra":
        quotas["up"] = 0
    elif change == "order":
        quotas = dict(reversed(list(quotas.items())))
    elif change == "bool":
        quotas["stop"] = True
    elif change == "near_balanced":
        quotas = dict(zip(recovery.CLASSES, (67, 67, 67, 66), strict=True))
    else:
        quotas = list(quotas.values())
    with pytest.raises(ValueError):
        build(budget=replace(recovery.RecoveryTrainingBudget(), per_class_quotas=quotas))


def test_deterministic_round_interleave_cycles_all86_and_fresh_exposure():
    pools, targets = fixtures()
    before = copy.deepcopy((pools, targets))
    rng_state = random.getstate()
    schedule = build(pools, targets)
    assert build(pools, targets) == schedule
    assert (pools, targets) == before and random.getstate() == rng_state
    assert (
        recovery.validate_recovery_schedule(schedule, pools, targets=targets, classify=classify)
        is schedule
    )
    rows = schedule["draws"]
    assert len(rows) == schedule["draw_count"] == 267
    assert schedule["unique_draw_samples"] == len(schedule["per_sample_exposure"]) == 86
    assert Counter(row["action_class"] for row in rows) == recovery.CLASS_QUOTAS
    assert [row["local_draw_index"] for row in rows] == list(range(1, 268))
    expected_classes = [
        name
        for round_index in range(70)
        for name in recovery.CLASSES
        if round_index < recovery.CLASS_QUOTAS[name]
    ]
    assert [row["action_class"] for row in rows] == expected_classes
    exposures = Counter()
    for row in rows:
        exposures[row["sample_id"]] += 1
        assert row["local_exposure"] == exposures[row["sample_id"]]
        assert set(row) == {
            "local_draw_index",
            "sample_id",
            "action_class",
            "class_draw_index",
            "class_cycle",
            "class_cycle_offset",
            "local_exposure",
        }
    assert dict(exposures) == schedule["per_sample_exposure"]
    for name, pool in pools.items():
        group = [row for row in rows if row["action_class"] == name]
        assert {row["sample_id"] for row in group[: len(pool)]} == set(pool)
        assert [row["class_draw_index"] for row in group] == list(range(1, len(group) + 1))
        for index, row in enumerate(group):
            assert row["class_cycle"] == index // len(pool) + 1
            assert row["class_cycle_offset"] == index % len(pool) + 1
        for start in range(0, len(group), len(pool)):
            cycle = group[start : start + len(pool)]
            assert len({row["sample_id"] for row in cycle}) == len(cycle)
        values = [exposures[sample_id] for sample_id in pool]
        assert max(values) - min(values) <= 1
    assert {exposures[sid] for sid in pools["forward"]} == {1}
    assert {exposures[sid] for sid in pools["left"]} == {11}
    assert {exposures[sid] for sid in pools["right"]} == {9, 10}
    assert {exposures[sid] for sid in pools["stop"]} == {21, 22}
    assert schedule["old_failed_updates_accounted_separately"] is True
    assert all(schedule[key] is False for key in recovery.FALSE_CLAIMS)


@pytest.mark.parametrize(
    "change",
    [
        "count",
        "duplicate",
        "cross_class",
        "class_order",
        "missing",
        "extra",
        "target_class",
        "target_missing",
        "target_extra",
    ],
)
def test_sources_and_semantic_targets_must_match_exactly(change):
    pools, targets = fixtures()
    if change == "count":
        pools["forward"].pop()
    elif change == "duplicate":
        pools["forward"][1] = pools["forward"][0]
    elif change == "cross_class":
        pools["left"][0] = pools["forward"][0]
    elif change == "class_order":
        pools = dict(reversed(list(pools.items())))
    elif change == "missing":
        del pools["stop"]
    elif change == "extra":
        pools["up"] = []
    elif change == "target_class":
        targets[pools["forward"][0]]["fixture_class"] = "left"
    elif change == "target_missing":
        del targets[pools["stop"][0]]
    else:
        targets["f" * 64] = {"fixture_class": "stop"}
    with pytest.raises(ValueError):
        build(pools, targets)


@pytest.mark.parametrize("value", ["forward-0", "A" * 64, "a" * 63, "a" * 65, True, 1, None])
def test_sample_identifiers_are_strict_sha256_values(value):
    pools, targets = fixtures()
    pools["forward"][0] = value
    with pytest.raises(ValueError, match="SHA-256"):
        build(pools, targets)


@pytest.mark.parametrize(
    "change",
    [
        "reorder_draws",
        "local_index",
        "exposure",
        "cycle",
        "suffix_field",
        "bool",
        "nonfinite",
        "source_order",
        "source_extra",
        "extra_top",
        "resume",
        "restore",
        "authorize",
        "old_contract",
        "draw_extra",
        "missing_ID",
    ],
)
def test_validator_rejects_any_declared_source_or_draw_contract_tamper(change):
    pools, targets = fixtures()
    schedule = build(pools, targets)
    if change == "reorder_draws":
        schedule["draws"][0], schedule["draws"][1] = schedule["draws"][1], schedule["draws"][0]
    elif change == "local_index":
        schedule["draws"][0]["local_draw_index"] = 14
    elif change == "exposure":
        schedule["draws"][0]["local_exposure"] = 2
    elif change == "cycle":
        schedule["draws"][0]["class_cycle"] = 2
    elif change == "suffix_field":
        schedule["draws"][0]["source_draw_index"] = 14
    elif change == "bool":
        schedule["schema_version"] = True
    elif change == "nonfinite":
        schedule["draw_count"] = float("nan")
    elif change == "source_order":
        schedule["sources"]["forward"].reverse()
    elif change == "source_extra":
        schedule["sources"]["up"] = []
    elif change == "extra_top":
        schedule["unknown"] = False
    elif change in {"resume", "restore", "authorize"}:
        schedule[
            {
                "resume": "exact_resume",
                "restore": "optimizer_state_restored",
                "authorize": "optimizer_authorized",
            }[change]
        ] = True
    elif change == "old_contract":
        schedule["contract"] = "multidetect.onboard-navigation-balanced-draw-schedule-v1"
    elif change == "draw_extra":
        schedule["draws"].append(copy.deepcopy(schedule["draws"][-1]))
    else:
        del schedule["per_sample_exposure"][pools["forward"][0]]
    with pytest.raises(ValueError):
        recovery.validate_recovery_schedule(schedule, pools, targets=targets, classify=classify)


def test_external_source_order_and_classifier_cannot_be_substituted():
    pools, targets = fixtures()
    schedule = build(pools, targets)
    pools["forward"].reverse()
    with pytest.raises(ValueError):
        recovery.validate_recovery_schedule(schedule, pools, targets=targets, classify=classify)
    pools["forward"].reverse()
    with pytest.raises(ValueError):
        recovery.validate_recovery_schedule(
            schedule, pools, targets=targets, classify=lambda _: "stop"
        )


def test_static_import_needs_only_stdlib_and_cannot_run_training(tmp_path):
    path = Path(recovery.__file__).resolve()
    code = (
        "import runpy,sys; "
        f"runpy.run_path({str(path)!r},run_name='not_a_training_entry'); "
        "assert not {'torch','transformers','PIL','peft','numpy','pyarrow'} & set(sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", code],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.iterdir())
    assert json.loads(json.dumps(build()))["draw_count"] == 267
