"""CPU synthetic terminal fixtures plus one bounded real README byte check.

No real campaign audit, weights, live WAL, processor, or model is executed.
The frozen v1 fixture is reused without changing its source or assertions.
"""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import audit_onboard_navigation_recovery267_terminal_v2 as audit

audit.authenticate_legacy()
SPEC = importlib.util.spec_from_file_location(
    "_terminal_v1_fixtures", audit.ROOT / audit.LEGACY_TEST
)
legacy_tests = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(legacy_tests)
case = legacy_tests.case
legacy = legacy_tests.audit
README_FIXTURE = (
    b"---\nlibrary_name: peft\n---\n# Synthetic PEFT modelcard, not training evidence\n"
)


@pytest.fixture
def v2_case(case, monkeypatch):
    # Synthetic bytes have an explicitly fixture-only identity. The production
    # constants remain pinned to the independently observed real r2 README.
    monkeypatch.setattr(audit, "README_BYTES", len(README_FIXTURE))
    monkeypatch.setattr(audit, "README_SHA256", hashlib.sha256(README_FIXTURE).hexdigest())
    monkeypatch.setattr(audit, "load_legacy", lambda: legacy)
    monkeypatch.setattr(legacy_tests, "run_audit", lambda value: run_audit(value))
    for step in (*range(8, 265, 8), 267):
        name = f"checkpoint-{step:03d}" if step != 267 else "adapter"
        (case["run"] / "train" / name / "README.md").write_bytes(README_FIXTURE)
    return case


def run_audit(value):
    return audit.audit_terminal(value["plan_path"], value["sha"])


def cli_args(value, output):
    return [
        "--plan",
        str(value["plan_path"]),
        "--plan-sha256",
        value["sha"],
        "--output-dir",
        str(output),
    ]


def test_five_file_synthetic_campaign_keeps_all_terminal_gates(v2_case):
    original = legacy.check_snapshots
    report = run_audit(v2_case)
    assert legacy.check_snapshots is original
    assert report["frozen_v1_snapshot_function_restored"] is True
    assert report["contract"] == audit.CONTRACT
    assert report["inherited_audit_contract"] == legacy.CONTRACT
    assert report["snapshot_count"] == report["peft_readme_count"] == 34
    assert report["periodic_snapshot_count"] == 33
    assert report["journal"]["event_count"] == 1068
    assert report["draw_event_count"] == 534
    assert report["final_adapter_parameter_sha256"] == v2_case["digest"]
    assert report["raw_FP32_tensor_count"] == 112
    assert report["candidate_reload_all86_raw_tokens_actor_prefix_exact"] is True
    assert report["raw_diagnostics_independently_rederived"] is True
    assert report["actual_tool_exit_status_independently_verified"] is False
    assert report["actual_process_exit_independently_observed"] is False
    assert report["peft_readme_is_parameter_or_quality_evidence"] is False
    assert report["peft_readme_bindings_rechecked_after_audit"] is True
    assert report["audit_source_bindings_unchanged"] is True
    assert len(report["audit_sources"]) == 4
    assert all(report[key] is False for key in legacy.FALSE_CLAIMS)
    assert [row["completed_new_updates"] for row in report["snapshots"]] == [
        *range(8, 265, 8),
        267,
    ]
    for row in report["peft_readme_bindings"]:
        assert row in report["bindings"]
        assert row == legacy.binding(row["path"])
    for snapshot in report["snapshots"]:
        receipt = legacy.read_bound(snapshot["receipt"])
        assert "peft_readme" not in receipt  # No invented historical receipt field.
    # The frozen v1 remains exact4 and still rejects these real-style sidecars.
    with pytest.raises(ValueError, match="Snapshot files"):
        legacy.audit_terminal(v2_case["plan_path"], v2_case["sha"])


@pytest.mark.parametrize("name", ("checkpoint-008", "adapter"))
@pytest.mark.parametrize("mode", ("missing", "extra", "changed", "oversized"))
def test_readme_and_file_set_fail_closed(v2_case, name, mode):
    directory = v2_case["run"] / "train" / name
    readme = directory / "README.md"
    if mode == "missing":
        readme.unlink()
    elif mode == "extra":
        (directory / "unknown-sidecar.txt").write_bytes(b"not admitted")
    elif mode == "changed":
        readme.write_bytes(README_FIXTURE.replace(b"Synthetic", b"Corrupted"))
    else:
        readme.write_bytes(b"x" * (audit.README_LIMIT + 1))
    original = legacy.check_snapshots
    output = (
        v2_case["root"] / "artifacts/diagnostics/onboard-navigation-recovery267-terminal-v2-bad"
    )
    assert audit.main(cli_args(v2_case, output)) == 1
    assert not output.exists()
    assert legacy.check_snapshots is original


def test_readme_changed_during_audit_is_caught_by_inherited_last_recheck(v2_case, monkeypatch):
    original = legacy.rederive_diagnostics
    path = v2_case["run"] / "train/adapter/README.md"

    def mutate_after_snapshots(*args):
        result = original(*args)
        path.write_bytes(README_FIXTURE.replace(b"Synthetic", b"Corrupted"))
        return result

    monkeypatch.setattr(legacy, "rederive_diagnostics", mutate_after_snapshots)
    with pytest.raises(ValueError, match="Audited output changed"):
        run_audit(v2_case)


@pytest.mark.parametrize("mode", ("missing-train", "missing-reload", "active", "unfinished"))
def test_incomplete_phase_precedes_any_wal_read_or_output(v2_case, monkeypatch, mode):
    if mode.startswith("missing-"):
        (v2_case["run"] / mode.removeprefix("missing-") / "report.json").unlink()
    elif mode == "active":
        (v2_case["run"] / "active-phase.lock").touch()
    else:
        legacy_tests.modify_report(v2_case, "train", "native_exit_code", 1)
    original_read = Path.read_bytes

    def guarded(path):
        assert path.name not in {
            "training-events.jsonl",
            "draw-events.jsonl",
            "training-events-prefix.jsonl",
            "README.md",
        }
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", guarded)
    output = (
        v2_case["root"] / "artifacts/diagnostics/onboard-navigation-recovery267-terminal-v2-pending"
    )
    original = legacy.check_snapshots
    assert audit.main(cli_args(v2_case, output)) == 1
    assert not output.exists()
    assert legacy.check_snapshots is original


@pytest.mark.parametrize(
    "mode", ("missing", "extra", "wrong-prefix", "bad-weight", "wrong-receipt")
)
def test_original_snapshot_parameter_receipt_and_prefix_gates(v2_case, mode):
    legacy_tests.test_snapshot_actual_byte_gates(v2_case, mode)


@pytest.mark.parametrize("mode", ("wal-order", "wal-truncated", "draw-error", "draw-type"))
def test_original_full_wal_and_draw_event_gates(v2_case, mode):
    legacy_tests.test_complete_wal_and_draw_events(v2_case, mode)


@pytest.mark.parametrize(
    "mode", ("token", "raw", "prefix", "actor", "id", "diagnostic", "cap", "bool-token")
)
def test_original_all86_raw_token_actor_prefix_and_diagnostic_gates(v2_case, mode):
    legacy_tests.test_all86_reload_exactness(v2_case, mode)


@pytest.mark.parametrize(
    "phase,key,value",
    (
        ("train", "optimizer_steps_attempted", 266),
        ("train", "optimizer_update_uncertain", True),
        ("train", "cumulative_campaign_attempts", 267),
        ("train", "fresh_optimizer_initial_state_empty", False),
        ("reload", "native_exit_code", False),
        ("reload", "optimizer_performed", True),
        ("reload", "closed_sample_accesses", 85),
        ("reload", "started_at_utc", "2026-01-01T00:02:30+00:00"),
        ("reload", "adapter_parameter_sha256_before", "0" * 64),
        ("train", "quality_claim", True),
    ),
)
def test_original_budget_source_chronology_and_load_gates(v2_case, phase, key, value):
    legacy_tests.test_phase_and_parameter_identity_failures(v2_case, phase, key, value)


def test_original_current_small_source_drift_gate(v2_case):
    legacy_tests.test_source_arrays_and_current_small_drift(v2_case)


def test_original_stored_raw_summary_must_match_independently_rederived_diagnostics(v2_case):
    report = v2_case["reports"]["train"]
    report["train_resubstitution_metrics_not_holdout"]["new_candidate"] = {
        "sample_count": 86,
        "resubstitution_only": True,
        "quality_claim": False,
        "strict_contract_valid": 86,
    }
    legacy_tests.write_json(v2_case["run"] / "train/report.json", report)
    with pytest.raises(ValueError, match="independently diagnosed"):
        run_audit(v2_case)


def test_audit_implementation_drift_prevents_success(v2_case, monkeypatch):
    original = audit.audit_sources
    calls = []

    def changed_sources():
        rows = original()
        calls.append(True)
        if len(calls) > 1:
            rows[0] = {**rows[0], "sha256": "0" * 64}
        return rows

    monkeypatch.setattr(audit, "audit_sources", changed_sources)
    with pytest.raises(ValueError, match="audit source changed"):
        run_audit(v2_case)


def test_v2_cli_create_only_and_explicit_prefix(v2_case):
    output = (
        v2_case["root"] / "artifacts/diagnostics/onboard-navigation-recovery267-terminal-v2-fixture"
    )
    args = cli_args(v2_case, output)
    assert audit.main(args) == 0
    before = (output / "report.json").read_bytes()
    assert legacy.strict_json(before)["contract"] == audit.CONTRACT
    assert audit.main(args) == 1
    assert (output / "report.json").read_bytes() == before
    for name in ("onboard-navigation-recovery267-terminal-fixture", "not-terminal"):
        with pytest.raises(ValueError, match="v2 terminal diagnostics prefix"):
            audit.output_target(output.parent / name)


def test_legacy_authenticated_before_import_and_kept_byte_identical():
    before = audit.authenticate_legacy()
    loaded = audit.load_legacy()
    assert loaded.CONTRACT == legacy.CONTRACT
    assert loaded is not legacy
    assert loaded.check_snapshots is not legacy.check_snapshots
    assert audit.authenticate_legacy() == before
    sources = audit.audit_sources()
    assert len(sources) == 4
    assert {Path(row["path"]).name for row in sources} == {
        "audit_onboard_navigation_recovery267_terminal.py",
        "audit_onboard_navigation_recovery267_terminal_v2.py",
        "test_audit_onboard_navigation_recovery267_terminal.py",
        "test_audit_onboard_navigation_recovery267_terminal_v2.py",
    }


@pytest.mark.parametrize("name", (audit.LEGACY_SOURCE, audit.LEGACY_TEST))
def test_legacy_tamper_is_rejected_before_any_python_execution(tmp_path, monkeypatch, name):
    root = audit.ROOT
    for relative in audit.LEGACY_IDENTITIES:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        data = (root / relative).read_bytes()
        path.write_bytes(data + b"# tampered\n" if relative == name else data)
    monkeypatch.setattr(audit, "ROOT", tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("Import must not be attempted before SHA admission")

    monkeypatch.setattr(importlib.util, "spec_from_file_location", forbidden)
    with pytest.raises(ValueError, match="Frozen v1 audit identity"):
        audit.load_legacy()


def test_observed_real_readme_small_bytes_style_only():
    path = (
        audit.ROOT
        / "artifacts/models/onboard-navigation-recovery-20261008-r2/train/adapter/README.md"
    )
    if not path.is_file():
        pytest.skip("Real runtime PEFT sidecar is not distributed with a clean source clone")
    assert path.stat().st_size == audit.README_BYTES == 5438
    data = path.read_bytes()  # Only this small README, never weights or WAL.
    assert hashlib.sha256(data).hexdigest() == audit.README_SHA256
    text = data.decode("utf-8")
    assert text.startswith("---\n")
    assert "library_name: peft\n" in text
    assert "pipeline_tag: text-generation\n" in text
    assert "# Model Card for Model ID" in text
    assert "PEFT 0.17.1" in text
    assert path.read_bytes() == data


def test_stdlib_only_import_and_authenticated_loader_in_isolated_no_site_python():
    path = Path(audit.__file__).resolve()
    code = (
        "import importlib.util,sys; "
        f"s=importlib.util.spec_from_file_location('terminal_v2',{str(path)!r}); "
        "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); m.load_legacy(); "
        "blocked={'torch','transformers','peft','accelerate','numpy','PIL','requests','pyarrow'}; "
        "assert not blocked.intersection(sys.modules); print('authenticated stdlib-only')"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "authenticated stdlib-only" in result.stdout
