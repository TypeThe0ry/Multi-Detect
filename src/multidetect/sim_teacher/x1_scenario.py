"""Opt-in fault windows for the disposable X1 SITL collector."""
from __future__ import annotations

import hashlib
import math
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from threading import Event, Lock, Thread

from multidetect.autonomy.x1_contingency_replay import Fault, load_faults

SUPPORTED = frozenset({"wind_step", "battery_drop", "link_loss", "target_lost", "terrain_conflict"})
MECHANISM = {
    "wind_step": "gazebo_wind_cmd", "battery_drop": "px4_battery_simulator",
    "link_loss": "collector_gcs_heartbeat_gate", "target_lost": "target_observation_mask",
    "terrain_conflict": "forward_dem_offset",
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

    def _mark(self, fault, index, phase, scheduled, **details):
        self.record({"message_type": f"scenario_{phase}", "fault": fault.kind, "fault_index": index,
                     "mechanism": MECHANISM[fault.kind], "values": dict(fault.values),
                     "scheduled_offset_ms": scheduled, "origin_boot_ms": self.origin,
                     "observed_boot_ms": self.clock(), "receive_monotonic_ns": time.monotonic_ns(), **details})

    def tick(self):
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
                    details = self.transport.apply(fault)
                    applied_stamp = self.clock()
                    if applied_stamp is None or applied_stamp < stamp or applied_stamp - self.origin >= fault.end_ms:
                        self._mark(fault, index, "missed", offset, applied=dict(details))
                        raise RuntimeError(f"X1 scenario apply exceeded its window: {fault.kind}")
                    self._mark(fault, index, "injected", offset, applied=dict(details))
                else:
                    self._restore(index, fault, "window_end")
            except Exception as exc:
                self._mark(fault, index, "error", offset, error=str(exc))
                raise

    def _restore(self, index, fault, reason):
        details = self.transport.restore(fault)
        del self.active[index]
        self._mark(fault, index, "restored", fault.end_ms, reason=reason, applied=dict(details))

    def close(self, *, completed=False):
        errors = []
        for index, fault in reversed(tuple(self.active.items())):
            try:
                self._restore(index, fault, "episode_exit")
            except Exception as exc:
                self._mark(fault, index, "restore_error", fault.end_ms, error=str(exc))
                errors.append(f"{fault.kind}: {exc}")
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

    def __init__(self, command, *, wind=None, battery_sample=None, clearance=None, link_lock=None):
        self.command = command
        self.wind = wind
        self.battery_sample = battery_sample
        self.clearance = clearance
        self.link_lock = link_lock if link_lock is not None else Lock()
        self.link_suppressed = Event()
        self.target_masked = Event()
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

    def apply(self, fault):
        kind, values = fault.kind, fault.values
        if kind == "wind_step":
            self.wind_written = True
            return self._wind((values["east_mps"], values["north_mps"], -values["down_mps"]))
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
            try:
                self._set_param("SIM_BAT_MIN_PCT", self.battery_prior)
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    sample = self.battery_sample()
                    if sample is not None and sample >= self.battery_prior - 1:
                        break
                    time.sleep(0.05)
                else:
                    raise RuntimeError("PX4 battery percentage recovery was not observed")
            finally:
                self._set_param("SIM_BAT_MIN_PCT", floor)
            self.battery_prior = None
            return {"floor_pct": floor, "drain_s": drain, "observed_recovery_pct": sample}
        if kind == "link_loss":
            with self.link_lock:
                self.link_suppressed.clear()
            return {"gcs_heartbeat_suppressed": False}
        if kind == "target_lost":
            self.target_masked.clear()
            return {"target_observations_masked": False}
        if kind == "terrain_conflict":
            self.terrain_offset = None
            return {"elevation_offset_m": 0}
        return {}

    def target_observation(self, observation):
        return {"visible": False, "track_state": "lost"} if self.target_masked.is_set() else observation

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


def wind_server_config(source, output):
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
    config.write(output, encoding="utf-8", xml_declaration=True)


class CollectorScenario:
    """Bridge the owned episode clock, telemetry and original compiled route to the injector."""

    def __init__(self, faults, *, clearance, make_decision=None):
        self.faults = faults
        self.clearance = clearance
        self.make_decision = make_decision
        self.worker = ScenarioWorker()
        self.decision = None
        self.heartbeat_lock = Lock()
        self.last_heartbeat_ms = None
        self.pending_events = set()
        self.processed_events = set()
        self.last_observed_boot_ms = None
        self.last_event_index = None
        self.gimbal = None
        self.step = 0

    def bind(self, docker, container, world, reader, connection, record, episode_id, *, clock):
        from collections import deque

        self.reader, self.clock, self.record = reader, clock, record
        self.target_peer = connection.target_system, connection.target_component
        self.markers = deque()
        wind = wind_baseline(world) if any(f.kind == "wind_step" for f in self.faults) else None
        self.transport = SitlScenarioTransport.in_container(
            docker, container, wind=wind, battery_sample=self.battery_pct,
            clearance=lambda: self.clearance(self.step, self.reader.state.get("LOCAL_POSITION_NED")),
            link_lock=self.heartbeat_lock,
        )
        if self.make_decision is not None:
            from .x1_scenario_decision import GimbalIngress

            self.gimbal = GimbalIngress()
            self.decision = self.make_decision(self.gimbal, record, episode_id, self.target_peer)

    def prepare(self):
        self.transport.prepare(self.faults)

    def _fresh_sample(self, name, max_age_ms=1500):
        from .streams import MavlinkReader

        state = self.reader.state
        received = state.received_monotonic_ns.get(name)
        message = state.get(name)
        if received is None or message is None or not 0 <= time.monotonic_ns() - received <= max_age_ms * 1_000_000:
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

    def tick(self, index, gimbal):
        from multidetect.autonomy.x1_contingency_executor import RuntimeFrame
        from multidetect.autonomy.x1_contingency_state import VTOL_STATES
        from .x1_scenario_decision import MAVLINK_TYPES

        self.check()
        self.step = index
        if self.worker.timeline is None:
            return
        boot_ms = self.clock()
        # A pending Altair reply may become ready during a paused sim frame.
        if self.decision is None and boot_ms == self.last_observed_boot_ms:
            return
        self.last_observed_boot_ms = boot_ms
        samples = {name: row for name in MAVLINK_TYPES if (row := self._fresh_sample(name)) is not None}
        if self.gimbal is not None:
            self.gimbal.hold = gimbal
        now = time.monotonic_ns() // 1_000_000
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
        events = self._new_events(samples, observations)
        if self.decision is not None:
            frame = RuntimeFrame(
                frame_id=self.reader.state.count, t_ms=now, mission_id=self.decision.mission_id,
                revision=self.decision.revision, event_id=0,
                px4_failsafe=True if sys_status in {5, 6, 8} else False if sys_status == 4 else None,
                armed_in_air=(type(base_mode) is int and bool(base_mode & 128) and vtol.get("landed_state") == 2),
                vtol_state=VTOL_STATES.get(vtol.get("vtol_state")), agl_m=None, airspeed_mps=None,
                position_ok=None, hold_sites={},
                gimbal_available=gimbal is not None and not gimbal.awaiting_readback,
            )
            event_current = (self.last_event_index is not None and boot_ms is not None
                             and boot_ms - self.worker.timeline.origin < self.faults[self.last_event_index].end_ms)
            self.decision.tick(frame, mav_rows=samples.values(), observations=observations, events=events,
                               event_current=event_current)

    def _observe_link(self, observations, age):
        down = self.transport.link_suppressed.is_set() and age >= 1000
        observations.append(("link", {"gcs": "down" if down else "up", "heartbeat_age_ms": age}, "link_monitor"))

    def _new_events(self, samples, observations):
        wind = samples.get("WIND_COV", {})
        wind_vector = tuple(wind.get(field) for field in ("wind_x", "wind_y", "wind_z"))
        battery = (samples.get("BATTERY_STATUS") or {}).get("battery_remaining")
        observed = {group: values for group, values, _ in observations}
        while self.markers:
            self.pending_events.add(self.markers.popleft()["fault_index"])
        timed_events = []
        clock = self.clock()
        if clock is None:
            return timed_events
        stamp = clock - self.worker.timeline.origin
        for index in sorted(self.pending_events - self.processed_events):
            fault = self.faults[index]
            if stamp >= fault.end_ms:
                self.record({"message_type": "scenario_unobserved", "fault": fault.kind, "fault_index": index,
                             "observed_boot_ms": clock, "receive_monotonic_ns": time.monotonic_ns()})
                self.processed_events.add(index)
                continue
            source = None
            if fault.kind == "link_loss" and observed.get("link", {}).get("gcs") == "down":
                source = "link_monitor"
            elif fault.kind == "target_lost" and observed.get("target", {}).get("visible") is False:
                source = "tracker"
            elif fault.kind == "terrain_conflict" and observed.get("terrain", {}).get("conflict") is True:
                source = "terrain"
            elif fault.kind == "battery_drop" and type(battery) is int and 0 <= battery <= fault.values["pct"] + 2:
                source = "mavlink"
            elif fault.kind == "wind_step" and all(type(x) in (int, float) and math.isfinite(x) for x in wind_vector):
                _, (east, north, up) = self.transport.wind
                if math.dist(wind_vector, (north, east, -up)) >= 2.0:
                    source = "mavlink"
            if source is not None:
                timed_events.append((fault.kind, source))
                self.last_event_index = index
                value = {"wind_step": wind_vector, "battery_drop": battery, "link_loss": observed.get("link"),
                         "target_lost": observed.get("target"), "terrain_conflict": observed.get("terrain")}[fault.kind]
                self.record({"message_type": "scenario_observed", "fault": fault.kind, "fault_index": index,
                             "observed_boot_ms": clock, "observed_value": value, "source": source,
                             "receive_monotonic_ns": time.monotonic_ns()})
                self.processed_events.add(index)
        return timed_events

    def close(self, *, completed=False):
        try:
            self.worker.close(completed=completed)
        finally:
            if self.decision is not None:
                self.decision.close()
                self.decision = None
