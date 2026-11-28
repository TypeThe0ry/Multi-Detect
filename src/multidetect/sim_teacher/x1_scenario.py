"""Opt-in fault windows for the disposable X1 SITL collector."""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event, Lock, Thread

from multidetect.autonomy.serialization import canonical_sha256
from multidetect.autonomy.x1_contingency_replay import Fault, load_faults
from multidetect.autonomy.x1_mission import X1PlanError
from multidetect.autonomy.x1_wind_estimate import WindEstimator, WindReading
from multidetect.px4_mission import PX4_AUTOPILOT_COMPONENT_ID

from .x1_live_clearance import MOTION_SAMPLE_MAX_AGE_MS
from .x1_native_handoff import AUTO_LOITER, NativeHandoffPort, RouteWriteGate, px4_mode
from .x1_retask import AirborneRouteSlot
from .x1_scenario_observation import project_target_found

# A hold re-request must reach Altair and come back within the 3 s decision
# deadline before the proven residence bound runs out.
HOLD_REVALIDATION_LEAD_MS = 10_000
NATIVE_LANDING_TIMEOUT_S = 900.0
_DISPATCHED = frozenset({"accepted_unconfirmed", "sent_unconfirmed"})

SUPPORTED = frozenset({"wind_step", "battery_drop", "link_loss", "target_lost", "target_found", "terrain_conflict"})
MECHANISM = {
    "wind_step": "gazebo_wind_cmd", "battery_drop": "px4_battery_simulator",
    "link_loss": "collector_gcs_heartbeat_gate", "target_lost": "target_observation_mask",
    "target_found": "injected_target_detection", "terrain_conflict": "forward_dem_offset",
}


def load_schedule(path: Path, kinds=()) -> tuple[Fault, ...]:
    faults = load_faults(path)
    selected = set(kinds) if kinds else {fault.kind for fault in faults}
    if not selected or selected - SUPPORTED:
        raise ValueError(f"select supported SITL faults explicitly: {sorted(selected - SUPPORTED)}")
    if selected - {fault.kind for fault in faults}:
        raise ValueError("requested SITL fault is absent from scenario JSON")
    result = tuple(fault for fault in faults if fault.kind in selected)
    for fault in result:
        if fault.kind == "battery_drop" and (not 0 <= fault.values["pct"] < 100
                                              or fault.end_ms - fault.at_ms < 1500):
            raise ValueError("battery drop requires pct < 100 and at least 1500 sim-ms")
        if fault.kind == "terrain_conflict" and fault.values["clearance_m"] >= 0:
            raise ValueError("terrain conflict requires negative clearance")
    return result


def schedule_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _wind_target(values):
    return values["east_mps"], values["north_mps"], -values["down_mps"]


class _WindRamp:
    PUBLISH_INTERVAL_MS = 100

    def __init__(self, index, fault, baseline, started_boot_ms):
        self.index = index
        self.fault = fault
        self.duration = fault.values["ramp_ms"]
        self.started_boot_ms = started_boot_ms
        self.parameters = {"ramp_ms": self.duration, "publish_interval_ms": self.PUBLISH_INTERVAL_MS,
                           "baseline_enu_mps": list(baseline), "target_enu_mps": list(_wind_target(fault.values))}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="x1-sitl-wind")
        self.future = None
        self.phase = None
        self.last_applied = {}
        self.last_fraction = None

    def fraction(self, elapsed):
        up_ms = elapsed - self.fault.at_ms
        down_ms = elapsed - (self.fault.end_ms - self.duration)
        if down_ms >= 0:
            sample = down_ms // self.PUBLISH_INTERVAL_MS * self.PUBLISH_INTERVAL_MS
            return max(0, 1 - sample / self.duration)
        if up_ms >= self.duration:
            return 1
        return (up_ms // self.PUBLISH_INTERVAL_MS * self.PUBLISH_INTERVAL_MS) / self.duration

    def submit(self, phase, operation, clock):
        def publish():
            details = operation()
            return details, clock()

        self.phase = phase
        self.future = self.executor.submit(publish)

    def applied(self):
        return {**self.parameters, "enu_mps": None, **self.last_applied}


class ScenarioTimeline:
    """Single owner of the actuators. A half-open window missed by the clock is a failure."""

    def __init__(self, faults, clock, transport, record, *, origin_ms):
        self.clock = clock
        self.origin = self.last = origin_ms
        self.transport = transport
        self.record = record
        self.active = {}
        self.edges = sorted(
            [(offset, on, index, fault) for index, fault in enumerate(faults)
             for offset, on in ((fault.at_ms, True), (fault.end_ms, False))],
            key=lambda edge: edge[:3],
        )
        self.next_edge = 0
        self.wind_ramp = None

    def _mark(self, fault, index, phase, scheduled, **details):
        self.record({"message_type": f"scenario_{phase}", "fault": fault.kind, "fault_index": index,
                     "mechanism": MECHANISM[fault.kind], "values": dict(fault.values),
                     "scheduled_offset_ms": scheduled, "origin_boot_ms": self.origin,
                     "observed_boot_ms": self.clock(), "receive_monotonic_ns": time.monotonic_ns(), **details})

    def tick(self):
        self._poll_wind()
        if self.wind_ramp is not None and self.wind_ramp.phase == "restore":
            return  # preserve wind restore ordering at shared edges without waiting on the command
        while self.next_edge < len(self.edges):
            stamp = self.clock()
            if stamp is None or stamp < self.last:
                raise RuntimeError("X1 scenario PX4 clock missing or reset")
            self.last = stamp
            elapsed = stamp - self.origin
            offset, enabled, index, fault = self.edges[self.next_edge]
            if elapsed < offset:
                break
            self.next_edge += 1
            if enabled and elapsed >= fault.end_ms:
                self._mark(fault, index, "missed", offset)
                raise RuntimeError(f"X1 scenario window missed: {fault.kind}")
            try:
                if enabled:
                    self.active[index] = fault  # restore even after a partial write
                    if fault.kind == "wind_step" and fault.values.get("ramp_ms", 0):
                        self.wind_ramp = _WindRamp(index, fault, self.transport.wind[1], stamp)
                        self._sample_wind(elapsed, "apply")
                        continue
                    details = self.transport.apply(fault)
                    applied_stamp = self.clock()
                    if applied_stamp is None or applied_stamp < stamp or applied_stamp - self.origin >= fault.end_ms:
                        self._mark(fault, index, "missed", offset, applied=dict(details))
                        raise RuntimeError(f"X1 scenario apply exceeded its window: {fault.kind}")
                    self._mark(fault, index, "injected", offset, applied=dict(details))
                else:
                    if self.wind_ramp is not None and self.wind_ramp.index == index:
                        ramp = self.wind_ramp
                        if ramp.future is not None:
                            self.next_edge -= 1
                        else:
                            ramp.submit("restore", lambda: self.transport.restore(fault), self.clock)
                        return
                    self._restore(index, fault, "window_end")
            except Exception as exc:
                self._mark(fault, index, "error", offset, error=str(exc))
                raise
        if self.wind_ramp is not None and self.wind_ramp.future is None:
            self._sample_wind(self.last - self.origin, "update")

    def _sample_wind(self, elapsed, phase):
        ramp = self.wind_ramp
        fraction = ramp.fraction(elapsed)
        if fraction == ramp.last_fraction:
            return  # hold the last vector and skip obsolete samples after a slow publish
        ramp.last_fraction = fraction
        ramp.submit(phase, lambda: self.transport.apply(ramp.fault, wind_fraction=fraction), self.clock)

    def _poll_wind(self, *, wait=False):
        ramp = self.wind_ramp
        if ramp is None or ramp.future is None or (not wait and not ramp.future.done()):
            return
        fault, future = ramp.fault, ramp.future
        ramp.future = None
        offset = fault.end_ms if ramp.phase == "restore" else fault.at_ms
        try:
            details, stamp = future.result()
            ramp.last_applied = details
            if ramp.phase == "apply":
                if stamp is None or stamp < ramp.started_boot_ms or stamp - self.origin >= fault.end_ms:
                    self._mark(fault, ramp.index, "missed", offset, applied=ramp.applied())
                    raise RuntimeError(f"X1 scenario apply exceeded its window: {fault.kind}")
                self._mark(fault, ramp.index, "injected", offset, applied=ramp.applied())
            elif ramp.phase == "restore":
                self._finish_restore(ramp.index, fault, "window_end", details)
        except Exception as exc:
            self._mark(fault, ramp.index, "error", offset, error=str(exc), applied=ramp.applied())
            raise

    def _restore(self, index, fault, reason):
        self._finish_restore(index, fault, reason, self.transport.restore(fault))

    def _finish_restore(self, index, fault, reason, details):
        ramp = self.wind_ramp
        if ramp is not None and ramp.index == index:
            ramp.last_applied = details or ramp.last_applied
            details = ramp.applied()
            ramp.executor.shutdown(wait=False)
            self.wind_ramp = None
        del self.active[index]
        self._mark(fault, index, "restored", fault.end_ms, reason=reason, applied=dict(details))

    def close(self, *, completed=False):
        errors = []
        try:
            self._poll_wind(wait=True)
        except Exception as exc:
            errors.append(f"wind_step: {exc}")
        for index, fault in reversed(tuple(self.active.items())):
            ramp = self.wind_ramp
            is_ramped_wind = ramp is not None and ramp.index == index
            # The pending write has drained. A failed cleanup publish gets one
            # bounded retry of the exact baseline, never another ramp sample.
            for _ in range(2 if is_ramped_wind else 1):
                try:
                    self._restore(index, fault, "episode_exit")
                    break
                except Exception as exc:
                    details = {"applied": ramp.applied()} if is_ramped_wind else {}
                    self._mark(fault, index, "restore_error", fault.end_ms, error=str(exc), **details)
                    errors.append(f"{fault.kind}: {exc}")
        if self.wind_ramp is not None:
            self.wind_ramp.executor.shutdown()
        if completed and self.next_edge != len(self.edges):
            for offset, on, index, fault in self.edges[self.next_edge:]:
                if on:
                    self._mark(fault, index, "missed", offset, reason="episode_ended")
            errors.append("episode ended before all scheduled window edges")
        if errors:
            raise RuntimeError("X1 scenario incomplete: " + "; ".join(errors))


def wind_baseline(world_sdf):
    worlds = ET.parse(world_sdf).getroot().findall("world")
    if len(worlds) != 1:
        raise ValueError("wind injection requires one SDF world")
    world = worlds[0]
    name = world.get("name", "")
    velocity = tuple(float(x) for x in world.findtext("wind/linear_velocity", "0 0 0").split())
    if (not re.fullmatch(r"[A-Za-z0-9_-]+", name) or len(velocity) != 3
            or not all(map(math.isfinite, velocity))):
        raise ValueError("invalid SDF world name or ENU wind baseline")
    return f"/world/{name}/wind", velocity


class SitlScenarioTransport:
    """A command runner bound to a collector-owned container, plus local observation gates."""

    PARAM = "/opt/px4-gazebo/bin/px4-param"

    def __init__(self, command, *, wind=None, battery_sample=None, clearance=None, link_lock=None,
                 recovery_exempt=None):
        self.command = command
        # Returns a reason only while a confirmed native return/land-now owns the vehicle.
        self.recovery_exempt = recovery_exempt
        self.wind = wind
        self.battery_sample = battery_sample
        self.clearance = clearance
        self.link_lock = link_lock if link_lock is not None else Lock()
        self.link_suppressed = Event()
        self.target_masked = Event()
        self.target_detection = None
        self.target_first_sample = None
        self.terrain_offset = None
        self.battery_prior = None
        self.battery_params = None
        self.wind_written = False

    @classmethod
    def in_container(cls, docker, container, **kwargs):
        def command(*args):
            result = subprocess.run([docker, "exec", container, *args], capture_output=True,
                                    text=True, timeout=4, check=False)
            if result.returncode:
                raise RuntimeError(f"{' '.join(args[:3])}: {result.stderr.strip() or result.stdout.strip()}")
            return result.stdout
        return cls(command, **kwargs)

    def prepare(self, faults):
        kinds = {fault.kind for fault in faults}
        if "wind_step" in kinds:
            topic, _ = self.wind
            deadline = time.monotonic() + 90.0
            while True:
                info = self.command("gz", "topic", "-i", "-t", topic)
                subscribers = info.partition("Subscribers")[2]
                if "gz.msgs.Wind" in subscribers or time.monotonic() >= deadline:
                    break
                time.sleep(2.0)
            if "gz.msgs.Wind" not in subscribers:
                raise RuntimeError(f"Gazebo WindEffects subscriber unavailable: {topic}: {info.strip()}")
        if "battery_drop" in kinds:
            drain = self._get_param("SIM_BAT_DRAIN")
            floor = self._get_param("SIM_BAT_MIN_PCT")
            if drain <= 0 or not 0 <= floor <= 100:
                raise RuntimeError("PX4 battery simulator drain or floor is unavailable")
            self.battery_params = drain, floor

    def _get_param(self, name):
        value = float(self.command(self.PARAM, "show", "-q", name).strip())
        if not math.isfinite(value):
            raise RuntimeError(f"invalid PX4 readback for {name}")
        return value

    def _set_param(self, name, value):
        self.command(self.PARAM, "set", name, format(value, ".9g"))
        if not math.isclose(self._get_param(name), value, rel_tol=1e-5, abs_tol=1e-5):
            raise RuntimeError(f"PX4 parameter readback mismatch for {name}")

    def _wind(self, enu):
        topic, _ = self.wind
        x, y, z = enu
        self.command("gz", "topic", "-t", topic, "-m", "gz.msgs.Wind", "-p",
                     f"enable_wind: true linear_velocity: {{x: {x} y: {y} z: {z}}}")
        return {"topic": topic, "enu_mps": list(enu), "readback": "publish_only"}

    def apply(self, fault, *, wind_fraction=None):
        kind, values = fault.kind, fault.values
        if kind == "wind_step":
            if wind_fraction is None:
                wind_fraction = 0 if values.get("ramp_ms", 0) else 1
            target = _wind_target(values)
            baseline = self.wind[1]
            if wind_fraction == 1:
                enu = target
            elif wind_fraction == 0:
                enu = baseline
            else:
                enu = tuple((1 - wind_fraction) * base + wind_fraction * goal
                            for base, goal in zip(baseline, target))
            self.wind_written = True
            return self._wind(enu)
        if kind == "battery_drop":
            baseline = self.battery_sample()
            if baseline is None or not values["pct"] < baseline <= 100:
                raise RuntimeError("battery drop requires a fresh native BATTERY_STATUS above the requested floor")
            self.battery_prior = baseline
            self._set_param("SIM_BAT_MIN_PCT", values["pct"])
            self._set_param("SIM_BAT_DRAIN", 1)
            return {"floor_pct": values["pct"], "drain_s": 1, "pre_drop_pct": baseline,
                    "readback": "parameters"}
        if kind == "link_loss":
            with self.link_lock:
                self.link_suppressed.set()
            return {"gcs_heartbeat_suppressed": True}
        if kind == "target_lost":
            self.target_masked.set()
            return {"target_observations_masked": True}
        if kind == "target_found":
            self.target_detection = values
            return {"target_report_injected": True}
        baseline = self.clearance()
        if baseline is None or not math.isfinite(baseline) or baseline < 0:
            raise RuntimeError("forward DEM needs a finite nonnegative baseline clearance")
        self.terrain_offset = baseline - values["clearance_m"]
        return {"base_clearance_m": baseline, "elevation_offset_m": self.terrain_offset}

    def restore(self, fault):
        kind = fault.kind
        if kind == "wind_step" and self.wind_written:
            result = self._wind(self.wind[1])
            self.wind_written = False
            return result
        if kind == "battery_drop" and self.battery_prior is not None:
            drain, floor = self.battery_params
            self._set_param("SIM_BAT_DRAIN", drain)
            # Refill the simulated pack to the captured percentage before removing
            # the temporary floor. The native low-battery mode may remain latched.
            recovery = {"status": "observed", "expected_min_pct": self.battery_prior - 1}
            try:
                self._set_param("SIM_BAT_MIN_PCT", self.battery_prior)
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    sample = self.battery_sample()
                    if sample is not None and sample >= self.battery_prior - 1:
                        break
                    time.sleep(0.05)
                else:
                    # The check guards a resumed route. A confirmed native return or
                    # land-now never resumes it, so record why it was not applied.
                    reason = self.recovery_exempt() if self.recovery_exempt is not None else None
                    if reason is None:
                        raise RuntimeError("PX4 battery percentage recovery was not observed")
                    recovery = {**recovery, "status": "not_applicable", "reason": reason}
            finally:
                self._set_param("SIM_BAT_MIN_PCT", floor)
            self.battery_prior = None
            return {"floor_pct": floor, "drain_s": drain, "observed_recovery_pct": sample, "recovery": recovery}
        if kind == "link_loss":
            with self.link_lock:
                self.link_suppressed.clear()
            return {"gcs_heartbeat_suppressed": False}
        if kind == "target_lost":
            self.target_masked.clear()
            return {"target_observations_masked": False}
        if kind == "target_found":
            self.target_detection = None
            return {"target_report_injected": False}
        if kind == "terrain_conflict":
            self.terrain_offset = None
            return {"elevation_offset_m": 0}
        return {}

    def target_observation(self, observation):
        return {"visible": False, "track_state": "lost"} if self.target_masked.is_set() else observation

    def perception_report(self, now_ms):
        current = self.target_detection
        if current is None or self.target_masked.is_set():
            return None
        previous = self.target_first_sample
        if previous is None or previous[0] is not current:
            self.target_first_sample = current, now_ms
        return project_target_found(current, observed_ms=self.target_first_sample[1], now_ms=now_ms)

    def terrain_observation(self):
        base = self.clearance() if self.clearance is not None else None
        if base is None:
            return None
        margin = base - (self.terrain_offset or 0)
        return {"clearance_m": margin, "conflict": margin < 0}


class ScenarioWorker:
    """Keep sim-time scheduling alive during the collector's blocking primitives."""

    def __init__(self):
        self.stop = Event()
        self.timeline = None
        self.thread = None
        self.error = None

    def start(self, faults, clock, transport, record):
        origin = clock()
        if origin is None:
            raise RuntimeError("X1 scenario needs PX4 LOCAL_POSITION_NED boot time")
        self.timeline = ScenarioTimeline(faults, clock, transport, record, origin_ms=origin)
        record({"message_type": "scenario_clock_started", "origin_boot_ms": origin,
                "receive_monotonic_ns": time.monotonic_ns()})
        self.thread = Thread(target=self._run, name="x1-sitl-scenario", daemon=True)
        self.thread.start()

    def _run(self):
        try:
            while not self.stop.is_set():
                self.timeline.tick()
                self.stop.wait(0.02)
        except Exception as exc:
            self.error = exc

    def check(self):
        if self.error is not None:
            raise RuntimeError(f"X1 scenario worker failed: {self.error}") from self.error

    def close(self, *, completed=False):
        if self.thread is None:
            return
        self.stop.set()
        self.thread.join()
        self.thread = None
        self.timeline.close(completed=completed)
        self.check()


def wind_server_config(source, output, *, wind_relative_airspeed=False):
    """Derive the owned launch config. Keep the normal physics/sensor systems."""
    config = ET.parse(source)
    plugins = config.find("plugins")
    if plugins is None:
        raise ValueError("Gazebo server config has no plugins stanza")
    if not any(plugin.get("name") == "gz::sim::systems::WindEffects" for plugin in plugins):
        plugin = ET.SubElement(plugins, "plugin", entity_name="*", entity_type="world",
                               filename="gz-sim-wind-effects-system", name="gz::sim::systems::WindEffects")
        ET.SubElement(plugin, "force_approximation_scaling_factor").text = "0.3"
        horizontal = ET.SubElement(plugin, "horizontal")
        for axis in ("magnitude", "direction"):
            ET.SubElement(ET.SubElement(horizontal, axis), "time_for_rise").text = "0.2"
    if wind_relative_airspeed:
        matches = [plugin for plugin in plugins.findall("plugin")
                   if plugin.get("filename") == "gz-sim-air-speed-system"
                   or plugin.get("name") == "gz::sim::systems::AirSpeed"]
        if len(matches) != 1:
            raise ValueError("X1 wind airspeed requires exactly one stock AirSpeed system")
        matches[0].set("filename", "libx1_wind_airspeed.so")
        matches[0].set("name", "multidetect::x1::WindAirspeed")
    config.write(output, encoding="utf-8", xml_declaration=True)


class CollectorScenario:
    """Bridge the owned episode clock, telemetry and original compiled route to the injector."""

    def __init__(self, faults, *, clearance, make_decision=None, terrain=None, motion_evidence=None,
                 cadence_steps=(), perception_tick=None):
        self.faults = faults
        self.clearance = clearance
        self.make_decision = make_decision
        self.terrain = terrain
        self.motion_evidence = motion_evidence
        self.perception_tick = perception_tick
        self.cadence_steps = tuple(cadence_steps)
        self.cadence_suffixes = tuple(
            [asdict(step) for step in self.cadence_steps[index:]] for index in range(len(self.cadence_steps))
        )
        self.worker = ScenarioWorker()
        self.decision = None
        self.heartbeat_lock = Lock()
        self.last_heartbeat_ms = None
        self.pending_events = {}
        self.processed_events = set()
        self.wind_estimator = WindEstimator(tailsitter=True)
        self.last_observed_boot_ms = None
        self.motion_boot_high_water_ms = 0
        self.last_event_index = None
        self.gimbal = None
        self.step = 0
        # Install route_gate.wrap() on the route executor link before flight.
        self.route_gate = RouteWriteGate()
        self.native = None
        self.hold = None
        self.native_failure = None
        self._seen_admission = None
        self.retask = AirborneRouteSlot()
        self._retask_entry_mode = None
        self._observed_detection_ms = None
        if getattr(motion_evidence, "retask_bounds", None) is not None:
            motion_evidence.stage_retask = self.retask.stage
            motion_evidence.installed_at = self.retask.installed_at

    @property
    def retask_end(self):
        return self.retask.end

    def retask_at(self, index):
        return self.retask.at(index)

    def retask_remaining_at(self, index):
        return self.retask.remaining_at(index)

    def check_retask_entry(self, index, *, gimbal_ready):
        """At first dispatch, prove the same suffix from fresh PX4 state or stop."""
        from multidetect.autonomy.x1_contingency_state import VTOL_STATES

        if not self.retask.starts_at(index):
            return
        reason = "retask_entry_changed"
        if not self.native_active and self.decision.installed_retask is not None:
            decision, admitted = self.decision.installed_retask
            boot_ms = self.clock()
            state = self.reader.latest_control_state()
            samples = {name: self._fresh_sample(name, state=state) or {}
                       for name in ("HEARTBEAT", "EXTENDED_SYS_STATE", "GPS_RAW_INT", "VFR_HUD")}
            heartbeat, vtol = samples["HEARTBEAT"], samples["EXTENDED_SYS_STATE"]
            base_mode = heartbeat.get("base_mode")
            mode = base_mode, heartbeat.get("custom_mode"), vtol.get("landed_state")
            vtol_state = VTOL_STATES.get(vtol.get("vtol_state"))
            if (type(base_mode) is int and base_mode & 128 and vtol.get("landed_state") == 2
                    and heartbeat.get("system_status") == 4 and type(mode[1]) is int
                    and mode == self._retask_entry_mode and vtol_state == admitted.vtol_state
                    and self.decision.revision == admitted.revision + 1):
                if getattr(self.motion_evidence, "corridor", None) is not None:
                    now_ns = time.monotonic_ns()
                    wind_samples = {name: row for name in ("WIND_COV", "ATTITUDE", "LOCAL_POSITION_NED",
                                                           "GLOBAL_POSITION_INT", "VFR_HUD", "SYS_STATUS",
                                                           "EXTENDED_SYS_STATE")
                                    if (row := self._fresh_sample(name, state=state, now_ns=now_ns)) is not None}
                    estimated = self.wind_estimator.update(wind_samples, now_ns=now_ns)
                    wind = WindReading.from_wind_cov(wind_samples.get("WIND_COV", {}), now_ns) or estimated
                    point, energy, remaining_time = self._motion_data(
                        index, boot_ms, state, now_ns=now_ns, corridor_wind=wind)
                else:
                    point, energy, remaining_time = self._motion_data(index, boot_ms, state)
                    now_ns = time.monotonic_ns()
                frame = replace(admitted, frame_id=now_ns, t_ms=now_ns // 1_000_000,
                                position_ne_amsl=point, remaining_energy_wh=energy,
                                remaining_time_s=remaining_time, airspeed_mps=self._airspeed_mps(samples),
                                agl_m=self._home_height_m(state=state), retask_gimbal_ready=gimbal_ready,
                                position_ok=self._position_ok(samples, state=state))
                executor = self.decision.executor
                try:
                    proof = self.motion_evidence.reprove_installed_retask(
                        decision, frame, self.retask.remaining_at(index))
                    reason = executor.envelope.veto(
                        decision, frame, proof, executor.package,
                        check_airborne_plan=executor.mission.check_airborne_plan)
                except X1PlanError as exc:
                    reason = exc.code
                except (ValueError, OSError, RuntimeError):
                    reason = "proof_or_envelope_error"
                if reason is None:
                    return
        self.record({"message_type": "collector_x1_retask_dispatch_blocked", "step_index": index,
                     "revision": self.retask.at(index).revision, "reason": reason,
                     "receive_monotonic_ns": time.monotonic_ns()})
        raise RuntimeError(f"X1 retask first dispatch blocked: {reason}")

    @property
    def native_active(self):
        """True once a native action claimed the route writer. It never reopens."""
        return self.route_gate.owner is not None

    def _recovery_exempt(self):
        native = self.native
        if native is not None and native.confirmed("return", "land-now"):
            return f"vehicle_under_native_{native.action}"
        return None

    def bind(self, docker, container, world, reader, connection, record, episode_id, *, clock):
        from collections import deque

        self.reader, self.clock, self.record = reader, clock, record
        # HEARTBEAT can resolve the system while pymavlink still targets all components.
        component = connection.target_component
        self.target_peer = (connection.target_system, PX4_AUTOPILOT_COMPONENT_ID if component == 0 else component)
        self.markers = deque()
        wind = wind_baseline(world) if any(f.kind == "wind_step" for f in self.faults) else None
        self.transport = SitlScenarioTransport.in_container(
            docker, container, wind=wind, battery_sample=self.battery_pct,
            clearance=lambda: self.clearance(self.step, self.reader.state.get("LOCAL_POSITION_NED")),
            link_lock=self.heartbeat_lock, recovery_exempt=self._recovery_exempt,
        )
        if self.make_decision is not None:
            from .x1_native_handoff import reader_ack_waiter
            from .x1_scenario_decision import GimbalIngress

            self.gimbal = GimbalIngress()
            extra = {}
            if getattr(connection, "mav", None) is not None:
                self.native = NativeHandoffPort(
                    connection, reader, self.route_gate, record, wait_ack=reader_ack_waiter(reader),
                    peer=self.target_peer,
                    local_from_wgs84=self.terrain.local_from_wgs84 if self.terrain is not None else None,
                )
                extra["native"] = self.native
            if getattr(self.motion_evidence, "retask_bounds", None) is not None:
                extra["replace_remaining"] = self.retask.replace_remaining
            self.decision = self.make_decision(self.gimbal, record, episode_id, self.target_peer, **extra)

    def prepare(self):
        self.transport.prepare(self.faults)
        assumed = self.motion_evidence.assumed_sim if self.motion_evidence is not None else None
        if assumed is not None:
            for parameter, expected in assumed["parameters"].items():
                if self.transport._get_param(parameter) != expected:
                    raise RuntimeError(f"ASSUMED_SIM prearm parameter readback mismatch for {parameter}")
        fw_rtl = getattr(self.motion_evidence, "fw_rtl", None)
        if fw_rtl is not None:
            self.motion_evidence.fw_rtl_ready = False
            commits, observed = [], {}
            error = None
            try:
                out = self.transport.command("/opt/px4-gazebo/bin/px4-ver", "all")
                commits = re.findall(r"(?m)^\s*PX4 git-hash:\s*([a-f0-9]{40})\s*$", out)
                if commits == [fw_rtl.px4_commit]:
                    for key in fw_rtl.params:
                        observed[key] = self.transport._get_param(key)
                    self.motion_evidence.fw_rtl_ready = all(
                        math.isclose(observed[k], v, rel_tol=0, abs_tol=1e-6) for k, v in fw_rtl.params.items()
                    )
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                error = str(exc)
            self.record({"message_type": "scenario_fw_rtl_readback",
                         "expected_px4_commit": fw_rtl.px4_commit,
                         "observed_px4_commits": commits, "observed_parameters": observed, "error": error,
                         "verified": self.motion_evidence.fw_rtl_ready})
            if not self.motion_evidence.fw_rtl_ready:
                raise RuntimeError("FW RTL candidate PX4 commit or parameter readback mismatch")

    def _fresh_sample(self, name, max_age_ms=1500, *, state=None, now_ns=None):
        from .streams import MavlinkReader

        if state is None:
            state = self.reader.latest_control_state()
        if now_ns is None:
            now_ns = time.monotonic_ns()
        received = state.received_monotonic_ns.get(name)
        message = state.get(name)
        if received is None or message is None or not 0 <= now_ns - received <= max_age_ms * 1_000_000:
            return None
        row = MavlinkReader._mapping(message)
        system, component = self.target_peer
        if row.get("_source_system") != system or (component and row.get("_source_component") != component):
            return None
        return {**row, "message_type": name, "receive_monotonic_ns": received}

    def battery_pct(self):
        row = self._fresh_sample("BATTERY_STATUS") or {}
        pct = row.get("battery_remaining")
        return pct if row.get("id") == 0 and type(pct) is int and 0 <= pct <= 100 else None

    def start(self):
        def record(row):
            self.record(row)
            if row["message_type"] == "scenario_injected":
                self.markers.append(row)
        self.worker.start(self.faults, self.clock, self.transport, record)

    def heartbeat(self, send, *args):
        with self.heartbeat_lock:
            if not self.transport.link_suppressed.is_set():
                send(*args)
                self.last_heartbeat_ms = time.monotonic_ns() // 1_000_000

    def check(self):
        self.worker.check()

    def tick(self, index, gimbal, *, lifecycle=None, step_boundary=False, retask_gimbal_ready=False,
             _boundary_waits=3):
        from multidetect.autonomy.x1_contingency_executor import RuntimeFrame
        from multidetect.autonomy.x1_contingency_state import VTOL_STATES

        from .x1_scenario_decision import MAVLINK_TYPES

        self.check()
        self.step = index
        self.retask.enter_tick(index if step_boundary else None)
        timeline = self.worker.timeline
        if timeline is None and (lifecycle != "takeoff" or not self.cadence_steps):
            return
        boot_ms = self.clock()
        # A pending Altair reply may become ready during a paused sim frame.
        if self.decision is None and boot_ms == self.last_observed_boot_ms:
            return
        self.last_observed_boot_ms = boot_ms
        state = self.reader.latest_control_state()
        now_ns = time.monotonic_ns()
        now = now_ns // 1_000_000
        wind_types = ("ATTITUDE", "LOCAL_POSITION_NED", "GLOBAL_POSITION_INT")
        samples = {name: row for name in (*MAVLINK_TYPES, *wind_types)
                   if (row := self._fresh_sample(name, state=state, now_ns=now_ns)) is not None}
        if self.gimbal is not None:
            self.gimbal.hold = gimbal
        estimated = self.wind_estimator.update(samples, now_ns=now_ns)
        wind = WindReading.from_wind_cov(samples.get("WIND_COV", {}), now_ns) or estimated
        heartbeat, vtol = samples.get("HEARTBEAT", {}), samples.get("EXTENDED_SYS_STATE", {})
        sys_status, base_mode = heartbeat.get("system_status"), heartbeat.get("base_mode")
        observations = []
        # A collector-side link witness measures silence of its outgoing GCS
        # heartbeat. PX4's native reception is a separate SITL witness.
        link_age = now - self.last_heartbeat_ms if self.last_heartbeat_ms is not None else None
        if link_age is not None and link_age >= 0:
            self._observe_link(observations, link_age)
        target = self.transport.target_observation(None)
        if target is not None:
            observations.append(("target", target, "tracker"))
        terrain = self.transport.terrain_observation() if any(f.kind == "terrain_conflict" for f in self.faults) else None
        if terrain is not None:
            observations.append(("terrain", terrain, "terrain"))
        retask_enabled = getattr(self.motion_evidence, "retask_bounds", None) is not None
        detection_ready = any(f.kind == "target_found" for f in self.faults)
        if retask_enabled:
            # Take the one synthetic camera sample at the serial boundary at the
            # end of the admitted FW leg, within the active fault window. Early/late
            # timeouts still leave the base plan alone. No Gazebo pose or target truth is read.
            position = self._motion_position(self._fresh_sample(
                "GLOBAL_POSITION_INT", max_age_ms=MOTION_SAMPLE_MAX_AGE_MS, state=state, now_ns=now_ns))
            detection_ready = (step_boundary and index == 2 and vtol.get("vtol_state") == 4 and position is not None and
                               math.dist(position[:2], self.motion_evidence.builder.outbound[:2]) <= 28)
            detection_ready |= self._observed_detection_ms is not None
        perception = self.transport.perception_report(now) if detection_ready else None
        events = (self._new_events(samples, observations, wind, perception=perception)
                  if timeline is not None else [])
        if perception is not None and not self._target_event_active(timeline, boot_ms):
            perception = None
        if perception is not None and perception["t_ms"] != self._observed_detection_ms:
            observations.append(("target", perception["target_status"], perception["source"]))
            self._observed_detection_ms = perception["t_ms"]
        if self.decision is not None:
            motion_index, installed_sha, installed_key = self._continue_motion_context(index, lifecycle)
            evidence_kwargs = {"installed_key": installed_key} if installed_key is not None else {}
            wind_kwargs = ({"corridor_wind": wind}
                           if getattr(self.motion_evidence, "corridor", None) is not None else {})
            hold_is_current = self._hold_events(now, events)
            point, remaining_energy_wh, remaining_time_s = self._motion_data(
                motion_index, boot_ms, state, now_ns=now_ns, **evidence_kwargs, **wind_kwargs)
            vtol_state = VTOL_STATES.get(vtol.get("vtol_state"))
            hold_kwargs = {**evidence_kwargs, "point": point} if installed_key is not None else {}
            holds = (self.motion_evidence.hold_sites(motion_index, vtol_state, **hold_kwargs)
                     if motion_index is not None and self.motion_evidence is not None
                     and hasattr(self.motion_evidence, "hold_sites") else {})
            estimate = perception["detection"]["position_estimate"] if perception is not None else None
            finding = None
            if estimate is not None and self.terrain is not None:
                finding = self.terrain.local_from_wgs84(estimate["lat_deg"], estimate["lon_deg"])
            frame = RuntimeFrame(
                frame_id=now_ns, t_ms=now, mission_id=self.decision.mission_id,
                revision=self.decision.revision, event_id=0,
                px4_failsafe=True if sys_status in {5, 6, 8} else False if sys_status == 4 else None,
                armed_in_air=(type(base_mode) is int and bool(base_mode & 128) and vtol.get("landed_state") == 2),
                vtol_state=vtol_state, agl_m=self._home_height_m(state=state, now_ns=now_ns),
                airspeed_mps=self._airspeed_mps(samples), position_ok=self._position_ok(samples, state=state, now_ns=now_ns),
                hold_sites=holds,
                gimbal_available=gimbal is not None and not gimbal.awaiting_readback,
                position_ne_amsl=point, active_motion_index=motion_index,
                remaining_energy_wh=remaining_energy_wh, remaining_time_s=remaining_time_s,
                terrain_conflict=terrain.get("conflict") if terrain is not None else None,
                retask_boundary=step_boundary,
                retask_finding_ne=finding,
                retask_finding_error_m=estimate["error_bound_m"] if estimate is not None else None,
                retask_gimbal_ready=retask_gimbal_ready,
                installed_plan_sha256=installed_sha,
                native_hold_active=hold_is_current,
            )
            event_current = hold_is_current or (
                timeline is not None and self.last_event_index is not None and boot_ms is not None
                and boot_ms - timeline.origin < self.faults[self.last_event_index].end_ms)
            # The selected wind is current or unknown, not a generic cached observation.
            mav_rows = (samples[name] for name in MAVLINK_TYPES if name != "WIND_COV" and name in samples)
            extra = {"step_boundary": step_boundary} if retask_enabled else {}
            if perception is not None:
                extra["perception"] = perception
            cadence = {}
            if self.cadence_steps:
                cadence = {
                    "mission_progress": self._mission_progress(index, lifecycle, state, frame, now_ns=now_ns),
                    "mode": (base_mode, heartbeat.get("custom_mode"), vtol.get("landed_state")),
                    "cadence_active": frame.px4_failsafe is False and frame.armed_in_air is True,
                }
            if self.perception_tick is not None:
                self.perception_tick(self, index, samples, gimbal, boot_ms, frame.position_ok, perception=perception)
            self.decision.tick(frame, mav_rows=mav_rows, observations=observations, events=events,
                               event_current=event_current, wind=wind, **extra, **cadence)
            if self.retask.starts_at(index) and self._retask_entry_mode is None:
                self._retask_entry_mode = base_mode, heartbeat.get("custom_mode"), vtol.get("landed_state")
            self._after_decision()
            if (_boundary_waits and step_boundary and index == 2 and perception is not None and event_current
                    and retask_enabled and self.retask_end is None and self.decision.future is not None
                    and frame.px4_failsafe is False and frame.armed_in_air is True):
                # A fast reply may be scheduled here. Budget one turn to drain a
                # preempted cadence call, one for the event, and one for a recorded
                # cadence follow-up, with fresh evidence.
                done, _ = wait((self.decision.future,), timeout=0.05)
                if done:
                    self.tick(index, gimbal, step_boundary=True, retask_gimbal_ready=retask_gimbal_ready,
                              _boundary_waits=_boundary_waits - 1)

    def _target_event_active(self, timeline, boot_ms):
        return (timeline is not None and boot_ms is not None
                and any(i in self.processed_events and fault.kind == "target_found"
                        and fault.at_ms <= boot_ms - timeline.origin < fault.end_ms
                        for i, fault in enumerate(self.faults)))

    def _mission_progress(self, index, lifecycle, state, frame, *, now_ns=None):
        from multidetect.autonomy.x1_contingency_state import mission_navigation

        # The collector executes the command-bearing steps between its takeoff and landing.
        if lifecycle == "takeoff":
            step_index = 0
        elif lifecycle == "approach":
            step_index = len(self.cadence_steps) - 1
        else:
            step_index = index + 1
        installed = self.retask.remaining_at(index) if lifecycle is None else ()
        if installed:
            remaining = installed
            step = installed[0]
            suffix_identity = [step.source_step_id for step in installed]
        else:
            step_index = min(step_index, len(self.cadence_steps) - 1)
            remaining = self.cadence_steps[step_index:]
            step = remaining[0]
            suffix_identity = self.cadence_suffixes[step_index]
        hold = self.hold
        native_action = self.native.action if self.native is not None else None
        phase = lifecycle
        if phase is None:
            if native_action in {"return", "land-now"}:
                phase = "approach"
            elif hold is not None:
                phase = "hold"
            elif (step.primitive in {"fw_transition", "back_transition"}
                  or frame.vtol_state in {"front_transition", "back_transition"}):
                phase = "transition"
            else:
                phase = "cruise" if frame.vtol_state == "fw" else "approach"
        waypoint = next((item.command.target_ned for item in remaining
                         if item.command is not None and item.command.target_ned is not None), None)
        if lifecycle == "approach":
            waypoint = (0.0, 0.0)  # Landing is at the end of the fixed MC return to home.
        local = self._fresh_sample("LOCAL_POSITION_NED", max_age_ms=MOTION_SAMPLE_MAX_AGE_MS, state=state, now_ns=now_ns)
        distance, home_distance, eta = mission_navigation(local, waypoint)
        return {"step_index": step_index, "step_id": step.source_step_id, "primitive": step.primitive,
                "phase": phase, "remaining_plan_sha256": canonical_sha256({
                    "compiled_plan_sha256": self.decision.plan_digest, "steps": suffix_identity}),
                "next_waypoint_distance_m": distance, "home_distance_m": home_distance, "eta_s": eta,
                "remaining_energy_wh": frame.remaining_energy_wh, "remaining_time_s": frame.remaining_time_s,
                "hold": {"active": hold is not None, "ref": hold["hold_ref"] if hold is not None else None,
                         "remaining_s": max(0, hold["deadline_ms"] - frame.t_ms) / 1000 if hold else None,
                         "native_action": native_action,
                         "native_status": self.native.status if self.native is not None else None}}

    def _hold_events(self, now, events):
        """Raise one revalidation event before an admitted hold's residence bound expires."""
        hold = self.hold
        if hold is None:
            return False
        if now >= hold["deadline_ms"]:
            self.hold = None
            self.native_failure = "hold_residence_expired"
            self.record({"message_type": "scenario_native_hold_expired", "hold_ref": hold["hold_ref"],
                         "deadline_ms": hold["deadline_ms"], "receive_monotonic_ns": time.monotonic_ns()})
            return False
        if not hold["revalidation_requested"] and now >= hold["deadline_ms"] - HOLD_REVALIDATION_LEAD_MS:
            hold["revalidation_requested"] = True
            events.append(("hold_revalidation", "executor"))
            self.record({"message_type": "scenario_native_hold_revalidation", "hold_ref": hold["hold_ref"],
                         "deadline_ms": hold["deadline_ms"], "receive_monotonic_ns": time.monotonic_ns()})
        return True

    def _after_decision(self):
        """Track the residence bound of a dispatched hold and advance native readback."""
        if self.native is not None:
            self.native.poll()
        admitted = getattr(self.decision, "last_admission", None)
        if admitted is None or admitted is self._seen_admission:
            return
        self._seen_admission = admitted
        admission, proof, admitted_ms, wire = admitted
        if admission.status not in _DISPATCHED:
            return
        if admission.action == "loiter" and proof is not None and type(proof.required_time_s) in (int, float):
            # The executor decoded and admitted this exact wire; it names the hold.
            hold_ref = json.loads(wire)["params"]["hold_ref"]
            residence = proof.required_time_s
            if getattr(self.motion_evidence, "corridor", None) is not None:
                residence = self.motion_evidence.hold_residence_s(hold_ref)
                if residence is None:
                    self.native_failure = "hold_residence_unverified"
                    return
            self.hold = {"hold_ref": hold_ref,
                         "deadline_ms": admitted_ms + int(residence * 1000),
                         "revalidation_requested": False}
        elif admission.action in {"return", "land-now"}:
            self.hold = None

    def finish_native(self, index, *, check=lambda: None, timeout_s=NATIVE_LANDING_TIMEOUT_S,
                      sleep=time.sleep, monotonic=time.monotonic):
        """After a native claim: keep readback and decisions running until PX4 lands.

        Fails closed. An unconfirmed takeover or an expired hold is reported,
        and the vehicle is left to PX4 (its own failsafe is the floor).
        """
        if not self.native_active:
            raise RuntimeError("X1 native finish requires a claimed route writer")
        started = monotonic()
        while True:
            check()
            self.tick(index, None)
            native = self.native
            if native is None or native.pending is None:
                raise RuntimeError("X1 native handoff claimed the route writer but dispatched no command")
            if native.status == "unconfirmed":
                raise RuntimeError(f"X1 native handoff unconfirmed: {native.action}; PX4 failsafe remains the floor")
            if self.native_failure is not None:
                raise RuntimeError(f"X1 native handoff failed closed: {self.native_failure}")
            if native.confirmed("return", "land-now"):
                landed = native.landed()
                if landed is not None:
                    return {"status": "landed", "method": f"native_{native.action}",
                            "native_handoff": [dict(row) for row in native.history],
                            "elapsed_s": monotonic() - started, **landed}
            if monotonic() - started > timeout_s:
                raise TimeoutError(f"X1 native {native.action} did not land within {timeout_s:.0f} s")
            sleep(0.1)

    def _home_height_m(self, *, state=None, now_ns=None):
        """Height above the SITL home point (flat launch pad), from fresh GLOBAL_POSITION_INT."""
        alt = (self._fresh_sample("GLOBAL_POSITION_INT", state=state, now_ns=now_ns) or {}).get("relative_alt")
        return alt / 1000.0 if type(alt) is int else None

    def _continue_motion_context(self, index, lifecycle):
        if self.retask_end is None:
            return index if lifecycle is None else None, None, None
        sha = self.decision.plan_digest
        route = self.retask.installed_at(index) if lifecycle is None else None
        if route is None or route.revision != self.decision.revision or route.plan_sha256 != sha:
            return None, sha, None
        return index, sha, ("installed-continue", route.revision, sha, index)

    def _motion_data(self, index, boot_ms, state, *, now_ns=None, installed_key=None, corridor_wind=None):
        """Read motion evidence from the same trusted snapshot used for this tick."""
        age = MOTION_SAMPLE_MAX_AGE_MS
        global_position = self._fresh_sample("GLOBAL_POSITION_INT", max_age_ms=age, state=state, now_ns=now_ns)
        point = self._motion_position(global_position)
        if self.motion_evidence is None:
            return point, None, None
        local = self._fresh_sample("LOCAL_POSITION_NED", max_age_ms=age, state=state, now_ns=now_ns)
        local_boot = (local or {}).get("time_boot_ms")
        global_boot = (global_position or {}).get("time_boot_ms")
        timeline = self.worker.timeline
        clock_floor = timeline.origin if timeline is not None else 1
        clock_valid = (type(local_boot) is int and type(boot_ms) is int
                       and 0 < clock_floor <= min(boot_ms, local_boot)
                       and self.motion_boot_high_water_ms <= min(boot_ms, local_boot)
                       and max(boot_ms, local_boot) < 2**32 and abs(local_boot - boot_ms) <= age)
        remaining_time = None
        if clock_valid:
            # The reader can advance between clock() and taking its snapshot.
            self.motion_boot_high_water_ms = min(local_boot, boot_ms)
            remaining_time = self.motion_evidence.remaining_time_s(max(local_boot, boot_ms))
        if (index is not None and clock_valid and type(global_boot) is int and 0 < global_boot < 2**32
                and abs(local_boot - global_boot) <= age):
            entry = {"installed_key": installed_key} if installed_key is not None else {}
            if (getattr(self.motion_evidence, "fw_rtl_ready", False) and self.hold is not None
                    and self.native is not None and self.native.confirmed("loiter")):
                heartbeat = self._fresh_sample("HEARTBEAT", state=state, now_ns=now_ns) or {}
                if px4_mode(heartbeat.get("custom_mode")) == AUTO_LOITER:
                    home = self._fresh_sample("HOME_POSITION", max_age_ms=3_000, state=state, now_ns=now_ns) or {}
                    home_z, relative_z = home.get("altitude"), global_position.get("relative_alt")
                    if (type(home_z) is int and type(relative_z) is int
                            and type(global_position.get("alt")) is int
                            and abs(global_position["alt"] - relative_z - home_z) <= 1_000):
                        entry.update(confirmed_hold_ref=self.hold["hold_ref"], observed_home=self._motion_position({
                            "lat": home.get("latitude"), "lon": home.get("longitude"), "alt": home_z,
                        }))
            if getattr(self.motion_evidence, "corridor", None) is not None:
                entry["corridor_wind"] = corridor_wind
            point = self.motion_evidence.check_entry(index, point, local, **entry)
        else:
            point = None
        battery = self._fresh_sample("BATTERY_STATUS", max_age_ms=age, state=state, now_ns=now_ns)
        if self.motion_evidence.assumed_sim is not None:
            # BATTERY_STATUS has no source time; the reader stamps it with its wire clock.
            wire_boot = state.vehicle_time_boot_ms
            stamp = state.received_vehicle_ms.get("BATTERY_STATUS")
            if not clock_valid or type(wire_boot) is not int or not 0 < wire_boot < 2**32 or type(stamp) is not int:
                battery = None
            else:
                route_boot = max(local_boot, boot_ms)
                # An out-of-order wire stamp must not make the sample younger.
                if abs(wire_boot - route_boot) > age or not 0 <= max(wire_boot, route_boot) - stamp <= age:
                    battery = None
        return point, self.motion_evidence.remaining_energy_wh(battery), remaining_time

    def _motion_position(self, row):
        if self.terrain is None or row is None:
            return None
        lat, lon, alt = (row.get(key) for key in ("lat", "lon", "alt"))
        if not all(type(value) is int for value in (lat, lon, alt)):
            return None
        if not (-900_000_000 <= lat <= 900_000_000 and -1_800_000_000 <= lon <= 1_800_000_000
                and -500_000 <= alt <= 10_000_000):
            return None
        n, e = self.terrain.local_from_wgs84(lat / 1e7, lon / 1e7)
        return n, e, alt / 1000.0

    @staticmethod
    def _airspeed_mps(samples):
        speed = (samples.get("VFR_HUD") or {}).get("airspeed")
        return float(speed) if type(speed) in (int, float) and math.isfinite(speed) else None

    def _position_ok(self, samples, *, state=None, now_ns=None):
        """True only with a 3D GPS fix and a fresh local position; never inferred otherwise."""
        fix = (samples.get("GPS_RAW_INT") or {}).get("fix_type")
        if self._fresh_sample("LOCAL_POSITION_NED", state=state, now_ns=now_ns) is None or type(fix) is not int:
            return None
        return fix >= 3

    def _observe_link(self, observations, age):
        down = self.transport.link_suppressed.is_set() and age >= 1000
        observations.append(("link", {"gcs": "down" if down else "up", "heartbeat_age_ms": age}, "link_monitor"))

    def _new_events(self, samples, observations, wind, *, perception=None):
        battery = (samples.get("BATTERY_STATUS") or {}).get("battery_remaining")
        observed = {group: values for group, values, _ in observations}
        while self.markers:
            marker = self.markers.popleft()
            self.pending_events[marker["fault_index"]] = marker["receive_monotonic_ns"]
        timed_events = []
        clock = self.clock()
        if clock is None:
            return timed_events
        stamp = clock - self.worker.timeline.origin
        for index in sorted(self.pending_events.keys() - self.processed_events):
            fault = self.faults[index]
            if stamp >= fault.end_ms:
                self.record({"message_type": "scenario_unobserved", "fault": fault.kind, "fault_index": index,
                             "observed_boot_ms": clock, "receive_monotonic_ns": time.monotonic_ns()})
                self.processed_events.add(index)
                continue
            source = None
            extra = {}
            if fault.kind == "link_loss" and observed.get("link", {}).get("gcs") == "down":
                source = "link_monitor"
            elif (fault.kind == "target_found" and perception is not None
                  and (samples.get("EXTENDED_SYS_STATE") or {}).get("vtol_state") == 4):
                source = perception["source"]
            elif fault.kind == "target_lost" and observed.get("target", {}).get("visible") is False:
                source = "tracker"
            elif fault.kind == "terrain_conflict" and observed.get("terrain", {}).get("conflict") is True:
                source = "terrain"
            elif fault.kind == "battery_drop" and type(battery) is int and 0 <= battery <= fault.values["pct"] + 2:
                source = "mavlink"
            elif (fault.kind == "wind_step" and wind is not None
                  and wind.support_since_ns >= self.pending_events[index]):
                # The SDF baseline is configuration, not a measurement.
                _, (east, north, up) = self.transport.wind
                wind_n, wind_e, wind_d = wind.ned_mps
                change = (math.hypot(wind_n - north, wind_e - east) if wind_d is None
                          else math.dist(wind.ned_mps, (north, east, -up)))
                if change >= 2.0:
                    source = "mavlink"
                    extra = {"wind_method": wind.method, "wind_confidence": wind.confidence,
                             "wind_age_ms": wind.age_ms(time.monotonic_ns() // 1_000_000)}
            if source is not None:
                timed_events.append((fault.kind, source))
                self.last_event_index = index
                value = {"wind_step": wind.ned_mps if wind is not None else None,
                         "battery_drop": battery, "link_loss": observed.get("link"),
                         "target_found": perception, "target_lost": observed.get("target"), "terrain_conflict": observed.get("terrain")}[fault.kind]
                self.record({"message_type": "scenario_observed", "fault": fault.kind, "fault_index": index,
                             "observed_boot_ms": clock, "observed_value": value, "source": source,
                             "receive_monotonic_ns": time.monotonic_ns(), **extra})
                self.processed_events.add(index)
        return timed_events

    def close(self, *, completed=False):
        try:
            self.worker.close(completed=completed)
        finally:
            if self.decision is not None:
                self.decision.close()
                self.decision = None
