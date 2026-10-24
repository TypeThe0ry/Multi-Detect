from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from multidetect import provenance
from multidetect.sim_teacher.recording import EpisodeRecorder


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def test_code_version_tracks_git_and_dirty_files(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "tracked.txt").write_text("base", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "base")
    clean = provenance.build_provenance(root=tmp_path)["code"]
    assert clean["available"] and clean["branch"] == "main" and clean["dirty"] is False
    (tmp_path / "tracked.txt").write_text("dirty", encoding="utf-8")
    dirty = provenance.build_provenance(root=tmp_path)["code"]
    assert dirty["dirty"] is True and dirty["changed_files"] == 1
    _git(tmp_path, "checkout", "--detach")
    assert provenance.build_provenance(root=tmp_path)["code"]["detached"] is True


def test_git_unavailable_degrades_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("git")
    monkeypatch.setattr(provenance.subprocess, "check_output", unavailable)
    code = provenance.build_provenance(root=tmp_path)["code"]
    assert code["available"] is False and code["commit"] is None


def test_episode_config_snapshot_and_model_hash_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "onboard.json"
    config.write_text(json.dumps({"gguf": "weights.gguf"}), encoding="utf-8")
    model = tmp_path / "weights.gguf"
    model.write_bytes(b"weights")
    cache = tmp_path / ".model-hash-cache.json"
    digest = provenance.build_provenance(root=tmp_path, config_paths=[config], model_paths=[model], model_cache=cache)
    assert digest["config"][0]["sha256"]
    assert digest["models"][0]["sha256"]
    with monkeypatch.context() as patch:
        patch.setattr(provenance, "_sha256", lambda path: pytest.fail("cache should be reused"))
        repeated = provenance.build_provenance(root=tmp_path, model_paths=[model], model_cache=cache)
    assert repeated["models"][0]["sha256"] == digest["models"][0]["sha256"]
    recorder = EpisodeRecorder(tmp_path / "out", "episode-test")
    recorder.meta["provenance"] = digest
    recorder.episode_path.write_text(json.dumps(recorder.meta), encoding="utf-8")
    assert json.loads(recorder.episode_path.read_text(encoding="utf-8"))["provenance"]["models"]
    snapshot = provenance.snapshot_config(config, recorder.episode_dir / "config")
    assert snapshot and Path(snapshot["snapshot"]).read_bytes() == config.read_bytes()


def test_report_and_replay_version_records() -> None:
    report = {"summary": {}, "provenance": provenance.build_provenance(root=Path.cwd())}
    assert "code" in report["provenance"]
    result = provenance.replay_version_record({"commit": "old"}, {"commit": "new"})
    assert result["version_mismatch"] is True
    assert provenance.replay_version_record(None, {"commit": "new"})["version_mismatch"] is False
