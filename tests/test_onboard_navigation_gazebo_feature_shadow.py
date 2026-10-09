"""CPU-only R3 integration/ownership fixtures, never actual model/flight proof."""

import hashlib
import json
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_onboard_navigation_gazebo_feature_shadow as live
from scripts.onboard_navigation_inference_backend import NavigationBackendRejected


class Recorder:
    def __init__(self):
        self.calls = []
        self.capture_error = None

    def begin(self, request):
        self.calls.append(("begin", request))

    def commit_outcome(self, request, **kwargs):
        self.calls.append(("commit", request, kwargs))
        return {"path": "fixture-receipt.json", "bytes": 1, "sha256": "f" * 64}


def test_same_backend_answer_has_one_call_and_one_bound_receipt():
    request, recorder, calls = object(), Recorder(), []
    raw = {"distance_m": -2, "kind": "sequence_stop"}
    original = {
        "metrics": {"result_accepted": True},
        "raw_prediction": raw,
        "learned_readout_result": object(),
    }

    def infer(value):
        calls.append(value)
        return original

    answer = live.FeatureRecordingBackend(SimpleNamespace(infer=infer), recorder).infer(request)
    assert calls == [request]
    assert answer is original and answer["raw_prediction"] is raw
    assert recorder.calls[0] == ("begin", request)
    assert len(recorder.calls) == 2
    assert recorder.calls[1][2]["accepted"] is True
    assert answer["metrics"]["live_feature_receipt"]["sha256"] == "f" * 64


def test_rejection_stays_rejected_with_exact_original_raw():
    recorder, calls = Recorder(), []
    raw = {"distance_m": -2, "angle_rad": -0.2}

    def infer(value):
        calls.append(value)
        raise NavigationBackendRejected(
            "expired during saved head",
            {"raw_prediction": raw, "head_readout_returned": True, "result_accepted": False},
        )

    with pytest.raises(NavigationBackendRejected, match="expired") as error:
        live.FeatureRecordingBackend(SimpleNamespace(infer=infer), recorder).infer("request")
    diagnostics = json.loads(error.value.diagnostics_json)
    assert calls == ["request"]
    assert recorder.calls[-1][2]["accepted"] is False
    assert diagnostics["raw_prediction"] == raw
    assert diagnostics["result_accepted"] is False
    assert diagnostics["live_feature_receipt"]["sha256"] == "f" * 64


def test_payload_fsync_failure_with_successful_receipt_aborts_not_normal_rejection():
    recorder = Recorder()
    recorder.capture_error = {"type": "OSError", "message": "feature fsync failed"}

    def infer(_):
        raise NavigationBackendRejected(
            "feature fsync failed",
            {"raw_prediction": None, "result_accepted": False, "head_readout_returned": False},
        )

    backend = live.FeatureRecordingBackend(SimpleNamespace(infer=infer), recorder)
    with pytest.raises(RuntimeError, match="diagnostic receipt retained"):
        backend.infer("request")
    assert len(recorder.calls) == 2
    assert recorder.calls[-1][2]["accepted"] is False


@pytest.mark.parametrize(
    "error", [RuntimeError("native crash"), KeyboardInterrupt(), SystemExit(2)]
)
def test_unexpected_exception_not_swallowed_or_retried(error):
    recorder, calls = Recorder(), []

    def infer(value):
        calls.append(value)
        raise error

    with pytest.raises(type(error)) as observed:
        live.FeatureRecordingBackend(SimpleNamespace(infer=infer), recorder).infer("request")
    assert observed.value is error
    assert calls == ["request"]
    assert recorder.calls[-1][2]["accepted"] is False
    assert recorder.calls[-1][2]["raw_prediction"] is None


def test_failure_receipt_error_keeps_original_exception_primary():
    recorder = Recorder()
    recorder.commit_outcome = lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk failure"))
    error = RuntimeError("original native crash")
    backend = SimpleNamespace(infer=lambda _: (_ for _ in ()).throw(error))
    with pytest.raises(RuntimeError, match="original") as observed:
        live.FeatureRecordingBackend(backend, recorder).infer("request")
    assert observed.value is error
    assert any("disk failure" in note for note in error.__notes__)


def test_begin_failure_prevents_forward():
    recorder = Recorder()
    recorder.begin = lambda _: (_ for _ in ()).throw(ValueError("duplicate request"))
    backend = SimpleNamespace(infer=lambda _: pytest.fail("must not forward"))
    with pytest.raises(ValueError, match="duplicate"):
        live.FeatureRecordingBackend(backend, recorder).infer("request")


def test_receipt_write_failure_does_not_publish_success():
    recorder = Recorder()
    recorder.commit_outcome = lambda *_a, **_k: (_ for _ in ()).throw(OSError("fsync failed"))
    backend = SimpleNamespace(
        infer=lambda _: {
            "metrics": {"result_accepted": True},
            "raw_prediction": {"kind": "sequence_stop"},
        }
    )
    with pytest.raises(OSError, match="fsync"):
        live.FeatureRecordingBackend(backend, recorder).infer("request")


def test_unverified_gate_cannot_be_written_as_accepted():
    recorder = Recorder()
    backend = SimpleNamespace(infer=lambda _: {"metrics": {"result_accepted": False}})
    with pytest.raises(ValueError, match="gate outcome"):
        live.FeatureRecordingBackend(backend, recorder).infer("request")
    assert len(recorder.calls) == 1


@pytest.mark.parametrize("missing", live.NEW_FILES)
def test_new_plan_cannot_omit_new_code_or_frozen_evidence(tmp_path, monkeypatch, missing):
    monkeypatch.setattr(live, "ROOT", tmp_path)
    source_files = [
        {"path": (tmp_path / name).as_posix()}
        for name in (*live.NEW_FILES, live.PRIOR_PLAN, live.PRIOR_REPORT)
        if name != missing
    ]
    with pytest.raises(ValueError, match="missing from plan"):
        live.require_r3_sources({"source_files": source_files})


def test_pinned_old_report_drift_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "ROOT", tmp_path)
    monkeypatch.setattr(live.previous, "record", lambda _: {"sha256": "0" * 64})
    rows = [
        {"path": (tmp_path / name).as_posix()}
        for name in (*live.NEW_FILES, live.PRIOR_PLAN, live.PRIOR_REPORT)
    ]
    with pytest.raises(ValueError, match="Frozen R2"):
        live.require_r3_sources({"source_files": rows})


def test_plan_keeps_exact_prior_prompt_load_and_capture(tmp_path, monkeypatch):
    old = {
        "prompt_text_original": "verbatim task",
        "capture_runtime": object(),
        "load_config": object(),
        "extra_source_files": ["old-module.py"],
    }
    monkeypatch.setattr(live, "ROOT", tmp_path)
    prior = tmp_path / live.PRIOR_PLAN
    prior.parent.mkdir(parents=True)
    prior.write_bytes(b"{}")
    monkeypatch.setattr(live, "PRIOR_PLAN_SHA", hashlib.sha256(b"{}").hexdigest())
    monkeypatch.setattr(live.previous, "record", lambda _: {"sha256": live.PRIOR_PLAN_SHA})
    monkeypatch.setattr(live.plans, "_json", lambda _: old)
    monkeypatch.setattr(
        live.plans,
        "admit_plan",
        lambda *_: pytest.fail(
            "completed prior evidence must not be re-admitted with fresh-output requirement"
        ),
    )
    calls = []

    def build(output, **kwargs):
        calls.append((output, kwargs))
        return {"fixture": True}

    monkeypatch.setattr(live.plans, "build_plan", build)
    monkeypatch.setattr(live, "require_r3_sources", lambda _: True)
    assert live.build_feature_plan("fresh") == {"fixture": True}
    assert calls[0][1]["load_config"] is old["load_config"]
    assert calls[0][1]["capture_runtime"] is old["capture_runtime"]
    assert calls[0][1]["prompt_text_original"] == "verbatim task"
    assert set(live.NEW_FILES) <= set(calls[0][1]["extra_source_files"])


def test_supervisor_launches_new_r3_child_and_checks_owned_cleanup(tmp_path, monkeypatch):
    output = tmp_path / "new-r3"
    plan = {
        "output_dir": str(output),
        "worker_timeout_s": 600,
        "load_config": {"runtime": {"python_executable": "bound-python.exe"}},
    }
    monkeypatch.setattr(live.plans, "admit_plan", lambda *_: (plan, {"sha256": "a" * 64}))
    monkeypatch.setattr(live, "require_r3_sources", lambda _: True)
    monkeypatch.setattr(live.shutil, "which", lambda _: "docker")
    monkeypatch.setattr(live.previous, "docker_inspect", lambda *_: None)
    command_seen = []

    class Child:
        pid = 999999
        returncode = None

        def __init__(self, command, **kwargs):
            command_seen.append(command)
            assert kwargs["cwd"] == live.ROOT

        def wait(self, timeout):
            assert timeout == 600
            output.mkdir()
            live.previous.write_new(output / "worker-report.json", {"success": True})
            self.returncode = 0
            return 0

        def poll(self):
            return self.returncode

    def cleanup(_docker, _name, _output, report):
        report["container_absent_after"] = True

    monkeypatch.setattr(live.subprocess, "Popen", Child)
    monkeypatch.setattr(live.previous, "observe_owned_cleanup", cleanup)
    assert live.supervise(tmp_path / "plan.json", "a" * 64) == 0
    command = command_seen[0]
    assert command[0] == "bound-python.exe"
    assert str(Path(live.__file__).resolve()) in command
    assert "--worker" in command and "-I" in command and "-B" in command
    report = json.loads(output.with_name(output.name + "-supervisor.json").read_text())
    assert report["worker_wait_completed"] is True
    assert all(value is False for value in report["claims"].values())


def test_import_is_quiet_and_does_not_load_ML_or_launch_process():
    root = str(live.ROOT)
    code = (
        "import sys; sys.path[:0] = [" + repr(root) + ", " + repr(str(live.ROOT / "src")) + "]; "
        "import scripts.run_onboard_navigation_gazebo_feature_shadow; "
        "heavy = {'torch','numpy','PIL','transformers','peft','pymavlink'}; "
        "assert not (heavy & set(sys.modules)); "
        "print('import-quiet')"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "import-quiet"


def feature_fixture(tmp_path):
    from scripts.onboard_navigation_feature_capture import canonical

    feature_path, six_path, receipt_path = (
        tmp_path / "feature.f32le",
        tmp_path / "six-output.json",
        tmp_path / "receipt.json",
    )
    feature_path.write_bytes(b"\0" * 8192)
    feature_tensor = {
        "shape": [1, 2048],
        "dtype": "torch.float32",
        "sha256": hashlib.sha256(b"\0" * 8192).hexdigest(),
    }
    values = [[0.0, 1.0, 2.0, 3.0, -2.0, -0.2]]
    # Use exact FP32 round-trip numbers, as the actual tensor's tolist() does.
    values = [list(struct.unpack("<6f", struct.pack("<6f", *values[0])))]
    six_tensor = {
        "shape": [1, 6],
        "dtype": "torch.float32",
        "sha256": hashlib.sha256(struct.pack("<6f", *values[0])).hexdigest(),
    }
    six = {"tensor": six_tensor, "values": values, "source": "actual_same_call_head_output"}
    live.previous.write_new(six_path, six)
    raw = {"selected_class": "sequence_stop"}
    diagnostics = {"actual_feature": feature_tensor, "actual_six_output": six_tensor}
    receipt = {
        "experiment_plan_sha256": "a" * 64,
        "request_sha256": "b" * 64,
        "backend_gate_accepted": False,
        "capture_error": None,
        "unadmitted_partial_files": [],
        "predictor_returned": True,
        "original_head_call_attempted": True,
        "feature": {"file": live.previous.record(feature_path), "tensor": feature_tensor},
        "six_output": {
            "file": live.previous.record(six_path),
            "tensor": six_tensor,
            "values": values,
        },
        "raw_prediction": raw,
        "backend_diagnostics_sha256": hashlib.sha256(canonical(diagnostics)).hexdigest(),
    }
    live.previous.write_new(receipt_path, receipt)
    row = {
        "request_sha256": "b" * 64,
        "accepted": False,
        "raw_prediction": raw,
        "metrics": {**diagnostics, "live_feature_receipt": live.previous.record(receipt_path)},
    }
    return row, receipt, receipt_path, feature_path, six_path


def test_complete_feature_capture_does_not_require_or_create_inference_acceptance(tmp_path):
    row, *_ = feature_fixture(tmp_path)
    assert live.verify_feature_closure([row], "a" * 64) == 1
    assert row["accepted"] is False


@pytest.mark.parametrize("changed", ["feature", "receipt", "six"])
def test_feature_closure_refuses_file_drift(tmp_path, changed):
    row, _, receipt_path, feature_path, six_path = feature_fixture(tmp_path)
    target = {"feature": feature_path, "receipt": receipt_path, "six": six_path}[changed]
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(ValueError, match="receipt|payload"):
        live.verify_feature_closure([row], "a" * 64)


@pytest.mark.parametrize("change", ["plan", "request", "acceptance", "diagnostic"])
def test_feature_closure_refuses_crossed_request_or_gate(tmp_path, change):
    row, *_ = feature_fixture(tmp_path)
    plan = "a" * 64
    if change == "plan":
        plan = "c" * 64
    elif change == "request":
        row["request_sha256"] = "c" * 64
    elif change == "acceptance":
        row["accepted"] = True
    else:
        row["metrics"]["invented_gate_fact"] = True
    with pytest.raises(ValueError, match="capture|snapshot"):
        live.verify_feature_closure([row], plan)
