from __future__ import annotations

import json
from pathlib import Path

import pytest

from multidetect.config import CompletionPolicy, MissionConfig, PlatformMode, SafetyLimits
from multidetect.domain import ConfigurationError

ROOT = Path(__file__).resolve().parents[1]


def test_demo_config_loads() -> None:
    config = MissionConfig.from_json(ROOT / "configs/missions/fire_suppression.demo.json")

    assert config.mission_id == "fire-demo-001"
    assert config.platform_mode is PlatformMode.MULTI_DEPLOYMENT
    assert len(config.payloads) == 3
    assert config.human_authorization_required is True


def test_payload_count_must_match() -> None:
    raw = json.loads(
        (ROOT / "configs/missions/fire_suppression.demo.json").read_text(encoding="utf-8")
    )
    raw["payload_count"] = 99

    with pytest.raises(ConfigurationError, match="payload_count"):
        MissionConfig.from_mapping(raw)


def test_patrol_config_allows_no_payload() -> None:
    config = MissionConfig.from_json(ROOT / "configs/missions/fire_patrol.demo.json")

    assert config.payloads == ()
    assert config.payload_installed is False
    assert config.deployment_capable is False
    assert config.require_independent_rgb_corroboration is False
    assert config.require_thermal_corroboration is False


def test_payload_demo_uses_rgb_only_independent_corroboration_gate() -> None:
    config = MissionConfig.from_json(ROOT / "configs/missions/fire_suppression.demo.json")

    assert config.deployment_capable is True
    assert config.target_classes == ("flame",)
    assert config.require_independent_rgb_corroboration is True
    assert config.require_thermal_corroboration is False


def test_disposable_platform_still_requires_one_payload() -> None:
    raw = json.loads((ROOT / "configs/missions/fire_patrol.demo.json").read_text(encoding="utf-8"))
    raw["platform_mode"] = "disposable"
    raw["completion_policy"] = "terminate_after_first"

    with pytest.raises(ConfigurationError, match="exactly one payload"):
        MissionConfig.from_mapping(raw)


def test_safety_limits_reject_nonfinite_values() -> None:
    with pytest.raises(ConfigurationError, match="finite"):
        SafetyLimits(maximum_altitude_agl_m=float("nan"))


def test_offensive_payload_is_rejected() -> None:
    raw = json.loads(
        (ROOT / "configs/missions/fire_suppression.demo.json").read_text(encoding="utf-8")
    )
    raw["payloads"][0]["payload_type"] = "explosive_payload"

    with pytest.raises(ConfigurationError, match="prohibited payload"):
        MissionConfig.from_mapping(raw)


def test_human_authorization_cannot_be_disabled() -> None:
    raw = json.loads(
        (ROOT / "configs/missions/fire_suppression.demo.json").read_text(encoding="utf-8")
    )
    raw["human_authorization_required"] = False

    with pytest.raises(ConfigurationError, match="human_authorization_required"):
        MissionConfig.from_mapping(raw)


def test_disposable_platform_has_one_payload_and_terminates() -> None:
    raw = json.loads(
        (ROOT / "configs/missions/fire_suppression.demo.json").read_text(encoding="utf-8")
    )
    raw["platform_mode"] = "disposable"
    raw["payloads"] = raw["payloads"][:1]
    raw["payload_count"] = 1
    raw["completion_policy"] = "terminate_after_first"

    config = MissionConfig.from_mapping(raw)

    assert config.completion_policy is CompletionPolicy.TERMINATE_AFTER_FIRST


def test_disposable_demo_config_loads() -> None:
    config = MissionConfig.from_json(
        ROOT / "configs/missions/fire_suppression_disposable.demo.json"
    )

    assert config.platform_mode is PlatformMode.DISPOSABLE
    assert len(config.payloads) == 1
    assert config.completion_policy is CompletionPolicy.TERMINATE_AFTER_FIRST


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("human_authorization_required", "false"),
        ("person_exclusion_enabled", "false"),
        ("require_independent_rgb_corroboration", 1),
        ("require_thermal_corroboration", 1),
    ],
)
def test_boolean_configuration_values_are_strict(field_name: str, invalid_value: object) -> None:
    raw = json.loads((ROOT / "configs/missions/fire_patrol.demo.json").read_text())
    raw[field_name] = invalid_value

    with pytest.raises(ConfigurationError, match=field_name):
        MissionConfig.from_mapping(raw)


def test_target_classes_must_be_an_array_not_a_string() -> None:
    raw = json.loads((ROOT / "configs/missions/fire_patrol.demo.json").read_text())
    raw["target_classes"] = "flame"

    with pytest.raises(ConfigurationError, match="target_classes must be an array"):
        MissionConfig.from_mapping(raw)


def test_fractional_track_observation_count_is_rejected() -> None:
    raw = json.loads((ROOT / "configs/missions/fire_patrol.demo.json").read_text())
    raw["minimum_track_observations"] = 4.9

    with pytest.raises(ConfigurationError, match="minimum_track_observations must be an integer"):
        MissionConfig.from_mapping(raw)


def test_schema_required_and_unknown_keys_are_enforced() -> None:
    raw = json.loads((ROOT / "configs/missions/fire_patrol.demo.json").read_text())
    del raw["platform_mode"]

    with pytest.raises(
        ConfigurationError, match="missing required configuration key: platform_mode"
    ):
        MissionConfig.from_mapping(raw)

    raw["platform_mode"] = "multi_deployment"
    raw["unexpected"] = True
    with pytest.raises(ConfigurationError, match="unknown configuration key: unexpected"):
        MissionConfig.from_mapping(raw)


def test_person_exclusion_requires_at_least_one_person_label() -> None:
    raw = json.loads((ROOT / "configs/missions/fire_patrol.demo.json").read_text())
    raw["person_labels"] = []

    with pytest.raises(ConfigurationError, match="person_labels cannot be empty"):
        MissionConfig.from_mapping(raw)


def test_invalid_json_is_reported_as_configuration_error(tmp_path: Path) -> None:
    config_path = tmp_path / "invalid.json"
    config_path.write_text('{"mission_id":', encoding="utf-8")

    with pytest.raises(ConfigurationError, match="invalid mission configuration JSON"):
        MissionConfig.from_json(config_path)
