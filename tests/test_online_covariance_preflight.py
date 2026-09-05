from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from multidetect import online_covariance_preflight as online
from multidetect.compat import UTC


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sources(tmp_path: Path) -> dict[str, Path]:
    result = {
        "config": tmp_path / "campaign.json",
        "mission": tmp_path / "mission.json",
        "feedback": tmp_path / "feedback.json",
        "profile": tmp_path / "profile.json",
    }
    for name, path in result.items():
        path.write_text(json.dumps({"source": name}), encoding="utf-8")
    return result


def _heartbeat() -> dict[str, object]:
    return {
        "source_system_id": 1,
        "source_component_id": 1,
        "autopilot_id": 12,
        "autopilot_name": "PX4",
        "vehicle_type_id": 1,
        "vehicle_type_name": "FIXED_WING",
        "system_status_id": 4,
        "system_status_name": "ACTIVE",
        "mavlink_version": 3,
    }


def _autopilot_version() -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_system_id": 1,
        "source_component_id": 1,
        "capabilities": 17,
        "flight_sw_version": 0x010203FF,
        "middleware_sw_version": 0,
        "os_sw_version": 0,
        "board_version": 1,
        "flight_custom_version_hex": "0102030405060708",
        "middleware_custom_version_hex": "0000000000000000",
        "os_custom_version_hex": "0000000000000000",
        "vendor_id": 9900,
        "product_id": 42,
        "device_uid_source": "none",
        "device_uid_hash": None,
        "capabilities_hex": "0x0000000000000011",
        "flight_sw_version_text": "1.2.3",
        "flight_sw_version_type": "255",
        "middleware_sw_version_text": "0.0.0",
        "middleware_sw_version_type": "0",
        "os_sw_version_text": "0.0.0",
        "os_sw_version_type": "0",
        "raw_device_uid_serialized": False,
        "identity_hash": "a" * 64,
    }


def _mode_binding(sources: dict[str, Path]) -> dict[str, object]:
    checks = {
        "feedback_schema_v2": True,
        "feedback_runtime_mode_exact": True,
        "feedback_gate_passed": True,
        "runtime_binding_gate_passed": True,
        "runtime_output_locked": True,
        "runtime_flight_control_disabled": True,
        "configured_endpoint_exact": True,
        "resolved_endpoint_exact": True,
        "baud_exact": True,
        "heartbeat_identity_exact": True,
        "current_firmware_identity_exact": True,
        "provider_read_only": True,
        "provider_hardware_control_disabled": True,
        "provider_transport_link_current": True,
        "provider_qualified_link_current": True,
        "provider_operational_qualification": True,
    }
    return {
        "schema_version": 1,
        "runtime_mode": "hil",
        "mission": {
            "path": str(sources["mission"]),
            "file_sha256": _sha(sources["mission"]),
            "mission_id": "field-fixed-wing",
            "mission_sha256": "b" * 64,
            "airframe_profile_hash": "c" * 64,
        },
        "mission_feedback": {
            "path": str(sources["feedback"]),
            "file_sha256": _sha(sources["feedback"]),
            "evidence_sha256": "d" * 64,
            "runtime_binding_sha256": "e" * 64,
        },
        "runtime_profile": {
            "path": str(sources["profile"]),
            "file_sha256": _sha(sources["profile"]),
            "profile_document_sha256": _sha(sources["profile"]),
            "profile_sha256": "f" * 64,
            "profile_id": "hil",
            "key_id": "field-key",
        },
        "current_heartbeat_identity": {
            "source_system_id": 1,
            "source_component_id": 1,
            "autopilot_id": 12,
            "vehicle_type_id": 1,
            "mavlink_version": 3,
        },
        "current_firmware_identity_hash": "a" * 64,
        "checks": checks,
        "gate_passed": True,
        "binding_sha256": "1" * 64,
    }


def _raw_probe(sources: dict[str, Path], mode_binding: dict[str, object]) -> dict[str, object]:
    heartbeat = _heartbeat()
    autopilot_version = _autopilot_version()
    return {
        "schema": "multidetect.pixhawk-covariance-online-preflight/v1",
        "schema_version": 1,
        "runtime_mode": "hil",
        "vehicle_class": "fixed-wing",
        "endpoint": "COM3",
        "baud": 57600,
        "probe": {
            "samples_requested": 100,
            "sample_count": 2,
            "interval_seconds": 0.1,
            "started_at_s": 10.0,
            "completed_at_s": 10.1,
            "duration_seconds": 0.1,
            "receive_errors": [],
        },
        "transport": {
            "configured_endpoint": "COM3",
            "resolved_endpoint": "COM3",
            "read_only": True,
            "hardware_control_enabled": False,
            "messages_received": 4,
            "messages_transmitted": 1,
            "transport_link_healthy": True,
            "qualified_link_healthy": True,
            "qualification": {"required": True, "passed": True, "reasons": []},
        },
        "identity_query": {
            "request_ranging_streams": False,
            "request_autopilot_version": True,
            "attempts": 1,
            "maximum_attempts": 3,
            "response_observed": True,
            "budget_valid": True,
        },
        "heartbeat_identity": heartbeat,
        "autopilot_version_identity": autopilot_version,
        "mode_binding": mode_binding,
        "source_preflight": {
            "schema": "multidetect.pixhawk-covariance-field-source-preflight/v1",
            "schema_version": 1,
            "release_label": "0.3.0-pre.8",
            "config_path": str(sources["config"]),
            "config_sha256": _sha(sources["config"]),
            "records": [{"status": "ready"}] * 6,
            "ready_count": 6,
            "blocked_count": 0,
            "gate_passed": True,
        },
        "reasons": [],
        "gate_passed": True,
        "claims": {
            "release_eligible": False,
            "hardware_acceptance": False,
            "hil_acceptance": False,
            "real_acceptance": False,
            "flight_acceptance": False,
        },
    }


def _source_preflight_summary(sources: dict[str, Path]) -> dict[str, object]:
    return {
        "schema": "multidetect.pixhawk-covariance-field-source-preflight/v1",
        "schema_version": 1,
        "release_label": "0.3.0-pre.8",
        "config_path": str(sources["config"]),
        "config_sha256": _sha(sources["config"]),
        "ready_count": 6,
        "blocked_count": 0,
        "gate_passed": True,
    }


def _observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, object], dict[str, Path]]:
    sources = _sources(tmp_path)
    mode_binding = _mode_binding(sources)
    monkeypatch.setattr(
        online,
        "build_pixhawk_covariance_mode_binding",
        lambda **kwargs: copy.deepcopy(mode_binding),
    )
    monkeypatch.setattr(
        online,
        "_verify_current_campaign",
        lambda *args, **kwargs: _source_preflight_summary(sources),
    )
    parity = json.loads(online.PARITY_PATH.read_text(encoding="utf-8"))
    return (
        online.build_online_covariance_preflight_observation(
            _raw_probe(sources, mode_binding),
            release_label=parity["release_label"],
            config_path=sources["config"],
            mission_path=sources["mission"],
            mission_feedback_path=sources["feedback"],
            runtime_profile_path=sources["profile"],
            observed_at=datetime(2026, 9, 6, 6, 0, tzinfo=UTC),
        ),
        sources,
    )


def test_schema_is_closed_and_resolves_covariance_contract() -> None:
    schema = json.loads(online.SCHEMA_PATH.read_text(encoding="utf-8"))
    covariance_schema = json.loads(online.COVARIANCE_SCHEMA_PATH.read_text(encoding="utf-8"))
    registry = Registry().with_resource(
        covariance_schema["$id"], Resource.from_contents(covariance_schema)
    )
    Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["source_binding"]["additionalProperties"] is False
    assert Draft202012Validator(schema, registry=registry).is_valid is not None


def test_build_write_load_and_freshness_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation, _ = _observation(tmp_path, monkeypatch)
    output = online.write_online_covariance_preflight_observation(
        tmp_path / "online-preflight.json", observation
    )

    loaded = online.load_online_covariance_preflight_observation(
        output,
        maximum_age_seconds=60.0,
        now=datetime(2026, 9, 6, 6, 0, 30, tzinfo=UTC),
    )

    assert loaded == observation
    assert loaded["schema"] == online.SCHEMA
    assert loaded["gate_passed"] is True
    assert "records" not in loaded["source_preflight"]
    assert loaded["source_binding"]["release_parity_hashes"]["px4_mission_path_hash"]


def test_loader_enforces_default_expiry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    observation, _ = _observation(tmp_path, monkeypatch)
    output = online.write_online_covariance_preflight_observation(
        tmp_path / "expired.json", observation
    )

    class ClockedDateTime(datetime):
        @staticmethod
        def now(tz: object) -> datetime:
            del tz
            return ClockedDateTime(2026, 9, 6, 6, 5, 1, tzinfo=UTC)

    monkeypatch.setattr(online, "datetime", ClockedDateTime)
    with pytest.raises(online.OnlineCovariancePreflightError, match="stale"):
        online.load_online_covariance_preflight_observation(output)


def test_verifier_rejects_tamper_unknown_fields_and_source_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation, sources = _observation(tmp_path, monkeypatch)

    digest_tamper = copy.deepcopy(observation)
    digest_tamper["evidence_sha256"] = "0" * 64
    with pytest.raises(online.OnlineCovariancePreflightError, match="evidence hash"):
        online.verify_online_covariance_preflight_observation(digest_tamper)

    unknown = copy.deepcopy(observation)
    unknown["trust_me"] = True
    with pytest.raises(online.OnlineCovariancePreflightError, match="schema validation"):
        online.verify_online_covariance_preflight_observation(unknown)

    sources["profile"].write_text('{"tampered":true}', encoding="utf-8")
    with pytest.raises(online.OnlineCovariancePreflightError, match="source binding"):
        online.verify_online_covariance_preflight_observation(observation)


def test_verifier_rejects_stale_and_future_observations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation, _ = _observation(tmp_path, monkeypatch)
    observed = datetime(2026, 9, 6, 6, 0, tzinfo=UTC)

    with pytest.raises(online.OnlineCovariancePreflightError, match="stale"):
        online.verify_online_covariance_preflight_observation(
            observation,
            maximum_age_seconds=30.0,
            now=observed + timedelta(seconds=31),
        )
    with pytest.raises(online.OnlineCovariancePreflightError, match="future-dated"):
        online.verify_online_covariance_preflight_observation(
            observation,
            maximum_age_seconds=30.0,
            now=observed - timedelta(seconds=1),
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda item: item["probe"].update(sample_count=101), "probe timing"),
        (lambda item: item["probe"].update(duration_seconds=0.2), "probe timing"),
        (
            lambda item: item["identity_query"].update(response_observed=False),
            "response claim",
        ),
    ],
)
def test_verifier_rejects_relational_claim_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: object,
    message: str,
) -> None:
    observation, _ = _observation(tmp_path, monkeypatch)
    tampered = copy.deepcopy(observation)
    mutate(tampered)  # type: ignore[operator]
    without_hash = dict(tampered)
    without_hash.pop("evidence_sha256")
    tampered["evidence_sha256"] = online._canonical_sha256(without_hash)

    with pytest.raises(online.OnlineCovariancePreflightError, match=message):
        online.verify_online_covariance_preflight_observation(tampered)


def test_verifier_rejects_current_campaign_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation, _ = _observation(tmp_path, monkeypatch)

    def reject_campaign(*args: object, **kwargs: object) -> dict[str, object]:
        raise online.OnlineCovariancePreflightError("current campaign changed")

    monkeypatch.setattr(online, "_verify_current_campaign", reject_campaign)
    with pytest.raises(online.OnlineCovariancePreflightError, match="current campaign changed"):
        online.verify_online_covariance_preflight_observation(observation)


def test_writer_and_loader_reject_symbolic_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation, _ = _observation(tmp_path, monkeypatch)
    link = tmp_path / "linked.json"
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda self: self == link or original_is_symlink(self),
    )

    with pytest.raises(online.OnlineCovariancePreflightError, match="symbolic link"):
        online.load_online_covariance_preflight_observation(link)
    with pytest.raises(online.OnlineCovariancePreflightError, match="symbolic link"):
        online.write_online_covariance_preflight_observation(link, observation)
