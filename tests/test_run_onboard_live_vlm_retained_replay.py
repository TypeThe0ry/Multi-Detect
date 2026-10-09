"""CPU wire/fake-worker fixtures are not native VLM, live clock or flight proof."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_onboard_live_vlm_contract import _live_input
from test_onboard_vlm_runtime_contract import _reply

from multidetect.autonomy.onboard_vlm_runtime_contract import parse_onboard_vlm_runtime_response
from multidetect.autonomy.onboard_vlm_worker import OnboardVlmWorkerResult
from multidetect.autonomy.serialization import canonical_json
from scripts import run_onboard_live_vlm_retained_replay as replay


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _fixture(tmp_path):
    original = _live_input()
    rgb_dir = tmp_path / "rgb"
    rgb_dir.mkdir()
    rows = []
    for index, frame in enumerate(original.frames):
        provenance = json.loads(original.factual_sensor_state_json[index])["source_provenance"]
        bgr = bytearray(frame.image_bytes)
        bgr[0::3], bgr[2::3] = frame.image_bytes[2::3], frame.image_bytes[0::3]
        filename = f"frame-{index:06d}.bgr24"
        (rgb_dir / filename).write_bytes(bgr)
        rows.append(
            {
                "bytes": len(bgr),
                "file": filename,
                "frame_id": frame.frame_id,
                "provenance": provenance,
                "sequence": index,
                "sha256": _sha(bgr),
                "wire_payload_sha256": _sha(bgr),
                "wire_record_sha256": provenance["record_sha256"],
                "raw_bytes_sha256_matches_wire_payload": True,
            }
        )
    manifest = rgb_dir / "manifest.json"
    document = {
        "schema": "multidetect.gazebo-bgr24-frame-set",
        "schema_version": 2,
        "wire_version": 3,
        "pixel_format": "BGR24",
        "frame_count": 2,
        "captured_at_clock": "host_monotonic",
        "frames": rows,
    }
    manifest.write_text(canonical_json(document), encoding="utf-8")
    snapshot = tmp_path / "snapshot" / replay.PINNED_REVISION
    snapshot.mkdir(parents=True)
    (snapshot / "model.safetensors").write_bytes(b"CPU fixture only, not model weights")
    (snapshot / "config.json").write_text('{"fixture_only":true}', encoding="utf-8")
    return (
        SimpleNamespace(
            repository_root=tmp_path,
            manifest=manifest,
            manifest_sha256=_sha(manifest.read_bytes()),
            snapshot=snapshot,
            model_sha256=_sha((snapshot / "model.safetensors").read_bytes()),
            prompt="  检查画面与实际传感器；不能确认就说明未知。\n",
            prompt_issued_at_utc="2026-10-07T00:00:00Z",
            output_directory=tmp_path / "new-output",
            deadline_s=30.0,
            max_total_pixels=200704,
            max_new_tokens=384,
            execute=False,
            source_run_report=None,
        ),
        original,
        document,
    )


def _prepare(args):
    return replay.prepare_retained_request(
        args.manifest,
        args.manifest_sha256,
        repository_root=args.repository_root,
        prompt_text=args.prompt,
        prompt_issued_at_utc=args.prompt_issued_at_utc,
        deadline_s=args.deadline_s,
        max_total_pixels=args.max_total_pixels,
    )


def _rewrite(args, document):
    args.manifest.write_text(canonical_json(document), encoding="utf-8")
    args.manifest_sha256 = _sha(args.manifest.read_bytes())


class FakeWorker:
    """Deterministic CPU result contract, not a model inference implementation."""

    mode = "accepted"
    instances = []

    def __init__(self, factory, *, startup_timeout_s):
        self.factory = factory
        self.state, self.error, self.last_attempt = "idle", None, None
        self.process_id = None
        self.result = None
        self.__class__.instances.append(self)

    def start(self):
        if self.mode == "startup_error":
            self.state, self.error = "failed", "backend_error:FixtureFailure"

    def submit(self, request, *, observed_now_s):
        assert observed_now_s == request.model_input.requested_at_s
        self.request = request
        if self.mode == "backend_error":
            self.state, self.error = "failed", "backend_error:LiveVlmPreprocessingError"
            return 0
        payload = _reply(frame_ids=[request.model_input.frames[-1].frame_id])
        raw = "  " + json.dumps(payload, ensure_ascii=False) + "\n"
        if self.mode == "strict_rejection":
            raw = '{"output":{"response":"record_location"}}'
        completed = observed_now_s + 0.05
        metrics = {"fixture_only": True, "generated_tokens": 7, "output_token_ids": [1, 2, 3]}
        self.last_attempt = {
            "generation": 0,
            "request_sha256": request.canonical_hash,
            "input_sha256": request.input_sha256,
            "raw_output": raw,
            "backend_metrics": metrics,
            "completed_at_s": completed,
            "inference_latency_ms": 50.0,
        }
        if self.mode == "strict_rejection":
            self.state, self.error = "failed", "host_validation:ValueError"
            return 0
        response = parse_onboard_vlm_runtime_response(
            raw,
            request,
            current_scope=request.model_input.scope,
            completed_at_s=completed,
            inference_latency_ms=50.0,
        )
        self.result = OnboardVlmWorkerResult(
            0, request.canonical_hash, response, raw, completed, 50.0, canonical_json(metrics)
        )
        if self.mode == "wrong_generation":
            self.result = replace(self.result, generation=1)
        if self.mode == "wrong_request":
            self.result = replace(self.result, request_sha256="a" * 64)
        if self.mode == "wrong_raw":
            self.result = replace(self.result, raw_output=raw + " ")
        if self.mode == "future_result":
            self.result = replace(self.result, completed_at_s=request.model_input.deadline_at_s + 1)
        self.state = "ready"
        return 0

    def take_result(self):
        result, self.result = self.result, None
        if result is not None:
            self.state = "idle"
        return result

    def close(self):
        if self.mode == "cleanup_error":
            raise RuntimeError("fixture cleanup failed")
        self.state = "closed"


def _run(args, monkeypatch, *, mode="accepted"):
    monkeypatch.setattr(FakeWorker, "mode", mode)
    monkeypatch.setattr(FakeWorker, "instances", [])
    code = replay.run(args, worker_type=FakeWorker, enforce_d_drive=False)
    return code, json.loads((args.output_directory / "report.json").read_text(encoding="utf-8"))


def test_prepare_preserves_original_wire_rgb_timestamps_ns_and_verbatim_prompt(tmp_path):
    args, original, document = _fixture(tmp_path)
    request, records = _prepare(args)
    assert request.model_input.prompt_text_original == args.prompt
    assert request.model_input.requested_at_s == 2.001
    assert request.model_input.deadline_at_s == 32.001
    assert (
        request.model_input.scope.clock_epoch
        == "retained-host-monotonic-replay:" + args.manifest_sha256
    )
    assert request.model_input.scope.home_sha256 is None
    for index, frame in enumerate(request.model_input.frames):
        assert frame.image_bytes == original.frames[index].image_bytes
        assert frame.captured_at_s == original.frames[index].captured_at_s
        assert frame.context is None
        facts = json.loads(request.model_input.factual_sensor_state_json[index])
        assert facts["source_provenance"] == document["frames"][index]["provenance"]
        assert facts["rgb_sha256"] == frame.image_sha256
    assert len(records) == 3
    request.validate_binding()


def test_default_prepare_never_constructs_worker_or_backend(tmp_path, monkeypatch):
    args, _, _ = _fixture(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("default prepare must not construct a worker/backend")

    monkeypatch.setattr(replay.LiveQwenVlmBackendFactory, "__call__", forbidden)
    assert replay.run(args, worker_type=forbidden, enforce_d_drive=False) == 0
    report = json.loads((args.output_directory / "report.json").read_text(encoding="utf-8"))
    for field in (
        "worker_created",
        "inference_submitted",
        "vlm_inference_performed",
        "native_generation_observed",
        "execution_authority",
        "flight_verified",
        "current_gazebo_verified",
        "training_performed",
        "adapter_loaded",
        "mission_compiled",
        "candidate_promoted",
    ):
        assert report[field] is False
    assert report["source_records_unchanged"] is True
    assert report["generation_budget"] == {"max_input_tokens": 8192, "max_new_tokens": 384}


def test_existing_output_refused_without_modifying_original(tmp_path, monkeypatch):
    args, _, _ = _fixture(tmp_path)
    args.output_directory.mkdir()
    marker = args.output_directory / "report.json"
    marker.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        replay.run(args, worker_type=FakeWorker, enforce_d_drive=False)
    assert marker.read_bytes() == b"original"


def test_wrong_manifest_hash_rejected_before_output(tmp_path):
    args, _, _ = _fixture(tmp_path)
    args.manifest_sha256 = "a" * 64
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        _prepare(args)
    assert not args.output_directory.exists()


@pytest.mark.parametrize("value", ['{"frames":[],"frames":[]}', '{"x":NaN}', '{"x":1e999}'])
def test_strict_manifest_duplicate_nonfinite_rejected(tmp_path, value):
    args, _, _ = _fixture(tmp_path)
    args.manifest.write_text(value, encoding="utf-8")
    args.manifest_sha256 = _sha(args.manifest.read_bytes())
    with pytest.raises(ValueError):
        _prepare(args)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d["frames"][0].update(file="../frame-000000.bgr24"),
        lambda d: d["frames"][0].update(sequence=True),
        lambda d: d["frames"][1].update(sequence=0),
        lambda d: d["frames"][0].update(raw_bytes_sha256_matches_wire_payload=False),
        lambda d: d.update(schema_version=2.0),
        lambda d: d["frames"][1]["provenance"].update(captured_at_clock="host_perf_counter"),
        lambda d: d["frames"][1]["provenance"].update(captured_at_s=0.5),
        lambda d: d["frames"][0]["provenance"]["wire_telemetry"].update(latitude_deg=48.0),
    ],
)
def test_rehashed_manifest_cannot_change_actual_source_binding(tmp_path, mutation):
    args, _, document = _fixture(tmp_path)
    mutation(document)
    _rewrite(args, document)
    with pytest.raises((ValueError, TypeError)):
        _prepare(args)


def test_changed_bgr_bytes_rejected(tmp_path):
    args, _, _ = _fixture(tmp_path)
    (args.manifest.parent / "frame-000000.bgr24").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="BGR"):
        _prepare(args)


def test_source_run_failed_status_bound_not_upgraded_to_task_success(tmp_path, monkeypatch):
    args, _, _ = _fixture(tmp_path)
    args.source_run_report = tmp_path / "original-run.json"
    args.source_run_report.write_text(
        '{"status":"failed","base_docker_acceptance":{"gate_passed":true}}', encoding="utf-8"
    )
    code, report = _run(args, monkeypatch)
    assert code == 0
    assert report["original_source_run"]["status"] == "failed"
    assert report["original_source_run"]["base_docker_acceptance_gate_passed"] is True
    assert report["original_source_run"]["complete_autonomous_task_success_claimed"] is False
    assert any(
        record["sha256"] == _sha(args.source_run_report.read_bytes())
        for record in report["source_records"]
    )


def test_cpu_worker_result_reparsed_once_raw_and_metrics_preserved(tmp_path, monkeypatch):
    args, _, _ = _fixture(tmp_path)
    args.execute = True
    code, report = _run(args, monkeypatch)
    assert code == 0
    assert report["strict_intent_accepted"] is True
    assert report["native_generation_observed"] is True
    assert report["worker_closed"] is True
    assert report["result"]["backend_metrics"]["fixture_only"] is True
    assert (args.output_directory / "raw-output.txt").read_bytes() == report["native_attempt"][
        "raw_output"
    ].encode("utf-8")
    assert report["result"]["completed_at_s"] == pytest.approx(2.051)
    assert report["live_clock_verified"] is False
    assert report["sensor_to_host_clock_conversion_claimed"] is False
    assert report["execution_authority"] is False
    assert FakeWorker.instances[0].result is None


@pytest.mark.parametrize(
    "mode", ["strict_rejection", "wrong_generation", "wrong_request", "wrong_raw", "future_result"]
)
def test_native_failure_keeps_original_raw_and_closes_worker(tmp_path, monkeypatch, mode):
    args, _, _ = _fixture(tmp_path)
    args.execute = True
    code, report = _run(args, monkeypatch, mode=mode)
    assert code == 1
    assert report["strict_intent_accepted"] is False
    assert report["worker_closed"] is True
    assert report["errors"]
    assert (args.output_directory / "raw-output.txt").read_bytes() == report["native_attempt"][
        "raw_output"
    ].encode("utf-8")


@pytest.mark.parametrize("mode", ["startup_error", "backend_error"])
def test_no_native_attempt_does_not_claim_actual_inference_or_raw(tmp_path, monkeypatch, mode):
    args, _, _ = _fixture(tmp_path)
    args.execute = True
    code, report = _run(args, monkeypatch, mode=mode)
    assert code == 1
    assert report["native_attempt"] is None
    assert report["vlm_inference_performed"] is False
    assert report["native_generation_observed"] is False
    assert not (args.output_directory / "raw-output.txt").exists()
    assert report["worker_closed"] is True
    if mode == "backend_error":
        assert report["inference_submitted"] is True
        assert report["backend_failure_details_available"] is False


def test_cleanup_error_preserves_failure_report_and_raw(tmp_path, monkeypatch):
    args, _, _ = _fixture(tmp_path)
    args.execute = True
    code, report = _run(args, monkeypatch, mode="cleanup_error")
    assert code == 1
    assert report["strict_intent_accepted"] is True
    assert report["worker_closed"] is False
    assert report["errors"][-1]["phase"] == "cleanup"
    assert (args.output_directory / "raw-output.txt").exists()


def test_cleanup_source_drift_fails_closed_and_retains_report(tmp_path, monkeypatch):
    args, _, _ = _fixture(tmp_path)
    args.execute = True
    original_close = FakeWorker.close

    def changed_close(worker):
        (args.manifest.parent / "frame-000001.bgr24").write_bytes(b"changed after submit")
        original_close(worker)

    monkeypatch.setattr(FakeWorker, "close", changed_close)
    code, report = _run(args, monkeypatch)
    assert code == 1
    assert report["strict_intent_accepted"] is True
    assert report["source_records_unchanged"] is False
    assert report["source_changes"][0]["reason"] == "source_bytes_changed"


def test_json_writer_rejects_overflow_before_exclusive_creation(tmp_path):
    path = tmp_path / "invalid.json"
    with pytest.raises(ValueError):
        replay._write_json(path, {"nested": {"nonfinite": float("inf")}})
    assert not path.exists()


def test_output_reopen_error_still_seals_failure_report(tmp_path, monkeypatch):
    args, _, _ = _fixture(tmp_path)
    original_record = replay._record

    def failure(path):
        if path.name == "prepared-input.json":
            raise PermissionError("fixture output became unreadable")
        return original_record(path)

    monkeypatch.setattr(replay, "_record", failure)
    code, report = _run(args, monkeypatch)
    assert code == 1
    assert report["errors"][-1]["phase"] == "output_reopen"
    assert report["source_records_unchanged"] is True


def test_explicit_repository_and_pinned_snapshot_required(tmp_path):
    args, _, _ = _fixture(tmp_path)
    with pytest.raises(ValueError, match="repository"):
        replay.prepare_retained_request(
            args.manifest,
            args.manifest_sha256,
            repository_root=args.snapshot,
            prompt_text=args.prompt,
            prompt_issued_at_utc=args.prompt_issued_at_utc,
        )
    args.snapshot = args.manifest.parent
    with pytest.raises(ValueError, match="pinned"):
        replay.run(args, enforce_d_drive=False)
    assert not args.output_directory.exists()


def test_output_directory_must_be_d_drive_for_real_cli(tmp_path):
    args, _, _ = _fixture(tmp_path)
    args.output_directory = Path("C:/retained-replay-forbidden-output")
    with pytest.raises(ValueError, match="D:"):
        replay.run(args)
