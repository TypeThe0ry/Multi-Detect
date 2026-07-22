"""Auditable robust calibration for outdoor monocular metric-depth estimates.

The runtime must never learn a new scale from arbitrary live detections.  A
calibration document is created offline from measured reference ranges, then
validated again when the live process starts.  This keeps the field fitting
step reproducible and makes an accidental one-point adjustment impossible.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

CALIBRATION_DOCUMENT_SCHEMA_VERSION = 1


class DepthCalibrationError(ValueError):
    """Raised when field calibration evidence is incomplete or inconsistent."""


@dataclass(frozen=True, slots=True)
class FieldDepthSample:
    """One manually measured slant-range reference for a depth-model output."""

    raw_depth_m: float
    actual_distance_m: float
    sample_id: str = ""

    def __post_init__(self) -> None:
        if not self.sample_id.strip():
            raise DepthCalibrationError("field calibration sample_id cannot be empty")
        for name, value in (
            ("raw_depth_m", self.raw_depth_m),
            ("actual_distance_m", self.actual_distance_m),
        ):
            if not math.isfinite(value) or value <= 0.0:
                raise DepthCalibrationError(f"{name} must be finite and positive")


@dataclass(frozen=True, slots=True)
class DepthCalibrationFit:
    """Robust affine fit from raw model depth to measured slant range."""

    scale: float
    offset_m: float
    sample_count: int
    inlier_indices: tuple[int, ...]
    mean_absolute_error_m: float
    root_mean_square_error_m: float
    maximum_absolute_error_m: float
    median_absolute_error_m: float
    residual_gate_m: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.scale) or self.scale <= 0.0:
            raise DepthCalibrationError("calibration scale must be finite and positive")
        if not math.isfinite(self.offset_m):
            raise DepthCalibrationError("calibration offset must be finite")
        if self.sample_count < 4 or len(self.inlier_indices) < 4:
            raise DepthCalibrationError("calibration requires at least four inlier samples")

    @property
    def inlier_count(self) -> int:
        return len(self.inlier_indices)


@dataclass(frozen=True, slots=True)
class DepthCalibrationProfile:
    """A validated calibration document ready for runtime use."""

    profile: str
    scale: float
    offset_m: float
    sample_count: int
    inlier_count: int
    document_path: Path
    document_sha256: str


def fit_outdoor_depth_calibration(
    samples: Iterable[FieldDepthSample],
    *,
    minimum_samples: int = 4,
    minimum_raw_span_m: float = 1.0,
    minimum_residual_gate_m: float = 0.50,
) -> DepthCalibrationFit:
    """Fit a positive affine calibration using Theil--Sen plus inlier OLS.

    The median pairwise slope makes the initial fit insensitive to a bad target
    association or tape measurement.  Residuals are then MAD-gated before a
    least-squares refinement on the surviving measured points.
    """

    values = tuple(samples)
    if minimum_samples < 4:
        raise ValueError("minimum_samples must be at least four")
    if len(values) < minimum_samples:
        raise DepthCalibrationError(
            f"field calibration requires at least {minimum_samples} measured samples"
        )
    if not math.isfinite(minimum_raw_span_m) or minimum_raw_span_m <= 0.0:
        raise ValueError("minimum_raw_span_m must be finite and positive")
    if not math.isfinite(minimum_residual_gate_m) or minimum_residual_gate_m <= 0.0:
        raise ValueError("minimum_residual_gate_m must be finite and positive")

    raw = tuple(sample.raw_depth_m for sample in values)
    actual = tuple(sample.actual_distance_m for sample in values)
    if max(raw) - min(raw) < minimum_raw_span_m:
        raise DepthCalibrationError("field calibration raw-depth span is too small")

    slopes = [
        (actual[right] - actual[left]) / (raw[right] - raw[left])
        for left in range(len(values) - 1)
        for right in range(left + 1, len(values))
        if abs(raw[right] - raw[left]) > 1e-9
    ]
    if not slopes:
        raise DepthCalibrationError("field calibration samples have no usable raw-depth slope")
    initial_scale = float(statistics.median(slopes))
    if not math.isfinite(initial_scale) or initial_scale <= 0.0:
        raise DepthCalibrationError("field calibration did not produce a positive scale")
    initial_offset = float(
        statistics.median(
            y - initial_scale * x for x, y in zip(raw, actual, strict=True)
        )
    )
    initial_residuals = tuple(
        y - (initial_scale * x + initial_offset)
        for x, y in zip(raw, actual, strict=True)
    )
    median_residual = float(statistics.median(initial_residuals))
    residual_mad = float(
        statistics.median(abs(value - median_residual) for value in initial_residuals)
    )
    residual_gate_m = max(minimum_residual_gate_m, 3.0 * 1.4826 * residual_mad)
    inlier_indices = tuple(
        index
        for index, value in enumerate(initial_residuals)
        if abs(value - median_residual) <= residual_gate_m
    )
    if len(inlier_indices) < minimum_samples:
        raise DepthCalibrationError("field calibration rejected too many inconsistent samples")

    inlier_raw = tuple(raw[index] for index in inlier_indices)
    inlier_actual = tuple(actual[index] for index in inlier_indices)
    mean_raw = statistics.fmean(inlier_raw)
    mean_actual = statistics.fmean(inlier_actual)
    denominator = sum((value - mean_raw) ** 2 for value in inlier_raw)
    if denominator <= 1e-12:
        raise DepthCalibrationError("field calibration inlier raw-depth span is too small")
    scale = sum(
        (x - mean_raw) * (y - mean_actual)
        for x, y in zip(inlier_raw, inlier_actual, strict=True)
    ) / denominator
    offset_m = mean_actual - scale * mean_raw
    if not math.isfinite(scale) or scale <= 0.0 or not math.isfinite(offset_m):
        raise DepthCalibrationError("field calibration fit is invalid")

    inlier_residuals = tuple(
        actual[index] - (scale * raw[index] + offset_m) for index in inlier_indices
    )
    absolute = tuple(abs(value) for value in inlier_residuals)
    return DepthCalibrationFit(
        scale=scale,
        offset_m=offset_m,
        sample_count=len(values),
        inlier_indices=inlier_indices,
        mean_absolute_error_m=statistics.fmean(absolute),
        root_mean_square_error_m=math.sqrt(
            statistics.fmean(value * value for value in inlier_residuals)
        ),
        maximum_absolute_error_m=max(absolute),
        median_absolute_error_m=float(statistics.median(absolute)),
        residual_gate_m=residual_gate_m,
    )


def calibration_document(
    *,
    profile: str,
    samples: Iterable[FieldDepthSample],
    fit: DepthCalibrationFit,
) -> dict[str, object]:
    """Return the versioned document consumed by the Jetson runtime."""

    clean_profile = profile.strip()
    if not clean_profile:
        raise DepthCalibrationError("calibration profile cannot be empty")
    values = tuple(samples)
    if len(values) != fit.sample_count:
        raise DepthCalibrationError("calibration document samples do not match fit")
    inliers = set(fit.inlier_indices)
    return {
        "schema_version": CALIBRATION_DOCUMENT_SCHEMA_VERSION,
        "profile": clean_profile,
        "calibration": {"scale": fit.scale, "offset_m": fit.offset_m},
        "fit": {
            "method": "theil_sen_mad_gated_ols",
            "sample_count": fit.sample_count,
            "inlier_count": fit.inlier_count,
            "mean_absolute_error_m": fit.mean_absolute_error_m,
            "root_mean_square_error_m": fit.root_mean_square_error_m,
            "maximum_absolute_error_m": fit.maximum_absolute_error_m,
            "median_absolute_error_m": fit.median_absolute_error_m,
            "residual_gate_m": fit.residual_gate_m,
        },
        "samples": [
            {
                "sample_id": sample.sample_id,
                "raw_depth_m": sample.raw_depth_m,
                "actual_distance_m": sample.actual_distance_m,
                "inlier": index in inliers,
            }
            for index, sample in enumerate(values)
        ],
    }


def write_calibration_document(
    path: Path, document: Mapping[str, object]) -> None:
    """Atomically write a calibration document after validating it in memory."""

    path = Path(path)
    if path.suffix.lower() != ".json":
        raise DepthCalibrationError("calibration document must use a .json suffix")
    _profile_from_document(document, path=path, digest="0" * 64)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_calibration_document(path: Path) -> DepthCalibrationProfile:
    """Load a field-fitted calibration only when its safety contract is complete."""

    path = Path(path)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise DepthCalibrationError(f"cannot read calibration document: {path}") from exc
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise DepthCalibrationError("calibration document is not valid JSON") from exc
    if not isinstance(document, Mapping):
        raise DepthCalibrationError("calibration document root must be an object")
    return _profile_from_document(
        document,
        path=path,
        digest=hashlib.sha256(payload).hexdigest(),
    )


def _profile_from_document(
    document: Mapping[str, object],
    *,
    path: Path,
    digest: str,
) -> DepthCalibrationProfile:
    if document.get("schema_version") != CALIBRATION_DOCUMENT_SCHEMA_VERSION:
        raise DepthCalibrationError("unsupported calibration document schema")
    profile = document.get("profile")
    calibration = document.get("calibration")
    fit = document.get("fit")
    samples = document.get("samples")
    if not isinstance(profile, str) or not profile.strip():
        raise DepthCalibrationError("calibration document profile is invalid")
    if (
        not isinstance(calibration, Mapping)
        or not isinstance(fit, Mapping)
        or not isinstance(samples, list)
    ):
        raise DepthCalibrationError("calibration document is missing required sections")
    scale = _finite_number(calibration.get("scale"), "calibration scale", positive=True)
    offset_m = _finite_number(calibration.get("offset_m"), "calibration offset")
    sample_count = _positive_integral(fit.get("sample_count"), "fit sample_count")
    inlier_count = _positive_integral(fit.get("inlier_count"), "fit inlier_count")
    if sample_count < 4 or inlier_count < 4 or inlier_count > sample_count:
        raise DepthCalibrationError("calibration document needs at least four valid inliers")
    if len(samples) != sample_count:
        raise DepthCalibrationError("calibration document sample count is inconsistent")
    documented_inliers = sum(
        sample.get("inlier") is True for sample in samples if isinstance(sample, Mapping)
    )
    if documented_inliers != inlier_count:
        raise DepthCalibrationError("calibration document inlier count is inconsistent")
    return DepthCalibrationProfile(
        profile=profile.strip(),
        scale=scale,
        offset_m=offset_m,
        sample_count=sample_count,
        inlier_count=inlier_count,
        document_path=path,
        document_sha256=digest,
    )


def _finite_number(value: object, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise DepthCalibrationError(f"{name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise DepthCalibrationError(f"{name} must be numeric") from exc
    if not math.isfinite(number) or (positive and number <= 0.0):
        qualifier = "finite and positive" if positive else "finite"
        raise DepthCalibrationError(f"{name} must be {qualifier}")
    return number


def _positive_integral(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise DepthCalibrationError(f"{name} must be an integer")
    try:
        integer = int(value)
    except (TypeError, ValueError) as exc:
        raise DepthCalibrationError(f"{name} must be an integer") from exc
    if integer <= 0 or integer != value:
        raise DepthCalibrationError(f"{name} must be a positive integer")
    return integer


__all__ = [
    "CALIBRATION_DOCUMENT_SCHEMA_VERSION",
    "DepthCalibrationError",
    "DepthCalibrationFit",
    "DepthCalibrationProfile",
    "FieldDepthSample",
    "calibration_document",
    "fit_outdoor_depth_calibration",
    "load_calibration_document",
    "write_calibration_document",
]
