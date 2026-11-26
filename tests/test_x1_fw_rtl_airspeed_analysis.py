"""CPU tests for the v2 collection/analysis contract, not a flight proof."""
import json
import struct
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import pytest

from scripts import analyze_x1_fw_rtl_probe as analysis
from scripts import measure_x1_fw_rtl as probe
from scripts.analyze_px4_ulog import MAGIC


def manifest():
    cells = probe.matrix([30, 39, 48, 57], [0, 6], [600, 1800], [75, 85], 1)
    inputs = {"airframe_sha256": analysis.digest(analysis.demo.PROFILE),
              "world_sha256": analysis.demo.assumptions.WORLD_SHA,
              "dem_sha256": analysis.demo.assumptions.DEM_SHA,
              "case_c_schedule_sha256": probe.CASE_C_SCHEDULE_SHA256}
    return {**analysis.META, "schema": probe.MATRIX_SCHEMA, "inputs": inputs,
            "coverage_minimum_replicates": probe.COVERAGE_MINIMUM_REPLICATES,
            "case_c_schedule": deepcopy(analysis.CASE_C_SCHEDULE), "cells": [asdict(c) for c in cells],
            "cell_parameter_pins": {c.id: analysis.expected_pins(c) for c in cells}}


def test_matrix_v2_contract():
    record = manifest()
    cells, pins = analysis.validate_matrix(record)
    assert pins == {c.id: probe._exact_pins(c) for c in cells}
    coverage = analysis.matrix_coverage(cells, [])
    assert coverage['planned_cells'] == 38
    assert coverage['estimated_minutes_no_retries'] == 236
    assert [coverage['families'][k]['planned'] for k in ('core', 'climb', 'alternate-loiter', 'case-c')] == [32, 2, 2, 2]
    assert [c.id for c in cells[32:]] == [
        f'{f}-{o}-w0-b1800-a85-r{r}'
        for f, o in [('climb', 'o39'), ('alternate-loiter', 'o0'), ('case-c', 'o167')] for r in (1, 2)]
    assert {c.family: pins[c.id]['RTL_RETURN_ALT'] for c in cells} == {
        'core': 60, 'climb': 100, 'alternate-loiter': 100, 'case-c': 60}


@pytest.mark.parametrize('change', ['legacy', 'omit_replica', 'omit_pin', 'wrong_pin', 'wrong_wind'])
def test_matrix_rejects_scope_or_config_drift(change):
    record = manifest()
    pins = next(iter(record['cell_parameter_pins'].values()))
    if change == 'legacy':
        record.pop('schema')
    elif change == 'omit_replica':
        record['cells'].pop()
    elif change == 'omit_pin':
        pins.pop('FW_AIRSPD_MAX')
    elif change == 'wrong_pin':
        pins['RTL_RETURN_ALT'] = 100
    else:
        record['case_c_schedule']['faults'][0]['values']['ramp_ms'] = 3900
    with pytest.raises(ValueError):
        analysis.validate_matrix(record)


def test_true_speed_checks_every_sample_not_mean_or_eas():
    one_ulp = analysis.TAS_TOLERANCE_MPS
    rows = [{'true_airspeed_m_s': tas, 'airspeed_source': 1, 'calibrated_airspeed_m_s': 15}
            for tas in [12, 18, 18 + one_ulp, 12]]
    assert max(analysis.check_airspeed(rows, 18)[0]) == 18 + one_ulp
    rows[2]['true_airspeed_m_s'] = 18 + 2 * one_ulp
    with pytest.raises(ValueError, match='exceeds profile ceiling'):
        analysis.check_airspeed(rows, 18)
    rows[2]['true_airspeed_m_s'] = float('nan')
    with pytest.raises(ValueError, match='nonfinite'):
        analysis.check_airspeed(rows, 18)


def test_coverage_orbits_require_the_matching_center_and_radius():
    cells, _ = analysis.validate_matrix(manifest())
    hold = {'centre': [10, 20, 50], 'radius_m': 80}
    activation = (90, 20, 50)
    params = {'NAV_LOITER_RAD': 65}
    for c, centre, radius, label in [(cells[32], [10, 20, 65], -80, 'established_hold'),
                                      (cells[34], [90, 20, 65], 65, 'activation_point')]:
        assert analysis.check_climb_orbit(c, centre, radius, 65, activation, hold, params) == label
        with pytest.raises(ValueError, match='unexpected climb orbit'):
            analysis.check_climb_orbit(c, centre, radius + 2, 65, activation, hold, params)
        with pytest.raises(ValueError, match='unexpected climb orbit'):
            analysis.check_climb_orbit(c, [100-centre[0], 20, 65], radius, 65, activation, hold, params)


def test_case_c_exact_window_and_witnesses():
    origin = 100_000
    wind = dict(ramp_ms=4000, target_enu_mps=[-4, 8, 0], baseline_enu_mps=[0, 0, 0],
                readback='publish_only', enu_mps=[0, 0, 0])
    common = dict(origin_boot_ms=origin, values=analysis.CASE_C_WIND, fault='wind_step', fault_index=0, applied=wind)
    markers = {'scenario_clock_started': {'origin_boot_ms': origin},
               'scenario_injected': dict(common, scheduled_offset_ms=21000, observed_boot_ms=origin+21100),
               'scenario_restored': dict(common, scheduled_offset_ms=30500, observed_boot_ms=origin+30600,
                                         reason='window_end')}
    publishes = [dict(boot_ms=origin+t, fraction=f, enu_mps=[-4*f, 8*f, 0], readback='publish_only')
                 for t, f in [(21020, 0), (23020, .5), (25020, 1), (29020, .375)]]
    rtl = {'clock_ms': origin + 193_000}
    analysis.case_c_witness(markers.__getitem__, {'boot_ms': origin + 25440}, rtl, publishes)
    for stamp in [25439, 30500]:
        with pytest.raises(ValueError, match='outside archived wind window'):
            analysis.case_c_witness(markers.__getitem__, {'boot_ms': origin + stamp}, rtl, publishes)
    with pytest.raises(ValueError, match='ramp'):
        analysis.case_c_witness(markers.__getitem__, {'boot_ms': origin + 25440}, rtl, publishes[:-1])


def test_analyze_dropout_outputs_reflight_and_no_bounds(tmp_path, monkeypatch):
    record = manifest()
    cells, pins = analysis.validate_matrix(record)
    cell = cells[0]
    root, out = tmp_path / 'matrix', tmp_path / 'out'
    run = root / cell.id
    evidence = run / 'episode/container-evidence'
    evidence.mkdir(parents=True)
    (root / 'matrix.json').write_text(json.dumps(record))
    (run / 'probe.json').write_text(json.dumps({**analysis.META, 'cell': asdict(cell), 'run_id': cell.id,
                                               'inputs': record['inputs'], 'parameter_pins': pins[cell.id],
                                               'scenario': analysis.demo.assumptions.WORLD, 'dem': 'unused'}))
    (run / 'episode.json').write_text('{"episode":"episode"}')
    (evidence.parent / 'episode.json').write_text(json.dumps({'status': 'complete', 'simulator_image': probe.IMAGE,
                                                             'px4_profile': 'main'}))
    (evidence / 'index.json').write_text('{"ulog_files":["container-evidence/a.ulg"]}')
    (evidence / 'a.ulg').write_bytes(MAGIC + b'\x01' + b'\x00'*8 + struct.pack('<HcH', 2, b'O', 0))
    # A dropout remains actionable even if MAVLink evidence is also missing.
    monkeypatch.setattr(analysis, 'load_local_dem_grid', lambda _: type('Dem', (), {
        'source_sha256': record['inputs']['dem_sha256']})())
    assert analysis.analyze(root, out, 'probe', allow_partial=True) == 2
    report = json.loads((out / 'probe.validation.json').read_text())
    assert report['observations'] == {}
    assert report['reflight_cells'][cell.id]['reason'] == 'ulog_dropout'
    assert len(report['skipped_unflown_cells']) == 37
    assert not (out / 'probe.json').exists()
