import json
from pathlib import Path

from scripts.realworld.x1_fw_stability_report import main, markdown, summarize

FIXTURES = Path(__file__).parent / "fixtures" / "x1_fw_stability"


def test_report_aggregates_valid_flights_and_keeps_voids_separate():
    report = summarize(FIXTURES)
    alpha, beta = report["sites"]
    assert alpha["valid_flights"] == 2
    assert alpha["accepted"] == 1
    assert alpha["all_gates_pass"] == 1
    assert alpha["turn_p95_deg"] == {"min": 2.0, "median": 4.0, "max": 6.0}
    assert alpha["cruise_lock_rate"] == {"min": 0.98, "median": 0.98, "max": 0.98}
    assert alpha["reachable_coverage"]["median"] == 0.9
    assert alpha["horizon_max_deg"]["max"] == 3.4
    assert alpha["hover_p95_deg"]["median"] == 2.3
    assert beta["valid_flights"] == 1
    assert beta["invalid_or_voided_rounds"] == 1
    assert beta["voided_reasons"] == {"gpu_lease_busy": 1}


def test_filters_since_site_and_tag_prefix():
    report = summarize(FIXTURES, since="2026-11-21T00:00:00+00:00", sites=["site_alpha"], tag_prefix="flight-b")
    assert len(report["sites"]) == 1
    assert report["sites"][0]["site"] == "site_alpha"
    assert report["sites"][0]["valid_flights"] == 1
    assert report["sites"][0]["accepted"] == 0


def test_markdown_contains_per_site_summary():
    output = markdown(summarize(FIXTURES))
    assert "| site_alpha |" in output
    assert "turn_p95_deg" in output
    assert "gpu_lease_busy" in output


def test_voided_fixed_wing_round_without_metrics_is_counted():
    report = summarize(FIXTURES)
    beta = next(row for row in report["sites"] if row["site"] == "site_beta")
    assert beta["voided_rounds"] == 1
    assert beta["invalid_rounds"] == 0
    assert beta["invalid_or_voided_rounds"] == 1


def test_cli_writes_markdown_and_json(tmp_path):
    md_path = tmp_path / "reports" / "summary.md"
    json_path = tmp_path / "reports" / "summary.json"
    assert main(["--root", str(FIXTURES), "--markdown-out", str(md_path),
                 "--json-out", str(json_path)]) == 0
    assert "site_alpha" in md_path.read_text(encoding="utf-8")
    assert json.loads(json_path.read_text(encoding="utf-8"))["sites"][0]["site"] == "site_alpha"


def test_mixed_receipts_and_tag_fallback(tmp_path):
    batch = tmp_path / "rw_example__noon" / "batch-a"
    batch.mkdir(parents=True)
    rows = [
        {"valid_round": True, "accepted": True},
        {"voided_reason": "hover-only"},
        {"inputs": {"gimbal_eval_fw": True}, "voided_reason": "load", "valid_round": True},
        {"inputs": {"gimbal_eval_fw": True}, "valid_round": False},
        {"inputs": {"gimbal_eval_fw": True, "eval_tag": "fw-repeat"}, "valid_round": True,
         "started_at": "2026-11-22T01:00:00+01:00", "accepted": True, "errors": [],
         "track_diagnostics": {"passed": False, "hold": {
             "fw": {"passed": True}, "overall": {"pointing_error_deg": {"p95": 0}}}},
         "horizon_acceptance": {"passed": True}},
    ]
    (batch / "results.jsonl").write_text("\n".join(map(json.dumps, rows)) + "\n{", encoding="utf-8")
    report = summarize(tmp_path)
    assert report["malformed_rows"] == 1
    site, = report["sites"]
    assert site["site"] == "rw_example"
    assert site["valid_flights"] == site["accepted"] == 1
    assert site["all_gates_pass"] == 0
    assert site["hover_p95_deg"]["median"] == 0
    assert site["turn_p95_deg"]["median"] is None
    assert site["invalid_rounds"] == 1
    assert site["voided_reasons"] == {"load": 1}
    assert summarize(tmp_path, tag_prefix="batch-")["sites"][0]["valid_flights"] == 0
    hash_batch = tmp_path / "rw_hash-site" / "batch-0123456789abcdefabcd"
    hash_batch.mkdir(parents=True)
    hashed = {"inputs": {"gimbal_eval_fw": True}, "valid_round": True}
    (hash_batch / "results.jsonl").write_text(json.dumps(hashed) + "\n", encoding="utf-8")
    assert summarize(tmp_path, tag_prefix="01234567")["sites"][-1]["valid_flights"] == 1
    assert summarize(tmp_path, since="2026-11-22", tag_prefix="fw-")["sites"][0]["valid_flights"] == 1
