"""Select an exclusive GPU for X1 without touching other tenants' processes."""
from datetime import datetime, timezone
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

from scripts.realworld import wide_collect_batch as batch
from scripts.realworld.sample_stats import run_read

KEEPALIVE = Path("/data/projects/Multi-Detect-env/runs/keepalive")
ORDER = (3, 1, 2, 0)
UUID = re.compile(r"GPU-[0-9a-fA-F-]{36}\Z")
HEARTBEAT_MAX_AGE_S = 900  # Three five-minute keepalive intervals.
DESKTOP = {"Xorg", "Xwayland", "gnome-shell", "kwin_wayland", "sunshine"}
ACTIVE = {"ALIVE", "PAUSED", "START_REQUESTED"}
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
    registry = {}
    for line in (root / "registry.tsv").read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, kind, live, _, _, project, _, gpus = line.split("\t")
        if kind == "train-unit" or (kind == "codex-lane" and project == "Altair"):
            registry[name] = (kind, live, gpu_ids(gpus))
    return registry


def training_reservations(root, registry):
    """Include jobs that reserved a device but haven't created a CUDA context yet."""
    reserved = set()
    for name, (kind, _, _) in registry.items():
        if kind != "codex-lane":
            continue
        current_path = root / "state" / name / "current"
        try:
            current = current_path.read_text().strip()
        except FileNotFoundError:
            continue
        if current:
            rows = (root / "queue" / "altair" / "jobs.tsv").read_text().splitlines()
            matches = [row.split("\t")[2] for row in rows if row.split("\t", 1)[0] == current]
            if len(matches) != 1:
                raise ValueError(f"unknown Altair assignment for {current}")
            reserved.update(gpu_ids(matches[0]))

    fixed_units = {live.split()[1]: ids for kind, live, ids in registry.values()
                   if kind == "train-unit" and ids and re.fullmatch(r"training_unit \S+", live)}
    units = run_read(["systemctl", "list-units", "--no-legend", "--plain", "--full", "--no-pager",
                      "--state=active,activating,deactivating,reloading,refreshing",
                      "md-lfm3-*.service", "md-sched-*.service", *fixed_units], timeout=5)
    for line in units.splitlines():
        if not line.strip():
            continue
        unit = line.split()[0]
        if unit in fixed_units:
            reserved.update(fixed_units[unit])
            continue
        env = run_read(["systemctl", "show", "-p", "Environment", "--value", "--", unit], timeout=5)
        assigned = re.search(r"(?:^|\s)(?:SCHED_GPU|CUDA_VISIBLE_DEVICES)=([^\s]+)", env)
        if assigned:
            devices = assigned[1].split(",")
        elif unit.endswith("-workflow.service"):
            continue  # The sibling workers hold the actual GPU assignments.
        else:
            command = run_read(["systemctl", "show", "-p", "ExecStart", "--value", "--", unit], timeout=5)
            devices = re.findall(r"GPU-[0-9a-fA-F-]{36}", command)
        if not devices or any(d not in {"0", "1", "2", "3"} and not UUID.fullmatch(d) for d in devices):
            raise ValueError(f"unknown training GPU assignment for {unit}")
        reserved.update(int(d) if d in {"0", "1", "2", "3"} else d for d in devices)
    return reserved


def heartbeat(root, registry):
    snapshot = batch.read_object(root / "heartbeat.json")
    timestamp = datetime.fromisoformat(snapshot["timestamp"])
    age = (datetime.now(timezone.utc) - timestamp).total_seconds()
    if not 0 <= age <= HEARTBEAT_MAX_AGE_S:
        raise ValueError("keepalive GPU heartbeat is stale or future-dated")
    reserved = set()
    for entry in snapshot["entries"]:
        name = entry["name"]
        if name in registry and entry["status"] in ACTIVE:
            reserved.update(registry[name][2] if registry[name][0] == "train-unit" else
                            gpu_ids(entry["gpu_requirement"]))
    cards = {row["index"]: row for row in snapshot["gpu"]}
    if len(cards) != len(snapshot["gpu"]):
        raise ValueError("duplicate GPU index in keepalive heartbeat")
    return cards, reserved


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
    util = int(device.findtext("./utilization/gpu_util", "").removesuffix(" %"))
    reasons = metric(memory, 2001 if index == 3 else 2048, "gpu_memory_mib")
    reasons += metric(util, 5, "gpu_util_pct")
    processes = device.find("processes")
    if (processes is None or (processes.text or "").strip() not in {"", "None"}
            or any(item.tag != "process_info" for item in processes)):
        raise ValueError("unknown GPU processes")
    for item in processes:
        kind = item.findtext("type", "unknown").strip()
        name = Path(item.findtext("process_name", "")).name
        if index == 0 and kind == "G" and name in DESKTOP:
            continue
        reasons.append(f"gpu_process:{item.findtext('pid', 'unknown')}:{kind}:{name}")
    return uuid, reasons


def select(status, *, indices=ORDER, expected_uuid=None):
    """Record the decision atomically, then return one GPU or defer to the next tick."""
    errors = (OSError, ValueError, KeyError, IndexError, TypeError, RuntimeError,
              ET.ParseError, subprocess.SubprocessError)
    root = KEEPALIVE
    rejected = {}
    selection = None
    try:
        registry = training_registry(root)
        reserved = training_reservations(root, registry)
    except errors as exc:
        rejected["keepalive"] = [f"training_reservations_unknown:{exc}"]
    else:
        cards, heartbeat_reserved = None, set()
        for index in indices:
            reasons = []
            try:
                uuid, reasons = live_gpu(index)
                if index != 3:
                    if cards is None:
                        cards, heartbeat_reserved = heartbeat(root, registry)
                    row = cards[index]
                    if row["uuid"] != uuid:
                        reasons.append("heartbeat_gpu_uuid_mismatch")
                    reasons += metric(row["used_mib"], 2048, "heartbeat_memory_mib")
                    reasons += metric(row["util_pct"], 5, "heartbeat_util_pct")
                    if not isinstance(row["compute"], list) or row["compute"]:
                        reasons.append("heartbeat_compute_present_or_unknown")
                if index in reserved | heartbeat_reserved or uuid in reserved:
                    reasons.append("keepalive_training_reservation")
                if expected_uuid is not None and uuid != expected_uuid:
                    reasons.append("selected_gpu_uuid_changed")
            except errors as exc:
                reasons.append(f"gpu_probe_failed:{exc}")
            if not reasons:
                reason = "preferred_gpu3_idle" if index == 3 else "fallback_gpu_idle"
                selection = {"gpu_index": index, "gpu_uuid": uuid, "gpu_reason": reason}
                break
            rejected[index] = reasons
    record = {"state": "READY" if selection else "WAITING", "timestamp": batch.now(),
              "gpu_index": None, "gpu_uuid": None, "gpu_reason": UNAVAILABLE,
              "blocked_gpus": {str(index): reasons for index, reasons in rejected.items()},
              **(selection or {})}
    status.parent.mkdir(parents=True, exist_ok=True)
    batch.atomic_text(status, batch.dump(record) + "\n")
    if selection is None:
        raise GPUUnavailable(f"{UNAVAILABLE}: {rejected}")
    return selection
