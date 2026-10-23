"""Evaluation harness: a crashed llama-server or a missing frame is never a model answer.

r19: llama-server died of CUDA OOM mid-run three times, and a second run scored
484 of 1,900 identity rows as "invalid model output" because their frames were
missing under --data-root.
"""

from __future__ import annotations

import io
import json
import urllib.error
from types import SimpleNamespace

import pytest
from onboard_factories import make_llama_runner

from scripts import eval_slow_system as slow


class _Process:
    def __init__(self, returncode=None):
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def _refused(*args, **kwargs):
    raise ConnectionRefusedError("refused")


def _eval_runner(monkeypatch, tmp_path, *, max_restarts=2):
    log = tmp_path / "server.log"
    log.write_text("E CUDA error: out of memory\n", encoding="utf-8")
    runner = make_llama_runner(
        port=1,
        max_restarts=max_restarts,
        restarts=[],
        process=_Process(returncode=3221226505),
        server_log=SimpleNamespace(name=str(log), flush=lambda: None),
    )
    started = []

    def fake_start():
        started.append(True)
        runner.process = _Process()

    monkeypatch.setattr(runner, "_start", fake_start)
    monkeypatch.setattr(slow.time, "sleep", lambda seconds: None)
    return runner, started


def test_a_crashed_server_is_restarted_and_the_same_request_retried(monkeypatch, tmp_path):
    runner, started = _eval_runner(monkeypatch, tmp_path)
    bodies = []

    def http(url, body=None, timeout=None):
        bodies.append(body)
        if not started:
            raise ConnectionRefusedError("refused")
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(slow, "_http_json", http)
    response = runner._completion({"row": 7})
    assert response["choices"][0]["message"]["content"] == "ok"
    assert bodies == [{"row": 7}, {"row": 7}]
    assert len(started) == 1
    assert len(runner.restarts) == 1
    assert runner.restarts[0]["state"] == "exited with 3221226505"
    assert "out of memory" in runner.restarts[0]["log_tail"]


def test_restart_budget_exhausted_stops_the_evaluation(monkeypatch, tmp_path):
    runner, started = _eval_runner(monkeypatch, tmp_path, max_restarts=1)

    def always_dead(url, body=None, timeout=None):
        runner.process = _Process(returncode=1)
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(slow, "_http_json", always_dead)
    with pytest.raises(slow.ServerDiedError, match="exited with 1"):
        runner._completion({})
    assert len(started) == 1 and len(runner.restarts) == 1


def test_a_failed_restart_is_a_dead_server_not_a_row_error(monkeypatch, tmp_path):
    runner, _ = _eval_runner(monkeypatch, tmp_path)

    def broken_start():
        raise RuntimeError("llama-server exited with 1: CUDA error: out of memory")

    monkeypatch.setattr(runner, "_start", broken_start)
    monkeypatch.setattr(slow, "_http_json", _refused)
    with pytest.raises(slow.ServerDiedError, match="restart failed"):
        runner._completion({})


def test_transient_failures_of_a_live_server_are_retried(monkeypatch, tmp_path):
    runner, started = _eval_runner(monkeypatch, tmp_path)
    runner.process = _Process()
    calls = []

    def flaky(url, body=None, timeout=None):
        calls.append(url)
        if len(calls) < 3:
            raise ConnectionResetError("reset")
        return {"choices": []}

    monkeypatch.setattr(slow, "_http_json", flaky)
    assert runner._completion({}) == {"choices": []}
    assert len(calls) == 3 and not started and not runner.restarts


def test_a_client_error_is_raised_without_restarting(monkeypatch, tmp_path):
    runner, started = _eval_runner(monkeypatch, tmp_path)
    runner.process = _Process()

    def bad_request(url, body=None, timeout=None):
        raise urllib.error.HTTPError(url, 400, "context exceeded", {}, io.BytesIO(b""))

    monkeypatch.setattr(slow, "_http_json", bad_request)
    with pytest.raises(urllib.error.HTTPError):
        runner._completion({})
    assert not started and not runner.restarts


def test_onboard_runtime_keeps_failing_fast_without_a_restart_budget(monkeypatch):
    runner = make_llama_runner(process=_Process())
    monkeypatch.setattr(slow, "_http_json", _refused)
    with pytest.raises(ConnectionRefusedError):
        runner._completion({})
    assert runner.restarts == []


def test_cache_ram_flag_reaches_the_server_command(monkeypatch, tmp_path):
    monkeypatch.setattr(slow.LlamaServerRunner, "_start", lambda self: None)
    model = tmp_path / "m.gguf"
    default = slow.LlamaServerRunner(model, executable=tmp_path / "llama-server.exe")
    assert "--cache-ram" not in default._command
    assert default.max_restarts == 0
    evaluation = slow.LlamaServerRunner(
        model, executable=tmp_path / "llama-server.exe", cache_ram_mib=0, max_restarts=3
    )
    index = evaluation._command.index("--cache-ram")
    assert evaluation._command[index + 1] == "0"
    assert evaluation.max_restarts == 3 and evaluation.restarts == []


def test_cli_defaults_disable_the_host_cache_and_allow_restarts(monkeypatch, tmp_path):
    seen = {}

    def fake_run(args):
        seen.update(vars(args))
        return {"metrics": {"harness_error_count": 0}, "rows": []}

    monkeypatch.setattr(slow, "run", fake_run)
    assert slow.main(["--backend", "gguf", "--output", str(tmp_path / "o.json")]) == 0
    assert seen["cache_ram_mib"] == 0 and seen["max_server_restarts"] == 3


def test_rows_without_a_model_answer_are_harness_errors(tmp_path, monkeypatch):
    rows = [
        {"sample_id": "a", "world": "w", "assistant_target": {"decision": "reject_track"}},
        {"sample_id": "b", "world": "w", "assistant_target": {"decision": "reject_track"}},
    ]
    answer = json.dumps(
        {
            "args": {"duration_s": None, "region": None, "track_id": "t"},
            "decision": "reject_track",
            "reason": "x",
        }
    )

    def predictor(row):
        if row["sample_id"] == "a":
            raise FileNotFoundError("000000_before.png")
        return answer, 0.1

    result = slow.evaluate_predictor(rows, predictor)
    assert result["metrics"]["harness_error_count"] == 1
    assert [row["harness_error"] for row in result["rows"]] == [True, False]

    monkeypatch.setattr(slow, "run", lambda args: result)
    assert slow.main(["--backend", "fake", "--output", str(tmp_path / "o.json")]) == 3
    assert json.loads((tmp_path / "o.json").read_text(encoding="utf-8"))["rows"]


def test_missing_frames_stop_the_run_before_any_server_starts(monkeypatch, tmp_path):
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    present = tmp_path / "frames" / "a.png"
    present.parent.mkdir()
    present.write_bytes(b"png")
    rows = [
        {"split": "test", "sample_id": "a", "image_path": "frames/a.png"},
        {"split": "test", "sample_id": "b", "image_path": "frames/missing.png"},
    ]
    (dataset / "test.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )

    def no_server(*args, **kwargs):
        raise AssertionError("llama-server must not start when frames are missing")

    monkeypatch.setattr(slow, "LlamaServerRunner", no_server)
    args = SimpleNamespace(dataset=dataset, data_root=tmp_path, crop_policy=None, backend="gguf")
    with pytest.raises(FileNotFoundError, match="1 of 2 dataset images are missing"):
        slow.run(args)
    assert slow.missing_images([{"image_path": str(present)}]) == []
    assert slow.missing_images([{"image_path": str(tmp_path / "x.png")}])
