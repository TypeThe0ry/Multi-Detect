from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from multidetect.autonomy.serialization import canonical_sha256
from scripts import capture_pixhawk_odometry_covariance as module


def _diagnostics() -> dict[str, object]:
    covariance = [0.0001, 0.0, 0.0, 0.0, 0.0004, 0.0, 0.0, 0.0, 0.0009]
    return {
        "message_type_counts": {"ODOMETRY": 30, "ATTITUDE": 40},
        "messages_transmitted": 15,
        "configured_endpoint": "COM7",
        "resolved_endpoint": "COM7",
        "read_only": True,
        "hardware_control_enabled": False,
        "transport_link_healthy": True,
        "qualified_link_healthy": True,
        "qualification": {"required": True, "passed": True, "reasons": []},
        "heartbeat_identity": {
            "source_system_id": 1,
            "source_component_id": 1,
            "autopilot_id": 12,
            "vehicle_type_id": 2,
            "mavlink_version": 3,
        },
        "autopilot_version_identity": {"identity_hash": "a" * 64},
        "autopilot_version_observed_at_s": 99.5,
        "autopilot_version_age_s": 0.5,
        "autopilot_version_requests": {
            "enabled": True,
            "attempts": 1,
            "last_request_age_s": 0.5,
            "command_id": 512,
            "message_id": 148,
            "response_observed": True,
        },
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
        "hil_covariance_observation": "not_claimed",
        "real_covariance_observation": "not_claimed",
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


def test_observation_report_fails_closed_without_requested_firmware_identity() -> None:
    diagnostics = _diagnostics()
    diagnostics["autopilot_version_identity"] = None
    diagnostics["autopilot_version_requests"] = {
        "enabled": True,
        "attempts": 3,
        "last_request_age_s": 0.5,
        "command_id": 512,
        "message_id": 148,
        "response_observed": False,
    }
    diagnostics["messages_transmitted"] = 17

    report = _report(diagnostics)

    assert report["checks"]["telemetry_requests_only"] is True
    assert report["checks"]["autopilot_version_identity_observed"] is False
    assert report["gate_passed"] is False
    assert report["claims"]["sim_acceptance"] == "not_claimed"


@pytest.mark.parametrize(
    ("field", "value", "failed_check"),
    [
        ("transport_link_healthy", False, "transport_link_current"),
        ("qualified_link_healthy", False, "qualified_link_current"),
        ("autopilot_version_age_s", 12.0, "autopilot_version_current_session"),
    ],
)
def test_observation_report_rejects_stale_or_unqualified_current_session(
    field: str,
    value: object,
    failed_check: str,
) -> None:
    diagnostics = _diagnostics()
    diagnostics[field] = value

    report = _report(diagnostics)

    assert report["checks"][failed_check] is False
    assert report["gate_passed"] is False
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


def test_observation_schema_is_draft_2020_closed_and_accepts_report() -> None:
    schema = json.loads(module.SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    report = _report(_diagnostics())

    assert schema["additionalProperties"] is False
    assert schema["properties"]["final_diagnostics"]["$ref"] == "#/$defs/diagnostics"
    assert schema["$defs"]["diagnostics"]["additionalProperties"] is False
    assert schema["$defs"]["attitude_uncertainty"]["additionalProperties"] is False
    assert schema["$defs"]["mode_binding"]["additionalProperties"] is False
    assert list(Draft202012Validator(schema).iter_errors(report)) == []


def test_observation_report_stores_only_closed_evidence_diagnostics() -> None:
    diagnostics = _diagnostics()
    diagnostics["motion"] = {"armed": True}
    diagnostics["unrelated_internal_counter"] = 99
    diagnostics["attitude_uncertainty"]["unrelated_internal_field"] = "discarded"
    diagnostics["qualification"]["reasons"] = ()

    report = _report(diagnostics)

    assert "motion" not in report["final_diagnostics"]
    assert "unrelated_internal_counter" not in report["final_diagnostics"]
    assert "unrelated_internal_field" not in report["final_diagnostics"]["attitude_uncertainty"]
    assert report["final_diagnostics"]["qualification"]["reasons"] == []


def test_failed_observation_without_covariance_remains_schema_valid() -> None:
    diagnostics = _diagnostics()
    diagnostics["attitude_uncertainty"] = None

    report = module.build_observation_report(
        runtime_mode="sim",
        endpoint="udpin:0.0.0.0:14801",
        started_at_utc="2026-09-06T00:00:00Z",
        finished_at_utc="2026-09-06T00:00:10Z",
        duration_s=10.0,
        samples_requested=100,
        records=[],
        diagnostics=diagnostics,
        minimum_odometry_messages=20,
        minimum_accepted_covariances=10,
    )

    assert report["gate_passed"] is False
    assert report["checks"]["captured_records_valid"] is False
    assert report["final_diagnostics"]["attitude_uncertainty"]["raw_angular_covariance_rad2"] == []
    assert module.verify_observation_report(report)["gate_passed"] is False


@pytest.mark.parametrize(
    ("mutate", "location"),
    [
        (lambda report: report.update({"unexpected": True}), "<root>"),
        (lambda report: report["final_diagnostics"].update({"unexpected": 1}), "final_diagnostics"),
        (lambda report: report["checks"].pop("transport_link_current"), "checks"),
        (lambda report: report.update({"duration_s": "ten"}), "duration_s"),
    ],
)
def test_observation_verifier_rejects_schema_drift_before_digest_rebuild(
    mutate: object,
    location: str,
) -> None:
    report = _report(_diagnostics())
    mutate(report)  # type: ignore[operator]
    report["evidence_sha256"] = canonical_sha256(
        {key: value for key, value in report.items() if key != "evidence_sha256"}
    )

    with pytest.raises(module.ObservationEvidenceError, match=location):
        module.verify_observation_report(report)


def _install_mode_binding_fixtures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    runtime_mode: str = "hil",
) -> tuple[Path, Path, Path]:
    mission_path = tmp_path / "mission.json"
    feedback_path = tmp_path / "feedback.json"
    profile_path = tmp_path / "profile.json"
    mission_path.write_text("{}\n", encoding="utf-8")
    feedback_path.write_text("{}\n", encoding="utf-8")
    profile_path.write_text("{}\n", encoding="utf-8")
    mission = SimpleNamespace(
        mission_id="mission-v83",
        canonical_hash="b" * 64,
        airframe_profile_hash="c" * 64,
    )
    feedback = {
        "schema_version": 2,
        "runtime_mode": runtime_mode,
        "feedback_gate_passed": True,
        "transport": {
            "configured_endpoint": "COM7",
            "resolved_endpoint": "COM7",
            "baud": 57_600,
        },
        "runtime_binding": {
            "gate_passed": True,
            "output_locked": True,
            "flight_control_enabled": False,
            "heartbeat_identity": {
                "source_system_id": 1,
                "source_component_id": 1,
                "autopilot_id": 12,
                "vehicle_type_id": 2,
                "mavlink_version": 3,
            },
            "observed_identity": {"firmware_identity_hash": "a" * 64},
            "binding_sha256": "d" * 64,
            "profile_document_sha256": "e" * 64,
            "profile_sha256": "f" * 64,
            "profile": {"profile_id": f"{runtime_mode}-profile"},
            "key_id": "operator-key",
        },
        "evidence_sha256": "1" * 64,
    }
    monkeypatch.setattr(module, "load_executable_mission_document", lambda path: mission)

    def _load_feedback(path: Path, *, expected_mission: object, runtime_profile_path: Path):
        assert path == feedback_path.resolve()
        assert expected_mission is mission
        assert runtime_profile_path == profile_path.resolve()
        return feedback

    monkeypatch.setattr(module, "load_px4_mission_feedback_observation", _load_feedback)
    return mission_path, feedback_path, profile_path


def _hil_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    diagnostics: dict[str, object] | None = None,
) -> dict[str, object]:
    mission_path, feedback_path, profile_path = _install_mode_binding_fixtures(
        monkeypatch, tmp_path
    )
    observed = _diagnostics() if diagnostics is None else diagnostics
    binding = module.build_mode_binding(
        runtime_mode="hil",
        endpoint="COM7",
        baud=57_600,
        diagnostics=observed,
        mission_path=mission_path,
        mission_feedback_path=feedback_path,
        runtime_profile_path=profile_path,
    )
    record = module._record_from_diagnostics(observed, sample_index=4)
    assert record is not None
    return module.build_observation_report(
        runtime_mode="hil",
        endpoint="COM7",
        baud=57_600,
        started_at_utc="2026-09-06T00:00:00Z",
        finished_at_utc="2026-09-06T00:00:10Z",
        duration_s=10.0,
        samples_requested=100,
        records=[record],
        diagnostics=observed,
        minimum_odometry_messages=20,
        minimum_accepted_covariances=10,
        mode_binding=binding,
    )


def test_hil_observation_requires_exact_runtime_identity_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    report = _hil_report(monkeypatch, tmp_path)

    assert report["gate_passed"] is True
    assert report["claims"]["hil_covariance_observation"] == "passed"
    assert report["claims"]["hil_acceptance"] == "not_claimed"
    assert module.verify_observation_report(report)["gate_passed"] is True


def test_hil_observation_fails_closed_on_current_firmware_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    diagnostics = _diagnostics()
    diagnostics["autopilot_version_identity"] = {"identity_hash": "b" * 64}

    report = _hil_report(monkeypatch, tmp_path, diagnostics=diagnostics)

    assert report["gate_passed"] is False
    assert report["mode_binding"]["checks"]["current_firmware_identity_exact"] is False
    assert report["claims"]["hil_covariance_observation"] == "not_claimed"


@pytest.mark.parametrize(
    ("field", "value", "binding_check"),
    [
        ("transport_link_healthy", False, "provider_transport_link_current"),
        ("qualified_link_healthy", False, "provider_qualified_link_current"),
        (
            "qualification",
            {"required": True, "passed": False, "reasons": ["heartbeat_stale"]},
            "provider_operational_qualification",
        ),
    ],
)
def test_hil_mode_binding_rejects_noncurrent_provider_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    value: object,
    binding_check: str,
) -> None:
    diagnostics = _diagnostics()
    diagnostics[field] = value

    report = _hil_report(monkeypatch, tmp_path, diagnostics=diagnostics)

    assert report["mode_binding"]["checks"][binding_check] is False
    assert report["mode_binding"]["gate_passed"] is False
    assert report["gate_passed"] is False
    assert report["claims"]["hil_covariance_observation"] == "not_claimed"


@pytest.mark.parametrize(
    "runtime_mode,endpoint,expected",
    [
        ("sim", "udpin:127.0.0.1:14801", True),
        ("sim", "COM7", False),
        ("hil", "COM7", True),
        ("real", "udpout:192.0.2.10:14551", True),
        ("hil", "udpin:0.0.0.0:14550", False),
        ("real", "udp:127.0.0.1:14550", False),
        ("real", "udpout:192.0.2.10:14550", False),
    ],
)
def test_endpoint_policy(runtime_mode: str, endpoint: str, expected: bool) -> None:
    assert module._endpoint_valid(runtime_mode, endpoint) is expected


def test_hil_cli_requires_all_identity_sources_before_opening_provider(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="HIL/REAL capture requires"):
        module.main(
            [
                "--runtime-mode",
                "hil",
                "--endpoint",
                "COM7",
                "--out",
                str(tmp_path / "observation.json"),
            ]
        )


def test_verify_cli_rejects_capture_identity_arguments(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot be combined"):
        module.main(
            [
                "--verify",
                str(tmp_path / "observation.json"),
                "--mission",
                str(tmp_path / "mission.json"),
            ]
        )
