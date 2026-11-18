#!/usr/bin/env python3
"""Run a resumable batch of prepared 6 km collection scenarios."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ROOT / "configs/scenarios/realworld/rc_cessna_mono_cam"
BATCHES = Path("/data/datasets/multidetect-sim-assets/wide-collect/batches")
AIRFRAME = "rc_cessna_mono_cam"
DELTA_AIRFRAME = "delta_tailsitter_gimbal_cam"
V9_CONFIG = ROOT / "simulation/gazebo/server-wide-area.config"
MIN_FREE_BYTES = 100 * 10**9
DEFAULT_MAX_BYTES = 8 * 1024**3
MIN_RTF = 0.9
MIN_DISTANCE_M = 4000
WEATHER = {
    ("noon", "clear"): "clear_noon",
    **{("noon", name): name for name in ("partly_cloudy", "overcast", "rain", "windy")},
    **{(name, "clear"): name for name in ("morning", "evening", "dusk", "night")},
}
ROOT_FIELDS = ("resolved_root", "overlay_root", "variant_model_root", "variant_world_root",
               "variant_map_root", "variant_terrain_root", "server_config")


def dump(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(data):
    return hashlib.sha256(dump(data).encode("utf-8")).hexdigest()


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_object(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return data


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_text(path, text):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=f".{path.name}.", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
            sync_dir(path.parent)
        finally:
            temporary.unlink(missing_ok=True)


def absolute(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty path")
    return Path(os.path.abspath(ROOT / Path(value).expanduser()))


def number(value):
    return type(value) in (float, int) and math.isfinite(value)


def validate_airframe(value, field):
    if value not in (AIRFRAME, DELTA_AIRFRAME):
        raise ValueError(f"{field} must be {AIRFRAME} or {DELTA_AIRFRAME}")
    return value


def expand(spec):
    unknown = spec.keys() - {"batch_id", "gpu", "slots", "share_gpu", "output_root", "jobs",
                             "airframe", "max_artifact_bytes", *ROOT_FIELDS}
    if unknown:
        raise ValueError(f"unknown batch fields: {', '.join(sorted(unknown))}")
    default_airframe = validate_airframe(spec.get("airframe", AIRFRAME), "airframe")
    gpu = spec.get("gpu", 1)
    slots = spec.get("slots", [5, 6, 7])
    share_gpu = spec.get("share_gpu")
    if type(gpu) is not int or gpu not in (1, 3):
        raise ValueError("gpu must be 1 or 3")
    if (not isinstance(slots, list) or not slots or
            any(type(slot) is not int or slot not in (5, 6, 7) for slot in slots) or
            len(set(slots)) != len(slots)):
        raise ValueError("slots must be a nonempty, unique subset of [5, 6, 7]")
    if share_gpu is not None and type(share_gpu) is not bool:
        raise ValueError("share_gpu must be boolean")
    raw_jobs = spec.get("jobs")
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise ValueError("jobs must be a nonempty array")

    jobs = {}
    for index, job in enumerate(raw_jobs):
        if not isinstance(job, dict):
            raise ValueError(f"jobs[{index}] must be an object")
        unknown = job.keys() - {"site", "scenario", "altitude_m", "time_of_day", "weather", "seed",
                                "airframe", "max_artifact_bytes", *ROOT_FIELDS}
        if unknown:
            raise ValueError(f"jobs[{index}] unknown fields: {', '.join(sorted(unknown))}")
        airframe = validate_airframe(job.get("airframe", default_airframe), f"jobs[{index}].airframe")
        site, altitude, seed = (job.get(key) for key in ("site", "altitude_m", "seed"))
        if not isinstance(site, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", site):
            raise ValueError(f"jobs[{index}].site must be a site slug")
        if not number(altitude) or not 0 < altitude <= 120:
            raise ValueError(f"jobs[{index}].altitude_m must be in (0, 120] m AGL")
        if type(seed) is not int or seed < 0:
            raise ValueError(f"jobs[{index}].seed must be a nonnegative integer")
        tod, wx = job.get("time_of_day"), job.get("weather")
        preset = WEATHER.get((tod, wx)) if isinstance(tod, str) and isinstance(wx, str) else None
        if preset is None:
            raise ValueError(f"jobs[{index}] unsupported time_of_day/weather: {tod!r}/{wx!r}")
        source = job.get("scenario", site)
        if source == site:
            scenario_dir = SCENARIOS if airframe == AIRFRAME else SCENARIOS.with_name(airframe)
            source = str(scenario_dir / f"{site}_w6km__{preset}.scenario.json")
        scenario = absolute(source, "scenario")
        if not re.fullmatch(rf"{re.escape(site)}_w6km[a-z0-9_]*__{preset}\.scenario\.json",
                            scenario.name):
            raise ValueError(f"jobs[{index}].scenario must match site, 6 km extent and {preset}: {scenario}")
        maximum = job.get("max_artifact_bytes", spec.get("max_artifact_bytes", DEFAULT_MAX_BYTES))
        if type(maximum) is not int or maximum <= 0:
            raise ValueError(f"jobs[{index}].max_artifact_bytes must be a positive integer")
        roots = {key: str(absolute(job.get(key, spec.get(key)), key)) for key in ROOT_FIELDS
                 if job.get(key, spec.get(key)) is not None}
        roots.setdefault("server_config", str(V9_CONFIG))
        roots.setdefault("resolved_root", str(ROOT / (
            ".tmp/realworld-wide-6km/models" if site == "rw_farm_iowa"
            else "artifacts/cache/gz-fuel/resolved")))
        inputs = dict(site=site, altitude_m=float(altitude), time_of_day=tod, weather=wx, seed=seed,
                      scenario=str(scenario), max_artifact_bytes=maximum)
        # A legacy Delta verdict does not cover the horizon gate.
        # Keep legacy RC identity and omit execution placement from capture identity.
        if airframe != AIRFRAME:
            inputs["airframe"] = airframe
        job_id = digest({**inputs, "roots": roots,
                         "schema_version": 2 if airframe == DELTA_AIRFRAME else 1})
        if job_id not in jobs:
            jobs[job_id] = {"job_id": job_id, "inputs": inputs, "weather_preset": preset,
                            "slot": slots[len(jobs) % len(slots)],
                            "matrix": {"name": job_id, "scenario": str(scenario), **roots,
                                       "variant": "V9", "extent_m": 6000, "airframe": airframe,
                                       "minimum_rtf": MIN_RTF, "minimum_max_distance_m": MIN_DISTANCE_M}}
    batch_id = spec.get("batch_id", digest({"jobs": list(jobs)})[:16])
    if not isinstance(batch_id, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}", batch_id):
        raise ValueError("batch_id must be a plain identifier")
    output_root = absolute(spec.get("output_root", str(BATCHES / batch_id)), "output_root")
    return {"schema_version": 1, "batch_id": batch_id, "output_root": str(output_root),
            "gpu": gpu, "slots": slots, "share_gpu": share_gpu, "jobs": list(jobs.values())}


def prepare(job, snapshot):
    from multidetect.terrain_dem import load_local_dem_grid
    from scripts.realworld.long_range_flight import load_mission, sidecar_path

    row = job["matrix"]
    airframe = row.get("airframe", AIRFRAME)
    source = Path(row["scenario"])
    manifest = read_object(source)
    channels = {}
    for key in ("runtime_input", "truth", "expectations"):
        channel = manifest["channels"][key]
        body = read_object(source.parent / channel["path"])
        if channel["sha256"] != digest(body):
            raise ValueError(f"{source}: {key} channel SHA-256 mismatch")
        channels[key] = body

    runtime = channels["runtime_input"]
    if runtime["vehicle"]["model"] != airframe:
        raise ValueError(f"{source}: runtime vehicle model must match batch airframe {airframe}")
    runtime["seed"] = job["inputs"]["seed"]
    world_id = manifest["scenario_id"]
    if world_id != source.name.removesuffix(".scenario.json"):
        raise ValueError(f"{source}: scenario_id must match filename")
    assets = {}
    for label, field, attr, uri, filename, root in (
        ("world", "variant_world_root", "world_asset", "gazebo", world_id, "simulation/gazebo/worlds"),
        ("map", "variant_map_root", "map_asset", "maps", f"{world_id}.geojson", "simulation/maps"),
        ("dem", "variant_terrain_root", "dem_asset", "terrain", f"{world_id}.dem.json", "simulation/terrain"),
    ):
        if runtime["world"][attr] != f"builtin://{uri}/{filename}":
            raise ValueError(f"{source}: unexpected {attr} binding")
        assets[label] = Path(row.get(field, ROOT / root)) / (f"{filename}.sdf" if label == "world" else filename)
    for key in ROOT_FIELDS[:-1]:
        if key in row and not Path(row[key]).is_dir():
            raise FileNotFoundError(f"{key} must be a local directory: {row[key]}")
    assets["v9_config"] = Path(row["server_config"])
    evidence = {key: {"path": str(path), "sha256": file_hash(path)} for key, path in assets.items()}

    mission = read_object(sidecar_path(source))
    if mission["extent_m"] != 6000:
        raise ValueError(f"{source}: long-range extent must be 6000 m")
    dem = load_local_dem_grid(assets["dem"])
    geometry = mission["mission_compile_runtime"]["mission_geometry"]
    home = mission["mission_compile_runtime"]["home_position"]

    def height(point):
        return dem.sample_wgs84(point["latitude_deg"], point["longitude_deg"]).elevation_m

    home_elevation = height(home)
    cruise = geometry["waypoints"]
    for point in cruise:
        point["altitude_m"] = height(point) - home_elevation + job["inputs"]["altitude_m"]
        point["altitude_reference"] = "relative_home"
    centerline = mission["task"]["geometry"]["centerline"]
    if len(centerline) == len(cruise) + 2:
        mission["task"]["geometry"]["centerline"] = [centerline[0], *cruise, centerline[-1]]
    elif airframe == DELTA_AIRFRAME and len(centerline) == len(cruise):
        mission["task"]["geometry"]["centerline"] = cruise
    else:
        raise ValueError(f"{source}: expected a centerline matching the waypoints")

    snapshot.mkdir()
    staged = snapshot / source.name
    for key, body in channels.items():
        name = f"{world_id}.{key}.json"
        atomic_text(snapshot / name, dump(body) + "\n")
        manifest["channels"][key] = {"path": name, "sha256": digest(body)}
    atomic_text(staged, dump(manifest) + "\n")
    atomic_text(sidecar_path(staged), dump(mission) + "\n")
    if airframe == AIRFRAME:
        load_mission(staged, minimum_rtf=MIN_RTF)
    else:
        load_mission(staged, minimum_rtf=MIN_RTF, dem_path=assets["dem"])
    return staged, evidence


def size_and_frames(attempt, *, airframe=AIRFRAME):
    total = episode_bytes = frames = 0
    episodes = attempt / "flight/episodes"
    for root, _, files in os.walk(attempt):
        for name in files:
            path = Path(root) / name
            if path.is_symlink():
                continue
            size = path.stat().st_size
            total += size
            if path.is_relative_to(episodes):
                episode_bytes += size
                if size == 0:
                    continue
                if airframe == DELTA_AIRFRAME:
                    episode_dir_parts = path.parent.relative_to(episodes).parts
                    is_frame = (
                        episode_dir_parts[1:] == ("video", "rgb")
                        and path.suffix in (".jpg", ".jpeg")
                    )
                else:
                    is_frame = path.parent.name == "rgb" and path.suffix == ".png"
                if is_frame:
                    frames += 1
    return total, episode_bytes, frames


def remove_episodes(attempt):
    flight = attempt / "flight"
    episodes = flight / "episodes"
    if not flight.is_symlink():
        if episodes.is_symlink():
            episodes.unlink()
        elif episodes.exists():
            shutil.rmtree(episodes)


def evaluate(attempt, job, batch, rc):
    airframe = job["matrix"].get("airframe", AIRFRAME)
    artifact_bytes, episode_bytes, frames = size_and_frames(attempt, airframe=airframe)
    errors = []
    if artifact_bytes > job["inputs"]["max_artifact_bytes"]:
        errors.append("artifact_size_limit_exceeded")
    if rc != 0:
        errors.append("matrix_command_failed")
    try:
        raw = read_object(attempt / "result.json")
    except (OSError, ValueError):
        raw = {}
        errors.append("missing_or_invalid_result_json")
    if raw.get("valid_round") is not True:
        errors.append("not_valid_round")
    if raw.get("route_completed") is not True:
        errors.append("route_not_completed")
    if raw.get("name") != job["job_id"] or str(raw.get("gpu")) != str(batch["gpu"]):
        errors.append("matrix_job_or_gpu_mismatch")
    if raw.get("airframe", AIRFRAME) != airframe:
        errors.append("matrix_airframe_mismatch")
    rtf = raw.get("rtf_window_median")
    ratio = raw.get("rtf_delta_sim_over_delta_real")
    distance = raw.get("max_distance_from_spawn_m")
    if not number(rtf) or rtf < MIN_RTF or not number(ratio) or ratio <= 0:
        errors.append("invalid_window_rtf")
    if not number(distance) or distance < MIN_DISTANCE_M:
        errors.append("distance_below_minimum")
    if frames == 0:
        errors.append("no_rgb_frames")
    episode_dirs = list((attempt / "flight/episodes").glob("*/episode.json"))
    try:
        episode = read_object(episode_dirs[0]) if len(episode_dirs) == 1 else {}
    except (OSError, ValueError):
        episode = {}
    identity = episode.get("simulator_container", {})
    container_id = raw.get("container_id")
    container_name = raw.get("container_name")
    if (not isinstance(identity, dict) or not container_id or not isinstance(container_name, str) or
            f"-slot-{job['slot']}-" not in container_name or
            identity.get("container_id") != container_id or identity.get("container_name") != container_name):
        errors.append("episode_stats_container_mismatch")
    if episode.get("status") != "complete":
        errors.append("episode_not_complete")
    gimbal_evidence = {}
    if airframe == DELTA_AIRFRAME:
        from multidetect.sim_teacher.gimbal_recording import check_gimbal_manifest
        from scripts.realworld.horizon_report import summarize as summarize_horizon

        gimbal_check = episode.get("gimbal_check")
        manifest = (episode_dirs[0].parent / "video" / "manifest.jsonl"
                    if len(episode_dirs) == 1 else None)
        current = (check_gimbal_manifest(manifest)
                   if manifest is not None else {"passed": False, "frame_count": 0})
        if (not isinstance(gimbal_check, dict) or gimbal_check.get("passed") is not True
                or current["passed"] is not True
                or current["frame_count"] != frames
                or gimbal_check.get("frame_count") != current["frame_count"]):
            errors.append("gimbal_state_missing")
        horizon = {"passed": False, "errors": ["horizon_data_missing"]}
        if manifest is not None:
            try:
                # Batch jobs request the default lock mode. Do not silently
                # admit a tracking capture under the lock collection identity.
                horizon = summarize_horizon(manifest, expected_mode="lock")["horizon_acceptance"]
            except (OSError, ValueError, TypeError, AttributeError, OverflowError) as exc:
                horizon["error"] = str(exc)
        errors.extend(horizon["errors"])
        gimbal_evidence["horizon_acceptance"] = horizon
    return {"accepted": not errors, "errors": errors, "matrix_returncode": rc,
            "valid_round": raw.get("valid_round") is True, "route_completed": raw.get("route_completed") is True,
            "round_errors": raw.get("round_errors", []), "container_id": container_id,
            "container_name": container_name, "gpu_uuid": raw.get("gpu_uuid"),
            "rtf_window_median": rtf if number(rtf) else None,
            "rtf_delta_sim_over_delta_real": ratio if number(ratio) else None,
            "max_distance_from_spawn_m": distance if number(distance) else None,
            "frame_count": frames, "artifact_bytes": artifact_bytes, "episode_bytes": episode_bytes,
            **gimbal_evidence}


def indexed(path, key, *, repair_tail=False):
    if not path.exists():
        return {}
    rows = []
    with path.open("r+b" if repair_tail else "rb") as stream:
        while line := stream.readline():
            if repair_tail and not line.endswith(b"\n"):
                # A crash during append can leave a torn final record. Its verdict is checkpointed.
                stream.truncate(stream.tell() - len(line))
                os.fsync(stream.fileno())
                break
            if line.strip():
                rows.append(json.loads(line))
    index = {row[key]: row for row in rows}
    if len(index) != len(rows):
        raise ValueError(f"duplicate {key} in {path}")
    return index


def now():
    return datetime.now(timezone.utc).isoformat()


def run_attempt(attempt, job, batch, command, lock_fd):
    record = {"job_id": job["job_id"], "attempt_id": f"{job['job_id']}/{attempt.name}",
              "attempt_dir": str(attempt), "gpu": batch["gpu"], "slot": job["slot"],
              "inputs": job["inputs"], "started_at": now()}
    try:
        staged, assets = prepare(job, attempt / "scenario")
        record["assets"] = assets
        record["world_sha256"] = assets["world"]["sha256"]
        record["v9_config_sha256"] = assets["v9_config"]["sha256"]
        record["code_commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        record["batch_script_sha256"] = file_hash(Path(__file__))
        record["matrix_script_sha256"] = file_hash(command)
        env = dict(os.environ, RTF_MATRIX_GPU=str(batch["gpu"]), RTF_MATRIX_SLOT=str(job["slot"]),
                   RTF_MATRIX_SINGLE_ROUND="1", RTF_MATRIX_ATTEMPT=attempt.name.split("-")[-1],
                   RTF_MATRIX_RUN_TAG=hashlib.sha256(os.urandom(16)).hexdigest()[:16],
                   MULTIDETECT_SITL_SLOTS="8")
        if batch["share_gpu"] is not None:
            env["RTF_MATRIX_SHARE_GPU"] = str(int(batch["share_gpu"]))
        manifest = attempt / "manifest.json"
        atomic_text(manifest, dump([{**job["matrix"], "scenario": str(staged)}]) + "\n")
        with (attempt / "matrix.stdout.log").open("wb") as log:
            proc = subprocess.run([str(command), "--manifest", str(manifest), "--out", str(attempt)],
                                  cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                  pass_fds=(lock_fd,), check=False)
        record.update(evaluate(attempt, job, batch, proc.returncode))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        record.update(accepted=False, errors=[f"attempt_error: {exc}"])
    record["finished_at"] = now()
    atomic_text(attempt / "batch-result.json", dump(record) + "\n")
    return record


def run_batch(batch, command):
    root = Path(batch["output_root"])
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError(f"batch is already running: {root}") from exc
        return run_locked(batch, command, lock.fileno())


def run_locked(batch, command, lock_fd):
    root = Path(batch["output_root"])
    manifest = root / "manifest.json"
    accepted_path, results_path, paused = (root / name for name in ("accepted.jsonl", "results.jsonl", "PAUSED"))
    if manifest.exists() and read_object(manifest)["batch_id"] != batch["batch_id"]:
        raise ValueError(f"output_root already belongs to another batch: {root}")
    atomic_text(manifest, dump(batch) + "\n")
    accepted = indexed(accepted_path, "job_id")
    results = indexed(results_path, "attempt_id", repair_tail=True)

    def publish(attempt, record):
        job_id = record["job_id"]
        if record["accepted"]:
            if job_id not in accepted:
                accepted[job_id] = record
                atomic_text(accepted_path, "".join(dump(row) + "\n" for row in accepted.values()))
        else:
            remove_episodes(attempt)
        if record["attempt_id"] not in results:
            with results_path.open("a", encoding="utf-8") as output:
                output.write(dump(record) + "\n")
                output.flush()
                os.fsync(output.fileno())
            sync_dir(root)
            results[record["attempt_id"]] = record

    pending = False
    for job in batch["jobs"]:
        job_id = job["job_id"]
        job_dir = root / "jobs" / job_id
        attempts = sorted((path for path in job_dir.glob("attempt-*")
                           if path.is_dir() and re.fullmatch(r"attempt-[1-9][0-9]*", path.name)),
                          key=lambda path: int(path.name.split("-")[-1]))
        for attempt in attempts:
            attempt_id = f"{job_id}/{attempt.name}"
            if attempt_id in results:
                continue
            checkpoint = attempt / "batch-result.json"
            if checkpoint.exists():
                record = read_object(checkpoint)
            else:
                record = {"job_id": job_id, "attempt_id": attempt_id, "attempt_dir": str(attempt),
                          "accepted": False, "errors": ["interrupted_before_batch_verdict"], "finished_at": now()}
                atomic_text(checkpoint, dump(record) + "\n")
            if record["attempt_id"] != attempt_id or record["job_id"] != job_id:
                raise ValueError(f"attempt checkpoint identity mismatch: {checkpoint}")
            publish(attempt, record)
        if job_id in accepted:
            continue
        free = shutil.disk_usage(root).free
        if free < MIN_FREE_BYTES:
            atomic_text(paused, dump({"reason": "low_disk_space", "free_bytes": free,
                                      "minimum_free_bytes": MIN_FREE_BYTES, "next_job_id": job_id,
                                      "at": now()}) + "\n")
            print(f"PAUSED: {free} bytes free; next job {job_id}", file=sys.stderr)
            return 75
        paused.unlink(missing_ok=True)
        next_attempt = 1 if not attempts else int(attempts[-1].name.split("-")[-1]) + 1
        attempt = job_dir / f"attempt-{next_attempt}"
        attempt.mkdir(parents=True)
        record = run_attempt(attempt, job, batch, command, lock_fd)
        publish(attempt, record)
        pending |= not record["accepted"]
        print(f"{job_id} {attempt.name}: {'accepted' if record['accepted'] else 'rejected'}")
    paused.unlink(missing_ok=True)
    return int(pending)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("--plan-only", action="store_true", help="print expanded jobs without launching anything")
    parser.add_argument("--matrix-command", type=Path, default=Path(__file__).with_name("rtf_matrix.sh"))
    args = parser.parse_args(argv)
    try:
        batch = expand(read_object(args.spec))
        if args.plan_only:
            print(json.dumps(batch, ensure_ascii=False, indent=2))
            return 0
        return run_batch(batch, args.matrix_command.resolve())
    except (OSError, ValueError) as exc:
        parser.exit(2, f"{parser.prog}: {exc}\n")


if __name__ == "__main__":
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]
    raise SystemExit(main())
