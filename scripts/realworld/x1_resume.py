#!/usr/bin/env python3
"""Resume the Iowa X1 bridge A/B and final collection on the pinned candidate."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
if __name__ == "__main__":
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from scripts.realworld import collection_guard as guard  # noqa: E402
from scripts.realworld import wide_collect_ab as chain  # noqa: E402
from scripts.realworld import wide_collect_batch as batch  # noqa: E402

MANIFEST = ROOT / "configs/simulation/x1-resume.json"
NAS_SHARE = Path("/mnt/nas/AdminHome")
NAS = NAS_SHARE / "volatic"
OUTPUT = NAS / "x1-resume"
UNIT = "md-x1-resume.service"
MATRIX = ROOT / "scripts/realworld/rtf_matrix.sh"
LOCK = Path("/run/lock/md-x1-resume.lock")
SITL_PYTHON = "/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python"
TOOLS_PYTHON = "/data/datasets/multidetect-sim-assets/tools/venv/bin/python"
START_GIB, STOP_GIB = 60, 53
GIB = 1024**3
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
THRESHOLD_ERRORS = {"horizon_feasible_p95_exceeded", "horizon_feasible_max_exceeded"}


def read_cmd(args):
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, timeout=15, check=False)
    if result.returncode:
        raise ValueError(f"{' '.join(map(str, args))}: {result.stderr.strip() or result.returncode}")
    return result.stdout.strip()


def mount_check(*, write=False):
    share = "//192.168.1.1/AdminHome"
    allowed = {(str(NAS_SHARE), share), (str(NAS), f"{share}[/volatic]")}
    target = OUTPUT if OUTPUT.exists() else NAS
    rows = json.loads(read_cmd(["findmnt", "--json", "--target", str(target),
                                "--output", "TARGET,SOURCE,FSTYPE,OPTIONS"]))["filesystems"]
    if (not any((row["target"], row["source"]) in allowed and row["fstype"] == "cifs"
                and "rw" in row["options"].split(",") for row in rows)
            or NAS.is_symlink() or OUTPUT.is_symlink() or not os.access(target, os.W_OK)):
        raise ValueError(f"expected a writable X1 NAS share at {NAS}")
    if write:
        OUTPUT.mkdir(exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=OUTPUT, prefix=".probe-") as probe:
            probe.write(b"x1\n")
            probe.flush()
            os.fsync(probe.fileno())


def image_check(reference):
    template = '{{.Id}} {{index .Config.Labels "org.multidetect.gimbal-bridge-stabilize"}}'
    fields = read_cmd(["docker", "image", "inspect", "--format", template, reference]).split()
    if len(fields) != 2 or not IMAGE_ID.fullmatch(fields[0]) or fields[1] != "1":
        raise ValueError(f"candidate lacks an immutable image ID or bridge capability: {reference}")
    return fields[0]


def make_plan(spec, phase, image_id, enabled=None):
    if phase == "ab":
        trials = spec["ab"]
    elif phase == "final":
        trials = [(seed, enabled) for seed in spec["final_seeds"]]
    else:  # a single bridge-on Iowa flight in an independent output directory
        trials = [next(trial for trial in spec["ab"] if trial[1] is True)]
    return batch.expand({**spec["batch"], "batch_id": f"x1-resume-{phase}",
                         "output_root": str(OUTPUT / phase),
                         "jobs": [dict(spec["job"], seed=seed, image=image_id, gimbal_bridge_stabilize=mode)
                                  for seed, mode in trials]})


def preflight(spec):
    image_id = image_check(spec["image"])
    mount_check()
    if shutil.disk_usage("/data").free < START_GIB * GIB:
        raise ValueError(f"/data requires {START_GIB} GiB free to start")
    if os.getloadavg()[0] >= 60:
        raise ValueError("host one-minute load must be below 60")
    blockers = guard.gpu_blockers(spec["batch"]["gpu"])
    if blockers:
        raise ValueError("GPU 3 unavailable: " + ", ".join(blockers))
    if read_cmd(["docker", "ps", "--filter", "name=md-rw-long-slot-5-", "--format", "{{.Names}}"]):
        raise ValueError("existing wide-area flight on slot 5")
    for path in (MATRIX, SITL_PYTHON, TOOLS_PYTHON):
        if not os.access(path, os.X_OK):
            raise ValueError(f"required executable unavailable: {path}")
    plan = make_plan(spec, "ab", image_id)
    row = plan["jobs"][0]["matrix"]
    if not Path(row["resolved_root"]).is_dir():
        raise ValueError("Iowa resolved models are unavailable")
    world = Path(row["variant_world_root"]) / "rw_farm_iowa_w6km__clear_noon.sdf"
    if batch.file_hash(world) != spec["world_sha256"]:
        raise ValueError(f"historical Iowa A/B world SHA-256 mismatch: {world}")
    return image_id


def receipt_matches(record, job, *, allow_horizon_reject=False):
    desired = job["inputs"]
    mode = desired["gimbal_bridge_stabilize"]
    bridge = record.get("bridge_stabilize_check") or {}
    if (record.get("job_id") != job["job_id"] or record.get("voided_reason")
            or record.get("image_id") != desired["image"] or record.get("bridge_stabilize") is not mode
            or bridge.get("passed") is not True or bridge.get("env_enabled") is not mode
            or record.get("valid_round") is not True):
        return False
    if not allow_horizon_reject:
        return record.get("accepted") is True
    horizon = record.get("horizon_acceptance") or {}
    p95 = (horizon.get("feasible_error") or {}).get("p95_deg")
    if not batch.number(p95) or p95 < 0:
        return False
    errors = set(record.get("errors", []))
    return record.get("accepted") is True or bool(errors and errors <= THRESHOLD_ERRORS)


def qualify_ab(record, job):
    return receipt_matches(record, job, allow_horizon_reject=True)


def verdicts(plan, *, ab=False):
    records = batch.indexed(Path(plan["output_root"]) / ("results.jsonl" if ab else "accepted.jsonl"),
                            "attempt_id" if ab else "job_id")
    jobs = {job["job_id"]: job for job in plan["jobs"]}
    return {record["job_id"]: record for record in records.values()
            if record.get("job_id") in jobs
            and receipt_matches(record, jobs[record["job_id"]], allow_horizon_reject=ab)}


def identity(spec, image_id):
    return {"image": spec["image"], "image_id": image_id, "manifest_sha256": batch.file_hash(MANIFEST)}


def complete(spec):
    mount_check()
    path = OUTPUT / "final" / "COMPLETE.json"
    if not path.is_file():
        return False
    receipt = batch.read_object(path)
    pin = batch.read_object(OUTPUT / "PIN.json")
    image_id = pin.get("image_id", "")
    mode = receipt.get("bridge_stabilize")
    if not IMAGE_ID.fullmatch(image_id) or type(mode) is not bool or pin != identity(spec, image_id):
        return False
    final = make_plan(spec, "final", image_id, mode)
    records = verdicts(final)
    return len(records) == len(final["jobs"]) and receipt == completion(pin, mode, records)


def completion(pin, mode, records):
    return {**pin, "bridge_stabilize": mode,
            "accepted": {key: record["attempt_id"] for key, record in records.items()}}


def run(spec, image_id, *, smoke=False):
    mount_check(write=True)
    os.environ.update(WIDE_COLLECT_KEEP_HORIZON_REJECTED="1", WIDE_COLLECT_MIN_FREE_GB="0",
                      RTF_MATRIX_HOST_DISK_MIN_GIB=str(STOP_GIB), SITL_PYTHON=SITL_PYTHON, TOOLS_PYTHON=TOOLS_PYTHON,
                      RTF_MATRIX_CPU_MAX="1000", MULTIDETECT_SIM_CGROUP_PARENT="mdsim.slice")
    batch.MIN_FREE_BYTES = 0  # The batch root is on NAS. The guard checks /data in exact GiB.
    pin = identity(spec, image_id)
    pin_path = OUTPUT / ("SMOKE-PIN.json" if smoke else "PIN.json")
    if pin_path.exists() and batch.read_object(pin_path) != pin:
        raise ValueError(f"candidate image or manifest changed since {pin_path} was written")
    batch.atomic_text(pin_path, batch.dump(pin) + "\n")
    if smoke:
        plan = make_plan(spec, "smoke", image_id)
        rc = batch.run_batch(plan, MATRIX, completed=qualify_ab)
        if rc in (0, 1) and verdicts(plan, ab=True):
            return 0
        return rc or 1
    ab = make_plan(spec, "ab", image_id)
    rc = batch.run_batch(ab, MATRIX, completed=qualify_ab)
    if rc not in (0, 1):
        return rc
    records = verdicts(ab, ab=True)
    if len(records) != len(ab["jobs"]):
        print(f"A/B incomplete: {len(records)}/{len(ab['jobs'])} usable trials", file=sys.stderr)
        return 1
    selection = OUTPUT / "ab" / "CHOICE.json"
    modes = {job["job_id"]: job["inputs"]["gimbal_bridge_stabilize"] for job in ab["jobs"]}
    eligible = selection.with_name("qualified.jsonl")
    batch.atomic_text(eligible, "".join(batch.dump(record) + "\n" for record in records.values()))
    mode, scores = chain.select_mode(eligible, modes)
    choice = {**pin, "bridge_stabilize": mode, "scores": {key: list(value) for key, value in scores.items()}}
    if selection.exists() and batch.read_object(selection) != choice:
        raise ValueError("A/B evidence would change the recorded final plan")
    batch.atomic_text(selection, batch.dump(choice) + "\n")
    mount_check()
    final = make_plan(spec, "final", image_id, mode)
    rc = batch.run_batch(final, MATRIX)
    if rc not in (0, 1):
        return rc
    records = verdicts(final)
    if len(records) != len(final["jobs"]):
        return rc or 1
    batch.atomic_text(OUTPUT / "final" / "COMPLETE.json", batch.dump(completion(pin, mode, records)) + "\n")
    return 0


def dispatch(image_id, *, smoke):
    command = ["systemd-run", "--unit", UNIT, "--collect", "--working-directory", str(ROOT),
               "--property", f"RequiresMountsFor={NAS}",
               "--property", "KillMode=control-group", "--setenv", "PYTHONDONTWRITEBYTECODE=1",
               sys.executable, str(Path(__file__).resolve()), "run", "--image-id", image_id]
    if smoke:
        command.append("--smoke")
    print(read_cmd(command))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("preflight", "complete", "launch", "smoke"):
        actions.add_parser(action)
    worker = actions.add_parser("run", help="systemd worker started by launch or smoke")
    worker.add_argument("--image-id", required=True)
    worker.add_argument("--smoke", action="store_true")
    args = parser.parse_args(argv)
    dispatching = False
    try:
        spec = batch.read_object(MANIFEST)
        if args.action == "complete":
            return 0 if complete(spec) else 1
        if args.action == "launch" and complete(spec):
            return 0
        if args.action == "run":
            with LOCK.open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                image_id = preflight(spec)
                if image_id != args.image_id:
                    raise ValueError("candidate tag changed between dispatch and worker startup")
                return run(spec, image_id, smoke=args.smoke)
        image_id = preflight(spec)
        if args.action == "preflight":
            print(f"ready: {spec['image']} {image_id}")
        else:
            dispatching = True
            dispatch(image_id, smoke=args.action == "smoke")
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f"x1-resume: {exc}", file=sys.stderr)
        if args.action == "complete":
            return 1
        return 2 if dispatching else 75


if __name__ == "__main__":
    raise SystemExit(main())
