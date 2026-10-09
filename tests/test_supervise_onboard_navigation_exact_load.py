"""Bounded stdlib/software children, never a VLM/load/camera experiment."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import diagnose_onboard_navigation_exact_load as frozen
from scripts import supervise_onboard_navigation_exact_load as api


def bound(path):
    return api.record(path)


def snapshot(pid=2000):
    return {
        "pid": pid,
        "observed_at_unix_s": 1234.5,
        "process_working_set_bytes": 100,
        "process_peak_working_set_bytes": 200,
        "process_private_commit_bytes": 100,
        "process_peak_commit_bytes": 200,
        "host_physical_total_bytes": 10000,
        "host_physical_available_bytes": 8000,
        "process_limited_commit_limit_bytes": 20000,
        "process_available_commit_bytes": 15000,
        "process_virtual_available_bytes": 20000,
        "continuous_sampling": False,
        "os_peak_scope": "process_lifetime",
        "GPU_memory_measured": False,
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "ROOT", tmp_path)
    (tmp_path / "artifacts/diagnostics").mkdir(parents=True)
    for name in (*api.FILES, api.ENTRY, api.NAV):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture-not-actual-code")
    monkeypatch.setattr(api, "ENTRY_SHA", bound(tmp_path / api.ENTRY)["sha256"])
    nav = bound(tmp_path / api.NAV)
    own = [bound(tmp_path / name) for name in api.FILES]
    source = [bound(tmp_path / api.ENTRY)]
    inventory = [{"binding": row, "verification": "current_byte_sha256"} for row in source]
    plan = {
        "output_directory": str(
            tmp_path / "artifacts/diagnostics/onboard-navigation-exact-load-toy"
        ),
        "memory_preflight": {
            "min_physical_available_bytes": 1000,
            "min_process_available_commit_bytes": 2000,
            "units": "bytes",
            "thresholds_are_operator_policy_not_proven_model_capacity": True,
        },
        "input_inventory": inventory,
        "source_files": source,
        "load_config": {
            "model": {"expected_parameter_sha256": {"base": "a" * 64, "adapter": "b" * 64}}
        },
    }
    path = tmp_path / "plan.json"
    path.write_bytes(api.canonical(plan))
    binding = bound(path)

    def read_bound(row):
        if bound(row["path"]) != row:
            raise ValueError("fixture source drift")
        return Path(row["path"]).read_bytes()

    loader = SimpleNamespace(_all_files=lambda config: source)
    diagnostic = SimpleNamespace(
        CONTRACT=frozen.CONTRACT,
        CLAIMS=dict(frozen.CLAIMS),
        _json=frozen._json,
        _read_bound=read_bound,
        _bootstrap=lambda: (loader, None),
        inventory=lambda rows: [
            {"binding": bound(row["path"]), "verification": "current_byte_sha256"} for row in rows
        ],
    )
    monkeypatch.setattr(api, "admit", lambda p, d: (diagnostic, plan, binding, nav, own))
    result = SimpleNamespace(
        root=tmp_path,
        api=diagnostic,
        plan=plan,
        binding=binding,
        nav=nav,
        own=own,
        source=source,
        inventory=inventory,
        output=tmp_path / "artifacts/diagnostics/onboard-navigation-exact-load-toy-supervisor-r1",
        calls=[],
        mutate_preflight=None,
        mutate_load=None,
        late=None,
    )

    def child(command, output, label, deadline):
        result.calls.append((label, command, deadline))
        row = closed_row(output, label)
        if label == "preflight":
            raw = preflight(result)
            if result.mutate_preflight:
                result.mutate_preflight(raw, row)
        else:
            directory = Path(plan["output_directory"])
            directory.mkdir()
            payloads = [
                api.write_new(directory / name, b"fixture")
                for name in ("phases.jsonl", "native-fault.log")
            ]
            report = {
                "contract": frozen.CONTRACT,
                "plan": binding,
                "pid": row["pid"],
                "success": True,
                "exact_load_context_closed": True,
                "sources_unchanged": True,
                "error": None,
                "source_before": inventory,
                "source_after": inventory,
                "claims": dict(frozen.CLAIMS),
                "load_observations": {
                    "load_succeeded": True,
                    "closed_successfully": True,
                    "source_bindings_unchanged": True,
                    "parameter_identity_before": plan["load_config"]["model"][
                        "expected_parameter_sha256"
                    ],
                    "parameter_identity_after": plan["load_config"]["model"][
                        "expected_parameter_sha256"
                    ],
                },
                "diagnostic_outputs": payloads,
            }
            report.update(
                {
                    key: 0
                    for key in (
                        "camera_calls",
                        "fused_forward_calls",
                        "head_forward_calls",
                        "generation_calls",
                        "optimizer_attempts",
                        "flight_commands",
                    )
                }
            )
            if result.mutate_load:
                result.mutate_load(report, row)
            api.write_new(directory / "report.json", api.canonical(report))
            raw = {"success": True, "report": plan["output_directory"]}
        Path(row["captures"][0]["path"]).write_bytes(
            b"__OWNED_WORKER__fixture\n" + api.canonical(raw)
        )
        row["captures"][0] = bound(row["captures"][0]["path"])
        if result.late:
            result.late(label)
        return row

    monkeypatch.setattr(api, "run_child", child)
    return result


def closed_row(output, label):
    captures = [api.write_new(output / f"{label}-{name}.log", b"") for name in ("stdout", "stderr")]
    return {
        "phase": label,
        "command": ["fixture"],
        "pid": 2000,
        "launcher_pid": 2000,
        "worker_pid": 2000,
        "worker_admitted_before_release": True,
        "launcher_started_suspended": True,
        "worker_provenance": {"job_membership_verified": True},
        "native_exit_code": 0,
        "watchdog_fired": False,
        "capture_overflow": False,
        "owned_tree_empty": True,
        "ownership_assigned_before_release": True,
        "capture_threads_closed": True,
        "captures": captures,
    }


def preflight(env):
    return {
        "contract": frozen.CONTRACT,
        "plan": env.binding,
        "preflight_only": True,
        "passed": True,
        "memory_snapshot": snapshot(),
        "memory_preflight": env.plan["memory_preflight"],
        "error": None,
        "model_load_attempts": 0,
        "faulthandler_enabled_by_this_call": False,
        "execute_output_created": False,
        "claims": dict(frozen.CLAIMS),
    }


def run(env, *, execute=False):
    return api.supervise(env.binding["path"], env.binding["sha256"], env.output, execute=execute)


def test_default_is_preflight_only_and_durable_not_load_success(env):
    result = run(env)
    assert result["success"] is True and result["preflight_passed"] is True
    assert result["model_load_attempts"] == 0 and result["model_load_succeeded"] is False
    assert [call[0] for call in env.calls] == ["preflight"]
    assert not Path(env.plan["output_directory"]).exists()
    stored = json.loads((env.output / "supervisor.json").read_bytes())
    assert stored["model_load_succeeded"] is False and stored["sources_unchanged"] is True
    assert not any(stored["claims"].values())


def test_explicit_execution_fresh_children_shared_deadline_and_exact_report(env):
    result = run(env, execute=True)
    assert result["success"] and result["model_load_succeeded"]
    assert result["model_load_attempts"] == 1
    assert [call[0] for call in env.calls] == ["preflight", "load"]
    assert env.calls[0][2] == env.calls[1][2]
    assert env.calls[0][1][0] == env.nav["path"]
    assert env.calls[0][1][-1] == "--preflight-only" and env.calls[1][1][-1] == "--execute"
    assert api.BOOTSTRAP in env.calls[0][1]
    assert result["diagnostic_report"] == bound(Path(env.plan["output_directory"]) / "report.json")


@pytest.mark.parametrize(
    "case",
    [
        "refused",
        "missing",
        "bool_attempt",
        "wrong_plan",
        "wrong_pid",
        "bad_memory",
        "extra",
        "created",
        "native",
        "overflow",
        "alive",
        "clock",
        "deadline",
        "worker_not_admitted",
        "job_membership",
    ],
)
def test_bad_preflight_cannot_launch_load(env, case):
    def mutate(raw, row):
        if case == "refused":
            raw["passed"] = False
        elif case == "missing":
            del raw["memory_snapshot"]
        elif case == "bool_attempt":
            raw["model_load_attempts"] = False
        elif case == "wrong_plan":
            raw["plan"] = dict(env.binding, sha256="0" * 64)
        elif case == "wrong_pid":
            raw["memory_snapshot"]["pid"] += 1
        elif case == "bad_memory":
            raw["memory_snapshot"]["host_physical_available_bytes"] = 1
        elif case == "extra":
            raw["not_declared"] = True
        elif case == "created":
            Path(env.plan["output_directory"]).mkdir()
        elif case == "native":
            row["native_exit_code"] = 1
        elif case == "overflow":
            row["capture_overflow"] = True
        elif case == "alive":
            row["owned_tree_empty"] = False
        elif case == "deadline":
            row["deadline_exhausted"] = True
        elif case == "worker_not_admitted":
            row["worker_admitted_before_release"] = False
        elif case == "job_membership":
            row["worker_provenance"]["job_membership_verified"] = False
        else:
            row["watchdog_fired"] = True

    env.mutate_preflight = mutate
    result = run(env, execute=True)
    assert result["success"] is False and result["model_load_attempts"] == 0
    assert [call[0] for call in env.calls] == ["preflight"]
    assert (env.output / "supervisor.json").exists()


@pytest.mark.parametrize(
    "case",
    [
        "false_success",
        "cleanup",
        "identity",
        "after_identity",
        "source",
        "pid",
        "optimizer",
        "claims",
        "native",
        "survivor",
    ],
)
def test_bad_load_report_or_native_cannot_be_success(env, case):
    def mutate(report, row):
        if case == "false_success":
            report["success"] = False
        elif case == "cleanup":
            report["load_observations"]["closed_successfully"] = False
        elif case == "identity":
            report["load_observations"]["parameter_identity_before"] = {}
        elif case == "after_identity":
            report["load_observations"]["parameter_identity_after"] = {}
        elif case == "source":
            report["source_after"] = []
        elif case == "pid":
            report["pid"] = 999
        elif case == "optimizer":
            report["optimizer_attempts"] = True
        elif case == "claims":
            report["claims"]["quality_claim"] = True
        elif case == "native":
            row["native_exit_code"] = 3221225477
        else:
            row["descendants_survived_native_exit"] = True

    env.mutate_load = mutate
    result = run(env, execute=True)
    assert result["success"] is False and result["model_load_attempts"] == 1
    assert result["model_load_succeeded"] is False


@pytest.mark.parametrize("where", ["entry", "own", "nav", "plan"])
def test_late_real_small_source_drift_is_failure(env, where):
    paths = {
        "entry": env.root / api.ENTRY,
        "own": env.root / api.FILES[0],
        "nav": env.root / api.NAV,
        "plan": Path(env.binding["path"]),
    }
    env.late = lambda label: paths[where].write_bytes(b"drift")
    result = run(env, execute=True)
    assert result["success"] is False and result["sources_unchanged"] is False
    assert result["model_load_attempts"] == 0


def test_create_only_supervisor_does_not_overwrite(env):
    env.output.mkdir()
    marker = env.output / "original.txt"
    marker.write_bytes(b"keep")
    with pytest.raises(ValueError, match="Fresh"):
        run(env)
    assert marker.read_bytes() == b"keep" and not env.calls


def test_mode_conflict_and_default_cli(env):
    args = [
        "--plan",
        env.binding["path"],
        "--plan-sha256",
        env.binding["sha256"],
        "--output-dir",
        str(env.output),
    ]
    with pytest.raises(SystemExit):
        api.main(args + ["--preflight-only", "--execute"])
    assert not env.calls
    assert api.main(args) == 0
    assert [call[0] for call in env.calls] == ["preflight"]


def test_frozen_entry_drift_rejected_before_import(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "ROOT", tmp_path)
    path = tmp_path / api.ENTRY
    path.parent.mkdir()
    path.write_bytes(b"raise RuntimeError('must never import')")
    with pytest.raises(ValueError, match="entry changed"):
        api.diagnostic_api()


def test_stdlib_quiet_fresh_import():
    code = (
        "import importlib.util,sys;"
        f"s=importlib.util.spec_from_file_location('sup',{api.__file__!r});"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
        "assert not any(n in sys.modules for n in ('torch','numpy','PIL','transformers','peft'))"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code], capture_output=True, timeout=10
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows Job test")
@pytest.mark.parametrize(
    "mode", ["normal", "timeout", "overflow", "assignment", "descendant", "close"]
)
def test_actual_tiny_owned_child_never_runs_model(tmp_path, monkeypatch, mode):
    # This is a real tiny Python subprocess, not the frozen diagnostic or Nav/model.
    entry = tmp_path / "toy.py"
    entry.write_text(
        "import time\nprint('toy CPU only',flush=True)\n"
        + (
            "time.sleep(30)\n"
            if mode == "timeout"
            else "print('x'*100000)\n"
            if mode == "overflow"
            else (
                "import subprocess,sys\n"
                "subprocess.Popen([sys.executable,'-I','-B','-S','-c',"
                "'import time;time.sleep(30)'])\n"
            )
            if mode == "descendant"
            else ""
        ),
        encoding="utf-8",
    )
    if mode == "overflow":
        monkeypatch.setattr(api, "CAPTURE_CAP", 1024)
    command = [sys.executable, "-I", "-B", "-S", "-c", api.BOOTSTRAP, str(entry)]
    owner = api.WindowsJob()
    original_assign = owner.assign
    if mode == "assignment":

        def failed_assign(process):
            raise OSError("ownership fixture refused")

        owner.assign = failed_assign
    else:
        owner.assign = original_assign
    if mode == "close":
        close = owner.close

        def close_then_report_error():
            close()
            raise OSError("fixture post-close observer failed")

        owner.close = close_then_report_error
    row = api.run_child(
        command,
        tmp_path,
        mode,
        time.monotonic() + (0.2 if mode == "timeout" else 10),
        owner_factory=lambda: owner,
    )
    assert row["owned_tree_empty"] is True and row["capture_threads_closed"] is True
    if mode == "normal":
        assert api.child_complete(row) and Path(row["captures"][0]["path"]).read_bytes().endswith(
            b"toy CPU only\r\n"
        )
    else:
        assert not api.child_complete(row)
    if mode == "timeout":
        assert row["watchdog_fired"]
    if mode == "overflow":
        assert row["capture_overflow"] and row["captures"][0]["bytes"] <= 1024
    if mode == "assignment":
        assert not row["ownership_assigned_before_release"]
        assert row["captures"][0]["bytes"] == 0
    if mode == "descendant":
        assert row["native_exit_code"] == 0 and row["descendants_survived_native_exit"]
    if mode == "close":
        assert row["native_exit_code"] == 0 and row["job_close_error"]["type"] == "OSError"


def test_actual_binding_and_short_write_hash_contract(tmp_path):
    path = tmp_path / "evidence.json"
    row = api.write_new(path, b"actual bytes")
    assert row["sha256"] == hashlib.sha256(b"actual bytes").hexdigest()
    with pytest.raises(FileExistsError):
        api.write_new(path, b"replacement")


@pytest.mark.parametrize("case", ["path", "declaration", "sha"])
def test_fixed_navigation_interpreter_gate_before_any_spawn(tmp_path, monkeypatch, case):
    monkeypatch.setattr(api, "ROOT", tmp_path)
    path = tmp_path / api.NAV
    path.parent.mkdir(parents=True)
    path.write_bytes(b"fixture executable, never executed")
    actual = bound(path)
    runtime = {"python_executable": str(path), "python_binding": actual}
    if case == "path":
        other = tmp_path / "not_navigation.exe"
        other.write_bytes(b"wrong")
        runtime["python_executable"] = str(other)
    elif case == "declaration":
        runtime["python_binding"] = dict(actual, sha256="0" * 64)
    monkeypatch.setattr(api, "NAV_SHA", actual["sha256"] if case != "sha" else "0" * 64)
    diagnostic = SimpleNamespace(
        admit_plan=lambda p, d: ({"load_config": {"runtime": runtime}}, {})
    )
    monkeypatch.setattr(api, "diagnostic_api", lambda: diagnostic)
    with pytest.raises(ValueError, match="Navigation interpreter"):
        api.admit("fixture plan", "a" * 64)


def test_global_deadline_not_reset_after_preflight_or_final_hashing(env, monkeypatch):
    values = iter((1.0, 602.0, 602.0))
    monkeypatch.setattr(api.time, "monotonic", lambda: next(values))
    result = run(env)
    assert result["success"] is False and result["preflight_passed"] is True
    assert result["model_load_attempts"] == 0 and result["error"]["type"] == "TimeoutError"


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows venv launcher identity")
@pytest.mark.parametrize("delay_assignment", [False, True])
def test_actual_nav_venv_worker_pid_and_atomic_suspended_start(tmp_path, delay_assignment):
    # Same pinned interpreter, but only a tiny stdlib toy/preflight schema.
    # Never calls the real diagnostic, loader, model, memory-policy plan or GPU.
    nav = (api.ROOT / api.NAV).resolve(strict=True)
    assert bound(nav)["sha256"] == api.NAV_SHA
    plan_path = tmp_path / "toy-plan.json"
    plan_path.write_bytes(b'{"fixture":true}')
    plan_binding = bound(plan_path)
    plan = {
        "output_directory": str(tmp_path / "not-created"),
        "memory_preflight": {
            "min_physical_available_bytes": 1000,
            "min_process_available_commit_bytes": 2000,
            "units": "bytes",
            "thresholds_are_operator_policy_not_proven_model_capacity": True,
        },
    }
    raw = {
        "contract": frozen.CONTRACT,
        "plan": plan_binding,
        "preflight_only": True,
        "passed": True,
        "memory_snapshot": snapshot(),
        "memory_preflight": plan["memory_preflight"],
        "error": None,
        "model_load_attempts": 0,
        "faulthandler_enabled_by_this_call": False,
        "execute_output_created": False,
        "claims": dict(frozen.CLAIMS),
    }
    entry = tmp_path / "stdlib-only.py"
    entry.write_text(
        "import json,os,sys\n"
        "assert not any(n in sys.modules for n in ('torch','numpy','PIL','peft','transformers'))\n"
        + f"value=json.loads({json.dumps(raw)!r})\n"
        + "value['memory_snapshot']['pid']=os.getpid()\nprint(json.dumps(value),flush=True)\n",
        encoding="utf-8",
    )
    command = [str(nav), "-I", "-B", "-S", "-c", api.BOOTSTRAP, str(entry)]
    owner = api.WindowsJob()
    original_assign = owner.assign
    if delay_assignment:

        def assign_after_suspended_launcher_waits(process):
            time.sleep(0.3)
            original_assign(process)

        owner.assign = assign_after_suspended_launcher_waits
    row = api.run_child(
        command, tmp_path, "nav", time.monotonic() + 10, owner_factory=lambda: owner
    )
    assert api.child_complete(row), json.dumps(row, sort_keys=True)
    assert row["worker_pid"] != row["launcher_pid"] == row["pid"]
    assert row["worker_provenance"]["actual_parent_pid"] == row["launcher_pid"]
    assert row["worker_provenance"]["job_membership_verified"] is True
    assert row["launcher_started_suspended"] is True
    assert row["worker_provenance"]["worker_explicitly_assigned"] is False
    result = api.verify_preflight(frozen, row, plan, plan_binding)
    assert result["memory_snapshot"]["pid"] == row["worker_pid"]
    assert not Path(plan["output_directory"]).exists()


@pytest.mark.parametrize("data", [b'{"passed":true', b'{"x":1,"x":2}', b'{"x":NaN}'])
def test_incomplete_duplicate_or_nonfinite_stdout_not_admitted(env, data):
    env.output.mkdir()
    row = closed_row(env.output, "preflight")
    path = Path(row["captures"][0]["path"])
    path.write_bytes(b"__OWNED_WORKER__fixture\n" + data)
    row["captures"][0] = bound(path)
    with pytest.raises(ValueError):
        api.verify_preflight(env.api, row, env.plan, env.binding)
    assert not env.calls and not Path(env.plan["output_directory"]).exists()
