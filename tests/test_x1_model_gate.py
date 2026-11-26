"""CPU contract tests for the gate wrapper and its strict default profile."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import x1_model_gate as gate
from scripts.realworld.x1_lock import flight_lock


def test_strict_profile_no_vacuous_pass_and_deadline_boundary():
    thresholds = gate.load_profile(gate.PROFILE)["thresholds"]
    assert gate.evaluate("schema_validity", {"schema_valid": 3, "turns_sent": 3}, thresholds)["verdict"] == "PASS"
    assert gate.evaluate("identity_echo", {"identity_valid": 9, "turns_sent": 10}, thresholds)["verdict"] == "FAIL"
    assert gate.evaluate("unnecessary_actions", {}, thresholds)["verdict"] == "FAIL"
    assert gate.evaluate("unnecessary_actions", {"quiet_ticks": 2}, thresholds)["verdict"] == "PASS"
    for on_time, expected in [(99, "FAIL"), (100, "PASS")]:
        result = gate.evaluate("deadline_miss_rate", {"deadline_missed": 1, "deadline_on_time": on_time}, thresholds)
        assert result["verdict"] == expected


def test_profile_is_configurable_and_rejects_invalid_thresholds(tmp_path):
    path = tmp_path / "gate.json"
    profile = gate.load_profile(gate.PROFILE)
    profile["thresholds"]["event_tool_first"]["min"] = 0.9
    gate.write_json(path, profile)
    thresholds = gate.load_profile(path)["thresholds"]
    result = gate.evaluate("event_tool_first", {"event_tool_first_required": 10,
                                               "event_tool_first_compliant": 9}, thresholds)
    assert result["verdict"] == "PASS"
    profile["thresholds"]["event_tool_first"]["min"] = True
    gate.write_json(path, profile)
    with pytest.raises(ValueError, match="event_tool_first"):
        gate.load_profile(path)


def test_cli_rejects_invalid_inputs(tmp_path):
    gguf = tmp_path / "fake.gguf"
    gguf.write_bytes(b"GGUF example")
    for args in (["--label", "../../escape"], ["--cuda-device", "-1"], ["--port", "65536"],
                 ["--sha256", "wrong"], ["--baseline", str(tmp_path)]):
        with pytest.raises(SystemExit) as exc:
            gate.parse_args([str(gguf), *args])
        assert exc.value.code == 2
    parsed = gate.parse_args([str(gguf), "--cuda-device", "0", "--label", "seed-31.1"])
    assert parsed.label == "seed-31.1" and parsed.cuda_device == 0 and parsed.gguf == gguf


def fixture_summary(*, evidence=False):
    good = {"turns_sent": 2, "schema_valid": 2, "identity_valid": 2, "deadline_on_time": 2,
            "event_tool_first_required": 1, "event_tool_first_compliant": 1, "retasks_proposed": 1,
            "retasks_structure_valid": 1, "retasks_timing_valid": 1, "retasks_timing_checked": 1,
            "battery_structure_checked": 1, "battery_structure_valid": 1,
            "turn_latency_ms_histogram": {"le_100": 2, "over_5000": 0}}
    cases = [{"case": {"id": case.id}, "metrics": dict(good), "checks": {"executor_effect": True},
              "status": "pass", "observed": {"quiet_ticks": 1, "unnecessary_actions": 0}}
             for case in gate.CASES]
    link = next(row for row in cases if row["case"]["id"] == "mc-link-loss")
    link["metrics"].update(event_tool_first_required=0, event_tool_first_compliant=0,
                           event_tool_first_eligible=1, event_tool_first_oracle_evaluated=1)
    if evidence:
        link.update(status="fail", checks={"executor_effect": False})
        link["metrics"]["evidence_scope_rejections"] = 1
    return {"cases": cases, "metrics": {**good, "evidence_scope_rejections": int(evidence)}, "error": None}


def test_evidence_veto_leaves_model_metrics_intact_but_fails_flight_check():
    metrics, cases, _, _ = gate.evaluate_suite(fixture_summary(evidence=True), gate.load_profile(gate.PROFILE))
    assert {row["verdict"] for row in metrics.values()} == {"PASS"}
    link = next(row for row in cases if row["case_id"] == "mc-link-loss")
    assert link["verdict"] == "FAIL" and link["evidence_scope_rejections"] == 1
    assert link["model_checks"]["event_tool_first"]["applicability"] == "oracle_did_not_require_tool"
    base = {"report_version": 1, "verdict": "PASS", "metrics": metrics,
            "evidence_scope_rejections": 0, "cases": [{"case_id": link["case_id"], "verdict": "PASS"}]}
    baseline = gate.compare(base, metrics, cases, 1, Path("/baseline"))
    assert baseline["cases"][link["case_id"]] == {"baseline": "PASS", "current": "FAIL"}
    assert baseline["metrics"]["identity_echo"]["delta"] == 0
    assert baseline["current_evidence_scope_rejections"] == 1


def test_fake_stages_publish_and_baseline(tmp_path, monkeypatch):
    fake_lock = tmp_path / "host.lock"
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    checkpoint = tmp_path / "fake.gguf"
    checkpoint.write_bytes(b"GGUF example")
    sha = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    (tmp_path / "SHA256SUMS").write_text(f"{sha}  {checkpoint.name}\n")
    out = tmp_path / "stable"
    args = gate.parse_args([str(checkpoint), "--output-root", str(out), "--label", "first"])
    stages = []

    def fake_stage(args, run_dir, stage, lock_fd):
        stages.append(stage)
        assert lock_fd >= 0
        # Another real open-file description cannot start a shared flight.
        with pytest.raises(BlockingIOError):
            with flight_lock(fake_lock, shared=True, wait=0):
                pytest.fail("gate did not hold exclusive host lock")
        (run_dir / stage).mkdir()
        if stage == "live":
            evidence = args.label == "second"
            gate.write_json(run_dir / "live/summary.json", fixture_summary(evidence=evidence))
            rows = ([{"failure_scope": "evidence_scope", "messages": []},
                     {"failure_scope": "model", "messages": []}] if evidence else [])
            (run_dir / "live/failures.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
            return {"exit_status": int(evidence)}
        return {"exit_status": 0}

    assert gate.run_gate(args, run=fake_stage, lock_path=fake_lock) == 0
    args = gate.parse_args([str(checkpoint), "--output-root", str(out), "--label", "second",
                           "--baseline", str(out / "first")])
    assert gate.run_gate(args, run=fake_stage, lock_path=fake_lock) == 1
    assert stages == ["preflight", "live", "preflight", "live"]
    report = gate.read_json(out / "second/gate.json")
    assert report["baseline"]["baseline_verdict"] == "PASS" and report["verdict"] == "FAIL"
    assert report["provenance"]["sha256"] == sha and report["provenance"]["sha256_verified"]
    assert "基线比较" in (out / "second/gate.md").read_text()
    assert len((out / "second/failures.jsonl").read_text().splitlines()) == 2
    assert gate.read_json(out / "second/model-failures.jsonl")["failure_scope"] == "model"
    assert gate.read_json(out / "second/evidence-scope-rejections.jsonl")["failure_scope"] == "evidence_scope"
    with pytest.raises(FileExistsError):
        gate.run_gate(args, run=fake_stage, lock_path=fake_lock)


def test_preflight_failure_bad_sha_and_held_lock_skip_live(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    gguf = tmp_path / "fake.gguf"
    gguf.write_bytes(b"GGUF example")
    out, lock = tmp_path / "stable", tmp_path / "host.lock"
    calls = []

    def fake_stage(args, run_dir, stage, lock_fd):
        calls.append(stage)
        return {"exit_status": 1}

    def run(label, *extra):
        args = gate.parse_args([str(gguf), "--output-root", str(out), "--label", label, *extra])
        return gate.run_gate(args, run=fake_stage, lock_path=lock)

    assert run("bad-digest", "--sha256", "0" * 64) == 1
    with flight_lock(lock, shared=True):
        assert run("busy") == 75
    assert not calls
    assert run("bad-preflight") == 1
    assert calls == ["preflight"]
    assert gate.read_json(out / "bad-preflight/gate.json")["error"] == "plan-only preflight failed"


def test_stage_command_preserves_admission_environment(tmp_path, monkeypatch):
    checkpoint = tmp_path / "fake.gguf"
    checkpoint.write_bytes(b"GGUF example")
    args = gate.parse_args([str(checkpoint), "--cuda-device", "2", "--port", "9970"])
    monkeypatch.setenv("RTF_MATRIX_START_LOAD_MAX", "70")
    calls = []

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(pid=11111, wait=lambda: 0)

    monkeypatch.setattr(gate.subprocess, "Popen", fake_popen)
    gate.run_stage(args, tmp_path, "preflight", 12)
    gate.run_stage(args, tmp_path, "live", 12)
    preflight, live = (call[0] for call in calls)
    assert "--plan-only" in preflight and "--plan-only" not in live
    for command, kwargs in calls:
        assert command[command.index("--cuda-device") + 1] == "2"
        env = kwargs["env"]
        assert env["RTF_MATRIX_START_LOAD_MAX"] == "70" and env[gate.FLIGHT_LOCK_FD_ENV] == "12"
        assert kwargs["pass_fds"] == (12,)
