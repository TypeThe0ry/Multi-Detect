#!/usr/bin/env python3
"""Dispatch verified next-world X1 scenarios under the shared flight lock."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
if __name__ == "__main__":
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from scripts.realworld import wide_collect_batch as batch  # noqa: E402
from scripts.realworld import x1_gpu  # noqa: E402
from scripts.realworld import x1_resume as resume  # noqa: E402

MANIFEST = ROOT / "configs/simulation/x1-worlds.json"
OUTPUT = resume.NAS / "x1-next"
UNIT = "md-x1-collect-next.service"
SLOTS = (5, 6, 7)
# Correctly grouped prior Iowa A/B favored bridge-on. The horizon2 candidate still
# requires a live verdict for every new site.
BRIDGE_STABILIZE = True


def verified_asset(asset, world_name):
    folder = asset / "x1"
    receipt = batch.read_object(folder / "preflight.json")
    scenario = folder / f"{world_name}.scenario.json"
    sidecar = folder / f"{world_name}.long_range.json"
    world = asset / "worlds" / f"{world_name}.sdf"
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


def site_plan(asset_root, site, index, image_id):
    asset = asset_root / f"{site}_w6km" / "v1"
    name = f"{site}_w6km__clear_noon"
    paths, receipt = verified_asset(asset, name)
    # Keep records for older versions. A rebuilt route or new image cannot qualify this run.
    suffix = batch.digest({"route": receipt, "image_id": image_id, "bridge": BRIDGE_STABILIZE})[:20]
    staging = ROOT / ".tmp" / "x1-worlds" / f"{site}-{suffix}"
    return batch.expand({
        "batch_id": f"x1-next-{site}-{suffix}",
        "output_root": str(OUTPUT / "collection" / site / suffix),
        "airframe": batch.DELTA_AIRFRAME,
        "gpu": 3,
        "slots": [SLOTS[index % len(SLOTS)]],
        "share_gpu": False,
        "start_load_max": 60,
        "resolved_root": str(asset / "models"),
        "variant_world_root": str(staging / "worlds"),
        "variant_map_root": str(staging / "maps"),
        "variant_terrain_root": str(staging / "terrain"),
        "jobs": [{"site": site, "scenario": str(paths["scenario"]), "seed": 401 + index,
                  "altitude_m": 85, "time_of_day": "noon", "weather": "clear",
                  "image": image_id, "gimbal_bridge_stabilize": BRIDGE_STABILIZE}],
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
            and batch.number(rtf) and rtf >= batch.MIN_RTF
            and (record.get("horizon_acceptance") or {}).get("passed") is True)


def accepted(plan):
    records = batch.indexed(Path(plan["output_root"]) / "accepted.jsonl", "job_id")
    job = plan["jobs"][0]
    return verdict(records.get(job["job_id"], {}), job)


def plans(spec, image_id):
    pending, done, unavailable = [], [], {}
    for index, site in enumerate(spec["sites"]):
        try:
            item = site_plan(Path(spec["asset_root"]), site, index, image_id)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            unavailable[site] = str(exc)
            continue
        if accepted(item[0]):
            done.append(site)
        else:
            pending.append(item)
    return pending, done, unavailable


def preflight(spec, image):
    resume.mount_check(output=OUTPUT)
    image_id = resume.image_check(image)
    pending, done, unavailable = plans(spec, image_id)
    if not pending:
        raise ValueError(f"no pending offline-qualified worlds ({len(done)} accepted): {unavailable}")
    resume.flight_gate(SLOTS)
    return image_id, pending, x1_gpu.select(OUTPUT / "GPU-PREFLIGHT.json")


def run(pending):
    resume.mount_check(write=True, output=OUTPUT)
    resume.matrix_environment()
    for plan, assets in pending:
        resume.flight_gate(SLOTS)
        staged = []
        try:
            for source, folder, sha256 in assets:
                staged.append(resume.stage_asset(source, folder, sha256))
            rc = resume.run_flight_batch(plan)
        finally:
            for path in staged:
                path.unlink(missing_ok=True)
        if rc not in (0, 1):
            return rc
    return 0 if all(accepted(plan) for plan, _ in pending) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("preflight", "complete", "launch"):
        actions.add_parser(name)
    actions.add_parser("run").add_argument("--image-id", required=True)
    args = parser.parse_args(argv)
    launching = False
    try:
        spec = batch.read_object(MANIFEST)
        image = batch.read_object(resume.MANIFEST)["image"]
        if args.action == "complete":
            resume.mount_check(output=OUTPUT)
            image_id = resume.image_check(image)
            _, done, _ = plans(spec, image_id)
            return 0 if len(done) == len(spec["sites"]) else 1
        if args.action == "run":
            with resume.flight_lock(resume.LOCK):
                image_id, pending, _ = preflight(spec, image)
                if args.image_id != image_id:
                    raise ValueError("candidate tag changed between dispatch and worker startup")
                return run(pending)
        image_id, pending, gpu = preflight(spec, image)
        if args.action == "preflight":
            print(f"ready: {len(pending)} world(s), GPU{gpu['gpu_index']} {gpu['gpu_uuid']}, "
                  f"slots 5–7, {image} {image_id}")
        else:
            with resume.flight_lock(resume.LOCK):
                pass
            launching = True
            print(resume.read_cmd(["systemd-run", "--unit", UNIT, "--collect", "--working-directory", str(ROOT),
                                   "--property", f"RequiresMountsFor={resume.NAS}",
                                   "--property", "KillMode=control-group", "--setenv", "PYTHONDONTWRITEBYTECODE=1",
                                   sys.executable, str(Path(__file__).resolve()), "run", "--image-id", image_id]))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f"x1-collect-next: {exc}", file=sys.stderr)
        if args.action == "complete":
            return 1
        return 2 if launching else 75


if __name__ == "__main__":
    raise SystemExit(main())
