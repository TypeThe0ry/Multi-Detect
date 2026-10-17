from __future__ import annotations

import ast
import json
import threading
from unittest.mock import Mock

import pytest

from scripts import bench_orin_onboard as bench
from scripts.bench_orin_onboard import (
    aggregate_latencies,
    aggregate_tegrastats_samples,
    evaluate_budgets,
    parse_cuda_device_index,
    parse_monitor_pid,
    parse_tegrastats_line,
)


def test_budget_evaluation_marks_fast_identity_and_memory_pass() -> None:
    report = {
        "phases": {
            "yoloe_tensorrt_imgsz_1280": {
                "latency": {"p95_s": 0.08},
                "resources": {"peak_ram_used_mb": 5000},
            },
            "r12_fast_identity": {
                "latency": {"p95_s": 0.9},
                "resources": {"peak_ram_used_mb": 5200},
            },
        }
    }
    result = evaluate_budgets(report)
    assert result["status"] == "pass"
    assert result["all_required_met"] is True
    assert result["checks"]["fast_system_hz"]["value"] == 12.5


def test_budget_evaluation_distinguishes_unmeasured_from_failure() -> None:
    result = evaluate_budgets({"phases": {"r12_fast_identity": {"latency": {"p95_s": 1.2}}}})
    assert result["status"] == "not_measured"
    assert result["checks"]["identity_p95_s"]["status"] == "fail"
    assert result["checks"]["fast_system_hz"]["status"] == "not_measured"


def test_chinese_report_contains_budget_table(tmp_path) -> None:
    report = {
        "generated_at_utc": "2026-10-17T00:00:00Z",
        "status": "measured",
        "system": {"platform": "test", "machine": "x86"},
        "phases": {},
        "budgets": evaluate_budgets({"phases": {}}),
    }
    path = tmp_path / "report.md"
    bench._write_markdown_chinese(report, path)
    text = path.read_text(encoding="utf-8")
    assert "预算判定" in text
    assert "未测量" in text

ORIN_NANO_LINE = (
    "RAM 3342/7620MB (lfb 638x4MB) SWAP 0/3810MB (cached 0MB) "
    "CPU [4%@729,off,2%@729,0%@729,off,1%@729] GR3D_FREQ 4%@[306] "
    "EMC_FREQ 3%@2133 PLL@38.0C CPU@45.125C GPU@43.25C tj@47.0C "
    "VDD_IN 3450mW/3400mW"
)

ORIN_NX_LINE = (
    "RAM 7284/15610MB (lfb 1240x4MB) SWAP 0/7805MB (cached 0MB) "
    "CPU [12%@729,8%@729,off,0%@729,3%@729,off,1%@729,0%@729] "
    "GR3D_FREQ 56%@918 EMC_FREQ 27%@3200 CPU@55.125C GPU@51.5C "
    "thermal@53.0C VDD_IN 8520mW/8250mW"
)


def test_parse_orin_nano_tegrastats_line() -> None:
    parsed = parse_tegrastats_line(ORIN_NANO_LINE)

    assert parsed == {
        "ram_used_mb": 3342,
        "ram_total_mb": 7620,
        "gr3d_percent": 4,
        "cpu_loads_percent": [4, None, 2, 0, None, 1],
        "temperatures_c": {"PLL": 38.0, "CPU": 45.125, "GPU": 43.25, "tj": 47.0},
        "vdd_in_mw": 3450,
        "vdd_in_avg_mw": 3400,
    }


def test_parse_orin_nx_line_with_optional_fields_and_clock_formats() -> None:
    parsed = parse_tegrastats_line(ORIN_NX_LINE)

    assert parsed is not None
    assert parsed["ram_used_mb"] == 7284
    assert parsed["ram_total_mb"] == 15610
    assert parsed["gr3d_percent"] == 56
    assert parsed["cpu_loads_percent"] == [12, 8, None, 0, 3, None, 1, 0]
    assert parsed["temperatures_c"] == {
        "CPU": 55.125,
        "GPU": 51.5,
        "thermal": 53.0,
    }
    assert parsed["vdd_in_mw"] == 8520
    assert parsed["vdd_in_avg_mw"] == 8250


def test_parse_tegrastats_line_keeps_memory_when_optional_metrics_are_absent() -> None:
    parsed = parse_tegrastats_line("RAM 1000/7620MB (lfb 20x4MB) SWAP 0/3810MB")

    assert parsed == {
        "ram_used_mb": 1000,
        "ram_total_mb": 7620,
        "gr3d_percent": None,
        "cpu_loads_percent": [],
        "temperatures_c": {},
        "vdd_in_mw": None,
        "vdd_in_avg_mw": None,
    }
    assert parse_tegrastats_line("tegrastats startup diagnostic") is None


def test_aggregate_tegrastats_samples_reports_peaks_and_unified_memory_boundary() -> None:
    samples = [parse_tegrastats_line(ORIN_NANO_LINE), parse_tegrastats_line(ORIN_NX_LINE)]

    assert aggregate_tegrastats_samples([sample for sample in samples if sample]) == {
        "sample_count": 2,
        "peak_ram_used_mb": 7284.0,
        "ram_total_mb": 15610.0,
        "peak_gpu_shared_memory_mb": None,
        "peak_gr3d_percent": 56.0,
        "peak_cpu_core_load_percent": 12.0,
        "peak_temperatures_c": {
            "PLL": 38.0,
            "CPU": 55.125,
            "GPU": 51.5,
            "tj": 47.0,
            "thermal": 53.0,
        },
        "peak_vdd_in_mw": 8520.0,
        "peak_vdd_in_avg_mw": 8250.0,
        "gpu_shared_memory_note": (
            "tegrastats reports unified RAM use, but not a separate GPU-shared allocation."
        ),
    }


def test_aggregate_latency_summary_uses_median_and_nearest_rank_p95() -> None:
    assert aggregate_latencies([0.5, 0.2, 0.4, 0.3, 0.1]) == {
        "count": 5,
        "min_s": 0.1,
        "p50_s": 0.3,
        "p95_s": 0.5,
        "max_s": 0.5,
        "mean_s": 0.3,
    }
    assert aggregate_latencies([]) == {
        "count": 0,
        "min_s": None,
        "p50_s": None,
        "p95_s": None,
        "max_s": None,
        "mean_s": None,
    }


@pytest.mark.parametrize("samples", [[-0.1], [float("nan")], [float("inf")]])
def test_aggregate_latency_rejects_invalid_values(samples: list[float]) -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        aggregate_latencies(samples)


def test_parse_monitor_pid_accepts_named_external_process() -> None:
    assert parse_monitor_pid("camera-service=4321") == ("camera-service", 4321)


@pytest.mark.parametrize("value, expected", [("0", 0), ("cuda:1", 1)])
def test_parse_cuda_device_index(value: str, expected: int) -> None:
    assert parse_cuda_device_index(value) == expected


def test_parse_cuda_device_index_rejects_multiple_or_non_cuda_devices() -> None:
    with pytest.raises(ValueError, match="one CUDA index"):
        parse_cuda_device_index("0,1")


def test_raw_tensorrt_engine_gets_vocabulary_metadata_without_mutating_source(tmp_path) -> None:
    raw = tmp_path / "proposer.engine"
    raw.write_bytes(b"raw serialized TensorRT bytes")

    wrapped = bench._prepare_tensorrt_engine(raw)

    assert raw.read_bytes() == b"raw serialized TensorRT bytes"
    prefix_length = int.from_bytes(wrapped.read_bytes()[:4], "little")
    metadata = json.loads(wrapped.read_bytes()[4 : 4 + prefix_length])
    assert metadata["task"] == "detect"
    assert len(ast.literal_eval(metadata["names"])) == 20


@pytest.mark.parametrize("value", ["camera=0", "camera=abc", "=4321", "camera service=4321"])
def test_parse_monitor_pid_rejects_invalid_process_specification(value: str) -> None:
    with pytest.raises(ValueError, match="label=positive_integer"):
        parse_monitor_pid(value)


def test_latency_even_median_and_p95_rank_boundary() -> None:
    summary = aggregate_latencies(list(range(1, 21)))
    assert summary["p50_s"] == 10.5
    assert summary["p95_s"] == 19


def test_rss_polling_continues_without_gpu_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    polled_in_background = threading.Event()
    main_thread = threading.current_thread()

    def rss(pid: int) -> int:
        if threading.current_thread() is not main_thread:
            polled_in_background.set()
            return 4096
        return 1024

    monkeypatch.setattr(bench.shutil, "which", lambda name: None)
    monkeypatch.setattr(bench, "_process_rss", rss)
    sampler = bench.ResourceSampler({"python": 1234}, interval_ms=10)
    sampler.start()
    try:
        assert polled_in_background.wait(timeout=2)
    finally:
        sampler.close()
    report = sampler.report()
    assert report["status"] == "not measured"
    assert report["peak_ram_used_mb"] is None
    assert report["peak_rss_bytes_by_process"] == {"python": 4096}
    assert not sampler.rss_thread.is_alive()


def test_failed_tegrastats_stream_falls_back_to_nvidia_smi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sampler = bench.ResourceSampler({}, interval_ms=10)
    sampler.source = "tegrastats"
    sampler.process = Mock(stdout=iter(["unsupported platform\n"]))
    fallback = Mock()
    monkeypatch.setattr(bench.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(sampler, "_read_nvidia_smi", fallback)
    sampler._read_tegrastats()
    assert sampler.source == "nvidia-smi"
    sampler.process.wait.assert_called_once()
    fallback.assert_called_once()


def test_nvidia_smi_telemetry_uses_selected_cuda_device(monkeypatch: pytest.MonkeyPatch) -> None:
    sampler = bench.ResourceSampler({}, interval_ms=10, gpu_index=2)
    sampler.source = "nvidia-smi"

    def check_output(command: list[str], **kwargs: object) -> str:
        sampler.stop_event.set()
        assert "--id=2" in command
        return "100, 8192, 40\n"

    monkeypatch.setattr(bench, "_system_memory_mb", lambda: (1000, 8000))
    monkeypatch.setattr(bench.subprocess, "check_output", check_output)
    sampler._read_nvidia_smi()
    assert sampler.samples[0]["gpu_memory_used_mb"] == 100
    assert sampler.samples[0]["gr3d_percent"] == 40


def test_identity_warmup_cycles_rows_and_is_excluded_from_samples() -> None:
    server = Mock()
    server.identity_call.side_effect = [
        {"latency_s": value} for value in [9, 9, 9, 0.1, 0.2, 0.3, 0.4]
    ]
    rows = [{"focus_track_id": "a"}, {"focus_track_id": "b"}]
    samples, errors = bench._repeat_identity(server, rows, 2, 3, 128)
    assert not errors
    assert [sample["latency_s"] for sample in samples] == [0.1, 0.2, 0.3, 0.4]
    assert server.identity_call.call_count == 7
    assert "_max_tokens" not in rows[0]
