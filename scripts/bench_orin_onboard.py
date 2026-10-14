#!/usr/bin/env python3
"""Measure the onboard YOLOE + llama.cpp path on an Orin or CUDA workstation."""

from __future__ import annotations

import argparse
import atexit
import base64
import gc
import json
import math
import os
import platform
import re
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

DEFAULT_WEIGHTS = ROOT / "artifacts/experiments/yoloe-v2-r3-seg-drop-full/train/weights/best.pt"
DEFAULT_GGUF = (
    ROOT
    / "artifacts/models/slow-system-lora-v2-r2-seg-merged-gguf"
    / "slow-system-v2r2seg-merged-Q5_K_M.gguf"
)
DEFAULT_MMPROJ = (
    ROOT / "artifacts/cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/"
    "52d6c8ffea26cc873ac5ad116f8631268d7eb503/"
    "mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf"
)
DEFAULT_IDENTITY_JSONL = ROOT / "artifacts/datasets/slow-system-sft-v2-r4-prop/test.jsonl"
DEFAULT_IMG_SIZES = (640, 960, 1280)
DEFAULT_PLANNING_TEXTS = (
    "Find the red car and return home.",
    "Count traffic cones in the yard, then return home.",
    "Inspect the parked truck and return home.",
)


def _number(value: str) -> int | float:
    return float(value) if "." in value else int(value)


def parse_monitor_pid(value: str) -> tuple[str, int]:
    """Parse an optional external process RSS monitor in ``label=pid`` form."""
    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_-]*)=(\d+)", value.strip())
    if match is None or int(match.group(2)) <= 0:
        raise ValueError("monitor PID must use label=positive_integer form")
    return match.group(1), int(match.group(2))


def parse_cuda_device_index(value: str) -> int:
    """Return a single CUDA device index from ``0`` or ``cuda:0`` syntax."""
    index = value.removeprefix("cuda:")
    if not index.isdecimal():
        raise ValueError("--device must name one CUDA index, such as 0 or cuda:1")
    return int(index)


def parse_tegrastats_line(line: str) -> dict[str, Any] | None:
    """Parse memory, load, temperature and VDD_IN fields from one tegrastats line."""
    ram = re.search(r"\bRAM\s+(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)(?:MB|MiB)\b", line)
    if ram is None:
        return None

    gr3d = re.search(r"\bGR3D_FREQ\s+(\d+(?:\.\d+)?)%", line)
    cpu_match = re.search(r"\bCPU\s+\[([^\]]*)\]", line)
    cpu_loads: list[int | float | None] = []
    if cpu_match:
        for core in cpu_match.group(1).split(","):
            load = re.search(r"(\d+(?:\.\d+)?)%@\d+", core.strip())
            cpu_loads.append(_number(load.group(1)) if load else None)

    temperatures = {
        name: _number(value)
        for name, value in re.findall(r"\b([A-Za-z][A-Za-z0-9_]*)@(-?\d+(?:\.\d+)?)C\b", line)
    }
    power = re.search(r"\bVDD_IN\s+(\d+(?:\.\d+)?)mW(?:/(\d+(?:\.\d+)?)mW)?", line)
    return {
        "ram_used_mb": _number(ram.group(1)),
        "ram_total_mb": _number(ram.group(2)),
        "gr3d_percent": _number(gr3d.group(1)) if gr3d else None,
        "cpu_loads_percent": cpu_loads,
        "temperatures_c": temperatures,
        "vdd_in_mw": _number(power.group(1)) if power else None,
        "vdd_in_avg_mw": _number(power.group(2)) if power and power.group(2) else None,
    }


def aggregate_latencies(values: Sequence[float]) -> dict[str, int | float | None]:
    """Summarize non-negative latency samples using median and nearest-rank p95."""
    samples = [float(value) for value in values]
    if any(not math.isfinite(value) or value < 0 for value in samples):
        raise ValueError("latencies must be finite and non-negative")
    if not samples:
        return {
            "count": 0,
            "min_s": None,
            "p50_s": None,
            "p95_s": None,
            "max_s": None,
            "mean_s": None,
        }
    ordered = sorted(samples)
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return {
        "count": len(ordered),
        "min_s": round(ordered[0], 4),
        "p50_s": round(statistics.median(ordered), 4),
        "p95_s": round(ordered[p95_index], 4),
        "max_s": round(ordered[-1], 4),
        "mean_s": round(statistics.fmean(ordered), 4),
    }


def aggregate_tegrastats_samples(samples: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate parsed Jetson samples without inventing GPU memory attribution."""

    def peak(key: str) -> float | None:
        values = [sample.get(key) for sample in samples]
        usable = [float(value) for value in values if isinstance(value, (int, float))]
        return round(max(usable), 2) if usable else None

    cpu_loads = [
        float(value)
        for sample in samples
        for value in sample.get("cpu_loads_percent", [])
        if isinstance(value, (int, float))
    ]
    temperatures: dict[str, float] = {}
    for sample in samples:
        for key, value in sample.get("temperatures_c", {}).items():
            temperatures[key] = max(temperatures.get(key, float("-inf")), float(value))
    ram_totals = [
        float(sample["ram_total_mb"])
        for sample in samples
        if isinstance(sample.get("ram_total_mb"), (int, float))
    ]
    return {
        "sample_count": len(samples),
        "peak_ram_used_mb": peak("ram_used_mb"),
        "ram_total_mb": ram_totals[-1] if ram_totals else None,
        "peak_gpu_shared_memory_mb": None,
        "peak_gr3d_percent": peak("gr3d_percent"),
        "peak_cpu_core_load_percent": round(max(cpu_loads), 2) if cpu_loads else None,
        "peak_temperatures_c": temperatures,
        "peak_vdd_in_mw": peak("vdd_in_mw"),
        "peak_vdd_in_avg_mw": peak("vdd_in_avg_mw"),
        "gpu_shared_memory_note": (
            "tegrastats reports unified RAM use, but not a separate GPU-shared allocation."
        ),
    }


def _read_proc_rss(pid: int) -> int | None:
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="ascii")
    except OSError:
        return None
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            fields = line.split()
            return int(fields[1]) * 1024 if len(fields) > 1 else None
    return None


def _process_rss(pid: int) -> int | None:
    try:
        import psutil

        return int(psutil.Process(pid).memory_info().rss)
    except Exception:  # psutil may report NoSuchProcess or AccessDenied during shutdown.
        return _read_proc_rss(pid)


def _system_memory_mb() -> tuple[float | None, float | None]:
    try:
        import psutil

        memory = psutil.virtual_memory()
        return (memory.total - memory.available) / (1024 * 1024), memory.total / (1024 * 1024)
    except ImportError:
        pass
    try:
        values = {}
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, value = line.split(":", 1)
            if key in {"MemTotal", "MemAvailable"}:
                values[key] = float(value.split()[0]) / 1024
        total = values.get("MemTotal")
        available = values.get("MemAvailable")
        return (
            (total - available, total)
            if total is not None and available is not None
            else (None, None)
        )
    except (OSError, ValueError, IndexError):
        return None, None


class ResourceSampler:
    """Sample system telemetry and RSS for one measurement phase."""

    def __init__(self, process_ids: dict[str, int], interval_ms: int, gpu_index: int = 0) -> None:
        self.process_ids = process_ids
        self.interval_ms = interval_ms
        self.gpu_index = gpu_index
        self.stop_event = threading.Event()
        self.samples: list[dict[str, Any]] = []
        self.peak_rss: dict[str, int] = {}
        self.source: str | None = None
        self.process: subprocess.Popen[str] | None = None
        self.thread: threading.Thread | None = None
        self.rss_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        tegrastats = shutil.which("tegrastats")
        if tegrastats:
            try:
                self.process = subprocess.Popen(
                    [tegrastats, "--interval", str(self.interval_ms)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                self.source = "tegrastats"
                self.thread = threading.Thread(target=self._read_tegrastats, daemon=True)
                self.thread.start()
            except OSError:
                self.process = None
        if self.source is None and shutil.which("nvidia-smi"):
            self.source = "nvidia-smi"
            self.thread = threading.Thread(target=self._read_nvidia_smi, daemon=True)
            self.thread.start()
        self._sample_rss()
        self.rss_thread = threading.Thread(target=self._read_rss, daemon=True)
        self.rss_thread.start()

    def _append(self, sample: dict[str, Any]) -> None:
        with self._lock:
            self.samples.append(sample)

    def _sample_rss(self) -> None:
        for label, pid in self.process_ids.items():
            rss = _process_rss(pid)
            if rss is not None:
                with self._lock:
                    self.peak_rss[label] = max(self.peak_rss.get(label, 0), rss)

    def _read_tegrastats(self) -> None:
        if self.process is None or self.process.stdout is None:
            return
        parsed_count = 0
        for line in self.process.stdout:
            if self.stop_event.is_set():
                break
            parsed = parse_tegrastats_line(line)
            if parsed is not None:
                self._append(parsed)
                parsed_count += 1
        if self.stop_event.is_set() or parsed_count:
            return

        # A found executable can still fail immediately (for example, outside a
        # Jetson container). Reap it before switching to the NVIDIA fallback.
        try:
            self.process.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        if shutil.which("nvidia-smi"):
            self.source = "nvidia-smi"
            self._read_nvidia_smi()

    def _read_rss(self) -> None:
        while not self.stop_event.is_set():
            self._sample_rss()
            self.stop_event.wait(self.interval_ms / 1000)

    def _read_nvidia_smi(self) -> None:
        while not self.stop_event.is_set():
            ram_used, ram_total = _system_memory_mb()
            sample: dict[str, Any] = {
                "ram_used_mb": ram_used,
                "ram_total_mb": ram_total,
                "gpu_memory_used_mb": None,
                "gpu_memory_total_mb": None,
                "gr3d_percent": None,
            }
            try:
                output = subprocess.check_output(
                    [
                        "nvidia-smi",
                        f"--id={self.gpu_index}",
                        "--query-gpu=memory.used,memory.total,utilization.gpu",
                        "--format=csv,noheader,nounits",
                    ],
                    text=True,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                )
                fields = [part.strip() for part in output.splitlines()[0].split(",")]
                if len(fields) >= 3:
                    sample.update(
                        {
                            "gpu_memory_used_mb": float(fields[0]),
                            "gpu_memory_total_mb": float(fields[1]),
                            "gr3d_percent": float(fields[2]),
                        }
                    )
            except (OSError, ValueError, IndexError, subprocess.SubprocessError):
                pass
            self._append(sample)
            self.stop_event.wait(self.interval_ms / 1000)

    def close(self) -> None:
        self._sample_rss()
        self.stop_event.set()
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        if self.thread is not None:
            self.thread.join(timeout=3)
        if self.rss_thread is not None:
            self.rss_thread.join(timeout=3)

    def report(self) -> dict[str, Any]:
        with self._lock:
            samples = list(self.samples)
            peak_rss = dict(self.peak_rss)
        if not self.source:
            status = "not measured"
        elif not samples:
            status = "not measured"
        else:
            status = "measured"

        def peak(key: str) -> float | None:
            values = [sample.get(key) for sample in samples]
            usable = [float(value) for value in values if isinstance(value, (int, float))]
            return round(max(usable), 2) if usable else None

        tegrastats = aggregate_tegrastats_samples(samples)
        return {
            "status": status,
            "source": self.source,
            "sample_count": len(samples),
            "peak_ram_used_mb": tegrastats["peak_ram_used_mb"],
            "ram_total_mb": tegrastats["ram_total_mb"],
            "peak_gpu_memory_used_mb": peak("gpu_memory_used_mb"),
            "peak_gpu_shared_memory_mb": tegrastats["peak_gpu_shared_memory_mb"],
            "gpu_shared_memory_note": (
                "Jetson tegrastats exposes unified RAM but not a separate GPU allocation; "
                "peak RAM includes system, camera and GPU-shared use."
                if self.source == "tegrastats"
                else "nvidia-smi memory.used is GPU memory; shared use is unknown."
                if self.source == "nvidia-smi"
                else "No tegrastats or nvidia-smi telemetry source was available."
            ),
            "peak_gr3d_percent": peak("gr3d_percent"),
            "peak_cpu_core_load_percent": tegrastats["peak_cpu_core_load_percent"],
            "peak_temperatures_c": tegrastats["peak_temperatures_c"],
            "peak_vdd_in_mw": peak("vdd_in_mw"),
            "peak_vdd_in_avg_mw": tegrastats["peak_vdd_in_avg_mw"],
            "peak_rss_bytes_by_process": peak_rss,
        }


def _measure_phase(
    name: str,
    action: Callable[[], tuple[list[dict[str, Any]], list[str]]],
    process_ids: dict[str, int],
    interval_ms: int,
    gpu_index: int = 0,
) -> dict[str, Any]:
    sampler = ResourceSampler(process_ids, interval_ms, gpu_index)
    sampler.start()
    try:
        samples, errors = action()
    except Exception as exc:
        samples, errors = [], [f"{name}: {type(exc).__name__}: {exc}"]
    finally:
        sampler.close()
    latencies = [
        float(sample["latency_s"]) for sample in samples if sample.get("latency_s") is not None
    ]
    status = "measured" if latencies else "not measured"
    return {
        "status": status,
        "latency": aggregate_latencies(latencies),
        "samples": samples,
        "errors": errors,
        "resources": sampler.report(),
    }


def _load_frames(directory: Path | None, limit: int) -> list[tuple[Path, Any]]:
    if directory is None or not directory.is_dir():
        return []
    try:
        import cv2
    except ImportError:
        return []
    frames = []
    for path in sorted(directory.glob("*.png")):
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is not None:
            frames.append((path, image))
        if limit > 0 and len(frames) >= limit:
            break
    return frames


def _run_yolo_samples(
    model: Any,
    frames: list[tuple[Path, Any]],
    *,
    image_size: int,
    repetitions: int,
    warmup: int,
    device: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    samples: list[dict[str, Any]] = []
    errors: list[str] = []

    def infer(image: Any) -> float:
        started = time.perf_counter()
        result = model.predict(
            image,
            imgsz=image_size,
            conf=0.05,
            device=device,
            half=True,
            verbose=False,
        )
        _consume_yolo_result(result)
        return time.perf_counter() - started

    try:
        for _ in range(warmup):
            infer(frames[0][1])
    except Exception as exc:
        return [], [f"warmup failed: {type(exc).__name__}: {exc}"]
    for repetition in range(repetitions):
        for path, image in frames:
            try:
                latency = infer(image)
                samples.append(
                    {"repetition": repetition + 1, "frame": path.name, "latency_s": latency}
                )
            except Exception as exc:
                errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
    return samples, errors


def _consume_yolo_result(results: Any) -> None:
    """Materialize detection outputs as the onboard proposer does."""
    if not results:
        return
    boxes = getattr(results[0], "boxes", None)
    if boxes is not None:
        boxes.xyxy.tolist()
        boxes.conf.tolist()
        boxes.cls.tolist()


def _make_pytorch_yoloe(weights: Path) -> Any:
    import torch

    from scripts.onboard_policy import YoloeProposer

    if not torch.cuda.is_available():
        raise RuntimeError("PyTorch CUDA is unavailable")
    if not weights.is_file():
        raise FileNotFoundError(f"YOLOE weights not found: {weights}")
    return YoloeProposer(weights, confidence=0.05, imgsz=1280).model


def _release_cuda_cache() -> None:
    gc.collect()
    try:
        import torch

        torch.cuda.empty_cache()
    except (ImportError, RuntimeError):
        pass


def _engine_input_shape(model: Any) -> tuple[bool, tuple[int, int] | None]:
    """Return (dynamic, fixed_hw) for an Ultralytics TensorRT backend."""
    predictor = getattr(model, "predictor", None)
    backend = getattr(predictor, "model", None)
    dynamic = bool(getattr(backend, "dynamic", False))
    candidates = []
    bindings = getattr(backend, "bindings", None)
    if bindings:
        binding = bindings.get("images", next(iter(bindings.values())))
        candidates.append(getattr(binding, "shape", None))
    candidates.append(getattr(backend, "imgsz", None))
    engine = getattr(backend, "engine", None)
    if engine is not None:
        try:
            binding_index = engine.get_binding_index("images")
            candidates.append(engine.get_binding_shape(binding_index))
        except (AttributeError, RuntimeError):
            pass
        try:
            candidates.append(engine.get_tensor_shape("images"))
        except (AttributeError, RuntimeError):
            pass
    for candidate in candidates:
        try:
            shape = tuple(int(value) for value in candidate)
        except (TypeError, ValueError):
            continue
        if len(shape) == 2 and all(value > 0 for value in shape):
            return dynamic, (shape[0], shape[1])
        if len(shape) >= 4 and all(value > 0 for value in shape[-2:]):
            return dynamic, (shape[-2], shape[-1])
    return dynamic, None


def _load_identity_rows(path: Path | None, limit: int) -> tuple[list[dict[str, Any]], str | None]:
    if path is None:
        return [], "identity JSONL path was not provided"
    if not path.is_file():
        return [], f"identity JSONL not found: {path}"
    try:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as exc:
        return [], f"could not read identity JSONL: {type(exc).__name__}: {exc}"
    eligible = [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("kind") != "mission_plan"
        and row.get("image_path")
        and row.get("focus_track_id")
        and row.get("candidates")
    ]
    for row in eligible:
        image = Path(row["image_path"])
        if not image.is_absolute():
            candidates = (path.parent / image, ROOT / image)
            image = next(
                (candidate for candidate in candidates if candidate.is_file()), candidates[0]
            )
        row["image_path"] = str(image.resolve())
    if limit > 0:
        eligible = eligible[:limit]
    if not eligible:
        return [], "JSONL contains no usable identity rows with focus_track_id and image candidates"
    return eligible, None


def _http_json(
    url: str, body: dict[str, Any] | None = None, timeout: float = 180
) -> dict[str, Any]:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


class LlamaServer:
    def __init__(self, args: argparse.Namespace, executable: Path) -> None:
        self.port = args.port
        self.timeout = args.timeout
        self.max_side = args.max_side
        self.process: subprocess.Popen[bytes] | None = None
        self._log = tempfile.TemporaryFile()
        self._closed = False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", self.port))
            except OSError as exc:
                self._log.close()
                raise RuntimeError(f"llama-server port {self.port} is unavailable") from exc
        command = [
            str(executable.resolve()),
            "-m",
            str(args.gguf_model.resolve()),
            "-mm",
            str(args.mmproj.resolve()),
            "-ngl",
            str(args.ngl),
            "-c",
            str(args.context),
            "-n",
            str(max(args.identity_max_tokens, args.planning_max_tokens)),
            "-np",
            "1",
            "--host",
            "127.0.0.1",
            "--port",
            str(args.port),
            "--no-webui",
            "--cache-prompt",
            "--cache-reuse",
            "64",
        ]
        try:
            self.process = subprocess.Popen(
                command,
                cwd=str(executable.resolve().parent),
                stdout=self._log,
                stderr=subprocess.STDOUT,
            )
        except OSError:
            self._log.close()
            raise
        try:
            self._wait_healthy(args.startup_timeout)
        except BaseException:
            self.close()
            raise

    def _wait_healthy(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self._log.seek(0, os.SEEK_END)
                self._log.seek(max(0, self._log.tell() - 3000))
                tail = self._log.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"llama-server exited with {self.process.returncode}: {tail}")
            try:
                if (
                    _http_json(f"http://127.0.0.1:{self.port}/health", timeout=2).get("status")
                    == "ok"
                ):
                    return
            except (OSError, ValueError, urllib.error.URLError):
                pass
            time.sleep(1)
        raise TimeoutError(f"llama-server did not become healthy within {timeout:g}s")

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process else None

    def identity_call(self, row: dict[str, Any]) -> dict[str, Any]:
        from multidetect.autonomy.onboard_decision import identity_decision_schema
        from scripts.slow_system_runtime import (
            image_jpeg_bytes_list,
            prompt_for_row,
            trained_key_order_schema,
        )

        model_row = {**row, "_crop_policy": "candidate_zoom_v2"}
        images = [
            "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")
            for image in image_jpeg_bytes_list(model_row, self.max_side, "candidate_zoom_v2")
        ]
        content = [{"type": "text", "text": prompt_for_row(model_row)}]
        content.extend({"type": "image_url", "image_url": {"url": image}} for image in images)
        schema = trained_key_order_schema(identity_decision_schema(row["focus_track_id"]))
        body = {
            "model": "local",
            "temperature": 0,
            "max_tokens": row.get("_max_tokens", 128),
            "stream": False,
            "logprobs": True,
            "top_logprobs": 5,
            "messages": [{"role": "user", "content": content}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "onboard_decision", "strict": True, "schema": schema},
            },
        }
        started = time.perf_counter()
        response = _http_json(
            f"http://127.0.0.1:{self.port}/v1/chat/completions", body, self.timeout
        )
        return {
            "latency_s": time.perf_counter() - started,
            "decision": response.get("choices", [{}])[0].get("message", {}).get("content"),
        }

    def planning_call(self, prompt: str, schema: dict[str, Any], max_tokens: int) -> dict[str, Any]:
        body = {
            "model": "local",
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "mission_plan", "strict": True, "schema": schema},
            },
        }
        started = time.perf_counter()
        response = _http_json(
            f"http://127.0.0.1:{self.port}/v1/chat/completions", body, self.timeout
        )
        return {
            "latency_s": time.perf_counter() - started,
            "plan": response.get("choices", [{}])[0].get("message", {}).get("content"),
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self._log.close()


def _repeat_identity(
    server: LlamaServer,
    rows: list[dict[str, Any]],
    repetitions: int,
    warmup: int,
    max_tokens: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    samples: list[dict[str, Any]] = []
    errors: list[str] = []
    for index in range(warmup):
        try:
            warm_row = {**rows[index % len(rows)], "_max_tokens": max_tokens}
            server.identity_call(warm_row)
        except Exception as exc:
            errors.append(f"warmup row {index + 1}: {type(exc).__name__}: {exc}")
    for repetition in range(repetitions):
        for index, row in enumerate(rows):
            try:
                result = server.identity_call({**row, "_max_tokens": max_tokens})
                samples.append(
                    {
                        "repetition": repetition + 1,
                        "row": index + 1,
                        "latency_s": result["latency_s"],
                    }
                )
            except Exception as exc:
                errors.append(f"row {index + 1}: {type(exc).__name__}: {exc}")
    return samples, errors


def _load_planning_prompts(
    jsonl_path: Path | None, texts: list[str]
) -> tuple[list[tuple[str, str]], str | None]:
    prompts: list[tuple[str, str]] = []
    try:
        from multidetect.autonomy.mission_planner import plan_prompt

        for index, task_text in enumerate(texts, start=1):
            prompts.append((f"task-{index}", plan_prompt(task_text)))
    except ImportError as exc:
        return [], f"planning prompt dependencies unavailable: {exc}"
    if jsonl_path is not None:
        if not jsonl_path.is_file():
            return [], f"planning JSONL not found: {jsonl_path}"
        try:
            for line_number, line in enumerate(
                jsonl_path.read_text(encoding="utf-8-sig").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                row = json.loads(line)
                if (
                    isinstance(row, dict)
                    and row.get("kind") == "mission_plan"
                    and isinstance(row.get("prompt"), str)
                ):
                    prompts.append((f"recorded-{line_number}", row["prompt"]))
        except (OSError, json.JSONDecodeError) as exc:
            return [], f"could not read planning JSONL: {type(exc).__name__}: {exc}"
    return prompts, None


def _repeat_planning(
    server: LlamaServer,
    prompts: list[tuple[str, str]],
    repetitions: int,
    warmup: int,
    max_tokens: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    from multidetect.autonomy.mission_planner import plan_json_schema
    from scripts.slow_system_runtime import trained_key_order_schema

    schema = trained_key_order_schema(plan_json_schema())
    samples: list[dict[str, Any]] = []
    errors: list[str] = []
    for index in range(warmup):
        label, prompt = prompts[index % len(prompts)]
        try:
            server.planning_call(prompt, schema, max_tokens)
        except Exception as exc:
            errors.append(f"warmup {label}: {type(exc).__name__}: {exc}")
    for repetition in range(repetitions):
        for label, prompt in prompts:
            try:
                result = server.planning_call(prompt, schema, max_tokens)
                samples.append(
                    {"repetition": repetition + 1, "task": label, "latency_s": result["latency_s"]}
                )
            except Exception as exc:
                errors.append(f"{label}: {type(exc).__name__}: {exc}")
    return samples, errors


def _run_concurrent(
    model: Any,
    frames: list[tuple[Path, Any]],
    server: LlamaServer,
    rows: list[dict[str, Any]],
    *,
    repetitions: int,
    warmup: int,
    max_tokens: int,
    rate_hz: float,
    device: str,
    image_size: int,
    process_ids: dict[str, int],
    interval_ms: int,
    gpu_index: int,
) -> dict[str, Any]:
    _, server_errors = _repeat_identity(server, rows, 0, warmup, max_tokens)
    _, warmup_errors = _run_yolo_samples(
        model,
        frames,
        image_size=image_size,
        repetitions=0,
        warmup=warmup,
        device=device,
    )
    yolo_samples: list[dict[str, Any]] = []
    yolo_errors = warmup_errors
    missed_slots = 0
    stop = threading.Event()
    interval = 1.0 / rate_hz

    def yolo_worker() -> None:
        nonlocal missed_slots
        index = 0
        next_call = time.perf_counter()
        while not stop.is_set():
            wait = next_call - time.perf_counter()
            if wait > 0 and stop.wait(wait):
                break
            path, image = frames[index % len(frames)]
            index += 1
            started = time.perf_counter()
            try:
                result = model.predict(
                    image,
                    imgsz=image_size,
                    conf=0.05,
                    device=device,
                    half=True,
                    verbose=False,
                )
                _consume_yolo_result(result)
                ended = time.perf_counter()
                yolo_samples.append(
                    {
                        "frame": path.name,
                        "latency_s": ended - started,
                        "started_s": started,
                        "ended_s": ended,
                    }
                )
            except Exception as exc:
                yolo_errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
            next_call += interval
            now = time.perf_counter()
            if now > next_call:
                skipped = math.floor((now - next_call) / interval) + 1
                missed_slots += skipped
                next_call += skipped * interval

    sampler = ResourceSampler(process_ids, interval_ms, gpu_index)
    worker = threading.Thread(target=yolo_worker)
    sampler.start()
    worker.start()
    identity_samples: list[dict[str, Any]] = []
    identity_errors: list[str] = []
    try:
        for repetition in range(repetitions):
            for index, row in enumerate(rows):
                try:
                    result = server.identity_call({**row, "_max_tokens": max_tokens})
                    ended = time.perf_counter()
                    identity_samples.append(
                        {
                            "repetition": repetition + 1,
                            "row": index + 1,
                            "latency_s": result["latency_s"],
                            "started_s": ended - result["latency_s"],
                            "ended_s": ended,
                        }
                    )
                except Exception as exc:
                    identity_errors.append(f"row {index + 1}: {type(exc).__name__}: {exc}")
    finally:
        stop.set()
        # Let any in-flight CUDA call finish before returning; Python cannot
        # safely cancel inference running in another thread.
        worker.join()
        sampler.close()
    overlaps = sum(
        any(
            yolo["started_s"] < identity["ended_s"] and yolo["ended_s"] > identity["started_s"]
            for yolo in yolo_samples
        )
        for identity in identity_samples
    )
    return {
        "status": "measured" if overlaps else "not measured",
        "overlapping_identity_calls": overlaps,
        "identity": {
            "latency": aggregate_latencies([item["latency_s"] for item in identity_samples]),
            "samples": identity_samples,
            "errors": server_errors + identity_errors,
        },
        "yoloe": {
            "target_rate_hz": rate_hz,
            "imgsz": image_size,
            "latency": aggregate_latencies([item["latency_s"] for item in yolo_samples]),
            "samples": yolo_samples,
            "errors": yolo_errors,
            "missed_schedule_slots": missed_slots,
        },
        "resources": sampler.report(),
    }


def _write_markdown(report: dict[str, Any], path: Path) -> None:
    phases = report["phases"]
    lines = [
        "# Orin onboard benchmark",
        "",
        f"- Generated: {report['generated_at_utc']}",
        f"- Device: {report['system']['platform']} ({report['system']['machine']})",
        f"- Report status: {report['status']}",
        "",
        "| Phase | Status | Samples | P50 (s) | P95 (s) | Peak RAM (MB) | "
        "GPU memory (MB) | RSS peak (MB) |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]

    def row(label: str, phase: dict[str, Any]) -> None:
        latency = phase.get("latency", {})
        resources = phase.get("resources", {})
        rss_values = resources.get("peak_rss_bytes_by_process", {}).values()
        peak_rss = max(rss_values, default=0) / (1024 * 1024)
        ram = resources.get("peak_ram_used_mb")
        gpu_memory = resources.get("peak_gpu_memory_used_mb")
        lines.append(
            f"| {label} | {phase.get('status', 'not measured')} | {latency.get('count', 0)} "
            f"| {latency.get('p50_s') if latency.get('p50_s') is not None else '—'} "
            f"| {latency.get('p95_s') if latency.get('p95_s') is not None else '—'} "
            f"| {ram if ram is not None else '—'} "
            f"| {gpu_memory if gpu_memory is not None else '—'} "
            f"| {round(peak_rss, 1) if peak_rss else '—'} |"
        )

    for size in DEFAULT_IMG_SIZES:
        key = f"yoloe_pytorch_imgsz_{size}"
        if key in phases:
            row(f"YOLOE PyTorch fp16 {size}", phases[key])
        engine_key = f"yoloe_tensorrt_imgsz_{size}"
        if engine_key in phases:
            row(f"YOLOE TensorRT {size}", phases[engine_key])
    for key, label in (
        ("identity", "llama-server identity"),
        ("planning", "llama-server planning"),
        ("concurrent", "concurrent load"),
    ):
        phase = phases.get(key)
        if not phase:
            continue
        if key == "concurrent":
            identity = phase.get("identity", {}).get("latency", {})
            yolo = phase.get("yoloe", {}).get("latency", {})
            resources = phase.get("resources", {})
            peak_rss = max(resources.get("peak_rss_bytes_by_process", {}).values(), default=0)
            lines.append(
                f"| {label} / identity | {phase.get('status')} | {identity.get('count', 0)} "
                f"| {identity.get('p50_s') if identity.get('p50_s') is not None else '—'} "
                f"| {identity.get('p95_s') if identity.get('p95_s') is not None else '—'} "
                f"| {resources.get('peak_ram_used_mb') or '—'} "
                f"| {resources.get('peak_gpu_memory_used_mb') or '—'} "
                f"| {round(peak_rss / (1024 * 1024), 1) if peak_rss else '—'} |"
            )
            lines.append(
                f"| {label} / YOLOE | {phase.get('status')} | {yolo.get('count', 0)} "
                f"| {yolo.get('p50_s') if yolo.get('p50_s') is not None else '—'} "
                f"| {yolo.get('p95_s') if yolo.get('p95_s') is not None else '—'} "
                f"| — | — | — |"
            )
        else:
            row(label, phase)
    lines.extend(
        ["", "Check the JSON report for errors, telemetry source and per-process RSS.", ""]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames-dir", type=Path, help="Folder containing recorded PNG frames")
    parser.add_argument("--yoloe-weights", type=Path, default=DEFAULT_WEIGHTS)
    parser.add_argument(
        "--tensorrt-engine", type=Path, help="YOLOE TensorRT engine with prompts baked in"
    )
    parser.add_argument("--identity-jsonl", type=Path, default=DEFAULT_IDENTITY_JSONL)
    parser.add_argument(
        "--planning-jsonl", type=Path, help="Recorded mission_plan rows with prompt fields"
    )
    parser.add_argument(
        "--planning-text", action="append", help="Task text to compile; may be repeated"
    )
    parser.add_argument("--gguf-model", type=Path, default=DEFAULT_GGUF)
    parser.add_argument("--mmproj", type=Path, default=DEFAULT_MMPROJ)
    parser.add_argument(
        "--llama-server", type=Path, help="Path to llama-server; otherwise search PATH"
    )
    parser.add_argument("--ngl", default="99", help="llama.cpp GPU layers (-ngl), e.g. 99 or all")
    parser.add_argument("--context", type=int, default=4096, help="llama.cpp context size")
    parser.add_argument("--port", type=int, default=9941)
    parser.add_argument("--identity-max-tokens", type=int, default=128)
    parser.add_argument("--planning-max-tokens", type=int, default=768)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--frame-limit", type=int, default=8, help="0 uses all PNG frames")
    parser.add_argument(
        "--identity-limit", type=int, default=8, help="0 uses all eligible JSONL rows"
    )
    parser.add_argument(
        "--max-side", type=int, default=448, help="Long side for VLM identity images"
    )
    parser.add_argument("--device", default="0", help="Ultralytics CUDA device index")
    parser.add_argument("--concurrent-yolo-hz", type=float, default=1.0)
    parser.add_argument("--concurrent-imgsz", type=int, choices=DEFAULT_IMG_SIZES, default=1280)
    parser.add_argument(
        "--monitor-pid",
        action="append",
        default=[],
        metavar="LABEL=PID",
        help="Also sample an external process RSS; may be repeated, e.g. camera=1234",
    )
    parser.add_argument("--tegrastats-interval-ms", type=int, default=500)
    parser.add_argument("--startup-timeout", type=float, default=180)
    parser.add_argument("--timeout", type=float, default=180, help="VLM request timeout in seconds")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "artifacts/benchmarks/orin-onboard-benchmark.json",
    )
    parser.add_argument("--markdown", type=Path, help="Markdown output path; defaults beside JSON")
    return parser


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    if args.repetitions < 1 or args.warmup < 0:
        raise ValueError("--repetitions must be positive and --warmup cannot be negative")
    if args.tegrastats_interval_ms <= 0:
        raise ValueError("--tegrastats-interval-ms must be positive")
    gpu_index = parse_cuda_device_index(args.device)
    monitored_processes: dict[str, int] = {}
    for value in args.monitor_pid:
        label, pid = parse_monitor_pid(value)
        if label in {"python", "llama_server", *monitored_processes}:
            raise ValueError(f"duplicate or reserved monitored process label: {label}")
        monitored_processes[label] = pid
    base_process_ids = {"python": os.getpid(), **monitored_processes}
    frames = _load_frames(args.frames_dir, args.frame_limit)
    identity_rows, identity_issue = _load_identity_rows(args.identity_jsonl, args.identity_limit)
    planning_texts = args.planning_text or list(DEFAULT_PLANNING_TEXTS)
    planning_prompts, planning_issue = _load_planning_prompts(args.planning_jsonl, planning_texts)
    report: dict[str, Any] = {
        "contract": "multidetect.orin-onboard-benchmark-v1",
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "measured",
        "system": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": sys.version.split()[0],
        },
        "configuration": {
            "repetitions": args.repetitions,
            "warmup": args.warmup,
            "frames_dir": str(args.frames_dir) if args.frames_dir else None,
            "frame_count": len(frames),
            "identity_jsonl": str(args.identity_jsonl) if args.identity_jsonl else None,
            "identity_rows": len(identity_rows),
            "monitored_processes": monitored_processes,
            "ngl": args.ngl,
            "context": args.context,
            "gguf_model": str(args.gguf_model),
            "mmproj": str(args.mmproj),
            "device": args.device,
            "cuda_device_index": gpu_index,
            "max_side": args.max_side,
            "identity_max_tokens": args.identity_max_tokens,
            "planning_max_tokens": args.planning_max_tokens,
            "concurrent_backend": "pytorch_fp16",
            "concurrent_imgsz": args.concurrent_imgsz,
            "concurrent_yolo_hz": args.concurrent_yolo_hz,
            "telemetry_interval_ms": args.tegrastats_interval_ms,
            "tensorrt_engine": str(args.tensorrt_engine) if args.tensorrt_engine else None,
            "latency_note": (
                "VLM timing covers the HTTP call after image/crop encoding; "
                "YOLOE covers preprocess, inference and materialized detections."
            ),
        },
        "phases": {},
    }
    pytorch_model = None
    yolo_error: str | None = None
    try:
        import torch

        if frames:
            pytorch_model = _make_pytorch_yoloe(args.yoloe_weights)
        report["configuration"]["pytorch_cuda_version"] = torch.version.cuda
        report["configuration"]["yoloe_weights"] = str(args.yoloe_weights)
    except Exception as exc:
        yolo_error = f"{type(exc).__name__}: {exc}"

    if not frames:
        reason = (
            "no readable PNG frames were found"
            if args.frames_dir
            else "--frames-dir was not provided"
        )
        if yolo_error:
            reason += f"; YOLOE setup: {yolo_error}"
        for size in DEFAULT_IMG_SIZES:
            report["phases"][f"yoloe_pytorch_imgsz_{size}"] = {
                "status": "not measured",
                "reason": reason,
                "latency": aggregate_latencies([]),
            }
    else:
        for size in DEFAULT_IMG_SIZES:
            key = f"yoloe_pytorch_imgsz_{size}"
            if pytorch_model is None:
                report["phases"][key] = {
                    "status": "not measured",
                    "reason": yolo_error or "YOLOE setup failed",
                    "latency": aggregate_latencies([]),
                }
                continue
            report["phases"][key] = _measure_phase(
                key,
                lambda size=size: _run_yolo_samples(
                    pytorch_model,
                    frames,
                    image_size=size,
                    repetitions=args.repetitions,
                    warmup=args.warmup,
                    device=args.device,
                ),
                base_process_ids,
                args.tegrastats_interval_ms,
                gpu_index,
            )

    if args.tensorrt_engine:
        if pytorch_model is not None:
            pytorch_model = None
            _release_cuda_cache()
        engine_model = None
        engine_error = None
        try:
            import torch
            from ultralytics import YOLO

            if not torch.cuda.is_available():
                raise RuntimeError("PyTorch CUDA is unavailable")
            if not args.tensorrt_engine.is_file():
                raise FileNotFoundError(f"TensorRT engine not found: {args.tensorrt_engine}")
            engine_model = YOLO(str(args.tensorrt_engine))
        except Exception as exc:
            engine_error = f"{type(exc).__name__}: {exc}"
        for size in DEFAULT_IMG_SIZES:
            key = f"yoloe_tensorrt_imgsz_{size}"
            if not frames or engine_model is None:
                report["phases"][key] = {
                    "status": "not measured",
                    "reason": "no readable PNG frames" if not frames else engine_error,
                    "latency": aggregate_latencies([]),
                }
            else:
                report["phases"][key] = _measure_phase(
                    key,
                    lambda size=size: _run_yolo_samples(
                        engine_model,
                        frames,
                        image_size=size,
                        repetitions=args.repetitions,
                        warmup=args.warmup,
                        device=args.device,
                    ),
                    base_process_ids,
                    args.tegrastats_interval_ms,
                    gpu_index,
                )
                engine_dynamic, engine_shape = _engine_input_shape(engine_model)
                report["phases"][key]["engine_input_shape"] = (
                    "dynamic" if engine_dynamic else engine_shape
                )
                if not engine_dynamic and engine_shape != (size, size):
                    report["phases"][key].update(
                        {
                            "status": "not measured",
                            "reason": (
                                "could not verify effective TensorRT input size"
                                if engine_shape is None
                                else (
                                    f"effective TensorRT input is {engine_shape}, "
                                    f"not {(size, size)}"
                                )
                            ),
                            "latency": aggregate_latencies([]),
                            "samples": [],
                        }
                    )
        if engine_model is not None:
            engine_model = None
            _release_cuda_cache()

    else:
        for size in DEFAULT_IMG_SIZES:
            report["phases"][f"yoloe_tensorrt_imgsz_{size}"] = {
                "status": "not measured",
                "reason": "--tensorrt-engine was not provided",
                "latency": aggregate_latencies([]),
            }

    rows = identity_rows
    server: LlamaServer | None = None
    server_issue: str | None = identity_issue or planning_issue
    executable = args.llama_server
    if executable is None:
        found = shutil.which("llama-server")
        executable = Path(found) if found else None
    if not rows and not planning_prompts:
        server_issue = server_issue or "no identity or planning requests were configured"
    elif not executable or not executable.is_file():
        server_issue = f"llama-server was not found: {executable or 'not on PATH'}"
    elif not args.gguf_model.is_file() or not args.mmproj.is_file():
        server_issue = "GGUF model or mmproj file is missing"
    else:
        try:
            server = LlamaServer(args, executable)
            atexit.register(server.close)
            report["configuration"]["llama_server"] = str(executable)
            report["configuration"]["llama_server_pid"] = server.pid
        except Exception as exc:
            server_issue = f"{type(exc).__name__}: {exc}"

    if server is None:
        report["phases"]["identity"] = {
            "status": "not measured",
            "reason": server_issue or "identity phase unavailable",
            "latency": aggregate_latencies([]),
        }
        report["phases"]["planning"] = {
            "status": "not measured",
            "reason": server_issue or "planning phase unavailable",
            "latency": aggregate_latencies([]),
        }
        report["phases"]["concurrent"] = {
            "status": "not measured",
            "reason": server_issue or "concurrent phase unavailable",
        }
    else:
        try:
            # Keep the actual proposer resident alongside llama-server for shared
            # memory measurements, without retaining the optional TensorRT engine.
            if frames and pytorch_model is None:
                try:
                    pytorch_model = _make_pytorch_yoloe(args.yoloe_weights)
                except Exception as exc:
                    yolo_error = f"{type(exc).__name__}: {exc}"
            process_ids = dict(base_process_ids)
            if server.pid is not None:
                process_ids["llama_server"] = server.pid
            if rows:
                report["phases"]["identity"] = _measure_phase(
                    "identity",
                    lambda: _repeat_identity(
                        server,
                        rows,
                        args.repetitions,
                        args.warmup,
                        args.identity_max_tokens,
                    ),
                    process_ids,
                    args.tegrastats_interval_ms,
                    gpu_index,
                )
            else:
                report["phases"]["identity"] = {
                    "status": "not measured",
                    "reason": identity_issue or "identity rows were not provided",
                    "latency": aggregate_latencies([]),
                }
            if planning_prompts:
                report["phases"]["planning"] = _measure_phase(
                    "planning",
                    lambda: _repeat_planning(
                        server,
                        planning_prompts,
                        args.repetitions,
                        args.warmup,
                        args.planning_max_tokens,
                    ),
                    process_ids,
                    args.tegrastats_interval_ms,
                    gpu_index,
                )
            else:
                report["phases"]["planning"] = {
                    "status": "not measured",
                    "reason": planning_issue or "no planning prompts available",
                    "latency": aggregate_latencies([]),
                }
            if pytorch_model is None or not frames or not rows:
                report["phases"]["concurrent"] = {
                    "status": "not measured",
                    "reason": "concurrent phase requires YOLOE CUDA, PNG frames and identity rows",
                }
            elif not math.isfinite(args.concurrent_yolo_hz) or args.concurrent_yolo_hz <= 0:
                report["phases"]["concurrent"] = {
                    "status": "not measured",
                    "reason": "--concurrent-yolo-hz must be positive",
                }
            else:
                report["phases"]["concurrent"] = _run_concurrent(
                    pytorch_model,
                    frames,
                    server,
                    rows,
                    repetitions=args.repetitions,
                    warmup=args.warmup,
                    max_tokens=args.identity_max_tokens,
                    rate_hz=args.concurrent_yolo_hz,
                    device=args.device,
                    image_size=args.concurrent_imgsz,
                    process_ids=process_ids,
                    interval_ms=args.tegrastats_interval_ms,
                    gpu_index=gpu_index,
                )
        finally:
            server.close()
            atexit.unregister(server.close)

    measured = [phase.get("status") == "measured" for phase in report["phases"].values()]
    report["status"] = "measured" if any(measured) else "not measured"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    report = run_benchmark(args)
    output = args.output
    markdown = args.markdown or output.with_suffix(".md")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    _write_markdown(report, markdown)
    print(
        json.dumps(
            {"status": report["status"], "json": str(output), "markdown": str(markdown)}, indent=2
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
