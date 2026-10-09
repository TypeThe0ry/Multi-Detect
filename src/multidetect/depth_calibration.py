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
from bisect import bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from .autonomy.serialization import AutonomyContractError, strict_json_loads

CALIBRATION_DOCUMENT_SCHEMA_VERSION = 1
CALIBRATION_CURVE_DOCUMENT_SCHEMA_VERSION = 2


class DepthCalibrationError(ValueError):
    """Raised when field calibration evidence is incomplete or inconsistent."""


@dataclass(frozen=True, slots=True)
class DepthCalibrationCurve:
    """Monotonic piecewise-linear mapping from raw depth to measured distance.

    The curve is intentionally small and dependency-free so the same validated
    document can be consumed by the Jetson runtime and offline verifiers. Values
    outside the measured raw-depth span use the slope of the nearest segment;
    callers can expose that extrapolation in their uncertainty budget.
    """

    raw_breakpoints_m: tuple[float, ...]
    actual_breakpoints_m: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.raw_breakpoints_m) < 2:
            raise DepthCalibrationError("calibration curve requires at least two breakpoints")
        if len(self.raw_breakpoints_m) != len(self.actual_breakpoints_m):
            raise DepthCalibrationError("calibration curve breakpoint counts must match")
        if not all(
            math.isfinite(value) and value > 0.0
            for value in (*self.raw_breakpoints_m, *self.actual_breakpoints_m)
        ):
            raise DepthCalibrationError("calibration curve values must be finite and positive")
        if any(right <= left for left, right in pairwise(self.raw_breakpoints_m)):
            raise DepthCalibrationError("calibration curve raw breakpoints must increase")
        if any(right <= left for left, right in pairwise(self.actual_breakpoints_m)):
            raise DepthCalibrationError("calibration curve actual distances must increase")

    @property
    def raw_min_m(self) -> float:
        return self.raw_breakpoints_m[0]

    @property
    def raw_max_m(self) -> float:
        return self.raw_breakpoints_m[-1]

    def map_scalar(self, raw_depth_m: float) -> float:
        """Map one raw depth, linearly extrapolating at either endpoint."""

        if isinstance(raw_depth_m, bool) or not math.isfinite(raw_depth_m):
            raise DepthCalibrationError("raw depth must be finite")
        if raw_depth_m <= self.raw_breakpoints_m[0]:
            index = 0
        elif raw_depth_m >= self.raw_breakpoints_m[-1]:
            index = len(self.raw_breakpoints_m) - 2
        else:
            index = bisect_right(self.raw_breakpoints_m, raw_depth_m) - 1
        raw_left = self.raw_breakpoints_m[index]
        raw_right = self.raw_breakpoints_m[index + 1]
        actual_left = self.actual_breakpoints_m[index]
        actual_right = self.actual_breakpoints_m[index + 1]
        fraction = (raw_depth_m - raw_left) / (raw_right - raw_left)
        return actual_left + fraction * (actual_right - actual_left)


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
    model: str = "affine"
    raw_breakpoints_m: tuple[float, ...] = ()
    actual_breakpoints_m: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        if not math.isfinite(self.scale) or self.scale <= 0.0:
            raise DepthCalibrationError("calibration scale must be finite and positive")
        if not math.isfinite(self.offset_m):
            raise DepthCalibrationError("calibration offset must be finite")
        if self.sample_count < 4 or len(self.inlier_indices) < 4:
            raise DepthCalibrationError("calibration requires at least four inlier samples")
        if self.model not in {"affine", "piecewise_linear"}:
            raise DepthCalibrationError("calibration model is unsupported")
        if self.model == "affine":
            if self.raw_breakpoints_m or self.actual_breakpoints_m:
                raise DepthCalibrationError("affine calibration cannot contain a curve")
        else:
            DepthCalibrationCurve(self.raw_breakpoints_m, self.actual_breakpoints_m)

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
    calibration_mode: str = "affine"
    raw_breakpoints_m: tuple[float, ...] = ()
    actual_breakpoints_m: tuple[float, ...] = ()
    extrapolation_sigma_m: float = 0.0

    @property
    def curve(self) -> DepthCalibrationCurve | None:
        if self.calibration_mode != "piecewise_linear":
            return None
        return DepthCalibrationCurve(self.raw_breakpoints_m, self.actual_breakpoints_m)

    def map_depth(self, raw_depth_m: float) -> float:
        curve = self.curve
        return (
            curve.map_scalar(raw_depth_m)
            if curve is not None
            else self.scale * raw_depth_m + self.offset_m
        )


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
        statistics.median(y - initial_scale * x for x, y in zip(raw, actual, strict=True))
    )
    initial_residuals = tuple(
        y - (initial_scale * x + initial_offset) for x, y in zip(raw, actual, strict=True)
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
    scale = (
        sum(
            (x - mean_raw) * (y - mean_actual)
            for x, y in zip(inlier_raw, inlier_actual, strict=True)
        )
        / denominator
    )
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


def fit_outdoor_depth_piecewise_calibration(
    samples: Iterable[FieldDepthSample],
    *,
    minimum_samples: int = 6,
    minimum_raw_span_m: float = 1.0,
    minimum_residual_gate_m: float = 0.50,
    minimum_knot_count: int = 4,
) -> DepthCalibrationFit:
    """Fit a robust monotonic piecewise-linear outdoor calibration curve.

    Samples are sorted by raw depth, duplicate-depth observations are reduced
    by median, and obvious ordering violations are removed before a local
    residual pass rejects isolated bad associations.  A pool-adjacent-violators
    pass then removes small measurement inversions before creating the curve.
    The affine parameters remain in the returned fit as a compatibility
    fallback; the curve is the runtime model.
    """

    if isinstance(minimum_knot_count, bool) or minimum_knot_count < 4:
        raise ValueError("minimum_knot_count must be at least four")
    values = tuple(samples)
    if len(values) < minimum_samples:
        raise DepthCalibrationError(
            f"field calibration requires at least {minimum_samples} measured samples"
        )
    if not math.isfinite(minimum_raw_span_m) or minimum_raw_span_m <= 0.0:
        raise ValueError("minimum_raw_span_m must be finite and positive")
    if not math.isfinite(minimum_residual_gate_m) or minimum_residual_gate_m <= 0.0:
        raise ValueError("minimum_residual_gate_m must be finite and positive")
    ordered = sorted(
        (sample.raw_depth_m, sample.actual_distance_m, index) for index, sample in enumerate(values)
    )
    if ordered[-1][0] - ordered[0][0] < minimum_raw_span_m:
        raise DepthCalibrationError("field calibration raw-depth span is too small")

    # Group repeated raw-depth readings.  Keeping all original indices lets
    # the resulting document account for every accepted measurement.
    raw_groups: list[tuple[list[float], list[float], list[int]]] = []
    for raw_depth_m, actual_distance_m, index in ordered:
        if raw_groups and raw_depth_m - raw_groups[-1][0][-1] <= 1e-6:
            raw_groups[-1][0].append(raw_depth_m)
            raw_groups[-1][1].append(actual_distance_m)
            raw_groups[-1][2].append(index)
        else:
            raw_groups.append(([raw_depth_m], [actual_distance_m], [index]))
    grouped = [
        (statistics.fmean(raw_values), float(statistics.median(actual_values)), tuple(indices))
        for raw_values, actual_values, indices in raw_groups
    ]

    active = list(grouped)
    while len(active) > minimum_samples:
        violations = [
            (index, active[index][1] - active[index + 1][1])
            for index in range(len(active) - 1)
            if active[index][1] > active[index + 1][1]
        ]
        if not violations:
            break
        violation_index, _ = max(violations, key=lambda item: item[1])
        # Remove the point that leaves the smaller total ordering violation.
        candidates = (violation_index, violation_index + 1)
        scores = []
        for candidate in candidates:
            trial = active[:candidate] + active[candidate + 1 :]
            score = sum(
                max(0.0, trial[index][1] - trial[index + 1][1]) for index in range(len(trial) - 1)
            )
            scores.append((score, candidate))
        active.pop(min(scores)[1])

    # Isolated interior outliers can remain monotonic (for example a wildly
    # high but still increasing tape reading).  Compare each interior point to
    # the line between its neighbours, with a range-relative floor so genuine
    # outdoor curvature is retained.
    while len(active) > minimum_samples:
        actual_span = active[-1][1] - active[0][1]
        local_gate = max(minimum_residual_gate_m, 0.05 * max(actual_span, 1.0))
        residuals = []
        for index in range(1, len(active) - 1):
            left, current, right = active[index - 1], active[index], active[index + 1]
            expected = left[1] + (current[0] - left[0]) / (right[0] - left[0]) * (
                right[1] - left[1]
            )
            residuals.append((abs(current[1] - expected), index))
        if not residuals:
            break
        largest_residual, candidate = max(residuals)
        if largest_residual <= local_gate:
            break
        active.pop(candidate)

    if len(active) < minimum_knot_count:
        raise DepthCalibrationError(
            "field calibration needs at least four strictly increasing curve knots"
        )
    measured_distances = _isotonic_non_decreasing(item[1] for item in active)
    collapsed_raw: list[float] = []
    collapsed_actual: list[float] = []
    collapsed_indices: list[int] = []
    for (raw_depth_m, _, indices), actual_distance_m in zip(
        active, measured_distances, strict=True
    ):
        if collapsed_actual and actual_distance_m <= collapsed_actual[-1] + 1e-9:
            collapsed_raw[-1] = statistics.fmean((collapsed_raw[-1], raw_depth_m))
            collapsed_indices.extend(indices)
        else:
            collapsed_raw.append(raw_depth_m)
            collapsed_actual.append(actual_distance_m)
            collapsed_indices.extend(indices)
    if len(collapsed_raw) < minimum_knot_count:
        raise DepthCalibrationError(
            "field calibration needs at least four strictly increasing curve knots"
        )
    curve = DepthCalibrationCurve(tuple(collapsed_raw), tuple(collapsed_actual))
    inlier_indices = tuple(sorted(collapsed_indices))
    inlier_raw = tuple(values[index].raw_depth_m for index in inlier_indices)
    inlier_actual = tuple(values[index].actual_distance_m for index in inlier_indices)
    mean_raw = statistics.fmean(inlier_raw)
    mean_actual = statistics.fmean(inlier_actual)
    denominator = sum((value - mean_raw) ** 2 for value in inlier_raw)
    if denominator <= 1e-12:
        raise DepthCalibrationError("field calibration inlier raw-depth span is too small")
    scale = (
        sum(
            (x - mean_raw) * (y - mean_actual)
            for x, y in zip(inlier_raw, inlier_actual, strict=True)
        )
        / denominator
    )
    offset_m = mean_actual - scale * mean_raw
    if not math.isfinite(scale) or scale <= 0.0 or not math.isfinite(offset_m):
        raise DepthCalibrationError("field calibration fit is invalid")
    residuals = tuple(
        curve.map_scalar(values[index].raw_depth_m) - values[index].actual_distance_m
        for index in inlier_indices
    )
    absolute = tuple(abs(value) for value in residuals)
    return DepthCalibrationFit(
        scale=scale,
        offset_m=offset_m,
        sample_count=len(values),
        inlier_indices=inlier_indices,
        mean_absolute_error_m=statistics.fmean(absolute),
        root_mean_square_error_m=math.sqrt(statistics.fmean(value * value for value in residuals)),
        maximum_absolute_error_m=max(absolute),
        median_absolute_error_m=float(statistics.median(absolute)),
        residual_gate_m=max(
            minimum_residual_gate_m,
            0.05 * max(curve.actual_breakpoints_m[-1] - curve.actual_breakpoints_m[0], 1.0),
        ),
        model="piecewise_linear",
        raw_breakpoints_m=curve.raw_breakpoints_m,
        actual_breakpoints_m=curve.actual_breakpoints_m,
    )


def _isotonic_non_decreasing(values: Iterable[float]) -> tuple[float, ...]:
    """Return the least-squares non-decreasing fit using PAVA."""

    blocks: list[list[float | int]] = []
    for value in values:
        if not math.isfinite(value):
            raise DepthCalibrationError("isotonic calibration values must be finite")
        blocks.append([float(value), 1])
        while len(blocks) >= 2:
            left, right = blocks[-2], blocks[-1]
            left_mean = float(left[0]) / int(left[1])
            right_mean = float(right[0]) / int(right[1])
            if left_mean <= right_mean:
                break
            blocks[-2:] = [
                [
                    float(left[0]) + float(right[0]),
                    int(left[1]) + int(right[1]),
                ]
            ]
    return tuple(
        mean for block in blocks for mean in [float(block[0]) / int(block[1])] * int(block[1])
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
    calibration: dict[str, object] = {
        "scale": fit.scale,
        "offset_m": fit.offset_m,
        "mode": fit.model,
    }
    if fit.model == "piecewise_linear":
        calibration["curve"] = {
            "raw_breakpoints_m": list(fit.raw_breakpoints_m),
            "actual_breakpoints_m": list(fit.actual_breakpoints_m),
            "extrapolation": "nearest_segment_linear",
        }
    return {
        "schema_version": (
            CALIBRATION_CURVE_DOCUMENT_SCHEMA_VERSION
            if fit.model == "piecewise_linear"
            else CALIBRATION_DOCUMENT_SCHEMA_VERSION
        ),
        "profile": clean_profile,
        "calibration": calibration,
        "fit": {
            "method": (
                "monotonic_piecewise_mad_ols"
                if fit.model == "piecewise_linear"
                else "theil_sen_mad_gated_ols"
            ),
            "model": fit.model,
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


def write_calibration_document(path: Path, document: Mapping[str, object]) -> None:
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
        document = strict_json_loads(payload, name="calibration document")
    except (AutonomyContractError, TypeError) as exc:
        raise DepthCalibrationError(
            f"calibration document is not valid strict JSON: {exc}"
        ) from exc
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
    schema_version = document.get("schema_version")
    if isinstance(schema_version, bool) or schema_version not in {
        CALIBRATION_DOCUMENT_SCHEMA_VERSION,
        CALIBRATION_CURVE_DOCUMENT_SCHEMA_VERSION,
    }:
        raise DepthCalibrationError("unsupported calibration document schema")
    profile = document.get("profile")
    # Gazebo's depth wire reports optical-axis Z (and is converted to a ray
    # slant distance by ``gazebo_depth.py`` using camera intrinsics).  It is a
    # different sensor contract from a monocular model's dense prediction.
    # Do not allow a simulator transport identity document to silently become
    # a Depth Anything calibration profile merely because its JSON shape has
    # four samples and an affine scale of one.
    source_kind = document.get("source_kind")
    if isinstance(source_kind, str) and source_kind.lower().startswith("gazebo_depth_wire"):
        raise DepthCalibrationError(
            "Gazebo depth transport calibration cannot be used as a metric-depth model calibration"
        )
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
    declared_mode = calibration.get("mode", "affine")
    if not isinstance(declared_mode, str) or declared_mode not in {
        "affine",
        "piecewise_linear",
    }:
        raise DepthCalibrationError("calibration document mode is invalid")
    fit_model = fit.get("model")
    if fit_model is not None and fit_model != declared_mode:
        raise DepthCalibrationError("calibration document model and mode disagree")
    model = fit_model if fit_model is not None else declared_mode
    if not isinstance(model, str) or model not in {"affine", "piecewise_linear"}:
        raise DepthCalibrationError("calibration document model is invalid")
    if schema_version == CALIBRATION_DOCUMENT_SCHEMA_VERSION and model != "affine":
        raise DepthCalibrationError("curve calibration requires schema version 2")
    raw_breakpoints: tuple[float, ...] = ()
    actual_breakpoints: tuple[float, ...] = ()
    extrapolation_sigma_m = 0.0
    curve = calibration.get("curve")
    if model == "piecewise_linear":
        if schema_version != CALIBRATION_CURVE_DOCUMENT_SCHEMA_VERSION:
            raise DepthCalibrationError("curve calibration requires schema version 2")
        if not isinstance(curve, Mapping):
            raise DepthCalibrationError("piecewise calibration curve is missing")
        raw_breakpoints = _positive_float_sequence(
            curve.get("raw_breakpoints_m"),
            "calibration curve raw breakpoints",
        )
        actual_breakpoints = _positive_float_sequence(
            curve.get("actual_breakpoints_m"),
            "calibration curve actual distances",
        )
        DepthCalibrationCurve(raw_breakpoints, actual_breakpoints)
        residual_gate = fit.get("residual_gate_m", 0.0)
        extrapolation_sigma_m = _finite_number(
            residual_gate,
            "calibration residual gate",
        )
    elif curve is not None:
        raise DepthCalibrationError("affine calibration cannot contain a curve")
    return DepthCalibrationProfile(
        profile=profile.strip(),
        scale=scale,
        offset_m=offset_m,
        sample_count=sample_count,
        inlier_count=inlier_count,
        document_path=path,
        document_sha256=digest,
        calibration_mode=model,
        raw_breakpoints_m=raw_breakpoints,
        actual_breakpoints_m=actual_breakpoints,
        extrapolation_sigma_m=extrapolation_sigma_m,
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


def _positive_float_sequence(value: object, name: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise DepthCalibrationError(f"{name} must contain at least two values")
    result = tuple(_finite_number(item, name, positive=True) for item in value)
    return result


__all__ = [
    "CALIBRATION_CURVE_DOCUMENT_SCHEMA_VERSION",
    "CALIBRATION_DOCUMENT_SCHEMA_VERSION",
    "DepthCalibrationError",
    "DepthCalibrationCurve",
    "DepthCalibrationFit",
    "DepthCalibrationProfile",
    "FieldDepthSample",
    "calibration_document",
    "fit_outdoor_depth_calibration",
    "fit_outdoor_depth_piecewise_calibration",
    "load_calibration_document",
    "write_calibration_document",
]
