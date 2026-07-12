from __future__ import annotations

import json
from pathlib import Path

import pytest

from multidetect.config import CompletionPolicy, MissionConfig, PlatformMode
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
