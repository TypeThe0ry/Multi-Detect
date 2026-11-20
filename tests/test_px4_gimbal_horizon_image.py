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
    assert "tag=local/px4:baseline-gimbal-horizon2\n" in result.stdout
    assert hashlib.sha256(PATCH.read_bytes()).hexdigest() in result.stdout
    assert not marker.exists()
    assert not out.exists()
    result = subprocess.run([str(BUILD), "--dry-run", "--from", "local/px4:baseline",
                             "--tag", "local/px4:baseline"], env=env, capture_output=True, text=True)
    assert result.returncode == 2
    assert not marker.exists()


def test_compile_check_requires_explicit_out_and_dry_run_is_read_only(tmp_path):
    out = tmp_path / "not-created"
    args = [str(BUILD), "--compile-check", "--dry-run"]
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode == 2 and "requires --out" in result.stderr
    result = subprocess.run([*args, "--out", str(out)], check=True, capture_output=True, text=True)
    assert "GZGimbal.cpp.o" in result.stdout and not out.exists()


@pytest.mark.parametrize("container_cache", [False, True], ids=["host", "work-mount"])
def test_compile_check_refreshes_only_recorded_sources_and_builds_object(tmp_path, container_cache):
    source = os.environ.get("PX4_SOURCE_ROOT")
    if not source:
        pytest.skip("set PX4_SOURCE_ROOT to the pinned checkout")
    bridge = "src/modules/simulation/gz_bridge"
    out = tmp_path / "out"
    src = out / "src"
    build = out / "build"
    build.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", source, str(src)], check=True)
    subprocess.run(["git", "sparse-checkout", "set", bridge], cwd=src, check=True)
    subprocess.run(["git", "checkout", "-q", "HEAD"], cwd=src, check=True)
    cpp = src / bridge / "GZGimbal.cpp"
    cpp.write_text(cpp.read_text() + "// previous build\n")
    receipt = subprocess.check_output(["git", "diff", "--binary"], cwd=src)
    (out / "applied.diff").write_bytes(receipt)
    prefix = Path("/work") if container_cache else out
    (build / "CMakeCache.txt").write_text(
        f"CMAKE_HOME_DIRECTORY:INTERNAL={prefix}/src\nCMAKE_CACHEFILE_DIR:INTERNAL={prefix}/build\n"
        "CMAKE_GENERATOR:INTERNAL=Ninja\n"
    )
    (build / "build.ninja").touch()
    for name in ("cmake", "docker"):
        stub = tmp_path / name
        stub.write_text('#!/bin/sh\nprintf "%s\\n" "$0" "$@" >> "$CALLS"\nexit "${STUB_RC:-0}"\n')
        stub.chmod(0o755)
    calls = tmp_path / "calls"
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "CALLS": str(calls), "PX4_BUILD_JOBS": "3"}
    args = [str(BUILD), "--compile-check", "--out", str(out)]
    subprocess.run(args, env=env, check=True)
    assert (out / "applied.diff").read_bytes() == receipt
    subprocess.run(["git", "apply", "--reverse", "--check", str(PATCH)], cwd=src, check=True)
    target = f"{bridge}/CMakeFiles/modules__simulation__gz_bridge.dir/GZGimbal.cpp.o"
    cmd = calls.read_text().splitlines()
    assert cmd[0] == str(tmp_path / ("docker" if container_cache else "cmake"))
    assert cmd[-5:] == ["--build", str(prefix / "build"), "--target", target, "-j3"]
    if container_cache:
        assert cmd[1:6] == ["run", "--rm", "--pull=never", "--network", "none"]
    mtime = cpp.stat().st_mtime_ns
    assert subprocess.run(args, env={**env, "STUB_RC": "41"}).returncode == 41
    assert cpp.stat().st_mtime_ns == mtime
    cpp.write_text("// unrecorded work\n")
    calls.unlink()
    assert subprocess.run(args, env=env).returncode == 2
    assert cpp.read_text() == "// unrecorded work\n" and not calls.exists()
