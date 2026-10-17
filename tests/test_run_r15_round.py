from __future__ import annotations

import json
import shutil
import subprocess
import sys
import types
from argparse import Namespace
from pathlib import Path

from scripts.run_r15_round import Round, compare_metrics, distribution, merge_without_copies, stratified_replay
from scripts.train_slow_system_lora import initialize_adapter, resolve_adapter_weights


def row(sample_id: str, decision: str, side: int = 10, category: str = "backpack") -> dict:
    return {
        "sample_id": sample_id,
        "task_category": category,
        "candidate_short_side_px": side,
        "assistant_target": {"decision": decision},
        "candidate_box": [0, 0, side, side],
    }


def test_merge_rejects_positive_copy_with_new_id_and_keeps_hard_negative():
    base = [row("base-pos", "confirm_target"), row("base-neg", "reject_track", 20)]
    additions = [row("renamed-copy", "confirm_target"), row("new-neg", "reject_track", 8)]
    merged = merge_without_copies(base, additions)
    assert [item["sample_id"] for item in merged] == ["base-pos", "base-neg", "new-neg"]
    stats = distribution(merged)
    assert stats["backpack|<12|negative"] == 1
    assert stats["backpack|<12|positive"] == 1
    assert stats["backpack|20-32|negative"] == 1


def test_acceptance_rules_require_all_four_gates():
    old = {"v1_identity_accuracy": .90, "v3_identity_accuracy": .785, "v3_under12_recall": .46, "v3_distractor_rejection": .905}
    good = {"v1_identity_accuracy": .90, "v3_identity_accuracy": .79, "v3_under12_recall": .56, "v3_distractor_rejection": .89}
    ok, reasons = compare_metrics(old, good)
    assert ok and not reasons
    bad = dict(good, v3_under12_recall=.55, v3_distractor_rejection=.88)
    ok, reasons = compare_metrics(old, bad)
    assert not ok
    assert any("<12" in reason for reason in reasons)
    assert any("干扰物" in reason for reason in reasons)


def test_replay_is_stratified_and_bounded():
    rows = [row(f"p{i}", "confirm_target", 8 if i % 2 else 24) for i in range(20)]
    rows += [row(f"n{i}", "reject_track", 8 if i % 2 else 24) for i in range(20)]
    replay = stratified_replay(rows, 0.25)
    assert len(replay) >= 4
    assert {item["assistant_target"]["decision"] for item in replay} == {"confirm_target", "reject_track"}
    assert {item["candidate_short_side_px"] for item in replay} == {8, 24}


def test_adapter_path_resolver_accepts_directory(tmp_path: Path):
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    weights = adapter / "adapter_model.safetensors"
    weights.write_bytes(b"synthetic")
    assert resolve_adapter_weights(adapter) == weights


def test_initialize_adapter_loads_weights_without_optimizer(monkeypatch, tmp_path: Path):
    adapter = tmp_path / "adapter_model.safetensors"
    adapter.write_bytes(b"synthetic")
    calls = {}
    peft = types.ModuleType("peft")
    peft.set_peft_model_state_dict = lambda model, weights: calls.update(model=model, weights=weights) or types.SimpleNamespace(unexpected_keys=[], missing_keys=[])
    safe = types.ModuleType("safetensors.torch")
    safe.load = lambda payload: {"lora": payload}
    monkeypatch.setitem(sys.modules, "peft", peft)
    monkeypatch.setitem(sys.modules, "safetensors", types.ModuleType("safetensors"))
    monkeypatch.setitem(sys.modules, "safetensors.torch", safe)
    initialize_adapter("model", adapter)
    assert calls["weights"]["lora"] == b"synthetic"


def test_stage_resume_writes_manifest_and_skips(capsys):
    out = Path.cwd() / ".r15-test-work" / "round"
    shutil.rmtree(out.parent, ignore_errors=True)
    args = Namespace(out=out, dry_run=False, min_free_gb=0, min_free_memory_gb=0)
    runner = Round(args)
    output = out / "tiny.txt"
    runner.run("tiny", [sys.executable, "-c", f"open(r'{output}', 'w', encoding='utf-8').write('ok')"], estimate="1 秒", disk="1 KiB", outputs=(output,))
    assert output.read_text() == "ok"
    manifest = out / "manifests" / "tiny.json"
    assert manifest.is_file()
    runner = Round(args)
    runner.run("tiny", [sys.executable, "-c", "raise SystemExit(99)"], estimate="1 秒", disk="1 KiB", outputs=(output,))
    assert "skip" in capsys.readouterr().out
    shutil.rmtree(out.parent, ignore_errors=True)
