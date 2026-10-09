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

Each world has one true target and zero to four distractors, static collision
geometry, a five-metre takeoff pad, a clear corridor around the origin, and
objects placed within a 150 m search radius.  The scenario directory contains
`*.scenario.json`, `*.runtime.json`, `*.truth.json`, and
`*.expectations.json`; `index.json` records world split, seed, Chinese task
text, target pose, and SHA-256 hashes for every artifact.

## Simulation speed

Gazebo's `<real_time_factor>` is emitted in each world's `<physics>` block and
defaults to `1.0`.  Values above one request faster-than-real-time stepping;
values below one deliberately slow the simulation.  PX4 SITL and Gazebo can
therefore run faster than wall clock when the host has spare CPU, but camera,
MAVLink and collector processing can become the practical limit.  The Docker
launcher does not overwrite this SDF value.  `PX4_SIM_SPEED_FACTOR` is not
consumed by the repository launcher, so use `--real-time-factor` for generated
worlds and treat any external environment variable as deployment-specific.
