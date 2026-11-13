"""Reproduction boundaries: immutable input, inherited PX4, and CPU-only Docker."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gz_transport_backport", ROOT / "scripts/gz_transport_backport.py")
backport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backport)


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def source_case(tmp_path):
    source = tmp_path / "input"
    source.mkdir()
    git(source, "init", "-q")
    (source / "src").mkdir()
    one = source / "src/NodeShared.cc"
    two = source / "src/NodeSharedPrivate.hh"
    one.write_text("baseline\n")
    two.write_text("unchanged\n")
    git(source, "add", ".")
    git(source, "-c", "user.name=TypeThe0ry", "-c", "user.email=hahatt0902@gmail.com",
        "commit", "-qm", "test: create isolated source fixture")
    base = git(source, "rev-parse", "HEAD")
    one.write_text("fixed\n")
    patch = tmp_path / "fix.patch"
    patch.write_bytes(subprocess.check_output(["git", "-C", str(source), "diff"]))
    git(source, "add", ".")
    tree = git(source, "write-tree")
    pins = {"upstream_commit": base, "patched_source_tree": tree,
            "patch_sha256": backport.sha256(patch),
            "patched_files": {"src/NodeShared.cc": backport.sha256(one)}}
    # Input edits/private files must neither be erased nor copied into the build.
    one.write_text("user edit retained\n")
    (source / "untracked-private.txt").write_text("local asset retained\n")
    output = tmp_path / "output"
    output.mkdir()
    return source, output, pins, patch


def test_preparation_uses_exact_objects_and_preserves_input(source_case):
    source, output, pins, patch = source_case
    prepared = backport.prepare_source(source, output, pins, patch)
    assert (prepared / "src/NodeShared.cc").read_text() == "fixed\n"
    assert not (prepared / "untracked-private.txt").exists()
    assert (source / "src/NodeShared.cc").read_text() == "user edit retained\n"
    assert (source / "untracked-private.txt").read_text() == "local asset retained\n"
    assert git(prepared, "write-tree") == pins["patched_source_tree"]


def test_patch_corruption_rejected_before_cloning(source_case):
    source, output, pins, patch = source_case
    patch.write_text("corrupted")
    with pytest.raises(ValueError, match="Patch hash"):
        backport.prepare_source(source, output, pins, patch)
    assert not (output / "source").exists()


def test_unexpected_result_tree_rejected(source_case):
    source, output, pins, patch = source_case
    pins["patched_source_tree"] = "0" * 40
    with pytest.raises(ValueError, match="source tree"):
        backport.prepare_source(source, output, pins, patch)
    assert (source / "src/NodeShared.cc").read_text() == "user edit retained\n"


def test_outputs_refuse_reuse_system_disk_and_symlink_parent(tmp_path):
    with pytest.raises(FileExistsError):
        backport.new_output(tmp_path)
    with pytest.raises(ValueError, match="/data"):
        backport.new_output(Path("/tmp/gz-repair-test-output-not-created"))
    link = tmp_path / "linked"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        backport.new_output(link / "output")


def test_frozen_assets_and_runtime_px4_guard():
    pins = backport.load_pins()
    text = f"{pins['px4_sha256']}  {backport.PX4}\n{pins['reference_library_sha256']}  {backport.LIBRARY}\n"
    backport.verify_base_hashes(text, pins)
    with pytest.raises(ValueError, match="PX4"):
        backport.verify_base_hashes(text.replace(pins["px4_sha256"], "0" * 64), pins)


def test_stage_refuses_unknown_binary_without_creating_context(tmp_path):
    library = tmp_path / "unknown.so"
    library.write_bytes(b"not the validated library")
    with pytest.raises(ValueError, match="Library SHA"):
        backport.stage_library(library, tmp_path, backport.load_pins())
    assert not (tmp_path / "runtime-context").exists()


def test_docker_boundary_is_cpu_only(monkeypatch, tmp_path):
    image = "sha256:" + "a" * 64
    cid = "b" * 64
    commands = []
    def fake_command(argv, **kwargs):
        commands.append(argv)
        if argv[:3] == ["docker", "image", "inspect"]:
            return image
        if argv[:2] == ["docker", "create"]:
            return cid
        if argv[:2] == ["docker", "inspect"]:
            return json.dumps([{"Id": cid, "Image": image, "State": {"ExitCode": 0, "Running": False},
                                "HostConfig": {}, "Mounts": []}])
        raise AssertionError(argv)
    monkeypatch.setattr(backport, "command", fake_command)
    monkeypatch.setattr(backport.subprocess, "run", lambda *args, **kwargs: None)
    backport.docker_phase(image, tmp_path, "test", ["/usr/bin/true"], [],
                          cpuset="56,62", cpus=2, memory_gb=2, timeout=10)
    create = next(x for x in commands if x[:2] == ["docker", "create"])
    for expected in ["--runtime=runc", "--network=none", "--cpus=2", "--memory=2g", "--memory-swap=2g", "CUDA_VISIBLE_DEVICES=", "NVIDIA_VISIBLE_DEVICES=void"]:
        assert expected in create
    assert not any(x.startswith(("--gpus", "--privileged", "--pid=host")) for x in create)


def test_validation_worker_is_reachable_through_its_readonly_mount(tmp_path):
    source, work, context = [tmp_path / name for name in ("source", "work", "context")]
    for directory in (source, work, context):
        directory.mkdir()
    entry, mounts = backport.validation_spec(source, work, context)
    worker = next(Path(x) for x in entry if x.endswith("runner.py"))
    candidates = []
    for value in mounts:
        if not value.startswith("type=bind,"):
            continue
        values = dict(part.split("=", 1) for part in value.split(",") if "=" in part)
        target = Path(values["dst"])
        if worker.is_relative_to(target):
            assert value.endswith(",readonly")
            candidates.append(Path(values["src"]) / worker.relative_to(target))
    assert len(candidates) == 1 and candidates[0].is_file()
