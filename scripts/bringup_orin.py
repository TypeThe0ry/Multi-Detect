#!/usr/bin/env python3
"""Copy, install, benchmark, and collect a Jetson Orin bring-up bundle.

The bundle is the prepared on-board tree (models, ONNX, llama.cpp and code;
``D:\\Projects\\mdwt\\orin-bundle\\Multi-Detect`` by default).  Its code is
refreshed from this repository's HEAD first, then the whole tree is streamed to
the board with ``tar | ssh`` — never the repository's own ``artifacts`` (a link
to hundreds of GB of experiment data).  SSH uses key authentication only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from multidetect.host_paths import host_path  # noqa: E402
REMOTE_ROOT = "Multi-Detect"
DEFAULT_BUNDLE = host_path("ORIN_BUNDLE_DIR", r"D:\Projects\mdwt\orin-bundle\Multi-Detect", ROOT / ".tmp/orin-bundle/Multi-Detect")
# Tracked paths refreshed into the bundle; its models/ONNX/llama.cpp stay as they are.
CODE_PATHS = (
    "src",
    "scripts",
    "configs",
    "deploy",
    "docs",
    "tests",
    "pyproject.toml",
    "CHANGELOG.md",
    "README.md",
)


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
        # A new board's host key is unknown; BatchMode would otherwise refuse it.
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]
    if key is not None:
        options[0:0] = ["-i", str(key)]
    return options


def _remote(user: str, host: str) -> str:
    return f"{user}@{host}"


def build_commands(
    host: str,
    user: str,
    key: Path | None,
    timestamp: str,
    bundle: Path = DEFAULT_BUNDLE,
) -> list[list[list[str]]]:
    """Return the steps executed by :func:`main`, in order.

    Each step is a pipeline: a list of commands whose stdout feeds the next.
    """
    target = _remote(user, host)
    ssh = ["ssh", *_ssh_options(key), target]
    scp = ["scp", *_ssh_options(key)]
    remote_root = f"/home/{user}/{REMOTE_ROOT}"
    copy = [
        ["tar", "-C", str(bundle), "-cf", "-", "."],
        [
            *ssh,
            f"mkdir -p {shlex.quote(remote_root)} && tar -xf - -C {shlex.quote(remote_root)}",
        ],
    ]
    date_utc = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    output = f"artifacts/orin/{timestamp}/orin-v3.json"
    install = (
        f"cd {shlex.quote(remote_root)} && "
        "sudo -n bash scripts/bootstrap_jetson_orin.sh "
        f"--date-utc {shlex.quote(date_utc)} --app-dir {shlex.quote(remote_root)} "
        f"--run-benchmark --benchmark-output {shlex.quote(output)}"
    )
    local_results = ROOT / "artifacts" / "orin" / timestamp
    fetch = [
        *scp,
        "-r",
        f"{target}:{remote_root}/artifacts/orin/{timestamp}",
        str(local_results.parent),
    ]
    return [copy, [[*ssh, install]], [fetch]]


def refresh_bundle_code(bundle: Path, revision: str = "HEAD") -> str:
    """Overwrite the bundle's tracked code with ``revision`` and record it."""
    archive = subprocess.Popen(
        ["git", "-C", str(ROOT), "archive", "--format=tar", revision, *CODE_PATHS],
        stdout=subprocess.PIPE,
    )
    extract = subprocess.run(["tar", "-x", "-C", str(bundle)], stdin=archive.stdout, check=False)
    archive.stdout.close()
    if archive.wait() != 0 or extract.returncode != 0:
        raise SystemExit("刷新上板包代码失败（git archive | tar）")
    short = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--short", revision],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (bundle / "BUNDLE_REVISION.txt").write_text(short + "\n", encoding="utf-8")
    return short


def _display(step: list[list[str]]) -> str:
    return " | ".join(shlex.join(command) for command in step)


def _run_step(step: list[list[str]]) -> None:
    if len(step) == 1:
        subprocess.run(step[0], check=True)
        return
    producer = subprocess.Popen(step[0], stdout=subprocess.PIPE)
    consumer = subprocess.run(step[1], stdin=producer.stdout, check=False)
    producer.stdout.close()
    if producer.wait() != 0 or consumer.returncode != 0:
        raise subprocess.CalledProcessError(consumer.returncode or 1, _display(step))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="一键部署并测量 Jetson Orin Nano 8GB")
    parser.add_argument("--host", required=True, help="Jetson 的 IP 地址或主机名")
    parser.add_argument("--user", required=True, help="Jetson 上的登录用户名")
    parser.add_argument("--key", type=Path, help="SSH 私钥路径；不提供时使用 ssh 默认密钥")
    parser.add_argument(
        "--bundle", type=Path, default=DEFAULT_BUNDLE, help="准备好的上板包目录（含模型）"
    )
    parser.add_argument(
        "--no-refresh", action="store_true", help="不把当前代码刷新进上板包，按包内原样上传"
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印命令，不连接或写文件")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.dry_run and not (args.bundle / "ORIN_BUNDLE_SHA256.json").is_file():
        raise SystemExit(f"上板包不完整或不存在：{args.bundle}")
    timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    steps = build_commands(args.host, args.user, args.key, timestamp, args.bundle)
    if args.no_refresh:
        print(f"# 不刷新代码，上传 {args.bundle}")
    elif args.dry_run:
        print(f"# git archive HEAD {' '.join(CODE_PATHS)} | tar -x -C {args.bundle}")
    else:
        print(f"# 上板包代码已刷新到 {refresh_bundle_code(args.bundle)}")
    if not args.dry_run:
        (ROOT / "artifacts" / "orin").mkdir(parents=True, exist_ok=True)
    for step in steps:
        print(_display(step))
        if not args.dry_run:
            _run_step(step)
    if not args.dry_run:
        print(f"# 结果：{ROOT / 'artifacts' / 'orin' / timestamp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
