"""Select an idle or graphics-only GPU for X1 without touching other tenants."""
import fcntl
import os
from pathlib import Path
import re
import stat
import subprocess
import xml.etree.ElementTree as ET

from scripts.realworld import wide_collect_batch as batch
from scripts.realworld.sample_stats import run_read

KEEPALIVE = Path("/data/projects/Multi-Detect-env/runs/keepalive")
CGROUP_ROOT = Path("/sys/fs/cgroup")
GPU_LOCK_ROOT = Path("/data/scratch/multidetect-rtf-gpu-locks")
ORDER = (0, 1, 2, 3)
UUID = re.compile(r"GPU-[0-9a-fA-F-]{36}\Z")
DISPLAY_PROCESSES = {"xorg", "xwayland", "gnome-shell", "kwin_wayland", "kwin_x11", "sunshine",
                     "chrome", "chromium", "chromium-browser", "google-chrome", "firefox"}
SHARED_MEMORY_MIB = 1024
SHARED_UTIL_PCT = 60
BUSY_MEMORY_MIB = 2048
# Temporary opt-in: while this marker exists, Sunshine's idle compute context (type C, still bound by
# the shared memory/utilization limits) may share a GPU with X1. Delete the marker to restore the rule.
SUNSHINE_COMPUTE_MARKER = Path("/data/scratch/x1-allow-sunshine-compute")


# Temporary opt-in (coordinator decision 2026-11-23): while this marker exists, X1 may share a GPU with
# f1-aero's FluidX3D compute job when enough device memory is left for X1; acceptance still requires
# RTF >= 0.9 on the shared card, so an unsuitable pairing is rejected afterwards, not trusted here.
FLUIDX3D_MARKER = Path("/data/scratch/x1-allow-fluidx3d-share")
FLUIDX3D_MIN_FREE_MIB = 10500


def is_fluidx3d(name):
    return name.lower() == "fluidx3d"


def shareable(kind, name):
    return ((kind in {"G", "C+G"} and name.lower() in DISPLAY_PROCESSES)
            or (kind == "C" and name.lower() == "sunshine" and SUNSHINE_COMPUTE_MARKER.exists())
            or (kind == "C" and is_fluidx3d(name) and FLUIDX3D_MARKER.exists()))

UNAVAILABLE = "x1_gpu_unavailable"


class GPUUnavailable(ValueError):
    pass


def gpu_ids(value):
    if value == "-":
        return set()
    if not re.fullmatch(r"[0-3](,[0-3])*", value):
        raise ValueError(f"invalid GPU list {value!r}")
    return set(map(int, value.split(",")))


def training_registry(root):
    units = {}
    for line in (root / "registry.tsv").read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, kind, live, _, _, _, _, gpus = line.split("\t")
        if kind == "train-unit":
            match = re.fullmatch(r"training_unit (\S+)", live)
            if not match:
                raise ValueError(f"unknown unit for train-unit {name}")
            units[match[1]] = gpu_ids(gpus)
    return units


def compute_gpu_uuids():
    by_pid = {}
    rows = run_read(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid",
                     "--format=csv,noheader,nounits"], timeout=5)
    for line in rows.splitlines():
        if line.strip():
            uuid, pid = map(str.strip, line.split(","))
            if not UUID.fullmatch(uuid) or not pid.isdecimal() or int(pid) == 0:
                raise ValueError(f"unknown GPU compute process: {line}")
            by_pid.setdefault(int(pid), set()).add(uuid)
    return by_pid


def systemd_property(unit, name):
    return run_read(["systemctl", "show", "-p", name, "--value", "--", unit], timeout=5).strip()


def unit_gpu_uuids(unit, compute):
    group = systemd_property(unit, "ControlGroup")
    if not group:
        return set()  # The unit may still be starting.
    path = Path(group)
    if path.anchor != "/" or path == Path("/") or ".." in path.parts:
        raise ValueError(f"invalid ControlGroup for {unit}: {group!r}")

    def raise_walk_error(exc):
        raise exc

    # A training worker may run in a descendant cgroup of the systemd unit.
    uuids = set()
    for directory, _, _ in os.walk(CGROUP_ROOT / path.relative_to("/"), onerror=raise_walk_error):
        for pid in (Path(directory) / "cgroup.procs").read_text().split():
            uuids.update(compute.get(int(pid), ()))
    return uuids


def training_occupancy(root):
    registered = training_registry(root)
    listing = run_read(["systemctl", "list-units", "--no-legend", "--plain", "--full", "--no-pager",
                        "--state=active,activating,deactivating,reloading,refreshing",
                        "md-lfm3-*.service", "md-sched-*.service", *registered], timeout=5)
    units = [line.split()[0] for line in listing.splitlines() if line.strip()]
    compute = compute_gpu_uuids() if units else {}
    occupied = set()
    for unit in units:
        actual = unit_gpu_uuids(unit, compute) if compute else set()
        if actual:
            occupied.update(actual)
            continue
        if declared := registered.get(unit):
            occupied.update(declared)
            continue
        # A running unit without a CUDA context still needs its declared GPU during warm-up.
        env = systemd_property(unit, "Environment")
        assigned = re.search(r"(?:^|\s)(?:SCHED_GPU|CUDA_VISIBLE_DEVICES)=([^\s]+)", env)
        if assigned:
            devices = assigned[1].split(",")
        elif unit.endswith("-workflow.service"):
            continue  # The sibling workers hold the actual GPU assignments.
        else:
            command = systemd_property(unit, "ExecStart")
            devices = re.findall(r"GPU-[0-9a-fA-F-]{36}", command)
        if not devices or any(d not in {"0", "1", "2", "3"} and not UUID.fullmatch(d) for d in devices):
            raise ValueError(f"unknown training GPU assignment for {unit}")
        occupied.update(int(d) if d in {"0", "1", "2", "3"} else d for d in devices)
    return occupied


def metric(value, limit, label):
    if type(value) is not int or not 0 <= value:
        raise ValueError(f"unknown {label}")
    return [f"{label}:{value}"] if value >= limit else []


def live_gpu(index):
    xml = run_read(["nvidia-smi", "-i", str(index), "-q", "-x"], timeout=5)
    device, = ET.fromstring(xml).findall("gpu")
    uuid = device.findtext("uuid", "").strip()
    if not UUID.fullmatch(uuid):
        raise ValueError("invalid GPU UUID")
    memory = int(device.findtext("./fb_memory_usage/used", "").removesuffix(" MiB"))
    total = int(device.findtext("./fb_memory_usage/total", "0").removesuffix(" MiB") or 0)
    util = int(device.findtext("./utilization/gpu_util", "").removesuffix(" %"))
    processes = device.find("processes")
    if (processes is None or (processes.text or "").strip() not in {"", "None"}
            or any(item.tag != "process_info" for item in processes)):
        raise ValueError("unknown GPU processes")
    reasons = []
    shared = []
    for item in processes:
        kind = item.findtext("type", "unknown").strip()
        name = Path((item.findtext("process_name", "").split() or [""])[0]).name  # nvidia-smi may report the whole command line
        pid = item.findtext("pid", "").strip()
        if not shareable(kind, name) or not pid.isdecimal() or int(pid) == 0:
            reasons.append(f"gpu_process:{pid or 'unknown'}:{kind}:{name}")
            continue
        used = int(item.findtext("used_memory", "").removesuffix(" MiB"))
        if not is_fluidx3d(name):
            reasons += metric(used, SHARED_MEMORY_MIB, f"gpu_process_memory_mib:{pid}")
        shared.append({"pid": int(pid), "type": kind, "name": name, "used_mib": used})
    if any(is_fluidx3d(p["name"]) for p in shared):
        # Sharing with a compute job: the rule is device-memory headroom for X1, not idleness.
        if total <= 0 or total - memory < FLUIDX3D_MIN_FREE_MIB:
            reasons.append(f"gpu_free_memory_mib:{max(total - memory, 0)}<{FLUIDX3D_MIN_FREE_MIB}")
        return uuid, memory, util, shared, reasons
    reasons += metric(memory, SHARED_MEMORY_MIB if shared else BUSY_MEMORY_MIB, "gpu_memory_mib")
    reasons += metric(util, SHARED_UTIL_PCT if shared else 5, "gpu_util_pct")
    return uuid, memory, util, shared, reasons


def lease_available(uuid):
    root = Path(os.environ.get("RTF_MATRIX_GPU_LOCK_ROOT") or GPU_LOCK_ROOT)
    try:
        fd = os.open(root / f"{uuid}.lock", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return True
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"non-regular GPU lease file for {uuid}")
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


def select(status, *, indices=ORDER, expected_uuid=None, skip_leased=False):
    """Record the decision and return one GPU. Optional lease probes do not reserve it."""
    errors = (OSError, ValueError, KeyError, IndexError, TypeError, RuntimeError,
              ET.ParseError, subprocess.SubprocessError)
    root = KEEPALIVE
    rejected = {}
    selection = best_shared = None
    try:
        occupied = training_occupancy(root)
    except errors as exc:
        rejected["keepalive"] = [f"training_occupancy_unknown:{exc}"]
    else:
        for index in indices:
            reasons = []
            try:
                uuid, memory, util, shared, reasons = live_gpu(index)
                if index in occupied or uuid in occupied:
                    reasons.append("active_training_unit")
                if expected_uuid is not None and uuid != expected_uuid:
                    reasons.append("selected_gpu_uuid_changed")
                if not reasons and skip_leased and not lease_available(uuid):
                    reasons.append("gpu_lease_busy")
            except errors as exc:
                reasons.append(f"gpu_probe_failed:{exc}")
            if not reasons:
                reason = "primary_gpu_idle" if index in (0, 1) else "fallback_gpu_idle"
                candidate = {"gpu_index": index, "gpu_uuid": uuid,
                             "gpu_reason": "shared_graphics_only" if shared else reason,
                             "gpu_shared_processes": shared}
                if not shared:
                    selection = candidate
                    break
                weight = (memory, util, len(shared))
                if best_shared is None or weight < best_shared[0]:
                    best_shared = weight, candidate
            else:
                rejected[index] = reasons
        if selection is None and best_shared is not None:
            selection = best_shared[1]
    record = {"state": "READY" if selection else "WAITING", "timestamp": batch.now(),
              "gpu_index": None, "gpu_uuid": None, "gpu_reason": UNAVAILABLE, "gpu_shared_processes": [],
              "blocked_gpus": {str(index): reasons for index, reasons in rejected.items()},
              **(selection or {})}
    status.parent.mkdir(parents=True, exist_ok=True)
    batch.atomic_text(status, batch.dump(record) + "\n")
    if selection is None:
        raise GPUUnavailable(f"{UNAVAILABLE}: {rejected}")
    return selection
