#!/usr/bin/env python3
"""Diagnostic replay only; cannot produce or register an X1 motion proof."""
from __future__ import annotations

import argparse
import csv
from functools import cache
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
import struct

import numpy as np

from multidetect.autonomy.x1_mission import _terrain_bounds
from multidetect.terrain_dem import load_local_dem_grid
from scripts.analyze_px4_ulog import MAGIC, _field_parts, _type_info

PHASES = ('fw_climb', 'fw_connector', 'fw_descent', 'fw_to_land', 'transition', 'mc', 'landing')
REF = Path('/data/scratch/cx-wc-sampling')
ANALYSIS = Path('/data/projects/Multi-Detect-env/mdwt/cx-x1-fw-analyze/.tmp')
MATRIX = REF / '.tmp/x1-fw-rtl.matrix1/matrix'
TOPICS = {
    'vehicle_global_position': ('timestamp', 'lat', 'lon', 'alt', 'dead_reckoning'),
    'vehicle_local_position': ('timestamp', 'vx', 'vy', 'vz'),
}


def read(path):
    return json.loads(path.read_bytes())


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


@cache
def decoder(fmt, wanted):
    offset = 0
    fields = {}
    for spec in fmt.split(';'):
        kind, name = _field_parts(spec)
        if not kind:
            continue
        _, count, code = _type_info(kind)
        item = struct.Struct(code)
        if name in wanted:
            if count != 1:
                raise ValueError(f'unsupported field {name}')
            fields[name] = (item, offset)
        offset += item.size * count
    if set(fields) != set(wanted):
        raise ValueError(f'missing fields in {fmt}')
    return tuple(fields[key] for key in wanted)


def read_positions(raw, start, end):
    """Extract only the two spatial topics (instance zero), noting every dropout."""
    if len(raw) < 16 or raw[:7] != MAGIC:
        raise ValueError('invalid ULog header')
    formats, subscriptions, dropout = {}, {}, []
    rows = {key: [] for key in TOPICS}
    pos = 16
    while pos < len(raw):
        size, kind = struct.unpack_from('<HB', raw, pos)
        p = pos + 3
        stop = p + size
        if stop > len(raw):
            raise ValueError(f'truncated ULog at {pos}')
        if kind == ord('F'):
            name, fmt = raw[p:stop].decode().split(':', 1)
            if name in TOPICS:
                formats[name] = decoder(fmt, TOPICS[name])
        elif kind == ord('A'):
            instance, message = struct.unpack_from('<BH', raw, p)
            name = raw[p + 3:stop].decode()
            if instance == 0 and name in TOPICS:
                subscriptions[message] = name
        elif kind == ord('R'):
            subscriptions.pop(struct.unpack_from('<H', raw, p)[0], None)
        elif kind == ord('D'):
            name = subscriptions.get(struct.unpack_from('<H', raw, p)[0])
            if name is not None:
                fields = formats[name]
                stamp = fields[0][0].unpack_from(raw, p + 2 + fields[0][1])[0]
                if start - 350_000 <= stamp <= end + 350_000:
                    rows[name].append((stamp, *(s.unpack_from(raw, p + 2 + k)[0] for s, k in fields[1:])))
        elif kind == ord('O'):
            dropout.append([pos, struct.unpack_from('<H', raw, p)[0]])
        pos = stop
    for name, values in rows.items():
        if not values or not all(a[0] <= b[0] for a, b in zip(values, values[1:])):
            raise ValueError(f'missing or nonmonotonic {name}')
    return rows, dropout


def interpolate(points, times):
    return np.column_stack([np.interp(times, points[:, 0], points[:, i]) for i in range(1, 4)])


def phase_trace(points, start, end):
    if not points[0, 0] <= start < end <= points[-1, 0]:
        raise ValueError('window not bracketed')
    raw = points[(points[:, 0] >= start) & (points[:, 0] <= end)]
    inside = points[(points[:, 0] > start) & (points[:, 0] < end)]
    bounds = np.column_stack(([start, end], interpolate(points, [start, end])))
    return raw, np.vstack((bounds[:1], inside, bounds[1:]))


def extract(out):
    old = read(ANALYSIS / 'x1-fw-sidecar-proof-30.json')
    obs = read(ANALYSIS / 'x1-fw-sidecar-incremental30-input.json')['observations']
    diagnostics = read(ANALYSIS / 'x1-fw-matrix-diagnostics.json')
    with (ANALYSIS / 'x1-fw-matrix-analysis.csv').open(newline='') as stream:
        prior = list(csv.DictReader(stream))
    assert len(prior) == len({r['run_id'] for r in prior}) == len(read(MATRIX / 'matrix.json')['cells']) == 32
    assert {r['run_id'] for r in prior if r['analyzer_admitted'] == 'True'} == set(obs)
    dem_path, dem_hash = next((k, v) for k, v in old['source_paths'].items() if k.endswith('.dem.json'))
    dem = load_local_dem_grid(dem_path)
    assert dem.source_sha256 == dem_hash
    runs = {}
    for row in prior:
        name = row['run_id']
        if name in obs:
            run = obs[name]
            assert run['evidence']['ulog'] == row['ulog_path']
        else:
            run = diagnostics[name]
            assert run['ulog_path'] == row['ulog_path']
        boundaries = run['phase_starts_us']
        ulog = Path(row['ulog_path'])
        assert ulog.resolve().is_relative_to((MATRIX / name).resolve())
        raw = ulog.read_bytes()
        digest = sha256(raw).hexdigest()
        if name in obs:
            assert digest == run['evidence']['ulog_sha256']
        topics, dropout = read_positions(raw, boundaries[0], boundaries[-1])
        assert bool(dropout) == (name not in obs)
        assert len(boundaries) == 8 and run['state_sequence'] == [2, 3, 4, 5, 6, 7, 8]
        points = np.array([(t, *dem.local_from_wgs84(lat, lon), z)
                           for t, lat, lon, z, dead in topics['vehicle_global_position'] if not dead])
        start, end = boundaries[0], boundaries[-1]
        relevant = points[(points[:, 0] >= start - 350_000) & (points[:, 0] <= end + 350_000)]
        assert np.isfinite(relevant).all()
        runs[name] = dict(ulog=str(ulog), sha256=digest, dropout=dropout, boundaries=boundaries,
                          home=run['home'], entry=run['start'], points=relevant.tolist(),
                          velocities=topics['vehicle_local_position'])
        print(f'{name}: {len(relevant)} positions; {len(dropout)} dropout records', flush=True)
    with (out / 'x1-fw-envelope-traces.json.gz').open('wb') as stream:
        with gzip.GzipFile(fileobj=stream, mode='wb', mtime=0, filename='') as packed:
            packed.write(json.dumps(runs, separators=(',', ':'), allow_nan=False).encode())



def capsule_error(points, a, b):
    ab = b - a
    squared = ab @ ab
    t = np.clip((points - a) @ ab / squared, 0, 1) if squared else 0
    offset = points - a - np.multiply.outer(t, ab) if squared else points - a
    return float(np.linalg.norm(offset, axis=1).max())


def upper(value, floor=1.0):
    return math.ceil(1000 * (value + max(floor, 0.2 * value))) / 1000


def rectangle(ne_min, ne_max):
    return (ne_min[0], ne_max[0], ne_min[1], ne_max[1])


def corridor_margin(box, lane):
    return min(box[0] - lane['north_min_m'], lane['north_max_m'] - box[1],
               box[2] - lane['east_min_m'], lane['east_max_m'] - box[3])


def sample_extrema(samples, dem, lane, error, radius):
    box = rectangle(samples[:, :2].min(axis=0), samples[:, :2].max(axis=0))
    agl = [z - dem.sample_local(n, e).elevation_m for n, e, z in samples]
    return dict(n_min=box[0], n_max=box[1], e_min=box[2], e_max=box[3],
                z_min=float(samples[:, 2].min()), z_max=float(samples[:, 2].max()),
                corridor=corridor_margin(box, lane),
                radius=radius - float(np.linalg.norm(samples[:, :2], axis=1).max()),
                agl_low=min(agl) - error, agl_high=max(agl) + error)


def drift_frame(v):
    speed = np.linalg.norm(v)
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError("missing transition heading")
    direction = v / speed
    return np.array([direction, [-direction[1], direction[0]]])


def ellipse(anchor, heading, extents):
    """Ellipse contains the entire forward/cross rectangle, not just the sample covariance."""
    mid = np.mean(extents, axis=1)
    axes = np.ptp(extents, axis=1) / math.sqrt(2)
    centre = anchor + mid @ heading
    ne_support = np.linalg.norm(axes[:, None] * heading, axis=0)
    return rectangle(centre - ne_support, centre + ne_support), float(np.linalg.norm(centre) + max(axes))


def stadium(a, b, width):
    return rectangle(np.minimum(a, b) - width, np.maximum(a, b) + width), max(np.linalg.norm(a), np.linalg.norm(b)) + width


def phase_model(i, trace, anchor, home, heading, datum, bounds):
    lo, hi = bounds['vertical'][i]
    nominal = datum if i < 2 else home[2] + 73
    points = trace[:, 1:3]
    if i == 2:
        # Full 80 m centreline: each arc lies within its chord plus sagitta.
        angles = np.linspace(0, 2 * np.pi, 73)
        circle = home[:2] + 80 * np.column_stack((np.cos(angles), np.sin(angles)))
        width = bounds['horizontal'][i] + 80 * (1 - math.cos(math.pi / 72))
        footprints = [stadium(a, b, width) for a, b in zip(circle, circle[1:])]
    elif i == 4:
        footprints = [ellipse(points[0], heading, bounds['drift'])]
    else:
        a, b = model_chord(i, points, anchor, home)
        # Split terrain coverage, not the path: exact union of these capsules.
        sub = np.linspace(a, b, max(2, math.ceil(math.dist(a, b) / 20) + 1))
        footprints = [stadium(x, y, bounds['horizontal'][i]) for x, y in zip(sub, sub[1:])]
    return footprints, nominal + lo, nominal + hi


def model_chord(i, points, anchor, home):
    if i == 0:
        return anchor[:2], anchor[:2]  # command slack only; CLIMBING not witnessed
    if i == 1:
        return anchor[:2], home[:2]  # deliberately coarse proxy for PX4 acquisition
    if i in (3, 5):
        return points[0], home[:2]  # retrospectively known handoff, NOT dispatch-known
    if i == 6:
        return np.zeros(2), np.zeros(2)  # surveyed pad, not movable PX4 Home
    raise ValueError(i)


def integrate(out):
    old = read(ANALYSIS / 'x1-fw-sidecar-proof-30.json')
    package_path = next(k for k in old['source_paths'] if k.endswith('/mission-package.json'))
    source = read(Path(package_path))['x1_live_evidence']
    for key, expected in old['source_paths'].items():
        actual = ANALYSIS / Path(key).name if not Path(key).is_absolute() else Path(key)
        assert sha256(actual.read_bytes()).hexdigest() == expected, key
    dem = load_local_dem_grid(next(k for k in old['source_paths'] if k.endswith('.dem.json')))
    assert old['dem_error'] == source['dem_error_m'] == 12
    assert old['mission_radius'] == 1200
    error, lane, radius = old['dem_error'], old['corridor'], old['mission_radius']
    with gzip.open(out / 'x1-fw-envelope-traces.json.gz', 'rt', encoding='utf-8') as stream:
        runs = json.load(stream)
    admit = set(old['rows'])
    rows, shapes = {}, {}
    for key, run in runs.items():
        points, anchor, home = (np.asarray(run[k]) for k in ('points', 'entry', 'home'))
        speed = np.asarray(run['velocities'])
        starts = run['boundaries']
        heading = drift_frame(speed[np.abs(speed[:, 0] - starts[4]).argmin(), 1:3])
        datum = max(anchor[2], home[2] + 60)
        measured, shapes[key] = [], []
        for i, (begin, end) in enumerate(zip(starts, starts[1:])):
            raw, trace = phase_trace(points, begin, end)
            xyz, coords = trace[:, 1:], trace[:, 1:3]
            nominal = datum if i < 2 else home[2] + 73
            samples = sample_extrema(raw[:, 1:], dem, lane, error, radius)
            gap = np.diff(trace[:, 0]).max() / 1e6
            samples.update(samples=len(raw), max_gap_s=float(gap), duration_s=(end - begin) / 1e6,
                           start=trace[0, 1:].tolist(), end=trace[-1, 1:].tolist())
            if i == 2:
                dist = float(np.abs(np.linalg.norm(coords - home[:2], axis=1) - 80).max())
            elif i == 4:
                displacement = (coords - coords[0]) @ heading.T
                dist = float(np.linalg.norm(coords - coords[0], axis=1).max())
                samples['drift'] = [[float(l), float(h)] for l, h in zip(displacement.min(axis=0), displacement.max(axis=0))]
                samples['net_drift_m'] = float(np.linalg.norm(coords[-1] - coords[0]))
            else:
                a, b = model_chord(i, coords, anchor, home)
                dist = capsule_error(coords, a, b)
            samples['horizontal_residual_m'] = dist
            vertical = xyz[:, 2] - nominal
            samples['vertical_relative_m'] = [float(vertical.min()), float(vertical.max())]
            if key in admit:
                if gap > 0.35:
                    raise ValueError(f'admitted trace has telemetry gap: {key} {PHASES[i]}')
                legacy = old['rows'][key]['legs'][PHASES[i]]
                box = legacy['box']
                width = legacy['width']
                a = np.array([box[0] + width, box[2] + width])
                b = np.array([box[1] - width, box[3] - width])
                # Determine the actual unpadded segment direction for a diagonal.
                if i == 0:
                    a, b = anchor[:2], np.array([320., 0.])
                same_box, far = stadium(a, b, width)
                dem_lo, dem_hi = _terrain_bounds(dem, same_box)
                samples['old'] = {name: legacy[name] for name in ('corridor', 'radius', 'agl_min', 'agl_max', 'width', 'vertical')}
                samples['old_disk_capsule'] = dict(corridor=corridor_margin(same_box, lane), radius=radius-far)
                tight_xy = (samples['n_min'], samples['n_max'], samples['e_min'], samples['e_max'])
                _, tight_hi = _terrain_bounds(dem, tight_xy)
                samples['old_vertical_at_sample_xy_agl_low'] = box[4] - tight_hi - error
                samples['observed_global_z_at_old_xy_agl_low'] = samples['z_min'] - dem_hi - error
            measured.append(samples)
            shapes[key].append((trace, anchor, home, heading, datum))
        rows[key] = dict(admitted=key in admit, dropout=run['dropout'], leg=dict(zip(PHASES, measured)))
        if key in admit:
            budget = old['rows'][key]
            rows[key]['budget'] = {k: budget[k] for k in ('entry_box', 'conditional_energy_wh', 'available_time_s', 'observed_soc_wh')}
            rows[key]['budget']['time_slack_s'] = budget['available_time_s'] - budget['proof']['required_time_s']
            rows[key]['budget']['energy_slack_wh'] = (budget['conditional_energy_wh'] -
                                                     budget['proof']['required_energy_wh'] - old['energy_reserve_wh'])
    assert sum(r['admitted'] for r in rows.values()) == 30
    bounds = dict(horizontal=[], vertical=[], drift=[])
    for i, name in enumerate(PHASES):
        clean = [r['leg'][name] for r in rows.values() if r['admitted']]
        bounds['horizontal'].append(upper(max(r['horizontal_residual_m'] for r in clean)))
        if i == 4:
            for j in range(2):
                bounds['drift'].append([-upper(max(0, -min(r['drift'][j][0] for r in clean))),
                                         upper(max(0, max(r['drift'][j][1] for r in clean)))])
        bounds['vertical'].append([min(0, min(r['vertical_relative_m'][0] for r in clean)),
                                   max(0, max(r['vertical_relative_m'][1] for r in clean))])
        # Pad uses a separate directional error at surveyed touchdown and at LAND entry.
        if i != 6:
            bounds['vertical'][-1] = [-upper(-bounds['vertical'][-1][0]), upper(bounds['vertical'][-1][1])]
    coverage, obstacles = source['obstacle_coverage'], np.array(source['obstacles'])
    pad = old['surveyed_pad']
    pad_radius = bounds['horizontal'][6]
    pad_low, pad_high = _terrain_bounds(dem, (-pad_radius, pad_radius, -pad_radius, pad_radius))
    starts_z = [shapes[k][6][0][0, 3] for k in admit]
    landing_top = max(rows[k]['leg']['landing']['z_max'] for k in admit) + 1
    land_bottom = pad['point'][2] - upper(max(0, pad['point'][2] - min(
        rows[k]['leg']['landing']['z_min'] for k in admit)))
    pad_check = dict(radius_margin=pad['radius_m'] - pad_radius,
                     relief_margin=min(pad_low - error - (pad['point'][2] - pad['elevation_error_m']),
                                       pad['point'][2] + pad['elevation_error_m'] - (pad_high + error)),
                     entry_clearance=min(starts_z) - 1 - pad_high - error,
                     contact_alt_margin=land_bottom - (pad['point'][2] - pad['elevation_error_m']))

    def margins(footprints, z1, z2, *, landing):
        values = []
        for box, far in footprints:
            d1, d2 = _terrain_bounds(dem, box)
            full_box = np.array([*box, z1, z2])
            lb = min(np.min(full_box[::2] - coverage[::2]), np.min(coverage[1::2] - full_box[1::2]))
            dist = np.linalg.norm(np.maximum(0, np.maximum(obstacles[:, ::2] - full_box[1::2],
                                                           full_box[::2] - obstacles[:, 1::2])), axis=1).min()
            values.append((corridor_margin(box, lane), radius - far, z1 - d2 - error,
                           z2 - d1 + error, min(lb, dist if dist > 0 else -1.0)))
        cols = list(zip(*values))
        area = old['area']
        return dict(corridor=min(cols[0]), radius=min(cols[1]), agl_low=min(cols[2]),
                    agl_high=max(cols[3]), obstacle=min(cols[4]),
                    altitude=min(area['max_altitude_m'] - (z2 - pad['point'][2]),
                                 math.inf if landing else (z1 - pad['point'][2]) - area['min_altitude_m']))

    for key, row in rows.items():
        for i, (name, measured) in enumerate(row['leg'].items()):
            shaped = shapes[key][i]
            if i == 6:
                # Non-flat vertical leg: bound pad relief and terminal contact separately.
                # Entry must start above the entire uncertain surface. Use joint LAND
                # start/end observations in the same surveyed frame, with +margin.
                f = [stadium(np.zeros(2), np.zeros(2), pad_radius)]
                z1, z2 = land_bottom, landing_top
                measured['landing_pad'] = pad_check
            else:
                f, z1, z2 = phase_model(i, *shaped, bounds)
            measured['envelope'] = margins(f, z1, z2, landing=i == 6)
            trace, _, _, heading, _ = shaped
            xyz = trace[:, 1:]
            if i == 4:
                extents = np.asarray(bounds['drift'])
                center, axes = np.mean(extents, axis=1), np.ptp(extents, axis=1) / math.sqrt(2)
                local = (xyz[:, :2] - xyz[0, :2]) @ heading.T
                inside_horizontal = bool(np.all(np.linalg.norm((local - center) / axes, axis=1) <= 1))
            else:
                inside_horizontal = measured['horizontal_residual_m'] <= bounds['horizontal'][i]
            measured['trace_contained'] = bool(inside_horizontal and xyz[:, 2].min() >= z1 and xyz[:, 2].max() <= z2)
            if key in admit and not measured['trace_contained']:
                raise ValueError(f'calibration envelope does not contain trace: {key} {name}')

    # Counterfactual transfer sensitivity only. Case C did not run a native RTL.
    # Only a dispatch fix and budget sample were observed. Future headings are
    # unknown, so pool maxima into disks rather than borrowing a probe heading.
    case = Path(package_path).parent
    case_meta = read(case / 'result.json')
    case_episode = case / 'flight/episodes/episode-0000-1a7e1caf'
    fix = battery = receipt = None
    with (case_episode / 'mavlink.jsonl').open() as stream:
        for line in stream:
            if not any(key in line for key in ('"GLOBAL_POSITION_INT"', '"BATTERY_STATUS"', '"scenario_decision_receipt"')):
                continue
            row = json.loads(line)
            kind = row['message_type']
            if kind == 'GLOBAL_POSITION_INT':
                fix = row
            elif kind == 'BATTERY_STATUS':
                battery = row
            elif row.get('action') == 'return':
                receipt = row
                break
    assert receipt['reason'] == 'unverified_clearance' and all(
        receipt['motion_proof'][key] is None for key in ('required_energy_wh', 'available_energy_wh',
                                                        'required_time_s', 'available_time_s'))
    assert (battery['energy_consumed'], battery['current_consumed']) == (-1, 0)
    assert 0 <= receipt['receive_monotonic_ns'] - battery['receive_monotonic_ns'] <= 500_000_000
    case_scope = case_meta['contingency_scenario']['motion_scope_sha256']
    assert case_scope == old['case_c_scope'] and case_scope != old['probe_scope']
    entry = np.array([*dem.local_from_wgs84(fix['lat'] / 1e7, fix['lon'] / 1e7), fix['alt'] / 1000])
    home_ne = np.zeros(2)
    observed_homes = np.array([runs[k]['home'] for k in admit])
    home_z_limits = (observed_homes[:, 2].min(), observed_homes[:, 2].max())
    home_ne_guard = np.linalg.norm(observed_homes[:, :2], axis=1).max()
    _, drift_rad = ellipse(np.zeros(2), np.eye(2), bounds['drift'])
    entry_rad = old['bounds']['fw_acceptance_m']
    disk = lambda point, radius: [stadium(point, point, radius)]
    case_legs = {}
    for i, name in enumerate(PHASES):
        lo, hi = bounds['vertical'][i]
        if i == 0:
            # No climb witnessed; only command-slack illustration here.
            footprint = disk(entry[:2], bounds['horizontal'][i])
        elif i == 1:
            footprint = [stadium(entry[:2], home_ne, bounds['horizontal'][i] + home_ne_guard)]
        elif i == 2:
            # Full ring would be smaller; disk covers uncertainty if any bearing is allowed.
            footprint = disk(home_ne, 80 + bounds['horizontal'][i] + home_ne_guard)
        elif i == 3:
            footprint = disk(home_ne, 80 + bounds['horizontal'][2] + bounds['horizontal'][i] + home_ne_guard)
        elif i == 4:
            footprint = disk(home_ne, entry_rad + drift_rad + home_ne_guard)
        elif i == 5:
            footprint = disk(home_ne, entry_rad + drift_rad + bounds['horizontal'][i] + home_ne_guard)
        else:
            footprint = disk(home_ne, pad_radius)
        z = (entry[2], entry[2]) if i < 2 else (home_z_limits[0] + 73, home_z_limits[1] + 73)
        z1, z2 = (land_bottom, landing_top) if i == 6 else (z[0] + lo, z[1] + hi)
        case_legs[name] = margins(footprint, z1, z2, landing=i == 6)
    ceil_time = sum(old['bounds'][name] for name in PHASES)
    required_wh = old['proof_numeric']['required_energy_wh']['min'][0]
    total_wh = required_wh + old['energy_reserve_wh']
    available_wh = 72 * (battery['battery_remaining'] - source['battery']['soc_error_pct']) / 100
    available_s = old['budget']['time_s'] - fix['time_boot_ms'] / 1000
    case_replay = dict(scope=case_scope, decision='rejected/unverified_clearance',
                       model='counterfactual transfer, unknown validity', entry=entry.tolist(),
                       home_z_limits=list(home_z_limits), drift_radius=drift_rad,
                       phase=case_legs, available_time_s=available_s, available_energy_wh=available_wh,
                       energy_slack_wh=available_wh-total_wh, time_slack_s=available_s-ceil_time)
    save(out / 'x1-fw-envelope-results.json', dict(bounds=bounds, rows=rows, case_c=case_replay,
                                                   input_sha256=old['source_paths']))
    with (out / 'x1-fw-envelope-per-cell.csv').open('w', newline='') as stream:
        flatten = lambda d: {f'{k}_{sub}': x for k, v in d.items()
                             for sub, x in (v.items() if isinstance(v, dict) else [('', v)])}
        records = [dict(run_id=key, phase=name, admitted=row['admitted'], **flatten(leg), **row.get('budget', {}))
                   for key, row in rows.items() for name, leg in row['leg'].items()]
        fields = list(dict.fromkeys(k for row in records for k in row))
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('out', type=Path)
    parser.add_argument('--extract', action='store_true')
    args = parser.parse_args()
    if args.extract:
        extract(args.out)
    integrate(args.out)
    print("Wrote 32 × 7 diagnostic segment rows and one counterfactual Case C replay; no proof registered.")


if __name__ == '__main__':
    main()
