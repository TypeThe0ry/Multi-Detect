from pathlib import Path

from scripts import run_planner_round as round_runner
from scripts.run_planner_round import replacement_decision, stage_current


def test_replacement_decision_requires_all_gates():
    baseline = {"planning-v3": {"exact_sequence_rate": 0.60}, "planning-v4": {"category_metrics": {
        "return_intent": {"count": 2, "accuracy": 0.5}, "unknown_place": {"count": 2, "accuracy": 0.5},
        "missing_payload_drop": {"count": 2, "accuracy": 0.5}}}}
    candidate = {"planning-v3": {"exact_sequence_rate": 0.61}, "planning-v4": {"category_metrics": {
        "return_intent": {"count": 2, "accuracy": 0.62}, "unknown_place": {"count": 2, "accuracy": 0.5},
        "missing_payload_drop": {"count": 2, "accuracy": 0.5}}}}
    passed = [{"ok": True}] * 15
    assert replacement_decision(candidate, baseline, passed, [])["replace_r14"] is True
    assert replacement_decision(candidate, baseline, passed[:14], [])["replace_r14"] is False
    candidate["planning-v3"]["exact_sequence_rate"] = 0.59
    assert replacement_decision(candidate, baseline, passed, [])["replace_r14"] is False


def test_replacement_decision_rejects_lower_v3_validity():
    baseline = {"planning-v3": {"exact_sequence_rate": 0.60, "valid_rate": 0.9375},
                "planning-v4": {"category_metrics": {"return_intent": {"count": 2, "accuracy": 0.5}}}}
    candidate = {"planning-v3": {"exact_sequence_rate": 0.61, "valid_rate": 0.875},
                 "planning-v4": {"category_metrics": {"return_intent": {"count": 2, "accuracy": 1.0}}}}
    decision = replacement_decision(candidate, baseline, [{"ok": True}] * 15, [])
    assert decision["replace_r14"] is False
    assert "planning-v3 有效率低于 r14" in decision["reasons"]
    candidate["planning-v3"]["valid_rate"] = 0.9375
    assert replacement_decision(candidate, baseline, [{"ok": True}] * 15, [])["replace_r14"] is True


def test_stage_current_requires_matching_hashes(tmp_path: Path):
    out = tmp_path
    for name in ("manifest.json", "train.jsonl", "val.jsonl"):
        path = out / "dataset" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
    import hashlib
    hashes = {}
    for path in (out / "dataset/manifest.json", out / "dataset/train.jsonl", out / "dataset/val.jsonl"):
        hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    state = {"stages": {"data": {"status": "complete", "sha256": hashes}}}
    assert stage_current("data", out, state)
    (out / "dataset/train.jsonl").write_text("changed\n", encoding="utf-8")
    assert not stage_current("data", out, state)


def test_dry_run_prints_commands_without_running_them(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(round_runner, "SCRATCH", tmp_path)
    out = tmp_path / "dry-run"
    assert round_runner.main(["--stage", "all", "--dry-run", "--out", str(out)]) == 0
    output = capsys.readouterr().out
    assert "build_plan_sft.py" in output
    assert str(round_runner.VLM_PYTHON) in output
    assert "run_planner_probe.py" in output
    assert "--learning-rate 0.0001" in output
    assert "--r20-plan-intent-count 40" in output
    assert "--replay-planner-train" not in output
    assert not (out / "round-state.json").exists()


def test_r22_options_are_explicit_without_changing_r21_defaults(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(round_runner, "SCRATCH", tmp_path)
    assert round_runner.main([
        "--stage", "all", "--dry-run", "--out", str(tmp_path / "r22"),
        "--intent-count", "80", "--replay-planner-train", "r14-train.jsonl",
        "--negative-target-mode", "rejected_candidate", "--learning-rate", "5e-5",
    ]) == 0
    output = capsys.readouterr().out
    assert "--r20-plan-intent-count 80" in output
    assert "--replay-planner-train r14-train.jsonl" in output
    assert "--negative-target-mode rejected_candidate" in output
    assert "--learning-rate 5e-05" in output


def test_r23_recipe_selects_rebalanced_data_and_learning_rate(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(round_runner, "SCRATCH", tmp_path)
    assert round_runner.main([
        "--stage", "all", "--dry-run", "--out", str(tmp_path / "r23"),
        "--eval-root", str(tmp_path / "eval"), "--r23-recipe",
        "--replay-planner-train", "r14-train.jsonl",
    ]) == 0
    output = capsys.readouterr().out
    assert "--r23-recipe --r23-v3-count 600 --r23-deliver-count 80" in output
    assert "--epochs 1" in output
    assert "--learning-rate 2.5e-05" in output


def test_r24_recipe_transmits_reference_families_and_learning_rate(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(round_runner, "SCRATCH", tmp_path)
    assert round_runner.main([
        "--stage", "all", "--dry-run", "--out", str(tmp_path / "r24"),
        "--r24-recipe", "--r24-reference-count", "12", "--r24-unknown-count", "8",
        "--r24-context-count", "6",
    ]) == 0
    output = capsys.readouterr().out
    assert "--r24-recipe --r24-reference-count 12 --r24-unknown-count 8 --r24-context-count 6" in output
    assert "--learning-rate 2e-05" in output


def test_r25_recipe_and_model_paths_are_transmitted(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(round_runner, "SCRATCH", tmp_path)
    init_adapter = tmp_path / "r24-adapter"
    baseline = tmp_path / "r24.gguf"
    assert round_runner.main([
        "--stage", "all", "--dry-run", "--out", str(tmp_path / "r25"),
        "--r25-recipe", "--r25-missing-payload-count", "12",
        "--r25-negated-query-count", "14", "--init-adapter", str(init_adapter),
        "--baseline-gguf", str(baseline), "--baseline-label", "r24",
    ]) == 0
    output = capsys.readouterr().out
    assert "--r23-recipe --r23-v3-count 600 --r23-deliver-count 80" in output
    assert "--r24-recipe --r24-reference-count 80 --r24-unknown-count 80 --r24-context-count 80" in output
    assert "--r25-recipe --r25-missing-payload-count 12 --r25-negated-query-count 14" in output
    assert f"--init-adapter {init_adapter}" in output
    assert f"--gguf {baseline}" in output
    assert "--learning-rate 2e-05" in output


def test_r26_recipe_passes_refusal_count_without_r25_negative_target_mode(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(round_runner, "SCRATCH", tmp_path)
    assert round_runner.main([
        "--stage", "data", "--dry-run", "--out", str(tmp_path / "r26"),
        "--r26-recipe", "--r26-refusal-count", "24",
    ]) == 0
    output = capsys.readouterr().out
    assert "--r26-recipe --r26-refusal-count 24" in output
    assert "--r24-recipe" in output
    assert "--r25-recipe" not in output


def test_r26_recipe_enables_decision_contract_for_candidate_and_baseline(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(round_runner, "SCRATCH", tmp_path)
    assert round_runner.main([
        "--stage", "eval", "--dry-run", "--out", str(tmp_path / "r26"), "--r26-recipe",
    ]) == 0
    assert capsys.readouterr().out.count("--decision-contract") == 4


def test_no_decision_contract_overrides_r26_auto_enable(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(round_runner, "SCRATCH", tmp_path)
    assert round_runner.main([
        "--stage", "eval", "--dry-run", "--out", str(tmp_path / "r26"), "--r26-recipe",
        "--no-decision-contract",
    ]) == 0
    assert "--decision-contract" not in capsys.readouterr().out


def test_replacement_reasons_use_configured_baseline_label():
    baseline = {"planning-v3": {"exact_sequence_rate": 0.6},
                "planning-v4": {"category_metrics": {}}}
    candidate = {"planning-v3": {"exact_sequence_rate": 0.5},
                 "planning-v4": {"category_metrics": {}}}
    decision = replacement_decision(candidate, baseline, [], [], "r24")
    assert "planning-v3 低于 r24" in decision["reasons"]


def test_default_lock_is_the_shared_sitl_lock_directory():
    # The lock is the mkdir directory sitl.lock; sitl_lock.sh is the shell helper.
    # Pointing SitlLock at the script made mkdir fail forever (or a stale reclaim
    # touch the helper itself).
    import scripts.run_planner_round as mod

    parser_source = open(mod.__file__, encoding="utf-8").read()
    assert r'_scratch\sitl.lock"' in parser_source
    assert "sitl_lock.sh\")" not in parser_source


def test_train_outputs_match_the_trainer_layout(tmp_path: Path):
    # train_slow_system_lora.py --output <out>/adapter saves the LoRA under
    # <out>/adapter/adapter and its report beside it; the r21 round trained for
    # two hours and then failed because this check looked one level up.
    out = tmp_path / "round"
    config, report = round_runner.stage_outputs("train", out)
    assert config == out / "adapter" / "adapter" / "adapter_config.json"
    assert report == out / "adapter" / "training-report.json"
