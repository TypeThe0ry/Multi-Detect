#!/usr/bin/env python3
"""Dispatch verified next-world X1 scenarios under the shared flight lock."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
if __name__ == "__main__":
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from scripts.realworld import wide_collect_batch as batch  # noqa: E402
from scripts.realworld import x1_gpu  # noqa: E402
from scripts.realworld import x1_resume as resume  # noqa: E402
from scripts.realworld import x1_scenario_class as policy  # noqa: E402
from scripts.realworld.x1_lock import flight_lane  # noqa: E402

MANIFEST = ROOT / "configs/simulation/x1-worlds.json"
VARIANT_MANIFEST = MANIFEST.with_name("x1-variants.json")
EVAL_MANIFEST = MANIFEST.with_name("x1-gimbal-eval.json")
OUTPUT = resume.NAS / "x1-next"
EVAL_OUTPUT = resume.NAS / "x1-gimbal-eval"
UNIT = "md-x1-collect-next.service"
SLOTS = (5, 6, 7)
EVAL_LANES = {"a": 5, "b": 6}
# Keep sparse, pre-upgrade eval receipts intact and collect fresh hold evidence.
EVAL_CAPTURE_REVISION = 3
IOWA_SITE = "rw_farm_iowa"
# Presets audited for this queue. The generator's wind presets need a different audit.
VARIANT_CONDITIONS = {("noon", "overcast"), ("dusk", "clear")}
# Correctly grouped prior Iowa A/B favored bridge-on. The horizon2 candidate still
# requires a live verdict for every new site.
BRIDGE_STABILIZE = True


def verified_asset(asset, world_name, *, variant_dir=None):
    root = asset if variant_dir is None else variant_dir
    folder = root / "x1"
    receipt = batch.read_object(folder / "preflight.json")
    scenario = folder / f"{world_name}.scenario.json"
    sidecar = folder / f"{world_name}.long_range.json"
    world = root / "worlds" / f"{world_name}.sdf"
    paths = {"world": world, "scenario": scenario, "sidecar": sidecar,
             "map": folder / f"{world_name}.geojson",
             "dem": folder / f"{world_name}.dem.json"}
    if receipt.get("passed") is not True or receipt.get("scenario") != scenario.name:
        raise ValueError(f"offline route has not passed: {folder}")
    for field, path in paths.items():
        if batch.file_hash(path) != receipt.get(f"{field}_sha256"):
            raise ValueError(f"offline route {field} identity mismatch: {path}")
    if not (asset / "models").is_dir():
        raise ValueError(f"resolved models unavailable: {asset / 'models'}")
    return paths, receipt


def site_plan(asset_root, site, index, image_id, spec=None, *,
              conditions=("noon", "clear"), asset_dir=None, gimbal_eval=False, slot=None, eval_tag=None,
              gimbal_eval_fw=False, fw_min_leg_m=None):
    preset = batch.WEATHER[conditions]
    asset = asset_root / f"{site}_w6km" / "v1" if asset_dir is None else asset_dir
    variant_dir = None if preset == "clear_noon" else asset / "x1-variants" / preset
    name = f"{site}_w6km__{preset}"
    paths, receipt = verified_asset(asset, name, variant_dir=variant_dir)
    scenario_class = policy.configured_class(name, spec)
    threshold = policy.verify_route_policy(receipt, name, scenario_class)
    sidecar = batch.read_object(paths["sidecar"])
    policy.require_current_flight_margin(sidecar, name)
    if scenario_class == policy.MOUNTAIN_SHORT:
        policy.verify_route_policy(sidecar, name, scenario_class)
        if (sidecar.get("minimum_max_distance_m") != threshold
                or sidecar["wide_area_unreachable"] != receipt["wide_area_unreachable"]):
            raise ValueError(f"{name}: mountain_short sidecar does not match route receipt")
    if variant_dir is not None and sidecar.get("minimum_max_distance_m") != threshold:
        raise ValueError(f"{name}: variant sidecar does not certify {threshold} m")
    # Keep records for older versions. A rebuilt route or new image cannot qualify this run.
    eval_options = ({"gimbal_eval": True, **({"gimbal_eval_fw": True} if gimbal_eval_fw else {}),
                     **({"fw_min_leg_m": fw_min_leg_m} if fw_min_leg_m is not None else {})}
                    if gimbal_eval else {})
    eval_identity = ({**eval_options, "eval_capture_revision": EVAL_CAPTURE_REVISION,
                      **({"eval_tag": eval_tag} if eval_tag else {})} if gimbal_eval else {})
    suffix = batch.digest({"route": receipt, "image_id": image_id, "bridge": BRIDGE_STABILIZE,
                           **eval_identity})[:20]
    queue_key = site if variant_dir is None else f"{site}__{preset}"
    staging = ROOT / ".tmp" / "x1-worlds" / f"{queue_key}-{suffix}"
    batch_prefix, output = ("x1-gimbal-eval", EVAL_OUTPUT) if gimbal_eval else ("x1-next", OUTPUT)
    return batch.expand({
        "batch_id": f"{batch_prefix}-{queue_key}-{suffix}",
        "output_root": str(output / "collection" / queue_key / suffix),
        "airframe": batch.DELTA_AIRFRAME,
        "gpu": 0,
        "slots": [SLOTS[index % len(SLOTS)] if slot is None else slot],
        "share_gpu": False,
        "start_load_max": 60,
        "resolved_root": str(asset / "models"),
        "variant_world_root": str(staging / "worlds"),
        "variant_map_root": str(staging / "maps"),
        "variant_terrain_root": str(staging / "terrain"),
        "jobs": [{"site": site, "scenario": str(paths["scenario"]), "seed": 401 + index,
                  "scenario_class": scenario_class, "minimum_max_distance_m": threshold,
                  "altitude_m": 85, "time_of_day": conditions[0], "weather": conditions[1],
                  "image": image_id, "gimbal_bridge_stabilize": BRIDGE_STABILIZE, **eval_options}],
    }), [(paths[field], staging / folder, receipt[f"{field}_sha256"])
         for field, folder in (("world", "worlds"), ("map", "maps"), ("dem", "terrain"))]


def verdict(record, job):
    rtf = record.get("rtf_window_median")
    # Older receipts predate GPU selection and were only produced on GPU3.
    gpu = record.get("gpu")
    uuid = record.get("gpu_uuid")
    gpu_audited = gpu == 3
    if "gpu_index" in record:
        gpu_audited = (type(gpu) is int and gpu in x1_gpu.ORDER and record["gpu_index"] == gpu
                       and isinstance(uuid, str) and bool(x1_gpu.UUID.fullmatch(uuid))
                       and bool(record.get("gpu_reason")))
    return (resume.receipt_matches(record, job)
            and gpu_audited and record.get("slot") == job["slot"]
            and record.get("route_completed") is True
            and batch.meets_distance(record, job)
            and batch.number(rtf) and rtf >= batch.MIN_RTF
            and (not job["inputs"].get("gimbal_eval") or bool(record.get("track_diagnostics")))
            and (record.get("horizon_acceptance") or {}).get("passed") is True)


def accepted(plan):
    records = batch.indexed(Path(plan["output_root"]) / "accepted.jsonl", "job_id")
    job = plan["jobs"][0]
    return verdict(records.get(job["job_id"], {}), job)


def collection_sites(spec):
    # Preserve the original indices (job seeds and slots) of the managed worlds.
    return (*spec["sites"], *spec.get("collection_only_sites", ()))


def plans(spec, image_id):
    pending, done, unavailable = [], [], {}
    for index, site in enumerate(collection_sites(spec)):
        try:
            item = site_plan(Path(spec["asset_root"]), site, index, image_id, spec)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            unavailable[site] = str(exc)
            continue
        if accepted(item[0]):
            done.append(site)
        else:
            pending.append(item)
    return pending, done, unavailable


def queue_plans(spec, image_id, iowa_spec):
    pending, done, unavailable = plans(spec, image_id)
    if pending or unavailable:
        return pending, done, unavailable

    variants = batch.read_object(VARIANT_MANIFEST)
    if variants.get("schema_version") != 1 or type(variants.get("enabled")) is not bool:
        raise ValueError("invalid X1 variant queue schema or enabled flag")
    if not variants["enabled"]:
        return pending, done, unavailable

    sites = variants["sites"]
    conditions = [(row["time_of_day"], row["weather"]) for row in variants["conditions"]]
    if (not isinstance(sites, list) or not sites or len(set(sites)) != len(sites)
            or any(site != IOWA_SITE and (site not in done or
                       policy.configured_class(site, spec) != policy.WIDE_AREA) for site in sites)
            or len(conditions) != 2 or set(conditions) != VARIANT_CONDITIONS):
        raise ValueError("X1 variants require unique wide-area base sites, overcast and dusk")
    if IOWA_SITE in sites and not resume.complete(iowa_spec, required_image_id=image_id):
        unavailable[IOWA_SITE] = "Iowa base collection is not complete for the candidate image"
        return pending, done, unavailable

    asset_root = Path(spec["asset_root"])
    overrides = variants.get("asset_dirs", {})
    if overrides.keys() - set(sites):
        raise ValueError("variant asset_dirs must refer to queued sites")
    for site_index, site in enumerate(sites):
        asset_dir = None
        if site in overrides:
            relative = Path(overrides[site])
            asset_dir = asset_root / relative
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"{site}: variant asset_dir must be relative to the asset root")
        for offset, pair in enumerate(conditions):
            label = f"{site}__{batch.WEATHER[pair]}"
            index = len(collection_sites(spec)) + site_index * len(conditions) + offset
            try:
                item = site_plan(asset_root, site, index, image_id, spec,
                                 conditions=pair, asset_dir=asset_dir)
                if accepted(item[0]):
                    done.append(label)
                else:
                    pending.append(item)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                unavailable[label] = str(exc)
    return pending, done, unavailable


def eval_config(*, require_enabled):
    spec = batch.read_object(EVAL_MANIFEST)
    if (type(spec.get("schema_version")) is not int or spec["schema_version"] != 1
            or type(spec.get("enabled")) is not bool or spec.get("gimbal_eval") is not True):
        raise ValueError("invalid X1 gimbal evaluation queue schema")
    if require_enabled and not spec["enabled"]:
        raise ValueError("X1 gimbal evaluation queue is disabled")
    if "image" in spec and (type(spec["image"]) is not str or not spec["image"]):
        raise ValueError("invalid X1 gimbal evaluation image override")
    if "only" in spec and (type(spec["only"]) is not list or not all(type(x) is str for x in spec["only"])):
        raise ValueError("invalid X1 gimbal evaluation condition filter")
    if "fw_legs" in spec and type(spec["fw_legs"]) is not bool:
        raise ValueError("invalid X1 gimbal evaluation fw_legs")
    if "fw_min_leg_m" in spec and (type(spec["fw_min_leg_m"]) not in (int, float)
                                   or isinstance(spec["fw_min_leg_m"], bool)
                                   or not 0 < spec["fw_min_leg_m"] <= 500):
        raise ValueError("invalid X1 gimbal evaluation fw_min_leg_m")
    if "tag" in spec and (type(spec["tag"]) is not str or not spec["tag"]):
        raise ValueError("invalid X1 gimbal evaluation tag")
    for key, high in (("track_preview_s", 1.0), ("track_preview_max_deg", 90.0)):
        if key in spec and (type(spec[key]) not in (int, float) or not 0 < spec[key] <= high):
            raise ValueError(f"invalid X1 gimbal evaluation {key}")
    return spec


def queue_image(iowa_spec, config):
    """Optional local image tag for the eval queue (e.g. a rebuilt bridge); defaults to the pinned Iowa image."""
    return (config or {}).get("image") or iowa_spec["image"]


def eval_plans(spec, iowa_spec, image_id, config, *, lane=None):
    variants = batch.read_object(VARIANT_MANIFEST)
    asset_root = Path(spec["asset_root"])
    sites = variants["sites"]
    pairs = (("noon", "clear"), *((row["time_of_day"], row["weather"]) for row in variants["conditions"]))
    if (len(sites) != 7 or len(set(sites)) != 7 or IOWA_SITE not in sites
            or any(site != IOWA_SITE and (site not in collection_sites(spec)
                   or policy.configured_class(site, spec) != policy.WIDE_AREA) for site in sites)
            or len(pairs) != 3 or set(pairs[1:]) != VARIANT_CONDITIONS):
        raise ValueError("X1 gimbal eval requires the seven-site base/variant matrix")
    for site_index, site in enumerate(sites):
        for offset, pair in enumerate(pairs):
            index = site_index * len(pairs) + offset
            job_lane = "a" if index % 2 == 0 else "b"
            if lane is not None and job_lane != lane:
                continue
            slot = EVAL_LANES[job_lane]
            label = f"{site}__{batch.WEATHER[pair]}"
            if config.get("only") and label not in config["only"]:
                continue
            if site == IOWA_SITE and offset == 0:
                source = Path(iowa_spec["world_source_root"]) / resume.IOWA_WORLD
                if batch.file_hash(source) != iowa_spec["world_sha256"]:
                    raise ValueError("Iowa base world does not match its pinned SHA-256")
                suffix = batch.digest({"iowa": iowa_spec, "image_id": image_id, "gimbal_eval": True,
                                       "eval_capture_revision": EVAL_CAPTURE_REVISION,
                                       **({"eval_tag": config["tag"]} if config.get("tag") else {}),
                                       **({"gimbal_eval_fw": True} if config.get("fw_legs") else {})})[:20]
                stage = ROOT / ".tmp" / "x1-worlds" / f"{label}-{suffix}"
                plan = batch.expand({**iowa_spec["batch"], "gpu": 0, "slots": [slot],
                                     "variant_world_root": str(stage),
                                     "batch_id": f"x1-gimbal-eval-{label}-{suffix}",
                                     "output_root": str(EVAL_OUTPUT / "collection" / label / suffix),
                                     "jobs": [dict(iowa_spec["job"], seed=401 + index, image=image_id,
                                                   gimbal_bridge_stabilize=True, gimbal_eval=True,
                                                   **({"gimbal_eval_fw": True} if config.get("fw_legs") else {}))]})
                assets = [(source, stage, iowa_spec["world_sha256"])]
            else:
                override = variants.get("asset_dirs", {}).get(site) if offset else None
                relative = Path(override) if override is not None else None
                if relative is not None and (relative.is_absolute() or ".." in relative.parts):
                    raise ValueError(f"{site}: invalid asset override")
                plan, assets = site_plan(asset_root, site, index, image_id, spec, conditions=pair,
                                         asset_dir=asset_root / relative if relative is not None else None,
                                         gimbal_eval=config["gimbal_eval"], slot=slot,
                                         **({"gimbal_eval_fw": True} if config.get("fw_legs") else {}),
                                         **({"fw_min_leg_m": config["fw_min_leg_m"]}
                                            if config.get("fw_min_leg_m") is not None else {}),
                                         **({"eval_tag": config["tag"]} if config.get("tag") else {}))
            yield label, plan, assets


def eval_cpu_preflight(spec, iowa_spec, image_id, config):
    from copy import deepcopy
    from scripts.realworld.long_range_flight import (
        GIMBAL_EVAL_HOLD_SIM_S, collection_plan, gimbal_eval_plan, load_mission,
    )
    from multidetect.sim_teacher.gimbal_recording import X1_TRACK_SAMPLE_HZ, X1_TRACK_TRANSIENT_SIM_S
    from dataclasses import replace

    if not resume.IMAGE_ID.fullmatch(image_id):
        raise ValueError("CPU preflight requires an explicit immutable image ID (not inspected)")
    output = ROOT / ".tmp" / "x1-gimbal-eval"
    output.mkdir(parents=True, exist_ok=True)
    rows, deploy = [], []
    with tempfile.TemporaryDirectory(dir=output) as workspace:
        scratch = Path(workspace)
        for label, plan, assets in eval_plans(spec, iowa_spec, image_id, config):
            job = deepcopy(plan["jobs"][0])
            matrix = job["matrix"]
            # Audit the production inputs at their existing source without deploying them.
            for (source, stage, digest), key in zip(
                assets, ("variant_world_root", "variant_map_root", "variant_terrain_root")
            ):
                matrix[key] = str(source.parent)
                deploy.append({"source": str(source), "destination": str(stage / source.name),
                               "sha256": digest})
            staged, evidence = batch.prepare(job, scratch / label)
            mission = load_mission(staged, dem_path=Path(evidence["dem"]["path"]),
                                   **({"fw_min_leg_m": job["fw_min_leg_m"]} if job.get("fw_min_leg_m") else {}))
            collector = gimbal_eval_plan(replace(collection_plan(staged, scratch / "no-flight",
                                                                  job["slot"], mission),
                                                gimbal_mode="lock", gimbal_bridge_stabilize=True), mission,
                                    fw_legs=bool(config.get("fw_legs")))
            rows.append({"condition": label, "job_id": job["job_id"], "route_assets": evidence,
                         "slot": job["slot"],
                         "planned_max_distance_m": mission["planned_max_distance_m"],
                         "minimum_max_distance_m": mission["minimum_max_distance_m"],
                         "video_hz": collector.video_hz, "steps_per_episode": collector.steps_per_episode,
                         "track_step": collector.gimbal_eval_track_step,
                         "fw_legs": collector.gimbal_eval_fw_legs,
                         "hover_sim_s": GIMBAL_EVAL_HOLD_SIM_S,
                         "tracking_sample_hz": X1_TRACK_SAMPLE_HZ,
                         "tracking_transient_sim_s": X1_TRACK_TRANSIENT_SIM_S,
                         "target": asdict(collector.track_targets[0])})
    receipt = {"schema_version": 1, "queue_enabled": config["enabled"], "flight_started": False,
               "eval_capture_revision": EVAL_CAPTURE_REVISION,
               "image_id_uninspected": image_id, "passed": len(rows) == 21, "conditions": rows}
    batch.atomic_text(output / "CPU-PREFLIGHT.json", batch.dump(receipt) + "\n")
    batch.atomic_text(output / "DEPLOY-ASSETS.json", batch.dump(deploy) + "\n")
    return receipt


def selected_queue(spec, image_id, iowa_spec, config, *, lane=None):
    if config is None:
        return queue_plans(spec, image_id, iowa_spec)
    pending, done = [], []
    for label, plan, assets in eval_plans(spec, iowa_spec, image_id, config, lane=lane):
        if accepted(plan):
            done.append(label)
        else:
            pending.append((plan, assets))
    return pending, done, {}


def preflight(spec, iowa_spec, *, config=None, lane=None):
    output = OUTPUT if config is None else EVAL_OUTPUT
    resume.mount_check(output=output)
    image_id = resume.image_check(queue_image(iowa_spec, config))
    pending, done, unavailable = selected_queue(spec, image_id, iowa_spec, config, lane=lane)
    if not pending:
        raise ValueError(f"no pending offline-qualified worlds ({len(done)} accepted): {unavailable}")
    resume.flight_gate((EVAL_LANES[lane],) if lane else SLOTS)
    status = f"GPU-PREFLIGHT-{lane}.json" if lane else "GPU-PREFLIGHT.json"
    return image_id, pending, x1_gpu.select(output / status, skip_leased=lane is not None)


def run(pending, *, output=None, lane=None):
    resume.mount_check(write=True, output=OUTPUT if output is None else output)
    resume.matrix_environment()
    for plan, assets in pending:
        resume.flight_gate((EVAL_LANES[lane],) if lane else SLOTS)
        staged = []
        try:
            for source, folder, sha256 in assets:
                staged.append(resume.stage_asset(source, folder, sha256))
            rc = resume.run_flight_batch(plan, skip_leased=lane is not None)
        finally:
            for path in staged:
                path.unlink(missing_ok=True)
        if rc not in (0, 1):
            return rc
    return 0 if all(accepted(plan) for plan, _ in pending) else 1


def worker_lock(lane):
    return flight_lane(resume.LOCK, lane) if lane else resume.flight_lock(resume.LOCK)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gimbal-eval", action="store_true", help="select the separate opt-in eval queue")
    parser.add_argument("--lane", choices=EVAL_LANES, help="run one half of the gimbal eval queue")
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("preflight", "complete", "launch"):
        actions.add_parser(name)
    actions.add_parser("run").add_argument("--image-id", required=True)
    actions.add_parser("gimbal-eval-cpu-preflight").add_argument("--image-id", required=True)
    args = parser.parse_args(argv)
    lane = args.lane
    if lane and (not args.gimbal_eval or args.action == "gimbal-eval-cpu-preflight"):
        parser.error("--lane is for --gimbal-eval preflight, complete, launch or run")
    launching = False
    try:
        spec = batch.read_object(MANIFEST)
        iowa_spec = batch.read_object(resume.MANIFEST)
        if args.action == "gimbal-eval-cpu-preflight":
            receipt = eval_cpu_preflight(spec, iowa_spec, args.image_id, eval_config(require_enabled=False))
            print(f"CPU preflight: {len(receipt['conditions'])}/21 audited conditions; no runtime started")
            return 0 if receipt["passed"] else 1
        config = eval_config(require_enabled=True) if args.gimbal_eval else None
        output = EVAL_OUTPUT if args.gimbal_eval else OUTPUT
        if args.action == "complete":
            resume.mount_check(output=output)
            image_id = resume.image_check(queue_image(iowa_spec, config))
            pending, _, unavailable = selected_queue(spec, image_id, iowa_spec, config, lane=lane)
            return 0 if not pending and not unavailable else 1
        if args.action == "run":
            with worker_lock(lane):
                image_id, pending, _ = preflight(spec, iowa_spec, config=config, lane=lane)
                if args.image_id != image_id:
                    raise ValueError("candidate tag changed between dispatch and worker startup")
                return run(pending, output=output, lane=lane)
        image_id, pending, gpu = preflight(spec, iowa_spec, config=config, lane=lane)
        if args.action == "preflight":
            placement = f"slot {EVAL_LANES[lane]}" if lane else "slots 5–7"
            print(f"ready: {len(pending)} world(s), GPU{gpu['gpu_index']} {gpu['gpu_uuid']}, "
                  f"{placement}, {iowa_spec['image']} {image_id}")
        else:
            with worker_lock(lane):
                pass
            launching = True
            unit = f"md-x1-gimbal-eval-{lane}.service" if lane else UNIT
            print(resume.read_cmd(["systemd-run", "--unit", unit, "--collect", "--working-directory", str(ROOT),
                                   "--property", f"RequiresMountsFor={resume.NAS}",
                                   "--property", "KillMode=control-group", "--setenv", "PYTHONDONTWRITEBYTECODE=1",
                                   *(f"--setenv=MULTIDETECT_X1_{name}={config[key]}"
                                     for key, name in (("track_preview_s", "TRACK_PREVIEW_S"),
                                                       ("track_preview_max_deg", "TRACK_PREVIEW_MAX_DEG"))
                                     if config and key in config),
                                   sys.executable, str(Path(__file__).resolve()),
                                   *(["--gimbal-eval"] if config else []), *(["--lane", lane] if lane else []),
                                   "run", "--image-id", image_id]))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f"x1-collect-next: {exc}", file=sys.stderr)
        if args.action == "complete":
            return 1
        return 2 if launching else 75


if __name__ == "__main__":
    raise SystemExit(main())
