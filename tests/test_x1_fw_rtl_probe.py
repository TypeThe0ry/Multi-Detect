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


PARAMS = {**probe._REQUIRED, "RTL_RETURN_ALT": 80, "RTL_DESCEND_ALT": 66, "RTL_LOITER_RAD": 80,
          "NAV_LOITER_RAD": 80, "VT_B_TRANS_DUR": 2, "VT_B_DEC_MSS": 2, "FW_R_LIM": 35}
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


def _fixture(cell, *, current=12, clock_error=0, invalid_state=False, tracking_outlier=0):
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
    params = {**PARAMS, "SIM_BAT_DRAIN": 1800, "SIM_BAT_MIN_PCT": 50,
              "SIM_BAT1_DRAIN": cell.drain_s, "SIM_BAT1_MIN_PCT": 50}
    readback = dict(verified=True, observed_px4_commits=[probe.PX4_RTL_COMMIT], observed_parameters=params, basis=probe.BASIS)

    def marker(kind, **values):
        return {"message_type": kind, **values}

    hold_ms = entry // 1000 - cell.offset_s * 1000
    mav = [marker("scenario_fw_rtl_readback", **readback), marker("fw_rtl_probe_hold_request"),
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
                           altitude=elevation if state == 2 else 300 if state == 8 else 366,
                           loiter_radius=80, altitude_is_relative=0))
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
        alt = elevation if t < 2_000_000 else 366 if t < 8_400_000 else 300
        if 3_000_000 <= t < 3_500_000:
            alt -= tracking_outlier
        rows = [stamped(stamp, nav_state=5 if stamp >= rtl else 3, nav_state_timestamp=rtl,
                        arming_state=2 if 0 <= t < 10_000_000 else 1, vehicle_type=2 if t < 7_000_000 else 1,
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


def test_analyze_serialized_matrix_writes_exact_bound(tmp_path, monkeypatch):
    root = tmp_path / "matrix"
    root.mkdir()
    probe.json_file(root / "matrix.json", {**analysis.META, "inputs": INPUTS, "cells": [asdict(c) for c in CELLS]})
    monkeypatch.setattr(analysis, "load_local_dem_grid", lambda _: FlatTerrain())
    for cell in CELLS:
        meta, mav, raw = _fixture(cell)
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
    assert bound.fw_peak_power_w == 216
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
