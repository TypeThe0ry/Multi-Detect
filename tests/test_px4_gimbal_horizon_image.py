"""Offline build entry point checks. No Docker process is launched."""

import hashlib
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "scripts/build_px4_gimbal_horizon_image.sh"
PATCHES = ROOT / "simulation/px4/patches"
PATCH = PATCHES / "gz-gimbal-horizon-stabilization.patch"


def test_horizon_patch_applies_to_pinned_source():
    source = os.environ.get("PX4_SOURCE_ROOT")
    if source is None:
        pytest.skip("set PX4_SOURCE_ROOT to the pinned PX4 checkout")
    bridge = Path(source) / "src/modules/simulation/gz_bridge"
    for name, sha in (
        ("GZGimbal.cpp", "1b5ff1dfc8c5575ed9ebaf70f781dbd93571dfcabc23934714bbfa05d6534a7a"),
        ("GZGimbal.hpp", "5ac1e9bf6463d5c8fc221df6f93480e2b47e826e898ef727a688513eb6264e7a"),
    ):
        assert hashlib.sha256((bridge / name).read_bytes()).hexdigest() == sha
    subprocess.run(["git", "apply", "--check", str(PATCHES / "lockstep-lifetime-r1.patch"), str(PATCH)],
                   cwd=source, check=True)


def test_horizon_image_dry_run_uses_selected_tag_and_never_calls_docker(tmp_path):
    marker = tmp_path / "docker-called"
    docker = tmp_path / "docker"
    docker.write_text('#!/bin/sh\ntouch "$DOCKER_CALLED"\nexit 97\n')
    docker.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "DOCKER_CALLED": str(marker)}
    out = tmp_path / "not-created"
    result = subprocess.run([str(BUILD), "--dry-run", "--from", "local/px4:baseline", "--out", str(out)],
                            env=env, capture_output=True, text=True, check=True)
    assert "base=local/px4:baseline\n" in result.stdout
    assert "tag=local/px4:baseline-gimbal-horizon1\n" in result.stdout
    assert hashlib.sha256(PATCH.read_bytes()).hexdigest() in result.stdout
    assert not marker.exists()
    assert not out.exists()
    result = subprocess.run([str(BUILD), "--dry-run", "--from", "local/px4:baseline",
                             "--tag", "local/px4:baseline"], env=env, capture_output=True, text=True)
    assert result.returncode == 2
    assert not marker.exists()
