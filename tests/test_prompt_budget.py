"""CPU-only checks for offline accounting and future evaluation aggregation."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import prompt_budget
from scripts.prompt_budget import (
    OfflineTokenizer,
    accounting_markdown,
    collect_evaluations,
    identity_parts,
    image_grid,
    local_tokenizer_snapshot,
    summarize_samples,
)

CONFIG = {"patch_size": 16, "merge_size": 2, "size": {"shortest_edge": 65536}}


@pytest.mark.parametrize("side", [160, 224])
def test_local_hf_minimum_prevents_crop_token_savings(side):
    grid = image_grid(side, side, CONFIG, 448 * 448)
    assert grid["processor_size"] == [256, 256]
    assert grid["visual_tokens"] == 64


def test_frame_reduction_obeys_rounding_and_minimum():
    assert image_grid(448, 252, CONFIG, 448 * 448)["visual_tokens"] == 112
    assert image_grid(336, 189, CONFIG, 448 * 448)["visual_tokens"] == 66


def test_oversized_image_obeys_hf_pixel_cap():
    grid = image_grid(4096, 2048, CONFIG, 448 * 448)
    assert grid["processed_pixels"] <= 448 * 448
    assert all(side % 32 == 0 for side in grid["processor_size"])


@pytest.mark.parametrize("size", [(0, 50), (50, -1), (1, 201)])
def test_invalid_image_sizes_fail(size):
    with pytest.raises(ValueError):
        image_grid(*size, CONFIG, 448 * 448)


def test_accounting_prefix_parts_preserve_bpe_boundary_differences():
    tokenizer = OfflineTokenizer.__new__(OfflineTokenizer)
    # A toy tokenizer with merges crossing a part boundary.
    tokenizer.count = lambda text: len(text.replace("a\nb", "X"))
    parts = {"system": "a", "empty": "", "task": "b"}
    counts = tokenizer.parts(parts)
    assert sum(counts.values()) == tokenizer.count("a\nb")
    assert counts["empty"] == 0
    assert tokenizer.parts({"a": "left\n", "b": "right"}, separator="") == {
        "a": 5, "b": 5,
    }


def test_identity_parts_reconstruct_original_prompt():
    prompt = "You plan. Choose one. Output JSON.\nTask: search\nMemory: none\nCandidates:\nx"
    parts = identity_parts(prompt)
    assert parts["examples"] == ""
    assert parts["system"] + parts["rules"] == prompt.splitlines()[0]
    assert "\n".join([parts["system"] + parts["rules"], parts["task"],
                       parts["memory"], parts["refs"]]) == prompt


def test_tokenizer_resolution_requires_exactly_one_local_snapshot(tmp_path):
    with pytest.raises(ValueError, match="found 0"):
        local_tokenizer_snapshot(tmp_path)
    snapshot = tmp_path / "snapshots" / "abc"
    snapshot.mkdir(parents=True)
    (snapshot / "tokenizer.json").write_text("{}", encoding="utf-8")
    assert local_tokenizer_snapshot(tmp_path) == snapshot.resolve()
    assert local_tokenizer_snapshot(snapshot) == snapshot.resolve()
    second = tmp_path / "snapshots" / "def"
    second.mkdir()
    (second / "tokenizer.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="found 2"):
        local_tokenizer_snapshot(tmp_path)


def test_empty_accounting_is_rejected():
    with pytest.raises(ValueError, match="empty"):
        summarize_samples([])


def _write_report(path: Path, report: dict):
    path.write_text(json.dumps(report), encoding="utf-8")


def test_collect_real_usage_and_accuracy_with_planning_retries(tmp_path):
    _write_report(tmp_path / "identity--test--focus_only.json", {
        "metrics": {"identity_accuracy": 0.75, "json_validity": 1,
                    "latency_s": {"p50": 0.9}},
        "rows": [{"prompt_tokens": 210, "completion_tokens": 30},
                 {"prompt_tokens": 230, "completion_tokens": 40}],
    })
    _write_report(tmp_path / "planning--tasks--no_examples.json", {
        "summary": {"exact_sequence_rate": 0.5, "valid_rate": 1,
                    "median_latency_s": 3.2},
        "rows": [{"prompt_tokens": 600, "prompt_tokens_total": 1240,
                  "completion_tokens_total": 180}],
    })
    table = collect_evaluations(tmp_path)
    assert "identity | test | focus_only | 2 | 75.0% | 100.0% | 220.00 | 35.00 | 0.90" in table
    assert "planning | tasks | no_examples | 1 | 50.0% | 100.0% | 600.00 | 180.00 | 3.20" in table


def test_collect_missing_usage_stays_unknown_and_unrelated_json_ignored(tmp_path):
    _write_report(tmp_path / "token-accounting.json", {"rows": []})
    with pytest.raises(ValueError, match="No evaluation reports"):
        collect_evaluations(tmp_path)
    _write_report(tmp_path / "identity--test--default.json", {
        "metrics": {"identity_accuracy": None, "json_validity": 0,
                    "latency_s": {"p50": 0.2}}, "rows": [{"prompt_tokens": None}],
    })
    assert "n/a | 0.0% | n/a | n/a | 0.20" in collect_evaluations(tmp_path)


def test_latency_scenario_separates_estimates_from_measurements():
    sample = {"suite": "planning", "dataset": "tasks", "variant": "compact",
              "parts": {"system": 10, "rules": 400, "refs": 80, "task": 10},
              "chat_overhead_tokens": 8, "visual_tokens": 0, "prompt_tokens": 508,
              "processed_megapixels": 0, "target_tokens": None}
    table = accounting_markdown([sample], 1000)
    assert "unmeasured assumption" in table
    assert "2.571–4.286" in table
    assert "2.934–4.649" in table


def test_eval_shell_dry_run_covers_all_twenty_combinations(tmp_path):
    root = Path(__file__).resolve().parents[1]
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    bash = str(git_bash) if git_bash.is_file() else shutil.which("bash")
    if not bash:
        pytest.skip("bash not installed; no downloads for shell verification")
    result = subprocess.run(
        [bash, str(root / "scripts/run_prompt_budget_eval.sh"), "--dry-run",
         "--output-dir", str(tmp_path / "reports")],
        cwd=root, env={**os.environ, "PYTHON": sys.executable, "DATA_ROOT": str(root)},
        text=True, capture_output=True, check=True,
    )
    commands = result.stdout.splitlines()
    identity = [line for line in commands if "eval_slow_system.py" in line]
    planning = [line for line in commands if "plan_from_task.py" in line]
    assert len(identity) == len(planning) == 10
    assert all("--no-prompt-cache" in line for line in identity + planning)
    assert all("--identity-only" in line for line in identity)
    assert all("--sorted-keys" in line for line in planning)
    assert sum("prompt_budget.py collect" in line for line in commands) == 1
    assert not (tmp_path / "reports").exists()


def test_accounting_provenance_supports_tokenizer_config_template_fallback(tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    for name in ("tokenizer.json", "tokenizer_config.json", "preprocessor_config.json"):
        (snapshot / name).write_text("{}", encoding="utf-8")
    for name in ("slow-system-sft-v2-r4-prop", "flight-identity-eval-v1"):
        dataset = tmp_path / "artifacts/datasets" / name
        dataset.mkdir(parents=True)
        (dataset / "test.jsonl").write_text("{}\n", encoding="utf-8")
    tokenizer = OfflineTokenizer.__new__(OfflineTokenizer)
    tokenizer.snapshot = snapshot
    monkeypatch.setattr(prompt_budget, "OfflineTokenizer", lambda path: tokenizer)
    monkeypatch.setattr(prompt_budget, "account_identity", lambda *args: [])
    monkeypatch.setattr(prompt_budget, "account_planning", lambda *args: [])
    output = tmp_path / "report"
    assert prompt_budget.main(["account", "--data-root", str(tmp_path),
                               "--output-dir", str(output)]) == 0
    provenance = json.loads((output / "token-accounting.json").read_text("utf-8"))
    assert "tokenizer_config.json" in provenance["files_sha256"]
    assert "chat_template.json" not in provenance["files_sha256"]
