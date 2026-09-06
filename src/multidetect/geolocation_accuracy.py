"""Offline WGS-84 accuracy evaluation for target geolocation evidence.

The runtime publishes a target coordinate and a horizontal 1-sigma estimate,
while a field operator can survey the same target independently.  This module
keeps that comparison out of the live control path and produces deterministic,
hashable evidence for the REAL/field release gate.  It never modifies a
calibration document and never emits a flight-control command.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .target_geolocation import (
    WGS84_FLATTENING,
    WGS84_SEMI_MAJOR_AXIS_M,
    WGS84_SEMI_MINOR_AXIS_M,
    wgs84_radii_of_curvature,
)

_MAX_ITERATIONS = 100
_CONVERGENCE_TOLERANCE = 1.0e-13
_COORDINATE_EPSILON = 1.0e-12


@dataclass(frozen=True, slots=True)
class GeolocationAccuracySample:
    """One estimated target coordinate paired with an independent survey."""

    sample_id: str
    estimated_latitude_deg: float
    estimated_longitude_deg: float
    ground_truth_latitude_deg: float
    ground_truth_longitude_deg: float
    reported_sigma_m: float | None = None
    target_id: str | None = None
    captured_at_s: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.sample_id, str) or not self.sample_id.strip():
            raise ValueError("geolocation accuracy sample_id cannot be empty")
        _validate_coordinate(
            self.estimated_latitude_deg,
            self.estimated_longitude_deg,
            name="estimated",
        )
        _validate_coordinate(
            self.ground_truth_latitude_deg,
            self.ground_truth_longitude_deg,
            name="ground_truth",
        )
        if self.reported_sigma_m is not None:
            _validate_non_negative(self.reported_sigma_m, "reported_sigma_m")
        if self.target_id is not None and (
            not isinstance(self.target_id, str) or not self.target_id.strip()
        ):
            raise ValueError("target_id must be a non-empty string when supplied")
        if self.captured_at_s is not None:
            _validate_non_negative(self.captured_at_s, "captured_at_s")


def wgs84_distance_m(
    *,
    latitude1_deg: float,
    longitude1_deg: float,
    latitude2_deg: float,
    longitude2_deg: float,
) -> float:
    """Return the shortest WGS-84 ellipsoid distance between two coordinates.

    Vincenty's inverse equations are used instead of a spherical haversine
    approximation.  The bounded iteration makes nearly-antipodal or malformed
    inputs fail closed rather than silently returning a less accurate value.
    """

    _validate_coordinate(latitude1_deg, longitude1_deg, name="coordinate1")
    _validate_coordinate(latitude2_deg, longitude2_deg, name="coordinate2")
    if (
        abs(latitude1_deg - latitude2_deg) <= _COORDINATE_EPSILON
        and abs(_wrap_longitude_delta(longitude2_deg - longitude1_deg)) <= _COORDINATE_EPSILON
    ):
        return 0.0

    flattening = WGS84_FLATTENING
    semi_major_m = WGS84_SEMI_MAJOR_AXIS_M
    semi_minor_m = WGS84_SEMI_MINOR_AXIS_M
    reduced_latitude1 = math.atan((1.0 - flattening) * math.tan(math.radians(latitude1_deg)))
    reduced_latitude2 = math.atan((1.0 - flattening) * math.tan(math.radians(latitude2_deg)))
    sin_u1, cos_u1 = math.sin(reduced_latitude1), math.cos(reduced_latitude1)
    sin_u2, cos_u2 = math.sin(reduced_latitude2), math.cos(reduced_latitude2)
    longitude_delta = math.radians(_wrap_longitude_delta(longitude2_deg - longitude1_deg))
    lam = longitude_delta

    for _ in range(_MAX_ITERATIONS):
        sin_lambda, cos_lambda = math.sin(lam), math.cos(lam)
        term1 = cos_u2 * sin_lambda
        term2 = cos_u1 * sin_u2 - sin_u1 * cos_u2 * cos_lambda
        sin_sigma = math.hypot(term1, term2)
        if sin_sigma <= _COORDINATE_EPSILON:
            return 0.0
        cos_sigma = sin_u1 * sin_u2 + cos_u1 * cos_u2 * cos_lambda
        sigma = math.atan2(sin_sigma, cos_sigma)
        sin_alpha = cos_u1 * cos_u2 * sin_lambda / sin_sigma
        cos_squared_alpha = max(0.0, 1.0 - sin_alpha * sin_alpha)
        if cos_squared_alpha <= _COORDINATE_EPSILON:
            cos_two_sigma_m = 0.0
        else:
            cos_two_sigma_m = cos_sigma - 2.0 * sin_u1 * sin_u2 / cos_squared_alpha
        coefficient_c = (
            flattening
            / 16.0
            * cos_squared_alpha
            * (4.0 + flattening * (4.0 - 3.0 * cos_squared_alpha))
        )
        next_lam = longitude_delta + (1.0 - coefficient_c) * flattening * sin_alpha * (
            sigma
            + coefficient_c
            * sin_sigma
            * (
                cos_two_sigma_m
                + coefficient_c * cos_sigma * (-1.0 + 2.0 * cos_two_sigma_m * cos_two_sigma_m)
            )
        )
        if abs(next_lam - lam) <= _CONVERGENCE_TOLERANCE:
            lam = next_lam
            break
        lam = next_lam
    else:
        raise ValueError("WGS-84 inverse geodesic did not converge")

    sin_lambda, cos_lambda = math.sin(lam), math.cos(lam)
    term1 = cos_u2 * sin_lambda
    term2 = cos_u1 * sin_u2 - sin_u1 * cos_u2 * cos_lambda
    sin_sigma = math.hypot(term1, term2)
    cos_sigma = sin_u1 * sin_u2 + cos_u1 * cos_u2 * cos_lambda
    sigma = math.atan2(sin_sigma, cos_sigma)
    sin_alpha = cos_u1 * cos_u2 * sin_lambda / max(sin_sigma, _COORDINATE_EPSILON)
    cos_squared_alpha = max(0.0, 1.0 - sin_alpha * sin_alpha)
    if cos_squared_alpha <= _COORDINATE_EPSILON:
        cos_two_sigma_m = 0.0
    else:
        cos_two_sigma_m = cos_sigma - 2.0 * sin_u1 * sin_u2 / cos_squared_alpha
    u_squared = (
        cos_squared_alpha
        * (semi_major_m * semi_major_m - semi_minor_m * semi_minor_m)
        / (semi_minor_m * semi_minor_m)
    )
    coefficient_a = 1.0 + (u_squared / 16384.0) * (
        4096.0 + u_squared * (-768.0 + u_squared * (320.0 - 175.0 * u_squared))
    )
    coefficient_b = (u_squared / 1024.0) * (
        256.0 + u_squared * (-128.0 + u_squared * (74.0 - 47.0 * u_squared))
    )
    delta_sigma = (
        coefficient_b
        * sin_sigma
        * (
            cos_two_sigma_m
            + coefficient_b
            / 4.0
            * (
                cos_sigma * (-1.0 + 2.0 * cos_two_sigma_m * cos_two_sigma_m)
                - coefficient_b
                / 6.0
                * cos_two_sigma_m
                * (-3.0 + 4.0 * sin_sigma * sin_sigma)
                * (-3.0 + 4.0 * cos_two_sigma_m * cos_two_sigma_m)
            )
        )
    )
    distance_m = semi_minor_m * coefficient_a * (sigma - delta_sigma)
    if not math.isfinite(distance_m) or distance_m < 0.0:
        raise ValueError("WGS-84 inverse geodesic returned an invalid distance")
    return distance_m


def evaluate_geolocation_accuracy(
    samples: Iterable[GeolocationAccuracySample],
    *,
    minimum_samples: int = 10,
    maximum_p95_m: float | None = None,
    minimum_one_sigma_coverage: float | None = None,
    minimum_two_sigma_coverage: float | None = None,
) -> dict[str, object]:
    """Evaluate surveyed errors and return a deterministic evidence document.

    ``gate_passed`` means the supplied evidence is complete for the requested
    thresholds.  If no accuracy threshold is supplied, the gate only checks
    sample completeness and coordinate validity; that is intentionally not a
    claim that the estimator meets a product accuracy target.
    """

    _validate_positive_integer(minimum_samples, "minimum_samples")
    _validate_optional_positive(maximum_p95_m, "maximum_p95_m")
    _validate_coverage(minimum_one_sigma_coverage, "minimum_one_sigma_coverage")
    _validate_coverage(minimum_two_sigma_coverage, "minimum_two_sigma_coverage")
    values = tuple(samples)
    if not values:
        raise ValueError("geolocation accuracy requires at least one sample")
    sample_ids = [sample.sample_id for sample in values]
    if len(set(sample_ids)) != len(sample_ids):
        raise ValueError("geolocation accuracy sample_id values must be unique")

    evaluated: list[dict[str, object]] = []
    distance_errors: list[float] = []
    north_errors: list[float] = []
    east_errors: list[float] = []
    one_sigma_hits = 0
    two_sigma_hits = 0
    sigma_count = 0
    for sample in values:
        error_m = wgs84_distance_m(
            latitude1_deg=sample.estimated_latitude_deg,
            longitude1_deg=sample.estimated_longitude_deg,
            latitude2_deg=sample.ground_truth_latitude_deg,
            longitude2_deg=sample.ground_truth_longitude_deg,
        )
        meridional_m, prime_vertical_m = wgs84_radii_of_curvature(sample.ground_truth_latitude_deg)
        north_m = (
            math.radians(sample.estimated_latitude_deg - sample.ground_truth_latitude_deg)
            * meridional_m
        )
        east_m = (
            math.radians(
                _wrap_longitude_delta(
                    sample.estimated_longitude_deg - sample.ground_truth_longitude_deg
                )
            )
            * prime_vertical_m
            * math.cos(math.radians(sample.ground_truth_latitude_deg))
        )
        distance_errors.append(error_m)
        north_errors.append(north_m)
        east_errors.append(east_m)
        item: dict[str, object] = {
            "sample_id": sample.sample_id,
            "target_id": sample.target_id,
            "captured_at_s": sample.captured_at_s,
            "estimated_latitude_deg": sample.estimated_latitude_deg,
            "estimated_longitude_deg": sample.estimated_longitude_deg,
            "ground_truth_latitude_deg": sample.ground_truth_latitude_deg,
            "ground_truth_longitude_deg": sample.ground_truth_longitude_deg,
            "error_distance_m": error_m,
            "signed_north_error_m": north_m,
            "signed_east_error_m": east_m,
            "reported_sigma_m": sample.reported_sigma_m,
        }
        if sample.reported_sigma_m is not None:
            sigma_count += 1
            one_sigma_hits += int(error_m <= sample.reported_sigma_m)
            two_sigma_hits += int(error_m <= 1.96 * sample.reported_sigma_m)
            item["within_one_sigma"] = error_m <= sample.reported_sigma_m
            item["within_two_sigma"] = error_m <= 1.96 * sample.reported_sigma_m
        else:
            item["within_one_sigma"] = None
            item["within_two_sigma"] = None
        evaluated.append(item)

    distance_metrics = _metrics(distance_errors)
    north_metrics = _metrics(north_errors)
    east_metrics = _metrics(east_errors)
    error_ellipse = _error_ellipse(north_errors, east_errors)
    one_sigma_coverage = one_sigma_hits / sigma_count if sigma_count else None
    two_sigma_coverage = two_sigma_hits / sigma_count if sigma_count else None
    reasons: list[str] = []
    if len(values) < minimum_samples:
        reasons.append(f"sample_count<{minimum_samples} (got {len(values)})")
    p95_m = float(distance_metrics["p95_m"])
    if maximum_p95_m is not None and p95_m > maximum_p95_m:
        reasons.append(f"p95_error>{maximum_p95_m:g}m (got {p95_m:.6g}m)")
    if minimum_one_sigma_coverage is not None:
        if one_sigma_coverage is None:
            reasons.append("one_sigma_coverage_unavailable")
        elif one_sigma_coverage < minimum_one_sigma_coverage:
            reasons.append(
                f"one_sigma_coverage<{minimum_one_sigma_coverage:g} (got {one_sigma_coverage:.6g})"
            )
    if minimum_two_sigma_coverage is not None:
        if two_sigma_coverage is None:
            reasons.append("two_sigma_coverage_unavailable")
        elif two_sigma_coverage < minimum_two_sigma_coverage:
            reasons.append(
                f"two_sigma_coverage<{minimum_two_sigma_coverage:g} (got {two_sigma_coverage:.6g})"
            )
    return {
        "schema_version": 1,
        "evidence_type": "multidetect.target-geolocation-accuracy.v1",
        "ground_truth_isolated": True,
        "advisory_only": True,
        "contains_flight_control": False,
        "requirements": {
            "minimum_samples": minimum_samples,
            "maximum_p95_m": maximum_p95_m,
            "minimum_one_sigma_coverage": minimum_one_sigma_coverage,
            "minimum_two_sigma_coverage": minimum_two_sigma_coverage,
        },
        "measurement": {
            "sample_count": len(values),
            "sigma_sample_count": sigma_count,
            "one_sigma_coverage": one_sigma_coverage,
            "two_sigma_coverage": two_sigma_coverage,
        },
        "distance_error_m": distance_metrics,
        "signed_north_error_m": north_metrics,
        "signed_east_error_m": east_metrics,
        "error_ellipse": error_ellipse,
        "samples": evaluated,
        "reasons": reasons,
        "gate_passed": not reasons,
    }


def verify_geolocation_accuracy_report(
    report: Mapping[str, object],
    *,
    require_gate_passed: bool = True,
) -> dict[str, object]:
    """Recompute and validate a serialized accuracy report.

    The release gate binds the report bytes, but a hash alone does not prove
    that the JSON was produced by the evaluator or that its summary matches
    the surveyed samples.  This verifier reconstructs the samples, reruns the
    WGS-84 calculation and compares every derived section.  ``require_gate_passed``
    is disabled by the manifest writer so a failed prerelease report can be
    retained as an explicit diagnostic; the release evaluator always enables it.
    Replay-derived reports may include a ``selection`` provenance object; its
    count-conservation equations are checked in addition to the error metrics.
    """

    reasons: list[str] = []
    if not isinstance(report, Mapping):
        return {
            "event": "multidetect.target-geolocation-accuracy-verified",
            "schema_version": 1,
            "evidence_type": "multidetect.target-geolocation-accuracy.v1",
            "recomputed_gate_passed": False,
            "accuracy_threshold_defined": False,
            "gate_passed": False,
            "passed": False,
            "reasons": ["accuracy report must be an object"],
        }

    for field, expected in (
        ("schema_version", 1),
        ("evidence_type", "multidetect.target-geolocation-accuracy.v1"),
        ("ground_truth_isolated", True),
        ("advisory_only", True),
        ("contains_flight_control", False),
    ):
        if not _strict_equal(report.get(field), expected):
            reasons.append(f"accuracy report {field} does not match verifier contract")

    requirements = report.get("requirements")
    raw_samples = report.get("samples")
    expected_report: dict[str, object] | None = None
    if not isinstance(requirements, Mapping):
        reasons.append("accuracy report requirements must be an object")
    if not isinstance(raw_samples, list):
        reasons.append("accuracy report samples must be an array")
    else:
        samples: list[GeolocationAccuracySample] = []
        for index, raw_sample in enumerate(raw_samples, start=1):
            if not isinstance(raw_sample, Mapping):
                reasons.append(f"accuracy report sample {index} must be an object")
                continue
            try:
                samples.append(
                    GeolocationAccuracySample(
                        sample_id=raw_sample["sample_id"],  # type: ignore[arg-type]
                        estimated_latitude_deg=raw_sample["estimated_latitude_deg"],  # type: ignore[arg-type]
                        estimated_longitude_deg=raw_sample["estimated_longitude_deg"],  # type: ignore[arg-type]
                        ground_truth_latitude_deg=raw_sample["ground_truth_latitude_deg"],  # type: ignore[arg-type]
                        ground_truth_longitude_deg=raw_sample["ground_truth_longitude_deg"],  # type: ignore[arg-type]
                        reported_sigma_m=raw_sample.get("reported_sigma_m"),  # type: ignore[arg-type]
                        target_id=raw_sample.get("target_id"),  # type: ignore[arg-type]
                        captured_at_s=raw_sample.get("captured_at_s"),  # type: ignore[arg-type]
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                reasons.append(f"accuracy report sample {index} is invalid: {exc}")

        if not isinstance(requirements, Mapping):
            requirements = {}
        try:
            expected_report = evaluate_geolocation_accuracy(
                samples,
                minimum_samples=requirements.get("minimum_samples", 10),  # type: ignore[arg-type]
                maximum_p95_m=requirements.get("maximum_p95_m"),  # type: ignore[arg-type]
                minimum_one_sigma_coverage=requirements.get("minimum_one_sigma_coverage"),  # type: ignore[arg-type]
                minimum_two_sigma_coverage=requirements.get("minimum_two_sigma_coverage"),  # type: ignore[arg-type]
            )
        except (TypeError, ValueError) as exc:
            reasons.append(f"accuracy report cannot be recomputed: {exc}")

    report_gate = report.get("gate_passed")
    if type(report_gate) is not bool:
        reasons.append("accuracy report gate_passed must be a boolean")

    if expected_report is not None:
        if report.get("requirements") != expected_report["requirements"]:
            reasons.append("accuracy report requirements do not match recomputation")
        comparison_fields = [
            "measurement",
            "distance_error_m",
            "signed_north_error_m",
            "signed_east_error_m",
            "samples",
            "reasons",
        ]
        # Reports written before the ellipse extension remain valid v1
        # evidence.  New reports include the derived section and therefore
        # get the stronger tamper/recomputation check below.
        if "error_ellipse" in report:
            comparison_fields.append("error_ellipse")
        else:
            expected_report = dict(expected_report)
            expected_report.pop("error_ellipse", None)
        for field in comparison_fields:
            if not _equivalent(report.get(field), expected_report[field]):
                reasons.append(f"accuracy report {field} does not match recomputation")
        recomputed_gate = expected_report["gate_passed"] is True
        if report_gate is not recomputed_gate:
            reasons.append("accuracy report gate_passed does not match recomputation")
    else:
        recomputed_gate = False

    selection = report.get("selection")
    if selection is not None:
        expected_sample_count = (
            expected_report["measurement"]["sample_count"]
            if isinstance(expected_report, Mapping)
            and isinstance(expected_report.get("measurement"), Mapping)
            else None
        )
        _validate_selection_metadata(selection, expected_sample_count, reasons)

    if require_gate_passed and report_gate is not True:
        reasons.append("accuracy report gate_passed is not true")
    thresholds_defined = isinstance(requirements, Mapping) and any(
        requirements.get(field) is not None
        for field in (
            "maximum_p95_m",
            "minimum_one_sigma_coverage",
            "minimum_two_sigma_coverage",
        )
    )
    return {
        "event": "multidetect.target-geolocation-accuracy-verified",
        "schema_version": 1,
        "evidence_type": "multidetect.target-geolocation-accuracy.v1",
        "recomputed_gate_passed": recomputed_gate,
        "accuracy_threshold_defined": thresholds_defined,
        "gate_passed": report_gate is True and recomputed_gate and not reasons,
        "passed": not reasons,
        "reasons": reasons,
    }


def _equivalent(left: object, right: object) -> bool:
    """Compare JSON-compatible values without accepting bool/int aliasing."""

    if type(left) is not type(right):
        if isinstance(left, (int, float)) and not isinstance(left, bool):
            if isinstance(right, (int, float)) and not isinstance(right, bool):
                return math.isclose(float(left), float(right), rel_tol=1.0e-12, abs_tol=1.0e-9)
        return False
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return set(left) == set(right) and all(_equivalent(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _equivalent(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return left == right


def _strict_equal(left: object, right: object) -> bool:
    """Compare scalar contract fields without bool/int coercion."""

    return type(left) is type(right) and left == right


def _validate_selection_metadata(
    selection: object,
    expected_sample_count: object,
    reasons: list[str],
) -> None:
    """Validate replay-to-truth selection diagnostics without trusting them.

    The verifier cannot reopen the source replay or truth sheet from an
    accuracy report alone.  It therefore checks the internal conservation
    equations and strict scalar/list domains; the source manifest hash remains
    provenance that the release gate can bind to the adjacent field artifact.
    """

    if not isinstance(selection, Mapping):
        reasons.append("accuracy report selection must be an object")
        return
    expected_keys = {
        "source_manifest_sha256",
        "source_manifest_bytes",
        "truth_row_count",
        "selected_event_count",
        "eligible_event_count",
        "matched_event_count",
        "skipped_event_count",
        "unmatched_event_ids",
        "unused_truth_ids",
        "target_id",
        "allow_unmatched_events",
        "allow_extra_truth",
        "max_truth_time_delta_s",
    }
    unknown = sorted(set(selection).difference(expected_keys))
    missing = sorted(expected_keys.difference(selection))
    if unknown:
        reasons.append(f"accuracy report selection has unknown field(s): {unknown!r}")
    if missing:
        reasons.append(f"accuracy report selection is missing field(s): {missing!r}")
        return

    source_hash = selection.get("source_manifest_sha256")
    if not _is_sha256(source_hash):
        reasons.append("accuracy report selection source_manifest_sha256 is invalid")
    source_bytes = selection.get("source_manifest_bytes")
    if type(source_bytes) is not int or source_bytes < 1:
        reasons.append("accuracy report selection source_manifest_bytes is invalid")

    counts: dict[str, int] = {}
    for name in (
        "truth_row_count",
        "selected_event_count",
        "eligible_event_count",
        "matched_event_count",
        "skipped_event_count",
    ):
        value = selection.get(name)
        if type(value) is not int or value < 0:
            reasons.append(f"accuracy report selection {name} must be a non-negative integer")
        else:
            counts[name] = value

    unmatched = _selection_id_list(
        selection.get("unmatched_event_ids"), "unmatched_event_ids", reasons
    )
    unused = _selection_id_list(selection.get("unused_truth_ids"), "unused_truth_ids", reasons)
    target_id = selection.get("target_id")
    if target_id is not None and (not isinstance(target_id, str) or not target_id.strip()):
        reasons.append("accuracy report selection target_id must be a non-empty string or null")
    for name in ("allow_unmatched_events", "allow_extra_truth"):
        if type(selection.get(name)) is not bool:
            reasons.append(f"accuracy report selection {name} must be a boolean")
    max_delta = selection.get("max_truth_time_delta_s")
    if max_delta is not None and (
        isinstance(max_delta, bool)
        or not isinstance(max_delta, (int, float))
        or not math.isfinite(float(max_delta))
        or float(max_delta) < 0.0
    ):
        reasons.append(
            "accuracy report selection max_truth_time_delta_s must be finite, non-negative, or null"
        )

    if (
        expected_sample_count is not None
        and counts.get("matched_event_count") != expected_sample_count
    ):
        reasons.append("accuracy report selection matched_event_count does not match samples")
    if all(
        name in counts
        for name in ("selected_event_count", "eligible_event_count", "skipped_event_count")
    ):
        if counts["selected_event_count"] != (
            counts["eligible_event_count"] + counts["skipped_event_count"]
        ):
            reasons.append("accuracy report selection event counts do not conserve selected events")
    if "eligible_event_count" in counts and "matched_event_count" in counts:
        if counts["eligible_event_count"] != counts["matched_event_count"] + len(unmatched):
            reasons.append("accuracy report selection eligible events do not conserve matches")
    if "truth_row_count" in counts and "matched_event_count" in counts:
        if counts["truth_row_count"] != counts["matched_event_count"] + len(unused):
            reasons.append("accuracy report selection truth rows do not conserve matches")


def _selection_id_list(value: object, name: str, reasons: list[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        reasons.append(f"accuracy report selection {name} must be an array of non-empty strings")
        return ()
    normalized = tuple(item.strip() for item in value)
    if len(set(normalized)) != len(normalized):
        reasons.append(f"accuracy report selection {name} must contain unique identifiers")
    return normalized


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _metrics(values: Iterable[float]) -> dict[str, float]:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("accuracy metrics require at least one value")
    mean = math.fsum(ordered) / len(ordered)
    return {
        "mean_m": mean,
        "median_m": float(statistics.median(ordered)),
        "p95_m": ordered[min(len(ordered) - 1, max(0, math.ceil(0.95 * len(ordered)) - 1))],
        "rmse_m": math.sqrt(math.fsum(value * value for value in ordered) / len(ordered)),
        "maximum_m": max(ordered),
    }


def _error_ellipse(
    north_errors: Iterable[float], east_errors: Iterable[float]
) -> dict[str, float | int]:
    """Fit a deterministic local-N/E error ellipse from WGS-84 residuals.

    The residuals are already expressed in metres at the surveyed latitude.
    We use the unbiased sample covariance (``n - 1``) when at least two
    observations exist, clamp only round-off level negative eigenvalues, and
    return both one-sigma axes and the 95% two-dimensional confidence ellipse.
    Orientation is clockwise from true north, matching the N/E convention used
    by target geolocation and the release planner.
    """

    north = tuple(float(value) for value in north_errors)
    east = tuple(float(value) for value in east_errors)
    if len(north) != len(east) or not north:
        raise ValueError("error ellipse requires paired non-empty N/E residuals")
    if any(not math.isfinite(value) for value in (*north, *east)):
        raise ValueError("error ellipse residuals must be finite")

    sample_count = len(north)
    bias_north = math.fsum(north) / sample_count
    bias_east = math.fsum(east) / sample_count
    denominator = sample_count - 1 if sample_count > 1 else 1
    covariance_north = (
        math.fsum((north_value - bias_north) ** 2 for north_value in north) / denominator
    )
    covariance_east = math.fsum((east_value - bias_east) ** 2 for east_value in east) / denominator
    covariance_cross = (
        math.fsum(
            (north_value - bias_north) * (east_value - bias_east)
            for north_value, east_value in zip(north, east, strict=True)
        )
        / denominator
    )

    # Eigenvalues of the symmetric 2x2 covariance matrix.  The covariance is
    # theoretically positive semidefinite; max(0, ...) only absorbs floating
    # point noise and never hides a negative input because all terms above are
    # sums of squares/products from finite residuals.
    discriminant = math.sqrt(
        max(
            0.0,
            (covariance_north - covariance_east) ** 2 + 4.0 * covariance_cross * covariance_cross,
        )
    )
    major_variance = max(
        0.0,
        0.5 * (covariance_north + covariance_east + discriminant),
    )
    minor_variance = max(
        0.0,
        0.5 * (covariance_north + covariance_east - discriminant),
    )
    major_1sigma = math.sqrt(major_variance)
    minor_1sigma = math.sqrt(minor_variance)
    if discriminant <= 1.0e-15:
        # An isotropic or zero covariance has no unique eigenvector.  Use a
        # stable north-aligned orientation instead of leaking numerical noise
        # into reports and hashes.
        orientation_deg = 0.0
    else:
        orientation_deg = 0.5 * math.degrees(
            math.atan2(2.0 * covariance_cross, covariance_north - covariance_east)
        )
    confidence95_scale = math.sqrt(5.991464547107979)
    return {
        "sample_count": sample_count,
        "bias_north_m": bias_north,
        "bias_east_m": bias_east,
        "covariance_north_m2": covariance_north,
        "covariance_east_m2": covariance_east,
        "covariance_north_east_m2": covariance_cross,
        "major_1sigma_m": major_1sigma,
        "minor_1sigma_m": minor_1sigma,
        "orientation_deg": orientation_deg,
        "major_95_m": confidence95_scale * major_1sigma,
        "minor_95_m": confidence95_scale * minor_1sigma,
    }


def _validate_coordinate(latitude_deg: object, longitude_deg: object, *, name: str) -> None:
    if (
        isinstance(latitude_deg, bool)
        or not isinstance(latitude_deg, (int, float))
        or not math.isfinite(float(latitude_deg))
        or not -90.0 < float(latitude_deg) < 90.0
    ):
        raise ValueError(f"{name} latitude must be finite and strictly inside (-90, 90)")
    if (
        isinstance(longitude_deg, bool)
        or not isinstance(longitude_deg, (int, float))
        or not math.isfinite(float(longitude_deg))
        or not -180.0 <= float(longitude_deg) <= 180.0
    ):
        raise ValueError(f"{name} longitude must be finite and within [-180, 180]")


def _validate_non_negative(value: object, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0.0
    ):
        raise ValueError(f"{name} must be finite and non-negative")


def _validate_positive_integer(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _validate_optional_positive(value: float | None, name: str) -> None:
    if value is not None:
        _validate_non_negative(value, name)
        if float(value) <= 0.0:
            raise ValueError(f"{name} must be positive when supplied")


def _validate_coverage(value: float | None, name: str) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0.0 <= float(value) <= 1.0
    ):
        raise ValueError(f"{name} must be within [0, 1]")


def _wrap_longitude_delta(delta_deg: float) -> float:
    """Normalize a longitude difference to [-180, 180]."""

    wrapped = (float(delta_deg) + 180.0) % 360.0 - 180.0
    # Keep the positive endpoint stable for callers that explicitly pass 180.
    return 180.0 if wrapped == -180.0 and delta_deg > 0.0 else wrapped


__all__ = [
    "GeolocationAccuracySample",
    "evaluate_geolocation_accuracy",
    "verify_geolocation_accuracy_report",
    "wgs84_distance_m",
]
