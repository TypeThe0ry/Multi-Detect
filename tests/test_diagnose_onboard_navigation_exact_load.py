"""Tiny stdlib fixtures, not an actual model-load or native-crash experiment."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import diagnose_onboard_navigation_exact_load as api


def snapshot():
    return {
        "pid": os.getpid(),
        "observed_at_unix_s": 1234.5,
        "source": "fixture_only_not_Windows_observation",
        "process_working_set_bytes": 100,
        "process_peak_working_set_bytes": 200,
        "process_private_commit_bytes": 150,
        "process_peak_commit_bytes": 250,
        "host_physical_total_bytes": 10000,
        "host_physical_available_bytes": 8000,
        "process_limited_commit_limit_bytes": 20000,
        "process_available_commit_bytes": 15000,
        "process_virtual_available_bytes": 100000,
        "continuous_sampling": False,
        "os_peak_scope": "process_lifetime",
        "GPU_memory_measured": False,
    }


class FakeFaultHandler:
    def __init__(self):
        self.enabled = False
        self.calls = []

    def is_enabled(self):
        return self.enabled

    def enable(self, *, file, all_threads):
        assert not file.closed and all_threads is True
        self.enabled = True
        self.calls.append("enabled")

    def disable(self):
        self.enabled = False
        self.calls.append("disabled")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "ROOT", tmp_path)
    (tmp_path / "configs/training").mkdir(parents=True)
    (tmp_path / "artifacts/diagnostics").mkdir(parents=True)
    inputs = tmp_path / "input.bin"
    inputs.write_bytes(b"bound_fixture_input")
    config = {"model": {"expected_parameter_sha256": {"base": "a" * 64, "adapter": "b" * 64}}}
    r3 = tmp_path / api.R3_PLAN
    r3.write_bytes(api.canonical({"load_config": config}))
    for relative in api.NEW_FILES:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture_not_real_source")
    pin = api.small_record(r3)
    monkeypatch.setattr(api, "_pins", lambda: [pin])
    source_rows = [api.small_record(inputs)]
    calls = []
    state = {"error": None, "wrong_identity": False, "late_drift": False}
    fault = FakeFaultHandler()

    @contextmanager
    def load_preloaded(actual_config, *, observations):
        assert actual_config == config and fault.enabled
        calls.append("entered")
        observations.update(
            model_loaded=True,
            load_succeeded=True,
            closed_successfully=False,
            source_bindings_unchanged=False,
            parameter_identity_before=actual_config["model"]["expected_parameter_sha256"],
        )
        if state["wrong_identity"]:
            observations["parameter_identity_before"] = {"base": "0" * 64, "adapter": "b" * 64}
        try:
            yield SimpleNamespace(observations=observations)
        finally:
            calls.append("exited")
            if state["error"] is not None:
                raise state["error"]
            if state["late_drift"]:
                inputs.write_bytes(b"same_size_but_drift__")
            observations.update(closed_successfully=True, source_bindings_unchanged=True)

    loader = SimpleNamespace(
        validate_load_config=lambda value: value,
        _all_files=lambda value: source_rows,
        load_preloaded=load_preloaded,
    )
    monkeypatch.setattr(api, "_bootstrap", lambda: (loader, SimpleNamespace(snapshot=snapshot)))
    monkeypatch.setattr(api, "faulthandler", fault)
    monkeypatch.setattr(api, "_no_preloaded_optional", lambda: None)
    return SimpleNamespace(
        root=tmp_path,
        config=config,
        loader=loader,
        source_rows=source_rows,
        pin=pin,
        state=state,
        calls=calls,
        fault=fault,
        inputs=inputs,
        output=tmp_path / "artifacts/diagnostics/onboard-navigation-exact-load-fixture",
    )


def plan_for(env):
    return api.build_plan(
        env.output, min_physical_available_bytes=1000, min_process_available_commit_bytes=2000
    )


def binding(plan):
    path = api.ROOT / "configs/training/onboard-navigation-exact-load-argument.json"
    if not path.exists():
        api._write_new(path, api.canonical(plan), api.MAX_PLAN)
    return api.small_record(path)


def test_stdlib_import_has_no_optional_side_effects():
    code = (
        "import sys; from scripts import diagnose_onboard_navigation_exact_load as a; "
        "assert not any(n.split('.')[0] in a.FORBIDDEN_PRELOAD for n in sys.modules); "
        "print(a.CONTRACT)"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=Path(api.__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == api.CONTRACT


def test_emit_admit_without_execute_never_calls_loader(setup):
    plan = plan_for(setup)
    path = setup.root / "configs/training/onboard-navigation-exact-load-fixture.json"
    row = api.emit_plan(path, plan)
    assert api.main(["--plan", str(path), "--plan-sha256", row["sha256"]]) == 0
    assert setup.calls == [] and not setup.output.exists() and setup.fault.calls == []
    assert json.loads(path.read_bytes())["load_config"] == setup.config
    with pytest.raises(FileExistsError):
        api.emit_plan(path, plan)


def test_execute_requires_explicit_bound_plan(setup):
    assert api.main(["--execute", "--output-dir", str(setup.output)]) == 1
    assert not setup.output.exists() and setup.calls == []


@pytest.mark.parametrize("enough", [True, False])
def test_readonly_preflight_observed_snapshot_no_output_or_model(setup, monkeypatch, enough):
    plan = plan_for(setup)
    current = snapshot()
    if not enough:
        current["host_physical_available_bytes"] = 1
    monkeypatch.setattr(
        api, "_bootstrap", lambda: (setup.loader, SimpleNamespace(snapshot=lambda: current))
    )
    row = binding(plan)
    result = api.readonly_preflight(plan, row)
    assert result["passed"] is enough and result["memory_snapshot"] == current
    assert result["model_load_attempts"] == 0 and setup.calls == []
    assert not setup.output.exists() and setup.fault.calls == []
    assert api.main(
        ["--plan", row["path"], "--plan-sha256", row["sha256"], "--preflight-only"]
    ) == (0 if enough else 1)


def test_execute_and_preflight_are_mutually_exclusive(setup):
    with pytest.raises(SystemExit):
        api.main(["--execute", "--preflight-only"])
    assert setup.calls == []


def test_fake_plan_binding_cannot_start_context(setup):
    plan = plan_for(setup)
    row = binding(plan)
    row["sha256"] = "f" * 64
    with pytest.raises(ValueError, match="drift"):
        api.execute_plan(plan, row)
    assert not setup.output.exists() and setup.calls == []


def test_success_fixture_full_context_and_claims(setup):
    plan = plan_for(setup)
    result = api.execute_plan(plan, binding(plan))
    assert result["success"] is True and result["exact_load_context_closed"] is True
    assert result["sources_unchanged"] is True
    assert setup.calls == ["entered", "exited"] and setup.fault.calls == ["enabled", "disabled"]
    assert not any(result["claims"].values())
    assert not any(result["scope"].values())
    assert all(
        result[key] == 0
        for key in (
            "camera_calls",
            "fused_forward_calls",
            "head_forward_calls",
            "generation_calls",
            "optimizer_attempts",
            "flight_commands",
        )
    )
    rows = [json.loads(line) for line in (setup.output / "phases.jsonl").read_bytes().splitlines()]
    assert len(rows) == result["phase_count"] == 12
    assert [row["sequence"] for row in rows] == list(range(1, 13))
    assert all(row["pid"] == os.getpid() for row in rows)
    assert set(row["phase"] for row in rows) == {
        "memory_preflight",
        "source_before",
        "load_context_enter",
        "loaded_context_body",
        "load_context_exit",
        "source_after",
    }
    saved = json.loads((setup.output / "report.json").read_bytes())
    assert saved == result and (setup.output / "native-fault.log").stat().st_size == 0


@pytest.mark.parametrize(
    "counter", ["host_physical_available_bytes", "process_available_commit_bytes"]
)
def test_threshold_refusal_is_failure_not_oom_cause(setup, monkeypatch, counter):
    original = snapshot()
    original[counter] = 0
    monkeypatch.setattr(
        api, "_bootstrap", lambda: (setup.loader, SimpleNamespace(snapshot=lambda: original))
    )
    plan = plan_for(setup)
    with pytest.raises(ValueError, match="Memory policy refuses"):
        api.execute_plan(plan, binding(plan))
    saved = json.loads((setup.output / "report.json").read_bytes())
    assert saved["success"] is False and setup.calls == []
    assert saved["scope"]["memory_pressure_proves_crash_cause"] is False
    assert setup.fault.enabled is False


def test_windows_api_failure_does_not_default_to_zero_or_pass(setup, monkeypatch):
    def broken():
        raise OSError("Windows API unavailable")

    monkeypatch.setattr(api, "_bootstrap", lambda: (setup.loader, SimpleNamespace(snapshot=broken)))
    plan = plan_for(setup)
    with pytest.raises(OSError, match="Windows API"):
        api.execute_plan(plan, binding(plan))
    saved = json.loads((setup.output / "report.json").read_bytes())
    assert not saved["success"] and setup.calls == []


@pytest.mark.parametrize("case", ["wrong_identity", "cleanup", "late_drift"])
def test_late_gates_cannot_publish_success(setup, case):
    if case == "cleanup":
        setup.state["error"] = RuntimeError("real cleanup failed")
    else:
        setup.state[case] = True
    plan = plan_for(setup)
    with pytest.raises((ValueError, RuntimeError)):
        api.execute_plan(plan, binding(plan))
    saved = json.loads((setup.output / "report.json").read_bytes())
    assert saved["success"] is False and saved["load_observations"]["model_loaded"] is True
    assert setup.calls == ["entered", "exited"] and not setup.fault.enabled


class BoundaryLog:
    def __init__(self, failure=None):
        self.failure = failure
        self.events = []

    def event(self, phase, status, **kwargs):
        self.events.append((phase, status))
        if self.failure == (phase, status):
            raise MemoryError("observer failed")


@pytest.mark.parametrize(
    "failure",
    [
        ("load_context_enter", "returned"),
        ("load_context_exit", "started"),
        ("load_context_exit", "returned"),
    ],
)
def test_observer_failure_after_enter_never_skips_real_exit(setup, failure):
    setup.fault.enabled = True
    with pytest.raises(MemoryError, match="observer failed"):
        with api.observed_load_context(setup.loader, setup.config, {}, BoundaryLog(failure)):
            pass
    assert setup.calls == ["entered", "exited"]


@pytest.mark.parametrize("body_kind", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_original_body_exception_priority_and_cleanup(setup, body_kind):
    setup.fault.enabled = True
    setup.state["error"] = ValueError("source cleanup failed")
    original = body_kind("body first")
    with pytest.raises(body_kind) as caught:
        with api.observed_load_context(
            setup.loader, setup.config, {}, BoundaryLog(("load_context_exit", "started"))
        ):
            raise original
    assert caught.value is original
    assert setup.calls == ["entered", "exited"]
    assert any("source cleanup failed" in note for note in original.__notes__)


def test_enter_failure_records_failed_boundary_no_false_return():
    calls = []

    @contextmanager
    def failed(config, *, observations):
        calls.append("enter attempted")
        raise RuntimeError("native phase stand-in")
        yield  # pragma: no cover

    log = BoundaryLog()
    with pytest.raises(RuntimeError):
        with api.observed_load_context(SimpleNamespace(load_preloaded=failed), {}, {}, log):
            pytest.fail("unreachable")
    assert log.events == [("load_context_enter", "started"), ("load_context_enter", "failed")]
    assert calls == ["enter attempted"]


@pytest.mark.parametrize("value", [None, True, 0, -1, 1.0, 2**63])
def test_threshold_types_strict(setup, value):
    with pytest.raises(ValueError):
        api.build_plan(
            setup.output, min_physical_available_bytes=value, min_process_available_commit_bytes=100
        )


@pytest.mark.parametrize(
    "key,value",
    [
        ("process_private_commit_bytes", True),
        ("host_physical_available_bytes", -1),
        ("observed_at_unix_s", float("nan")),
        ("continuous_sampling", True),
        ("os_peak_scope", "this_phase"),
        ("GPU_memory_measured", True),
        ("process_available_commit_bytes", 999999),
    ],
)
def test_snapshot_scope_and_invalid_counters_fail(key, value):
    row = snapshot()
    row[key] = value
    with pytest.raises(ValueError):
        api.validate_snapshot(row)


def test_missing_snapshot_counter_fail():
    row = snapshot()
    del row["process_private_commit_bytes"]
    with pytest.raises(ValueError, match="Missing/invalid"):
        api.validate_snapshot(row)


def test_config_sources_plan_sha_and_path_fail_closed(setup):
    plan = plan_for(setup)
    plan["load_config"]["model"]["expected_parameter_sha256"]["adapter"] = "0" * 64
    with pytest.raises(ValueError, match="configuration changed"):
        api.validate_plan(plan)
    assert not setup.output.exists()
    with pytest.raises(ValueError, match="direct-child"):
        api.output_path(setup.root / "artifacts/diagnostics/../escape")
    path = setup.root / "configs/training/onboard-navigation-exact-load-nope.json"
    path.write_bytes(b"{}")
    with pytest.raises(ValueError, match="Explicit plan SHA"):
        api.admit_plan(path, "0" * 64)


def test_output_reuse_refused_and_no_false_load(setup):
    plan = plan_for(setup)
    setup.output.mkdir()
    with pytest.raises(ValueError, match="already exists"):
        api.execute_plan(plan, binding(plan))
    assert setup.calls == []


def test_source_small_same_size_drift_refused_before_output(setup):
    plan = plan_for(setup)
    setup.inputs.write_bytes(b"X" * setup.inputs.stat().st_size)
    with pytest.raises(ValueError, match="SHA changed"):
        api.execute_plan(plan, binding(plan))
    assert not setup.output.exists() and setup.calls == []


def test_inventory_large_only_stat_and_external_exact_binding(setup, monkeypatch, tmp_path):
    large = tmp_path / "large.bin"
    large.write_bytes(b"abcdefgh")
    monkeypatch.setattr(api, "MAX_SMALL", 4)
    declared = {"path": large.as_posix(), "bytes": 8, "sha256": "f" * 64}
    rows = api.inventory([declared])
    assert rows[0]["verification"] == "current_stat_only_not_full_sha"
    assert rows[0]["binding"]["sha256"] == "f" * 64
    with pytest.raises(ValueError, match="Conflicting"):
        api.inventory([declared, dict(declared, sha256="0" * 64)])
    assert api.inventory([declared, declared]) == rows


def test_phase_log_limits_and_no_fake_internal_visibility(setup):
    path = setup.root / "events.jsonl"
    with path.open("xb") as stream:
        log = api.PhaseLog(stream, "d" * 64, snapshot, clock=lambda: 1.0)
        for _ in range(api.LIMITS["max_events"]):
            log.event("source_before", "started")
        before = path.stat().st_size
        with pytest.raises(ValueError, match="budget exhausted"):
            log.event("source_before", "started")
        assert path.stat().st_size == before
        with pytest.raises(ValueError, match="externally visible"):
            log.event("from_pretrained_returned", "returned")


def test_phase_clock_regression_refused(setup):
    values = iter([2.0, 1.0])
    with (setup.root / "events.jsonl").open("xb") as stream:
        log = api.PhaseLog(stream, "d" * 64, snapshot, clock=lambda: next(values))
        log.event("source_before", "started")
        with pytest.raises(ValueError, match="regressed"):
            log.event("source_before", "returned")


def test_durable_marker_sink_failure_prevents_loader(setup, monkeypatch):
    plan = plan_for(setup)
    original = api.PhaseLog.event

    def fail(log, phase, status, **kwargs):
        if phase == "load_context_enter" and status == "started":
            raise OSError("fsync failed")
        return original(log, phase, status, **kwargs)

    monkeypatch.setattr(api.PhaseLog, "event", fail)
    with pytest.raises(OSError, match="fsync failed"):
        api.execute_plan(plan, binding(plan))
    assert setup.calls == [] and not setup.fault.enabled
    assert not json.loads((setup.output / "report.json").read_bytes())["success"]


def test_json_strict_duplicate_and_nonfinite():
    for data in (b'{"x":1,"x":2}', b'{"x":NaN}'):
        with pytest.raises(ValueError):
            api._json(data)


def test_declared_input_byte_hash_not_model_parameter_hash(setup):
    row = setup.source_rows[0]
    assert api.inventory([row])[0]["verification"] == "current_byte_sha256"
    assert row["sha256"] == hashlib.sha256(setup.inputs.read_bytes()).hexdigest()
    assert row["sha256"] != setup.config["model"]["expected_parameter_sha256"]["base"]
