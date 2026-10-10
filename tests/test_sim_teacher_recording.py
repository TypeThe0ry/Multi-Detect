from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import numpy as np
import pytest

from multidetect.sim_teacher import (
    ActionPrimitive,
    DatasetAuditError,
    EpisodeRecorder,
    audit_dataset,
)


def _obs(value: float = 1.0) -> dict:
    return {
        "rgb": b"not-a-real-png",
        "depth": np.full((2, 3), value, dtype=np.float32),
        "camera_info": {"fx": 10.0, "fy": 10.0},
        "pose": {"north_m": 0.0, "east_m": 0.0, "down_m": -5.0},
        "velocity": {"north_mps": 0.0},
    }


def _dataset_dir() -> Path:
    path = Path(".tmp-sim-teacher-tests") / uuid.uuid4().hex
    path.mkdir(parents=True)
    return path


def test_recorder_layout_and_append_only() -> None:
    root = _dataset_dir()
    try:
        recorder = EpisodeRecorder(root, "episode-001", task_text="task")
        recorder.record_mavlink({"type": "HEARTBEAT", "seq": 1})
        recorder.record_step(
            _obs(),
            ActionPrimitive.translate(1, 0),
            labels={"target_in_fov": False},
            obs_after=_obs(2),
        )
        recorder.record_step(_obs(2), ActionPrimitive.stop(), outcome={"status": "timeout"})
        episode = root / "episodes" / "episode-001"
        assert (episode / "steps.jsonl").exists()
        assert (episode / "mavlink.jsonl").exists()
        assert len(list((episode / "rgb").glob("*.png"))) == 3
        assert len(list((episode / "depth").glob("*.npy"))) == 3
        rows = [json.loads(line) for line in (episode / "steps.jsonl").read_text().splitlines()]
        assert [row["step_index"] for row in rows] == [0, 1]
        assert rows[0]["obs_before"]["rgb_path"].endswith("before.png")
        assert rows[0]["obs_after"]["depth_path"].endswith("after.npy")
        assert json.loads((episode / "episode.json").read_text())["step_count"] == 2
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_auditor_writes_manifest_and_detects_tampering() -> None:
    root = _dataset_dir()
    try:
        recorder = EpisodeRecorder(root, "episode-001")
        recorder.record_step(_obs(), ActionPrimitive.hold(0.1), outcome={"status": "success"})
        result = audit_dataset(root)
        assert result["step_count"] == 1
        assert result["success_rate"] == pytest.approx(1.0)
        assert any(
            path.replace("\\\\", "/") == "episodes/episode-001/steps.jsonl"
            for path in result["files"]
        )
        row = json.loads((root / "episodes" / "episode-001" / "steps.jsonl").read_text())
        rgb_path = root / "episodes" / "episode-001" / row["obs_before"]["rgb_path"]
        rgb_path.unlink()
        with pytest.raises(DatasetAuditError):
            audit_dataset(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_auditor_detects_non_monotonic_steps() -> None:
    root = _dataset_dir()
    try:
        recorder = EpisodeRecorder(root, "episode-001")
        recorder.record_step(_obs(), ActionPrimitive.stop())
        steps = root / "episodes" / "episode-001" / "steps.jsonl"
        row = json.loads(steps.read_text())
        steps.write_text(json.dumps(row) + "`n")
        with pytest.raises(DatasetAuditError):
            audit_dataset(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _live_obs(value: float = 1.0) -> dict:
    # Minimal valid PNG (1x1 RGB) accepted by live admission checks.
    return {
        "rgb": b"\x89PNG\r\n\x1a\n" + b"live-frame",
        "depth": np.full((2, 3), value, dtype=np.float32),
        "camera_info": {"fx": 10.0, "fy": 10.0},
        "pose": {"local_ned_m": [0.0, 0.0, -5.0], "lat_deg": 1.3, "lon_deg": 103.8, "alt_m": 5.0},
        "velocity": [0.0, 0.0, 0.0],
        "gps_fix": {"fix_type": 3, "satellites": 10},
    }


def test_live_admission_and_episode_statistics() -> None:
    root = _dataset_dir()
    try:
        recorder = EpisodeRecorder(root, "live-001", sensor_status="live")
        recorder.record_step(
            _live_obs(),
            ActionPrimitive.translate(1.0, 0.0),
            ack={"action_ack": True},
            outcome={
                "status": "success",
                "commanded_delta_ned": [1.0, 0.0, 0.0],
                "achieved_delta_ned": [0.8, 0.0, 0.0],
                "achieved_yaw_delta_deg": 2.0,
            },
            obs_after=_live_obs(2.0),
        )
        recorder.mark_finished()
        result = audit_dataset(root)
        assert result["episodes"]["live-001"]["duration_s"] >= 0.0
        assert result["achieved_vs_commanded_error"]["position_m"]["count"] == 1
        assert result["achieved_vs_commanded_error"]["position_m"]["mean"] == pytest.approx(0.2)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_live_admission_rejects_missing_after_and_gps() -> None:
    root = _dataset_dir()
    try:
        recorder = EpisodeRecorder(root, "live-bad", sensor_status="live")
        bad = _live_obs()
        bad["gps_fix"] = {"fix_type": 1}
        recorder.record_step(bad, ActionPrimitive.stop(), ack={"action_ack": True})
        with pytest.raises(DatasetAuditError, match="obs_after"):
            audit_dataset(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
