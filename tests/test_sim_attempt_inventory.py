import hashlib
import json

import pytest

from scripts.inventory_sim_attempts import classify_outer, inventory


def report(requested, status, gate, failure=None):
    return {
        "schema": "multidetect.vtol-mission-sensor-capture",
        "execution": {"requested": requested, "status": status},
        "gate_passed": gate,
        "failure": failure,
    }


@pytest.mark.parametrize(
    "args,expected",
    [
        ((False, "planned_not_executed", False), "planned_not_executed"),
        ((True, "passed", True), "execution_acceptance_passed"),
        ((True, "failed", False, "capture failed"), "execution_acceptance_rejected"),
        ((True, "passed", False), "unresolved_or_inconsistent"),
        ((1, "passed", True), "unresolved_or_inconsistent"),
    ],
)
def test_categories_do_not_turn_dryrun_into_failure(args, expected):
    assert classify_outer(report(*args)) == expected


def test_inventory_binding_duplicates_and_no_success_rate(tmp_path):
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report(True, "passed", True)), encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    result = inventory([(path, digest)])
    assert result["task_success_rate"] is None
    assert not result["underlying_gates_revalidated"]
    with pytest.raises(ValueError, match="duplicate"):
        inventory([(path, digest), (path, digest)])
    with pytest.raises(ValueError, match="hash"):
        inventory([(path, "0" * 64)])
