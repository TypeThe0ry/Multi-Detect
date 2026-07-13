from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import multidetect.cli as cli_module
from multidetect.cli import main
from multidetect.jetson_bench import (
    JetsonVisionBenchConfig,
    read_jetson_device_model,
    read_temperatures_c,
    run_jetson_vision_bench,
)

OBSERVED_AT = datetime(2026, 7, 13, 4, 30, tzinfo=UTC)


class _StepClock:
    def __init__(self, step: float = 0.01) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


class _Source:
    reconnect_count = 1

    def __init__(self, frame_count: int = 3) -> None:
        self.remaining = frame_count

    def read(self):
        self.remaining -= 1
        return SimpleNamespace(width=1280, height=720, image_bgr=object())


class _Detector:
    provider_names = ("CUDAExecutionProvider", "CPUExecutionProvider")

    def detect(self, _image):
        return (object(),)


def _observed_at() -> datetime:
    return OBSERVED_AT


def test_jetson_bench_passes_only_with_orin_gpu_temperature_and_soak() -> None:
    result = run_jetson_vision_bench(
        _Source(),
        _Detector(),
        JetsonVisionBenchConfig(
            minimum_frames=3,
            minimum_duration_seconds=0,
            maximum_duration_seconds=1,
            maximum_temperature_c=95,
        ),
        device_model_reader=lambda: "NVIDIA Jetson Orin Nano Engineering Reference\x00",
        temperature_reader=lambda: (55.0, 61.5),
        clock=_StepClock(),
        observed_at=_observed_at,
    )

    assert result["event"] == "jetson_orin_nano_bench_passed"
    assert result["passed"] is True
    assert result["hardware_observed"] is True
    assert result["device_model"] == "Jetson Orin Nano"
    assert result["active_inference_provider"] == "CUDAExecutionProvider"
    assert result["processed_frames"] == 3
    assert result["detections_processed"] == 3
    assert result["maximum_temperature_c"] == 61.5
    assert result["physical_release_enabled"] is False


def test_jetson_bench_rejects_cpu_fallback_and_non_jetson_host() -> None:
    class _CpuDetector(_Detector):
        provider_names = ("CPUExecutionProvider",)

    result = run_jetson_vision_bench(
        _Source(),
        _CpuDetector(),
        JetsonVisionBenchConfig(
            minimum_frames=1,
            minimum_duration_seconds=0,
            maximum_duration_seconds=1,
        ),
        device_model_reader=lambda: "Windows development workstation",
        temperature_reader=lambda: (45.0,),
        clock=_StepClock(),
        observed_at=_observed_at,
    )

    assert result["event"] == "jetson_orin_nano_bench_failed"
    assert result["passed"] is False
    assert result["hardware_observed"] is False
    assert "system model is not recognized as Jetson Orin Nano" in result["reasons"]
    assert "TensorRT or CUDA inference provider is not active" in result["reasons"]


def test_jetson_bench_fails_when_temperature_exceeds_limit() -> None:
    result = run_jetson_vision_bench(
        _Source(frame_count=1),
        _Detector(),
        JetsonVisionBenchConfig(
            minimum_frames=1,
            minimum_duration_seconds=0,
            maximum_duration_seconds=1,
            maximum_temperature_c=80,
        ),
        device_model_reader=lambda: "Jetson Orin Nano",
        temperature_reader=lambda: (81.0,),
        clock=_StepClock(),
        observed_at=_observed_at,
    )

    assert result["passed"] is False
    assert result["maximum_temperature_c"] == 81.0
    assert "Jetson temperature exceeded the configured limit" in result["reasons"]


def test_jetson_device_and_temperature_readers_handle_sysfs(tmp_path: Path) -> None:
    model = tmp_path / "model"
    model.write_text("NVIDIA Jetson Orin Nano\x00", encoding="utf-8")
    thermal = tmp_path / "thermal"
    zone0 = thermal / "thermal_zone0"
    zone1 = thermal / "thermal_zone1"
    zone0.mkdir(parents=True)
    zone1.mkdir(parents=True)
    (zone0 / "temp").write_text("62500\n", encoding="utf-8")
    (zone1 / "temp").write_text("54.5\n", encoding="utf-8")

    assert "Jetson Orin Nano" in read_jetson_device_model(model)
    assert read_temperatures_c(thermal) == (62.5, 54.5)


def test_jetson_bench_cli_writes_bound_evidence(tmp_path: Path, monkeypatch, capsys) -> None:
    model = tmp_path / "model.onnx"
    manifest = tmp_path / "model.manifest.json"
    output = tmp_path / "jetson-bench.json"
    model.write_bytes(b"test-model")
    manifest.write_text("{}", encoding="utf-8")
    verified = SimpleNamespace(
        model_version="test-v1",
        model_role="fire_candidate",
        status="quarantined",
        production_approved=False,
    )

    class _CliDetector:
        def __init__(self, _config) -> None:
            pass

    class _CliSource:
        def __init__(self, _config) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(cli_module, "_verify_optional_model_manifest", lambda **_kwargs: verified)
    monkeypatch.setattr(cli_module, "OnnxNx6Detector", _CliDetector)
    monkeypatch.setattr(cli_module, "OpenCVFrameSource", _CliSource)
    monkeypatch.setattr(
        cli_module,
        "run_jetson_vision_bench",
        lambda *_args: {
            "event": "jetson_orin_nano_bench_passed",
            "passed": True,
            "production_approved": False,
        },
    )

    assert (
        main(
            [
                "jetson-vision-bench",
                "--source",
                "0",
                "--onnx-model",
                str(model),
                "--model-manifest",
                str(manifest),
                "--out",
                str(output),
            ]
        )
        == 0
    )

    emitted = json.loads(capsys.readouterr().out)
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written == emitted
    assert written["model_version"] == "test-v1"
    assert written["model_role"] == "fire_candidate"
    assert written["manifest_production_approved"] is False
