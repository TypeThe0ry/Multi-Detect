#!/usr/bin/env python3
"""Check NVIDIA EGL rendering and camera throughput in one disposable SIM container.

This script plans and starts the same PX4/Gazebo container as the collector.  It
does not pull images or build Docker layers.  The caller owns the real Docker
run and should use the derived image when the pinned image lacks EGL userspace
packages (see ``simulation/docker/gpu/Dockerfile``).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts.gazebo_sensor_stream import WIRE_HEADER_BYTES, unpack_header  # noqa: E402
from scripts.run_simulator_docker import (  # noqa: E402
    PINNED_IMAGE,
    _split_gpu_list,
    build_docker_simulator_plan,
)


def _frame_count(payload: bytes) -> int:
    """Count complete MDGZSIM3 RGB records without decoding image pixels."""

    count = 0
    offset = 0
    while len(payload) - offset >= WIRE_HEADER_BYTES:
        header = unpack_header(payload[offset : offset + WIRE_HEADER_BYTES])
        record_bytes = WIRE_HEADER_BYTES + int(header.payload_bytes)
        if record_bytes > len(payload) - offset:
            break
        offset += record_bytes
        count += 1
    return count


def _renderer_is_gpu(text: str) -> bool:
    lowered = text.lower()
    if "software rasterizer" in lowered or "llvmpipe" in lowered or "mesa swrast" in lowered:
        return False
    return bool(re.search(r"nvidia|geforce|quadro|rtx\s*30", lowered, re.IGNORECASE))


def build_stream_command(
    *,
    container: str,
    model: str,
    frames: int,
    timeout_seconds: float,
) -> list[str]:
    return [
        "docker",
        "exec",
        container,
        "python3",
        "/workspace/scripts/gazebo_sensor_stream.py",
        "--frames",
        str(frames),
        "--timeout-seconds",
        f"{timeout_seconds:.3f}",
        "--world",
        "default",
        "--model-instance",
        model,
        "--image-topic",
        f"/world/default/model/{model}/link/camera_link/sensor/camera/image",
        "--camera-info-topic",
        f"/world/default/model/{model}/link/camera_link/sensor/camera/camera_info",
        "--navsat-topic",
        f"/world/default/model/{model}/link/base_link/sensor/navsat_sensor/navsat",
        "--odometry-topic",
        f"/model/{model}/odometry_with_covariance",
    ]


def _wait_running(container: str, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip().lower() == "true":
            return
        if result.returncode == 0 and result.stdout.strip().lower() == "false":
            raise RuntimeError(f"SIM container exited before readiness: {container}")
        time.sleep(0.25)
    raise TimeoutError(f"SIM container did not become running within {timeout_seconds:.1f}s")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, default=ROOT)
    parser.add_argument("--job-dir", type=Path, default=None)
    parser.add_argument("--image", default=PINNED_IMAGE)
    parser.add_argument("--container-name", default="md-sim-gpu-check")
    parser.add_argument("--gpu-device", default=None)
    parser.add_argument("--gpu-devices", default=None)
    parser.add_argument("--worker-index", type=int, default=0)
    parser.add_argument("--frames", type=int, default=60)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--startup-timeout-seconds", type=float, default=240.0)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    if args.frames < 1 or args.timeout_seconds <= 0 or args.startup_timeout_seconds <= 0:
        raise SystemExit("frames and timeouts must be positive")
    scenario = args.scenario.resolve()
    job_dir = args.job_dir or ROOT / "_scratch" / "sim-gpu-check"
    gpu_devices = (
        _split_gpu_list(args.gpu_devices, label="--gpu-devices")
        if args.gpu_devices is not None
        else None
    )
    plan = build_docker_simulator_plan(
        scenario,
        repository_root=args.repository_root,
        job_dir=job_dir,
        container_name=args.container_name,
        image=args.image,
        gpu_render=True,
        gpu_device=args.gpu_device,
        gpu_devices=gpu_devices,
        worker_index=args.worker_index,
    )
    model = plan.model_name if plan.model_name.endswith("_0") else f"{plan.model_name}_0"
    container = plan.container_name
    started = False
    try:
        launch = subprocess.run(plan.command, capture_output=True, text=True, check=False, timeout=30)
        if launch.returncode != 0:
            raise RuntimeError((launch.stderr or launch.stdout or "Docker launch failed").strip())
        started = True
        _wait_running(container, args.startup_timeout_seconds)
        egl = subprocess.run(
            ["docker", "exec", container, "sh", "-lc", "eglinfo -B 2>&1 || true"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        logs = subprocess.run(
            ["docker", "logs", "--tail", "200", container],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        stream_command = build_stream_command(
            container=container,
            model=model,
            frames=args.frames,
            timeout_seconds=args.timeout_seconds,
        )
        started_at = time.monotonic()
        stream = subprocess.run(
            stream_command,
            capture_output=True,
            check=False,
            timeout=args.timeout_seconds + 10.0,
        )
        elapsed = max(time.monotonic() - started_at, 1.0e-9)
        frames = _frame_count(stream.stdout)
        renderer_text = (egl.stdout + egl.stderr + logs.stdout + logs.stderr).strip()
        result = {
            "gpu_rendering_requested": True,
            "gpu_device": plan.gpu_device,
            "gpu_rendering_active": _renderer_is_gpu(renderer_text),
            "frames": frames,
            "fps": frames / elapsed,
            "renderer_info": renderer_text[-12000:],
            "stream_stderr": stream.stderr.decode("utf-8", "replace")[-4000:],
            "container": container,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["gpu_rendering_active"] and frames > 0 else 1
    finally:
        if started:
            subprocess.run(
                ["docker", "rm", "--force", container],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

