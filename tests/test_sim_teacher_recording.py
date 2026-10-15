from __future__ import annotations

import json
import os
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
from multidetect.sim_teacher.recording import load_depth_array


def test_low_disk_start_guard_records_abort_without_starting_episode(tmp_path: Path) -> None:
    import scripts.collect_sim_teacher_episodes as collector
    plan = type("Plan", (), {"out": str(tmp_path), "min_free_gb": 15.0})()
    path = collector.run_episode(plan, Path("unused"), 0, free_space_fn=lambda _: 1)
    meta = json.loads((path / "episode.json").read_text(encoding="utf-8"))
    assert meta["status"] == "aborted_low_disk"
    assert "below" in meta["failure"]


def test_low_disk_guard_checks_initial_and_periodic_samples() -> None:
    import scripts.collect_sim_teacher_episodes as collector
    values = iter([20 * 1024**3, 14 * 1024**3])
    def fn(_):
        return next(values)
    assert collector.disk_space_abort_reason(".", 15.0, free_space_fn=fn) is None
    assert "below" in collector.disk_space_abort_reason(".", 15.0, free_space_fn=fn)


def test_campaign_and_scorer_report_abort_separately(tmp_path: Path) -> None:
    from scripts.run_sim_teacher_campaign import _write_campaign
    from scripts.score_onboard_loop import score_episode
    episode = tmp_path / "episode"
    (episode / "episodes").mkdir(parents=True)
    ep = episode / "episodes" / "aborted"
    ep.mkdir()
    (ep / "episode.json").write_text(
        json.dumps({"status": "aborted_low_disk", "failure": "disk floor"}), encoding="utf-8"
    )
    (ep / "steps.jsonl").write_text("", encoding="utf-8")
    row = {"world": "w", "seed": 1, "split": "test", "teacher": "explore",
           "dataset": str(episode), "status": "aborted_low_disk", "attempts": 1,
           "duration_s": 0}
    report = _write_campaign(tmp_path / "campaign.json", [row], "explore", 1, 1, 0, tmp_path, 1, 0)
    assert report["summary"]["aborted_low_disk"] == 1
    scored = score_episode(ep, {"targets": []})
    assert scored["status"] == "aborted_low_disk"
    assert scored["excluded_from_model_failure_metrics"] is True


@pytest.mark.parametrize("depth_format", ["npy", "npz", "png16"])
def test_opt_in_depth_formats_load_to_equivalent_arrays(tmp_path: Path, depth_format: str) -> None:
    recorder = EpisodeRecorder(tmp_path, "episode", depth_format=depth_format)
    source = np.array([[1.25, 2.5], [3.75, 4.0]], dtype=np.float32)
    recorder.record_step({"depth": source}, {})
    path = recorder.episode_dir / "depth" / next(
        name for name in os.listdir(recorder.episode_dir / "depth")
    )
    loaded = load_depth_array(path)
    expected = source
    np.testing.assert_array_equal(loaded, expected)


def test_default_recorder_metadata_has_legacy_shape_and_fallback(tmp_path: Path) -> None:
    recorder = EpisodeRecorder(tmp_path, "episode")
    meta = json.loads(recorder.episode_path.read_text(encoding="utf-8"))
    assert "depth_format" not in meta
    recorder.record_step({"depth": {"legacy": 1}}, {})
    path = recorder.episode_dir / "depth" / "000000_before.npy"
    assert json.loads(path.read_text(encoding="utf-8")) == {"legacy": 1}


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
        # Gazebo and PX4 simulation clocks (milliseconds here for a compact
        # fixture); the auditor derives and verifies this relationship.
        "frame_sensor_stamp_ns": 1_000_000_000,
        "pose_time_boot_ms": 1_000,
        "sim_skew_ms": 0.0,
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



def test_live_admission_rejects_sim_skew_over_limit() -> None:
    root = _dataset_dir()
    try:
        recorder = EpisodeRecorder(root, "live-skew", sensor_status="live")
        bad = _live_obs()
        bad["frame_sensor_stamp_ns"] = 1_300_000_000
        bad["sim_skew_ms"] = 300.0
        recorder.record_step(
            bad,
            ActionPrimitive.stop(),
            ack={"action_ack": True},
            obs_after={**bad},
        )
        with pytest.raises(DatasetAuditError, match="sim_skew_ms exceeds"):
            audit_dataset(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_auditor_reports_per_episode_and_global_sim_skew_counts() -> None:
    root = _dataset_dir()
    try:
        good = EpisodeRecorder(root, "live-good", sensor_status="live")
        good_obs = _live_obs()
        good.record_step(
            good_obs,
            ActionPrimitive.hold(0.1),
            ack={"action_ack": True},
            obs_after={**good_obs},
        )

        bad = EpisodeRecorder(root, "live-bad-skew", sensor_status="live")
        bad.record_step(
            good_obs,
            ActionPrimitive.hold(0.1),
            ack={"action_ack": True},
            obs_after={**good_obs},
        )
        offending = _live_obs()
        offending["frame_sensor_stamp_ns"] = 1_300_000_000
        offending["sim_skew_ms"] = 300.0
        bad.record_step(
            offending,
            ActionPrimitive.stop(),
            ack={"action_ack": True},
            obs_after={**offending},
        )

        # ``record_step`` can legitimately be called twice within one clock
        # tick on a fast filesystem; make the fixture's receive ordering
        # deterministic for the auditor's monotonicity check.
        bad_steps = root / "episodes" / "live-bad-skew" / "steps.jsonl"
        bad_rows = [json.loads(line) for line in bad_steps.read_text().splitlines()]
        bad_rows[1]["receive_monotonic_ns"] = bad_rows[0]["receive_monotonic_ns"] + 1
        bad_steps.write_text(
            "\n".join(json.dumps(row) for row in bad_rows) + "\n",
            encoding="utf-8",
        )

        with pytest.raises(DatasetAuditError, match="over-limit steps by episode"):
            audit_dataset(root)

        report = json.loads((root / "audit.json").read_text(encoding="utf-8"))
        assert report["episodes"]["live-good"]["sim_skew_over_limit_steps"] == 0
        assert report["episodes"]["live-bad-skew"]["sim_skew_over_limit_steps"] == 1
        assert report["episodes"]["live-bad-skew"]["sim_skew_over_limit_count"] == 1
        assert report["sim_skew_ms"]["over_limit_count"] == 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_live_admission_rejects_stuck_frames_when_pose_moves() -> None:
    root = _dataset_dir()
    try:
        recorder = EpisodeRecorder(root, "live-stuck", sensor_status="live")
        first = _live_obs()
        recorder.record_step(
            first,
            ActionPrimitive.hold(0.1),
            ack={"action_ack": True},
            obs_after={**first},
        )
        moved = _live_obs()
        moved["pose"] = {
            "local_ned_m": [2.0, 0.0, -5.0],
            "lat_deg": 1.3,
            "lon_deg": 103.8,
            "alt_m": 5.0,
        }
        # Keep the stale frame stamp near the start of the episode, as in the
        # r5 backlog evidence; the pose has moved by two metres.
        recorder.record_step(
            moved,
            ActionPrimitive.stop(),
            ack={"action_ack": True},
            obs_after={**moved},
        )
        # Make the fixture deterministic even on filesystems with coarse clock
        # resolution: the production recorder normally gets distinct values.
        steps_path = root / "episodes" / "live-stuck" / "steps.jsonl"
        rows = [json.loads(line) for line in steps_path.read_text().splitlines()]
        rows[1]["receive_monotonic_ns"] = rows[0]["receive_monotonic_ns"] + 1
        steps_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        with pytest.raises(DatasetAuditError, match="frame stamps do not advance"):
            audit_dataset(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
