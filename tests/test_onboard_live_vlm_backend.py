"""Native-shaped processor/model CPU fakes; no CUDA/model download is run."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_onboard_live_vlm_contract import _live_input

from multidetect.autonomy import onboard_live_vlm_backend as live_backend
from multidetect.autonomy.onboard_live_vlm_backend import (
    LiveQwenVlmBackend,
    LiveQwenVlmBackendFactory,
    LiveVlmPreprocessingError,
)
from multidetect.autonomy.onboard_live_vlm_contract import (
    OnboardLiveVlmRequest,
    gazebo_sensor_fact_json,
)
from multidetect.autonomy.onboard_vlm_backend import QwenVlmBackend, QwenVlmBackendFactory
from multidetect.autonomy.onboard_vlm_runtime_contract import OnboardVlmRuntimeRequest
from multidetect.autonomy.serialization import canonical_sha256
from multidetect.backends.gazebo_sim import GAZEBO_SIM_WIRE_HEADER


class _Inputs(dict):
    def to(self, device):
        assert device == "cuda:0"
        return self


class _Cuda:
    def reset_peak_memory_stats(self):
        pass

    def synchronize(self):
        pass

    def max_memory_allocated(self):
        return 123

    def max_memory_reserved(self):
        return 456

    def mem_get_info(self):
        return (1000, 2000)


class _Processor:
    def __init__(self, torch, *, tokens=12, raw="  ORIGINAL\n"):
        self.torch = torch
        self.tokens = tokens
        self.raw = raw
        self.messages = None
        self.tokenizer = SimpleNamespace(decode=self.decode)
        self.image_processor = SimpleNamespace(patch_size=16, merge_size=2)
        self.forced_grid = None

    def apply_chat_template(self, messages, **kwargs):
        base_kwargs = dict(
            tokenize=True, add_generation_prompt=True, return_dict=True, return_tensors="pt"
        )
        assert {
            key: value for key, value in kwargs.items() if key != "images_kwargs"
        } == base_kwargs
        assert kwargs["images_kwargs"]["min_pixels"] == 1024
        self.messages = messages
        return _Inputs(
            input_ids=self.torch.arange(self.tokens).unsqueeze(0),
            pixel_values=self.torch.ones((2, 3), dtype=self.torch.float32),
            image_grid_thw=self.torch.tensor(
                self.forced_grid
                or [[1, 2, 2]]
                * sum(item["type"] == "image" for msg in messages for item in msg["content"])
            ),
        )

    def decode(self, ids, **kwargs):
        assert kwargs == dict(skip_special_tokens=True, clean_up_tokenization_spaces=False)
        assert ids == [71, 72, 73]
        return self.raw


class _Model:
    def __init__(self, torch):
        self.torch = torch
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return self.torch.cat((kwargs["input_ids"], self.torch.tensor([[71, 72, 73]])), dim=1)


def _backend(*, input_tokens=12, max_input_tokens=256, max_new_tokens=8):
    torch = pytest.importorskip("torch")
    backend = object.__new__(LiveQwenVlmBackend)
    backend.torch = SimpleNamespace(
        Tensor=torch.Tensor, uint8=torch.uint8, cuda=_Cuda(), inference_mode=torch.inference_mode
    )
    backend.config = LiveQwenVlmBackendFactory(
        "D:/fixture-local-model", "a" * 64, max_input_tokens, max_new_tokens
    )
    backend.processor = _Processor(torch, tokens=input_tokens)
    backend.model = _Model(torch)
    backend.load_s = 0.125
    return backend


def test_live_backend_reuses_loader_without_mutating_legacy_serializer():
    assert LiveQwenVlmBackend.__init__ is QwenVlmBackend.__init__
    assert isinstance(LiveQwenVlmBackendFactory("D:/fixture", "a" * 64), QwenVlmBackendFactory)
    assert LiveQwenVlmBackend.infer is not QwenVlmBackend.infer


def test_real_processor_messages_have_rgb_prompt_and_partial_gps_not_truth():
    backend = _backend()
    request = OnboardLiveVlmRequest(_live_input())
    result = backend.infer(request)
    messages = backend.processor.messages
    text = "\n".join(
        item["text"] for msg in messages for item in msg["content"] if item["type"] == "text"
    )
    assert '"latitude_deg":47.3' in text
    assert '"home_position":null' in text and '"wind":null' in text
    assert request.model_input.prompt_text_original in text
    for forbidden in (
        "world_truth",
        "labels",
        "wire_unsigned_header_hex",
        "frame_telemetry",
        '"position_healthy":true',
    ):
        assert forbidden not in text
    images = [
        item["image"] for msg in messages for item in msg["content"] if item["type"] == "image"
    ]
    assert [i.tobytes() for i in images] == [f.image_bytes for f in request.model_input.frames]
    assert result["raw_output"] == "  ORIGINAL\n"
    metrics = result["metrics"]
    assert metrics["input_sha256"] == request.input_sha256
    assert metrics["request_sha256"] == request.canonical_hash
    assert metrics["factual_sensor_state_sha256"] == canonical_sha256(
        {"factual_sensor_state": request.model_input.to_mapping()["factual_sensor_state"]}
    )
    assert metrics["output_token_ids"] == [71, 72, 73]
    assert metrics["generated_tokens"] == 3 and metrics["max_new_tokens_reached"] is False
    assert metrics["input_tensors"]["input_ids"]["shape"] == [1, 12]
    assert metrics["input_tensors"]["pixel_values"]["shape"] == [2, 3]
    assert len(metrics["actual_processor_messages_sha256"]) == 64
    assert metrics["actual_processor_total_pixels"] == 2048
    assert metrics["actual_processor_pixel_budget_verified"] is True
    assert metrics["adapter_loaded"] is metrics["execution_authority"] is False
    generation = backend.model.calls[0]
    assert generation["do_sample"] is False and generation["max_new_tokens"] == 8
    assert 0 < generation["max_time"] <= 2.0


def test_input_token_budget_is_fail_closed_no_silent_truncation():
    backend = _backend(input_tokens=257)
    with pytest.raises(ValueError, match="cannot be silently truncated"):
        backend.infer(OnboardLiveVlmRequest(_live_input()))
    assert backend.model.calls == []


def test_preprocessing_exhaustion_refuses_generation(monkeypatch):
    backend = _backend()
    clock = iter([0.0, 3.0])
    monkeypatch.setattr(live_backend, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    with pytest.raises(TimeoutError):
        backend.infer(OnboardLiveVlmRequest(_live_input()))
    assert backend.model.calls == []


def test_output_ceiling_is_reported_not_hidden_or_repaired():
    result = _backend(max_new_tokens=3).infer(OnboardLiveVlmRequest(_live_input()))
    assert result["metrics"]["max_new_tokens_reached"] is True
    assert result["raw_output"] == "  ORIGINAL\n"


def test_live_backend_refuses_ordinary_v2_request_before_processor():
    backend = _backend()
    with pytest.raises(TypeError):
        backend.infer(OnboardVlmRuntimeRequest(_live_input()))
    assert backend.processor.messages is None and backend.model.calls == []


def test_partial_sensor_changes_change_message_and_input_binding_with_same_rgb():
    left = _backend().infer(OnboardLiveVlmRequest(_live_input(latitude=47.3)))
    right = _backend().infer(OnboardLiveVlmRequest(_live_input(latitude=48.0)))
    assert (
        left["metrics"]["actual_processor_messages_sha256"]
        != right["metrics"]["actual_processor_messages_sha256"]
    )
    assert left["metrics"]["input_sha256"] != right["metrics"]["input_sha256"]
    assert left["metrics"]["processed_images"] == right["metrics"]["processed_images"]


@pytest.mark.parametrize(
    "values",
    [
        {"snapshot_path": "relative"},
        {"model_sha256": "bad"},
        {"max_input_tokens": True},
        {"max_input_tokens": 255},
        {"max_new_tokens": 0},
        {"max_new_tokens": 1025},
    ],
)
def test_inherited_factory_configuration_is_bounded(values):
    defaults = {"snapshot_path": "D:/fixture", "model_sha256": "a" * 64}
    defaults.update(values)
    with pytest.raises(ValueError):
        LiveQwenVlmBackendFactory(**defaults)


def _large_request(count):
    """Four CPU wire fixtures, not new live capture or semantic evidence."""
    base = _live_input()
    frames = []
    facts = []
    for sequence in range(count):
        template = base.frames[sequence % 2]
        rgb = template.image_bytes * (1280 * 960 // (template.width * template.height))
        frame = replace(
            template,
            frame_id=f"fixture-large-{sequence}",
            captured_at_s=float(sequence + 1),
            image_bytes=rgb,
            image_sha256=hashlib.sha256(rgb).hexdigest(),
            width=1280,
            height=960,
        )
        p = json.loads(base.factual_sensor_state_json[sequence % 2])["source_provenance"]
        stamp = (sequence + 1) * 1_000_000_000
        header = list(GAZEBO_SIM_WIRE_HEADER.unpack(bytes.fromhex(p["wire_unsigned_header_hex"])))
        header[3:12] = [
            sequence,
            stamp,
            1280,
            960,
            1280 * 3,
            len(rgb),
            stamp - 3,
            stamp - 2,
            stamp - 1,
        ]
        unsigned = GAZEBO_SIM_WIRE_HEADER.pack(*header)
        bgr = bytearray(rgb)
        bgr[0::3], bgr[2::3] = rgb[2::3], rgb[0::3]
        p.update(
            frame_id=frame.frame_id,
            sequence=sequence,
            captured_at_s=frame.captured_at_s,
            width=1280,
            height=960,
            stride=3840,
            payload_bytes=len(rgb),
            sensor_stamp_ns=stamp,
            camera_info_stamp_ns=stamp - 3,
            navsat_stamp_ns=stamp - 2,
            odometry_stamp_ns=stamp - 1,
            wire_unsigned_header_hex=unsigned.hex(),
            payload_sha256=hashlib.sha256(bgr).hexdigest(),
            record_sha256=hashlib.sha256(unsigned + bgr).hexdigest(),
        )
        p["frame_telemetry"].update(
            gps_observed_at_s=frame.captured_at_s - 2e-9,
            attitude_observed_at_s=frame.captured_at_s - 1e-9,
            velocity_observed_at_s=frame.captured_at_s - 1e-9,
        )
        frames.append(frame)
        facts.append(gazebo_sensor_fact_json(frame, p))
    frames = tuple(frames)
    return OnboardLiveVlmRequest(
        replace(
            base,
            frames=frames,
            ledger=replace(base.ledger, frames=frames),
            requested_at_s=float(count),
            deadline_at_s=float(count + 30),
            factual_sensor_state_json=tuple(facts),
        ),
        max_total_pixels=200704,
    )


@pytest.mark.parametrize(
    "count,grid,expected", [(2, [1, 18, 22], 202752), (4, [1, 14, 20], 286720)]
)
def test_actual_smart_resize_overflow_is_rejected_with_records_before_generation(
    count, grid, expected
):
    backend = _backend()
    backend.processor.forced_grid = [grid] * count
    with pytest.raises(LiveVlmPreprocessingError, match="actual processor grids exceed") as caught:
        backend.infer(_large_request(count))
    records = caught.value.to_mapping()
    assert records["actual_processor_total_pixels"] == expected > 200704
    assert records["actual_image_grid_thw"] == [grid] * count
    assert records["actual_processor_pixel_budget_verified"] is False
    assert len(records["input_tensors"]["image_grid_thw"]["sha256"]) == 64
    assert records["input_tensors"]["image_grid_thw"]["shape"] == [count, 3]
    assert json.loads(str(caught.value).split("; preprocessing=", 1)[1]) == records
    assert backend.model.calls == []


@pytest.mark.parametrize("count,grid", [(2, [1, 16, 22]), (4, [1, 12, 16])])
def test_two_and_four_frames_actual_grid_total_is_verified(count, grid):
    backend = _backend()
    backend.processor.forced_grid = [grid] * count
    metrics = backend.infer(_large_request(count))["metrics"]
    assert metrics["actual_processor_total_pixels"] == count * grid[1] * grid[2] * 256 <= 200704
    assert metrics["processor_images_kwargs"] == {"min_pixels": 1024, "max_pixels": 200704 // count}
    assert metrics["actual_processor_pixel_budget_verified"] is True
    assert len(backend.model.calls) == 1


@pytest.mark.parametrize(
    "grid",
    [
        [[1, 2, 2]],
        [[0, 2, 2], [1, 2, 2]],
        [[2, 2, 2], [1, 2, 2]],
        [[1.0, 2.0, 2.0], [1.0, 2.0, 2.0]],
        [[True, True, True], [True, True, True]],
    ],
)
def test_missing_malformed_or_non_image_temporal_grid_is_not_silently_accepted(grid):
    backend = _backend()
    backend.processor.forced_grid = grid
    with pytest.raises(LiveVlmPreprocessingError):
        backend.infer(OnboardLiveVlmRequest(_live_input()))
    assert backend.model.calls == []


def test_minimum_processor_grid_cannot_fit_too_small_budget():
    backend = _backend()
    with pytest.raises(LiveVlmPreprocessingError, match="minimum processor grids"):
        backend.infer(OnboardLiveVlmRequest(_live_input(), max_total_pixels=100))
    assert backend.processor.messages is None and backend.model.calls == []


@pytest.mark.parametrize("count", [2, 4])
def test_pinned_actual_processor_cpu_respects_total_grid_budget(count):
    transformers = pytest.importorskip("transformers")
    if not hasattr(transformers, "Qwen3VLProcessor"):
        pytest.skip("this CPU environment does not contain the pinned Qwen3-VL processor")
    snapshot = (
        Path(__file__).resolve().parents[1]
        / "artifacts/cache/huggingface-vlm/models--Qwen--Qwen3-VL-2B-Instruct/snapshots"
        / "89644892e4d85e24eaac8bacfd4f463576704203"
    )
    if not snapshot.is_dir():
        pytest.skip("pinned local processor snapshot absent; never download")
    processor = transformers.AutoProcessor.from_pretrained(
        str(snapshot), local_files_only=True, trust_remote_code=False
    )

    class CpuProcessor:
        image_processor = processor.image_processor
        tokenizer = processor.tokenizer

        def apply_chat_template(self, *args, **kwargs):
            return _Inputs(processor.apply_chat_template(*args, **kwargs))

    backend = _backend(max_input_tokens=8192)
    backend.processor = CpuProcessor()
    metrics = backend.infer(_large_request(count))["metrics"]
    assert len(metrics["actual_image_grid_thw"]) == count
    assert metrics["actual_processor_total_pixels"] <= 200704
    assert metrics["actual_processor_pixel_budget_verified"] is True
    assert len(backend.model.calls) == 1  # fake model only: actual processor, no base weights/CUDA
