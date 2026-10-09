import copy
import hashlib
import json

import pytest

from scripts.audit_mission_clock_receipts import audit_files, audit_receipts, read_bound
from scripts.gazebo_clock_capture import describe_clock_progress


def receipts():
    return [
        {
            "host_receive_monotonic_ns": (i + 1) * 1000000000,
            "clock": {
                "schema_version": 1,
                "source": "gz.msgs.Clock",
                "topic": "/world/default/clock",
                "sequence": i,
                "sim_ns": i * 100000000,
                "real_ns": i * 1000000000,
                "system_ns": 1788856937257446631 + i,
                "container_receive_monotonic_ns": i * 1000000000,
                "host_px4_alignment_claimed": False,
            },
        }
        for i in range(5)
    ]


def test_brackets_keep_distinct_clock_domains():
    result = audit_receipts(receipts(), 2.5, 3.5)
    assert result["receipt_window_covered"]
    assert result["strict_receipt_brackets"]["arm"]["receipt_associated_sim_ns"] == [
        100000000,
        200000000,
    ]
    assert result["maximum_sample_gaps_ns"]["host_receive_monotonic_ns"] == 1000000000
    assert not result["training_label_eligible"]
    assert not result["source_event_time_bounds_claimed"]
    assert result["receipt_continuity"]["status"] == "budget_not_supplied"
    assert not result["receipt_continuity"]["qualified"]


@pytest.mark.parametrize("budget,status", [(999999999, "gap_exceeded"), (1000000000, "passed")])
def test_explicit_receipt_budget_does_not_qualify_source_time(budget, status):
    result = audit_receipts(receipts(), 2.5, 3.5, max_receipt_gap_ns=budget)
    continuity = result["receipt_continuity"]
    assert continuity["status"] == status
    assert continuity["qualified"] == (status == "passed")
    assert continuity["maximum_window_gap_ns"] == 1000000000
    assert continuity["window_gap_count"] == 2
    assert not continuity["observation_freshness_claimed"]
    assert not continuity["source_alignment_claimed"]
    assert not result["training_label_eligible"]


def test_receipt_budget_uses_window_not_entire_capture():
    rows = receipts()
    rows[-1]["host_receive_monotonic_ns"] = 100000000000
    result = audit_receipts(rows, 2.5, 3.5, max_receipt_gap_ns=1000000000)
    assert result["maximum_sample_gaps_ns"]["host_receive_monotonic_ns"] == 96000000000
    assert result["receipt_continuity"]["qualified"]
    # Exact endpoint equality still requires the strict right neighbor.
    result = audit_receipts(rows, 2.5, 4, max_receipt_gap_ns=1000000000)
    assert result["receipt_continuity"]["status"] == "gap_exceeded"


def test_receipt_budget_missing_boundary_cannot_pass():
    result = audit_receipts(receipts(), 0, 3, max_receipt_gap_ns=3600000000000)
    assert result["receipt_continuity"]["status"] == "insufficient_coverage"
    assert result["receipt_continuity"]["maximum_window_gap_ns"] is None
    assert not result["receipt_continuity"]["qualified"]


@pytest.mark.parametrize("budget", [True, 0, -1, 1.0, float("nan"), "100", 3600000000001])
def test_invalid_receipt_budgets(budget):
    with pytest.raises(ValueError, match="receipt gap budget"):
        audit_receipts(receipts(), 2, 4, max_receipt_gap_ns=budget)


@pytest.mark.parametrize("start,end", [(0, 3), (2, 6), (1, 5)])
def test_missing_strict_boundary_not_extrapolated(start, end):
    assert not audit_receipts(receipts(), start, end)["receipt_window_covered"]


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r[1].update(host_receive_monotonic_ns=1),
        lambda r: r[1].update(host_receive_monotonic_ns=True),
        lambda r: r[0]["clock"].update(sequence=False),
        lambda r: r[0]["clock"].update(schema_version=True),
        lambda r: r[0]["clock"].update(source="other"),
        lambda r: r[0]["clock"].update(topic="/world/other/clock"),
        lambda r: r[0]["clock"].update(host_px4_alignment_claimed=True),
        lambda r: r[2]["clock"].update(sim_ns=0),
        lambda r: r[0]["clock"].update(extra=1),
    ],
)
def test_invalid_receipts_rejected(change):
    rows = receipts()
    change(rows)
    with pytest.raises(ValueError):
        audit_receipts(rows, 2, 4)


@pytest.mark.parametrize("start,end", [(True, 4), (2, float("nan")), (4, 2), (2, 2)])
def test_invalid_windows(start, end):
    with pytest.raises(ValueError):
        audit_receipts(receipts(), start, end)


def test_sim_pause_and_wall_adjustment_are_not_resets():
    rows = receipts()
    rows[2]["clock"]["sim_ns"] = rows[1]["clock"]["sim_ns"]
    rows[2]["clock"]["system_ns"] = 1
    assert audit_receipts(rows, 2, 4)["sim_pause_intervals"] == 1


def test_file_binding_and_precision(tmp_path):
    rows = receipts()
    clock = tmp_path / "clock.jsonl"
    clock.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    mission = tmp_path / "mission.json"
    mission.write_text(
        json.dumps(
            {
                "gate_passed": True,
                "failure": None,
                "container": {"id": "a" * 64},
                "mission_receive_window": {
                    "arm_request_host_monotonic_s": 2,
                    "completed_host_monotonic_s": 4,
                },
                "flight": {"completed_host_monotonic_s": 4},
            }
        ),
        encoding="utf-8",
    )
    outer = tmp_path / "outer.json"
    report = {
        "gate_passed": True,
        "gates": {"clock_capture": True},
        "clock_capture": {
            "exit_code": 0,
            "host_px4_alignment_claimed": False,
            "sha256": hashlib.sha256(clock.read_bytes()).hexdigest(),
            "bytes": clock.stat().st_size,
            "container_id": "a" * 64,
            "summary": describe_clock_progress([r["clock"] for r in rows]),
        },
        "campaign": {
            "verified_sorties": [
                {"report_sha256": hashlib.sha256(mission.read_bytes()).hexdigest()}
            ]
        },
    }
    outer.write_text(json.dumps(report), encoding="utf-8")
    digest = hashlib.sha256(outer.read_bytes()).hexdigest()
    assert audit_files(outer, digest, clock, mission)["receipt_window_covered"]
    assert audit_files(
        outer, digest, clock, mission, max_receipt_gap_ns=1000000000
    )["receipt_continuity"]["qualified"]
    single = copy.deepcopy(report)
    single.pop("campaign")
    mission_sha = hashlib.sha256(mission.read_bytes()).hexdigest()
    single["mission"] = {"file_sha256": mission_sha}
    single["final_integrity"] = {
        "hashes": {"mission_evidence": mission_sha},
        "revalidated_after_mission_and_cleanup": True,
    }
    single["gates"].update(complete_vtol_mission=True, final_artifact_integrity=True)
    outer.write_text(json.dumps(single), encoding="utf-8")
    assert audit_files(outer, hashlib.sha256(outer.read_bytes()).hexdigest(), clock, mission)[
        "receipt_window_covered"
    ]
    for alteration in ("primary_hash", "final_hash", "revalidated", "gate"):
        broken_single = copy.deepcopy(single)
        if alteration == "primary_hash":
            broken_single["mission"]["file_sha256"] = "0" * 64
        elif alteration == "final_hash":
            broken_single["final_integrity"]["hashes"]["mission_evidence"] = "0" * 64
        elif alteration == "revalidated":
            broken_single["final_integrity"]["revalidated_after_mission_and_cleanup"] = False
        else:
            broken_single["gates"]["complete_vtol_mission"] = False
        outer.write_text(json.dumps(broken_single), encoding="utf-8")
        with pytest.raises(ValueError, match="single capture"):
            audit_files(outer, hashlib.sha256(outer.read_bytes()).hexdigest(), clock, mission)
    for altered in ("clock", "container", "summary", "duplicate"):
        broken = copy.deepcopy(report)
        if altered == "clock":
            broken["clock_capture"]["sha256"] = "0" * 64
        elif altered == "container":
            broken["clock_capture"]["container_id"] = "b" * 64
        elif altered == "summary":
            broken["clock_capture"]["summary"]["sample_count"] = 9
        else:
            broken["campaign"]["verified_sorties"] *= 2
        outer.write_text(json.dumps(broken), encoding="utf-8")
        with pytest.raises(ValueError):
            audit_files(outer, hashlib.sha256(outer.read_bytes()).hexdigest(), clock, mission)
    with pytest.raises(ValueError):
        read_bound(clock, "0" * 64)
    with pytest.raises(ValueError):
        read_bound(clock, "invalid")
