"""Experimental navigation inference identities, never flight admission.

The frozen actor alone supplies model messages. Outer source/scope/time and
model-file hashes bind integrity, not sensor authentication, exact model loading,
tokenizer recomputation, clearance, pose conversion or permission to fly. A STOP
is only an external navigation-sequence stop, not hover/RTL/landing/completion.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from math import pi

from .onboard_history import OnboardInferenceScope
from .onboard_multimodal import OnboardImageFrame, OnboardMultimodalScope
from .onboard_navigation_actor import NavigationActorInput, serialize_navigation_actor_messages
from .onboard_navigation_supervision import TARGET_CONTRACT
from .serialization import (
    AutonomyContractError,
    canonical_sha256,
    require_int,
    require_real,
    require_schema_version,
    require_sha256,
    strict_json_loads,
    strict_object,
)

REQUEST_CONTRACT = "multidetect.onboard-navigation-inference-request-v1"
RESULT_CONTRACT = "multidetect.onboard-navigation-inference-result-v1"
MAX_PREFIX_TOKENS = 4096
MAX_NEW_TOKENS = 192
MAX_RESPONSE_BYTES = 64 * 1024
_TARGET_FIELDS = frozenset(
    {
        "schema_version",
        "contract",
        "kind",
        "translation_frame",
        "yaw_convention",
        "translation_m",
        "yaw_delta_rad",
    }
)
Scope = OnboardInferenceScope | OnboardMultimodalScope


class NavigationInferenceError(AutonomyContractError):
    """Malformed, changed, stale or unbound inference evidence."""


def _sha(value: object, name: str) -> str:
    if require_sha256(value, name) != value:
        raise NavigationInferenceError(f"{name} must be canonical lowercase SHA-256")
    return value


def _scope_hash(scope: Scope) -> str:
    if type(scope) not in (OnboardInferenceScope, OnboardMultimodalScope):
        raise TypeError("scope must be an existing immutable onboard scope")
    replace(scope)
    return scope.canonical_hash


def _raw_bytes(payload: str | bytes) -> bytes:
    if type(payload) not in (str, bytes):
        raise NavigationInferenceError("response must be immutable JSON text or bytes")
    try:
        raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    except UnicodeEncodeError as exc:
        raise NavigationInferenceError("response is not valid UTF-8 text") from exc
    if not 0 < len(raw) <= MAX_RESPONSE_BYTES:
        raise NavigationInferenceError("response must be nonempty bounded UTF-8 JSON")
    return raw


@dataclass(frozen=True, slots=True)
class PlannedNavigationTarget:
    """One strict primitive; null metric values remain explicitly unknown."""

    kind: str
    translation_m: tuple[float, float, float] | None = None
    yaw_delta_rad: float | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not str or self.kind not in {"translate", "turn_yaw", "stop"}:
            raise NavigationInferenceError("unsupported navigation primitive")
        if self.translation_m is not None:
            if type(self.translation_m) is not tuple or len(self.translation_m) != 3:
                raise NavigationInferenceError("translation needs an immutable FRD triple")
            vector = tuple(
                require_real(v, "translation_m", minimum=-500, maximum=500)
                for v in self.translation_m
            )
            if self.kind != "translate" or not any(vector):
                raise NavigationInferenceError("only translate permits a nonzero FRD vector")
            object.__setattr__(self, "translation_m", vector)
        if self.yaw_delta_rad is not None:
            yaw = require_real(self.yaw_delta_rad, "yaw_delta_rad", minimum=-pi, maximum=pi)
            if self.kind != "turn_yaw" or yaw == 0:
                raise NavigationInferenceError("only turn_yaw permits a nonzero bounded yaw")
            object.__setattr__(self, "yaw_delta_rad", yaw)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> PlannedNavigationTarget:
        raw = strict_object(value, name="planned navigation target", required=_TARGET_FIELDS)
        require_schema_version(raw["schema_version"])
        if (
            raw["contract"] != TARGET_CONTRACT
            or raw["translation_frame"] != "body_frd"
            or raw["yaw_convention"] != "clockwise_right_positive"
        ):
            raise NavigationInferenceError("unsupported navigation target/frame/yaw contract")
        vector = raw["translation_m"]
        if vector is not None and type(vector) is not list:
            raise NavigationInferenceError("translation_m must be a JSON array or null")
        return cls(raw["kind"], None if vector is None else tuple(vector), raw["yaw_delta_rad"])

    def to_mapping(self) -> dict:
        return {
            "schema_version": 1,
            "contract": TARGET_CONTRACT,
            "kind": self.kind,
            "translation_frame": "body_frd",
            "yaw_convention": "clockwise_right_positive",
            "translation_m": None if self.translation_m is None else list(self.translation_m),
            "yaw_delta_rad": self.yaw_delta_rad,
        }

    @property
    def metric_known(self) -> bool:
        return self.translation_m is not None or self.yaw_delta_rad is not None


def parse_navigation_target(payload: str | bytes) -> PlannedNavigationTarget:
    """Reject extra/duplicate fields, bool metrics, nonfinite values and guessing."""
    return PlannedNavigationTarget.from_mapping(
        strict_json_loads(_raw_bytes(payload), name="navigation target")
    )


@dataclass(frozen=True, slots=True)
class NavigationInferenceRequest:
    """Parent-owned input identity; outer bindings never become actor messages.

    model_sha256 and adapter_sha256 identify the base and adapter *weight files*,
    not parameter digests. source_binding_sha256 is a caller's original source
    receipt/wire digest; this class cannot authenticate its producer.
    """

    actor_input: NavigationActorInput
    scope: Scope
    source_frames: tuple[OnboardImageFrame, ...]
    source_binding_sha256: str
    model_sha256: str
    adapter_sha256: str
    adapter_config_sha256: str
    generation: int
    submitted_at_s: float
    deadline_s: float
    max_frame_age_s: float = 5.0
    max_new_tokens: int = MAX_NEW_TOKENS
    _bound_actor_sha256: str = field(init=False, repr=False)
    _bound_request_sha256: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        for name in ("submitted_at_s", "deadline_s", "max_frame_age_s"):
            object.__setattr__(self, name, require_real(getattr(self, name), name, minimum=0))
        self._validate_fields()
        object.__setattr__(self, "_bound_actor_sha256", self.actor_input.identity_sha256)
        object.__setattr__(self, "_bound_request_sha256", canonical_sha256(self.to_mapping()))

    def _validate_fields(self) -> None:
        if type(self.actor_input) is not NavigationActorInput:
            raise TypeError("actor_input must be the frozen NavigationActorInput")
        replace(self.actor_input)
        _scope_hash(self.scope)
        if hashlib.sha256(self.actor_input.prompt_original.encode("utf-8")).hexdigest() != (
            self.scope.prompt_sha256
        ):
            raise NavigationInferenceError("scope prompt differs from the original actor prompt")
        require_int(self.generation, "generation", minimum=1)
        require_int(self.max_new_tokens, "max_new_tokens", minimum=1, maximum=MAX_NEW_TOKENS)
        start = require_real(self.submitted_at_s, "submitted_at_s", minimum=0)
        end = require_real(self.deadline_s, "deadline_s", minimum=0)
        age = require_real(self.max_frame_age_s, "max_frame_age_s", minimum=0, maximum=30)
        if not 0 < end - start <= 30 or age <= 0:
            raise NavigationInferenceError("request requires a positive bounded deadline/frame age")
        for name in (
            "source_binding_sha256",
            "model_sha256",
            "adapter_sha256",
            "adapter_config_sha256",
        ):
            _sha(getattr(self, name), name)
        if type(self.source_frames) is not tuple or len(self.source_frames) != len(
            self.actor_input.frames
        ):
            raise NavigationInferenceError(
                "source frames must cover exactly the actor observations"
            )
        previous_time, ids = None, set()
        for actor, source in zip(self.actor_input.frames, self.source_frames, strict=True):
            if type(source) is not OnboardImageFrame:
                raise TypeError("source frames must be existing immutable OnboardImageFrame values")
            replace(source)
            if source.scope != self.scope or source.frame_id in ids:
                raise NavigationInferenceError("source frame scope/identity mismatch")
            ids.add(source.frame_id)
            observed = require_real(source.captured_at_s, "source captured_at_s", minimum=0)
            if not 0 <= start - observed <= age or (
                previous_time is not None and observed <= previous_time
            ):
                raise NavigationInferenceError(
                    "source frames are future, stale or nonchronological"
                )
            previous_time = observed
            if (
                source.image_bytes != actor.image_bytes
                or source.image_sha256 != actor.sha256
                or (source.width, source.height, source.encoding)
                != (actor.width, actor.height, actor.encoding)
            ):
                raise NavigationInferenceError(
                    "source RGB differs from the frozen actor observation"
                )

    @property
    def actor_identity_sha256(self) -> str:
        return self._bound_actor_sha256

    @property
    def canonical_hash(self) -> str:
        return self._bound_request_sha256

    def to_mapping(self) -> dict:
        return {
            "schema_version": 1,
            "contract": REQUEST_CONTRACT,
            "actor_identity_sha256": self.actor_input.identity_sha256,
            "scope_sha256": _scope_hash(self.scope),
            "source_frames": [frame.to_mapping() for frame in self.source_frames],
            "source_binding_sha256": self.source_binding_sha256,
            "model_sha256": self.model_sha256,
            "adapter_sha256": self.adapter_sha256,
            "adapter_config_sha256": self.adapter_config_sha256,
            "generation": self.generation,
            "submitted_at_s": self.submitted_at_s,
            "deadline_s": self.deadline_s,
            "max_frame_age_s": self.max_frame_age_s,
            "max_prefix_tokens": MAX_PREFIX_TOKENS,
            "max_new_tokens": self.max_new_tokens,
            "usage": "experimental_only",
            "transport_authority": False,
            "execution_authority": False,
            "completion_evidence_created": False,
        }

    def validate_binding(self) -> None:
        self._validate_fields()
        if self.actor_input.identity_sha256 != self._bound_actor_sha256 or (
            canonical_sha256(self.to_mapping()) != self._bound_request_sha256
        ):
            raise NavigationInferenceError("immutable navigation request identity changed")

    def validate_freshness(self, now_s: float) -> None:
        self.validate_binding()
        now = require_real(now_s, "now_s", minimum=0)
        if not self.submitted_at_s <= now < self.deadline_s or any(
            not 0 <= now - frame.captured_at_s <= self.max_frame_age_s
            for frame in self.source_frames
        ):
            raise NavigationInferenceError("navigation request/result is future, stale or expired")


def serialize_navigation_inference_messages(request: NavigationInferenceRequest) -> list[dict]:
    """Fresh frozen-actor messages only; caller owns closing returned PIL images."""
    if type(request) is not NavigationInferenceRequest:
        raise TypeError("request must be NavigationInferenceRequest")
    request.validate_binding()
    return serialize_navigation_actor_messages(request.actor_input)


@dataclass(frozen=True, slots=True)
class NavigationInferenceResult:
    """Bound backend observations, not independent tokenizer/model evidence."""

    request: NavigationInferenceRequest
    generation: int
    request_sha256: str
    raw_output: bytes
    generated_token_ids: tuple[int, ...]
    generated_tokens: int
    prefix_tokens: int
    completed_at_s: float
    inference_latency_ms: float
    model_sha256: str
    adapter_sha256: str
    adapter_config_sha256: str
    target: PlannedNavigationTarget = field(init=False)
    _bound_result_sha256: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._validate_fields()
        object.__setattr__(self, "target", parse_navigation_target(self.raw_output))
        object.__setattr__(self, "_bound_result_sha256", canonical_sha256(self.to_mapping()))

    def _validate_fields(self) -> None:
        if type(self.request) is not NavigationInferenceRequest:
            raise TypeError("result requires the original typed request")
        self.request.validate_binding()
        require_int(self.generation, "generation", minimum=1)
        if (
            self.generation != self.request.generation
            or _sha(self.request_sha256, "request_sha256") != self.request.canonical_hash
        ):
            raise NavigationInferenceError("result generation/request identity mismatch")
        for name in ("model_sha256", "adapter_sha256", "adapter_config_sha256"):
            if _sha(getattr(self, name), name) != getattr(self.request, name):
                raise NavigationInferenceError("result model/adapter/config file identity mismatch")
        if type(self.raw_output) is not bytes:
            raise NavigationInferenceError("result raw_output must be immutable bytes")
        _raw_bytes(self.raw_output)
        if type(self.generated_token_ids) is not tuple:
            raise NavigationInferenceError("actual generated token IDs must be an immutable tuple")
        count = require_int(self.generated_tokens, "generated_tokens", minimum=1)
        if count != len(self.generated_token_ids) or count >= self.request.max_new_tokens:
            raise NavigationInferenceError("generated token count mismatches IDs or exhausts cap")
        for token in self.generated_token_ids:
            require_int(token, "generated token ID", minimum=0)
        require_int(self.prefix_tokens, "prefix_tokens", minimum=1, maximum=MAX_PREFIX_TOKENS)
        completed = require_real(self.completed_at_s, "completed_at_s", minimum=0)
        latency = require_real(self.inference_latency_ms, "inference_latency_ms", minimum=0)
        self.request.validate_freshness(completed)
        if latency > (completed - self.request.submitted_at_s) * 1000 + 1e-6:
            raise NavigationInferenceError("actual host latency exceeds the bound request interval")

    @property
    def raw_output_sha256(self) -> str:
        return hashlib.sha256(self.raw_output).hexdigest()

    @property
    def canonical_hash(self) -> str:
        return self._bound_result_sha256

    def to_mapping(self) -> dict:
        return {
            "schema_version": 1,
            "contract": RESULT_CONTRACT,
            "request_sha256": self.request_sha256,
            "actor_identity_sha256": self.request.actor_identity_sha256,
            "scope_sha256": _scope_hash(self.request.scope),
            "generation": self.generation,
            "raw_output_sha256": self.raw_output_sha256,
            "generated_token_ids": list(self.generated_token_ids),
            "generated_tokens": self.generated_tokens,
            "prefix_tokens": self.prefix_tokens,
            "max_new_tokens": self.request.max_new_tokens,
            "max_new_tokens_reached": self.generated_tokens >= self.request.max_new_tokens,
            "completed_at_s": self.completed_at_s,
            "inference_latency_ms": self.inference_latency_ms,
            "model_sha256": self.model_sha256,
            "adapter_sha256": self.adapter_sha256,
            "adapter_config_sha256": self.adapter_config_sha256,
            "target": self.target.to_mapping(),
            "tokenizer_independently_verified": False,
            "transport_authority": False,
            "execution_authority": False,
            "completion_evidence_created": False,
        }

    def validate_binding(self, *, current_scope: Scope, now_s: float) -> None:
        self._validate_fields()
        self.request.validate_freshness(now_s)
        if _scope_hash(current_scope) != _scope_hash(self.request.scope):
            raise NavigationInferenceError("current scope changed since navigation submission")
        if require_real(now_s, "now_s", minimum=0) < self.completed_at_s:
            raise NavigationInferenceError("result completion is in the host's future")
        if parse_navigation_target(self.raw_output) != self.target or (
            canonical_sha256(self.to_mapping()) != self._bound_result_sha256
        ):
            raise NavigationInferenceError("immutable navigation result identity changed")


def parse_navigation_inference_response(
    payload: str | bytes,
    request: NavigationInferenceRequest,
    *,
    current_scope: Scope,
    generation: int,
    request_sha256: str,
    generated_token_ids: tuple[int, ...],
    generated_tokens: int,
    prefix_tokens: int,
    completed_at_s: float,
    inference_latency_ms: float,
    model_sha256: str,
    adapter_sha256: str,
    adapter_config_sha256: str,
) -> NavigationInferenceResult:
    """Bind the backend's raw decode and actual IDs; do not recompute decoding.

    Rejected attempts should retain raw bytes/token observations in the calling
    worker's diagnostic log. Parsing does not grant sensor or flight admission.
    """
    result = NavigationInferenceResult(
        request,
        generation,
        request_sha256,
        _raw_bytes(payload),
        generated_token_ids,
        generated_tokens,
        prefix_tokens,
        completed_at_s,
        inference_latency_ms,
        model_sha256,
        adapter_sha256,
        adapter_config_sha256,
    )
    result.validate_binding(current_scope=current_scope, now_s=completed_at_s)
    return result


__all__ = [
    "MAX_NEW_TOKENS",
    "MAX_PREFIX_TOKENS",
    "MAX_RESPONSE_BYTES",
    "REQUEST_CONTRACT",
    "RESULT_CONTRACT",
    "NavigationInferenceError",
    "NavigationInferenceRequest",
    "NavigationInferenceResult",
    "PlannedNavigationTarget",
    "parse_navigation_inference_response",
    "parse_navigation_target",
    "serialize_navigation_inference_messages",
]
