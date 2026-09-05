"""Aggregate reproduced Gazebo geolocation evidence into a regime-coverage gate.

The matrix is deliberately narrower than a release gate.  It proves only that
the same software-SIM camera/ranging/WGS-84 chain has been reproduced across
separated altitude, distance and terrain-slope regimes, plus a fail-closed
no-coordinate case.  It does not claim QGC, HIL, hardware, outdoor or flight
acceptance.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .autonomy.serialization import canonical_sha256, strict_json_loads
from .gazebo_target_geolocation_acceptance import (
    EVIDENCE_SCHEMA as SOURCE_EVIDENCE_SCHEMA,
)
from .gazebo_target_geolocation_acceptance import (
    EVIDENCE_SCHEMA_VERSION as SOURCE_EVIDENCE_SCHEMA_VERSION,
)
from .gazebo_target_geolocation_acceptance import (
    verify_gazebo_target_geolocation_evidence,
    write_evidence_exclusive,
)
from .model_manifest import sha256_file

MATRIX_SCHEMA = "multidetect.gazebo-geolocation-regime-matrix"
MATRIX_SCHEMA_VERSION = 1
MATRIX_EVALUATOR_SOURCE_PATH = "src/multidetect/gazebo_geolocation_regime_matrix.py"


class GazeboGeolocationRegimeMatrixError(ValueError):
    """Raised when matrix inputs violate their immutable contracts."""


@dataclass(frozen=True, slots=True)
class GazeboGeolocationRegimeRequirements:
    minimum_positive_cases: int = 3
    minimum_positive_frames: int = 15
    minimum_negative_no_coordinate_cases: int = 1
    minimum_negative_frames: int = 5
    minimum_altitude_regimes: int = 3
    altitude_regime_separation_m: float = 5.0
    minimum_distance_regimes: int = 3
    distance_regime_separation_m: float = 5.0
    minimum_slope_regimes: int = 2
    slope_regime_separation_deg: float = 3.0

    def __post_init__(self) -> None:
        for name in (
            "minimum_positive_cases",
            "minimum_positive_frames",
            "minimum_negative_no_coordinate_cases",
            "minimum_negative_frames",
            "minimum_altitude_regimes",
            "minimum_distance_regimes",
            "minimum_slope_regimes",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise GazeboGeolocationRegimeMatrixError(f"{name} must be a positive integer")
        for name in (
            "altitude_regime_separation_m",
            "distance_regime_separation_m",
            "slope_regime_separation_deg",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0.0
            ):
                raise GazeboGeolocationRegimeMatrixError(f"{name} must be finite and positive")


Verifier = Callable[..., Mapping[str, Any]]


def evaluate_gazebo_geolocation_regime_matrix(
    *,
    repository_root: str | Path,
    positive_evidence_paths: Sequence[str | Path],
    negative_no_coordinate_evidence_paths: Sequence[str | Path],
    requirements: GazeboGeolocationRegimeRequirements | None = None,
    verifier: Verifier = verify_gazebo_target_geolocation_evidence,
) -> dict[str, Any]:
    """Reproduce source evidence and evaluate cross-regime SIM coverage."""

    root = _repository_root(Path(repository_root))
    required = requirements or GazeboGeolocationRegimeRequirements()
    positive_paths = _source_paths(root, positive_evidence_paths, "positive evidence")
    negative_paths = _source_paths(
        root,
        negative_no_coordinate_evidence_paths,
        "negative no-coordinate evidence",
    )
    all_paths = positive_paths + negative_paths
    if len(set(all_paths)) != len(all_paths):
        raise GazeboGeolocationRegimeMatrixError("evidence paths must be unique across the matrix")

    positive_cases = [
        _verified_case(root, path, "positive", verifier=verifier) for path in positive_paths
    ]
    negative_cases = [
        _verified_case(root, path, "negative_no_coordinate", verifier=verifier)
        for path in negative_paths
    ]
    cases = positive_cases + negative_cases

    identity_reasons = _identity_reasons(cases)
    altitude_values = [case["regime"]["vehicle_altitude_agl_m"]["mean"] for case in positive_cases]
    distance_values = [case["regime"]["truth_ground_range_m"]["mean"] for case in positive_cases]
    slope_values = [case["regime"]["terrain_at_truth"]["slope_deg"] for case in positive_cases]
    altitude_regimes = _separated_regimes(altitude_values, required.altitude_regime_separation_m)
    distance_regimes = _separated_regimes(distance_values, required.distance_regime_separation_m)
    slope_regimes = _separated_regimes(slope_values, required.slope_regime_separation_deg)
    positive_frames = sum(case["frame_count"] for case in positive_cases)
    negative_frames = sum(case["frame_count"] for case in negative_cases)

    gates = {
        "all_source_evidence_reproduced": all(
            case["verification"]["passed"] is True for case in cases
        ),
        "consistent_release_identity": not any(
            reason.startswith("release identity") for reason in identity_reasons
        ),
        "consistent_evaluator_identity": not any(
            reason.startswith("evaluator identity") for reason in identity_reasons
        ),
        "minimum_positive_cases": len(positive_cases) >= required.minimum_positive_cases,
        "minimum_positive_frames": positive_frames >= required.minimum_positive_frames,
        "minimum_negative_no_coordinate_cases": len(negative_cases)
        >= required.minimum_negative_no_coordinate_cases,
        "minimum_negative_frames": negative_frames >= required.minimum_negative_frames,
        "minimum_altitude_regimes": len(altitude_regimes) >= required.minimum_altitude_regimes,
        "minimum_distance_regimes": len(distance_regimes) >= required.minimum_distance_regimes,
        "minimum_slope_regimes": len(slope_regimes) >= required.minimum_slope_regimes,
    }
    reasons = _gate_reasons(
        gates,
        required,
        positive_case_count=len(positive_cases),
        positive_frames=positive_frames,
        negative_case_count=len(negative_cases),
        negative_frames=negative_frames,
        altitude_regime_count=len(altitude_regimes),
        distance_regime_count=len(distance_regimes),
        slope_regime_count=len(slope_regimes),
    )
    gate_passed = all(gates.values()) and not reasons

    document: dict[str, Any] = {
        "schema": MATRIX_SCHEMA,
        "schema_version": MATRIX_SCHEMA_VERSION,
        "evaluated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "scope": "gazebo_reproduced_camera_to_wgs84_software_regime_matrix",
        "source_evidence_contract": {
            "schema": SOURCE_EVIDENCE_SCHEMA,
            "schema_version": SOURCE_EVIDENCE_SCHEMA_VERSION,
        },
        "matrix_evaluator_identity": {
            "module_path": MATRIX_EVALUATOR_SOURCE_PATH,
            "module_sha256": sha256_file(root / MATRIX_EVALUATOR_SOURCE_PATH),
        },
        "requirements": asdict(required),
        "release_identity": cases[0]["release_identity"] if cases else None,
        "evaluator_identity": cases[0]["evaluator_identity"] if cases else None,
        "cases": cases,
        "coverage": {
            "positive_case_count": len(positive_cases),
            "positive_frame_count": positive_frames,
            "negative_no_coordinate_case_count": len(negative_cases),
            "negative_frame_count": negative_frames,
            "altitude_regimes_m": altitude_regimes,
            "distance_regimes_m": distance_regimes,
            "slope_regimes_deg": slope_regimes,
        },
        "gates": gates,
        "reasons": reasons,
        "claims": {
            "environment": "software_simulation",
            "sim_regime_matrix_acceptance": gate_passed,
            "qgc_rendering": "not_claimed",
            "manual_visual_acceptance": "not_claimed",
            "hardware": "not_claimed",
            "outdoor_gnss": "not_claimed",
            "hil": "not_claimed",
            "real": "not_claimed",
            "flight": "not_claimed",
        },
        "production_approved": False,
        "qgc_visual_acceptance": "not_claimed",
        "hardware_acceptance": False,
        "hil_acceptance": False,
        "real_acceptance": False,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
        "gate_passed": gate_passed,
    }
    document["deterministic_result_sha256"] = _deterministic_hash(document)
    document["evidence_sha256"] = canonical_sha256(document)
    return document


def build_matrix_input_error_document(message: str) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema": MATRIX_SCHEMA,
        "schema_version": MATRIX_SCHEMA_VERSION,
        "evaluated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "input_contract_error",
        "error": {"type": "GazeboGeolocationRegimeMatrixError", "message": str(message)},
        "claims": {
            "environment": "software_simulation",
            "sim_regime_matrix_acceptance": False,
            "qgc_rendering": "not_claimed",
            "manual_visual_acceptance": "not_claimed",
            "hardware": "not_claimed",
            "outdoor_gnss": "not_claimed",
            "hil": "not_claimed",
            "real": "not_claimed",
            "flight": "not_claimed",
        },
        "production_approved": False,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
        "gate_passed": False,
    }
    document["evidence_sha256"] = canonical_sha256(document)
    return document


def _verified_case(
    root: Path,
    path: Path,
    role: str,
    *,
    verifier: Verifier,
) -> dict[str, Any]:
    document = _load_object(path, "source evidence")
    if document.get("schema") != SOURCE_EVIDENCE_SCHEMA:
        raise GazeboGeolocationRegimeMatrixError(f"{path}: source evidence schema is invalid")
    if document.get("schema_version") != SOURCE_EVIDENCE_SCHEMA_VERSION:
        raise GazeboGeolocationRegimeMatrixError(
            f"{path}: source evidence schema_version must be {SOURCE_EVIDENCE_SCHEMA_VERSION}"
        )
    verification = dict(
        verifier(
            path,
            repository_root=root,
            require_gate_passed=role == "positive",
        )
    )
    if verification.get("passed") is not True:
        reasons = verification.get("reasons")
        raise GazeboGeolocationRegimeMatrixError(
            f"{path}: source evidence did not reproduce: {reasons}"
        )
    gate_passed = document.get("gate_passed")
    frames = _array(document.get("frames"), f"{path}: frames")
    scoring = _mapping(document.get("scoring"), f"{path}: scoring")
    if role == "positive" and gate_passed is not True:
        raise GazeboGeolocationRegimeMatrixError(f"{path}: positive evidence gate is not true")
    if role == "negative_no_coordinate":
        if gate_passed is not False:
            raise GazeboGeolocationRegimeMatrixError(
                f"{path}: negative no-coordinate evidence gate must be false"
            )
        if scoring.get("geolocated_frame_count") != 0:
            raise GazeboGeolocationRegimeMatrixError(
                f"{path}: negative evidence geolocated_frame_count must be zero"
            )
        if any(
            _mapping(frame, f"{path}: frame").get("predicted_target") is not None
            for frame in frames
        ):
            raise GazeboGeolocationRegimeMatrixError(
                f"{path}: negative evidence contains a predicted target coordinate"
            )

    regime = _validated_regime(document.get("regime"), path)
    scenario = _mapping(document.get("scenario"), f"{path}: scenario")
    frame_set = _mapping(document.get("frame_set_identity"), f"{path}: frame_set_identity")
    model = _mapping(document.get("model_identity"), f"{path}: model_identity")
    release_identity = dict(_mapping(document.get("release_identity"), f"{path}: release_identity"))
    evaluator_identity = dict(
        _mapping(document.get("evaluator_identity"), f"{path}: evaluator_identity")
    )
    return {
        "role": role,
        "evidence_path": path.relative_to(root).as_posix(),
        "evidence_file_bytes": path.stat().st_size,
        "evidence_file_sha256": sha256_file(path),
        "evidence_sha256": _sha256(document.get("evidence_sha256"), f"{path}: evidence_sha256"),
        "deterministic_result_sha256": _sha256(
            document.get("deterministic_result_sha256"),
            f"{path}: deterministic_result_sha256",
        ),
        "release_identity": release_identity,
        "evaluator_identity": evaluator_identity,
        "scenario_id": _text(scenario.get("scenario_id"), f"{path}: scenario_id"),
        "frame_set_manifest_path": _text(
            frame_set.get("manifest_path"), f"{path}: frame-set manifest_path"
        ),
        "model_id": _text(model.get("model_id"), f"{path}: model_id"),
        "frame_count": _positive_int(scoring.get("frame_count"), f"{path}: frame_count"),
        "regime": regime,
        "scoring": dict(scoring),
        "verification": {
            "passed": True,
            "reproduced": verification.get("reproduced") is True,
            "verification_sha256": verification.get("verification_sha256"),
        },
    }


def _identity_reasons(cases: Sequence[Mapping[str, Any]]) -> list[str]:
    if not cases:
        return []
    release = cases[0]["release_identity"]
    evaluator = cases[0]["evaluator_identity"]
    reasons: list[str] = []
    if any(case["release_identity"] != release for case in cases[1:]):
        reasons.append("release identity differs across source evidence")
    if any(case["evaluator_identity"] != evaluator for case in cases[1:]):
        reasons.append("evaluator identity differs across source evidence")
    return reasons


def _separated_regimes(values: Sequence[float], separation: float) -> list[float]:
    representatives: list[float] = []
    for value in sorted(values):
        if not representatives or value - representatives[-1] >= separation:
            representatives.append(value)
    return representatives


def _gate_reasons(
    gates: Mapping[str, bool],
    required: GazeboGeolocationRegimeRequirements,
    **actual: int,
) -> list[str]:
    messages = {
        "all_source_evidence_reproduced": "not all source evidence reproduced",
        "consistent_release_identity": "release identity differs across source evidence",
        "consistent_evaluator_identity": "evaluator identity differs across source evidence",
        "minimum_positive_cases": (
            f"positive cases {actual['positive_case_count']} < {required.minimum_positive_cases}"
        ),
        "minimum_positive_frames": (
            f"positive frames {actual['positive_frames']} < {required.minimum_positive_frames}"
        ),
        "minimum_negative_no_coordinate_cases": (
            "negative no-coordinate cases "
            f"{actual['negative_case_count']} < {required.minimum_negative_no_coordinate_cases}"
        ),
        "minimum_negative_frames": (
            f"negative frames {actual['negative_frames']} < {required.minimum_negative_frames}"
        ),
        "minimum_altitude_regimes": (
            f"altitude regimes {actual['altitude_regime_count']} < "
            f"{required.minimum_altitude_regimes}"
        ),
        "minimum_distance_regimes": (
            f"distance regimes {actual['distance_regime_count']} < "
            f"{required.minimum_distance_regimes}"
        ),
        "minimum_slope_regimes": (
            f"slope regimes {actual['slope_regime_count']} < {required.minimum_slope_regimes}"
        ),
    }
    return [messages[name] for name, passed in gates.items() if not passed]


def _validated_regime(value: Any, path: Path) -> dict[str, Any]:
    regime = _mapping(value, f"{path}: regime")
    altitude = _metric_summary(regime.get("vehicle_altitude_agl_m"), f"{path}: altitude")
    distance = _metric_summary(regime.get("truth_ground_range_m"), f"{path}: distance")
    terrain = _mapping(regime.get("terrain_at_truth"), f"{path}: terrain_at_truth")
    slope = _finite_number(terrain.get("slope_deg"), f"{path}: slope_deg")
    return {
        "vehicle_altitude_agl_m": altitude,
        "truth_ground_range_m": distance,
        "terrain_at_truth": {**dict(terrain), "slope_deg": slope},
    }


def _metric_summary(value: Any, label: str) -> dict[str, float]:
    summary = _mapping(value, label)
    result = {
        name: _finite_number(summary.get(name), f"{label}.{name}")
        for name in ("minimum", "maximum", "mean")
    }
    if result["minimum"] > result["maximum"] or not (
        result["minimum"] <= result["mean"] <= result["maximum"]
    ):
        raise GazeboGeolocationRegimeMatrixError(f"{label} is not an ordered metric summary")
    return result


def _source_paths(root: Path, values: Sequence[str | Path], label: str) -> list[Path]:
    result: list[Path] = []
    for value in values:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file():
            raise GazeboGeolocationRegimeMatrixError(f"{label} is not a regular file: {resolved}")
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise GazeboGeolocationRegimeMatrixError(
                f"{label} must remain inside repository root: {resolved}"
            ) from exc
        result.append(resolved)
    return result


def _repository_root(path: Path) -> Path:
    root = path.resolve(strict=True)
    if not root.is_dir() or not (root / "pyproject.toml").is_file():
        raise GazeboGeolocationRegimeMatrixError(
            f"repository root must contain pyproject.toml: {root}"
        )
    return root


def _load_object(path: Path, label: str) -> dict[str, Any]:
    value = strict_json_loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise GazeboGeolocationRegimeMatrixError(f"{label} must contain a JSON object")
    return value


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GazeboGeolocationRegimeMatrixError(f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise GazeboGeolocationRegimeMatrixError(f"{label} must be an array")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GazeboGeolocationRegimeMatrixError(f"{label} must be a non-empty string")
    return value


def _sha256(value: Any, label: str) -> str:
    text = _text(value, label).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise GazeboGeolocationRegimeMatrixError(f"{label} must be a SHA-256 digest")
    return text


def _positive_int(value: Any, label: str) -> int:
    if type(value) is not int or value < 1:
        raise GazeboGeolocationRegimeMatrixError(f"{label} must be a positive integer")
    return value


def _finite_number(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise GazeboGeolocationRegimeMatrixError(f"{label} must be a finite number")
    return float(value)


def _deterministic_hash(document: Mapping[str, Any]) -> str:
    payload = dict(document)
    payload.pop("evaluated_at_utc", None)
    payload.pop("deterministic_result_sha256", None)
    payload.pop("evidence_sha256", None)
    return canonical_sha256(payload)


def _build_parser() -> argparse.ArgumentParser:
    defaults = GazeboGeolocationRegimeRequirements()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--positive-evidence", type=Path, action="append", default=[])
    parser.add_argument("--negative-no-coordinate-evidence", type=Path, action="append", default=[])
    parser.add_argument(
        "--minimum-positive-cases", type=int, default=defaults.minimum_positive_cases
    )
    parser.add_argument(
        "--minimum-positive-frames", type=int, default=defaults.minimum_positive_frames
    )
    parser.add_argument(
        "--minimum-negative-no-coordinate-cases",
        type=int,
        default=defaults.minimum_negative_no_coordinate_cases,
    )
    parser.add_argument(
        "--minimum-negative-frames", type=int, default=defaults.minimum_negative_frames
    )
    parser.add_argument(
        "--minimum-altitude-regimes", type=int, default=defaults.minimum_altitude_regimes
    )
    parser.add_argument(
        "--altitude-regime-separation-m", type=float, default=defaults.altitude_regime_separation_m
    )
    parser.add_argument(
        "--minimum-distance-regimes", type=int, default=defaults.minimum_distance_regimes
    )
    parser.add_argument(
        "--distance-regime-separation-m", type=float, default=defaults.distance_regime_separation_m
    )
    parser.add_argument("--minimum-slope-regimes", type=int, default=defaults.minimum_slope_regimes)
    parser.add_argument(
        "--slope-regime-separation-deg", type=float, default=defaults.slope_regime_separation_deg
    )
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        requirements = GazeboGeolocationRegimeRequirements(
            minimum_positive_cases=args.minimum_positive_cases,
            minimum_positive_frames=args.minimum_positive_frames,
            minimum_negative_no_coordinate_cases=args.minimum_negative_no_coordinate_cases,
            minimum_negative_frames=args.minimum_negative_frames,
            minimum_altitude_regimes=args.minimum_altitude_regimes,
            altitude_regime_separation_m=args.altitude_regime_separation_m,
            minimum_distance_regimes=args.minimum_distance_regimes,
            distance_regime_separation_m=args.distance_regime_separation_m,
            minimum_slope_regimes=args.minimum_slope_regimes,
            slope_regime_separation_deg=args.slope_regime_separation_deg,
        )
        document = evaluate_gazebo_geolocation_regime_matrix(
            repository_root=args.repository_root,
            positive_evidence_paths=args.positive_evidence,
            negative_no_coordinate_evidence_paths=args.negative_no_coordinate_evidence,
            requirements=requirements,
        )
    except (GazeboGeolocationRegimeMatrixError, OSError, TypeError, ValueError) as exc:
        document = build_matrix_input_error_document(str(exc))
    write_evidence_exclusive(args.out, document)
    print(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
    return 0 if document["gate_passed"] else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "MATRIX_SCHEMA",
    "MATRIX_SCHEMA_VERSION",
    "MATRIX_EVALUATOR_SOURCE_PATH",
    "GazeboGeolocationRegimeMatrixError",
    "GazeboGeolocationRegimeRequirements",
    "build_matrix_input_error_document",
    "evaluate_gazebo_geolocation_regime_matrix",
    "main",
]
