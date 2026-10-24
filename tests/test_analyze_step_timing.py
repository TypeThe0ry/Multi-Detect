import json
from pathlib import Path

from scripts.analyze_step_timing import analyze_steps, main


def _write(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def test_legacy_rows_have_coarse_breakdown(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "steps.jsonl",
        [
            {"step_index": 0, "receive_monotonic_ns": 1_000_000_000, "outcome": {"elapsed_s": 1.0}, "labels": {"onboard": {"fast_latency_s": 0.1, "slow_calls": [{"latency_s": 0.2}]}}},
            {"step_index": 1, "receive_monotonic_ns": 3_500_000_000, "outcome": {"elapsed_s": 1.0}, "labels": {"onboard": {"fast_latency_s": 0.1, "slow_calls": [{"latency_s": 0.2}]}}},
        ],
    )
    report = analyze_steps(path)
    assert report["source"] == "legacy"
    assert report["phases"]["wall"]["mean_ms"] == 1250.0
    assert report["phases"]["primitive"]["mean_ms"] == 1000.0
    assert report["phases"]["detector"]["mean_ms"] == 100.0
    assert report["phases"]["slow_model"]["mean_ms"] == 200.0


def test_fine_nested_timing_and_cli_json(tmp_path: Path, capsys) -> None:
    path = _write(
        tmp_path / "steps.jsonl",
        [{"task_text": "demo", "timing": {"total_ms": 20, "phases": {"before_frame_ms": 3, "policy": {"detector_ms": 5}}}}],
    )
    report = analyze_steps(path)
    assert report["source"] == "fine"
    assert report["phases"]["before_frame_ms"]["median_ms"] == 3.0
    assert report["phases"]["policy.detector_ms"]["p95_ms"] == 5.0
    assert main([str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["episodes"][0]["source"] == "fine"
