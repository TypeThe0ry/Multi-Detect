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


def test_host_size_gate_keeps_identity_and_picks_confirm_or_approach() -> None:
    from multidetect.autonomy.onboard_decision import (
        gate_confirm_by_size,
        identity_of,
        parse_onboard_decision,
    )

    def make(kind: str):
        return parse_onboard_decision(
            {"decision": kind, "args": {"track_id": "track-1", "region": None, "duration_s": None}, "reason": "Matches task."}
        )

    small = gate_confirm_by_size(make("confirm_target"), candidate_min_side_px=9, confirm_min_side_px=20)
    large = gate_confirm_by_size(make("approach_track"), candidate_min_side_px=31, confirm_min_side_px=20)
    reject = gate_confirm_by_size(make("reject_track"), candidate_min_side_px=50, confirm_min_side_px=20)
    assert (small.decision, large.decision, reject.decision) == ("approach_track", "confirm_target", "reject_track")
    assert identity_of(small.decision) == identity_of(large.decision) == "match"
    assert identity_of("reject_track") == "no_match" and identity_of("search_region") is None


def test_identity_schema_only_allows_identity_verdicts_and_keeps_the_rest() -> None:
    from multidetect.autonomy.onboard_decision import (
        DECISION_JSON_SCHEMA,
        IDENTITY_DECISIONS,
        identity_decision_schema,
    )

    schema = identity_decision_schema()
    assert schema["properties"]["decision"]["enum"] == list(IDENTITY_DECISIONS)
    assert "ask_clarify" not in schema["properties"]["decision"]["enum"]
    assert schema["properties"]["args"] == DECISION_JSON_SCHEMA["properties"]["args"]
    # The shared schema is not mutated.
    assert "ask_clarify" in DECISION_JSON_SCHEMA["properties"]["decision"]["enum"]


def test_identity_schema_can_pin_the_focus_track() -> None:
    from multidetect.autonomy.onboard_decision import identity_decision_schema

    args = identity_decision_schema("track-1")["properties"]["args"]["properties"]
    assert args["track_id"] == {"type": "string", "enum": ["track-1"]}
    assert args["region"] == {"type": "null"} and args["duration_s"] == {"type": "null"}
