"""Environment prerequisites for sealed navigation evidence, never hash fallbacks."""

from pathlib import Path

import pytest


def local_artifacts_unavailable(root: Path) -> str | None:
    artifacts = root / "artifacts"
    if not artifacts.resolve().is_relative_to(root.resolve()):
        return (
            "sealed navigation evidence requires a repository-local artifacts bundle; "
            "this worktree uses read-only shared artifacts outside its root"
        )
    if not artifacts.exists():
        return "sealed navigation artifacts bundle is absent from this clean clone"
    assert artifacts.is_dir(), "repository-local artifacts root must be a directory"
    return None


def requires_local_artifacts(root: Path):
    reason = local_artifacts_unavailable(root)
    return pytest.mark.skipif(reason is not None, reason=reason or "local artifacts available")


def require_local_artifacts(root: Path) -> None:
    reason = local_artifacts_unavailable(root)
    if reason is not None:
        pytest.skip(reason)
