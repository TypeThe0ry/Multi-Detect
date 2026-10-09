import hashlib
import json

import pytest

from multidetect.clock import HighResolutionSystemClock
from scripts import admit_sim_execution_record as module


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    identity = HighResolutionSystemClock(session_id="admission-test").identity()
    mission = {
        "schema": "multidetect.px4-vtol-sitl-mission-acceptance",
        "gate_passed": True,
        "failure": None,
        "container": {"id": "a" * 64},
        "safety": dict.fromkeys(
            ("real_pixhawk_contacted", "hardware_control_enabled", "ground_truth_used_for_mission"),
            False,
        ),
        "transaction": {"id": "tx"},
        "protocol": {"committed_protocol_sha256": "b" * 64},
        "documents": {
            "executable_mission": {"sha256": "c" * 64},
            "task_spec": {"document": {"task_id": "generated-id", "task_type": "survey"}},
        },
        "scenario": {"scenario_id": "scenario-a", "manifest_sha256": "d" * 64},
        "flight": {
            **dict.fromkeys(
                (
                    "auto_mission_confirmed",
                    "armed_after_permit",
                    "in_air_observed",
                    "fixed_wing_observed",
                    "multicopter_observed",
                    "landed_observed",
                    "terminal_disarmed",
                ),
                True,
            ),
            "host_clock_identity": identity,
            "observer_origin_host_monotonic_s": 2,
            "completed_host_monotonic_s": 5,
            "duration_s": 3,
        },
        "mission_receive_window": {
            "clock": "host_perf_counter_since_collector_start",
            "host_clock_identity": identity,
            "available": True,
            "start_semantics": "before_ARM_command_after_permit",
            "end_semantics": "accepted_landed_disarmed_observation",
            "arm_request_host_monotonic_s": 1,
            "completed_host_monotonic_s": 5,
        },
        "shared_outcome_shadow": {
            "outcome": {"status": "completed", "mission_hash": "c" * 64, "elapsed_s": 3}
        },
    }
    outer = {
        "scenario": {"scenario_id_expected": "scenario-a", "manifest_sha256": "d" * 64},
        "schema": "multidetect.vtol-mission-sensor-capture",
        "gate_passed": True,
        "failure": None,
        "container": {"id": "a" * 64},
        "gates": dict.fromkeys(
            ("complete_vtol_mission", "mission_journal_integrity", "final_artifact_integrity"), True
        ),
    }
    calls = []

    def validate(path, payload, **kwargs):
        calls.append((path, payload, kwargs))
        return {"test_double_journal_validation": True}

    monkeypatch.setattr(module, "validate_mission_journal", validate)

    def run(*, bad_digest=False):
        journal_path = tmp_path / "journal.jsonl"
        journal_path.write_text("fixture", encoding="utf-8")
        jp = hashlib.sha256(journal_path.read_bytes()).hexdigest()
        mission_path = tmp_path / "mission.json"
        mission_path.write_text(json.dumps(mission), encoding="utf-8")
        mp = hashlib.sha256(mission_path.read_bytes()).hexdigest()
        outer["final_integrity"] = {
            "revalidated_after_mission_and_cleanup": True,
            "hashes": {"mission_evidence": mp, "mission_journal": jp},
        }
        outer["mission"] = {"file_sha256": mp}
        outer_path = tmp_path / "outer.json"
        outer_path.write_text(json.dumps(outer), encoding="utf-8")
        op = hashlib.sha256(outer_path.read_bytes()).hexdigest()
        return module.admit(
            outer_path, "0" * 64 if bad_digest else op, mission_path, mp, journal_path, jp
        )

    return mission, outer, run, calls


def test_admission_is_label_specific_not_global_promotion(evidence):
    _, _, run, calls = evidence
    result = run()
    assert result["labels"] == {"completed": True, "arm_to_completion_host_receive_s": 4}
    assert len(calls) == 1
    assert calls[0][2]["transaction_id"] == "tx"
    assert not result["synchronized_pixel_labels_eligible"]
    assert not result["physical_energy_labels_eligible"]
    assert not result["model_promotion_allowed"]


@pytest.mark.parametrize(
    "change",
    [
        lambda m: m.update(gate_passed=False),
        lambda m: m["container"].update(id="d" * 64),
        lambda m: m["safety"].update(real_pixhawk_contacted=True),
        lambda m: m["flight"].update(terminal_disarmed=False),
        lambda m: m["flight"].update(host_clock_identity={}),
        lambda m: m["flight"].update(duration_s=999),
        lambda m: m["mission_receive_window"].update(arm_request_host_monotonic_s=True),
        lambda m: m["mission_receive_window"].update(arm_request_host_monotonic_s=6),
        lambda m: m["shared_outcome_shadow"]["outcome"].update(mission_hash="d" * 64),
    ],
)
def test_invalid_execution_rejected(evidence, change):
    mission, _, run, _ = evidence
    change(mission)
    with pytest.raises(ValueError):
        run()


def test_bad_hash_rejected_before_journal(evidence):
    _, _, run, calls = evidence
    with pytest.raises(ValueError, match="hash"):
        run(bad_digest=True)
    assert not calls


def test_task_id_changes_do_not_change_semantic_group(evidence):
    mission, _, run, _ = evidence
    first = run()["grouping"]
    mission["documents"]["task_spec"]["document"]["task_id"] = "different-generated-id"
    assert run()["grouping"] == first


def test_scenario_cannot_disagree_with_parent(evidence):
    mission, _, run, _ = evidence
    mission["scenario"]["scenario_id"] = "renamed"
    with pytest.raises(ValueError, match="scenario binding"):
        run()


def test_journal_failure_not_hidden(evidence, monkeypatch):
    _, _, run, _ = evidence
    monkeypatch.setattr(module, "validate_mission_journal", lambda *a, **k: {"chain": False})
    with pytest.raises(ValueError, match="journal validation"):
        run()
