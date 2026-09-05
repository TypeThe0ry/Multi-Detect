from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from multidetect.autonomy.serialization import canonical_sha256
from multidetect.gazebo_geolocation_regime_matrix import (
    MATRIX_SCHEMA,
    GazeboGeolocationRegimeMatrixError,
    GazeboGeolocationRegimeRequirements,
    evaluate_gazebo_geolocation_regime_matrix,
)
from multidetect.gazebo_target_geolocation_acceptance import (
    EVIDENCE_SCHEMA,
    EVIDENCE_SCHEMA_VERSION,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SHA_A = "a" * 64
SHA_B = "b" * 64


def _repository(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    source = tmp_path / "src" / "multidetect" / "gazebo_geolocation_regime_matrix.py"
    source.parent.mkdir(parents=True)
    source.write_text("# matrix evaluator fixture\n", encoding="utf-8")
    return tmp_path


def _document(
    *,
    scenario_id: str,
    altitude_m: float,
    distance_m: float,
    slope_deg: float,
    positive: bool,
    frame_count: int = 5,
) -> dict[str, Any]:
    frames = [
        {
            "frame_id": f"frame-{index}",
            "predicted_target": {"latitude_deg": 1.3, "longitude_deg": 103.8} if positive else None,
        }
        for index in range(frame_count)
    ]
    document: dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "release_identity": {
            "product": "Multi-Detect",
            "release_label": "0.3.0-pre.8",
            "runtime_mode": "sim",
            "parity": {"core_algorithm_hash": SHA_A},
        },
        "evaluator_identity": {
            "module_path": "src/multidetect/gazebo_target_geolocation_acceptance.py",
            "module_sha256": SHA_B,
        },
        "scenario": {"scenario_id": scenario_id},
        "frame_set_identity": {"manifest_path": f"fixtures/{scenario_id}/frames.json"},
        "model_identity": {"model_id": "fixture-model"},
        "regime": {
            "vehicle_altitude_agl_m": {
                "minimum": altitude_m - 0.1,
                "maximum": altitude_m + 0.1,
                "mean": altitude_m,
            },
            "truth_ground_range_m": {
                "minimum": distance_m,
                "maximum": distance_m,
                "mean": distance_m,
            },
            "terrain_at_truth": {
                "elevation_msl_m": 25.0,
                "north_slope_m_per_m": 0.0,
                "east_slope_m_per_m": 0.0,
                "slope_m_per_m": 0.0,
                "slope_deg": slope_deg,
            },
        },
        "scoring": {
            "frame_count": frame_count,
            "geolocated_frame_count": frame_count if positive else 0,
        },
        "frames": frames,
        "gate_passed": positive,
        "deterministic_result_sha256": SHA_A,
    }
    document["evidence_sha256"] = canonical_sha256(document)
    return document


def _write(root: Path, name: str, document: dict[str, Any]) -> Path:
    path = root / "evidence" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    return path


def _passing_verifier(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return {
        "passed": True,
        "reproduced": True,
        "reasons": [],
        "verification_sha256": SHA_B,
    }


def test_complete_separated_regime_matrix_passes(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    positives = [
        _write(
            root,
            f"positive-{index}",
            _document(
                scenario_id=f"positive-{index}",
                altitude_m=altitude,
                distance_m=distance,
                slope_deg=slope,
                positive=True,
            ),
        )
        for index, (altitude, distance, slope) in enumerate(
            ((5.0, 5.0, 0.0), (15.0, 15.0, 4.0), (25.0, 25.0, 8.0)),
            start=1,
        )
    ]
    negative = _write(
        root,
        "negative",
        _document(
            scenario_id="negative",
            altitude_m=0.2,
            distance_m=5.5,
            slope_deg=0.0,
            positive=False,
        ),
    )

    result = evaluate_gazebo_geolocation_regime_matrix(
        repository_root=root,
        positive_evidence_paths=positives,
        negative_no_coordinate_evidence_paths=[negative],
        verifier=_passing_verifier,
    )

    assert result["schema"] == MATRIX_SCHEMA
    assert result["gate_passed"] is True
    assert result["reasons"] == []
    assert all(result["gates"].values())
    assert result["coverage"] == {
        "positive_case_count": 3,
        "positive_frame_count": 15,
        "negative_no_coordinate_case_count": 1,
        "negative_frame_count": 5,
        "altitude_regimes_m": [5.0, 15.0, 25.0],
        "distance_regimes_m": [5.0, 15.0, 25.0],
        "slope_regimes_deg": [0.0, 4.0, 8.0],
    }
    assert result["claims"]["sim_regime_matrix_acceptance"] is True
    assert result["claims"]["qgc_rendering"] == "not_claimed"
    unhashed = dict(result)
    evidence_sha256 = unhashed.pop("evidence_sha256")
    assert evidence_sha256 == canonical_sha256(unhashed)


def test_current_like_single_flat_positive_reports_exact_coverage_gaps(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    positive = _write(
        root,
        "positive",
        _document(
            scenario_id="positive",
            altitude_m=6.2,
            distance_m=8.0,
            slope_deg=0.0,
            positive=True,
        ),
    )
    negative = _write(
        root,
        "negative",
        _document(
            scenario_id="negative",
            altitude_m=0.2,
            distance_m=5.5,
            slope_deg=0.0,
            positive=False,
        ),
    )

    result = evaluate_gazebo_geolocation_regime_matrix(
        repository_root=root,
        positive_evidence_paths=[positive],
        negative_no_coordinate_evidence_paths=[negative],
        verifier=_passing_verifier,
    )

    assert result["gate_passed"] is False
    assert result["reasons"] == [
        "positive cases 1 < 3",
        "positive frames 5 < 15",
        "altitude regimes 1 < 3",
        "distance regimes 1 < 3",
        "slope regimes 1 < 2",
    ]
    assert result["gates"]["minimum_negative_no_coordinate_cases"] is True
    assert result["gates"]["minimum_negative_frames"] is True


@pytest.mark.parametrize("identity", ["release_identity", "evaluator_identity"])
def test_mismatched_source_identity_fails_closed(tmp_path: Path, identity: str) -> None:
    root = _repository(tmp_path)
    first = _document(
        scenario_id="first",
        altitude_m=5.0,
        distance_m=5.0,
        slope_deg=0.0,
        positive=True,
    )
    second = deepcopy(first)
    second["scenario"]["scenario_id"] = "second"
    second[identity] = {**second[identity], "different": True}
    first_path = _write(root, "first", first)
    second_path = _write(root, "second", second)

    result = evaluate_gazebo_geolocation_regime_matrix(
        repository_root=root,
        positive_evidence_paths=[first_path, second_path],
        negative_no_coordinate_evidence_paths=[],
        requirements=GazeboGeolocationRegimeRequirements(
            minimum_positive_cases=1,
            minimum_positive_frames=1,
            minimum_negative_no_coordinate_cases=1,
            minimum_negative_frames=1,
            minimum_altitude_regimes=1,
            minimum_distance_regimes=1,
            minimum_slope_regimes=1,
        ),
        verifier=_passing_verifier,
    )

    assert result["gate_passed"] is False
    assert (
        f"{identity.removesuffix('_identity')} identity differs across source evidence"
        in result["reasons"]
    )


def test_negative_case_with_coordinate_is_rejected(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    document = _document(
        scenario_id="unsafe-negative",
        altitude_m=0.2,
        distance_m=5.5,
        slope_deg=0.0,
        positive=False,
    )
    document["frames"][2]["predicted_target"] = {
        "latitude_deg": 1.3,
        "longitude_deg": 103.8,
    }
    path = _write(root, "unsafe-negative", document)

    with pytest.raises(
        GazeboGeolocationRegimeMatrixError,
        match="negative evidence contains a predicted target coordinate",
    ):
        evaluate_gazebo_geolocation_regime_matrix(
            repository_root=root,
            positive_evidence_paths=[],
            negative_no_coordinate_evidence_paths=[path],
            verifier=_passing_verifier,
        )


def test_duplicate_evidence_path_is_rejected(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    path = _write(
        root,
        "positive",
        _document(
            scenario_id="positive",
            altitude_m=5.0,
            distance_m=5.0,
            slope_deg=0.0,
            positive=True,
        ),
    )
    with pytest.raises(GazeboGeolocationRegimeMatrixError, match="must be unique"):
        evaluate_gazebo_geolocation_regime_matrix(
            repository_root=root,
            positive_evidence_paths=[path, path],
            negative_no_coordinate_evidence_paths=[],
            verifier=_passing_verifier,
        )


def test_script_help_runs_without_installed_package() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(REPOSITORY_ROOT / "scripts" / "run_gazebo_geolocation_regime_matrix.py"),
            "--help",
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "--positive-evidence" in result.stdout
    assert "--negative-no-coordinate-evidence" in result.stdout
