"""Injected lifecycle and typed-request fixtures; no Docker/ML/Gazebo execution."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_onboard_navigation_inference import _request

from multidetect.clock import HighResolutionSystemClock
from scripts import run_gazebo_navigation_input_capture as capture
from scripts.onboard_navigation_gazebo_shadow_input import PreparedGazeboNavigationRequest


class BufferStandIn:
    def __init__(self, prompt, **kwargs):
        self.prompt, self.kwargs, self.offers = prompt, kwargs, []
        self.clock = kwargs["clock"]
        self.clock_object = self.clock.__self__
        assert self.clock_object.identity() == kwargs["host_clock_identity"]

    def offer(self, frame, provenance):
        self.offers.append((frame, provenance))

    def build_request(self, identity, *, generation):
        receipt = json.dumps(
            {"fixture_source_only": True, "generation": generation}, sort_keys=True
        )
        request = _request(
            generation=generation,
            source_binding_sha256=hashlib.sha256(receipt.encode()).hexdigest(),
            model_sha256=identity.model_sha256,
            adapter_sha256=identity.adapter_sha256,
            adapter_config_sha256=identity.adapter_config_sha256,
        )
        now = self.clock()
        request = replace(
            request,
            source_frames=tuple(replace(f, captured_at_s=now - 1) for f in request.source_frames),
            submitted_at_s=now,
            deadline_s=now + 5,
        )
        return PreparedGazeboNavigationRequest(request, receipt)


class CaptureStandIn:
    def __init__(self):
        self.calls, self.helper_calls, self.cleanup = [], [], []
        self.mutate = None
        self.failure = None
        self.helper_failure = None
        self.evidence_mode = "normal"
        self.suppress_callback_error = False
        self.extra_frame = False
        self.gate = True
        self.return_hook = None
        self._capture_gazebo_sensor_transport = self.original_helper
        self.original = self._capture_gazebo_sensor_transport

    def original_helper(self, *args, **kwargs):
        self.helper_calls.append((args, kwargs))
        assert type(kwargs["clock"]) is HighResolutionSystemClock
        if self.helper_failure:
            raise self.helper_failure
        for index in range(kwargs["frame_count"] + int(self.extra_frame)):
            kwargs["frame_callback"](
                SimpleNamespace(index=index), SimpleNamespace(index=index), object()
            )
        return {"qualified": True}

    def run_acceptance(self, source, evidence, **kwargs):
        self.calls.append((source, evidence, kwargs))
        assert kwargs["container_name"] == capture.CONTAINER_NAME
        assert kwargs["sensor_capture_concurrent"] is False
        assert kwargs["sensor_capture_start_event"] is None
        assert kwargs["mission_transaction_runner"] is None
        assert kwargs["sensor_width"] == 1280 and kwargs["sensor_height"] == 960
        reply = {
            "gate_passed": self.gate,
            "execution_requested": kwargs["execute"],
            "safety": {"flight_control_output_enabled": False, "flight_control_commands_sent": 0},
            "mission_transaction": {"requested": False},
            "sensor_capture": {"concurrent": False},
            "failure": None,
            "fixture_owned_cleanup": True,
        }
        try:
            if self.failure:
                raise self.failure
            if kwargs["execute"]:
                self._capture_gazebo_sensor_transport(
                    plan="fixture-plan",
                    frame_count=kwargs["sensor_frame_count"],
                    frame_callback=kwargs["sensor_frame_callback"],
                    untouched="sentinel",
                )
            if self.mutate:
                self.mutate()
            if self.return_hook:
                self.return_hook(reply)
        except Exception as exc:
            reply["failure"] = str(exc)
            if not self.suppress_callback_error:
                reply["gate_passed"] = False
        # Mirrors frozen cleanup being ordinary flow, not a finally on BaseException.
        self.cleanup.append(True)
        if self.evidence_mode == "normal":
            Path(evidence).write_text(json.dumps(reply), encoding="utf-8")
        elif self.evidence_mode == "oversized":
            Path(evidence).write_bytes(b"x" * (8 * 1024 * 1024 + 1))
        return reply


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "ROOT", tmp_path)
    source = tmp_path / "scenario.json"
    source.write_text('{"scope":"fixture-only"}', encoding="utf-8")
    monkeypatch.setattr(capture, "_sources", lambda p: [capture._record(p)])
    module = CaptureStandIn()
    out = tmp_path / "artifacts/input-capture-fixture"
    kwargs = dict(
        prompt="  Look at the building\n",
        model_sha256="1" * 64,
        adapter_sha256="2" * 64,
        adapter_config_sha256="3" * 64,
        capture_module=module,
        buffer_factory=BufferStandIn,
    )
    return SimpleNamespace(
        source=source, out=out, evidence=out / "capture.json", kwargs=kwargs, module=module
    )


def run(f, **kwargs):
    return capture.run_input_capture(f.source, f.evidence, f.out, **(f.kwargs | kwargs))


def test_input_capture_closed_preserves_original_and_prepared_receipts(setup):
    f = setup
    report = run(f, execute=True)
    assert report["success"] is True and report["native_exit_code"] == 0
    assert report["prepared_request_count"] == 8
    assert report["external_callback_requested"] is False
    assert report["external_callback_completed_count"] == 0
    assert all(v is False for v in report["claims"].values())
    assert report["maximum_RGB_and_depth_payload_bytes"] == 68_812_800
    assert report["prompt_text_original"] == f.kwargs["prompt"]
    scope = report["scoped_capture_adapter"]
    assert scope["capture_helper_calls"] == 1
    assert scope["scoped_capture_override_restored"] is True
    assert scope["legacy_timestamps_relabelled"] is False
    assert f.module._capture_gazebo_sensor_transport is f.module.original
    assert f.module.helper_calls[0][1]["untouched"] == "sentinel"
    actual_clock = f.module.helper_calls[0][1]["clock"]
    assert actual_clock.identity() == report["clock_identity"]
    assert report["source_bindings_before"] == report["source_bindings_after"]
    for row in report["prepared_requests"]:
        assert (
            hashlib.sha256(row["source_receipt_json"].encode()).hexdigest()
            == row["source_binding_sha256"]
        )
        assert row["request"]["actor_identity_sha256"] == row["actor_identity_sha256"]
        assert row["request"]["model_sha256"] == "1" * 64
        assert row["inference_performed_here"] is False
    stored = json.loads((f.out / "input-report.json").read_bytes())
    assert stored == report


def test_preflight_no_helper_or_runtime_observations(setup):
    f = setup
    report = run(f)
    assert report["success"] is True and report["status"] == "preflight_not_executed"
    assert report["prepared_request_count"] == 0 and not f.module.helper_calls
    assert report["scoped_capture_adapter"]["scoped_capture_override_restored"] is True


def test_callback_receives_typed_request_after_prepared_record(setup):
    seen = []
    report = run(setup, execute=True, sensor_frames=2, request_callback=seen.append)
    assert len(seen) == report["external_callback_completed_count"] == 2
    assert all(type(v) is PreparedGazeboNavigationRequest for v in seen)
    assert report["external_callback_requested"] is True
    assert report["claims"]["inference_performed_here"] is False


@pytest.mark.parametrize("suppressed", [False, True])
def test_callback_exception_preserved_fail_closed_owned_cleanup(setup, suppressed):
    f = setup
    f.module.suppress_callback_error = suppressed

    def fail(value):
        raise KeyError("original callback failure")

    report = run(f, execute=True, sensor_frames=1, request_callback=fail)
    assert report["success"] is False and report["native_exit_code"] == 1
    assert (
        report["prepared_request_count"] == 1 and report["external_callback_completed_count"] == 0
    )
    assert report["error"]["type"] == "KeyError"
    assert "original callback failure" in report["error"]["message"]
    assert f.module.cleanup == [True]
    assert f.module._capture_gazebo_sensor_transport is f.module.original
    assert report["scoped_capture_adapter"]["scoped_capture_override_restored"] is True


@pytest.mark.parametrize("error", [KeyboardInterrupt("interrupt sentinel"), SystemExit(71)])
def test_base_exception_restores_original_and_reraises_exact_error(setup, error):
    f = setup
    f.module.failure = error
    with pytest.raises(type(error)) as exc:
        run(f, execute=True)
    assert exc.value is error
    assert f.module._capture_gazebo_sensor_transport is f.module.original
    report = json.loads((f.out / "input-report.json").read_bytes())
    assert report["native_exit_code"] == 1
    assert report["scoped_capture_adapter"]["scoped_capture_override_restored"] is True


@pytest.mark.parametrize("error", [KeyboardInterrupt("capture interrupted"), SystemExit(83)])
@pytest.mark.parametrize("origin", ["helper", "callback"])
def test_helper_interruptions_bridge_cleanup_then_restore_and_rethrow(setup, error, origin):
    f = setup
    if origin == "helper":
        f.module.helper_failure = error
        callback = None
    else:

        def callback(value):
            raise error

    with pytest.raises(type(error)) as exc:
        run(f, execute=True, request_callback=callback)
    assert exc.value is error
    assert f.module.cleanup == [True]
    assert f.module._capture_gazebo_sensor_transport is f.module.original
    report = json.loads((f.out / "input-report.json").read_bytes())
    assert report["success"] is False and report["error"]["type"] == type(error).__name__
    assert report["scoped_capture_adapter"]["helper_interruptions_bridged"] == 1
    assert report["scoped_capture_adapter"]["scoped_capture_override_restored"] is True
    assert report["capture_evidence"] is not None


def test_callback_primary_error_survives_secondary_runner_error(setup):
    original = ValueError("callback cause")
    runner = setup.module.run_acceptance

    def fail_callback(value):
        raise original

    def fail_runner(*args, **kwargs):
        runner(*args, **kwargs)
        raise OSError("secondary runner failure")

    report = run(setup, execute=True, request_callback=fail_callback, capture_runner=fail_runner)
    assert report["error"] == {"type": "ValueError", "message": "callback cause"}
    assert report["success"] is False


@pytest.mark.parametrize("mode", ["missing", "oversized", "read_error"])
def test_capture_evidence_missing_or_failed_binding_never_succeeds(setup, monkeypatch, mode):
    setup.module.evidence_mode = mode if mode != "read_error" else "normal"
    if mode == "read_error":
        original = capture._record

        def checked(path):
            if Path(path) == setup.evidence:
                raise OSError("late evidence read error")
            return original(path)

        monkeypatch.setattr(capture, "_record", checked)
    report = run(setup, execute=True)
    assert report["success"] is False and report["native_exit_code"] == 1
    assert report["capture_evidence"] is None
    assert report["source_bindings_unchanged"] is True
    if mode != "missing":
        assert "final_validation_error" in report


def test_global_helper_drift_is_rejected_even_though_original_restored(setup):
    setup.module.mutate = lambda: setattr(
        setup.module, "_capture_gazebo_sensor_transport", lambda: None
    )
    report = run(setup, execute=True)
    assert report["success"] is False and report["native_exit_code"] == 1
    assert (
        report["scoped_capture_adapter"]["scoped_capture_override_identity_intact_before_restore"]
        is False
    )
    assert report["scoped_capture_adapter"]["scoped_capture_override_restored"] is True
    assert setup.module._capture_gazebo_sensor_transport is setup.module.original


def test_delayed_binding_validation_rejected_before_record_or_callback(setup, monkeypatch):
    real_now = HighResolutionSystemClock.monotonic
    original = PreparedGazeboNavigationRequest.__post_init__
    offset = [0.0]
    calls = [0]
    monkeypatch.setattr(
        HighResolutionSystemClock, "monotonic", lambda self: real_now(self) + offset[0]
    )

    def validate(value):
        original(value)
        calls[0] += 1
        # First check is the typed constructor; the runner rechecks afterwards.
        if calls[0] == 2:
            offset[0] = 6.0

    monkeypatch.setattr(PreparedGazeboNavigationRequest, "__post_init__", validate)
    seen = []
    report = run(setup, execute=True, request_callback=seen.append)
    assert report["success"] is False and report["prepared_request_count"] == 0
    assert not seen and "expired" in report["error"]["message"]
    assert setup.module.cleanup == [True]


def test_scope_rejects_existing_clock_and_passes_every_other_kwarg():
    received = []

    def original(*a, **k):
        received.append((a, k))

    module = SimpleNamespace(_capture_gazebo_sensor_transport=original)
    clock = HighResolutionSystemClock("fixture-clock")
    state = {"capture_helper_calls": 0}
    with capture.scoped_capture_clock(module, clock, state):
        with pytest.raises(ValueError, match="Existing capture clock"):
            module._capture_gazebo_sensor_transport(clock=None)
        module._capture_gazebo_sensor_transport("positional", value=object())
    assert (
        state["capture_helper_calls"] == 1 and module._capture_gazebo_sensor_transport is original
    )
    assert received[0][0] == ("positional",) and received[0][1]["clock"] is clock


def test_scope_refuses_nested_concurrent_and_other_thread_invocation():
    def original(**kwargs):
        return None

    module = SimpleNamespace(_capture_gazebo_sensor_transport=original)
    clock = HighResolutionSystemClock("fixture-clock")
    errors = []
    with capture.scoped_capture_clock(module, clock, {"capture_helper_calls": 0}):
        with pytest.raises(RuntimeError, match="Nested or concurrent"):
            with capture.scoped_capture_clock(module, clock, {}):
                pass

        def call():
            try:
                module._capture_gazebo_sensor_transport()
            except RuntimeError as exc:
                errors.append(str(exc))

        thread = threading.Thread(target=call)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert "exclusive owner thread" in errors[0]
    assert module._capture_gazebo_sensor_transport is original
    with pytest.raises(ValueError, match="Concurrent"):
        with capture.scoped_capture_clock(module, clock, {}, sensor_capture_concurrent=True):
            pass


def test_extra_frame_fail_closed_retains_partial(setup):
    setup.module.extra_frame = True
    report = run(setup, execute=True, sensor_frames=1)
    assert report["prepared_request_count"] == 1 and report["success"] is False
    assert setup.module.cleanup == [True]


def test_source_drift_rejects_after_restored_scope(setup):
    f = setup
    f.module.mutate = lambda: f.source.write_text('{"changed":true}', encoding="utf-8")
    report = run(f, execute=True)
    assert report["source_bindings_unchanged"] is False and report["success"] is False
    assert report["scoped_capture_adapter"]["scoped_capture_override_restored"] is True


@pytest.mark.parametrize("change", ["flight", "mission", "concurrent", "mode"])
def test_returned_capture_safety_mode_mismatch_rejects(setup, change):
    def mutate(reply):
        if change == "flight":
            reply["safety"]["flight_control_commands_sent"] = 1
        elif change == "mission":
            reply["mission_transaction"]["requested"] = True
        elif change == "concurrent":
            reply["sensor_capture"]["concurrent"] = True
        else:
            reply["execution_requested"] = False

    setup.module.return_hook = mutate
    assert run(setup, execute=True)["success"] is False


@pytest.mark.parametrize(
    "changes",
    [
        {"sensor_frames": 9},
        {"sensor_frames": True},
        {"timeout_s": 121},
        {"execute": 1},
        {"control_port": 14550},
        {"telemetry_port": 24580},
        {"request_callback": 7},
        {"model_sha256": "invalid"},
    ],
)
def test_invalid_budgets_no_output_or_capture(setup, changes):
    with pytest.raises((ValueError, TypeError)):
        run(setup, **changes)
    assert not setup.out.exists() and not setup.module.calls


def test_fresh_output_not_overwritten(setup):
    setup.out.mkdir(parents=True)
    with pytest.raises(FileExistsError):
        run(setup)
    assert not setup.module.calls


def test_bad_evidence_scope_no_output(setup):
    with pytest.raises(ValueError):
        capture.run_input_capture(
            setup.source, setup.out / "input-report.json", setup.out, **setup.kwargs
        )
    assert not setup.out.exists()


def test_standalone_I_help_no_ML_or_Docker():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(root / "scripts/run_gazebo_navigation_input_capture.py"),
            "--help",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0 and "--adapter-config-sha256" in result.stdout


def test_guarded_import_no_ML_dependencies():
    root = Path(__file__).resolve().parents[1]
    code = (
        f"import sys; sys.path[:0]=[{str(root)!r},{str(root / 'src')!r}]; "
        "import scripts.run_gazebo_navigation_input_capture; "
        "assert not {'torch','numpy','PIL','transformers','peft',"
        "'safetensors','pyarrow','datasets'}.intersection(sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr
