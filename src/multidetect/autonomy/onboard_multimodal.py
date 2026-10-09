"""Versioned development/shadow inputs and intents, without execution authority.

Image and receipt bytes remain immutable and hash-bound. The ledger records
caller-supplied evidence; hashes are integrity checks, not authentication or
proof of flight. Trusted receipt sources must be chosen outside model output.
Unknown context stays unknown rather than becoming invented telemetry.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, fields
from io import BytesIO
from uuid import UUID

from .contracts import ContextSnapshot, PromptEnvelope
from .onboard_agent import OnboardModelOutput
from .onboard_history import OnboardInferenceScope
from .serialization import (
    AutonomyContractError,
    canonical_sha256,
    require_int,
    require_real,
    require_schema_version,
    require_sha256,
    require_text,
    strict_json_loads,
    strict_object,
)

INPUT_CONTRACT = "multidetect.onboard-multimodal-shadow-input-v1"
INTENT_CONTRACT = "multidetect.onboard-multimodal-shadow-intent-v1"
RECEIPT_CONTRACT = "multidetect.onboard-multimodal-evidence-receipt-v1"
MAX_IMAGE_BYTES = 32 * 1024 * 1024
_STAGES = ("proposed", "accepted", "executed", "completed")


class OnboardMultimodalError(AutonomyContractError):
    """A multimodal identity, content, timing or evidence check failed closed."""


def _text(value: object, name: str) -> str:
    text = require_text(value, name)
    if text != value:
        raise OnboardMultimodalError(f"{name} must not have surrounding whitespace")
    return text


def _bytes_hash(payload: bytes, expected: str, name: str, *, maximum: int) -> None:
    if type(payload) is not bytes or not 0 < len(payload) <= maximum:
        raise OnboardMultimodalError(f"{name} must be nonempty bounded immutable bytes")
    if hashlib.sha256(payload).hexdigest() != require_sha256(expected, f"{name}_sha256"):
        raise OnboardMultimodalError(f"{name} payload SHA256 mismatch")


def _ids(value: object, name: str, *, nonempty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, tuple) or (nonempty and not value):
        raise OnboardMultimodalError(f"{name} must be an immutable tuple")
    result = tuple(_text(item, name) for item in value)
    if len(set(result)) != len(result):
        raise OnboardMultimodalError(f"{name} contains duplicate IDs")
    return result


@dataclass(frozen=True, slots=True)
class OnboardMultimodalScope:
    """Shadow identity; absent home/profile values are explicitly unknown."""

    session_id: str
    prompt_sha256: str
    aircraft_id: str
    selected_task_id: str
    clock_epoch: str
    airframe_profile_id: str | None = None
    airframe_profile_sha256: str | None = None
    vehicle_class: str | None = None
    home_sha256: str | None = None

    def __post_init__(self) -> None:
        session = _text(self.session_id, "session_id")
        if str(UUID(session)) != session:
            raise OnboardMultimodalError("session_id must be a canonical UUID")
        for name in ("aircraft_id", "selected_task_id", "clock_epoch"):
            _text(getattr(self, name), name)
        require_sha256(self.prompt_sha256, "prompt_sha256")
        if (self.airframe_profile_id is None) != (self.airframe_profile_sha256 is None):
            raise OnboardMultimodalError("profile ID and hash must be known together")
        if self.airframe_profile_id is not None:
            _text(self.airframe_profile_id, "airframe_profile_id")
            require_sha256(self.airframe_profile_sha256, "airframe_profile_sha256")
        if self.home_sha256 is not None:
            require_sha256(self.home_sha256, "home_sha256")
        if self.vehicle_class not in {None, "multirotor", "fixed-wing", "vtol"}:
            raise OnboardMultimodalError("vehicle_class must be canonical or unknown")

    @classmethod
    def from_inference_scope(cls, scope: OnboardInferenceScope) -> OnboardMultimodalScope:
        if not isinstance(scope, OnboardInferenceScope):
            raise TypeError("scope must be OnboardInferenceScope")
        return cls(**{item.name: getattr(scope, item.name) for item in fields(cls)})

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "contract": "multidetect.onboard-multimodal-shadow-scope-v1",
            **{item.name: getattr(self, item.name) for item in fields(self)},
            "clock_semantics": "explicit_caller_epoch_not_sensor_clock_claim",
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    def validate_context(self, context: ContextSnapshot) -> None:
        if not isinstance(context, ContextSnapshot):
            raise TypeError("context must be ContextSnapshot")
        if (
            self.airframe_profile_id is None
            or self.home_sha256 is None
            or context.airframe_profile_id != self.airframe_profile_id
            or context.airframe_profile_hash != self.airframe_profile_sha256
            or context.home_position.canonical_hash != self.home_sha256
        ):
            raise OnboardMultimodalError("known context requires matching profile/home scope")


Scope = OnboardInferenceScope | OnboardMultimodalScope


def _scope(scope: Scope) -> None:
    if not isinstance(scope, (OnboardInferenceScope, OnboardMultimodalScope)):
        raise TypeError("scope must be an immutable onboard inference/shadow scope")


@dataclass(frozen=True, slots=True)
class OnboardImageFrame:
    scope: Scope
    frame_id: str
    captured_at_s: float
    image_bytes: bytes
    image_sha256: str
    width: int
    height: int
    encoding: str = "rgb8"
    context: ContextSnapshot | None = None

    def __post_init__(self) -> None:
        _scope(self.scope)
        _text(self.frame_id, "frame_id")
        require_real(self.captured_at_s, "captured_at_s", minimum=0)
        require_int(self.width, "width", minimum=1, maximum=4096)
        require_int(self.height, "height", minimum=1, maximum=4096)
        _bytes_hash(self.image_bytes, self.image_sha256, "image", maximum=MAX_IMAGE_BYTES)
        if self.encoding == "rgb8":
            if len(self.image_bytes) != self.width * self.height * 3:
                raise OnboardMultimodalError("rgb8 byte length does not match dimensions")
        elif self.encoding in {"png", "jpeg"}:
            try:
                from PIL import Image

                with Image.open(BytesIO(self.image_bytes)) as image:
                    if (
                        image.format != {"png": "PNG", "jpeg": "JPEG"}[self.encoding]
                        or image.size != (self.width, self.height)
                        or image.mode not in {"RGB", "RGBA"}
                        or getattr(image, "n_frames", 1) != 1
                    ):
                        raise OnboardMultimodalError("encoded RGB image metadata mismatch")
                    image.verify()
                with Image.open(BytesIO(self.image_bytes)) as image:
                    image.load()
            except ImportError as exc:
                raise OnboardMultimodalError(
                    "encoded images require the optional Pillow decoder"
                ) from exc
            except Exception as exc:
                raise OnboardMultimodalError(f"invalid encoded RGB image: {exc}") from exc
        else:
            raise OnboardMultimodalError("encoding must be rgb8, png or jpeg")
        if self.context is not None:
            self.scope.validate_context(self.context)
            if self.context.captured_at_s != self.captured_at_s:
                raise OnboardMultimodalError("context must have the exact image observation time")

    def to_mapping(self) -> dict[str, object]:
        return {
            "frame_id": self.frame_id,
            "scope_sha256": self.scope.canonical_hash,
            "clock_epoch": self.scope.clock_epoch,
            "captured_at_s": self.captured_at_s,
            "image_sha256": self.image_sha256,
            "image_bytes": len(self.image_bytes),
            "encoding": self.encoding,
            "width": self.width,
            "height": self.height,
            "context_status": "unknown" if self.context is None else "provided",
            "context": None if self.context is None else self.context.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class OnboardEvidenceReceipt:
    """Integrity-bound host evidence, never constructed by the intent parser.

    The payload is a host wrapper around the original receipt hash. Source trust
    and the original receipt's authenticity remain the collecting caller's duty.
    """

    evidence_id: str
    scope: Scope
    stage: str
    observed_at_s: float
    source_id: str
    receipt_bytes: bytes
    receipt_sha256: str
    proposal_id: str
    source_receipt_bytes: bytes
    prior_evidence_id: str | None = None
    frame_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _scope(self.scope)
        for name in ("evidence_id", "source_id", "proposal_id"):
            _text(getattr(self, name), name)
        require_real(self.observed_at_s, "observed_at_s", minimum=0)
        if self.stage not in _STAGES:
            raise OnboardMultimodalError("receipt stage is unsupported")
        if self.prior_evidence_id is not None:
            _text(self.prior_evidence_id, "prior_evidence_id")
        _ids(self.frame_ids, "frame_ids", nonempty=True)
        _bytes_hash(self.receipt_bytes, self.receipt_sha256, "receipt", maximum=1024 * 1024)
        raw = strict_object(
            strict_json_loads(self.receipt_bytes, name="evidence receipt"),
            name="evidence receipt",
            required=frozenset(
                {
                    "schema_version",
                    "contract",
                    "evidence_id",
                    "scope_sha256",
                    "stage",
                    "observed_at_s",
                    "source_id",
                    "proposal_id",
                    "prior_evidence_id",
                    "frame_ids",
                    "authority",
                    "source_receipt_sha256",
                    "success",
                }
            ),
        )
        canonical_sha256(raw)  # Also rejects exponent-overflow nonfinite JSON values.
        require_schema_version(raw["schema_version"])
        if raw["contract"] != RECEIPT_CONTRACT or raw["success"] is not True:
            raise OnboardMultimodalError("receipt must be a successful versioned host record")
        _bytes_hash(
            self.source_receipt_bytes,
            raw["source_receipt_sha256"],
            "source_receipt",
            maximum=1024 * 1024,
        )
        require_real(raw["observed_at_s"], "receipt.observed_at_s", minimum=0)
        expected = self.to_mapping()
        for name in (
            "evidence_id",
            "scope_sha256",
            "stage",
            "observed_at_s",
            "source_id",
            "proposal_id",
            "prior_evidence_id",
            "frame_ids",
        ):
            if raw[name] != expected[name]:
                raise OnboardMultimodalError(f"receipt payload binding mismatch: {name}")
        authority = "model_proposal" if self.stage == "proposed" else "executor_receipt"
        if raw["authority"] != authority:
            raise OnboardMultimodalError(
                "model statements cannot supply execution/completion facts"
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "scope_sha256": self.scope.canonical_hash,
            "stage": self.stage,
            "observed_at_s": self.observed_at_s,
            "source_id": self.source_id,
            "receipt_sha256": self.receipt_sha256,
            "source_receipt_sha256": hashlib.sha256(self.source_receipt_bytes).hexdigest(),
            "proposal_id": self.proposal_id,
            "prior_evidence_id": self.prior_evidence_id,
            "frame_ids": list(self.frame_ids),
        }


@dataclass(frozen=True, slots=True)
class MissionEvidenceLedger:
    scope: Scope
    frames: tuple[OnboardImageFrame, ...] = ()
    receipts: tuple[OnboardEvidenceReceipt, ...] = ()
    trusted_receipt_sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _scope(self.scope)
        _ids(self.trusted_receipt_sources, "trusted_receipt_sources")
        if not isinstance(self.frames, tuple) or not isinstance(self.receipts, tuple):
            raise OnboardMultimodalError("ledger entries must be immutable tuples")
        if len(self.frames) > 4096 or len(self.receipts) > 4096:
            raise OnboardMultimodalError("ledger exceeds development evidence bound")
        frames: dict[str, OnboardImageFrame] = {}
        previous_time = -1.0
        for frame in self.frames:
            if not isinstance(frame, OnboardImageFrame) or frame.scope != self.scope:
                raise OnboardMultimodalError("ledger frame crosses scope")
            if frame.frame_id in frames or frame.captured_at_s <= previous_time:
                raise OnboardMultimodalError("ledger observations must be unique and ordered")
            frames[frame.frame_id] = frame
            previous_time = frame.captured_at_s
        receipts: dict[str, OnboardEvidenceReceipt] = {}
        latest: dict[str, OnboardEvidenceReceipt] = {}
        previous_time = -1.0
        for receipt in self.receipts:
            if not isinstance(receipt, OnboardEvidenceReceipt) or receipt.scope != self.scope:
                raise OnboardMultimodalError("ledger receipt crosses scope")
            if receipt.evidence_id in receipts or receipt.observed_at_s < previous_time:
                raise OnboardMultimodalError("ledger receipts must be unique and ordered")
            if any(
                item not in frames or frames[item].captured_at_s > receipt.observed_at_s
                for item in receipt.frame_ids
            ):
                raise OnboardMultimodalError("receipt cites unknown or future observations")
            prior = latest.get(receipt.proposal_id)
            if receipt.stage == "proposed":
                if prior is not None or receipt.prior_evidence_id is not None:
                    raise OnboardMultimodalError("proposal lineage must start once without a prior")
            elif (
                receipt.source_id not in self.trusted_receipt_sources
                or prior is None
                or receipt.prior_evidence_id != prior.evidence_id
                or _STAGES.index(receipt.stage) != _STAGES.index(prior.stage) + 1
                or receipt.observed_at_s < prior.observed_at_s
            ):
                raise OnboardMultimodalError("execution evidence requires trusted causal receipts")
            receipts[receipt.evidence_id] = receipt
            latest[receipt.proposal_id] = receipt
            previous_time = receipt.observed_at_s

    def to_mapping(self) -> dict[str, object]:
        return {
            "contract": "multidetect.onboard-mission-evidence-ledger-v1",
            "scope_sha256": self.scope.canonical_hash,
            "observations": [item.to_mapping() for item in self.frames],
            "receipts": [item.to_mapping() for item in self.receipts],
            "trusted_receipt_sources": list(self.trusted_receipt_sources),
            "receipt_authentication": "caller_verified_not_proven_by_hash",
        }


@dataclass(frozen=True, slots=True)
class OnboardMultimodalInput:
    scope: Scope
    prompt: PromptEnvelope
    frames: tuple[OnboardImageFrame, ...]
    ledger: MissionEvidenceLedger
    requested_at_s: float
    deadline_at_s: float
    max_frame_age_s: float = 5.0
    max_gap_s: float = 5.0
    prompt_text_original: str | None = None

    def __post_init__(self) -> None:
        _scope(self.scope)
        if not isinstance(self.prompt, PromptEnvelope):
            raise TypeError("prompt must be PromptEnvelope")
        if (
            self.prompt.canonical_hash != self.scope.prompt_sha256
            or self.prompt.aircraft_id != self.scope.aircraft_id
        ):
            raise OnboardMultimodalError("prompt hash/aircraft does not match scope")
        if self.prompt_text_original is not None and (
            not isinstance(self.prompt_text_original, str)
            or self.prompt_text_original.strip() != self.prompt.text
        ):
            raise OnboardMultimodalError("original prompt text does not match PromptEnvelope")
        require_real(self.requested_at_s, "requested_at_s", minimum=0)
        deadline = require_real(self.deadline_at_s, "deadline_at_s", minimum=self.requested_at_s)
        if deadline == self.requested_at_s:
            raise OnboardMultimodalError("inference deadline must be strictly later than request")
        if require_real(self.max_frame_age_s, "max_frame_age_s", minimum=0) == 0:
            raise OnboardMultimodalError("max_frame_age_s must be positive")
        if require_real(self.max_gap_s, "max_gap_s", minimum=0) == 0:
            raise OnboardMultimodalError("max_gap_s must be positive")
        if not isinstance(self.frames, tuple) or not 1 <= len(self.frames) <= 8:
            raise OnboardMultimodalError("input requires 1..8 immutable real image frames")
        if not isinstance(self.ledger, MissionEvidenceLedger) or self.ledger.scope != self.scope:
            raise OnboardMultimodalError("ledger scope mismatch")
        known = {item.frame_id: item.to_mapping() for item in self.ledger.frames}
        ids: set[str] = set()
        previous_time = None
        for frame in self.frames:
            if not isinstance(frame, OnboardImageFrame) or frame.scope != self.scope:
                raise OnboardMultimodalError("image history crosses scope")
            if frame.frame_id in ids or known.get(frame.frame_id) != frame.to_mapping():
                raise OnboardMultimodalError("input observation is duplicate or absent from ledger")
            if (
                previous_time is not None
                and not 0 < frame.captured_at_s - previous_time <= self.max_gap_s
            ):
                raise OnboardMultimodalError(
                    "history time regressed, repeated or exceeded gap bound"
                )
            ids.add(frame.frame_id)
            previous_time = frame.captured_at_s
        if any(item.captured_at_s > self.requested_at_s for item in self.ledger.frames) or any(
            item.observed_at_s > self.requested_at_s for item in self.ledger.receipts
        ):
            raise OnboardMultimodalError("ledger contains future evidence")
        self.validate_freshness(self.requested_at_s)

    def validate_freshness(self, at_s: float) -> None:
        require_real(at_s, "at_s", minimum=self.requested_at_s)
        if at_s > self.deadline_at_s or any(
            not 0 <= at_s - item.captured_at_s <= self.max_frame_age_s for item in self.frames
        ):
            raise OnboardMultimodalError(
                "image history is future, expired or past inference deadline"
            )

    def to_mapping(self) -> dict[str, object]:
        original = (
            self.prompt.text if self.prompt_text_original is None else self.prompt_text_original
        )
        return {
            "schema_version": 1,
            "contract": INPUT_CONTRACT,
            "scope": self.scope.to_mapping(),
            "scope_sha256": self.scope.canonical_hash,
            "prompt": self.prompt.to_mapping(),
            "prompt_envelope_sha256": self.prompt.canonical_hash,
            "prompt_original_text": original,
            "prompt_text_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
            "frames": [item.to_mapping() for item in self.frames],
            "ledger": self.ledger.to_mapping(),
            "requested_at_s": self.requested_at_s,
            "deadline_at_s": self.deadline_at_s,
            "max_frame_age_s": self.max_frame_age_s,
            "max_gap_s": self.max_gap_s,
            "usage": "development_shadow_only",
            "training_ready": False,
            "execution_authority": False,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class OnboardMultimodalIntent:
    input_sha256: str
    scope_sha256: str
    frame_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    output: OnboardModelOutput
    completed_at_s: float
    inference_latency_ms: float

    def __post_init__(self) -> None:
        require_sha256(self.input_sha256, "input_sha256")
        require_sha256(self.scope_sha256, "scope_sha256")
        _ids(self.frame_ids, "frame_ids", nonempty=True)
        _ids(self.evidence_ids, "evidence_ids")
        if not isinstance(self.output, OnboardModelOutput):
            raise TypeError("output must be OnboardModelOutput")
        require_real(self.completed_at_s, "completed_at_s", minimum=0)
        require_real(self.inference_latency_ms, "inference_latency_ms", minimum=0)

    def to_mapping(self) -> dict[str, object]:
        output = self.output
        return {
            "schema_version": 1,
            "contract": INTENT_CONTRACT,
            "input_sha256": self.input_sha256,
            "scope_sha256": self.scope_sha256,
            "frame_ids": list(self.frame_ids),
            "evidence_ids": list(self.evidence_ids),
            "output": {
                "response": output.response.value,
                "reason": output.reason,
                "confidence": output.confidence,
                "task_action": None if output.task_action is None else output.task_action.value,
                "flight_plan": None
                if output.flight_plan is None
                else output.flight_plan.to_mapping(),
            },
            "completed_at_s": self.completed_at_s,
            "inference_latency_ms": self.inference_latency_ms,
            "execution_authority": False,
            "completion_evidence_created": False,
        }


def parse_onboard_multimodal_intent(
    payload: str | bytes,
    model_input: OnboardMultimodalInput,
    *,
    completed_at_s: float,
    inference_latency_ms: float,
    expected_scope: Scope,
) -> OnboardMultimodalIntent:
    """Parse only model intent; timing/trust come from the calling host, not JSON."""
    if type(payload) not in {str, bytes} or not 0 < len(payload) <= 1024 * 1024:
        raise OnboardMultimodalError("intent payload must be bounded JSON text or bytes")
    if not isinstance(model_input, OnboardMultimodalInput):
        raise TypeError("model_input must be OnboardMultimodalInput")
    _scope(expected_scope)
    if expected_scope != model_input.scope:
        raise OnboardMultimodalError("current scope changed during inference")
    model_input.validate_freshness(completed_at_s)
    latency = require_real(inference_latency_ms, "inference_latency_ms", minimum=0)
    if latency > (completed_at_s - model_input.requested_at_s) * 1000 + 1e-6:
        raise OnboardMultimodalError("measured latency exceeds the host observation interval")
    if latency > (model_input.deadline_at_s - model_input.requested_at_s) * 1000:
        raise OnboardMultimodalError("actual inference latency exceeded deadline budget")
    raw = strict_object(
        strict_json_loads(payload, name="multimodal intent"),
        name="multimodal intent",
        required=frozenset(
            {
                "schema_version",
                "contract",
                "input_sha256",
                "scope_sha256",
                "frame_ids",
                "evidence_ids",
                "output",
            }
        ),
    )
    canonical_sha256(raw)
    require_schema_version(raw["schema_version"])
    if raw["contract"] != INTENT_CONTRACT:
        raise OnboardMultimodalError("unsupported multimodal intent contract")
    if (
        raw["input_sha256"] != model_input.canonical_hash
        or raw["scope_sha256"] != expected_scope.canonical_hash
    ):
        raise OnboardMultimodalError("intent did not echo the exact input/scope hashes")
    for name in ("frame_ids", "evidence_ids"):
        if not isinstance(raw[name], list):
            raise OnboardMultimodalError(f"{name} must be a JSON array")
    frame_ids = _ids(tuple(raw["frame_ids"]), "frame_ids", nonempty=True)
    evidence_ids = _ids(tuple(raw["evidence_ids"]), "evidence_ids")
    if not set(frame_ids) <= {item.frame_id for item in model_input.frames} or not set(
        evidence_ids
    ) <= {item.evidence_id for item in model_input.ledger.receipts}:
        raise OnboardMultimodalError("intent cites unknown or unprovided evidence")
    output = OnboardModelOutput.from_mapping(raw["output"])
    if output.flight_plan is not None:
        if output.flight_plan.reference_id is not None:
            raise OnboardMultimodalError(
                "development v1 has no typed grounded target registry; reference_id must be null"
            )
        if output.task_action is not None:
            from .onboard_onnx import is_task_flight_compatible

            if not is_task_flight_compatible(output.task_action, output.flight_plan.action):
                raise OnboardMultimodalError(
                    "task/flight intent violates existing compatibility contract"
                )
    return OnboardMultimodalIntent(
        model_input.canonical_hash,
        expected_scope.canonical_hash,
        frame_ids,
        evidence_ids,
        output,
        completed_at_s,
        latency,
    )
