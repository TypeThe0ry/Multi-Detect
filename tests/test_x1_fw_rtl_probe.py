"""CPU-only fixtures. No simulator, container, serial link or GPU calls."""
import json
import math
import shutil
import struct
from pathlib import Path, PurePosixPath
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from scripts import analyze_x1_fw_rtl_probe as analysis
from scripts import measure_x1_fw_rtl as probe
from scripts import run_x1_autonomy_demo as demo
from scripts.analyze_px4_ulog import MAGIC, _parse_internal
from multidetect.sim_teacher.x1_fw_native_rtl import FwRtlBounds


class FlatTerrain:
    source_sha256 = demo.assumptions.DEM_SHA

    def local_from_wgs84(self, lat, lon):
        return (lat - 42) * 100_000, (lon + 93) * 100_000

    def sample_local(self, *_):
        return SimpleNamespace(elevation_m=300)


PARAMS = {**probe._REQUIRED, "RTL_RETURN_ALT": 60, "RTL_LOITER_RAD": 80,
          "NAV_LOITER_RAD": 80, "VT_B_TRANS_DUR": 2, "VT_B_DEC_MSS": 2, "FW_R_LIM": 35}
FIXTURES = Path(__file__).with_name("fixtures")
INPUTS = {"world_sha256": demo.assumptions.WORLD_SHA, "dem_sha256": demo.assumptions.DEM_SHA,
          "airframe_sha256": probe.digest(demo.PROFILE), "wind_toward_deg": 0}
CELLS = probe.matrix([30, 39, 48, 57], [0, 4], [600, 1800], [85, 95], 1)
SCALAR = {int: ("int32_t", "i"), float: ("double", "d")}


def _frame(kind, payload):
    return struct.pack("<Hc", len(payload), kind.encode()) + payload


def _param(key, value):
    kind, fmt = ("float", "f") if isinstance(value, float) else ("int32_t", "i")
    name = f"{kind} {key}".encode()
    return _frame("P", bytes([len(name)]) + name + struct.pack("<" + fmt, value))


@pytest.fixture
def pilot_parameter_evidence():
    # Trimmed from matrix1/o30-w0-b600-a85-r1 (episode-0000-a5568093).
    # The ULog retains tracked P records and vehicle_status F/A/D frames in
    # original order; the JSONL retains the readback and clock witnesses.
    raw = (FIXTURES / "x1_fw_rtl_pilot_prearm.ulg").read_bytes()
    mav = analysis.read_mavlink(FIXTURES / "x1_fw_rtl_pilot_prearm.mavlink.jsonl")
    marker = next(row for row in mav if row["message_type"] == "scenario_fw_rtl_readback")
    return raw, mav, marker


def _assert_pilot_pins(raw, mav, marker):
    parsed = _parse_internal(raw)
    assert not parsed.warnings
    statuses = analysis.Topic({"vehicle_status": parsed.topics["vehicle_status"].rows}, "vehicle_status")
    analysis._assert_parameter_log(raw, mav, marker, statuses)


def _insert_before_last_status(raw, frames, *, records_from_end=1):
    offset = 16
    data_offsets = []
    while offset < len(raw):
        size, kind = struct.unpack_from("<Hc", raw, offset)
        if kind == b"D":
            data_offsets.append(offset)
        offset += 3 + size
    pos = data_offsets[-records_from_end]
    return raw[:pos] + frames + raw[pos:]


def test_boot_ulog_accepts_pilot_prearm_pins_and_same_value_rewrite(pilot_parameter_evidence):
    raw, mav, marker = pilot_parameter_evidence
    _assert_pilot_pins(raw, mav, marker)
    _assert_pilot_pins(_insert_before_last_status(raw, _param("RTL_TYPE", 0)), mav, marker)


def test_boot_ulog_rejects_changed_pin_during_flight(pilot_parameter_evidence):
    raw, mav, marker = pilot_parameter_evidence
    # Insert the mutation immediately after the first armed sample, before disarm.
    with pytest.raises(ValueError, match="tracked parameter changed at or after arming: RTL_TYPE"):
        _assert_pilot_pins(_insert_before_last_status(raw, _param("RTL_TYPE", 1)), mav, marker)


@pytest.mark.parametrize("placement", ["missing", "after_arming"])
def test_boot_ulog_rejects_parameter_never_established_before_arming(pilot_parameter_evidence, placement):
    raw, mav, marker = pilot_parameter_evidence
    first, pinned = _param("RTL_TYPE", 1), _param("RTL_TYPE", 0)
    assert raw.count(first) == 1 and raw.count(pinned) == 2
    missing = raw.replace(first, b"").replace(pinned, b"")
    if placement == "after_arming":
        missing = _insert_before_last_status(missing, pinned)
    with pytest.raises(ValueError, match="tracked parameter never established before arming: RTL_TYPE"):
        _assert_pilot_pins(missing, mav, marker)


def test_boot_ulog_accepts_omitted_default_only_with_matching_verified_readback(pilot_parameter_evidence):
    raw, mav, marker = pilot_parameter_evidence
    pinned = _param("FW_R_LIM", 35.0)
    assert raw.count(pinned) == 1
    missing = raw.replace(pinned, _frame("Q", b"\x01" + pinned[3:]))
    _assert_pilot_pins(missing, mav, marker)
    with pytest.raises(ValueError, match="missing verified pre-arm MAVLink readback"):
        _assert_pilot_pins(missing, mav, {**marker, "verified": False})
    with pytest.raises(ValueError, match="ULog parameter at arming disagrees with pinned PX4 readback: FW_R_LIM"):
        values = {**marker["observed_parameters"], "FW_R_LIM": 36.0}
        _assert_pilot_pins(missing, mav, {**marker, "observed_parameters": values})


def test_boot_ulog_rejects_change_straddling_arming(pilot_parameter_evidence):
    raw, mav, marker = pilot_parameter_evidence
    uncertain = _insert_before_last_status(raw, _param("RTL_TYPE", 1), records_from_end=2)
    with pytest.raises(ValueError, match="tracked parameter change cannot be placed before arming: RTL_TYPE"):
        _assert_pilot_pins(uncertain, mav, marker)


def test_boot_ulog_rejects_prearm_value_disagreeing_with_readback(pilot_parameter_evidence):
    raw, mav, marker = pilot_parameter_evidence
    with pytest.raises(ValueError, match="ULog parameter at arming disagrees with pinned PX4 readback: RTL_TYPE"):
        _assert_pilot_pins(raw.replace(_param("RTL_TYPE", 0), b""), mav, marker)


def test_boot_ulog_rejects_readback_after_arming(pilot_parameter_evidence):
    raw, mav, marker = pilot_parameter_evidence
    # After the MAVLink boot clock crosses the ULog arm time but before the
    # first MAVLink heartbeat that reports being armed.
    late = {**marker, "receive_monotonic_ns": mav[-2]["receive_monotonic_ns"] + 1}
    with pytest.raises(ValueError, match="MAVLink parameter readback at or after ULog arming"):
        _assert_pilot_pins(raw, [mav[0], *mav[2:-1], late, mav[-1]], late)


def test_boot_ulog_rejects_unbracketed_readback(pilot_parameter_evidence):
    raw, mav, marker = pilot_parameter_evidence
    with pytest.raises(ValueError, match="cannot establish MAVLink parameter readback before ULog arming"):
        _assert_pilot_pins(raw, mav[:4] + mav[5:], marker)


def _ulog(topics, params):
    chunks = [MAGIC + b"\x01" + b"\x00" * 8, *(_param(k, v) for k, v in params.items())]
    data = []
    for n, (topic, rows) in enumerate(topics.items(), 1):
        types, formats = [], []
        for key, value in rows[0].items():
            if key.endswith("timestamp"):
                type_name, fmt = "uint64_t", "Q"
            elif isinstance(value, tuple):
                type_name, fmt = f"double[{len(value)}]", "d" * len(value)
            else:
                type_name, fmt = SCALAR[type(value)]
            types.append(f"{type_name} {key};")
            formats.append(fmt)
        chunks += [_frame("F", f"{topic}:{''.join(types)}".encode()),
                   _frame("A", struct.pack("<BH", 0, n) + topic.encode())]
        for row in rows:
            values = [item for value in row.values() for item in (value if isinstance(value, tuple) else (value,))]
            data.append((row["timestamp"], _frame("D", struct.pack("<H" + "".join(formats), n, *values))))
    return b"".join(chunks + [packet for _, packet in sorted(data)])


def _fixture(cell, *, descend_m=73, current=12, clock_error=0, invalid_state=False, tracking_outlier=0,
             idle_before_land_us=None, idle_command=0):
    cell_index = CELLS.index(cell)
    entry = 225_000_000 + 20_000_000 * cell_index
    rtl, land, disarm = entry + 100_000, entry + 9_000_000, entry + 10_000_000
    elevation = 385 + (cell.cruise_agl_m - 85)
    lat0, lon0 = 42.0, -93.0
    hold = {"centre": [320, 0, elevation], "lat_lon": [42.0032, lon0], "radius_m": 80,
            "speed_mps": 15, "padding_alt_m": 12, "padding_ne_m": 120}
    start = [240, 0, elevation]
    meta = {**analysis.META, "run_id": cell.id, "cell": asdict(cell), "hold": hold, "pad": [0, 0, 300],
            "inputs": INPUTS, "dem": "unused", "scenario": demo.assumptions.WORLD}
    params = {**PARAMS, "RTL_DESCEND_ALT": descend_m, "SIM_BAT_DRAIN": 1800, "SIM_BAT_MIN_PCT": 50,
              "SIM_BAT1_DRAIN": cell.drain_s, "SIM_BAT1_MIN_PCT": 50}
    readback = dict(verified=True, observed_px4_commits=[probe.PX4_RTL_COMMIT], observed_parameters=params, basis=probe.BASIS)

    def marker(kind, **values):
        return {"message_type": kind, **values}

    def clock(kind, ns, **values):
        return marker(kind, _source_system=1, _source_component=1, receive_monotonic_ns=ns, **values)

    hold_ms = entry // 1000 - cell.offset_s * 1000
    mav = [clock("LOCAL_POSITION_NED", 1000, time_boot_ms=entry // 1000 - 210),
           marker("scenario_fw_rtl_readback", receive_monotonic_ns=2000, **readback),
           clock("HEARTBEAT", 3000, base_mode=29),
           clock("LOCAL_POSITION_NED", 4000, time_boot_ms=entry // 1000 - 190),
           marker("fw_rtl_probe_hold_request"),
           marker("scenario_native_command", action="loiter", command=34, ack_result=0, mode_before=[6, 0],
                  params=[80, 15, 0, 0, *hold["lat_lon"], elevation]),
           marker("scenario_native_handoff", action="loiter", command=34, status="confirmed", observed_mode=[4, 3]),
           marker("fw_rtl_probe_hold_confirmed", boot_ms=hold_ms)]
    if cell.wind_mps:
        for offset, fraction in ((0, 0), (2000, 0.5), (4000, 1)):
            mav.append(marker("fw_rtl_probe_wind_publish", boot_ms=hold_ms + offset, enu_mps=[0, fraction * cell.wind_mps, 0],
                              fraction=fraction, readback="publish_only"))
    mav += [marker("fw_rtl_probe_dispatch", boot_ms=entry // 1000 + clock_error, clock_ms=entry // 1000,
                   hold_boot_ms=hold_ms, position=start, motion_proof=False, basis=probe.BASIS),
            marker("scenario_native_command", action="return", command=20, ack_result=0, mode_before=[4, 3], params=[0] * 7),
            marker("scenario_native_handoff", action="return", command=20, status="confirmed", observed_mode=[4, 5]),
            marker("fw_rtl_probe_disarmed", boot_ms=disarm // 1000, **readback),
            marker("collector_landing_complete", disarmed=True)]
    if cell.wind_mps:
        mav.append(marker("scenario_restored", fault="wind_step", reason="episode_exit", applied={"enu_mps": [0, 0, 0]}))

    def stamped(at, **values):
        return {"timestamp": at, **values}

    nav = []
    for state, delta in ((2, 100_000), (3, 2_000_000), (4, 2_500_000), (5, 3_000_000),
                         (6, 4_000_000), (7, 7_000_000), (8, 8_000_000)):
        nav.append(stamped(entry + delta, sequence_current=1 if invalid_state and state == 3 else state,
                           nav_cmd=3000 if state == 6 else 16, latitude=lat0, longitude=lon0,
                           altitude=elevation if state == 2 else 300 if state == 8 else 300 + descend_m,
                           loiter_radius=80, altitude_is_relative=0))
    if idle_before_land_us is not None:
        nav.append({**nav[-1], "timestamp": land - idle_before_land_us, "sequence_current": 9, "nav_cmd": idle_command})
    topics = {"navigator_mission_item": nav, "vehicle_command": [stamped(entry + 50_000, command=20, from_external=1)],
              "vehicle_land_detected": [stamped(land, landed=1)],
              "home_position": [stamped(entry - 100_000, lat=lat0, lon=lon0, alt=300, valid_hpos=1, valid_alt=1)]}
    names = "vehicle_status vehicle_global_position vehicle_local_position vehicle_attitude vehicle_angular_velocity battery_status airspeed_validated wind".split()
    topics.update({name: [] for name in names})
    angle = math.radians((cell.offset_s - 30) / 9 * 90 + 30) / 2
    q = tuple(v / math.sqrt(2) for v in (math.cos(angle), math.sin(angle), -math.cos(angle), math.sin(angle)))
    for t in range(-200_000, 10_100_000, 100_000):
        stamp = entry + t
        local_x = 240.0 if t <= 200_000 else 0.0
        alt = elevation if t < 2_000_000 else 300 + descend_m if t < 8_400_000 else 300
        if 3_000_000 <= t < 3_500_000:
            alt -= tracking_outlier
        rows = [stamped(stamp, nav_state=5 if stamp >= rtl else 3, nav_state_timestamp=rtl,
                        arming_state=2 if 0 <= t < 10_000_000 else 1, vehicle_type=2 if t < 7_000_000 else 1,
                        system_id=1, component_id=1,
                        failsafe=0, in_transition_mode=int(4_000_000 <= t < 7_000_000), in_transition_to_fw=0),
                stamped(stamp, lat=lat0 + local_x / 100_000, lon=lon0, alt=alt, dead_reckoning=0),
                stamped(stamp, vx=15, vy=0, vz=1, ax=0, ay=0, az=0, xy_valid=1, z_valid=1, v_xy_valid=1, v_z_valid=1),
                stamped(stamp, q=q), stamped(stamp, xyz=(0, 0, 0.1)),
                stamped(stamp, id=1, connected=1, voltage_v=15, current_a=current,
                        remaining=(0.81 if cell.drain_s == 600 else 0.94) - max(0, t) * 1e-9),
                stamped(stamp, true_airspeed_m_s=15, airspeed_source=1), stamped(stamp, windspeed_north=cell.wind_mps, windspeed_east=0)]
        for k, row in zip(names, rows, strict=True):
            topics[k].append(row)
    return meta, mav, _ulog(topics, params)


def _run(cell, **kwargs):
    meta, mav, raw = _fixture(cell, **kwargs)
    parsed = _parse_internal(raw)
    assert not parsed.warnings
    return analysis.extract_run(meta, mav, {k: v.rows for k, v in parsed.topics.items()}, FlatTerrain(),
                                analysis.load_airframe_profile(demo.PROFILE), raw)


@pytest.mark.parametrize("current", [12, -1])
def test_analyze_serialized_matrix_writes_exact_bound(tmp_path, monkeypatch, current):
    root = tmp_path / "matrix"
    root.mkdir()
    probe.json_file(root / "matrix.json", {**analysis.META, "inputs": INPUTS, "cells": [asdict(c) for c in CELLS]})
    monkeypatch.setattr(analysis, "load_local_dem_grid", lambda _: FlatTerrain())
    for cell in CELLS:
        meta, mav, raw = _fixture(cell, current=current)
        run_dir = root / cell.id
        episode = run_dir / "evidence"
        (episode / "container-evidence").mkdir(parents=True)
        probe.json_file(run_dir / "probe.json", meta)
        probe.json_file(run_dir / "episode.json", {"episode": "evidence"})
        probe.json_file(episode / "episode.json", {"status": "complete", "simulator_image": probe.IMAGE, "px4_profile": "main"})
        probe.json_file(episode / "container-evidence/index.json", {"ulog_files": ["container-evidence/flight.ulg"]})
        (episode / "container-evidence/flight.ulg").write_bytes(raw)
        (episode / "mavlink.jsonl").write_text("".join(json.dumps(row) + "\n" for row in mav))
    output = tmp_path / "analysis"
    status = analysis.analyze(root, output, "probe")
    sidecar = output / "probe.validation.json"
    report = demo.read_object(sidecar)
    assert status == 0, report["refusals"]
    bound = FwRtlBounds.from_host_manifest(demo.read_object(output / "probe.json"))
    assert bound.valid_for(report["scope"], analysis.load_airframe_profile(demo.PROFILE))
    assert report["status"] == "reviewed" and report["basis"] == "ASSUMED_SIM"
    assert bound.validation_sha256 == probe.digest(sidecar)
    assert bound.params["RTL_DESCEND_ALT"] == 73
    assert bound.fw_peak_power_w == (216 if current > 0 else 344)
    assert bound.hover_peak_power_w == (216 if current > 0 else 750)
    derivations = report["derivations"]
    assert set(derivations) == {*analysis.FLOOR_OPEN, *analysis._PHASES}
    for key, entry in derivations.items():
        expected_basis = "ASSUMED_SIM" if current < 0 and key in analysis.POWER_METRICS else "MEASURED_SIM"
        assert entry["basis"] == expected_basis
    if current < 0:
        fw, hover = (derivations[key] for key in analysis.POWER_METRICS)
        assert fw["inputs_w"] == {"loiter_power_w": 172}
        assert hover["inputs_w"] == {"climb_power_w": 750, "hover_power_w": 500, "descent_power_w": 420}
        for entry in (fw, hover):
            assert entry["airframe_profile_sha256"] == INPUTS["airframe_sha256"]
            assert set(entry["values_by_run_id"].values()) == {None}
            assert "max_observed" not in entry
    assert bound.phase_time_s == (1.1, 2.9, 2, 2, 4, 2, 2)
    trial = report["observations"][CELLS[0].id]
    assert trial["landing_offset_from_home_m"] == 0
    assert trial["transition_altitude_loss_m"] == 0
    assert trial["vtol_transition_observed_s"] == 3
    assert trial["airspeed_mps"] == [15, 15]
    assert trial["energy_used_wh_from_soc"] == pytest.approx(0.648)
    assert len(report["derivations"]["fw_peak_power_w"]["values_by_run_id"]) == len(CELLS)

    partial_root = tmp_path / "partial-matrix"
    partial_root.mkdir()
    shutil.copyfile(root / "matrix.json", partial_root / "matrix.json")
    shutil.copytree(root / CELLS[0].id, partial_root / CELLS[0].id)
    partial_out = tmp_path / "partial-analysis"
    assert analysis.analyze(partial_root, partial_out, "probe", allow_partial=True) == 2
    partial = demo.read_object(partial_out / "probe.validation.json")
    assert list(partial["observations"]) == [CELLS[0].id]
    assert partial["skipped_unflown_cells"] == [c.id for c in CELLS[1:]]
    assert not (partial_out / "probe.json").exists()


def test_landed_timestamp_idle_does_not_change_flight_phases():
    baseline = _run(CELLS[0])
    terminal_idle = _run(CELLS[0], idle_before_land_us=0)
    assert terminal_idle["raw_state_sequence"] == [2, 3, 4, 5, 6, 7, 8, 9]
    assert terminal_idle["state_sequence"] == baseline["state_sequence"]
    assert terminal_idle["phase_time_s"] == baseline["phase_time_s"]
    assert terminal_idle["observations"] == baseline["observations"]


@pytest.mark.parametrize("idle_before_land_us,idle_command", [(10_000, 0), (0, 16)])
def test_refuses_airborne_idle_or_non_idle_item_at_touchdown(idle_before_land_us, idle_command):
    with pytest.raises(ValueError, match="unsupported/missing Navigator state sequence"):
        _run(CELLS[0], idle_before_land_us=idle_before_land_us, idle_command=idle_command)


def test_conditioned_variation_accepts_repeatable_altitude_shift_and_keeps_global_max():
    runs = [_run(c) for c in CELLS]
    for cell, run in zip(CELLS, runs, strict=True):
        run["observations"]["move_to_land_vertical_m"] = 2 if cell.cruise_agl_m == 85 else 15
    high = next(run for run in runs if run["cell"]["cruise_agl_m"] == 95)
    high["observations"]["move_to_land_vertical_m"] = 18
    bound, report = analysis.evaluate(CELLS, runs, analysis.load_airframe_profile(demo.PROFILE), INPUTS, [])
    assert report["refusals"] == []
    assert bound["move_to_land_vertical_m"] == 21.6
    entry = report["derivations"]["move_to_land_vertical_m"]
    assert entry["pooled_variation_ceiling"] == 17
    assert {error for b in entry["conditioned_variation"]["bins"]
            for error in b["residual_by_run_id"].values()} == {0, 3}


def test_conditioned_variation_refuses_thin_and_empty_bins_even_with_global_minimum():
    thin = [c for c in CELLS if c.offset_s == 30 and c.wind_mps == 0]
    empty = [c for c in CELLS if c.offset_s == 39 and c.wind_mps == 0]
    missing = {c.id for c in thin[:2] + empty}
    runs = [_run(c) for c in CELLS if c.id not in missing]
    assert len(runs) >= probe.MIN_RUNS
    bound, report = analysis.evaluate(CELLS, runs, analysis.load_airframe_profile(demo.PROFILE), INPUTS, [])
    assert bound is None
    assert "fw_tracking_m: fewer than 3 valid runs in a declared condition bin, see run IDs" in report["refusals"]
    thin_bin, empty_bin = (b for b in report["derivations"]["fw_tracking_m"]["conditioned_variation"]["bins"]
                          if b["condition"]["wind_mps"] == 0 and b["condition"]["offset_s"] in {30, 39})
    assert thin_bin["valid_run_ids"] == [c.id for c in thin[2:]]
    assert thin_bin["missing_run_ids"] == [c.id for c in thin[:2]]
    assert empty_bin["valid_run_ids"] == empty_bin["outlier_run_ids"] == []
    assert empty_bin["missing_run_ids"] == [c.id for c in empty]


def test_conditioned_variation_detects_high_residual_hidden_by_pooled_median():
    values = {c.id: 30 if c.wind_mps else 0 for c in CELLS}
    high = CELLS[0].id
    values[high] = 8
    _, pooled_stable = analysis._upper(values, 1)
    assert pooled_stable
    fields, minimum = analysis.VARIATION_CONDITIONS["fw_tracking_m"]
    result = analysis._conditioned_variation(CELLS, values, fields, minimum, 1)
    assert [run_id for b in result["bins"] for run_id in b["outlier_run_ids"]] == [high]
    assert next(b for b in result["bins"] if high in b["valid_run_ids"])["residual_by_run_id"][high] == 8


def test_refuses_missing_runs():
    bound, report = analysis.evaluate(CELLS, [_run(CELLS[0])], analysis.load_airframe_profile(demo.PROFILE), INPUTS, [])
    assert bound is None and report["status"] == "refused"
    assert any("at least 16" in reason for reason in report["refusals"])


@pytest.mark.parametrize("missing", ["index", "empty_list", "missing_file", "empty_file"])
def test_analysis_reports_missing_ulog_and_allows_partial_cells(tmp_path, monkeypatch, missing):
    root = tmp_path / "matrix"
    root.mkdir()
    probe.json_file(root / "matrix.json", {**analysis.META, "inputs": INPUTS, "cells": [asdict(c) for c in CELLS]})
    monkeypatch.setattr(analysis, "load_local_dem_grid", lambda _: FlatTerrain())
    cell = CELLS[0]
    meta, _, _ = _fixture(cell)
    run_dir = root / cell.id
    episode = run_dir / "evidence"
    evidence = episode / "container-evidence"
    evidence.mkdir(parents=True)
    probe.json_file(run_dir / "probe.json", meta)
    probe.json_file(run_dir / "episode.json", {"episode": "evidence"})
    probe.json_file(episode / "episode.json", {"status": "complete", "simulator_image": probe.IMAGE,
                                               "px4_profile": "main"})
    if missing != "index":
        listing = [] if missing == "empty_list" else ["container-evidence/flight.ulg"]
        probe.json_file(evidence / "index.json", {"ulog_files": listing})
    if missing == "empty_file":
        (evidence / "flight.ulg").touch()
    # This directory exists but was not properly captured; partial mode must not hide it.
    (root / CELLS[1].id).mkdir()

    strict_output = tmp_path / "strict"
    assert analysis.analyze(root, strict_output, "probe") == 2
    strict = demo.read_object(strict_output / "probe.validation.json")
    assert strict["refusals"][0] == f"{cell.id}: ulog_missing"
    assert strict["refusals"][1:-1] == [f"{c.id}: probe_missing" for c in CELLS[1:]]
    assert not (strict_output / "probe.json").exists()

    partial_output = tmp_path / "partial"
    assert analysis.main(["--mode", "sim", "--matrix", str(root), "--output", str(partial_output),
                          "--name", "probe", "--allow-partial"]) == 2
    partial = demo.read_object(partial_output / "probe.validation.json")
    assert partial["refusals"] == [f"{cell.id}: ulog_missing", f"{CELLS[1].id}: probe_missing",
                                   strict["refusals"][-1]]
    assert partial["skipped_unflown_cells"] == [c.id for c in CELLS[2:]]
    assert partial["status"] == "refused" and not (partial_output / "probe.json").exists()


def test_probe_copies_same_prearm_ulog_after_disarm(tmp_path, monkeypatch):
    events = []
    source = PurePosixPath("/opt/px4-gazebo/fs/log/2026-11-26/10_48_34.ulg")
    _, _, ulog_bytes = _fixture(CELLS[0])
    readback = {"verified": True}
    scenario = probe._Probe("sim", cell=CELLS[0], faults=(), terrain=FlatTerrain(), hold=None, params=None,
                            guard=None, episode_root=tmp_path)
    scenario.docker, scenario.container, scenario.episode_dir = "docker", "owned-sim", tmp_path / "episode"
    scenario._readback = lambda: readback
    scenario.record = lambda row: events.append(row["message_type"])
    scenario.check = lambda: None
    scenario.clock = lambda: 1000
    scenario.native = SimpleNamespace(poll=lambda: "confirmed", confirmed=lambda action: action == "return",
                                      landed=lambda: True, history=[])
    scenario._sample = lambda: ({"base_mode": 0}, {}, None, 1000)
    scenario.rtl_sent = True

    def command(*argv):
        events.append(argv)
        if argv[0] == "find":
            assert argv[1] == str(source.parent.parent)
            return str(source) + "\n"
        return ""

    def copy(argv, **_):
        assert argv[:3] == ["docker", "cp", f"owned-sim:{source}"]
        assert Path(argv[-1]).suffix == ".partial"
        events.append("copy")
        Path(argv[-1]).write_bytes(ulog_bytes)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    scenario.transport = SimpleNamespace(prepare=lambda _: None, command=command)
    monkeypatch.setattr(probe.subprocess, "run", copy)
    scenario.prepare()
    assert scenario.finish_native(1, check=lambda: None)["disarmed"] is True
    assert events == ["scenario_fw_rtl_readback", (probe.LOGGER, "on"),
                      ("find", str(probe.ULOG_ROOT), "-type", "f", "-name", "*.ulg", "-size", "+0c", "-print"),
                      "fw_rtl_probe_disarmed", (probe.LOGGER, "stop"), "copy", "fw_rtl_probe_ulog_copied"]
    archive = scenario.episode_dir / "container-evidence/px4-ulog"
    assert list(archive.iterdir()) == [archive / source.name]
    assert (archive / source.name).read_bytes() == ulog_bytes


def test_failed_ulog_copy_does_not_publish_partial_bytes(tmp_path, monkeypatch):
    def copy(argv, **_):
        Path(argv[-1]).write_bytes(b"partial")
        return SimpleNamespace(returncode=1, stdout="", stderr="source vanished")

    monkeypatch.setattr(probe.subprocess, "run", copy)
    with pytest.raises(RuntimeError, match="source vanished"):
        probe._copy_ulog("docker", "owned-sim", probe.ULOG_ROOT / "flight.ulg", tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_prearm_settings_pin_descend_alt_and_reject_legacy_readback():
    cell = CELLS[0]
    params, settings = probe._probe_parameters(cell)
    assert params["RTL_DESCEND_ALT"] == 73 and dict(settings)["RTL_DESCEND_ALT"] == "73"
    assert "RTL_RETURN_ALT" not in dict(settings)
    instance = object.__new__(probe._Probe)
    instance.params = params
    instance.transport = SimpleNamespace(command=lambda *_: f"PX4 git-hash: {probe.PX4_RTL_COMMIT}\n")
    for altitude, expected in ((30, False), (73, True)):
        actual = {**PARAMS, **{k: v for k, v in params.items() if v is not None}, "RTL_DESCEND_ALT": altitude}
        instance.transport._get_param = actual.__getitem__
        assert instance._readback()["verified"] is expected
    with pytest.raises(ValueError, match="RTL_DESCEND_ALT readback 30 differs from prearm pin 73 m"):
        _run(cell, descend_m=30)


def test_missing_non_power_measurement_refuses_even_with_assumed_power():
    runs = [_run(c, current=-1) for c in CELLS]
    del runs[0]["observations"]["max_descent_mps"]
    bound, report = analysis.evaluate(CELLS, runs, analysis.load_airframe_profile(demo.PROFILE), INPUTS, [])
    assert bound is None and report["refusals"] == ["max_descent_mps: missing valid measurement"]
    assert report["derivations"]["hover_peak_power_w"]["basis"] == "ASSUMED_SIM"


def test_refuses_outlier_and_missing_current():
    runs = [_run(c, current=-1 if i == 1 else 12, tracking_outlier=25 if i == 0 else 0) for i, c in enumerate(CELLS)]
    bound, report = analysis.evaluate(CELLS, runs, analysis.load_airframe_profile(demo.PROFILE), INPUTS, [])
    assert bound is None and report["status"] == "refused"
    assert "fw_peak_power_w: missing valid electrical samples; SOC cannot identify peak power" in report["refusals"]
    assert "move_to_land_vertical_m: excessive variation/outlier, see run IDs" in report["refusals"]
    assert report["observations"][CELLS[1].id]["energy_used_wh_from_soc"] > 0


@pytest.mark.parametrize("kwargs, error", [({"clock_error": -5000}, "dispatch outside probe clock contract"),
                                          ({"invalid_state": True}, "state sequence")])
def test_refuses_clock_or_state_mismatch(kwargs, error):
    with pytest.raises(ValueError, match=error):
        _run(CELLS[0], **kwargs)


@pytest.mark.parametrize("mode", ["real", "hil", "sitl", None])
def test_non_sim_refused_before_output_or_launch(mode, tmp_path):
    output = tmp_path / "blocked"
    args = SimpleNamespace(mode=mode, output=output)
    with pytest.raises(ValueError, match="explicit --mode sim"):
        probe.run(args, launch=lambda *_: pytest.fail("launcher was called"))
    with pytest.raises(ValueError, match="explicit --mode sim"):
        probe._Probe(mode, cell=None, faults=(), terrain=None, hold=None, params=None, guard=None, episode_root=None)
    assert not output.exists()


def test_normal_demo_cannot_select_probe(monkeypatch, tmp_path):
    monkeypatch.setenv("MULTIDETECT_X1_FW_RTL_PROBE", "1")
    with pytest.raises(SystemExit) as error:
        demo.main(["--fw-rtl-probe", "--mode", "sim", "--output", str(tmp_path / "blocked")])
    assert error.value.code == 2
    assert not (tmp_path / "blocked").exists()
    assert "measure_x1_fw_rtl" not in (demo.ROOT / "scripts/run_x1_autonomy_demo.py").read_text()


def test_probe_matrix_resume_preserves_completed_cells(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_text("synthetic input", encoding="utf-8")
    output = tmp_path / "matrix"
    monkeypatch.setattr(demo, "load_site", lambda *_: SimpleNamespace(world_sha256=demo.assumptions.WORLD_SHA,
                                                                      terrain=FlatTerrain()))
    receipt = tmp_path / "receipt.json"
    probe.json_file(receipt, {**analysis.META, "status": "static_verified"})
    monkeypatch.setattr(probe, "_prepare", lambda *_: (None, None, None, None, {}))
    args = SimpleNamespace(mode="sim", offsets_s=[30, 39, 48, 57], winds_mps=[0, 4], drains_s=[600, 1800],
                           altitudes_agl_m=[85, 95], repeats=1, wind_toward_deg=0, output=output,
                           prepare_only=False, resume=False, cell=CELLS[0].id, image_check=receipt,
                           mission_package=source, recorded_plan=source, wind_plugin=source)
    started = []

    def fake_launch(_args, _plan, _steps, _mission, _probe, out):
        started.append(out.name)
        episode = out / "finished"
        episode.mkdir()
        probe.json_file(episode / "episode.json", {"status": "complete"})
        return episode

    probe.run(args, launch=fake_launch)
    assert started == [CELLS[0].id]
    args.resume, args.cell = True, None
    probe.run(args, launch=fake_launch)
    assert started == [cell.id for cell in CELLS]
    args.wind_toward_deg = 1
    with pytest.raises(ValueError, match="identical matrix"):
        probe.run(args, launch=lambda *_: pytest.fail("launcher was called"))
