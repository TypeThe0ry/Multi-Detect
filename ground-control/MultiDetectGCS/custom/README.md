# Multi-Detect G20 custom QGroundControl

This directory contains the native QGC application for the G20 ground station. The current desktop
and Android ARM64 milestone provides:

- raw QGC video with a local target-box overlay;
- touch-drag selection with explicit selection mode, cancel and target switching;
- patrol and payload-capable display modes;
- Plan-map rectangular GPS area selection with automatic native Survey coverage routing;
- mission, tracking, safety, authorization and Jetson-health panels;
- an authenticated Jetson operator-metadata link over MAVLink 2 `TUNNEL` payload type 42000;
- a localhost protocol harness for regression tests.

The operator link exchanges target selection and acknowledgement, tracking, patrol target-pool and
return-observe advisory, mission, safety, authorization challenge, bound operator decision and decision acknowledgement messages. Each
application packet carries an HMAC-SHA256 authentication tag. Production traffic additionally requires
outer MAVLink 2 signing.

## Control ownership

QGC sends authenticated target selection and Mode-3 execution confirmation to Jetson. When the
Jetson runtime reports `flightControlEnabled=true`, that confirmation enables its bounded fixed-wing
attitude-target controller. Jetson is the single Pixhawk setpoint writer; QGC deliberately does not
duplicate those setpoints. The build requires
`MULTIDETECT_QGC_DIRECT_PIXHAWK_WRITES=0` and `MULTIDETECT_PHYSICAL_RELEASE=0`; C++ static assertions reject
any other value. Physical payload release is a separate path and remains absent from this build.

Unsigned outer MAVLink is accepted only when all of the following explicit software-HIL controls are
present: loopback UDP, `MULTIDETECT_OPERATOR_ALLOW_UNSIGNED_HIL=1`, and a valid ephemeral HIL port. Do
not enable that override on the aircraft, Jetson, G20 or a routable network. QGC must also be started
with `--isolated-hil`; that mode uses temporary clean settings and disables automatic serial, UDP and
saved links while keeping only the explicitly requested localhost HIL link.

Physical release remains outside this milestone. It must stay disabled through PX4 SITL, signed-link
software HIL and an actuator-disconnected bench test.

## GPS area route planning

In **Plan**, select **区域航线** in the left tool strip and drag once across the required map area.
The application converts the drag bounds to four GPS polygon vertices, appends a standard QGC
`Survey` item after the current plan item, and QGC immediately generates the fixed-wing coverage
transects. Existing mission items are retained. The resulting Survey remains an ordinary QGC mission
item: its polygon, altitude, grid angle, spacing and turnaround distance can be adjusted in the
standard Survey editor, and normal Plan save/upload handling is unchanged.

## Target GPS map evidence

When Jetson reports a qualified target WGS84 coordinate, **Fly** projects the current matching target
onto the main map as a red marker with a yellow horizontal `1σ` uncertainty circle. The accompanying
"目标落图 · 只读" card shows the coordinate, target class, fused distance, relative bearing,
target/source frame and source age. Qualified coordinates can be copied to the local clipboard or
saved as a local JSONL metadata snapshot. QGC hides the marker when navigation quality degrades, a
coordinate is stale or invalid, or its target ID no longer matches the selected target; the remaining
card then explicitly reports "不可定位" and disables copy/save instead of placing a guessed point.

This presentation is deliberately read-only: it creates no mission item, guided action, flight command
or editable map object. The circle is an uncertainty visualization, not a navigation or safety radius.
Jetson is responsible for withholding target coordinates unless its GPS-aided navigation and fused
target offset qualify. A snapshot contains only target metadata (ID/class, coordinates, uncertainty,
range, bearing, source frame and local UTC save time) in the application-local
`snapshots/target-snapshots.jsonl` file. It is neither a video/depth capture nor a network upload.

## LCK and Mode 3 audio cues

- The authoritative target-pool transition to a primary `LCK` emits the bundled `LOCKED` voice once.
  Optimistic button state and repeated target-pool refreshes do not replay it; the cue is re-armed
  only after that confirmed primary lock clears or changes target.
- An accepted Mode 3 execution starts the existing acknowledgement cue plus a lower-volume repeating
  double-tone. The loop is tied to `mode3AimUiActive` and stops immediately when the operator cancels,
  changes mission mode, or a pilot-input takeover clears the execution state.
- All three WAV assets are compiled into `custom.qrc` as `qrc:/Custom/audio/*`; no runtime audio file
  deployment or settings configuration is required.

## Operator configuration

The desktop or Android process reads the following runtime values:

| Variable | Requirement |
| --- | --- |
| `MULTIDETECT_OPERATOR_KEY` | At least 32 UTF-8 bytes; provision as a secret on both QGC and Jetson. |
| `MULTIDETECT_OPERATOR_ID` | Non-empty operator identity used to bind authorization decisions. |
| `MULTIDETECT_OPERATOR_STREAM_ID` | Optional; defaults to `camera-main`. |
| `MULTIDETECT_OPERATOR_STREAM_WIDTH` / `HEIGHT` | Optional; defaults to `1280` / `720`. |
| `MULTIDETECT_OPERATOR_STREAM_ROTATION` | Optional; defaults to `0`. |
| `MULTIDETECT_OPERATOR_UDP_HOST` | Jetson direct metadata-plane address; for this bench, `192.168.144.20`. |
| `MULTIDETECT_OPERATOR_UDP_PORT` | Jetson metadata port; defaults to `14580`. |
| `MULTIDETECT_OPERATOR_UDP_LOCAL_PORT` | QGC receive port; defaults to `14581` and must differ from the remote port. |
| `MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX` | Exactly 32 random bytes encoded as 64 hexadecimal digits for outer MAVLink 2 signing. |
| `MULTIDETECT_VIDEO_RTSP_URL` | Optional RTSP source injected into QGC without UI setup. |

When `MULTIDETECT_OPERATOR_UDP_HOST` is present, the custom application creates an ephemeral direct
UDP link only for signed operator metadata. It does not create a vehicle and does not carry flight
commands. The normal GR01/V6X QGC link remains separate. Target selection, cancellation and switching
use the direct link; raw video remains RTSP and aircraft telemetry remains standard MAVLink.

Production must not set any `MULTIDETECT_OPERATOR_HIL_*` variable or
`MULTIDETECT_OPERATOR_ALLOW_UNSIGNED_HIL`.

## Local closed-loop HIL

The HIL driver is `custom/tests/operator_closed_loop_hil.py`. It is restricted to `127.0.0.1` and
verifies the complete selection, tracking, patrol-status, safety and challenge-bound approval loop. Use the same
temporary key in both terminals and never reuse it as a production key.

QGC terminal (from the repository root, after loading the normal Qt/GStreamer runtime environment):

```powershell
$env:MULTIDETECT_OPERATOR_KEY = "replace-with-a-temporary-key-of-at-least-32-bytes"
$env:MULTIDETECT_OPERATOR_ID = "software-hil-operator"
$env:MULTIDETECT_OPERATOR_ALLOW_UNSIGNED_HIL = "1"
$env:MULTIDETECT_OPERATOR_HIL_UDP_PORT = "14669"
$env:MULTIDETECT_OPERATOR_HIL_AUTO_EXIT = "1"
$env:MULTIDETECT_OPERATOR_HIL_AUTO_EXERCISE = "1"
./build-multidetect-release/Release/MultiDetectGCS.exe --system-id 255 --isolated-hil
```

Jetson-simulator terminal:

```powershell
$env:MULTIDETECT_OPERATOR_KEY = "replace-with-a-temporary-key-of-at-least-32-bytes"
$env:PYTHONPATH = (Resolve-Path ../Multi-Detect/src)
../Multi-Detect/.venv/Scripts/python.exe custom/tests/operator_closed_loop_hil.py `
    --port 14669 --timeout 20
```

Success requires driver exit code 0, QGC exit code 0 and no new crash dump.

### Concurrent PX4 SITL + metadata-only Jetson HIL

The sibling `Multi-Detect` repository provides the authoritative orchestration:

```powershell
cd ..\Multi-Detect
.\scripts\run_px4_sitl_qgc_operator_acceptance.ps1
```

This mode additionally sets `MULTIDETECT_OPERATOR_HIL_REQUIRE_INITIAL_CONNECT=1`, so automated target
selection cannot start while the real PX4 SITL vehicle is still downloading parameters or component
metadata. The Jetson driver sends only component `1/191` presence and operator metadata; PX4 SITL is
the sole source of the `1/1` autopilot heartbeat.

The localhost router permits bounded read/status traffic, including read-only MAVLink FTP and
`MAV_CMD_RUN_PREARM_CHECKS`. It never forwards operator `TUNNEL` packets to PX4 and blocks clock
updates, unknown commands, file mutations, parameter writes, mission uploads, arm/mode, actuator and
payload traffic. The current acceptance also sends a three-track target pool in two authenticated pages
and 30 continuous tracking packets at 20 Hz. It requires QGC to log one atomic pool revision, receive all
30 type-3 packets and authenticate at least 37 total metadata packets before it can pass. The earlier
2026-07-14 run completed six authenticated message classes; the expanded run still requires QGC exit
code 0 and no new crash dump.

## PX4 datalink-loss parameters

The current QGC PX4 metadata defines `COM_DL_LOSS_T` with a minimum of 5 seconds and a default of 10
seconds. A value of 1 is invalid and must not be written. `NAV_DLL_ACT=1` means Hold mode, but it must be
evaluated in PX4 SITL before any disconnected-actuator bench proposal. The last read-only V6X capture
showed `COM_DL_LOSS_T=10`, `NAV_DLL_ACT=0` and TELEM1 at 115200; this application does not change them.

## Acceptance order

1. Desktop build, protocol self-test, cold boot and localhost closed-loop HIL.
2. PX4 SITL for datalink loss, mode transitions and reconnect behavior.
3. Jetson-to-V6X read-only telemetry with command, parameter and mission writes still blocked.
4. Signed operator metadata on an actuator-disconnected bench.
5. G20 Android build with RTSP video and local metadata overlay.
6. Inert payload simulator and independent hardware safety review before any release hardware is enabled.

## Build

QGroundControl automatically detects this `custom` directory. Configure the normal desktop or Android
QGC build from the repository root. The application name is `Multi-Detect GCS` and the build is limited
to PX4, matching the Pixhawk V6X platform. The current product line starts at **MultiDetectGCS v0.2.0**.
All new release files use the product and version, for example
`MultiDetectGCS-v0.2.0-windows-amd64.exe`; feature names are not used as artifact suffixes.

### Historical G20 Android bench artifact

The locally validated Android 13/ARM64 bench artifact is:

`build-multidetect-android/MultiDetectGCS-G20-arm64-v8a-0.1.0-hil-debug-signed.apk`

It has package ID `com.multidetect.gcs`, version `0.1.0`, minimum SDK 28, target/compile SDK 36 and
contains only `arm64-v8a` native code. `aapt`, `apksigner` and `zipalign` validation passed; Android
host tests passed 23/23 and release lint has zero errors. The local artifact is signed by the
`Multi-Detect Debug` bench certificate and is not a production-distribution signature. Its SHA-256 is
`e8764211ff0913d4053095b339eaac47acaa47ae38f531d8c6b1301c82bd807a`.

This is a historical bench artifact only. The next Android release must use the v0.2.0 product version
or a later semantic version.

No Android device was present during this build, so installation, startup, 1920x1200 layout, H.265
hardware decode, RTSP latency and touch-coordinate behavior still require a real G20 bench. The build
continues to compile both flight-control writes and physical release as `0`; installing this APK does
not authorize enabling either path. The Windows-hosted local build uses the bundled OpenSSL 3.1
development dependency; production CI must replace it with the pinned supported OpenSSL 3.5.6 path.

For a standalone Windows runtime, building the `Release` target is not sufficient: that directory
contains the executable but depends on the developer Qt/GStreamer environment. Install NSIS and run
the generated CMake deployment step:

```powershell
cmake --install build-multidetect-release --config Release
```

Run the deployed application from
`build-multidetect-release/staging/bin/MultiDetectGCS.exe`, with that `bin` directory as its working
directory, or use the versioned `build-multidetect-release/MultiDetectGCS-v0.2.0-windows-amd64.exe`.
The installer excludes
dependency headers, static import libraries and Debug CRT binaries. Local development installers are
unsigned; a production-distribution build requires an approved Windows code-signing identity and a
separate signature verification gate.

Every release is installed into an isolated `staging-v<version>` directory so it cannot overwrite an
active `staging` runtime. Record its installed EXE and installer SHA-256 in the parent repository's
release document before handing it to an operator.
