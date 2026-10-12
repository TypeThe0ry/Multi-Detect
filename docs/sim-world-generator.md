# Procedural SIM world generator

`scripts/generate_sim_worlds.py` creates repository-local Gazebo Harmonic SDF
worlds and the matching scenario channels used by the privileged sim-teacher
collector.  It uses only SDF planes, boxes, cylinders and materials, so no
Gazebo Fuel download is needed at runtime.

```powershell
python scripts/generate_sim_worlds.py --count 20 --seed 20261010 `
  --split-ratios 0.7,0.15,0.15 `
  --out-worlds simulation/gazebo/worlds `
  --out-scenarios configs/scenarios/generated `
  --real-time-factor 1.0
```

The generated runtime channel points at `builtin://gazebo/gen_w0000`.
Generated files are written directly to `simulation/gazebo/worlds/` as
`gen_w0000.sdf`, `gen_w0001.sdf`, and so on.  This matches the existing
release-parity-bound resolver, which accepts a top-level world basename.  The
Docker simulator planner mounts the selected world directory
at `/opt/px4-gazebo/share/gz/worlds`, sets `PX4_GZ_WORLDS` and includes that
directory in `GZ_SIM_RESOURCE_PATH`; PX4 starts Gazebo with
`PX4_GZ_WORLD=default`, while the mounted SDF keeps the world name `default`.
The same planner mounts the repository model directory and sets
`PX4_GZ_MODELS`, so generated primitive worlds remain Fuel-free.

## Realistic Fuel worlds (`fuel_v1`)

Use the versioned mode for realistic OpenRobotics models:

```powershell
python scripts/generate_sim_worlds.py --asset-mode fuel_v1 --count 40 --seed 20261011 `
  --out-worlds simulation/gazebo/worlds --out-scenarios configs/scenarios/generated-v2
```

`configs/sim-assets/fuel-catalog.json` catalogs all 39 local CC0-1.0 assets,
including their Fuel source URL (`https://fuel.gazebosim.org/1.0/OpenRobotics/models/<Name>`),
semantic words, role eligibility, intrinsic color, measured mesh bounds, and
bottom offset. `python scripts/build_sim_asset_catalog.py` rebuilds the catalog
and creates `artifacts/cache/gz-fuel/resolved/<slug>/` with rewritten local
`model://<slug>/...` URIs. The Docker planner mounts this tree read-only and
prepends it to `GZ_SIM_RESOURCE_PATH`, so simulation never fetches assets.

The v2 index preserves the 28/6/6 train/validation/test proportions. Targets
and distractors use different subtypes or attributes, and test entries record
held-out target combinations for open-vocabulary generalization.

Each world has one true target and zero to four distractors, static collision
geometry, a five-metre takeoff pad, a clear corridor around the origin, and
objects placed within a 150 m search radius.  The scenario directory contains
`*.scenario.json`, `*.runtime.json`, `*.truth.json`, and
`*.expectations.json`; `index.json` records world split, seed, Chinese task
text, target pose, and SHA-256 hashes for every artifact. The strict scenario
manifest remains unchanged; split and task metadata live in the generated index.

### Segmentation labels

Pass `--segmentation-labels` when generating `fuel_v1` worlds for a Gazebo
Harmonic segmentation camera:

```powershell
python scripts/generate_sim_worlds.py --asset-mode fuel_v1 --count 1 --seed 20261012 `
  --segmentation-labels --out-worlds simulation/gazebo/worlds `
  --out-scenarios configs/scenarios/generated-v2
```

This opt-in mode adds a `gz-sim-label-system` plugin to each outer object model
and its nested mesh models, using the same label throughout the object.
The plugin's integer label is stable for a generated world and is recorded in
the matching truth channel under `segmentation_labels`, keyed by the decimal
label id. Each entry contains `target_id`, `asset_slug`, and `role` (`target`,
`distractor`, or `obstacle`). A segmentation image can therefore be reduced to
visible pixels for one label and mapped back to the truth object without using
the vehicle pose. Labels are in the range 1-254, reserving 0 and 255; with the
collector's panoptic camera the semantic label occupies raw image channel 2,
and channels 0-1 retain the renderer's per-entity instance id. Distinct visuals
of a nested object may have distinct instance ids but share its stable label.
The default box mode and fuel mode without this flag do not
emit plugins or the extra truth field, preserving their existing bytes.

## Simulation speed

Gazebo's `<real_time_factor>` is emitted in each world's `<physics>` block and
defaults to `1.0`.  Values above one request faster-than-real-time stepping;
values below one deliberately slow the simulation.  PX4 SITL and Gazebo can
therefore run faster than wall clock when the host has spare CPU, but camera,
MAVLink and collector processing can become the practical limit.  The Docker
launcher does not overwrite this SDF value.  `PX4_SIM_SPEED_FACTOR` is not
consumed by the repository launcher, so use `--real-time-factor` for generated
worlds and treat any external environment variable as deployment-specific.
