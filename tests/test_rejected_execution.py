import pytest

from multidetect.autonomy.serialization import canonical_sha256
from scripts.classify_rejected_execution import classify


def report(status):
    doc = {"items": [], "schema_version": 1}
    digest = canonical_sha256(doc)
    return {
        "schema": "multidetect.px4-vtol-sitl-mission-acceptance",
        "gate_passed": False,
        "failure": "capture rejected",
        "documents": {"executable_mission": {"document": doc, "sha256": digest}},
        "shared_outcome_shadow": {"outcome": {"status": status, "mission_hash": digest}},
    }


@pytest.mark.parametrize(
    "status,category",
    [
        ("completed", "acceptance_rejected_despite_completed_outcome_claim"),
        ("failed", "acceptance_rejected_with_unsuccessful_outcome_claim"),
        ("timeout", "acceptance_rejected_with_unsuccessful_outcome_claim"),
        ("waiting", "acceptance_rejected_without_terminal_outcome_claim"),
    ],
)
def test_failure_does_not_become_negative_reward(status, category):
    result = classify(report(status))
    assert result["classification"] == category
    assert result["labels"] == {"task_completed": None, "policy_reward": None}
    assert not result["training_eligible"]
    assert not result["raw_telemetry_replayed"]


def test_changed_mission_does_not_inherit_outcome():
    value = report("completed")
    value["documents"]["executable_mission"]["document"]["items"] = [1]
    result = classify(value)
    assert not result["outcome_document_hash_bound"]
    assert result["classification"].endswith("unbound_or_missing")


def test_no_outcome_is_unknown():
    value = report("completed")
    value.pop("shared_outcome_shadow")
    assert classify(value)["labels"]["task_completed"] is None


@pytest.mark.parametrize("gate", [True, None, 0])
def test_not_explicit_failure_rejected(gate):
    value = report("completed")
    value["gate_passed"] = gate
    with pytest.raises(ValueError):
        classify(value)
