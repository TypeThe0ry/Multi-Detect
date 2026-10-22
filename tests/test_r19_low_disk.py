"""r19 low-disk collection: JPEG RGB, no depth on disk, lossless segmentation, and
cleanup that only removes this round's own consumed, hash-recorded files."""

from __future__ import annotations

import hashlib
import io
import json
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from PIL import Image

import scripts.collect_sim_teacher_episodes as collector
import scripts.export_slow_system_r15 as export
import scripts.run_r15_round as r15
from multidetect.sim_teacher import (
    ActionPrimitive,
    DatasetAuditError,
    EpisodeRecorder,
    audit_dataset,
)
from multidetect.sim_teacher.segmentation import load_labels_map


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _png(array: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(array).save(buffer, format="PNG")
    return buffer.getvalue()


def _frame(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = np.linspace(0, 255, 64 * 48 * 3).reshape((48, 64, 3))
    return (base + rng.integers(0, 16, base.shape)).clip(0, 255).astype(np.uint8)


def _segmentation(seed: int) -> np.ndarray:
    labels = np.zeros((48, 64, 3), dtype=np.uint8)
    labels[10 + seed : 20 + seed, 5:30] = (3, 0, 7)  # instance 3, semantic 7
    labels[30:40, 40:60] = (1, 2, 9)
    return labels


def _live_obs(seed: int, *, rgb=None) -> dict:
    return {
        "rgb": _png(_frame(seed)) if rgb is None else rgb,
        "depth": np.full((48, 64), 5.0 + seed, dtype=np.float32),
        "segmentation": _segmentation(seed),
        "camera_info": {"fx": 50.0, "fy": 50.0, "width": 64, "height": 48},
        "pose": {"local_ned_m": [0.0, 0.0, -5.0], "lat_deg": 1.3, "lon_deg": 103.8, "alt_m": 5.0},
        "velocity": [0.0, 0.0, 0.0],
        "gps_fix": {"fix_type": 3, "satellites": 10},
        "frame_sensor_stamp_ns": 1_000_000_000 * (seed + 1),
        "pose_time_boot_ms": 1_000 * (seed + 1),
        "sim_skew_ms": 0.0,
    }


def _record_low_disk(root: Path, episode_id: str = "episode-0000-aaaa0001", steps: int = 2):
    recorder = EpisodeRecorder(
        root,
        episode_id,
        sensor_status="live",
        depth_format="png16",
        rgb_format="jpeg",
        rgb_quality=90,
        record_depth=False,
        compress_mavlink=True,
    )
    recorder.record_mavlink({"type": "HEARTBEAT"})
    for index in range(steps):
        recorder.record_step(
            _live_obs(2 * index),
            ActionPrimitive.translate(1.0, 0.0),
            ack={"action_ack": True},
            outcome={"status": "success"},
            obs_after=_live_obs(2 * index + 1),
        )
    recorder.mark_finished()
    return recorder


# --- recorder and audit -----------------------------------------------------------


def test_low_disk_recorder_writes_jpeg_rgb_no_depth_and_lossless_segmentation(tmp_path: Path):
    recorder = _record_low_disk(tmp_path)
    episode = recorder.episode_dir
    rows = [json.loads(line) for line in (episode / "steps.jsonl").read_text().splitlines()]
    meta = json.loads((episode / "episode.json").read_text())
    assert meta["rgb_format"] == "jpeg" and meta["rgb_quality"] == 90
    assert meta["depth_recorded"] is False and meta["mavlink_compressed"] is True
    assert not (episode / "depth").exists()
    assert (episode / "mavlink.jsonl.gz").is_file()
    for index, row in enumerate(rows):
        for offset, side in enumerate(("obs_before", "obs_after")):
            obs = row[side]
            assert obs["rgb_path"].endswith(".jpg")
            assert "depth_path" not in obs
            frame = episode / obs["rgb_path"]
            assert frame.read_bytes()[:3] == b"\xff\xd8\xff"
            # Readers used by cache_onboard_proposals (cv2) and build_sft/train (PIL).
            assert cv2.imread(str(frame)).shape == (48, 64, 3)
            with Image.open(frame) as opened:
                assert opened.mode == "RGB"
            seg = load_labels_map(episode / obs["segmentation_path"])
            assert obs["segmentation_path"].endswith(".png")
            np.testing.assert_array_equal(seg, _segmentation(2 * index + offset))


def test_default_recorder_still_writes_png_rgb_and_depth(tmp_path: Path):
    recorder = EpisodeRecorder(tmp_path, "episode", sensor_status="live")
    recorder.record_step(_live_obs(0), {})
    row = json.loads((recorder.episode_dir / "steps.jsonl").read_text())
    assert row["obs_before"]["rgb_path"].endswith(".png")
    assert row["obs_before"]["depth_path"].endswith(".npy")
    meta = json.loads(recorder.episode_path.read_text())
    assert "rgb_format" not in meta and "depth_recorded" not in meta


@pytest.mark.parametrize(
    "kwargs", [{"rgb_format": "webp"}, {"rgb_quality": 0}, {"rgb_quality": 101}]
)
def test_recorder_rejects_unknown_rgb_options(tmp_path: Path, kwargs: dict):
    with pytest.raises(ValueError):
        EpisodeRecorder(tmp_path, "episode", **kwargs)


def test_live_audit_accepts_low_disk_jpeg_episode_without_depth(tmp_path: Path):
    # One step: Windows monotonic_ns can repeat between back-to-back steps.
    _record_low_disk(tmp_path, steps=1)
    report = audit_dataset(tmp_path)
    assert report["episodes"]["episode-0000-aaaa0001"]["step_count"] == 1


def test_live_audit_rejects_rgb_whose_bytes_do_not_match_the_suffix(tmp_path: Path):
    recorder = _record_low_disk(tmp_path, steps=1)
    row = json.loads((recorder.episode_dir / "steps.jsonl").read_text())
    # A PNG (or a JSON fallback) renamed .jpg is not a real JPEG frame.
    (recorder.episode_dir / row["obs_before"]["rgb_path"]).write_bytes(_png(_frame(0)))
    with pytest.raises(DatasetAuditError, match="no real rgb file"):
        audit_dataset(tmp_path)


def test_live_audit_rejects_unknown_rgb_extension(tmp_path: Path):
    recorder = _record_low_disk(tmp_path, steps=1)
    steps = recorder.episode_dir / "steps.jsonl"
    row = json.loads(steps.read_text())
    old = recorder.episode_dir / row["obs_before"]["rgb_path"]
    new = old.with_suffix(".bmp")
    old.rename(new)
    row["obs_before"]["rgb_path"] = str(new.relative_to(recorder.episode_dir))
    steps.write_text(json.dumps(row) + "\n")
    with pytest.raises(DatasetAuditError, match="no real rgb file"):
        audit_dataset(tmp_path)


def test_live_audit_still_requires_depth_unless_the_episode_declares_it_off(tmp_path: Path):
    recorder = EpisodeRecorder(tmp_path, "live-png", sensor_status="live", rgb_format="jpeg")
    obs = _live_obs(0)
    obs.pop("depth")
    recorder.record_step(
        obs, ActionPrimitive.stop(), ack={"action_ack": True}, obs_after=_live_obs(1)
    )
    recorder.mark_finished()
    with pytest.raises(DatasetAuditError, match="finite depth"):
        audit_dataset(tmp_path)


# --- collector ----------------------------------------------------------------


def test_collector_no_depth_keeps_the_frame_server_depth_contract(tmp_path: Path, monkeypatch):
    runtime = tmp_path / "runtime.json"
    runtime.write_text(
        json.dumps({"sensors": [{"kind": "rgb_camera"}, {"kind": "depth_camera"}]}),
        encoding="utf-8",
    )
    plan = collector.CollectionPlan(
        "s.json",
        str(runtime),
        "t.json",
        "expert_search",
        1,
        1,
        0,
        str(tmp_path),
        capture_depth=False,
        rgb_format="jpeg",
    )
    monkeypatch.setattr(collector, "_docker_executable", lambda plan: "docker")
    command = collector._camera_stream_command(plan, SimpleNamespace(name="c"))
    # The frame server requires the contract and streams depth on the wire; only the
    # recorder stops persisting it, so the stream layout must stay unchanged.
    assert "--depth-contract-json" in command


def test_collector_parser_low_disk_flags():
    args = collector.build_parser().parse_args(
        [
            "--scenario",
            "s",
            "--teacher",
            "expert_search",
            "--out",
            "o",
            "--episodes",
            "1",
            "--steps-per-episode",
            "1",
            "--no-depth",
            "--rgb-format",
            "jpeg",
            "--rgb-quality",
            "85",
            "--compress-mavlink",
        ]
    )
    assert args.no_depth and args.rgb_format == "jpeg" and args.rgb_quality == 85
    defaults = collector.build_parser().parse_args(
        [
            "--scenario",
            "s",
            "--teacher",
            "explore",
            "--out",
            "o",
            "--episodes",
            "1",
            "--steps-per-episode",
            "1",
        ]
    )
    assert not defaults.no_depth and defaults.rgb_format == "png" and defaults.rgb_quality == 90


# --- round runner -------------------------------------------------------------


def _round_args(tmp_path: Path, **overrides) -> Namespace:
    values = dict(
        out=tmp_path / "round",
        episodes=tmp_path / "episodes",
        scenarios=tmp_path / "scenarios",
        dry_run=True,
        min_free_gb=0,
        min_free_memory_gb=0,
        collector_min_free_gb=None,
        low_disk=True,
        steps_per_episode=120,
        workers=2,
        retries=0,
        teacher_target="true",
    )
    values.update(overrides)
    return Namespace(**values)


def test_low_disk_collect_command_adds_flags_and_keeps_target_last(tmp_path: Path, capsys):
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    worlds = [f"gen_r19_w{index:04d}" for index in range(20)]
    for world in worlds:
        (scenarios / f"{world}.scenario.json").write_text("{}")
    index_path = scenarios / "index.json"
    index_path.write_text(json.dumps({"worlds": [{"world_id": world} for world in worlds]}))
    runner = r15.Round(_round_args(tmp_path, min_free_gb=7, r15_index=index_path))
    runner.collect()
    out = capsys.readouterr().out
    assert "--rgb-format jpeg --rgb-quality 90 --no-depth --compress-mavlink --out" in out
    assert "--depth-format png16 --segmentation" in out  # segmentation still recorded
    assert "--min-free-gb 7" in out  # collector gate defaults to --min-free-gb
    assert "--scenario " + str(scenarios / "gen_r19_w0019.scenario.json") in out
    normal = r15.Round(_round_args(tmp_path, low_disk=False, r15_index=index_path))
    normal.collect()
    assert "--no-depth" not in capsys.readouterr().out


def test_round_estimate_low_disk_is_smaller_but_export_peak_is_shared():
    normal = r15.estimate_round_disk_gib(low_disk=False)
    low = r15.estimate_round_disk_gib(low_disk=True)
    assert low["episodes_gib"] < normal["episodes_gib"] / 4
    assert low["export_peak_gib"] == normal["export_peak_gib"]
    assert low["after_round_gib"] < normal["after_round_gib"] - 7
    assert low["peak_gib"] < normal["peak_gib"]


def _collected_episode(root: Path, world: str, idx: int, episode_id: str) -> tuple[str, dict, Path]:
    recorder = _record_low_disk(root / world / f"ep-{idx}" / "attempt-0", episode_id)
    episode_json = recorder.episode_path
    return (
        f"{world}:{idx}",
        {
            "status": "complete",
            "path": str(episode_json),
            "sha256": _sha(episode_json),
            "attempt": 0,
        },
        recorder.episode_dir,
    )


def _sft_manifest(path: Path, files: list[Path], *, prefix: str = "artifacts/r19-episodes") -> Path:
    source = {}
    for file in files:
        rel = file.as_posix().split("/episodes-root/", 1)[1]
        source[f"{prefix}/{rel}"] = _sha(file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"source_sha256": source}), encoding="utf-8")
    return path


def test_cleanup_removes_only_this_rounds_consumed_hashed_segmentation(tmp_path: Path):
    root = tmp_path / "episodes-root"
    key, item, episode = _collected_episode(root, "gen_r15_w0000", 0, "episode-0000-own00001")
    seg = sorted((episode / "seg").glob("*.png"))
    rgb = sorted((episode / "rgb").glob("*.jpg"))
    assert len(seg) == 4
    # An episode under the same root that this round did not collect (another run).
    foreign = _record_low_disk(
        root / "gen_r15_w0001" / "ep-0" / "attempt-0", "episode-0000-other001"
    )
    foreign_seg = sorted((foreign.episode_dir / "seg").glob("*.png"))
    # A failed attempt of this round's own key: not admitted, so not consumed.
    stale = _record_low_disk(root / "gen_r15_w0000" / "ep-0" / "attempt-1", "episode-0000-stale001")
    # build_sft consumed three of our four masks and the foreign one (e.g. --extra-episodes).
    manifest = _sft_manifest(
        tmp_path / "round" / "r15-additions" / "manifest.json", [*seg[:3], *rgb, foreign_seg[0]]
    )
    hashes = {p: _sha(p) for p in seg}
    ledger = tmp_path / "round" / r15.LOW_DISK_LEDGER

    summary = r15.cleanup_consumed_segmentation(
        episodes_root=root, collected={key: item}, sft_manifest=manifest, ledger=ledger
    )

    assert summary["removed_files"] == 3 and summary["kept_unconsumed"] == 1
    assert [p.is_file() for p in seg] == [False, False, False, True]
    assert all(p.is_file() for p in rgb)  # training reads RGB through image_path
    assert all(p.is_file() for p in foreign_seg)
    assert all(p.is_file() for p in (stale.episode_dir / "seg").glob("*.png"))
    assert len(list((stale.episode_dir / "seg").glob("*.png"))) == 4
    record = json.loads(ledger.read_text(encoding="utf-8"))
    files = record["episodes"][key]["files"]
    assert record["sft_manifest_sha256"] == _sha(manifest)
    assert set(files) == {p.relative_to(root).as_posix() for p in seg[:3]}
    for path in seg[:3]:
        entry = files[path.relative_to(root).as_posix()]
        assert entry["sha256"] == hashes[path] and entry["removed"] is True
        assert entry["consumed_by"] == "build_sft"
    assert r15.ledger_removed(ledger) == 3

    # Idempotent on resume: nothing more goes, the hash records stay.
    again = r15.cleanup_consumed_segmentation(
        episodes_root=root, collected={key: item}, sft_manifest=manifest, ledger=ledger
    )
    assert again["removed_files"] == 0 and seg[3].is_file()
    assert json.loads(ledger.read_text(encoding="utf-8"))["episodes"][key]["files"] == files


def test_cleanup_keeps_files_whose_hash_differs_from_what_build_sft_consumed(tmp_path: Path):
    root = tmp_path / "episodes-root"
    key, item, episode = _collected_episode(root, "gen_r15_w0000", 0, "episode-0000-own00002")
    seg = sorted((episode / "seg").glob("*.png"))
    manifest = _sft_manifest(tmp_path / "manifest.json", seg)
    Image.fromarray(np.zeros((48, 64, 3), dtype=np.uint8)).save(seg[0])  # rewritten after SFT
    summary = r15.cleanup_consumed_segmentation(
        episodes_root=root, collected={key: item}, sft_manifest=manifest, ledger=tmp_path / "l.json"
    )
    assert summary["removed_files"] == 3 and seg[0].is_file()


def test_cleanup_skips_episodes_outside_root_or_changed_since_collection(tmp_path: Path):
    root = tmp_path / "episodes-root"
    outside_root = tmp_path / "elsewhere" / "episodes-root"
    key, item, episode = _collected_episode(
        outside_root, "gen_r15_w0000", 0, "episode-0000-out00001"
    )
    key2, item2, episode2 = _collected_episode(root, "gen_r15_w0002", 1, "episode-0000-chg00001")
    item2 = {**item2, "sha256": "0" * 64}  # episode.json is not the one collected
    files = [*sorted((episode / "seg").glob("*.png")), *sorted((episode2 / "seg").glob("*.png"))]
    manifest = _sft_manifest(tmp_path / "manifest.json", files)
    summary = r15.cleanup_consumed_segmentation(
        episodes_root=root,
        collected={key: item, key2: item2},
        sft_manifest=manifest,
        ledger=tmp_path / "l.json",
    )
    assert summary["removed_files"] == 0
    assert sorted(summary["skipped_episodes"]) == sorted([key, key2])
    assert all(p.is_file() for p in files)


def test_build_sft_refuses_to_rebuild_after_cleanup_removed_consumed_masks(tmp_path: Path):
    args = _round_args(tmp_path, dry_run=False, stage="build_sft")
    args.out.mkdir(parents=True)
    root = tmp_path / "episodes-root"
    rel = "gen/ep-0/attempt-0/episodes/e/seg/000000_before.png"
    (args.out / r15.LOW_DISK_LEDGER).write_text(
        json.dumps(
            {"episodes_root": str(root), "episodes": {"gen:0": {"files": {rel: {"removed": True}}}}}
        ),
        encoding="utf-8",
    )
    runner = r15.Round(args)
    with pytest.raises(RuntimeError, match="不能重建 SFT"):
        runner.execute()


# --- export -------------------------------------------------------------------


def test_export_drop_refuses_a_merged_dir_it_did_not_create(tmp_path: Path):
    merged = tmp_path / "merged-hf"
    merged.mkdir()
    (merged / "model.safetensors").write_bytes(b"someone else's weights")
    with pytest.raises(SystemExit):
        export.claim_merged_dir(merged, tmp_path / "adapter")
    with pytest.raises(SystemExit):
        export.drop_intermediate(tmp_path, merged)
    assert (merged / "model.safetensors").is_file()
    assert not (tmp_path / export.LEDGER).exists()


def test_export_drop_records_hashes_before_deleting_own_intermediates(tmp_path: Path):
    merged = tmp_path / "merged-hf"
    export.claim_merged_dir(merged, tmp_path / "adapter")
    (merged / "model.safetensors").write_bytes(b"merged")
    f16 = tmp_path / "slow-system-f16.gguf"
    f16.write_bytes(b"f16")
    keep = tmp_path / "slow-system-Q5_K_M.gguf"
    keep.write_bytes(b"q5")
    export.drop_intermediate(tmp_path, merged)
    export.drop_intermediate(tmp_path, f16)
    assert not merged.exists() and not f16.exists() and keep.is_file()
    removed = json.loads((tmp_path / export.LEDGER).read_text())["removed"]
    assert removed["merged-hf/model.safetensors"]["sha256"] == hashlib.sha256(b"merged").hexdigest()
    assert removed["slow-system-f16.gguf"] == {
        "bytes": 3,
        "sha256": hashlib.sha256(b"f16").hexdigest(),
    }


def test_round_dry_run_prints_both_disk_estimates(monkeypatch, capsys, tmp_path: Path):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_r15_round.py",
            "--dry-run",
            "--stage",
            "report",
            "--low-disk",
            "--out",
            str(tmp_path / "o"),
        ],
    )
    assert r15.main() == 0
    out = capsys.readouterr().out
    assert "[dry-run] normal:" in out and "[dry-run] low-disk:" in out
