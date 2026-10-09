"""Small CPU fixtures only; these are not real Qwen/Gazebo/flight evidence."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from test_onboard_live_vlm_contract import _live_input
from test_run_onboard_live_vlm_retained_replay import FakeWorker, _fixture

from multidetect.autonomy.onboard_live_vlm_contract import OnboardLiveVlmRequest
from multidetect.autonomy.serialization import canonical_sha256
from scripts import audit_onboard_live_vlm_retained_replay as audit
from scripts import run_onboard_live_vlm_retained_replay as replay


class Processor:
    """Only tiny CPU tensors and exact decoder bytes; no AutoModel import."""

    def __init__(self, raw="  ORIGINAL\n"):
        self.torch = pytest.importorskip("torch")
        self.raw = raw
        self.image_processor = SimpleNamespace(patch_size=16, merge_size=2)
        self.tokenizer = SimpleNamespace(decode=self.decode)
        self.last_kwargs = None

    def decode(self, ids, **kwargs):
        assert ids == [7, 8, 9]
        assert kwargs == {"skip_special_tokens": True, "clean_up_tokenization_spaces": False}
        return self.raw

    def apply_chat_template(self, messages, **kwargs):
        self.last_kwargs = kwargs
        return {
            "input_ids": self.torch.tensor([[1, 2, 3]], dtype=self.torch.int64),
            "pixel_values": self.torch.tensor([[0.25, 0.5]], dtype=self.torch.float32),
            "image_grid_thw": self.torch.tensor([[1, 2, 2], [1, 2, 2]]),
        }


def _metrics(request, processor):
    messages = audit.serialize_onboard_live_vlm_messages(request)
    binding = audit.message_binding(messages)
    inputs = processor.apply_chat_template(messages)
    return {
        "actual_processor_messages_sha256": canonical_sha256(binding),
        "processed_images": [
            item
            for msg in binding["messages"]
            for item in msg["content"]
            if item["type"] == "image"
        ],
        "processor_images_kwargs": {"min_pixels": 1024, "max_pixels": 100352},
        "input_tensors": audit.tensor_records(inputs),
        "input_tokens": 3,
        "actual_image_grid_thw": [[1, 2, 2], [1, 2, 2]],
        "processor_patch_size": 16,
        "actual_processor_pixels_per_image": [1024, 1024],
        "actual_processor_total_pixels": 2048,
        "maximum_total_processor_pixels": 200704,
        "actual_processor_pixel_budget_verified": True,
        "output_token_ids": [7, 8, 9],
        "generated_tokens": 3,
        "max_new_tokens_reached": False,
    }


def test_processor_cpu_exact_byte_tensor_and_token_rebuild():
    request = OnboardLiveVlmRequest(_live_input(), max_total_pixels=200704)
    processor = Processor()
    metrics = _metrics(request, processor)
    result = audit.audit_processor(
        request,
        metrics,
        processor,
        processor.raw,
        budget={"max_input_tokens": 8192, "max_new_tokens": 384},
    )
    assert result["token_decode_equals_exact_raw"] is True
    assert result["input_tensors"] == metrics["input_tensors"]
    assert result["actual_processor_total_pixels"] == 2048
    assert processor.last_kwargs["images_kwargs"] == metrics["processor_images_kwargs"]


def test_official_processor_mutated_empty_kwargs_remain_actual_observation():
    class ConsumingProcessor(Processor):
        def apply_chat_template(self, messages, **kwargs):
            result = super().apply_chat_template(messages, **kwargs)
            if "images_kwargs" in kwargs:
                kwargs["images_kwargs"].clear()
            return result

    request = OnboardLiveVlmRequest(_live_input(), max_total_pixels=200704)
    processor = ConsumingProcessor()
    metrics = _metrics(request, processor)
    metrics["processor_images_kwargs"] = {}
    result = audit.audit_processor(
        request,
        metrics,
        processor,
        processor.raw,
        budget={"max_input_tokens": 8192, "max_new_tokens": 384},
    )
    assert result["processor_images_kwargs_reconstructed_before_call"] == {
        "min_pixels": 1024,
        "max_pixels": 100352,
    }
    assert result["processor_images_kwargs_observed_after_call"] == {}
    assert metrics["processor_images_kwargs"] == {}


@pytest.mark.parametrize(
    "field,value",
    [
        ("actual_processor_messages_sha256", "a" * 64),
        ("processed_images", []),
        ("processor_images_kwargs", {"min_pixels": 1, "max_pixels": 100352}),
        ("input_tensors", {}),
        ("input_tokens", 4),
        ("actual_processor_total_pixels", 2049),
        ("output_token_ids", [True, 8, 9]),
        ("generated_tokens", 4),
        ("max_new_tokens_reached", True),
    ],
)
def test_processor_rejects_record_or_native_budget_drift(field, value):
    request = OnboardLiveVlmRequest(_live_input(), max_total_pixels=200704)
    processor = Processor()
    metrics = _metrics(request, processor)
    metrics[field] = value
    with pytest.raises(ValueError):
        audit.audit_processor(
            request,
            metrics,
            processor,
            processor.raw,
            budget={"max_input_tokens": 8192, "max_new_tokens": 384},
        )


def test_native_raw_utf8_cannot_be_trimmed_or_repaired():
    request = OnboardLiveVlmRequest(_live_input(), max_total_pixels=200704)
    processor = Processor()
    with pytest.raises(ValueError, match="unaltered raw"):
        audit.audit_processor(
            request,
            _metrics(request, processor),
            processor,
            "ORIGINAL",
            budget={"max_input_tokens": 8192, "max_new_tokens": 384},
        )


def test_tensor_dtype_shape_and_content_are_independently_bound():
    torch = pytest.importorskip("torch")

    def records(tensor):
        return audit.tensor_records(
            {"input_ids": tensor, "pixel_values": tensor, "image_grid_thw": tensor}
        )["input_ids"]

    original = records(torch.tensor([[1, 2]], dtype=torch.int32))
    assert original != records(torch.tensor([[1, 2]], dtype=torch.int64))
    assert original != records(torch.tensor([1, 2], dtype=torch.int32))
    assert original != records(torch.tensor([[1, 3]], dtype=torch.int32))


def test_source_record_actual_bytes_and_duplicate_paths_rejected(tmp_path):
    path = tmp_path / "source.bin"
    path.write_bytes(b"actual source")
    record = audit.file_record(path)
    assert audit.verify_records([record]) == {record["path"]: record}
    with pytest.raises(ValueError, match="duplicated"):
        audit.verify_records([record, record])
    path.write_bytes(b"different bytes")
    with pytest.raises(ValueError, match="changed"):
        audit.verify_records([record])


def _rejected_fixture(tmp_path, monkeypatch):
    args, _, _ = _fixture(tmp_path)
    args.execute = True
    monkeypatch.setattr(FakeWorker, "mode", "strict_rejection")
    assert replay.run(args, worker_type=FakeWorker, enforce_d_drive=False) == 1
    report_path = args.output_directory / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    request, _ = replay.prepare_retained_request(
        args.manifest,
        args.manifest_sha256,
        repository_root=args.repository_root,
        prompt_text=args.prompt,
        prompt_issued_at_utc=args.prompt_issued_at_utc,
    )
    processor = Processor(report["native_attempt"]["raw_output"])
    metrics = _metrics(request, processor)
    metrics.update(
        {
            "backend": "native_pinned_qwen3_vl_cuda_live_partial_state_v1",
            "request_contract": audit.LIVE_REQUEST_CONTRACT,
            "request_sha256": request.canonical_hash,
            "input_sha256": request.input_sha256,
            "model_sha256": report["model_sha256"],
            "adapter_loaded": False,
            "execution_authority": False,
            "partial_navigation_context_only": True,
            "processed_images_role": "serializer_output_before_processor_smart_resize",
            "factual_sensor_state_sha256": canonical_sha256(
                {"factual_sensor_state": request.model_input.to_mapping()["factual_sensor_state"]}
            ),
        }
    )
    report["native_attempt"]["backend_metrics"] = metrics
    report["native_attempt"]["scope_sha256"] = request.model_input.scope.canonical_hash
    report["native_attempt"]["execution_authority"] = False
    attempt_path = args.output_directory / "native-attempt.json"
    attempt_path.write_text(json.dumps(report["native_attempt"]), encoding="utf-8")
    report["output_records"] = [
        audit.file_record(p) for p in args.output_directory.iterdir() if p.name != "report.json"
    ]
    report_path.write_text(json.dumps(report), encoding="utf-8")
    audit_args = SimpleNamespace(
        repository_root=args.repository_root,
        replay_directory=args.output_directory,
        report_sha256=audit.file_record(report_path)["sha256"],
        manifest=args.manifest,
        snapshot=args.snapshot,
        output_directory=tmp_path / "cpu-audit",
    )
    return audit_args, processor, report


def test_end_to_end_rejected_raw_audit_does_not_upgrade_native_exit(tmp_path, monkeypatch):
    args, processor, _ = _rejected_fixture(tmp_path, monkeypatch)

    def loader(path, **kwargs):
        assert path == str(args.snapshot)
        assert kwargs == {"local_files_only": True, "trust_remote_code": False}
        return processor

    assert audit.audit(args, processor_loader=loader, enforce_d_drive=False) == 0
    receipt = json.loads((args.output_directory / "audit.json").read_text(encoding="utf-8"))
    assert receipt["audit_verified"] is True
    assert receipt["original_native_exit_code"] == 1
    assert receipt["original_strict_intent_accepted"] is False
    assert receipt["strict_rejection_preserved"]
    for key in ("model_weights_loaded", "gpu_used", "generation_repeated", "flight_verified"):
        assert receipt[key] is False


def test_cpu_audit_failure_keeps_original_inputs_and_saves_native1(tmp_path, monkeypatch):
    args, processor, _ = _rejected_fixture(tmp_path, monkeypatch)
    original = (args.replay_directory / "raw-output.txt").read_bytes()
    processor.raw = "cannot repair raw output"
    assert (
        audit.audit(args, processor_loader=lambda *a, **kw: processor, enforce_d_drive=False) == 1
    )
    receipt = json.loads((args.output_directory / "audit.json").read_text(encoding="utf-8"))
    assert receipt["native_exit_code"] == 1 and not receipt["audit_verified"]
    assert receipt["errors"]
    assert (args.replay_directory / "raw-output.txt").read_bytes() == original


def test_existing_audit_output_is_not_overwritten(tmp_path):
    target = tmp_path / "existing"
    target.mkdir()
    marker = target / "original.txt"
    marker.write_bytes(b"preserve")
    with pytest.raises(FileExistsError):
        audit.audit(SimpleNamespace(output_directory=target), enforce_d_drive=False)
    assert marker.read_bytes() == b"preserve"


def test_claim_boundary_exact_booleans_and_native_code_consistent(tmp_path, monkeypatch):
    _, _, report = _rejected_fixture(tmp_path, monkeypatch)
    audit.check_report(report)
    for key, value in [
        ("flight_verified", 0),
        ("current_gazebo_verified", True),
        ("native_exit_code", False),
        ("native_exit_code", 0),
    ]:
        changed = copy.deepcopy(report)
        changed[key] = value
        with pytest.raises(ValueError):
            audit.check_report(changed)


def test_replay_completion_cannot_be_restamped_to_new_host_clock(tmp_path, monkeypatch):
    args, processor, report = _rejected_fixture(tmp_path, monkeypatch)
    report["native_attempt"]["completed_at_s"] += 50000
    attempt_path = args.replay_directory / "native-attempt.json"
    attempt_path.write_text(json.dumps(report["native_attempt"]), encoding="utf-8")
    report["output_records"] = [
        audit.file_record(p) for p in args.replay_directory.iterdir() if p.name != "report.json"
    ]
    report_path = args.replay_directory / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    args.report_sha256 = audit.file_record(report_path)["sha256"]
    assert (
        audit.audit(args, processor_loader=lambda *a, **kw: processor, enforce_d_drive=False) == 1
    )
    receipt = json.loads((args.output_directory / "audit.json").read_text(encoding="utf-8"))
    assert "replay clock domain" in receipt["errors"][0]


def test_original_failed_source_run_cannot_be_renamed_success(tmp_path, monkeypatch):
    args, processor, report = _rejected_fixture(tmp_path, monkeypatch)
    source_run = tmp_path / "old-source-run.json"
    source_run.write_text(
        '{"status":"failed","base_docker_acceptance":{"gate_passed":true}}', encoding="utf-8"
    )
    report["source_records"].append(audit.file_record(source_run))
    report["original_source_run"] = {
        "path": str(source_run.resolve()),
        "status": "success",
        "base_docker_acceptance_gate_passed": True,
        "complete_autonomous_task_success_claimed": False,
    }
    report_path = args.replay_directory / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    args.report_sha256 = audit.file_record(report_path)["sha256"]
    assert (
        audit.audit(args, processor_loader=lambda *a, **kw: processor, enforce_d_drive=False) == 1
    )
    receipt = json.loads((args.output_directory / "audit.json").read_text(encoding="utf-8"))
    assert "source run facts were changed" in receipt["errors"][0]
