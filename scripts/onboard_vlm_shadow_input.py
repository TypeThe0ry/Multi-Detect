"""Bind real local RGB bytes to the shared, non-executing multimodal contract.

This adapter deliberately has no aircraft telemetry, synthetic task labels,
execution receipts or flight transport. Its explicit clock is an offline replay
clock: elapsed host inference time is added to the last source frame time.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from io import BytesIO
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from multidetect.autonomy.contracts import PromptEnvelope
from multidetect.autonomy.onboard_multimodal import (
    MAX_IMAGE_BYTES,
    MissionEvidenceLedger,
    OnboardImageFrame,
    OnboardMultimodalInput,
    OnboardMultimodalScope,
)
from multidetect.autonomy.serialization import (
    AutonomyContractError,
    canonical_sha256,
    require_real,
    require_sha256,
    require_text,
)


def prepare_shadow_case(
    case: Mapping[str, object],
    *,
    repository_root: Path,
    deadline_s: float = 30.0,
) -> OnboardMultimodalInput:
    """Validate source bytes and preserve missing context, without using labels.

    Frames use repository-relative paths and a single caller-labelled clock.
    ``evaluation_metadata`` is never read, hashed into model input or supplied
    to the model. Changing the task text creates a separate inference scope.
    """
    if not isinstance(case, Mapping):
        raise AutonomyContractError("shadow case must be an object")
    case_id = require_text(case.get("case_id"), "case_id")
    original_prompt = case.get("prompt")
    prompt_text = require_text(original_prompt, "prompt")
    budget = require_real(deadline_s, "deadline_s", minimum=0.01, maximum=300.0)
    raw_frames = case.get("frames")
    if not isinstance(raw_frames, list) or not 1 <= len(raw_frames) <= 8:
        raise AutonomyContractError("shadow case requires 1..8 frames")
    root = Path(repository_root).resolve(strict=True)
    if not root.is_dir():
        raise AutonomyContractError("repository_root must be a directory")
    decoded: list[tuple[str, float, bytes, str, int, int, str]] = []
    domains: set[str] = set()
    clips: set[str] = set()
    source_frame_ids: set[str] = set()
    for raw in raw_frames:
        if not isinstance(raw, Mapping):
            raise AutonomyContractError("shadow frame must be an object")
        frame_id = require_text(raw.get("frame_id"), "frame_id")
        if frame_id in source_frame_ids:
            raise AutonomyContractError("duplicate source frame identity")
        source_frame_ids.add(frame_id)
        relative = Path(require_text(raw.get("path"), "path"))
        if relative.is_absolute() or relative.drive or ".." in relative.parts:
            raise AutonomyContractError("frame path must stay repository-relative")
        path = (root / relative).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise AutonomyContractError("frame resolved outside the repository or is not a file")
        if not 0 < path.stat().st_size <= MAX_IMAGE_BYTES:
            raise AutonomyContractError("frame file exceeds the bounded image input")
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if digest != require_sha256(raw.get("sha256"), "sha256"):
            raise AutonomyContractError("shadow image file SHA256 mismatch")
        captured = require_real(raw.get("captured_at_s"), "captured_at_s", minimum=0)
        domain = require_text(raw.get("clock_domain"), "clock_domain")
        domains.add(domain)
        clip = require_text(raw.get("source_clip_id"), "source_clip_id")
        clips.add(clip)
        from PIL import Image

        with Image.open(BytesIO(payload)) as image:
            encoding = {"PNG": "png", "JPEG": "jpeg"}.get(image.format)
            if encoding is None:
                raise AutonomyContractError("shadow frame must be a PNG or JPEG RGB image")
            width, height = image.size
        for name, actual in (("width", width), ("height", height), ("encoding", encoding)):
            if name in raw and raw[name] != actual:
                raise AutonomyContractError(f"shadow frame manifest {name} mismatch")
        opaque_frame_id = (
            "rgb-"
            + canonical_sha256(
                {
                    "source_frame_id": frame_id,
                    "source_clip_id": clip,
                    "source_clock_domain": domain,
                    "captured_at_s": captured,
                    "image_sha256": digest,
                }
            )[:20]
        )
        decoded.append((opaque_frame_id, captured, payload, digest, width, height, encoding))
    if len(domains) != 1:
        raise AutonomyContractError("shadow history crosses source clock domains")
    if len(clips) > 1:
        raise AutonomyContractError("shadow history crosses source recordings")

    # This is an explicit protocol fixture timestamp, not a claimed camera UTC.
    issued_at = case.get("prompt_issued_at", "2026-10-07T00:00:00Z")
    aircraft_id = "offline-rgb-shadow-no-aircraft"
    prompt = PromptEnvelope(
        prompt_id=str(uuid5(NAMESPACE_URL, f"multidetect:shadow-prompt:{case_id}:{prompt_text}")),
        text=prompt_text,
        language="zh" if any("\u4e00" <= char <= "\u9fff" for char in prompt_text) else "en",
        issued_at=issued_at,
        operator_id="offline-shadow-evaluator",
        aircraft_id=aircraft_id,
    )
    source_identity = canonical_sha256(
        {
            "case_id": case_id,
            "prompt_sha256": prompt.canonical_hash,
            "frames": [
                {"frame_id": row[0], "captured_at_s": row[1], "image_sha256": row[3]}
                for row in decoded
            ],
            "source_clock_domain": next(iter(domains)),
            "source_clip_id": next(iter(clips)),
        }
    )
    scope = OnboardMultimodalScope(
        session_id=str(uuid5(NAMESPACE_URL, f"multidetect:rgb-shadow:{source_identity}")),
        prompt_sha256=prompt.canonical_hash,
        aircraft_id=aircraft_id,
        selected_task_id=f"offline-shadow:{source_identity}",
        clock_epoch=f"offline-replay-not-live-sensor:{source_identity}",
    )
    frames = tuple(
        OnboardImageFrame(scope, frame_id, captured, payload, digest, width, height, encoding)
        for frame_id, captured, payload, digest, width, height, encoding in decoded
    )
    requested = frames[-1].captured_at_s
    span = requested - frames[0].captured_at_s
    return OnboardMultimodalInput(
        scope=scope,
        prompt=prompt,
        frames=frames,
        ledger=MissionEvidenceLedger(scope, frames),
        requested_at_s=requested,
        deadline_at_s=requested + budget,
        max_frame_age_s=span + budget,
        max_gap_s=5.0,
        prompt_text_original=original_prompt,
    )
