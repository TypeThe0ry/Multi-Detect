"""Only missing/shared bundles skip; repository-local evidence stays strict."""

import hashlib

import pytest

from tests import test_onboard_navigation_streamed_cpu_evidence as evidence
from tests.onboard_navigation_evidence_support import require_local_artifacts


def symlink(link, target):
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows symlink fixtures require Developer Mode or symlink privilege")
        raise


def test_missing_bundle_has_explicit_environment_reason(tmp_path):
    with pytest.raises(pytest.skip.Exception, match="absent.*clean clone"):
        require_local_artifacts(tmp_path)


def test_shared_bundle_is_not_admitted_or_modified(tmp_path):
    root = tmp_path / "worktree"
    shared = tmp_path / "shared-artifacts"
    root.mkdir()
    shared.mkdir()
    payload = shared / "report.json"
    payload.write_bytes(b"original evidence")
    symlink(root / "artifacts", shared)
    with pytest.raises(pytest.skip.Exception, match="read-only shared artifacts outside"):
        require_local_artifacts(root)
    assert payload.read_bytes() == b"original evidence"


def test_repository_local_binding_drift_still_fails(tmp_path, monkeypatch):
    (tmp_path / "artifacts").mkdir()
    payload = tmp_path / "artifacts/report.json"
    payload.write_bytes(b"original evidence")
    row = {
        "path": "artifacts/report.json",
        "bytes": payload.stat().st_size,
        "sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
    }
    monkeypatch.setattr(evidence, "ROOT", tmp_path)
    require_local_artifacts(tmp_path)
    assert evidence.bound(row) == payload
    payload.write_bytes(b"modified evidence")
    with pytest.raises(AssertionError):
        evidence.bound(row)


def test_nested_symlink_escape_still_fails(tmp_path, monkeypatch):
    (tmp_path / "artifacts").mkdir()
    symlink(tmp_path / "artifacts/escape", tmp_path.parent)
    monkeypatch.setattr(evidence, "ROOT", tmp_path)
    require_local_artifacts(tmp_path)
    with pytest.raises(AssertionError):
        evidence.repository_path("artifacts/escape/outside.json")


def test_internal_artifact_directory_symlink_is_admitted(tmp_path):
    target = tmp_path / "local-evidence"
    target.mkdir()
    symlink(tmp_path / "artifacts", target)
    require_local_artifacts(tmp_path)


def test_invalid_local_artifacts_root_is_not_skipped(tmp_path):
    (tmp_path / "artifacts").write_bytes(b"not a directory")
    with pytest.raises(AssertionError, match="must be a directory"):
        require_local_artifacts(tmp_path)
