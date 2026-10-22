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

The direct operator metadata link uses the link ID assigned by QGC's signing
channel allocator. The Python Jetson endpoint may set
`receive_signing_link_id=<id>` when the channel is fixed. If it is omitted, the
endpoint learns the link ID from the first signature-verified frame received
from the configured peer, persists that pin with replay state, and rejects any
different link ID for the rest of the session and after restart. One signing
key is provisioned per physical link; link ID is part of the endpoint/session
identity.

## Control ownership

QGC sends authenticated target selection and Mode-3 execution confirmation to Jetson. When the
Jetson runtime reports `flightControlEnabled=true`, that confirmation enables its bounded fixed-wing
attitude-target controller. Jetson is the single Pixhawk setpoint writer; QGC deliberately does not
duplicate those setpoints. The build requires
`MULTIDETECT_QGC_DIRECT_PIXHAWK_WRITES=0` and `MULTIDETECT_PHYSICAL_RELEASE=0`; C++ static assertions reject
any other value. Physical payload release is a separate path and remains absent from this build.

The operator metadata plane always requires a signed MAVLink 2 outer envelope. Software HIL is isolated
by loopback-only UDP ports and the explicit `--isolated-hil` boundary; there is no unsigned outer-MAVLink
fallback or environment switch. The isolated mode uses temporary clean settings and disables automatic
serial, UDP and saved links while keeping only the explicitly requested localhost HIL links.

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

Current type-26 target observations carry a required nonzero 32-bit `producer_epoch` and exactly fill
one 128-byte MAVLink `TUNNEL` application payload. A newly observed epoch retires the previous producer
session and atomically clears its visible range/geolocation/GNSS values plus replay/sequence guards
before the new triple is adopted. Packets from a retired epoch remain rejected, so delayed traffic from
a restarted Jetson process cannot revive an old target. New snapshots use
`multidetect.target-evidence.v6` and persist epoch health level/reason, restart/rejection counters,
the latest transition and the latest incident time; v1-v5 records remain read-only compatible.
The outdoor ranging-quality panel latches and color-codes `WAITING`, `HEALTHY`, `NOTICE`, `WARNING`
and `CRITICAL`, alongside the current epoch and counters. The
retired set keeps at most 32 epochs without eviction; after that capacity is reached, a new epoch is
rejected while the current session remains active until QGC is restarted. This bounded fail-closed rule
prevents very late traffic from becoming eligible again merely because an older epoch was discarded.

The controller also retains the latest 64 epoch lifecycle/incident events while separately counting
all events and evicted diagnostic entries. The Fly target card's `复制诊断` action copies an indented
`multidetect.producer-epoch-diagnostics.v1` JSON document with event indices, timestamps, epoch
transitions, health state and incident counters. It remains available before the first event so a
`WAITING` state is diagnosable. This diagnostic ring does not weaken the separate non-evicting retired
epoch set, and the exported document explicitly records `advisory_only=true`, flight control disabled
and physical release disabled.

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

The verified target-event replay dialog also offers **落图预览** for a selected event whose GPS
coordinates passed the native hash/freshness checks. The orange marker and its `1σ` circle are a
read-only preview of that historical event; the action does not change `MultiDetectState`, does not
select a live target, and cannot create a Mission, Guided action or flight-control packet. Loading a
new manifest or pressing **清除落图预览** removes the preview.

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
| `MULTIDETECT_OPERATOR_ED25519_PRIVATE_KEY` | Optional 32-byte Ed25519 seed (hex/base64, optional `ed25519-private:` prefix) signing mission packages and ground commands. |
| `MULTIDETECT_OPERATOR_SIGNING_KEY` | Optional `hmac:<hex\|base64>` (16-4096 bytes) or `ed25519-private:<hex\|base64>`; replaces the seed above for packages and ground commands, matching onboard `--operator-key`. |
| `MULTIDETECT_GROUND_COMMAND_KEY` | Optional key in the same format for ground commands only (plan approval/rejection, abort, pause, resume, policy), i.e. the key the aircraft verifies ground commands with (`scripts/fake_gcs_console.py --command-key`). Without any key, commands are sent unsigned and REAL mode refuses them. A configured but malformed key fails closed. All three key variables are removed from the process environment after reading. |
| `MULTIDETECT_OPERATOR_KEY_ID` | Optional `key_id` written into signed documents; defaults to `default`. |
| `MULTIDETECT_VIDEO_RTSP_URL` | Optional RTSP source injected into QGC without UI setup. |
| `MULTIDETECT_UI_SNAPSHOT_DIR` | Acceptance-only absolute directory. Enables `QQuickWindow::grabWindow()` PNG snapshots; unset in production. |
| `MULTIDETECT_UI_SNAPSHOT_INTERVAL_S` | Optional snapshot interval in seconds (default `2`, clamped to `1`-`3600`). |
| `MULTIDETECT_UI_SNAPSHOT_MAX` | Optional maximum PNG count (default `20`, max `1000`). QML may call `multiDetectUiSnapshotController.requestSnapshot("event")`. |
| `MULTIDETECT_UI_AUTO_EXIT_S` | Optional acceptance-only clean event-loop exit delay in seconds (maximum `3600`); independent of isolated HIL. |

When `MULTIDETECT_OPERATOR_UDP_HOST` is present, the custom application creates an ephemeral direct
UDP link only for signed operator metadata. It does not create a vehicle and does not carry flight
commands. The normal GR01/V6X QGC link remains separate. Target selection, cancellation and switching
use the direct link; raw video remains RTSP and aircraft telemetry remains standard MAVLink.

Production must not set any `MULTIDETECT_OPERATOR_HIL_*`,
`MULTIDETECT_OPERATOR_ALLOW_UNSIGNED_HIL`, or acceptance-only
`MULTIDETECT_HIL_UI_AUTO_*` or `MULTIDETECT_UI_*SNAPSHOT*` variable. Windows acceptance still calls the native
`finishCloseProcess()` path for link/video teardown; the controlled exit after
`MULTIDETECT_HIL_UI_AUTO_EXIT` avoids the QML/global-singleton destruction race.

For locked-desktop or remote acceptance, set `QT_QPA_PLATFORM=offscreen` and optionally
`QT_QUICK_BACKEND=software`, then set `MULTIDETECT_UI_SNAPSHOT_DIR` to an absolute directory.
The custom plugin binds the main `QQuickWindow` after QML startup and writes `snapshot-####-event.png`
files without relying on Win32 `PrintWindow` or screen capture. The hook is completely inactive when
the directory variable is absent. `MULTIDETECT_UI_AUTO_EXIT_S` is useful for bounded offscreen runs;
it only schedules `QCoreApplication::quit()` when explicitly set.
On the current locked Windows session, this QGC build crashes under `QT_QPA_PLATFORM=offscreen` before
creating a window (`0xC0000005`); `QT_QPA_PLATFORM=minimal` with the software renderer starts cleanly,
but does not expose a capturable QQuickWindow. Treat PNG generation as an environment-dependent
acceptance check and retain the process logs when the platform plugin cannot create a window.
The three earlier QGC + pinned PX4 SITL runs and the two palette rebuilds are
historical pre.3 stability evidence: all reported `exit_code=0`/`all_passed=true`
with no new crash dump, and the then-latest run was `20260825T190317Z-5732`.

## Local closed-loop HIL

The HIL driver is `custom/tests/operator_closed_loop_hil.py`. It is restricted to `127.0.0.1` and
verifies the complete selection, tracking, patrol-status, safety and challenge-bound approval loop. Use the same
temporary key in both terminals and never reuse it as a production key.

QGC terminal (from the repository root, after loading the normal Qt/GStreamer runtime environment):

```powershell
$env:MULTIDETECT_OPERATOR_KEY = "replace-with-a-temporary-key-of-at-least-32-bytes"
$env:MULTIDETECT_OPERATOR_ID = "software-hil-operator"
$env:MULTIDETECT_OPERATOR_UDP_HOST = "127.0.0.1"
$env:MULTIDETECT_OPERATOR_UDP_PORT = "14666"
$env:MULTIDETECT_OPERATOR_UDP_LOCAL_PORT = "14669"
$env:MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX = "replace-with-64-hex-digits"
$env:MULTIDETECT_HIL_UI_AUTO_EXERCISE = "1" # acceptance-only
$env:MULTIDETECT_HIL_UI_AUTO_EXIT = "1"     # acceptance-only
        .\build\windows-v0.3.0-pre.3-release\Release\MultiDetectGCS.exe `
            --system-id 255 --isolated-hil `
            --isolated-hil-px4-udp-port 14670 --isolated-hil-px4-target-port 14667
```

Jetson-simulator terminal:

```powershell
$env:MULTIDETECT_OPERATOR_KEY = "replace-with-a-temporary-key-of-at-least-32-bytes"
$env:MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX = "replace-with-64-hex-digits"
$env:PYTHONPATH = (Resolve-Path .\src)
.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe `
    .\ground-control\MultiDetectGCS\custom\tests\operator_closed_loop_hil.py `
    --port 14669 --local-port 14666 --metadata-only --timeout 20 `
    --signing-key-hex-env MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX
```

The driver sends Jetson component `1/191` heartbeat and signed metadata only; the PX4 autopilot
heartbeat must come from the isolated SITL link. Success requires driver exit code 0, QGC exit code 0,
all operator packets to report `outer_signed=true`, and no new crash dump.

### Concurrent PX4 SITL + metadata-only Jetson HIL

The outer unified `Multi-Detect` repository provides the authoritative orchestration. From its root:

```powershell
cd D:\Projects\Multi-Detect
& .\scripts\run_px4_sitl_qgc_operator_acceptance.ps1
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
30 type-3 packets, authenticate at least 37 total metadata packets, and complete the signed
challenge-bound `APPROVE` before it can pass. The PX4 router remains read-only and keeps the operator
`TUNNEL` local to the QGC/driver plane.

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
to PX4, matching the Pixhawk V6X platform. The product line starts at **MultiDetectGCS v0.2.0**;
the current source candidate is **v0.3.0-pre.8**, while final **v0.3.0 is not released**. The canonical
version comes from the outer repository's `configs/product-version.json`: CMake/PE receives numeric
`0.3.0`, Python receives `0.3.0rc8`, and the UI, build directory and artifacts retain release label
`0.3.0-pre.8`. The wrapper targets a versioned portable directory; an optional installer, if explicitly
enabled and actually generated, uses `MultiDetectGCS-v0.3.0-pre.8-windows-amd64.exe`. Feature names are never used as artifact
suffixes.

The current software acceptance pointer is maintained by the outer repository:
`D:\Projects\Multi-Detect\artifacts\evaluation\multidetect-goal-acceptance-20260831-final-qa22.json`
(SHA-256 `F38EE2065DCB57A1CA2C12A99AD1F401DC4D48DA0F87848D5B6BBDD2913B7ACF`,
`v0.3.0-pre.8`, software `pass_with_environment_gap`, hardware `offline_pending`).
The matching portable executable is
`D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.8-release\portable-v0.3.0-pre.8\bin\MultiDetectGCS.exe`
(SHA-256 `E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`).
QA20 and earlier acceptance files remain historical; final `v0.3.0` is not released.

### 当前 RTSP helper 便携部署与多客户端回归（pre.8，2026-09-03）

当前 portable 目录中的 `MultiDetectReplayRtspPublisher.exe` 与 Release helper 为同一字节：
`95,744` bytes，SHA-256
`DE94D5404BEF48B80919BFC06465BD53EBFD50A8B65A50F38DA3BCDDADB58312`。便携 helper 路径为
`D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.8-release\portable-v0.3.0-pre.8\bin\MultiDetectReplayRtspPublisher.exe`。
同一 portable 运行时的主程序 `MultiDetectGCS.exe` 为 `46,575,616` bytes，SHA-256
`4D7A024F80157E5EB8671B77D6E1817FB960ECE8A8A97D0F470A222E7F7C8E40`；Release 与 portable
主程序字节一致。

默认单客户端使用非共享 `GstRTSPMediaFactory` 的全部 appsrc generation；`--minimum-client-count
1..64` 定义活动客户端下限。当下限大于 1 时 helper 切换到 GStreamer shared media，由一条
编码管线原生扇出到并发客户端，避免多个非共享 DESCRIBE 串行阻塞；下限为 1 时保留重连代际
隔离。两种模式都只在达到配置下限后发出 `frame_accepted`。SDP 使用正的 `30/1` nominal
frame rate 仅用于协商，wire frame 的 `captured_at_ns` 仍是唯一 PTS/DTS 来源。

对应的当前原始 RTSP 多客户端证据为
`D:\Projects\Multi-Detect\artifacts\evaluation\replay-rtsp-multiclient-20260903-pre.8-v9-portable-4clients-shared.json`
（3,299 bytes，SHA-256
`994818A9FE96AD5D1BF1C02286E8A1FF67181F63F203C89B7FB6CE75076308B2`）。四个并发客户端均完成
`OPTIONS → DESCRIBE → SETUP → PLAY`，H.264/正帧率 SDP、RTPv2/PT96 包增量、两个稳态 fan-out
frame、EOS 和 summary 均通过，`gate_passed=true`。该文件标记为
`software_only_local_rtsp` / `hardware_claim=none`，不提升真实相机/Jetson、室外 GNSS、
Pixhawk、HIL/REAL 或人工 Fly View 发布门；此前双客户端 v4 文件仍保留为历史记录。

### Signed runtime identity badge

The existing operator metadata channel now carries authenticated `RuntimeStatus` message type `23`.
The 72-byte frame is inner-HMAC authenticated, wrapped in a MAVLink 2 signed `TUNNEL` message, and
contains only display/audit metadata: runtime mode, backend health, scenario seed/hash64, airframe-profile
hash64, run hash64 and isolation flags. It never enables direct Pixhawk writes or a physical release.

The shared Fly View renders the remote mode as `REPLAY`, `SIM`, `HIL` or `REAL`. Missing, invalid or more
than two seconds stale status renders `UNKNOWN`; set `MULTIDETECT_EXPECTED_RUNTIME_MODE` before launching
QGC to make a different signed remote mode render `MISMATCH`. The expected-mode variable is only a local
guard and cannot fabricate a remote mode. There is no simulator-only QML view.

### Same UI and algorithm boundary

All four runtime modes use this same Fly/Plan UI and the same Detector, Tracker, Ranging, Geolocation,
Planner, Safety and Mission algorithms. Only the RuntimeBackend/world adapter may replace clock,
read-only sensor sources, dynamics, scenario assets and fault injection. A mode-specific QML view or a
SIM-only safety/planner shortcut violates the architecture.

The compatible `ReplayBackend` remains explicitly an `ObservationReplayBackend`: its records already
contain Detection and normalized telemetry, so that backend covers only the `FrameObservation` downstream
path. Separately, the v0.3.0-pre.2 headless `integrated-replay` path now sends a complete verified raw
recording through `RawSensorRuntimeBackend -> shared production perception factory -> LiveMissionRunner`.
The pre.3 `integrated-replay-qgc` extension now proves the same-detector/core binding plus the real QGC
RTSP/metadata barrier and native Fly View surface as `E4 INTEGRATED`; Gazebo, HIL and REAL acceptance
remain separate.

### QGC 原生风格验收（pre.3）

真实截图位于：

```text
D:\Projects\Multi-Detect\build\e4-qgc-visual-20260825-224648\qgc-flyview-video-initial.png
D:\Projects\Multi-Detect\build\e4-qgc-visual-20260825-224648\qgc-flyview-video-stable.png
```

画面由 QGC 的 `FlightDisplayViewVideo`、`PipState`、原生 toolbar/map PiP 和 QGC controls 渲染；
Multi-Detect overlay 只作为 QGC surface 内的 QML layer，使用 `QGCLabel`、`QGCButton`、
`QGCPalette`、`ScreenTools`，不创建另一套 simulator UI。临时验收 profile 通过
`QGCQml/MainFlyWindowIsMap=false` 让视频启动即为 Fly View 主面板，避免点击 PiP 时重启 RTSP
session；产品默认设置和用户的 QGC 操作模型不被改变。

机器 result 中 `operator_visual_acceptance` 仍为 `not_claimed`，因为机器 barrier 与人工画面检查
必须分开；人工截图检查已在外层 pre.3 evidence JSON 中单独记录。
历史 pre.3 palette rebuild 的 native-QGC style smoke 位于
`D:\Projects\Multi-Detect\build\e4-qgc-visual-20260826-024914\qgc-flyview-video-stable.png`；
它来自当时的 pre.3 portable binary；RTSP helper 提前断开，未计入 E4 gate，`operator_visual_acceptance` 仍为 `not_claimed`。

随后完成的 fixed-wing PX4 SITL signed metadata 门位于
`D:\Projects\Multi-Detect\artifacts\evaluation\px4-sitl-qgc-operator-acceptance-20260825T190317Z-5732.json`：
QGC/PX4 exit `0`、PX4 heartbeat `5`、signed metadata `37`、tracking `30 @ 20 Hz`、目标池
`3 tracks / 2 pages`、`SELECT_TRK` 和 challenge-bound `APPROVE` 均通过；禁止 PX4 消息、文件变更
FTP、system-time、operator tunnel-to-PX4 及所有任务/参数/飞控/载荷写入为 `0`，保护 `14550`
未改变，真实 V6X/Jetson/G20/GR01 未接触。该门只接受严格 MAVLink 2 signing，UI 仍在原生
QGC Fly View layer 内运行，不产生第二个窗口或 simulator-only 样式。
Windows acceptance 关闭仍调用原生 `finishCloseProcess()` 完成 link/video teardown；
`MULTIDETECT_HIL_UI_AUTO_EXIT` 之后使用受控退出，避开 QML/global-singleton 销毁竞态。
此前 3 次 QGC + pinned PX4 SITL 结果均为 `exit_code=0`、`all_passed=true`、无新 crash dump；
随后两次 QGC palette rebuild 也通过同一门槛，当时 `stability_runs` 共 5 次，最新结果为
`20260825T190317Z-5732`，并写入 pre.3 evidence。

fixed-wing SIH 的 Mission 事务另有独立证据：
`D:\Projects\Multi-Detect\artifacts\evaluation\px4-fixed-wing-sitl-auto-mission-acceptance-20260825T170332Z-62660.json`。
3 个 item 经 `MISSION_COUNT` + `MISSION_REQUEST_INT` 回读，`items_match=true`、`hash_match=true`，
canonical upload/readback SHA-256 为
`14acad2d86fa047ae8af8388f7f792a018793fa65d30c9be62e425c33b484018`。这只代表 pinned fixed-wing
SIH 的 software Mission 子门，不代表完整 VTOL/Gazebo/真实 HIL 或 REAL flight。

### Gazebo camera source boundary

The prerelease now has a reproducible headless Gazebo Harmonic sensor sub-gate:
`scripts/run_gazebo_sitl_camera_acceptance.ps1` launches the digest-pinned
`px4io/px4-sitl-gazebo` image with `gz_x500_mono_cam` and captures real image,
CameraInfo and NavSat messages. The latest evidence is
`D:\Projects\Multi-Detect\artifacts\evaluation\gazebo-sitl-camera-acceptance-20260825T193511Z-3908.json`
with SHA-256 `63EC2715ABDC54929DABE6975D1014693E0C69761394F63101B0803367E88C2D`.
This historical sensor result is a source proof only; it does not turn capture logs
into a QGC acceptance claim. `operator_visual_acceptance` remains `not_claimed`.

### Gazebo raw pixels through the production core

The persisted Gazebo frames were then converted by
`scripts/create_gazebo_raw_replay_fixture.py` into a raw-only recording. The bridge
copies only received RGB pixels and read-only NavSat telemetry; it does not consume
Scenario Ground Truth and does not produce flight-control or actuator output. The
recording is sent through the same production perception factory and
`LiveMissionRunner`, then to the real portable QGC RTSP/signed-metadata transport:

```text
source capture:
  D:\Projects\Multi-Detect\artifacts\evaluation\gazebo-sitl-camera-acceptance-20260825T194926Z-22752.json
  result JSON SHA-256: DB7C0705E6E281745B8A05A33507058D55A31A502D6D893B3DDD2307C8178823
  captured payload SHA-256: 6D677CAD22D8949BA7A8FA0BC081B0740680D118C8BD415F5759589F07847BC5
recording manifest:
  D:\Projects\Multi-Detect\build\qgc-replay-fixture-gazebo-v0.3.0-pre.3\recording\recording.manifest.json
  SHA-256: 87334899FB51950CE6BF2D82108BC5B7A43267B4538D8900CB0087B8F56421A4
QGC result:
  D:\Projects\Multi-Detect\build\e4-gazebo-qgc-20260826\e4.result.json
  SHA-256: 520B1BD6732E8FB35B1FE876E457A3C95111AB751C338287252E7E7ED3529926
```

The real QGC process observed `12/12` video frames and `12/12` metadata barriers,
with maximum ACK-to-metadata skew `0.0 ms`, RTSP helper exit `0`, and EOS/summary
received. This closes the Gazebo-pixel-to-QGC transport sub-gate, not manual Fly View
acceptance; `operator_visual_acceptance` remains `not_claimed`, and complete
fixed-wing/VTOL SIM, real Pixhawk/Jetson HIL, outdoor GNSS and REAL gates remain
pending for final `v0.3.0`.

### Historical G20 Android bench artifact

The locally validated Android 13/ARM64 bench artifact is:

`build-multidetect-android/MultiDetectGCS-G20-arm64-v8a-0.1.0-hil-debug-signed.apk`

It has package ID `com.multidetect.gcs`, version `0.1.0`, minimum SDK 28, target/compile SDK 36 and
contains only `arm64-v8a` native code. `aapt`, `apksigner` and `zipalign` validation passed; Android
host tests passed 23/23 and release lint has zero errors. The local artifact is signed by the
`Multi-Detect Debug` bench certificate and is not a production-distribution signature. Its SHA-256 is
`e8764211ff0913d4053095b339eaac47acaa47ae38f531d8c6b1301c82bd807a`.

This is a historical bench artifact only. The next Android release must use the canonical product
manifest and retain the current prerelease label until the v0.3.0 final acceptance gates pass.

No Android device was present during this build, so installation, startup, 1920x1200 layout, H.265
hardware decode, RTSP latency and touch-coordinate behavior still require a real G20 bench. The build
continues to compile both flight-control writes and physical release as `0`; installing this APK does
not authorize enabling either path. The Windows-hosted local build uses the bundled OpenSSL 3.1
development dependency; production CI must replace it with the pinned supported OpenSSL 3.5.6 path.

For a standalone Windows runtime, building the `Release` target is not sufficient: that directory
contains the executable but depends on the developer Qt/GStreamer environment. Use the outer repository
build wrapper, which reads the canonical manifest, configures CMake, runs the selected targets and creates
a portable runtime:

```powershell
cd D:\Projects\Multi-Detect
& .\scripts\build_multidetect_windows.ps1 `
  -Configuration Release -Parallel 8 -BuildSelfTests -InstallPortable
```

The historical `v0.3.0-pre.3` build completed in the fresh
`D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.3-release\` directory. The portable entry is
`D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.3-release\portable-v0.3.0-pre.3\bin\MultiDetectGCS.exe`.
The Release and portable executables are both `46,556,672` bytes with SHA-256
`5F462F8EDAD3677AE3CE88C0EC1B49CA9ECCA6B0B25A7E504A80B0BC7BBC0FD3`; the RTSP helper is
`75,264` bytes with SHA-256 `28CD41016BA93A406FC9B9E3B98B0809B4AB8675D9A04821E1DD64C495FC5C47`.
Three C++ protocol/wire self-tests exit `0`. The historical QGC/PX4 signed-metadata acceptance result is in
`D:\Projects\Multi-Detect\build\e4-qgc-20260825-222239\e4.result.json`, and the native Fly View
 screenshots are in `D:\Projects\Multi-Detect\build\e4-qgc-visual-20260825-224648\`.
The consolidated pre.3 evidence is
`D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.3-release\verification\v0.3.0-pre.3-evidence.json`
with SHA-256
`865E321F0F618220181BE45E2F7C7B48E467F47BC88A781DA5BA8BAE4B8F776B`.

The historical `v0.3.0-pre.2` build completed in the fresh
`D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.2-release\` directory. The 2623-step Ninja graph,
both C++ protocol self-tests, install/windeployqt and the portable dependency closure completed with
wrapper exit code `0`. Run that historical portable build from
`D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.2-release\portable-v0.3.0-pre.2\bin\MultiDetectGCS.exe`.
The Release and portable executables are both `46,525,440` bytes, PE FileVersion/ProductVersion
`0.3.0.0`, with SHA-256
`D5A096F69DA89CAA58E0227B85A184C228B310BD5DCA314FDE906E7379125769`. The portable tree has
`1012` files totaling `529,158,529` bytes; its `bin` has `232` files, including `62` Qt DLLs,
`Qt6SerialPort.dll`, `43` named GStreamer DLLs, QML and the `qwindows`/`qoffscreen` plugins. An
offscreen/software smoke process remained alive for `15 s`, produced empty stdout and stderr, and was
then stopped through the exact process object. This closes pre.2 `E3 BUILT`. The machine-readable evidence
manifest is
`build/windows-v0.3.0-pre.2-release/verification/v0.3.0-pre.2-evidence.json`, with SHA-256
`D9D0EE425AEE6976A077B69B3EB8AD4C1C8F34FC69E1F41286AB28C9CCF5FC86`.

The historical 2026-08-25 `v0.3.0-pre.1` build and portable deployment completed successfully. Run that
historical baseline from
`D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.1-release\portable-v0.3.0-pre.1\bin\MultiDetectGCS.exe`,
with that `bin` directory as its working directory. Its main executable is `46,525,440` bytes and has
SHA-256 `83BBAB5BD4D0C560E07DF1BBE79DE02F7815921DF2833524A598C74087F7349B`; the complete portable
dependency closure was present, and an offscreen smoke process remained alive for `15 s` with an empty
stderr before the exact smoke-test PID was terminated. The v0.2.0 portable tree remains the verified
rollback baseline. If an installer is enabled later, it excludes
dependency headers, static import libraries and Debug CRT binaries. Local development installers are
unsigned; a production-distribution build requires an approved Windows code-signing identity and a
separate signature verification gate.

Every successful portable deployment is written to an isolated `portable-v<version>` directory so it
cannot overwrite an active runtime. Record its portable EXE and optional installer SHA-256 in the outer repository's release
document before handing it to an operator. Evidence is graded separately as source, automated test,
native build, integrated Replay/SIM, HIL and real flight; see the outer
[`README.md`](../../../README.md) and
[`docs/qgc-windows-release.md`](../../../docs/qgc-windows-release.md).


### VTOL SIM vehicle gate (pre.3)

Run from the repository root:

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_px4_gazebo_vtol_scenario_acceptance.ps1 -ScenarioManifest .\configs\scenarios\river_rescue_vtol_001.scenario.json -EvidencePath .\artifacts\evaluation\px4-vtol-sitl-scenario-acceptance-20260826-final.json -MavlinkPort 18573
```

The result is `artifacts/evaluation/px4-vtol-sitl-scenario-acceptance-20260826-final.json` (SHA-256 `2E82C2453F2D7B28815235E02B06C793366782112E8329C0B679DC1EF01ECF6E`; container log SHA-256 `022EB07077DEB3216966C5BD5C21C4CCDFB8B8942A4EB381AC386D06C7247E88`). It is a real digest-pinned `standard_vtol` PX4/Gazebo vehicle-dynamics gate: heartbeat/type 22, GPS fix, ARM, fixed-wing transition, multicopter transition and DISARM all passed. The scenario runtime receives only `ScenarioRuntimeInput`; truth/expectations stay evaluator-only.

The QGC surface remains native `FlyView`/`FlightDisplayViewVideo` with QGC palette and controls; this runner does not create a second UI and keeps `operator_visual_acceptance=not_claimed`. Full fixed-wing/VTOL Mission, manual Fly View, HIL and REAL acceptance remain pending.

The fixed-wing SIH/QGC continuation run on 2026-08-26 is recorded at
`artifacts/evaluation/px4-sitl-qgc-operator-acceptance-continue-20260826T090000Z.json` with
SHA-256 `8B4271BE2FDEA1A62576EE77392B458774C65440BA9D1985F23AEFEADF18F2F7`. It passed the
same read-only signed-metadata boundary. The Windows wrapper resolves Docker from PATH first and
falls back to `C:\Program Files\Docker\Docker\resources\bin\docker.exe`; this is only tool discovery,
not a change to the PX4/QGC isolation policy.
## Native QGC E4/reconnect contract (v0.3.0-pre.3 amendment, 2026-08-26)

The custom plugin extends the native QGC `FlyView` / `PipView` and
`FlightDisplayViewVideo`; it does not create a second application window, a simulator-only view, or a
hand-drawn FixedWing HUD. `FlyViewVideo.qml` keeps PiP annotations read-only and enables target
interaction only while the video is in full view. `FlyViewCustomLayer.qml` composes the mission surface
with `QGCPopupDialog`, `QGCSlider`, `QGCToolInsets`, `QGCButton`, `QGCLabel`, `QGCPalette`, and
`ScreenTools`, preserving QGC spacing, colors, scale, and interaction conventions.

`REPLAY`, `SIM`, `HIL`, and `REAL` use the same controller, QML surface, and autonomy/perception core.
A mode changes only the runtime channel, sensor/dynamics implementation, and clock.

The canonical E4 order for every frame is:

```text
successful appsrc push -> frame_accepted
                        -> signed RuntimeStatus (type 23) sent
                        -> MultiDetectOperatorController adopts native state
                        -> authenticated matching RuntimeStatusAck (type 24)
```

A type 24 ACK proves only **native-controller post-adoption**. It does not prove QML rendering,
operator-visible correctness, or runtime-mode policy acceptance. Machine-produced evidence therefore
retains `operator_visual_acceptance=not_claimed`, `qml_rendering_proved=false`, and
`runtime_mode_policy_acceptance_proved=false` until a separate human Fly View acceptance is recorded.

`MultiDetectReplayRtspPublisher` assigns a monotonic generation to each media/appsrc session. A normal
QGC PiP/startVideo disconnect before the first frame or mid-stream may reconnect to a newer generation;
a late `unprepared` callback from the old media cannot clear the new appsrc. A wire frame retried across
generations emits exactly one `frame_accepted`, and only after a real successful push. No-client timeout,
push failure, malformed frames, and EOS without any client remain fail-closed conditions.

For an already configured build tree, rebuild and install the portable closure from the outer repository:

```powershell
cd D:\Projects\Multi-Detect
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_multidetect_windows.ps1 `
  -SkipConfigure -Configuration Release -Parallel 2 `
  -BuildSelfTests -InstallPortable
```

Expected outputs are:

```text
D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.3-release\Release\MultiDetectGCS.exe
D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.3-release\portable-v0.3.0-pre.3\bin\MultiDetectGCS.exe
D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.3-release\portable-v0.3.0-pre.3\bin\MultiDetectReplayRtspPublisher.exe
D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.3-release\verification\v0.3.0-pre.3-evidence.json
```

The exact pre.3 sizes and hashes written earlier in this file describe a pre-reconnect intermediate
snapshot. The final rebuilt EXE/helper identity must be taken only from the regenerated evidence JSON;
this amendment intentionally does not guess those values. It does not claim that a combined 300-frame
gate passed. Full REPLAY/SIM, PX4 Mission, HIL, REAL/outdoor GNSS, and operator visual acceptance remain
open before the final `v0.3.0` release.
## Authoritative pre.3 portable and 300-frame combined gate (2026-08-26)

This block supersedes the earlier pre.3 intermediate hashes and pending-gate note without deleting any
historical pre.1/pre.2/pre.3 evidence.

| Portable artifact | Bytes | SHA-256 |
| --- | ---: | --- |
| `MultiDetectGCS.exe` | `46,561,280` | `4C3584F984592FDDF9114EC9C08EC3C68F86C768BFAD4EB3D0F505A263FD55FE` |
| `MultiDetectReplayRtspPublisher.exe` | `79,872` | `AB3E0043DA4A124270BEE1D5EC75A717A9845CF37CB63A8ADC8A50B4752D206F` |

The authoritative run is
`D:\Projects\Multi-Detect\artifacts\evaluation\combined-sim-qgc-20260826T013505Z-64000`.
Its `combined-evidence.json` is `87,623` bytes with SHA-256
`6374DB67E985BBC003682BFC9F764B416604A9C3566A4A33D0164728D44219AA` and records
`gate_passed=true`.

The full transport barrier contains 300 Gazebo frames, 300 real RTSP `frame_accepted` events, contiguous
signed type 23 sequences `1..300`, and contiguous authenticated native-controller post-adoption type 24
sequences `1..300`. The RTSP helper exited `0`, sent EOS, and emitted its summary.

The native PiP state change was exercised against exact QGC PID `40048` and HWND `0x1C534FC`.
`WindowFromPoint(286,937)` returned that same HWND and owner PID before `SendInput` generated the normal
pointer click at `2026-08-26T01:35:40.3756314Z`. QSettings then changed
`QGCQml/MainFlyWindowIsMap` from `true` by QGC default to an explicit `false` value:

| | UTC mtime | SHA-256 |
| --- | --- | --- |
| before | `2026-08-26T01:35:38.3155174Z` | `BD2E993A77172B0668F6ADBF3DCA9F4F3C0083235F9F1B7258734F111CF9B578` |
| after | `2026-08-26T01:35:40.4183819Z` | `764677D852C516ACE28A070886277565D0282E8D09756B1A6A56EF96401C52B8` |

A normal native PiP swap does not restart the video receiver; the same run retained its contiguous
300-frame type 23/type 24 chain. The exact-window `PrintWindow(PW_RENDERFULLCONTENT)` captures are machine
smoke only: a GPU-backed Qt/GStreamer surface may appear black in `PrintWindow`. They therefore do not
change `operator_visual_acceptance=not_claimed` or `qml_rendering_proved=false`.

PX4 completed ARM, multicopter-to-fixed-wing transition, multicopter return, and DISARM. GPS was qualified
with fix type `3`, 10 satellites, 506 samples, a `0.011 m` maximum jump, and no false geolocation. Protected
UDP `14550` was unchanged, all test ports were released, and the combined-owned container was stopped and
removed. Real HIL/REAL, outdoor camera/GNSS hardware, complete PX4 Mission, and human Fly View visual
acceptance still block the final `v0.3.0` release.
## Sole authoritative pre.3 combined run (2026-08-26T02:08Z)

The sole current successful run is
`D:\Projects\Multi-Detect\artifacts\evaluation\combined-sim-qgc-20260826T020829Z-57624`.
Its `combined-evidence.json` is `87,530` bytes, SHA-256
`62CFCA2B42EE1A9A69676B91220476609D1505DA46DE36C187E2A85BBA14EB6F`, with
`gate_passed=true`. It binds Release/portable QGC `46,561,280` bytes / SHA-256
`7A4D8B90F0DF0680536B9A91BEF36177542153F97745049D83D25B13BF52554D` and the portable RTSP
helper `79,872` bytes / SHA-256
`AB3E0043DA4A124270BEE1D5EC75A717A9845CF37CB63A8ADC8A50B4752D206F`.
The earlier `T013505Z-64000` / `4C3584F9...` identity remains historical and is not current.

Before launching QGC, the wrapper uses PowerShell `Remove-Item Env:` to make
`MULTIDETECT_DISABLE_VEHICLE_LINK`, `MULTIDETECT_HIL_UI_AUTO_EXERCISE`, and
`MULTIDETECT_HIL_UI_AUTO_EXIT` truly absent. It does not assign `""` or `"0"`, because Qt's fail-closed
environment parsing can still treat an empty or present variable as enabled. Evidence records
`acceptance_only_environment_absent_before_launch=true` (`env_absent=true`),
`hil_ui_auto_exercise_log_count=0`, and `hil_ui_auto_exercise_disabled=true`.

The 300-frame sensor deadline is dynamically `180 s`. Run `combined-sim-qgc-20260826T020108Z-52536`
is a historical failed retry that exposed the old fixed `100 s` timeout; its `gate_passed=false` result
must not be cited as successful acceptance.

The authoritative run has contiguous type 23 and post-adoption type 24 sequences `1..300`, RTSP helper
exit `0`, EOS, and summary. Its exact QGC PID/HWND is `55056 / 0x18D20DE`; a normal native PiP swap does
not restart video. Screenshot semantic inspection shows the native badge `环境 SIM · HEALTHY`, but the
captures remain machine smoke because a GPU-backed surface may appear black through `PrintWindow`:
`operator_visual_acceptance=not_claimed`, `qml_rendering_proved=false`.

PX4 fixed-wing transition/return, qualified GPS, unchanged UDP `14550`, released ports, and container
removal passed. Real HIL/REAL, outdoor hardware, complete PX4 Mission, and human Fly View acceptance
still block final `v0.3.0`.

## Native QGC / VTOL Mission contract (v0.3.0-pre.8)

The canonical version tuple is release label `0.3.0-pre.8`, Python `0.3.0rc8`, and CMake/PE numeric
`0.3.0`. The historical pre.7 Windows Release/portable artifact, sizes, SHA-256 values, and software regression are recorded in the repository release notes. The current combined SIM/QGC machine gate is
`D:\\Projects\\Multi-Detect\\artifacts\\evaluation\\combined-sim-qgc-20260826T125250Z-62292\\combined-evidence.json`
(`gate_passed=true`, 600 frames, 7-item VTOL Mission). The pre.2/pre.3/pre.4 blocks above retain their historical identity; this machine gate still does not represent HIL/REAL, outdoor hardware, or human Fly View acceptance.

### One native QGC surface

The product must continue to use upstream QGC `FlyView`, `FlyViewVideo`, `PipState`,
`FlightDisplayViewVideo`, native toolbar/map PiP, and the native gimbal/tracking pointer chain.
Multi-Detect may add only an in-surface overlay using `QGCPalette`, `QGCButton`, `QGCLabel`,
`QGCSlider`, `QGCPopupDialog`, `QGCToolInsets`, and `ScreenTools`. PiP contains read-only annotations;
selection, LCK/TRK, cancel, depth sampling, and other target operations are enabled only in the full-video
state.

`Window`, `ApplicationWindow`, a second video surface, a separate simulator, simulator-only QML, a
hand-drawn FixedWing HUD, and a custom Multi-Detect theme are architecture violations. Computer Use,
SendInput, and screenshot automation may interact with the exact native QGC PID/HWND only; they may not
construct a replacement acceptance UI.

### Exact-client desktop capture

The earlier pre.4 combined wrapper resolves the exact QGC PID and owner HWND, enables Per-Monitor V2 DPI, maps
`GetClientRect` through `ClientToScreen`, requires one-monitor physical bounds, restores/foregrounds the
window, temporarily sets topmost, calls `DwmFlush`, enumerates external top-level occlusion, and checks a
25-point `WindowFromPoint` ownership grid. It then calls desktop
`CopyFromScreen(SourceCopy|CaptureBlt)` for the exact client rectangle only and restores the cursor,
window placement, topmost state, foreground window, and DPI context.

`scripts/validate_qgc_desktop_capture.py` evaluates a normalized video-only ROI that excludes native
toolbar, PiP, crosshair, runtime badge, action, depth, and status chrome. Its hard gate requires both
captures to be nonblack/nonuniform, RTSP streaming and decoding evidence, and at least five native
post-adoption type 24 sequence advances. Temporal changed pixels, MAD, and dHash are diagnostic because a
Gazebo scene may be nearly static. Even a passing run proves only a transport-bound visible video surface:

```text
exact_source_frame_identity_proved=false
qml_rendering_proved=false
operator_visual_acceptance=not_claimed
```

The older `PrintWindow` captures remain pre.3 machine smoke and must not be relabelled as pre.4 visible-
video or operator evidence.

For a standalone desktop smoke without the full PX4/Gazebo wrapper, run
`scripts/capture_qgc_native_style.ps1`. It launches the already-built portable executable, chooses a
monitor that can fit the existing QGC window with the fewest occluders, captures only that HWND's client
rectangle, and restores all temporarily moved/minimized windows. The generated JSON is deliberately
machine-only: it records `single_native_surface_machine_check=true` when the capture is unobstructed,
while `operator_visual_acceptance`, `qml_rendering_proved`, and `desktop_capture_gpu_surface_proved`
remain false until their separate gates are performed.

### Bound VTOL Mission path

`src/multidetect/px4_mission.py` provides a pure, deterministic, no-network contract/compiler/transaction.
It accepts runtime geometry only, rejects truth/evaluator channels, and emits at most 64 canonical MAVLink
2 `MISSION_ITEM_INT` items under adapter `px4-vtol-mission-int-v1` and primary Mission type `0`. The
compiled lifecycle must contain VTOL takeoff, forward transition, route/search, loiter, return to
multicopter, and exactly one terminal VTOL land. Prompt/task/plan/context, airframe, geofence, vehicle,
runtime descriptor, and runtime provenance hashes are bound to the transaction.

Every item sequence must be requested with `MISSION_REQUEST_INT`; legacy `MISSION_REQUEST` is denied.
Upload ACK, exact-source INT-only readback, and canonical protocol hash equality are required before commit
and an exact PX4 `AUTO Mission` start permit.

`px4_gazebo_qgc_router.py` keeps the QGC channel read-only and grants the evaluator only bounded Mission
frames plus the four expected lifecycle/transition commands. Target system/component, Mission type, item
count, commands, frames, and coordinates are validated; `MISSION_CLEAR_ALL` and unrelated writes are
blocked and counted. `scripts/vtol_sitl_mission_acceptance.py` is a headless, test-only adapter that
attaches to the container/router/PX4 already owned by the combined wrapper. It does not create or stop a
second simulator and does not own a UI.

The combined wrapper has source integration for the Mission helper, active-ready synchronization, a
capture of the same native Fly View, and Mission evidence. The current combined Mission run is
`D:\Projects\Multi-Detect\artifacts\evaluation\combined-sim-qgc-20260826T125250Z-62292\combined-evidence.json`
with `gate_passed=true`; all real HIL/REAL, outdoor camera/GNSS/hardware, and human Fly View gates remain
incomplete. `operator_visual_acceptance=not_claimed` and final `v0.3.0` remains unreleased.
