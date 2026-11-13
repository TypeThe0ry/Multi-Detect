import json
import sys

from scripts import run_planner_probe as probe


def test_plan_pregate_hit_is_reported_without_starting_llama_server(tmp_path, monkeypatch):
    tasks = tmp_path / "tasks.json"
    output = tmp_path / "probe.json"
    tasks.write_text(json.dumps({"refs": [], "tasks": [{
        "id": "unknown", "text": "Fly to the nonexistent Raven Pier.", "expect": [[]],
    }]}), encoding="utf-8")

    def forbidden(*args, **kwargs):
        raise AssertionError("all tasks were preflighted; llama-server must not start")

    monkeypatch.setattr(probe, "LlamaServerRunner", forbidden)
    monkeypatch.setattr(sys, "argv", ["run_planner_probe.py"])
    assert probe.main([
        "--tasks", str(tasks), "--model", str(tmp_path / "model.gguf"),
        "--label", "test", "--out", str(output), "--plan-pregate",
    ]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["host_pregate"] == 1
    assert report["summary"]["model_processed"] == 0
    assert report["rows"][0]["source"] == "host_pregate"
