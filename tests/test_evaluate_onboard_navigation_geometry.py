from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.autonomy import OnboardFlightAction
from scripts import evaluate_onboard_navigation_geometry as geometry


def _evaluate_exact_vector(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    prediction_horizon_m: float | None,
    raw_head_semantics: str = geometry.DEFAULT_RAW_HEAD_SEMANTICS,
    bearing_error_deg: float = 0.0,
) -> dict:
    class Runner:
        def infer(self, model_input):
            event = model_input.event
            bearing = math.radians(event.target_bearing_deg + bearing_error_deg)
            distance = event.target_distance_m
            if prediction_horizon_m is not None:
                distance = min(distance, prediction_horizon_m)
            return SimpleNamespace(
                flight_plan=SimpleNamespace(
                    north_offset_m=math.cos(bearing) * distance,
                    east_offset_m=math.sin(bearing) * distance,
                    action=OnboardFlightAction.FOLLOW_TARGET,
                )
            )

    monkeypatch.setattr(
        geometry.OnboardOnnxModelRunner,
        "from_path",
        staticmethod(lambda *_args, **_kwargs: Runner()),
    )
    model_path = tmp_path / "diagnostic-model.onnx"
    model_path.write_bytes(b"fixture model identity")
    return geometry.evaluate(
        model_path,
        count=80,
        seed=20261007,
        step_horizon_m=40.0,
        raw_head_semantics=raw_head_semantics,
    )


def test_full_target_is_default_and_exact_full_vector_passes(monkeypatch, tmp_path):
    report = _evaluate_exact_vector(monkeypatch, tmp_path, prediction_horizon_m=None)
    assert report["sampling"]["raw_head_semantics"] == "full_observed_target_offset"
    assert report["thresholds"]["raw_head_semantics"] == "full_observed_target_offset"
    assert report["thresholds"]["diagnostic_pass"] is True
    assert report["metrics"]["range_mae_m"] == pytest.approx(0.0, abs=1e-9)
    assert report["metrics"]["endpoint_mae_m"] == pytest.approx(0.0, abs=1e-9)
    assert report["metrics"]["bounded_step_reference"]["range_mae_m"] > 30.0
    assert report["metrics"]["direction_within_20deg_rate"] == 1.0


def test_exact_full_vector_fails_explicit_bounded_step_semantics(monkeypatch, tmp_path):
    report = _evaluate_exact_vector(
        monkeypatch,
        tmp_path,
        prediction_horizon_m=None,
        raw_head_semantics="bounded_next_step",
    )
    assert report["thresholds"]["diagnostic_pass"] is False
    assert report["metrics"]["range_mae_m"] > 30.0
    assert report["metrics"]["full_target_reference"]["range_mae_m"] == pytest.approx(0.0, abs=1e-9)


def test_exact_bounded_step_passes_only_explicit_bounded_semantics(monkeypatch, tmp_path):
    full = _evaluate_exact_vector(monkeypatch, tmp_path, prediction_horizon_m=40.0)
    bounded = _evaluate_exact_vector(
        monkeypatch,
        tmp_path,
        prediction_horizon_m=40.0,
        raw_head_semantics="bounded_next_step",
    )
    assert full["thresholds"]["diagnostic_pass"] is False
    assert full["metrics"]["range_mae_m"] > 30.0
    assert bounded["thresholds"]["diagnostic_pass"] is True
    assert bounded["metrics"]["range_mae_m"] == pytest.approx(0.0, abs=1e-9)
    assert bounded["metrics"]["endpoint_mae_m"] == pytest.approx(0.0, abs=1e-9)
    assert bounded["metrics"]["full_target_reference"]["range_mae_m"] > 30.0


@pytest.mark.parametrize("semantics", geometry.RAW_HEAD_SEMANTICS)
def test_neither_semantics_hides_direction_error(monkeypatch, tmp_path, semantics):
    report = _evaluate_exact_vector(
        monkeypatch,
        tmp_path,
        prediction_horizon_m=None if semantics == "full_observed_target_offset" else 40.0,
        raw_head_semantics=semantics,
        bearing_error_deg=90.0,
    )
    assert report["metrics"]["range_mae_m"] == pytest.approx(0.0, abs=1e-9)
    assert report["metrics"]["angle_p95_deg"] == pytest.approx(90.0, abs=1e-9)
    assert report["metrics"]["direction_within_20deg_rate"] == 0.0
    assert report["thresholds"]["diagnostic_pass"] is False
    assert report["thresholds"]["angle_p95_deg"] == 20.0
    assert report["thresholds"]["range_mae_m"] == 30.0


def test_invalid_semantics_rejected_before_loading_model(monkeypatch, tmp_path):
    def fail_load(*_args, **_kwargs):
        pytest.fail("invalid semantics must not load the model")

    monkeypatch.setattr(geometry.OnboardOnnxModelRunner, "from_path", staticmethod(fail_load))
    with pytest.raises(ValueError, match="raw_head_semantics must be one of"):
        geometry.evaluate(
            tmp_path / "absent.onnx", count=80, seed=20261007, raw_head_semantics="unknown"
        )


def test_cli_rejects_invalid_semantics(tmp_path):
    with pytest.raises(SystemExit) as result:
        geometry.main(
            ["--raw-head-semantics", "unknown", "--output", str(tmp_path / "report.json")]
        )
    assert result.value.code == 2
    assert not (tmp_path / "report.json").exists()
