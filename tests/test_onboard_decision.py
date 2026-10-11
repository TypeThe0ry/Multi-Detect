"""CPU validation for non-executing onboard slow decisions."""

from __future__ import annotations

import json

import pytest

from multidetect.autonomy.onboard_decision import DECISIONS, parse_onboard_decision
from multidetect.autonomy.serialization import AutonomyContractError


def _decision(name: str = "confirm_target", **args: object) -> dict[str, object]:
    return {
        "decision": name,
        "args": {"track_id": "track-7", "region": None, "duration_s": 2.0, **args},
        "reason": "Visual class and colour match",
    }


@pytest.mark.parametrize("name", DECISIONS)
def test_valid_decisions(name: str) -> None:
    payload = _decision(name, region="north-east")
    parsed = parse_onboard_decision(json.dumps(payload))
    assert parsed.decision == name
    assert parsed.args.track_id == "track-7"


@pytest.mark.parametrize(
    "payload",
    [
        {**_decision(), "motor_pwm": 1500},
        {**_decision(), "decision": "set_motor"},
        {**_decision(), "args": {**_decision()["args"], "mavlink": "LAND"}},
        _decision("confirm_target", track_id=None),
        _decision("search_region", region=None),
        _decision(duration_s=-1),
        _decision(duration_s=31),
        _decision(duration_s=float("nan")),
        _decision(duration_s=True),
        {**_decision(), "reason": "one " * 21},
        {**_decision(), "reason": ""},
    ],
)
def test_rejects_unsafe_or_malformed_payload(payload: dict[str, object]) -> None:
    with pytest.raises(AutonomyContractError):
        parse_onboard_decision(payload)


def test_rejects_duplicate_json_keys() -> None:
    raw = json.dumps(_decision())
    with pytest.raises(AutonomyContractError, match="duplicate"):
        parse_onboard_decision(raw.replace('"decision":', '"decision": "hover", "decision":', 1))


def test_rejects_unknown_arg() -> None:
    payload = _decision()
    payload["args"]["x"] = 1  # type: ignore[index]
    with pytest.raises(AutonomyContractError, match="unknown"):
        parse_onboard_decision(payload)
