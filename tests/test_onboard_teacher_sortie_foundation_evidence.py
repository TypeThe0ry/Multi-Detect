"""Reopen retained stage evidence; these tests never establish live authority."""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "docs/evidence/onboard-teacher-sortie-foundation-20261008-r1.json"
INDEX_KEY = "onboard_teacher_sortie_foundation_20261008_r1"


def read(path):
    return json.loads(path.read_bytes())


def check_binding(binding):
    path = (ROOT / binding["path"]).resolve(strict=True)
    assert path.is_relative_to(ROOT)
    raw = path.read_bytes()
    assert len(raw) == binding["bytes"]
    assert hashlib.sha256(raw).hexdigest() == binding["sha256"]
    return path


def test_stage_sources_artifacts_and_living_index_match_actual_bytes():
    stage = read(STAGE)
    paths = set()
    for binding in (*stage["sources"], *stage["retained_artifact_bindings"], *stage["protected"]):
        path = check_binding(binding)
        assert path not in paths
        paths.add(path)
    entry = read(ROOT / "docs/current-progress.json")[INDEX_KEY]
    assert check_binding(entry["evidence"]) == STAGE
    assert entry["claims"] == stage["claims"]
    assert entry["validation"] == stage["validation"]


def test_five_original_refusals_and_original_source_copies_remain_refusals():
    stage = read(STAGE)
    trials = stage["validation"]["original_no_ARM_trials"]
    assert [trial["trial"] for trial in trials] == [1, 2, 3, 4, 5]
    expected = (
        "packet stale/future/not ready",
        "exact whole-file EvidenceBinding",
        "HOME/reference changed",
        "stabilization wait exhausted",
        "checkpoint expired/future",
    )
    for trial, fragment in zip(trials, expected, strict=True):
        worker = read(check_binding(trial["report"]))
        supervisor = read(check_binding(trial["supervisor"]))
        assert worker["status"] == trial["worker_status"] == "checkpoint-rejected"
        assert fragment in worker["error"] == trial["error"]
        assert not worker["checkpoint_qualified_while_session_open"]
        assert all(value is False for value in worker["claims"].values())
        assert supervisor["child"]["native_exit_code"] == trial["native_exit_code"] == 1
        assert supervisor["child"]["owned_tree_empty"] is True
        assert supervisor["child"]["watchdog_fired"] is False
        assert supervisor["independent_cleanup"]["verified"] is True
        assert supervisor["sources_before"] == supervisor["sources_after"]
        raw = worker["raw_telemetry"]
        assert raw["packet_attempt_count"] == trial["raw"]
        assert raw["accepted_source_count"] == trial["accepted"]
        assert raw["foreign_source_count"] == trial["foreign"]
        assert raw["orphan_packet_count"] == trial["orphan"] == 0
        assert trial["raw"] == trial["accepted"] + trial["foreign"]
        for binding in trial["source_snapshots"]:
            snapshot = check_binding(binding)
            before = next(
                row for row in supervisor["sources_before"] if row["path"].endswith(snapshot.name)
            )
            assert before["bytes"] == binding["bytes"]
            assert before["sha256"] == binding["sha256"]


def test_cpu_components_and_metadata_queries_are_not_flight_or_training():
    stage = read(STAGE)
    assert stage["release_label"] == "0.3.0-pre.8"
    assert stage["default_policy"] == "r33"
    assert all(value is False for value in stage["claims"].values())
    assert stage["static_route_execution_ready"] is False
    assert stage["no_ARM_probe_proves_complete_sortie_enforcement"] is False
    assert stage["research"]["weights_downloaded"] is False
    assert stage["research"]["GPU_run"] is False
    assert stage["research"]["checkpoint_verification"] == "HEAD-only, no body SHA"
    validation = stage["validation"]
    assert validation["root_combined_CPU_native_exit_code"] == 0
    assert validation["root_combined_CPU_tests"] >= 100
    assert validation["source_inventory_complete_dependency_attestation"] is False


def test_living_documents_link_current_stage_and_preserve_previous_stage():
    link = "onboard-teacher-sortie-foundation-20261008.md"
    for relative in (
        "README.md",
        "docs/current-progress.md",
        "docs/product-goals-and-roadmap.md",
        "docs/implementation-roadmap.md",
    ):
        assert link in (ROOT / relative).read_text(encoding="utf-8")
    assert "onboard-teacher-live-checkpoint-and-spawn-20261008.md" in (
        ROOT / "README.md"
    ).read_text(encoding="utf-8")
    assert "onboard-pretrained-action-research-20261008-r4.md" in (ROOT / "docs" / link).read_text(
        encoding="utf-8"
    )
