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


def _texts(path: Path) -> set[str]:
    return {item["text"] for item in json.loads(path.read_text(encoding="utf-8"))["tasks"]}


@pytest.mark.parametrize("variant", ["no_examples", "two_examples", "compact_rules", "compact"])
def test_cli_short_prompt_variant_is_used_for_retraining(variant):
    output = ROOT / "tmp" / f"plan-sft-short-{uuid.uuid4().hex}"
    try:
        assert build_plan_sft.main([
            "--output", str(output), "--rows", "4", "--val-rows", "2",
            "--prompt-variant", variant, "--require-skills",
        ]) == 0
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["prompt_variant"] == variant
        expected_examples = {"no_examples": 0, "two_examples": 2,
                             "compact_rules": 6, "compact": 0}[variant]
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


@pytest.mark.parametrize("version", ["v2", "v3", "v4"])
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


@pytest.mark.parametrize("version", ["v2", "v3", "v4"])
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
@pytest.mark.parametrize("version", ["v2", "v4"])
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
