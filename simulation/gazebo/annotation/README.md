# Capture-only Gazebo 3-D annotations

This directory contains an optional annotation rig for building reviewed
OnboardAI training data from the repository-owned 3-D Gazebo scenes.

It is deliberately separate from the mainline vehicle and world assets.
`multidetect_annotation_x500_v1` wraps the existing `x500_mono_cam` model and
adds two sensors at the same camera pose:

- `boundingbox_camera` on `multidetect/annotation/bounding_boxes_3d`;
- instance `segmentation` on `multidetect/annotation/instance_segmentation`.

`multidetect_annotation_target_v1` wraps the existing structural target and
adds Gazebo's label system with offline label ID `42`. The label map and
bounding boxes are annotation evidence only. They must never be subscribed to
by the OnboardAI runtime, included in `ContextSnapshot`, or passed to the
detector/tracker as an input. Runtime inputs remain RGB, registered FLOAT32
depth, CameraInfo, camera pose and PX4 telemetry.

## Materialize a temporary capture world

The checked-in mainline world is never edited. From the repository root:

```powershell
python scripts/materialize_gazebo_3d_annotation_capture.py `
  --manifest configs/scenarios/gazebo-3d-annotation-capture-v1.json `
  --output-world artifacts/annotation-capture/urban_inspection_multirotor_001.sdf `
  --output-binding artifacts/annotation-capture/binding.json `
  --output-resource-root artifacts/annotation-capture/models
```

The command only prepares an output SDF. It does not launch Gazebo, Docker,
PX4 or QGC, and it does not claim that a sensor topic was observed. The
`--output-resource-root` option creates a new, read-only-mountable resource
root containing the two capture wrappers, the repository-owned target and the
vehicle composition. Prepend that directory to `GZ_SIM_RESOURCE_PATH` for the
future capture-only run; the pinned PX4 image still supplies its upstream
`x500`/`mono_cam` dependencies. The binding receipt records every copied file
and both source/output hashes, so a launcher cannot silently use a different
model bundle.

The result is still `training_ready=false`. To become a training input, an
offline collector must bind every annotation frame to the same RGB payload
hash, depth payload hash, CameraInfo, pose and telemetry frame timestamp, then
an independent reviewer must approve the trajectory labels. Use the existing
trajectory review receipt contract; do not promote labels merely because a
Gazebo annotation sensor produced a message.

## Boundary

The assets here are not part of the runtime release parity hash and are not a
replacement for live detector/tracker evidence. They provide objective 2-D/3-D
label evidence for annotation and review while leaving the SIM/Jetson policy
path truth-free.
