# Implementation roadmap

目标平台是一架通用折叠串联固定翼无人机。固定摄像头、Jetson、Pixhawk 和数传始终安装；灭火载荷是可选任务模块。软件从配置中的载荷清单自动推导能力：空清单只执行巡检告警，有载荷时才允许进入安全检查和人工授权流程。

## 当前 pre.8 户外定位/SIM 资源收口（v57）

- [x] 将三轴射线不确定性失败映射为 Python/QGC 共用的 `RANGE_STATUS` bit 31，保持内部诊断细粒度并
  让操作端稳定显示 fail-closed 状态。
- [x] 为三种机型场景加入仓库自带 GeoJSON 地图和严格 `.dem.json` 网格，校验场景 ID、WGS-84、
  Gazebo world origin、网格边界、文件 containment 与 SHA-256。
- [x] 将 map/DEM 纳入 simulator aggregate asset identity、外部 runtime 环境和 VTOL acceptance 的
  真实文件 availability 判断。
- [x] 在共享 `MultiModalRangingEngine` 中加入局部 DEM 高程/坡度采样、射线-地形交点和坡度不确定性
  传播；REPLAY/SIM/HIL/REAL 必须复用同一实现。
- [x] 将同一合格 raw GPS 样本的绝对 MSL 高度贯通 Pixhawk、live、raw replay 和 GNSS sidecar；配置
  DEM 后，时间偏斜/越界/无交点禁止平地回退，并避免同源 AGL 二次融合。
- [x] 增加 `--ranging-dem` CLI、严格 WGS-84/MSL/原点/100 km 域校验和三机型 v52 确定性 hash binding。
- [x] 建立非平坦 `terrain_ranging_validation_001` SDF/DEM 对，并从 SDF transform 独立恢复解析平面
  truth；41 个表面样本与 7 条射线（6 条跨单元、1 条垂直基准）通过严格误差门。该结果只关闭
  deterministic asset-geometry 子门。
- [x] 在外部 PX4/Gazebo 中实际加载非平坦 world，采集 image/NavSat/Odometry，并由 qualified NavSat
  MSL − scenario DEM MSL 产生 AGL；v54 关闭 sensor-runtime/高度基准子门，但该记录只在 home 采样。
- [x] 在 x500 disposable PX4/Gazebo 中让 truth-free synthetic target 经过共享 perception → AI →
  `MANUAL_CONTROL` 路径，观察 PX4 `GLOBAL_POSITION_INT` 最大 `0.818282 m` 位移；v55 不使用
  truth/pose injection，只关闭单机型 reported-position response 机器子门。
- [x] 对 v55 current-code 执行全仓库回归：2,907 tests 中 `2906 passed, 1 skipped, 0 failed, 0 errors`；
  唯一跳过为 Windows symlink 权限项，JUnit SHA-256 为
  `A2C2595C49D0B663468D09274D22976CC1B4D9128425BB23458445B1C5397F51`。
- [x] 使用 5 帧真实 Gazebo BGR24 camera 像素经过 quarantined candidate、共享 semantic ground-contact
  anchor、DEM/GNSS ranging 与 WGS-84 target geolocation：5/5 落图，最大水平误差 `0.674040 m`、
  最大帧间漂移 `0.035197 m`；近地平线负向 5/5 不落假点。两份 evidence 均由当前输入/parity
  独立重跑复现。该结果只关闭单高度 software/SIM 算法子门。
- [x] 将 semantic image anchor 纳入 `core_algorithm` parity，重新生成五组共享契约并从六份不可变
  external source 重建三机型 join/revalidation；当前 E7 report-only 仍为 32 blockers。
- [x] 对 v57 current-code 执行全仓库回归：3,233 tests 中 `3232 passed, 1 skipped, 0 failed, 0 errors`；
  唯一跳过仍为 Windows symlink 权限项；JUnit 469,702 bytes / SHA-256
  `2FD79F5349A56DBDB72EB6AD1A77653707310D141F99CF996A36FFA3A23A9B6F`。
- [ ] 以独立机械提交清理 410 个历史 Ruff format 文件；31 个 parity 绑定 Python 源会随之改变，
  因此完成后必须重新生成 parity，并重跑定位、三机型 external join、E7 和全量回归，不能删除 format
  gate 或复用旧 evidence 冒充完成。
- [ ] 将上述真实 Gazebo camera → shared ranging → target geolocation 扩展到多个坡高/距离，并经过
  evidence card 与原生 QGC Fly View 完成动态精度和人工视觉验收。
- [ ] 生成 fixed-wing 与 VTOL 对等动态 response evidence；只有明确纳入发布门并重跑 E7 wrapper 后，
  才能更新三机型 external source 或 blocker 数。
- [ ] 使用真实 GNSS、已测相机外参和多距离/多坡度 survey truth 完成室外定位验证，再进入 HIL/REAL。

## Phase 1: software-only mission loop

### 1. Unified aircraft configuration

- [x] One mission configuration supports zero or multiple payload slots.
- [x] Zero payload means patrol-only; it is not treated as exhausted inventory.
- [x] RGB-only patrol may issue a clearly bounded fire alert after multi-frame confirmation; payload-capable deployment keeps independent corroboration and safety gates.
- [x] Disposable missions still require exactly one approved non-hazardous payload.
- [x] Deployment remains simulation-only and human authorization cannot be disabled.
- [x] Live payload mode can complete an explicitly enabled operator-triggered `FakePayloadPort` HIL cycle.
- [x] Connect the authenticated localhost inert-controller exchange above `FakePayloadPort` through
  a mission adapter; bind the consumed authorization and scene identity, map rejection/timeout to
  terminal fail-closed states, and retain mandatory independent confirmation after `EXECUTED`.
- [x] Add a separately keyed independent bay/departure-sensor HIL message with distinct sensor
  identity, release/slot/time/sequence binding, mutation replay protection and fail-closed evidence
  handling; controller module identity cannot be registered as the independent source.
- [x] Carry independent confirmation over a separate localhost UDP evidence channel that emits no
  commands, ignores invalid datagrams, and cannot fabricate success on timeout.
- [x] Add an explicit live-camera two-channel inert-HIL coordinator. It requires the existing
  operator-triggered simulation flag, three different environment-only keys/key IDs, distinct
  controller/sensor identities, bounded waits and localhost-only transports; the default in-memory
  fake cycle remains unchanged.
- [x] Add fixed-wing minimum/maximum ground-speed limits and a still-air, point-mass HIL
  release-window planner that returns advisory `WAIT/READY` metadata only.
- [x] Bind the configured HIL release window into the safety decision and authorization scene
  digest; moving outside the window invalidates readiness.
- [x] Add `release-window-check` for geometry-only bench checks with explicit no-control output.
- [x] Add a one-command software acceptance report covering patrol alert-only, authorized
  fixed-wing `FakePayloadPort` HIL and overlapping-person fail-closed behavior.
- [x] Add a separate Mode-2 end-to-end acceptance that uses one authenticated, signed loopback
  session for target selection, unique independently confirmed fire resolution, continuous-slide
  confirmation, status return, separate authorization and exactly one simulated release. Verify
  person/ordinary-vehicle exclusion, target-switch and timeout revocation, and person-entry veto
  with no flight-control or physical payload interface.
- [x] Add Mode-3 end-to-end signed HIL for both vehicle and person selections. Require a continuous
  target-bound slide before centering/approach advice, and prove occlusion, loss, `AVOID`, `INVALID`,
  target switch and old-confirmation replay enter or preserve a latched advisory abort with no
  actuator interface.

Acceptance evidence:

```powershell
python -m multidetect validate-config configs/missions/fire_patrol.demo.json
python -m multidetect replay configs/missions/fire_patrol.demo.json examples/fire_mission_replay.jsonl
python -m multidetect replay configs/missions/fire_suppression_fixed_wing.demo.json examples/fire_fixed_wing_hil_replay.jsonl --simulate-authorized-cycle
```

The replay must emit one deduplicated `fire_alert`, remain in `searching`, create no authorization challenge and submit no fake release request.
The fixed-wing HIL replay must expose a `READY` advisory window, create a redacted human
authorization challenge, submit exactly one fake request only after approval, require both simulated
feedback sources and finish in `return_requested` with physical control disabled.

### 2. Live patrol console

- [x] Show frame-by-frame detections, confirmed tracks, dwell time and mission capability.
- [x] Show an immediate fire-confirmed banner.
- [x] Add a persistent event list, target queue, telemetry strip and map-ready position panel.
- [x] Add alert acknowledgement and visible delivery failure state.
- [x] Keep camera buffering and duplicate-frame history bounded so a long patrol cannot accumulate
  an unbounded image queue or frame-ID set; frame timestamps remain strictly increasing.

### 3. Data-link software boundary

- [x] Produce a versionable JSON fire-alert envelope in real time.
- [x] Provide recording and JSON Lines publishers for tests and local integration.
- [x] Audit successful and failed live alert delivery attempts.
- [x] Add an optional SQLite outbox that persists before send and retries pending alerts after restart.
- [x] Bound delivered outbox retention transactionally while never pruning pending or failed alerts.
- [x] Stream live audit events to disk with fsync while bounding the in-memory event window.
- [x] Add correlated receiver ACK validation and bounded exponential retry/backoff with a loopback HIL transport.
- [x] Add an HMAC-SHA256 authenticated UDP sender/receiver with signed correlation, timestamp checks and persistent receiver deduplication.
- [x] Run the camera/ONNX/tracker task and ground receiver as separate local processes: 105 predictions, one authenticated alert, one correlated ACK, delivered aircraft outbox row and matching ground dedup row.
- [x] Prove restart recovery with the real UDP adapter: ground offline leaves the original alert pending; the next run retransmits the same alert ID before new frames and clears it only after authenticated ACK.
- [x] Add a bounded authenticated alert-link bench that emits new alert IDs, retry/missing-ACK accounting and ACK P50/P95 metrics; loopback output is explicitly software baseline and cannot satisfy a hardware gate.
- [x] Hash-bind a passing REAL authenticated alert-link bench into the E7 manifest; re-open and re-verify its source report on every evaluation while keeping prerelease manifests backward-compatible.
- [ ] Implement and validate the authenticated ACK transport on the selected real data link.

### 4. Model and replay validation

- [x] Add manifest verification for artifact SHA-256, model version, class order, strict Nx6 fields, prohibited uses and production approval.
- [x] Add a `model-check` gate for SHA-256, active provider, strict `N x 6` output and synthetic latency.
- [x] Bind runtime box-coordinate interpretation to the model manifest contract.
- [x] Bind fire and safety-object artifacts to distinct manifest roles; runtime rejects a fire-candidate manifest supplied as person-safety evidence.
- [x] Verify every supplied model manifest before loading any ONNX graph, and keep person-detector health false unless safety-object role evidence is verified.
- [x] Stream normalized predictions and inference latency for every processed frame.
- [x] Add a frame-aligned IoU evaluator for per-class/overall precision, recall, false alarms, misses and P50/P95 latency.
- [x] Add an optional GNSS-to-camera body-NED lever-arm correction to the shared ranging/WGS-84
  projector; rotate it with roll/pitch/heading, propagate survey sigma into ground/bearing
  uncertainty, persist it in ChArUco calibration provenance, and keep zero-valued legacy files
  byte/behavior compatible.
- [x] Propagate the common camera-mount survey sigma independently through pitch/yaw/roll NED ray
  endpoints for range and bearing, and fail closed when any one-sigma mount-axis endpoint crosses
  the horizon; keep REPLAY/SIM/HIL/REAL on the same ranging implementation.
- [x] Run the full local-camera path through a real constant-output ONNX HIL artifact: 120 frames, one multi-frame-confirmed patrol alert, zero authorization/payload events and complete prediction/audit logs.
- [x] Add a hash-bound model review envelope that joins the candidate manifest, post-NMS `N x 6`
  model-check report, deployment-domain accuracy report and four independent governance decisions;
  it never mutates the manifest, and synthetic HIL remains ineligible for production approval.
- [ ] Add a reviewed post-NMS `N x 6` fire/smoke ONNX artifact and manifest.
- [ ] Build day, night, haze, reflection, smoke-only and no-fire replay sets.
- [ ] Measure deployment-domain per-class precision, recall, false alarms, missed detections and latency with the completed evaluator.
- [ ] Calibrate confidence and multi-frame confirmation thresholds from deployment-domain data.
  A reproducible per-class sweep and lighting/content strata report now exists for the local
  validation set; deployment-domain RTSP imagery is still required for this gate.

Phase 1 exit criteria:

- Patrol-only and payload-capable configurations both pass automated tests.
- Local camera and recorded video can run continuously without unbounded frame backlog.
- Confirmed fires are tracked and emitted once per configured cooldown window.
- Every alert and authorization change is auditable.
- No module can send flight-control or physical release commands.

## Phase 2: Jetson and RTSP integration

- [x] Add a hardened unprivileged systemd deployment template for patrol-only Jetson operation.
- [x] Add a credential-redacting static Jetson profile preflight that checks the patrol-only mission,
  model class/coordinate contract, RTSP syntax, read-only Pixhawk endpoint and authenticated alert
  settings without opening hardware or ONNX Runtime.
- [ ] Install and validate the package on the actual Orin Nano/JetPack image.
- [x] Implement deterministic TensorRT → CUDA → CPU provider ordering and bounded suffix fallback
  when default session construction fails; explicit provider lists remain strict and selection attempts
  are emitted in model-check/Jetson bench evidence.
- [ ] Validate the provider order and failover on the actual Orin Nano/JetPack image.
- [x] Add bounded capture/inference P50/P95 metrics, average FPS and reconnect counters.
- [x] Add configurable RTSP/local-camera reconnect attempts without a stale-frame queue.
- [x] Keep credential-bearing RTSP URIs out of service process arguments and redact them from application camera errors.
- [x] Add multi-frame `camera-check` with FPS, capture P50/P95 and reconnect reporting; local 640×480/120-frame smoke test passed with zero reconnects.
- [x] Raise the Jetson soak gate to 60 minutes/54,000 frames with >=15 FPS, <=66.7 ms inference
  P95 and a capture-queue high-watermark of at most one; the prior 30-minute artifact is retained
  only as a baseline and no longer satisfies the verifier.
- [x] Sample process RSS throughout the soak after a 60-second warmup; reject missing time-series
  evidence or more than 256 MB robust start-to-end growth, and report the hourly regression slope.
- Validate RTSP reconnect, end-to-end video-overlay latency, dropped frames, thermal load and power budget on the target Jetson.
- [x] Show camera/model health and reconnect count; audit and stop on acquisition or inference failure instead of treating it as an empty scene.
- Current sensor profile is RGB-only. Thermal synchronization is out of scope unless the hardware
  is explicitly changed in a future revision.

Exit criteria: representative RTSP video runs for the required mission duration with measured latency, temperature, power and reconnect behavior.

## Phase 3: Pixhawk V6X integration

- [x] Begin with a read-only MAVLink provider that never transmits heartbeat, commands, parameters, missions or stream requests.
- [x] Decode geographic position, relative altitude, attitude, speed, heading, battery, satellite count, armed state, flight mode and mission sequence.
- [x] Display link/position freshness, aircraft coordinates and a relative aircraft trail in the live console.
- [x] Add a standalone `pixhawk-check` sampler that proves the diagnostic path transmits zero messages.
- [x] Passively parse a complete `AUTOPILOT_VERSION` only from the accepted heartbeat
  system/component, redact raw `uid2`/`uid` behind a domain-separated SHA-256, expose version age and
  rejection counters, and add a zero-transmit `--require-autopilot-version` diagnostic gate.
- [x] Promote the sanitized `AUTOPILOT_VERSION` identity hash into newly provisioned signed HIL/REAL
  RuntimeProfiles and all six Mission-feedback E7 leaves while keeping legacy profile documents
  readable for diagnostics; provision now requires a same-source version frame observed during its
  own receive-only, zero-transmit probe window.
- [x] Separate legacy RuntimeProfile diagnostics from flight authority: every HIL/REAL launcher and
  REAL output request now requires a profile-bound, live-matching firmware identity, and the
  low-level flight provider independently rejects firmwareless profiles.
- [x] Separate launcher-probe authorization from actual-provider liveness: every new flight provider
  starts output-locked, must independently re-attest a fresh same-source heartbeat and firmware
  identity before any stream request or control output, and revokes/clears that attestation on
  timeout, stable identity change or close. CLI, Mode-3 approach status and fixed-wing audit events
  consume this dynamic state instead of a startup snapshot.
- [x] Make the native QGC Mode-3 banner consume the same dynamic authority: “aim active” requires
  current flight-control enablement, while locked/stale/abort/cancel states explicitly state that no
  attitude or mode command is being sent. Keep the shared Fly View and QGC-native controls.
- [ ] Capture `AUTOPILOT_VERSION` from the real V6X and use that exact identity in six independent
  HIL/REAL × fixed-wing/multirotor/VTOL profile/report leaves.
- [x] Add a separate, single-use `pixhawk-param-backup` path that requires explicit acknowledgement,
  sends exactly one `PARAM_REQUEST_LIST`, preserves PX4 bytewise integer values and raw wire bytes,
  rejects incomplete lists, and has no parameter-write, command, mission or actuator send path. Its
  real pymavlink localhost UDP HIL passed; it has not been run against the real V6X.
- [x] Add strict offline `pixhawk-param-verify` and `pixhawk-param-diff` commands. Verification
  rejects schema/invariant/value/hash tampering; diff denies every unlisted change by default and can
  require named expected changes. Both transmit zero messages and explicitly state that the embedded
  list hash is self-consistency evidence, not cryptographic authentication. The explicitly
  authorized real-V6X read completed 1102/1102 parameters with one request, zero writes and zero
  flight/mission/actuator messages; the real parameters were not changed.
- [x] Add an offline `pixhawk-link-audit` that separates GR01/TELEM1 at 115200 from the
  Jetson/V6X board-internal Ethernet primary path and the optional UART1/TELEM2 921600 fallback.
  The real snapshot passes both required primary configurations; TELEM2 is reported as disabled,
  optional and not physically verified. The audit opens no transport and sends zero messages.
- [x] Add a read-only mission-lifecycle gate: wait for fresh link/position, armed state, approved auto mode and configured mission sequence before entering search.
- [x] Verify the board-internal Ethernet path on the real V6X: `192.168.0.3:14550` produced only
  MAVLink 2 from system/component `1:1`; the project receiver matched PX4 fixed-wing identity and
  transmitted zero messages. The current `UNINIT` state and absent global position remain correctly
  blocked as flight-readiness issues rather than being misreported as a link failure.
- [ ] Compare every displayed field and freshness transition with QGroundControl on the real V6X link.
- [x] Add a read-only, HMAC-authenticated file HIL contract for allowed-area, geofence-health and release-zone-clear evidence, bound to mission ID, fresh Pixhawk position, timestamp and monotonic sequence; every validation failure restores unknown predicates.
- [ ] Replace the file HIL bridge with independently validated real geofence, allowed-area and deployment-zone evidence; generic telemetry must not imply permission.
- [x] Keep route execution, stabilization, failsafe and return-to-launch inside Pixhawk/PX4; the Jetson lifecycle observer sends no commands.
- [x] Exercise the real pymavlink serialization and UDP receive path with a telemetry-only
  fixed-wing HIL emitter: all displayed fields matched, 29/30 samples had fresh link/position,
  stale transition was verified, the receiver transmitted zero messages, and zone predicates
  remained unknown.
- [x] Run one combined Live software HIL with real pymavlink UDP input, authenticated position-bound
  zone evidence, observed armed/AUTO/mission-sequence lifecycle, signed G20 UDP selection and
  authorization, automatic authenticated inert-controller feedback and a separate independent
  confirmation UDP channel. Jetson transmitted zero Pixhawk messages; camera/model and all hardware
  remained simulated.
- [x] Run the official PX4 fixed-wing SIH/SITL process through the strict passive receiver and the
  Live lifecycle path. The original run exercised a local camera; the repeatable script now fixes
  the source to clock-paced `synthetic://patrol`, opens no local/network camera, and retains the same
  unarmed `LOITER` fail-closed assertions with zero authorization, payload and transmitted MAVLink
  events. Stopping SITL makes the fresh-link gate fail closed on isolated UDP 14650.
- [x] Add an explicit, Docker-confined positive patrol option. It arms only the newly created SITL
  process, permits `LOITER` for gate-plumbing HIL, observes `standby -> navigating -> searching`,
  emits one deduplicated patrol alert, creates no authorization/payload action and disarms before
  cleanup; Multi-Detect still transmits zero MAVLink messages. It is not an AUTO mission test.
- [x] Exercise the complete observed lifecycle through an armed PX4 AUTO mission in an owned,
  disposable software-only SITL container. Mission upload, parameter overrides, arm and mode
  changes are confined to that container; Multi-Detect remains receive-only and observes Mission
  sequence progress, movement and one patrol alert with zero authorization or payload action. The
  current no-camera run processes 900 deterministic frames at about 29.69 FPS with 900 target-pool,
  short-term-tracking, patrol-advisory and monocular-avoidance updates; avoidance is valid after its
  single warmup frame, capture backpressure is zero, and no camera device or URL is opened. The same
  armed `MISSION` run now retains ten identities through
  `DETECTED -> LOCKED -> TRACKING -> OCCLUDED -> REACQUIRING -> RECOVERED`, reaches LOST only after
  the conservative branch, recovers the same ID with strong ReID in 0.15 s, retains the background
  lock and emits only a human-confirmed return-observe advisory. The advice remains `DEGRADED`
  because generic SITL telemetry does not prove geofence health; application MAVLink writes remain 0.
- [x] Validate PX4 datalink-loss Hold behavior in the same strict container boundary. With a
  loopback-only GCS heartbeat and SITL-only `COM_DL_LOSS_T=5`, `NAV_DLL_ACT=1`, the fixed-wing
  transition was `MISSION -> LOITER`; heartbeat recovery cleared `gcs_connection_lost` but retained
  LOITER until an explicit operator mode change. Protected UDP 14550 was unchanged, Multi-Detect
  transmitted zero MAVLink messages and no real V6X or payload controller was contacted.
- [ ] Repeat the AUTO mission and datalink-loss acceptances with a PX4 SITL build exactly matched to
  the real V6X firmware. The current pinned `v1.18.0-beta1` SIH evidence is interface/failsafe
  evidence only and does not validate real airframe behavior.

Exit criteria: loss or staleness of required telemetry is visible and prevents deployment readiness without affecting Pixhawk failsafes.

## Phase 4: payload hardware-in-the-loop

- [x] Define and validate a versioned read-only inventory evidence contract with HMAC, key ID, timestamp, monotonic sequence, rollback rejection and same-sequence content consistency for the file-based HIL bridge; device key provisioning remains pending.
- [x] Verify module identity, exact slot IDs/types, lock state, controller/interlock health and independent presence sensors.
- [x] Fail closed in live mode when the payload controller or inventory evidence is unavailable.
- [x] Revoke pending or approved authorization when inventory becomes unknown or mismatched.
- Test only with inert, non-hazardous loads in HIL and a restrained ground rig.
- [x] Inject software/HIL disconnects, duplicate/mismatched inventory, stale authorization, failed slot/interlock, wrong feedback, timeout and uncertain release; physical jam testing remains pending.
- [x] Define an HMAC-authenticated, bounded inert-load HIL request/result protocol bound to
  mission, module, slot/type, release ID, consumed authorization identity, target revision, scene
  digest, ruleset, expiry and monotonic sequence. It rejects tamper, replay mutation, stale data,
  concurrent slots and success reports with unhealthy interlocks.
- [x] Validate the protocol across a real localhost UDP socket with bounded identical-request
  retry, first-response loss, controller-side idempotent result caching, authenticated rejection,
  and no implicit execution unless the inert-HIL flag is explicit.
- [ ] Select and connect an independently reviewed real controller transport; the localhost HIL
  transport is not mission-connected and has no serial, CAN, GPIO, PWM, MAVLink actuator or
  physical output.
- Never allow perception output to call a release actuator directly.

Exit criteria: every uncertain or inconsistent condition stays locked, no automatic retry occurs, and two independent feedback sources are required for confirmation.

## Phase 5: controlled field validation

- Complete hazard analysis, operational approval and test-site procedures.
- Validate launch, patrol, tracking, approach, abort, egress and return separately.
- Use approved inert loads before any actual fire-suppression payload.
- Add the real non-hazardous payload only after independent safety review.

## Phase 6: G20 / GR01 operator link

- [x] Confirm the documented camera stream, GR01 interfaces, serial rates and radio bandwidth.
- [x] Define strict target-selection and tracking-status domain messages without a live transport.
- [x] Keep target selection separate from payload deployment authorization.
- [ ] Bench-test simultaneous RTSP clients through an onboard Ethernet switch.
- [ ] Verify bidirectional IP packets between Jetson and the custom G20 application over GR01.
- [x] Implement and deterministically mutation-fuzz the compact signed `TUNNEL` payload codec, bounded ACK retry, TTL, correlation and replay protection.
- [x] Wrap the codec in signed, addressed MAVLink2 `TUNNEL` byte frames; verify software
  round-trip, source/target identity, tamper rejection, replay rejection and unrelated-message rejection.
- [x] Provide a no-control UDP diagnostic server/client and verify a real localhost socket round-trip.
- [x] Add a separate compact read-only mission-status stream for phase, safety, authorization
  display state, payload count and fixed-wing release-window advisories; verify HMAC,
  MAVLink2 and localhost UDP round-trips without adding a control path.
- [x] Add a separate 86-byte read-only safety-status stream with a versioned 19-rule registry,
  present/pass/deny/unknown masks, target/ruleset binding, HMAC, signed MAVLink2, UDP round-trip,
  change-driven delivery and a 1 Hz unchanged heartbeat.
- [x] Add and validate a platform-neutral G20 viewport transform for aspect-fit letterbox,
  center-crop, black-bar rejection and 0/90/180/270-degree source/display box round trips; wire
  boxes remain normalized to the original source frame.
- [x] Add a separate authenticated G20 authorization challenge/decision/ACK contract with nonce
  confinement, full published-snapshot binding, bounded retry, replay protection, one decision per
  challenge and a final current-safety recheck; approval only advances mission state and never
  requests physical release.
- [x] Add Mode-2 payload-target challenge/continuous-slide/ACK/status messages bound to both the
  selected object and the independently confirmed fire aimpoint. Complete Python/UDP/QGC golden
  vectors, a native QGC slider, Windows Release compilation, and Windows plus Jetson headless HIL;
  real QGC-to-Jetson touch interaction remains a separate bench gate.
- [x] Close the equivalent Mode-3 signed selection/slide/status loop for arbitrary tracked classes
  and rerun it headlessly on Windows and Jetson Python 3.10. Keep all centering, approach and bounded
  climb outputs advisory-only; real QGC touch and PX4 SITL flight behavior remain separate gates.
- [x] Add a native QGC custom-build source skeleton with raw-video-local target overlay, explicit
  touch-selection mode, patrol/payload display modes, tracking/safety/authorization panels and a
  deterministic software-only UI simulator. Compile-time and QML invariants keep flight-control
  writes and physical release disabled.
- [x] Compile and run the custom desktop QGC source against the pinned Qt/MSVC toolchain. A concurrent
  isolated HIL with real PX4 fixed-wing SITL and a metadata-only Jetson component completed target
  selection, tracking/safety state, challenge-bound approval and ACK with QGC exit code 0. The strict
  router forwarded only read/status traffic and kept flight, parameter, mission, actuator and payload
  writes disabled.
- [ ] Visually validate the 1280x720-to-1920x1200 viewport vector and all UI transitions on a real
  1920x1200 G20-class render; the deterministic transform tests and desktop HIL do not replace this.
- [ ] Implement and bench-test the authorization drawer in the custom Android QGC build with an
  explicit operator identity, evidence review, approve/deny confirmation and link-loss behavior. The
  ARM64 APK now contains the drawer and passes host tests/lint; real G20 interaction remains pending.
- [ ] Validate actual bidirectional routing over GR01 direct IP or, if required, the V6X fallback path.
- [ ] Install the built Android ARM64 QGC custom Fly View overlay on G20 and validate the 1920x1200
  coordinate transforms, H.265/RTSP path and touch behavior. Package/ABI/signature/static validation
  is complete; no ADB device was present for the real-device gate.
- [x] Validate targeted operator `TUNNEL` isolation in PX4 fixed-wing SITL: QGC `255/190` metadata for
  Jetson `1/191` remained local to the operator loop and was never forwarded into PX4.
- [ ] Repeat the targeted routing check on an actuator-disconnected real V6X only if GR01 direct IP is
  unavailable; keep all control and payload writes disabled.

The detailed topology, UI proposal and acceptance sequence are in
[`g20-gr01-integration.md`](g20-gr01-integration.md).

The deployment planner may recommend an approved fire-area deployment region and a validated release window. It must not target people or vehicles, and human authorization remains mandatory. The current planner is explicitly HIL-only: it omits wind, terrain, calibrated payload aerodynamics and aircraft response, so it cannot establish a physical release solution.
