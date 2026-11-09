#!/usr/bin/env python3
"""Prepare/build/validate the pinned transport13 repair, without adopting an image."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "simulation/gz-transport"
PX4 = "/opt/px4-gazebo/bin/px4"
LIBRARY = "/usr/lib/x86_64-linux-gnu/libgz-transport13.so.13.5.0"


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def command(argv: list[str], *, cwd: Path | None = None) -> str:
    return subprocess.check_output(argv, cwd=cwd, text=True, stderr=subprocess.PIPE).strip()


def load_pins() -> dict:
    pins = json.loads((ASSETS / "pins.json").read_text())
    if sha256(ASSETS / pins["patch"]) != pins["patch_sha256"]:
        raise ValueError("Frozen transport patch hash mismatch")
    if sha256(ROOT / "simulation/px4/patches/lockstep-lifetime-r1.patch") != pins["px4_patch_sha256"]:
        raise ValueError("Existing PX4 repair no longer matches the reviewed input")
    for name, expected in pins["validation_assets"].items():
        if sha256(ASSETS / "validation" / name) != expected:
            raise ValueError(f"Validation asset changed: {name}")
    dockerfile = (ASSETS / "Dockerfile.runtime").read_text()
    if pins["px4_sha256"] not in dockerfile or pins["validated_library_sha256"] not in dockerfile:
        raise ValueError("Runtime Dockerfile lost its inherited PX4/library checks")
    return pins


def new_output(path: Path) -> Path:
    path = path.absolute()
    if "," in str(path) or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Output/mount paths must not contain commas or symlink parents")
    if path.exists():
        raise FileExistsError(f"Output must be fresh: {path}")
    resolved = path.resolve()
    if not resolved.is_relative_to("/data"):
        raise ValueError("Sources, outputs and build artifacts must stay on /data")
    resolved.mkdir(parents=True, exist_ok=False)
    return resolved


def prepare_source(source: Path, output: Path, pins: dict, patch: Path) -> Path:
    """Clone exact Git objects; ignore and preserve all input working-tree edits."""
    if sha256(patch) != pins["patch_sha256"]:
        raise ValueError("Patch hash mismatch")
    source = source.resolve(strict=True)
    command(["git", "-C", str(source), "cat-file", "-e", pins["upstream_commit"] + "^{commit}"])
    dest = output / "source"
    command(["git", "clone", "--quiet", "--no-hardlinks", "--no-checkout", str(source), str(dest)])
    command(["git", "checkout", "--quiet", "--detach", pins["upstream_commit"]], cwd=dest)
    command(["git", "apply", "--index", str(patch.resolve())], cwd=dest)
    if command(["git", "write-tree"], cwd=dest) != pins["patched_source_tree"]:
        raise ValueError("Patched source tree differs from the frozen repair")
    command(["git", "diff", "--exit-code"], cwd=dest)
    for name, expected in pins["patched_files"].items():
        if sha256(dest / name) != expected:
            raise ValueError(f"Patched file mismatch: {name}")
    return dest


def stage_library(library: Path, output: Path, pins: dict) -> dict:
    actual = sha256(library)
    if actual != pins["validated_library_sha256"]:
        raise ValueError(f"Library SHA {actual} differs from the validated Release; inspect build inputs, do not bypass this guard")
    context = output / "runtime-context"
    context.mkdir(exist_ok=False)
    shutil.copyfile(library, context / Path(LIBRARY).name)
    shutil.copyfile(ASSETS / "Dockerfile.runtime", context / "Dockerfile")
    return {"library_sha256": actual, "context": str(context), "image_built": False,
            "runtime_base_image_id": pins["runtime_base_image_id"], "px4_sha256": pins["px4_sha256"]}


def bind(source: Path, target: str, *, readonly: bool = True) -> list[str]:
    source = source.resolve(strict=True)
    if "," in str(source):
        raise ValueError("Docker bind source must not contain a comma")
    return ["--mount", f"type=bind,src={source},dst={target}" + (",readonly" if readonly else "")]


def docker_phase(image: str, output: Path, label: str, entry: list[str], mounts: list[str],
                 *, cpuset: str | None, cpus: int, memory_gb: int, timeout: int) -> dict:
    if command(["docker", "image", "inspect", image, "--format", "{{.Id}}"]) != image:
        raise ValueError("An exact local immutable Docker image is required; no pull/install fallback")
    token = uuid.uuid4().hex
    argv = ["docker", "create", "--label", "multidetect.gz-repair=" + token,
            "--runtime=runc", "--network=none", f"--cpus={cpus}", "--cpu-shares=64",
            f"--memory={memory_gb}g", f"--memory-swap={memory_gb}g", "--pids-limit=128",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only",
            "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=256m", "--env", "HOME=/tmp",
            "--env", "CUDA_VISIBLE_DEVICES=", "--env", "NVIDIA_VISIBLE_DEVICES=void",
            "--env", "DISPLAY=", "--env", "WAYLAND_DISPLAY="]
    if cpuset:
        argv += ["--cpuset-cpus=" + cpuset]
    argv += mounts + ["--entrypoint", entry[0], image, *entry[1:]]
    cid = command(argv)
    record = {"image_id": image, "container_id": cid, "ownership_label": token, "create_argv": argv}
    receipt = output / (label + "-container.json")
    write_json(receipt, record)
    try:
        with (output / (label + ".stdout")).open("w") as stdout, (output / (label + ".stderr")).open("w") as stderr:
            subprocess.run(["docker", "start", "--attach", cid], stdout=stdout, stderr=stderr,
                           timeout=timeout, check=False)
    except BaseException:
        # Only the exact container created above can be stopped on interruption.
        info = json.loads(command(["docker", "inspect", cid]))[0]
        if info["Id"] == cid and info["Config"]["Labels"].get("multidetect.gz-repair") == token:
            subprocess.run(["docker", "kill", cid], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        raise
    finally:
        info = json.loads(command(["docker", "inspect", cid]))[0]
        record.update(state=info["State"], actual_image_id=info["Image"],
                      resources={k: info["HostConfig"].get(k) for k in
                                 ["Runtime", "NetworkMode", "NanoCpus", "CpusetCpus", "Memory", "MemorySwap", "DeviceRequests"]},
                      mounts=info["Mounts"])
        write_json(receipt, record)
    if record["state"]["ExitCode"] != 0 or record["state"]["Running"]:
        raise RuntimeError(f"{label} failed; retained container {cid} and raw logs")
    return record


def validation_spec(source: Path, work: Path, context: Path) -> tuple[list[str], list[str]]:
    worker = ASSETS / "validation/runner.py"
    if not worker.is_file():
        raise ValueError("Missing protocol validation worker")
    entry = ["/usr/bin/nice", "-n", "10", "/usr/bin/python3", "/assets/validation/runner.py", "/out"]
    mounts = bind(ASSETS, "/assets") + bind(source, "/src") + bind(work, "/out", readonly=False) + bind(context, "/candidate-lib")
    return entry, mounts


def verify_base_hashes(text: str, pins: dict) -> None:
    values = {line.split()[-1]: line.split()[0] for line in text.splitlines() if len(line.split()) == 2}
    if values.get(PX4) != pins["px4_sha256"] or values.get(LIBRARY) != pins["reference_library_sha256"]:
        raise ValueError("Runtime base changed: the existing PX4 repair and reference SO are required")


def check_base(output: Path, pins: dict, cpuset: str | None) -> None:
    actual_tag = command(["docker", "image", "inspect", pins["runtime_base_tag"], "--format", "{{.Id}}"])
    if actual_tag != pins["runtime_base_image_id"]:
        raise ValueError("Runtime base tag drifted; no existing tag will be overwritten")
    docker_phase(pins["runtime_base_image_id"], output, "base", ["/usr/bin/sha256sum", PX4, LIBRARY], [],
                 cpuset=cpuset, cpus=1, memory_gb=1, timeout=30)
    verify_base_hashes((output / "base.stdout").read_text(), pins)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["prepare", "build", "validate"])
    parser.add_argument("--source", required=True, type=Path, help="Read-only local Git cache containing the pinned upstream commit")
    parser.add_argument("--output", required=True, type=Path, help="Fresh directory under /data; never reused")
    parser.add_argument("--library", type=Path, help="Validated Release SO (required for validate, optional staging input for prepare)")
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--cpuset", help="Optional CPU set; workstation example: 56,62")
    args = parser.parse_args(argv)
    if not 1 <= args.jobs <= 8:
        parser.error("--jobs must be 1..8")
    if args.mode == "validate" and args.library is None:
        parser.error("validate requires --library")
    if args.mode == "build" and args.library is not None:
        parser.error("build does not accept an imported --library")
    pins = load_pins()
    output = new_output(args.output)
    manifest = {"mode": args.mode, "pins": pins, "state": "preparing", "image_built": False,
                "flight_acceptance": "pending; protocol components do not establish flight acceptance"}
    write_json(output / "manifest.json", manifest)
    try:
        source = prepare_source(args.source, output, pins, ASSETS / pins["patch"])
        manifest["source_tree"] = pins["patched_source_tree"]
        work = output / "work"
        work.mkdir()
        if args.mode == "build":
            check_base(output, pins, args.cpuset)
            script = """set -euo pipefail
cmake -S /src -B /out/build -G Ninja -DBUILD_TESTING=OFF -DCMAKE_BUILD_TYPE=RelWithDebInfo -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
cmake --build /out/build --target gz-transport13 --parallel "${MD_GZ_JOBS}"
g++ --version > /out/compiler.txt
dpkg-query -W > /out/system-packages.txt
"""
            mounts = bind(source, "/src") + bind(work, "/out", readonly=False)
            mounts += ["--env", f"MD_GZ_JOBS={args.jobs}"]
            docker_phase(pins["toolchain_image_id"], output, "build", ["/usr/bin/nice", "-n", "10", "/bin/bash", "-c", script], mounts,
                         cpuset=args.cpuset, cpus=args.jobs, memory_gb=8, timeout=1800)
            library = work / "build/lib" / Path(LIBRARY).name
            manifest["runtime"] = stage_library(library, output, pins)
        elif args.library is not None:
            manifest["runtime"] = stage_library(args.library, output, pins)
        if args.mode == "validate":
            check_base(output, pins, args.cpuset)
            context = output / "runtime-context"
            # The original SONAME resolves to exactly the imported, hash-checked SO.
            (context / "libgz-transport13.so.13").symlink_to(Path(LIBRARY).name)
            entry, mounts = validation_spec(source, work, context)
            docker_phase(pins["toolchain_image_id"], output, "validation", entry,
                         mounts, cpuset=args.cpuset, cpus=2, memory_gb=2, timeout=300)
            result = json.loads((work / "results.json").read_text())
            if result.get("component_acceptance_pass") is not True:
                raise RuntimeError("Protocol component validation failed; raw evidence retained")
            manifest["component_acceptance_pass"] = True
        command(["git", "diff", "--exit-code"], cwd=source)
        if command(["git", "write-tree"], cwd=source) != pins["patched_source_tree"]:
            raise ValueError("Prepared source index changed during build/validation")
        manifest["state"] = "completed"
    except BaseException as exc:
        manifest.update(state="failed", error=str(exc))
        raise
    finally:
        write_json(output / "manifest.json", manifest)
    print(json.dumps({"output": str(output), "state": manifest["state"], "image_built": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
