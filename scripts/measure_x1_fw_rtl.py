#!/usr/bin/env python3
"""Dedicated ASSUMED_SIM Iowa FW-Hold -> native RTL measurement, not an X1 runner mode."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import itertools
import json
import math
import os
import re
import struct
import subprocess
import sys
import time
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath

from multidetect.autonomy.mission_package import load_mission_package
from multidetect.sim_teacher.x1_fw_native_rtl import PX4_RTL_COMMIT, _POSITIVE, _REQUIRED
from multidetect.sim_teacher.x1_native_handoff import (
    AUTO_LOITER, NativeHandoffPort, px4_mode, reader_ack_waiter,
)
from multidetect.sim_teacher.x1_scenario import CollectorScenario, load_schedule, wind_baseline, wind_server_config
from multidetect.target_geolocation import wgs84_forward_from_ned
from scripts import run_x1_autonomy_demo as demo
from scripts.analyze_px4_ulog import _parse_internal

IMAGE = "sha256:6ef7805c139866d61a2f20c7c260a789179aff984522654069e273ab3ab8aa79"
BASIS = "ASSUMED_SIM"
WORLD = "rw_farm_iowa_w6km"
SCENARIO = Path(f"configs/scenarios/realworld/{demo.AIRFRAME}/{WORLD}__clear_noon.scenario.json")
ROOT = demo.ROOT
HOLD_REF = "hold:yard-orbit"
CAPACITY_WH = 72.0
MIN_RUNS = 16
RAMP_MS = 4000
LOGGER = "/opt/px4-gazebo/bin/px4-logger"
# -s selects rcS explicitly, so PX4 keeps the launch workdir as its rootfs.
ULOG_ROOT = PurePosixPath("/opt/px4-gazebo/fs/log")
CRUISE_INDEX = 1
MATRIX_SCHEMA = "x1.fw-rtl-probe.matrix.v2"
COVERAGE_MINIMUM_REPLICATES = {"climb": 2, "alternate-loiter": 2, "case-c": 2}
CASE_C_SCHEDULE_SHA256 = "3a08c7d235adc95948ac66876af5b9b98d3aa5822d642962cb8a2d8ed6547fa7"
# Archived Case C command at route +25.440 s; the fault window ends at +30.500 s.
CASE_C_HOLD_WINDOW_MS = (25_440, 30_500)
RETRYABLE_ADMISSION = ("x1_gpu_unavailable", "one-minute load", "x1_start_load_exceeded",
                       "timed out waiting for mavlink_heartbeat", "SITL slots busy", "GiB free to start",
                       "existing wide-area flight on requested slots", "horizon stabilization did not settle")


class RetryableCell(RuntimeError):
    """Evidence is preserved, but a fresh attempt can recover this cell."""


class AttemptsExhausted(RuntimeError):
    """Skip this cell for now, but never report the matrix as complete."""


def _probe_parameters(cell):
    # Write the exact safety and per-cell pins before arm. Other positive
    # image/controller values are witnessed but not overridden.
    params = {**dict.fromkeys(_POSITIVE), **_REQUIRED, "RTL_RETURN_ALT": cell.return_alt, "SIM_BAT_DRAIN": 1800, "SIM_BAT_MIN_PCT": 50,
              "SIM_BAT1_DRAIN": cell.drain_s, "SIM_BAT1_MIN_PCT": 50}
    settings = tuple((k, str(v)) for k, v in params.items() if v is not None and k not in {"VT_TYPE", "SYS_AUTOSTART"})
    return params, settings


def require_sim(mode):
    if mode != "sim":
        raise ValueError("FW RTL probe requires explicit --mode sim; no real, HIL, or attached vehicle")


@dataclass(frozen=True)
class Cell:
    offset_s: int
    wind_mps: int
    drain_s: int
    cruise_agl_m: int
    repeat: int
    family: str = "core"
    return_alt: int = 60  # metres above PX4 Home, not AGL

    @property
    def id(self):
        suffix = f"o{self.offset_s}-w{self.wind_mps}-b{self.drain_s}-a{self.cruise_agl_m}-r{self.repeat}"
        return suffix if self.family == "core" else f"{self.family}-{suffix}"


def matrix(offsets, winds, drains, altitudes, repeats):
    for axis in (offsets, winds, drains, altitudes):
        if not axis or len(axis) != len(set(axis)) or any(type(v) is not int for v in axis):
            raise ValueError("matrix axes require distinct integers")
    if (len(offsets) < 4 or not all(8 <= t <= 100 for t in offsets)
            or 0 not in winds or not any(4 <= w <= 6 for w in winds)
            or any(w not in {0, 4, 5, 6} for w in winds)
            or len(drains) < 2 or not all(300 <= t <= 3600 for t in drains)
            or len(altitudes) < 2 or 85 not in altitudes or not all(75 <= h <= 100 for h in altitudes)
            or type(repeats) is not int or not 1 <= repeats <= 5):
        raise ValueError("need >=4 hold offsets (8–100 s), calm and 4–6 m/s, >=2 drains (300–3600 s),"
                         " >=2 cruise altitudes (75–100 m, one at 85 m), and 1–5 repeats")
    core = [Cell(*values) for values in itertools.product(
        sorted(offsets), sorted(winds), sorted(drains), sorted(altitudes), range(1, repeats + 1))]
    # CLIMBING has two distinct centres depending on the active setpoint. Both
    # families force a climb, at the archive's 85 m nominal approach altitude.
    return core + [Cell(offset, 0, 1800, 85, repeat, family, return_alt)
                   for family, offset, return_alt in (("climb", 39, 100), ("alternate-loiter", 0, 100),
                                                      ("case-c", 167, 60))
                   for repeat in range(1, COVERAGE_MINIMUM_REPLICATES[family] + 1)]


def _exact_pins(cell):
    return {key: value for key, value in _probe_parameters(cell)[0].items() if value is not None}


def json_file(path, value):
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _copy_ulog(docker, container, source, destination):
    # Stage outside the collector's *.ulg glob until Docker has copied a complete file.
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / source.name
    pending = target.with_suffix(".partial")
    try:
        result = subprocess.run([docker, "cp", f"{container}:{source}", str(pending)],
                                capture_output=True, text=True, timeout=90, check=False)
        if result.returncode:
            raise RuntimeError(f"ULog copy failed ({result.returncode}): {(result.stderr or result.stdout).strip()}")
        if pending.stat().st_size == 0:
            raise RuntimeError(f"ULog copy is empty: {source}")
        pending.rename(target)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ULog copy timed out: {source}") from exc
    finally:
        pending.unlink(missing_ok=True)
    return target


class _Probe(CollectorScenario):
    """Only constructed by the measurement CLI; never installed in production or demo."""

    def __init__(self, mode, *, cell, faults, terrain, hold, params, guard, episode_root):
        require_sim(mode)
        super().__init__(faults, clearance=lambda *_: None, terrain=terrain)
        self.cell, self.site_hold, self.params, self.guard = cell, hold, params, guard
        self.episode_root = episode_root
        self.hold_boot_ms = None
        self.rtl_sent = False
        self.wind_target_boot_ms = None
        self.route_origin_boot_ms = None

    def bind(self, docker, container, world, reader, connection, record, episode_id, *, clock):
        super().bind(docker, container, world, reader, connection, record, episode_id, clock=clock)
        self.docker, self.container = docker, container
        self.episode_dir = self.episode_root / episode_id
        if math.hypot(*wind_baseline(world)[1]) > 0.01:
            raise RuntimeError("probe requires a calm Iowa SDF baseline")
        if self.faults:
            apply = self.transport.apply

            def witnessed_apply(fault, *, wind_fraction=None):
                result = apply(fault, wind_fraction=wind_fraction)
                stamp = self.clock()
                self.record({"message_type": "fw_rtl_probe_wind_publish", "boot_ms": stamp,
                             "fraction": wind_fraction, **result})
                if wind_fraction == 1 and self.wind_target_boot_ms is None:
                    self.wind_target_boot_ms = stamp
                return result

            self.transport.apply = witnessed_apply
        self.native = NativeHandoffPort(connection, reader, self.route_gate, record,
                                        wait_ack=reader_ack_waiter(reader), peer=self.target_peer,
                                        local_from_wgs84=self.terrain.local_from_wgs84)

    def _readback(self):
        commits = re.findall(r"(?m)^\s*PX4 git-hash:\s*([a-f0-9]{40})\s*$",
                             self.transport.command("/opt/px4-gazebo/bin/px4-ver", "all"))
        values = {name: self.transport._get_param(name) for name in sorted(self.params)}
        ok = commits == [PX4_RTL_COMMIT] and all(
            values[k] > 0 if v is None else math.isclose(values[k], v, rel_tol=0, abs_tol=1e-6)
            for k, v in self.params.items())
        return {"observed_px4_commits": commits, "observed_parameters": values, "verified": ok}

    def prepare(self):
        self.transport.prepare(self.faults)
        self.prearm = self._readback()
        self.record({"message_type": "scenario_fw_rtl_readback", "basis": BASIS, **self.prearm})
        if not self.prearm["verified"]:
            raise RuntimeError("probe candidate prearm parameter or commit mismatch")
        self.transport.command(LOGGER, "on")  # Override arm/disarm logging in this owned container.
        # Remember the single full log before arming, not whichever file is newest at shutdown.
        deadline = time.monotonic() + 15
        while True:
            paths = self.transport.command("find", str(ULOG_ROOT), "-type", "f", "-name", "*.ulg",
                                           "-size", "+0c", "-print").splitlines()
            if len(paths) == 1:
                self.ulog_source = PurePosixPath(paths[0])
                break
            if paths or time.monotonic() >= deadline:
                raise RuntimeError(f"expected one nonempty prearm ULog in {ULOG_ROOT}, found {len(paths)}")
            time.sleep(0.2)

    def start(self):
        # Case C uses the original route-start clock, not the later Hold clock.
        # Core wind starts on confirmation and persists until disarm.
        if self.cell.family == "case-c":
            super().start()
            self.route_origin_boot_ms = self.worker.timeline.origin

    def _dispatch(self, point, boot, clock):
        self.rtl_sent = True
        self.record({"message_type": "fw_rtl_probe_dispatch", "basis": BASIS,
                     "motion_proof": False, "boot_ms": boot, "hold_boot_ms": self.hold_boot_ms,
                     "position": point, "clock_ms": clock, "family": self.cell.family})
        self.native.rtl()  # Exactly NAV_RETURN_TO_LAUNCH(0,0,0,0,0,0,0).

    def _sample(self):
        state = self.reader.latest_control_state()
        hb = self._fresh_sample("HEARTBEAT", state=state) or {}
        ext = self._fresh_sample("EXTENDED_SYS_STATE", state=state) or {}
        pos = self._fresh_sample("GLOBAL_POSITION_INT", state=state, max_age_ms=500)
        return hb, ext, self._motion_position(pos), (pos or {}).get("time_boot_ms")

    def tick(self, index, gimbal, *, lifecycle=None, step_boundary=False, **_):
        self.check()
        self.step = index
        if lifecycle is not None or self.native_active or index < CRUISE_INDEX:
            return
        if index > CRUISE_INDEX:
            raise RetryableCell("missing FW cruise probe trigger; no later route action admitted")
        hb, ext, point, boot = self._sample()
        if (point is None or boot is None or not hb.get("base_mode", 0) & 128
                or hb.get("system_status") != 4 or ext.get("vtol_state") != 4 or ext.get("landed_state") != 2
                or point[0] < 160):
            return
        n, e, altitude = point
        n_min, n_max, e_min, e_max, z_min, z_max = self.site_hold["entry"]
        hud = self._fresh_sample("VFR_HUD") or {}
        gps = self._fresh_sample("GPS_RAW_INT") or {}
        low, high = self.site_hold["airspeed_mps"]
        if (px4_mode(hb.get("custom_mode")) != (6, 0)
                or not (n_min <= n <= n_max and e_min <= e <= e_max and z_min <= altitude <= z_max)
                or not low <= hud.get("airspeed", math.nan) <= high
                or not 0 <= hud.get("groundspeed", math.nan) <= self.site_hold["max_entry_speed_mps"]
                or gps.get("fix_type", 0) < 3):
            return
        local = self._fresh_sample("LOCAL_POSITION_NED", max_age_ms=500) or {}
        ned = tuple(local.get(axis) for axis in ("x", "y", "z"))
        if not all(type(v) in (int, float) and math.isfinite(v) for v in ned):
            return
        if not self.guard(ned):
            return
        clock = self.clock()
        if self.cell.family == "case-c":
            lo, hi = CASE_C_HOLD_WINDOW_MS
            if clock is None or self.route_origin_boot_ms is None:
                return
            elapsed = clock - self.route_origin_boot_ms
            if elapsed >= hi:
                raise RetryableCell("Case C missed its guarded Hold window")
            if elapsed < lo or not self.markers or abs(clock - boot) > 500:
                return
        if self.cell.family == "alternate-loiter" and (clock is None or abs(clock - boot) > 500):
            return
        if not self.native.claim():
            raise RuntimeError("could not claim exclusive route ownership")
        if self.cell.family == "alternate-loiter":
            self._dispatch(point, boot, clock)
            return
        self.record({"message_type": "fw_rtl_probe_hold_request", "boot_ms": boot, "position": point})
        target = self.site_hold
        self.native.loiter_at(target["lat_lon"], target["radius_m"], target["centre"][2], speed=target["speed_mps"])

    def finish_native(self, index, *, check, timeout_s=900, sleep=time.sleep, monotonic=time.monotonic):
        deadline = monotonic() + timeout_s
        while monotonic() < deadline:
            check()
            self.check()
            if self.native.poll() == "unconfirmed":
                raise RuntimeError(f"probe {self.native.action} unconfirmed; PX4 remains in control")
            hb, ext, point, boot = self._sample()
            clock = self.clock()
            if not self.rtl_sent and self.native.confirmed("loiter") and clock is not None:
                if self.hold_boot_ms is None:
                    self.hold_boot_ms = clock
                    if self.faults and self.cell.family != "case-c":
                        super().start()
                    self.record({"message_type": "fw_rtl_probe_hold_confirmed", "boot_ms": clock})
                elapsed = clock - self.hold_boot_ms
                if elapsed >= self.cell.offset_s * 1000:
                    centre = self.site_hold["centre"]
                    if (elapsed > self.cell.offset_s * 1000 + 500 or point is None or boot is None
                            or abs(boot - clock) > 500 or px4_mode(hb.get("custom_mode")) != AUTO_LOITER
                            or not hb.get("base_mode", 0) & 128 or hb.get("system_status") != 4
                            or ext.get("vtol_state") != 4 or ext.get("landed_state") != 2
                            or abs(math.dist(point[:2], centre[:2]) - self.site_hold["radius_m"])
                               > self.site_hold["padding_ne_m"] - self.site_hold["radius_m"]
                            or abs(point[2] - centre[2]) > self.site_hold["padding_alt_m"]):
                        raise RetryableCell("no fresh, legal FW Hold sample at the probe offset")
                    if self.faults and (self.wind_target_boot_ms is None or self.wind_target_boot_ms > clock):
                        raise RuntimeError("full wind vector was not published before the probe offset")
                    if self.cell.family == "case-c" and self.transport.wind_written:
                        raise RetryableCell("Case C wind was not restored before the RTL dispatch")
                    self._dispatch(point, boot, clock)
            if self.native.confirmed("return") and self.native.landed() and type(hb.get("base_mode")) is int:
                if not hb["base_mode"] & 128 and boot is not None:
                    post = self._readback()
                    if post != self.prearm:
                        raise RuntimeError("probe configuration changed after arming")
                    self.record({"message_type": "fw_rtl_probe_disarmed", "basis": BASIS, "boot_ms": boot, **post})
                    # Stop and flush after disarm, before the collector stops/removes the container.
                    self.transport.command(LOGGER, "stop")
                    archive = _copy_ulog(self.docker, self.container, self.ulog_source,
                                         self.episode_dir / "container-evidence/px4-ulog")
                    self.record({"message_type": "fw_rtl_probe_ulog_copied", "source": str(self.ulog_source),
                                 "path": archive.relative_to(self.episode_dir).as_posix(),
                                 "bytes": archive.stat().st_size})
                    return {"status": "landed", "method": "native_return", "landed_state": 1,
                            "disarmed": True, "native_handoff": self.native.history}
            sleep(0.05)
        raise TimeoutError("FW RTL probe did not reach confirmed landing and disarm")

    def close(self, *, completed=False):
        # The probe's wind window intentionally extends to disarm. Drain any
        # publish, restore the baseline, then require the restore publish to succeed.
        super().close(completed=False)
        if completed and self.faults:
            if self.wind_target_boot_ms is None or self.transport.wind_written:
                raise RuntimeError("probe wind apply/restore did not complete")


def _prepare(args, cell, out, site, inputs):
    document = demo.read_object(args.mission_package)
    plan_json = demo.read_object(args.recorded_plan)
    if (document.get("places", {}).get("poi:yard") != [320, 0]
            or [s["skill"] for s in plan_json["steps"]] != [
                "vtol_takeoff", "fw_transition", "fw_goto_waypoint", "back_transition", "return_home"]
            or plan_json["steps"][2]["params"]["target_ref"] != "poi:yard"):
        raise ValueError("expected the fixed Iowa yard mission and recorded five-step plan")
    for step in plan_json["steps"]:
        if "altitude_agl_m" in step["params"]:
            step["params"]["altitude_agl_m"] = cell.cruise_agl_m
    sentence = f"Fly over the yard at {cell.cruise_agl_m} m AGL, then return home and land."
    package = load_mission_package({**document, "task_text": sentence}, mode="sim")
    plan, caps = demo.compile_plan(package, lambda *_: json.dumps(plan_json), {"validator": {}})
    collector_plan, steps, _, mission = demo.prepare(package, plan, caps, site, output=out, slot=args.slot)
    hold = demo.assumptions.generate(mode="sim", world=demo.assumptions.WORLD,
                                     image=demo.IMAGE, profile_path=demo.PROFILE)["native"]["holds"][HOLD_REF]["value"]
    north, east, height = hold["centre"]
    lat, lon = wgs84_forward_from_ned(latitude_deg=site.terrain.origin_latitude_deg,
                                      longitude_deg=site.terrain.origin_longitude_deg,
                                      north_offset_m=north, east_offset_m=east)
    north, east = site.terrain.local_from_wgs84(lat, lon)
    delta = cell.cruise_agl_m - 85
    entry = [*hold["entry"][:4], *(value + delta for value in hold["entry"][4:])]
    home_ne = site.terrain.local_from_wgs84(site.home.latitude_deg, site.home.longitude_deg)
    pad_amsl = site.terrain.sample_local(*home_ne).elevation_m
    hold = {"ref": HOLD_REF, "centre": [north, east, height + delta],
            "lat_lon": [lat, lon], "entry": entry,
            "airspeed_mps": hold["airspeed"], "max_entry_speed_mps": hold["max_entry_speed_mps"], "radius_m": hold["radius_m"],
            "speed_mps": hold["tangential_speed_mps"], "padding_ne_m": hold["padding_ne_m"],
            "padding_alt_m": hold["padding_alt_m"]}
    params, settings = _probe_parameters(cell)
    collector_plan = replace(collector_plan, extra_sim_params=settings, keep_px4_ulog=True,
                             seed=cell.repeat, px4_profile="main")
    faults = ()
    if cell.family == "case-c":
        schedule = out / "wind.json"
        schedule.write_bytes(args.case_c_schedule.read_bytes())
        if digest(schedule) != inputs["case_c_schedule_sha256"]:
            raise ValueError("Case C schedule changed during staging")
        faults = load_schedule(schedule, ("wind_step",))
    elif cell.wind_mps:
        theta = math.radians(args.wind_toward_deg)
        schedule = out / "wind.json"
        json_file(schedule, {"schema": "x1.contingency.faults.v1", "faults": [{"type": "wind_step",
                  "at_ms": 0, "duration_ms": 1_000_000, "values": {"north_mps": cell.wind_mps * math.cos(theta),
                  "east_mps": cell.wind_mps * math.sin(theta), "down_mps": 0, "ramp_ms": RAMP_MS}}]})
        faults = load_schedule(schedule, ("wind_step",))
    probe = _Probe(args.mode, cell=cell, faults=faults, terrain=site.terrain, hold=hold,
                   params=params, guard=mission["x1_cruise_guard"],
                   episode_root=Path(collector_plan.out) / "episodes")
    record = {"basis": BASIS, "mode": args.mode, "image_id": IMAGE, "px4_commit": PX4_RTL_COMMIT,
              "cell": asdict(cell), "run_id": cell.id, "hold": hold, "inputs": inputs, "parameter_pins": _exact_pins(cell),
              "pad": [*home_ne, pad_amsl], "dem": str(site.dem_path), "scenario": site.scenario_id}
    if cell.family == "case-c":
        record["case_c_schedule"] = demo.read_object(schedule)
    return collector_plan, steps, mission, probe, record


def _launch(args, plan, steps, mission, probe, out):
    require_sim(args.mode)
    from scripts import collect_sim_teacher_episodes as collector
    from scripts.realworld import collection_guard, x1_gpu, x1_resume
    from scripts.realworld.x1_lock import LOCK, flight_lock
    from scripts.run_simulator_docker import X1_WIND_AIRSPEED_ENV
    from scripts.sitl_lock import SITL_LOCK_HELD_ENV, SitlLock, sitl_lock_context

    if (plan.attach_container or Path(sys.executable).resolve() != Path(demo.long_range.SITL_PYTHON).resolve()
            or os.environ.get(SITL_LOCK_HELD_ENV) == "1"
            or os.environ.get("MULTIDETECT_PX4_PROFILE") != "main"
            or os.environ.get("MULTIDETECT_PX4_LOGGER_MODE") != "default"
            or int(os.environ.get("MULTIDETECT_SITL_SLOTS", "0")) < 3):
        raise ValueError("probe requires a fresh owned main-profile SITL, default ULog, SITL Python and >=3 slots")
    with flight_lock(LOCK, shared=True):
        if x1_resume.image_check(IMAGE) != IMAGE:
            raise ValueError("probe candidate image mismatch")
        x1_resume.flight_gate((args.slot,))
        with sitl_lock_context(SitlLock(label="x1-fw-rtl-probe", slots=8, slot=args.slot, retry_s=20), wait=False):
            selection = x1_gpu.select(out / "GPU-PREFLIGHT.json", skip_leased=True)
            gpu, uuid = selection["gpu_index"], selection["gpu_uuid"]
            lock_root = Path(os.environ.get("RTF_MATRIX_GPU_LOCK_ROOT") or x1_gpu.GPU_LOCK_ROOT)
            lock_root.mkdir(parents=True, exist_ok=True)
            with (lock_root / f"{uuid}.lock").open("ab") as lease, ExitStack() as stack:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                admission = collection_guard.x1_start(out, gpu, collection_guard.environment_settings(), uuid)
                json_file(out / "gpu-admission.json", admission)
                stack.callback(demo._restore_env, X1_WIND_AIRSPEED_ENV, os.environ.pop(X1_WIND_AIRSPEED_ENV, None))
                config = "MULTIDETECT_RTF_SERVER_CONFIG"
                stack.callback(demo._restore_env, config, os.environ.get(config))
                source = ROOT / "simulation/gazebo/server-wide-area.config"
                os.environ[config] = str(source.resolve())
                if probe.faults:
                    target = out / "x1-wind-server.config"
                    wind_server_config(source, target, wind_relative_airspeed=True)
                    os.environ[config] = str(target.resolve())
                    os.environ[X1_WIND_AIRSPEED_ENV] = str(args.wind_plugin.resolve())
                stack.enter_context(demo.long_range.collector_overrides(
                    collector, mission, x1_heading_deg=steps[1].required_mc_heading_deg))
                plan = replace(plan, image=IMAGE, requested_image=IMAGE, gpu_render=True,
                               gpu_device=str(gpu), gpu_devices=(str(gpu),))
                return collector.run_episode(plan, Path(plan.truth), 0, land_after_route=True,
                                             policy_override=demo.MissionPolicy(steps, -20), x1_scenario=probe)


def _cell_evidence_error(out):
    """Return the archived evidence defect, or None for a clean completed cell."""
    try:
        episode = (out / demo.read_object(out / "episode.json")["episode"]).resolve()
    except (OSError, KeyError, TypeError, ValueError) as exc:
        return f"missing or unreadable episode pointer: {exc}"
    if not episode.is_relative_to(out.resolve()):
        raise ValueError("episode path escaped its matrix cell")
    try:
        if demo.read_object(episode / "episode.json").get("status") != "complete":
            return "episode status is not complete"
        ulogs = demo.read_object(episode / "container-evidence/index.json").get("ulog_files")
        if not isinstance(ulogs, list) or len(ulogs) != 1:
            return "expected exactly one ULog in the capture index"
        ulog = (episode / ulogs[0]).resolve()
    except (OSError, TypeError, ValueError) as exc:
        return f"incomplete episode capture index: {exc}"
    if not ulog.is_relative_to(episode):
        raise ValueError("ULog path escaped its episode")
    try:
        warnings = _parse_internal(ulog.read_bytes(), ulog).warnings
    except (OSError, ValueError, struct.error) as exc:
        return f"missing or unreadable ULog: {exc}"
    if warnings:
        return "; ".join(warnings[:3])
    return None


def _retryable(exc):
    return any(isinstance(error, (RetryableCell, BlockingIOError, TimeoutError))
               or (isinstance(error, (ValueError, RuntimeError))
                   and any(marker in str(error) for marker in RETRYABLE_ADMISSION))
               for error in (exc, exc.__cause__))


def _run_cell(args, cell, out, site, inputs, launch):
    archive = args.output / "failed-evidence"
    attempts = (re.fullmatch(rf"{re.escape(cell.id)}\.try([0-9]+)", path.name)
                for path in archive.glob(f"{cell.id}.try*"))
    used = max((int(match[1]) for match in attempts if match), default=0)

    def preserve(reason):
        nonlocal used
        archive.mkdir(exist_ok=True)
        used += 1
        dest = archive / f"{cell.id}.try{used:02}"
        out.rename(dest)
        json_file(dest / "probe-reflight.json", {"cell": cell.id, "attempt": used,
                                                "status": "reflight_required", "reason": reason})
        print(f"{cell.id}: retained attempt {used} at {dest}: {reason}", file=sys.stderr)

    def retry_or_exhaust():
        if used >= args.max_attempts:
            raise AttemptsExhausted(f"{cell.id}: exhausted {args.max_attempts} attempts; evidence in {archive}")
        time.sleep(args.retry_delay_s)

    if out.exists():
        reason = _cell_evidence_error(out)
        if reason is None:
            return
        if args.max_attempts == 1:
            raise ValueError(f"incomplete cell at {out}: {reason}; use --max-attempts to preserve and retry")
        preserve(reason)
        retry_or_exhaust()
    if used >= args.max_attempts:
        raise AttemptsExhausted(f"{cell.id}: exhausted {args.max_attempts} attempts; evidence in {archive}")
    while True:
        out.mkdir()
        plan, steps, mission, probe, record = _prepare(args, cell, out, site, inputs)
        json_file(out / "probe.json", record)
        if args.prepare_only:
            return
        try:
            episode = launch(args, plan, steps, mission, probe, out)
            json_file(out / "episode.json", {"episode": str(episode.resolve().relative_to(out.resolve()))})
            reason = _cell_evidence_error(out)
            if reason is not None:
                raise RetryableCell(f"incomplete cell evidence: {reason}")
            return
        except (OSError, ValueError, RuntimeError) as exc:
            if args.max_attempts == 1 or not _retryable(exc):
                raise
            preserve(f"{type(exc).__name__}: {exc}")
            retry_or_exhaust()


def run(args, *, launch=_launch):
    require_sim(args.mode)
    cells = matrix(args.offsets_s, args.winds_mps, args.drains_s, args.altitudes_agl_m, args.repeats)
    if not 1 <= args.max_attempts <= 12 or not 0 <= args.retry_delay_s <= 3600:
        raise ValueError("--max-attempts must be 1–12 and --retry-delay-s 0–3600")
    if not 0 <= args.wind_toward_deg < 360:
        raise ValueError("wind bearing must be a finite direction toward which wind blows [0, 360)")
    if digest(args.case_c_schedule) != CASE_C_SCHEDULE_SHA256:
        raise ValueError("Case C requires the byte-identical archived wind schedule")
    case_c_schedule = demo.read_object(args.case_c_schedule)
    receipt = demo.read_object(args.image_check)
    if any(receipt.get(k) != v for k, v in {"image_id": IMAGE, "px4_commit": PX4_RTL_COMMIT,
                                           "basis": BASIS, "status": "static_verified"}.items()):
        raise ValueError("probe requires static verification for the exact candidate image")
    site = demo.load_site(WORLD, ROOT / SCENARIO)
    if (site.world_sha256, site.terrain.source_sha256) != (demo.assumptions.WORLD_SHA, demo.assumptions.DEM_SHA):
        raise ValueError("probe requires the pinned Case C world and DEM")
    inputs = {"world_sha256": site.world_sha256, "dem_sha256": site.terrain.source_sha256,
              "airframe_sha256": digest(demo.PROFILE), "static_check_sha256": digest(args.image_check),
              "recorded_plan_sha256": digest(args.recorded_plan), "mission_package_sha256": digest(args.mission_package),
              "wind_plugin_sha256": digest(args.wind_plugin), "wind_toward_deg": args.wind_toward_deg,
              "capture_script_sha256": digest(Path(__file__)),
              "case_c_schedule_sha256": CASE_C_SCHEDULE_SHA256,
              "gazebo_server_sha256": digest(ROOT / "simulation/gazebo/server-wide-area.config")}
    matrix_record = {"schema": MATRIX_SCHEMA, "basis": BASIS, "mode": "sim", "image_id": IMAGE,
                     "px4_commit": PX4_RTL_COMMIT, "inputs": inputs, "cells": [asdict(c) for c in cells],
                     "coverage_minimum_replicates": COVERAGE_MINIMUM_REPLICATES, "case_c_schedule": case_c_schedule,
                     "cell_parameter_pins": {cell.id: _exact_pins(cell) for cell in cells}}
    selected = [cell for cell in cells if args.cell is None or cell.id == args.cell]
    if not selected:
        raise ValueError(f"unknown matrix cell: {args.cell}")
    if args.resume:
        if args.prepare_only or demo.read_object(args.output / "matrix.json") != matrix_record:
            raise ValueError("resume requires an identical matrix, assets and capture script")
    else:
        args.output.mkdir(parents=True)
        json_file(args.output / "matrix.json", matrix_record)
    with (args.output / ".probe.lock").open("ab") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        exhausted = []
        for cell in selected:
            try:
                _run_cell(args, cell, args.output / cell.id, site, inputs, launch)
            except AttemptsExhausted as exc:
                exhausted.append(str(exc))
        if exhausted:
            raise RuntimeError("; ".join(exhausted))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("sim",))
    parser.add_argument("--slot", required=True, type=int, choices=demo.SLOTS)
    for name in ("image-check", "mission-package", "recorded-plan", "wind-plugin", "case-c-schedule", "output"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    parser.add_argument("--offsets-s", type=int, nargs="+", default=[30, 39, 48, 57])
    parser.add_argument("--winds-mps", type=int, nargs="+", default=[0, 6])
    parser.add_argument("--drains-s", type=int, nargs="+", default=[600, 1800])
    parser.add_argument("--altitudes-agl-m", type=int, nargs="+", default=[75, 85])
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--wind-toward-deg", type=float, default=333.435, help="toward bearing, clockwise from north")
    parser.add_argument("--cell", help="run one declared cell ID while retaining the complete expected matrix")
    parser.add_argument("--resume", action="store_true", help="run missing cells in an identical matrix")
    parser.add_argument("--max-attempts", type=int, default=1,
                        help="total attempts per cell (1–12); >1 archives failed evidence and retries")
    parser.add_argument("--retry-delay-s", type=int, default=120, help="seconds before each automatic retry (0–3600)")
    parser.add_argument("--prepare-only", action="store_true", help="validate and stage inputs without container/GPU access")
    args = parser.parse_args(argv)
    try:
        run(args)
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        parser.exit(2, f"FW RTL probe: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
