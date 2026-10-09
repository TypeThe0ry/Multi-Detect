"""Bounded, read-only capture-runtime probe; never start Docker or a simulator."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

PINNED_IMAGE = (
    "px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8"
)


def command_result(command: list[str], timeout_s: float) -> dict:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"exit_code": None, "failure": type(exc).__name__, "succeeded": False}
    return {
        "exit_code": result.returncode,
        "succeeded": result.returncode == 0,
        "stdout": result.stdout.strip()[:2000],
        "stderr": result.stderr.strip()[:2000],
    }


def probe(timeout_s: float = 8.0) -> dict:
    docker = shutil.which("docker")
    gz = shutil.which("gz")
    report = {
        "schema_version": 1,
        "contract": "multidetect.gazebo-capture-runtime-readonly-probe-v1",
        "read_only": True,
        "docker_cli_available": docker is not None,
        "gazebo_cli_available": gz is not None,
        "pinned_image": PINNED_IMAGE,
        "native_gazebo": None
        if gz is None
        else command_result([gz, "sim", "--versions"], timeout_s),
        "docker_server": None,
        "docker_image": None,
        "container_capture_prerequisites_ready": False,
        "sensor_capture_performed": False,
        "rendering_verified": False,
        "training_ready": False,
        "candidate_promoted": False,
        "physical_performance_claimed": False,
        "hil_real_evidence": False,
        "outdoor_gnss_evidence": False,
        "final_release_allowed": False,
    }
    if docker is not None:
        server = command_result([docker, "info", "--format", "{{.ServerVersion}}"], timeout_s)
        report["docker_server"] = server
        if server["succeeded"]:
            image = command_result(
                [docker, "image", "inspect", PINNED_IMAGE, "--format", "{{json .RepoDigests}}"],
                timeout_s,
            )
            report["docker_image"] = image
            if image["succeeded"]:
                try:
                    digests = json.loads(image["stdout"])
                except (ValueError, TypeError):
                    digests = []
                report["container_capture_prerequisites_ready"] = (
                    isinstance(digests, list) and PINNED_IMAGE in digests
                )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-s", type=float, default=8.0)
    args = parser.parse_args(argv)
    if not 0.1 <= args.timeout_s <= 30.0:
        raise ValueError("timeout must be between 0.1 and 30 seconds")
    if args.output.exists():
        raise FileExistsError(f"refusing to replace runtime probe: {args.output}")
    report = probe(args.timeout_s)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["container_capture_prerequisites_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
