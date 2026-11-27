import json

import pytest

from scripts.x1_gate_failures_to_sft import (
    DECISION_SCHEMA,
    TOOL_SCHEMA,
    canonical_json,
    convert,
    read_jsonl,
    write_outputs,
)
from scripts.x1_gate_trend import load_gates, markdown


def _request(request_id, *, event_id=7, actions=("continue", "return")):
    return {
        "actions": list(actions),
        "capabilities": ["airframe:x1"],
        "event_id": event_id,
        "hold_refs": [],
        "perception": {"call_index": 0, "calls_remaining": 2, "results": [],
                       "schema": "x1.contingency.tool.v1",
                       "tools": ["battery_status", "target_status"]},
        "request_id": request_id,
        "schema": "x1.contingency.request.v1",
        "state": {"mission_id": "m1", "revision": 1, "schema": "x1.contingency.state.v1",
                  "t_ms": 1000},
    }


def _row(request_id, *, label_status="oracle_labeled", label=None, scope="model",
         category="quiet-cadence", episode_id="ep-1", event_id=7, extra_request=None):
    request = _request(request_id, event_id=event_id)
    if extra_request:
        request.update(extra_request)
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": canonical_json({"task": "task", "request": request})},
    ]
    if label is None:
        label = {
            "schema": DECISION_SCHEMA,
            "request_id": request_id,
            "event_id": event_id,
            "mission_id": "m1",
            "revision": 1,
            "action": "continue",
            "params": {},
        }
    return {
        "case_id": category,
        "category": category,
        "episode_id": episode_id,
        "sample_id": f"{request_id}:0",
        "request_id": request_id,
        "messages": messages,
        "oracle_label": label,
        "label_status": label_status,
        "label_reason": None,
        "failure_scope": scope,
        "failure_reasons": ["action_mismatch"],
        "model_output": "{}",
        "request": request,
    }


def _write_gate(path, rows, label="g1"):
    path.mkdir(parents=True, exist_ok=True)
    (path / "gate.json").write_text(
        canonical_json({"label": label, "verdict": "FAIL", "metrics": {}}), encoding="utf-8"
    )
    with (path / "model-failures.jsonl").open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(canonical_json(row) + "\n")


def test_conversion_keeps_messages_and_echoes_identity(tmp_path):
    gate = tmp_path / "g1"
    tool_label = {"schema": TOOL_SCHEMA, "tool": "battery_status", "args": {}}
    rows = [
        _row("r-1"),
        _row("r-2", category="tool-case", label=tool_label),
        _row("r-3", label_status="unreliable"),
        _row("r-4", label_status="missing"),
        _row("r-5", scope="infrastructure"),
        _row("r-6", scope="evidence_scope"),
        _row("r-7", extra_request={"expected_action": "return"}),
        _row("r-8", label={"schema": DECISION_SCHEMA, "request_id": "wrong", "event_id": 7,
                           "mission_id": "m1", "revision": 1, "action": "continue",
                           "params": {}}),
        _row("r-9", label={"schema": "not-a-schema"}),
    ]
    _write_gate(gate, rows, label="g1")

    result = convert([gate], cap=10, val_percent=0)
    assert len(result["rows"]) == 2
    decision = next(row for row in result["rows"] if row["sample_id"] == "g1:r-1:0")
    tool = next(row for row in result["rows"] if row["sample_id"] == "g1:r-2:0")
    assert decision["messages"][:2] == rows[0]["messages"]
    assert decision["messages"][2]["role"] == "assistant"
    assert json.loads(decision["messages"][2]["content"]) == rows[0]["oracle_label"]
    assert json.loads(tool["messages"][2]["content"]) == {
        "schema": TOOL_SCHEMA,
        "tool": "battery_status",
        "args": {},
        "request_id": "r-2",
        "call_index": 0,
    }
    assert result["dropped"]["label_status:unreliable"] == 1
    assert result["dropped"]["label_status:missing"] == 1
    assert result["dropped"]["infrastructure"] == 1
    assert result["dropped"]["evidence_scope"] == 1
    assert result["dropped"]["oracle_field_leak"] == 1
    assert result["dropped"]["oracle_identity_mismatch"] == 1
    assert result["dropped"]["oracle_schema_invalid"] == 1

    manifest = write_outputs(result, tmp_path / "out")
    assert manifest["counts"] == {
        "rows_read": 9,
        "unique_requests": 2,
        "duplicate_requests": 0,
        "kept": 2,
        "train": 2,
        "val": 0,
    }
    assert not (tmp_path / "out" / "x1-flight-loop-holdout.jsonl").exists()
    train = read_jsonl(tmp_path / "out" / "train.jsonl")
    assert [set(row) for row in train] == [
        {"sample_id", "episode_id", "split", "category", "source", "messages"}
    ] * 2
    assert {row["split"] for row in train} == {"train"}
    assert {row["source"] for row in train} == {"gate_feedback"}


def test_dedupe_by_request_content(tmp_path):
    gate = tmp_path / "g1"
    first = _row("r-1")
    duplicate = _row("r-1", category="other-category")
    duplicate["sample_id"] = "different-sample"
    duplicate["model_output"] = "different model output"
    _write_gate(gate, [first, duplicate])

    result = convert([gate], cap=10, val_percent=0)
    assert len(result["rows"]) == 1
    assert result["unique"] == 1
    assert result["duplicates"] == 1
    assert result["dropped"]["duplicate_request"] == 1


def test_explicit_per_category_caps(tmp_path):
    gate = tmp_path / "g1"
    rows = [_row(f"a-{i}", category="a") for i in range(5)]
    rows += [_row(f"b-{i}", category="b") for i in range(3)]
    _write_gate(gate, rows)

    result = convert([gate], cap=2, category_caps={"b": 1}, val_percent=0)
    assert result["category_stats"]["a"]["kept"] == 2
    assert result["category_stats"]["a"]["capped"] == 3
    assert result["category_stats"]["b"]["kept"] == 1
    assert result["category_stats"]["b"]["capped"] == 2
    assert result["dropped"]["category_cap"] == 5
    assert len(result["rows"]) == 3


def test_leakage_guard_holdout_and_no_oracle_reuse(tmp_path):
    holdout = tmp_path / "holdout.jsonl"
    holdout.write_text(
        canonical_json(
            {
                "sample_id": "fl-95147-00-00:1:0",
                "episode_id": "fl-95147-00-00",
                "split": "holdout",
                "category": "quiet_climb",
                "source": "synthetic_cpu",
                "messages": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    gate = tmp_path / "g1"
    leaked = _row("r-held", episode_id="fl-95147-00-00")
    kept = _row("r-ok", episode_id="ep-other")
    _write_gate(gate, [leaked, kept])

    result = convert([gate], cap=10, val_percent=0, holdout_path=holdout)
    assert len(result["rows"]) == 1
    assert result["rows"][0]["sample_id"] == "g1:r-ok:0"
    assert result["dropped"]["holdout_collision"] == 1
    assert all(row["split"] != "holdout" for row in result["rows"])
    assert "oracle_label" not in canonical_json(result["rows"][0]["messages"][1])
    with pytest.raises(ValueError):
        write_outputs(result, tmp_path / "out", val_name="x1-flight-loop-holdout.jsonl")


def test_val_split_is_episode_level(tmp_path):
    gate = tmp_path / "g1"
    rows = [_row("r-1", episode_id="ep-1"), _row("r-2", episode_id="ep-1"),
            _row("r-3", episode_id="ep-2")]
    _write_gate(gate, rows)

    result = convert([gate], cap=10, val_percent=50)
    by_episode = {}
    for row in result["rows"]:
        by_episode.setdefault(row["episode_id"], set()).add(row["split"])
    assert all(len(splits) == 1 for splits in by_episode.values())
    assert {row["split"] for row in result["rows"]} == {"train", "val"}


def test_trend_table_deltas_and_reason_counts(tmp_path):
    first = tmp_path / "g1"
    second = tmp_path / "g2"
    for path, label, verdict, schema, identity, evidence in [
        (first, "g1", "FAIL", 0.8, 0.7, 4),
        (second, "g2", "PASS", 0.9, 0.75, 1),
    ]:
        path.mkdir()
        (path / "gate.json").write_text(
            canonical_json(
                {
                    "label": label,
                    "verdict": verdict,
                    "metrics": {
                        "schema_validity": {"value": schema, "verdict": "FAIL"},
                        "identity_echo": {"value": identity, "verdict": "FAIL"},
                        "retask_structure": {"value": None, "verdict": "FAIL"},
                    },
                    "evidence_scope_rejections": evidence,
                    "failure_scopes": {"model": 3, "evidence_scope": evidence,
                                       "infrastructure": 1},
                    "evidence_scope_reasons": {"missing_or_unbound_motion_proof": evidence},
                    "coverage": {"turns_sent": 10},
                }
            ),
            encoding="utf-8",
        )
        with (path / "model-failures.jsonl").open("w", encoding="utf-8") as stream:
            for reason in ("action_mismatch", "invalid_schema"):
                stream.write(canonical_json({"failure_reasons": [reason]}) + "\n")

    gates = load_gates([first, second])
    text = markdown(gates)
    assert "| schema_validity | g1 | 0.8000 | FAIL | — | — |" in text
    assert "| schema_validity | g2 | 0.9000 | FAIL | +0.1000 | +0.1000 |" in text
    assert "| identity_echo | g1 | 0.7000 | FAIL | — | — |" in text
    assert "| identity_echo | g2 | 0.7500 | FAIL | +0.0500 | +0.0500 |" in text
    assert "| retask_structure | g2 | — | FAIL | — | — |" in text
    assert "| action_mismatch | 1 | 1 |" in text
    assert "| invalid_schema | 1 | 1 |" in text
    assert "| g2 | 1 | 3 | 1 |" in text