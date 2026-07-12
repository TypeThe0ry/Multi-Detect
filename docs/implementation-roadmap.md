# Implementation roadmap

目标平台是一架通用折叠串联固定翼无人机。固定摄像头、Jetson、Pixhawk 和数传始终安装；灭火载荷是可选任务模块。软件从配置中的载荷清单自动推导能力：空清单只执行巡检告警，有载荷时才允许进入安全检查和人工授权流程。

## Phase 1: software-only mission loop

### 1. Unified aircraft configuration

- [x] One mission configuration supports zero or multiple payload slots.
- [x] Zero payload means patrol-only; it is not treated as exhausted inventory.
- [x] RGB-only patrol may issue a clearly bounded fire alert after multi-frame confirmation; payload-capable deployment keeps independent corroboration and safety gates.
- [x] Disposable missions still require exactly one approved non-hazardous payload.
- [x] Deployment remains simulation-only and human authorization cannot be disabled.
- [x] Live payload mode can complete an explicitly enabled operator-triggered `FakePayloadPort` HIL cycle.

Acceptance evidence:

```powershell
python -m multidetect validate-config configs/missions/fire_patrol.demo.json
python -m multidetect replay configs/missions/fire_patrol.demo.json examples/fire_mission_replay.jsonl
```

The replay must emit one deduplicated `fire_alert`, remain in `searching`, create no authorization challenge and submit no fake release request.

### 2. Live patrol console

- [x] Show frame-by-frame detections, confirmed tracks, dwell time and mission capability.
- [x] Show an immediate fire-confirmed banner.
- [x] Add a persistent event list, target queue, telemetry strip and map-ready position panel.
- [x] Add alert acknowledgement and visible delivery failure state.

### 3. Data-link software boundary

- [x] Produce a versionable JSON fire-alert envelope in real time.
- [x] Provide recording and JSON Lines publishers for tests and local integration.
- [x] Audit successful and failed live alert delivery attempts.
- [x] Add an optional SQLite outbox that persists before send and retries pending alerts after restart.
- [x] Stream live audit events to disk with fsync while bounding the in-memory event window.
- [x] Add correlated receiver ACK validation and bounded exponential retry/backoff with a loopback HIL transport.
- [x] Add an HMAC-SHA256 authenticated UDP sender/receiver with signed correlation, timestamp checks and persistent receiver deduplication.
- [x] Run the camera/ONNX/tracker task and ground receiver as separate local processes: 105 predictions, one authenticated alert, one correlated ACK, delivered aircraft outbox row and matching ground dedup row.
- [x] Prove restart recovery with the real UDP adapter: ground offline leaves the original alert pending; the next run retransmits the same alert ID before new frames and clears it only after authenticated ACK.
- [ ] Implement and validate the authenticated ACK transport on the selected real data link.

### 4. Model and replay validation

- [x] Add manifest verification for artifact SHA-256, model version, class order, strict Nx6 fields, prohibited uses and production approval.
- [x] Add a `model-check` gate for SHA-256, active provider, strict `N x 6` output and synthetic latency.
- [x] Bind runtime box-coordinate interpretation to the model manifest contract.
- [x] Bind fire and safety-object artifacts to distinct manifest roles; runtime rejects a fire-candidate manifest supplied as person-safety evidence.
- [x] Stream normalized predictions and inference latency for every processed frame.
- [x] Add a frame-aligned IoU evaluator for per-class/overall precision, recall, false alarms, misses and P50/P95 latency.
- [x] Run the full local-camera path through a real constant-output ONNX HIL artifact: 120 frames, one multi-frame-confirmed patrol alert, zero authorization/payload events and complete prediction/audit logs.
- [ ] Add a reviewed post-NMS `N x 6` fire/smoke ONNX artifact and manifest.
- [ ] Build day, night, haze, reflection, smoke-only and no-fire replay sets.
- [ ] Measure deployment-domain per-class precision, recall, false alarms, missed detections and latency with the completed evaluator.
- [ ] Calibrate confidence and multi-frame confirmation thresholds from deployment-domain data.

Phase 1 exit criteria:

- Patrol-only and payload-capable configurations both pass automated tests.
- Local camera and recorded video can run continuously without unbounded frame backlog.
- Confirmed fires are tracked and emitted once per configured cooldown window.
- Every alert and authorization change is auditable.
- No module can send flight-control or physical release commands.

## Phase 2: Jetson and RTSP integration

- [x] Add a hardened unprivileged systemd deployment template for patrol-only Jetson operation.
- [ ] Install and validate the package on the actual Orin Nano/JetPack image.
- Select TensorRT, CUDA or CPU provider by validated fallback order.
- [x] Add bounded capture/inference P50/P95 metrics, average FPS and reconnect counters.
- [x] Add configurable RTSP/local-camera reconnect attempts without a stale-frame queue.
- [x] Keep credential-bearing RTSP URIs out of service process arguments and redact them from application camera errors.
- [x] Add multi-frame `camera-check` with FPS, capture P50/P95 and reconnect reporting; local 640×480/120-frame smoke test passed with zero reconnects.
- Validate RTSP reconnect, end-to-end latency, dropped frames, thermal load and power budget on the target Jetson.
- [x] Show camera/model health and reconnect count; audit and stop on acquisition or inference failure instead of treating it as an empty scene.
- Synchronize RGB and thermal evidence if a thermal camera is added.

Exit criteria: representative RTSP video runs for the required mission duration with measured latency, temperature, power and reconnect behavior.

## Phase 3: Pixhawk V6X integration

- [x] Begin with a read-only MAVLink provider that never transmits heartbeat, commands, parameters, missions or stream requests.
- [x] Decode geographic position, relative altitude, attitude, speed, heading, battery, satellite count, armed state, flight mode and mission sequence.
- [x] Display link/position freshness, aircraft coordinates and a relative aircraft trail in the live console.
- [x] Add a standalone `pixhawk-check` sampler that proves the diagnostic path transmits zero messages.
- [x] Add a read-only mission-lifecycle gate: wait for fresh link/position, armed state, approved auto mode and configured mission sequence before entering search.
- [ ] Compare every displayed field and freshness transition with QGroundControl on the real V6X link.
- [ ] Add independently validated geofence, allowed-area and deployment-zone evidence; generic telemetry must not imply permission.
- [x] Keep route execution, stabilization, failsafe and return-to-launch inside Pixhawk/PX4; the Jetson lifecycle observer sends no commands.
- Validate the patrol application with software-in-the-loop before considering any command path.

Exit criteria: loss or staleness of required telemetry is visible and prevents deployment readiness without affecting Pixhawk failsafes.

## Phase 4: payload hardware-in-the-loop

- [x] Define and validate a versioned read-only inventory evidence contract with HMAC, key ID, timestamp, monotonic sequence, rollback rejection and same-sequence content consistency for the file-based HIL bridge; device key provisioning remains pending.
- [x] Verify module identity, exact slot IDs/types, lock state, controller/interlock health and independent presence sensors.
- [x] Fail closed in live mode when the payload controller or inventory evidence is unavailable.
- [x] Revoke pending or approved authorization when inventory becomes unknown or mismatched.
- Test only with inert, non-hazardous loads in HIL and a restrained ground rig.
- [x] Inject software/HIL disconnects, duplicate/mismatched inventory, stale authorization, failed slot/interlock, wrong feedback, timeout and uncertain release; physical jam testing remains pending.
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
- [ ] Validate actual bidirectional routing over GR01 direct IP or, if required, the V6X fallback path.
- [ ] Build the Android QGC custom Fly View overlay and validate coordinate transforms.
- [ ] If direct IP is unavailable, validate targeted `TUNNEL` routing in ArduPlane SITL and V6X.

The detailed topology, UI proposal and acceptance sequence are in
[`g20-gr01-integration.md`](g20-gr01-integration.md).

The deployment planner may recommend an approved fire-area deployment region and a validated release window. It must not target people or vehicles, and human authorization remains mandatory.
