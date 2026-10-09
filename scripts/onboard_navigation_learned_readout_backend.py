"""Preloaded, observation-only learned navigation readout; no loader or flight.

Import is stdlib-only. The caller authenticates and exactly loads candidate
base/LoRA plus a stable ``load_readout`` bundle, including its TRAIN-fitted norm,
and supplies the returned metadata and three file SHAs. These declarations do
not themselves authenticate the supplied objects. No bundle is loaded here.
Load streaming hooks before any optional model/readout imports in that caller.

Use the same frozen NavigationInferenceRequest/Actor, but a sibling result:
six learned numbers are not generated tokens, decoded model text or confidence.
The caller owns sensor/host clock admission, exclusive engine lifecycle and an
absolute process watchdog. A synchronous forward cannot enforce a hard timeout.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from dataclasses import dataclass, field

from multidetect.autonomy.onboard_navigation_inference import (
    NavigationInferenceRequest,
    PlannedNavigationTarget,
    parse_navigation_target,
)
from multidetect.autonomy.serialization import (
    canonical_json,
    require_int,
    require_real,
    require_sha256,
    strict_json_loads,
)
from scripts.onboard_navigation_inference_backend import (
    NavigationBackendRejected,
    NavigationEngineIdentity,
    PreloadedNavigationInferenceBackend,
    _default_prefix_checker,
    _default_prefix_processor,
)

CONTRACT = "multidetect.onboard-navigation-learned-readout-result-v1"
FOUR_PREFIX = {"input_ids", "attention_mask", "pixel_values", "image_grid_thw"}
CLASS_NAMES = ("forward", "turn_yaw_left", "turn_yaw_right", "sequence_stop")
FALSE_CLAIMS = {
    "exact_model_load_verified_here": False,
    "head_bundle_authenticated_here": False,
    "sensor_authenticity_verified_here": False,
    "absolute_process_watchdog_owned": False,
    "quality_claim": False,
    "candidate_promoted": False,
    "transport_authority": False,
    "execution_authority": False,
    "completion_evidence_created": False,
    "final_release_allowed": False,
}


def _json_bytes(value):
    return canonical_json(value).encode("utf-8")


def _sha(value):
    if require_sha256(value, "declared SHA-256") != value:
        raise ValueError("Canonical lowercase SHA-256 required")
    return value


def _record(value, shape, dtype="torch.float32"):
    if (
        type(value) is not dict
        or set(value) != {"dtype", "shape", "sha256"}
        or value["dtype"] != dtype
        or value["shape"] != list(shape)
        or any(type(v) is not int or v < 1 for v in value["shape"])
    ):
        raise ValueError("Exact typed tensor record required")
    _sha(value["sha256"])


@dataclass(frozen=True, slots=True)
class SavedReadoutIdentity:
    """Caller-admitted receipt/weights/metadata file identities, not live proof."""

    head_receipt_sha256: str
    head_weights_sha256: str
    head_metadata_sha256: str

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            _sha(getattr(self, name))

    def to_mapping(self):
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


def _expected_head_state(metadata):
    if (
        type(metadata) is not dict
        or metadata.get("contract") != "multidetect.onboard-navigation-stable-readout-v1"
        or metadata.get("kind") != "readout"
        or type(metadata.get("feature_dim")) is not int
        or metadata["feature_dim"] != 2048
        or metadata.get("optimizer_state_saved") is not False
        or metadata.get("small_or_constant_scale_rule") != "population_std_below_epsilon_uses_one"
    ):
        raise ValueError("Caller must supply saved stable 2048-dimensional readout metadata")
    ids = metadata["normalization_train_ids"]
    if (
        type(ids) is not list
        or not ids
        or any(type(sid) is not str or not sid for sid in ids)
        or len(set(ids)) != len(ids)
        or metadata["training_report"]["training_sample_ids"] != ids
        or metadata["training_report"]["success"] is not True
    ):
        raise ValueError("Saved train-only normalization identities required")
    require_real(metadata["epsilon"], "normalization epsilon", minimum=1e-15, maximum=1)
    records = metadata["tensors"]
    if set(records) != {"weight", "bias", "center", "scale"}:
        raise ValueError("Exact head and normalization tensors required")
    for name, shape in (
        ("weight", (6, 2048)),
        ("bias", (6,)),
        ("center", (2048,)),
        ("scale", (2048,)),
    ):
        _record(records[name], shape)
    return _json_bytes(
        {"tensors": records, "normalization_train_ids": ids, "epsilon": metadata["epsilon"]}
    )


def _default_head_snapshot(head, normalization, *, torch_module):
    from scripts import onboard_navigation_stable_readout as stable

    if type(normalization) is not stable.TrainNormalization:
        raise ValueError("Exact saved TrainNormalization required")
    if type(head) is not torch_module.nn.Linear:
        raise ValueError("Exact saved FP32 Linear head required")
    normalization.validate()
    if head.training or any(p.requires_grad or p.grad is not None for p in head.parameters()):
        raise ValueError("Preloaded head must be eval/frozen without gradients")
    tensors = {
        "weight": head.weight,
        "bias": head.bias,
        "center": normalization.center,
        "scale": normalization.scale,
    }
    return {
        "tensors": {name: stable.tensor_record(value) for name, value in tensors.items()},
        "normalization_train_ids": list(normalization.sample_ids),
        "epsilon": normalization.epsilon,
    }


def _default_feature_extractor(engine, inputs):
    from scripts import onboard_navigation_stable_readout as stable

    torch = engine.torch
    trunk = engine.model.get_base_model().model
    if type(trunk).__name__ != "Qwen3VLModel":
        raise ValueError("Exact Qwen3VLModel fusion trunk required; no LM-logits path")
    device = next(trunk.parameters()).device
    moved, output, hidden = None, None, None
    try:
        moved = {name: tensor.to(device) for name, tensor in inputs.items()}
        if device.type == "cuda":
            torch.cuda.synchronize(device)
        with torch.inference_mode():
            output = trunk(
                **moved,
                use_cache=False,
                past_key_values=None,
                return_dict=True,
                output_hidden_states=False,
            )
            hidden = output.last_hidden_state
            if tuple(hidden.shape) != (*inputs["input_ids"].shape, 2048):
                raise ValueError("Actual fused final hidden state shape differs")
            feature = (
                stable._loss_api()
                .extract_last_prefix_feature(hidden, moved["attention_mask"])
                .cpu()
                .clone()
            )
        if device.type == "cuda":
            torch.cuda.synchronize(device)
        return feature
    finally:
        moved = output = hidden = None


def _default_head_predictor(head, normalization, feature, *, torch_module):
    from scripts import onboard_navigation_stable_readout as stable

    with torch_module.inference_mode():
        return stable.predict(head, normalization, feature)


def _default_tensor_record(value, shape):
    from scripts import onboard_navigation_stable_readout as stable

    stable._tensor(value, shape=shape)
    return stable.tensor_record(value)


def _default_raw_prediction(outputs, declared_bindings):
    from scripts import onboard_navigation_stable_readout as stable

    api = stable._loss_api()
    return api.readout_result(outputs, provenance=api.ActionReadoutProvenance(**declared_bindings))


def _validate_raw(raw, request, identity, prefix):
    fields = {
        "schema_version",
        "contract",
        "source_kind",
        "class_names",
        "selected_class",
        "raw_logits",
        "raw_distance_m",
        "raw_angle_magnitude_rad",
        "declared_bindings",
        "actual_model_identity_verified",
        "prefix_only_verified",
        "quality_claim",
        "candidate_promoted",
        "execution_authority",
        "final_release_allowed",
        "target",
    }
    if (
        type(raw) is not dict
        or set(raw) != fields
        or raw.get("contract") != "multidetect.onboard-navigation-action-readout-v1"
        or type(raw.get("schema_version")) is not int
        or raw.get("schema_version") != 1
        or raw.get("source_kind") != "learned_action_readout_not_text_generation"
        or raw.get("class_names") != list(CLASS_NAMES)
    ):
        raise ValueError("Standalone learned six-number observation required")
    logits = raw["raw_logits"]
    values = [*logits, raw["raw_distance_m"], raw["raw_angle_magnitude_rad"]]
    if (
        type(logits) is not list
        or len(logits) != 4
        or any(type(v) not in (int, float) or not math.isfinite(v) for v in values)
    ):
        raise ValueError("Four finite logits plus two finite unmodified scales required")
    selected = max(range(4), key=lambda index: logits[index])
    if raw["selected_class"] != CLASS_NAMES[selected]:
        raise ValueError("Learned selected class differs from raw argmax")
    expected = {
        "prefix_sha256": hashlib.sha256(_json_bytes(prefix["tensors"])).hexdigest(),
        "base_weights_sha256": request.model_sha256,
        "adapter_weights_sha256": request.adapter_sha256,
        "head_weights_sha256": identity.head_weights_sha256,
        "head_config_sha256": identity.head_metadata_sha256,
    }
    if raw["declared_bindings"] != expected:
        raise ValueError("Raw readout source declarations differ from request/bundle/prefix")
    for name in (
        "actual_model_identity_verified",
        "prefix_only_verified",
        "quality_claim",
        "candidate_promoted",
        "execution_authority",
        "final_release_allowed",
    ):
        if raw[name] is not False:
            raise ValueError("Raw readout cannot grant authentication or execution")
    distance, angle = values[-2:]
    if selected == 0:
        if not 0 < distance <= 500:
            raise ValueError("Selected learned distance is invalid; no clipping or fixed scale")
        target = PlannedNavigationTarget("translate", (distance, 0.0, 0.0))
    elif selected in (1, 2):
        if not 0 < angle <= math.pi:
            raise ValueError("Selected learned angle is invalid; no clipping or fixed scale")
        target = PlannedNavigationTarget(
            "turn_yaw", yaw_delta_rad=-angle if selected == 1 else angle
        )
    else:
        target = PlannedNavigationTarget("stop")
    if parse_navigation_target(_json_bytes(raw["target"])) != target:
        raise ValueError(
            "Target must preserve the learned selected scale and FRD/yaw/null contract"
        )
    return target


@dataclass(frozen=True, slots=True)
class LearnedNavigationReadoutResult:
    """Request-bound observation, deliberately not NavigationInferenceResult."""

    request: NavigationInferenceRequest
    identity: SavedReadoutIdentity
    prefix_json: bytes
    feature_json: bytes
    raw_prediction_json: bytes
    head_state_sha256: str
    started_at_s: float
    completed_at_s: float
    target: PlannedNavigationTarget = field(init=False)
    _bound_sha256: str = field(init=False, repr=False)

    def __post_init__(self):
        target = self._validate()
        object.__setattr__(self, "target", target)
        object.__setattr__(
            self, "_bound_sha256", hashlib.sha256(_json_bytes(self.to_mapping())).hexdigest()
        )

    def _validate(self):
        if (
            type(self.request) is not NavigationInferenceRequest
            or type(self.identity) is not SavedReadoutIdentity
        ):
            raise TypeError("Frozen navigation request and saved readout identity required")
        self.request.validate_binding()
        self.identity.__post_init__()
        _sha(self.head_state_sha256)
        started = require_real(self.started_at_s, "started_at_s", minimum=0)
        completed = require_real(self.completed_at_s, "completed_at_s", minimum=0)
        if not self.request.submitted_at_s <= started <= completed:
            raise ValueError("Host readout time reversed or predates submission")
        self.request.validate_freshness(completed)
        for value in (self.prefix_json, self.feature_json, self.raw_prediction_json):
            if type(value) is not bytes or not 0 < len(value) <= 64 * 1024:
                raise ValueError("Bounded immutable JSON observations required")
        prefix = strict_json_loads(self.prefix_json, name="actual prefix")
        tokens = require_int(prefix["prefix_tokens"], "prefix_tokens", minimum=1, maximum=4096)
        if set(prefix["tensors"]) != FOUR_PREFIX:
            raise ValueError("Only four prefix tensor records admitted")
        for key in ("input_ids", "attention_mask"):
            _record(prefix["tensors"][key], (1, tokens), "torch.int64")
        for key in ("pixel_values", "image_grid_thw"):
            row = prefix["tensors"][key]
            _record(
                row,
                tuple(row["shape"]),
                "torch.float32" if key == "pixel_values" else "torch.int64",
            )
        _record(strict_json_loads(self.feature_json, name="actual feature"), (1, 2048))
        return _validate_raw(
            strict_json_loads(self.raw_prediction_json, name="raw readout"),
            self.request,
            self.identity,
            prefix,
        )

    def to_mapping(self):
        return {
            "schema_version": 1,
            "contract": CONTRACT,
            "request_sha256": self.request.canonical_hash,
            "actor_identity_sha256": self.request.actor_identity_sha256,
            "scope_sha256": self.request.scope.canonical_hash,
            "source_binding_sha256": self.request.source_binding_sha256,
            "generation": self.request.generation,
            "model_sha256": self.request.model_sha256,
            "adapter_sha256": self.request.adapter_sha256,
            "adapter_config_sha256": self.request.adapter_config_sha256,
            **self.identity.to_mapping(),
            "head_state_sha256": self.head_state_sha256,
            "actual_prefix": strict_json_loads(self.prefix_json, name="prefix"),
            "actual_feature": strict_json_loads(self.feature_json, name="feature"),
            "raw_prediction": strict_json_loads(self.raw_prediction_json, name="raw"),
            "target": self.target.to_mapping(),
            "started_at_s": self.started_at_s,
            "completed_at_s": self.completed_at_s,
            "inference_latency_ms": (self.completed_at_s - self.started_at_s) * 1000,
            "timing_scope": "host_prefix_processing_fused_forward_D2H_CPU_readout_projection",
            "text_generation_performed": False,
            "optimizer_performed": False,
            **FALSE_CLAIMS,
        }

    @property
    def canonical_hash(self):
        return self._bound_sha256

    def validate_binding(self, *, current_scope, now_s):
        target = self._validate()
        self.request.validate_freshness(now_s)
        if (
            type(current_scope) is not type(self.request.scope)
            or current_scope.canonical_hash != self.request.scope.canonical_hash
            or now_s < self.completed_at_s
            or target != self.target
            or hashlib.sha256(_json_bytes(self.to_mapping())).hexdigest() != self._bound_sha256
        ):
            raise ValueError("Readout result changed, crossed scope, or completed in the future")


class PreloadedLearnedNavigationBackend:
    """Exclusive per-instance readout; caller owns exact load/global ownership."""

    def __init__(
        self,
        engine,
        engine_identity,
        head,
        normalization,
        readout_identity,
        readout_metadata,
        *,
        clock=time.monotonic,
        prefix_processor=_default_prefix_processor,
        prefix_checker=_default_prefix_checker,
        feature_extractor=_default_feature_extractor,
        head_snapshot=_default_head_snapshot,
        head_predictor=_default_head_predictor,
        tensor_record=_default_tensor_record,
        raw_prediction=_default_raw_prediction,
    ):
        if (
            type(engine_identity) is not NavigationEngineIdentity
            or type(readout_identity) is not SavedReadoutIdentity
        ):
            raise TypeError("Typed model and saved head file identities required")
        if not all(
            callable(fn)
            for fn in (
                clock,
                prefix_processor,
                prefix_checker,
                feature_extractor,
                head_snapshot,
                head_predictor,
                tensor_record,
                raw_prediction,
            )
        ):
            raise TypeError("Clock and operations must be callable")
        self.engine, self.engine_identity = engine, engine_identity
        self.head, self.normalization, self.identity = head, normalization, readout_identity
        self.expected_head_state = _expected_head_state(readout_metadata)
        self._identity_json = _json_bytes(readout_identity.to_mapping())
        self.clock, self.prefix_processor, self.prefix_checker = (
            clock,
            prefix_processor,
            prefix_checker,
        )
        self.feature_extractor, self.head_snapshot, self.head_predictor = (
            feature_extractor,
            head_snapshot,
            head_predictor,
        )
        self.tensor_record, self.raw_prediction = tensor_record, raw_prediction
        self._last_now, self._exclusive = None, threading.Lock()

    def _now(self):
        now = require_real(self.clock(), "host monotonic time", minimum=0)
        if self._last_now is not None and now < self._last_now:
            raise ValueError("Host inference clock regressed")
        self._last_now = now
        return now

    def _state(self):
        self.identity.__post_init__()
        if _json_bytes(self.identity.to_mapping()) != self._identity_json:
            raise ValueError("Immutable saved readout identity changed")
        model = self.engine.model
        PreloadedNavigationInferenceBackend._check_adapter_state(model)
        if model.training or any(p.requires_grad or p.grad is not None for p in model.parameters()):
            raise ValueError("Preloaded base/LoRA must be eval/frozen without gradients")
        state = _json_bytes(
            self.head_snapshot(self.head, self.normalization, torch_module=self.engine.torch)
        )
        if state != self.expected_head_state:
            raise ValueError(
                "Preloaded head or TRAIN normalization differs from declared saved metadata"
            )
        return hashlib.sha256(state).hexdigest()

    def infer(self, request):
        if type(request) is not NavigationInferenceRequest:
            raise TypeError("Frozen NavigationInferenceRequest required")
        diagnostics = {
            "backend": "experimental_preloaded_learned_navigation_readout",
            "request_sha256": request.canonical_hash,
            "generation": request.generation,
            **self.identity.to_mapping(),
            **FALSE_CLAIMS,
            "text_generation_performed": False,
            "optimizer_performed": False,
            "fused_forward_attempted": False,
            "fused_forward_returned": False,
            "head_readout_attempted": False,
            "head_readout_returned": False,
            "result_accepted": False,
        }
        if not self._exclusive.acquire(blocking=False):
            raise NavigationBackendRejected(
                "Readout backend is already serving a request", diagnostics
            )
        inputs = feature = outputs = None
        try:
            started = self._now()
            request.validate_freshness(started)
            for name in ("model_sha256", "adapter_sha256", "adapter_config_sha256"):
                if getattr(request, name) != getattr(self.engine_identity, name):
                    raise ValueError("Request differs from preloaded base/adapter identity")
            state = self._state()
            inputs = self.prefix_processor(request.actor_input, self.engine.processor)
            if type(inputs) is not dict or set(inputs) != FOUR_PREFIX:
                raise ValueError("Only four actual prefix tensors may reach the fusion trunk")
            prefix = self.prefix_checker(
                inputs,
                processor=self.engine.processor,
                image_count=len(request.actor_input.frames),
                torch_module=self.engine.torch,
            )
            diagnostics["actual_prefix"] = prefix
            request.validate_freshness(self._now())
            if self._state() != state:
                raise ValueError("State changed during prefix processing")
            diagnostics["fused_forward_attempted"] = True
            feature = self.feature_extractor(self.engine, inputs)
            diagnostics["fused_forward_returned"] = True
            feature_record = self.tensor_record(feature, (1, 2048))
            _record(feature_record, (1, 2048))
            diagnostics["actual_feature"] = feature_record
            request.validate_freshness(self._now())
            diagnostics["head_readout_attempted"] = True
            outputs = self.head_predictor(
                self.head, self.normalization, feature, torch_module=self.engine.torch
            )
            diagnostics["head_readout_returned"] = True
            output_record = self.tensor_record(outputs, (1, 6))
            _record(output_record, (1, 6))
            diagnostics["actual_six_output"] = output_record
            provenance = {
                "prefix_sha256": hashlib.sha256(_json_bytes(prefix["tensors"])).hexdigest(),
                "base_weights_sha256": request.model_sha256,
                "adapter_weights_sha256": request.adapter_sha256,
                "head_weights_sha256": self.identity.head_weights_sha256,
                "head_config_sha256": self.identity.head_metadata_sha256,
            }
            raw = self.raw_prediction(outputs, provenance)
            diagnostics["raw_prediction"] = raw
            completed = self._now()
            request.validate_freshness(completed)
            if self._state() != state:
                raise ValueError(
                    "Persistent adapter/head/normalization state changed during readout"
                )
            result = LearnedNavigationReadoutResult(
                request,
                self.identity,
                _json_bytes(prefix),
                _json_bytes(feature_record),
                _json_bytes(raw),
                state,
                started,
                completed,
            )
            checked_at = self._now()
            result.validate_binding(current_scope=request.scope, now_s=checked_at)
            # Binding revalidation decodes/hashes the images. Measure its end,
            # then check time only; another binding call would leave that work
            # outside the acceptance clock again.
            checked_at = self._now()
            if not request.submitted_at_s <= checked_at < request.deadline_s or any(
                not 0 <= checked_at - frame.captured_at_s <= request.max_frame_age_s
                for frame in request.source_frames
            ):
                raise ValueError("Readout expired during final binding validation")
            diagnostics["acceptance_checked_at_s"] = checked_at
            diagnostics["host_elapsed_until_acceptance_ms"] = (checked_at - started) * 1000
            diagnostics["result_accepted"] = True
            return {"learned_readout_result": result, "raw_prediction": raw, "metrics": diagnostics}
        except Exception as exc:
            if hasattr(exc, "observation"):
                diagnostics["raw_prediction"] = exc.observation
            diagnostics["error_type"] = type(exc).__name__
            raise NavigationBackendRejected(str(exc), diagnostics) from exc
        finally:
            inputs = feature = outputs = None
            self._exclusive.release()


__all__ = [
    "SavedReadoutIdentity",
    "LearnedNavigationReadoutResult",
    "PreloadedLearnedNavigationBackend",
]
