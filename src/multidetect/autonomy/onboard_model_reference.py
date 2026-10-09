"""Hash-bound reference for the currently selected shared OnboardAI model.

The repository has historical model artifacts and evidence from several
training rounds.  A runtime must not guess which one is current.  This module
loads one small, reviewable JSON reference and verifies every file identity
before returning a model path.  It is intentionally metadata-only: loading a
reference does not promote a candidate, enable actuators, or claim Jetson,
Gazebo, HIL, or real-flight acceptance.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .serialization import require_sha256, strict_json_loads

ONBOARD_MODEL_REFERENCE_CONTRACT = "multidetect.onboard-model-reference-v1"
ONBOARD_MODEL_REFERENCE_SCHEMA_VERSION = 1
DEFAULT_ONBOARD_MODEL_REFERENCE = (
    Path(__file__).resolve().parents[3]
    / "configs"
    / "training"
    / "onboard-policy-current-reference-v1.json"
)


class OnboardModelReferenceError(ValueError):
    """Raised when the selected model reference is missing or inconsistent."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_file(
    value: object,
    *,
    name: str,
    repository_root: Path,
    expected_sha256: str,
    require_exists: bool,
) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise OnboardModelReferenceError(f"{name} must be a repository-relative path")
    path = (repository_root / value).resolve()
    try:
        path.relative_to(repository_root.resolve())
    except ValueError as exc:
        raise OnboardModelReferenceError(f"{name} escapes the repository") from exc
    if require_exists:
        if not path.is_file():
            raise OnboardModelReferenceError(f"{name} does not exist: {path}")
        actual = _sha256(path)
        if actual != expected_sha256:
            raise OnboardModelReferenceError(
                f"{name} SHA-256 mismatch: expected {expected_sha256}, got {actual}"
            )
    return path


def _required_text(document: dict[str, Any], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value.strip():
        raise OnboardModelReferenceError(f"{name} must be a non-empty string")
    return value


def _object(value: object, *, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise OnboardModelReferenceError(f"{name} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise OnboardModelReferenceError(f"{name} keys must be strings")
    return dict(value)


@dataclass(frozen=True, slots=True)
class OnboardModelReference:
    """One selected model and its 3-D training provenance."""

    model_id: str
    model_version: str
    model_path: Path
    model_sha256: str
    training_config_path: Path
    training_config_sha256: str
    training_report_path: Path
    training_report_sha256: str
    gazebo_manifest_path: Path
    gazebo_manifest_sha256: str
    feature_width: int
    status: str
    selected: bool
    final_release_allowed: bool
    physical_performance_claimed: bool
    jetson_evidence: bool
    external_gazebo_evidence: bool
    hil_real_evidence: bool
    outdoor_gnss_evidence: bool

    def to_mapping(self, *, repository_root: Path) -> dict[str, object]:
        root = repository_root.resolve()

        def relative(path: Path) -> str:
            return path.resolve().relative_to(root).as_posix()

        return {
            "schema_version": ONBOARD_MODEL_REFERENCE_SCHEMA_VERSION,
            "contract": ONBOARD_MODEL_REFERENCE_CONTRACT,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "model": {"path": relative(self.model_path), "sha256": self.model_sha256},
            "training_config": {
                "path": relative(self.training_config_path),
                "sha256": self.training_config_sha256,
            },
            "training_report": {
                "path": relative(self.training_report_path),
                "sha256": self.training_report_sha256,
            },
            "gazebo_3d_manifest": {
                "path": relative(self.gazebo_manifest_path),
                "sha256": self.gazebo_manifest_sha256,
            },
            "feature_width": self.feature_width,
            "status": self.status,
            "selected": self.selected,
            "claim_boundary": {
                "final_release_allowed": self.final_release_allowed,
                "physical_performance_claimed": self.physical_performance_claimed,
                "jetson_evidence": self.jetson_evidence,
                "external_gazebo_evidence": self.external_gazebo_evidence,
                "hil_real_evidence": self.hil_real_evidence,
                "outdoor_gnss_evidence": self.outdoor_gnss_evidence,
            },
        }


def load_onboard_model_reference(
    path: str | Path = DEFAULT_ONBOARD_MODEL_REFERENCE,
    *,
    repository_root: str | Path | None = None,
    require_artifacts: bool = True,
) -> OnboardModelReference:
    """Load and hash-check the selected shared OnboardAI reference."""

    reference_path = Path(path).expanduser().resolve()
    root = (
        Path(repository_root).expanduser().resolve()
        if repository_root is not None
        else Path(__file__).resolve().parents[3]
    )
    try:
        reference_path.relative_to(root)
    except ValueError as exc:
        raise OnboardModelReferenceError("reference must be inside the repository") from exc
    try:
        raw = reference_path.read_bytes()
        document = _object(
            strict_json_loads(raw, name="OnboardAI model reference"),
            name="OnboardAI model reference",
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        if isinstance(exc, OnboardModelReferenceError):
            raise
        raise OnboardModelReferenceError(f"cannot read model reference: {reference_path}") from exc

    if document.get("schema_version") != ONBOARD_MODEL_REFERENCE_SCHEMA_VERSION:
        raise OnboardModelReferenceError("unsupported OnboardAI model reference schema")
    if document.get("contract") != ONBOARD_MODEL_REFERENCE_CONTRACT:
        raise OnboardModelReferenceError("unsupported OnboardAI model reference contract")

    model = _object(document.get("model"), name="model")
    training_config = _object(document.get("training_config"), name="training_config")
    training_report = _object(document.get("training_report"), name="training_report")
    manifest = _object(document.get("gazebo_3d_manifest"), name="gazebo_3d_manifest")

    def digest(mapping: dict[str, Any], name: str) -> str:
        try:
            return require_sha256(mapping.get("sha256"), f"{name}.sha256")
        except (TypeError, ValueError) as exc:
            raise OnboardModelReferenceError(str(exc)) from exc

    model_sha = digest(model, "model")
    training_config_sha = digest(training_config, "training_config")
    training_report_sha = digest(training_report, "training_report")
    manifest_sha = digest(manifest, "gazebo_3d_manifest")
    model_path = _relative_file(
        model.get("path"),
        name="model.path",
        repository_root=root,
        expected_sha256=model_sha,
        require_exists=require_artifacts,
    )
    training_config_path = _relative_file(
        training_config.get("path"),
        name="training_config.path",
        repository_root=root,
        expected_sha256=training_config_sha,
        require_exists=require_artifacts,
    )
    training_report_path = _relative_file(
        training_report.get("path"),
        name="training_report.path",
        repository_root=root,
        expected_sha256=training_report_sha,
        require_exists=require_artifacts,
    )
    manifest_path = _relative_file(
        manifest.get("path"),
        name="gazebo_3d_manifest.path",
        repository_root=root,
        expected_sha256=manifest_sha,
        require_exists=require_artifacts,
    )
    feature_width = document.get("feature_width")
    if type(feature_width) is not int or feature_width != 40:
        raise OnboardModelReferenceError("feature_width must be the shared 40-feature contract")
    status = _required_text(document, "status")
    if document.get("selected") is not True:
        raise OnboardModelReferenceError(
            "only the selected reference may be loaded for runtime use"
        )
    boundary = _object(document.get("claim_boundary"), name="claim_boundary")
    flags = (
        "final_release_allowed",
        "physical_performance_claimed",
        "jetson_evidence",
        "external_gazebo_evidence",
        "hil_real_evidence",
        "outdoor_gnss_evidence",
    )
    for flag in flags:
        if type(boundary.get(flag)) is not bool:
            raise OnboardModelReferenceError(f"claim_boundary.{flag} must be boolean")
    if any(boundary[flag] for flag in flags):
        raise OnboardModelReferenceError(
            "development reference cannot claim a release or hardware gate"
        )
    return OnboardModelReference(
        model_id=_required_text(document, "model_id"),
        model_version=_required_text(document, "model_version"),
        model_path=model_path,
        model_sha256=model_sha,
        training_config_path=training_config_path,
        training_config_sha256=training_config_sha,
        training_report_path=training_report_path,
        training_report_sha256=training_report_sha,
        gazebo_manifest_path=manifest_path,
        gazebo_manifest_sha256=manifest_sha,
        feature_width=feature_width,
        status=status,
        selected=True,
        final_release_allowed=False,
        physical_performance_claimed=False,
        jetson_evidence=False,
        external_gazebo_evidence=False,
        hil_real_evidence=False,
        outdoor_gnss_evidence=False,
    )


__all__ = [
    "DEFAULT_ONBOARD_MODEL_REFERENCE",
    "ONBOARD_MODEL_REFERENCE_CONTRACT",
    "ONBOARD_MODEL_REFERENCE_SCHEMA_VERSION",
    "OnboardModelReference",
    "OnboardModelReferenceError",
    "load_onboard_model_reference",
]
