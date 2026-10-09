"""Tiny CPU tensors and stand-in fusion trunk; not actual model/Gazebo evidence."""

from __future__ import annotations

import copy
import gc
import json
import subprocess
import sys
import weakref
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_onboard_navigation_inference import _request, _scope

from scripts import onboard_navigation_learned_readout_backend as backend
from scripts import onboard_navigation_stable_readout as stable
from scripts.onboard_navigation_inference_backend import (
    NavigationBackendRejected,
    NavigationEngineIdentity,
)

torch = pytest.importorskip("torch")


@pytest.fixture(autouse=True)
def no_cuda(monkeypatch):
    assert not torch.cuda.is_initialized()
    monkeypatch.setattr(torch.cuda, "_lazy_init", lambda *a, **k: pytest.fail("No CUDA fixture"))
    yield
    assert not torch.cuda.is_initialized()


class Clock:
    def __init__(self):
        self.now = 10.0

    def __call__(self):
        return self.now


class Qwen3VLModel(torch.nn.Module):
    """Only a stand-in returning tiny predetermined prefix hidden states."""

    def __init__(self, clock):
        super().__init__()
        self.marker = torch.nn.Parameter(torch.zeros(1), requires_grad=False)
        self.clock, self.calls, self.hook = clock, [], None

    def forward(self, **kwargs):
        assert torch.is_inference_mode_enabled()
        assert set(kwargs) == backend.FOUR_PREFIX | {
            "use_cache",
            "past_key_values",
            "return_dict",
            "output_hidden_states",
        }
        assert kwargs["use_cache"] is False and kwargs["past_key_values"] is None
        assert kwargs["output_hidden_states"] is False and kwargs["return_dict"] is True
        assert all(kwargs[k].device.type == "cpu" for k in backend.FOUR_PREFIX)
        self.calls.append({k: kwargs[k].clone() for k in backend.FOUR_PREFIX})
        hidden = torch.zeros(1, kwargs["input_ids"].shape[1], 2048)
        hidden[0, :, 0] = torch.arange(1, hidden.shape[1] + 1)
        if self.hook:
            self.hook(hidden)
        self.clock.now += 0.1
        return SimpleNamespace(last_hidden_state=hidden)


class Model:
    def __init__(self, clock):
        self.trunk = Qwen3VLModel(clock).eval()
        self.active_adapters, self.training = ["default"], False
        self.layers = [
            SimpleNamespace(
                lora_A={"default": object()},
                lora_B={"default": object()},
                disable_adapters=False,
                active_adapters=["default"],
            )
        ]

    def named_modules(self):
        return iter([(str(i), layer) for i, layer in enumerate(self.layers)])

    def parameters(self):
        return self.trunk.parameters()

    def get_base_model(self):
        return SimpleNamespace(model=self.trunk)

    def generate(self, *args, **kwargs):
        pytest.fail("Learned readout must never call generate")


class Processor:
    def __init__(self, clock):
        self.clock, self.calls, self.hook, self.refs = clock, [], None, []
        self.image_processor = SimpleNamespace(patch_size=16, temporal_patch_size=2)
        self.tokenizer = SimpleNamespace(decode=lambda *a, **k: pytest.fail("No text decode"))

    def apply_chat_template(self, messages, **kwargs):
        assert [r["role"] for r in messages] == ["system", "user"]
        assert kwargs == {
            "add_generation_prompt": True,
            "tokenize": True,
            "return_dict": True,
            "return_tensors": "pt",
            "images_kwargs": {"min_pixels": 1024, "max_pixels": 50176},
        }
        images = [v["image"] for m in messages for v in m["content"] if v["type"] == "image"]
        texts = [v["text"] for m in messages for v in m["content"] if v["type"] == "text"]
        self.calls.append(
            {"images": images, "text": texts, "pixels": [im.tobytes() for im in images]}
        )
        value = {
            "input_ids": torch.tensor([[31, 32, 33, 34]], dtype=torch.int64),
            "attention_mask": torch.tensor([[1, 0, 1, 0]], dtype=torch.int64),
            "pixel_values": torch.full((4 * len(images), 1536), 0.25),
            "image_grid_thw": torch.tensor([[1, 2, 2]] * len(images), dtype=torch.int64),
        }
        if self.hook:
            self.hook(value)
        self.refs = [weakref.ref(v) for v in value.values() if isinstance(v, torch.Tensor)]
        self.clock.now += 0.1
        return value


def metadata(head, norm):
    return {
        "contract": stable.CONTRACT,
        "kind": "readout",
        "feature_dim": 2048,
        "normalization_train_ids": list(norm.sample_ids),
        "epsilon": norm.epsilon,
        "optimizer_state_saved": False,
        "small_or_constant_scale_rule": "population_std_below_epsilon_uses_one",
        "training_report": {"training_sample_ids": list(norm.sample_ids), "success": True},
        "tensors": {
            name: stable.tensor_record(value)
            for name, value in {
                "weight": head.weight,
                "bias": head.bias,
                "center": norm.center,
                "scale": norm.scale,
            }.items()
        },
    }


def fixture(**operations):
    clock = Clock()
    engine = SimpleNamespace(model=Model(clock), processor=Processor(clock), torch=torch)
    head = torch.nn.Linear(2048, 6, dtype=torch.float32).eval()
    with torch.no_grad():
        head.weight.zero_()
        head.bias.copy_(torch.tensor([4.0, 1.0, 0.0, -1.0, 2.0, 0.7]))
        head.weight[4, 0] = 0.1
    for p in head.parameters():
        p.requires_grad_(False)
    center, scale = torch.zeros(2048), torch.ones(2048)
    center[0], scale[0] = 1, 2
    norm = stable.TrainNormalization(center, scale, ("fixture-train-0", "fixture-train-1"), 1e-6)
    md = metadata(head, norm)
    identity = backend.SavedReadoutIdentity("e" * 64, "f" * 64, "a" * 64)
    service = backend.PreloadedLearnedNavigationBackend(
        engine,
        NavigationEngineIdentity("b" * 64, "c" * 64, "d" * 64),
        head,
        norm,
        identity,
        md,
        clock=clock,
        **operations,
    )
    return SimpleNamespace(
        clock=clock,
        engine=engine,
        head=head,
        norm=norm,
        metadata=md,
        identity=identity,
        backend=service,
    )


def diagnostics(exc):
    return json.loads(exc.value.diagnostics_json)


@pytest.mark.parametrize("two_frames", [False, True])
def test_actual_default_prefix_fusion_stable_predict_no_generation(two_frames):
    f = fixture()
    request = _request(two_frames=two_frames)
    value = f.backend.infer(request)
    result = value["learned_readout_result"]
    assert result.target.translation_m[0] == pytest.approx(2.1)
    assert result.target.translation_m[0] != 3.0
    assert result.to_mapping()["contract"] == backend.CONTRACT
    assert result.to_mapping()["generation"] == request.generation
    assert result.to_mapping()["source_binding_sha256"] == request.source_binding_sha256
    assert not {"generated_token_ids", "generated_tokens", "confidence"}.intersection(
        result.to_mapping()
    )
    assert result.to_mapping()["text_generation_performed"] is False
    assert result.to_mapping()["optimizer_performed"] is False
    assert all(result.to_mapping()[k] is False for k in backend.FALSE_CLAIMS)
    assert len(f.engine.model.trunk.calls) == 1
    assert len(f.engine.processor.calls) == 1
    assert f.engine.processor.calls[0]["text"][1] == request.actor_input.prompt_original
    assert len(f.engine.processor.calls[0]["images"]) == (2 if two_frames else 1)
    for image in f.engine.processor.calls[0]["images"]:
        with pytest.raises(ValueError):
            image.tobytes()
    assert result.to_mapping()["actual_feature"]["shape"] == [1, 2048]
    assert value["metrics"]["result_accepted"] is True
    result.validate_binding(current_scope=request.scope, now_s=f.clock.now)
    gc.collect()
    assert all(ref() is None for ref in f.engine.processor.refs)


@pytest.mark.parametrize("selected", range(4))
def test_four_selected_classes_and_unused_scales(selected):
    f = fixture()
    with torch.no_grad():
        f.head.bias[:4].fill_(-2)
        f.head.bias[selected] = 8
        if selected == 3:
            f.head.bias[4:] = -5  # unused finite scales survive verbatim
    f.metadata = metadata(f.head, f.norm)
    f.backend.expected_head_state = backend._expected_head_state(f.metadata)
    value = f.backend.infer(_request())
    raw = value["raw_prediction"]
    assert raw["selected_class"] == backend.CLASS_NAMES[selected]
    target = value["learned_readout_result"].target
    if selected == 0:
        assert target.kind == "translate" and target.yaw_delta_rad is None
    elif selected in (1, 2):
        assert target.kind == "turn_yaw" and target.translation_m is None
        assert target.yaw_delta_rad == pytest.approx(-0.7 if selected == 1 else 0.7)
    else:
        assert target.kind == "stop" and not target.metric_known
        assert raw["raw_distance_m"] < 0 and raw["raw_angle_magnitude_rad"] == -5


@pytest.mark.parametrize("selected,scale", [(0, 0), (0, -1), (0, 501), (1, 0), (1, -0.2), (2, 3.2)])
def test_invalid_selected_scale_preserved_and_rejected(selected, scale):
    f = fixture()
    with torch.no_grad():
        f.head.weight.zero_()
        f.head.bias[:4].fill_(-2)
        f.head.bias[selected] = 8
        f.head.bias[4 if selected == 0 else 5] = scale
    f.backend.expected_head_state = backend._expected_head_state(metadata(f.head, f.norm))
    with pytest.raises(NavigationBackendRejected) as exc:
        f.backend.infer(_request())
    d = diagnostics(exc)
    assert d["raw_prediction"][
        "raw_distance_m" if selected == 0 else "raw_angle_magnitude_rad"
    ] == pytest.approx(scale)
    assert "target" not in d["raw_prediction"]
    assert d["head_readout_returned"] is True and d["result_accepted"] is False


@pytest.mark.parametrize("field", ["model_sha256", "adapter_sha256", "adapter_config_sha256"])
def test_request_engine_file_mismatch_before_processor(field):
    f = fixture()
    with pytest.raises(NavigationBackendRejected):
        f.backend.infer(_request(**{field: "9" * 64}))
    assert not f.engine.processor.calls and not f.engine.model.trunk.calls


@pytest.mark.parametrize(
    "change", ["training", "grad", "requires_grad", "disabled", "wrong_adapter", "no_layers"]
)
def test_actual_base_adapter_state_fail_before_prefix(change):
    f = fixture()
    model = f.engine.model
    if change == "training":
        model.training = True
    elif change == "grad":
        model.trunk.marker.grad = torch.ones(1)
    elif change == "requires_grad":
        model.trunk.marker.requires_grad_(True)
    elif change == "disabled":
        model.layers[0].disable_adapters = True
    elif change == "wrong_adapter":
        model.active_adapters = ["other"]
    else:
        model.layers = []
    with pytest.raises(NavigationBackendRejected):
        f.backend.infer(_request())
    assert not f.engine.processor.calls


@pytest.mark.parametrize(
    "change", ["head_weight", "head_training", "head_grad", "norm_center", "norm_scale", "norm_ids"]
)
def test_preloaded_head_norm_state_mismatch(change):
    f = fixture()
    with torch.no_grad():
        if change == "head_weight":
            f.head.weight[0, 0] += 1
        elif change == "head_training":
            f.head.train()
        elif change == "head_grad":
            f.head.weight.grad = torch.ones_like(f.head.weight)
        elif change == "norm_center":
            f.norm.center[0] += 1
        elif change == "norm_scale":
            f.norm.scale[0] = 0
        else:
            object.__setattr__(f.norm, "sample_ids", ("different-training",))
    with pytest.raises(NavigationBackendRejected):
        f.backend.infer(_request())
    assert not f.engine.processor.calls


@pytest.mark.parametrize("change", ["extra_gold", "missing_mask", "float_ids", "bad_pixels"])
def test_bad_prefix_never_enters_fusion(change):
    f = fixture()

    def mutate(value):
        if change == "extra_gold":
            value["labels"] = torch.tensor([[0]])
        elif change == "missing_mask":
            del value["attention_mask"]
        elif change == "float_ids":
            value["input_ids"] = value["input_ids"].float()
        else:
            value["pixel_values"][0, 0] = float("nan")

    f.engine.processor.hook = mutate
    with pytest.raises(NavigationBackendRejected):
        f.backend.infer(_request())
    assert not f.engine.model.trunk.calls


@pytest.mark.parametrize(
    "feature",
    [
        torch.zeros(1, 2047),
        torch.zeros(1, 2048, dtype=torch.float64),
        torch.full((1, 2048), float("nan")),
    ],
)
def test_invalid_feature_not_used_by_head(feature):
    f = fixture(feature_extractor=lambda engine, inputs: feature)
    with pytest.raises(NavigationBackendRejected) as exc:
        f.backend.infer(_request())
    assert diagnostics(exc)["head_readout_attempted"] is False


@pytest.mark.parametrize("change", ["head", "norm", "adapter"])
def test_persistent_mutation_after_forward_rejected(change):
    f = fixture()

    def hook(hidden):
        if change == "head":
            f.head.bias[0] += 1
        elif change == "norm":
            f.norm.center[0] += 1
        else:
            f.engine.model.layers[0].disable_adapters = True

    f.engine.model.trunk.hook = hook
    with pytest.raises(NavigationBackendRejected) as exc:
        f.backend.infer(_request())
    assert diagnostics(exc)["result_accepted"] is False


@pytest.mark.parametrize("phase", ["prefix", "feature", "readout"])
def test_deadline_rejection_at_each_phase(phase):
    f = fixture()
    if phase == "prefix":
        f.engine.processor.hook = lambda value: setattr(f.clock, "now", 14.0)
    elif phase == "feature":
        f.engine.model.trunk.hook = lambda hidden: setattr(f.clock, "now", 14.0)
    else:

        def predictor(head, norm, feature, **kwargs):
            value = backend._default_head_predictor(head, norm, feature, **kwargs)
            f.clock.now = 14
            return value

        f.backend.head_predictor = predictor
    with pytest.raises(NavigationBackendRejected) as exc:
        f.backend.infer(_request())
    assert diagnostics(exc)["result_accepted"] is False


def test_oldest_frame_age_not_only_current():
    f = fixture()
    f.engine.model.trunk.hook = lambda hidden: setattr(f.clock, "now", 14.1)
    with pytest.raises(NavigationBackendRejected):
        f.backend.infer(_request(two_frames=True, deadline_s=20))


@pytest.mark.parametrize("kind", ["deadline", "oldest_frame", "clock_regression"])
def test_time_after_final_binding_validation_is_gated(monkeypatch, kind):
    f = fixture()
    request = _request(two_frames=True, deadline_s=20 if kind == "oldest_frame" else 13)
    original = backend.LearnedNavigationReadoutResult.validate_binding

    def delayed(self, **kwargs):
        original(self, **kwargs)
        f.clock.now = 14.1 if kind != "clock_regression" else 10.0

    monkeypatch.setattr(backend.LearnedNavigationReadoutResult, "validate_binding", delayed)
    with pytest.raises(NavigationBackendRejected, match="expired|regressed") as exc:
        f.backend.infer(request)
    assert diagnostics(exc)["result_accepted"] is False
    assert diagnostics(exc)["head_readout_returned"] is True
    assert f.backend._exclusive.acquire(blocking=False)
    f.backend._exclusive.release()


def test_clock_regression_rejected_and_no_forward():
    f = fixture()
    f.engine.processor.hook = lambda value: setattr(f.clock, "now", 9.5)
    with pytest.raises(NavigationBackendRejected, match="regressed"):
        f.backend.infer(_request())
    assert not f.engine.model.trunk.calls


def test_exclusive_instance_no_nested_request_and_release_on_error():
    f = fixture()
    seen = []

    def nested(value):
        with pytest.raises(NavigationBackendRejected, match="already serving") as exc:
            f.backend.infer(_request())
        seen.append(diagnostics(exc)["fused_forward_attempted"])

    f.engine.processor.hook = nested
    f.backend.infer(_request())
    assert seen == [False] and len(f.engine.processor.calls) == 1
    f.engine.processor.hook = lambda value: (_ for _ in ()).throw(ValueError("fixture failure"))
    with pytest.raises(NavigationBackendRejected):
        f.backend.infer(_request())
    assert f.backend._exclusive.acquire(blocking=False)
    f.backend._exclusive.release()


def test_result_immutable_scope_time_and_content_binding():
    f = fixture()
    request = _request()
    result = f.backend.infer(request)["learned_readout_result"]
    with pytest.raises(FrozenInstanceError):
        result.completed_at_s = 11
    with pytest.raises(ValueError):
        result.validate_binding(current_scope=_scope(selected_task_id="other"), now_s=f.clock.now)
    with pytest.raises(ValueError):
        result.validate_binding(
            current_scope=SimpleNamespace(canonical_hash=request.scope.canonical_hash),
            now_s=f.clock.now,
        )
    with pytest.raises(ValueError):
        result.validate_binding(current_scope=request.scope, now_s=result.completed_at_s - 0.01)
    feature = json.loads(result.feature_json)
    feature["sha256"] = "9" * 64
    object.__setattr__(result, "feature_json", backend._json_bytes(feature))
    with pytest.raises(ValueError, match="changed"):
        result.validate_binding(current_scope=request.scope, now_s=f.clock.now)


@pytest.mark.parametrize(
    "change", ["confidence", "fake_tokens", "changed_scale", "wrong_prefix", "schema_bool"]
)
def test_result_rejects_fake_or_changed_raw(change):
    f = fixture()

    def raw_predict(outputs, provenance):
        value = backend._default_raw_prediction(outputs, provenance)
        if change == "confidence":
            value["confidence"] = 0.99
        elif change == "fake_tokens":
            value["generated_token_ids"] = [1]
        elif change == "changed_scale":
            value["target"]["translation_m"][0] = 3
        elif change == "wrong_prefix":
            value["declared_bindings"]["prefix_sha256"] = "9" * 64
        else:
            value["schema_version"] = True
        return value

    f.backend.raw_prediction = raw_predict
    with pytest.raises(NavigationBackendRejected):
        f.backend.infer(_request())


@pytest.mark.parametrize(
    "field", ["head_receipt_sha256", "head_weights_sha256", "head_metadata_sha256"]
)
def test_readout_sha_type_and_tamper(field):
    values = {name: "a" * 64 for name in backend.SavedReadoutIdentity.__dataclass_fields__}
    values[field] = "A" * 64
    with pytest.raises(ValueError):
        backend.SavedReadoutIdentity(**values)
    f = fixture()
    object.__setattr__(f.identity, field, "9" * 64)
    with pytest.raises(NavigationBackendRejected, match="identity changed"):
        f.backend.infer(_request())


def test_constructor_does_not_mutate_or_retrain_normalization():
    f = fixture()
    before = [v.clone() for v in (f.head.weight, f.head.bias, f.norm.center, f.norm.scale)]
    original = copy.deepcopy(f.metadata)
    f.metadata["tensors"]["weight"]["sha256"] = "9" * 64
    f.backend.infer(_request())
    assert f.backend.expected_head_state == backend._expected_head_state(original)
    assert all(
        torch.equal(a, b)
        for a, b in zip(
            before, (f.head.weight, f.head.bias, f.norm.center, f.norm.scale), strict=True
        )
    )


def test_guarded_import_is_stdlib_only():
    root = Path(__file__).resolve().parents[1]
    code = (
        f"import sys; sys.path[:0]=[{str(root)!r},{str(root / 'src')!r}]; "
        "import scripts.onboard_navigation_learned_readout_backend; "
        "assert not {'torch','numpy','PIL','transformers','peft',"
        "'safetensors','pyarrow','datasets'}.intersection(sys.modules)"
    )
    run = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code], capture_output=True, text=True, timeout=20
    )
    assert run.returncode == 0, run.stderr
