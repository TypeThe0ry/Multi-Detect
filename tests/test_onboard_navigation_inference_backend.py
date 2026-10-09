"""Pure-Python orchestration stand-ins, NOT native processor/model evidence."""

from __future__ import annotations

import builtins
import copy
import gc
import hashlib
import json
import math
import struct
import subprocess
import sys
import weakref
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_onboard_navigation_inference import _request, _target

from multidetect.autonomy.onboard_navigation_actor import ACTOR_SYSTEM_PROMPT
from multidetect.autonomy.serialization import canonical_json
from scripts import onboard_navigation_inference_backend as backend_module
from scripts.onboard_navigation_inference_backend import (
    NavigationBackendRejected,
    NavigationEngineIdentity,
    PreloadedNavigationInferenceBackend,
)


def _flatten(data):
    if isinstance(data, list):
        return [value for part in data for value in _flatten(part)]
    return [data]


def _mapped(data, operation):
    if isinstance(data, list):
        return [_mapped(value, operation) for value in data]
    return operation(data)


class PythonTensorStandIn:
    """Only the CPU-checker/record/generate surface; never a real Torch tensor."""

    def __init__(self, data, dtype="torch.int64", device="cpu", requires_grad=False):
        self.data = copy.deepcopy(data)
        self.dtype = dtype
        self.device = SimpleNamespace(type=device.split(":")[0])
        self.requires_grad = requires_grad

    @property
    def shape(self):
        shape, current = [], self.data
        while isinstance(current, list):
            shape.append(len(current))
            current = current[0] if current else None
        return tuple(shape)

    @property
    def ndim(self):
        return len(self.shape)

    def __getitem__(self, index):
        return type(self)(self.data[index], self.dtype, self.device.type, self.requires_grad)

    def __lt__(self, value):
        return type(self)(_mapped(self.data, lambda item: item < value), "bool")

    def __eq__(self, value):
        return type(self)(_mapped(self.data, lambda item: item == value), "bool")

    def __or__(self, other):
        def combine(left, right):
            if isinstance(left, list):
                return [combine(a, b) for a, b in zip(left, right, strict=True)]
            return left or right

        return type(self)(combine(self.data, other.data), "bool")

    def any(self):
        return type(self)(any(_flatten(self.data)), "bool")

    def all(self):
        return type(self)(all(_flatten(self.data)), "bool")

    def item(self):
        return self.data

    def detach(self):
        return self

    def contiguous(self):
        return self

    def to(self, device):
        assert device == "cuda:0"  # a string only; no CUDA exists in this stand-in
        return type(self)(self.data, self.dtype, device, self.requires_grad)

    def cpu(self):
        return self if self.device.type == "cpu" else type(self)(self.data, self.dtype)

    def tolist(self):
        return copy.deepcopy(self.data)

    def view(self, dtype):
        assert dtype == "torch.uint8"
        values = _flatten(self.data)
        code = "q" if self.dtype == "torch.int64" else "f"
        payload = struct.pack("<" + code * len(values), *values)
        return SimpleNamespace(numpy=lambda: SimpleNamespace(tobytes=lambda: payload))


class ClockStandIn:
    def __init__(self, now=10.0):
        self.now = now

    def __call__(self):
        return self.now


class TorchStandIn:
    Tensor = PythonTensorStandIn
    int64 = "torch.int64"
    float32 = "torch.float32"
    uint8 = "torch.uint8"

    def __init__(self):
        self.inference_active = False
        self.inference_entries = 0
        self.synchronizations = 0
        self.cuda = SimpleNamespace(synchronize=self.synchronize)

    def synchronize(self):
        self.synchronizations += 1

    @staticmethod
    def isfinite(tensor):
        return PythonTensorStandIn(_mapped(tensor.data, math.isfinite), "bool")

    @contextmanager
    def inference_mode(self):
        assert not self.inference_active
        self.inference_entries += 1
        self.inference_active = True
        try:
            yield
        finally:
            self.inference_active = False


class ProcessorStandIn:
    def __init__(self, clock, raw=None):
        self.clock = clock
        self.raw = canonical_json(_target()) if raw is None else raw
        self.image_processor = SimpleNamespace(patch_size=16, temporal_patch_size=2)
        self.tokenizer = SimpleNamespace(decode=self.decode)
        self.calls, self.decode_calls, self.input_refs = [], [], []
        self.process_hook = self.decode_hook = None
        self.prefix_tokens = 4
        self.prefix_ids = [31, 32, 33, 34]

    def apply_chat_template(self, messages, **kwargs):
        assert kwargs == {
            "add_generation_prompt": True,
            "tokenize": True,
            "return_dict": True,
            "return_tensors": "pt",
            "images_kwargs": {"min_pixels": 1024, "max_pixels": 50176},
        }
        assert [message["role"] for message in messages] == ["system", "user"]
        assert messages[0]["content"][0]["text"] == ACTOR_SYSTEM_PROMPT
        images = [
            item["image"]
            for message in messages
            for item in message["content"]
            if item["type"] == "image"
        ]
        texts = [
            item["text"]
            for message in messages
            for item in message["content"]
            if item["type"] == "text"
        ]
        self.calls.append(
            {
                "texts": texts,
                "rgb": [image.tobytes() for image in images],
                "kwargs": copy.deepcopy(kwargs),
                "images": images,
            }
        )
        ids = self.prefix_ids if self.prefix_tokens == 4 else list(range(self.prefix_tokens))
        inputs = {
            "input_ids": PythonTensorStandIn([ids]),
            "attention_mask": PythonTensorStandIn([[1] * self.prefix_tokens]),
            "pixel_values": PythonTensorStandIn(
                [[0.25] * 1536 for _ in range(4 * len(images))], dtype="torch.float32"
            ),
            "image_grid_thw": PythonTensorStandIn([[1, 2, 2] for _ in images]),
        }
        if self.process_hook is not None:
            self.process_hook(inputs)
        self.input_refs.extend(
            weakref.ref(value)
            for value in inputs.values()
            if isinstance(value, PythonTensorStandIn)
        )
        return inputs

    def decode(self, ids, **kwargs):
        assert kwargs == {"skip_special_tokens": True, "clean_up_tokenization_spaces": False}
        self.decode_calls.append(list(ids))
        if self.decode_hook is not None:
            self.decode_hook()
        return self.raw


class ModelStandIn:
    def __init__(self, torch, clock):
        self.torch, self.clock = torch, clock
        self.active_adapters = ["default"]
        self.disable_adapters = self._forbidden  # real Transformers base exposes a method
        self.layers = [
            SimpleNamespace(
                lora_A={"default": object()},
                lora_B={"default": object()},
                disable_adapters=False,
                active_adapters=["default"],
            )
        ]
        self.calls, self.output_refs = [], []
        self.eval_calls = 0
        self.suffix_ids = [71, 72, 73]
        self.generate_hook = None

    @staticmethod
    def _forbidden(*args, **kwargs):
        raise AssertionError("No training, optimizer, adapter mutation or flight action")

    backward = train = step = _forbidden

    def named_modules(self):
        return [("base", self), *[("lora-" + str(i), layer) for i, layer in enumerate(self.layers)]]

    def eval(self):
        self.eval_calls += 1
        return self

    def generate(self, **kwargs):
        assert self.torch.inference_active and self.eval_calls > 0
        for key in ("input_ids", "attention_mask", "pixel_values", "image_grid_thw"):
            assert kwargs[key].device.type == "cuda"  # stand-in label, NOT a native device
        self.calls.append(
            {
                "prefix_ids": kwargs["input_ids"].tolist()[0],
                **{
                    key: kwargs[key]
                    for key in ("do_sample", "max_new_tokens", "max_time", "use_cache")
                },
            }
        )
        self.clock.now += 0.25
        output = PythonTensorStandIn([kwargs["input_ids"].tolist()[0] + self.suffix_ids])
        if self.generate_hook is not None:
            output = self.generate_hook(output, kwargs)
        self.output_refs.append(weakref.ref(output))
        return output


@pytest.fixture(autouse=True)
def no_heavy_imports(monkeypatch):
    original = builtins.__import__
    denied = {"torch", "transformers", "peft", "safetensors", "datasets", "cuda"}

    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in denied:
            raise AssertionError("Unexpected heavy import: " + name)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)


def _backend(request=None, raw=None, **injected):
    request = request or _request()
    clock, torch = ClockStandIn(), TorchStandIn()
    processor = ProcessorStandIn(clock, raw=raw)
    model = ModelStandIn(torch, clock)
    engine = SimpleNamespace(
        model=model,
        processor=processor,
        torch=torch,
        encode=ModelStandIn._forbidden,
        optimizer=ModelStandIn._forbidden,
    )
    identity = NavigationEngineIdentity(
        request.model_sha256, request.adapter_sha256, request.adapter_config_sha256
    )
    backend = PreloadedNavigationInferenceBackend(engine, identity, clock=clock, **injected)
    return backend, request, clock


def _rejection(backend, request, match=None):
    with pytest.raises(NavigationBackendRejected, match=match) as caught:
        backend.infer(request)
    return json.loads(caught.value.diagnostics_json)


@pytest.mark.parametrize("two_frames", [False, True])
def test_default_functions_share_exact_actor_prefix_and_cpu_checker_metadata(
    two_frames, monkeypatch
):
    from scripts import onboard_navigation_balanced_encoding as gold_encoder
    from scripts import train_onboard_vlm_navigation_pilot as pilot
    from scripts import verify_onboard_navigation_actor_inputs as official_checker

    monkeypatch.setattr(gold_encoder, "encode_navigation_sample", ModelStandIn._forbidden)
    monkeypatch.setattr(pilot.NavigationBackend, "encode", ModelStandIn._forbidden)
    monkeypatch.setattr(pilot.NavigationBackend, "encode_messages", ModelStandIn._forbidden)
    calls = []
    original = official_checker.validate_prefix_inputs

    def checker_spy(*args, **kwargs):
        calls.append({key: value for key, value in kwargs.items() if key != "torch_module"})
        return original(*args, **kwargs)

    monkeypatch.setattr(official_checker, "validate_prefix_inputs", checker_spy)
    backend, request, _clock = _backend(_request(two_frames))
    result = backend.infer(request)
    actual = result["metrics"]["actual_prefix"]
    assert len(calls) == 1 and calls[0]["image_count"] == len(request.actor_input.frames)
    assert calls[0]["tensor_record"] is pilot.tensor_record
    assert set(actual["tensors"]) == {
        "input_ids",
        "attention_mask",
        "pixel_values",
        "image_grid_thw",
    }
    assert actual["actual_image_pixels"] == [1024] * len(request.actor_input.frames)
    assert actual["actual_image_grid_thw"] == [[1, 2, 2]] * len(request.actor_input.frames)
    assert actual["prefix_tokens"] == 4
    processor = backend.engine.processor
    assert processor.calls[0]["rgb"] == [frame.image_bytes for frame in request.actor_input.frames]
    assert request.actor_input.prompt_original in processor.calls[0]["texts"]
    assert not any(
        "source-" in text or "fixture-task" in text for text in processor.calls[0]["texts"]
    )
    for image in processor.calls[0]["images"]:
        with pytest.raises(ValueError):
            image.tobytes()  # frozen processor helper closed fresh images on exit


def test_expected_hashes_never_claim_exact_load_sensor_authenticity_or_flight():
    backend, request, _ = _backend()
    result = backend.infer(request)
    for key in (
        "exact_model_load_verified_here",
        "sensor_authenticity_verified_here",
        "native_processor_or_model_acceptance_claimed",
        "absolute_process_watchdog_owned",
        "transport_authority",
        "execution_authority",
        "optimizer_performed",
    ):
        assert result["metrics"][key] is False
    assert result["metrics"]["result_accepted"] is True
    assert result["navigation_result"].to_mapping()["completion_evidence_created"] is False
    assert backend.engine.model.eval_calls == 1
    assert backend.engine.torch.inference_entries == 1
    assert backend.engine.torch.inference_active is False
    assert backend.engine.torch.synchronizations == 2
    assert backend.engine.model.calls[0] == {
        "prefix_ids": [31, 32, 33, 34],
        "do_sample": False,
        "max_new_tokens": 192,
        "max_time": 4.0,
        "use_cache": True,
    }


@pytest.mark.parametrize("name", ["model_sha256", "adapter_sha256", "adapter_config_sha256"])
def test_each_wrong_expected_file_hash_rejects_before_processing_or_generation(name):
    backend, request, _ = _backend()
    backend.identity = replace(backend.identity, **{name: "e" * 64})
    diagnostic = _rejection(backend, request, "expected preloaded engine identity")
    assert not backend.engine.processor.calls and not backend.engine.model.calls
    assert diagnostic["generation_attempted"] is False


@pytest.mark.parametrize(
    "case",
    [
        "no_layers",
        "disabled",
        "disabled_zero",
        "layer_callable_disable",
        "wrong_model_active",
        "wrong_layer_active",
        "multiple_model_active",
        "multiple_layer_active",
        "missing_a",
        "missing_b",
        "other_a",
        "other_b",
        "no_model",
        "missing_layer_disable",
    ],
)
def test_actual_default_lora_state_is_required_not_only_declared_identity(case):
    backend, request, _ = _backend()
    model = backend.engine.model
    layer = model.layers[0]
    if case == "no_layers":
        model.layers.clear()
    elif case == "disabled":
        layer.disable_adapters = True
    elif case == "disabled_zero":
        layer.disable_adapters = 0
    elif case == "layer_callable_disable":
        layer.disable_adapters = model._forbidden
    elif case in {"wrong_model_active", "multiple_model_active"}:
        model.active_adapters = ["other"] if case.startswith("wrong") else ["default", "other"]
    elif case in {"wrong_layer_active", "multiple_layer_active"}:
        layer.active_adapters = ["other"] if case.startswith("wrong") else ["default", "other"]
    elif case in {"missing_a", "missing_b", "other_a", "other_b"}:
        setattr(
            layer, "lora_" + case[-1].upper(), {} if case.startswith("missing") else {"other": 1}
        )
    elif case == "no_model":
        del backend.engine.model
    else:
        del layer.disable_adapters
    diagnostic = _rejection(backend, request)
    assert diagnostic["generation_attempted"] is False
    assert not model.calls and not backend.engine.processor.calls


@pytest.mark.parametrize(
    "case",
    [
        "gpu",
        "autograd",
        "missing",
        "extra",
        "float_ids",
        "negative_ids",
        "bad_mask",
        "float_pixels",
        "nonfinite_pixels",
        "bad_grid",
        "too_many_pixels",
        "too_many_tokens",
    ],
)
def test_default_cpu_prefix_checker_fail_closed_stand_in_shapes_and_budgets(case):
    backend, request, _ = _backend()

    def corrupt(inputs):
        if case == "gpu":
            inputs["input_ids"].device.type = "cuda"
        elif case == "autograd":
            inputs["pixel_values"].requires_grad = True
        elif case == "missing":
            del inputs["attention_mask"]
        elif case == "extra":
            inputs["labels"] = PythonTensorStandIn([[-100] * 4])
        elif case == "float_ids":
            inputs["input_ids"].dtype = "torch.float32"
        elif case == "negative_ids":
            inputs["input_ids"].data[0][0] = -1
        elif case == "bad_mask":
            inputs["attention_mask"].data[0][0] = 2
        elif case == "float_pixels":
            inputs["pixel_values"].dtype = "torch.float64"
        elif case == "nonfinite_pixels":
            inputs["pixel_values"].data[0][0] = float("nan")
        elif case == "bad_grid":
            inputs["image_grid_thw"].data[0][0] = 2
        elif case == "too_many_pixels":
            inputs["image_grid_thw"].data[0] = [1, 100, 100]

    if case == "too_many_tokens":
        backend.engine.processor.prefix_tokens = 4097
    backend.engine.processor.process_hook = corrupt
    diagnostic = _rejection(backend, request)
    assert diagnostic["generation_attempted"] is False and not backend.engine.model.calls


@pytest.mark.parametrize("case", ["changed_prefix", "float", "one_dim", "batch", "empty_suffix"])
def test_actual_generation_must_preserve_cpu_prefix_and_int64_single_row(case):
    backend, request, _ = _backend()

    def mutate(output, _kwargs):
        if case == "changed_prefix":
            output.data[0][0] += 1
        elif case == "float":
            output.dtype = "torch.float32"
        elif case == "one_dim":
            output.data = output.data[0]
        elif case == "batch":
            output.data.append(list(output.data[0]))
        else:
            output.data[0] = output.data[0][:4]
        return output

    backend.engine.model.generate_hook = mutate
    diagnostic = _rejection(backend, request)
    assert diagnostic["generation_returned"] is True and diagnostic["result_accepted"] is False
    assert not backend.engine.processor.decode_calls


@pytest.mark.parametrize(
    "ids",
    [[-1], [True], [71] * 192, [71] * 193],
    ids=["negative", "bool", "cap-exhausted", "over-cap"],
)
def test_actual_suffix_ids_count_and_cap_are_retained_on_rejection(ids):
    backend, request, _ = _backend()
    backend.engine.model.suffix_ids = ids
    diagnostic = _rejection(backend, request)
    assert diagnostic["generated_token_ids"] == ids
    assert diagnostic["generated_tokens"] == len(ids)
    assert diagnostic["raw_output"] == backend.engine.processor.raw
    assert diagnostic["max_new_tokens_reached"] == (len(ids) >= 192)
    assert diagnostic["result_accepted"] is False


def test_nonexhausted_191_suffix_and_4096_prefix_caps_are_separate():
    backend, request, _ = _backend()
    backend.engine.processor.prefix_tokens = 4096
    backend.engine.model.suffix_ids = list(range(191))
    output = backend.infer(request)
    assert output["navigation_result"].prefix_tokens == 4096
    assert output["navigation_result"].generated_tokens == 191
    assert backend.engine.processor.decode_calls == [list(range(191))]


@pytest.mark.parametrize(
    "target",
    [
        _target(translation_m=None),
        _target(kind="turn_yaw", translation_m=None, yaw_delta_rad=None),
        _target(kind="turn_yaw", translation_m=None, yaw_delta_rad=-1.5),
        _target(kind="turn_yaw", translation_m=None, yaw_delta_rad=1.5),
        _target(kind="stop", translation_m=None),
    ],
)
def test_unknown_stop_and_yaw_semantics_survive_raw_decode_without_flight_mapping(target):
    raw = json.dumps(target, indent=2) + "\n"
    backend, request, _ = _backend(raw=raw)
    output = backend.infer(request)
    assert output["raw_output"] == raw
    assert output["navigation_result"].target.to_mapping() == target
    assert output["metrics"]["raw_output_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert backend.engine.processor.decode_calls == [[71, 72, 73]]
    for key in ("confidence", "reason", "flight_plan", "task_action", "task_complete"):
        assert key not in output["metrics"]
        assert key not in output["navigation_result"].to_mapping()


@pytest.mark.parametrize(
    "raw",
    [
        "not JSON",
        "[]",
        "{}",
        '{"kind":"stop","kind":"translate"}',
        json.dumps(_target(confidence=1.0)),
        json.dumps(_target(translation_m=[True, 0, 0])),
        json.dumps(_target(translation_m=[float("nan"), 0, 0])),
        json.dumps(_target(kind="land", translation_m=None)),
    ],
)
def test_malformed_raw_output_keeps_raw_and_suffix_diagnostics(raw):
    backend, request, _ = _backend(raw=raw)
    diagnostic = _rejection(backend, request)
    assert diagnostic["raw_output"] == raw
    assert diagnostic["generated_token_ids"] == [71, 72, 73]
    assert diagnostic["raw_output_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert diagnostic["generation_attempted"] is diagnostic["generation_returned"] is True
    assert diagnostic["result_accepted"] is False


@pytest.mark.parametrize("raw", [None, True, b"not decoded text", {"kind": "stop"}])
def test_nontext_decoder_result_rejects_and_preserves_actual_suffix(raw):
    backend, request, _ = _backend()
    backend.engine.processor.raw = raw
    diagnostic = _rejection(backend, request, "decode must return UTF-8 text")
    assert diagnostic["generated_token_ids"] == [71, 72, 73]
    assert diagnostic["generated_tokens"] == 3
    assert diagnostic["result_accepted"] is False


@pytest.mark.parametrize("phase", ["preprocess", "generate", "decode", "source_age"])
def test_every_expensive_phase_and_oldest_frame_age_can_reject_expired_results(phase):
    request = _request(max_frame_age_s=1.3) if phase == "source_age" else _request()
    backend, request, clock = _backend(request)
    if phase == "preprocess":
        backend.engine.processor.process_hook = lambda _inputs: setattr(clock, "now", 14)
    elif phase == "generate":

        def expire(output, _kwargs):
            clock.now = 14
            return output

        backend.engine.model.generate_hook = expire
    else:
        backend.engine.processor.decode_hook = lambda: setattr(
            clock, "now", 10.4 if phase == "source_age" else 14
        )
    diagnostic = _rejection(backend, request)
    assert diagnostic["result_accepted"] is False
    if phase == "preprocess":
        assert diagnostic["generation_attempted"] is False and not backend.engine.model.calls
        assert "actual_prefix" in diagnostic
    else:
        assert diagnostic["generation_returned"] is True
        assert diagnostic["raw_output"] == backend.engine.processor.raw
        assert diagnostic["generated_token_ids"] == [71, 72, 73]


def test_cooperative_max_time_respects_oldest_rgb_freshness_not_only_deadline():
    request = _request(max_frame_age_s=1.5)
    backend, request, _ = _backend(request)
    backend.infer(request)
    assert backend.engine.model.calls[0]["max_time"] == 0.5


@pytest.mark.parametrize("case", ["expired", "future", "scope_mutation", "generation_mutation"])
def test_request_and_scope_time_mutations_reject_without_generation(case):
    backend, request, clock = _backend()
    if case == "expired":
        clock.now = 14
    elif case == "future":
        clock.now = 9
    elif case == "scope_mutation":
        object.__setattr__(request.scope, "clock_epoch", "unbound-epoch")
    else:
        object.__setattr__(request, "generation", 8)
    diagnostic = _rejection(backend, request)
    assert diagnostic["generation_attempted"] is False
    assert not backend.engine.model.calls and not backend.engine.processor.calls


def test_request_scope_mutation_during_generate_invalidates_raw_result():
    backend, request, _ = _backend()

    def change_scope(output, _kwargs):
        object.__setattr__(request.scope, "clock_epoch", "another-epoch")
        return output

    backend.engine.model.generate_hook = change_scope
    diagnostic = _rejection(backend, request)
    assert diagnostic["raw_output"] == backend.engine.processor.raw
    assert diagnostic["generated_tokens"] == 3 and diagnostic["result_accepted"] is False


@pytest.mark.parametrize("phase", ["process", "generate", "decode"])
@pytest.mark.parametrize("mutation", ["disabled", "active", "missing_default"])
def test_actual_lora_state_drift_at_each_phase_is_rejected(phase, mutation):
    backend, request, _ = _backend()
    layer = backend.engine.model.layers[0]

    def change_state():
        if mutation == "disabled":
            layer.disable_adapters = True
        elif mutation == "active":
            layer.active_adapters = ["other"]
        else:
            layer.lora_A.clear()

    if phase == "process":
        backend.engine.processor.process_hook = lambda _inputs: change_state()
    elif phase == "generate":

        def mutate(output, _kwargs):
            change_state()
            return output

        backend.engine.model.generate_hook = mutate
    else:
        backend.engine.processor.decode_hook = change_state
    diagnostic = _rejection(backend, request)
    assert diagnostic["result_accepted"] is False
    if phase == "process":
        assert not backend.engine.model.calls and diagnostic["generation_attempted"] is False
    else:
        assert diagnostic["generation_returned"] is True
        assert diagnostic["generated_token_ids"] == [71, 72, 73]
        assert diagnostic["raw_output"] == backend.engine.processor.raw


@pytest.mark.parametrize("phase", ["process", "generate", "decode"])
def test_engine_errors_preserve_phase_diagnostics_and_exit_inference_mode(phase):
    backend, request, _ = _backend()

    def fail(*args):
        raise RuntimeError("stand-in " + phase + " failure")

    if phase == "process":
        backend.engine.processor.process_hook = fail
    elif phase == "generate":
        backend.engine.model.generate_hook = fail
    else:
        backend.engine.processor.decode_hook = fail
    diagnostic = _rejection(backend, request, "stand-in")
    assert diagnostic["error_type"] == "RuntimeError"
    assert diagnostic["generation_attempted"] == (phase != "process")
    assert diagnostic["generation_returned"] == (phase == "decode")
    if phase == "decode":
        assert diagnostic["generated_token_ids"] == [71, 72, 73]
    assert backend.engine.torch.inference_active is False


def test_no_cross_request_tensor_cache_even_when_results_remain_referenced():
    backend, request, _clock = _backend()
    results = [backend.infer(request)]
    gc.collect()
    assert all(reference() is None for reference in backend.engine.processor.input_refs)
    assert all(reference() is None for reference in backend.engine.model.output_refs)
    refs = list(backend.engine.processor.input_refs)

    def assert_previous_released(_inputs):
        assert all(reference() is None for reference in refs)

    backend.engine.processor.process_hook = assert_previous_released
    results.append(backend.infer(replace(request, generation=8)))
    gc.collect()
    assert all(reference() is None for reference in backend.engine.processor.input_refs)
    assert results[0]["navigation_result"].request.generation == 7
    assert results[1]["navigation_result"].request.generation == 8
    assert len(backend.engine.processor.calls) == 2
    assert (
        backend.engine.processor.calls[0]["images"][0]
        is not (backend.engine.processor.calls[1]["images"][0])
    )


@pytest.mark.parametrize("bad", [True, "bad", "B" * 64, " " + "b" * 64])
def test_engine_identity_requires_canonical_file_shas(bad):
    with pytest.raises(ValueError):
        NavigationEngineIdentity(bad, "c" * 64, "d" * 64)


def test_wrong_request_type_and_noncallable_injected_operations_are_rejected():
    backend, request, _ = _backend()
    with pytest.raises(TypeError):
        backend.infer(object())
    with pytest.raises(TypeError):
        PreloadedNavigationInferenceBackend(backend.engine, backend.identity, clock=0)
    with pytest.raises(TypeError):
        PreloadedNavigationInferenceBackend(backend.engine, "declared-only")
    for operation in ("prefix_processor", "prefix_checker"):
        with pytest.raises(TypeError):
            PreloadedNavigationInferenceBackend(
                backend.engine, backend.identity, **{operation: None}
            )
    assert request.canonical_hash


def test_script_import_is_guarded_against_heavy_model_dependencies():
    root = Path(__file__).resolve().parents[1]
    probe = """
import builtins, sys
original = builtins.__import__
denied = {'torch', 'transformers', 'peft', 'safetensors', 'datasets', 'cuda'}
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in denied:
        raise AssertionError('unexpected heavy import: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import scripts.onboard_navigation_inference_backend
assert not denied.intersection(sys.modules)
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe, str(root), str(root / "src")],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_default_callable_identity_is_the_frozen_prefix_only_path():
    backend, _request_value, _ = _backend()
    assert backend.prefix_processor is backend_module._default_prefix_processor
    assert backend.prefix_checker is backend_module._default_prefix_checker
