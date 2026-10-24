#!/usr/bin/env python3
"""Start the SIM operator-link session and the real MultiDetectGCS together."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = Path(r"D:\Projects\mdwt\_scratch\cx-demo-launcher")
DEFAULT_LOCK = Path(r"D:\Projects\mdwt\_scratch\sitl.lock")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from multidetect.autonomy.mission_security import AuthenticationKey, sign_document  # noqa: E402
from scripts.run_nightly_regression import SitlLock  # noqa: E402


@dataclass
class OwnedResources:
    session: subprocess.Popen[str] | None = None
    gcs: subprocess.Popen[str] | None = None
    container_name: str | None = None


def mask(value: str) -> str:
    if not value:
        return value
    return value[:4] + "..." + value[-4:] if len(value) > 10 else "***"


def find_gcs(path: str | Path | None) -> Path:
    if path:
        candidate = Path(path).expanduser()
        if not candidate.is_file():
            raise FileNotFoundError(f"GCS executable not found: {candidate}")
        return candidate.resolve()
    candidates = sorted(
        ROOT.glob("build/windows-*/portable-*/bin/MultiDetectGCS.exe"), reverse=True
    )
    candidates += sorted(ROOT.glob("build/**/MultiDetectGCS.exe"), reverse=True)
    if not candidates:
        raise FileNotFoundError("no portable MultiDetectGCS.exe found; pass --gcs-exe")
    return candidates[0].resolve()


def gcs_environment(link: dict[str, Any], base: dict[str, str] | None = None) -> dict[str, str]:
    """Map operator-link.json names to the names consumed by MultiDetectGCS."""
    required = ("operator_port", "operator_hmac_key_hex", "operator_mavlink_signing_key_hex")
    missing = [key for key in required if not link.get(key)]
    if missing:
        raise ValueError("operator-link.json missing: " + ", ".join(missing))
    env = dict(base or os.environ)
    hmac_hex = str(link["operator_hmac_key_hex"])
    package_hex = str(link.get("operator_package_key_hex") or hmac_hex)
    env.update(
        {
            "MULTIDETECT_EXPECTED_RUNTIME_MODE": "SIM",
            "MULTIDETECT_OPERATOR_KEY": hmac_hex,
            "MULTIDETECT_OPERATOR_ID": "sim-qgc-operator",
            "MULTIDETECT_OPERATOR_UDP_HOST": str(link.get("operator_bind_host", "127.0.0.1")),
            "MULTIDETECT_OPERATOR_UDP_PORT": str(link["operator_port"]),
            "MULTIDETECT_OPERATOR_UDP_LOCAL_PORT": str(int(link["operator_port"]) + 1),
            "MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX": str(link["operator_mavlink_signing_key_hex"]),
            "MULTIDETECT_OPERATOR_SIGNING_KEY": f"hmac:{package_hex}",
            "MULTIDETECT_GROUND_COMMAND_KEY": f"hmac:{hmac_hex}",
            "MULTIDETECT_OPERATOR_STREAM_ID": "camera-main",
            "MULTIDETECT_OPERATOR_STREAM_WIDTH": "1280",
            # Must match the camera-main geometry the onboard operator link registers
            # (scripts/onboard_main.py: VideoGeometry("camera-main", 1280, 720)).
            "MULTIDETECT_OPERATOR_STREAM_HEIGHT": "720",
            "MULTIDETECT_OPERATOR_STREAM_ROTATION": "0",
            "MULTIDETECT_OPERATOR_GCS_SYSTEM_ID": "255",
            "MULTIDETECT_OPERATOR_JETSON_SYSTEM_ID": "1",
            "MULTIDETECT_OPERATOR_JETSON_COMPONENT_ID": "191",
        }
    )
    # These are deliberately absent for a human acceptance run.
    for name in (
        "MULTIDETECT_UI_AUTO_EXIT_S",
        "MULTIDETECT_HIL_UI_AUTO_EXIT",
        "MULTIDETECT_HIL_UI_AUTO_EXERCISE",
    ):
        env.pop(name, None)
    return env


def sign_task_package(source: Path, destination: Path, package_hex: str) -> Path:
    document = json.loads(source.read_text(encoding="utf-8"))
    key = AuthenticationKey("hmac-sha256", bytes.fromhex(package_hex), "default")
    destination.write_text(
        json.dumps(sign_document(document, key), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination


def redact_environment(env: dict[str, str]) -> dict[str, str]:
    secret_names = ("KEY", "SIGNING", "PACKAGE")
    return {
        key: (mask(value) if any(token in key for token in secret_names) else value)
        for key, value in env.items()
        if key.startswith("MULTIDETECT_")
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--world")
    parser.add_argument("--airframe", default="x500_mono_cam")
    parser.add_argument("--gcs-exe")
    parser.add_argument("--task-package", type=Path)
    parser.add_argument("--operator-out", type=Path, default=SCRATCH / "operator-out")
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--wait-timeout", type=float, default=300.0)
    return parser


def cleanup(resources: OwnedResources, docker: str | None = None) -> None:
    # Capture only descendants of our own session process before terminating it;
    # this covers the collector/Docker CLI without name-based process matching.
    descendants = []
    if resources.session is not None and resources.session.poll() is None:
        try:
            import psutil

            descendants = [
                psutil.Process(child.pid)
                for child in psutil.Process(resources.session.pid).children(recursive=True)
            ]
        except Exception:
            descendants = []
    for process in (resources.gcs, resources.session):
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
    for child in descendants:
        try:
            child.terminate()
        except (OSError, ProcessLookupError):
            pass
    for child in descendants:
        try:
            child.wait(timeout=3)
        except Exception:
            try:
                child.kill()
            except (OSError, ProcessLookupError):
                pass
    if docker and resources.container_name:
        subprocess.run(
            [docker, "stop", "--timeout", "2", resources.container_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            [docker, "rm", resources.container_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    out = args.operator_out.resolve()
    container = f"multidetect-operator-demo-{uuid.uuid4().hex[:8]}"
    session_cmd = [
        sys.executable,
        str(ROOT / "scripts/run_sim_session.py"),
        "--operator-link",
        "--airframe",
        args.airframe,
        "--onboard-out",
        str(out),
        "--container-name",
        container,
        "--session-log",
        str(SCRATCH / "sim-session.log"),
    ]
    if args.world:
        session_cmd += ["--world", args.world]
    if args.dry_run:
        print("SITL lock:", args.lock)
        print("SIM command:", subprocess.list2cmdline(session_cmd))
        print(
            "GCS command:",
            subprocess.list2cmdline(
                [
                    str(Path(args.gcs_exe).resolve())
                    if args.gcs_exe
                    else "<auto-portable-MultiDetectGCS.exe>"
                ]
            ),
        )
        fake = {
            "operator_port": 14580,
            "operator_bind_host": "127.0.0.1",
            "operator_hmac_key_hex": "0" * 64,
            "operator_mavlink_signing_key_hex": "1" * 64,
            "operator_package_key_hex": "2" * 64,
        }
        print(
            "GCS environment:",
            json.dumps(redact_environment(gcs_environment(fake)), ensure_ascii=False, indent=2),
        )
        if args.task_package:
            print("Task package:", args.task_package.resolve(), "(signed after link is available)")
        return 0
    gcs_exe = find_gcs(args.gcs_exe)
    lock = SitlLock(args.lock, label="operator-demo", retry_s=2)
    resources = OwnedResources(container_name=container)
    try:
        lock.acquire(wait=False)
        out.mkdir(parents=True, exist_ok=True)
        resources.session = subprocess.Popen(
            session_cmd,
            cwd=ROOT,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        link_path = out / "operator-link.json"
        deadline = time.monotonic() + args.wait_timeout
        while (
            not link_path.is_file()
            and resources.session.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.5)
        if not link_path.is_file():
            raise RuntimeError("operator-link.json was not produced before timeout/session exit")
        link = json.loads(link_path.read_text(encoding="utf-8"))
        env = gcs_environment(link)
        package = args.task_package
        if package:
            signed = out / (package.stem + ".signed.json")
            sign_task_package(package.resolve(), signed, str(link["operator_package_key_hex"]))
            package = signed
        resources.gcs = subprocess.Popen(
            [str(gcs_exe)],
            cwd=gcs_exe.parent,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        print("地面站联合验收已启动。")
        print(
            "在地面站点击“任务包”加载任务文件；收到计划后使用批准/拒绝按钮。可用暂停、恢复、中止验证控制链路，结束后查看最终摘要。"
        )
        if package:
            print(f"已生成会话签名任务包：{package}")
        print("noVNC：http://127.0.0.1:6080/vnc.html?autoconnect=1")
        while resources.gcs.poll() is None and resources.session.poll() is None:
            time.sleep(1)
        return resources.gcs.returncode or resources.session.returncode or 0
    except KeyboardInterrupt:
        return 0
    finally:
        cleanup(resources, shutil.which("docker"))
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
