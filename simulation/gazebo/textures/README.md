# Gazebo test textures

This directory contains repository-local raster assets used by explicit
software-only simulator controls. These files are test inputs, not REAL/HIL
evidence and not production model-accuracy evidence.

## `coco-car-positive-control-v1`

- `coco-car-positive-control-v1.png` is a generated, single-object vehicle
  image intended to exercise the quarantined COCO candidate detector.
- The `.provenance.json` sidecar binds the exact PNG SHA-256, dimensions,
  generation prompt, intended scope, and evidence exclusions.
- The `.annotation.json` sidecar is a manual pixel annotation. It is explicitly
  independent of candidate-model output and is the only expected bounding box
  accepted by the candidate-control evaluator.

The source PNG must never be overwritten in place. A changed image requires a
new versioned filename, provenance sidecar, annotation, and SHA-256.
