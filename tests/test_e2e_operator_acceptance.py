"""Operator-link e2e acceptance evaluator, regression-tested on SITL runs 9 and 10.

``tests/fixtures/e2e_operator/run{9,10}`` are trimmed copies of
``D:/Projects/mdwt/_scratch/e2e/run{9,10}``: every non-status ground document,
the status received just before/after each of them, onboard key records, the
last episode step and console lines other than accepted heartbeat ACKs.  The
same assertions run against the full directories when they exist locally.
"""

import json
from pathlib import Path

import pytest

from scripts.run_e2e_operator_acceptance import SitlLock, _task_validation, evaluate_run, main

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "e2e_operator"
REAL = Path("D:/Projects/mdwt/_scratch/e2e")


def _sources(name: str):
    return [
        pytest.param(FIXTURES / name, id=f"{name}-fixture"),
        pytest.param(
            REAL / name,
            id=f"{name}-real",
            marks=pytest.mark.skipif(
                not (REAL / name / "gcs-documents.jsonl").is_file(), reason="local evidence absent"
            ),
        ),
    ]


@pytest.mark.parametrize("run_dir", _sources("run9"))
def test_run9_signed_plan_partial_summary_battery_stale_return(run_dir):
    report = evaluate_run(run_dir)

    plan = report["plan"]
    assert plan["received_at_ground"] and plan["signed_command_accepted"] and plan["approved"]
    assert plan["skills"] == ["count", "return_home"]
    assert plan["approval_latency_s"] == pytest.approx(0.593, abs=1e-3)

    findings = report["findings"]
    assert [f["id"] for f in findings] == ["f1", "f2"]
    assert all(f["arrived_at_ground"] for f in findings)
    for f in findings:
        assert 0.0 <= f["latency_lower_s"] <= f["latency_upper_s"] < 5.0

    summary = report["final_summary"]
    assert summary["complete"] is False
    assert summary["pages_total"] == 2 and summary["pages_received"] == [1]
    assert summary["duplicate_page_documents"] > 0
    assert summary["recorded_onboard"] is True

    term = report["termination"]
    assert term["status"] == "ended_early" and term["onboard_status"] == "ended_early"
    assert term["action"] == "return_now"
    assert "battery telemetry stale" in term["reason"]
    assert term["source"] == "ground_escalation"

    count = report["count"]
    assert count["reported"] == 2 and count["source"] == "ground_status"
    assert count["is_lower_bound"] is True
    assert count["expected"] == 3 and count["matched"] == 2 and count["false_findings"] == 0
    assert count["count_exact"] is False

    link = report["link"]
    assert link["rejected_ack_total"] == 2
    assert link["rejected_after_first_document"] == 0
    assert {(a["kind"], a["phase"]) for a in link["rejected_acks"]} == {
        (34, "before_first_document")
    }
    assert link["vehicle_telemetry_gaps"] == []
    assert link["onboard_records_missing_at_ground"] == [
        {"kind": "final_summary", "sequence": 1308}
    ]

    assert report["world_id"] == "gen_cnt2_w0000"
    assert report["verdict"] == "FAIL"
    assert not report["criteria"]["final_summary_complete"]
    assert not report["criteria"]["mission_completed"]


@pytest.mark.parametrize("run_dir", _sources("run10"))
def test_run10_complete_summary_lower_bound_count_and_return(run_dir):
    report = evaluate_run(run_dir)

    assert report["plan"]["approved"] and report["plan"]["signed_command_accepted"]
    assert report["plan"]["approval_latency_s"] == pytest.approx(0.375, abs=1e-3)

    findings = report["findings"]
    assert len(findings) == 1 and findings[0]["arrived_at_ground"]
    assert 0.0 <= findings[0]["latency_upper_s"] < 5.0

    summary = report["final_summary"]
    assert summary["complete"] is True and summary["status"] == "completed"
    assert summary["source"] == "final_summary"

    assert report["termination"]["status"] == "completed"
    assert report["termination"]["reason"] is None

    count = report["count"]
    assert count["reported"] == 1 and count["source"] == "ground_final_summary"
    assert count["is_lower_bound"] is True
    assert count["step_reasons"] == {"s1": "budget_exhausted"}
    assert count["expected"] == 3 and count["matched"] == 1

    assert report["return_home"]["arrived_reported"] is True
    assert report["return_home"]["reason"] == "arrived"

    link = report["link"]
    assert link["rejected_ack_total"] == 0 and link["vehicle_telemetry_gaps"] == []
    assert link["onboard_records_missing_at_ground"] == []

    failed = [k for k, ok in report["criteria"].items() if not ok]
    assert failed == ["count_matches_truth"]
    assert report["verdict"] == "FAIL"


def _write(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            (row if isinstance(row, str) else json.dumps(row, ensure_ascii=False)) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def _status(t: float, **extra) -> dict:
    return {
        "kind": "status",
        "mission_id": "m",
        "t": t,
        "supervisor": {"link": "up", "link_silent_s": 0.5},
        **extra,
    }


def _synthetic_run(tmp_path: Path, *, drop_second_finding: bool = False) -> Path:
    run = tmp_path / "run"
    summary = {
        "status": "completed",
        "terminal_action": None,
        "terminal_reason": None,
        "counts": {"s1": 2},
        "count_is_lower_bound": False,
        "history": [
            {"step": "s1", "skill": "count", "state": "succeeded", "reason": "covered"},
            {"step": "s2", "skill": "return_home", "state": "succeeded", "reason": "arrived"},
        ],
    }
    body = json.dumps(summary, separators=(",", ":"))
    half = len(body) // 2
    plan = {
        "kind": "plan",
        "mission_id": "m",
        "sequence": 2,
        "timestamp": 10.0,
        "plan_sha256": "abc",
        "requires_approval": True,
        "steps": [{"id": "s1", "skill": "count"}, {"id": "s2", "skill": "return_home"}],
    }
    approved = {
        "kind": "plan_approved",
        "mission_id": "m",
        "sequence": 5,
        "timestamp": 12.5,
        "plan_sha256": "abc",
    }
    f1 = {
        "kind": "finding",
        "mission_id": "m",
        "sequence": 10,
        "timestamp": 20.0,
        "id": "f1",
        "n": 1.0,
        "e": 2.0,
    }
    f2 = {
        "kind": "finding",
        "mission_id": "m",
        "sequence": 11,
        "timestamp": 30.0,
        "id": "f2",
        "n": 50.0,
        "e": 50.0,
    }
    gap = {
        "kind": "vehicle_telemetry_gap",
        "timestamp": 25.0,
        "vehicle_start_s": 354.0,
        "vehicle_end_s": 361.1,
        "gap_s": 7.1,
    }
    started = {
        "kind": "mission_started",
        "mission_id": "m",
        "sequence": 1,
        "timestamp": 9.0,
        "package": {"home": [0.0, 0.0]},
    }
    ground = [_status(9.5), plan, _status(11.0), approved, _status(19.0), f1, _status(21.5)]
    if not drop_second_finding:
        ground += [_status(29.5), f2, _status(31.0)]
    ground += [
        {
            "kind": "final_summary_page",
            "mission_id": "m",
            "page": 2,
            "pages": 2,
            "body": body[half:],
        },
        {
            "kind": "final_summary_page",
            "mission_id": "m",
            "page": 1,
            "pages": 2,
            "body": body[:half],
        },
        {
            "kind": "final_summary_page",
            "mission_id": "m",
            "page": 1,
            "pages": 2,
            "body": body[:half],
        },
    ]
    _write(run / "gcs-documents.jsonl", ground)
    _write(
        run / "gcs.out",
        [
            "uploaded m",
            "onboard ack kind=34 index=0 accepted=False 任务标识不匹配或没有机载任务",
            json.dumps(started),
            "onboard ack kind=37 index=0 accepted=True ground command accepted",
            "onboard ack kind=34 index=0 accepted=False 任务标识不匹配或没有机载任务",
        ],
    )
    onboard_rows = [
        started,
        plan,
        {"kind": "ground_event", "event_type": "approve_plan", "sequence": 4},
        approved,
        f1,
        gap,
        f2,
        {"kind": "final_summary", "sequence": 40, "summary": summary},
    ]
    _write(
        run / "onboard" / "collector" / "ep-onboard-frames" / "mission_log" / "mission.jsonl",
        onboard_rows,
    )
    episode = run / "onboard" / "collector" / "episodes" / "episode-0000"
    _write(
        episode / "steps.jsonl",
        [{"obs_after": {"pose": {"local_ned_m": [0.3, 0.4, -10.0]}, "landed": False}}],
    )
    return run


TRUTH = {
    "targets": [
        {"count_member": True, "pose": {"y_north_m": 1.5, "x_east_m": 2.0}},
        {"count_member": True, "pose": {"y_north_m": 52.0, "x_east_m": 50.0}},
        {"count_member": False, "pose": {"y_north_m": 0.0, "x_east_m": 0.0}},
    ]
}


def test_pages_assembled_latency_bracket_gap_and_late_rejection(tmp_path):
    report = evaluate_run(_synthetic_run(tmp_path), truth=TRUTH)

    summary = report["final_summary"]
    assert summary["complete"] and summary["source"] == "final_summary_pages_assembled_here"
    assert summary["pages_received"] == [1, 2] and summary["duplicate_page_documents"] == 1

    assert report["plan"]["approval_latency_s"] == pytest.approx(2.5)
    f1, f2 = report["findings"]
    # Received after the t=19.0 status (before f1 was recorded) and before the t=21.5 one.
    assert (f1["latency_lower_s"], f1["latency_upper_s"]) == (0.0, 1.5)
    assert (f2["latency_lower_s"], f2["latency_upper_s"]) == (0.0, 1.0)

    assert report["count"]["matched"] == 2 and report["count"]["count_exact"] is True
    assert report["return_home"]["arrived_reported"] is True
    assert report["return_home"]["final_distance_home_m"] == pytest.approx(0.5)

    link = report["link"]
    assert link["vehicle_telemetry_gaps"] == [
        {"vehicle_start_s": 354.0, "vehicle_end_s": 361.1, "gap_s": 7.1}
    ]
    assert link["rejected_ack_total"] == 2 and link["rejected_after_first_document"] == 1
    assert report["criteria"]["no_rejected_acks_after_admission"] is False
    assert [k for k, ok in report["criteria"].items() if not ok] == [
        "no_rejected_acks_after_admission"
    ]


def test_finding_missing_at_ground_fails_and_is_listed(tmp_path):
    report = evaluate_run(_synthetic_run(tmp_path, drop_second_finding=True), truth=TRUTH)
    f2 = report["findings"][1]
    assert f2["arrived_at_ground"] is False and f2["latency_upper_s"] is None
    assert {"kind": "finding", "sequence": 11} in report["link"][
        "onboard_records_missing_at_ground"
    ]
    assert report["criteria"]["all_findings_at_ground"] is False
    # Only ground-received positions are matched against truth.
    assert report["count"]["matched"] == 1


def test_precise_ground_clock_latency_uses_status_monotonic_alignment(tmp_path):
    run = _synthetic_run(tmp_path)
    path = run / "gcs-documents.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        if row.get("kind") == "status":
            row["received_at_monotonic"] = row["t"] + 1000.0
        elif row.get("kind") == "finding" and row.get("id") == "f1":
            row["received_at_monotonic"] = 1022.5
    _write(path, rows)
    report = evaluate_run(run, truth=TRUTH)
    finding = report["findings"][0]
    assert finding["latency_s"] == pytest.approx(2.5)
    assert finding["latency_method"] == "ground_clock_aligned"
    assert report["timing_alignment"]["method"] == "status_median_offset"


def test_cli_evaluate_writes_reports_and_exit_code(tmp_path, capsys):
    stem = tmp_path / "out" / "run10"
    assert main(["--evaluate", str(FIXTURES / "run10"), "--report", str(stem)]) == 1
    data = json.loads(stem.with_suffix(".json").read_text(encoding="utf-8"))
    assert data["verdict"] == "FAIL" and data["count"]["reported"] == 1
    markdown = stem.with_suffix(".md").read_text(encoding="utf-8")
    assert "E2E 操作员验收" in markdown and "budget_exhausted" in markdown
    assert '"verdict": "FAIL"' in capsys.readouterr().out


def test_sitl_lock_atomic_and_releases(tmp_path):
    lock = SitlLock("unit", tmp_path / "sitl.lock")
    lock.acquire(wait_s=0)
    assert (tmp_path / "sitl.lock" / "owner").read_text(encoding="utf-8").startswith("unit ")
    with pytest.raises(TimeoutError):
        SitlLock("other", tmp_path / "sitl.lock").acquire(wait_s=0)
    lock.release()
    assert not (tmp_path / "sitl.lock").exists()


def test_non_count_task_validation_uses_alert_precision_recall():
    kind, metrics, criteria = _task_validation(
        ["monitor", "return_home"],
        {"alerts": ["s1:alert:backpack@1,2", "s1:alert:unknown@3,4"]},
        [],
        {
            "targets": [
                {"alert_member": True, "label": "backpack"},
            ]
        },
        10.0,
    )
    assert kind == "alerts"
    assert metrics["precision"] == pytest.approx(0.5)
    assert metrics["recall"] == pytest.approx(1.0)
    assert criteria["alerts_match_truth"] is False


def test_non_count_inspection_task_validation_matches_label_and_position():
    kind, metrics, criteria = _task_validation(
        ["inspect", "return_home"],
        {},
        [{"arrived_at_ground": True, "label": "mailbox", "n": 4.0, "e": 5.0}],
        {
            "targets": [
                {
                    "inspection_member": True,
                    "label": "mailbox",
                    "pose": {"y_north_m": 4.5, "x_east_m": 5.0},
                },
            ]
        },
        10.0,
    )
    assert kind == "inspection"
    assert metrics["matched_inspections"] == 1
    assert criteria["inspection_matches_truth"] is True
