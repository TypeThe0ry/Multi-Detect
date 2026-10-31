import json
import random
import shutil
import subprocess
import sys
import uuid
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import build_plan_sft  # noqa: E402
from build_plan_sft import mission_v3  # noqa: E402

from multidetect.autonomy.mission_plan import SKILLS, validate_mission_plan  # noqa: E402
from multidetect.autonomy.mission_planner import repair_plan_document  # noqa: E402


def _texts(path: Path) -> set[str]:
    return {item["text"] for item in json.loads(path.read_text(encoding="utf-8"))["tasks"]}


@pytest.mark.parametrize("variant", ["no_examples", "two_examples", "compact_rules", "compact"])
@pytest.mark.parametrize("version", ["v4", "v5"])
def test_cli_short_prompt_variant_is_used_for_retraining(variant, version):
    output = ROOT / "tmp" / f"plan-sft-short-{uuid.uuid4().hex}"
    try:
        assert build_plan_sft.main([
            "--output", str(output), "--rows", "4", "--val-rows", "2",
            "--version", version,
            "--prompt-variant", variant, "--require-skills",
        ]) == 0
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["prompt_variant"] == variant
        expected_examples = {"no_examples": 0, "two_examples": 2,
                             "compact_rules": 6, "compact": 0}[variant]
        if version == "v5":
            expected_examples -= manifest["excluded_prompt_examples"]
        for split in ("train", "val"):
            rows = [json.loads(line) for line in
                    (output / f"{split}.jsonl").read_text(encoding="utf-8").splitlines()]
            for row in rows:
                assert row["prompt"].count("Task:") == expected_examples + 1
                assert row["prompt"].endswith(f"Task: {row['task_text']}\nPlan:")
                assert ("Skills (* required):" in row["prompt"]) == (
                    variant in {"compact_rules", "compact"}
                )
    finally:
        shutil.rmtree(output, ignore_errors=True)


def test_v3_samples_validate_and_cover_disambiguating_phrases():
    rng = random.Random(20261015)
    rows = [mission_v3(rng) for _ in range(300)]
    for _zh, en, steps, _ in rows:
        validate_mission_plan({"plan_version": 1, "goal": en, "steps": steps, "report": ["status"]})
    text = " ".join(zh + en for zh, en, _, _ in rows)
    assert any(word in text for word in ("统计", "清点", "数一数", "How many", "Tally", "Count up"))
    assert any(
        word in text for word in ("跟随", "跟着", "盯住并跟着", "track and follow", "shadow")
    )
    assert any(
        word in text
        for word in ("拍照", "拍一张", "留存照片", "take a picture", "capture evidence")
    )
    assert "hover" in text and "悬停" in text


def test_v3_terminal_matches_explicit_ending():
    rng = random.Random(7)
    for _ in range(500):
        zh, en, steps, _ = mission_v3(rng)
        terminal = steps[-1]["skill"]
        ending = zh + en
        if any(token in ending for token in ("降落", "land")):
            assert terminal == "land"
        else:
            assert terminal == "return_home"


def test_generated_patrol_repetition_uses_loops_and_survives_repair_unchanged():
    rng = random.Random(20261016)
    patrols = 0
    for _ in range(500):
        _zh, en, steps, report = mission_v3(rng)
        if not any(step["skill"] == "patrol" for step in steps):
            continue
        patrols += 1
        document = {
            "plan_version": 1, "goal": en, "steps": steps, "report": report,
        }
        validate_mission_plan(document)
        repaired, notes = repair_plan_document(document)
        assert repaired == document
        assert notes == []
        patrol = next(step for step in steps if step["skill"] == "patrol")
        assert isinstance(patrol["params"]["loops"], int)
        assert patrol["on_success"] == patrol["on_fail"] == steps[-1]["id"]
    assert patrols > 0


def test_eval_v2_is_disjoint_from_v1_and_generated_samples():
    v1 = _texts(ROOT / "configs/onboard/plan-eval-tasks.json")
    v2 = _texts(ROOT / "configs/onboard/plan-eval-tasks-v2.json")
    assert len(v2) == 60
    assert len(v2 & v1) == 0
    rng = random.Random(20261015)
    generated = set()
    for _ in range(20_000):
        zh, en, _, _ = mission_v3(rng)
        generated.add(" ".join(zh.split()))
        generated.add(" ".join(en.split()))
    assert not (generated & (v1 | v2))


def test_replay_planner_rows_are_preserved_and_holdout_normalized():
    workspace = ROOT / "tmp" / f"plan-sft-replay-{uuid.uuid4().hex}"
    source = workspace / "source.jsonl"
    output = workspace / "out"
    try:
        source.parent.mkdir(parents=True)
        row = {
            "sample_id": "replay-1", "kind": "mission_plan", "split": "train",
            "task_text": "Replay a safe task", "prompt": "original prompt",
            "assistant_target": {"plan_version": 1, "goal": "safe", "steps": [{"id": "s1", "skill": "return_home", "params": {}, "on_success": None, "on_fail": None}], "report": []},
        }
        source.write_text(json.dumps(row) + "\n", encoding="utf-8")
        assert build_plan_sft.main([
            "--output", str(output), "--rows", "1", "--val-rows", "0",
            "--replay-planner-train", str(source), "--r20-plan-intent",
            "--r20-plan-intent-count", "1", "--r20-plan-intent-mix", "--require-skills",
        ]) == 0
        rows = [json.loads(line) for line in (output / "train.jsonl").read_text(encoding="utf-8").splitlines()]
        replayed = next(item for item in rows if item["sample_id"] == "replay-1")
        assert replayed == row
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["replay_planner_train_rows"] == 1
        assert manifest["family_counts"]["train"]["r14_replay"] == 1
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_r25_rows_keep_missing_payload_and_negated_query_candidates_admissible(tmp_path):
    from train_slow_system_lora import admit_train_rows

    output = tmp_path / "r25"
    assert build_plan_sft.main([
        "--output", str(output), "--rows", "1", "--val-rows", "0", "--version", "v4",
        "--r25-recipe", "--r23-v3-count", "0", "--r23-deliver-count", "0",
        "--r24-reference-count", "0", "--r24-unknown-count", "0", "--r24-context-count", "0",
        "--r25-missing-payload-count", "2", "--r25-negated-query-count", "2",
        "--negative-target-mode", "rejected_candidate", "--require-skills",
    ]) == 0
    rows, _ = admit_train_rows(output)
    r25 = [row for row in rows if str(row.get("family", "")).startswith("r25_")]
    assert len(r25) == 8
    missing = [row for row in r25 if row["family"] == "r25_missing_payload"]
    capable = [row for row in r25 if row["family"] == "r25_payload_capable"]
    negated = [row for row in r25 if row["family"] == "r25_negated_query" and not row["expected_valid"]]
    assert len(missing) == len(capable) == len(negated) == 2
    assert all(row["capabilities"] == [] for row in missing)
    assert all(row["assistant_target"]["steps"][0]["skill"] == "deliver" for row in missing)
    assert all(row["negative_target_mode"] == "rejected_candidate" for row in missing + negated)
    assert all(row["expected_findings"] == ["missing_capability"] for row in missing)
    assert all(row["expected_findings"] == ["ungrounded_query"] for row in negated)


def test_r26_recipe_emits_structured_refusals_and_admits_without_r25_candidates(tmp_path):
    from train_slow_system_lora import admit_train_rows

    output = tmp_path / "r26"
    assert build_plan_sft.main([
        "--output", str(output), "--rows", "200", "--val-rows", "0", "--version", "v4",
        "--r26-recipe", "--r26-refusal-count", "6", "--r23-v3-count", "0",
        "--r23-deliver-count", "0", "--r24-reference-count", "0",
        "--r24-unknown-count", "0", "--r24-context-count", "0",
        "--require-skills", "return_home",
    ]) == 0
    rows, _ = admit_train_rows(output)
    refusals = [row for row in rows if str(row.get("family", "")).startswith("r26_")]
    decisions = [row for row in refusals if row.get("pair_role") == "refusal"]
    positives = [row for row in rows if row.get("pair_role") == "positive"]
    assert len(decisions) == len(positives) == 30
    assert sum(row["assistant_target"].get("decision") == "reject" for row in decisions) == 18
    assert sum(row["assistant_target"].get("decision") == "clarify" for row in decisions) == 12
    assert {row["reason_code"] for row in decisions} == {
        "missing_capability", "unknown_place", "ungrounded_query", "ambiguous_task", "unsafe_request"
    }
    assert all("rejected_candidate" not in row for row in refusals)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["r26_recipe"]["r25_rejected_candidate_rows_included"] is False


def test_r26_converts_r24_unknown_place_target_and_balances_its_positive_pair(tmp_path):
    from train_slow_system_lora import admit_train_rows

    output = tmp_path / "r26-r24-upgrade"
    assert build_plan_sft.main([
        "--output", str(output), "--rows", "1", "--val-rows", "0", "--version", "v4",
        "--r26-recipe", "--r26-refusal-count", "6", "--r23-v3-count", "0",
        "--r23-deliver-count", "0", "--r24-reference-count", "0",
        "--r24-unknown-count", "4", "--r24-context-count", "0", "--require-skills", "return_home",
    ]) == 0
    rows, _ = admit_train_rows(output)
    unknown = [row for row in rows if row.get("reason_code") == "unknown_place"]
    refusals = [row for row in unknown if row["assistant_target"].get("decision") == "clarify"]
    positives = [row for row in unknown if row.get("pair_role") == "positive"]
    assert len(refusals) == len(positives) == 6
    legacy = [row for row in refusals if row["family"] == "r24_unknown_place"]
    assert len(legacy) == 4
    assert all(row["assistant_target"]["unresolved_refs"] for row in legacy)
    assert all(row["assistant_target"] != row["rejected_candidate"] for row in legacy)


def test_r26_fails_when_finite_template_pool_cannot_meet_request(tmp_path):
    with pytest.raises(ValueError, match="template pool exhausted"):
        build_plan_sft.main([
            "--output", str(tmp_path / "r26-exhausted"), "--rows", "1", "--val-rows", "0",
            "--version", "v4", "--r26-recipe", "--r26-refusal-count", "1000",
            "--r23-v3-count", "0", "--r23-deliver-count", "0",
            "--r24-reference-count", "0", "--r24-unknown-count", "0", "--r24-context-count", "0",
        ])


@pytest.mark.parametrize("version", ["v2", "v3", "v4", "v5"])
def test_cli_writes_requested_rows(version):
    output = ROOT / "tmp" / f"plan-sft-test-{version}-{uuid.uuid4().hex}"
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/build_plan_sft.py"),
                "--output",
                str(output),
                "--rows",
                "8",
                "--val-rows",
                "2",
                "--version",
                version,
                "--require-skills",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        assert json.loads(result.stdout) == {"train": 8, "val": 2}
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["version"] == version
        assert manifest["seed"] == 20261015
        assert manifest["rows"] == {"train": 8, "val": 2}
        for split in ("train", "val"):
            rows = [
                json.loads(line)
                for line in (output / f"{split}.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            sequences = [
                [step["skill"] for step in row["assistant_target"]["steps"]] for row in rows
            ]
            counts = {
                "skill_counts": Counter(skill for sequence in sequences for skill in sequence),
                "first_skill_counts": Counter(sequence[0] for sequence in sequences),
                "final_skill_counts": Counter(sequence[-1] for sequence in sequences),
            }
            for key, expected in counts.items():
                assert manifest[key][split] == {skill: expected[skill] for skill in SKILLS}
    finally:
        shutil.rmtree(output, ignore_errors=True)


@pytest.mark.parametrize("version", ["v2", "v3", "v4", "v5"])
def test_cli_default_skill_coverage(version):
    output = ROOT / "tmp" / f"plan-sft-coverage-{version}-{uuid.uuid4().hex}"
    try:
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/build_plan_sft.py"),
                "--output",
                str(output),
                "--version",
                version,
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        counts = manifest["skill_counts"]["train"]
        assert counts["deliver"] == 0
        if version == "v3":
            assert all(counts[skill] == 0 for skill in ("inspect", "monitor", "survey"))
            assert manifest["first_skill_counts"]["train"]["return_home"] == 0
            assert manifest["first_skill_counts"]["train"]["land"] == 0
        else:
            assert all(counts[skill] > 0 for skill in SKILLS if skill != "deliver")
            assert manifest["first_skill_counts"]["train"]["return_home"] > 0
            assert manifest["first_skill_counts"]["train"]["land"] > 0
    finally:
        shutil.rmtree(output, ignore_errors=True)


@pytest.mark.parametrize(
    ("version", "options", "missing"),
    [
        (
            "v3",
            ["--require-skills", "inspect", "monitor", "survey"],
            ["inspect", "monitor", "survey"],
        ),
        ("v2", ["--rows", "0", "--val-rows", "100"], sorted(set(SKILLS) - {"deliver"})),
    ],
)
def test_cli_rejects_missing_training_skills(version, options, missing):
    output = ROOT / "tmp" / f"plan-sft-missing-{version}-{uuid.uuid4().hex}"
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/build_plan_sft.py"),
                "--output",
                str(output),
                "--version",
                version,
                *options,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "missing required skills in training split: " + ", ".join(missing) in result.stderr
        assert not output.exists()
    finally:
        shutil.rmtree(output, ignore_errors=True)


@pytest.mark.parametrize("selection", ["default", "grouped", "repeated"])
@pytest.mark.parametrize("version", ["v2", "v4", "v5"])
def test_cli_excludes_all_selected_eval_files(monkeypatch, selection, version):
    workspace = ROOT / "tmp" / f"plan-sft-held-out-{uuid.uuid4().hex}"
    try:
        eval_dir = workspace / "configs/onboard"
        eval_dir.mkdir(parents=True)
        empty = workspace / "empty.json"
        empty.write_text('{"tasks": []}', encoding="utf-8")
        common = ["--rows", "20", "--val-rows", "2", "--version", version, "--require-skills"]
        baseline = workspace / "baseline"
        assert build_plan_sft.main(
            ["--output", str(baseline), *common, "--eval-tasks", str(empty)]
        ) == 0
        baseline_rows = [
            json.loads(line)
            for line in (baseline / "train.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        held_out = {row["task_text"] for row in baseline_rows[:2]}
        paths = [eval_dir / "plan-eval-tasks.json", eval_dir / "plan-eval-tasks-v2.json"]
        if version == "v5":
            # V5 always retains default exclusions, including few-shot examples.
            for path in (ROOT / "configs/onboard").glob("plan-eval-tasks*.json"):
                shutil.copyfile(path, eval_dir / path.name)
            paths = [eval_dir / f"plan-eval-tasks-extra-{i}.json" for i in range(2)]
        for path, row in zip(paths, baseline_rows, strict=False):
            # Match the generator's whitespace normalization, including newlines.
            path.write_text(
                json.dumps({"tasks": [{"text": "  " + row["task_text"] + "\n"}]}),
                encoding="utf-8",
            )
        monkeypatch.setattr(build_plan_sft, "ROOT", workspace)
        options = {
            "default": [],
            "grouped": ["--eval-tasks", *(str(path) for path in paths)],
            "repeated": ["--eval-tasks", str(paths[0]), "--eval-tasks", str(paths[1])],
        }[selection]
        output = workspace / "filtered"
        assert build_plan_sft.main(["--output", str(output), *common, *options]) == 0
        generated = {
            json.loads(line)["task_text"]
            for split in ("train", "val")
            for line in (output / f"{split}.jsonl").read_text(encoding="utf-8").splitlines()
        }
        assert len(generated) == 22
        assert not generated & held_out

        # Loading extra files must not consume RNG state when no text collides.
        for path in paths:
            path.write_text(
                '{"tasks": [{"text": "An unrelated held-out instruction"}]}',
                encoding="utf-8",
            )
        unchanged = workspace / "unchanged"
        assert build_plan_sft.main(["--output", str(unchanged), *common, *options]) == 0
        for name in ("train.jsonl", "val.jsonl", "manifest.json"):
            assert (unchanged / name).read_bytes() == (baseline / name).read_bytes()
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
