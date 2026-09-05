from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from multidetect import field_covariance_campaign as campaign


def _write_config(tmp_path: Path) -> Path:
    entries: list[dict[str, object]] = []
    for index, (mode, vehicle) in enumerate(campaign.REQUIRED_COMBINATIONS):
        source_dir = tmp_path / "sources" / mode / vehicle
        source_dir.mkdir(parents=True, exist_ok=True)
        mission = source_dir / "mission.json"
        feedback = source_dir / "feedback.json"
        profile = source_dir / "profile.json"
        for path in (mission, feedback, profile):
            path.write_text("{}", encoding="utf-8")
        entries.append(
            {
                "runtime_mode": mode,
                "vehicle_class": vehicle,
                "endpoint": f"serial:COM{index + 3}",
                "baud": 57600,
                "mission_path": str(mission.relative_to(tmp_path)),
                "mission_feedback_path": str(feedback.relative_to(tmp_path)),
                "runtime_profile_path": str(profile.relative_to(tmp_path)),
                "report_path": f"reports/{mode}/{vehicle}.json",
            }
        )
    path = tmp_path / "campaign.json"
    path.write_text(
        json.dumps(
            {
                "schema": campaign.SCHEMA,
                "schema_version": campaign.SCHEMA_VERSION,
                "release_label": "0.3.0-pre.8",
                "entries": entries,
            }
        ),
        encoding="utf-8",
    )
    return path


def _verified_record(
    mode: str,
    vehicle: str,
    report_path: Path,
    *,
    manifest_directory: Path,
) -> dict[str, object]:
    del mode, vehicle, manifest_directory
    return {
        "report_sha256": "a" * 64,
        "report_bytes": report_path.stat().st_size,
        "evidence_sha256": "b" * 64,
        "records_verified": 12,
        "firmware_identity_hash": "c" * 64,
    }


def test_campaign_schema_is_valid_and_closed() -> None:
    schema = json.loads(campaign.CONFIG_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["entry"]["additionalProperties"] is False
    example = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "configs"
            / "pixhawk-covariance-field-campaign.example.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(example)


def test_status_orders_exact_matrix_and_selects_one_next_leaf(tmp_path: Path) -> None:
    config = campaign.load_campaign_config(_write_config(tmp_path))

    status = campaign.campaign_status(config)

    assert [leaf.key for leaf in config.leaves] == list(campaign.REQUIRED_COMBINATIONS)
    assert status["passed_count"] == 0
    assert status["pending_count"] == 6
    assert status["failed_count"] == 0
    assert status["next_leaf"]["runtime_mode"] == "hil"
    assert status["next_leaf"]["vehicle_class"] == "fixed-wing"
    assert status["e7_arguments"] == []


def test_capture_next_dry_run_never_opens_transport_or_writes_report(tmp_path: Path) -> None:
    config = campaign.load_campaign_config(_write_config(tmp_path))

    exit_code, status = campaign.capture_next(
        config,
        samples=30,
        interval_seconds=0.2,
        minimum_odometry_messages=10,
        minimum_accepted_covariances=5,
        dry_run=True,
    )

    assert exit_code == 0
    assert status["dry_run"] is True
    assert status["capture_command"][0] == campaign.sys.executable
    assert "--runtime-mode" in status["capture_command"]
    assert "hil" in status["capture_command"]
    assert "--vehicle-class" not in status["capture_command"]
    assert not config.leaves[0].report_path.exists()


def test_complete_status_emits_all_six_e7_argument_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = campaign.load_campaign_config(_write_config(tmp_path))
    for leaf in config.leaves:
        leaf.report_path.parent.mkdir(parents=True, exist_ok=True)
        leaf.report_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(campaign, "verify_pixhawk_covariance_leaf", _verified_record)

    status = campaign.campaign_status(config)

    assert status["complete"] is True
    assert status["passed_count"] == 6
    assert status["pending_count"] == 0
    assert status["failed_count"] == 0
    assert status["e7_arguments"].count("--pixhawk-covariance-evidence") == 6


def test_existing_failed_report_blocks_capture_instead_of_overwriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = campaign.load_campaign_config(_write_config(tmp_path))
    first = config.leaves[0].report_path
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_text("{}", encoding="utf-8")

    def reject(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise ValueError("source binding drift")

    monkeypatch.setattr(campaign, "verify_pixhawk_covariance_leaf", reject)
    with pytest.raises(campaign.FieldCovarianceCampaignError, match="immutable report"):
        campaign.capture_next(
            config,
            samples=30,
            interval_seconds=0.2,
            minimum_odometry_messages=10,
            minimum_accepted_covariances=5,
            dry_run=False,
        )
    assert first.read_text(encoding="utf-8") == "{}"


def test_zero_capture_process_exit_still_fails_when_report_verification_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = campaign.load_campaign_config(_write_config(tmp_path))
    first = config.leaves[0].report_path

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    def reject(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise ValueError("schema mismatch")

    monkeypatch.setattr(campaign.subprocess, "run", run)
    monkeypatch.setattr(campaign, "verify_pixhawk_covariance_leaf", reject)
    exit_code, status = campaign.capture_next(
        config,
        samples=30,
        interval_seconds=0.2,
        minimum_odometry_messages=10,
        minimum_accepted_covariances=5,
        dry_run=False,
    )

    assert exit_code == 1
    assert status["capture_process_exit_code"] == 0
    assert status["capture_verified"] is False
    assert status["failed_count"] == 1


def test_config_rejects_duplicate_leaf_even_with_six_entries(tmp_path: Path) -> None:
    path = _write_config(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["entries"][-1]["runtime_mode"] = document["entries"][0]["runtime_mode"]
    document["entries"][-1]["vehicle_class"] = document["entries"][0]["vehicle_class"]
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(campaign.FieldCovarianceCampaignError, match="duplicate"):
        campaign.load_campaign_config(path)


def test_config_schema_rejects_unknown_entry_field(tmp_path: Path) -> None:
    path = _write_config(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["entries"][0]["claim"] = "trust-me"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(campaign.FieldCovarianceCampaignError, match="schema validation"):
        campaign.load_campaign_config(path)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"samples": 0}, "samples"),
        ({"interval_seconds": float("nan")}, "interval_seconds"),
        ({"minimum_odometry_messages": -1}, "minimum_odometry_messages"),
        ({"minimum_accepted_covariances": 0}, "minimum_accepted_covariances"),
    ],
)
def test_capture_next_rejects_invalid_budget_before_transport(
    tmp_path: Path,
    overrides: dict[str, object],
    message: str,
) -> None:
    config = campaign.load_campaign_config(_write_config(tmp_path))
    values: dict[str, object] = {
        "samples": 30,
        "interval_seconds": 0.2,
        "minimum_odometry_messages": 10,
        "minimum_accepted_covariances": 5,
        "dry_run": False,
    }
    values.update(overrides)

    with pytest.raises(campaign.FieldCovarianceCampaignError, match=message):
        campaign.capture_next(config, **values)
