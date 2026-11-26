#!/usr/bin/env python3
"""Offline, fail-closed analysis of the isolated Iowa FW RTL probe matrix."""
from __future__ import annotations

import argparse
import gzip
import json
import struct
import math
import statistics
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from multidetect.autonomy.airframe_profile import load_airframe_profile
from multidetect.autonomy.serialization import canonical_sha256
from multidetect.autonomy.x1_motion_proof import LandingPad
from multidetect.sim_teacher.x1_fw_native_rtl import FwRtlBounds, _PHASES, _POSITIVE, _REQUIRED
from multidetect.terrain_dem import load_local_dem_grid
from scripts.analyze_px4_ulog import _parse_internal
from scripts.measure_x1_fw_rtl import (
    BASIS, CAPACITY_WH, IMAGE, MIN_RUNS, PX4_RTL_COMMIT, Cell, demo, digest, json_file, matrix, require_sim,
)

# PX4 53afcc82 rtl_direct.h / rtl_direct.cpp: LOITER_DOWN and LOITER_HOLD
# are one proof leg. CLIMBING is optional; AVOID_GEOFENCE is not admitted.
STATE_PHASE = {0: 0, 2: 1, 3: 2, 4: 2, 5: 3, 6: 4, 7: 5, 8: 6}
FLOOR_OPEN = {
    "fw_tracking_m": 1, "fw_vertical_m": 1, "fw_acceptance_m": 1, "move_to_land_vertical_m": 1,
    "transition_vertical_m": 1, "mc_tracking_m": 1, "mc_vertical_m": 1, "landing_tracking_m": 1,
    "landing_vertical_m": 1, "max_ground_speed_mps": 0.5, "max_bank_deg": 0.5,
    "max_climb_mps": 0.15, "max_descent_mps": 0.15, "max_acceleration_mps2": 0.15,
    "max_angular_rate_dps": 2, "fw_peak_power_w": 10, "hover_peak_power_w": 10,
}
PAD_RADIUS_M = 16
PAD_ERROR_M = 13
DEM_ERROR_M = 12
META = {"basis": BASIS, "mode": "sim", "image_id": IMAGE, "px4_commit": PX4_RTL_COMMIT}
UNVERIFIED = [
    "Grid interpolation, untested heading/wind directions, gust timing, SOC and sample-to-send latency.",
    "The 4-6 m/s hold ramp is not Case C's 8 N/-4 E m/s command/window or target_found/cadence.",
    "Climb fallback with a different orbit, alternate/passing-plane triggers and routes not seen in the logs.",
    "FW_R_LIM, navigator acceptance and MC controller settings are not trajectory or timing bounds.",
    "SOC * 72 Wh estimates synthetic depletion only; it does not identify instantaneous propulsion power.",
    "Independent site obstacles/geofence, DEM uncertainty, geometry admissibility and reserve-time/energy proof.",
    "Between-sample extrema, peak-power instrument bandwidth/calibration, and any real-aircraft applicability.",
]


def need(condition, message):
    if not condition:
        raise ValueError(message)


def one(rows, predicate, name):
    matches = [row for row in rows if predicate(row)]
    need(len(matches) == 1, f"expected exactly one {name}, got {len(matches)}")
    return matches[0]


def number(value):
    need(type(value) in (int, float) and math.isfinite(value), "missing or nonfinite telemetry")
    return value


def read_mavlink(path):
    # Collector JSONL can contain NaN in unused MAVLink fields.
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def mav_witness(rows, cell, meta):
    def marker(name):
        return one(rows, lambda r: r.get("message_type") == name, name)

    arming = marker("scenario_fw_rtl_readback")
    post = marker("fw_rtl_probe_disarmed")
    params = arming["observed_parameters"]
    need(arming.get("basis") == BASIS and all(r.get("verified") is True
         and r.get("observed_px4_commits") == [PX4_RTL_COMMIT]
         and r.get("observed_parameters") == params for r in (arming, post)), "prearm/post-disarm readback mismatch")
    need(set(params) == {*_REQUIRED, *_POSITIVE, "SIM_BAT_DRAIN", "SIM_BAT_MIN_PCT",
                         "SIM_BAT1_DRAIN", "SIM_BAT1_MIN_PCT"}
         and all(abs(number(params[k]) - v) <= 1e-6 for k, v in _REQUIRED.items())
         and all(number(params[k]) > 0 for k in _POSITIVE)
         and [params[k] for k in ("SIM_BAT_DRAIN", "SIM_BAT_MIN_PCT", "SIM_BAT1_DRAIN", "SIM_BAT1_MIN_PCT")]
         == [1800, 50, cell.drain_s, 50], "wrong native or battery parameters")
    for action, command, submode in (("loiter", 34, 3), ("return", 20, 5)):
        sent = one(rows, lambda r: r.get("message_type") == "scenario_native_command"
                   and r.get("action") == action, f"{action} dispatch")
        confirmed = one(rows, lambda r: r.get("message_type") == "scenario_native_handoff"
                        and r.get("action") == action, f"{action} confirmation")
        need(sent["command"] == command and sent["ack_result"] == 0 and confirmed["status"] == "confirmed"
             and confirmed["observed_mode"] == [4, submode] and confirmed["command"] == command,
             f"{action} did not reach confirmed native mode")
        if action == "return":
            need(sent["params"] == [0] * 7 and sent["mode_before"] == [4, 3], "RTL was rewritten or not from Hold")
        else:
            target = meta["hold"]
            need(sent["mode_before"] == [6, 0] and sent["params"] == [target["radius_m"], target["speed_mps"],
                 0, 0, *target["lat_lon"], target["centre"][2]], "orbit target differs from probe plan")
    handoffs = [r for r in rows if r.get("message_type") == "scenario_native_command"]
    need([r["action"] for r in handoffs] == ["loiter", "return"], "unexpected native action")
    marker("fw_rtl_probe_hold_request")
    hold = marker("fw_rtl_probe_hold_confirmed")
    dispatch = marker("fw_rtl_probe_dispatch")
    offset = number(dispatch["clock_ms"]) - number(hold["boot_ms"])
    need(dispatch["basis"] == BASIS and dispatch["motion_proof"] is False
         and abs(number(dispatch["boot_ms"]) - dispatch["clock_ms"]) <= 500
         and hold["boot_ms"] == dispatch["hold_boot_ms"] and 0 <= offset - 1000 * cell.offset_s <= 500,
         "dispatch outside probe clock contract")
    need(marker("collector_landing_complete")["disarmed"] is True, "collector did not observe disarm")
    need(not any(r.get("message_type") in ("scenario_error", "scenario_missed", "scenario_restore_error") for r in rows),
         "scenario transport reported an error")
    publishes = [r for r in rows if r.get("message_type") == "fw_rtl_probe_wind_publish"]
    if cell.wind_mps:
        bearing = math.radians(meta["inputs"]["wind_toward_deg"])
        enu = (cell.wind_mps * math.sin(bearing), cell.wind_mps * math.cos(bearing), 0)
        ramp = [r for r in publishes if hold["boot_ms"] <= number(r["boot_ms"]) <= dispatch["clock_ms"]]
        fractions = [number(r["fraction"]) for r in ramp]
        need(len(ramp) >= 3 and fractions[0] == 0 and fractions[-1] == 1
             and all(a < b for a, b in zip(fractions, fractions[1:]))
             and all(r["readback"] == "publish_only"
                     and math.dist(r["enu_mps"], [f * x for x in enu]) <= 0.02
                     for r, f in zip(ramp, fractions, strict=True)), "missing or incorrect Gazebo wind ramp publishes")
        restored = marker("scenario_restored")
        need(restored["fault"] == "wind_step" and restored["reason"] == "episode_exit"
             and math.hypot(*restored["applied"]["enu_mps"]) <= 0.01, "missing baseline wind restore publish")
    else:
        need(not publishes, "unexpected wind publish in calm cell")
    return params, dispatch, post


class Topic:
    def __init__(self, topics, name, *, battery=False):
        self.name = name
        self.rows = [r for r in topics.get(name, ()) if not battery or r.get("id") == 1]
        self.stamps = [number(r["timestamp"]) for r in self.rows]
        need(all(b > a for a, b in zip(self.stamps, self.stamps[1:])), f"non-monotonic {name}")

    def window(self, start, end):
        return self.rows[bisect_left(self.stamps, start):bisect_right(self.stamps, end)]

    def coverage(self, start, end, gap):
        rows = self.window(start, end)
        stamps = [start, *(r["timestamp"] for r in rows), end]
        need(rows and max(b - a for a, b in zip(stamps, stamps[1:])) <= gap, f"telemetry gap: {self.name}")
        return rows

    def near(self, stamp, gap):
        rows = self.window(stamp - gap, stamp + gap)
        need(rows, f"no nearby {self.name} row")
        return min(rows, key=lambda r: abs(r["timestamp"] - stamp))


def _vector(row, keys):
    return tuple(number(row[k]) for k in keys)


def _position(row, terrain):
    lat, lon, alt = _vector(row, ("lat", "lon", "alt"))
    return (*terrain.local_from_wgs84(lat, lon), alt)


def _errors(point, start, end):
    # The actual proof authorizes an axis-aligned box, not a cylinder nor a
    # time-parametrized centre line. Report box residuals independently in N/E and AMSL.
    d = [max(0, min(a, b) - p, p - max(a, b)) for p, a, b in zip(point, start, end, strict=True)]
    return max(d[:2]), d[2]


def _fw_angles(row):
    w, x, y, z = _vector(row, ("q[0]", "q[1]", "q[2]", "q[3]")) if "q[0]" in row else tuple(map(number, row["q"]))
    need(abs(math.hypot(w, x, y, z) - 1) < 0.01, "bad attitude quaternion")
    # Same q_MC * inverse(q_mc_to_fw) as PX4 FixedwingAttitudeControl.
    w, x, y, z = (v / math.sqrt(2) for v in (w - y, x - z, y + w, z + x))
    roll = math.degrees(math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y)))
    heading = math.degrees(math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))) % 360
    return roll, heading


def sweep_for(raw, run, profile):
    h = run["hold"]
    return FwRtlBounds.from_host_manifest(raw).sweep(
        SimpleNamespace(digest="offline-probe"), SimpleNamespace(frame_id=0, position_ne_amsl=run["start"]),
        SimpleNamespace(centre=h["centre"], padding_alt_m=h["padding_alt_m"], padding_ne_m=h["padding_ne_m"],
                        site=SimpleNamespace(radius_m=h["radius_m"])), run["home"],
        LandingPad(run["pad"], PAD_RADIUS_M, PAD_ERROR_M), profile)


def _manifest(scope, params, values, times, validation_hash="0" * 64):
    return {"scope": scope, "px4_commit": PX4_RTL_COMMIT, "image_id": IMAGE, "params": params,
            "validation_sha256": validation_hash, "home_only_verified": True, "configuration_locked": True,
            "phase_time_s": times, **values}


def _assert_parameter_log(raw, expected):
    """Logger writes initial P records before data and emits changed P records thereafter."""
    initial = {}
    offset, in_data = 16, False
    while offset + 3 <= len(raw):
        size, kind = struct.unpack_from("<Hc", raw, offset)
        begin = offset + 3
        offset = begin + size
        need(offset <= len(raw), "truncated ULog record")
        if kind == b"D":
            in_data = True
        elif kind == b"P":
            value_pos = begin + 1 + raw[begin]
            value_type, _, name = raw[begin + 1:value_pos].decode("ascii").partition(" ")
            if name in expected:
                need(offset == value_pos + 4 and value_type in {"int32_t", "float"}, "invalid ULog parameter record")
                value = struct.unpack_from("<i" if value_type == "int32_t" else "<f", raw, value_pos)[0]
                if in_data:
                    need(name in initial and value == initial[name], f"tracked parameter changed after log start: {name}")
                else:
                    need(name not in initial, f"duplicate initial ULog parameter: {name}")
                    initial[name] = value
    need(offset == len(raw) and in_data and initial.keys() == expected.keys()
         and all(math.isclose(initial[k], expected[k], rel_tol=0, abs_tol=1e-6) for k in expected),
         "ULog initial tracked configuration disagrees with PX4 readback")


def extract_run(meta, mav, topics, terrain, profile, raw_ulog):
    cell = Cell(**meta["cell"])
    readback, dispatch, post = mav_witness(mav, cell, meta)
    _assert_parameter_log(raw_ulog, readback)
    params = {k: readback[k] for k in (*_REQUIRED, *_POSITIVE)}

    def topic(name):
        return Topic(topics, name, battery=name == "battery_status")

    command_sample = number(dispatch["boot_ms"]) * 1000
    post_us = number(post["boot_ms"]) * 1000
    rtl = one(topic("vehicle_command").window(command_sample, post_us),
              lambda r: r["command"] == 20 and r["from_external"] == 1, "native RTL ULog command")
    sent = rtl["timestamp"]
    need(0 <= sent - command_sample <= 1_000_000, "MAVLink sample-to-native RTL clock mismatch")
    statuses = topic("vehicle_status").window(sent - 500_000, post_us + 500_000)
    entry = next((r for r in statuses if r["nav_state"] == 5 and r["nav_state_timestamp"] >= sent), None)
    need(entry is not None and entry["arming_state"] == 2 and entry["vehicle_type"] == 2,
         "ULog did not enter armed FW RTL")
    activation = entry["nav_state_timestamp"]
    need(0 <= activation - sent <= 1_000_000, "RTL activation delay exceeds 1 s")
    disarm = next((r["timestamp"] for r in statuses
                   if r["timestamp"] > activation and r["arming_state"] != 2), None)
    need(disarm is not None and abs(post_us - disarm) <= 2_000_000, "ULog did not record matching disarm")
    land = next((r["timestamp"] for r in topic("vehicle_land_detected").window(activation, disarm)
                 if r["landed"] == 1), None)
    need(land is not None and land < disarm, "missing ULog touch-down before disarm")
    need(any(r["arming_state"] != 2 for r in topic("vehicle_status").rows if r["timestamp"] < sent),
         "ULog did not capture prearm configuration")
    need(all(r["arming_state"] == 2 for r in statuses if activation <= r["timestamp"] < land),
         "unexpected intermediate disarm")
    need(all(r["nav_state"] == 5 and not r["failsafe"] for r in
             topic("vehicle_status").coverage(activation, land, 750_000)), "RTL exited or saw a failsafe before landing")
    transition = next((r["timestamp"] for r in statuses if activation < r["timestamp"] < land
                       and r["in_transition_mode"] and not r["in_transition_to_fw"]), None)
    hover = next((r["timestamp"] for r in statuses if transition is not None and transition < r["timestamp"] < land
                  and r["vehicle_type"] == 1 and not r["in_transition_mode"]), None)
    need(transition is not None and hover is not None, "ULog never confirmed FW to MC transition")

    nav = topic("navigator_mission_item").window(activation, land)
    states = [r["sequence_current"] for r in nav]
    need(states in ([2, 3, 4, 5, 6, 7, 8], [0, 2, 3, 4, 5, 6, 7, 8], [2, 4, 5, 6, 7, 8],
                    [0, 2, 4, 5, 6, 7, 8]), f"unsupported/missing Navigator state sequence: {states}")
    need(nav[0]["timestamp"] - activation <= 250_000, "first direct RTL state was too late")
    starts = {STATE_PHASE[state]: row["timestamp"] for state, row in reversed(list(zip(states, nav, strict=True)))}
    starts[0] = command_sample  # charge sample/command/activation slack even when CLIMBING was skipped
    if 0 not in states:
        starts[1] = activation
    starts[7] = land
    need(starts[3] <= transition and hover <= starts[6] + 750_000, "VTOL state outside direct RTL sequence")
    # Bound the physical transition as well as the Navigator transition item.
    starts[4] = min(transition, starts[4])
    starts[5] = max(hover, starts[5])
    need(all(starts[i] <= starts[i + 1] for i in range(7)), "phase timestamps overlap or run backwards")

    homes = [r for r in topic("home_position").rows if r["timestamp"] <= disarm]
    before = [r for r in homes if r["timestamp"] <= activation]
    need(before, "missing PX4 Home before RTL")
    home = _position(before[-1], terrain)
    pad = tuple(meta["pad"])
    need(math.dist(home[:2], pad[:2]) <= 5 and abs(home[2] - pad[2]) <= PAD_ERROR_M, "Home disagrees with Iowa pad")
    for row in [before[-1], *(r for r in homes if r["timestamp"] > activation)]:
        need(row["valid_hpos"] and row["valid_alt"] and math.dist(_position(row, terrain), home) <= 0.25,
             "PX4 Home changed or was invalid")
    start = tuple(map(number, dispatch["position"]))
    need(math.dist(start, _position(topic("vehicle_global_position").near(command_sample, 350_000), terrain)) <= 8,
         "MAVLink position disagrees with nearest ULog sample")
    hold = meta["hold"]
    return_z = max(start[2], home[2] + params["RTL_RETURN_ALT"])
    descend_z = min(return_z, home[2] + params["RTL_DESCEND_ALT"])
    for row in nav:
        phase = STATE_PHASE[row["sequence_current"]]
        if phase == 4:  # command-only VTOL_TRANSITION item, no position
            need(row["nav_cmd"] == 3000, "non-native transition item")
            continue
        need(not row["altitude_is_relative"], "unexpected relative RTL item altitude")
        target = (*terrain.local_from_wgs84(*_vector(row, ("latitude", "longitude"))), number(row["altitude"]))
        reference = (*home[:2], return_z if phase == 1 else home[2] if phase == 6 else descend_z)
        if phase == 0:
            need(min(math.dist(target[:2], p[:2]) for p in (start, hold["centre"])) <= 3
                 and abs(target[2] - return_z) <= 2, "unexpected climb orbit")
        else:
            need(math.dist(target[:2], reference[:2]) <= 1.5 and abs(target[2] - reference[2]) <= 2,
                 "alternate rally, approach, avoidance or incorrect home/altitude")
        if phase in (1, 2):
            need(abs(abs(number(row["loiter_radius"])) - params["RTL_LOITER_RAD"]) <= 0.1,
                 "RTL loiter radius differs from proof")

    run = {"run_id": cell.id, "cell": asdict(cell), "params": params, "readback": readback,
           "hold": hold, "home": home, "pad": pad, "start": start}
    zero = dict.fromkeys(FLOOR_OPEN, 0)
    times = [(starts[i + 1] - starts[i]) / 1e6 for i in range(7)]
    geometry = sweep_for(_manifest("probe", params, zero, times), run, profile)
    need(geometry is not None, "entry, pad and turn-radius geometry incompatible with proof")

    global_pos, local_pos = topic("vehicle_global_position"), topic("vehicle_local_position")
    velocity = local_pos.coverage(command_sample, land, 300_000)
    need(all(r["xy_valid"] and r["z_valid"] and r["v_xy_valid"] and r["v_z_valid"] for r in velocity),
         "position/velocity estimate invalid during RTL")
    attitude = topic("vehicle_attitude").coverage(command_sample, starts[4], 200_000)
    airspeeds = topic("airspeed_validated").coverage(activation, starts[4], 600_000)
    air = [number(r["true_airspeed_m_s"]) for r in airspeeds]
    air_sources = sorted({r["airspeed_source"] for r in airspeeds})
    need(all(v > 0 for v in air) and all(s in {0, 1, 2, 3, 4} for s in air_sources), "invalid FW true airspeed")
    omega = topic("vehicle_angular_velocity").coverage(command_sample, land, 200_000)
    batteries = topic("battery_status").coverage(command_sample, land, 500_000)
    socs = [number(r["remaining"]) for r in batteries]
    need(all(r["connected"] and 0 <= v <= 1 for r, v in zip(batteries, socs, strict=True)), "invalid battery SOC")
    entry_speed = _vector(local_pos.near(command_sample, 300_000), ("vx", "vy"))
    need(math.hypot(*entry_speed) >= 5, "hold offset is not an established moving FW entry")
    wind = topic("wind").coverage(activation, min(starts[4], activation + 8_000_000), 1_500_000)
    observed_wind = [statistics.median(number(r[key]) for r in wind) for key in ("windspeed_north", "windspeed_east")]
    theta = math.radians(meta["inputs"]["wind_toward_deg"])
    requested_wind = [cell.wind_mps * math.cos(theta), cell.wind_mps * math.sin(theta)]
    wind_ok = math.dist(observed_wind, requested_wind) <= 2

    positions, errors, agls = [], [], []
    for i, leg in enumerate(geometry.legs):
        if i == 0 and 0 not in states:
            positions.append([])
            errors.append([0, 0])
            agls.append(None)
            continue
        rows = global_pos.coverage(starts[i], starts[i + 1], 350_000) if times[i] > 0.35 else [
            global_pos.near(starts[i], 350_000), global_pos.near(starts[i + 1], 350_000)]
        need(not any(r["dead_reckoning"] for r in rows), "dead-reckoned position during RTL")
        points = [_position(r, terrain) for r in rows]
        positions.append(points)
        raw = [_errors(p, leg.start, leg.end) for p in points]
        errors.append([max(e[axis] for e in raw) for axis in (0, 1)])
        agls.append(min(p[2] - terrain.sample_local(*p[:2]).elevation_m for p in points))
    entry_at_transition = _position(global_pos.near(starts[4], 350_000), terrain)
    touchdown = _position(global_pos.near(land, 350_000), terrain)
    transition_loss = max(0, entry_at_transition[2] - min(p[2] for p in positions[4]))
    values = {
        "fw_tracking_m": max(max(0, errors[i][0] - geometry.legs[i].padding_ne_m) for i in range(4)),
        "fw_vertical_m": max(e[1] for e in errors[:3]),
        "fw_acceptance_m": math.dist(entry_at_transition[:2], home[:2]),
        "move_to_land_vertical_m": errors[3][1], "transition_vertical_m": max(transition_loss, errors[4][1]),
        "mc_tracking_m": max(0, errors[5][0] - geometry.legs[5].padding_ne_m), "mc_vertical_m": errors[5][1],
        "landing_tracking_m": max(0, errors[6][0] - geometry.legs[6].padding_ne_m), "landing_vertical_m": errors[6][1],
        "max_ground_speed_mps": max(math.hypot(*_vector(r, ("vx", "vy"))) for r in velocity),
        "max_bank_deg": max(abs(_fw_angles(r)[0]) for r in attitude), "max_climb_mps": max(0, max(-number(r["vz"]) for r in velocity)),
        "max_descent_mps": max(0, max(number(r["vz"]) for r in velocity)),
        "max_acceleration_mps2": max(math.hypot(*_vector(r, ("ax", "ay", "az"))) for r in velocity),
        "max_angular_rate_dps": max(math.degrees(math.hypot(*map(number, r["xyz"]))) for r in omega),
    }
    # Stock BatterySimulator returns -1 A. Report SOC but refuse a power bound
    # unless a future instrumented SIM battery supplies valid electrical samples.
    powers = []
    for row in batteries:
        voltage, current = row.get("voltage_v"), row.get("current_a")
        powers.append(voltage * current if all(type(v) in (int, float) and math.isfinite(v) and v > 0
                                                for v in (voltage, current)) else None)
    for key, samples in (("fw_peak_power_w", [w for r, w in zip(batteries, powers, strict=True) if r["timestamp"] <= starts[4]]),
                         ("hover_peak_power_w", [w for r, w in zip(batteries, powers, strict=True) if r["timestamp"] >= starts[4]])):
        values[key] = max(samples) if samples and all(w is not None for w in samples) else None
    run.update({"observations": values, "phase_time_s": times, "state_sequence": states,
                "phase_starts_us": [starts[i] for i in range(8)], "disarm_us": disarm,
                "vtol_transition_observed_s": (hover - transition) / 1e6,
                "entry_heading_deg": _fw_angles(topic("vehicle_attitude").near(command_sample, 200_000))[1],
                "positions": positions, "input_to_send_s": (sent - command_sample) / 1e6,
                "send_to_rtl_s": (activation - sent) / 1e6, "phase_tracking_ne_and_vertical_m": errors,
                "phase_min_agl_m": agls, "transition_altitude_loss_m": transition_loss,
                "landing_offset_from_home_m": math.dist(touchdown[:2], home[:2]),
                "landing_entry_speed_mps": math.hypot(*_vector(local_pos.near(starts[6], 300_000), ("vx", "vy"))),
                "airspeed_mps": [min(air), max(air)], "px4_airspeed_sources": air_sources, "entry_course_deg": math.degrees(math.atan2(entry_speed[1], entry_speed[0])) % 360,
                "entry_soc": socs[0], "landing_soc": socs[-1], "soc_floor_reached": min(socs) <= 0.51,
                "energy_used_wh_from_soc": CAPACITY_WH * max(0, socs[0] - min(socs)), "wind_ne_mps": observed_wind,
                "wind_verified": wind_ok})
    return run


def _upper(values, margin, floor=0):
    maximum, median = max(values.values()), statistics.median(values.values())
    limit = max(2 * median, median + 4 * margin)
    evidence = {"values_by_run_id": values, "max_run_ids": [k for k, v in values.items() if v == maximum],
                "max_observed": maximum, "median": median, "variation_ceiling": limit, "absolute_margin": margin,
                "floor": floor, "upper": math.ceil(1000 * max(floor, maximum + max(0.20 * maximum, margin))) / 1000}
    return evidence, maximum <= limit


def evaluate(cells, runs, profile, inputs, errors):
    refusals = list(errors)
    ids = [r["run_id"] for r in runs]
    if len(ids) < MIN_RUNS or len(ids) != len(set(ids)) or set(ids) != {c.id for c in cells}:
        refusals.append(f"need every declared matrix cell exactly once and at least {MIN_RUNS} valid runs")
    scope = canonical_sha256({"probe": "x1-fw-rtl.v1", "inputs": inputs, "cells": [asdict(c) for c in cells]})
    report = {**META, "scope": scope, "status": "refused", "rule": {
        "minimum_runs": MIN_RUNS, "relative_margin": 0.20, "formula": "ceil_mm(max(floor, observed_max + max(20% of observed_max, absolute_margin)))",
        "variation": "reject if max > max(2*median, median + 4*absolute_margin); do not discard trials",
        "scope": "measured grid only; not a case-C registration scope"},
        "refusals": refusals, "source_assumptions_unverified": UNVERIFIED, "observations": {
            r["run_id"]: {k: v for k, v in r.items() if k != "positions"} for r in runs}, "derivations": {}}
    if not runs:
        return None, report
    params = runs[0]["params"]
    if any(r["params"] != params for r in runs):
        refusals.append("native parameters vary across the matrix")
    if any(not r["wind_verified"] for r in runs):
        refusals.append("EKF wind estimate does not corroborate the requested wind")
    if any(r["cell"]["wind_mps"] and not set(r["px4_airspeed_sources"]) <= {1, 2, 3} for r in runs):
        refusals.append("windy FW airspeed was not consistently sensor-backed; EKF wind corroboration is not independent")
    dry85 = [r for r in runs if r["cell"]["wind_mps"] == 0 and r["cell"]["cruise_agl_m"] == 85]
    if len({int(r["entry_heading_deg"] // 90) for r in dry85}) != 4:
        refusals.append("calm 85 m observations do not cover all four heading quadrants")
    soc = defaultdict(list)
    for r in runs:
        soc[r["cell"]["drain_s"]].append(r["entry_soc"])
    centers = {drain: statistics.median(samples) for drain, samples in sorted(soc.items())}
    report["entry_soc_median_by_drain_s"] = centers
    if len(centers) < 2 or any(b - a < 0.03 for a, b in zip(list(centers.values()), list(centers.values())[1:])):
        refusals.append("adjacent drain levels did not yield entry SOC medians spaced by 3 percentage points")
    if any(r["soc_floor_reached"] for r in runs):
        refusals.append("SOC reaches the SIM floor; depletion is censored")
    if any(min(v for v in r["phase_min_agl_m"][:5] if v is not None) - DEM_ERROR_M < 40 for r in runs):
        refusals.append("observed FW/transition clearance fails 40 m AGL after DEM uncertainty")
    if any(r["landing_offset_from_home_m"] + math.dist(r["home"][:2], r["pad"][:2]) > PAD_RADIUS_M for r in runs):
        refusals.append("touchdown outside conservative pad radius")

    by_metric = {name: {r["run_id"]: r["observations"][name] for r in runs} for name in FLOOR_OPEN}
    by_metric.update({phase: {r["run_id"]: r["phase_time_s"][i] for r in runs} for i, phase in enumerate(_PHASES)})
    floors = {"max_ground_speed_mps": profile.max_airspeed_mps, "max_bank_deg": params["FW_R_LIM"],
              "transition": params["VT_B_TRANS_DUR"] + 0.001}
    bounded = {}
    for key, values in by_metric.items():
        if any(v is None for v in values.values()):
            report["derivations"][key] = {"values_by_run_id": values, "upper": None}
            refusals.append(f"{key}: missing valid electrical samples; SOC cannot identify peak power")
            continue
        evidence, stable = _upper(values, FLOOR_OPEN.get(key, 1), floors.get(key, 0))
        report["derivations"][key] = evidence
        bounded[key] = evidence["upper"]
        if not stable:
            refusals.append(f"{key}: excessive variation/outlier, see run IDs")
    for key in ("airspeed_mps", "energy_used_wh_from_soc", "transition_altitude_loss_m", "landing_offset_from_home_m",
                "vtol_transition_observed_s", "input_to_send_s", "landing_entry_speed_mps", "entry_soc", "landing_soc"):
        report.setdefault("metrics_by_run_id", {})[key] = {r["run_id"]: r[key] for r in runs}
    report["parameter_derivation"] = {k: {"value": v, "run_ids": ids, "source": "initial ULog parameter records plus prearm and post-disarm PX4 readback"}
                                      for k, v in params.items()}
    if refusals:
        return None, report
    raw = _manifest(scope, params, {k: bounded[k] for k in FLOOR_OPEN}, [bounded[p] for p in _PHASES])
    if not FwRtlBounds.from_host_manifest(raw).valid_for(scope, profile):
        refusals.append("observed maxima + margins exceed FwRtlBounds/profile limits; never clamp down")
    for r in runs:
        sweep = sweep_for(raw, r, profile)
        if sweep is None or any(
            (d := _errors(p, leg.start, leg.end))[0] > leg.padding_ne_m or d[1] > leg.padding_alt_m
            for leg, points in zip(sweep.legs, r["positions"], strict=True) for p in points
        ):
            refusals.append(f"{r['run_id']}: conservative seven-segment sweep does not contain observations")
    if refusals:
        return None, report
    report["status"] = "reviewed"  # automated rule review for this measured scope only
    return raw, report


def _within(parent, relative):
    path = (parent / relative).resolve()
    need(path.is_relative_to(parent.resolve()), "episode or ULog path escapes the probe run")
    return path


def analyze(root, out, name, *, allow_partial=False):
    expected = demo.read_object(root / "matrix.json")
    need(all(expected.get(k) == v for k, v in META.items()), "not an immutable ASSUMED_SIM measurement matrix")
    cells = [Cell(**item) for item in expected["cells"]]
    need(cells and cells == matrix(*[sorted({getattr(cell, field) for cell in cells}) for field in
                                    ("offset_s", "wind_mps", "drain_s", "cruise_agl_m")],
                                  max(cell.repeat for cell in cells)), "matrix missing cells or not in canonical order")
    inputs = expected["inputs"]
    need(inputs["world_sha256"] == demo.assumptions.WORLD_SHA and inputs["dem_sha256"] == demo.assumptions.DEM_SHA
         and inputs["airframe_sha256"] == digest(demo.PROFILE), "world/DEM/profile identity mismatch")
    profile = load_airframe_profile(demo.PROFILE)
    runs, errors, skipped = [], [], []
    seen = set()
    for cell in cells:
        run_dir = root / cell.id
        try:
            meta = demo.read_object(run_dir / "probe.json")
        except FileNotFoundError:
            if allow_partial and not run_dir.exists():
                skipped.append(cell.id)
            else:
                errors.append(f"{cell.id}: probe_missing")
            continue
        except (OSError, ValueError) as exc:
            errors.append(f"{cell.id}: {exc}")
            continue
        try:
            need(meta["cell"] == asdict(cell) and meta["run_id"] == cell.id and meta["inputs"] == inputs
                 and meta["scenario"] == demo.assumptions.WORLD
                 and all(meta.get(k) == v for k, v in META.items()), "run identity mismatch")
            terrain = load_local_dem_grid(meta["dem"])
            need(terrain.source_sha256 == inputs["dem_sha256"], "DEM bytes changed")
            episode = _within(run_dir, demo.read_object(run_dir / "episode.json")["episode"])
            end = demo.read_object(episode / "episode.json")
            need(end.get("status") == "complete" and end.get("simulator_image") == IMAGE
                 and end.get("px4_profile") == "main", "episode did not complete under the pinned candidate/main profile")
            try:
                evidence = demo.read_object(episode / "container-evidence" / "index.json")
            except FileNotFoundError as exc:
                raise ValueError("ulog_missing") from exc
            ulog_files = evidence.get("ulog_files", [])
            need(isinstance(ulog_files, list), "invalid ULog index")
            need(ulog_files, "ulog_missing")
            need(len(ulog_files) == 1, "expected exactly one ULog")
            ulog = _within(episode, ulog_files[0])
            need(ulog.is_file() and ulog.stat().st_size > 0, "ulog_missing")
            mav = episode / "mavlink.jsonl"
            if not mav.is_file():
                mav = episode / "mavlink.jsonl.gz"
            hashes = {"mavlink_sha256": digest(mav), "ulog_sha256": digest(ulog), "probe_sha256": digest(run_dir / "probe.json")}
            need(hashes["ulog_sha256"] not in seen, "duplicate ULog bytes across cells")
            seen.add(hashes["ulog_sha256"])
            raw_ulog = ulog.read_bytes()
            parsed = _parse_internal(raw_ulog, ulog)
            need(not parsed.warnings, f"ULog contains dropout or parse errors: {parsed.warnings[:3]}")
            result = extract_run(meta, read_mavlink(mav), {k: v.rows for k, v in parsed.topics.items()}, terrain, profile, raw_ulog)
            result["evidence"] = {"mavlink": str(mav), "ulog": str(ulog), **hashes}
            runs.append(result)
        except (OSError, KeyError, TypeError, ValueError, IndexError, struct.error) as exc:
            errors.append(f"{cell.id}: {exc}")
    raw, report = evaluate(cells, runs, profile, inputs, errors)
    if allow_partial:
        report["skipped_unflown_cells"] = skipped
    out.mkdir(parents=True)
    sidecar = out / f"{name}.validation.json"
    json_file(sidecar, report)
    if raw is None:
        return 2
    raw["validation_sha256"] = digest(sidecar)
    json_file(out / f"{name}.json", raw)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("sim",))
    parser.add_argument("--matrix", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--name", default="x1-fw-rtl")
    parser.add_argument("--allow-partial", action="store_true",
                        help="skip absent cell directories for diagnostics; full coverage is still required for a manifest")
    args = parser.parse_args(argv)
    try:
        require_sim(args.mode)
        need(args.name.isascii() and args.name.replace("-", "").replace("_", "").isalnum(), "invalid output name")
        return analyze(args.matrix.resolve(), args.output, args.name, allow_partial=args.allow_partial)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(2, f"FW RTL analysis: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
