# Gazebo camera calibration contracts

These strict schema-v1 documents bind the shared Multi-Detect ranging geometry
to the repository-owned PX4/Gazebo camera compositions. They are simulator
sensor contracts, not field-camera calibration evidence.

All three airframes include the same pinned PX4 `mono_cam` sensor. Its source
contract is 1280 x 960 with a horizontal field of view of 1.74 radians and no
declared distortion. The focal length is therefore derived without a fitted or
guessed value:

```text
fx = fy = 1280 / (2 * tan(1.74 / 2)) = 539.93636802453 px
cx = 640 px
cy = 480 px
```

The mount rotations and body-NED lever arms come from the checked-in composite
model SDF files:

| Calibration ID | Composition | Forward / right / down | Pitch down |
| --- | --- | --- | --- |
| `gazebo-multirotor-front-camera-v1` | `x500_mono_cam` | 0.18 / 0.00 / 0.12 m | 0.18 rad |
| `gazebo-front-camera-v1` | `standard_vtol_mono_cam` | 0.35 / 0.00 / 0.02 m | 0.6108652381980153 rad (35°) |
| `gazebo-fixed-wing-front-camera-v1` | `rc_cessna_mono_cam` | 0.48 / 0.00 / 0.10 m | 0 rad |

Runtime acceptance must check the calibration ID against the scenario RGB
sensor, check width and height against the received wire frame, and bind the
calibration file SHA-256 into the integrated run identity. A real camera must
still use its own ChArUco calibration and physical lever-arm survey; these
Gazebo contracts cannot satisfy the REAL or outdoor-GNSS release gates.

The Gazebo depth wire is a separate simulator-only sensor contract for
`optical_axis_z_m`. The runtime converts that value to a ray/slant distance
with the checked-in camera intrinsics before it enters multimodal ranging. It
must not be passed to `--metric-depth-calibration-document`: that option is
reserved for a monocular model's own raw-output-to-slant calibration, with
paired measurements, model identity, preprocessing/output semantics, and an
independent holdout. The loader rejects documents whose `source_kind` starts
with `gazebo_depth_wire` so a zero-error simulator identity mapping cannot be
mistaken for Depth Anything or outdoor-camera accuracy evidence.

No model calibration is claimed by the Gazebo sensor contract. A valid
simulator run may exercise the independent Gazebo depth transport and a raw
monocular model smoke path, but it must keep their calibration hashes and
distance semantics separate. Neither path closes the REAL, HIL, hardware, or
outdoor-GNSS release gates.

## Opt-in depth companion capture

`scripts/run_simulator_docker_acceptance.py` can persist the validated depth
payloads with `--sensor-depth-output-dir` when
`--sensor-frame-output-dir` is also supplied. The two directories are kept
separate so the existing RGB frame-set manifest and VTOL exact-directory
consumer remain unchanged. Each depth directory contains files such as
`depth-000000.float32_m` and a
`gazebo-depth-companion-set.manifest.json`.

The files are raw `FLOAT32_M` payloads, not standalone `MDGZDEP1` records. The
manifest stores the canonical metadata, RGB wire digest, depth wire digest,
geometry, timestamps, calibration identity and a per-frame RGB/Depth pair
binding. The capture code reconstructs the signed `MDGZDEP1` digest from the
payload and metadata before sealing the manifest, and verifies it again when
the manifest is written. This makes the pair replayable without pretending
that a simulator sensor identity is a monocular metric-depth calibration.

The resulting evidence is software-SIM sensor provenance only. It does not
claim an outdoor calibration, physical distance accuracy, HIL/REAL behavior,
hardware contact or final-release eligibility.
