from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scripts.realworld import wide_collect_batch as batch


IOWA = "rw_farm_iowa"
STEM = "rw_farm_iowa_w6km__clear_noon"


@pytest.fixture
def spec(tmp_path):
    worlds = tmp_path / "worlds"
    worlds.mkdir()
    (worlds / f"{STEM}.sdf").write_text('<sdf version="1.9"><world name="default"/></sdf>\n')
    return {"batch_id": "test", "output_root": str(tmp_path / "batch"), "gpu": 3, "slots": [6, 5],
            "share_gpu": True, "variant_world_root": str(worlds), "resolved_root": str(tmp_path),
            "jobs": [{"site": IOWA, "altitude_m": 90, "time_of_day": "noon", "weather": "clear", "seed": 42}]}


@pytest.fixture
def matrix(tmp_path, monkeypatch):
    monkeypatch.setattr(batch, "MIN_FREE_BYTES", 0)
    command = tmp_path / "fake-rtf-matrix"
    command.write_text(f"#!{sys.executable}\n" + '''
import argparse, json, os
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--manifest", type=Path, required=True)
p.add_argument("--out", type=Path, required=True)
a = p.parse_args()
row, = json.loads(a.manifest.read_text())
assert len(a.manifest.read_text().splitlines()) == 1
assert row["minimum_rtf"] == 0.9 and row["minimum_max_distance_m"] == 4000
assert os.environ["RTF_MATRIX_SINGLE_ROUND"] == "1"
assert os.environ["RTF_MATRIX_ATTEMPT"] == a.out.name.split("-")[-1]
assert os.environ["MULTIDETECT_SITL_SLOTS"] == "8"
assert os.environ["RTF_MATRIX_SHARE_GPU"] == "1"
scenario = Path(row["scenario"])
assert scenario.parent == a.out / "scenario"
runtime_path = json.loads(scenario.read_text())["channels"]["runtime_input"]["path"]
assert json.loads((scenario.parent / runtime_path).read_text())["seed"] >= 42
assert row["extent_m"] == 6000 and row["airframe"] == "rc_cessna_mono_cam"
mode = os.environ.get("FAKE_MATRIX_MODE")
slot = os.environ["RTF_MATRIX_SLOT"]
gpu = os.environ["RTF_MATRIX_GPU"]
name = f"md-rw-long-slot-{slot}-fake"
episode = a.out / "flight/episodes/episode-fixture"
(episode / "rgb").mkdir(parents=True)
(episode / "rgb/000001.png").write_bytes(b"data" * 10_000)
identity = {"container_id": "a" * 64, "container_name": name}
(episode / "episode.json").write_text(json.dumps({"status": "complete", "simulator_container": identity}))
(a.out / "stats.json").write_text("{}")
result = {**identity, "name": row["name"], "gpu": gpu, "valid_round": True,
          "route_completed": True, "rtf_window_median": 0.94,
          "rtf_delta_sim_over_delta_real": 0.88, "max_distance_from_spawn_m": 4100}
if mode == "invalid":
    result["valid_round"] = False
if mode == "truthy":
    result["valid_round"] = 1
if mode == "short":
    result["route_completed"] = False
if mode == "low_rtf":
    result["rtf_window_median"] = 0.89
if mode == "wrong_container":
    result["container_id"] = "b" * 64
if mode != "missing":
    (a.out / "result.json").write_text(json.dumps(result))
''')
    command.chmod(0o755)
    return command


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_expand_is_deterministic_deduplicates_and_assigns_slots(spec):
    first = spec["jobs"][0]
    spec["jobs"] += [{**first, "scenario": IOWA, "altitude_m": 90.0}, {**first, "seed": 43}]
    plan = batch.expand(spec)
    assert [job["slot"] for job in plan["jobs"]] == [6, 5]
    assert len({job["job_id"] for job in plan["jobs"]}) == 2
    assert plan["jobs"][0]["inputs"]["max_artifact_bytes"] == 8 * 1024**3
    assert batch.expand({**spec, "gpu": 1, "slots": [7]})["jobs"][0]["job_id"] == plan["jobs"][0]["job_id"]
    defaults = batch.expand({"jobs": [first]})
    assert Path(defaults["output_root"]) == batch.BATCHES / defaults["batch_id"]


@pytest.mark.parametrize("field,value", [("altitude_m", 121), ("altitude_m", True), ("weather", "fog"),
                                            ("seed", -1), ("scenario", "other_site")])
def test_rejects_invalid_job(spec, field, value):
    spec["jobs"][0][field] = value
    with pytest.raises(ValueError):
        batch.expand(spec)


def test_accepts_once_and_preserves_accepted_attempt(spec, matrix):
    plan = batch.expand(spec)
    root = Path(plan["output_root"])
    assert batch.run_batch(plan, matrix) == 0
    record, = rows(root / "accepted.jsonl")
    assert record["valid_round"] is True and record["route_completed"] is True
    assert record["rtf_window_median"] == 0.94 and record["rtf_delta_sim_over_delta_real"] == 0.88
    assert record["gpu"] == 3 and record["slot"] == 6 and record["frame_count"] == 1
    assert record["artifact_bytes"] >= record["episode_bytes"] > 40_000
    assert record["world_sha256"] == batch.file_hash(Path(spec["variant_world_root"]) / f"{STEM}.sdf")
    assert record["v9_config_sha256"] == batch.file_hash(batch.V9_CONFIG)
    assert len(record["code_commit"]) >= 40
    attempt = Path(record["attempt_dir"])
    assert (attempt / "flight/episodes").is_dir()
    assert (attempt / "scenario" / f"{STEM}.scenario.json").is_file()
    before = (root / "accepted.jsonl").stat().st_ino
    assert batch.run_batch(plan, matrix) == 0
    assert list(attempt.parent.iterdir()) == [attempt]
    assert (root / "accepted.jsonl").stat().st_ino == before
    assert rows(root / "results.jsonl") == [record]


@pytest.mark.parametrize("mode,reason", [
    ("invalid", "not_valid_round"), ("truthy", "not_valid_round"), ("short", "route_not_completed"),
    ("wrong_container", "episode_stats_container_mismatch"), ("missing", "missing_or_invalid_result_json"),
    ("low_rtf", "invalid_window_rtf"),
])
def test_rejects_and_cleans_only_the_failed_attempt(spec, matrix, monkeypatch, mode, reason):
    monkeypatch.setenv("FAKE_MATRIX_MODE", mode)
    plan = batch.expand(spec)
    root = Path(plan["output_root"])
    assert batch.run_batch(plan, matrix) == 1
    assert not (root / "accepted.jsonl").exists()
    failed, = rows(root / "results.jsonl")
    assert reason in failed["errors"]
    attempt = Path(failed["attempt_dir"])
    assert not (attempt / "flight/episodes").exists()
    assert (attempt / "stats.json").exists()
    monkeypatch.delenv("FAKE_MATRIX_MODE")
    assert batch.run_batch(plan, matrix) == 0
    accepted, = rows(root / "accepted.jsonl")
    assert accepted["attempt_dir"].endswith("/attempt-2")
    assert rows(root / "results.jsonl") == [failed, accepted]


def test_disk_pause_and_resume(spec, matrix, monkeypatch):
    spec["jobs"].append({**spec["jobs"][0], "seed": 43})
    plan = batch.expand(spec)
    root = Path(plan["output_root"])
    monkeypatch.setattr(batch, "MIN_FREE_BYTES", 100 * 10**9)
    free = iter((100 * 10**9, 100 * 10**9 - 1))
    monkeypatch.setattr(shutil, "disk_usage", lambda _: shutil._ntuple_diskusage(0, 0, next(free)))
    assert batch.run_batch(plan, matrix) == 75
    assert json.loads((root / "PAUSED").read_text())["next_job_id"] == plan["jobs"][1]["job_id"]
    assert len(rows(root / "results.jsonl")) == 1
    monkeypatch.setattr(shutil, "disk_usage", lambda _: shutil._ntuple_diskusage(0, 0, 100 * 10**9))
    assert batch.run_batch(plan, matrix) == 0
    assert not (root / "PAUSED").exists()
    assert [row["slot"] for row in rows(root / "accepted.jsonl")] == [6, 5]


def test_oversize_valid_round_is_rejected(spec, matrix):
    spec["jobs"][0]["max_artifact_bytes"] = 20_000
    plan = batch.expand(spec)
    root = Path(plan["output_root"])
    assert batch.run_batch(plan, matrix) == 1
    record, = rows(root / "results.jsonl")
    assert record["valid_round"] is True
    assert record["errors"] == ["artifact_size_limit_exceeded"]
    assert not (Path(record["attempt_dir"]) / "flight/episodes").exists()
    assert not (root / "accepted.jsonl").exists()


def test_interrupted_before_checkpoint_is_cleaned_and_recorded(spec, matrix):
    plan = batch.expand(spec)
    root = Path(plan["output_root"])
    old = root / "jobs" / plan["jobs"][0]["job_id"] / "attempt-1"
    (old / "flight/episodes/partial").mkdir(parents=True)
    assert batch.run_batch(plan, matrix) == 0
    interrupted, accepted = rows(root / "results.jsonl")
    assert interrupted["errors"] == ["interrupted_before_batch_verdict"]
    assert not (old / "flight/episodes").exists()
    assert accepted["attempt_dir"].endswith("/attempt-2")


def test_repairs_interrupted_results_append_from_verdict(spec, matrix):
    plan = batch.expand(spec)
    root = Path(plan["output_root"])
    assert batch.run_batch(plan, matrix) == 0
    results = root / "results.jsonl"
    complete = results.read_bytes()
    results.write_bytes(complete[:20])
    assert batch.run_batch(plan, matrix) == 0
    assert results.read_bytes() == complete


@pytest.mark.parametrize("valid", [True, False])
def test_real_shell_single_round_with_stubbed_tools(tmp_path, monkeypatch, valid):
    python = tmp_path / "fake-python"
    python.write_text('''#!/bin/bash
case "$1" in
  */long_range_flight.py)
    [[ "$MULTIDETECT_LONG_RANGE_CONTAINER_PREFIX" == md-rw-long-slot-5-test ]]
    mkdir -p "$4/episodes"
    printf '{"route_completed":true,"max_distance_from_spawn_m":4100}' > "$4/long-range-result.json"
    ;;
  */sample_stats.py)
    [[ "$2" == --container-prefix && "$3" == md-rw-long-slot-5-test- ]]
    printf '{"container_name":"md-rw-long-slot-5-test-episode","rtf_window_median":0.95,"errors":[],"measurement_complete":%s}' "$FAKE_VALID" > "${@: -1}"
    ;;
  *) exec "$FAKE_REAL_PYTHON" "$@" ;;
esac
''')
    nvidia = tmp_path / "nvidia-smi"
    nvidia.write_text('''#!/bin/bash
case "$3" in
  --query-gpu=uuid) echo GPU-123-abc ;;
  --query-gpu=memory.used) echo 0 ;;
  *) exit 0 ;;
esac
''')
    for command in (python, nvidia):
        command.chmod(0o755)
    for key, value in {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "TOOLS_PYTHON": str(python), "SITL_PYTHON": str(python), "FAKE_REAL_PYTHON": sys.executable,
        "FAKE_VALID": json.dumps(valid), "RTF_MATRIX_SINGLE_ROUND": "1", "RTF_MATRIX_ATTEMPT": "2",
        "RTF_MATRIX_RUN_TAG": "test", "RTF_MATRIX_GPU": "3", "RTF_MATRIX_SLOT": "5",
        "RTF_MATRIX_MIN_FREE_GB": "0", "RTF_MATRIX_CPU_MAX": "1000000000", "RTF_MATRIX_SHARE_GPU": "0",
        "RTF_MATRIX_WAIT_S": "1", "RTF_MATRIX_GPU_LOCK_ROOT": str(tmp_path / "locks"),
    }.items():
        monkeypatch.setenv(key, value)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([dict(name="fixture", scenario="unused", resolved_root=str(tmp_path), extent_m=6000)]))
    out = tmp_path / "attempt-2"
    result = subprocess.run([str(batch.ROOT / "scripts/realworld/rtf_matrix.sh"), "--manifest", str(manifest),
                             "--out", str(out)], capture_output=True, text=True)
    assert result.returncode == (0 if valid else 1), result.stderr
    raw = json.loads((out / "result.json").read_text())
    assert raw["valid_round"] is valid and raw["attempt"] == 2 and raw["gpu_uuid"] == "GPU-123-abc"
    assert (out / "flight/episodes").exists()  # Only the batch decides whether to remove it.
