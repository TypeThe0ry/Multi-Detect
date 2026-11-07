"""CPU stand-ins only: no native VLM/capture/loading or hardware evidence."""

from __future__ import annotations

import gc
import hashlib
import json
import struct
import subprocess
import sys
import weakref
from dataclasses import replace
from pathlib import Path

import pytest
torch = pytest.importorskip("torch", reason="navigation feature capture tests require optional PyTorch")
from test_onboard_navigation_inference import _request as request_fixture
from test_onboard_navigation_learned_readout_backend import fixture

from scripts import onboard_navigation_feature_capture as capture
from scripts import onboard_navigation_learned_readout_backend as frozen
from scripts.onboard_navigation_inference_backend import NavigationBackendRejected


def _request(**changes):
    return request_fixture(**({"generation": 1} | changes))


@pytest.fixture(autouse=True)
def no_cuda_or_optimizer(monkeypatch):
    assert not torch.cuda.is_initialized()

    def forbidden(*args, **kwargs):
        pytest.fail("Capture must not initialize CUDA or invoke any optimizer")

    monkeypatch.setattr(torch.cuda, "init", forbidden)
    monkeypatch.setattr(torch.cuda, "synchronize", forbidden)
    monkeypatch.setattr(torch.optim.Optimizer, "step", forbidden)
    yield
    assert not torch.cuda.is_initialized()


def make_recorder(tmp_path, monkeypatch, **changes):
    monkeypatch.setattr(capture, "ROOT", tmp_path)
    output = tmp_path / "artifacts/evaluation/onboard-navigation-live-shadow-fixture"
    output.mkdir(parents=True)
    f = fixture()
    recorder = capture.FeatureCaptureRecorder(
        output / "features",
        experiment_plan_sha256="9" * 64,
        engine_identity=f.backend.engine_identity,
        readout_identity=f.identity,
        readout_metadata=f.metadata,
        **changes,
    )
    f.backend.head_predictor = recorder.head_predictor
    return f, recorder


def commit(recorder, request, value=None, error=None):
    diagnostics = value["metrics"] if value is not None else json.loads(error.diagnostics_json)
    row = recorder.commit_outcome(
        request,
        accepted=value is not None,
        diagnostics=diagnostics,
        raw_prediction=diagnostics.get("raw_prediction"),
    )
    raw = Path(row["path"]).read_bytes()
    assert row["bytes"] == len(raw) and row["sha256"] == hashlib.sha256(raw).hexdigest()
    return json.loads(raw)


def test_actual_same_cpu_feature_six_raw_and_one_predictor(tmp_path, monkeypatch):
    f, recorder = make_recorder(tmp_path, monkeypatch)
    request = _request(two_frames=True)
    original = frozen._default_head_predictor
    calls, refs = [], []

    def observe(head, norm, feature, **kwargs):
        calls.append(id(feature))
        refs.append(weakref.ref(feature))
        return original(head, norm, feature, **kwargs)

    monkeypatch.setattr(frozen, "_default_head_predictor", observe)
    recorder.begin(request)
    answer = f.backend.infer(request)
    receipt = commit(recorder, request, value=answer)
    assert len(calls) == len(f.engine.model.trunk.calls) == len(f.engine.processor.calls) == 1
    raw = Path(receipt["feature"]["file"]["path"]).read_bytes()
    values = struct.unpack("<2048f", raw)
    assert len(raw) == 8192 and values[0] == 3 and not any(values[1:])
    assert receipt["feature"]["tensor"] == answer["metrics"]["actual_feature"]
    six = json.loads(Path(receipt["six_output"]["file"]["path"]).read_bytes())
    raw_six = struct.pack("<6f", *six["values"][0])
    assert hashlib.sha256(raw_six).hexdigest() == answer["metrics"]["actual_six_output"]["sha256"]
    assert receipt["scope_sha256"] == request.scope.canonical_hash
    assert receipt["experiment_plan_sha256"] == "9" * 64
    assert receipt["frame_ids"] == ["source-0", "source-1"]
    assert receipt["actor_sequences"] == [0, 1]
    assert receipt["backend_gate_accepted"] is receipt["diagnostic_only"] is True
    assert all(value is False for value in receipt["claims"].values())
    assert "context_sha256" not in receipt and "generated_tokens" not in receipt
    assert recorder.active is None
    assert not recorder.capture_failure_requires_worker_abort
    gc.collect()
    assert all(ref() is None for ref in refs)


def test_post_payload_fsync_delay_rejects_but_preserves_bytes(tmp_path, monkeypatch):
    f, recorder = make_recorder(tmp_path, monkeypatch)
    request = _request()
    original = capture.os.fsync
    calls = []

    def slow(fd):
        original(fd)
        calls.append(fd)
        if len(calls) == 1:
            f.clock.now += 6

    monkeypatch.setattr(capture.os, "fsync", slow)
    recorder.begin(request)
    with pytest.raises(NavigationBackendRejected, match="expired") as error:
        f.backend.infer(request)
    receipt = commit(recorder, request, error=error.value)
    assert receipt["backend_gate_accepted"] is False
    assert not recorder.capture_failure_requires_worker_abort
    assert receipt["feature"] and receipt["six_output"] and len(calls) == 3
    assert len(f.engine.model.trunk.calls) == 1


@pytest.mark.parametrize("failure", ["head", "mutation", "feature_fsync", "output_fsync"])
def test_failure_partial_and_rejected_outcome_are_honest(tmp_path, monkeypatch, failure):
    f, recorder = make_recorder(tmp_path, monkeypatch)
    request = _request()
    original = frozen._default_head_predictor
    if failure in {"head", "mutation"}:

        def fail(head, norm, feature, **kwargs):
            if failure == "head":
                raise MemoryError("CPU stand-in failure")
            output = original(head, norm, feature, **kwargs)
            with torch.no_grad():
                head.bias[0] += 1
            return output

        monkeypatch.setattr(frozen, "_default_head_predictor", fail)
    else:
        original_sync = capture.os.fsync
        sync_count = 0

        def fail_sync(fd):
            nonlocal sync_count
            sync_count += 1
            if sync_count == (1 if failure == "feature_fsync" else 2):
                raise OSError("fixture fsync failure")
            original_sync(fd)

        monkeypatch.setattr(capture.os, "fsync", fail_sync)
    recorder.begin(request)
    with pytest.raises(NavigationBackendRejected) as error:
        f.backend.infer(request)
    saved_error = recorder.capture_error
    assert saved_error is not None
    changed = recorder.capture_error
    changed["message"] = "changed caller copy"
    assert recorder.capture_error == saved_error
    receipt = commit(recorder, request, error=error.value)
    assert receipt["backend_gate_accepted"] is False and receipt["capture_error"]
    assert recorder.capture_failure_requires_worker_abort
    assert receipt["capture_failure_requires_worker_abort"] is True
    assert recorder.last_receipt is not None
    assert recorder.capture_error is None
    with pytest.raises(RuntimeError, match="requires worker abort"):
        recorder.begin(replace(request, generation=2))
    assert receipt["raw_prediction"] is None
    if failure.endswith("fsync"):
        assert len(receipt["unadmitted_partial_files"]) == 1
    else:
        assert receipt["feature"] is not None
    assert recorder.attempted == 1 and recorder.active is None


def test_pre_feature_rejection_closes_without_fabricated_payload(tmp_path, monkeypatch):
    f, recorder = make_recorder(tmp_path, monkeypatch)
    request = _request()
    recorder.begin(request)
    f.engine.model.layers[0].disable_adapters = True
    with pytest.raises(NavigationBackendRejected) as error:
        f.backend.infer(request)
    receipt = commit(recorder, request, error=error.value)
    assert not receipt["predictor_attempted"] and not receipt["original_head_call_attempted"]
    assert receipt["feature"] is receipt["six_output"] is None
    assert receipt["backend_gate_accepted"] is False


@pytest.mark.parametrize("change", ["feature", "raw", "prefix", "accepted_bool", "request"])
def test_changed_or_fabricated_commit_cannot_seal(tmp_path, monkeypatch, change):
    f, recorder = make_recorder(tmp_path, monkeypatch)
    request = _request()
    recorder.begin(request)
    answer = f.backend.infer(request)
    diagnostics = json.loads(capture.canonical(answer["metrics"]))
    raw = json.loads(capture.canonical(answer["raw_prediction"]))
    accepted = True
    if change == "feature":
        diagnostics["actual_feature"]["sha256"] = "0" * 64
    elif change == "raw":
        raw["raw_distance_m"] += 1
        diagnostics["raw_prediction"] = raw
    elif change == "prefix":
        del diagnostics["actual_prefix"]["tensors"]["pixel_values"]
    elif change == "accepted_bool":
        accepted = 1
    else:
        request = replace(request, source_binding_sha256="0" * 64)
    with pytest.raises((ValueError, TypeError)):
        recorder.commit_outcome(
            request, accepted=accepted, diagnostics=diagnostics, raw_prediction=raw
        )
    assert recorder.active is None and not list(recorder.directory.rglob("receipt.json"))


def test_disk_drift_before_seal_is_rejected_without_overwrite(tmp_path, monkeypatch):
    f, recorder = make_recorder(tmp_path, monkeypatch)
    request = _request()
    recorder.begin(request)
    answer = f.backend.infer(request)
    path = recorder.active["directory"] / "feature.f32le"
    path.write_bytes(b"fixture drift")
    with pytest.raises(ValueError, match="changed before"):
        commit(recorder, request, value=answer)
    assert path.read_bytes() == b"fixture drift"


def test_no_retry_nested_request_or_extra_predictor(tmp_path, monkeypatch):
    f, recorder = make_recorder(tmp_path, monkeypatch, max_requests=1)
    request = _request()
    recorder.begin(request)
    with pytest.raises(ValueError, match="Active"):
        recorder.begin(request)
    answer = f.backend.infer(request)
    with pytest.raises(ValueError, match="Exactly one"):
        recorder.head_predictor(f.head, f.norm, torch.zeros(1, 2048), torch_module=torch)
    commit(recorder, request, value=answer)
    with pytest.raises(ValueError, match="excess"):
        recorder.begin(replace(request, generation=2))
    with pytest.raises(ValueError, match="No active"):
        commit(recorder, request, value=answer)


@pytest.mark.parametrize("invalid", ["nan", "dtype", "shape", "gradient", "endian"])
def test_invalid_cpu_feature_never_calls_head(tmp_path, monkeypatch, invalid):
    f, recorder = make_recorder(tmp_path, monkeypatch)
    recorder.begin(_request())
    feature = torch.zeros(1, 2048)
    if invalid == "nan":
        feature[0, 0] = float("nan")
    elif invalid == "dtype":
        feature = feature.double()
    elif invalid == "shape":
        feature = feature[:, :2047]
    elif invalid == "gradient":
        feature.requires_grad_(True)
    else:
        monkeypatch.setattr(capture.sys, "byteorder", "big")
    with pytest.raises(ValueError):
        recorder.head_predictor(f.head, f.norm, feature, torch_module=torch)
    assert recorder.active["original_head_call_attempted"] is False
    assert not list(recorder.directory.rglob("feature.f32le"))


def test_fresh_path_guard_and_import_is_ml_quiet(tmp_path, monkeypatch):
    f, recorder = make_recorder(tmp_path, monkeypatch)
    kwargs = dict(
        experiment_plan_sha256="9" * 64,
        engine_identity=f.backend.engine_identity,
        readout_identity=f.identity,
        readout_metadata=f.metadata,
    )
    with pytest.raises(FileExistsError):
        capture.FeatureCaptureRecorder(recorder.directory, **kwargs)
    with pytest.raises(ValueError, match="Fresh capture"):
        capture.FeatureCaptureRecorder(tmp_path / "escape", **kwargs)
    code = (
        "import importlib.util,sys;"
        f"s=importlib.util.spec_from_file_location('capture',{str(Path(capture.__file__))!r});"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
        "assert not any(x in sys.modules for x in ('torch','numpy','PIL','transformers','peft'))"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
