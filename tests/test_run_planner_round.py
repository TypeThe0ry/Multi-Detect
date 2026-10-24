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
    assert "D:\\Projects\\Multi-Detect\\.venv-vlm\\Scripts\\python.exe" in output
    assert "run_planner_probe.py" in output
    assert not (out / "round-state.json").exists()
