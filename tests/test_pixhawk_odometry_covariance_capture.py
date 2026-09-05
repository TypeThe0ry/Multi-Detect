from __future__ import annotations

from copy import deepcopy

import pytest

from multidetect.autonomy.serialization import canonical_sha256
from scripts import capture_pixhawk_odometry_covariance as module


def _diagnostics() -> dict[str, object]:
    covariance = [0.0001, 0.0, 0.0, 0.0, 0.0004, 0.0, 0.0, 0.0, 0.0009]
    return {
        "message_type_counts": {"ODOMETRY": 30, "ATTITUDE": 40},
        "messages_transmitted": 14,
        "hardware_control_enabled": False,
        "ranging_stream_requests": {
            "attempts": 2,
            "requested_message_intervals_us": {
                "ATTITUDE": 50_000,
                "ODOMETRY": 50_000,
                "SCALED_PRESSURE": 100_000,
                "LOCAL_POSITION_NED": 66_667,
                "GLOBAL_POSITION_INT": 100_000,
                "ALTITUDE": 100_000,
                "HOME_POSITION": 1_000_000,
            },
        },
        "attitude_uncertainty": {
            "source": module.UNCERTAINTY_SOURCE,
            "roll_sigma_deg": 0.5729577951,
            "pitch_sigma_deg": 1.1459155903,
            "heading_sigma_deg": 1.7188733854,
            "observed_at_s": 100.0,
            "age_s": 0.01,
            "fresh": True,
            "associated_with_attitude": True,
            "raw_angular_covariance_rad2": covariance,
            "configured_floor_deg": {"roll": 0.3, "pitch": 0.3, "heading": 1.0},
            "reset_counter": 2,
            "child_frame_id": 1,
            "accepted_count": 25,
            "rejections": {},
        },
    }


def _report(diagnostics: dict[str, object]) -> dict[str, object]:
    record = module._record_from_diagnostics(diagnostics, sample_index=4)
    assert record is not None
    return module.build_observation_report(
        runtime_mode="sim",
        endpoint="udpin:0.0.0.0:14801",
        started_at_utc="2026-09-06T00:00:00Z",
        finished_at_utc="2026-09-06T00:00:10Z",
        duration_s=10.0,
        samples_requested=100,
        records=[record],
        diagnostics=diagnostics,
        minimum_odometry_messages=20,
        minimum_accepted_covariances=10,
    )


def test_observation_report_binds_nonzero_covariance_and_telemetry_only_requests() -> None:
    report = _report(_diagnostics())

    assert report["gate_passed"] is True
    assert all(report["checks"].values())
    assert report["claims"] == {
        "estimator_attitude_covariance_observed": True,
        "sim_acceptance": "passed",
        "hil_acceptance": "not_claimed",
        "real_acceptance": "not_claimed",
        "hardware_acceptance": "not_claimed",
        "flight_acceptance": "not_claimed",
    }
    without_hash = {key: value for key, value in report.items() if key != "evidence_sha256"}
    assert report["evidence_sha256"] == canonical_sha256(without_hash)


def test_observation_report_rejects_zero_attitude_variance() -> None:
    diagnostics = _diagnostics()
    tampered = deepcopy(diagnostics)
    tampered["attitude_uncertainty"]["raw_angular_covariance_rad2"][4] = 0.0

    report = _report(tampered)

    assert report["gate_passed"] is False
    assert report["checks"]["captured_records_valid"] is False
    assert report["claims"]["estimator_attitude_covariance_observed"] is False
    assert report["claims"]["sim_acceptance"] == "not_claimed"


def test_observation_report_verifier_rebuilds_every_derived_field() -> None:
    report = _report(_diagnostics())

    verification = module.verify_observation_report(report)

    assert verification == {
        "schema": module.SCHEMA,
        "verified": True,
        "gate_passed": True,
        "runtime_mode": "sim",
        "records_verified": 1,
        "evidence_sha256": report["evidence_sha256"],
    }

    tampered = deepcopy(report)
    tampered["gate_passed"] = False
    tampered["evidence_sha256"] = canonical_sha256(
        {key: value for key, value in tampered.items() if key != "evidence_sha256"}
    )
    with pytest.raises(
        module.ObservationEvidenceError,
        match="does not reproduce.*gate_passed",
    ):
        module.verify_observation_report(tampered)


def test_observation_report_verifier_rejects_digest_tampering() -> None:
    report = _report(_diagnostics())
    report["accepted_covariances_observed"] = 99

    with pytest.raises(module.ObservationEvidenceError, match="evidence_sha256 mismatch"):
        module.verify_observation_report(report)
