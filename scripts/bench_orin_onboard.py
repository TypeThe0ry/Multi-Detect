#!/usr/bin/env python3
"""Measure the onboard YOLOE + llama.cpp path on a Jetson Orin Nano 8GB or CUDA workstation."""

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

DEFAULT_WEIGHTS = ROOT / "artifacts/experiments/yoloe-v3-onboard-1280/train/weights/best.pt"
DEFAULT_GGUF = (
    ROOT
    / "artifacts/models/slow-system-lora-v2-r18-multi-merged-gguf"
    / "slow-system-v2r18multi-merged-Q5_K_M.gguf"
)
DEFAULT_GGUF_SHA256 = "0326614baf72947f76c1c23ad038d9a15221d5d58c6f29cdc38bb257c924ba6e"
DEFAULT_FAST_DETECTOR_SHA256 = "402d91abcaf924ab22971ac9b9130843109fa8821dfbfd88c0c52d574f9e89a4"
POSITION_DEFAULTS = {"step_triangulation": True, "ground_height_sigma_m": 5.0}
DEFAULT_MMPROJ = (
    ROOT / "artifacts/cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/"
    "52d6c8ffea26cc873ac5ad116f8631268d7eb503/"
    "mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf"
)
DEFAULT_IDENTITY_JSONL = ROOT / "artifacts/datasets/slow-system-sft-v2-r4-prop/test.jsonl"
ARTIFACTS_ROOT = Path(os.environ.get("MULTIDETECT_ARTIFACTS", str(ROOT / "artifacts")))
DEFAULT_R14_GGUF = (
    ARTIFACTS_ROOT
    / "models/slow-system-lora-v2-r14-multi-merged-gguf/slow-system-v2r14multi-merged-Q5_K_M.gguf"
)
DEFAULT_DEPTH_ONNX = ARTIFACTS_ROOT / "evaluation/depth-anything-v2-metric-outdoor-small.onnx"
DEFAULT_IMG_SIZES = (640, 960, 1280)
DEFAULT_PLANNING_TEXTS = (
    "Find the red car and return home.",
    "Count traffic cones in the yard, then return home.",
    "Inspect the parked truck and return home.",
)

# These are deliberately expressed in the units emitted by the report.  The
# memory limit follows the feasibility note's conservative 6.5 GB gate:
# 6.5e9 bytes is approximately 6199 MiB, which is what tegrastats reports.
ORIN_BUDGETS = {
    "fast_hz_min": 10.0,
    "identity_p95_s_max": 1.0,
    "peak_ram_used_mb_max": 6199.0,
}

FEASIBILITY_ESTIMATES = {
    "fast_latency_s": (0.05, 0.09),
    "fast_identity_s": (0.6, 1.0),
    "planning_s": (4.0, 6.0),
    "peak_memory_gb": (4.7, 6.7),
}


def evaluate_budgets(report: dict[str, Any]) -> dict[str, Any]:
    """Evaluate the deployment gates from measured phases.

    Missing or failed phases are reported as ``not_measured`` so a partial
    workstation run cannot accidentally be presented as an onboard pass.
    """
    phases = report.get("phases", {})

    def latency(phase_name: str) -> float | None:
        phase = phases.get(phase_name, {})
        value = phase.get("latency", {}).get("p95_s")
        return float(value) if isinstance(value, (int, float)) else None

    fast_latency = latency("yoloe_tensorrt_imgsz_1280")
    if fast_latency is None:
        fast_latency = latency("yoloe_tensorrt_imgsz_960")
    fast_hz = (1.0 / fast_latency) if fast_latency and fast_latency > 0 else None
    identity_latency = latency("fast_identity")
    # Read the old phase key only for older archived reports.
    if identity_latency is None:
        identity_latency = latency("r12_fast_identity")
    if identity_latency is None:
        identity_latency = latency("identity_one_token")
    resources = []
    for phase in phases.values():
        value = phase.get("resources", {}).get("peak_ram_used_mb")
        if isinstance(value, (int, float)):
            resources.append(float(value))
    peak_ram = max(resources) if resources else None

    def check(name: str, value: float | None, *, limit: float, operator: str) -> dict[str, Any]:
        if value is None:
            return {"status": "not_measured", "value": None, "limit": limit, "operator": operator}
        passed = value >= limit if operator == ">=" else value <= limit
        return {
            "status": "pass" if passed else "fail",
            "value": round(value, 4),
            "limit": limit,
            "operator": operator,
            "gap": round(value - limit, 4),
        }

    checks = {
        "fast_system_hz": check(
            "fast_system_hz", fast_hz, limit=ORIN_BUDGETS["fast_hz_min"], operator=">="
        ),
        "identity_p95_s": check(
            "identity_p95_s",
            identity_latency,
            limit=ORIN_BUDGETS["identity_p95_s_max"],
            operator="<=",
        ),
        "peak_ram_used_mb": check(
            "peak_ram_used_mb",
            peak_ram,
            limit=ORIN_BUDGETS["peak_ram_used_mb_max"],
            operator="<=",
        ),
    }

    def phase_p95(*names: str) -> float | None:
        for name in names:
            value = latency(name)
            if value is not None:
                return value
        return None

    measured_estimates = {
        "fast_latency_s": phase_p95("yoloe_tensorrt_imgsz_1280", "yoloe_tensorrt_imgsz_960"),
        "fast_identity_s": phase_p95("fast_identity", "r12_fast_identity", "identity_one_token"),
        "planning_s": phase_p95("r14_planning", "planning"),
        "peak_memory_gb": peak_ram * (1024 * 1024) / 1e9 if peak_ram is not None else None,
    }
    estimate_comparison = {}
    for name, expected in FEASIBILITY_ESTIMATES.items():
        measured = measured_estimates[name]
        estimate_comparison[name] = {
            "measured": round(measured, 4) if measured is not None else None,
            "estimated_range": list(expected),
            "delta_to_estimate_upper": (
                round(measured - expected[1], 4) if measured is not None else None
            ),
            "status": "measured" if measured is not None else "not_measured",
        }
    measured = [item["status"] != "not_measured" for item in checks.values()]
    report_status = "pass" if all(item["status"] == "pass" for item in checks.values()) else "fail"
    return {
        "thresholds": dict(ORIN_BUDGETS),
        "checks": checks,
        "all_required_measured": all(measured),
        "all_required_met": report_status == "pass",
        "status": report_status if all(measured) else "not_measured",
        "gaps": [name for name, item in checks.items() if item["status"] != "pass"],
        "feasibility_estimates": {
            name: list(value) for name, value in FEASIBILITY_ESTIMATES.items()
        },
        "estimate_comparison": estimate_comparison,
        "source": "docs/orin-nano-feasibility.md conservative budget gates",
    }


def _build_tensorrt_engine(
    onnx_path: Path, engine_path: Path, trtexec: str | Path = "trtexec"
) -> dict[str, Any]:
    """Build one engine on the target using the installed TensorRT ``trtexec``."""
    command = [
        str(trtexec),
        f"--onnx={onnx_path}",
        f"--saveEngine={engine_path}",
        "--fp16",
        "--memPoolSize=workspace:512",
        "--skipInference",
    ]
    engine_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    try:
        completed = subprocess.run(command, check=False, text=True, capture_output=True)
    except OSError as exc:
        return {
            "status": "not measured",
            "onnx": str(onnx_path),
            "engine": str(engine_path),
            "command": command,
            "error": f"{type(exc).__name__}: {exc}",
        }
    result = {
        "status": "measured" if completed.returncode == 0 and engine_path.is_file() else "failed",
        "onnx": str(onnx_path),
        "engine": str(engine_path),
        "command": command,
        "returncode": completed.returncode,
        "elapsed_s": round(time.perf_counter() - started, 4),
    }
    if completed.returncode != 0:
        result["stderr_tail"] = completed.stderr[-2000:]
    return result


def _build_requested_engines(args: argparse.Namespace) -> dict[str, Any]:
    """Build 960/1280 engines when explicitly requested, one after another."""
    if not args.build_engines:
        return {"status": "not requested", "engines": {}}
    trtexec = args.trtexec or shutil.which("trtexec") or "/usr/src/tensorrt/bin/trtexec"
    pairs = {
        960: (args.proposer_onnx_960, args.tensorrt_engine_960),
        1280: (args.proposer_onnx_1280, args.tensorrt_engine_1280),
    }
    outputs: dict[str, Any] = {}
    for size, (onnx_path, engine_path) in pairs.items():
        if onnx_path is None or engine_path is None:
            outputs[str(size)] = {"status": "not measured", "reason": "ONNX/engine path missing"}
            continue
        if not onnx_path.is_file():
            outputs[str(size)] = {
                "status": "not measured",
                "reason": f"ONNX not found: {onnx_path}",
            }
            continue
        outputs[str(size)] = _build_tensorrt_engine(onnx_path, engine_path, trtexec)
    if args.depth_onnx is not None and args.depth_engine is not None:
        outputs["depth"] = (
            _build_tensorrt_engine(args.depth_onnx, args.depth_engine, trtexec)
            if args.depth_onnx.is_file()
            else {"status": "not measured", "reason": f"ONNX not found: {args.depth_onnx}"}
        )
    status = (
        "measured"
        if outputs and all(item.get("status") == "measured" for item in outputs.values())
        else "failed"
    )
    return {"status": status, "trtexec": str(trtexec), "engines": outputs}


def _prepare_tensorrt_engine(engine: Path) -> Path:
    """Add Ultralytics metadata to a raw trtexec engine without changing the source."""
    raw = engine.read_bytes()
    if len(raw) >= 4:
        metadata_length = int.from_bytes(raw[:4], "little")
        if 0 < metadata_length <= len(raw) - 4:
            try:
                metadata = json.loads(raw[4 : 4 + metadata_length].decode("utf-8"))
                if isinstance(metadata, dict):
                    return engine
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
    from scripts.onboard_policy import VOCABULARY

    metadata = {
        "names": repr(dict(enumerate(VOCABULARY))),
        "args": repr({"dynamic": False, "nms": False}),
        "stride": 32,
        "task": "detect",
    }
    wrapped = engine.with_name(f"{engine.stem}.ultralytics.engine")
    metadata_bytes = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
    wrapped.write_bytes(len(metadata_bytes).to_bytes(4, "little") + metadata_bytes + raw)
    return wrapped


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
            agnostic_nms=True,
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


def _run_step_triangulation_samples(
    repetitions: int, warmup: int
) -> tuple[list[dict[str, Any]], list[str]]:
    """Time the production step triangulation primitive on representative views."""
    from multidetect.autonomy.step_triangulation import ViewObservation, triangulate_views

    target = (12.0, 0.5, -1.0)
    views = []
    for step in range(8):
        camera = (step * 0.7, 0.0, 0.0)
        delta = tuple(target[i] - camera[i] for i in range(3))
        norm = math.sqrt(sum(value * value for value in delta))
        views.append(
            ViewObservation(camera, tuple(value / norm for value in delta), step, step * 0.1)
        )
    errors: list[str] = []
    try:
        for _ in range(warmup):
            triangulate_views(views)
    except Exception as exc:
        return [], [f"warmup failed: {type(exc).__name__}: {exc}"]
    samples: list[dict[str, Any]] = []
    for repetition in range(repetitions):
        started = time.perf_counter()
        try:
            result = triangulate_views(views)
            elapsed = time.perf_counter() - started
            if result is None:
                errors.append("triangulation rejected representative views")
            samples.append({"repetition": repetition + 1, "latency_s": elapsed})
        except Exception as exc:
            errors.append(f"repetition {repetition + 1}: {type(exc).__name__}: {exc}")
    return samples, errors


def _run_depth_samples(
    model_path: Path | None,
    frames: list[tuple[Path, Any]],
    repetitions: int,
    warmup: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Measure Depth Anything V2 Small via ONNX Runtime when available.

    TensorRT engine timing is collected by the target-side ``trtexec`` build;
    this local path intentionally stays optional and reports a clean reason
    when the development machine lacks onnxruntime or the model.
    """
    if model_path is None:
        return [], ["depth model path was not provided"]
    if not model_path.is_file():
        return [], [f"depth model not found: {model_path}"]
    if not frames:
        return [], ["no readable PNG frames"]
    try:
        import cv2
        import numpy as np
        import onnxruntime as ort
    except ImportError as exc:
        return [], [f"ONNX Runtime unavailable: {exc}"]
    try:
        session = ort.InferenceSession(
            str(model_path), providers=["CUDAExecutionProvider", "CPUExecutionProvider"]
        )
        input_meta = session.get_inputs()[0]
        shape = input_meta.shape
        height = int(shape[-2]) if isinstance(shape[-2], int) else 518
        width = int(shape[-1]) if isinstance(shape[-1], int) else 518
        input_name = input_meta.name
    except Exception as exc:
        return [], [f"could not load depth ONNX: {type(exc).__name__}: {exc}"]

    def infer(image: Any) -> None:
        resized = cv2.resize(image, (width, height), interpolation=cv2.INTER_LINEAR)
        tensor = resized[:, :, ::-1].astype(np.float32) / 255.0
        tensor = np.transpose(tensor, (2, 0, 1))[None, ...]
        session.run(None, {input_name: tensor})

    try:
        for _ in range(warmup):
            infer(frames[0][1])
    except Exception as exc:
        return [], [f"depth warmup failed: {type(exc).__name__}: {exc}"]
    samples: list[dict[str, Any]] = []
    errors: list[str] = []
    for repetition in range(repetitions):
        for path, image in frames:
            started = time.perf_counter()
            try:
                infer(image)
                samples.append(
                    {
                        "repetition": repetition + 1,
                        "frame": path.name,
                        "latency_s": time.perf_counter() - started,
                    }
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
        torch = sys.modules.get("torch")
        if torch is not None:
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
        self._verdict_tokens: dict[str, dict[str, int]] = {}
        self._verdict_pieces: dict[str, dict[str, str]] = {}
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

    def _verdict_first_tokens(self, prefix: str) -> dict[str, int]:
        if prefix not in self._verdict_tokens:

            def tokens(value: str) -> list[int]:
                response = _http_json(
                    f"http://127.0.0.1:{self.port}/tokenize", {"content": value}, self.timeout
                )
                return [
                    item["id"] if isinstance(item, dict) else item for item in response["tokens"]
                ]

            base = tokens(prefix)
            firsts = {}
            for verdict in ("approach_track", "confirm_target", "reject_track"):
                full = tokens(prefix + verdict + '"')
                if full[: len(base)] != base or len(full) <= len(base):
                    raise RuntimeError(f"verdict {verdict} does not extend prefix tokenization")
                firsts[verdict] = full[len(base)]
            if len(set(firsts.values())) != len(firsts):
                raise RuntimeError("identity verdicts share a first token")
            pieces = {
                verdict: _http_json(
                    f"http://127.0.0.1:{self.port}/detokenize", {"tokens": [token]}, self.timeout
                )["content"]
                for verdict, token in firsts.items()
            }
            self._verdict_tokens[prefix] = firsts
            self._verdict_pieces[prefix] = pieces
        return self._verdict_tokens[prefix]

    def identity_call(self, row: dict[str, Any], *, fast_identity: bool = False) -> dict[str, Any]:
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
        if fast_identity:
            args = {"duration_s": None, "region": None, "track_id": row["focus_track_id"]}
            prefix = json.dumps({"args": args}, separators=(",", ":"))[:-1] + ',"decision":"'
            firsts = self._verdict_first_tokens(prefix)
            body = {
                "model": "local",
                "temperature": 0,
                "max_tokens": 1,
                "stream": False,
                "logprobs": True,
                "top_logprobs": 20,
                "messages": [
                    {"role": "user", "content": content},
                    {"role": "assistant", "content": prefix},
                ],
            }
        else:
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
        result = {
            "latency_s": time.perf_counter() - started,
            "decision": response.get("choices", [{}])[0].get("message", {}).get("content"),
        }
        if fast_identity:
            try:
                entries = response["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
            except (KeyError, IndexError, TypeError) as exc:
                raise RuntimeError(
                    "one-token identity needs top_logprobs; no full-decode fallback"
                ) from exc
            pieces = self._verdict_pieces[prefix]
            scores = {}
            for verdict, token in firsts.items():
                for entry in entries:
                    if entry.get("id") == token or (
                        entry.get("id") is None and entry.get("token") == pieces[verdict]
                    ):
                        scores[verdict] = float(entry["logprob"])
                        break
            if not scores:
                raise RuntimeError("no identity verdict in top_logprobs; no full-decode fallback")
            result["decision"] = max(scores, key=scores.get)
            result["verdict_logprobs"] = scores
        timings = response.get("timings") or {}
        for key in ("prompt_n", "prompt_ms", "predicted_n", "predicted_ms"):
            result[key] = timings.get(key)
        result["mode"] = "fast_identity" if fast_identity else "full_constrained"
        return result

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
    fast_identity: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
    samples: list[dict[str, Any]] = []
    errors: list[str] = []
    for index in range(warmup):
        try:
            warm_row = {**rows[index % len(rows)], "_max_tokens": max_tokens}
            server.identity_call(warm_row, fast_identity=fast_identity)
        except Exception as exc:
            errors.append(f"warmup row {index + 1}: {type(exc).__name__}: {exc}")
    for repetition in range(repetitions):
        for index, row in enumerate(rows):
            try:
                result = server.identity_call(
                    {**row, "_max_tokens": max_tokens}, fast_identity=fast_identity
                )
                samples.append(
                    {
                        "repetition": repetition + 1,
                        "row": index + 1,
                        "latency_s": result["latency_s"],
                        **{
                            key: result[key]
                            for key in ("prompt_n", "prompt_ms", "predicted_n", "predicted_ms")
                            if key in result
                        },
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
                    agnostic_nms=True,
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
        "# Jetson Orin Nano 8GB onboard benchmark",
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
        ("identity", "llama-server identity / full constrained"),
        ("identity_one_token", "llama-server identity / one token"),
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


def _write_markdown_chinese(report: dict[str, Any], path: Path) -> None:
    """Write the operator-facing Chinese report, including budget decisions."""
    phases = report.get("phases", {})
    budgets = report.get("budgets", {})
    lines = [
        "# Jetson Orin Nano 8GB 机载实测报告",
        "",
        f"- 生成时间（UTC）：{report.get('generated_at_utc')}",
        f"- 设备：{report.get('system', {}).get('platform')}（"
        f"{report.get('system', {}).get('machine')}）",
        f"- 运行状态：{report.get('status')}",
        "",
        "## 预算判定",
        "",
        "| 门槛 | 实测值 | 要求 | 结果 |",
        "|---|---:|---:|---|",
    ]
    labels = {
        "fast_system_hz": "快系统频率（Hz）",
        "identity_p95_s": "fast_identity P95（秒）",
        "peak_ram_used_mb": "峰值统一内存（MiB）",
    }
    result_labels = {"pass": "通过", "fail": "不通过", "not_measured": "未测量"}
    for name, check in budgets.get("checks", {}).items():
        value = check.get("value")
        lines.append(
            f"| {labels.get(name, name)} | {value if value is not None else '未测量'} | "
            f"{check.get('operator')} {check.get('limit')} | "
            f"{result_labels.get(check.get('status'), check.get('status'))} |"
        )
    lines.extend(
        [
            "",
            "- 总预算结论："
            f"{result_labels.get(budgets.get('status'), budgets.get('status', '未测量'))}",
            f"- 与 feasibility 估算的差距项：{', '.join(budgets.get('gaps', [])) or '无'}",
            "",
            "## 与可行性估算对照",
            "",
            "| 指标 | 实测 | 估算范围 | 相对估算上限差值 |",
            "|---|---:|---:|---:|",
        ]
    )
    estimate_labels = {
        "fast_latency_s": "快系统 P95（秒）",
        "fast_identity_s": "fast_identity P95（秒）",
        "planning_s": "规划 P95（秒）",
        "peak_memory_gb": "峰值内存（十进制 GB）",
    }
    for name, comparison in budgets.get("estimate_comparison", {}).items():
        expected = comparison.get("estimated_range", [])
        lines.append(
            f"| {estimate_labels.get(name, name)} | {comparison.get('measured', '未测量')} "
            f"| {expected[0]}–{expected[1]} | "
            f"{comparison.get('delta_to_estimate_upper', '未测量')} |"
        )
    lines.extend(
        [
            "",
            "差值为实测值减去可行性估算上限；正数表示超过上限，负数表示低于上限。",
            "",
            "## 分阶段延迟",
            "",
            "| 阶段 | 状态 | 样本数 | P50（秒） | P95（秒） | 峰值 RAM（MiB） | GPU 显存（MiB） | "
            "RSS 峰值（MiB） |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )

    def row(label: str, phase: dict[str, Any]) -> None:
        latency = phase.get("latency", {})
        resources = phase.get("resources", {})
        rss = max(resources.get("peak_rss_bytes_by_process", {}).values(), default=0) / (
            1024 * 1024
        )

        def value(key):
            return latency.get(key) if latency.get(key) is not None else "未测量"

        lines.append(
            f"| {label} | {phase.get('status', 'not measured')} | {latency.get('count', 0)} | "
            f"{value('p50_s')} | {value('p95_s')} | {resources.get('peak_ram_used_mb', '未测量')} "
            f"| {resources.get('peak_gpu_memory_used_mb', '未测量')} | "
            f"{round(rss, 1) if rss else '未测量'} |"
        )

    labels_by_phase = {
        "yoloe_tensorrt_imgsz_960": "YOLOE TensorRT 960",
        "yoloe_tensorrt_imgsz_1280": "YOLOE TensorRT 1280",
        "full_identity": "当前身份模型完整判断（默认 r18）",
        "fast_identity": "当前身份模型 fast_identity（默认 r18）",
        "planning": "身份模型规划（兼容阶段）",
        "r14_planning": "r14 规划",
        "r14_model_switch": "r14 模型切换",
        "step_triangulation_cpu": "步间三角化 CPU",
        "depth_anything_v2_small": "近距深度 Depth Anything V2 Small",
    }
    for key, label in labels_by_phase.items():
        if key in phases:
            row(label, phases[key])
    concurrent = phases.get("concurrent")
    if concurrent:
        identity = concurrent.get("identity", {})
        yolo = concurrent.get("yoloe", {})
        row(
            "并行身份判断",
            {
                "status": concurrent.get("status"),
                "latency": identity.get("latency", {}),
                "resources": concurrent.get("resources", {}),
            },
        )
        row(
            "并行 YOLOE",
            {
                "status": concurrent.get("status"),
                "latency": yolo.get("latency", {}),
                "resources": concurrent.get("resources", {}),
            },
        )
    lines.extend(
        [
            "",
            "JSON 文件保留逐条样本、错误、tegrastats 来源和各进程 "
            "RSS；缺少目标工具的阶段会标记为未测量。",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames-dir", type=Path, help="Folder containing recorded PNG frames")
    parser.add_argument(
        "--pytorch-comparison",
        action="store_true",
        help="Optional PyTorch comparison; never needed on the flight image",
    )
    parser.add_argument("--yoloe-weights", type=Path, default=DEFAULT_WEIGHTS)
    parser.add_argument(
        "--tensorrt-engine", type=Path, help="YOLOE TensorRT engine with prompts baked in"
    )
    parser.add_argument("--tensorrt-engine-960", type=Path)
    parser.add_argument("--tensorrt-engine-1280", type=Path)
    parser.add_argument("--proposer-onnx-960", type=Path)
    parser.add_argument("--proposer-onnx-1280", type=Path)
    parser.add_argument(
        "--build-engines",
        action="store_true",
        help="Build both proposer TensorRT engines with trtexec",
    )
    parser.add_argument("--trtexec", type=Path, help="trtexec executable on the Jetson")
    parser.add_argument("--identity-jsonl", type=Path, default=DEFAULT_IDENTITY_JSONL)
    parser.add_argument(
        "--planning-jsonl", type=Path, help="Recorded mission_plan rows with prompt fields"
    )
    parser.add_argument(
        "--planning-text", action="append", help="Task text to compile; may be repeated"
    )
    parser.add_argument("--gguf-model", type=Path, default=DEFAULT_GGUF)
    parser.add_argument("--r14-gguf-model", type=Path, default=DEFAULT_R14_GGUF)
    parser.add_argument("--mmproj", type=Path, default=DEFAULT_MMPROJ)
    parser.add_argument(
        "--llama-server", type=Path, help="Path to llama-server; otherwise search PATH"
    )
    parser.add_argument("--ngl", default="99", help="llama.cpp GPU layers (-ngl), e.g. 99 or all")
    parser.add_argument(
        "--context", type=int, default=2048, help="llama.cpp context size (flight default: 2048)"
    )
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
    parser.add_argument("--device", default="0", help="CUDA device index")
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
    parser.add_argument("--depth-onnx", type=Path, default=DEFAULT_DEPTH_ONNX)
    parser.add_argument("--depth-engine", type=Path, help="Depth Anything TensorRT engine on Orin")
    parser.add_argument("--skip-depth", action="store_true")
    parser.add_argument("--skip-triangulation", action="store_true")
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
    # Keep the legacy single-engine option while allowing one command to carry
    # both fixed-size engines through the target build and benchmark.
    if args.tensorrt_engine_1280 is None and args.tensorrt_engine is not None:
        args.tensorrt_engine_1280 = args.tensorrt_engine
    engine_build = _build_requested_engines(args)
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
            "target": "Jetson Orin Nano 8GB",
            "default_gguf_sha256": DEFAULT_GGUF_SHA256,
            "fast_detector_sha256": DEFAULT_FAST_DETECTOR_SHA256,
            "position_defaults": dict(POSITION_DEFAULTS),
            "pytorch_comparison": args.pytorch_comparison,
            "concurrent_backend": None,
            "concurrent_imgsz": args.concurrent_imgsz,
            "concurrent_yolo_hz": args.concurrent_yolo_hz,
            "telemetry_interval_ms": args.tegrastats_interval_ms,
            "tensorrt_engine": str(args.tensorrt_engine) if args.tensorrt_engine else None,
            "tensorrt_engines": {
                "960": str(args.tensorrt_engine_960) if args.tensorrt_engine_960 else None,
                "1280": str(args.tensorrt_engine_1280) if args.tensorrt_engine_1280 else None,
            },
            "engine_build": engine_build,
            "identity_gguf_model": str(args.gguf_model),
            "r14_gguf_model": str(args.r14_gguf_model),
            "depth_onnx": str(args.depth_onnx) if args.depth_onnx else None,
            "depth_engine": str(args.depth_engine) if args.depth_engine else None,
            "latency_note": (
                "VLM timing covers the HTTP call after image/crop encoding; "
                "YOLOE covers preprocess, inference and materialized detections."
            ),
        },
        "phases": {},
    }
    pytorch_model = None
    engine_model = None
    engine_models: dict[int, Any] = {}
    yolo_error = None
    if args.pytorch_comparison and frames:
        try:
            pytorch_model = _make_pytorch_yoloe(args.yoloe_weights)
        except Exception as exc:
            yolo_error = f"{type(exc).__name__}: {exc}"
    for size in DEFAULT_IMG_SIZES:
        key = f"yoloe_pytorch_imgsz_{size}"
        if pytorch_model is None:
            report["phases"][key] = {
                "status": "not measured",
                "reason": (
                    "optional comparison disabled; enable --pytorch-comparison"
                    if not args.pytorch_comparison
                    else yolo_error or "no readable PNG frames"
                ),
                "latency": aggregate_latencies([]),
            }
        else:
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
    # Free the optional comparison model before measuring the flight stack.
    pytorch_model = None
    _release_cuda_cache()
    engine_error = "--tensorrt-engine was not provided"
    if frames:
        try:
            from scripts.orin_tensorrt_proposer import TensorRTProposer

            for size, engine_path in (
                (960, args.tensorrt_engine_960),
                (1280, args.tensorrt_engine_1280),
            ):
                if engine_path is None:
                    continue
                candidate = TensorRTProposer(engine_path, device=gpu_index)
                engine_models[size] = candidate
            engine_model = (
                engine_models.get(args.concurrent_imgsz)
                or engine_models.get(1280)
                or next(iter(engine_models.values()), None)
            )
            if engine_model is not None:
                report["configuration"]["fast_backend"] = engine_model.backend
        except Exception as exc:
            engine_error = f"{type(exc).__name__}: {exc}"
    report["configuration"].setdefault("fast_backend", "unavailable")
    for size in DEFAULT_IMG_SIZES:
        key = f"yoloe_tensorrt_imgsz_{size}"
        size_model = engine_models.get(size)
        if size_model is None or tuple(size_model.input_shape[-2:]) != (size, size):
            report["phases"][key] = {
                "status": "not measured",
                "reason": (
                    "no readable PNG frames"
                    if not frames
                    else engine_error
                    if engine_model is None
                    else f"fixed engine input is {size_model.input_shape}"
                ),
                "latency": aggregate_latencies([]),
            }
        else:
            report["phases"][key] = _measure_phase(
                key,
                lambda size=size: _run_yolo_samples(
                    size_model,  # noqa: B023
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
            report["phases"][key]["backend"] = size_model.backend
            report["phases"][key]["engine_input_shape"] = size_model.input_shape

    if args.skip_triangulation:
        report["phases"]["step_triangulation_cpu"] = {
            "status": "not measured",
            "reason": "disabled by --skip-triangulation",
            "latency": aggregate_latencies([]),
        }
    else:
        report["phases"]["step_triangulation_cpu"] = _measure_phase(
            "step_triangulation_cpu",
            lambda: _run_step_triangulation_samples(args.repetitions, args.warmup),
            base_process_ids,
            args.tegrastats_interval_ms,
            gpu_index,
        )
    if args.skip_depth:
        report["phases"]["depth_anything_v2_small"] = {
            "status": "not measured",
            "reason": "disabled by --skip-depth",
            "latency": aggregate_latencies([]),
        }
    else:
        report["phases"]["depth_anything_v2_small"] = _measure_phase(
            "depth_anything_v2_small",
            lambda: _run_depth_samples(args.depth_onnx, frames, args.repetitions, args.warmup),
            base_process_ids,
            args.tegrastats_interval_ms,
            gpu_index,
        )

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
            concurrent_model = engine_model
            if concurrent_model is None and args.pytorch_comparison and frames:
                try:
                    pytorch_model = _make_pytorch_yoloe(args.yoloe_weights)
                    concurrent_model = pytorch_model
                except Exception as exc:
                    yolo_error = f"{type(exc).__name__}: {exc}"
            report["configuration"]["concurrent_backend"] = (
                engine_model.backend
                if engine_model is not None
                else "pytorch_fp16"
                if concurrent_model is not None
                else None
            )
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
            if rows:
                report["phases"]["identity_one_token"] = _measure_phase(
                    "identity_one_token",
                    lambda: _repeat_identity(server, rows, args.repetitions, args.warmup, 1, True),
                    process_ids,
                    args.tegrastats_interval_ms,
                    gpu_index,
                )
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
            if engine_model is not None and tuple(engine_model.input_shape[-2:]) != (
                args.concurrent_imgsz,
                args.concurrent_imgsz,
            ):
                report["phases"]["concurrent"] = {
                    "status": "not measured",
                    "reason": "--concurrent-imgsz must match the fixed TensorRT input",
                }
            elif concurrent_model is None or not frames or not rows:
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
                    concurrent_model,
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
            # Stable names make the current identity results explicit in JSON.
            if "identity" in report["phases"]:
                report["phases"]["full_identity"] = report["phases"]["identity"]
            if "identity_one_token" in report["phases"]:
                report["phases"]["fast_identity"] = report["phases"]["identity_one_token"]

            # r14 is used for planning.  It is intentionally a separate
            # llama-server process so the report includes the model switch and
            # does not conflate caches from the identity run.
            if (
                planning_prompts
                and args.r14_gguf_model.is_file()
                and args.r14_gguf_model != args.gguf_model
            ):
                switch_started = time.perf_counter()
                old_model = args.gguf_model
                r14_server: LlamaServer | None = None
                try:
                    server.close()
                    args.gguf_model = args.r14_gguf_model
                    r14_server = LlamaServer(args, executable)
                    switch_elapsed = time.perf_counter() - switch_started
                    r14_ids = dict(base_process_ids)
                    if r14_server.pid is not None:
                        r14_ids["llama_server"] = r14_server.pid
                    report["phases"]["r14_planning"] = _measure_phase(
                        "r14_planning",
                        lambda: _repeat_planning(
                            r14_server,
                            planning_prompts,
                            args.repetitions,
                            args.warmup,
                            args.planning_max_tokens,
                        ),
                        r14_ids,
                        args.tegrastats_interval_ms,
                        gpu_index,
                    )
                    report["phases"]["r14_model_switch"] = {
                        "status": "measured",
                        "latency": {
                            "count": 1,
                            "min_s": switch_elapsed,
                            "p50_s": switch_elapsed,
                            "p95_s": switch_elapsed,
                            "max_s": switch_elapsed,
                            "mean_s": switch_elapsed,
                        },
                        "resources": r14_server
                        and {
                            "peak_rss_bytes_by_process": {
                                "llama_server": _process_rss(r14_server.pid)
                            }
                        },
                    }
                except Exception as exc:
                    report["phases"]["r14_planning"] = {
                        "status": "not measured",
                        "reason": f"{type(exc).__name__}: {exc}",
                        "latency": aggregate_latencies([]),
                    }
                    report["phases"]["r14_model_switch"] = {
                        "status": "failed",
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                finally:
                    if r14_server is not None:
                        r14_server.close()
                    args.gguf_model = old_model
            else:
                report["phases"]["r14_planning"] = {
                    "status": "not measured",
                    "reason": f"r14 GGUF not found: {args.r14_gguf_model}",
                    "latency": aggregate_latencies([]),
                }
                report["phases"]["r14_model_switch"] = {
                    "status": "not measured",
                    "reason": "r14 planning model unavailable",
                }
        finally:
            server.close()
            atexit.unregister(server.close)

    for loaded_engine in {id(model): model for model in engine_models.values()}.values():
        loaded_engine.close()
    if "identity_one_token" not in report["phases"]:
        report["phases"]["identity_one_token"] = {
            "status": "not measured",
            "reason": server_issue or identity_issue or "no identity rows",
            "latency": aggregate_latencies([]),
        }
    report["phases"].setdefault(
        "full_identity",
        report["phases"].get(
            "identity", {"status": "not measured", "latency": aggregate_latencies([])}
        ),
    )
    report["phases"].setdefault("fast_identity", report["phases"]["identity_one_token"])
    report["phases"].setdefault(
        "r14_planning",
        {
            "status": "not measured",
            "reason": "r14 planning model was not started",
            "latency": aggregate_latencies([]),
        },
    )
    report["phases"].setdefault(
        "r14_model_switch",
        {"status": "not measured", "reason": "r14 planning model was not started"},
    )
    report["budgets"] = evaluate_budgets(report)
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
    _write_markdown_chinese(report, markdown)
    print(
        json.dumps(
            {"status": report["status"], "json": str(output), "markdown": str(markdown)}, indent=2
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
