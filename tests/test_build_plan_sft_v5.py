"""V5 data contracts and frozen v4 output; no inference or external assets."""

import hashlib
import json
import random
import re
import shutil
import sys
import uuid
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_plan_sft as sft  # noqa: E402
import plan_from_task  # noqa: E402

from multidetect.autonomy.mission_plan import SKILLS, validate_mission_plan  # noqa: E402
from multidetect.autonomy.mission_planner import (  # noqa: E402
    check_plan_refs,
    compile_mission_plan,
)


@pytest.fixture
def workspace():
    path = ROOT / "tmp" / f"plan-sft-v5-test-{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path)


def _read_rows(output):
    return {
        split: [json.loads(line) for line in
                (output / f"{split}.jsonl").read_text(encoding="utf-8").splitlines()]
        for split in ("train", "val")
    }


def _walk(steps, failures=()):
    by_id = {step["id"]: step for step in steps}
    current, result = steps[0], []
    while current:
        assert current["id"] not in {step["id"] for step in result}
        result.append(current)
        current = by_id.get(current["on_fail" if current["id"] in failures else "on_success"])
    return result


@pytest.mark.parametrize("family", sft.V5_FAMILIES)
def test_new_family_labels_and_edges(family):
    rng = random.Random(35)
    variants = set()
    pairs = set()
    for _ in range(250):
        zh, en, steps, report = sft.mission_v5(rng, family)
        document = {"plan_version": 1, "goal": en[:160], "steps": steps, "report": report}
        plan = validate_mission_plan(document)
        # The host must preserve the intended graph rather than silently repair it.
        refs = tuple(f"{ref} ({label} / {english})" for ref, label, english in
                     sft.ZONES + sft.POIS + sft.ROUTES)
        raw = json.dumps(document)
        compiled, outcome = compile_mission_plan(lambda *args, raw=raw: raw, zh, refs=refs)
        assert compiled == plan
        assert outcome["repairs"] == []
        terminal = steps[-1]
        assert terminal["skill"] == ("land" if "降落" in zh else "return_home")
        for step in steps:
            if step is not terminal:
                assert step["on_success"] and step["on_fail"]
            # Every error branch must also have a finite path to a terminal.
            assert _walk(steps, {step["id"]})[-1] == terminal
        if family == "conditional_branch":
            assert [step["skill"] for step in _walk(steps)] == [
                "area_search", "record", terminal["skill"],
            ]
            assert [step["skill"] for step in _walk(steps, {"s1"})] == [
                "area_search", "goto", terminal["skill"],
            ]
            assert steps[1]["params"]["query"] == steps[0]["params"]["query"]
        elif family == "two_targets":
            searches = [step for step in steps if step["skill"] == "area_search"]
            assert len(searches) == 2
            assert searches[0]["params"]["query"] != searches[1]["params"]["query"]
            pairs.add(tuple(step["params"]["query"] for step in searches))
            assert searches[1] in _walk(steps, {"s1"})
            for search in searches:
                failure_path = _walk(steps, {search["id"]})
                assert not any(step["skill"] == "record"
                               and step["params"]["query"] == search["params"]["query"]
                               for step in failure_path)
            variants.add(len(steps))
        elif family == "count_stop":
            assert steps[0]["skill"] == "count"
            n = steps[0]["params"]["max_count"]
            assert str(n) in zh
            assert str(n) in en or sft.EN_NUMBERS[n] in en
            assert steps[0]["on_success"] == steps[0]["on_fail"] == terminal["id"]
            assert len(steps) == 2
        elif family == "alert_action":
            variants.add(steps[0]["skill"])
            search = next(step for step in steps if step["skill"] == "area_search")
            path = _walk(steps)
            assert [step["skill"] for step in path[-4:]] == [
                "area_search", "hover", "record", terminal["skill"],
            ]
            assert not {"hover", "record"} & {
                step["skill"] for step in _walk(steps, {search["id"]})
            }
            if steps[0]["skill"] == "monitor":
                assert "时间到了再搜" in zh and "afterward search" in en
            if steps[0]["skill"] == "patrol":
                assert "巡完再搜" in zh and "when done search" in en
            assert steps[-2]["params"]["query"] == search["params"]["query"]
        elif family == "evidence_only":
            assert [step["skill"] for step in steps] == ["record", terminal["skill"]]
            variants.add(steps[0]["params"]["query"])
        else:
            variants.add(steps[0]["skill"])
            assert any(word in zh for word in (
                "瞅瞅", "瞄一眼", "找找看", "瞧瞧", "转一圈", "溜一圈", "兜一圈", "盯着",
            ))
    if family == "two_targets":
        assert variants == {3, 5}
        assert ("red car", "blue backpack") in pairs
    if family == "alert_action":
        assert variants == {"area_search", "monitor", "patrol"}
    if family == "evidence_only":
        assert "current view" in variants
    if family == "colloquial":
        assert variants == {"area_search", "goto", "monitor", "patrol"}


@pytest.mark.parametrize("left,right", [
    ("Count FIVE cars, then return!", " count ５ CARS then return. "),
    ("悬停十五秒，然后回来！", " 悬停15秒 然后回来 "),
    ("Count two hundred twenty-five cars", "Count 225 cars"),
    ("清点两百零五个背包", "清点２０５个背包"),
    ("Hover FORTY-FIVE seconds", "hover forty five seconds."),
])
def test_normalized_leakage_key(left, right):
    assert sft.normalize_task(left) == sft.normalize_task(right)
    assert sft.normalize_task("two cars") != sft.normalize_task("three cars")


def test_v5_sample_distribution_refs_and_leakage(workspace):
    args = ["--version", "v5", "--rows", "1800", "--val-rows", "150"]
    assert sft.main(["--output", str(workspace / "data"), *args]) == 0
    splits = _read_rows(workspace / "data")
    manifest = json.loads((workspace / "data/manifest.json").read_text(encoding="utf-8"))
    held_out = {
        sft.normalize_task(task["text"])
        for path in (ROOT / "configs/onboard").glob("plan-eval-tasks*.json")
        for task in json.loads(path.read_text(encoding="utf-8"))["tasks"]
    }
    assert manifest["excluded_prompt_examples"] == 1
    seen = set()
    for split, rows in splits.items():
        size = 1800 if split == "train" else 150
        counts = Counter(row["family"] for row in rows)
        assert len(rows) == size
        assert counts["v4_backbone"] == size * 7 // 10
        assert set(counts) == {"v4_backbone", *sft.V5_FAMILIES}
        assert counts == manifest["family_counts"][split]
        assert max(counts[f] for f in sft.V5_FAMILIES) - min(
            counts[f] for f in sft.V5_FAMILIES
        ) <= 1
        skills = Counter()
        for row in rows:
            plan = row["assistant_target"]
            validate_mission_plan(plan)
            skills.update(step["skill"] for step in plan["steps"])
            key = sft.normalize_task(row["task_text"])
            assert key not in held_out | seen
            seen.add(key)
            known = re.search(r"^Known places: (.*)\.$", row["prompt"], re.MULTILINE)
            refs = tuple(known[1].split(", ")) if known else ()
            assert not check_plan_refs(plan, refs, substitute=False)[2]
            assert row["prompt"].endswith(f"Task: {row['task_text']}\nPlan:")
            prompt_tasks = re.findall(r"^Task: (.*)$", row["prompt"], re.MULTILINE)
            assert not {sft.normalize_task(text) for text in prompt_tasks} & held_out
        assert manifest["skill_counts"][split] == {s: skills[s] for s in SKILLS}
        assert all(skills[s] > 0 for s in SKILLS if s != "deliver")
    # Retained v4 tasks/labels/ids stay exact; held-out prompt examples are removed.
    assert sft.main(["--output", str(workspace / "v4"), *args, "--version", "v4"]) == 0
    legacy = {row["sample_id"]: row for rows in _read_rows(workspace / "v4").values()
              for row in rows}
    for rows in splits.values():
        for row in rows:
            if row["family"] == "v4_backbone":
                expected = legacy[row["sample_id"]]
                assert {k: v for k, v in row.items()
                        if k not in {"family", "split", "prompt"}} == {
                    k: v for k, v in expected.items() if k not in {"split", "prompt"}
                }
    # Reproducibility is not sensitive to paths or process-global RNG state.
    assert sft.main(["--output", str(workspace / "again"), *args]) == 0
    for filename in ("train.jsonl", "val.jsonl", "manifest.json"):
        assert (workspace / "data" / filename).read_bytes() == (
            workspace / "again" / filename
        ).read_bytes()


def test_v5_filters_normalized_eval_variants_from_every_family(workspace, monkeypatch):
    empty = workspace / "empty.json"
    empty.write_text('{"tasks": []}', encoding="utf-8")
    args = ["--version", "v5", "--rows", "240", "--val-rows", "40", "--require-skills"]
    assert sft.main(["--output", str(workspace / "base"), *args,
                     "--eval-tasks", str(empty)]) == 0
    originals = {}
    for rows in _read_rows(workspace / "base").values():
        for row in rows:
            originals.setdefault(row["family"], row["task_text"])
    eval_dir = workspace / "configs/onboard"
    eval_dir.mkdir(parents=True)
    for i, text in enumerate(originals.values()):
        text = re.sub(r"[0-9]", lambda m: chr(ord(m[0]) + 0xFEE0), text.upper())
        path = eval_dir / f"plan-eval-tasks-{i}.json"
        path.write_text(json.dumps({"tasks": [{"text": f"！ {text} !"}]}), encoding="utf-8")
    monkeypatch.setattr(sft, "ROOT", workspace)
    assert sft.main(["--output", str(workspace / "filtered"), *args]) == 0
    held = {sft.normalize_task(text) for text in originals.values()}
    assert not held & {
        sft.normalize_task(row["task_text"])
        for rows in _read_rows(workspace / "filtered").values() for row in rows
    }


def test_ambiguous_place_alias_is_rejected():
    before = [sft.S("s1", "goto", {"target_ref": "poi:a"}, "s2", "s2"),
              sft.S("s2", "goto", {"target_ref": "poi:b"}, "s9", "s9")]
    after = [sft.S("s1", "goto", {"target_ref": "poi:site"}, "s2", "s2"),
             sft.S("s2", "goto", {"target_ref": "poi:site"}, "s9", "s9")]
    labels = {"poi:a": "A", "poi:b": "B"}
    assert not sft._labelled_refs_match(before, after, ("poi:site (B)",), labels)


def test_v5_extra_eval_file_keeps_default_exclusions(workspace, monkeypatch):
    eval_dir = workspace / "configs/onboard"
    eval_dir.mkdir(parents=True)
    # Force every v5 attempt to collide with the repository's default held-out set.
    (eval_dir / "plan-eval-tasks.json").write_text(
        '{"tasks": [{"text": "Excluded Task"}]}', encoding="utf-8",
    )
    extra = workspace / "extra.json"
    extra.write_text('{"tasks": []}', encoding="utf-8")
    monkeypatch.setattr(sft, "ROOT", workspace)
    monkeypatch.setattr(sft, "mission_v5", lambda *args: (
        "EXCLUDED TASK!", "EXCLUDED TASK!", [sft.H("s1")], ["status"],
    ))
    with pytest.raises(ValueError, match="template pool exhausted"):
        sft.main(["--output", str(workspace / "bad"), "--version", "v5", "--rows", "2",
                  "--val-rows", "0", "--require-skills", "--eval-tasks", str(extra)])
    assert not (workspace / "bad").exists()


def test_v5_required_skill_guard_and_refusal_boundary(workspace, capsys):
    with pytest.raises(SystemExit):
        sft.main(["--output", str(workspace / "bad"), "--version", "v5", "--rows", "0"])
    assert "missing required skills in training split" in capsys.readouterr().err
    assert not (workspace / "bad").exists()
    assert sft.main(["--output", str(workspace / "no-guard"), "--version", "v5",
                     "--rows", "0", "--val-rows", "0", "--require-skills"]) == 0
    home = {"plan_version": 1, "goal": "unsupported task", "steps": [sft.H("s1")],
            "report": ["status"]}
    plan, outcome = compile_mission_plan(lambda *args: json.dumps(home), "unsupported task")
    assert plan is not None and not outcome["repairs"]
    assert not plan_from_task.score_task({"expect_invalid": True}, plan, None)["exact"]


def test_v4_outputs_keep_pre_v5_byte_identity(workspace):
    # seed 20261015, default prompt and all default eval files. Re-captured at f69c0da6:
    # holdout/dedup compare normalize_task() keys, dropping 19 punctuation/spacing
    # near-duplicates the old exact-text check let through (none matched an eval task).
    expected = {
        "manifest.json": "065bbb04288eac1b50d4241b1256332dc68b98fe3cdaee521d1f9e27d71827bb",
        "train.jsonl": "fa6bdeecf178273d22db8bbad60d3ea8b8f1a3d001b41619662611f1f865f3a3",
        "val.jsonl": "2c0ca69e242a2fa7efb81bd2ea17fb3fccd490371b46148072121a6c2c0c6f2d",
    }
    assert sft.main(["--output", str(workspace), "--version", "v4", "--rows", "1800",
                     "--val-rows", "150"]) == 0
    for filename, digest in expected.items():
        assert hashlib.sha256((workspace / filename).read_bytes()).hexdigest() == digest


def test_r23_recipe_adds_v3_coverage_and_faithful_deliver_rows(workspace):
    out = workspace / "r23"
    assert sft.main(["--output", str(out), "--version", "v4", "--rows", "80",
                     "--val-rows", "5", "--r23-recipe", "--r23-v3-count", "10",
                     "--r23-deliver-count", "5", "--require-skills"]) == 0
    rows = _read_rows(out)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    train = rows["train"]
    assert sum(row.get("family") == "r23_v3_coverage" for row in train) == 10
    deliver = [row for row in train if row.get("family") == "r23_deliver_faithful"]
    assert len(deliver) == 5
    assert all(row["assistant_target"]["steps"][0]["skill"] == "deliver" for row in deliver)
    assert manifest["r23_recipe"]["rows_added"] == 15
    assert (out / "r23-data-stats.md").is_file()
