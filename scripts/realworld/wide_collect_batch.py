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
import time
from collections import deque


ROOT = Path(__file__).resolve().parents[2]
if __name__ == "__main__":
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from scripts.realworld.collection_runtime import read_episode_metadata, runtime_evidence, runtime_options  # noqa: E402
from scripts.realworld import x1_scenario_class as policy  # noqa: E402

SCENARIOS = ROOT / "configs/scenarios/realworld/rc_cessna_mono_cam"
BATCHES = Path("/data/datasets/multidetect-sim-assets/wide-collect/batches")
AIRFRAME = "rc_cessna_mono_cam"
DELTA_AIRFRAME = "delta_tailsitter_gimbal_cam"
V9_CONFIG = ROOT / "simulation/gazebo/server-wide-area.config"
MIN_FREE_BYTES = int(float(os.environ.get("WIDE_COLLECT_MIN_FREE_GB", "100")) * 10**9)
DEFAULT_MAX_BYTES = 8 * 1024**3
MIN_RTF = 0.9
MIN_DISTANCE_M = policy.WIDE_DISTANCE_M
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


def distance_fields(job):
    row = job["matrix"]
    return {"scenario_class": row.get("scenario_class", policy.WIDE_AREA),
            "minimum_max_distance_m": row.get("minimum_max_distance_m", MIN_DISTANCE_M)}


def distance_identity_matches(record, job):
    expected = distance_fields(job)
    for key, default in (("scenario_class", policy.WIDE_AREA), ("minimum_max_distance_m", MIN_DISTANCE_M)):
        if record.get(key, default) != expected[key]:
            return False
    return True


def meets_distance(record, job):
    distance = record.get("max_distance_from_spawn_m")
    return (distance_identity_matches(record, job) and number(distance)
            and distance >= distance_fields(job)["minimum_max_distance_m"])


def class_counts(jobs, accepted=()):
    counts = {scenario_class: {"jobs": 0, "accepted_jobs": 0}
              for scenario_class in (policy.WIDE_AREA, policy.MOUNTAIN_SHORT)}
    for job in jobs:
        count = counts[distance_fields(job)["scenario_class"]]
        count["jobs"] += 1
        count["accepted_jobs"] += job["job_id"] in accepted
    return counts


def expand(spec):
    unknown = spec.keys() - {"batch_id", "gpu", "slots", "share_gpu", "output_root", "jobs",
                             "airframe", "max_artifact_bytes", *ROOT_FIELDS,
                             "start_load_max", "start_wait_s", "poll_interval_s", "abort_rtf",
                             "abort_load_max", "abort_duration_s", "rtf_window_s", "max_retries",
                             "retry_backoff_s", "retry_backoff_max_s"}
    if unknown:
        raise ValueError(f"unknown batch fields: {', '.join(sorted(unknown))}")
    default_airframe = validate_airframe(spec.get("airframe", AIRFRAME), "airframe")
    gpu = spec.get("gpu", 1)
    slots = spec.get("slots", [5, 6, 7])
    share_gpu = spec.get("share_gpu")
    if type(gpu) is not int or gpu not in (0, 1, 2, 3):
        raise ValueError("gpu must be 0, 1, 2 or 3")
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
                                "airframe", "max_artifact_bytes", "image", "gimbal_bridge_stabilize", "gimbal_eval",
                                "scenario_class", "minimum_max_distance_m", *ROOT_FIELDS}
        if unknown:
            raise ValueError(f"jobs[{index}] unknown fields: {', '.join(sorted(unknown))}")
        airframe = validate_airframe(job.get("airframe", default_airframe), f"jobs[{index}].airframe")
        runtime = runtime_options(job, airframe, f"jobs[{index}]")
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
        scenario_class = job.get("scenario_class", policy.WIDE_AREA)
        threshold = policy.distance_gate(scenario.name.removesuffix(".scenario.json"), scenario_class)
        if job.get("minimum_max_distance_m", threshold) != threshold:
            raise ValueError(f"jobs[{index}].minimum_max_distance_m must be {threshold} for {scenario_class}")
        if scenario_class == policy.MOUNTAIN_SHORT and airframe != DELTA_AIRFRAME:
            raise ValueError("mountain_short requires the X1 airframe")
        if runtime.get("gimbal_eval") and scenario_class != policy.WIDE_AREA:
            raise ValueError("gimbal_eval requires a wide_area route")
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
        # Presence is significant, even for an explicit false. Legacy inputs omit these keys.
        inputs.update(runtime)
        # Keep prior wide-area job IDs and accepted records reusable.
        if scenario_class != policy.WIDE_AREA:
            inputs.update(scenario_class=scenario_class, minimum_max_distance_m=threshold)
        job_id = digest({**inputs, "roots": roots,
                         "schema_version": 2 if airframe == DELTA_AIRFRAME else 1})
        if job_id not in jobs:
            jobs[job_id] = {"job_id": job_id, "inputs": {**inputs, "scenario_class": scenario_class},
                            "weather_preset": preset,
                            "slot": slots[len(jobs) % len(slots)],
                            "matrix": {"name": job_id, "scenario": str(scenario), **roots,
                                       "variant": "V9", "extent_m": 6000, "airframe": airframe,
                                       "minimum_rtf": MIN_RTF, "scenario_class": scenario_class,
                                       "minimum_max_distance_m": threshold,
                                       **runtime}}
    batch_id = spec.get("batch_id", digest({"jobs": list(jobs)})[:16])
    if not isinstance(batch_id, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}", batch_id):
        raise ValueError("batch_id must be a plain identifier")
    output_root = absolute(spec.get("output_root", str(BATCHES / batch_id)), "output_root")
    from scripts.realworld.collection_guard import DEFAULTS, settings
    guard = settings({key: spec[key] for key in DEFAULTS if key in spec})
    return {"schema_version": 1, "batch_id": batch_id, "output_root": str(output_root), **guard,
            "gpu": gpu, "slots": slots, "share_gpu": share_gpu, "jobs": list(jobs.values()),
            "scenario_class_counts": class_counts(jobs.values())}


def prepare(job, snapshot):
    from multidetect.terrain_dem import load_local_dem_grid
    from scripts.realworld.long_range_flight import load_mission, sidecar_path, verify_geometry_inputs

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

    mission_path = sidecar_path(source)
    mission_bytes = mission_path.read_bytes()
    mission = json.loads(mission_bytes)
    if mission["extent_m"] != 6000:
        raise ValueError(f"{source}: long-range extent must be 6000 m")
    scenario_class = distance_fields(job)["scenario_class"]
    threshold = policy.verify_route_policy(mission, world_id, scenario_class)
    if threshold != distance_fields(job)["minimum_max_distance_m"]:
        raise ValueError(f"{source}: job distance threshold does not match route policy")
    receipt_path = source.parent / "preflight.json"
    if "route_preflight" in mission or receipt_path.exists():
        preflight = mission.get("route_preflight")
        receipt = read_object(receipt_path)
        policy.verify_route_policy(receipt, world_id, scenario_class)
        hashes = {f"{key}_sha256": evidence[key]["sha256"] for key in ("world", "map", "dem")}
        if (airframe != DELTA_AIRFRAME or not isinstance(preflight, dict)
                or receipt.get("passed") is not True or receipt.get("scenario") != source.name
                or receipt.get("scenario_sha256") != file_hash(source)
                or receipt.get("sidecar_sha256") != hashlib.sha256(mission_bytes).hexdigest()
                or receipt.get("wide_area_unreachable") != mission.get("wide_area_unreachable")
                or any(receipt.get(key) != value for key, value in hashes.items())
                or preflight.get("assets") != hashes or "variant_model_root" in row):
            raise ValueError(f"{source}: X1 route preflight receipt or staged asset identity mismatch")
        verify_geometry_inputs(preflight, row["resolved_root"])
    else:
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
    if "route_preflight" in mission:
        atomic_text(sidecar_path(staged), mission_bytes.decode("utf-8"))
    else:
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
    if not distance_identity_matches(raw, job):
        errors.append("matrix_distance_policy_mismatch")
    if not number(distance) or distance < distance_fields(job)["minimum_max_distance_m"]:
        errors.append("distance_below_minimum")
    if frames == 0:
        errors.append("no_rgb_frames")
    episode_dirs = list((attempt / "flight/episodes").glob("*/episode.json"))
    episode = read_episode_metadata(episode_dirs)
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
                mixed = job["inputs"].get("gimbal_eval") is True
                stabilization = episode.get("horizon_stabilization") or {}
                track_step = stabilization.get("eval_track_step") if mixed else None
                summary = summarize_horizon(manifest, expected_mode=None if mixed else "lock",
                                            track_action_index=track_step)
                horizon = summary["horizon_acceptance"]
                if mixed:
                    from scripts.realworld.long_range_flight import GIMBAL_EVAL_MIN_TRACK_FRAMES
                    from scripts.realworld.x1_gimbal_eval import read_track_hold

                    track = summary["track_acceptance"]
                    gimbal_evidence["track_diagnostics"] = track
                    if (type(track_step) is not int or track_step < 0
                            or track["eval_action_frames"] < GIMBAL_EVAL_MIN_TRACK_FRAMES):
                        errors.append("gimbal_eval_track_segment_missing")
                    diagnostics_issues = []
                    _, hold = read_track_hold(episode_dirs[0].parent, episode, diagnostics_issues)
                    if hold is not None:
                        track["hold"] = hold
                    if diagnostics_issues:
                        track["hold_issues"] = diagnostics_issues
            except (OSError, ValueError, TypeError, AttributeError, OverflowError) as exc:
                horizon["error"] = str(exc)
        errors.extend(horizon["errors"])
        gimbal_evidence["horizon_acceptance"] = horizon
    return {"accepted": not errors, "errors": errors, "matrix_returncode": rc, **distance_fields(job),
            "valid_round": raw.get("valid_round") is True, "route_completed": raw.get("route_completed") is True,
            "round_errors": raw.get("round_errors", []), "container_id": container_id,
            "container_name": container_name, "gpu_uuid": raw.get("gpu_uuid"),
            "rtf_window_median": rtf if number(rtf) else None,
            "rtf_delta_sim_over_delta_real": ratio if number(ratio) else None,
            "max_distance_from_spawn_m": distance if number(distance) else None,
            "frame_count": frames, "artifact_bytes": artifact_bytes, "episode_bytes": episode_bytes,
            **gimbal_evidence, **runtime_evidence(job["inputs"], episode)}


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


def run_attempt(attempt, job, batch, command, lock_fd, gate):
    selection_keys = ("gpu_index", "gpu_uuid", "gpu_reason", "gpu_shared_processes")
    selection = {key: gate[key] for key in selection_keys if key in gate}
    record = {"job_id": job["job_id"], "attempt_id": f"{job['job_id']}/{attempt.name}",
              **distance_fields(job),
              "attempt_dir": str(attempt), "gpu": batch["gpu"], "slot": job["slot"],
              "inputs": job["inputs"], "started_at": now(), "start_load": gate["start_load"],
              "max_load": gate["start_load"], "voided_reason": None, "requeue_count": 0,
              "retry_exhausted": False, "next_retry_at": None,
              **runtime_evidence(job["inputs"], {}), **selection}
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
                   RTF_MATRIX_START_LOAD_MAX=str(batch["start_load_max"]),
                   RTF_MATRIX_START_WAIT_S=str(batch["start_wait_s"]),
                   RTF_MATRIX_POLL_INTERVAL_S=str(batch["poll_interval_s"]),
                   RTF_MATRIX_ABORT_RTF=str(batch["abort_rtf"]),
                   RTF_MATRIX_ABORT_LOAD_MAX=str(batch["abort_load_max"]),
                   RTF_MATRIX_ABORT_DURATION_S=str(batch["abort_duration_s"]),
                   RTF_MATRIX_RTF_WINDOW_S=str(batch["rtf_window_s"]),
                   MULTIDETECT_SITL_SLOTS="8")
        if batch["share_gpu"] is not None:
            env["RTF_MATRIX_SHARE_GPU"] = str(int(batch["share_gpu"]))
        env.pop("RTF_MATRIX_X1_GPU_UUID", None)
        if selection:
            env["RTF_MATRIX_X1_GPU_UUID"] = selection["gpu_uuid"]
        manifest = attempt / "manifest.json"
        atomic_text(attempt / "gate.json", dump(gate) + "\n")
        atomic_text(manifest, dump([{**job["matrix"], "scenario": str(staged)}]) + "\n")
        with (attempt / "matrix.stdout.log").open("wb") as log:
            proc = subprocess.run([str(command), "--manifest", str(manifest), "--out", str(attempt)],
                                  cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                  pass_fds=(lock_fd,), check=False)
        record.update(evaluate(attempt, job, batch, proc.returncode))
        if selection:
            if record["gpu_uuid"] != selection["gpu_uuid"] and record["valid_round"]:
                record.update(accepted=False, valid_round=False,
                              errors=[*record["errors"], "matrix_gpu_uuid_mismatch"])
            record.update(selection)
            # A successful under-lease gate may have observed different graphics processes.
            confirmed = read_object(attempt / "gate.json")
            if (confirmed.get("gpu_index") == selection["gpu_index"]
                    and confirmed.get("gpu_uuid") == selection["gpu_uuid"]):
                record.update({key: confirmed[key] for key in selection_keys if key in confirmed})
        guard_path = attempt / "guard.json"
        if guard_path.exists():
            guard = read_object(guard_path)
            for key in ("start_load", "max_load", "voided_reason", "rtf_window_median"):
                value = guard.get(key)
                if value is not None:
                    record[key] = value
            if record["voided_reason"]:
                record["accepted"] = False
                record["errors"] = [f"voided:{record['voided_reason']}"]
                record["valid_round"] = False
                if isinstance(guard.get("detail"), str):
                    record["voided_detail"] = guard["detail"]
            elif (record["accepted"] and (not number(record["rtf_window_median"])
                                          or record["rtf_window_median"] < MIN_RTF)):
                record["accepted"] = False
                record["errors"] = ["invalid_window_rtf"]
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        record.update(accepted=False, errors=[f"attempt_error: {exc}"])
    record["finished_at"] = now()
    atomic_text(attempt / "batch-result.json", dump(record) + "\n")
    return record


def run_batch(batch, command, *, completed=None, gpu_selector=None):
    """Run the batch, optionally recognizing usable verdicts for comparison trials."""
    root = Path(batch["output_root"])
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError(f"batch is already running: {root}") from exc
        return run_locked(batch, command, lock.fileno(), completed=completed, gpu_selector=gpu_selector)


def run_locked(batch, command, lock_fd, *, completed=None, gpu_selector=None):
    root = Path(batch["output_root"])
    manifest = root / "manifest.json"
    accepted_path, results_path, paused = (root / name for name in ("accepted.jsonl", "results.jsonl", "PAUSED"))
    if manifest.exists() and read_object(manifest)["batch_id"] != batch["batch_id"]:
        raise ValueError(f"output_root already belongs to another batch: {root}")
    jobs = {job["job_id"]: job for job in batch["jobs"]}
    accepted = indexed(accepted_path, "job_id")
    qualified = {job_id for job_id, record in accepted.items()
                 if job_id in jobs and record.get("accepted") is True and meets_distance(record, jobs[job_id])}
    results = indexed(results_path, "attempt_id", repair_tail=True)

    def write_manifest():
        atomic_text(manifest, dump({**batch, "scenario_class_counts": class_counts(jobs.values(), qualified)}) + "\n")

    write_manifest()

    def publish(attempt, record):
        job_id = record["job_id"]
        job = jobs[job_id]
        if record["accepted"] and not meets_distance(record, job):
            record.update(accepted=False, errors=[*record.get("errors", []), "invalid_distance_receipt"])
        record.update(distance_fields(job))
        if record["accepted"]:
            if job_id not in qualified:
                accepted[job_id] = record
                atomic_text(accepted_path, "".join(dump(row) + "\n" for row in accepted.values()))
                qualified.add(job_id)
                write_manifest()
        elif (os.environ.get("WIDE_COLLECT_KEEP_HORIZON_REJECTED") == "1"
              and record.get("errors") and all(str(e).startswith("horizon_") for e in record["errors"])):
            pass  # keep episodes rejected only by the horizon gate, for diagnosis (stored on NAS, ~160 MB each)
        else:
            remove_episodes(attempt)
        if record["attempt_id"] not in results:
            with results_path.open("a", encoding="utf-8") as output:
                output.write(dump(record) + "\n")
                output.flush()
                os.fsync(output.fileno())
            sync_dir(root)
            results[record["attempt_id"]] = record

    from scripts.realworld.collection_guard import DEFER_REASONS, GateTimeout, settings, wait_for_start
    from scripts.realworld.x1_gpu import GPUUnavailable
    config = settings({key: batch[key] for key in batch if key in settings()})
    queue = deque(batch["jobs"])
    lease_reselected = set()
    pending = False
    while queue:
        job = queue.popleft()
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
                episode = read_episode_metadata(list((attempt / "flight/episodes").glob("*/episode.json")))
                record = {"job_id": job_id, "attempt_id": attempt_id, "attempt_dir": str(attempt),
                          "accepted": False, "errors": ["interrupted_before_batch_verdict"],
                          "start_load": None, "max_load": None, "voided_reason": None,
                          "requeue_count": 0, "finished_at": now(),
                          **runtime_evidence(job["inputs"], episode)}
                atomic_text(checkpoint, dump(record) + "\n")
            if record["attempt_id"] != attempt_id or record["job_id"] != job_id:
                raise ValueError(f"attempt checkpoint identity mismatch: {checkpoint}")
            publish(attempt, record)
        prior = [row for row in results.values() if row.get("job_id") == job_id]
        if completed is None:
            if job_id in qualified:
                continue
        elif any(completed(row, job) for row in prior):
            continue
        free = shutil.disk_usage(root).free
        if free < MIN_FREE_BYTES:
            atomic_text(paused, dump({"reason": "low_disk_space", "free_bytes": free,
                                      "minimum_free_bytes": MIN_FREE_BYTES, "next_job_id": job_id,
                                      "at": now()}) + "\n")
            print(f"PAUSED: {free} bytes free; next job {job_id}", file=sys.stderr)
            return 75
        paused.unlink(missing_ok=True)
        voided = [row for row in prior if row.get("voided_reason") and row["voided_reason"] not in DEFER_REASONS]
        requeues = max((row.get("requeue_count", 0) for row in prior), default=0)
        if len(voided) > batch["max_retries"]:
            pending = True
            continue
        next_attempt = 1 if not attempts else int(attempts[-1].name.split("-")[-1]) + 1
        if voided:
            deadline = voided[-1].get("next_retry_epoch", 0)
            remaining = deadline - time.time()
            if remaining > 0 and queue:
                from scripts.realworld.collection_guard import write_status
                write_status(root / "WAITING", {"state": "WAITING", "reason": "retry_backoff",
                                                  "job_id": job_id, "requeue_count": requeues,
                                                  "wait_remaining_s": remaining})
                queue.append(job)
                time.sleep(min(config["poll_interval_s"], remaining))
                continue
            if remaining > 0:
                time.sleep(remaining)
        selection = None
        if gpu_selector is not None:
            try:
                selection = gpu_selector(root / "GPU-STATUS.json")
            except GPUUnavailable as exc:
                print(str(exc), file=sys.stderr)
                return 75
        attempt = job_dir / f"attempt-{next_attempt}"
        attempt.mkdir(parents=True)
        gate = None
        try:
            if selection is None:
                gate = wait_for_start(root / "WAITING", batch["gpu"], config)
                execution = batch
            else:
                # The matrix rechecks the selected card under its UUID lease before starting SITL.
                gate = {"start_load": os.getloadavg()[0], **selection}
                execution = {**batch, "gpu": selection["gpu_index"]}
            record = run_attempt(attempt, job, execution, command, lock_fd, gate)
        except GateTimeout as exc:
            record = {"job_id": job_id, "attempt_id": f"{job_id}/{attempt.name}",
                      "attempt_dir": str(attempt), "accepted": False, "errors": [str(exc)],
                      "start_load": None, "max_load": None,
                      "voided_reason": str(exc) if str(exc) in DEFER_REASONS else None,
                      "requeue_count": requeues, "finished_at": now()}
        record.setdefault("requeue_count", requeues)
        record.setdefault("start_load", gate.get("start_load") if gate else None)
        record.setdefault("max_load", record.get("start_load"))
        record.setdefault("voided_reason", None)
        record.setdefault("retry_exhausted", False)
        record.setdefault("next_retry_at", None)
        publish(attempt, record)
        if (gpu_selector is not None and record.get("voided_reason") == "x1_gpu_unavailable"
                and record.get("voided_detail") == "gpu_lease_busy" and job_id not in lease_reselected):
            # Concurrent workers can select before either has taken the UUID lease.
            # Give the loser one fresh selection, without consuming its flight retries.
            lease_reselected.add(job_id)
            queue.appendleft(job)
            continue
        if record.get("voided_reason") in DEFER_REASONS:
            return 75
        if record.get("voided_reason"):
            new_requeues = requeues + 1 if requeues < batch["max_retries"] else requeues
            record["requeue_count"] = new_requeues
            if new_requeues > requeues:
                delay = min(batch["retry_backoff_s"] * (2 ** max(0, new_requeues - 1)),
                            batch["retry_backoff_max_s"])
                record["next_retry_epoch"] = time.time() + delay
                record["next_retry_at"] = datetime.fromtimestamp(
                    record["next_retry_epoch"], timezone.utc).isoformat()
                record["retry_exhausted"] = False
            else:
                record["next_retry_at"] = None
                record["retry_exhausted"] = True
            # Refresh the append-only row and checkpoint with retry metadata.
            results[record["attempt_id"]] = record
            atomic_text(attempt / "batch-result.json", dump(record) + "\n")
            atomic_text(results_path, "".join(dump(row) + "\n" for row in results.values()))
            if new_requeues > requeues:
                queue.append(job)
            else:
                pending = True
        else:
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
    raise SystemExit(main())
