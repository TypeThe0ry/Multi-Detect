# Onboard ground projection audit and DEM support

步间多视角三角化与地面不确定度融合（默认关闭）的配置、用途和离线证据见 [中文说明](onboard-step-triangulation.md)。

Audited and replayed on 2026-10-15. All measurements below use offline CPU geometry;
no simulator, aircraft, detector, VLM, GPU, Docker, or downloads were used.

## Current assumptions

References are to the implementation in this change.

| Question | Current behavior | Evidence |
|---|---|---|
| Which detection pixel? | Bottom centre: `((x1+x2)/2, y2)`. This approximates object/terrain contact; a box is not a surveyed ground-contact pixel. | `src/multidetect/autonomy/onboard_loop.py:366`, `:834` |
| Which ground? | Default plane is local-up 0, equivalently local-NED z=0. | `src/multidetect/simulator/camera_projection.py:153`, `:180` |
| Is that home altitude? | Only when home ground and the PX4 estimator origin happen to coincide. There is no home-ground calibration in this projection path. | `src/multidetect/sim_teacher/streams.py:715`, `:795`; repository warning in `src/multidetect/pixhawk.py:1622` |
| Which vehicle altitude? | Raw `LOCAL_POSITION_NED.z`, negated to up; camera lever arm is added. `pose.alt_m` separately records `GLOBAL_POSITION_INT.relative_alt/1000`, but is not used for this projection. | `src/multidetect/sim_teacher/streams.py:715`, `:798`; `src/multidetect/simulator/camera_projection.py:68`, `:130` |
| Which attitude? | MAVLink `ATTITUDE` roll/pitch/yaw, radians converted to degrees. Full inverse rotates the mount and ray using all three axes. Missing attitude fields in the projector silently become zero. | `src/multidetect/sim_teacher/streams.py:801`; `src/multidetect/simulator/camera_projection.py:84`, `:169` |
| Which camera pitch? | Fixed mount pitch 0.18 rad (10.313 degrees), plus measured body attitude. Defaults are not a gimbal attitude or inferred from terrain. | `src/multidetect/simulator/camera_projection.py:20`, `:154`, `:170` |
| Camera location? | 0.18 m forward and 0.12 m above vehicle origin; rotated with body attitude. | `src/multidetect/simulator/camera_projection.py:130`, `:138` |
| `CameraModel` equivalent? | Standalone fallback is a level-body approximation: fixed downward pitch and yaw only, no roll/body pitch or lever arm. `next_action` passes `max(0,-pose_ned[2])`. Policy supplies the full-attitude callback instead. | `src/multidetect/autonomy/onboard_loop.py:50`, `:65`, `:754`; `scripts/onboard_policy.py:440` |
| Missing/parallel rays? | Rays with down <= 1e-6, invalid pose, or camera on/below the flat plane return `None`. | `src/multidetect/simulator/camera_projection.py:164`, `:181` |
| World knowledge? | Existing policy loads obstacles from the supplied SIM SDF. It does not automatically load `simulation/maps/*.geojson` or a DEM. | `scripts/onboard_policy.py:192`; `src/multidetect/sim_teacher/policies.py:63` |

The separate Pixhawk adapter learns a disarmed takeoff ground offset
(`src/multidetect/pixhawk.py:1622-1640`). That does **not** mean this observation
pipeline uses its calibrated altitude. Real flight needs a registered local
reference or a valid terrain measurement. Neither a DEM nor obstacle truth can be
assumed available merely because SIM has matching files.

## Analytic error

Let camera elevation above the assumed local-zero plane be H, true target terrain
elevation be delta_h, and the true ray depression below horizontal be alpha.
Assume the vertical plane containing the ray and H > delta_h.

```text
flat estimated range = H cot(alpha)
true horizontal range = (H - delta_h) cot(alpha)
signed flat bias = delta_h cot(alpha)
general N/E bias = delta_h * (ray_north, ray_east) / ray_down
```

If assumed depression is alpha + delta (delta in radians):

```text
combined signed error = H cot(alpha + delta) - (H - delta_h) cot(alpha)
pitch-only first-order error = -H csc(alpha)^2 * delta
height-bias / true range = delta_h / (H - delta_h)
```

Positive terrain offset projects too far; excessive assumed depression projects
too near. Positive body pitch is nose-up and reduces depression for the central
ray; its sign is opposite the mount/depression error used here. Away from the
vertical plane, roll, pitch and pixel bearing couple: evaluate the full rotated
ray, not only this scalar approximation. As alpha approaches zero, sensitivity
diverges. DEM alone cannot fix pitch calibration, timing, box clipping, occlusion,
object extent, lens distortion, camera extrinsics, or map registration.

At H=20 m, one error source at a time (signed along the horizontal ray):

| Depression | Nominal range (m) | Terrain +5 m (m) | Depression +1 degree (m) | Depression +5 degrees (m) |
|---:|---:|---:|---:|---:|
| 10 degrees | 113.426 | +28.356 | -10.535 | -38.785 |
| 20 degrees | 54.950 | +13.737 | -2.848 | -12.059 |
| 45 degrees | 20.000 | +5.000 | -0.686 | -3.218 |
| 60 degrees | 11.547 | +2.887 | -0.461 | -2.221 |

## Configuring a surface

`GroundModel.intersect_ray(origin_neu, ray_ned)` uses metres in local N/E/up
for the origin and a non-normalized N/E/down ray. Returns the first ground N/E hit
or `None`. Implementations live in `src/multidetect/autonomy/ground_model.py`:

- `FlatGround(ground_up_m=0)` implements the existing horizontal plane.
- `DemGround(grid, reference_elevation_m=..., max_range_m=10000, fallback=None)`
  accepts the existing validated `LocalDemGrid`. It marches cell boundaries of
  its bilinear grid, splits at any within-cell clearance extremum and bisects the
  first crossing. A ridge between two above-terrain endpoints is detected.
- Missing `ground_model` leaves the original projection arithmetic intact.
  Full-attitude projector and level-camera API both accept `ground_model=`.

Example fragment for the existing onboard policy JSON (`--onboard-config`):

```json
{
  "ground_model": {
    "type": "dem",
    "path": "simulation/terrain/gen_v2_w0035.dem.json",
    "reference_elevation_m": 18.0,
    "max_range_m": 10000.0,
    "fallback": "none",
    "use_agl": false,
    "agl_max_age_s": 1.0
  }
}
```

Paths resolve relative to the repository root today. A future mission-package
loader can supply its own `base_dir` to `ground_projection_from_config`, or an
already-loaded grid directly to `DemGround`; there is no implicit world lookup.
Invalid/missing configured DEM files fail during policy construction, before
model startup. The strict existing DEM schema and loader are reused unchanged.

The grid's N/E origin must coincide with PX4 local NED (0,0). Regrid/register a
mission DEM before use; this interface does not infer horizontal registration.
`reference_elevation_m` is the **MSL altitude of local NED z=0**, not necessarily
home MSL. It defaults to `grid.origin_elevation_m` for the existing SIM grids.
Terrain local up = DEM MSL elevation minus that reference. Thus constant 18 m MSL
with origin 18 m means local ground 0, not +18 m.

Uncovered camera origins, rays leaving coverage before intersection, and range
limits return `None`; explicitly choose `"fallback": "flat"` and optionally
`"ground_up_m": 0` to use a known local plane on a DEM miss. Parallel/upward or
nonfinite rays return `None` even with fallback. No raster edge is extrapolated.

## Optional AGL

Set `"ground_model": {"type": "flat", "use_agl": true}` or enable `use_agl`
on a DEM config. The collector then includes optional normalized `ground_agl`
telemetry. Fresh valid AGL takes precedence and defines a local horizontal plane:

```text
ground_up_m = -vehicle_local_ned_z - vertical_agl_m
```

The camera lever arm remains applied by the camera projector, not counted twice.
Missing, stale, future-dated, nonfinite or nonpositive AGL falls back to the
configured DEM/plane. A rangefinder measures near the aircraft at its beam
footprint; on a slope, verticalized slant range can differ from nadir AGL. It
does not describe an uphill target tens of metres away. The option chooses
an AGL plane, rather than silently shifting a registered DEM.

MAVLink semantics follow the official common message definitions:
<https://mavlink.io/en/messages/common.html#DISTANCE_SENSOR> and
<https://mavlink.io/en/messages/common.html#ALTITUDE>.

- `DISTANCE_SENSOR`: body-down orientation 25, centimetres converted to metres,
  min/max and invalid quality checked. Vertical AGL = slant distance times
  cos(roll) cos(pitch); a custom mount is not accepted as body down. Tilt
  compensation requires fresh attitude within 0.25 s of the range sample
  (or the configured age bound when smaller); otherwise try ALTITUDE.
- `ALTITUDE.altitude_terrain` is already height **above terrain**, not MSL terrain
  elevation. `bottom_clearance` is the fallback distance; negative/NaN values are
  unavailable. No subtraction of `relative_alt` or MSL altitude is used.
- Freshness uses the recorded monotonic receive time relative to the frame;
  default bound is one second. Transport latency and sensor extrinsics still need
  mission-specific calibration. Measurements must be referenced to vehicle
  origin; compensate rangefinder lever arm upstream if needed.
- Disabled by default: observation output adds no AGL fields. Sensor messages
  are retained when reported, but no new stream requests or sensor assumptions
  are introduced. A future live mission adapter may supply normalized
  `ground_agl={"height_m": ..., "age_s": ..., "source": ...}` directly.

## Recorded and synthetic validation

Validation completed: **146 passed, 1 skipped** across the 13 targeted test files;
Ruff and `git diff --check` pass for the changed implementation. The skipped
existing camera regression needs a legacy recording fixture absent from this
worktree; the separate replay below reads the available shared artifact corpus.

Report: `docs/evidence/onboard-ground-replay-20261015.json` (input/source SHA-256,
per-episode statistics, skipped samples, and 212 per-hit records).

Read-only input corpus:
`D:/Projects/Multi-Detect/artifacts/datasets/onboard-loop/eval-v2-test-r12multi`.
Eight recorded episodes over six `gen_v2_w0034` through `gen_v2_w0039` worlds:
225 steps, 231 track boxes. Four additional metadata-only episodes have no
`steps.jsonl` and are explicitly skipped. No flight was rerun.

Identity is assigned independently of projected ground: maximum image-space
IoU >= 0.1 against recorded segmentation boxes. All 212 scored samples use
segmentation; 19 boxes have no match. The CLI also supports projected truth-box
association when segmentation is absent, which is less robust to occlusion and
overlap. This is per-observation geometry replay, not a counterfactual flight
policy evaluation. Truth loading reuses `scripts/score_onboard_loop.py`.

| Case | Samples | Flat mean (m) | Flat median (m) | Flat RMSE (m) | Flat P95 (m) | Flat max (m) | DEM max (m) |
|---|---:|---:|---:|---:|---:|---:|---:|
| Recorded SIM | 212 | 2.294 | 1.549 | 3.487 | 8.314 | 18.493 | 18.493 |
| Synthetic slopes N=+0.14, E=+0.05 | 9 | 70.634 | 47.251 | 93.724 | 175.512 | 175.512 | < 1e-9 |
| Synthetic slopes N=-0.08, E=+0.10 | 9 | 15.747 | 13.988 | 19.052 | 33.776 | 33.776 | < 1e-9 |

All recorded DEMs are exactly flat at 18 m MSL with origin 18 m; their DEM error
statistics equal FlatGround to displayed precision. Maximum flat/DEM difference
is 4.151e-13 m. Nonzero errors are to object truth centres: detector contact-box
bias, object extent and view geometry remain even on a perfect flat map.

Synthetic report cases use exact ground-contact pixels with mount offsets,
level body attitude, 135 m MSL reference, 30 m nominal flight height and multiple
N/E target locations. Additional unit tests cover DEM contact projection with
nonzero roll/pitch/yaw. Ground slope is known independently; there is no detector
or box-edge uncertainty. The shallow rays in the uphill case explain the large
flat error. DEM intersections agree with analytic truth below 1e-9 m.

All 231 replayed boxes have exactly equal default and explicit FlatGround
results. Byte-level tests additionally compare the original projection arithmetic
across roll/pitch/yaw and pixel cases. Recorded stored `ground_ne` is not an exact
golden value for rounded logged boxes: mean difference 0.01776 m, maximum 0.97244 m;
the report preserves this diagnostic rather than claiming stored-point parity.

Reproduce from the worktree (CPU only):

```powershell
python scripts/replay_ground_projection.py D:/Projects/Multi-Detect/artifacts/datasets/onboard-loop/eval-v2-test-r12multi --include-hits --output docs/evidence/onboard-ground-replay-20261015.json
python -m pytest -q -p no:warnings --basetemp=.tmp/pytest-ground tests/test_ground_model.py tests/test_ground_agl.py tests/test_onboard_ground_policy.py tests/test_replay_ground_projection.py tests/test_onboard_loop.py tests/test_onboard_policy_plan.py tests/test_sim_teacher_live_loop.py tests/test_sim_teacher_pose_alignment.py tests/test_terrain_dem.py tests/test_sim_camera_projection.py
python -m ruff check src/multidetect/autonomy/ground_model.py src/multidetect/autonomy/onboard_loop.py src/multidetect/simulator/camera_projection.py src/multidetect/sim_teacher/streams.py scripts/onboard_policy.py scripts/collect_sim_teacher_episodes.py scripts/replay_ground_projection.py tests/test_ground_model.py tests/test_ground_agl.py tests/test_onboard_ground_policy.py tests/test_replay_ground_projection.py
```
