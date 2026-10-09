"""Shared prompt/RGB actor messages, not a trainer, backend or flight interface.

The actor owns immutable image bytes, never caller-owned mutable PIL objects.
Source IDs, pose, gold targets and transport identities stay outside messages.
Caller admission supplies observable chronology; an increasing sequence alone
does not authenticate a sensor. Processor-token parity requires a separate
official-processor check: this module establishes only the message boundary.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .onboard_navigation_supervision import (
    TARGET_CONTRACT,
    NavigationFrame,
    NavigationSourceFile,
    NavigationSourceValidationSession,
    NavigationSupervisionBudget,
    NavigationSupervisionSample,
)
from .serialization import AutonomyContractError, canonical_sha256, require_int, require_sha256

ACTOR_CONTRACT = "multidetect.onboard-navigation-actor-v1"
ACTOR_SYSTEM_PROMPT = (
    "Predict only the next externally planned navigation primitive from the original instruction "
    "and actual RGB observations, ordered oldest to current. Return one JSON object with exactly "
    "schema_version, contract, kind, translation_frame, yaw_convention, translation_m and "
    "yaw_delta_rad. schema_version must be 1 and contract must be "
    f"'{TARGET_CONTRACT}'. translation_frame must be 'body_frd' and yaw_convention must be "
    "'clockwise_right_positive'. kind must be translate, turn_yaw or stop, mutually exclusively. "
    "For translate: translation_m is [forward,right,down] in metres or null when unknown; "
    "yaw_delta_rad must be null, never zero or a simultaneous yaw. A known translation must be "
    "nonzero, finite, and bounded to 500 metres per component. For turn_yaw: translation_m must "
    "be null; yaw_delta_rad is a finite nonzero angle within [-pi,pi] radians or null when "
    "unknown. "
    "For stop: translation_m and yaw_delta_rad must both be null. Unknown metric quantities "
    "stay null; do not guess them or replace them with zero. STOP means only external navigation "
    "sequence stop, not landing, arrival or task completion. Do not emit confidence, speed, "
    "duration, telemetry, phase, transport or flight commands."
)
_DEFAULT_BUDGET = NavigationSupervisionBudget()


class NavigationActorError(AutonomyContractError):
    """Reject unbound, malformed, over-budget or nonchronological actor input."""


@dataclass(frozen=True, slots=True)
class NavigationActorFrame:
    """Bound source RGB; sequence is caller metadata, never model text."""

    sequence: int
    image_bytes: bytes
    byte_count: int
    sha256: str
    width: int
    height: int
    encoding: str

    def __post_init__(self) -> None:
        require_int(self.sequence, "actor frame sequence", minimum=0)
        require_int(self.byte_count, "actor frame byte_count", minimum=1)
        require_int(self.width, "actor frame width", minimum=1)
        require_int(self.height, "actor frame height", minimum=1)
        if type(self.image_bytes) is not bytes or len(self.image_bytes) != self.byte_count:
            raise NavigationActorError("actor image bytes require immutable exact bound length")
        if require_sha256(self.sha256, "actor frame sha256") != self.sha256:
            raise NavigationActorError("actor frame SHA-256 must be canonical lowercase")
        if hashlib.sha256(self.image_bytes).hexdigest() != self.sha256:
            raise NavigationActorError("actual actor image SHA-256 differs")
        if type(self.encoding) is not str or self.encoding not in {"rgb8", "png", "jpeg"}:
            raise NavigationActorError("actor admits only actual RGB8, PNG or JPEG")
        if self.encoding == "rgb8" and self.byte_count != self.width * self.height * 3:
            raise NavigationActorError("RGB8 requires exactly width*height*3 bytes")

    @classmethod
    def from_observation(
        cls, *, sequence, image_bytes, byte_count, sha256, width, height, encoding="rgb8"
    ):
        """The caller must bind these bytes to a real, admitted observation."""
        return cls(sequence, image_bytes, byte_count, sha256, width, height, encoding)

    def decode(self, budget: NavigationSupervisionBudget):
        """Fresh RGB image per call; never resize or normalize source pixels."""
        if not isinstance(budget, NavigationSupervisionBudget):
            raise TypeError("actor budget must be NavigationSupervisionBudget")
        if self.byte_count > budget.max_image_bytes:
            raise NavigationActorError("actor frame exceeds per-image byte budget")
        if (
            self.width > budget.max_image_side
            or self.height > budget.max_image_side
            or self.width * self.height > budget.max_image_pixels
        ):
            raise NavigationActorError("actor frame exceeds per-image dimension/pixel budget")
        if self.encoding == "rgb8":
            try:
                from PIL import Image
            except ImportError as exc:
                raise NavigationActorError("actor RGB decoding requires optional Pillow") from exc
            return Image.frombytes("RGB", (self.width, self.height), self.image_bytes)
        # Reuse the frozen source decoder's format/mode/dimensions/animation and
        # decompression-bomb checks. Its fixed metadata never enters messages.
        frame = NavigationFrame(
            frame_id="actor-frame",
            episode_id="actor-source",
            scene_id="actor-source",
            step_index=self.sequence,
            path="actor-source." + self.encoding,
            bytes=self.byte_count,
            sha256=self.sha256,
            width=self.width,
            height=self.height,
            encoding=self.encoding,
            source_clock_id=None,
            source_stamp=None,
            stamp_unit=None,
        )
        decoded = frame.decode(self.image_bytes, budget)
        try:
            from PIL import Image

            # Container text/EXIF is source provenance, not an actor channel.
            return Image.frombytes("RGB", decoded.size, decoded.tobytes())
        finally:
            decoded.close()


@dataclass(frozen=True, slots=True)
class NavigationActorInput:
    """Immutable minimal input; last observation is the declared current one."""

    prompt_original: str
    current_sequence: int
    frames: tuple[NavigationActorFrame, ...]
    budget: NavigationSupervisionBudget = _DEFAULT_BUDGET

    def __post_init__(self) -> None:
        if not isinstance(self.budget, NavigationSupervisionBudget):
            raise TypeError("actor budget must be NavigationSupervisionBudget")
        if type(self.prompt_original) is not str or not self.prompt_original.strip():
            raise NavigationActorError("actor requires a nonblank original prompt")
        if len(self.prompt_original.encode("utf-8")) > self.budget.max_prompt_bytes:
            raise NavigationActorError("actor prompt exceeds UTF-8 byte budget")
        require_int(self.current_sequence, "actor current_sequence", minimum=0)
        if (
            type(self.frames) is not tuple
            or not 1 <= len(self.frames) <= 2
            or any(type(frame) is not NavigationActorFrame for frame in self.frames)
        ):
            raise NavigationActorError("actor requires 1-2 immutable bound RGB frames")
        sequences = [frame.sequence for frame in self.frames]
        if any(value > self.current_sequence for value in sequences):
            raise NavigationActorError("actor cannot contain a future frame")
        if any(a >= b for a, b in zip(sequences, sequences[1:], strict=False)):
            raise NavigationActorError("actor frame sequences must strictly ascend")
        if sequences[-1] != self.current_sequence:
            raise NavigationActorError("actor frame history must end at current sequence")
        if sum(frame.byte_count for frame in self.frames) > self.budget.max_total_image_bytes:
            raise NavigationActorError("actor images exceed total source byte budget")
        if sum(frame.width * frame.height for frame in self.frames) > self.budget.max_total_pixels:
            raise NavigationActorError("actor images exceed total pixel budget")
        for frame in self.frames:
            image = frame.decode(self.budget)
            image.close()

    @classmethod
    def from_observation(
        cls, *, prompt_original, current_sequence, frames, budget=_DEFAULT_BUDGET
    ):
        """No metadata/gold kwargs are accepted; outer admission owns authenticity."""
        return cls(prompt_original, current_sequence, frames, budget)

    @classmethod
    def from_supervision_sample(
        cls, sample, artifact_root: Path, *, budget=_DEFAULT_BUDGET, session=None
    ):
        """Project validated supervision to the same prompt/RGB boundary.

        With session=None, source validation finishes its final SHA check here.
        A supplied primed NavigationSourceValidationSession defers that final
        source rehash to context exit. The caller must require session.verified
        before publishing evidence or authorizing any downstream use. Each RGB
        is always reread with exact bytes/SHA after the existing validation.
        """
        if not isinstance(sample, NavigationSupervisionSample):
            raise TypeError("sample must be NavigationSupervisionSample")
        if not isinstance(budget, NavigationSupervisionBudget):
            raise TypeError("actor budget must be NavigationSupervisionBudget")
        if session is not None:
            if not isinstance(session, NavigationSourceValidationSession):
                raise TypeError("session must be NavigationSourceValidationSession")
            if session.budget != budget:
                raise NavigationActorError(
                    "source validation session budget differs from actor budget"
                )
        images = sample.validate_sources(artifact_root, budget=budget, session=session)
        for image in images:
            image.close()
        frames = tuple(
            NavigationActorFrame(
                sequence=frame.step_index,
                image_bytes=NavigationSourceFile(frame.path, frame.bytes, frame.sha256).read(
                    artifact_root, maximum_bytes=budget.max_image_bytes
                ),
                byte_count=frame.bytes,
                sha256=frame.sha256,
                width=frame.width,
                height=frame.height,
                encoding=frame.encoding,
            )
            for frame in sample.frames
        )
        return cls(sample.prompt.text_original, sample.step_index, frames, budget)

    def to_identity_mapping(self) -> dict:
        """Actor-message identity only, not source/request/transport identity."""
        bindings = []
        for frame in self.frames:
            image = frame.decode(self.budget)
            try:
                bindings.append(
                    {
                        "mode": image.mode,
                        "width": image.width,
                        "height": image.height,
                        "rgb_pixels_sha256": hashlib.sha256(image.tobytes()).hexdigest(),
                    }
                )
            finally:
                image.close()
        return {
            "actor_contract": ACTOR_CONTRACT,
            "system_prompt": ACTOR_SYSTEM_PROMPT,
            "prompt_original": self.prompt_original,
            "ordered_rgb_pixel_bindings": bindings,
        }

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.to_identity_mapping())


def serialize_navigation_actor_messages(actor: NavigationActorInput) -> list[dict]:
    """One message builder for training prefixes and inference; no gold target.

    Training appends its assistant target in a separate label-only path. Never
    call an old sample encode method (which also consumes gold) for inference.
    """
    if type(actor) is not NavigationActorInput:
        raise TypeError("actor must be NavigationActorInput")
    content = [{"type": "text", "text": actor.prompt_original}]
    images = []
    try:
        for index, frame in enumerate(actor.frames):
            image = frame.decode(actor.budget)
            images.append(image)
            content.extend(
                [
                    {
                        "type": "text",
                        "text": "Current RGB observation"
                        if index == len(actor.frames) - 1
                        else "Most recent past RGB observation",
                    },
                    {"type": "image", "image": image},
                ]
            )
    except Exception:
        for image in images:
            image.close()
        raise
    return [
        {"role": "system", "content": [{"type": "text", "text": ACTOR_SYSTEM_PROMPT}]},
        {"role": "user", "content": content},
    ]


__all__ = [
    "ACTOR_CONTRACT",
    "ACTOR_SYSTEM_PROMPT",
    "NavigationActorError",
    "NavigationActorFrame",
    "NavigationActorInput",
    "serialize_navigation_actor_messages",
]
