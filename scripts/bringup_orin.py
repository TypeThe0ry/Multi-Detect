#!/usr/bin/env python3
"""Copy, install, benchmark, and collect a Jetson Orin bring-up bundle."""

from __future__ import annotations

import argparse
import datetime as dt
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REMOTE_ROOT = "Multi-Detect"


def _ssh_options(key: Path | None) -> list[str]:
    options = [
        "-o",
        "BatchMode=yes",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "PreferredAuthentications=publickey",
    ]
    if key is not None:
        options[0:0] = ["-i", str(key)]
    return options


def _remote(user: str, host: str) -> str:
    return f"{user}@{host}"


def build_commands(host: str, user: str, key: Path | None, timestamp: str) -> list[list[str]]:
    """Return the exact commands executed by :func:`main`, in order."""
    target = _remote(user, host)
    ssh = ["ssh", *_ssh_options(key), target]
    scp = ["scp", *_ssh_options(key)]
    remote_root = f"/home/{user}/{REMOTE_ROOT}"
    bundle_parts = ["src", "scripts", "docs", "configs", "pyproject.toml", "README.md"]
    optional_parts = ["tmp", "artifacts"]
    bundle_parts.extend(name for name in optional_parts if (ROOT / name).exists())

    commands = [
        [*ssh, "mkdir -p", remote_root],
        [*scp, *(str(ROOT / part) for part in bundle_parts), f"{target}:{remote_root}/"],
    ]
    date_utc = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    output = f"artifacts/orin/{timestamp}/orin-v3.json"
    install = (
        f"cd {shlex.quote(remote_root)} && "
        "sudo -n bash scripts/bootstrap_jetson_orin.sh "
        f"--date-utc {shlex.quote(date_utc)} --app-dir {shlex.quote(remote_root)} "
        f"--run-benchmark --benchmark-output {shlex.quote(output)}"
    )
    commands.append([*ssh, install])
    local_results = ROOT / "artifacts" / "orin" / timestamp
    commands.append(
        [
            *scp,
            "-r",
            f"{target}:{remote_root}/artifacts/orin/{timestamp}",
            str(local_results.parent),
        ]
    )
    return commands


def _display(command: list[str]) -> str:
    return shlex.join(command)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="一键部署并测量 Jetson Orin Nano 8GB")
    parser.add_argument("--host", required=True, help="Jetson 的 IP 地址或主机名")
    parser.add_argument("--user", required=True, help="Jetson 上的登录用户名")
    parser.add_argument("--key", type=Path, help="SSH 私钥路径；不提供时使用 ssh 默认密钥")
    parser.add_argument("--dry-run", action="store_true", help="只打印命令，不连接或写文件")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    commands = build_commands(args.host, args.user, args.key, timestamp)
    if not args.dry_run:
        (ROOT / "artifacts" / "orin").mkdir(parents=True, exist_ok=True)
    for command in commands:
        print(_display(command))
        if not args.dry_run:
            subprocess.run(command, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
