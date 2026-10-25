"""Model-free regression checks for round orchestration and resource guards."""

from __future__ import annotations

import json
import shutil
import sys
import uuid
from types import SimpleNamespace

import pytest

from scripts import model_round_guard as guard
from scripts import run_model_round_eval as round_eval


@pytest.fixture
def tmp_path():
    """Keep temporary artifacts inside the worktree allowed to the test runner."""
    path = round_eval.ROOT / ".pytest-model-round" / uuid.uuid4().hex
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_lock_contention_preserves_owner_and_releases(tmp_path):
    lock = tmp_path / "gpu.lock"
    with guard.GPULock(lock, "owner"):
        owner = lock.read_text(encoding="utf-8")
        assert json.loads(owner)["model"] == "owner"
        with pytest.raises(RuntimeError, match="GPU lock held"):
            with guard.GPULock(lock, "contender"):
                pytest.fail("A contender acquired an occupied lock")
        assert lock.read_text(encoding="utf-8") == owner
    assert not lock.exists()


def test_stale_lock_dead_owner_is_removed_and_replaced(tmp_path, monkeypatch, caplog):
    lock = tmp_path / "gpu.lock"
    lock.write_text(
        json.dumps(
            {
                "pid": 424242,
                "host": guard.socket.gethostname(),
                "start_time": 123.0,
                "model": "dead-owner",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(guard, "_process_snapshot", lambda pid: (False, None))
    with guard.GPULock(lock, "replacement"):
        owner = json.loads(lock.read_text(encoding="utf-8"))
        assert owner["model"] == "replacement"
        assert owner["pid"] == __import__("os").getpid()
        assert "start_time" in owner
    assert "Removing stale GPU lock" in caplog.text


def test_stale_lock_pid_reuse_is_removed(tmp_path, monkeypatch, caplog):
    lock = tmp_path / "gpu.lock"
    lock.write_text(
        json.dumps(
            {
                "pid": 424242,
                "host": guard.socket.gethostname(),
                "start_time": 123.0,
                "model": "old-owner",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(guard, "_process_snapshot", lambda pid: (True, 456.0))
    with guard.GPULock(lock, "replacement"):
        assert json.loads(lock.read_text(encoding="utf-8"))["model"] == "replacement"
    assert "different process start time" in caplog.text


def test_live_lock_cannot_be_stolen_even_with_matching_start_time(tmp_path, monkeypatch):
    lock = tmp_path / "gpu.lock"
    lock.write_text(
        json.dumps(
            {
                "pid": 424242,
                "host": guard.socket.gethostname(),
                "start_time": 456.0,
                "model": "live-owner",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(guard, "_process_snapshot", lambda pid: (True, 456.0))
    with pytest.raises(RuntimeError, match="GPU lock held"):
        with guard.GPULock(lock, "contender"):
            pytest.fail("A live lock owner must not be replaced")
    assert json.loads(lock.read_text(encoding="utf-8"))["model"] == "live-owner"


def test_force_never_bypasses_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(round_eval, "ROOT", tmp_path)
    out = tmp_path / "round"
    out.mkdir()
    previous = out / "summary.json"
    previous.write_text('{"owner": true}', encoding="utf-8")
    lock = tmp_path / "gpu.lock"

    def forbidden(*args, **kwargs):
        pytest.fail("Lock contention must stop before GPU inspection or stage execution")

    monkeypatch.setattr(round_eval, "check_gpu", forbidden)
    with guard.GPULock(lock, "owner"):
        result = round_eval.main(
            [
                "--model",
                "contender",
                "--stages",
                "offline",
                "--out",
                str(out),
                "--lock-file",
                str(lock),
                "--force",
            ],
            runner=forbidden,
        )
        assert result == 1
        assert json.loads(previous.read_text(encoding="utf-8")) == {"owner": True}
        assert lock.exists()


def test_disk_guard_probes_existing_parent_and_accepts_boundary(tmp_path, monkeypatch):
    probes = []

    def disk_usage(path):
        probes.append(path)
        return SimpleNamespace(free=30_000_000_000)

    monkeypatch.setattr(guard.shutil, "disk_usage", disk_usage)
    target = tmp_path / "missing" / "output"
    guard.ensure_free_disk(target, 30)
    assert probes == [tmp_path.resolve()]
    with pytest.raises(RuntimeError, match="Disk guard.*need 31 GB"):
        guard.ensure_free_disk(target, 31)
    assert not target.exists()


@pytest.mark.parametrize(
    "output",
    [
        "123, trainer.exe, 512\n",
        "123, llama-server, 4096\n",
        "123, compute.exe, N/A\n",
    ],
)
def test_gpu_guard_rejects_heavy_or_unknown_consumers(output):
    def runner(command, **kwargs):
        assert command[0] == "nvidia-smi"
        assert kwargs["check"] is True
        return SimpleNamespace(stdout=output)

    with pytest.raises(RuntimeError, match="Other heavy GPU processes"):
        guard.check_gpu(runner=runner)


def test_gpu_guard_allows_light_or_empty_and_force_skips_inspection():
    for output in ("", "123, small.exe, 511\n"):
        guard.check_gpu(
            runner=lambda *args, output=output, **kwargs: SimpleNamespace(stdout=output)
        )

    def missing(*args, **kwargs):
        raise FileNotFoundError("nvidia-smi")

    with pytest.raises(RuntimeError, match="Cannot verify GPU availability"):
        guard.check_gpu(runner=missing)
    guard.check_gpu(force=True, runner=missing)


def test_dead_server_rows_suppress_accuracy():
    document = {
        "rows": [
            {"valid": False, "error": "<urlopen error [WinError 10061] Connection refused>"}
            for _ in range(962)
        ],
        "metrics": {"accuracy": 0},
    }
    with pytest.raises(RuntimeError, match="transport errors 962/962.*accuracy suppressed"):
        round_eval.validate_result(document)


def test_transport_error_boundary_and_nested_errors():
    rows = [{"valid": True} for _ in range(100)]
    rows[0]["details"] = {"error": "Connection refused"}
    checked = round_eval.validate_result({"rows": rows, "metrics": {"accuracy": 0.5}})
    assert checked["transport_rate"] == 0.01
    assert checked["transport_errors"] == 1
    rows[1]["error"] = "HTTP Error 503: Service Unavailable"
    with pytest.raises(RuntimeError, match="transport errors 2/100"):
        round_eval.validate_result({"rows": rows})
    assert not round_eval.has_transport_error(
        {
            "prediction": "connection refused",
            "error": "wrong identity",
        }
    )


def test_json_valid_rate_boundary_and_incomplete_rows():
    rows = [{"valid": True} for _ in range(100)]
    rows[0]["valid"] = False
    checked = round_eval.validate_result({"rows": rows, "metrics": {"accuracy": 0.5}})
    assert checked["json_valid_rate"] == 0.99
    rows[1]["valid"] = False
    with pytest.raises(RuntimeError, match="JSON valid rate"):
        round_eval.validate_result({"rows": rows})
    with pytest.raises(RuntimeError, match="expected 101 rows, got 100"):
        round_eval.validate_result({"rows": rows}, expected=101)


def test_planning_expect_invalid_row_counts_as_valid_json():
    checked = round_eval.validate_result(
        {
            "rows": [{"valid": False, "expect_invalid": True, "exact": True}],
            "metrics": {"accuracy": 1},
        }
    )
    assert checked["json_valid_rate"] == 1


def test_closed_loop_transport_rate_uses_calls_and_ignores_archived_retry_history(tmp_path):
    episode_root = tmp_path / "find"
    live = episode_root / "episode-001"
    archived = episode_root / "retry-history" / "episode-001"
    live.mkdir(parents=True)
    archived.mkdir(parents=True)
    healthy = {"labels": {"onboard": {"slow_calls": [{"ok": True}]}}}
    bad = {"labels": {"onboard": {"slow_calls": [{"error": "Connection refused"}]}}}
    (live / "steps.jsonl").write_text(
        "\n".join(json.dumps(healthy if index > 1 else bad) for index in range(100)) + "\n",
        encoding="utf-8",
    )
    # This archived failure must not dilute the live call error denominator.
    (archived / "steps.jsonl").write_text(json.dumps(bad) + "\n", encoding="utf-8")
    rows = [{"valid": True, "id": index} for index in range(100)]
    with pytest.raises(RuntimeError, match="transport errors 2/100"):
        round_eval.validate_result(
            {"rows": rows, "metrics": {"accuracy": 1}}, episode_root=episode_root
        )


def test_build_stages_planning_uses_requested_model_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(round_eval, "ROOT", tmp_path)
    tasks = tmp_path / "configs/onboard/plan-eval-tasks.json"
    tasks.parent.mkdir(parents=True)
    tasks.write_text(json.dumps({"tasks": [{"task": "one"}]}), encoding="utf-8")
    args = SimpleNamespace(
        out=tmp_path / "out",
        data_root=tmp_path,
        model="v2-r13-multi",
        gguf=tmp_path / "r13.gguf",
        mmproj=tmp_path / "mmproj.gguf",
        llama_server=tmp_path / "llama-server.exe",
        stages=["planning"],
        min_free_disk_gb=30,
        disk_check_every=30,
        retry_failed_episodes=False,
        episodes=1,
        index=tmp_path / "index.json",
        onboard_config=tasks,
        suite_v1=tasks,
        suite_v2=tasks,
    )
    stages = round_eval.build_stages(args)
    assert len(stages) == 1
    command = stages[0].command
    assert str(args.gguf) in command
    assert str(args.mmproj) in command
    assert stages[0].expected == 1


def test_round_runs_sequentially_stops_at_invalid_and_saves_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(round_eval, "ROOT", tmp_path)
    out = tmp_path / "round"
    lock = tmp_path / "gpu.lock"
    stages = [
        round_eval.Stage(name, ["fake", name], out / f"{name}.json", 1)
        for name in ("offline", "flight", "planning")
    ]
    monkeypatch.setattr(round_eval, "build_stages", lambda args: stages)
    events = []
    monkeypatch.setattr(round_eval, "ensure_free_disk", lambda *args: events.append("disk"))
    monkeypatch.setattr(round_eval, "check_gpu", lambda **kwargs: events.append("gpu"))

    def runner(command, **kwargs):
        name = command[1]
        assert lock.exists()
        assert kwargs["env"]["HF_HUB_OFFLINE"] == "1"
        if name == "flight":
            saved = json.loads((out / "summary.json").read_text(encoding="utf-8"))
            assert saved["stages"][0]["status"] == "valid"
        events.append(name)
        row = {"valid": True}
        if name == "flight":
            row["error"] = "Connection refused"
        round_eval.write_json(out / f"{name}.json", {"rows": [row], "metrics": {"accuracy": 1}})
        return SimpleNamespace(returncode=0)

    assert (
        round_eval.main(
            [
                "--model",
                "fake",
                "--out",
                str(out),
                "--lock-file",
                str(lock),
                "--stages",
                "offline",
                "flight",
                "planning",
            ],
            runner=runner,
        )
        == 1
    )
    assert events == ["disk", "gpu", "disk", "gpu", "offline", "disk", "gpu", "flight"]
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "failed"
    assert [(stage["name"], stage["status"]) for stage in summary["stages"]] == [
        ("offline", "valid"),
        ("flight", "invalid"),
    ]
    assert "accuracy suppressed" in summary["error"]
    assert "comparison" not in summary["stages"][1]
    markdown = (out / "summary.md").read_text(encoding="utf-8")
    assert "Status: **failed**" in markdown
    assert "INVALID" in markdown
    assert not lock.exists()
    assert not (out / "planning.json").exists()


def test_outputs_may_live_under_a_junctioned_artifacts_tree(tmp_path, monkeypatch):
    """r19-eval: artifacts/ is a junction to a shared tree; planning output under it
    resolves outside the worktree and was refused."""
    worktree, shared = tmp_path / "worktree", tmp_path / "shared-artifacts"
    worktree.mkdir()
    shared.mkdir()
    try:
        (worktree / "artifacts").symlink_to(shared, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            pytest.skip("cannot create a directory link here")
        import _winapi  # junctions need no symlink privilege

        _winapi.CreateJunction(str(shared), str(worktree / "artifacts"))
    monkeypatch.setattr(round_eval, "ROOT", worktree)
    inside = round_eval.local_output(worktree / "artifacts" / "r19-eval-planning")
    assert inside == (shared / "r19-eval-planning").resolve()
    assert round_eval.local_output(worktree / "out") == (worktree / "out").resolve()
    with pytest.raises(ValueError, match="inside this worktree"):
        round_eval.local_output(tmp_path / "elsewhere")


def test_local_output_accepts_a_nested_junction_below_artifacts(tmp_path, monkeypatch):
    # The integration worktree has a real artifacts/ whose evaluation/ entry is a
    # junction to the shared tree: the r21 planner round failed here twice.
    if sys.platform != "win32":
        pytest.skip("directory junctions are Windows-only")
    import _winapi

    import scripts.run_model_round_eval as round_eval

    shared = tmp_path / "shared-evaluation"
    shared.mkdir()
    worktree = tmp_path / "worktree"
    (worktree / "artifacts").mkdir(parents=True)
    _winapi.CreateJunction(str(shared), str(worktree / "artifacts" / "evaluation"))
    monkeypatch.setattr(round_eval, "ROOT", worktree)
    target = worktree / "artifacts" / "evaluation" / "r21-planner-round" / "candidate"
    assert round_eval.local_output(target) == (shared / "r21-planner-round" / "candidate").resolve()
    with pytest.raises(ValueError, match="inside this worktree"):
        round_eval.local_output(tmp_path / "elsewhere")


def test_gpu_guard_uses_device_memory_when_wddm_hides_per_process_usage():
    apps = "21684, C:\\Windows\\explorer.exe, [N/A]\n22064, WindowsTerminal.exe, [N/A]\n"

    def runner_for(device_mib):
        def runner(command, **kwargs):
            if "--query-gpu=memory.used" in command:
                return SimpleNamespace(stdout=f"{device_mib}\n")
            return SimpleNamespace(stdout=apps)
        return runner

    # An idle desktop (702 MiB in the r21 round) is not a heavy consumer.
    guard.check_gpu(runner=runner_for(702))
    with pytest.raises(RuntimeError, match="Other heavy GPU processes"):
        guard.check_gpu(runner=runner_for(5200))
