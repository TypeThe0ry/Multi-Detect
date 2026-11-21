#!/usr/bin/env python3
"""Resume and supervise the six X1 asset builds on the workstation."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from scripts.realworld.x1_lock import (  # noqa: E402
    LOCK, TEMPFAIL, LockBusy, Yielded, flight_requested, try_lock, world_owner, world_phase,
)
from scripts.realworld.x1_pad import PAD_REJECTED, PadRejected  # noqa: E402
from scripts.realworld import x1_scenario_class as scenario_policy  # noqa: E402


def read_json(path):
    return json.loads(path.read_text())


CONFIG = read_json(ROOT / "configs/simulation/x1-worlds.json")
SITE_CONFIG = read_json(ROOT / "configs/sim-assets/realworld-sites.json")
SITES = tuple(f"{name}_w6km" for name in CONFIG["sites"])
ASSET_ROOT = Path(CONFIG["asset_root"])
NAS = Path(CONFIG["nas_root"])
TOOLS = CONFIG["tools_python"]
SYSTEM_PYTHON = "/usr/bin/python3"
ROUTE = HERE / "x1_world_route.py"
SEED_MODELS = ("rw_sky", "rw_actor_walk", "rw_movers")
DATA = Path("/data")
UNIT = "md-x1-worlds.service"
START_GIB, ABORT_GIB = 70, 53
GIB = 1024**3
POLL_SECONDS = 2
YIELD_GRACE_SECONDS = 10
YIELD_RECEIPT = "world-yield"
EXTENT = 6000
RETARGETED_SITES = {f"rw_{name}_w6km" for name in ("alpine_grindelwald", "port_rotterdam", "city_shenzhen")}


class Pending(RuntimeError):
    pass


class FlightsBusy(Pending):
    pass


class HardAbort(RuntimeError):
    pass


class MissingRawProducts(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def command(argv):
    return subprocess.run(argv, cwd=ROOT, text=True, capture_output=True, timeout=15, check=True).stdout


def check_mount(path, *, nas=False):
    mount = json.loads(command(["findmnt", "--json", "--target", str(path), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"]))["filesystems"][-1]
    if nas:
        share = "//192.168.1.1/AdminHome"
        valid = mount["fstype"] == "cifs" and mount["source"] in {share, f"{share}[/volatic]"}
    else:
        valid = path.resolve().is_relative_to(DATA.resolve()) and path.stat().st_dev == DATA.stat().st_dev
    if not valid or "rw" not in mount["options"].split(","):
        raise Pending(f"expected writable {'NAS' if nas else '/data'} mount at {path}")


def check_flights():
    output = command(["systemctl", "list-units", "--all", "--type=service",
                      "--state=active,activating,deactivating,reloading", "--plain", "--no-legend", "--no-pager",
                      "md-wc-*.service", "md-x1-*.service"])
    busy = [line.split()[0] for line in output.splitlines() if line.strip() and line.split()[0] != UNIT]
    if busy:
        raise FlightsBusy(f"X1/Wide Collect units busy: {', '.join(busy)}")


@contextmanager
def probe_lock():
    # Never create or replace the main lock inode from a read-only readiness probe.
    if flight_requested(LOCK):
        raise Pending("flight requested the X1 lock")
    try:
        stream = LOCK.open("rb")
    except FileNotFoundError:
        yield
        return
    with stream:
        if not try_lock(stream):
            raise Pending(f"X1 lock held: {LOCK}")
        yield


class Asset:
    def __init__(self, name):
        self.name = name
        self.base = name.removesuffix("_w6km")
        self.configured_site = SITE_CONFIG["sites"][name]
        self.site = self.configured_site
        self.base_site = SITE_CONFIG["sites"][self.base]
        self.source_2km = Path(SITE_CONFIG["asset_root"]) / CONFIG["sites"][self.base] / self.base / "raw"
        self.root = ASSET_ROOT / name / "v1"
        self.raw = self.root / "raw" / name / "raw"
        self.scene = self.root / "scenes" / name / "scene.json"
        self.models = self.root / "models"
        self.terrain = self.models / name / "model.sdf"
        self.world = self.root / "worlds" / f"{name}__clear_noon.sdf"
        self.state = self.root / ".pipeline"
        self.ready = self.root / "asset-ready.json"
        self.pad_journal = self.root.with_name(".x1-pad-retries-v1.json")

    def load_pad_history(self):
        self.site = self.configured_site
        if not self.pad_journal.exists():
            return []
        row = read_json(self.pad_journal)
        if row["configured_site"] != self.configured_site:
            raise ValueError(f"{self.pad_journal}: configured site changed; review existing retries")
        attempts = row["attempts"]
        if attempts:
            self.site = self.candidate_site(attempts[-1]["candidate"])
        return attempts

    def candidate_site(self, candidate):
        lat, lon = (candidate[key] for key in ("lat", "lon"))
        if not (-90 < lat < 90 and -180 <= lon <= 180):
            raise ValueError(f"{self.name}: invalid pad candidate coordinates")
        return {**self.configured_site, "lat": lat, "lon": lon, "origin_moved_for_pad": True}

    def origin_args(self):
        if self.site == self.configured_site:
            return []
        return ["--origin", str(self.site["lat"]), str(self.site["lon"])]

    def pin(self, site=None):
        return {"schema_version": 1, "site": self.name, "config": self.site if site is None else site,
                "base_config": self.base_site, "source_2km": str(self.source_2km),
                "model_seed": CONFIG["model_seed"]}

    def bind(self):
        path = self.state / "input.json"
        if path.exists():
            if read_json(path) != self.pin():
                raise ValueError(f"{self.name}: inputs changed after v1 was initialized")
        else:
            if self.root.exists() and any(p.name != ".pipeline" for p in self.root.iterdir()):
                raise ValueError(f"{self.root}: refusing to replace an unmanaged asset")
            atomic_json(path, self.pin())

    def install_pad(self, attempts):
        """Replay an interrupted origin change without mixing raw generations."""
        pin_path = self.state / "input.json"
        if pin_path.is_file() and read_json(pin_path) != self.pin():
            old_pin = read_json(pin_path)
            if attempts:
                prior = self.candidate_site(attempts[-2]["candidate"]) if len(attempts) > 1 else self.configured_site
                eligible = old_pin == self.pin(prior)
                archive = self.root.with_name(f"v1-rejected-pad-{len(attempts):02}")
            else:
                # One-time migration of the three manually retargeted, unaccepted v1 assets.
                old_site = old_pin["config"]
                changed = {k for k in old_site.keys() | self.site.keys() if old_site.get(k) != self.site.get(k)}
                eligible = (self.name in RETARGETED_SITES and self.site.get("origin_moved_for_pad") is True
                            and changed <= {"lat", "lon", "origin_moved_for_pad"} and old_pin == self.pin(old_site))
                archive = self.root.with_name("v1-before-pad-retarget")
            if (not eligible or self.root.is_symlink() or archive.exists() or archive.is_symlink()
                    or (self.root / "x1/preflight.json").exists()):
                raise ValueError(f"{self.name}: inputs changed after v1 was initialized")
            self.root.rename(archive)
        self.bind()

    def path_in_asset(self, relative):
        path = self.root / relative
        if Path(relative).is_absolute() or not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError(f"{self.name}: asset path escapes v1: {relative}")
        return path

    def required_file(self, path):
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError(f"missing or empty file: {path}")
        return path

    def fetch_provenance(self):
        path = self.raw / "provenance.json"
        value = read_json(path)
        if (value["site"] != self.name or value["size_m"] != EXTENT
                or any(value[key] != self.site[key] for key in ("lat", "lon"))
                or value["provider"] != self.site["provider"]):
            raise ValueError(f"{path}: raw metadata does not match the configured site")
        downloads = {row["path"]: row["bytes"] for row in value["downloads"]}
        required = {"osm.json", "dem.npy", "core.png"} | {f"naip_{i}_{j}.png" for i in range(6) for j in range(6)}
        if not required.issubset(downloads):
            missing = ", ".join(sorted(required - downloads.keys()))
            raise MissingRawProducts(f"{path}: provenance omits required raw products: {missing}")
        for relative, size in downloads.items():
            product = self.path_in_asset((self.raw / relative).relative_to(self.root))
            if not product.resolve().is_relative_to(self.raw.resolve()) or self.required_file(product).stat().st_size != size:
                raise ValueError(f"{path}: raw product mismatch: {relative}")
        return digest(path)

    def built(self, raw_hash):
        try:
            marker = read_json(self.state / "build.json")
            return marker == self.build_pin(raw_hash)
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def build_pin(self, raw_hash):
        return {"raw_sha256": raw_hash, "scene_sha256": digest(self.scene), "model_sha256": digest(self.terrain)}

    def receipt(self):
        row = read_json(self.ready)
        expected = {"schema_version": 1, "site": self.name, "size_m": EXTENT,
                    "world": str(self.world.relative_to(self.root)), "models": "models",
                    "scene": str(self.scene.relative_to(self.root)), "input": self.pin()}
        if any(row.get(key) != value for key, value in expected.items()):
            raise ValueError(f"{self.ready}: unexpected asset identity")
        for key in ("world", "scene"):
            if row[f"{key}_sha256"] != digest(self.path_in_asset(row[key])):
                raise ValueError(f"{self.ready}: {key} changed")
        if row["raw_provenance_sha256"] != self.fetch_provenance() or not self.built(row["raw_provenance_sha256"]):
            raise ValueError(f"{self.ready}: build or raw cache changed")
        resources = row["model_resources"]
        if not resources:
            raise ValueError(f"{self.ready}: no model resources")
        for relative, size in resources.items():
            candidate = self.path_in_asset(Path("models") / relative)
            if not candidate.resolve().is_relative_to(self.models.resolve()) or self.required_file(candidate).stat().st_size != size:
                raise ValueError(f"{self.ready}: model resource mismatch: {relative}")
        return row

    def route_receipt(self, world_hash):
        folder = self.root / "x1"
        row = read_json(folder / "preflight.json")
        stem = self.world.stem
        paths = {"scenario": f"{stem}.scenario.json", "sidecar": f"{stem}.long_range.json",
                 "map": f"{stem}.geojson", "dem": f"{stem}.dem.json"}
        if row.get("passed") is not True or row.get("world_sha256") != world_hash or row.get("scenario") != paths["scenario"]:
            raise ValueError(f"{folder}: no passing route receipt for this world")
        scenario_class = scenario_policy.configured_class(stem, CONFIG)
        scenario_policy.verify_route_policy(row, stem, scenario_class)
        for kind, name in paths.items():
            if row.get(f"{kind}_sha256") != digest(self.path_in_asset(Path("x1") / name)):
                raise ValueError(f"{folder}: route {kind} changed")
        scenario = read_json(folder / paths["scenario"])
        if scenario_class == scenario_policy.MOUNTAIN_SHORT:
            sidecar = read_json(folder / paths["sidecar"])
            threshold = scenario_policy.verify_route_policy(sidecar, stem, scenario_class)
            if (sidecar.get("minimum_max_distance_m") != threshold
                    or sidecar["wide_area_unreachable"] != row["wide_area_unreachable"]):
                raise ValueError(f"{folder}: mountain_short sidecar does not match route receipt")
        if set(scenario["channels"]) != {"runtime_input", "truth", "expectations"}:
            raise ValueError(f"{folder}: missing scenario channels")
        for channel in scenario["channels"].values():
            path = self.path_in_asset(Path("x1") / channel["path"])
            if not path.resolve().is_relative_to(folder.resolve()):
                raise ValueError(f"{folder}: scenario channel escapes x1")
            text = json.dumps(read_json(path), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if hashlib.sha256(text.encode()).hexdigest() != channel["sha256"]:
                raise ValueError(f"{folder}: scenario channel changed: {path.name}")
        return row


def free_bytes():
    return shutil.disk_usage(DATA).free


def input_fingerprint():
    """Hash of the config and code that decide world success; changes when a fix lands."""
    import hashlib
    digest = hashlib.sha256()
    names = sorted(HERE.glob("x1_world_*.py")) + [HERE / "fetch_site.py", HERE / "build_scene.py", HERE / "x1_find_pad.py", HERE / "x1_pad.py",
             HERE / "x1_scenario_class.py",
             ROOT / "configs/sim-assets/realworld-sites.json", ROOT / "configs/simulation/x1-worlds.json"]
    for path in names:
        if path.exists():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def check_prior_failure():
    """Do not relaunch when every attempt failed on identical inputs; wait for a fix."""
    receipt = NAS / "FAILED.json"
    if receipt.exists():
        data = read_json(receipt)
        if data.get("fingerprint") == input_fingerprint():
            raise Pending(f"previous run failed on identical inputs ({', '.join(data.get('sites', []))}); change code or config first")


def host_preflight(assets, *, check_start_disk=True, check_nas=True):
    if check_nas:
        check_mount(NAS if NAS.exists() else NAS.parent, nas=True)
    check_mount(ASSET_ROOT)
    if check_start_disk:
        free = free_bytes()
        if free < START_GIB * GIB:
            raise Pending(f"/data has {free / GIB:.2f} GiB free, need {START_GIB} GiB")
    # World fetch/build is CPU/network work (niced), not a real-time flight: allow a higher load ceiling.
    load_max = float(os.environ.get("X1_WORLD_LOAD_MAX", "110"))
    if os.getloadavg()[0] >= load_max:
        raise Pending(f"host one-minute load must be below {load_max:g}")
    check_flights()
    check_prior_failure()
    # Verify write access without publishing live status or touching asset state.
    with tempfile.NamedTemporaryFile(prefix=".preflight-", dir=NAS if NAS.is_dir() else NAS.parent):
        pass
    if not os.access(ASSET_ROOT, os.W_OK) or not os.access(TOOLS, os.X_OK):
        raise Pending("asset root must be writable and tools Python executable")
    if not ROUTE.is_file():
        raise Pending(f"offline route helper is missing: {ROUTE}")
    seed = ASSET_ROOT / CONFIG["model_seed"]
    for relative in ("rw_sky/noon.jpg", "rw_sky/dome.obj", "rw_actor_walk/meshes/walk.dae", "rw_movers/meshes/rc_boat.dae"):
        if not (seed / relative).is_file():
            raise Pending(f"seed model resource missing: {seed / relative}")
    for asset in assets:
        if not asset.ready.is_file():
            for name in ("provenance.json", "osm.json"):
                asset.required_file(asset.source_2km / name)


class Progress:
    def __init__(self, paths, log):
        self.log = log
        self.sites = {name: {"state": "pending"} for name in paths}
        try:
            old = read_json(NAS / "status.json")["sites"]
        except (OSError, ValueError, KeyError):
            old = {}
        self.sites.update({name: old[name] for name in self.sites if name in old})

    def write(self, state, *, site=None, phase=None, reason=None, free=None):
        payload = {"schema_version": 1, "updated_utc": now(), "state": state,
                   "site": site, "phase": phase, "reason": reason, "data_free_bytes": free}
        if site and phase is None and state not in {"asset_ready", "ready"}:
            payload["phase"] = self.sites[site].get("phase")
        if site:
            self.sites[site] = {key: payload[key] for key in ("state", "phase", "reason", "updated_utc")}
        atomic_json(NAS / "status.json", {**payload, "sites": self.sites})
        print(json.dumps(payload), file=self.log, flush=True)


def signal_group(proc, signo):
    try:
        os.killpg(proc.pid, signo)
    except ProcessLookupError:
        pass


def checked_free_bytes():
    try:
        free = free_bytes()
    except OSError as exc:
        raise HardAbort(f"/data free-space probe failed: {exc}") from exc
    if free < ABORT_GIB * GIB:
        raise HardAbort(f"/data has {free / GIB:.2f} GiB free, below {ABORT_GIB} GiB")
    return free


def guarded(argv, env, log, report: Callable, *, requested=None):
    """Suspend or kill the whole phase group while checking /data independently of child output."""
    proc = None
    paused = False
    previous = None
    yield_deadline = None
    try:
        while True:
            if requested is not None and requested():
                if yield_deadline is None:
                    yield_deadline = time.monotonic() + YIELD_GRACE_SECONDS
                if proc is None or paused or time.monotonic() >= yield_deadline:
                    # A slow network call or CPU phase can outlast the handoff
                    # budget. The finally block reaps it before releasing flock.
                    raise Yielded("flight requested the X1 lock; replay unfinished stage")
            free = checked_free_bytes()
            pause = free < START_GIB * GIB
            if pause and not paused and proc is not None:
                signal_group(proc, signal.SIGSTOP)
                paused = True
            state = "paused_disk" if pause else "running"
            if state != previous:
                report(state, free)
                previous = state
                # NAS status writes can block. Resample disk before spawning or
                # resuming a child after a blocked write.
                continue
            if paused and not pause:
                signal_group(proc, signal.SIGCONT)
                paused = False
            if proc is None and not pause:
                proc = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                        start_new_session=True)
            if proc is not None:
                try:
                    code = proc.wait(timeout=POLL_SECONDS)
                except subprocess.TimeoutExpired:
                    continue
                checked_free_bytes()
                if code == TEMPFAIL and env is not None and "MD_X1_FETCH_LOCK" in env:
                    raise Yielded("flight requested the X1 lock; fetch checkpoint saved")
                if code:
                    raise subprocess.CalledProcessError(code, argv)
                return
            # A pause before launch has no child to wait on.
            time.sleep(POLL_SECONDS)
    finally:
        if proc is not None:
            # Also cover surviving descendants on failure, stop, or a process exit.
            signal_group(proc, signal.SIGKILL)
            proc.wait()


def phase_command(asset, phase):
    if phase == "fetch":
        return [TOOLS, "-Bu", str(HERE / "fetch_site.py"), asset.name, "--raw-root", str(asset.root / "raw"),
                "--extent-m", str(EXTENT), *asset.origin_args()]
    if phase == "route":
        return [SYSTEM_PYTHON, "-Bu", str(ROUTE), "--asset", str(asset.root), "--world", str(asset.world)]
    return [TOOLS, "-Bu", str(Path(__file__).resolve()), "_phase", asset.name, phase]


def run_phase(asset, phase):
    # Heavy CPU and filesystem operations are children of guarded(), including the audits.
    if phase in {"check-2km", "check-6km"}:
        from scripts.realworld.x1_world_origin import audit, validate_dem

        raw = asset.source_2km if phase == "check-2km" else asset.raw
        base = asset.base_site if phase == "check-2km" else asset.site
        result = audit(raw, base, asset.site, extent_m=2000 if phase == "check-2km" else EXTENT)
        print(json.dumps(result), flush=True)
        atomic_json(asset.root / f"origin-{phase[-3:]}.json", result)
        if not result["passed"]:
            message = f"{asset.name}: {phase} launch origin failed the {result['clearance_m']} m water rule"
            if phase == "check-6km":
                raise PadRejected(message)
            raise ValueError(message)
        if phase == "check-6km":
            validate_dem(raw / "dem.npy")
    elif phase == "find-pad":
        from pyproj import Geod
        from scripts.realworld.x1_find_pad import candidates

        geod = Geod(ellps="WGS84")
        attempts = asset.load_pad_history()
        seen = [asset.configured_site, *(entry["candidate"] for entry in attempts)]

        def distance(a, b):
            return geod.inv(a["lon"], a["lat"], b["lon"], b["lat"])[2]

        def new(row):
            return (distance(row, seen[0]) <= 1800
                    and all(distance(row, other) >= 75 for other in seen))

        selected = None
        options = [{}, {"max_slope": 8, "max_relief": 6, "min_water": 80, "min_building": 60}]
        for limits in options:
            selected = next((row for row in candidates(asset.raw, **limits) if new(row)), None)
            if selected is not None:
                break
        atomic_json(asset.state / "next-pad.json", selected)
    elif phase == "build":
        for directory in (asset.terrain.parent, asset.scene.parent):
            if directory.exists():
                shutil.rmtree(directory)
        subprocess.run([TOOLS, "-Bu", str(HERE / "build_scene.py"), asset.name,
                        "--raw-root", str(asset.root / "raw"), "--scene-root", str(asset.scene.parent.parent),
                        "--output-root", str(asset.models), "--extent-m", str(EXTENT), *asset.origin_args()], check=True)
        atomic_json(asset.state / "build.json", asset.build_pin(asset.fetch_provenance()))
    elif phase == "make":
        from validate_wide_area import audit

        for name in SEED_MODELS:
            source = ASSET_ROOT / CONFIG["model_seed"] / name
            destination = asset.models / name
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(source, destination)
        subprocess.run([TOOLS, "-Bu", str(HERE / "make_world.py"), asset.name, "--weather", "clear_noon",
                        "--world-out-dir", str(asset.world.parent), "--extent-m", str(EXTENT),
                        "--scale-sky-to-extent", "--skip-scenarios"], check=True)
        result = audit(asset.scene, asset.world, asset.models)
        atomic_json(asset.root / "world-audit.json", result)
        world = ET.parse(asset.world).getroot().find("world")
        if (world is None or world.get("name") != "default"
                or result["extent_m"] != EXTENT or result["world_model_count"] == 0
                or not all(result[k] for k in ("budget_ok", "world_static_ok", "model_resources_ok"))):
            raise ValueError(f"{asset.name}: world audit failed, see world-audit.json")
        resources = {}
        for node in world.iter():
            if node.text and node.text.strip().startswith("model://"):
                relative = node.text.strip().removeprefix("model://")
                resources[relative] = asset.required_file(asset.path_in_asset(Path("models") / relative)).stat().st_size
        if not resources:
            raise ValueError(f"{asset.name}: missing world model references")
        atomic_json(asset.ready, {
            "schema_version": 1, "site": asset.name, "size_m": EXTENT, "ready_utc": now(),
            "world": str(asset.world.relative_to(asset.root)), "models": "models",
            "scene": str(asset.scene.relative_to(asset.root)), "input": asset.pin(),
            "world_sha256": digest(asset.world), "scene_sha256": digest(asset.scene),
            "raw_provenance_sha256": asset.fetch_provenance(), "model_resources": resources,
        })
    else:
        raise ValueError(f"unknown pipeline phase: {phase}")


def generate(asset, progress):
    with world_phase(LOCK):
        attempts = asset.load_pad_history()
        asset.install_pad(attempts)
    while True:
        try:
            generate_at_origin(asset, progress, use_base_audit=not attempts)
            return
        except PadRejected as exc:
            if len(attempts) >= CONFIG["max_pad_retries"]:
                raise ValueError(f"{asset.name}: exhausted {len(attempts)} pad retries ({exc})") from exc
            progress.write("retrying_pad", site=asset.name, reason=str(exc))
            run_guarded_phase(asset, progress, "find-pad")
            with world_phase(LOCK):
                candidate = read_json(asset.state / "next-pad.json")
                if candidate is None:
                    raise ValueError(f"{asset.name}: no other dry, open, flat pad found ({exc})") from exc
                site = asset.candidate_site(candidate)
                attempts.append({"candidate": candidate, "rejected_origin": [asset.site["lat"], asset.site["lon"]],
                                 "reason": str(exc), "utc": now()})
                # Publish the decision before moving v1. install_pad() replays either side of the rename.
                atomic_json(asset.pad_journal, {"configured_site": asset.configured_site, "attempts": attempts})
                asset.site = site
                asset.install_pad(attempts)
            progress.write("retrying_pad", site=asset.name, reason=f"retry {len(attempts)}: {candidate}")


def run_guarded_phase(asset, progress, phase):
    with world_phase(LOCK):
        scratch = asset.state / "tmp"
        scratch.mkdir(exist_ok=True)
        env = {**os.environ, "RW_SCENE_ROOT": str(asset.scene.parent.parent), "RW_RESOLVED_ROOT": str(asset.models),
               "RW_FACADE_OUT": str(Path(SITE_CONFIG["asset_root"]) / "library/facades"), "TMPDIR": str(scratch),
               "PYTHONPATH": f"{ROOT}:{ROOT / 'src'}", "PYTHONDONTWRITEBYTECODE": "1", "PROJ_NETWORK": "OFF"}
        if phase == "fetch":
            env["MD_X1_FETCH_LOCK"] = str(LOCK)
        try:
            guarded(phase_command(asset, phase), env, progress.log,
                    lambda state, free: progress.write(state, site=asset.name, phase=phase, free=free),
                    requested=lambda: flight_requested(LOCK))
        except subprocess.CalledProcessError as exc:
            if exc.returncode == PAD_REJECTED and phase in {"check-6km", "route"}:
                reason = "route rejected launch pad" if phase == "route" else "6 km water clearance failed"
                raise PadRejected(reason) from exc
            raise


def generate_at_origin(asset, progress, *, use_base_audit):
    def run(phase):
        run_guarded_phase(asset, progress, phase)

    if not asset.ready.exists():
        # The origin audits are small and replayable. All expensive stages are
        # independently restartable from their atomically published receipts.
        # Auto-selected pads can be outside the old 2 km cache. Each one must
        # still pass the matching, newly fetched 6 km audit before construction.
        if use_base_audit:
            run("check-2km")
        try:
            with world_phase(LOCK):
                asset.fetch_provenance()
        except (FileNotFoundError, MissingRawProducts):
            run("fetch")
        run("check-6km")
        with world_phase(LOCK):
            built = asset.built(asset.fetch_provenance())
        if not built:
            run("build")
        run("make")
    # Keep the asset receipt even if the route fails. A retry reuses the asset.
    with world_phase(LOCK):
        receipt = asset.receipt()
    progress.write("asset_ready", site=asset.name)
    if not (asset.root / "x1/preflight.json").exists():
        run("route")
    with world_phase(LOCK):
        asset.route_receipt(receipt["world_sha256"])
    progress.write("ready", site=asset.name)


def worker(assets, keepalive_state=None):
    with world_owner(LOCK):
        check_mount(NAS if NAS.exists() else NAS.parent, nas=True)
        NAS.mkdir(parents=True, exist_ok=True)
        with (NAS / "pipeline.log").open("a", buffering=1) as log:
            progress = Progress([asset.name for asset in assets], log)
            failed = []
            site = None
            try:
                with world_phase(LOCK):
                    host_preflight(assets, check_start_disk=False, check_nas=False)
                progress.write("starting", free=checked_free_bytes())
                for asset in assets:
                    site = asset.name
                    try:
                        generate(asset, progress)
                    except (HardAbort, Yielded):
                        raise
                    except (OSError, RuntimeError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
                        failed.append(site)
                        progress.write("failed", site=site, reason=str(exc))
            except (Yielded, FlightsBusy) as exc:
                # The dispatch was preempted, even if a previous site failed.
                # Its failure remains in the per-site status and phase log.
                if keepalive_state is not None:
                    attempt = read_json(keepalive_state / "attempts")
                    atomic_json(keepalive_state / YIELD_RECEIPT, {"state": "yielded", "attempt": attempt})
                progress.write("yielded", site=site, reason=str(exc))
                return 0
            except HardAbort as exc:
                progress.write("aborted_disk", site=site, reason=str(exc))
                return 2
            except KeyboardInterrupt:
                progress.write("interrupted", site=site, reason="worker stopped")
                return 130
            if failed:
                progress.write("failed", reason=f"failed sites: {', '.join(failed)}")
                atomic_json(NAS / "FAILED.json", {"fingerprint": input_fingerprint(), "sites": failed,
                                                  "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                return 1
            progress.write("complete")
            receipt = NAS / "FAILED.json"
            if receipt.exists():
                receipt.unlink()
            return 0


def reconcile_keepalive_yield(state_dir):
    """Refund one acknowledged dispatch under the keepalive supervisor's lock."""
    receipt = state_dir / YIELD_RECEIPT
    if not receipt.exists():
        return False
    attempts_path = state_dir / "attempts"
    attempts = int(attempts_path.read_text().strip()) if attempts_path.exists() else 0
    if attempts > 0 and read_json(receipt) == {"state": "yielded", "attempt": attempts}:
        atomic_json(attempts_path, attempts - 1)
    # If interrupted after the refund, the attempt no longer matches and the
    # next tick can consume the receipt without refunding again.
    receipt.unlink()
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "keepalive-preflight", "complete", "launch", "run", "origins", "_phase"))
    parser.add_argument("site", nargs="?", choices=SITES)
    parser.add_argument("phase", nargs="?")
    parser.add_argument("--keepalive-state", type=Path)
    args = parser.parse_args(argv)
    if args.keepalive_state is not None and args.action not in {"launch", "run", "keepalive-preflight"}:
        parser.error("--keepalive-state is only for keepalive dispatch")
    if args.action == "keepalive-preflight" and args.keepalive_state is None:
        parser.error("keepalive-preflight requires --keepalive-state")
    assets = [Asset(name) for name in SITES]
    dispatching = False
    try:
        if args.action == "_phase" and args.site and args.phase:
            asset = Asset(args.site)
            asset.load_pad_history()
            run_phase(asset, args.phase)
            return 0
        if args.site or args.phase or args.action == "_phase":
            parser.error("unexpected arguments")
        if args.action == "origins":
            if sys.executable != TOOLS:
                return subprocess.call([TOOLS, "-B", str(Path(__file__).resolve()), "origins"], cwd=ROOT)
            from scripts.realworld.x1_world_origin import audit

            rows = {a.name: audit(a.source_2km, a.base_site, a.site) for a in assets}
            print(json.dumps(rows, indent=2))
            return 0 if all(row["passed"] for row in rows.values()) else 1
        if args.action == "complete":
            for asset in assets:
                asset.load_pad_history()
                receipt = asset.receipt()
                asset.route_receipt(receipt["world_sha256"])
            print(f"all {len(assets)} X1 world assets and offline routes verified")
            return 0
        if args.action == "run":
            for sig in (signal.SIGTERM, signal.SIGHUP):
                signal.signal(sig, stop_worker)
            return worker(assets, args.keepalive_state)
        if args.action == "keepalive-preflight" and reconcile_keepalive_yield(args.keepalive_state):
            # keepalive cached attempts before running its live probe. Defer this
            # cycle so it re-reads the refunded count before the next dispatch.
            return 11
        with probe_lock():
            host_preflight(assets)
        if args.action in {"preflight", "keepalive-preflight"}:
            print(f"ready to generate {len(assets)} X1 world assets")
            return 0
        dispatching = True
        cmd = ["systemd-run", "--unit", UNIT, "--collect", "--service-type=exec", "--working-directory", str(ROOT),
               "--property", f"RequiresMountsFor={NAS} {ASSET_ROOT}", "--property", "KillMode=control-group",
               "--property", "Nice=10", SYSTEM_PYTHON, "-B", str(Path(__file__).resolve()), "run"]
        if args.keepalive_state is not None:
            cmd.extend(["--keepalive-state", str(args.keepalive_state.resolve())])
        print(command(cmd))
        return 0
    except PadRejected as exc:
        print(f"x1-worlds: {exc}", file=sys.stderr)
        return PAD_REJECTED
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
        print(f"x1-worlds: {detail}", file=sys.stderr)
        if args.action in {"preflight", "keepalive-preflight"}:
            return 11
        if args.action == "launch":
            return 2 if dispatching else 75
        if args.action == "run" and isinstance(exc, (Pending, LockBusy)):
            return 75
        return 1


def stop_worker(_signo, _frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    raise SystemExit(main())
