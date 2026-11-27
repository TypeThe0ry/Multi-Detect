import json
from collections import defaultdict
from copy import deepcopy

import pytest
from multidetect.autonomy.x1_perception_tools import TOOL_SCHEMA

from multidetect.autonomy.serialization import canonical_json, canonical_sha256
from scripts.build_x1_flight_loop_sft import (
    ADMITTED,
    ROOT,
    build,
    generate,
    policy,
)
from scripts.x1_flight_loop_eval import evaluate, read_jsonl, score_turn


@pytest.fixture(scope="module")
def reference_root():
    staged = ROOT / ".tmp/pristine"
    return (
        staged
        if (staged / "configs/simulation/x1-evidence-follow-faults-v1.json").is_file()
        else ROOT
    )


@pytest.fixture(scope="module")
def windows(reference_root):
    return {
        name: (window, receipt, lowered)
        for name, window, receipt, lowered in generate("train", reference_root, replicas=1)
    }


def test_oracle_final_actions_and_real_envelope(windows):
    expected = {
        "quiet_climb": "continue",
        "quiet_cruise": "continue",
        "quiet_transition": "continue",
        "quiet_approach": "continue",
        "target_event": "continue",
        "target_lost": "continue",
        "target_found": "retask",
        "target_moved": "retask",
        "energy_return": "return",
        "link_return": "return",
        "battery_land": "land-now",
        "wind_hold": "loiter",
        "wind_mild": "continue",
        "transition_no_override": "continue",
    }
    for name, action in expected.items():
        window, receipt, lowered = windows[name]
        request, label = window.turns[-1]
        assert label["action"] == receipt["action"] == action
        assert receipt["status"] in ADMITTED
        if name.startswith("quiet_"):
            assert label["params"] == {} and len(window.turns) == 1
        if action == "retask":
            assert lowered == (
                "fw_goto_waypoint",
                "loiter_observe",
                "back_transition",
                "mc_return_to_home",
                "vtol_land",
            )
            missing = deepcopy(request)
            missing["perception"]["results"] = []
            rejected, plan, commands = window.replay_final(missing, canonical_json(label))
            assert rejected.reason == "retask_reference_unavailable" and not plan and commands == 0
    window, receipt, _ = windows["px4_owns"]
    assert (
        receipt["status"] == "rejected" and receipt["reason"] == "px4_owns_or_flight_state_unknown"
    )
    assert window.turns[-1][1]["action"] == "continue"


def test_link_loss_returns_immediately_without_grace_window(windows):
    window, _, _ = windows["link_return"]
    request, _ = window.turns[-1]
    fresh = deepcopy(request)
    fresh["state"]["telemetry"]["link"]["heartbeat_age_ms"] = 1015
    assert policy(fresh)["action"] == "return"


def test_perception_is_real_ordered_and_failure_is_unavailable(windows):
    expected = {
        "target_found": ["look", "target_status", "route_status"],
        "target_lost": ["look", "target_status"],
        "battery_unavailable": ["battery_status"],
        "stale_target": ["look", "target_status"],
        "tool_budget": ["look"],
        "wind_hold": ["terrain_ahead", "route_status"],
    }
    for category, tools in expected.items():
        window, receipt, _ = windows[category]
        assert receipt["status"] in ADMITTED
        assert [label["tool"] for _, label in window.turns[:-1]] == tools
        for index, (request, label) in enumerate(window.turns):
            perception = request["perception"]
            assert perception["call_index"] == index
            assert perception["state_sha256"] == canonical_sha256(window.turns[0][0]["state"])
            assert [result["tool"] for result in perception["results"]] == tools[:index]
            assert policy(json.loads(canonical_json(request))) == label
        request, label = window.turns[-1]
        if category in {"battery_unavailable", "stale_target", "target_lost"}:
            assert all(
                not r["ok"] and r["data"] is None and r["error"]
                for r in request["perception"]["results"]
            )
            assert label["action"] == "continue"
        if category == "stale_target":
            assert all(
                r["age_ms"] > 1000 and r["error"] == "stale_observation"
                for r in request["perception"]["results"]
            )
        if category == "tool_budget":
            assert (
                request["perception"]["calls_remaining"] == 0
                and request["perception"]["tools"] == []
            )
            assert label["action"] == "continue"


def test_scorer_penalizes_unnecessary_action_and_enforces_protocol(windows):
    window, _, _ = windows["quiet_cruise"]
    request, label = window.turns[-1]
    result = score_turn(
        "quiet_cruise", request, label, window, canonical_json({**label, "action": "return"})
    )
    assert result["executor_admitted"] is True  # Being legal is not the same as being warranted.
    assert result["unnecessary_action"] is True and result["action_correct"] is False
    assert not score_turn("quiet_cruise", request, label, window, '{"schema": 1, "schema": 2}')[
        "schema_valid"
    ]
    result = score_turn(
        "quiet_cruise", request, label, window, canonical_json({**label, "event_id": 1})
    )
    assert (
        result["schema_valid"] and not result["identity_valid"] and not result["executor_admitted"]
    )

    window, _, _ = windows["tool_budget"]
    request, label = window.turns[-1]
    tool = {
        "schema": TOOL_SCHEMA,
        "tool": "target_status",
        "args": {},
        "request_id": request["request_id"],
        "call_index": 1,
    }
    result = score_turn("tool_budget", request, label, window, canonical_json(tool))
    assert (
        result["schema_valid"]
        and not result["tool_use_correct"]
        and result["reason"] == "tool_budget_or_offer"
    )


def test_retask_accuracy_separate_from_validity(windows):
    window, _, _ = windows["target_found"]
    request, label = window.turns[-1]
    good = score_turn("target_found", request, label, window, canonical_json(label))
    assert good["required_retask_valid"]
    bad = deepcopy(label)
    bad["params"]["plan"]["steps"][0]["params"]["target_ref"] = "poi:unknown"
    result = score_turn("target_found", request, label, window, canonical_json(bad))
    assert result["schema_valid"] and result["action_correct"] and not result["executor_admitted"]
    assert result["retask_plan_valid"] is False and result["required_retask_valid"] is False


def test_fixed_holdout_no_prompt_leakage_and_review_sample(tmp_path, reference_root):
    manifest = build(tmp_path, reference_root)
    data = {
        part: read_jsonl(tmp_path / name)
        for part, name in (
            ("train", "train.jsonl"),
            ("val", "val.jsonl"),
            ("holdout", "x1-flight-loop-holdout.jsonl"),
        )
    }
    sample = read_jsonl(tmp_path / "sample.jsonl")
    assert len(sample) == manifest["sample_turns"] == 200
    assert {r["category"] for r in sample} == {r["category"] for r in data["train"]}
    sample_episodes = {row["episode_id"] for row in sample}
    assert {row["sample_id"] for row in sample} == {
        row["sample_id"] for row in data["train"] if row["episode_id"] in sample_episodes
    }
    episode_splits = defaultdict(set)
    for split, rows in data.items():
        for row in rows:
            episode_splits[row["episode_id"]].add(split)
            messages = row["messages"]
            assert [m["role"] for m in messages] == (
                ["system", "user"] if split == "holdout" else ["system", "user", "assistant"]
            )
            user = json.loads(messages[1]["content"])
            assert user.keys() == {"task", "request"}
            wire = messages[1]["content"]
            assert not any(
                f'"{name}"' in wire
                for name in (
                    "scenario_observation",
                    "motion_proof",
                    "category",
                    "oracle",
                    "expected",
                    "assistant_target",
                )
            )
            request = user["request"]
            progress = request.get("mission_progress")
            if progress is not None:
                prior = [r["request_id"] for r in progress["recent_receipts"]]
                assert len(prior) <= 4 and request["request_id"] not in prior
    assert all(len(parts) == 1 for parts in episode_splits.values())
    result = evaluate(data["holdout"], [], reference_root, reference_smoke=True)
    assert result["source"] == "oracle_smoke"
    assert result["summary"]["ticket_reference_path_exact_rate"] == 1
    assert result["summary"]["unnecessary_action_rate"] == 0
    assert result["summary"]["retasks_required"] == 8
    assert result["summary"]["turns"] == 348
    assert all(
        row["reason"] == "px4_owns_or_flight_state_unknown"
        for row in read_jsonl(tmp_path / "rejected-tickets.jsonl")
    )
    with pytest.raises(ValueError, match="regenerated"):
        evaluate(data["holdout"][1:], [], reference_root, reference_smoke=True)
    with pytest.raises(ValueError, match="one ordered reply"):
        evaluate(data["holdout"], [], reference_root)
