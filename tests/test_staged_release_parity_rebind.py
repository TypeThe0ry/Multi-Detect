from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import finalize_onboard_training_progress as finisher
from scripts import rebind_current_release_parity_r11 as rebind


def put(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(rebind, "ROOT", tmp_path)
    monkeypatch.setattr(rebind, "PARITY_PATH", tmp_path / "configs/release-parity.json")
    monkeypatch.setattr(rebind, "PROGRESS_PATH", tmp_path / "docs/current-progress.json")
    monkeypatch.setattr(finisher, "ROOT", tmp_path)
    source = tmp_path / "src/core.py"
    source.parent.mkdir()
    source.write_text("new source\n")
    put(
        rebind.PARITY_PATH,
        {
            "source_files": {"core_algorithm": ["src/core.py"]},
            "parity": {"core_algorithm_hash": "new-core"},
            "release_label": "development",
        },
    )
    put(rebind.PROGRESS_PATH, {"release_parity": {"hashes": {"core_algorithm_hash": "old"}}})
    return tmp_path


def generate(root: Path) -> dict:
    rebind.main(["--output", "docs/staged.json", "--artifact-directory", "artifacts/audit"])
    return rebind.load(root / "docs/staged.json")


@pytest.mark.parametrize("payload", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}'])
def test_strict_current_json_rejects_ambiguous_or_nonfinite(payload):
    with pytest.raises(ValueError):
        rebind.decode(payload, Path("current.json"))


def test_bom_decode_does_not_change_original_bytes(tmp_path):
    path = tmp_path / "bom.json"
    payload = b'\xef\xbb\xbf{"x":1}'
    path.write_bytes(payload)
    assert rebind.load(path) == {"x": 1}
    assert path.read_bytes() == payload


def test_top_level_hash_update_is_old_identity_limited():
    parity = {"hashes": {"core_algorithm_hash": "new"}, "bytes": 3, "file_sha256": "file"}
    old = {"core_algorithm_hash": "old"}
    assert rebind.rebind_value(old, parity, old_hashes=old)["core_algorithm_hash"] == "new"
    unrelated = {"core_algorithm_hash": "model-identity", "source_sha256": "old"}
    assert rebind.rebind_value(unrelated, parity, old_hashes=old) == unrelated


def test_generate_is_scoped_child_first_and_retains_sources(repo):
    child = put(repo / "artifacts/child.json", {"release_parity_core_algorithm_hash": "old"})
    child_hash = rebind.sha256(child)
    parent = put(
        repo / "artifacts/parent.json",
        {"source_evidence": "artifacts/child.json", "source_evidence_sha256": child_hash},
    )
    raw = put(repo / "artifacts/raw-gate.json", {"parity": {"core_algorithm_hash": "old"}})
    progress = rebind.load(rebind.PROGRESS_PATH)
    progress[rebind.CURRENT_KEYS[0]] = {
        "evidence": "artifacts/parent.json",
        "evidence_sha256": rebind.sha256(parent),
    }
    progress["evidence"] = {
        "release_gate_report": {"local_path": "artifacts/raw-gate.json"},
        "unrelated_history": {"local_path": "artifacts/child.json"},
    }
    put(rebind.PROGRESS_PATH, progress)
    before = {p: p.read_bytes() for p in [child, parent, raw, rebind.PROGRESS_PATH]}
    staged = generate(repo)
    parent_copy = rebind.load(repo / staged[rebind.CURRENT_KEYS[0]]["evidence"])
    child_copy = repo / parent_copy["source_evidence"]
    assert parent_copy["source_evidence_sha256"] == rebind.sha256(child_copy)
    assert parent_copy["retained_audit_source"]["path"] == "artifacts/parent.json"
    assert parent_copy["runtime_source_parity_verified"] is False
    assert parent_copy["external_sim_rerun_performed"] is False
    assert staged["evidence"] == progress["evidence"]
    assert all(p.read_bytes() == b for p, b in before.items())
    assert len(list((repo / "artifacts/audit").glob("*.json"))) == 2


def test_cycle_rejected_before_any_copy_is_written(repo):
    put(
        repo / "artifacts/a.json",
        {"next": "artifacts/b.json", "next_sha256": "bound-b", "core_algorithm_hash": "old"},
    )
    put(
        repo / "artifacts/b.json",
        {"next": "artifacts/a.json", "next_sha256": "bound-a", "core_algorithm_hash": "old"},
    )
    p = rebind.load(rebind.PROGRESS_PATH)
    p[rebind.CURRENT_KEYS[0]] = {"evidence": "artifacts/a.json"}
    put(rebind.PROGRESS_PATH, p)
    with pytest.raises(ValueError, match="cycle"):
        generate(repo)
    assert not (repo / "artifacts/audit").exists()
    assert not (repo / "docs/staged.json").exists()


def test_compact_targets_distinguish_same_named_sources(repo):
    a = put(repo / "artifacts/a/" / ("x" * 180 + ".json"), {})
    b = put(repo / "artifacts/b/" / ("x" * 180 + ".json"), {})
    target = rebind.target_for(a, set(), repo / "artifacts/audit")
    other = rebind.target_for(b, {target}, repo / "artifacts/audit")
    assert target != other
    assert len(target.name) < 60


def test_generic_local_path_and_canonical_bindings(repo):
    doc = {"x": 1}
    doc["sha256"] = rebind.canonical(doc)
    path = put(repo / "artifacts/copy.json", doc)
    m = {"artifacts/original.json": "artifacts/copy.json"}
    result = rebind.refresh_file_bindings(
        {"local_path": m["artifacts/original.json"], "file_bytes": 0, "file_sha256": "old"}, m
    )
    assert result["file_bytes"] == path.stat().st_size
    assert result["file_sha256"] == rebind.sha256(path)
    result = rebind.refresh_file_bindings(
        {
            "evidence": path.relative_to(repo).as_posix(),
            "evidence_file_sha256": "old",
            "evidence_sha256": "canonical-old",
        },
        m,
    )
    assert result["evidence_sha256"] == doc["sha256"]
    assert result["evidence_file_sha256"] == rebind.sha256(path)


def test_absolute_paths_rebind_and_refresh(repo):
    path = put(repo / "artifacts/copy.json", {})
    mapping = {"artifacts/original.json": "artifacts/copy.json"}
    result = rebind.update_paths(
        {
            "runtime_evidence": str(repo / "artifacts/original.json"),
            "runtime_evidence_sha256": "old",
        },
        mapping,
    )
    result = rebind.refresh_file_bindings(result, mapping)
    assert Path(result["runtime_evidence"]) == path
    assert result["runtime_evidence_sha256"] == rebind.sha256(path)


def test_over_windows_limit_command_stdout_are_never_resolved(repo, monkeypatch):
    path = put(repo / "artifacts/copy.json", {})
    text = "render-command-or-stdout " * 2500
    assert len(text) > 32767
    value = {
        "command": text,
        "stdout": text,
        "nested": [{"command": text, "stdout": text}],
        "local_path": path.relative_to(repo).as_posix(),
        "file_sha256": "old",
    }
    monkeypatch.setattr(Path, "resolve", lambda *a, **k: pytest.fail("unexpected resolve"))
    result = rebind.refresh_file_bindings(value, {"old": "artifacts/copy.json"})
    assert result["command"] == result["stdout"] == text
    assert result["nested"] == value["nested"]
    assert result["file_sha256"] == rebind.sha256(path)


def test_unknown_bound_long_string_does_not_touch_filesystem(repo, monkeypatch):
    value = {"path": "x" * 40000, "sha256": "unrelated-record"}
    monkeypatch.setattr(Path, "resolve", lambda *a, **k: pytest.fail("unexpected resolve"))
    monkeypatch.setattr(Path, "is_file", lambda *a, **k: pytest.fail("unexpected stat"))
    assert rebind.refresh_file_bindings(value, {"old": "artifacts/copy.json"}) == value


def test_real_mapped_file_read_error_is_not_swallowed(repo, monkeypatch):
    path = put(repo / "artifacts/copy.json", {})

    def unreadable(target):
        raise OSError("mapped child read failed")

    monkeypatch.setattr(rebind, "load", unreadable)
    with pytest.raises(OSError, match="mapped child read failed"):
        rebind.refresh_file_bindings(
            {"local_path": str(path), "file_sha256": "old"}, {"old": "artifacts/copy.json"}
        )


def test_write_refuses_even_identical_existing_output(repo):
    path = put(repo / "artifacts/existing.json", {})
    with pytest.raises(FileExistsError):
        rebind.write_json(path, {})


def test_missing_mapped_child_rejected(repo):
    with pytest.raises(ValueError, match="not been written"):
        rebind.refresh_file_bindings(
            {"local_path": "artifacts/missing.json", "file_sha256": "bound"},
            {"old": "artifacts/missing.json"},
        )


def test_actual_runtime_paths_evidence_self_reference_has_no_byte_dependency(repo):
    source = put(
        repo / "artifacts/runtime.json",
        {
            "release_parity_core_algorithm_hash": "old",
            "paths": {
                "evidence": str(repo / "artifacts/runtime.json"),
                "mission_evidence": str(repo / "artifacts/mission.json"),
            },
            "mission": {"path": str(repo / "artifacts/mission.json"), "file_sha256": "bound"},
        },
    )
    mission = put(repo / "artifacts/mission.json", {"core_algorithm_hash": "old"})
    p = rebind.load(rebind.PROGRESS_PATH)
    p[rebind.CURRENT_KEYS[0]] = {"evidence": "artifacts/runtime.json"}
    put(rebind.PROGRESS_PATH, p)
    assert source not in rebind.byte_dependencies(rebind.load(source))
    assert mission in rebind.byte_dependencies(rebind.load(source))
    staged = generate(repo)
    copy_path = repo / staged[rebind.CURRENT_KEYS[0]]["evidence"]
    copied = rebind.load(copy_path)
    assert Path(copied["paths"]["evidence"]) == copy_path
    assert copied["mission"]["file_sha256"] == rebind.sha256(Path(copied["mission"]["path"]))
    assert "evidence_sha256" not in copied["paths"]


def test_pure_path_backlink_is_not_a_hash_cycle(repo):
    a = put(
        repo / "artifacts/a.json", {"core_algorithm_hash": "old", "backlink": "artifacts/b.json"}
    )
    b = put(
        repo / "artifacts/b.json",
        {
            "core_algorithm_hash": "old",
            "source_evidence": "artifacts/a.json",
            "source_evidence_sha256": rebind.sha256(a),
        },
    )
    originals = {a: rebind.load(a), b: rebind.load(b)}
    assert rebind.child_first_order(originals, {a, b}) == [a, b]
    mapping = {
        "artifacts/a.json": "artifacts/a-new.json",
        "artifacts/b.json": "artifacts/b-new.json",
    }
    # A's unbound backlink may point at an unwritten B. No fabricated hash is added.
    copied = rebind.refresh_file_bindings(rebind.update_paths(originals[a], mapping), mapping)
    assert copied["backlink"] == "artifacts/b-new.json"
    assert "backlink_sha256" not in copied


@pytest.mark.parametrize(
    "bound", [{"file_sha256": "hash"}, {"bytes": 123}, {"canonical_sha256": "hash"}]
)
def test_bound_self_reference_remains_a_true_cycle(repo, bound):
    source = repo / "artifacts/self.json"
    put(source, {"reference": {"path": str(source), **bound}})
    with pytest.raises(ValueError, match="cycle"):
        rebind.child_first_order({source: rebind.load(source)}, {source})


def test_malformed_current_dependency_rejected(repo):
    bad = repo / "artifacts/bad.json"
    bad.parent.mkdir()
    bad.write_text("not json")
    p = rebind.load(rebind.PROGRESS_PATH)
    p[rebind.CURRENT_KEYS[0]] = {"evidence": "artifacts/bad.json"}
    put(rebind.PROGRESS_PATH, p)
    with pytest.raises(ValueError):
        generate(repo)
    assert not (repo / "docs/staged.json").exists()


@pytest.mark.parametrize("mode", ["fail", "staged_mutation", "source_mutation", "current_mutation"])
def test_promotion_fails_closed_on_verify_or_identity_change(repo, monkeypatch, mode):
    generate(repo)
    staged = repo / "docs/staged.json"
    before = rebind.PROGRESS_PATH.read_bytes()

    def verify(path):
        if mode == "fail":
            raise ValueError("verification failed")
        target = {
            "staged_mutation": staged,
            "source_mutation": repo / "src/core.py",
            "current_mutation": rebind.PROGRESS_PATH,
        }[mode]
        target.write_bytes(target.read_bytes() + b" ")
        return {"status": "passed"}

    monkeypatch.setattr(rebind, "verify_staged", verify)
    with pytest.raises(ValueError):
        rebind.promote(staged)
    if mode != "current_mutation":
        assert rebind.PROGRESS_PATH.read_bytes() == before
    assert not list((repo / "docs").glob("current-progress-before-*.json"))


def test_promotion_copies_exact_verified_bytes_with_backup(repo, monkeypatch):
    generate(repo)
    staged = repo / "docs/staged.json"
    payload = staged.read_bytes()
    old = rebind.PROGRESS_PATH.read_bytes()
    seen = []
    monkeypatch.setattr(rebind, "verify_staged", lambda p: seen.append(p) or {"status": "passed"})
    result = rebind.promote(staged)
    assert seen == [staged]
    assert rebind.PROGRESS_PATH.read_bytes() == payload
    assert list((repo / "docs").glob("current-progress-before-*.json"))[0].read_bytes() == old
    assert result["sha256"] == hashlib.sha256(payload).hexdigest()


def test_verifier_requires_source_recomputation(repo, monkeypatch):
    from scripts import verify_current_progress_index as verifier

    seen = []
    monkeypatch.setattr(verifier, "verify", lambda *a, **k: seen.append((a, k)) or {})
    rebind.verify_staged(repo / "docs/staged.json")
    assert seen[0][1] == {"check_source_parity": True}


def test_finisher_rejects_authoritative_index_and_bad_tags(repo):
    with pytest.raises(ValueError, match="distinct staged"):
        finisher.main(["--progress", "docs/current-progress.json", "--tag", "history-r1"])
    with pytest.raises(SystemExit):
        finisher.main(["--progress", "docs/current-progress.json", "--tag", "../../escape"])


def test_finisher_staged_compare_before_replace(repo):
    path = put(repo / "docs/final.json", {"old": 1})
    before = path.read_bytes()
    finisher.replace_staged(path, {"new": 1}, before)
    with pytest.raises(ValueError, match="changed"):
        finisher.replace_staged(path, {"other": 1}, before)
    assert finisher.load(path) == {"new": 1}


def test_protected_model_metadata_and_recording_never_renamed(repo):
    model = put(repo / "artifacts/models/meta.json", {"core_algorithm_hash": "old"})
    recording = put(
        repo / "artifacts/record.json",
        {"schema": "physical-recording", "core_algorithm_hash": "old"},
    )
    p = rebind.load(rebind.PROGRESS_PATH)
    p[rebind.CURRENT_KEYS[0]] = {
        "model": model.relative_to(repo).as_posix(),
        "recording": recording.relative_to(repo).as_posix(),
    }
    put(rebind.PROGRESS_PATH, p)
    staged = generate(repo)
    assert staged[rebind.CURRENT_KEYS[0]] == p[rebind.CURRENT_KEYS[0]]
    assert not (repo / "artifacts/audit").exists()


def test_generation_rejects_overwrite_and_current_as_output(repo):
    generate(repo)
    with pytest.raises(FileExistsError):
        generate(repo)
    with pytest.raises(FileExistsError):
        rebind.main(
            ["--output", "docs/current-progress.json", "--artifact-directory", "artifacts/other"]
        )


def test_apply_cannot_bypass_full_verification(repo, monkeypatch):
    original = rebind.PROGRESS_PATH.read_bytes()
    monkeypatch.setattr(rebind, "verify_staged", lambda p: {"status": "failed"})
    with pytest.raises(ValueError, match="did not pass"):
        rebind.main(
            ["--output", "docs/staged.json", "--artifact-directory", "artifacts/audit", "--apply"]
        )
    assert rebind.PROGRESS_PATH.read_bytes() == original


@pytest.mark.parametrize("drift", [False, True])
def test_finisher_rebuilds_staged_only_skips_r90_and_preserves_raw_modes(repo, monkeypatch, drift):
    def doc(name, value):
        path = put(repo / f"artifacts/{name}.json", value)
        return path.relative_to(repo).as_posix()

    qgc = doc("qgc", {})
    qgc_source = doc("qgc-source", {})
    vtol = doc("vtol", {"receipt_sha256": "vtol-canonical"})
    fixed = doc("fixed", {})
    multi = doc("multi", {})
    matrix = doc("matrix", {})
    vehicle = doc("vehicle", {})
    ai_raw = doc("ai-raw", {})
    ai = doc("ai", {"simulator_ai_flight_layer": {"path": str(repo / ai_raw)}})
    raw_report = doc("raw-report", {"modes": {"sim": {"parity": {"core_algorithm_hash": "old"}}}})
    manifest = doc("manifest", {})
    old_audit = doc("old-audit", {})
    progress = {
        "parity_rebind_audit": {"source_files": []},
        "combined_sim_qgc_external_vtol_pass_20261003_r4": {
            "evidence": qgc,
            "source_evidence": qgc_source,
        },
        "shared_policy_current_vtol_campaign_20260928_r2": {"receipt": vtol},
        "external_fixed_wing_shared_policy_depth_mission_20260930": {"evidence": fixed},
        "external_multirotor_shared_policy_depth_mission_20260930": {"evidence": multi},
        "external_mission_matrix_20260927_r6": {"evidence": matrix},
        "shared_policy_vehicle_matrix_20260927_r2": {"evidence": vehicle},
        "external_docker_ai_flight_parity_receipt_20260927_r2": {"receipt": ai},
        "external_docker_ai_flight_acceptance_20260927_r1": {},
        "evidence": {
            "release_gate_report": {"local_path": raw_report, "audit_summary_path": old_audit}
        },
    }
    staged = put(repo / "docs/staged.json", progress)
    staged_before = staged.read_bytes()
    current_before = rebind.PROGRESS_PATH.read_bytes()
    raw_before = (repo / raw_report).read_bytes()
    monkeypatch.setattr(finisher, "create_receipt", lambda **k: {"receipt_sha256": "matrix-new"})
    monkeypatch.setattr(
        finisher,
        "create_shared_policy_vehicle_receipt",
        lambda **k: {"receipt_sha256": "vehicle-new"},
    )

    def ai_builder(**kwargs):
        payload = {"receipt_sha256": "ai-new"}
        finisher.write_new(kwargs["output"], payload)
        if drift:
            (repo / vtol).write_bytes((repo / vtol).read_bytes() + b" ")
        return payload

    monkeypatch.setattr(finisher, "build_receipt", ai_builder)
    seen = []

    def gate_builder(root, report, parity, probe):
        seen.append(report.read_bytes())
        assert rebind.load(report)["modes"]["sim"]["parity"]["core_algorithm_hash"] == "old"
        return {
            "manifest_path": manifest,
            "report_bytes": len(raw_before),
            "report_sha256": hashlib.sha256(raw_before).hexdigest(),
            "current_release_parity_bound": False,
        }

    monkeypatch.setattr(finisher, "create_audit", gate_builder)
    args = ["--progress", "docs/staged.json", "--tag", "history-r1"]
    if drift:
        with pytest.raises(ValueError, match="source changed"):
            finisher.main(args)
        assert staged.read_bytes() == staged_before
    else:
        assert finisher.main(args) == 0
        finalized = finisher.load(staged)
        assert "onboard_increment_training_20261007_r90" not in finalized
        assert finalized["evidence"]["release_gate_report"]["local_path"] == raw_report
        assert finalized["external_mission_matrix_20260927_r6"]["receipt_sha256"] == "matrix-new"
        assert seen == [raw_before]
    assert rebind.PROGRESS_PATH.read_bytes() == current_before
    assert (repo / raw_report).read_bytes() == raw_before
