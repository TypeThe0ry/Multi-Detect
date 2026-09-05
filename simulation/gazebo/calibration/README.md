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
