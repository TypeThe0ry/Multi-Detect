from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from .domain import Detection, SensorKind

RGB_FIRE_EVIDENCE_CONTRACT_VERSION = 1
_FIRE_EVIDENCE_LABELS = frozenset({"fire", "flame", "smoke", "smoldering_area", "burned_area"})
_EVIDENCE_METADATA_PREFIX = "independent_rgb_"
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def _canonical_label(label: str) -> str:
    normalized = label.strip().lower()
    return "flame" if normalized == "fire" else normalized


@dataclass(frozen=True, slots=True)
class IndependentRgbFireCorroborationConfig:
    minimum_iou: float = 0.3
    minimum_verifier_confidence: float = 0.65
    evidence_qualified: bool = False
    primary_artifact_sha256: str | None = None
    verifier_artifact_sha256: str | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.minimum_iou) or not 0.0 < self.minimum_iou <= 1.0:
            raise ValueError("independent RGB minimum IoU must be in (0, 1]")
        if (
            not math.isfinite(self.minimum_verifier_confidence)
            or not 0.0 <= self.minimum_verifier_confidence <= 1.0
        ):
            raise ValueError("independent RGB verifier confidence must be in [0, 1]")
        if not isinstance(self.evidence_qualified, bool):
            raise ValueError("independent RGB evidence_qualified must be a boolean")
        primary_digest = self._normalize_digest(
            self.primary_artifact_sha256,
            "primary_artifact_sha256",
        )
        verifier_digest = self._normalize_digest(
            self.verifier_artifact_sha256,
            "verifier_artifact_sha256",
        )
        object.__setattr__(self, "primary_artifact_sha256", primary_digest)
        object.__setattr__(self, "verifier_artifact_sha256", verifier_digest)
        if self.evidence_qualified:
            if primary_digest is None or verifier_digest is None:
                raise ValueError("qualified independent RGB evidence requires both artifact hashes")
            if primary_digest == verifier_digest:
                raise ValueError("primary and verifier RGB fire artifacts must be different")

    @staticmethod
    def _normalize_digest(value: str | None, name: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a SHA-256 string")
        normalized = value.strip().lower()
        if not _SHA256_PATTERN.fullmatch(normalized):
            raise ValueError(f"{name} must contain 64 lowercase hexadecimal characters")
        return normalized


@dataclass(frozen=True, slots=True)
class IndependentRgbFireCorroborationResult:
    detections: tuple[Detection, ...]
    primary_fire_candidate_count: int
    verifier_candidate_count: int
    eligible_verifier_candidate_count: int
    corroborated_detection_count: int
    rejected_same_model_count: int
    evidence_qualified: bool


class IndependentRgbFireCorroborator:
    """Bind fire candidates to a distinct, manifest-qualified RGB verifier.

    Verifier-only observations are never emitted. Upstream corroboration metadata is
    removed before matching, so a primary detector cannot assert its own independent
    evidence at this boundary.
    """

    def __init__(self, config: IndependentRgbFireCorroborationConfig) -> None:
        self.config = config

    def corroborate(
        self,
        primary_detections: Sequence[Detection],
        verifier_detections: Sequence[Detection],
    ) -> IndependentRgbFireCorroborationResult:
        sanitized = tuple(_without_independent_rgb_evidence(item) for item in primary_detections)
        fire_indices = tuple(
            index
            for index, detection in enumerate(sanitized)
            if _canonical_label(detection.label) in _FIRE_EVIDENCE_LABELS
        )
        verifier_candidates = tuple(
            detection
            for detection in verifier_detections
            if detection.sensor is SensorKind.RGB
            and _canonical_label(detection.label) in _FIRE_EVIDENCE_LABELS
        )
        rejected_same_model_count = 0
        eligible: list[Detection] = []
        for verifier in verifier_candidates:
            if verifier.confidence < self.config.minimum_verifier_confidence:
                continue
            if any(
                sanitized[index].model_version == verifier.model_version
                for index in fire_indices
                if _canonical_label(sanitized[index].label) == _canonical_label(verifier.label)
            ):
                rejected_same_model_count += 1
                continue
            eligible.append(verifier)

        matches: dict[int, tuple[int, float]] = {}
        if self.config.evidence_qualified:
            candidates: list[tuple[float, int, int]] = []
            for primary_index in fire_indices:
                primary = sanitized[primary_index]
                for verifier_index, verifier in enumerate(eligible):
                    if _canonical_label(primary.label) != _canonical_label(verifier.label):
                        continue
                    if primary.model_version == verifier.model_version:
                        continue
                    overlap = primary.bbox.iou(verifier.bbox)
                    if overlap >= self.config.minimum_iou:
                        candidates.append((overlap, primary_index, verifier_index))
            used_verifiers: set[int] = set()
            for overlap, primary_index, verifier_index in sorted(
                candidates,
                key=lambda item: (-item[0], item[1], item[2]),
            ):
                if primary_index in matches or verifier_index in used_verifiers:
                    continue
                matches[primary_index] = (verifier_index, overlap)
                used_verifiers.add(verifier_index)

        output: list[Detection] = []
        for index, detection in enumerate(sanitized):
            match = matches.get(index)
            if match is None:
                output.append(detection)
                continue
            verifier_index, overlap = match
            verifier = eligible[verifier_index]
            metadata = dict(detection.metadata)
            metadata.update(
                {
                    "independent_rgb_corroborated": True,
                    "independent_rgb_evidence_contract_version": (
                        RGB_FIRE_EVIDENCE_CONTRACT_VERSION
                    ),
                    "independent_rgb_iou": overlap,
                    "independent_rgb_confidence": verifier.confidence,
                    "independent_rgb_label": _canonical_label(verifier.label),
                    "independent_rgb_verifier_model_version": verifier.model_version,
                    "independent_rgb_primary_artifact_sha256": (
                        self.config.primary_artifact_sha256
                    ),
                    "independent_rgb_verifier_artifact_sha256": (
                        self.config.verifier_artifact_sha256
                    ),
                }
            )
            output.append(
                Detection(
                    label=detection.label,
                    confidence=detection.confidence,
                    bbox=detection.bbox,
                    sensor=detection.sensor,
                    model_version=detection.model_version,
                    metadata=metadata,
                )
            )

        return IndependentRgbFireCorroborationResult(
            detections=tuple(output),
            primary_fire_candidate_count=len(fire_indices),
            verifier_candidate_count=len(verifier_candidates),
            eligible_verifier_candidate_count=len(eligible),
            corroborated_detection_count=len(matches),
            rejected_same_model_count=rejected_same_model_count,
            evidence_qualified=self.config.evidence_qualified,
        )

    def fail_closed(
        self,
        primary_detections: Sequence[Detection],
    ) -> IndependentRgbFireCorroborationResult:
        """Remove all prior corroboration claims when verifier inference is unavailable."""

        return self.corroborate(primary_detections, ())


def _without_independent_rgb_evidence(detection: Detection) -> Detection:
    metadata = {
        key: value
        for key, value in detection.metadata.items()
        if not key.startswith(_EVIDENCE_METADATA_PREFIX)
    }
    if _canonical_label(detection.label) in _FIRE_EVIDENCE_LABELS:
        metadata["independent_rgb_corroborated"] = False
    if metadata == detection.metadata:
        return detection
    return Detection(
        label=detection.label,
        confidence=detection.confidence,
        bbox=detection.bbox,
        sensor=detection.sensor,
        model_version=detection.model_version,
        metadata=metadata,
    )


def is_qualified_independent_rgb_fire_evidence(detection: Detection) -> bool:
    """Validate the complete, artifact-bound independent RGB evidence contract.

    This deliberately rejects the legacy boolean-only marker. Live inference owns
    evidence construction; downstream tracking only consumes a fully bound record.
    """

    metadata = detection.metadata
    if detection.sensor is not SensorKind.RGB:
        return False
    if _canonical_label(detection.label) not in _FIRE_EVIDENCE_LABELS:
        return False
    if metadata.get("independent_rgb_corroborated") is not True:
        return False
    version = metadata.get("independent_rgb_evidence_contract_version")
    if isinstance(version, bool) or version != RGB_FIRE_EVIDENCE_CONTRACT_VERSION:
        return False

    verifier_label = metadata.get("independent_rgb_label")
    if not isinstance(verifier_label, str):
        return False
    if _canonical_label(verifier_label) != _canonical_label(detection.label):
        return False

    verifier_model_version = metadata.get("independent_rgb_verifier_model_version")
    if not isinstance(verifier_model_version, str) or not verifier_model_version.strip():
        return False
    if verifier_model_version.strip() == detection.model_version.strip():
        return False

    overlap = metadata.get("independent_rgb_iou")
    verifier_confidence = metadata.get("independent_rgb_confidence")
    if isinstance(overlap, bool) or not isinstance(overlap, (int, float)):
        return False
    if isinstance(verifier_confidence, bool) or not isinstance(verifier_confidence, (int, float)):
        return False
    if not math.isfinite(float(overlap)) or not 0.0 < float(overlap) <= 1.0:
        return False
    if (
        not math.isfinite(float(verifier_confidence))
        or not 0.0 <= float(verifier_confidence) <= 1.0
    ):
        return False

    primary_digest = metadata.get("independent_rgb_primary_artifact_sha256")
    verifier_digest = metadata.get("independent_rgb_verifier_artifact_sha256")
    if not isinstance(primary_digest, str) or not isinstance(verifier_digest, str):
        return False
    primary_digest = primary_digest.strip().lower()
    verifier_digest = verifier_digest.strip().lower()
    return bool(
        _SHA256_PATTERN.fullmatch(primary_digest)
        and _SHA256_PATTERN.fullmatch(verifier_digest)
        and primary_digest != verifier_digest
    )


__all__ = [
    "IndependentRgbFireCorroborationConfig",
    "IndependentRgbFireCorroborationResult",
    "IndependentRgbFireCorroborator",
    "RGB_FIRE_EVIDENCE_CONTRACT_VERSION",
    "is_qualified_independent_rgb_fire_evidence",
]
