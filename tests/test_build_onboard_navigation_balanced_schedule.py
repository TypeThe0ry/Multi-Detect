"""Balanced exposure preserves original train identities, not flight authority."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import build_onboard_navigation_balanced_schedule as builder


def fixture_pool():
    samples, targets = [], {}
    for action_class, count in builder.SOURCE_COUNTS.items():
        for index in range(count):
            sample_id = f"{action_class}-{index}"
            samples.append(
                SimpleNamespace(
                    sample_id=sample_id,
                    partition="train",
                    scene_id="scene-1",
                    source=SimpleNamespace(original_split="train"),
                )
            )
            targets[sample_id] = action_class
    return samples, targets, [sample.sample_id for sample in samples]


def draws(samples, targets, ids, seed=20261007):
    return builder.build_draws(samples, targets, ids, seed=seed, classify=lambda value: value)


def test_seeded_four_class_schedule_exact_coverage_cycles_and_exposures():
    samples, targets, ids = fixture_pool()
    rows, exposures = draws(samples, targets, ids)
    assert draws(samples, targets, ids) == (rows, exposures)
    assert len(rows) == sum(exposures.values()) == 280
    assert set(exposures) == set(ids)
    assert Counter(row["action_class"] for row in rows) == dict.fromkeys(builder.CLASSES, 70)
    assert [row["draw_index"] for row in rows] == list(range(1, 281))
    observed = Counter()
    for row in rows:
        observed[row["sample_id"]] += 1
        assert row["per_sample_exposure"] == observed[row["sample_id"]]
        assert set(row) == {
            "draw_index",
            "action_class",
            "sample_id",
            "class_draw_index",
            "class_cycle",
            "class_cycle_offset",
            "per_sample_exposure",
        }
    assert dict(observed) == exposures
    for action_class, source_count in builder.SOURCE_COUNTS.items():
        group = [row for row in rows if row["action_class"] == action_class]
        assert [row["class_draw_index"] for row in group] == list(range(1, 71))
        for index, row in enumerate(group):
            assert row["class_cycle"] == index // source_count + 1
            assert row["class_cycle_offset"] == index % source_count + 1
        for start in range(0, 70, source_count):
            cycle = group[start : start + source_count]
            assert len({row["sample_id"] for row in cycle}) == len(cycle)
        class_exposures = [
            exposures[sample.sample_id]
            for sample in samples
            if targets[sample.sample_id] == action_class
        ]
        assert max(class_exposures) - min(class_exposures) <= 1
    assert {exposures[value] for value in ids if value.startswith("forward-")} == {1}
    assert {exposures[value] for value in ids if value.startswith("left-")} == {11, 12}
    assert {exposures[value] for value in ids if value.startswith("right-")} == {10}
    assert {exposures[value] for value in ids if value.startswith("stop-")} == {23, 24}


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "missing",
        "order",
        "unknown",
        "nontrain",
        "holdout",
        "counts",
        "target_extra",
        "target_missing",
        "scene",
        "seed",
    ],
)
def test_schedule_rejects_bad_identity_partition_class_counts_and_seed(mutation):
    samples, targets, ids = fixture_pool()
    seed = 20261007
    if mutation == "duplicate":
        samples[1].sample_id = samples[0].sample_id
    elif mutation == "missing":
        samples.pop()
    elif mutation == "order":
        samples[0], samples[1] = samples[1], samples[0]
    elif mutation == "unknown":
        targets[ids[0]] = "up"
    elif mutation == "nontrain":
        samples[0].partition = "dev-validation"
    elif mutation == "holdout":
        samples[0].source.original_split = "test"
    elif mutation == "counts":
        targets[ids[0]] = "left"
    elif mutation == "target_extra":
        targets["holdout"] = "forward"
    elif mutation == "target_missing":
        del targets[ids[0]]
    elif mutation == "scene":
        samples[0].scene_id = "scene-2"
    else:
        seed = True
    with pytest.raises(ValueError):
        draws(samples, targets, ids, seed)


def test_streaming_source_bindings_reject_drift_and_escape(tmp_path):
    path = tmp_path / "source.json"
    path.write_bytes(b"original")
    row = builder.binding(path, tmp_path)
    assert builder.verify_sources([row, row], tmp_path) == [row]
    path.write_bytes(b"modified")
    with pytest.raises(ValueError, match="SHA changed"):
        builder.verify_sources([row], tmp_path)
    with pytest.raises(ValueError, match="escaped"):
        builder.binding(Path(builder.__file__), tmp_path)
    bad = copy.deepcopy(row)
    bad["truth_for_actor"] = True
    with pytest.raises(ValueError, match="fields differ"):
        builder.verify_sources([bad], tmp_path)


def test_real_isolated_cli_rejects_unsealed_plan_without_dependencies_or_outputs(tmp_path):
    output = tmp_path / "new"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(Path(builder.__file__).resolve()),
            "--plan",
            str(tmp_path / "absent.json"),
            "--plan-sha256",
            "a" * 64,
            "--output-dir",
            str(output),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["event"] == "balanced_schedule_rejected"
    assert "Only the exact sealed" in json.loads(result.stdout)["error"]
    assert not output.exists()
    assert result.stderr == ""


def test_existing_output_and_outside_repository_rejected_before_source_loading(
    tmp_path, monkeypatch
):
    root = tmp_path / "repo"
    training = root / "artifacts/training"
    training.mkdir(parents=True)
    existing = training / "already"
    existing.mkdir()
    sentinel = existing / "schedule.json"
    sentinel.write_bytes(b"user-owned")
    plan = root / "plan.json"
    raw = json.dumps({"artifact_root": str(root)}).encode()
    plan.write_bytes(raw)
    monkeypatch.setattr(builder, "ROOT", root)
    monkeypatch.setattr(builder, "PLAN_SHA256", hashlib.sha256(raw).hexdigest())
    for output, error in (
        (existing, FileExistsError),
        (tmp_path / "outside", ValueError),
        (training, ValueError),
    ):
        with pytest.raises(error):
            builder.build_schedule(plan, builder.PLAN_SHA256, output)
    assert sentinel.read_bytes() == b"user-owned"


def test_preserved_actor_draw_rows_never_contain_prompt_images_truth_or_target_labels():
    samples, targets, ids = fixture_pool()
    rows, _ = draws(samples, targets, ids)
    forbidden = {
        "prompt",
        "rgb",
        "frames",
        "pose",
        "target",
        "translation_m",
        "yaw_delta_rad",
        "future",
        "truth",
        "actor_input",
    }
    assert all(not forbidden.intersection(row) for row in rows)
