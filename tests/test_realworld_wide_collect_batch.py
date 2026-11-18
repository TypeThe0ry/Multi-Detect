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
DELTA = "delta_tailsitter_gimbal_cam"


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
    from scripts.realworld import collection_guard
    monkeypatch.setattr(collection_guard, "gpu_blockers", lambda _gpu: [])
    monkeypatch.setattr(collection_guard.os, "getloadavg", lambda: (1.0, 1.0, 1.0))
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
assert row["extent_m"] == 6000
assert row["airframe"] == os.environ.get("FAKE_EXPECTED_AIRFRAME", "rc_cessna_mono_cam")
mode = os.environ.get("FAKE_MATRIX_MODE")
slot = os.environ["RTF_MATRIX_SLOT"]
gpu = os.environ["RTF_MATRIX_GPU"]
name = f"md-rw-long-slot-{slot}-fake"
episode = a.out / "flight/episodes/episode-fixture"
if row["airframe"] == "delta_tailsitter_gimbal_cam":
    rgb = episode / "video/rgb"
    rgb.mkdir(parents=True)
    (rgb / "000001.jpg").write_bytes(b"data" * 10_000)
else:
    (episode / "rgb").mkdir(parents=True)
    (episode / "rgb/000001.png").write_bytes(b"data" * 10_000)
identity = {"container_id": "a" * 64, "container_name": name}
metadata = {"status": "complete", "simulator_container": identity}
if row["airframe"] == "delta_tailsitter_gimbal_cam":
    frame = {"frame_index": 0, "sensor_stamp_ns": 1000,
             "gimbal_state": {"roll_rad": 0, "pitch_rad": 0, "yaw_rad": 0,
                              "reference_frame": "body_FRD", "source": "gazebo_joint_state",
                              "timestamp_ns": 1000, "frame_timestamp_ns": 1000,
                              "age_ms": 0, "skew_ms": 0, "fresh": True,
                              "stabilization": {"mode": "lock", "measurement_fresh": True,
                                                "horizon_feasible": True, "horizon_error_deg": 1.0,
                                                "horizon_degraded": False, "yaw_degraded": True,
                                                "yaw_error_deg": 32.0}},
             "camera_pose": {"center_ned": [0, 0, 0], "optical_to_world": [1,0,0,0,1,0,0,0,1],
                             "reference_frame": "NED", "extrinsics_source": "fixture.sdf", "age_ms": 0}}
    if mode == "missing_horizon":
        del frame["gimbal_state"]["stabilization"]
    elif mode == "bad_horizon":
        frame["gimbal_state"]["stabilization"]["horizon_error_deg"] = 9.0
    elif mode == "track":
        frame["gimbal_state"]["stabilization"].update(
            mode="track", target_ref={"kind": "world_point", "lat_deg": 42, "lon_deg": -93, "alt_m": 300},
            pointing_error_deg=0.1, target_reachable=True, horizon_feasible_given_target=True)
    (episode / "video/manifest.jsonl").write_text(json.dumps(frame) + chr(10))
    metadata["gimbal_check"] = {"passed": True, "frame_count": 1}
(episode / "episode.json").write_text(json.dumps(metadata))
(a.out / "stats.json").write_text("{}")
result = {**identity, "name": row["name"], "gpu": gpu, "valid_round": True,
          "route_completed": True, "rtf_window_median": 0.94,
          "rtf_delta_sim_over_delta_real": 0.88, "max_distance_from_spawn_m": 4100}
if row["airframe"] != "rc_cessna_mono_cam":
    result["airframe"] = row["airframe"]
if mode == "wrong_airframe":
    result["airframe"] = "rc_cessna_mono_cam"
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
    legacy_job = plan["jobs"][0]
    assert "airframe" not in legacy_job["inputs"]
    assert legacy_job["job_id"] == batch.digest({
        **legacy_job["inputs"],
        "roots": {key: legacy_job["matrix"][key] for key in batch.ROOT_FIELDS if key in legacy_job["matrix"]},
        "schema_version": 1,
    })


def test_expand_airframe_defaults_overrides_and_identity(spec):
    original = batch.expand(spec)
    job = spec["jobs"][0]
    rc, delta = batch.AIRFRAME, DELTA
    assert batch.expand({**spec, "airframe": rc}) == original
    plan = batch.expand({**spec, "airframe": delta, "jobs": [job, {**job, "airframe": rc}]})
    inherited, overridden = plan["jobs"]
    delta_scenario = batch.ROOT / "configs/scenarios/realworld" / delta / f"{STEM}.scenario.json"
    assert inherited["matrix"]["scenario"] == str(delta_scenario)
    assert inherited["inputs"]["airframe"] == inherited["matrix"]["airframe"] == delta
    assert overridden["job_id"] == original["jobs"][0]["job_id"]
    assert inherited["job_id"] != batch.digest({
        **inherited["inputs"],
        "roots": {key: inherited["matrix"][key] for key in batch.ROOT_FIELDS if key in inherited["matrix"]},
        "schema_version": 1,
    })
    explicit_source = {**job, "scenario": inherited["matrix"]["scenario"]}
    explicit = batch.expand({**spec, "jobs": [
        {**explicit_source, "airframe": delta}, {**explicit_source, "airframe": rc},
        {**explicit_source, "airframe": delta},
    ]})
    assert [item["matrix"]["airframe"] for item in explicit["jobs"]] == [delta, rc]
    assert explicit["jobs"][0]["job_id"] == inherited["job_id"]
    assert explicit["jobs"][1]["job_id"] != inherited["job_id"]


@pytest.mark.parametrize("value", [None, "../delta_tailsitter_gimbal_cam", "x500", False])
def test_rejects_invalid_airframe(spec, value):
    with pytest.raises(ValueError, match="^airframe must be"):
        batch.expand({**spec, "airframe": value})
    spec["jobs"][0]["airframe"] = value
    with pytest.raises(ValueError, match=r"^jobs\[0\]\.airframe must be"):
        batch.expand(spec)


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


def test_delta_stages_vtol_geometry_and_checks_matrix_airframe(spec, matrix, tmp_path, monkeypatch):
    from multidetect.px4_mission import VtolMissionGeometry
    from multidetect.terrain_dem import load_local_dem_grid
    from scripts.realworld import long_range_flight

    rc = batch.SCENARIOS / f"{STEM}.scenario.json"
    source = tmp_path / rc.name
    manifest = batch.read_object(rc)
    for key, channel in manifest["channels"].items():
        body = batch.read_object(rc.with_name(channel["path"]))
        if key == "runtime_input":
            body["vehicle"].update(model=DELTA, airframe_profile_id="delta_tailsitter_gimbal")
            channel["sha256"] = batch.digest(body)
        source.with_name(channel["path"]).write_text(batch.dump(body))
    source.write_text(batch.dump(manifest))

    mission = batch.read_object(long_range_flight.sidecar_path(rc))
    mission["airframe"] = DELTA
    mission["kind"] = "vtol"
    runtime = mission["mission_compile_runtime"]
    runtime["vehicle_class"] = "vtol"
    fixed_wing = runtime["mission_geometry"]
    geometry = {key: fixed_wing[key] for key in (
        "waypoints", "landing", "takeoff_altitude_m", "acceptance_radius_m",
    )}
    geometry.update(loiter_time_s=1.0, loiter_radius_m=40.0)
    runtime["mission_geometry"] = geometry
    VtolMissionGeometry.from_runtime_mapping(runtime)
    mission["task"]["geometry"]["centerline"] = geometry["waypoints"]
    long_range_flight.sidecar_path(source).write_text(batch.dump(mission))
    terrain = tmp_path / "terrain"
    terrain.mkdir()
    dem_override = terrain / f"{STEM}.dem.json"
    shutil.copyfile(batch.ROOT / "simulation/terrain" / dem_override.name, dem_override)
    spec["variant_terrain_root"] = str(terrain)
    dem = load_local_dem_grid(dem_override)

    def height(point):
        return dem.sample_wgs84(point["latitude_deg"], point["longitude_deg"]).elevation_m

    def check_staged(staged, *, minimum_rtf, dem_path):
        assert minimum_rtf == 0.9
        assert dem_path == dem_override
        staged_mission = batch.read_object(long_range_flight.sidecar_path(staged))
        staged_runtime = staged_mission["mission_compile_runtime"]
        parsed = VtolMissionGeometry.from_runtime_mapping(staged_runtime)
        assert staged_runtime.keys() == runtime.keys()
        staged_geometry = staged_runtime["mission_geometry"]
        assert {**staged_geometry, "waypoints": geometry["waypoints"]} == geometry
        for point, original in zip(parsed.waypoints, geometry["waypoints"], strict=True):
            expected = 90 + height(original) - height(runtime["home_position"])
            assert point.altitude_m == pytest.approx(expected)
        assert staged_mission["task"]["geometry"]["centerline"] == staged_geometry["waypoints"]

    monkeypatch.setattr(long_range_flight, "load_mission", check_staged)
    spec["jobs"][0].update(airframe=DELTA, scenario=str(source))
    monkeypatch.setenv("FAKE_EXPECTED_AIRFRAME", DELTA)
    monkeypatch.setenv("FAKE_MATRIX_MODE", "wrong_airframe")
    plan = batch.expand(spec)
    wrong_model, = batch.expand({**spec, "jobs": [{**spec["jobs"][0], "airframe": batch.AIRFRAME}]})["jobs"]
    with pytest.raises(ValueError, match="runtime vehicle model must match batch airframe"):
        batch.prepare(wrong_model, tmp_path / "wrong-model")
    root = Path(plan["output_root"])
    assert batch.run_batch(plan, matrix) == 1
    rejected, = rows(root / "results.jsonl")
    assert rejected["errors"] == ["matrix_airframe_mismatch"]
    monkeypatch.setenv("FAKE_MATRIX_MODE", "missing_horizon")
    assert batch.run_batch(plan, matrix) == 1
    missing = rows(root / "results.jsonl")[-1]
    assert "horizon_data_missing" in missing["errors"]
    assert missing["horizon_acceptance"]["missing_frames"] == 1
    monkeypatch.setenv("FAKE_MATRIX_MODE", "bad_horizon")
    assert batch.run_batch(plan, matrix) == 1
    bad = rows(root / "results.jsonl")[-1]
    assert bad["errors"] == ["horizon_feasible_p95_exceeded", "horizon_feasible_max_exceeded"]
    monkeypatch.setenv("FAKE_MATRIX_MODE", "track")
    assert batch.run_batch(plan, matrix) == 1
    unexpected_track = rows(root / "results.jsonl")[-1]
    assert "horizon_unexpected_track_frames" in unexpected_track["errors"]
    assert not (root / "accepted.jsonl").exists()
    monkeypatch.delenv("FAKE_MATRIX_MODE")
    assert batch.run_batch(plan, matrix) == 0
    accepted, = rows(root / "accepted.jsonl")
    assert accepted["inputs"]["airframe"] == DELTA
    assert accepted["frame_count"] == 1
    assert accepted["horizon_acceptance"]["passed"] is True
    assert accepted["horizon_acceptance"]["yaw_degraded_fraction"] == 1


def test_size_and_frames_counts_only_the_airframe_layout(tmp_path):
    episode = tmp_path / "flight/episodes/episode-fixture"
    for name, contents in {
        "video/rgb/1.jpg": b"a", "video/rgb/2.jpeg": b"b", "video/rgb/3.jpg": b"",
        "video/aux/1.jpg": b"c", "rgb/1.jpg": b"d", "rgb/1.png": b"e",
    }.items():
        frame = episode / name
        frame.parent.mkdir(parents=True, exist_ok=True)
        frame.write_bytes(contents)
    assert batch.size_and_frames(tmp_path, airframe=DELTA) == (5, 5, 2)
    assert batch.size_and_frames(tmp_path) == (5, 5, 1)


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


def test_voided_attempt_requeues_at_tail_and_records_load_and_retry_count(spec, matrix, monkeypatch):
    spec["jobs"].append({**spec["jobs"][0], "seed": 43})
    spec["retry_backoff_s"] = 0
    spec["retry_backoff_max_s"] = 0
    plan = batch.expand(spec)
    seen = []

    def attempt(path, job, _batch, _command, _lock_fd, gate):
        seen.append(job["inputs"]["seed"])
        void = job["inputs"]["seed"] == 42 and seen.count(42) == 1
        return {"job_id": job["job_id"], "attempt_id": f"{job['job_id']}/{path.name}",
                "attempt_dir": str(path), "accepted": not void, "errors": ["voided:load_spike"] if void else [],
                "voided_reason": "load_spike" if void else None, "start_load": gate["start_load"],
                "max_load": 135 if void else 47, "valid_round": not void, "frame_count": 1}

    monkeypatch.setattr(batch, "run_attempt", attempt)
    monkeypatch.setattr("scripts.realworld.collection_guard.wait_for_start",
                        lambda *_: {"start_load": 47, "waited_s": 0})
    assert batch.run_batch(plan, matrix) == 0
    assert seen == [42, 43, 42]
    records = rows(Path(plan["output_root"]) / "results.jsonl")
    assert records[0]["voided_reason"] == "load_spike"
    assert records[0]["start_load"] == 47 and records[0]["max_load"] == 135
    assert records[0]["requeue_count"] == 1
    assert records[0]["retry_exhausted"] is False
    assert records[0]["next_retry_at"] is not None
    assert records[2]["accepted"] is True


def test_void_retry_limit_and_accepted_job_are_persistent(spec, matrix, monkeypatch):
    spec["jobs"].append({**spec["jobs"][0], "seed": 43})
    spec["max_retries"] = 1
    spec["retry_backoff_s"] = spec["retry_backoff_max_s"] = 0
    plan = batch.expand(spec)
    calls = []

    def attempt(path, job, _batch, _command, _lock_fd, gate):
        calls.append(job["inputs"]["seed"])
        void = job["inputs"]["seed"] == 42
        return {"job_id": job["job_id"], "attempt_id": f"{job['job_id']}/{path.name}",
                "attempt_dir": str(path), "inputs": job["inputs"], "accepted": not void, "errors": [],
                "voided_reason": "rtf_drop" if void else None, "start_load": gate["start_load"],
                "max_load": 47, "valid_round": not void, "frame_count": 1}

    monkeypatch.setattr(batch, "run_attempt", attempt)
    monkeypatch.setattr("scripts.realworld.collection_guard.wait_for_start",
                        lambda *_: {"start_load": 47, "waited_s": 0})
    assert batch.run_batch(plan, matrix) == 1
    assert calls == [42, 43, 42]
    assert batch.run_batch(plan, matrix) == 1
    assert calls == [42, 43, 42]
    records = rows(Path(plan["output_root"]) / "results.jsonl")
    assert [row["requeue_count"] for row in records if row["job_id"] == plan["jobs"][0]["job_id"]] == [1, 1]
    assert records[-1]["retry_exhausted"] is True
    accepted = rows(Path(plan["output_root"]) / "accepted.jsonl")
    assert [row["inputs"]["seed"] for row in accepted] == [43]


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


@pytest.mark.parametrize("airframe,valid,single_round", [
    (None, True, True), (None, False, True), (DELTA, True, True), (DELTA, False, True),
    (DELTA, True, False),
])
def test_real_shell_with_stubbed_tools(tmp_path, monkeypatch, airframe, valid, single_round):
    python = tmp_path / "fake-python"
    python.write_text('''#!/bin/bash
set -e
case "$1" in
  */collection_guard.py)
    action="$2"; shift 2
    while (($#)); do case "$1" in --out) out="$2"; shift 2;; --flight-command) flight_json="$2"; shift 2;; --stats-command) stats_json="$2"; shift 2;; *) shift;; esac; done
    mkdir -p "$out"
    if [[ "$action" == gate ]]; then printf '{"start_load":1,"waited_s":0}' > "$out/gate.json"; exit 0; fi
    flight_out=$("$FAKE_REAL_PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1])[4])' "$flight_json")
    stats_out=$("$FAKE_REAL_PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1])[-1])' "$stats_json")
    frame_dir="$flight_out/episodes/episode-fixture/rgb"; frame_name=000001.png
    if [[ "$FAKE_EXPECTED_AIRFRAME" == delta_tailsitter_gimbal_cam ]]; then frame_dir="$flight_out/episodes/episode-fixture/video/rgb"; frame_name=000001.jpg; fi
    mkdir -p "$frame_dir"
    printf '{"route_completed":true,"max_distance_from_spawn_m":4100}' > "$flight_out/long-range-result.json"
    printf 'data' > "$frame_dir/$frame_name"
    printf '{"status":"complete","simulator_container":{"container_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","container_name":"md-rw-long-slot-5-test-episode"}}' > "$flight_out/episodes/episode-fixture/episode.json"
    printf '{"container_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","container_name":"md-rw-long-slot-5-test-episode","measurement_complete":%s,"rtf_window_median":0.95,"errors":[]}' "$FAKE_VALID" > "$stats_out"
    printf '{"start_load":1,"max_load":1,"voided_reason":null,"flight_returncode":0,"stats_returncode":0}' > "$out/guard.json"
    exit 0
    ;;
  */long_range_flight.py)
    [[ "$MULTIDETECT_LONG_RANGE_CONTAINER_PREFIX" == md-rw-long-slot-5-test ]]
    if [[ -n "$FAKE_EXPECTED_AIRFRAME" ]]; then
      [[ "$#" == 6 && "$5" == --airframe && "$6" == "$FAKE_EXPECTED_AIRFRAME" ]]
    else
      [[ "$#" == 4 ]]
    fi
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
        "FAKE_EXPECTED_AIRFRAME": airframe or "",
        "FAKE_VALID": json.dumps(valid), "RTF_MATRIX_SINGLE_ROUND": str(int(single_round)),
        "RTF_MATRIX_ATTEMPT": "2", "RTF_MATRIX_GAP_S": "0", "RTF_MATRIX_MAX_ATTEMPTS": "1",
        "RTF_MATRIX_RUN_TAG": "test", "RTF_MATRIX_GPU": "3", "RTF_MATRIX_SLOT": "5",
        "RTF_MATRIX_MIN_FREE_GB": "0", "RTF_MATRIX_CPU_MAX": "1000000000", "RTF_MATRIX_SHARE_GPU": "0",
        "RTF_MATRIX_WAIT_S": "1", "RTF_MATRIX_GPU_LOCK_ROOT": str(tmp_path / "locks"),
    }.items():
        monkeypatch.setenv(key, value)
    manifest = tmp_path / "manifest.json"
    row = dict(name="fixture", scenario="unused", resolved_root=str(tmp_path), extent_m=6000)
    if airframe is not None:
        row["airframe"] = airframe
    manifest.write_text(json.dumps([row]))
    out = tmp_path / "out"
    result = subprocess.run([str(batch.ROOT / "scripts/realworld/rtf_matrix.sh"), "--manifest", str(manifest),
                             "--out", str(out)], capture_output=True, text=True)
    assert result.returncode == (0 if valid else 1), result.stderr
    attempt = out if single_round else out / "fixture/attempt-1"
    raw = json.loads((attempt / "result.json").read_text())
    assert raw["valid_round"] is valid
    assert raw["attempt"] == (2 if single_round else 1)
    assert raw["gpu_uuid"] == ("GPU-123-abc" if single_round else None)
    assert raw["airframe"] == (airframe or batch.AIRFRAME)
    assert raw["flight_returncode"] == raw["stats_returncode"] == 0
    assert (attempt / "flight/episodes").exists()
    if not single_round:
        assert json.loads((out / "summary.json").read_text())["all_requested_rounds_valid"] is True


@pytest.mark.parametrize("single_round", ["0", "1"])
def test_real_shell_rejects_invalid_airframe_before_running(tmp_path, monkeypatch, single_round):
    manifest = tmp_path / "manifest.json"
    row = dict(name="fixture", scenario="unused", resolved_root=str(tmp_path), extent_m=6000)
    manifest.write_text(json.dumps([{**row, "airframe": "unknown"}]))
    out = tmp_path / "out"
    for key, value in {
        "SITL_PYTHON": sys.executable, "TOOLS_PYTHON": sys.executable,
        "RTF_MATRIX_SINGLE_ROUND": single_round, "RTF_MATRIX_SLOT": "5", "RTF_MATRIX_GPU": "1",
        "MULTIDETECT_SITL_SLOTS": "8",
    }.items():
        monkeypatch.setenv(key, value)
    proc = subprocess.run([str(batch.ROOT / "scripts/realworld/rtf_matrix.sh"), "--manifest", str(manifest),
                           "--out", str(out)], capture_output=True, text=True)
    assert proc.returncode == 2 and "airframe" in proc.stderr
    assert not list(out.iterdir())
