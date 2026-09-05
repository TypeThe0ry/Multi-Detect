"""Create and independently verify physical camera-mount survey evidence.

The runtime camera calibration contains mount attitude and GNSS-to-camera
lever-arm values, but numeric CLI input alone is not evidence that an installed
aircraft was measured.  This module closes that provenance seam.  It requires
repeated physical measurements, calibrated equipment, distinct surveyor and
reviewer identities, explicit FRD-frame attestations, and hash-bound raw notes.

The resulting gate proves only that one mount survey is internally complete
and agrees with one camera calibration.  It never enables flight control,
claims full hardware acceptance, or promotes a release.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import statistics
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .autonomy.serialization import strict_json_loads
from .product_version import PRODUCT_VERSION

CAMERA_MOUNT_SURVEY_SCHEMA_VERSION = 1
CAMERA_MOUNT_SURVEY_EVIDENCE_TYPE = "multidetect.camera-mount-survey.v1"
CAMERA_MOUNT_MEASUREMENT_PREFLIGHT_EVIDENCE_TYPE = (
    "multidetect.camera-mount-measurement-preflight.v1"
)
CAMERA_MOUNT_SURVEY_FRAME = "aircraft_frd"
_SAMPLE_FIELDS = (
    "pitch_down_deg",
    "yaw_right_deg",
    "roll_clockwise_deg",
    "offset_forward_m",
    "offset_right_m",
    "offset_down_m",
)
_ANGLE_FIELDS = _SAMPLE_FIELDS[:3]
_OFFSET_FIELDS = _SAMPLE_FIELDS[3:]
_ATTESTATIONS = (
    "aircraft_physically_present",
    "camera_measured_in_final_installed_configuration",
    "gnss_antenna_reference_point_marked",
    "aircraft_frd_axes_verified",
    "raw_measurements_retained",
    "simulated_or_nominal_values_excluded",
)
_RAW_CSV_COLUMNS = ("sample_id", *_SAMPLE_FIELDS)


def create_camera_mount_equipment_calibration_record(
    *,
    equipment_id: str,
    issuer: str,
    valid_from_utc: str,
    valid_until_utc: str,
    angular_accuracy_deg: float,
    linear_accuracy_m: float,
    certificate_path: str | Path,
) -> dict[str, object]:
    """Create a strict record that binds equipment claims to a certificate file."""

    valid_from = _utc_timestamp(valid_from_utc, "valid_from_utc")
    valid_until = _utc_timestamp(valid_until_utc, "valid_until_utc")
    if _parse_utc(valid_until, "valid_until_utc") <= _parse_utc(valid_from, "valid_from_utc"):
        raise ValueError("equipment calibration valid_until_utc must follow valid_from_utc")
    return {
        "schema_version": 1,
        "equipment_id": _text(equipment_id, "equipment_id"),
        "issuer": _text(issuer, "issuer"),
        "valid_from_utc": valid_from,
        "valid_until_utc": valid_until,
        "angular_accuracy_deg": _positive_bounded(
            angular_accuracy_deg, "angular_accuracy_deg", 5.0
        ),
        "linear_accuracy_m": _positive_bounded(linear_accuracy_m, "linear_accuracy_m", 1.0),
        "certificate": _file_reference(certificate_path),
    }


def write_camera_mount_equipment_calibration_record(
    destination: str | Path,
    document: Mapping[str, object],
    *,
    overwrite: bool = False,
) -> Path:
    """Validate and atomically write one equipment calibration record."""

    temporary_document = dict(document)
    # Exercise the exact same semantic validator used by survey creation.
    certificate = temporary_document.get("certificate")
    if not isinstance(certificate, Mapping) or not isinstance(certificate.get("path"), str):
        raise ValueError("equipment calibration certificate reference is missing")
    normalized = create_camera_mount_equipment_calibration_record(
        equipment_id=temporary_document.get("equipment_id"),
        issuer=temporary_document.get("issuer"),
        valid_from_utc=temporary_document.get("valid_from_utc"),
        valid_until_utc=temporary_document.get("valid_until_utc"),
        angular_accuracy_deg=temporary_document.get("angular_accuracy_deg"),
        linear_accuracy_m=temporary_document.get("linear_accuracy_m"),
        certificate_path=certificate["path"],
    )
    if temporary_document != normalized:
        raise ValueError("equipment calibration record does not match current certificate")
    payload = (
        json.dumps(normalized, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    return _write_bytes(destination, payload, overwrite=overwrite, label="equipment record")


def create_camera_mount_survey(
    *,
    survey_id: str,
    aircraft_id: str,
    camera_id: str,
    gnss_antenna_id: str,
    method: str,
    surveyor: str,
    reviewer: str,
    equipment_id: str,
    equipment_angular_accuracy_deg: float,
    equipment_linear_accuracy_m: float,
    equipment_calibration_path: str | Path,
    calibration_path: str | Path,
    raw_measurements_path: str | Path,
    attestations: Mapping[str, object],
    samples: Sequence[Mapping[str, object]] | None = None,
    minimum_samples: int = 5,
    maximum_angular_sigma_deg: float = 1.0,
    maximum_linear_sigma_m: float = 0.10,
    maximum_calibration_angle_delta_deg: float = 0.05,
    maximum_calibration_offset_delta_m: float = 0.01,
    created_at_utc: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic survey report from physical measurement inputs."""

    created = created_at_utc or datetime.now(timezone.utc).isoformat()
    raw_reference = _file_reference(raw_measurements_path)
    raw_samples = _load_raw_measurements_csv(Path(raw_reference["path"]))
    selected_samples = raw_samples if samples is None else list(samples)
    document: dict[str, Any] = {
        "event": "camera_mount_survey_evaluated",
        "schema_version": CAMERA_MOUNT_SURVEY_SCHEMA_VERSION,
        "evidence_type": CAMERA_MOUNT_SURVEY_EVIDENCE_TYPE,
        "release_label": PRODUCT_VERSION.release_label,
        "survey_id": _text(survey_id, "survey_id"),
        "created_at_utc": _utc_timestamp(created, "created_at_utc"),
        "aircraft_id": _text(aircraft_id, "aircraft_id"),
        "camera_id": _text(camera_id, "camera_id"),
        "gnss_antenna_id": _text(gnss_antenna_id, "gnss_antenna_id"),
        "measurement_frame": CAMERA_MOUNT_SURVEY_FRAME,
        "method": _text(method, "method"),
        "surveyor": _text(surveyor, "surveyor"),
        "reviewer": _text(reviewer, "reviewer"),
        "equipment": {
            "equipment_id": _text(equipment_id, "equipment_id"),
            "angular_accuracy_deg": _positive_bounded(
                equipment_angular_accuracy_deg, "equipment_angular_accuracy_deg", 5.0
            ),
            "linear_accuracy_m": _positive_bounded(
                equipment_linear_accuracy_m, "equipment_linear_accuracy_m", 1.0
            ),
            "calibration_valid_at_survey": True,
        },
        "attestations": {name: attestations.get(name) for name in _ATTESTATIONS},
        "policy": {
            "minimum_samples": _integer(minimum_samples, "minimum_samples", minimum=3),
            "maximum_angular_sigma_deg": _positive_bounded(
                maximum_angular_sigma_deg, "maximum_angular_sigma_deg", 10.0
            ),
            "maximum_linear_sigma_m": _positive_bounded(
                maximum_linear_sigma_m, "maximum_linear_sigma_m", 1.0
            ),
            "maximum_calibration_angle_delta_deg": _positive_bounded(
                maximum_calibration_angle_delta_deg,
                "maximum_calibration_angle_delta_deg",
                5.0,
            ),
            "maximum_calibration_offset_delta_m": _positive_bounded(
                maximum_calibration_offset_delta_m,
                "maximum_calibration_offset_delta_m",
                1.0,
            ),
        },
        "inputs": {
            "equipment_calibration": _file_reference(equipment_calibration_path),
            "camera_calibration": _file_reference(calibration_path),
            "raw_measurements": raw_reference,
        },
        "samples": [_sample(item, index) for index, item in enumerate(selected_samples)],
        "estimate": {},
        "checks": {},
        "blocking_reasons": [],
        "gate_passed": False,
        "mount_survey_acceptance": "not_claimed",
        "hardware_acceptance": "not_claimed",
        "release_eligible": False,
        "read_only_evidence": True,
        "advisory_only": True,
        "contains_flight_control": False,
        "simulated_values_used": False,
    }
    return _evaluate(document)


def verify_camera_mount_survey(
    source: str | Path | Mapping[str, object],
    *,
    expected_release_label: str | None = None,
    require_gate_passed: bool = False,
) -> dict[str, Any]:
    """Re-hash inputs, recompute all statistics, and compare the full report."""

    result: dict[str, Any] = {"passed": False, "gate_passed": False, "reasons": []}
    reasons: list[str] = result["reasons"]
    try:
        document = _load_document(source)
        _require_exact_top_level(document)
        if (
            expected_release_label is not None
            and document.get("release_label") != expected_release_label
        ):
            reasons.append("camera mount survey release_label does not match expectation")
        recomputed_seed = dict(document)
        inputs = document.get("inputs")
        if not isinstance(inputs, Mapping):
            raise ValueError("inputs must be an object")
        # Validate the serialized references before replacing them with fresh
        # descriptors for recomputation.  This preserves a precise failure
        # reason when either reviewed source file changed after sign-off.
        _reference(document, "camera_calibration")
        _reference(document, "raw_measurements")
        recomputed_seed["inputs"] = {
            "equipment_calibration": _file_reference(
                _reference_path(inputs, "equipment_calibration")
            ),
            "camera_calibration": _file_reference(_reference_path(inputs, "camera_calibration")),
            "raw_measurements": _file_reference(_reference_path(inputs, "raw_measurements")),
        }
        recomputed = _evaluate(recomputed_seed)
        if document != recomputed:
            reasons.append("camera mount survey does not match independent re-evaluation")
        result["gate_passed"] = recomputed.get("gate_passed") is True
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        reasons.append(f"verification error: {type(exc).__name__}: {exc}")
    if require_gate_passed and result["gate_passed"] is not True:
        reasons.append("camera mount survey gate_passed is not true")
    result["passed"] = not reasons
    return result


def write_camera_mount_survey(
    destination: str | Path,
    document: Mapping[str, object],
    *,
    overwrite: bool = False,
) -> Path:
    requested = Path(destination).expanduser()
    if requested.is_symlink():
        raise FileExistsError(f"camera mount survey output must not be a symlink: {requested}")
    output = requested.resolve()
    if output.exists() and not overwrite:
        raise FileExistsError(f"camera mount survey output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return output


def write_camera_mount_measurement_template(
    destination: str | Path,
    *,
    sample_count: int = 5,
    overwrite: bool = False,
) -> Path:
    """Write a deterministic blank v1 measurement worksheet atomically."""

    count = _integer(sample_count, "sample_count", minimum=5)
    if count > 1000:
        raise ValueError("sample_count must be at most 1000")
    rows = [",".join(_RAW_CSV_COLUMNS)]
    rows.extend(f"measurement-{index + 1:03d},,,,,," for index in range(count))
    payload = ("\n".join(rows) + "\n").encode("utf-8")
    return _write_bytes(destination, payload, overwrite=overwrite, label="measurement template")


def check_camera_mount_measurements(
    source: str | Path,
    *,
    minimum_samples: int = 5,
) -> dict[str, Any]:
    """Preflight one completed raw CSV without creating acceptance evidence."""

    minimum = _integer(minimum_samples, "minimum_samples", minimum=5)
    if minimum > 1000:
        raise ValueError("minimum_samples must be at most 1000")
    reference = _file_reference(source)
    reasons: list[str] = []
    samples: list[dict[str, object]] = []
    try:
        samples = _load_raw_measurements_csv(Path(reference["path"]))
    except (OSError, TypeError, ValueError) as exc:
        reasons.append(f"{type(exc).__name__}: {exc}")
    sample_ids = [sample["sample_id"] for sample in samples]
    if samples and len(samples) < minimum:
        reasons.append(f"sample count {len(samples)} is below required minimum {minimum}")
    if samples and len(sample_ids) != len(set(sample_ids)):
        reasons.append("sample IDs are not unique")
    return {
        "event": "camera_mount_measurements_preflighted",
        "schema_version": 1,
        "evidence_type": CAMERA_MOUNT_MEASUREMENT_PREFLIGHT_EVIDENCE_TYPE,
        "release_label": PRODUCT_VERSION.release_label,
        "input": reference,
        "expected_columns": list(_RAW_CSV_COLUMNS),
        "minimum_samples": minimum,
        "sample_count": len(samples),
        "sample_ids_unique": bool(samples) and len(sample_ids) == len(set(sample_ids)),
        "blocking_reasons": reasons,
        "gate_passed": not reasons,
        "hardware_acceptance": "not_claimed",
        "release_eligible": False,
        "read_only_evidence": True,
        "advisory_only": True,
        "contains_flight_control": False,
        "simulated_values_used": False,
    }


def _write_bytes(
    destination: str | Path,
    payload: bytes,
    *,
    overwrite: bool,
    label: str,
) -> Path:
    requested = Path(destination).expanduser()
    if requested.is_symlink():
        raise FileExistsError(f"camera mount {label} output must not be a symlink: {requested}")
    output = requested.resolve()
    if output.exists() and not overwrite:
        raise FileExistsError(f"camera mount {label} output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return output


def _evaluate(source: Mapping[str, object]) -> dict[str, Any]:
    document = dict(source)
    samples_raw = document.get("samples")
    if not isinstance(samples_raw, Sequence) or isinstance(samples_raw, (str, bytes, bytearray)):
        raise ValueError("samples must be an array")
    samples = [_sample(item, index) for index, item in enumerate(samples_raw)]
    ids = [item["sample_id"] for item in samples]
    policy = _policy(document.get("policy"))
    equipment = _equipment(document.get("equipment"))
    attestations = _attestations(document.get("attestations"))

    means = {
        name: statistics.fmean(float(item[name]) for item in samples) for name in _SAMPLE_FIELDS
    }
    standard_deviations = {
        name: statistics.stdev(float(item[name]) for item in samples) if len(samples) > 1 else 0.0
        for name in _SAMPLE_FIELDS
    }
    combined_sigma = {
        name: math.hypot(
            standard_deviations[name],
            equipment["angular_accuracy_deg"]
            if name in _ANGLE_FIELDS
            else equipment["linear_accuracy_m"],
        )
        for name in _SAMPLE_FIELDS
    }
    calibration_ref = _reference(document, "camera_calibration")
    equipment_calibration_ref = _reference(document, "equipment_calibration")
    equipment_calibration = _load_equipment_calibration_record(
        Path(equipment_calibration_ref["path"])
    )
    equipment_certificate_ref = _nested_file_reference(
        equipment_calibration, "certificate", context="equipment calibration"
    )
    raw_ref = _reference(document, "raw_measurements")
    raw_samples = _load_raw_measurements_csv(Path(raw_ref["path"]))
    calibration = _load_calibration(Path(calibration_ref["path"]))
    calibration_id = calibration["calibration_id"]
    camera_matches = document.get("camera_id") == calibration_id
    angle_mapping = {
        "pitch_down_deg": "mount_pitch_down_deg",
        "yaw_right_deg": "mount_yaw_right_deg",
        "roll_clockwise_deg": "mount_roll_clockwise_deg",
    }
    offset_mapping = {
        "offset_forward_m": "camera_offset_forward_m",
        "offset_right_m": "camera_offset_right_m",
        "offset_down_m": "camera_offset_down_m",
    }
    angle_deltas = {
        name: abs(means[name] - _number(calibration.get(key), f"calibration.{key}"))
        for name, key in angle_mapping.items()
    }
    offset_deltas = {
        name: abs(means[name] - _number(calibration.get(key, 0.0), f"calibration.{key}"))
        for name, key in offset_mapping.items()
    }
    angular_sigma = max(combined_sigma[name] for name in _ANGLE_FIELDS)
    linear_sigma = max(combined_sigma[name] for name in _OFFSET_FIELDS)
    calibration_boresight_sigma = _number(
        calibration.get("boresight_sigma_deg"), "calibration.boresight_sigma_deg"
    )
    calibration_offset_sigma = _number(
        calibration.get("camera_offset_sigma_m", 0.0), "calibration.camera_offset_sigma_m"
    )
    checks = {
        "sample_ids_unique": len(ids) == len(set(ids)),
        "minimum_samples": len(samples) >= policy["minimum_samples"],
        "surveyor_reviewer_distinct": (
            _text(document.get("surveyor"), "surveyor").casefold()
            != _text(document.get("reviewer"), "reviewer").casefold()
        ),
        "physical_attestations_complete": all(attestations.values()),
        "equipment_calibration_valid": (
            _parse_utc(equipment_calibration["valid_from_utc"], "valid_from_utc")
            <= _parse_utc(document.get("created_at_utc"), "created_at_utc")
            <= _parse_utc(equipment_calibration["valid_until_utc"], "valid_until_utc")
        ),
        "equipment_calibration_hash_bound": equipment_calibration_ref["bytes"] > 0,
        "equipment_certificate_hash_bound": equipment_certificate_ref["bytes"] > 0,
        "equipment_identity_matches_calibration": (
            equipment["equipment_id"] == equipment_calibration["equipment_id"]
        ),
        "equipment_accuracy_matches_calibration": (
            equipment["angular_accuracy_deg"] == equipment_calibration["angular_accuracy_deg"]
            and equipment["linear_accuracy_m"] == equipment_calibration["linear_accuracy_m"]
        ),
        "raw_measurements_hash_bound": raw_ref["bytes"] > 0,
        "samples_match_raw_measurements": samples == raw_samples,
        "camera_calibration_hash_bound": calibration_ref["bytes"] > 0,
        "camera_calibration_id_matches": camera_matches,
        "angular_repeatability_within_limit": angular_sigma <= policy["maximum_angular_sigma_deg"],
        "linear_repeatability_within_limit": linear_sigma <= policy["maximum_linear_sigma_m"],
        "calibration_angles_match_survey": all(
            value <= policy["maximum_calibration_angle_delta_deg"]
            for value in angle_deltas.values()
        ),
        "calibration_offsets_match_survey": all(
            value <= policy["maximum_calibration_offset_delta_m"]
            for value in offset_deltas.values()
        ),
        "calibration_angular_uncertainty_not_understated": calibration_boresight_sigma
        >= angular_sigma,
        "calibration_linear_uncertainty_not_understated": calibration_offset_sigma >= linear_sigma,
    }
    blocking = [name for name, passed in checks.items() if passed is not True]
    document["equipment"] = equipment
    document["equipment"]["calibration_valid_at_survey"] = checks["equipment_calibration_valid"]
    document["attestations"] = attestations
    document["policy"] = policy
    document["inputs"] = {
        "equipment_calibration": equipment_calibration_ref,
        "camera_calibration": calibration_ref,
        "raw_measurements": raw_ref,
    }
    document["samples"] = samples
    document["estimate"] = {
        "calibration_id": calibration_id,
        "equipment_calibration": {
            "issuer": equipment_calibration["issuer"],
            "valid_from_utc": equipment_calibration["valid_from_utc"],
            "valid_until_utc": equipment_calibration["valid_until_utc"],
            "certificate_sha256": equipment_certificate_ref["sha256"],
        },
        "means": means,
        "sample_standard_deviation": standard_deviations,
        "conservative_one_sigma": combined_sigma,
        "boresight_sigma_deg": angular_sigma,
        "camera_offset_sigma_m": linear_sigma,
        "calibration_angle_delta_deg": angle_deltas,
        "calibration_offset_delta_m": offset_deltas,
    }
    document["checks"] = checks
    document["blocking_reasons"] = blocking
    document["gate_passed"] = not blocking
    document["mount_survey_acceptance"] = "passed" if not blocking else "not_claimed"
    return document


def _load_document(source: str | Path | Mapping[str, object]) -> dict[str, Any]:
    if isinstance(source, Mapping):
        return dict(source)
    path = Path(source).expanduser().resolve()
    value = strict_json_loads(path.read_text(encoding="utf-8-sig"), name="camera mount survey")
    if not isinstance(value, Mapping):
        raise ValueError("camera mount survey must be an object")
    return dict(value)


def _load_calibration(path: Path) -> Mapping[str, object]:
    value = strict_json_loads(path.read_text(encoding="utf-8-sig"), name="camera calibration")
    if not isinstance(value, Mapping) or value.get("schema_version") != 1:
        raise ValueError("camera calibration must be a schema_version 1 object")
    calibration = value.get("calibration")
    if not isinstance(calibration, Mapping):
        raise ValueError("camera calibration calibration object is missing")
    return calibration


def _load_equipment_calibration_record(path: Path) -> dict[str, object]:
    value = strict_json_loads(
        path.read_text(encoding="utf-8-sig"), name="equipment calibration record"
    )
    if not isinstance(value, Mapping):
        raise ValueError("equipment calibration record must be an object")
    required = {
        "schema_version",
        "equipment_id",
        "issuer",
        "valid_from_utc",
        "valid_until_utc",
        "angular_accuracy_deg",
        "linear_accuracy_m",
        "certificate",
    }
    if set(value) != required or value.get("schema_version") != 1:
        raise ValueError("equipment calibration record must match schema_version 1 exactly")
    valid_from = _utc_timestamp(value.get("valid_from_utc"), "valid_from_utc")
    valid_until = _utc_timestamp(value.get("valid_until_utc"), "valid_until_utc")
    if _parse_utc(valid_until, "valid_until_utc") <= _parse_utc(valid_from, "valid_from_utc"):
        raise ValueError("equipment calibration valid_until_utc must follow valid_from_utc")
    record: dict[str, object] = {
        "schema_version": 1,
        "equipment_id": _text(value.get("equipment_id"), "equipment_id"),
        "issuer": _text(value.get("issuer"), "issuer"),
        "valid_from_utc": valid_from,
        "valid_until_utc": valid_until,
        "angular_accuracy_deg": _positive_bounded(
            value.get("angular_accuracy_deg"), "angular_accuracy_deg", 5.0
        ),
        "linear_accuracy_m": _positive_bounded(
            value.get("linear_accuracy_m"), "linear_accuracy_m", 1.0
        ),
        "certificate": dict(value["certificate"])
        if isinstance(value.get("certificate"), Mapping)
        else value.get("certificate"),
    }
    _nested_file_reference(record, "certificate", context="equipment calibration")
    return record


def _nested_file_reference(
    document: Mapping[str, object], name: str, *, context: str
) -> dict[str, object]:
    value = document.get(name)
    if not isinstance(value, Mapping):
        raise ValueError(f"{context} {name} must be a file reference")
    path = value.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ValueError(f"{context} {name}.path must be a string")
    actual = _file_reference(path)
    if dict(value) != actual:
        raise ValueError(f"{context} {name} hash/size/path does not match the current file")
    return actual


def _file_reference(value: str | Path) -> dict[str, object]:
    path = Path(value).expanduser().resolve()
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"survey input must be a regular non-symlink file: {path}")
    payload = path.read_bytes()
    return {"path": str(path), "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}


def _reference(document: Mapping[str, object], name: str) -> dict[str, object]:
    inputs = document.get("inputs")
    if not isinstance(inputs, Mapping):
        raise ValueError("inputs must be an object")
    reference = inputs.get(name)
    if not isinstance(reference, Mapping):
        raise ValueError(f"inputs.{name} must be an object")
    path = _reference_path(inputs, name)
    actual = _file_reference(path)
    if dict(reference) != actual:
        raise ValueError(f"inputs.{name} hash/size/path does not match the current file")
    return actual


def _reference_path(inputs: Mapping[str, object], name: str) -> Path:
    reference = inputs.get(name)
    if not isinstance(reference, Mapping):
        raise ValueError(f"inputs.{name} must be an object")
    path = reference.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ValueError(f"inputs.{name}.path must be a string")
    return Path(path).expanduser().resolve()


def _load_raw_measurements_csv(path: Path) -> list[dict[str, object]]:
    """Parse the exact v1 survey CSV used as the statistical source of truth."""

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != list(_RAW_CSV_COLUMNS):
            raise ValueError(
                "raw measurement CSV header must be exactly " + ",".join(_RAW_CSV_COLUMNS)
            )
        samples: list[dict[str, object]] = []
        for index, row in enumerate(reader):
            if None in row or set(row) != set(_RAW_CSV_COLUMNS):
                raise ValueError(f"raw measurement CSV row {index + 2} has extra columns")
            candidate: dict[str, object] = {"sample_id": row["sample_id"]}
            for name in _SAMPLE_FIELDS:
                try:
                    candidate[name] = float(row[name])
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"raw measurement CSV row {index + 2} {name} must be numeric"
                    ) from exc
            samples.append(_sample(candidate, index))
    if not samples:
        raise ValueError("raw measurement CSV must contain at least one sample")
    return samples


def _sample(value: object, index: int) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"sample {index} must be an object")
    allowed = {"sample_id", *_SAMPLE_FIELDS}
    if set(value) != allowed:
        raise ValueError(f"sample {index} must contain exactly {sorted(allowed)}")
    sample: dict[str, object] = {"sample_id": _text(value.get("sample_id"), "sample_id")}
    for name in _SAMPLE_FIELDS:
        number = _number(value.get(name), name)
        limit = 90.0 if name in _ANGLE_FIELDS else 10.0
        if abs(number) > limit:
            raise ValueError(f"sample {index} {name} is outside +/-{limit:g}")
        sample[name] = number
    return sample


def _policy(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("policy must be an object")
    return {
        "minimum_samples": _integer(value.get("minimum_samples"), "minimum_samples", minimum=3),
        "maximum_angular_sigma_deg": _positive_bounded(
            value.get("maximum_angular_sigma_deg"), "maximum_angular_sigma_deg", 10.0
        ),
        "maximum_linear_sigma_m": _positive_bounded(
            value.get("maximum_linear_sigma_m"), "maximum_linear_sigma_m", 1.0
        ),
        "maximum_calibration_angle_delta_deg": _positive_bounded(
            value.get("maximum_calibration_angle_delta_deg"),
            "maximum_calibration_angle_delta_deg",
            5.0,
        ),
        "maximum_calibration_offset_delta_m": _positive_bounded(
            value.get("maximum_calibration_offset_delta_m"),
            "maximum_calibration_offset_delta_m",
            1.0,
        ),
    }


def _equipment(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("equipment must be an object")
    return {
        "equipment_id": _text(value.get("equipment_id"), "equipment_id"),
        "angular_accuracy_deg": _positive_bounded(
            value.get("angular_accuracy_deg"), "angular_accuracy_deg", 5.0
        ),
        "linear_accuracy_m": _positive_bounded(
            value.get("linear_accuracy_m"), "linear_accuracy_m", 1.0
        ),
        "calibration_valid_at_survey": value.get("calibration_valid_at_survey") is True,
    }


def _attestations(value: object) -> dict[str, bool]:
    if not isinstance(value, Mapping) or set(value) != set(_ATTESTATIONS):
        raise ValueError("attestations must contain the exact physical survey checklist")
    return {name: value.get(name) is True for name in _ATTESTATIONS}


def _require_exact_top_level(document: Mapping[str, object]) -> None:
    expected = {
        "event",
        "schema_version",
        "evidence_type",
        "release_label",
        "survey_id",
        "created_at_utc",
        "aircraft_id",
        "camera_id",
        "gnss_antenna_id",
        "measurement_frame",
        "method",
        "surveyor",
        "reviewer",
        "equipment",
        "attestations",
        "policy",
        "inputs",
        "samples",
        "estimate",
        "checks",
        "blocking_reasons",
        "gate_passed",
        "mount_survey_acceptance",
        "hardware_acceptance",
        "release_eligible",
        "read_only_evidence",
        "advisory_only",
        "contains_flight_control",
        "simulated_values_used",
    }
    if set(document) != expected:
        raise ValueError("camera mount survey top-level fields are not exact")
    constants = {
        "event": "camera_mount_survey_evaluated",
        "schema_version": CAMERA_MOUNT_SURVEY_SCHEMA_VERSION,
        "evidence_type": CAMERA_MOUNT_SURVEY_EVIDENCE_TYPE,
        "measurement_frame": CAMERA_MOUNT_SURVEY_FRAME,
        "hardware_acceptance": "not_claimed",
        "release_eligible": False,
        "read_only_evidence": True,
        "advisory_only": True,
        "contains_flight_control": False,
        "simulated_values_used": False,
    }
    for name, expected_value in constants.items():
        if document.get(name) != expected_value:
            raise ValueError(f"{name} must equal {expected_value!r}")
    _utc_timestamp(document.get("created_at_utc"), "created_at_utc")


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _number(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _positive_bounded(value: object, name: str, maximum: float) -> float:
    number = _number(value, name)
    if not 0.0 < number <= maximum:
        raise ValueError(f"{name} must be in (0, {maximum:g}]")
    return number


def _integer(value: object, name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _utc_timestamp(value: object, name: str) -> str:
    text = _text(value, name)
    _parse_utc(text, name)
    return text


def _parse_utc(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{name} must include UTC timezone")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    template = commands.add_parser("template", help="write a blank raw-measurement CSV")
    template.add_argument("--output", required=True, type=Path)
    template.add_argument("--sample-count", type=int, default=5)
    template.add_argument("--overwrite", action="store_true")
    preflight = commands.add_parser(
        "validate-raw", help="validate a completed raw CSV before survey creation"
    )
    preflight.add_argument("raw_measurements", type=Path)
    preflight.add_argument("--minimum-samples", type=int, default=5)
    equipment = commands.add_parser(
        "equipment-record", help="bind equipment identity, accuracy and validity to a certificate"
    )
    equipment.add_argument("--equipment-id", required=True)
    equipment.add_argument("--issuer", required=True)
    equipment.add_argument("--valid-from-utc", required=True)
    equipment.add_argument("--valid-until-utc", required=True)
    equipment.add_argument("--angular-accuracy-deg", required=True, type=float)
    equipment.add_argument("--linear-accuracy-m", required=True, type=float)
    equipment.add_argument("--certificate", required=True, type=Path)
    equipment.add_argument("--output", required=True, type=Path)
    equipment.add_argument("--overwrite", action="store_true")
    create = commands.add_parser("create", help="create physical mount survey evidence")
    for name in (
        "survey-id",
        "aircraft-id",
        "camera-id",
        "gnss-antenna-id",
        "method",
        "surveyor",
        "reviewer",
        "equipment-id",
    ):
        create.add_argument(f"--{name}", required=True)
    create.add_argument("--equipment-angular-accuracy-deg", required=True, type=float)
    create.add_argument("--equipment-linear-accuracy-m", required=True, type=float)
    create.add_argument("--equipment-calibration", required=True, type=Path)
    create.add_argument("--camera-calibration", required=True, type=Path)
    create.add_argument("--raw-measurements", required=True, type=Path)
    for name in _ATTESTATIONS:
        create.add_argument(
            f"--attest-{name.replace('_', '-')}", action="store_true", required=True
        )
    create.add_argument("--output", required=True, type=Path)
    create.add_argument("--overwrite", action="store_true")
    verify = commands.add_parser("verify", help="independently re-evaluate a survey")
    verify.add_argument("survey", type=Path)
    verify.add_argument("--expected-release-label")
    verify.add_argument("--require-gate-passed", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "template":
        try:
            output = write_camera_mount_measurement_template(
                args.output,
                sample_count=args.sample_count,
                overwrite=args.overwrite,
            )
        except (OSError, TypeError, ValueError) as exc:
            print(f"camera mount measurement template failed: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({"output": str(output), "sample_count": args.sample_count}, indent=2))
        return 0
    if args.command == "validate-raw":
        try:
            result = check_camera_mount_measurements(
                args.raw_measurements,
                minimum_samples=args.minimum_samples,
            )
        except (OSError, TypeError, ValueError) as exc:
            print(f"camera mount raw measurement preflight failed: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True))
        return 0 if result["gate_passed"] else 1
    if args.command == "equipment-record":
        try:
            record = create_camera_mount_equipment_calibration_record(
                equipment_id=args.equipment_id,
                issuer=args.issuer,
                valid_from_utc=args.valid_from_utc,
                valid_until_utc=args.valid_until_utc,
                angular_accuracy_deg=args.angular_accuracy_deg,
                linear_accuracy_m=args.linear_accuracy_m,
                certificate_path=args.certificate,
            )
            output = write_camera_mount_equipment_calibration_record(
                args.output, record, overwrite=args.overwrite
            )
        except (OSError, TypeError, ValueError) as exc:
            print(f"camera mount equipment calibration record failed: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({"output": str(output), "equipment_id": args.equipment_id}, indent=2))
        return 0
    if args.command == "verify":
        result = verify_camera_mount_survey(
            args.survey,
            expected_release_label=args.expected_release_label,
            require_gate_passed=args.require_gate_passed,
        )
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True))
        return 0 if result["passed"] else 2
    try:
        attestations = {name: getattr(args, f"attest_{name}") for name in _ATTESTATIONS}
        document = create_camera_mount_survey(
            survey_id=args.survey_id,
            aircraft_id=args.aircraft_id,
            camera_id=args.camera_id,
            gnss_antenna_id=args.gnss_antenna_id,
            method=args.method,
            surveyor=args.surveyor,
            reviewer=args.reviewer,
            equipment_id=args.equipment_id,
            equipment_angular_accuracy_deg=args.equipment_angular_accuracy_deg,
            equipment_linear_accuracy_m=args.equipment_linear_accuracy_m,
            equipment_calibration_path=args.equipment_calibration,
            calibration_path=args.camera_calibration,
            raw_measurements_path=args.raw_measurements,
            attestations=attestations,
        )
        output = write_camera_mount_survey(args.output, document, overwrite=args.overwrite)
    except (OSError, TypeError, ValueError) as exc:
        print(f"camera mount survey failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"output": str(output), "gate_passed": document["gate_passed"]}, indent=2))
    return 0 if document["gate_passed"] else 1


__all__ = [
    "CAMERA_MOUNT_SURVEY_EVIDENCE_TYPE",
    "CAMERA_MOUNT_SURVEY_FRAME",
    "CAMERA_MOUNT_SURVEY_SCHEMA_VERSION",
    "CAMERA_MOUNT_MEASUREMENT_PREFLIGHT_EVIDENCE_TYPE",
    "build_parser",
    "check_camera_mount_measurements",
    "create_camera_mount_equipment_calibration_record",
    "create_camera_mount_survey",
    "main",
    "verify_camera_mount_survey",
    "write_camera_mount_measurement_template",
    "write_camera_mount_equipment_calibration_record",
    "write_camera_mount_survey",
]


if __name__ == "__main__":
    raise SystemExit(main())
