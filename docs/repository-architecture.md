# 统一仓库架构

## v127 source-bound remote installation receipt（2026-09-06，`v0.3.0-pre.8`）

`src/multidetect/jetson_evidence_install.py` 是远端安装后的独立消费边界；installed CLI 为
`multidetect-jetson-evidence-install-verify`，wrapper 为 `scripts/verify_jetson_evidence_installation.py`，closed
contract 位于 `configs/schemas/jetson-evidence-tools-installation.schema.json`。PowerShell producer 先保存 SSH
输出，再以退出码和唯一完成 marker 为条件生成回执；verifier 重新打开精确 package receipt/tar 与 remote output，
交叉检查 source/deployment/archive/target/runtime 身份。package-only 和失败 SSH 不存在“安装成功”路径；该层也不
提升 hardware、HIL/REAL、flight 或 release。

## v126 independent package verification（2026-09-06，`v0.3.0-pre.8`）

`src/multidetect/jetson_evidence_package.py` 是与 PowerShell producer 独立的消费边界；installed CLI 为
`multidetect-jetson-evidence-package-verify`，wrapper 为 `scripts/verify_jetson_evidence_capture_package.py`，
receipt contract 位于 `configs/schemas/jetson-evidence-tools-package.schema.json`。它用 `tarfile` 只读流式验证，
不把 archive 解压到目标目录；通过结果仍区分 package integrity 与尚未发生的 remote/hardware acceptance。

## v125 isolated Jetson evidence deployment（2026-09-06，`v0.3.0-pre.8`）

`scripts/deploy_jetson_evidence_capture.ps1` 把完整 Python package、`configs/product-version.json` 和 recorder
打成可自复核 tar；package receipt 绑定 source Git/dirty、archive 和 manifest。`-PackageOnly` 不访问远端；联网
模式经 BatchMode SSH 将通过本地复核的包放入独立 tools root，远端二次校验后 staging→target 原子移动。
该部署器与生产巡检 service 目录和 lifecycle 分离，不调用 sudo/systemctl，也不产生硬件或 release claim。

## v124 one-command Jetson evidence session（2026-09-06，`v0.3.0-pre.8`）

`scripts/record_jetson_rtsp_evidence.sh` 是 Jetson 现场编排入口：在新的 session root 内调用既有
`record-rtsp-evidence`、`multidetect.rtsp_evidence_replay` 与 `multidetect.evidence_bundle_transfer snapshot`。
它只组合三个已经独立验证的证据边界，不另建 frame/manifest 合同，也不加载 detector、QGC、Pixhawk 或载荷路径。
失败目录保留供检查，成功目录可直接交给 v123 Windows transfer verifier。

## v123 byte-exact evidence transfer（2026-09-06，`v0.3.0-pre.8`）

`src/multidetect/evidence_bundle_transfer.py` 提供 source inventory、destination verification receipt 及各自的
strict verifier；installed CLI 为 `multidetect-evidence-transfer`，源码树入口为
`scripts/evidence_bundle_transfer.py`，两个 closed schema 位于 `configs/schemas/evidence-bundle-transfer-*.schema.json`。
inventory 可随证据目录复制但不把自身计入哈希；verification receipt 必须写在目的目录外，避免改变待验证集合。
该层只证明复制前后字节与相对路径一致，不证明采集设备、现场人员、GNSS、HIL/REAL 或 release。

## v122 portable RTSP evidence chain（2026-09-06，`v0.3.0-pre.8`）

v3 采集清单优先以 manifest 目录为基准记录容器相对路径；raw replay 的 source binding 再明确记录
`path_base=recording_root|absolute`。因此 Jetson 采集目录与派生 raw replay 处于同一可搬迁父目录时，可以原样
复制到 Windows：verifier 从新 raw root 重建引用，重新验证未改变的 manifest canonical hash、容器 bytes/SHA、
full probe 和 raw recording。summary 里的创建机绝对显示路径不作为定位权威；跨盘无法建立相对关系时仍明确
绑定绝对路径，不伪装成 portable。迁移不改变 camera-only、未知导航、人工 provenance 待确认或 release 关闭状态。

## v121 RTSP evidence → raw replay（2026-09-06，`v0.3.0-pre.8`）

```text
H.265 RTSP --stream-copy--> Matroska + v3 manifest
  manifest = UTC/monotonic window + video bytes/SHA + full probe + canonical hash
  -> strict source reload (credentials never serialized)
  -> multidetect-rtsp-to-replay
  -> camera-only raw manifest/events/PNG + canonical source binding
  -> full source + output reload
  -> operator attestation / independent GNSS join remain separate
```

采集/复核位于 `src/multidetect/rtsp_evidence_recording.py`，转换/全链复核位于
`src/multidetect/rtsp_evidence_replay.py`，clean-checkout wrapper 为
`scripts/create_rtsp_evidence_replay_recording.py`，closed schema 为
`configs/schemas/rtsp-evidence-recording.schema.json`。新转换复用 `video_replay_recording.py` 与
`RawSensorReplayRecording`，不创建第二套 frame/telemetry contract；`tracking_review.py` 的 v2 仅作历史兼容，
v3 必须走严格 source verifier。该链证明容器身份和记录的系统 UTC，不证明现场人员/设备/地点、可信时间戳、
GNSS、目标定位、HIL/REAL 或最终 release。

## v120 video→attestation 时间交接（2026-09-06，`v0.3.0-pre.8`）

`video_replay_recording.py` 在写出 raw manifest 后重新加载它，并将 start、最后事件相对时间和 minimum end 写进
`recording-summary.json.source_attestation_handoff`；状态固定为 `operator_confirmation_required`，不创建证明。
attestation CLI 的 `--derive-times-from-recording` 消费同一权威 manifest，但仍要求人工 provenance 五元组。
camera-only 默认的 1970 时间锚会标记为 ineligible，writer/verifier 也独立拒绝，防止确定性测试时间被包装成
真实采集时间。该交接不改变 raw replay、GNSS join、感知、QGC 或 PX4 Mission 数据路径。

## v119 deployment recording 来源证明边界（2026-09-06，`v0.3.0-pre.8`）

```text
physical local/RTSP capture
  -> recording.manifest.json + events/images
  -> deployment-recording-source.json
       stratum + operator/device/site/source + UTC window
       manifest bytes/SHA-256 + recording_hash + canonical_hash
  -> replay_set_intake: copy and receipt-bind exact attestation bytes
  -> field_replay_campaign: require all six verified attestations
  -> annotation/prediction copies preserve attestation
  -> ReplaySetManifest.source_attestation artifact reference
  -> loader reopens artifact, sibling location, stratum, time and recording binding
```

核心实现位于 `src/multidetect/deployment_recording_attestation.py`，CLI 位于
`src/multidetect/deployment_recording_attestation_cli.py`，clean-checkout wrapper 为
`scripts/attest_deployment_recording.py`，closed contract 为
`configs/schemas/deployment-recording-source-attestation.schema.json`。intake receipt schema 和 replay-set manifest
schema 同步携带来源证明绑定。这个边界消除“只把字符串写成 deployment 就升级来源”的旁路；它保留人员声明
的完整性，但不把声明提升为可信硬件签名、独立审核、HIL/REAL 或最终发布证据。

## v94 receipt-bound field session（2026-09-06，`v0.3.0-pre.8`）

```text
external v93 receipt + received workspace
  -> verify receipt / current parity / static sources / existing declared reports
  -> select exact campaign next_leaf
  -> live Pixhawk identity probe
  -> write-new online-preflight.json
  -> reload before capture (fresh + still-next + full source/mode/query verification)
  -> one covariance capture subprocess
  -> authoritative report verifier
  -> write-new session.json outside workspace
       receipt file/digest/workspace identity
       campaign hash + exact ordinal/mode/airframe
       archived probe file/evidence identity
       report file/evidence/firmware identity
       exact post-capture progression
       acceptance claims false
  -> verify-record remains valid as later leaves advance
```

`field_covariance_session.py` 是现场单叶操作编排层，不实现第二套 Pixhawk 或 covariance 判定；它组合已有
workspace receipt、campaign runner、online verifier 和 report verifier。`online_covariance_preflight.py`
的 archived 模式只用于事后审计已经完成的叶，默认模式仍强制 observation 对应当前唯一 next leaf。
`pixhawk-covariance-field-session-record.schema.json` 和编排器都绑定到 `px4_mission_path_hash`。

本机 workspace 的静态身份仍由 manifest 的 20 文件计算；生成报告不进入 workspace digest。回执复验只
允许 manifest/campaign 已声明的 report 路径，并逐份正式验证，避免“严格 20 文件”在第一次成功采集后
错误阻断，同时仍拒绝任何未声明附件。

## v93 transactional workspace receive（2026-09-06，`v0.3.0-pre.8`）

```text
removable-media bundle
  -> verify closed 20-file source + current parity + six-leaf preflight
  -> copy exact manifest file set to destination sibling temp directory
  -> verify copied workspace again
  -> atomic publish local destination
  -> write-new external receipt
       source/destination absolute paths
       workspace + receipt canonical digests
       current parity + 6 leaves + 18 sources + 20 JSON files
       all acceptance claims false
  -> source media may be removed
  -> verify-receipt reopens the local destination
  -> local preflight/probe/capture remains mandatory
```

接收回执故意位于 workspace 外，避免破坏 v92 的精确闭合文件集。回执中的源路径只是事务 provenance；
独立复验依赖本机 destination、当前 parity 与内容 hash，不声称源介质仍然存在。复制或回执任一步失败时，
只清理本次临时目录/新目标，既有文件绝不覆盖。receipt schema 同样纳入 `px4_mission_path_hash`。

## v92 portable field covariance workspace（2026-09-06，`v0.3.0-pre.8`）

```text
prepared six-leaf campaign on machine A
  -> authoritative source preflight READY × 6
  -> export exact source bytes
       workspace.manifest.json
       campaign.json (relative paths only)
       sources/{hil,real}/{airframe}/{mission,feedback,profile}.json × 18
  -> closed file set + per-file size/SHA + canonical workspace digest
  -> bind current release-parity file + PX4 Mission path hash
  -> copy complete directory to machine B
  -> verify bytes/path boundary/parity + rerun source preflight
  -> machine B runs a new online probe and capture
```

`field_covariance_workspace.py` 是静态证据的可搬迁边界。它不修改签名文档、不重新签发 profile，也不把
第一台机器的 monotonic/UTC/link 状态转换成第二台的状态。portable campaign 的 endpoint/baud 保持原值；
若第二机 COM 或设备身份不同，应重新 provision 对应 feedback/profile，而不是改 manifest 绕过绑定。
`pixhawk-covariance-field-workspace.schema.json`、实现与 verifier 均属于 `px4_mission_path_hash`。

## v91 sealed online preflight contract（2026-09-06，`v0.3.0-pre.8`）

```text
live next-leaf probe
  -> closed online-preflight schema v2 + UTC + canonical digest
  -> source binding: campaign + Mission + feedback + profile
                     + schema + runner + mode-binding authority + parity
  -> verify-probe (default max age 300 s)
       reopen current six-leaf campaign and require same next_leaf
       rerun all-six source preflight
       rebuild authoritative mode binding and query/TX budget
       reject time/content/source/parity/leaf drift
  -> operational authorization for the immediate next capture only
  -> never E7/HIL/REAL/hardware/flight acceptance
```

`src/multidetect/online_covariance_preflight.py` owns the immutable observation contract and independent verifier；
`configs/schemas/pixhawk-covariance-online-preflight.schema.json` closes every nested object and reuses the existing
heartbeat、firmware、mode-binding、qualification 与 parity definitions。记录绑定原 campaign workspace 的
绝对来源路径；它仍只声称同一机器、本次连接内的独立复验。跨机文件路径由 v92 静态 workspace bundle
解决，在线 observation 本身按设计不搬迁。

## v90 current Pixhawk online identity probe（2026-09-06，`v0.3.0-pre.8`）

```text
campaign status -> exact next pending leaf
  -> v89 static source preflight READY × 6
  -> PixhawkReadOnlyTelemetryProvider(next endpoint)
       request_ranging_streams = false
       request_autopilot_version = true, attempts <= 3
  -> capture verifier build_mode_binding(current diagnostics, current sources)
  -> endpoint + heartbeat + firmware + link + qualification exact
  -> pass: capture-next may spawn one capture
  -> fail: capture_started=false, no subprocess
```

公共 `build_pixhawk_covariance_mode_binding()` 只是将现有 capture script 的 authoritative builder 暴露给
runner，因此 online preflight 与正式 evidence 不会出现两套身份规则。probe report 不进入 E7；它保留
查询预算、当前诊断和全部 false acceptance claims，供操作员在正式采集前排错。

## v89 field covariance source preflight（2026-09-06，`v0.3.0-pre.8`）

```text
six-leaf campaign config
  -> reopen executable Mission -> exact vehicle adapter ID/hash
  -> reopen feedback v2 + signed profile -> exact mode and source binding
  -> compare configured endpoint + baud
  -> require feedback/runtime gates + receive-only + zero TX + output locked
  -> READY × 6
  -> capture-next may start exactly one telemetry-only subprocess
```

`campaign_preflight()` 是 transport 之前的静态信任边界。它每次从磁盘重开源文件，不缓存上一次 READY；
因此 profile、feedback 或 Mission 漂移会在下一次 capture 前暴露。在线 HEARTBEAT、firmware identity、
resolved endpoint 与 current qualified link 不在这层猜测，继续由 covariance capture 的真实 provider
快照验证。preflight status 可以覆盖更新，但不进入 E7 release evidence。

## v88 field covariance campaign operator loop（2026-09-06，`v0.3.0-pre.8`）

```text
strict six-leaf campaign config
  -> status: reopen all existing reports -> failed | pending | passed
  -> exactly one next HIL/REAL airframe
  -> capture-next: isolated schema-v3 capture process
  -> verify persisted report regardless of process exit
  -> repeat six times
  -> verify: build E7 covariance matrix + emit six CLI argument groups
```

`src/multidetect/field_covariance_campaign.py` 是现场 operator loop；它不控制模式、Mission 或执行器，
只启动已有 telemetry-only capture。status 文件允许原子覆盖以反映进度，六份 observation report 仍使用
write-new 语义且不可静默覆盖。配置模板与 Schema 分别位于 `configs/` 与 `configs/schemas/`。

## v87 E7 field covariance matrix（2026-09-06，`v0.3.0-pre.8`）

```text
HIL/REAL × fixed-wing/multirotor/VTOL covariance report
  -> schema-v3 report verifier + exact derived rebuild
  -> reopen Mission / feedback v2 / signed runtime profile
  -> bind endpoint + baud + heartbeat + firmware + current link qualification
  -> six distinct leaves + current px4_mission_path_hash
  -> E7 manifest schema/writer/evaluator/final-release blocker
```

`src/multidetect/pixhawk_covariance_binding.py` 只建立可重放索引，不复制一套弱化的报告判定。
它动态加载仓库内 capture verifier，逐叶验证后才抽取 compact record；E7 evaluator 再次执行相同重放并
与 source exact compare。当前仓库保留 SIM 正控，真实 HIL/REAL 六叶必须由现场独立采集。

## v86 strict covariance evidence schema（2026-09-06，`v0.3.0-pre.8`）

```text
provider full diagnostics
  -> _evidence_diagnostics(): 16-field closed projection
  -> build derived checks/claims + source/schema/parity hashes
  -> Draft 2020-12 validation
  -> atomic write-new
  -> verifier: schema -> digest -> current source reopen -> exact rebuild
```

`configs/schemas/pixhawk-odometry-covariance-observation.schema.json` 属于 PX4 Mission contract；
schema 与 capture script 任一变化都会刷新同一个 `px4_mission_path_hash`。失败报告允许必要的 null/空
集合以保留诊断，但 passing record、请求集合、hash 和 mode binding 仍有严格结构与数值约束。

## v85 current qualified covariance evidence（2026-09-06，`v0.3.0-pre.8`）

`capture_pixhawk_odometry_covariance.py` 在报告层和 HIL/REAL identity binding 层都消费同一个
provider 结束快照：transport、qualified link、operational qualification 任一不是当前通过即
fail-closed；firmware age 还必须不大于采集 duration 加 1 秒边界。该约束防止断链后缓存的姿态
covariance/firmware 身份与旧 Mission 材料拼成伪现场证据，不改变 provider 的遥测解析或控制边界。

## v84 bounded firmware identity acquisition（2026-09-06，`v0.3.0-pre.8`）

`PixhawkReadOnlyTelemetryProvider.snapshot()` 在 receive drain 后依次执行可选 firmware identity request 与
ranging stream request。前者有独立的 per-heartbeat budget/clock/diagnostics，后者保留既有 cadence；
两类 `COMMAND_LONG` 都进入统一 `messages_transmitted`，covariance verifier 从两个 attempts 重新计算
精确发送总数。默认配置仍不发任何消息；只有 covariance field-capture 显式打开两种 telemetry 请求。

## v83 field covariance evidence binding（2026-09-06，`v0.3.0-pre.8`）

`scripts/capture_pixhawk_odometry_covariance.py` 现在把 HIL/REAL observation 接到既有 Mission/profile/
feedback 信任链。capture 端读取当前 Pixhawk diagnostics；verifier 端重新打开 Mission、feedback 与
profile，再重建 endpoint、baud、heartbeat、firmware 和输出锁 binding，最后与保存报告 exact compare。
因此单独修改 JSON claim、替换 profile/feedback 文件、换飞控 firmware 或跨链路重放都不能通过。
SIM 分支显式拒绝这些现场材料，保持仿真与现场 provenance 分离。

## v82 PX4 estimator covariance ingress（2026-09-06，`v0.3.0-pre.8`）

```text
PX4 estimator -> MAVLink ODOMETRY(331)
  -> heartbeat system/component identity binding
  -> LOCAL_NED parent + LOCAL_NED/BODY_FRD child validation
  -> q attitude + 21-value upper-triangle pose covariance
  -> angular 3x3 finite / positive diagonal / PSD validation
  -> sqrt(diagonal) rad->deg + explicit R/P/H floor
  -> <=250 ms attitude association + freshness
  -> VehicleTelemetry uncertainty tuple + shared ranging/evidence
  -> telemetry-only capture -> source/parity binding -> reconstruction verifier
```

`src/multidetect/pixhawk.py` 和 `scripts/capture_pixhawk_odometry_covariance.py` 属于
`px4_mission_path_hash`。parent frame 决定 position；child frame 决定 twist，因此 `BODY_FRD` twist 不会
被冒充为 N/E velocity。SIM/HIL/REAL 使用同一 parser/report contract，runtime mode 只改变可声明的门。

## v81 三机型外部 covariance evidence（2026-09-06，`v0.3.0-pre.8`）

```text
pinned Docker/PX4/Gazebo: fixed-wing | multirotor | VTOL
  -> MDGZSIM3 × 3 frames per airframe
  -> raw BGR24 + wire header + record SHA-256
  -> frame provenance: raw 3x3 covariance + derived R/P/H sigma + source
  -> frame-set manifest: convention/count/nonzero-count/floor/source
  -> independent persisted-frame reconstruction and acceptance
```

三份实采均发现默认 publisher 的姿态 covariance 为零，系统保留原始事实并显式应用
`0.3°/0.3°/1.0°` floor。该结果证明 transport、backend 与证据链一致，不将零矩阵解释为零误差，
也不将配置 floor 解释为 estimator 输出。

## v80 Gazebo covariance wire 与 parity（2026-09-06，`v0.3.0-pre.8`）

```text
Gazebo OdometryWithCovariance (row-major 6x6)
  -> extract angular rows/columns 3..5
  -> validate finite + symmetric + positive-semidefinite
  -> MDGZSIM3 (276-byte header + BGR24 + whole-record SHA-256)
  -> reconstruct/hash-bind raw 3x3 covariance in frame manifest
  -> numerical Jacobian: ENU/FLU fixed-axis XYZ -> NED/FRD Euler RPH
  -> transformed 1σ + configured floor + provenance
  -> VehicleTelemetry -> shared ranging -> RangeSolution/operator evidence
```

`scripts/gazebo_sensor_stream.py` 和 `src/multidetect/backends/gazebo_sim.py` 现在都属于
`core_algorithm_hash`；wire 或坐标变换任一字节变化都会使四模式共享身份失效。v81 已采集三机型
外部 v3 source，并将最终 sigma/source 写入逐帧证据；现场与 HIL/REAL 门仍独立阻断。

## v79 姿态 uncertainty provenance（2026-09-06，`v0.3.0-pre.8`）

```text
telemetry roll/pitch/heading + observed_at
  + optional complete roll/pitch/heading 1σ + source
  -> shared aircraft_pose_from_telemetry
       |- complete attributed telemetry tuple: use it
       |- entirely absent tuple: explicit mode/config fallback
       `- partial/malformed/unattributed tuple: fail closed
  -> AircraftPose -> shared ranging geometry
  -> RangeSolution pose sigma/source -> operator evidence
```

Replay/raw replay 保留该 tuple，整组缺失时仍兼容历史 schema-v1 identity。`domain.py`/replay 属于
`task_contract_hash`，ranging resolver/solution 属于 `core_algorithm_hash`。v79 时 Gazebo 的
MDGZSIM2/wire-v2 只传 pose/twist；v80 已由 MDGZSIM3 完成 covariance、坐标变换和 floor 策略升级。

## v78 共享几何不确定性端点（2026-09-06，`v0.3.0-pre.8`）

```text
nominal camera ray + GNSS→camera body-FRD lever arm
  -> aircraft attitude rotation
  -> NED ray + NED camera offset
  -> one-at-a-time ±1σ geometry endpoints
       |- camera-ground slant
       |- DEM intersection
       |- GNSS-datum ground range
       `- GNSS-datum absolute bearing
  -> WGS-84 projection + horizontal uncertainty
```

`_GeometryPerturbation` 把射线和杆臂作为同一物理端点传播；均值仍由一个总 N/E offset 投影，避免杆臂
重复应用。该实现属于 shared `core_algorithm_hash`，REPLAY/SIM/HIL/REAL 不能分叉使用旧误差模型。

## v77 field-evidence binding schema（2026-09-06，`v0.3.0-pre.8`）

```text
8 input descriptors -> input_sha256/input_bytes (fixed keys)
target-event + recording + mount survey -> typed optional bindings
policy -> bounded telemetry age + REAL-domain requirement
  -> strict JSON Schema (no unknown bindings)
  -> independent source re-evaluation and deterministic section comparison
```

schema 负责结构边界，`verify_field_evidence_bundle_report` 负责重新读取来源和语义重算；两者不能相互替代。

## v76 target-event 标定跨证据绑定（2026-09-06，`v0.3.0-pre.8`）

```text
strict camera calibration JSON
  -> create_target_event_replay.py
  -> bundle/camera-calibration.json + manifest ID/bytes/SHA-256
  -> load_target_event_replay_manifest（rehash + full semantic validation）
  -> field_evidence_bundle.target_event
  -> compare with camera_mount_survey calibration ID/SHA-256
```

`target_event_replay.py` 与 `target-event-replay-manifest.schema.json` 同属 shared core algorithm identity；
`field_evidence_bundle.py` 属于 shared task contract。证据可携带完整标定 provenance，但仍是只读记录路径，
不会创建 Mission、MAVLink 控制或载荷输出。

## v75 设备校准嵌套证据（2026-09-06，`v0.3.0-pre.8`）

```text
issued certificate file
  -> equipment-record（path + bytes + SHA-256 + issuer + validity + accuracy）
  -> equipment calibration JSON（strict schema）
  -> mount survey reopens record + certificate
  -> identity/accuracy/time-window checks
```

设备记录 schema 和 survey 执行代码均属于 shared task contract；证书内容仍需现场人员确认来自实际签发方。

## v74 现场工作表准备层（2026-09-06，`v0.3.0-pre.8`）

```text
camera_mount_survey.py template -> blank canonical CSV（no overwrite）
operator fills physical measurements
camera_mount_survey.py validate-raw -> read-only preflight JSON
camera_mount_survey.py create -> reviewed survey evidence
camera_mount_survey.py verify -> independent re-hash/recompute
```

预检交换结构由 `camera-mount-measurement-preflight.schema.json` 固定，并随 task parity 变化。

## v73 mount-survey CSV 单一数据源（2026-09-06，`v0.3.0-pre.8`）

```text
raw-measurements.csv（固定 v1 header，hash-bound）
  -> strict CSV parser -> canonical samples -> statistics/checks
  -> serialized report
  -> independent verifier reopens same CSV -> exact sample comparison + recompute
```

CLI 不再接受第二套 `--sample` 数字；Python API 兼容显式 samples 仅用于验证一致性，任何差异都会关闭门。

## v72 物理安装测量合同（2026-09-06，`v0.3.0-pre.8`）

```text
设备校准证书 + 原始重复测量 + camera calibration
  -> camera_mount_survey.py（FRD boresight + GNSS→camera lever arm）
  -> camera-mount-survey.schema.json
  -> independent re-hash/recompute verifier
  -> field_evidence_bundle.camera_mount（第七层）
```

`src/multidetect/camera_mount_survey.py` 是测量生成/复核核心，`scripts/camera_mount_survey.py` 是源码树
入口，`configs/schemas/camera-mount-survey.schema.json` 是严格交换合同，`tests/test_camera_mount_survey.py`
覆盖合格、篡改与 fail-closed 情况。它们和 field bundle 已纳入 task parity；测试数据不构成现场验收。

## v69 combined evidence 双层传感器门（2026-09-05，`v0.3.0-pre.8`）

```text
combined runner
  -> assert CameraInfo semantic status before source gate
  -> combined-evidence.json
  -> independent verifier reopens source
       |- exact CameraInfo semantic status
       |- sensor expected == received and complete
       `- odometry timestamps == expected + full pose/velocity claim
  -> E7 combined binding only after every check passes
```

source 生成器和独立 verifier 不再共享一个宽松的“sensor ready”结论。即使根级 gate 被改成 true，
CameraInfo 改回 pending、少一帧 image 或少一帧 odometry 都会产生独立 failure reason。当前
`combined-sim-qgc-20260905T152520Z-75420` 在该架构下为 44/44 checks，仍不提升人工或硬件声明。

## v68 CameraInfo 标定语义绑定（2026-09-05，`v0.3.0-pre.8`）

```text
simulation/gazebo/calibration/gazebo-front-camera-v1.json
  -> CameraCalibration canonical fields + semantic SHA-256
  -> GazeboSimBackend sensor binding identity
  -> gazebo_sensor_stream --expected-camera-calibration-json
  -> actual CameraInfo K/P/R + PLUMB_BOB/D validation
  -> MDGZSIM2 frames
  -> runtime result + persisted frame-set source attribution
```

`scripts/gazebo_sensor_stream.py` 是实际 Gazebo message 的 fail-closed 边界：先验证 1280×960 geometry，
再验证 K/P 标准 pinhole 结构、R identity、零 stereo translation、PLUMB_BOB 和五项 D。intrinsic 的
`1e-4 px` 容差只覆盖 Gazebo 32-bit float 序列化（本次最大实测差约 `3.53e-5 px`）；R/D 仍为
`1e-12`。backend 构造、CLI 和 acceptance runner 都必须显式提供标定，不能靠 topic 名或图像尺寸
冒充完整相机模型。

当前 source `artifacts/evaluation/combined-sim-qgc-20260905T150510Z-27780/combined-evidence.json`
在干净 `c0a4c32` 上完成 900/900 帧与 41/41 独立 checks，状态为
`validated_k_p_rectification_plumb_bob_distortion`。该架构只关闭 SIM 相机语义机器门；真实镜头标定、
HIL/REAL 与人工视觉仍必须使用各自来源重新验收。

## v67 联合 SIM 传感器时间策略（2026-09-05，`v0.3.0-pre.8`）

```text
Gazebo transport callbacks
  CameraInfo -> session-static calibration: positive + causal + per-image geometry binding
  NavSat/Odometry -> dynamic state: nearest causal sample, age <= 1.5 s
  Image -> future/missing/stale dynamic binding? drop frame and wait
        -> persistent gap? bounded helper timeout -> fail closed
        -> fresh binding? MDGZSIM2 -> shared production core -> RTSP -> QGC type-24 ACK
```

`scripts/gazebo_sensor_stream.py` 保留每 topic 严格递增历史并选择 image 时刻之前最近样本。
`CameraInfo` 的 Harmonic publisher 会重复发布但保留初始 sensor stamp，因此它的时间语义是会话级标定，
不能冒充动态状态；其 width/height 仍逐帧约束图像。NavSat/Odometry 不降级：超过 1.5 秒的候选不会写入
wire。由于 Gazebo transport worker 可短暂错序，单张 image 被丢弃而不是令整个无限传感器流立即退出；
若新鲜动态样本没有赶上，进程仍在统一 deadline 退出失败。

当前成功 source 为
`artifacts/evaluation/combined-sim-qgc-20260905T142324Z-32372/combined-evidence.json`：900 个有效帧、
4 次 stale NavSat/Odometry 图像丢弃、900 个 type-24 ACK、VTOL attached/Mission/QGC toolbar gate 全通过，
41/41 独立 checks 通过。该层仍把
`camera_info_intrinsics_semantic_validation=pending_k_p_distortion_validation` 写入证据，不能据此声明完整
相机标定、人工视觉、HIL/REAL 或硬件验收。

## v66 四模式 QGC machine-capture adapter（2026-09-05，`v0.3.0-pre.8`）

```text
REPLAY / SIM / HIL / REAL runner
  -> exact MultiDetectGCS PID + explicit runtime_mode + run_id
  -> capture_qgc_native_style.ps1 schema v2
  -> bind EXE + release parity + exact client rect + occlusion state + PNG
  -> qgc_visual_acceptance native-v2 adapter
  -> normalized machine binding + human checklist + E7 four-mode aggregate
```

`configs/schemas/qgc-native-style-capture.schema.json` 定义 schema-v2 capture 四模式统一主契约；它不依赖
HIL 专属的 `jetson_metadata_hil` 或外层 assertions。
适配器仍能读取历史 `px4_sitl_qgc_operator_acceptance`，但 legacy event 只对 `runtime_mode=hil` 有效。
这避免“代码字段声称四模式、源报告实际只能来自 HIL”的假对等。native-v2 必须同时通过 event/version、
mode、PID、client rect、零遮挡、单一 surface、PNG、EXE identity、parity identity、UI contract 和
`operator_visual_acceptance=not_claimed` 检查；人工声明仍只能由后续 review record 给出。

## v65 四模式 QGC 人工视觉 E7 证据（2026-09-05，`v0.3.0-pre.8`）

| 组件 | 职责 | 声明边界 |
|---|---|---|
| `qgc_visual_acceptance.py` / schema / checklist | 为单次人工点验绑定 runtime mode、exact machine report、PNG、QGC EXE、UI contract 和定位负控 | review SHA 仅是内容完整性摘要；不冒充密码学身份或硬件/实飞批准 |
| `release_gate_manifest.py` | 接受多份记录，拒绝重复 mode/evidence/machine report/screenshot，并按 replay/sim/hil/real 生成稳定集合 | 少于 4 种模式时 `complete=false/gate_passed=false` |
| `release_gate.py` | 独立重开每份 record 及全部绑定源，复算文件身份、review digest、运行模式与 source identities | 最终 release 必须 4/4；当前 0/4，集合 absent，门保持关闭 |
| `run_current_release_gate_pre8.ps1` | 通过空默认数组或显式 `QgcOperatorVisualAcceptanceEvidence` 将真实人工记录送入同一 E7 | 默认不寻找“最新”文件，也不自动生成或伪造人工记录 |

release-gate schema 进入 `px4_mission_path` parity 后，hash 更新为
`F0804FEB609A9CBBBCE6E3932ED77BD7852C9AF71BDB612A76BBD56EAD12247A`；v65 matrix/campaign/external
join/revalidation 均按新 parity 从既有不可变源重建。v61 截图只属于 HIL machine capture，不能充当
REPLAY/SIM/REAL 的人工证据。

## v63 PX4 响应 campaign（2026-09-05，`v0.3.0-pre.8`）

| 组件 | 职责 | 声明边界 |
|---|---|---|
| `run_simulator_docker_ai_acceptance.py` | 在正常 reported-position gate 之外提供 target-refresh、heartbeat、mode-loss 三个显式 SITL 故障注入点 | 仅允许 disposable loopback PX4/Gazebo；故障必须在 runtime 成功后被安全门观察并 fail-closed |
| `run_px4_reported_position_response_campaign.py` | 聚合三机型各 2 个 positive 与 3 个 fault，计算 minimum/P50/P95/maximum 并绑定 15 份源证据 | 重复源哈希、缺机型、缺故障、错误状态或硬件声明均关闭 campaign |
| `verify_px4_reported_position_response_campaign.py` | 重开所有源报告、scenario、parity 与工具源码，重算统计和 campaign SHA-256 | 不信任汇总文件中的通过状态；源文件漂移即失败 |
| E7 manifest/evaluator/schema | 绑定 `px4_reported_position_response_campaign` 的文件哈希、摘要、6/9 计数、三机型和 verifier checks | 只关闭 SIM 机器重复性/负向子门，不提升人工、HIL、REAL、硬件或实飞声明 |

## v62 三机型 PX4 reported-position 响应证据（2026-09-05，`v0.3.0-pre.8`）

| 组件 | 职责 | 声明边界 |
|---|---|---|
| `scripts/run_px4_reported_position_response_matrix.py` | 汇总 fixed-wing/multirotor/VTOL 三份 external Docker AI acceptance，绑定 source、scenario、parity 与生成工具哈希，检查同一控制路径和 PX4 reported displacement | 只接受恰好三个 canonical airframe；拒绝 Gazebo truth/pose injection、非 pinned image、残留容器、mode/heartbeat/target refresh/位移失败 |
| `scripts/verify_px4_reported_position_response_matrix.py` | 从矩阵记录的相对路径重新打开全部输入并复算稳定摘要 | 不信任矩阵内的通过布尔值；任一源码或源证据漂移即关闭子门 |
| `release_gate_manifest.py` / `release_gate.py` | 将矩阵以 SHA-256/bytes/matrix digest 绑定为 E7 `px4_reported_position_response_matrix` 并在每次评估时重放 verifier | 仅关闭外部 SIM 动态响应子门；hardware/HIL/REAL/flight/operator visual 固定不提升 |

## v61 同一原生 QGC PID/HWND 的定位机器捕获（2026-09-05，`v0.3.0-pre.8`）

```text
current-parity Gazebo geolocation evidence
  -> strict release-label + five-group parity comparison
  -> signed type 10/type 22 refresh during bounded capture hold
  -> pinned PX4 SIH + read-only router + native MultiDetectGCS
  -> exact existing QGC PID -> owner HWND -> physical client rect
  -> >=1600x900 native resize -> external-occluder isolation/restore
  -> PNG + process observation + HIL acceptance JSON
```

`operator_closed_loop_hil.py`、`run_px4_sitl_qgc_operator_acceptance.ps1` 和
`capture_qgc_native_style.ps1` 形成一条机器证据链；截图脚本不再另开一个脱离状态的空白 QGC。
QML 的 isolated-HIL 自动退出可在 `0..15000 ms` 内延时，截图模式使用 8 秒并最终走同一受控退出。
该路径只允许 `MultiDetectGCS` 精确进程名，附着模式不拥有进程退出，所有被移动/最小化/临时隐藏的
窗口都在 `finally` 恢复。机器截图绝不提升 `operator_visual_acceptance`，也不接触 PX4 写入或物理载荷。

## v60 定位证据到原生 QGC 状态链（2026-09-05，`v0.3.0-pre.8`）

v60 不修改 QML/C++ 或另造 UI，而是在既有 isolated-HIL acceptance 上补齐实际定位消息：

```text
passed schema-v2 Gazebo geolocation evidence
  -> operator_closed_loop_hil.py strict evidence intake
  -> rebase final range/WGS-84/1σ onto one live HIL target + frame
  -> MAVLink-2-signed operator metadata type 10 + type 22
  -> existing native MultiDetectOperatorController
  -> existing MultiDetectState / FlyViewCustomLayer map and evidence bindings
```

PowerShell wrapper 要求 QGC 日志同时出现 type 10/22，记录 source evidence SHA/scenario/release，且继续
约束 pinned PX4 SIH、localhost 独立端口、只读 router、0 写入/0 物理动作。实际 v60 运行认证 40 包，
两类定位状态各 1 包，所有隔离/清理检查通过。由于本轮不改变 UI contract，现有 EXE 和五组 parity
保持不变；该结果是机器 transport/native-adoption 子门，不是地图/卡片人工视觉或 HIL/REAL 硬件门。

## v59 三档坡高/距离定位矩阵闭环（2026-09-05，`v0.3.0-pre.8`）

v59 保持唯一 shared ranging/WGS-84 实现，并以两套新增资产关闭 v58 定义的工况覆盖聚合门：

```text
multiple schema-v2 Gazebo camera geolocation evidence files
  -> verify_gazebo_target_geolocation_evidence
       reopen raw bound inputs + rerun detector/ranging/geolocation
  -> gazebo_geolocation_regime_matrix.py
       same release/evaluator identity
       separated altitude/distance/slope regimes
       positive accuracy cases + fail-closed no-coordinate cases
  -> deterministic matrix report (software/SIM only)
```

`src/multidetect/gazebo_geolocation_regime_matrix.py` 是聚合门，
`scripts/run_gazebo_geolocation_regime_matrix.py` 是未安装环境也能运行的入口，CLI 同时由
`pyproject.toml` 暴露。每份 schema-v2 source 绑定其定位评估器源码；矩阵报告再绑定矩阵评估器自身。
当前正向工况为 `6.20 m AGL / 8 m / 0 deg`、`12.17 m / 15 m / 4.00 deg`、
`20.15 m / 25 m / 7.97 deg`，每档各 5 帧；近地平线负控仍 5/5 拒绝坐标。矩阵完整重放四份 source 后
`gate_passed=true`。这只是 software/SIM acceptance，不进入 QGC、E7、HIL/REAL 或物理输出权限。

## v57 当前事实入口与可复现目标落图（2026-09-05，`v0.3.0-pre.8`）

仓库当前状态不再从超长根 README 或聊天历史推断。稳定入口是根 `README.md`，详细现状与
机器可读镜像分别是 `docs/current-progress.md` 和 `docs/current-progress.json`，旧 v1-v56
逐轮开发记录原样保存在 `docs/history/README-development-log-through-v56.md`。以后每次功能、
证据、版本或门禁变化必须遵循 `docs/development-update-policy.md`，同步更新进度、CHANGELOG、
路线图/专题文档和测试。

v57 没有新增第二套定位算法。实际 Gazebo camera 像素沿唯一共享链路进入结果与独立复核：

```text
Gazebo BGR24 frames + NavSat/Odometry + scenario/DEM
  -> quarantined ONNX candidate detector
  -> target_image_anchor.py semantic ground-contact point
  -> MultiModalRangingEngine (camera ray + DEM/GNSS + uncertainty)
  -> target_geolocation.py WGS-84 projection + 1-sigma uncertainty
  -> gazebo_target_geolocation_acceptance.py evidence
  -> verify_gazebo_target_geolocation_acceptance.py
       reopen scenario/frame-set/model/manifest
       recompute all five release-parity groups
       rerun detector/ranging/geolocation
       compare timestamp-free deterministic result digest
```

`target_image_anchor.py` 已纳入 `core_algorithm` parity，任何绑定核心源、场景、帧集、模型或
manifest 字节变化都会让旧证据复核失败。当前正向 5/5 帧落图、最大水平误差 `0.674040 m`；
近地平线负向 5/5 帧都以 `target_ray_does_not_intersect_ground_safely` 拒绝坐标。两项只关闭
单高度 software/SIM 子门，不外推为原生 QGC 人工视觉、室外 GNSS、HIL、REAL、实飞或生产
模型批准。E7 当前仍有 32 条 blocker，最终 `v0.3.0` 保持阻断。

## v56 camera-frame、候选三控与当前 E7 绑定（2026-09-05，`v0.3.0-pre.8`）

v56 继续使用同一 simulator/backend/perception/QGC 路径，没有增加 simulator-only UI：

```text
simulation/gazebo/models/{x500,standard_vtol,rc_cessna}_mono_cam/model.sdf
  -> mono_cam include + fixed joint relative_to base_link
  -> Gazebo Image top_to_bottom

scripts/gazebo_sensor_stream.py
  -> RGB-to-BGR + stride normalization, no vertical row flip
  -> MDGZSIM2 top_to_bottom wire record

src/multidetect/backends/gazebo_sim.py
  -> preserve row order
  -> existing shared perception/ranging/geolocation/operator path

src/multidetect/candidate_detector_controls.py
  -> strict core source identity + additive provenance
  -> row-order trio validated before ONNX session construction
  -> present / zero-blank / independently annotated occlusion controls

scripts/run_current_release_gate_pre8.ps1
  -> current release parity
  -> v56 fixed-wing / multirotor / VTOL deterministic evidence
  -> six-source external runtime/AI join + independent source recomputation
  -> report-only diagnostic or strict final-release decision
```

实际 headless Gazebo pixel runtime、quarantined candidate 三控、三机型 deterministic binding 的 SHA-256
依次为 `3FBF2BDE...2643D`、`D48865EC...65BC9`、`7DAB627C...96B2`。旧 v52 external join 因记录旧
parity 文件 hash，在当前 source recomputation 中正确失败；当前 join/revalidation 则为
`D5120A0F...4327F` / `AA925E65...6D46` 并通过。E7 对 v56 三机型与 external 子门均通过，但总门仍有
32 条 blocker，strict 退出 `2`；strict report SHA-256 为 `B4BE420E...87DF`。

候选模型仍为 `quarantined`，没有部署域 accuracy 或 model-review approval；它没有进入 E7 model-review。
外部 join 复用的是六份不可变 software-only source，而不是一次新的 Gazebo/PX4 运行。完整回归为 3,199
tests：`3198 passed, 1 skipped`，无失败/错误；JUnit SHA-256 为
`2799FCA4C87F095F0218CC73FB65B03B7E6BD8C63978CE8C470B6ED5911CBBBC`，唯一跳过是 Windows symlink
能力。本层未修改 QGC
C++/QML，未生成新 EXE，也不改变真实 Jetson/相机/Pixhawk、室外 GNSS、人工 Fly View、HIL/REAL 或实飞门。

## v55 持续 PX4 position-response 验收层（2026-09-05，`v0.3.0-pre.8`）

v55 的新增文件职责停留在 acceptance/tooling 层，不另建 UI、planner 或 flight controller：

```text
scripts/run_simulator_docker_ai_acceptance.py
  -> opt-in synthetic perception refresh
  -> disposable SITL preflight + same-socket POSCTL request
  -> bounded frame/poll/time/mode/heartbeat/safety evidence
  -> shared multidetect.geolocation_accuracy.wgs84_distance_m

src/multidetect/simulator/live_ai_bridge.py
src/multidetect/simulator/ai_executor.py
src/multidetect/simulator/control_gateway.py / existing MAVLink adapter
  -> existing production perception, policy, authority, watchdog and MANUAL_CONTROL path

tests/test_simulator_docker_ai_acceptance.py
  -> positive response plus frame-limit, time-freeze, target-lease, mode-loss,
     heartbeat-dropout, baseline, wire/preflight and safety-fault regressions
```

`COM_RC_IN_MODE=1` 只写入本次 hash-pinned disposable container，使 POSCTL 在 SITL acceptance 中消费既有
joystick/`MANUAL_CONTROL` 输入；不会修改 host、Pixhawk 或产品 profile。目标刷新仍通过
`SimulatorTelemetryBridge.ingest_perception`，SET_MODE 与 MANUAL_CONTROL 共用 literal-loopback socket。
脚本不使用 Gazebo truth/pose service/teleport，也没有 `SimController`、`FlyViewSim.qml` 或第二套任务逻辑。

current-code canonical 是
`artifacts/evaluation/terminal-continuation-v55/terrain-ai-flight-response-v55-pre8-v4.json`，661,936 bytes、
SHA-256 `B56F20AB40B3E8AF9DF169FAAED9D4F326FFCDFDD052ED0FC6FF804FD7C44073`。它关闭的是单机型
disposable PX4-reported position-response machine sub-gate；不授权 REAL 输出，不证明相机/测距/落图、
QGC 视觉、actuator/airborne、fixed-wing/VTOL、硬件、室外 GNSS 或 HIL/REAL。QGC C++/QML 未改，
因此 v55 没有新的 portable EXE；E7 输入与 32 个 blocker 也未变化。

v55 current-code 全量回归共 2,907 tests：`2906 passed, 1 skipped, 0 failed, 0 errors`。JUnit 位于
`artifacts/evaluation/terminal-continuation-v55/full-regression-v55-px4-position-response-final-20260905.xml`，
411,702 bytes，SHA-256 `A2C2595C49D0B663468D09274D22976CC1B4D9128425BB23458445B1C5397F51`；唯一跳过为
Windows symlink 权限项。

## v54 外部 Gazebo sensor binding 与 validation scenario（2026-09-05，`v0.3.0-pre.8`）

v54 把外部模拟器输入拆成可审计的三层，同时保留原生 QGC Fly View 和共享任务链：

```text
simulation/gazebo/models/*/model.sdf
  -> official 3-D OdometryPublisher + camera + NavSat
scripts/gazebo_sensor_stream.py
  -> MDGZSIM2 wire v2 (204-byte header + BGR24 + record SHA-256)
src/multidetect/backends/gazebo_sim.py
  -> ENU/FLU to NED/FRD
  -> qualified NavSat MSL - hash-bound LocalDemGrid MSL = AGL
  -> existing integrated SIM / perception / mission / operator / QGC path
```

新增的场景文件位于 `configs/scenarios/validation/terrain_ranging_validation_001.*`，地图位于
`simulation/maps/terrain_ranging_validation_001.geojson`，并复用同名 SDF/DEM。validation 子目录使它能被
显式 CLI/Docker run 选择，同时不被默认顶层 `*.scenario.json` 三机型发布矩阵自动发现。runtime channel
不包含目标真值；truth/expectations 由 evaluator 单独验证 hash 后读取。

Docker planner 将 `scripts/` 只读挂载，DEM 由 host backend 只读加载并把 SHA 写入 plan/sensor identity。
运行证据显式携带 `terrain_agl_reference=GAZEBO_NAVSAT_MSL_MINUS_SCENARIO_DEM_MSL` 和
`odometry_local_up_used_as_agl=false`。外部非平地 x500 证据 SHA-256 为
`8FB0FE4338EEA71021C7CCD8515ACFB2577847A2517297126C921AE4D825C4B0`；v54 该记录证明 transport/world 可运行，
不证明坡地航迹、人工视觉、硬件或室外精度。

三份 model SDF 的 OdometryPublisher 变更使旧 v52 model hash 正确失效。当前三机型 source 重新生成后
绑定到 `terminal-continuation-v54/simulator-3d-control-binding-v54-odometry-pre8.json`，2,359 bytes、
SHA-256 `78066C298A9B9EEDB012B7596D9B7DA66E334DE7449EACDF1CCD59513181B00C`；fixed-wing / multirotor /
VTOL source SHA-256 依次为 `91C8482BE3B828455045023A30B1AADB7B471764F57CCE701F7BAAA73FDEB484`、
`AF03A19145308D217EE48FFFB8D62BD49019CA6CDA321AE821658D9276D868CF`、
`665C9885461518769A5F370237F2B4BFCD5B31397509E4904EE4983CDCBC77A5`。E7 默认 wrapper 仅把 3-D
control 源切换到 v54，v52 parity-valid external 六源继续保留其历史边界。strict 退出 `2`、32 blockers；
完整回归为 `2888 passed, 1 skipped`（失败/错误为 0），JUnit SHA-256
`C3D01A9F3A157A3B57225319A67FA7EE79883AAB70C025DA14E896F0C1EA4532`。

## v53 独立 SDF 地形真值验收层（2026-09-05，`v0.3.0-pre.8`）

v53 增加的是验收层和独立场景资产，不是新的 runtime estimator：

```text
repository-owned non-flat Gazebo SDF
  -> strict independent SDF parser
  -> ENU normal / analytic plane / ray-plane truth
                                              \
shared LocalDemGrid -> bilinear samples / exact ray-cell roots
                                              /
  -> 41 surface comparisons + 7 rays (6 cross-cell + 1 nadir baseline)
  -> schema-bound deterministic asset-truth evidence
```

`terrain_truth.py` 从 SDF 的模型姿态恢复 `Rz * Ry * Rx` 法向，并用平面方程独立计算交点；它不调用
`MultiModalRangingEngine` 的地形算法来制造 expected value。匹配 DEM 使用同一 WGS-84/MSL 原点、
`+0.1 m/m` 北向坡度和 400 m × 400 m 网格。验收覆盖每个网格节点、每个双线性单元中心以及七条
不同杆臂/方向的 NED 射线，其中六条跨越网格边界、一条保留垂直基准；SDF/DEM/parity 均保存文件
字节数和 SHA-256。

这两个文件没有加入三机型生产 scenario matrix，也没有加入五组产品 parity：前者仍固定覆盖 VTOL、
fixed-wing、multirotor 三种正式场景，后者只绑定 REPLAY/SIM/HIL/REAL 共享的部署契约。v53 证据
v2 SHA-256 为 `CCB34D5AE3088DE1A66CCF4E6D521C7E009DEF1CD25051E6DDA61DC4A526FB9B`，当前产品 parity
仍为 `C28AC3C300DDACAD7B97097EC80987AED78E8EB8B19101AD98FD8D66F7985739`。该证据没有启动
Gazebo/PX4，也没有创建 simulator-only UI；外部 runtime、人工 QGC 视觉、硬件和室外定位仍是独立门。
完整回归为 `2865 passed, 1 skipped, 0 failed, 0 errors`（2,866 tests），JUnit SHA-256
`2E54AD65923CDB93D1BD570A0C726FCBFD32316FB3A0EB5A8D3B13A116205092`；跳过项仅来自 Windows
symlink 权限缺失。

## v52 DEM/GNSS 地形测距进入共享核心（2026-09-05，`v0.3.0-pre.8`）

v52 没有增加 simulator-only estimator、第二套 UI 或第二条飞控路径。数据流为：

```text
qualified GPS_RAW_INT (WGS-84 + MSL + hAcc/vAcc + observed_at)
  -> VehicleTelemetry / raw replay / GNSS sidecar
  -> shared LocalDemGrid (bilinear elevation/slope + exact ray intersection)
  -> DemGpsTerrainObservation
  -> shared MultiModalRangingEngine
  -> existing RANGE_STATUS / TARGET_GEOLOCATION
  -> existing native QGC Fly View evidence + map circle
```

`terrain_dem.py` 属于 `core_algorithm`；raw replay schema/backend、domain、GNSS sidecar 和 replay serializer
属于 `task_contract`；DEM schema 与 simulator stack 属于 `safety_policy`。REPLAY、SIM、HIL、REAL 只替换
输入 backend，地形交点与不确定性传播代码不分叉。DEM+GPS 路径拒绝同源 AGL 二次融合，且在 GPS/DEM
时间偏斜、越界或交点失败时不生成平地假点。

当前 parity SHA-256 为
`C28AC3C300DDACAD7B97097EC80987AED78E8EB8B19101AD98FD8D66F7985739`；hash 为
`core=e90559d121468097f9378790e15902002aca8e6cdacd80c8d5bb8bb5426a93a1`、
`px4=18023962f68a61fdfb14c44e5f0a6a5bb713c782166386bd2b1f2bf7b567651d`、
`safety=b4627350ba8d0eea675e56d7dec3a5c26cf4b00583b18d5c92ce5c527f4a3f82`、
`task=b3c4aa6bd6bde2ba73707c8d2edae459daf58af0d4a1c70f2a8a6e54ebd63dab`、
`ui=c46a0727a854371cfd01a3b1eba0d56a1b5774a6e12dc00331e0a17afc11d3a7`。v52 三机型 binding
SHA-256 为 `2B886AEF69197268A8658E1A5C8AA24D753DE4E560934CC245A667D7AA49948B`，聚焦回归
`458 passed`；完整回归为 `2855 passed, 1 skipped`，JUnit SHA-256
`E82F4F8FCF3C0639B272ECCECC06B8BFE85B8D946B78C3E8B29A1F1BFEDCE077`。该 binding 是 deterministic
protocol/asset preflight；v52 的三机型 DEM/世界仍平坦，外部 3-D、人工视觉、真实
GNSS/Pixhawk、HIL/REAL 和室外精度均未宣称通过。v53 后续增加的独立坡面资产真值不改变该记录范围。

v52 阶段的 E7 wrapper 固定到 v52 external join/revalidation 与当时的三机型 3-D 文件，不按 mtime 扫描
artifact；自 v54 起仅三机型 3-D 输入切换到当前 Odometry SDF 证据。六源 rebind 的 SHA-256 为
`3B82910E3122DF3A929DA24E5D4C119916B0BE22035BBEB3C25208CE491F7A79` /
`CE6A3E4DE25D3E60466BA20FFF70C9FC51B59A7455AC5506935E2A3C3AD460B6`。strict evaluator 精确退出 `2`，
32 项 blocker 保留，说明 shared-core 前进没有绕开外场与最终 release 边界。

## v51 协议失败语义与 SIM 地理资产均进入共享契约（2026-09-05，`v0.3.0-pre.8`）

v51 没有创建新的 UI、simulator-only estimator 或第二条飞控路径。新增绑定沿现有共享链路进入：

```text
MultiModalRangingEngine internal geometry reason
  -> operator_status canonical wire reason (bit 31)
  -> authenticated RANGE_STATUS
  -> Python/QGC C++ shared reason registry
  -> native Fly View invalid/degraded status

scenario runtime manifest builtin URI
  -> simulator/stack.py strict resolver
  -> repository GeoJSON + local DEM schema/content/world-origin validation
  -> per-file SHA-256 + aggregate stack identity
  -> VTOL/SIM acceptance real-file availability
```

三个正式机型场景地图位于 `simulation/maps/`，高程网格位于 `simulation/terrain/`，格式契约为
`configs/schemas/local-dem-grid.schema.json`。resolver 同时拒绝路径逃逸、符号链接、场景 ID 错配、网络
URI、非法 WGS-84 坐标、世界原点错配、非矩形/非有限网格和无覆盖原点的 DEM。v51 当时的网格刻意
保持平坦，与对应 Gazebo world 一致；坡度插值和 v53 独立坡面资产真值属于后续记录，不在 v51 证据范围内。

当前 parity SHA-256 为
`FA7EF5D6B1E8042F88F119D0FB9832540A1517B2320784956BD990567F77DD43`；hash 为
`core=f5ae7dca085db1556ad2c63ad6b08c1f155b229eb72ad78fb6bc6653e7b46044`、
`px4=544079908cbb34f9b6a28fe02e344153595cb1b983271aaa027c50d1e2247ce1`、
`safety=100f5954da745058d479c8dc6a055589e33116120ade8995765b506b865b7c92`、
`task=3fe92424dc4fbb881165863470ea776bf29ece6fc6b96c4f23e1c2b10f82ba04`、
`ui=c46a0727a854371cfd01a3b1eba0d56a1b5774a6e12dc00331e0a17afc11d3a7`。当前 portable EXE SHA-256
为 `06E7B1BBEC1B96DC6E5A231B12B88725122476075ED86A24BBEF554CB899E5FE`；真实现场门仍未关闭。

## v50 三轴安装角误差仍走同一个 Ranging 核（2026-09-05，`v0.3.0-pre.8`）

v50 没有增加 backend、simulator-only estimator 或第二套地图逻辑。相机安装调查值沿既有唯一链路进入：

```text
camera calibration JSON
  -> CameraCalibration mount pitch/yaw/roll + common per-axis 1σ
  -> MultiModalRangingEngine three-axis NED ray perturbations
  -> ground range / bearing uncertainty
  -> TargetGeolocationEstimate horizontal 1σ
  -> authenticated RANGE_STATUS / TARGET_GEOLOCATION
  -> native QGC Fly View evidence + map circle
```

REPLAY、SIM、HIL、REAL 只替换数据源和 runtime backend，以上算法文件完全相同。当前 parity SHA-256
为 `0DA306BCD0A635DF70584BB14514153FB05433A452F52F7D58FB32F7B1C46336`，相机标定 schema 与
`multimodal_ranging.py` 同时纳入 core，`core_algorithm_hash` 更新为
`f5ae7dca085db1556ad2c63ad6b08c1f155b229eb72ad78fb6bc6653e7b46044`；UI contract、safety、task
contract 与 PX4 Mission path hash 未变。2,829 项全回归中 2,828 通过、1 项因 Windows symlink 权限跳过；
E7 strict 仍为 32 blockers，软件几何证据没有被升级成现场或硬件证据。

## v49 QGC 显示状态与动态飞控权限同源（2026-09-05，`v0.3.0-pre.8`）

v49 没有增加 backend、控制器或 simulator-only view。动态权限仍沿 v48 的唯一链路进入 QGC：

```text
ProfileBoundFlightControlProvider.live attestation
              -> LiveApproachHilCoordinator.flight_control_enabled supplier
              -> APPROACH_STATUS.flightControlEnabled
              -> MultiDetectState.fixedWingAimControlEnabled
              -> fixedWingAimActive = approach active AND control enabled
              -> native FlyView banner
```

这样 provider 锁定时，QGC 只能显示“目标已确认 / 飞控输出锁定 / 仅显示姿态建议”，不会出现执行器零输出
但 UI 标成“正在瞄准”的分叉。`FlyViewCustomLayer.qml` 仍复用 QGC 原生 palette、字体、按钮和 ScreenTools；
SIM/HIL/REAL 沿用同一状态和同一 Fly View。

当前 parity SHA-256 为 `B8D2918DBC3040596A86BB6B9FC8FB7843E660929DBCD2D677C70D3592F69167`，
UI contract hash 为 `1dc7e2ae6edbb7ed2f4b212232ebf24fcb2fd54cf84373e4e2941af0dc88c992`；
PX4 Mission path hash 保持 `544079908cbb34f9b6a28fe02e344153595cb1b983271aaa027c50d1e2247ce1`。
重新构建的 Release/portable EXE 字节一致，SHA-256
`EE2A6A5E7B4561D56D6130FE9D81AEAC8F7093EB1A352792CB85AB353133F57D`；原生协议自测、隐藏
offscreen boot 和 `2826 passed, 1 skipped` 全回归均通过。E7 strict 仍因 32 个真实/人工/发布输入缺口
返回 `2`，因此软件验收没有被升级为硬件或最终发布证据。

## v48 静态授权与 live attestation 分层（2026-09-05，`v0.3.0-pre.8`）

v48 没有增加 backend、控制协议或 simulator-only 路径；它把现有 profile-bound runtime 的两个不同
生命周期显式拆开：

```text
runtime_profiles.py        signed profile / HMAC / static identity authorization
          │
runtime_launcher.py        receive-only probe, then construct a new locked provider
          │
          ├─ provider.snapshot / refresh_profile_binding
          │    fresh heartbeat + same-source firmware -> live attestation
          │
          ├─ fixed_wing_aim_control.py
          │    every output reads current transport authority
          │
          └─ approach_live.py / cli.py / live.py
               every status and audit reads the same dynamic authority
```

probe provider 与 flight provider 不共享 freshness。新的 flight provider 从锁定开始；未认证时连 ranging/
RC stream request 也不能发出，输出方法不能顺便完成第一次认证。断链、超时、稳定身份变化和固件错配会
清除认证与固件缓存。这保留了同一个 QGC Fly View、同一个 PX4 Mission 路径和同一个安全状态源，避免
出现“UI 说锁定、执行器仍沿用旧布尔值”的分叉。

`configs/release-parity.json` 已将 `src/multidetect/approach_live.py` 与
`src/multidetect/fixed_wing_aim_control.py` 加入 `px4_mission_path`。当前 parity SHA-256 为
`49D9B243A26591784DA0651A0B8789C5AEC0E162911199EC447C38EE370152D6`，PX4 Mission path hash 为
`544079908cbb34f9b6a28fe02e344153595cb1b983271aaa027c50d1e2247ce1`。完整回归为
`2825 passed, 1 skipped`（2,826 tests）；v48 software-only external join/revalidation 通过，但 E7 strict
仍因 32 个真实/人工/发布输入缺口返回失败，`px4_mission_feedback.present=false`。架构账本因此没有把
软件时序测试升级为硬件或最终发布证据。

## v47 legacy 诊断与 REAL 输出权限分离（2026-09-05，`v0.3.0-pre.8`）

v47 不改变目录边界、不增加 simulator-only backend，也不引入第二套控制接口；它把已有
`runtime_profiles.py` 与 `runtime_launcher.py` 的权限语义收紧为：

```text
legacy signed profile ── generic diagnostic load, no output request ──> readable + locked
                    └── REAL output request ── firmware missing ──────> invalid + locked

current signed profile + live same firmware + explicit opt-in
                    └── launcher strict load ──> profile-bound provider
                                                └── rebind before output
```

launcher 对 HIL/REAL 总是启用 firmware requirement；provider 构造器独立拒绝无 firmware hash 的 flight
profile，动态 `_profile_identity_matches()` 对 flight profile 也始终比较 firmware。observer profile 没有
输出能力，历史文档可读性不会被错误解释为运行授权。

当前 parity SHA-256 为 `B40053E5EE74D09FA890E506BEACF7392288778AAE22F449016FC88D1563BBCF`，
PX4 Mission path hash 为 `63674e1adedc5884561ffa62f6c6b4f35d1e4a067bb5114e29ab3a6d305917c5`；v47
external join/revalidation SHA-256 为
`456524B74F70F8431C186557255E067C6F70DFB4A8E6C42D93F2C16622DA1890` /
`F4000DF499E8DEE33C4D795D49B6CE177347DEA5E661959118277E450692907C`，仍是 software-only 重绑定。

完整回归为 `2820 passed, 1 skipped`（2,821 tests），JUnit SHA-256
`557FA4D8D306193388DB3D5600A8E5AD4228C2C2E494112F4857EB201EDD8934`。最新 E7 report-only/strict
分别返回 `0`/`2`；strict manifest/report/strict-report SHA-256 为
`88C4E86F007F92F9ABC5BB9D8D507BEF3E4933B9A1374F059025EE2B351C92C6`、
`2E871C79E3F58C7520B0EF70993AF7C2B1BFE29FEB8E2BCDD75CBB58B9F72155`、
`CC62CD5D4BBFCB026022DEDE23483746AF7E9C76C32AFF31BAD16603FF5CC931`。PX4 feedback 六叶仍 absent，
所以本轮没有改变 HIL/REAL/硬件/外场的证据等级。

## v46 固件身份绑定 RuntimeProfile 与 E7（2026-09-05，`v0.3.0-pre.8`）

v46 沿用 v45 的 receive-only `AUTOPILOT_VERSION` parser，不新增第二套设备身份或控制路径：

```text
Mission + endpoint + fresh heartbeat + same-source AUTOPILOT_VERSION
                         │ provision probe window; zero transmit
                         v
signed RuntimeProfile(profile.firmware_identity_hash)
                         │
                         v
feedback observation v2(runtime_binding.observed_identity.firmware_identity_hash)
                         │ reopen and compare
                         v
six-leaf E7 binding(all_firmware_identities_bound)
```

`runtime.py` / `runtime_profiles.py` 将 firmware hash 设计为结构上的可选字段，使历史 HIL/REAL 文档仍能
加载；`px4_mission_feedback_cli.py provision` 是生成当前 profile 的严格入口，必然写入该字段；
`px4_mission_feedback_runtime.py` 与 `px4_mission_feedback_binding.py` 则在 E7 边界将其作为强制值。
因此“legacy 可读”和“当前发布证据可通过”不会混为一谈。所有层只传规范 SHA-256，不序列化原始 UID。

当前 parity SHA-256 为 `3A85B02436CD545E1409B842D28AC3A600DC02749B5F4A5D2130A525D4A21F95`，
PX4 Mission path hash 为 `3639c0a56d8d9ab3e799242ff190838a8f9959d07a938996b36e5e86ab775e5a`。
v46 external join/revalidation 只是既有 software/SIM 来源对当前源码的重绑定；真实 V6X 与 HIL/REAL
六叶仍待现场采集。

v46 完整回归为 `2816 passed, 1 skipped`（2,817 tests），JUnit SHA-256
`18EDEF1953C0F4E4EA452AF210185B5074C4D61C3E02F331B368E802806401F1`。最新 E7 report-only/strict
分别返回 `0`/`2`；strict run 的 manifest/report/strict-report SHA-256 为
`CC1BB8BF9F68AD904CA90049293D7C5526005217F199EE9C3ECB369AF5778C33`、
`CA5601CB7D3BB18CE0387E9AD8809ED3087EC55A0751544EBD6303738069AC00`、
`F413957547B5AE31A3F5E48AA1B68BC5D1A8EA155EE4E1C51FE7B9CA54831020`。E7 中 external binding
通过，PX4 feedback 六叶 absent，边界与代码结构一致。

## v45 PX4 固件身份被动观测（2026-09-04，`v0.3.0-pre.8`）

`pixhawk.py` 的 receive-only provider 现在把动态 heartbeat 身份与稳定固件/板卡身份分开。只有先接受
autopilot heartbeat 后，来自同一 MAVLink system/component 的完整 `AUTOPILOT_VERSION` 才能生成
`PixhawkAutopilotVersionIdentity`：

```text
HEARTBEAT(system/component/autopilot/MAV_TYPE/MAVLink) ─ source lock
                                                        │
AUTOPILOT_VERSION ─ strict wire widths + arrays ────────┤
                                                        ├─ public firmware fields
uid2, else uid ─ domain-separated SHA-256 only ─────────┤
                                                        └─ canonical identity SHA-256
```

原始 UID 在解析栈帧之外不保存，identity 对象与 JSON 只保留不可逆 hash。当前或更新的畸形版本帧会清空
旧值；其他 system/component 的帧只计拒绝，不污染已经锁定的来源；heartbeat 的稳定绑定字段改变会清空
版本状态。`pixhawk-check --require-autopilot-version` 复用该状态并继续声明/验证零发送。该层在 v45
只是后续 RuntimeProfile firmware binding 的输入；v46 已接入 v2 feedback/E7 schema，但本地测试仍不会
升级为真实 V6X、HIL 或 REAL 证据。

该边界的完整回归为 `2808 passed, 1 skipped`；JUnit 395,570 bytes，SHA-256
`E1EB8D218E039A59D6A9084D837B17C965DC9D67A44330A39DA2A6B0333646B3`。当前 parity SHA-256
`02BF3559EDE0C955A700B743436EA9AEA652DA29F1FCCCEC8E4C7D3A1F642DF6`，PX4 path hash 为
`9dfcd64df28141ad9f08f439bf839b7a49e2b0ebdefb9cd8367d9a720e2a91f8`。v45 external join
`6F2BE5D26B5FDA2BDCC7760ADE684356F8E92CBD3A0EB5CEFF7DD0DDAA86EA14` 与 revalidation
`7862D75A70933A11E4EB5A62393930FC3D8036B8248D30473538D329EFB0A15D` 均通过；它们仍只代表
software/SIM 证据。

## v44 RuntimeProfile 现场 provisioning（2026-09-04，`v0.3.0-pre.8`）

`px4_mission_feedback_cli.py provision` 现在是 profile 文件的唯一现场 bootstrap 入口。它复用
`mission_planning.py` 的严格 Mission loader、`pixhawk.py` 的 receive-only provider、
`runtime_launcher.py` 的 vehicle/endpoint identity 派生，以及 `runtime_profiles.py` 的 HMAC signer/loader。
数据流为：

```text
ExecutableMission ─ adapter/airframe hash ┐
fresh PX4 HEARTBEAT ─ vehicle identity    ├─ signed RuntimeProfile document
resolved endpoint + baud ─ endpoint hash ┘            │
                                                       └─ feedback observation v2 → E7
```

命令不请求 ranging streams、不发送 MAVLink、不启动 Mission，也不打开 flight-control opt-in。profile
写入前会从临时文件重新验证签名、observed identity 和 locked-output 决定，默认禁止覆盖。console script
为 `multidetect-px4-runtime-profile-provision`；核心函数与测试仍位于 PX4 Mission feedback 模块边界，
因此同一文件由 `px4_mission_path` parity 绑定。

当前 parity SHA-256 为 `BB5D6285E81B813494BF56432F76FB045F7F4DAC454553F43B4041728F8C2F62`，
PX4 path hash 为 `a8d5a95d18e7efb5c829f53a8402c0d8bc3019d6012186351196a20c553b1f87`；v44 E7
manifest/report/strict-report SHA-256 为
`432A9835B2D061B15E8CB9D54759FBF36D9D1E511F0FD09E26E14953DB0DACDF`、
`423D15645792457F7107BF7E1C9A59F0D1ED125F87733A72B62B80DF26CB4527`、
`2D5510308BC3CB49C517F69C86EEE09F2941274A41193489970520F6972D72BC`。现场六源仍未采集，
profile provisioning 的成功不能升级为 HIL/REAL、硬件或最终 release 通过。

该边界的聚焦回归为 `47 passed`，完整 Python 回归为 `2793 passed, 1 skipped`、退出码 `0`；
3,226-byte 汇总日志
`artifacts/evaluation/terminal-continuation-v44/full-regression-v44-runtime-profile-provision-final-20260904.log`
的 SHA-256 为 `C437BD893FF13E5E161161F6A762A821B661CC1C44A7534FF5D27573A4938C5F`。

## v43 签名 RuntimeProfile 进入 HIL/REAL feedback 叶节点（2026-09-04，`v0.3.0-pre.8`）

`px4_mission_feedback_runtime.py` 位于 receive-only observer 与 v42 六项 E7 聚合器之间。它复用
`runtime_launcher.py` / `runtime_profiles.py` 的身份派生与 HMAC profile loader，把一次观测锁到：签名
profile 文档及其 canonical profile、完整 PX4 heartbeat 派生的 vehicle identity、resolved endpoint +
baud identity、Mission airframe profile 和禁用输出的 loader decision。HIL 只接受 `hil` profile 且不能
携带 signing identity；REAL 只接受 `flight` profile，但采集时不打开 output opt-in。

`px4_mission_feedback_cli.py` 写 v2 报告，`px4-mission-feedback-observation-v2.schema.json` 封闭其结构；
旧 v1 reader 保留用于历史诊断。`px4_mission_feedback_binding.py` schema v2 将每个来源升级为
`MODE + VEHICLE + MISSION + SIGNED_RUNTIME_PROFILE + REPORT_V2`，要求六份 profile 与六份 report
各自不同，并由 manifest evaluator 重新打开三类文件。HMAC/signing key 仅从环境变量读取，不写入证据。

当前 parity SHA-256 为 `B4E080286DAEA35AF55734DE1C3B0AF05427373A10F90A5E186712AF5E6E7D5B`，
PX4 path hash 为 `c5cb36fed3a1c8576178a1021908529ea7d8e12dd3035a39ab4ee5bd5d1f3ccb`；完整回归为
`2772 passed, 1 skipped`，汇总日志 SHA-256 为
`399453AB6E33089442CDB13795F242683777C0D2A15D948AB4BF5963BDD7EA95`。v43b E7
manifest/report/strict-report SHA-256 分别为
`C649E62BF1E4C88DEB5CE6BE173B2987DF55826DF9799812934477981CDFF996`、
`89E9A82B4675C0DBB4F1FB36301C9F98BDC6BEBF9CE31F49808F3B470FAFD06B`、
`C609F0BA2E1B7F8E7886797FFD8A1AEE3B7812B85F72AEEDD92CF1DCB3211814`。当前没有现场六源，
`px4_mission_feedback` 仍 absent，总门保持关闭；software-only parity 重算不替代真实 HIL/REAL。

## v42 HIL/REAL 三机型反馈的 E7 直接绑定（2026-09-04，`v0.3.0-pre.8`）

`px4_mission_feedback_binding.py` 位于 Mission wire observer 与 release gate 之间。它不创造新状态机，
只把六个最小可信观测单元组成封闭矩阵：`hil|real × fixed-wing|multirotor|vtol`。每项保存相对路径、
文件 hash/bytes、Mission identity、airframe adapter、runtime/source identity、feedback hash 和
receive-only 状态；读取时从磁盘复算所有字段。报告文件必须六份不同，Mission 则允许同机型在
HIL/REAL 共用。record 的 hardware/HIL/REAL/operator-visual claims 固定为 false，所以子门通过也不能
越权宣称整套现场验收。

`release_gate_manifest.py` 负责构建记录，`release_gate.py` 负责独立重验并与四模式统一
`px4_mission_path_hash` 比较，`release-gate-manifest.schema.json` 负责 JSON 结构闭锁，
`run_current_release_gate_pre8.ps1` 提供现场六元数组入口。该模块和 schema 已进入
`release_parity.py` 的 PX4 Mission path 文件组。最终 release 必须有合格矩阵；当前 prerelease
快照故意为空，`present=false` / `passed=false`，真实 HIL/REAL 仍 pending。

当前 parity SHA-256 为 `C2BAC5A77719B24B9F899B278053DE5CC3526FEDB2C46371AF6D94090DDAB8E9`，
PX4 Mission path hash 为 `9ff62c8ac9daea498732a9f5690ef195b8091dd94e621947e5d2ea3fb11500e8`；完整回归
`2764 passed, 1 skipped`。v42 report-only E7 manifest/report/strict-report SHA-256 分别为
`9F8330CEC3D4175E314F78C00C99A3D6CCFBCE711D08F0D6C5E4A0417B859A2A`、
`8CBBD0DD0468796EDAF596C6A6359EF4A7A9D27CA32168DD1F953D5F55C8FB7E`、
`27E5A41035A6D31251F85F0913CB0B25256B15B3FD7DC762E93F07F95167BE5C`；总门保持关闭。

## v41 PX4 Mission 实机反馈契约（2026-09-04，`v0.3.0-pre.8`）

新增 `px4_mission_feedback.py`，把 `MISSION_CURRENT` / `MISSION_ITEM_REACHED` 绑定到同一份
`ExecutableMission` canonical hash、任务项数量、MAVLink source identity、单调接收时间和 `1.5 s`
新鲜度。`pixhawk.py` 分别维护 current/reached 时间戳与缓存超时；`mission_progress.py` 可选要求同一
决策时刻的合格 feedback 才继续评估已经启动的 Mission。反馈链只读，不上传任务、不解锁、不改变
模式，也不替代 WGS-84/GPS/接受半径算法。

两个严格 schema 允许 MissionProgress/scene stream 嵌入 hash-bound feedback；省略字段的旧快照与
旧 replay 保持 canonical identity。新增测试还锁定了 legacy raw replay 重新打开后的
`recording_hash`，避免未来增加遥测字段再次静默改写证据。

`px4_mission_feedback_cli.py` 是现场可执行的只读边界：共用 `mission_planning.py` 的严格加载器读取
bare/compiled/planner 三种 Mission artifact，通过 `PixhawkReadOnlyTelemetryProvider` 监听 MAVLink，
并写入 `px4-mission-feedback-observation.schema.json`。报告同时记录接收错误、消息类型计数、零发送
计数、source identity、feedback hash、evidence hash 和全部 false 的硬件/发布 claims；独立 verifier
会重新计算，而不是相信文件中的布尔值。

当前 parity 文件 SHA-256 为
`B5FE05726534F5781995E56C85D3CA7043553B766C548B17083711FCAFF53776`，PX4 Mission path hash 为
`1fa6bfe715ace25dc0e1c0818571e25af113d4ab1e104d40da002e5a5c765b7b`；完整回归
`2756 passed, 1 skipped`。这不是实际 Pixhawk/HIL/REAL 通过记录；现场硬件、室外 GNSS、实飞和
人工 Fly View 仍 pending。

## v40 3-D scene stream 的 Mission 进度绑定（2026-09-04，`v0.3.0-pre.8`）

`SimulatorSceneSnapshot` 对编译 Mission 增加可选 `mission_progress` 字段：它保存同一
`SimulatorClosedLoop` tick 的严格 `MissionProgressSnapshot`，让 3-D renderer/replay 与 runner steps、
总 evidence 使用同一份 Mission hash、序列游标、GPS/link freshness、距离和安全门。旧流没有该字段，
因此 canonical hash 与回放保持兼容；新字段由进度契约再次解析，篡改比例、序列、未知字段或 truth
注入都会 fail-closed；同一条 stream 还要求每帧 Mission ID/hash 一致且不能半途缺帧。
`simulator-scene-stream.schema.json` 内联相同定义，可离线独立验证。

`python -m multidetect.simulator.scene_state` 现在也接受 `--mission` 和显式
`--start-mission`，与 scenario runner 共用 Mission 文档解析和 start 语义；省略启动开关只记录
`ready`，不产生隐式 Mission 控制。这一步仍属于 truth-free software/SIM replay 子门，真实
PX4 `MISSION_CURRENT`、GNSS/Pixhawk、HIL/REAL、人工 Fly View 和最终 `v0.3.0` 发布门保持 pending。

## v39 场景运行器接入编译 Mission 进度（2026-09-04，`v0.3.0-pre.8`）

`SimulatorScenarioRunner` 现在可以在不改变既有 manifest/seed/clock 语义的前提下接收一个
hash-bound `ExecutableMission`。构造器参数 `mission` 与 `mission_progress_config` 会原样交给
`SimulatorClosedLoop`，`bind_mission()`、`clear_mission()` 和 `start_mission()` 只转发到同一个
authority → adapter → MAVLink sink；每个场景步骤额外写入 `mission_progress` snapshot，场景总
evidence 继续重算同一份 canonical hash。未显式调用 `start_mission()` 时进度保持 `ready`，不会
因为传入编译产物而隐式发送任务或执行器输出。

新增回归覆盖 runner 构造绑定、显式启动、步骤证据和无 Mission 时的配置拒绝。该层仍是
truth-free、observation-only 的 software/SIM 观测边界；PX4 SITL `MISSION_CURRENT`、真实 GNSS、
Pixhawk、HIL/REAL、人工 Fly View 与最终 `v0.3.0` 发布门保持 pending。`mission_progress.py` 与
其 schema 仍由 `core_algorithm` parity 绑定；当前 `configs/release-parity.json` SHA-256 为
`A95880A967954CFD4F8F11A6C8C35B12C5F1AFB1E0A1FE5D656DBA31012A8CD0`，
`core_algorithm_hash=0f29c28f30c581f292fe9496ca5f14b7ee3b12bfd6ae72bc19300b82cf67031e`。
命令行 `python -m multidetect.simulator scenario --mission <artifact.json>` 同时接受裸
`ExecutableMission` 和完整 `PlannedMissionCompilation`；只有显式添加 `--start-mission` 才会
记录启动动作，省略时仅输出 `ready` 进度。

## v37 文件化 Planner → PX4 Mission 编译入口（2026-09-04，`v0.3.0-pre.8`）

`MissionCompileRuntime v1` 将 runtime home、三机型 geometry 和 safety policy 收在一个严格
JSON 文档中；未知字段、truth/expectations 或 vehicle-class 错配均在进入 compiler 前拒绝。
`VtolMissionGeometry`、`FixedWingMissionGeometry`、`Px4MissionSafetyPolicy` 和
`Px4FixedWingSafetyPolicy` 的 `to_mapping()` / `from_mapping()` 保留完整包线字段。新增
`multidetect-mission-compile`（`python -m multidetect.mission_compile_cli`）读取 task/context/
runtime JSON 并生成可由 `PlannedMissionCompilation.from_mapping()` 重载的 hash-bound plan、
executable mission 与 `MISSION_ITEM_INT` wire artifact；它仍不打开链路、不上传 PX4、不产生
飞控输出。输入 Schema 为
`configs/schemas/autonomy-mission-compile-runtime.schema.json`，CLI、schema 和 mission seam
均纳入当前 `px4_mission_path` parity；真实 GNSS、Pixhawk、HIL/REAL、人工 Fly View 和最终
`v0.3.0` 仍保持 pending。

v37 VTOL CLI smoke 已生成并通过 `PlannedMissionCompilation.from_mapping()` 重载：证据文件
为 `artifacts/evaluation/terminal-continuation-v37/mission-compile-smoke-20260904.json`，
SHA-256 为 `BEA3FFBE5D42464534E366FF3BEEAB661EA474A6AEC9CB281D959E93E7ED0B1C`，包含 8 步
plan 与 7 个 wire item。当前 parity 文件 SHA-256 为
`AD1ED71E79A8E048D5F50365F6BE9B80F69AB505917C7CA476B66B1B57A91310`，对应
`px4_mission_path_hash=e533372c6b49bdfa05760f02328305e5ec012777487a8e708a829773a4f22ada`；
证据仍是 local software-only，不升级室外 GNSS、真实硬件、HIL/REAL 或最终发布门。完整 Python
回归为 `2713 passed, 1 skipped`、退出码 `0`，日志 SHA-256 为
`7A02B4E7C4D10D5E7656FA3B91234A5766B5D0A8D0FBECA681902EF55A534BB8`，日志路径为
`artifacts/evaluation/terminal-continuation-v37/full-regression-v37-mission-compile-20260904-rerun4-summary.log`。
同日只读现场链路预检 `artifacts/evaluation/terminal-continuation-v37/field-link-preflight-20260904.json`
的 SHA-256 为 `3807E51123850D250D73193ACC5288CCDBB3473AEC9AA5D4F8A5D7E39D78C9A6`；本机
源地址 `192.168.1.13` 不在期望的 `192.168.144.0/24`，Jetson/GR01/RTSP 不可达，
`hardware_link_ready=false`。该预检是只读环境诊断，不升级硬件、GNSS、HIL/REAL 或发布状态。

## v36 Planner → PX4 Mission typed seam（2026-09-04，`v0.3.0-pre.8`）

`src/multidetect/autonomy/planner.py` 是 REPLAY/SIM/HIL/REAL 共用的 platform-neutral 任务规划
入口；`src/multidetect/mission_planning.py` 将同一份 task/context 结果交给 VTOL、fixed-wing
或 multirotor 的 canonical PX4 compiler。`PlannedMissionCompilation.to_mapping()` /
`from_mapping()` 严格保存并重算 plan、executable mission 和 `MISSION_ITEM_INT` wire hash，
跨上下文、未知字段或篡改声明都会 fail-closed。planner 与该 seam 已分别纳入 v36 阶段 parity 的
`core_algorithm` / `px4_mission_path` 组；v36 阶段 parity 文件 SHA-256 为
`66DB8D58198B183E8322D4CE2575DA98FD940F56120F05CF3326F5DCB41F22E2`，对应核心回归
`2709 passed, 1 skipped`（日志 SHA-256
`D531C53B29FB887FB612E9064E9EC6F6DC53632809641F9B40EEA8E55651ABFB`）。该路径只做 authoring/compile 和审计，不打开飞控链路、不上传 PX4，
现场 GNSS、人工 Fly View、HIL/REAL 与最终 `v0.3.0` 仍按总门定义保持 pending。

## v35 加速度预算完整性门与当前 parity（2026-09-04，`v0.3.0-pre.8`）

目标帧与 GPS 时间对齐时，如果 telemetry 给出 `acceleration_uncertainty_mps2` 却没有完整 N/E
速度，`target_geolocation.py` 现在 fail-closed，避免静默丢失 `0.5·dt²·σa`。当前
`configs/release-parity.json` SHA-256 为
`D9CF6D9E625E6B0ADB56084628CCB39ED358E2156AF89201633C2F6E1F2B9AEF`，core hash 为
`7D35526D63E3BFE247776F1ED2CC14F2950E0E61AB72E10A170C5C41BB930CF3`。

当前 parity-valid external join/revalidation 使用 v35 目录，SHA-256 为
`B459F6CDA7E6BD60EE5BED486E56F25870D388E31650F54333AC2FA3741CD12B` /
`D825A2A8A1D8E3F152C35C6C8C70C3D847D9C26E3329A4F8A7EE2D27974B338C`，独立重算均为通过且
software-only。E7 report-only manifest/report 为
`B4801000CCF23B3243ADCBAA4788C00539973A0BA1A3D6D48233758782A728E4` /
`2E4CCE1FA23899958BBF05E5F399171CA462B83745D6435509BF5AEEC2FE71E7`；总门仍关闭，现场、
人工视觉、HIL/REAL 和最终 release channel 保持 pending。

## v34 机动运动误差预算与 replay-set 身份重绑（2026-09-04，`v0.3.0-pre.8`）

> v34 是 v35 之前的历史 parity snapshot，文件与证据保留用于审计；当前 wrapper 不再选择其
> external join/revalidation。

`src/multidetect/target_geolocation.py` 新增可选
`acceleration_uncertainty_mps2`，由 `VehicleTelemetry`、raw replay、live runner 和
operator-status 统一转发。投影均值继续按恒速模型，水平运动 `1σ` 用
`hypot(|dt|·σv, 0.5·dt²·σa)` 合并；非法/非有限输入或缺少完整 N/E 速度时 fail-closed，省略字段的
旧 telemetry 保持兼容。该层只服务目标落图与证据，不改变 QGC wire、任务契约、飞控或物理载荷输出。

telemetry identity schema 扩展使历史 Gazebo raw replay 的旧 `recording_hash` 不再能通过当前
verifier。本轮从同一个保存的 camera capture 生成独立 v34 副本，未修改历史
`artifacts/replay-sets/sim-gazebo-2026/`：
`artifacts/evaluation/terminal-continuation-v34/replay-set-sim-gazebo-2026/` 的 manifest
为 4,965 bytes、SHA-256 `F577BF8085F33311D20AE27BB882C5C5F0F7225FC6F86B379A5384982E1FEDFF`，
canonical hash `F2834DC4CD03C95DC6502C137EAA66430A3F68CC2F145498889156621FA32E4C2`；新的 raw
recording hash 为 `F1C3841E80889AFD39631CDE5AD4C341A243CA9055EEC4DEDA98FE19DB7E829`。六个
stratum 仍是 `synthetic_demo` 空基线，`accuracy_validated=false` 是预期边界。

v34 阶段按当时 parity 通过的 external SIM/runtime + AI join/revalidation：SHA-256
`80599422FD5D39E3445F8D3ADA5473CAC0F62D184023A2B2F2BD4CFF6E46C1FE` /
`06D4C67C5A095661F5D54A691C334C8F6D2576E5821CB365166C8CE2E28E1BAE`，两者 gate 均为 true
且仍只证明 software-only。v34 阶段 `run_current_release_gate_pre8.ps1` 无参数默认绑定 v34 replay-set、
accuracy/calibration 和 external 输入；E7 report-only manifest/report 在
`artifacts/evaluation/terminal-continuation-v34/release-gate-current/`，SHA-256 为
`7C08D3254C49DB9531D1B1032939FF1D4C01426164299C0EEF212A0B6C661FBF` /
`12C8C52A9F58EEA97EC6AF863295BAAA73605CC65D1AC0B83408BD48D75156C5`。总门仍关闭，现场
Jetson/GR01/相机、室外 GNSS、真实 Pixhawk、HIL/REAL、人工 Fly View 和最终 release channel
保持 pending。

## v32 导航速度残差不确定度层（2026-09-04，`v0.3.0-pre.8`）

`src/multidetect/navigation_fusion.py` 是 REPLAY、SIM、HIL、REAL 共用的速度融合核心。
GPS/VIO/空速候选先经过 `3.5σ` 成对一致性门，再以 North/East 两轴残差
`χ² / (2*(N-1))` 检查声明协方差是否可信；Birge ratio 会在过散时放大 `σ`，追加
`navigation_velocity_uncertainty_inflated`，并把结果降为 `DEGRADED`、保持 advisory-only。
GPS 质量门统一为 `fix_type>=3`、卫星 `>=6`，`255` 是未知哨兵而非有效计数。该层只服务
态势感知、目标落图和证据，不进入飞控/物理释放输出。

验收脚本与证据：
`scripts/create_navigation_residual_acceptance.py`、
`tests/test_navigation_fusion.py`、
`artifacts/evaluation/terminal-continuation-v32/navigation-residual-inflation-acceptance-20260904.json`
（3,351 bytes，SHA-256 `AA1E0EF13A40F9EDBD777B5D214F1601299E280DFF8E4B2B40353FF5037D47C7`）。
nominal/spread 场景专项 18 passed；当前 parity SHA-256
`723CC760FE277DB378BB3FA55BDB0D54149F72AF38C29D2EEC67EC95BA434B4B`，core hash
`354d6a61f1dc986eca15e30fefff7ee820e7d83d0c3a18f4071904fc2a194d35`。

新 parity 已重新绑定三机型六源 external SIM/runtime + AI：VTOL 源
`081B922CD725ACD1F42ED218F142E7AE1E8E5A684CA02F622B4AECE738E12DA`，join
`7AA749EB13AA18A42A734D890401588B9885A7805969CEE2BAF3AB500923E392`，revalidation
`F5D1F057D85498C8E1A8BC563D950419E2D5AAAA9FDBBB28D13DF768D8A7348F`，均位于
`artifacts/evaluation/terminal-continuation-v32/` 且通过；它们只代表 software-only
PX4/Gazebo/AI 协议证据。当前 E7 report-only manifest/report 位于
`release-gate-current-20260904T070708Z/`，SHA-256 分别为
`064FD23D88A0FA65280D4493A6AB9330748DC8D23C1FA261557DC8E7E515EBA4` /
`87D0D7F61FC802DBA66D54A592D3ED0AB30EB83115ADEBB091162D1E580D9187`；总门保持
`gate_passed=false`，现场链路、真实 GNSS/Pixhawk、HIL/REAL、人工 QGC Fly View、
replay-set 和最终 release channel 仍 pending。

## v33 现场链路预检与 E7 证据绑定（2026-09-04，`v0.3.0-pre.8`）

现场链路证据由 `scripts/run_field_link_preflight.ps1` 以 2 个样本重复生成，接口诊断和
结构/freshness verifier 通过；实际工作站 `Ethernet=192.168.1.13/24` 不匹配目标
`192.168.144.0/24`，Jetson/GR01/相机端点均超时，故 `hardware_link_ready=false`。报告
`artifacts/evaluation/terminal-continuation-v33/field-link-preflight-live-20260904T-current.json`
（12,923 bytes，SHA-256 `2B3694E74332B96B3257F405842B3584A44AE07D5FCEA8EC5E47B07DF31E651A`）与接口
诊断（4,412 bytes，SHA-256
`9800D930FD0FAF1D945420BF3849175F165DC463AEBD3308411143BDD99E9D68`）均只读；没有修改
网卡/IP/路由、登录机载设备、请求 MAVLink/RTSP 或发送控制数据。

当前 E7 report-only 绑定在
`artifacts/evaluation/terminal-continuation-v33/release-gate-current/`，manifest/report SHA-256
分别为 `C66DB565A12DC664C28F4DB12E38AD7E6611E28A14F650839362A18EDB817C19` /
`340CE3C01968194B77010A808AE8A09BD62F269D9D56C786224E88099A3827D1`；结构通过但总门仍
`gate_passed=false` / `release_allowed=false`，31 条 replay-set、现场、人工视觉、Mission、
HIL/REAL 与 prerelease 阻断保留。wrapper 无参数默认现指向 v33 现场报告；接通飞机网段后再
确认机载 IP 并重跑预检。

## v31 多源测距残差不确定度层（2026-09-04，`v0.3.0-pre.8`）

> 历史条目：当前 parity 默认包含 v32 导航速度残差层；以下 v31 测距 hash 与证据仅供
> 演进追溯。

`src/multidetect/multimodal_ranging.py` 是 REPLAY、SIM、HIL、REAL 共用的测距核心。当前
逆方差融合在硬一致性门之后再计算接受源的加权 residual chi-square；Birge ratio 会在实际
分歧超过声明协方差时放大 `σ`，并把 `absolute_range_uncertainty_inflated` /
`direct_range_uncertainty_inflated` 传到 `RangeSolution.reasons` 与 CI95。目标落图、evidence
card 和 replay bundle 复用该区间，结果保持 `DEGRADED` 和 advisory-only，不进入飞控或物理
释放链路。回归夹具和结构化证据位于
`tests/test_multimodal_ranging.py` 与
`artifacts/evaluation/terminal-continuation-v31/ranging-residual-inflation-acceptance-20260904.json`
（SHA-256 `88516D2203D168C894945EDC5F1D7DFA6569F07B8DF9DD8ED910058BC27FA2DA`）。

本轮同步刷新 `configs/release-parity.json`；core hash 为
`4ed386e9eaf91952b7c7be56d3d272efeaca4bb70a50e78ed57e04aa5307f5ad`。标准 wrapper 生成的当前 E7
report-only manifest/report 位于
`artifacts/evaluation/terminal-continuation-v31/release-gate-current-20260904T065500Z/`，
SHA-256 分别为
`CA97AE02767F93B691A3CB2B41F823FA0E53EB1D5D6B726C657D086861F523E1` /
`2E66751A6D7E0DEB16276431C5F9DECE74969D783C3EC78461EDD95FE0EC9B6E`。Docker Desktop Server
`28.4.0` 已完成当前 VTOL live-AI 外部运行；六源 external SIM/runtime join 与独立
`source_recomputed=true` 复核均通过，external 与 scene-stream set 子门均为 `passed`。
现场 preflight freshness 已通过但硬件端点和 aircraft route 仍不可达，因此真实
GNSS/Pixhawk、HIL/REAL、QGC 人工视觉和最终 release channel 继续是 pending；总门仍保持
`gate_passed=false` / `release_allowed=false`。这些 external 记录只证明 software-only
PX4/Gazebo/AI 进程和共享契约，不升级为真实硬件或视觉验收。

## v30d 三机型 scene-stream set 正式进入 E7（2026-09-04，`v0.3.0-pre.8`）

`src/multidetect/simulator/scene_stream_set.py` 是三机型 SIM 场景流的聚合边界：它把
`fixed-wing`、`multirotor`、`vtol` 各自已经通过 `scene_state` verifier 的 truth-free
120 帧流绑定为一个确定性集合，并重新打开每一条源流做 hash、scenario、版本和模式校验。
集合必须恰好包含这三种 mode，不能用重复 mode、缺失源文件或改写摘要字段旁路；独立入口
`scripts/verify_simulator_scene_stream_set.py` 使用同一 verifier，契约由
`configs/schemas/simulator-scene-stream-set.schema.json` 固定。

`release_gate_manifest.py` / `release_gate.py` 通过重复的
`--simulator-scene-stream-set-evidence`（短别名 `--sim-scene-stream-set-evidence`）把集合
写入并重验 E7 manifest 的 `simulator_scene_stream_set` 顶层 binding；Windows
`scripts/run_current_release_gate_pre8.ps1` 已将三条当前源流设为默认输入。该 binding 只证明
SIM 场景流的确定性和完整性，不暴露 Ground Truth，也不替代室外 GNSS、Jetson/Pixhawk、
真实执行器、HIL/REAL 或人工 QGC Fly View 验收；最终 `release` channel 仍要求它存在且通过。

当前绑定清单/报告分别为
`artifacts/evaluation/terminal-continuation-v30/release-gate-continuation-20260904T133400Z/release-gate-pre.8-current-20260904T133400Z-v6-bound.json`
（20,828 bytes，SHA-256 `98A301DA0147010CBF6F927122304E3AE1F47B08B095B8E9D89D535A2C37547C`）和
`artifacts/evaluation/terminal-continuation-v30/release-gate-continuation-20260904T133400Z/release-gate-pre.8-current-20260904T133400Z-v6-bound-report.json`
（52,806 bytes，SHA-256 `1A8B3A8402132B02788217787D564B32F87E2552506152232843B8D528C61EE8`）。
两者均记录 `present=true`、`passed=true`、3 条源记录和 9 项 checks 全 true；总 E7 仍为
`gate_passed=false` / `release_allowed=false`，因为现场链路预检为
`hardware_link_ready=false`。结构化验收摘要为
`artifacts/evaluation/terminal-continuation-v30/scene-stream-set-release-gate-acceptance-20260904-v3.json`
（2,858 bytes，SHA-256 `3FAAEF71A53E303B56EFBA550EDCD57A41C704894F860FA6DD3D080EF7D81214`；v3
绑定本次 3/3 现场样本并保留 v1/v2 历史，专项回归计数为 85）。本轮聚焦回归为 85 passed，严格源码回归为 2,667 passed / 1 skipped；跳过项仅是当前
Windows 账户缺少符号链接权限，版本继续保持 prerelease。

## v29 模型审核到 E7 的统一证据边界（2026-09-04，`v0.3.0-pre.8`）

模型运行时不再只由模型文件自身描述：`model_manifest.py` 固定 fire/smoke 候选的
`post_nms_N_x_6`/`N_x_6` 输出契约和 artifact SHA-256，`model_review.py` 将该 manifest、
精确模型文件、model-check、部署域 replay accuracy 与 provenance/dataset-rights/security/
quality 四类治理审核组成可重算 envelope。`scripts/create_release_gate_manifest.py` 的
`--model-review-bundle` 参数把 envelope 作为顶层 `model_review` 写入 E7 清单；
Windows `run_current_release_gate_pre8.ps1` 通过 `-ModelReviewBundle` 转发同一 envelope，
并默认绑定当前 v29 parity-valid external SIM 证据；`release_gate.py` 读取清单时重新哈希源
文件并重跑审核，防止只改摘要字段或替换模型后沿用旧指标。预发布可以保存 pending/synthetic
诊断，最终 `release` channel 要求
`production_approval_eligible=true`、`gate_passed=true`，审核包中的生产批准、飞控和物理
释放开关仍固定为 `false`。

该接线后的严格源码回归为 `2653 passed + 1 skipped`、退出码 `0`；日志
`artifacts/evaluation/terminal-continuation-v29/full-regression-model-review-wrapper-v2-summary-20260904.log`
的 SHA-256 为 `DCE90748ABBFD8E1A370D8CE285D603C9BFEC9BDAA23A0EAED9DBAF9671CD0FC`。唯一跳过
项是当前 Windows 账户没有符号链接权限；这不会改变任何现场、HIL/REAL 或最终发布门状态。

## v29 三机型外部 SIM/live-AI parity 重新绑定（2026-09-04，`v0.3.0-pre.8`）

当前仓库的外部 SIM 证据已经按照 canonical parity 重新生成：VTOL、fixed-wing、
multirotor 各一份 passive runtime 和一份 live-AI runtime，共六个独立 JSON 源记录。六者
均锁定同一 digest-pinned PX4/Gazebo image、只读 scenario/model 挂载、loopback 端口和
cleanup；AI 源另有 armed heartbeat、正向 AI steps 与 target exercise 证据。历史绑定旧
`434b86c6...` 的 join 保留为失败历史，当前 `core_algorithm_hash` 为
`994ae1dffb3856eac5d71b479a7377f3f1b1c5beda2d154d6c2ee3b733c15af3`，没有手工改 hash。

当前 join 文件
`artifacts/evaluation/terminal-continuation-v29/simulator-external-runtime-ai-evidence-20260904-current.json`
为 5,479 bytes，SHA-256 `1CD106451833C2CABBDFF3873DBF2893BB23D98C1D4EF0266ACAAD879AAFBE2E`，
`gate_passed=true`。独立 revalidation 文件为 560 bytes，SHA-256
`2848B8D8663DAB23AA8226E68876A513994D1F8BC1C59FC550432DFF3B443F8C`，`source_recomputed=true`、
`gate_passed=true`。

当前源码严格回归 `python -W error -m pytest --assert=plain` 为 `2639 passed + 1 skipped`，
退出码 `0`；日志 `artifacts/evaluation/terminal-continuation-v29/full-regression-strict-current.log`
为 3,041 bytes，SHA-256 `2615E7E55929F6AF229DD98CDBF0AA25798BFAFA0C676D3F13D94B7622B3EA37`。
`simulator_external_evidence.py` 已补齐显式 `python -m` 入口，避免 CLI 静默返回；模块和
安装的 console script `--help` 均通过。
根包的 release-gate 导出随后改为惰性加载，因此 `python -W error -m
multidetect.simulator_external_evidence --help` 不再触发 runpy warning；补充严格回归日志
`artifacts/evaluation/terminal-continuation-v29/full-regression-strict-after-lazy-root.log`
为 3,041 bytes，SHA-256 `F858F0C8FDC90706353EC0E9AADBF0B723D5F01BA129AC204B9D4C1645C3C778`，
仍为 `2639 passed + 1 skipped`。

release-gate report
`artifacts/evaluation/terminal-continuation-v29/release-gate-pre.8-current-20260904T014510Z-v6-bound-report.json`
为 47,012 bytes，SHA-256 `4BC6D27971AF76F9D100D5CE2C5C03C6613D811260CC58AF916CC4076A50BEB8`；
总门仍为 `gate_passed=false` / `release_allowed=false`。外部 SIM 通过不等于现场、室外
GNSS、REAL 目标、人工 Fly View、mission-path 或 HIL/REAL 通过；本轮 artifact 明确是
software-only disposable evidence，版本仍保持 prerelease。

当前以太网上的只读发现报告
`artifacts/evaluation/terminal-continuation-v29/field-link-discovery-live-20260904.json`
（8,526 bytes，SHA-256 `479732E9D458CA620DCB658B485FB2F4675EBD279BB1B8B2B65E51838A367369`）
扫描 254 台主机，只有 5 个 SSH 响应，MAVLink `5760` 和 RTSP `554` 均无命中；结构 verifier
通过但 readiness 仍为 false。发现结果不自动改动配置或提升现场门。

## v29 正向 replay evaluator 诊断夹具（2026-09-04，`v0.3.0-pre.8`）

`artifacts/evaluation/terminal-continuation-v29/replay-set-positive-synthetic/` 是单独的
synthetic evaluator plumbing 目录，不属于 canonical `artifacts/replay-sets/sim-gazebo-2026/`。
它从已校验 raw replay 生成五个有正例的 strata 和一个 `no_fire` 负例层；manifest SHA-256 为
`61E69B979C33F88B1E044F6192292E2CC55BF5EF6928645A7F28E16F5172BF46`。reviewed evaluator
report SHA-256 为 `6EF07FAFE7BA56E696498A856532BF46FE35227F22ADDC5C048FE4DE8C8048F2`，
readiness SHA-256 为 `1EB519C6A5350007973742593A60F669F94AECFD646E2CEED5730BD4E507C13C`；五层
正例覆盖和 aggregate IoU precision/recall `1.0/1.0` 均可重算。

该目录全部条目仍是 `source_kind=synthetic_demo`，未完成独立标注且没有部署域录像，因此
`deployment_domain_evidence_complete=false`、`accuracy_validated=false`、`gate_passed=false`
是预期且不可手工覆盖的状态。它只用于 evaluator/schema/hash 回归，不能作为部署精度、室外
GNSS、HIL/REAL 或最终 `v0.3.0` 发布证据。

## v28e simulator orchestrator 资源句柄收口（2026-09-04，`v0.3.0-pre.8`）

`SimulatorOrchestrator._launch_process()` 在进程工厂返回后释放父进程日志句柄，并在启动
失败时走同一清理路径；新增生命周期回归和严格警告全量回归
`python -W error -m pytest -q --assert=plain`（`2639 passed + 1 skipped`，退出码 `0`）。
日志为 `artifacts/evaluation/terminal-continuation-v28e/full-regression-warnings.log`；带跳过摘要的
复核日志为 `artifacts/evaluation/terminal-continuation-v28e/full-regression-with-counts.log`，
SHA-256 `807A392CC092389C8E1F51361A1C7922671E32111473F760F65347D13D0566C6`。
该变化不改结果证据格式，只改善 Windows/SIM 资源管理，仍不替代外部 3-D、真实硬件、
室外 GNSS、人工 Fly View、HIL/REAL 或最终 `v0.3.0` 发布门。

## v28d authenticated UDP header type hardening（2026-09-04，`v0.3.0-pre.8`）

告警数据链 header 现在对 JSON 数值类型做 fail-closed 校验：`version` 必须为严格整数 `1`，
`sent_at_unix_s` 拒绝布尔值、非数值和非有限值。sender、receiver 与离线 verifier 共用同一
规则，3 个类型混淆回归向量通过；当前源码全量回归为 `2638 passed + 1 skipped`，ruff/
compileall 均为 `0`。带完整 pytest 汇总的日志
`artifacts/evaluation/terminal-continuation-v28d/full-regression-with-counts.log` 为 305,776
bytes，SHA-256 为 `D8769517FA595283741E7129ABBD96AB2CC5B868B83CED54117953B441B2B068`。
本轮只收紧软件告警输入边界，不替代真实数据链、室外 GNSS、人工 Fly View、HIL/REAL 或
最终 `v0.3.0` 发布门。

## v28c simulator scene-state lazy import 与全量回归（2026-09-04，`v0.3.0-pre.8`）

`multidetect.simulator` 的 scene-state 类型和 verifier 现在惰性导出，scenario runner 只在
`record_scene_stream=true` 时导入 scene-state；直接运行
`python -m multidetect.simulator.scene_state --help` 不再产生 `runpy RuntimeWarning`，默认
runner、矩阵和 canonical parity 保持不变。editable venv 已生成
`multidetect-sim-scene-stream-verify`，valid 120 帧 stream 复核退出 `0`。

当前源码全量回归为 `2635 passed + 1 skipped`、退出码 `0`；日志
`artifacts/evaluation/terminal-continuation-v28c/full-regression-summary.log` 的 SHA-256 为
`3FAF04E313E44E1EBBE4C5FADD85E17CF247EA35AC36A255B77B4DF530336811`，ruff/compileall 均为
`0`。本轮仍只关闭 truth-free software/SIM 启动子门，不替代真实硬件、室外 GNSS、HIL/REAL
或人工视觉。

## v28b 独立 scene-stream verifier 回归（2026-09-04，`v0.3.0-pre.8`）

`verify_file`/`verify_main` 与 `scripts/verify_simulator_scene_stream.py` 已接入公共路径；
三份 120 帧 stream 通过文件 SHA、canonical stream SHA、逐帧 hash、schema 和 replay。当前
源码全量回归为 `2635 passed + 1 skipped`、退出码 `0`；日志
`artifacts/evaluation/terminal-continuation-v28b/full-regression-summary.log` 的 SHA-256 为
`B04577D8AE62918985336F718DAB039B38C7819304B4D119A340CA828615C5D8`。本轮仍只关闭
truth-free software/SIM 子门，不替代真实硬件、室外 GNSS、HIL/REAL 或人工视觉。

## v28 场景流 parser/schema fail-closed hardening（2026-09-04，`v0.3.0-pre.8`）

`SimulatorSceneStream.load_json()` 使用严格 JSON 解码，重复键、NaN/Infinity、非字符串
嵌套键、重复 fault ID 和 `pose/world_origin` 中的 evaluator truth 字段均 fail-closed；
`configs/schemas/simulator-scene-stream.schema.json` 对运行时对象的保留字段同步收紧。三份
既有 120 帧 stream 重新通过 schema、stream hash 与 round-trip replay，历史 artifact 未被
覆盖。最新全量回归为 `2634 passed + 1 skipped`，退出码 `0`；日志
`artifacts/evaluation/terminal-continuation-v28/full-regression-summary.log` 的 SHA-256 为
`997AF8F7E50D96A699342ACE72EFBF8D778D247B5110AF5C2787D9E93EF1C171`。本轮仍只关闭
truth-free software/SIM 子门，不替代外部 PX4/Gazebo、人工 Fly View、真实硬件、室外 GNSS、
HIL/REAL 或最终发布门。

独立复核入口为 `scripts/verify_simulator_scene_stream.py` /
`multidetect-sim-scene-stream-verify`，统一输出文件 SHA、canonical stream SHA、逐帧 hash、
schema 和 release label 绑定结果。

## v27 可重放 3-D 场景状态流（2026-09-04，`v0.3.0-pre.8`）

SIM runner 新增显式 `record_scene_stream` 分支，输出由
`src/multidetect/simulator/scene_state.py` 定义的 `SimulatorSceneStream`。它是 native QGC
Fly View、外部 3-D bridge 和 headless replay 共用的 runtime 状态边界，不是另一套
simulator UI。每个 `SimulatorSceneSnapshot` 绑定场景 manifest/version/hash、WGS-84 origin、
实测 vehicle pose（latitude/longitude/altitude/local N/E/姿态）、telemetry、observation、
AI/manual/authority/watchdog 和活动 fault，并带有逐帧 canonical SHA-256；整条流再绑定
source runner evidence hash 和 stream SHA-256。

对外 schema 为 `configs/schemas/simulator-scene-stream.schema.json`，公共导出位于
`multidetect.simulator`，命令入口为 `python -m multidetect.simulator.scene_state` 或
`multidetect-sim-scene-stream`。默认记录关闭，因此既有矩阵 evidence hash 不变。解析器
拒绝 evaluator truth、极点坐标、非法 freshness 形态、乱序/时间倒退和嵌套 hash 篡改。

本轮三份 120 帧状态流位于
`artifacts/evaluation/terminal-continuation-v27-scene-stream-{vtol,fixed-wing,multirotor}.json`，
文件 SHA-256 分别为 `F5D3BB2674E52871CC8729AD05C108768A8B759E9AC3023E163C9CE922DD0A22`、
`D149453D429E2336FE217F075953C70B8897721B1A645A9DC7CE137C4E679D6A` 和
`0A1792FE46A7D2544FB0D14794EFC0DFBB801204A76D9776C9D12E05712DF7E1`；均已用 JSON Schema、
逐帧 hash、stream hash 和 round-trip replay 校验。它们只关闭 truth-free software/SIM
状态重放子门，不替代外部 PX4/Gazebo、人工 Fly View、真实硬件、室外 GNSS、HIL/REAL 或
最终 `v0.3.0` 发布门。

新契约加入后的全量 Python 回归收集 `2634` 项，结果为 `2633 passed + 1 skipped`，退出码
`0`；ruff、compileall 和 parity 同步校验均通过。日志
`artifacts/evaluation/terminal-continuation-v27c/full-regression.log` 的 SHA-256 为
`79204B900F8FBAC1F6E48B4670A1B0C008975689933B6091CA63EE9E3A989D37`。

## v26 SIM 证据更新（2026-09-04，`v0.3.0-pre.8`）

当前软件/SIM 证据新增两份独立快照：

- `artifacts/evaluation/terminal-continuation-v26-scenario-matrix.json`：三机型
  deterministic scenario runner，统一 clock/backend/AI/authority/watchdog，故障尾段覆盖率
  `1.0`，重复 hash 一致，文件 SHA-256
  `06C8E829CCA2321C5D10F2F700E04D2469772E0C9C6E7711738B160DBC110FC1`。
- `artifacts/evaluation/terminal-continuation-v26-interactive-acceptance.json`：native-QGC
  interactive fixture，VTOL、fixed-wing、multirotor 共用 manual/AI authority、watchdog 和
  Mission write 阻断路径，文件 SHA-256
  `041B882EA4A9C3CB44A3AF2A74E61D3F216C99C4D3AE24B924D74E300BBFBAB8`。

两份 artifact 都是 truth-free software/SIM 证据，不能替代外部 PX4/Gazebo、人工 QGC
Fly View、室外 GNSS、真实 Pixhawk/Jetson/相机、HIL/REAL 或最终版本发布门。

v26 全量 Python 回归日志
`artifacts/evaluation/terminal-continuation-v26/full-regression.log` 的 SHA-256 为
`A70388821B0E7A4B8BE782162681BD3FDEFB5F710F40701A8A24C08617BC596E`，结果为
`2624 passed + 1 skipped`、退出码 `0`。

## v25 现场链路绑定复核（2026-09-04，`v0.3.0-pre.8`）

当前 E7 wrapper 使用 v25 watch 报告重新生成了独立绑定样本：输入
`artifacts/evaluation/terminal-continuation-v25/field-link-preflight-live-20260904a.json`
的 SHA-256 为
`53E45C0C4D7B6EB7942BB7438A90F29BC422893E3E6259F97A8A4C33EF74AB39`；输出目录
`artifacts/evaluation/terminal-continuation-v25/e7-freshness-bound-900/` 中 manifest 的
SHA-256 为 `6C854C43E932FF4CC010B099A44ACED2CF62749C3A86D8405F27C6CFB6271CA4`，report 的
SHA-256 为 `E192B4E7BED44B2B13660F58A019CF2CCAE572DB12D8C2F2012E4FCC67AD6B84`。
这份绑定包含成对的 `max_age_s=900`/`report_timestamp_epoch_s`，schema 错误数为 `0`；
`hardware_link_ready=false` 与 `gate_passed=false` 原样保留，不能把离线网卡或 report-only
退出码当成 REAL/HIL 通过。配套全量回归收集 `2625` 项，结果为 `2624 passed + 1 skipped`，
日志 `artifacts/evaluation/terminal-continuation-v25/full-regression-final.log` 的 SHA-256
为 `74CCDE8D3C72276B8122DFB03579D1E76CAF98AAD0F5030F6F7AEB7DD3CDC0B7`，ruff/compileall
退出码均为 `0`。同一 v25b 报告绑定到完整 goal-acceptance 后，快照
`artifacts/evaluation/terminal-continuation-v25/goal-acceptance/multidetect-goal-acceptance.json`
的 SHA-256 为 `4514AF0836C0E33117475B3BD1C272F24D94A857B215C253BAFE05CFCE16F5D7`，日志
SHA-256 为 `78342FAA5DDC8B43BBDDDC1EAC986A1F494F693E87AFED36366479CE03A56F59`；
`software_status=pass`、`hardware_status=offline_pending`，不能把该软件快照升级为现场、
HIL/REAL 或最终发布证据。

## 最新 goal-acceptance v24（2026-09-04，`v0.3.0-pre.8`）

当前源码在 REAL 现场链路 freshness 绑定后重新执行的总验收位于
`artifacts/evaluation/terminal-continuation-v24/multidetect-goal-acceptance.json`，46,146
bytes，SHA-256
`5E5820365FD95E8BBC3987E8559A88878922CC3E3CD478D3A60AB6272C53E3C3`。它绑定核心收集
`2624`、QGC custom `66`、协议/QML、软件安全和三机型确定性 SIM 子门；现场报告 freshness
约 `27.2/900 s` 通过，但 `hardware_link_ready=false`、`hardware_status=offline_pending`，
所以不会把该快照升级为现场、HIL/REAL 或最终发布证据。

v24 的现场报告为
`artifacts/evaluation/terminal-continuation-v24/field-link-preflight-live-20260904e.json`，
SHA-256 `2341C97D1146CC33D0734D7947A5A543D05A6D96C7E6C8B0896E1C974EA6B5F2`；release-gate
writer/evaluator 现在同时绑定 `max_age_s` 与 `report_timestamp_epoch_s`，两个 freshness
字段必须成对出现；旧 prerelease 清单保持可读，最终 `v0.3.0` 仍需 freshness-bound REAL 记录。

## 最新 goal-acceptance v23（2026-09-04，`v0.3.0-pre.8`）

当前源码在 freshness 缺失时间戳回归之后重新执行的总验收位于
`artifacts/evaluation/terminal-continuation-v23/multidetect-goal-acceptance.json`，46,003
bytes，SHA-256
`ABB54E58F04C12330E37B3F2574DF73979B8C6224DC8D8F5EDDEF9BDE2D35DEB`。它绑定核心收集
`2623`、QGC custom `66`、协议/QML 和三机型确定性 SIM 子门；现场报告 freshness 通过但
`hardware_link_ready=false`，因此 `hardware_status=offline_pending`，不会把该快照升级为
现场、HIL/REAL 或最终发布证据。

## field-link freshness 证据绑定（2026-09-04，`v0.3.0-pre.8`）

`src/multidetect/field_link_preflight.py` 的 verifier 现在可按调用者提供的
`max_age_s` 检查报告观察时刻：one-shot 读取 `generated_at_epoch_s`，watch 读取
`finished_at_epoch_s`。目标验收 PowerShell 入口默认传入 900 秒窗口，并把时间戳、年龄、
窗口和 freshness 检查结果写入 `field_link_preflight` 节点；过期报告不会因为 hash 仍然
一致而重新获得现场可信度。

本次绑定使用的现场报告位于
`artifacts/evaluation/terminal-continuation-v22/field-link-preflight-live-20260904c.json`，
SHA-256 `0F211E23FD0C2671C8424C276E41993EB7DABB15059A4585A044B000D3F1D682`；离线复核
SHA-256 为 `4E58BF188B0D99D10CBCCFE1DBCA18E0A04B4E655E7BE2541A2D1090F50BFF94`。
对应总验收快照
`artifacts/evaluation/terminal-continuation-v22/multidetect-goal-acceptance.json` 为
46,003 bytes，SHA-256
`B2569EAF162AD9E33266DE25ABC1D61EEFB7A214C9021807A02A80C7078CD8B2`，
`software_status=pass`、核心收集 `2622`、QGC custom `66`，但现场网段仍未接通，
`hardware_status=offline_pending`。

## 最新 goal-acceptance v21（2026-09-04，`v0.3.0-pre.8`）

当前台式机的完整验收快照位于
`artifacts/evaluation/terminal-continuation-v21/multidetect-goal-acceptance.json`，45,234
bytes，SHA-256
`78498FAA088110E49B640BF3BA605B23B133142BC10AAFC6B184E1091F53D089`；配套原始日志
`artifacts/evaluation/terminal-continuation-v21/goal-acceptance-rerun.log` 为 57,303 bytes，
SHA-256 `F23336F60FE289A63DDAE65E9551180A5B594B28EE4924FD2181352D1913292F`。
它把当前源码、版本、核心测试 `2619`、QGC custom `66`、协议/QML 和三机型确定性
3-D SIM 证据绑定在一起，`software_status=pass`；同时保留
`hardware_status=offline_pending`、`field_link_ready=false` 和六项 live gates，
所以不会把离线环境当作现场、HIL/REAL 或最终 `v0.3.0` 证据。

## 三机型 3-D SIM 绑定证据（2026-09-04，`v0.3.0-pre.8`）

`artifacts/evaluation/terminal-continuation-v20/` 保存当前源码重新生成的三份机型记录和
绑定结果 `simulator-3d-control-binding.json`。绑定 SHA-256 为
`967C162D4B996C4CA709B678732A8690FFA18256B6AD958CDCC08EE6CAC61B4E`，
`all_airframes=true`、`all_deterministic_gate_passed=true`、`records_hash_bound=true`。
这层把 `scenario manifest → SDF world/model → stack plan → manual/AI authority` 绑定到
同一身份；外部 PX4/Gazebo 进程、GPU 视频和人工操作仍是后续独立门，不因该绑定而自动通过。

## 最新 goal-acceptance 快照（2026-09-04，`v0.3.0-pre.8`）

在构建 wrapper 修复后重新执行的 canonical 总验收位于
`artifacts/evaluation/terminal-continuation-v19/multidetect-goal-acceptance.json`，45,234
bytes，SHA-256
`5C6E0618F0C25A04A52832F7B00CB5113EF21BC3C2C1375F658899A8B2A04066`。它确认
`software_status=pass`、核心收集 `2619`、QGC custom `66`，同时保留
`hardware_status=offline_pending`、`field_link_ready=false` 和六项现场/REPLAY live gates。
该快照是软件/证据绑定更新，不改变硬件、HIL/REAL 或最终版本发布门。

## Windows build wrapper 哈希收口（2026-09-04，`v0.3.0-pre.8`）

`scripts/build_multidetect_windows.ps1` 用 .NET `SHA256::Create()` 流式计算 Release EXE 指纹，
避免构建收尾依赖 PowerShell 可选哈希模块。Windows PowerShell 5.1 的完整构建/安装 portable
运行返回 `0`；日志
`artifacts/evaluation/terminal-continuation-v18/build-wrapper-after-sha256-fix.log` 为 142,536
bytes、SHA-256 `F28147F8FEBEF709936A31BEFD88EA7B6243AB294A00C1CF447E9AFBD2752E23`。Release
和 canonical portable `MultiDetectGCS.exe` 均为 46,575,616 bytes、SHA-256
`4D7A024F80157E5EB8671B77D6E1817FB960ECE8A8A97D0F470A222E7F7C8E40`，字节一致；portable
RTSP helper 为 95,744 bytes、SHA-256 `DE94D5404BEF48B80919BFC06465BD53EBFD50A8B65A50F38DA3BCDDADB58312`。
构建后的 portable `--offline --simple-boot-test` 退出码为 `0`，日志
`artifacts/evaluation/terminal-continuation-v18/qgc-simple-boot-after-sha256-fix.log` 为 220
bytes、SHA-256 `946D08771ACF7DE07C43218D395CC514F16CDB253A34C9AC5D4601F70299BEA9`。
该层只修复构建 wrapper 的跨宿主稳定性，不改写任何硬件/REAL 发布门。

## goal-acceptance wrapper 的 StrictMode 兼容性（2026-09-04）

主验收入口 `scripts/run_goal_acceptance.ps1` 现在通过 `Get-NativeExitCode` 读取所有原生
命令退出码，覆盖 Python/Git、QGC 测试收集、SIM/HIL helper 和三机型 3-D 控制检查。这样在
迁移后的 Windows 主机、嵌套 `powershell.exe -File` 或无输出命令场景中，不会因为
`$LASTEXITCODE` 自动变量缺失而提前中止；真实非零退出码仍会 fail-closed。该层与
`run_current_release_gate_pre8.ps1`、`run_field_link_preflight.ps1` 保持同一读取约定，
只改善脚本稳定性，不改变硬件/现场/最终版本门。

## 现场链路 wrapper 的 PowerShell 兼容性（2026-09-04）

`scripts/run_field_link_preflight.ps1` 对原生 Python 预检、验证器和
`prepare_field_link.ps1 -VerifyOnly` 统一采用 `Get-Variable -Name LASTEXITCODE
-ErrorAction SilentlyContinue` 读取退出码，并在宿主没有创建自动变量时使用 `0`。这避免
`Set-StrictMode` 因空输出管道中止，同时保持诊断失败的 `hardware_link_ready=false` 和
`-RequireReady` 的严格行为不变。冒烟记录
`artifacts/evaluation/terminal-continuation-v18/field-link-wrapper-smoke-20260904b.json`
（SHA-256 `C437051B274C76ED4732FD0019FF20EC67FCCA16824900D9609193E1891CA0C`）验证了
wrapper/verifier 的完整只读流程；它不构成硬件链路或 REAL 证据。
对应脚本/wrapper/field-preflight 回归为 `51 passed`，日志
`artifacts/evaluation/terminal-continuation-v18/field-link-wrapper-regression-20260904.log`
（SHA-256 `5ED87FCDFDBE91521B726EDEBBC270047EA6F0DE4FA104749D72A3D0F5DEDBEF`）。随后完整
Python 回归为 `2616 passed / 1 skipped`（共收集 2617，退出码 `0`），日志
`artifacts/evaluation/terminal-continuation-v18/pytest-full-20260904-after-field-wrapper.log`
（SHA-256 `29AD92466E928C6DEC1CFBF1EC4D58C14AFF4190A74C11D1D2C976D372ABC5C0`）。

显式 `Ethernet` 的现场链路 live watch 也已在本轮重跑 3 个样本：
`artifacts/evaluation/terminal-continuation-v18/field-link-preflight-live-20260904b.json`
（16,807 bytes，SHA-256 `AC62A886C3723A5209D9C4F441537C4D7D82B0AC530DF90AE52D340F02B7238A`）。
wrapper/verifier 均退出 `0`，但 Jetson/GR01/RTSP 三次均超时，路由源 `192.168.1.13` 不在
期望 `192.168.144.0/24`，本地 `14580/14581` 可绑定，报告保持
`all_passed=false`、`hardware_link_ready=false`；原始日志 SHA-256 为
`362095D6D841613B338CF8268445A3C3FCB3D596152261FDFCD10B54D4475C92`。Auto-select 在没有
期望网段活动地址时退出 `1` 且不写假报告，这是 fail-closed 路径，不改变现场/HIL/REAL 门。

## 目标

`Multi-Detect` 是唯一的工作仓库。机载感知/控制代码与 MultiDetectGCS 源码在同一 Git 根目录中维护，
但仍通过目录边界保持 Python/Jetson 和 Qt/C++/QML 的独立构建工具链。当前台式机的实际开发根目录是
`D:\Projects\Multi-Detect`；当前源码版本为 `0.3.0-pre.8`，Python 为 `0.3.0rc8`，CMake/PE numeric
为 `0.3.0`。

当前质量收口（2026-09-04）已把 `NavigationFusionConfig` 的 GPS 速度源最低 fix/卫星数
接到 `mavlink_gps.py` 的 canonical 常量；只允许收紧，不允许用 2-D 或未知卫星数进入户外导航
观测。速度路径的 `0.75 s` age 窗口仍比目标落图 `1.5 s` 坐标窗口严格。相机标定与户外
深度标定文档现在也使用同一套严格 JSON 解析器：接受 UTF-8 BOM，拒绝重复键及 NaN/Infinity，
避免把损坏或含糊的标定样本带入实际距离算法。GNSS→相机杆臂也已接入测距/WGS-84 落图：
body-NED 安装平移按姿态旋转到 NED，光心高度求交点，且杆臂 sigma 进入距离/方位误差预算；
缺省零值保持旧标定兼容。最新 parity 文件
SHA-256 为 `BFE773CE40773A9AFA651E29FC2BA80FB1119B6B2519424A7A2A2348302BED24`，
`core_algorithm_hash=994ae1dffb3856eac5d71b479a7377f3f1b1c5beda2d154d6c2ee3b733c15af3`；
杆臂校正后的当前全量 Python 回归共收集 `2617` 项，结果为 `2616 passed / 1 skipped / 0 failed`，
日志 `artifacts/evaluation/terminal-continuation-v14/pytest-full-20260904-lever-arm-summary.log`
（3,536 bytes，SHA-256
`0194FF54B0A24456B1CF045698602C428F0D4FDE3DD4AB1AB7D9B63773329B1C`）。E7 的 external SIM
绑定状态见下方“杆臂校正后的 parity/E7 重绑”小节；历史清单仍保留为
`artifacts/evaluation/release-gate-pre.8-current-20260903T084500Z-v6-bound.json`
（SHA-256 `818A77FAB6F7C5AC6735726438CB16E447D16A7058A7C17E7611E8CC4438ED9D`），对应
最新 report-only 复核为
`artifacts/evaluation/release-gate-pre.8-current-20260903T103906Z-v7-alert-link-bound-report.json`
（47,716 bytes，SHA-256 `BD79B0B83FB46C2A531327B273AC5E40A8566FBF52A9F0D49780D497FEA4FF64`）。报告明确
`gate_passed=false` / `release_allowed=false`，不改变现场/HIL/REAL/最终版本门；源清单仍为上述
`084500Z-v6-bound.json`，保持历史输入可追溯。

### 2026-09-04 杆臂校正后的 parity/E7 重绑（当前 pre.8）

GNSS→相机杆臂校正进入 canonical `core_algorithm` 后，旧的六源 external SIM join
`artifacts/evaluation/simulator-external-runtime-ai-evidence-20260903-current-provider-fallback-parity.json`
（SHA-256 `EF0631BF43B11DF6AC170C2F2A6A68C7CFF26AF800FD16260DC861C2EDF07557`）仍绑定旧
parity `0AFF3DF8913E8C99C8495FAD743F1E85D68716DE86719E1D91BA628F47343DF7`。当前 parity
重算输出 `artifacts/evaluation/terminal-continuation-v15/external-join-revalidation-current-parity.json`
（SHA-256 `5F874F6BDE9FBD62F17226465D46C2242C786EC3020B0C342E29B2793430E283`）为
`gate_passed=false`，诊断为 `join: stored summary does not match source recomputation`。
严格 E7 wrapper 在 manifest 写入前因 parity mismatch 退出 `1`，日志
`artifacts/evaluation/terminal-continuation-v15/e7-rebind-attempt-20260904T023000Z.log`
（SHA-256 `F3DF0617A033997169741B4F4BFBBBB9076FB042393BD7F556343210BC6D7721`）；该目录没有
新的 manifest/report。当时的 PATH-only 探测未找到 `docker.exe`；后续已从 Docker Desktop
运行时目录直接调用 CLI，完成当前 parity 下的六源重绑，见下节。旧诊断不代表当前 Docker
状态，也不改变 E7/现场/HIL/REAL/最终版本门。

### 2026-09-04 外部 PX4/Gazebo + live-AI 六源重绑（当前 pre.8）

Docker Desktop 4.46.0 的 CLI 位于
`C:\Program Files\Docker\Docker\resources\bin\docker.exe`，固定镜像为
`px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`。
当前 parity 下重新执行了 fixed-wing、VTOL、multirotor 三份 runtime 和三份 live-AI source；
六份 source 的 `gate_passed` 均为 `true`，runtime 的 PX4/GPS/loopback/只读挂载/cleanup 通过，
AI 的 arm/disarm、AI policy、target TRACK 与 cleanup 通过，且均保持 truth-free、硬件
`not_claimed`。

join 证据为
`artifacts/evaluation/terminal-continuation-v17/simulator-external-runtime-ai-evidence-20260904-current-pre8.json`
（5,287 bytes，SHA-256
`E0B2BA71345D6C046CA9EEFAA42DA45325B8712069CB2000DDAEEF3E7B3D858C`）；其 external runtime
和 AI target policy 子门均为通过，独立重验
`artifacts/evaluation/terminal-continuation-v17/external-join-revalidation-current-parity.json`
（565 bytes，SHA-256
`0BEF11653C13933E10F27E6A63F0DB3A1E89DCBC4438F53FE56CEF1EE58FD688`）重新计算 source 后
`source_recomputed=true`、`gate_passed=true`、`reasons=[]`。

随后以该 join 运行严格 E7 wrapper，生成的 manifest/report/strict report 位于
`artifacts/evaluation/terminal-continuation-v17/e7-rebind/`。E7 报告已接受新 external
join，但总门仍 `gate_passed=false`、`release_allowed=false`，保留 REPLAY accuracy、
现场 field-link、HIL/REAL 硬件/真实控制、人工 Fly View、prerelease channel 等 31 项阻断。
这组证据只关闭 software-only external SIM/runtime/AI 子门，不替代真实飞行验收。

随后把现场预检显式绑定到新的
`artifacts/evaluation/terminal-continuation-v17/e7-rebind-fieldlink/`（不覆盖前一份 E7）：
manifest `release-gate-pre.8-current-20260903T184551Z-v6-bound.json`（17,774 bytes，SHA-256
`E8841054FB71BD5FC0374ED6C12A5E3485444C161869AE627C29673CC1506A0C`）、report-only
`release-gate-pre.8-current-20260903T184551Z-v6-bound-report.json`（46,662 bytes，SHA-256
`9E40DC8A46E7235504A244D8B00697B0D0D2C13AD5A6E986EDC3ECFB19F1B3D9`）和 strict report
`release-gate-pre.8-current-20260903T184551Z-v6-bound-strict.json`（46,662 bytes，SHA-256
`0840AD72023A877BB258B2A7975ACC8002DF1A5C0F89502CE972A307961EC92D`）。严格 wrapper 退出码
`2`，总门仍 fail-closed；`real_field_link` 已绑定该 `hardware_link_ready=false` 现场状态。

本轮还保存现场链路只读预检
`artifacts/evaluation/terminal-continuation-v17/field-link-preflight-current-20260904.json`
（3,434 bytes，SHA-256
`C64890B38EC040FD365C8AB9D6BA3D31620F7442F256A7BF812CBE803009039F`）：Jetson SSH
`192.168.144.20:22`、GR01 MAVLink `192.168.144.11:5760` 与相机 RTSP
`192.168.144.108:554` 均超时，本机 metadata/QGC UDP `14580/14581` 可绑定，
`hardware_link_ready=false`。这是只读网络状态证据，不是硬件验收。

为排除迁移后地址变化，另保存有界只读发现
`artifacts/evaluation/terminal-continuation-v18/field-link-discovery-192.168.1-20260904.json`
（8,525 bytes，SHA-256
`79D3AA8F6E873D7C682DF3C719E941F25E5172EBEBC517BA01410651B0B89A92`）：扫描
`192.168.1.0/24` 的 254 个地址，5 个 SSH 响应，MAVLink `5760`/RTSP `554` 均无命中。
它是诊断证据，不自动选择端点或连接未知主机。

随后运行完整 goal-acceptance，所有新输出写入
`artifacts/evaluation/terminal-continuation-v18/`。主快照
`multidetect-goal-acceptance-20260904-current-pre8.json` 为 60,087 bytes，SHA-256
`CB6A884DFA76E52DCB8EB34F8EF678C58EA137361C31D08FDF01F3D991306507`，脚本退出码 `0`；
核心测试收集 `2617`、QGC 自定义测试 `66`、QGC protocol self-test/QML parse 均通过，
六源 external gate 已绑定且通过。快照仍记录 `software_status=pass`、
`hardware_status=offline_pending`、REPLAY readiness 未通过和现场链路未就绪；六项剩余 live gate
保持为 deployment replay accuracy、现场 ChArUco/舵面/取消/失锁、真实 DET→TRK→LCK→TGT、
Jetson soak 与三场景人工 Fly View 验收。

### 2026-09-04 E7 wrapper 健壮性修复与绑定刷新（当前 pre.8）

`scripts/run_current_release_gate_pre8.ps1` 的 `Invoke-Python` 已修复严格模式下的退出状态读取：
调用原生解释器后通过 `Get-Variable -Name LASTEXITCODE` 获取状态；变量未被主机创建时使用确定性
`0`，避免在 manifest 写入前抛出未定义变量错误。修复只作用于 wrapper 的状态采集，所有发布门
条件保持不变；wrapper 与 goal-acceptance 接线回归共 `15 passed`，完整 E7 专项集合共
`56 passed`。原始日志 `artifacts/evaluation/terminal-continuation-v18/e7-wrapper-regression-20260904.log`
为 618 bytes，SHA-256 `762CEA25A1B6F793DECC8B945CEF2FE81D7EB780B44CD689FB57CE3FAA594C12`。

基于当前六源 external join、v18 field-link 预检、replay-set、三机型 3-D control、SIM/actuator
safety 及 PX4 Mission transaction 输入，E7 重新绑定到独立目录
`artifacts/evaluation/terminal-continuation-v18/e7-rebind-current-v5/`。manifest
`release-gate-pre.8-current-20260903T190334Z-v6-bound.json` 为 17,702 bytes，SHA-256
`1C88F8C7EC0F087126C3B274C17CFF23412969C213A158BA261DB6B8EB498CC8`；report-only 为
46,713 bytes，SHA-256 `04D04E9B822935DB0150F7AC0CE93BAE209E5C7D0674B24244B6E173DA9ECDFA`；
strict report 为 46,713 bytes，SHA-256
`2F9A7CD22BEA34C12832316759A8147BAC08E7F5E9F6415B4DF99A4ED0C394EF`，严格 evaluator 退出 `2`。
报告已接受 `simulator_external_evidence`（`passed=true`），但总门仍
`gate_passed=false`、`release_allowed=false`，保留 31 项 REPLAY accuracy/deployment、现场链路、
人工 Fly View、HIL/REAL 硬件/真实控制及 prerelease channel 阻断；`hardware_link_ready=false`。

### 2026-09-04 三机型确定性 SIM 场景矩阵（当前 pre.8）

当前源码运行 `scripts/run_simulator_scenario_matrix.py --scenario-directory configs/scenarios
--frames 180 --dt 0.05 --require-fault-coverage --cover-faults`，执行 fixed-wing、VTOL、
multirotor 三个仓库自有场景。AI/arm/重复运行和故障覆盖均开启；故障覆盖自动将每个场景
延长到 `5221` 帧，三份重复 evidence hash 一致，所有声明故障均触发，矩阵
`gate_passed=true`、`deterministic_gate_passed=true`、`repeatability_checked=true`。
证据 `artifacts/evaluation/terminal-continuation-v16/simulator-scenario-matrix-20260904-current-pre8.json`
（13,302 bytes，SHA-256 `6F39876CE26EFF7B8E4DCFF9E4E083E3A450441BA97F63439B62392BFB29A2FF`）经
`scripts/verify_simulator_scenario_matrix.py` 独立验证退出码 `0`。该记录是 truth-free、
进程内 deterministic SIM 证据；external 3-D runtime、QGC 人工视觉、硬件和 REAL 仍为
`not_claimed`，不会替代 Docker/PX4/Gazebo 或重开 E7。

### 2026-09-04 外部 PX4/Gazebo + 原生 QGC 组合 SIM/Mission（当前 pre.8）

首轮 300 帧组合运行在 post-adoption type-24 sequence `300` 处严格超时，失败 evidence
`artifacts/evaluation/combined-sim-qgc-20260903T164006Z-39412/combined-evidence.json` 保留为
历史 provenance。按脚本的帧余量要求提升到 900 帧后，当前台式机成功 source 为
`artifacts/evaluation/combined-sim-qgc-20260903T164443Z-4900/combined-evidence.json`
（249,113 bytes，SHA-256
`0DC4044DB459888D4DC1648CC8266B545FBEF86D346FF45E8199C75909911762`，`gate_passed=true`）。
独立 `verify_combined_sim_evidence.py` 复核为
`.../combined-evidence-verification.json`（1,956 bytes，SHA-256
`A31188E69D6EAA5A8752CA11CEF06D2CFE63AAB8601A3ABCEC2F980B536CF0CF`，40/40 checks，退出码 `0`）。
该 source 的专属 sim release-mode envelope 为
`artifacts/evaluation/terminal-continuation-v10/release-mode-sim-combined-qgc-20260903T164443Z-4900.json`
（542 bytes，SHA-256 `80AB0572968BE5992E523BDD8ED2E402D996FAA772A9ED70AB0974769FF1EB52`）；带
`--require-release-mode` 的复核输出
`.../combined-evidence-verification-release-mode.json`（2,596 bytes，SHA-256
`BDD55C221332DDB27599433AF09B58F078C26CC95399EA88B97D84EC5D1489F9`）仍为 40/40 checks 通过。
随后以该 source/envelope 生成独立 E7 目录
`artifacts/evaluation/terminal-continuation-v10/e7-combined-qgc-rerun/`：manifest SHA-256
`D2A4FE349CB45D0AC872ACA589532B6A3E2586EB1196856791647BE708FA9207`、report-only SHA-256
`DA99390FE22DFFD8D3280DCAA94B3A5B89E5755BAB23DA2C0A661E14A4D71B18`、strict SHA-256
`D71E6AB91BEEE570A5F321ED3A9309D9AADCE9433B197D2E0CE9CC03A023E3D5`；清单重新计算的
`simulator_combined.passed=true`，但其他 E7 阻断和 prerelease channel 仍让总门
`gate_passed=false`/`release_allowed=false`，strict 退出码为 `2`。

该 source 绑定同一 portable QGC、仓库场景和固定 digest Docker PX4/Gazebo；camera/CameraInfo/NavSat
稳定检查 `2/2`，9 个 loopback 端口唯一且全部释放，保护 `14550` 未改变，容器启动/停止/移除
完整。RTSP/type-23/type-24 各 900 帧，Mission-ready 后序列 `569→574→579`；VTOL
`MC→FW→MC`、7 项 INT-only Mission readback/reached、AUTO.MISSION、land/disarm 均通过。
它只关闭 software-only Combined SIM/QGC/Mission machine 子门；QGC 原生像素/传输绑定机器证明
通过，但 `operator_visual_acceptance=not_claimed`，不替代人工 Fly View、真实 Pixhawk/Jetson/
相机、室外 GNSS、执行器、HIL/REAL 或最终 `v0.3.0`。

### 2026-09-03 RTSP 多客户端扇出（当前 pre.8）

`ground-control/MultiDetectGCS/custom/tools/MultiDetectReplayRtspPublisher.cc` 对默认单客户端
使用非共享 `GstRTSPMediaFactory`，维护活动 `appsrc` generation 集合；每个已建立媒体管线
收到同一校验帧的独立 `GstBuffer` 引用，重连重试只覆盖失败或新 generation。`--minimum-client-count
1..64` 把 `frame_accepted` 的客户端下限写进 helper 契约；当下限大于 1 时切换到 GStreamer
shared media，由同一编码管线原生扇出到并发 RTSP 客户端，避免多个非共享 `DESCRIBE` 串行阻塞；
默认单客户端仍保留非共享代际隔离。caps 只声明正的 `30/1` nominal rate 供 RTSP SDP 协商，
回放 `captured_at_ns` 仍独占 GST PTS/DTS，不改变真实 cadence。

便携 helper 已用 Release 重建，二者均为 `95,744` bytes、SHA-256
`DE94D5404BEF48B80919BFC06465BD53EBFD50A8B65A50F38DA3BCDDADB58312`。原始并发客户端验收
脚本为 `scripts/verify_replay_rtsp_multiclient.py`，当前便携运行证据
`artifacts/evaluation/replay-rtsp-multiclient-20260903-pre.8-v9-portable-4clients-shared.json`
（3,299 bytes，SHA-256 `994818A9FE96AD5D1BF1C02286E8A1FF67181F63F203C89B7FB6CE75076308B2`）记录四个
客户端完成 `OPTIONS → DESCRIBE → SETUP → PLAY`、H.264/正帧率 SDP、RTPv2/PT96 包增量、两个
稳态 fan-out、EOS 与 summary，`gate_passed=true`。它属于 `software_only_local_rtsp`，不关闭真实
相机/Jetson、室外 GNSS、Pixhawk、HIL/REAL 或人工 Fly View 发布门；此前 v4 双客户端文件仍保留。

ONNX Runtime provider 由 `src/multidetect/inference_provider.py` 统一选择：默认策略按
`TensorRT → CUDA → CPU` 排序，并只在默认 session 构造失败时尝试后缀 provider；显式列表
保持严格，不会静默回退。vision、metric depth、appearance/vehicle ReID 与 semantic
environment 共用该选择器，`model-check`/`jetson-bench` 的证据会保留每次尝试和最终 provider。

2026-09-03 的 SIM evidence 迁移收口把 runner 的 `world_origin.world_path`、矩阵 `manifest` 和
`scenario_directory` 改成仓库根相对 POSIX 路径；绝对路径只在运行时解析本地资产时使用。当前
便携矩阵 `artifacts/evaluation/simulator-scenario-matrix-20260903-portable-paths-pre8.json`
（文件 SHA-256 `06C8E829CCA2321C5D10F2F700E04D2469772E0C9C6E7711738B160DBC110FC1`，canonical
SHA-256 `CE87333B43B3D6E5964C031E27AE57EA3F682787EB54FE2F202DCD5FAD1FF95E`）在等价 checkout
回归中保持单场景 evidence hash 与顶层矩阵 hash 不变。该矩阵已绑定到
`release-gate-pre.8-current-20260903T084500Z-v6-bound.json`（SHA-256
`818A77FAB6F7C5AC6735726438CB16E447D16A7058A7C17E7611E8CC4438ED9D`）及报告
`F97E634AACE4F8E87D01B6880D16DA2A18689CA87B73E77299BC97DF71CA30CA`；只关闭 SIM software
子门，不提升现场、室外 GNSS、人工 Fly View、HIL/REAL 或最终 `v0.3.0`。
路径收口子门当时的全量 Python pytest 计数为 `2577` 项；结果为 `2576 passed / 1 skipped`，
属于该历史 acceptance 快照，不是当前 provider 回退回归的测试计数。

下面的 `bearing-uncertainty`、`navigation-gps-floor`、旧 parity 和旧 E7 文件名均是历史
provenance；当前状态以本节开头的深度/相机严格解析 parity、当前 E7 报告和当前 external
证据为准。

前一轮测距核心已把视线几何 one-sigma 传播纳入 `ground_range_ci95_m`，并在 near-horizon
不确定性跨界时 fail-closed；时间对齐、姿态耦合方位和 WGS-84 弧长投影的 bearing 快照仍作为
历史 provenance 保留。当前导航质量收口、标定解析、parity 与软件验收见本节开头的当前记录。

上一轮 bearing-uncertainty 复核使用的 parity（历史 provenance）为 `configs/release-parity.json`
SHA-256 `CCDCA6FA7FBE5512200B9A4DCE40F7303E6591ED43BDC33D009AE93075441C2D`，其中
`core_algorithm_hash=338981686f2142144f438ce34b4f91a18f6cab76b9f3d8efd238ac1e3fd525e7`；
当时 canonical parity 已更新为当时记录中的
`0AFF3DF8913E8C99C8495FAD743F1E85D68716DE86719E1D91BA628F47343DF7` /
`434b86c6a8b677e60a4ae5766586b603855609e43cc0521695aa1e21995963f9`。历史 hash 仍保留，
不能回写为当前状态。此前历史版本的 `safety_policy_hash=f781b90021938e5b9d45056eb48c6367599401ee65f2f8f0279f0aaa03bab2d3`。
这份历史 hash 已包含 SIM AI 的 canonical GNSS hAcc/vAcc/age fail-closed 门、共享
WGS-84 SIM geodetic endpoint 和 QGC `MANUAL_CONTROL` freshness fail-safe 门。上一轮严格总验收为
`artifacts/evaluation/multidetect-goal-acceptance-20260903-bearing-uncertainty.json`
（SHA-256 `04AFC8C6C61E340020FB7E89483D00111B00C8D61B799289EEDF7F2B4B2BF459`，核心测试收集
`2536`，QGC custom `66`，`software_status=pass`，`gate_overridden=false`）；该快照覆盖时间对齐、
姿态耦合方位不确定度与 WGS-84 弧长投影；此前视线不确定度收口的
`multidetect-goal-acceptance-20260903-partial-velocity-fail-closed.json`
以及此前 19,357-byte 同名快照作为独立历史 provenance 保留。E7
仍为 `release_allowed=false`，因为现场链路、室外 GNSS、HIL/REAL 和人工 Fly View 还没有关闭。

本次 bearing-uncertainty 复核的 E7 清单为
`artifacts/evaluation/release-gate-pre.8-current-20260903T024500Z-v6-bound.json`
（SHA-256 `3D379F24C67F438A287D5F714CD4AEE79C6877DE67B6691EE49AF2D012258BEC`），
report-only SHA-256 为
`C8749E0D7B5AB8C976477CF244586E734DFCEEBD5C07A8047689F62D5BCE040C`；它明确保留
`gate_passed=false` / `release_allowed=false`，没有把桌面软件验收伪装成室外或实机证据。

此前 watchdog v2 的软件验收快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260902-manual-watchdog-v2.json`（核心测试
`2431`、QGC custom `66/66`）。三机型 external join 与独立 revalidation 均通过当时 parity
重算；E7 v2 清单仍保持 `gate_passed=false`，因为这份仓库架构不会把 software/SIM provenance
提升为真实 Jetson、室外 GNSS、Pixhawk、HIL/REAL 或人工视觉证据。

2026-09-03 的 external runtime/live-AI 六源绑定是旧 parity 的历史 provenance：
`artifacts/evaluation/simulator-external-runtime-ai-evidence-20260903-current-provider-fallback-parity.json`
（5,365 bytes，SHA-256
`EF0631BF43B11DF6AC170C2F2A6A68C7CFF26AF800FD16260DC861C2EDF07557`）和独立
revalidation `artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260903-current-provider-fallback-parity.json`
（558 bytes，SHA-256
`EE0B97095738CEBB37BC3AC6435385D22B51FBD63FB4EE6645662392A4D2CC95`）均为
当时 `source_recomputed=true` / `gate_passed=true`，六个 source 路径互不相同并绑定
`configs/release-parity.json` SHA-256
`0AFF3DF8913E8C99C8495FAD743F1E85D68716DE86719E1D91BA628F47343DF7`。这组 Docker/PX4/Gazebo
结果只代表 software-only SIM provenance，不提升真实 Jetson、室外 GNSS、Pixhawk/执行器、
HIL/REAL 或人工 Fly View 状态。

当时的 E7 清单还把该六源 provenance 作为顶层
`simulator_external_evidence` 绑定：join 与独立 revalidation 的相对路径、SHA-256、字节数、
六个 runtime/AI source hash、五组 parity hash 和 13 项 fail-closed check 会在写入和读取时
重新计算。当时清单为
`artifacts/evaluation/release-gate-pre.8-current-20260903T084500Z-v6-bound.json`
（SHA-256 `818A77FAB6F7C5AC6735726438CB16E447D16A7058A7C17E7611E8CC4438ED9D`）；它的
`simulator_external_evidence.gate_passed=true` 当时只代表三机型 software-only SIM source
完整且未漂移，最终 release 仍需现场、Replay accuracy、HIL/REAL 和人工 Fly View 门。

现场会话验证使用单一目标身份门：当 GPS/RANGE 样本携带 `target_id` 时，两类来源的 ID 集合必须完全一致且唯一；部分交集或多目标集合会关闭现场 evidence gate，并在 `target_binding.cross_record_mismatches` / `multiple_target_ids` 中保留诊断。

```text
Multi-Detect/
├── src/multidetect/
│   ├── autonomy/                    # 任务契约、确定性规划、安全与任务执行核心
│   │   ├── planner.py               # 四类任务共用的 PlatformNeutralPlan 与能源/资源门
│   │   └── planner_cli.py           # 严格 task/context JSON -> plan/estimate CLI
│   ├── mission_planning.py          # Planner -> canonical PX4 compiler typed seam
│   ├── mission_compile_cli.py       # 严格 task/context/runtime JSON -> PX4 artifact CLI
│   ├── mission_progress.py          # ExecutableMission -> fresh telemetry 的只读进度观察器
│   ├── backends/
│   │   ├── replay.py                # 当前 post-detection ObservationReplayBackend
│   │   ├── raw_replay.py            # raw-only recording + ManualClock Replay backend
│   │   └── sensors.py               # pre-detection RawSensorRuntimeBackend 契约
│   ├── simulation/                  # Scenario Manifest、场景加载与 evaluator 边界
│   ├── simulator/                   # 3-D SIM 编排、控制 authority、PX4/Gazebo 与 readiness
│   │   ├── runtime_preflight.py     # native PX4/Gazebo、Docker daemon、WSL 只读预检
│   │   ├── scenario_runner.py       # 单场景 deterministic runner 与可选 Mission 进度绑定
│   │   ├── closed_loop.py           # 同核 authority/adapter/flight model 与 Mission 进度快照
│   │   ├── scene_state.py            # hash-bound truth-free 3-D 场景快照/回放流
│   │   ├── scene_stream_set.py       # fixed-wing/multirotor/VTOL 三流集合与 E7 binding verifier
│   │   └── scenario_matrix.py       # 三机型发现、重复 hash 与 truth-free 聚合门
│   ├── clock.py                     # Real/Sim/Replay 可注入时钟
│   ├── integrated_replay.py         # raw backend + production perception + Live core E2
│   ├── perception_factory.py        # live-camera/integrated-replay 共用感知图
│   ├── inference_provider.py        # ONNX Runtime TensorRT→CUDA→CPU 顺序与有界构造回退
│   ├── model_manifest.py             # fire/smoke 候选模型 manifest、artifact hash 与输出契约
│   ├── model_review.py               # 模型审核 envelope、四类治理复核与 production eligibility
│   ├── alerts.py                     # JSONL/SQLite 发件箱与 HMAC-SHA256 UDP 关联 ACK transport
│   ├── alert_link_bench.py           # 有界告警 ACK round-trip/丢包/P95 证据（回环与现场分域）
│   ├── camera_calibration.py         # 严格 ChArUco 内参/安装姿态/导航到光心杆臂标定
│   ├── depth_calibration.py         # affine v1 / 单调 piecewise v2 室外实距标定
│   ├── geolocation_accuracy.py      # 离线 WGS-84 目标坐标真值误差/P95/σ 覆盖率
│   ├── geolocation_survey.py        # 已验证 target-event replay → 独立真值严格配对
│   ├── metric_depth.py              # 米制深度推理、曲线映射和不确定度传播
│   ├── multimodal_ranging.py        # 姿态/高度/测距/杆臂融合与视线不确定度传播
│   ├── target_event_replay.py       # 视频/深度 + GPS/测距的 hash-bound 只读事件回放 bundle
│   ├── mission_journal.py            # PX4 Mission hash-chain JSONL checkpoint/掉电恢复边界
│   ├── mission_progress.py           # WGS-84/GPS/link 与 PX4 feedback 共同约束的只读 Mission 进度
│   ├── px4_mission_feedback.py       # hash/source/time-bound MISSION_CURRENT/ITEM_REACHED 证据
│   ├── px4_mission_feedback_cli.py   # v1 历史诊断 / v2 签名运行身份现场采集与独立重验
│   ├── px4_mission_feedback_runtime.py # profile/heartbeat/endpoint/airframe/output-lock 会话绑定
│   ├── px4_mission_feedback_binding.py # HIL/REAL × 三机型 Mission/profile/report 的 E7 六源绑定
│   ├── field_covariance_campaign.py # 六叶 covariance 现场顺序、静态/在线预检、采集与聚合入口
│   ├── online_covariance_preflight.py # 5 分钟时效、source/parity-bound 的在线探测 observation/verifier
│   ├── field_covariance_workspace.py # 六叶静态 source 的 hash-bound 相对路径导出/搬迁复验
│   ├── field_covariance_session.py # receipt-bound 单叶 probe/capture/session seal 与事后复验
│   ├── replay_set.py                 # 六类部署域分层回放集 manifest、帧对齐与哈希校验
│   ├── replay_set_evaluation.py      # 六分层 IoU/延迟精度报告、独立复核与 E7 哈希绑定
│   ├── replay_set_readiness.py        # 采集阶段逐层 readiness、正例分母与精度报告重算
│   ├── replay_set_calibration.py     # 哈希绑定预测的类别阈值扫描与六分层稳定性报告
│   ├── replay_set_fixture.py          # synthetic_demo 正例 evaluator fixture；永不充当部署域证据
│   ├── video_replay_recording.py     # 录制视频 → hash-bound raw replay（GPS 明确未知）
│   ├── replay_telemetry_join.py      # 独立 GNSS/raw telemetry sidecar 与录像帧的严格时间轴合并
│   ├── gnss_capture_sidecar.py       # raw GNSS capture → 完整 telemetry sidecar（未知字段不猜测）
│   ├── field_link_preflight.py      # Jetson/GR01/RTSP/metadata 现场链路预检、网卡诊断 hash 绑定与报告复核
│   ├── field_session_acceptance.py  # DET/TRK/LCK/TGT + GPS/RANGE 现场会话离线 gate
│   ├── field_session_capture.py     # 只读尾随 AuditLog、筛选会话并调用现场 gate
│   ├── gnss_capture.py               # 只读 GPS_RAW_INT/GLOBAL_POSITION_INT 坐标与质量采集
│   ├── field_evidence_bundle.py      # 六层现场证据 hash-bound 只读 join 与 fail-closed gate
│   ├── release_gate.py               # REPLAY/SIM/HIL/REAL E7 parity、目标态证据与发布门聚合器
│   ├── release_mode_evidence.py     # 单个模式 runner 证据的显式 hash-bound envelope
│   ├── px4_mission.py               # VTOL Mission contract/compiler/transaction
│   ├── px4_gazebo_qgc_router.py     # owned SITL 的 QGC 只读/evaluator 有界路由
│   ├── replay.py                    # 不可变 FrameObservation 记录与内容 hash
│   ├── live.py                      # Jetson 实时运行入口
│   └── ...                          # 感知、跟踪、融合测距、定位、审计和飞控集成
├── configs/
│   ├── product-version.json         # 产品版本单一真源
│   ├── release-parity.json          # 当前源码五组共享契约 hash（由生成器重算）
│   ├── pixhawk-covariance-field-campaign.example.json # HIL/REAL × 三机型六叶现场配置模板
│   ├── calibration/                 # 可追踪的现场标定输入资产（ChArUco 打印板）
│   ├── schemas/                     # 对外 JSON Schema（含 E7/replay/scene/covariance online-preflight）
│   └── scenarios/                   # 可复现实验场景清单
├── scripts/                         # 部署、构建、Replay/SITL/Mission/HIL/现场验收（含 scenario-matrix wrapper、verify_simulator_scene_stream.py、verify_simulator_scene_stream_set.py、create_release_mode_evidence.py、create_release_gate_manifest.py、check_release_gate.py、check_field_evidence_bundle.py、create_replay_set_manifest.py、check_replay_set_readiness.py、create_sim_replay_set_fixture.py、create_replay_set_positive_fixture.py、create_video_replay_recording.py、gnss_capture_to_replay_telemetry.py、join_raw_replay_telemetry.py、evaluate_replay_set.py、calibrate_replay_set.py、run_alert_link_bench.py、verify_alert_link_bench.py、run_field_link_preflight.py、run_field_link_preflight.ps1（只读现场一键入口）、inspect_field_link_interfaces.ps1（迁移后只读网卡诊断）、verify_field_link_preflight.py、prepare_field_link.ps1（含显式 UAC -Elevate）、capture_field_session.py、verify_field_session_capture.py、validate_field_session.py、capture_gnss_samples.py、verify_gnss_capture.py、build_target_geolocation_accuracy.py、recover_docker_desktop_stale_socket.ps1）
├── tests/                           # Python 单元、契约、确定性与集成回归
├── deploy/                          # Jetson/现场部署资料与运行 profile
├── docs/
│   ├── autonomy-architecture.md     # v0.3.0 自主任务与同核模拟器设计
│   ├── repository-architecture.md   # 本文
│   ├── field-bringup.md             # 接线后只读链路/路由/协议预检与六层证据 bundle runbook
│   └── qgc-windows-release.md       # Windows 构建、便携交付与版本规则
├── ground-control/
│   ├── README.md                    # GCS 导入边界与版本规则
│   └── MultiDetectGCS/              # 定制 QGroundControl 源码
│       ├── custom/                  # Multi-Detect QML、协议、控制器和产品配置
│       ├── src/                     # QGroundControl C++ 模块
│       ├── cmake/                   # Qt/CMake/安装器逻辑
│       ├── tools/ 和 test/          # QGC 工具、自检与回归
│       └── .multidetect-upstream.json # 上游基线锚点
├── CHANGELOG.md                     # 产品级变更与 prerelease 状态
└── artifacts/                       # 本机交付物；Git 忽略
```

目录图同时表达目标边界和当前开发方向；某个目录或接口出现在树中不代表 SIM、HIL 或实机验收已经
完成。验收状态以根 README 的运行模式表、CHANGELOG 和对应测试/现场证据为准。

## v0.3.0 分层边界

```text
MultiDetectGCS / Mission Authoring
        |
Autonomy Core / Contracts / Safety
        |                        |
Perception Core            Flight Integration
        \                        /
         RuntimeBackend + Clock
          |      |      |      |
       REPLAY   SIM    HIL    REAL
```

- UI 只维护一套，不创建独立 `FlyViewSim.qml` 或仿真专用操作流程。
- `RuntimeMode` 只描述后端和权限，不得进入 Planner/Safety 成为绕过条件。
- Detector、Tracker、Ranging、Geolocation、Planner、Safety 和 Mission Executive 算法只维护
  一套；运行模式只允许通过 backend/world adapter 替换时钟、只读传感器来源、动力学和故障源。
- 兼容 `RuntimeBackend`/`ReplayBackend` 仍是检测后的 observation-stream 回归边界，显式别名为
  `ObservationRuntimeBackend`/`ObservationReplayBackend`；它不冒充 raw replay。
- `RawSensorRuntimeBackend` 只暴露 `FrameSource`、只读 `TelemetryProvider`、`Clock` 与
  `RuntimeDescriptor`，并由同一个 production `LiveMissionRunner` 注入 detector 和任务核心。
- headless `integrated-replay` 将 raw-only `RawSensorReplayBackend`、`ManualClock`、与
  `live-camera` 共用的 production perception factory 和完整 `LiveMissionRunner` 绑定，并严格
  消费整个已验证录制；当前证据为 headless 软件 `E2`。
- pre.3 已完成真实便携版 QGC RTSP/Fly View + signed operator metadata barrier 的 `E4`；
  随后又完成 pinned fixed-wing PX4 SIH + signed operator metadata 的 software-only 门；
  另完成 digest 固定 Gazebo Harmonic `gz_x500_mono_cam` 的真实 image/CameraInfo/NavSat
  capture 子门，并将持久化 Gazebo pixels 经 shared production perception、LiveMissionRunner
  接入真实 QGC RTSP/签名 metadata 同帧 transport 子门；该机器 transport 证据仍不替代人工
  Fly View/operator acceptance、HIL 或 REAL 验收。历史 pre.4 combined SIM machine gate
  另见下文历史证据段；当前 pre.8 证据见本节后面的权威段。
- 两类 backend 都不暴露 Ground Truth；truth 和 expected result 只属于 Scenario evaluator。
- 任务契约和 canonical hash 跨四种模式保持一致。
- REPLAY、SIM、HIL、REAL 的审计事件、任务状态和 operator 状态使用同一协议。
- 飞控写入必须由独立 profile、端点约束、身份、签名、计划 hash 和操作者授权共同门控。
- PX4 Mission 事务在 `mission_journal.py` 追加 hash-bound、fsync JSONL checkpoint；截断尾巴、
  绑定漂移和跳状态只返回 `recovery_required`，不会自动 ACK/readback/`AUTO.MISSION`。这个
  适配器在四种模式中相同，不把恢复证据提升为硬件门。
- `release_gate.py` 和 `configs/schemas/release-gate-manifest.schema.json` 是 E7 聚合边界：
  它重新计算四模式证据 SHA-256，并比较同 UI、同核心算法、同任务契约、同安全策略和同
  PX4 Mission path 的 parity；它不会把缺失的 HIL/REAL 或人工视觉证据补成通过。现场
  capture report 可通过 `--real-target-state-evidence-report` 写入 hash-bound 目标态记录，
  评估时会重跑严格 `TGT` GPS/RANGE verifier；最终 `v0.3.0` 还要求该记录、route-bound
  REAL 链路和独立目标坐标精度报告同时通过。共享执行器软件 bench 可通过
  `--actuator-safety-evidence` 写入顶层 `actuator_safety`；E7 evaluator 会重哈希并重跑
  八项 neutral/limit/failsafe check，最终 release channel 要求该绑定通过，同时保留
  `software_only=true`/无物理输出边界。
  认证告警 ACK bench 可通过 `--real-alert-link-bench` 写入顶层 `real_alert_link`；装配器和
  evaluator 会重哈希并重跑硬件域、非回环端点、关联/认证 ACK 与阈值检查，loopback
  software baseline 不会满足最终 release channel。
- `simulator/scene_stream_set.py` 与 `configs/schemas/simulator-scene-stream-set.schema.json`
  是 SIM 三机型的集合证据边界：`build_binding()` 固定恰好一条 fixed-wing、一条
  multirotor 和一条 vtol，并为聚合文件与每条源流保留路径、字节数、SHA-256、scenario
  manifest hash、canonical hash、snapshot 数和 truth-free 状态。`release_gate.py` 每次
  读取顶层 `simulator_scene_stream_set` 时都会重新打开聚合文件和三条源流，要求
  `simulator_scene_stream_set_revalidated` 事件及 9 项 checks 全部通过；缺失、重复机型、
  源文件篡改或摘要漂移都会关闭 binding，最终 `release` channel 不能旁路此门。
- `model_review.py` 和 `configs/schemas/model-review-bundle.schema.json` 是候选模型的审核
    边界：它把 fire/smoke manifest、精确模型 artifact SHA-256、model-check、部署域 replay
    accuracy 和 provenance/dataset-rights/security/quality 四类审核组成可重算 envelope；
    `create_release_gate_manifest.py --model-review-bundle` 将其写入顶层 `model_review`，
    `release_gate.py` 每次读取时重新验证。预发布可以记录 pending/synthetic，最终 `release`
    channel 要求 `production_approval_eligible=true` 与 `gate_passed=true`，而
    `production_approved`、`flight_control_enabled`、`physical_release_enabled` 固定为
    `false`。
- `MultiModalRangingEngine.solve_direct()` 是没有瞬时垂向参考时的显式降级路径；它仍复用
  目标射线的向下交会安全门，并拒绝非法评估时间和重复 direct-range source。该路径的
  fail-closed 回归属于软件质量门，不会把 `DEGRADED` 测距升级为 REAL/室外 GNSS 证据。
- `scripts/create_release_gate_manifest.py` 是清单装配入口：REPLAY/SIM/HIL/REAL 证据路径、
  五个 parity hash 以及 operator/hardware 状态全部由调用方显式传入，脚本只做文件存在性、
  JSON 事件/运行模式检查、SHA-256 计算和原子写入；三条 SIM scene stream 通过重复的
  `--simulator-scene-stream-set-evidence` 绑定为一个顶层集合。它与 `check_release_gate.py`
  分离，先装配、再严格评估；goal acceptance、fixture、Docker 单门证据不会被自动升级为
  四模式发布门。
- `release_mode_evidence.py` 与 `scripts/create_release_mode_evidence.py` 位于 runner 证据和
  四模式清单之间的窄适配层：它把一个明确指定的 REPLAY/SIM/HIL/REAL runner JSON 包成
  deterministic、hash-bound envelope，保留源文件路径、事件、运行模式、`gate_passed` 和
  原因。源证据缺少 `gate_passed`、声明为 `false`、缺少运行模式或模式不匹配时，不会被升级；
  缺失/不匹配只有在调用方显式传入 opt-in 开关后才会记录为 projection，且原始 gate 语义仍保持。
  对外数据契约为 `configs/schemas/release-mode-evidence.schema.json`；安装 package 后的
  `multidetect-release-mode-evidence` 与仓库 wrapper 共用同一 `main()`，随后仍必须经过
  `create_release_gate_manifest.py` 与 `check_release_gate.py` 的四模式状态门。
- `scripts/create_release_parity.py` 从显式源码集合生成 `configs/release-parity.json`；它规范化
  换行、拒绝绝对/越界/生成目录路径，并输出五组可审计 hash。该文件是当前源码契约输入，源码
  变更后必须重算；它本身不代表四模式或最终发布已通过。
- `replay_set.py` 与 `configs/schemas/replay-set-manifest.schema.json` 维护部署域分层数据边界。
  清单逐条绑定 raw recording、人工真值和可选预测流，重新验证文件哈希与帧 ID；六类
  `day/night/haze/reflection/smoke_only/no_fire` 覆盖、部署来源和标注复核只决定数据集门，
  `accuracy_validated` 永远等待独立 IoU evaluator 报告，不会由目录存在性或合成数据自动置真。
- `field_replay_campaign.py`、`scripts/run_field_replay_campaign.py` 与
  `configs/schemas/field-replay-campaign-plan.schema.json` 是上述现场证据链的可恢复编排层：计划
  固定六个来源、模型和质量门参数，`advance` 只执行可确定的机器 stage，遇到采集或四人独立审核
  就停住；每次 `status` 都从磁盘重开模型、receipt、accuracy 和 calibration，不保存可伪造的动态通过位；
  `annotation-ui` 仅在 fresh `annotation_required` stage 启动当前 plan 固定的 loopback 工作台，并先记录
  campaign/dataset/plan hash 授权事件。
- `replay_annotation_workspace.py` schema v2 与 `replay_annotation_cli.py annotate/review` 把人工动作限制在
  原子、逐帧 mutation：`review_content_hash` 固定 assignment、所有 bbox/disposition、两个人员身份和 UTC
  时间。v1 只用于历史读取，新 campaign 在 finalize 前要求 v2 内容绑定，避免复核后裸改 JSON 或降级绕过。
- `replay_annotation_workbench.py` 与 `scripts/run_replay_annotation_workbench.py` 在数值 loopback 上提供
  hash-verified 帧浏览、canvas bbox、正/负例、导航、计数和 review；随机 mutation token、CSP nonce、严格
  JSON 和图像路径/hash 复验保护工作区，并明确不产生任何相机、Pixhawk 或执行器 I/O。
- `replay_set_evaluation.py` 与 `scripts/evaluate_replay_set.py` 在不修改原清单的前提下，逐条
  复用 frame-aligned IoU evaluator，汇总 precision/recall、误报/漏检帧率和 p95 延迟；报告同时
  绑定回放集 raw/canonical SHA-256，并要求显式 `--reviewed --reviewer` 才能将独立精度门置真。
  `release_gate.py` 在最终 `release` 通道再次核对报告事件、文件哈希、摘要绑定和所有质量状态，
  所以 pending/篡改报告不能旁路 E7。
- `replay_set_readiness.py` 与 `scripts/check_replay_set_readiness.py` 是采集阶段的只读 intake
  工具：逐层统计真值/预测内容、检查 `deployment_recording` 与人工复核，确认至少存在可计算
  precision/recall 的正例分母；提供 accuracy report 时按其中门限重新运行 evaluator 并核对
  关键字段。它输出 `blocking_reasons`/`next_actions`，但不会创建标签或升级 SIM/实验室数据，
  报告格式为 `configs/schemas/replay-set-readiness.schema.json`。
- `replay_set_calibration.py` 与 `scripts/calibrate_replay_set.py` 在相同的哈希边界内调用
  类别阈值扫描器，输出 aggregate 和六个条件层的推荐/当前阈值对照；它只生成调参报告，
  不修改模型或运行时配置。`promotion_ready` 仍要求完整 `deployment_recording`、全预测流和
  显式 reviewer，因此 SIM 空基线不会被升级为部署域精度证据。报告 schema 为
  `configs/schemas/replay-set-calibration-report.schema.json`；
  `verify_replay_set_calibration_report` 与 `scripts/verify_replay_set_calibration.py` 会
  重新绑定 manifest hash 并确定性重算整份报告，复核 schema 为
  `configs/schemas/replay-set-calibration-verification.schema.json`。E7 writer/evaluator
  可通过 `--replay-set-calibration-verification` 将该独立复核 artifact 作为
  `replay_set.calibration_verification` 子绑定，并在每次读取时重跑校准请求；它只证明
  阈值报告未漂移，不替代 IoU 精度或人工复核门。
- `scripts/create_sim_replay_set_fixture.py` 将已持久化的 Gazebo/Harmonic raw capture 复制成
  六类 SIM 条件目录，生成显式空基线真值/预测并复用同一 `ReplaySetManifest`；它用于验证
  raw-sensor 哈希、帧对齐和 evaluator 接线，固定写入 `source_kind=synthetic_demo`，因此不会
  把合成画面升级成部署域精度或最终发布证据。
- `video_replay_recording.py` 与 `scripts/create_video_replay_recording.py` 将普通录制视频
  转成同一 `RawSensorReplayRecording`：输出 PNG 帧、严格单调的 telemetry/frame 事件流、
  recording manifest 和源视频哈希。转换器明确写入 camera-only/unknown-navigation 状态，
  不生成 GPS、真值、预测或部署域声明；现场 GNSS/raw-sensor、人工真值和模型预测必须作为
  独立证据加入后，才能进入回放集精度门。
- `replay_telemetry_join.py` 与 `scripts/join_raw_replay_telemetry.py` 把独立采集的完整
  `RawReplayTelemetrySample` JSONL sidecar 合并到已经验证的录像回放：每帧采用
  latest-at-or-before、禁止未来样本和插值，并以 `maximum_telemetry_age_s` 拒绝 stale
  覆盖；图像、事件流和 recording hash 会重新生成并验证，GPS 质量按 canonical policy
 逐帧重算。源 manifest 若已有完整 scenario 三元组，join 会保留该绑定并让它参与新的
 recording identity；缺失时不合成 scenario。joined 结果仍是软件回放证据，明确保持部署域、
 精度和发布 gate 为 false，不把 telemetry 绑定动作写成室外实飞或最终版本通过。
- `gnss_capture_sidecar.py` 与 `scripts/gnss_capture_to_replay_telemetry.py` 将已验证的
  `capture_gnss_samples.py` 报告转换成严格 `RawReplayTelemetrySample` JSONL：只保留
  `GPS_RAW_INT/GPS2_RAW`，按显式 monotonic origin/offset 归一化时间，并把未知姿态、AGL、
  围栏、任务和执行器谓词保留为 unknown/false。输出符合
  `configs/schemas/raw-replay-telemetry-sidecar.schema.json`，且不提升为目标真值、室外精度或
  HIL/REAL 发布证据。

## 构建与运行产物边界

### QGC UI 复用约束（当前 pre.8）

所有 REPLAY/SIM/HIL/REAL 画面都进入同一套 QGC `FlyView.qml`/`FlyViewVideo.qml` 组合；视频由
`FlightDisplayViewVideo`、`PipState` 和 QGC 原生 toolbar/map/PiP 承载。Multi-Detect overlay 只能
作为该 surface 上的 QGC QML layer，使用 `QGCLabel`、`QGCButton`、`QGCPalette` 和 `ScreenTools`
尺寸/颜色体系，不能另起一个窗口、另写一套 simulator UI 或复制一套地图。pre.3 的真实截图位于
`build/e4-qgc-visual-20260825-224648/`，其 profile 通过 `QGCQml/MainFlyWindowIsMap=false`
让同一个 Fly View 启动时直接显示视频主面板；这只是验收 profile，不改变产品 UI 契约。
历史 pre.3 palette rebuild 的 style smoke 另存于 `build/e4-qgc-visual-20260826-024914/`；此前 pre.4 combined
run 进一步用 exact PID/HWND 与 QGC QML geometry 导出验证 toolbar/drawer，仍不把机器证据写成人工
视觉验收。

Gazebo sensor 子门证据为
`artifacts/evaluation/gazebo-sitl-camera-acceptance-20260825T193511Z-3908.json`（SHA-256
`63EC2715ABDC54929DABE6975D1014693E0C69761394F63101B0803367E88C2D`）。该证据只记录真实
Gazebo 相机帧、CameraInfo、NavSat 和严格时间戳；不读取 Ground Truth、不接触真实硬件，且
`counted_as_qgc_e4_gate=false`、`operator_visual_acceptance=not_claimed`。

同一传感器链现在另有 Gazebo→QGC transport 证据：
`build/qgc-replay-fixture-gazebo-v0.3.0-pre.3/` 的 raw-only manifest SHA-256 为
`87334899FB51950CE6BF2D82108BC5B7A43267B4538D8900CB0087B8F56421A4`；真实便携版 QGC 结果
`build/e4-gazebo-qgc-20260826/e4.result.json` 的 SHA-256 为
`520B1BD6732E8FB35B1FE876E457A3C95111AB751C338287252E7E7ED3529926`，完成 `12/12` video、
`12/12` metadata barrier、`0.0 ms` 最大偏差、RTSP helper exit `0`、EOS/summary。该证据闭合
Gazebo pixels 到 QGC transport，不冒充人工画面验收；完整 fixed-wing/VTOL SIM、HIL 和 REAL
仍保持 pending。


- `build/`、QGC `.cache/`、`.venv/`、测试缓存和下载依赖都是可重建工作区，不进入 Git。
- 裸 `Release/MultiDetectGCS.exe` 是链接产物，缺少 Qt/GStreamer/QML 运行时，不作为交付物。
- Windows 可运行交付是 `portable-<version>/bin/MultiDetectGCS.exe` 及同目录完整运行时。
- 当前构建 wrapper 只创建 Release 链接产物和 portable 目录，不创建 ZIP 或 NSIS 安装器；文档中
  的 `.exe` 安装包名称只适用于未来显式启用并实际生成的 installer。
- `artifacts/` 用于本机归档和验收，不作为源代码仓库的一部分。
- `v0.3.0-pre.N` 构建使用 prerelease 文件名和目录；只有最终验收门全部通过才去掉 `-pre.N`。

## Git 与发布边界

- 仅根目录 `Multi-Detect/.git` 是 Git 元数据；`ground-control/MultiDetectGCS` 不含嵌套 Git 仓库。
- QGC 的嵌套 `.gitignore` 与根 `.gitignore` 一起排除构建目录、CPM 缓存、Qt/Android 生成物、虚拟环境、日志、安装包以及仓库根目录的 `tmp-*` 本地探针；历史文件不删除，只是不再污染 Git 状态。
- 经过校验的 Windows 便携归档或安装包若被额外生成，只保存在 `artifacts/qgc/windows/`，文件名
  使用 `MultiDetectGCS-v<version>-windows-amd64.<ext>`；`artifacts/` 中没有该文件时不得把预期
  名称写成已发布制品。
- MultiDetectGCS 从 `v0.2.0` 起使用语义版本；功能名称不进入文件名。当前开发线是 `v0.3.0-pre.8`，最终 `v0.3.0` 尚未发布。
- 当前 Windows 构建目录为 `build/windows-v0.3.0-pre.8-release/`；Release、三个 C++ 协议/wire
  自检、portable 部署与 offscreen 启动冒烟均通过，wrapper 退出码 `0`。Release 和 portable 主
  EXE 均为 `46,575,616` bytes，SHA-256
  `4D7A024F80157E5EB8671B77D6E1817FB960ECE8A8A97D0F470A222E7F7C8E40`；可运行入口是
  `build/windows-v0.3.0-pre.8-release/portable-v0.3.0-pre.8/bin/MultiDetectGCS.exe`。2026-09-01
  的 E7/SIM 矩阵 verifier 历史回归已在无 `PYTHONPATH` 的包装器路径和最终
  `release` 缺失矩阵阻断路径上补齐；全量 Python 回归为 `2324 passed, 3 skipped`，退出码
  `0`。原始输出封存于
  `artifacts/evaluation/pytest-full-20260901-scenario-matrix-e7-final-with-wrapper.log`
  （SHA-256 `41FDF5C1B83ADAFFF0530FF0BF2AA5D293CCA5BCCEE84F4CCE142E0065A1A3C0`）；三个
  skip 是本机 `onnxruntime` 缺失的两个测试与 Windows symlink 能力缺失测试，均不记作发布
  通过。当时封存的 goal-acceptance 为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-combined-qgc-v5.json`
  （SHA-256 `E4690933BC325D1CE2828DDAAC63C65689832AB8E1AFD07AB42AA9D397DE2447`，核心测试收集
  `2315` 项、QGC 自定义回归 `66/66`）；最新 E7 清单/报告为
  `artifacts/evaluation/release-gate-pre.8-current-20260901-combined-qgc-v5.json` /
  `artifacts/evaluation/release-gate-pre.8-current-20260901-combined-qgc-v5-report.json`，总发布门
  仍为 `gate_passed=false` / `release_allowed=false`，现场链路未就绪；
  现场验证器、一键采集器与交接复核器的更新补充快照为 `artifacts/evaluation/multidetect-goal-acceptance-20260831-field-capture-verify-v14.json`
  （SHA-256 `68C9F93C90D3790F3AA8D2F9F11EC8715795F6A0C6CD39A46E091207C264F7EA`，核心测试收集
  `2055` 项，QGC 自定义回归 `66/66`）。QA22、field-capture-verify-v14、field-capture-verify-v13、field-capture-v12、field-capture-v11、field-binding-v10 及更早快照均保留原始 provenance；
  旧 `final-qa`、`final-qa2`、`final-qa3`、`final-qa4`、`final-qa5`、`final-qa6` 与
  `final-regression` 文件保留为历史快照）；
   软件为 `pass_with_environment_gap`、硬件为 `offline_pending`，不能升级为最终发布。QA22 历史快照在验收启动时从
  工作树快照构建，artifact 记录 `HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、
  `dirty_entry_count=295`、`status_sha256=851a1abde3f79abd859ad032242cee0ee9bfd790663f615c3fe73b6b67d97c74`、
  `worktree_clean=false`，故属于带 provenance 的可复验 snapshot，不是干净提交发布制品。QA22 延续并验证了
  Pixhawk `GPS_RAW_INT` per-source timestamp cursor 单调性，以及 GPI `GLOBAL_POSITION_INT` 与 GPS 原子解析/非法
  sentinel 语义：同一来源旧/乱序观测不会回退 freshness、坐标或高度游标；坐标、卫星数、高度、fix 和时间戳在
  同一帧内原子校验；任一非法或缺失字段都会清除对应 raw freshness/坐标，拒绝未来时间戳，并恢复独立 EKF/LOCAL
  高度，live bridge 与此语义一致。QA22 同时修复 `depth_grid_udp` 发送完成后才起算限流窗口的竞态，并以全量回归验证；
   SIM `arm_on_start`、Docker 标准路径回退和 canonical parity 继续有效。
- 台式机补齐 CPU `onnxruntime=1.29.0` 后，完整 Python 回归为 `2449 passed`、无 skip；原始输出
  `artifacts/evaluation/pytest-full-20260902-onnxruntime.log` 的 SHA-256 为
  `4F5360DEC8E64B9C8B71CDF9BEF3ACEF7FB8C7FE87C7CD4FF7361B4B2C296012`。随后在无凭据覆盖的严格
  goal-acceptance 模式下生成 `artifacts/evaluation/multidetect-goal-acceptance-20260902-onnxruntime-credentials.json`
  （SHA-256 `F17D535362B2F18351178A7F9865766A3EAA3D015AC3392C2C437129C0750A52`），记录
  `core_tests_collected=2449`、QGC custom `66`、`software_status=pass`、
  `operator_credential_readiness.ready=true`、`gate_overridden=false`。现场仍为
  `hardware_status=offline_pending`，所以该快照只提升软件 provenance，不关闭 GNSS、Pixhawk、HIL/REAL
  或人工 Fly View 门。
- 协议级只读现场预检与稳定性 watch 回归后的当前 pre.8 软件门快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-field-watch-v29.json`（SHA-256
  `DEF51B47D75987FE948AB6D2639128B2FBC99E68F11591CABF28542C24D9E96D`，核心测试收集 `2094`，
  QGC 自定义回归 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`）。
  同轮现场协议快照为 `artifacts/evaluation/field-link-preflight-20260831-protocol-v3.json`（SHA-256
  `6DC0830C30A675AC13064EE5D3E7F5E31FA5D8BBA28D5E26219CF4E53EB78F30`）：三个机载端点均超时，
  本机 metadata/QGC 端口可绑定，`protocol_checks_passed=false`、`hardware_link_ready=false`；
  这是现场网络未接入的诊断结果，不关闭 Jetson/相机/室外 GNSS、执行器、HIL/REAL 或人工 Fly View 门，
  也不发送登录、心跳、视频启动或飞控控制数据。v27 及更早快照继续保留原始 provenance。
  随后新增三次只读稳定性 watch：`artifacts/evaluation/field-link-watch-20260831-continuation.json`
  （SHA-256 `17743B74CC2A1A276BDD4C0BAB4B0026F8A6D4C1F834BBDB00E5418EE4F63167`）。三次样本均采集，
  但机载端点连续超时，`max_consecutive_failures=3`、`stability_ratio=0.0`、
  `hardware_link_ready=false`；该证据只证明现场网络仍未接通。
 -  现场链路报告独立复核 CLI、会话 provenance 绑定以及总验收脚本证据绑定后的最新 pre.8 软件门快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-field-link-bind.json`（SHA-256
  `5842826C15783B55A662CA4A81F73096BFFA31EB0D343E97A40255A666E0CAA6`，核心测试收集 `2137`，
  QGC 自定义回归 `66/66`）。`verify_field_link_report()`、`scripts/verify_field_link_preflight.py`、
  `--link-preflight`/`--require-link-preflight` 以及总验收脚本的
  `-FieldLinkPreflight`/`-RequireFieldLinkPreflight` 已接入现场证据链；它们只会把通过且 hash 未漂移的
  链路报告作为会话或总验收前置证据，当前 watch 仍为 `hardware_link_ready=false`，因此不关闭硬件、GNSS、
  HIL/REAL 或人工 Fly View 门。此前的 `preflight-cli` 快照保留作历史 provenance。
- E7 release-gate 现在还支持顶层 `real_field_link`：
  `scripts/create_release_gate_manifest.py --real-field-link-preflight <report.json>` 写入现场报告的
  相对路径、SHA-256、字节数和严格 verifier 结果，`release_gate.py` 读取清单时重新计算并校验报告内容、
  readiness 与 gate。预发布清单省略该字段保持向后兼容；最终 `release` 通道把缺失、篡改、样本不完整或未就绪
  的绑定列为发布阻断原因。该机制只绑定现场链路证据，不把替换、摘要漂移、失败 watch
  或 REAL 自报状态升级为硬件通过。
 - 当前 pre.8 四模式清单与报告已用独立的 mode-evidence envelope 重新装配（旧的无
   `-field-rerun` 文件保留为历史 provenance）：
   `artifacts/evaluation/release-gate-pre.8-current-20260901-field-rerun.json`（SHA-256
   `A13D0223504221C986645F7EE73C6F3B15F8B1656687CE66421FCA8BD80F0A94`）和
   `artifacts/evaluation/release-gate-pre.8-current-20260901-field-rerun-report.json`（SHA-256
   `746FBF1CD8D144D532E67B7AAF0E0E615A03B11397801DC190A88E5767F4AD43`）。
   `scripts/create_release_mode_evidence.py` 只把明确指定的 runner JSON 包成
   `release-mode-*.json`，保留源文件 hash、事件、模式和 gate；缺失/不匹配的 runtime identity
   必须显式 opt-in，缺失或 `false` 的源 gate 永远保持关闭。当前报告的五组 parity 全部一致，
    REPLAY/SIM/HIL runner 源 gate 均为真；本轮 SIM envelope 已切换到
    `artifacts/evaluation/combined-sim-qgc-20260831T234111Z-53232/combined-evidence.json`
    （SHA-256 `5194CC77F45572483B6A17A21FC8FC0DBE222BE62B9A8EB1CC14777C1B464F6C`），
   900 帧、PX4/Gazebo、VTOL、Mission、QGC 原生 QQuickItem 抓帧和清理机器门通过。但人工视觉、
   Mission 状态、HIL 的视觉/硬件/real-control、REAL 的全部状态仍为 `not_claimed`，现场链路
   `hardware_link_ready=false`，`replay_set` 尚未绑定，因此 `gate_passed=false`、
   `release_allowed=false`。这份清单是当前权威开发线状态，不是最终 `v0.3.0` 发布证明。
   随后生成的 replay-bound 补充清单
   `artifacts/evaluation/release-gate-pre.8-current-20260901-field-rerun-replay-bound.json`
   （SHA-256 `261CB2755E55013278E9BDEA698AC80D110B55A09F79193C52DD91A4AB27B75C`）及报告
   `artifacts/evaluation/release-gate-pre.8-current-20260901-field-rerun-replay-bound-report.json`
   （SHA-256 `DDE5D24A785D07AE1F40D21EBA8D27B379EBD0B5CAE5CBC9633786D50FDA96C8`）只补齐
   `replay_set.present=true` 的证据绑定；其准确性、现场链路与发布门仍保持失败/未完成状态，
   不替换上述 field-rerun 权威清单。
- 本轮重新执行后的当前 pre.8 软件门快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-real-field-link-gate.json`（SHA-256
  `00F4A58B2CC809665F1580CAB5E143251387ABE671EC7D6BBC793240C97610E2`，核心测试收集 `2140`，
  QGC 自定义回归 `66/66`）。其三采样现场 watch 仍是 `hardware_link_ready=false`、verifier exit `1`，
  因此 Jetson、室外 GNSS、真实 Pixhawk/执行器、HIL/REAL 和人工 Fly View 门继续保持未完成。
- E7 `real_field_link` 绑定接线后的最新 pre.8 软件门快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-real-field-link-gate-v2.json`（SHA-256
  `7075D414DEF8A809AFEB7FB5BDD41DFE8008F80C4FE08C0228596546D01386C0`，核心测试收集 `2141`，
  QGC 自定义回归 `66/66`）。该快照把三采样 watch 的 `verified=true`、verifier exit `1`、
  `hardware_link_ready=false` 和 `gate_passed=false` 原样绑定，未关闭任何真实硬件、GNSS、HIL/REAL
  或人工验收门。
- 目标落图源帧新鲜度修复后的 v30 pre.8 goal acceptance 为
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-target-geo-age-v30.json`
  （SHA-256 `772672FE9FEAAD9369DC8B31E1A6F5B30F1EB2C1A0279ED53A3C51F2A7FD95D1`，核心测试收集 `2097`，
  QGC 自定义回归 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`）。
  生产者、Python 消息模型/编解码器和 QGC C++ 解码器统一执行 `source frame age <=1.5 s`；过期坐标原子变为不可用或被拒绝，
  不会在地图、复制、快照或重放路径中复活。canonical `configs/release-parity.json` 已同步，当前文件 SHA-256 为
  `676817B29C868FCEF90606BFD9E8FE0A2A6DD69E96177E6AE7B7AE0506A86964`。该 snapshot 仍不关闭 Jetson、室外 GNSS、真实 Pixhawk/执行器、HIL/REAL 或人工 Fly View 门；v31 已重新生成 parity。
- 精确极点坐标 fail-closed 与 native 协议自测收口后的当前 pre.8 goal acceptance 为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-target-geo-pole-v31.json`
  （SHA-256 `CC6371291F416E62E1308C78CF70CC2E08B10854D7B5A5817194E5B5164D18EE`，核心测试收集 `2101`，
  QGC 自定义回归 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`）。
  Python `TargetGeolocation`、消息模型、QGC C++ 解码器、地图/复制/快照/JSONL 重载共同拒绝精确
  `±90°` 纬度；Operator、44-fragment depth-grid 和 RTSP wire 三个 C++ 自检均退出 `0`。
  当前 canonical `configs/release-parity.json` 与同字节审计快照
  `artifacts/evaluation/release-parity-pre.8-20260901-target-geo-pole.json` 的 SHA-256 为
  `54BB83A9E7091259C38BE9A17B4996BF7B9B810BF69DA76D45F29315A9993A62`，主 EXE SHA-256 为
  `61F16B6A13247316E356C7B626E79BB1033A6AC03C54D4DD676F75BCCD4CE497`。
- QA19 原始 goal acceptance 作为历史快照保留：
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-final-qa19.json`（SHA-256
  `082092AB58848D498050901C5BD9768AABF2357569C91D72D531DD105E527515`，核心测试收集 `2001` 项，
  QGC 自定义回归 `64/64`）；它曾提升为
  `artifacts/evaluation/multidetect-goal-acceptance-latest.json`，现由 QA22 指向；原始文件和 SHA 不覆盖。
  QA19 的软件状态为 `pass_with_environment_gap`、硬件状态为 `offline_pending`，provenance 为
  `HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、`dirty_entry_count=293`、
  `status_sha256=b98a96dd5850dac360714ff836f0818da09338cc382a4bbe3af1f01c3235dbba`。
  该历史快照同样记录 GPS_RAW_INT per-source timestamp cursor 单调性与原子 sentinel 处理，不升级真实硬件或最终发布门。
- QA18 原始 goal acceptance 作为历史快照保留：
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-final-qa18.json`（SHA-256
  `230A2EC5EC44982DE4563AEAEB949E32ACD85123D706C320E056EA7BF3951F7F`，核心测试收集 `2000` 项，
  运行输出 `1999 passed / 1 skipped`，QGC 自定义回归 `64/64`）；它曾提升为
  `artifacts/evaluation/multidetect-goal-acceptance-latest.json`，现由 QA22 指向；原始文件和 SHA 不覆盖。
- QA17 原始 goal acceptance 作为历史快照保留：
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-final-qa17.json`（SHA-256
  `BF93CE4FFD7DEE0CA9D166614DAF8D6216BF0ACC7D09808DDF913B3E11D6FFA2`，核心测试收集 `2000` 项，运行输出
  `1999 passed / 1 skipped`，QGC 自定义回归 `64/64`）；它曾提升为
  `artifacts/evaluation/multidetect-goal-acceptance-latest.json`，现由 QA22 指向。QA17 的软件状态为
  `pass_with_environment_gap`、硬件状态为 `offline_pending`，并保持 `operator_visual_acceptance=not_claimed`；
  原始文件和 SHA 不覆盖。
- QA16 原始 goal acceptance 作为历史快照保留：
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa16.json`（SHA-256
  `403B319255A9C2446012BD060C85BB2633DB9A4D7DE45CF4B9BB495D96B234EE`，核心测试收集 `1976` 项，
  `1975 passed / 1 skipped`，QGC 自定义回归 `64/64`）；它曾提升为
  `artifacts/evaluation/multidetect-goal-acceptance-latest.json`，现由 QA22 指向；原始文件和 SHA 不覆盖。
- QA15 原始 goal acceptance 仍作为历史快照保留：
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa15.json`（SHA-256
  `B978D244A7440B3C102E1BDD8408110F511BE2C0209C4BC7DDB340EE56487808`，核心测试
  `1975` 项，其中 `1974 passed / 1 skipped`、QGC 自定义回归 `64/64`）；它曾经提升为
  `artifacts/evaluation/multidetect-goal-acceptance-latest.json`，现由 QA22 指向。
- pre.6 构建和 combined gate 继续作为历史证据保留。其 portable 主 EXE 为 `46,551,552` bytes，
  SHA-256 `AAFA5448E74C9D5041360F4384C98E140C5197A856751768DD88135436F4C662`；RTSP helper 为
  `79,872` bytes，SHA-256 `33935619A2A9385E96106CC73AD9ED3F2090FC2F49C5ED2A00EC3D5732DDF387`。权威 pre.6
  combined 结果为
  `artifacts/evaluation/combined-sim-qgc-20260826T125250Z-62292/combined-evidence.json`，
  文件大小 `309,023` bytes、SHA-256
  `7D6FCD1976242C85EF783E9D812D98475E2A723BAB396241801CACDDBCA0EDFC`，其中
  `gate_passed=true`、600 frames、7-item VTOL Mission、MC/FW/MC、land/disarm、GPS 和
  原生 QGC video/toolbar machine smoke 均有记录；`operator_visual_acceptance=not_claimed`，
  `qml_rendering_proved=false`，`desktop_capture_gpu_surface_proved=false`。
  历史 pre.3 Release、三个
  C++ 协议/wire 自检、portable 部署和真实 QGC E4 transport/画面证据继续独立保留，其 portable
  主 EXE 为 `46,556,672` bytes，SHA-256
  `5F462F8EDAD3677AE3CE88C0EC1B49CA9ECCA6B0B25A7E504A80B0BC7BBC0FD3`。
- pinned PX4 SIH signed metadata 结果位于
  `artifacts/evaluation/px4-sitl-qgc-operator-acceptance-20260825T190317Z-5732.json`；它是
  software-only SIM/HIL 边界证据，不接触真实 V6X/Jetson/G20/GR01，也不解锁飞控、任务或载荷写入。
- 2026-08-26 续验结果位于
  `artifacts/evaluation/px4-sitl-qgc-operator-acceptance-continue-20260826T090000Z.json`，SHA-256
  `8B4271BE2FDEA1A62576EE77392B458774C65440BA9D1985F23AEFEADF18F2F7`；它保持同一固定翼
  SIH/只读路由边界。`run_px4_sitl_qgc_operator_acceptance.ps1` 已增加 Docker Desktop 标准安装路径
  回退，避免 PATH 差异导致“docker not recognized”而未进入真正验收。
- `v0.3.0-pre.1` 的 wheel、portable 路径、大小、SHA-256、15 秒冒烟和 evidence
  manifest 属于历史证据，必须完整保留，不得映射或复用为 pre.2 结果。
- 原 `QGroundControl-MultiDetect` 目录不再是开发入口，保留为迁移回退副本，待统一仓库稳定后再由项目负责人决定归档或删除。

## 文档与证据责任

- 根 [`README.md`](../README.md)：当前版本、运行模式、能力状态和 `E0`–`E7` 证据等级。
- [`autonomy-architecture.md`](autonomy-architecture.md)：同 UI/同算法以及 backend/world 隔离的架构合同。
- [`qgc-windows-release.md`](qgc-windows-release.md)：Windows 构建命令、portable 入口、大小、SHA-256
  与启动冒烟；未知字段必须保留为 `PENDING`。
- [`release-gate.md`](release-gate.md)：E7 四模式清单、mode-evidence envelope、现场链路绑定、
  replay-set 质量门和严格/诊断检查命令。
- 根 [`CHANGELOG.md`](../CHANGELOG.md)：已进入版本的源码变化、已验证等级和尚未完成的发布门。
- 历史记录只证明记录中明确写出的版本、设备和验收范围，不能替代当前 prerelease 的重新验证。


### pre.3 VTOL SIM runner

- `scripts/vtol_sitl_scenario_acceptance.py`: digest-pinned PX4/Gazebo `standard_vtol` vehicle-dynamics evaluator. It keeps runtime input separate from truth/expectations, binds one UDP endpoint, records bounded MAVLink command ACKs, GPS quality and VTOL states, and emits evidence.
- `scripts/run_px4_gazebo_vtol_scenario_acceptance.ps1`: Windows wrapper that selects the repository QGC Python venv and writes an explicit evidence path.
- `tests/test_vtol_sitl_scenario_acceptance.py`: static boundary checks plus runtime-channel/fault-timeline contract tests.
- `artifacts/evaluation/px4-vtol-sitl-scenario-acceptance-20260826-final.json`: passing VTOL vehicle sub-gate; it is not a Fly View visual or real-hardware claim.

The UI boundary remains the native QGC surface. The runner does not add a simulator window, duplicate map or alternate color theme.
## pre.3 原生 QGC / E4 模块责任补充（2026-08-26）

本轮继续维持单仓库、单 QGC surface。没有 simulator-only UI，也没有独立 FixedWing HUD。关键责任为：

| 路径 | 责任 |
| --- | --- |
| `ground-control/MultiDetectGCS/custom/src/FlyViewVideo.qml` | 复用 QGC `FlyView` / `PipView` 视频 surface；PiP 只读 annotations，full view 才开放交互 |
| `ground-control/MultiDetectGCS/custom/src/FlyViewCustomLayer.qml` | 使用 `QGCPopupDialog`、`QGCSlider`、`QGCToolInsets` 与 QGC palette/controls 组合任务层 |
| `ground-control/MultiDetectGCS/custom/src/MultiDetectOperatorController.*` | 认证 type 23、采纳 native state，并在采纳后生成 signed type 24 ACK |
| `ground-control/MultiDetectGCS/custom/tools/MultiDetectReplayRtspPublisher.cc` | 对实际 appsrc push 产生 `frame_accepted`，管理 RTSP media/appsrc reconnect generation |
| `ground-control/MultiDetectGCS/custom/tools/MultiDetectReplayRtspGeneration.h` | 单调 generation 与 identity-safe cleanup 的小型生命周期状态 |
| `src/multidetect/qgc_replay.py` | E4 RTSP/QGC transport 编排，不伪造 publisher ACK 或 native-controller ACK |
| `scripts/run_px4_gazebo_qgc_combined_sim_acceptance.ps1` | 组合 SIM 验收入口；其存在不等于 gate 已通过 |
| `build/windows-v0.3.0-pre.3-release/verification/v0.3.0-pre.3-evidence.json` | 最终 build/test/integration 身份的机器可读汇总；必须在最终 portable rebuild 后重新计算 |

Canonical E4 顺序为：真实 RTSP `frame_accepted` → signed RuntimeStatus type 23 send → QGC native
controller adoption → authenticated matching RuntimeStatusAck type 24。type 24 的责任边界止于
**native-controller post-adoption**，不能声明 QML rendering、操作者视觉验收或运行模式策略接受。
`operator_visual_acceptance` 必须保持 `not_claimed`，直到人工 Fly View 点验留证。

RTSP 断开重连通过每次 media/appsrc 会话的新 generation 处理；旧 media 的迟到 cleanup 不得清理
新 appsrc。同一 wire frame 在 generation 切换中仅在实际 push 成功后产生一次 `frame_accepted`；
超时、push 失败、非法帧和最终无客户端继续 fail closed。

`REPLAY` / `SIM` 与后续 `HIL` / `REAL` 共用上述 controller、QML 和算法核心，只替换 channel、
传感器/动力学与时钟。完整 REPLAY/SIM、PX4 Mission、HIL、REAL 和户外 GNSS 尚未全部闭合；
本节不声明 combined 300-frame gate 通过。

重构后的最终 EXE 大小与 SHA-256 等待 root portable rebuild。前文 pre.3 精确值只代表此前中间快照；
最终值只认 `build/windows-v0.3.0-pre.3-release/verification/v0.3.0-pre.3-evidence.json`。
## pre.3 权威 combined evidence 归档（2026-08-26）

前一节中的 pending build/gate 状态已由下列权威运行取代，历史块继续保留：

```text
artifacts/evaluation/combined-sim-qgc-20260826T013505Z-64000/
  combined-evidence.json
  integrated-sim-qgc.result.json
  integrated-sim-qgc.audit.jsonl
  qgc-before-pip-swap.png
  qgc-after-pip-swap.png
  qgc-fly-view-fixed-wing.png
  vtol-attached.result.json
  container.inspect.sanitized.json
  container.log
```

`combined-evidence.json` 为 `87,623` bytes，SHA-256
`6374DB67E985BBC003682BFC9F764B416604A9C3566A4A33D0164728D44219AA`，记录
`gate_passed=true`。它绑定的 portable 制品为：

| 文件 | bytes | SHA-256 |
| --- | ---: | --- |
| `MultiDetectGCS.exe` | `46,561,280` | `4C3584F984592FDDF9114EC9C08EC3C68F86C768BFAD4EB3D0F505A263FD55FE` |
| `MultiDetectReplayRtspPublisher.exe` | `79,872` | `AB3E0043DA4A124270BEE1D5EC75A717A9845CF37CB63A8ADC8A50B4752D206F` |

责任链已经闭合为 `300 frame_accepted → 300 contiguous type 23 → native adoption → 300 contiguous
post-adoption type 24`，RTSP helper exit `0` 且 EOS/summary 完整。PX4 完成 fixed-wing transition 与
multicopter return，GPS qualified；受保护 UDP `14550` 未改变、测试端口全部释放、任务 container
停止并删除。

PiP evidence 仍落在原生 QGC 层：exact PID/HWND `40048 / 0x1C534FC`，
`WindowFromPoint(286,937)` 验证 hit PID/HWND 后再 `SendInput`；QSettings
`QGCQml/MainFlyWindowIsMap` 从 `true(default)` 变为显式 `false`。before/after UTC mtime 为
`2026-08-26T01:35:38.3155174Z` / `2026-08-26T01:35:40.4183819Z`，hash 为
`BD2E993A77172B0668F6ADBF3DCA9F4F3C0083235F9F1B7258734F111CF9B578` /
`764677D852C516ACE28A070886277565D0282E8D09756B1A6A56EF96401C52B8`。普通 PiP swap 不重启
video receiver。

截图只是 machine smoke；GPU-backed surface 用 `PrintWindow` 可能为黑面，故
`operator_visual_acceptance=not_claimed`、`qml_rendering_proved=false`。真实 HIL/REAL、室外硬件、
完整 PX4 Mission 与人工视觉验收仍阻塞最终 `v0.3.0`。
## pre.3 唯一权威 evidence 指针（2026-08-26T02:08Z）

历史 pre.3 run 的 authoritative artifact tree 是：

```text
D:\Projects\Multi-Detect\artifacts\evaluation\combined-sim-qgc-20260826T020829Z-57624\
  combined-evidence.json  # 87,530 bytes; SHA-256 62CFCA2B42EE1A9A69676B91220476609D1505DA46DE36C187E2A85BBA14EB6F
```

该历史 pre.3 run 绑定 QGC `46,561,280` bytes /
`7A4D8B90F0DF0680536B9A91BEF36177542153F97745049D83D25B13BF52554D` 和 helper `79,872` bytes /
`AB3E0043DA4A124270BEE1D5EC75A717A9845CF37CB63A8ADC8A50B4752D206F`。前一节
`T013505Z-64000` 与 `4C3584F9...` 保留为历史 pre.3 run，不再是当前发布身份。

Runner ownership 补充：combined wrapper 必须以 `Remove-Item Env:` 删除
`MULTIDETECT_DISABLE_VEHICLE_LINK`、`MULTIDETECT_HIL_UI_AUTO_EXERCISE`、
`MULTIDETECT_HIL_UI_AUTO_EXIT`，不能赋空字符串。权威证据为
`acceptance_only_environment_absent_before_launch=true`（`env_absent=true`）、
`hil_ui_auto_exercise_log_count=0`、`hil_ui_auto_exercise_disabled=true`。300-frame stream timeout
动态为 `180 s`；`combined-sim-qgc-20260826T020108Z-52536` 是旧固定 `100 s` timeout 的历史失败重试，不计成功 gate。

当前 run 的 type 23/type 24 均为连续 `1..300`，helper exit `0`、EOS/summary 完整。QGC exact
PID/HWND 为 `55056 / 0x18D20DE`；QSettings `MainFlyWindowIsMap` 从 `true(default)` 变为显式
`false`，普通 PiP swap 不重启视频。截图语义检查显示 `环境 SIM · HEALTHY`，但仍只是 machine
visual smoke：`operator_visual_acceptance=not_claimed`、`qml_rendering_proved=false`。

PX4 transition/return、GPS qualified、UDP `14550` unchanged、测试端口释放、container 删除已闭合；
真实 HIL/REAL、室外硬件、完整 PX4 Mission 与人工视觉门继续阻塞最终 `v0.3.0`。

## pre.8 模块责任与证据边界（2026-08-31 更新）

pre.8 是当前源码候选；Windows Release/portable 已完成重建并封存制品证据。版本单一真源为
`configs/product-version.json`：`release_label=0.3.0-pre.8`、`python=0.3.0rc8`、
`numeric=0.3.0`。此前责任清单记录的 pre.8 主 EXE `46,552,576` bytes、SHA-256
`C8A3FB009EFE14AFAB5EF4D3FE6BC950D3C6B0AFE37BCD0D46A88612964A6F2B` 属于历史构建；
  GPS/回放、QGC source-age/frame-hold、SIM world-origin、测距 freshness、离线 depth-grid 初始化和极点坐标门加固后的
  **2026-08-31 历史快照**主 EXE 为 `46,575,104` bytes、SHA-256
  `61F16B6A13247316E356C7B626E79BB1033A6AC03C54D4DD676F75BCCD4CE497`；它不代表当前
  portable 制品。RTSP helper
`79,872` bytes、SHA-256 `50F17CF7E19542748035329585DBE59526D2739CB7BC41B65A72003C84C1F6A4`；
  新 portable 离线 `--simple-boot-test` 退出码为 `0`，depth-grid bind failure 未再出现；日志证据为
  `artifacts/evaluation/qgc-simple-boot-20260831-depth-grid-fix.log`（SHA-256
  `AED8F43CD039490BA5B775C3A3FE4A7DBAC957FE228B5BEC6FE7A20B5E6AD80F`）。
  同一构建后的 deterministic SIM follow-up 三件套也已封存：3-D 控制/资产预检
  `artifacts/evaluation/simulator-3d-control-acceptance-20260831-followup.json`
  （SHA-256 `56099CE79D65DF7D240BFC633E3479EA848053E4665F62AA0B6157F2509A82AC`）、
  AI authority `simulator-ai-acceptance-20260831-followup.json`
  （SHA-256 `121FEA672C027C299A3B40B9D7F249EC15DD3FB46CA17C2FC6D18BE3221AC84E`）和
  closed-loop `simulator-closed-loop-acceptance-20260831-followup.json`
  （SHA-256 `11195BBC7B8E28082A446524040A32F8D026B7B9C87164F85389771C17ECA949`）均为
  `gate_passed=true`；外部 PX4/Gazebo 工具不可用时保持 `external_runtime_gate_passed=false`，
  因而不替代视觉、硬件、HIL 或 REAL 证据。
  当前 portable inventory 为 `1,013` files / `529,288,077` bytes，`bin/` 为 `233` files。exact-client
原生 QGC machine capture 为 `build/qgc-native-style-pre.8-current/qgc-native-style.png`，
`475,725` bytes、SHA-256 `1BC638D8AC3A3AEBD9BB19206438D621A9276C2F2B76C64679EC6779EF216634`；
`single_native_surface_machine_check=true`。pre.6 combined evidence 已独立归档于
`artifacts/evaluation/combined-sim-qgc-20260826T125250Z-62292/combined-evidence.json`，大小
`309,023` bytes、SHA-256 `7D6FCD1976242C85EF783E9D812D98475E2A723BAB396241801CACDDBCA0EDFC`，
`gate_passed=true`；最终 SIM/HIL/REAL 和人工验收仍按下述边界单独计账。

现场链路预检、JSONL 会话验证器、一键采集器与交接复核器的最新软件门补充证据为
`artifacts/evaluation/multidetect-goal-acceptance-20260831-field-capture-verify-v14.json`，SHA-256
`68C9F93C90D3790F3AA8D2F9F11EC8715795F6A0C6CD39A46E091207C264F7EA`；核心测试收集 `2055`、
QGC 自定义回归 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`。
该补充证据不改写 `multidetect-goal-acceptance-latest.json` 的 QA22 历史指针，也不关闭真实
Jetson/相机/室外 GNSS、执行器、HIL/REAL 或人工 Fly View 门。
同一台式机的现场链路 follow-up 预检为
`artifacts/evaluation/field-link-preflight-20260831-followup.json`（SHA-256
`4CE813D240826C6D9304A943D76FDCA675AF65E39B1B6F1688D0DC32ADE90156`）：本机 `14580/14581` 可绑定，
但 Jetson `192.168.144.20:22`、GR01 `192.168.144.11:5760` 与相机 `192.168.144.108:554` 均超时，
`hardware_link_ready=false`；该状态是现场网络未接入，不是软件门失败。

现场采集入口 `scripts/capture_field_session.py`（安装后命令为
`multidetect-field-capture`）只读尾随 AuditLog/JSONL，并把完整记录复制到新的证据流；它支持
`session_id`、sequence/timestamp 起点、`DET/TRK/LCK/TGT` 停止状态、最大记录数、时长和空闲超时。
每条记录单独 flush/fsync，结束时生成收口时整份源 SHA-256、实际消费前缀的 SHA-256/字节数、复制
输出的 SHA-256/字节数、过滤/损坏行、未完成尾部、停止原因和共享 `field_session_acceptance` 结果。
源日志在收口后继续追加不会改变 `source_prefix_sha256`；采集错误会与验证失败一起使
`gate_passed=false`，所以“采到一段日志”不会被写成完整现场通过；该工具不连接飞控、不发送控制数据。

2026-09-01 的最新 combined SIM/QGC/Mission machine gate 已替代上述历史基线作为当前
pre.8 运行证据（Docker Desktop 恢复后重新执行 900 帧）：

当前台式机最新 900 帧外部 Docker PX4/Gazebo/QGC source evidence 为
`artifacts/evaluation/combined-sim-qgc-20260901T074256Z-48728/combined-evidence.json`
（772,736 bytes，SHA-256
`4A50E88770FF1B2084A92D72E228C4C1913E49E69BAAFE46BA43B742939D774F`，
`gate_passed=true`）。独立 33/33 verifier 输出位于
`artifacts/evaluation/combined-sim-qgc-20260901T074256Z-48728/combined-evidence-verification.json`
（SHA-256 `B2855FD4C7B64DA178676A00B01EFB3E05969681ED01B9DE597D130883D1FCD7`），SIM
envelope 为
`artifacts/evaluation/release-mode-sim-20260901-pre.8-combined-dynamic-ports-v2.json`
（SHA-256 `F608493C7F1385D070B8CCAE903444F4028B78FF21AB30152C9C05958BC8ABC0`）。该 run
覆盖动态端口、传感器流、VTOL `MC→FW→MC`、Mission barrier、原生 QGC toolbar/video
machine smoke、Docker stop/rm 和端口释放，仍保持 software-only 语义。

同一 source/envelope 已绑定到
`artifacts/evaluation/multidetect-goal-acceptance-20260901-combined-qgc-v3.json`
（SHA-256 `64E92ADB8B99033520EE27703BAC442505828E1EA12B3E56D211864C66EC7882`）和
`artifacts/evaluation/release-gate-pre.8-current-20260901-combined-qgc-v3.json`
（SHA-256 `8B78B49D7F857051FB171C5A64C2DC6D920990B926004291A7472C9745F0B1D7`）。整体
发布门仍因现场链路、REPLAY/HIL/REAL、人工 Fly View 和最终 release channel 保持关闭。

上一轮 source 保留为历史 provenance：
`artifacts/evaluation/combined-sim-qgc-20260831T234111Z-53232/combined-evidence.json`
（771,782 bytes，SHA-256
`5194CC77F45572483B6A17A21FC8FC0DBE222BE62B9A8EB1CC14777C1B464F6C`，
`gate_passed=true`）。它闭合 900 帧同核 integrated SIM/RTSP、helper `exit=0`、VTOL Mission、
原生 QQuickItem VideoOutput/PiP/fixed-wing 抓帧、toolbar 抽屉机器 smoke 和 disposable 容器清理；
`operator_visual_acceptance=not_claimed`、真实硬件/室外 GNSS/HIL/REAL/实飞及最终 `v0.3.0`
发布门仍未闭合。

当前证据还经过独立的 Combined SIM 契约复核：
`src/multidetect/simulator_combined_evidence.py` 和
`scripts/verify_combined_sim_evidence.py` 对 source JSON 重新计算 33 项检查，覆盖动态
loopback 端口分配/释放、保护 UDP `14550`、单容器生命周期、VTOL/Mission/QGC barrier，
并递归拒绝硬件或人工视觉的正向声明。验证结果保存在
`artifacts/evaluation/combined-sim-qgc-20260901T051239Z-76220/combined-evidence-verification.json`
（SHA-256 `E8440B7B4DDD5981A1A8B7FD279CF210AE016E1858D008B9DE8D5DD507BFFCA2`）。
E7 release-gate manifest 的顶层 `simulator_combined` 字段现在绑定 source 与
release-mode evidence 的路径、SHA-256、版本和逐项 checks；当前 v2 清单/报告 SHA-256
分别为 `C026AE1A3209BFE4798DDD600A12EBC4F1684DCE49E871DF5F28D80253D5A188` 和
`47222082B48FFB8E20AB1264A78CC4C25F6FFBBD9F0321AF17C18E420A65F0FD`，其中
`simulator_combined.passed=true`，但总 release 仍保持 `gate_passed=false`。
该复核只增强 SIM 证据完整性，不提升 `operator_visual_acceptance`、真实 Pixhawk/Jetson、
室外 GNSS、HIL/REAL 或实飞状态。

| 路径 | pre.8 责任 | 禁止越界 |
| --- | --- | --- |
| `src/multidetect/px4_mission.py` | runtime-only VTOL geometry、安全 policy、canonical `MISSION_ITEM_INT` compiler、hash-bound transaction | 无 network I/O；不接 evaluator/Ground Truth；不提供 generic command bypass |
| `src/multidetect/mission_progress.py` + `configs/schemas/mission-progress.schema.json` | 将 hash-bound `ExecutableMission` 与 SIM/HIL/REAL 只读 telemetry 绑定为 WGS-84 距离、接受半径、盘旋驻留、VTOL 转换、RTL/降落和 GPS/link freshness 的 `MissionProgressSnapshot`；不产生 MAVLink 或 actuator 输出 | observation-only 进度证据；stale/future/弱 GPS、极点坐标和未解锁/断链保持 fail-closed，不替代 PX4 `MISSION_CURRENT`、真实 GNSS、HIL/REAL 或最终 release |
| `src/multidetect/px4_mission_feedback.py` + `configs/schemas/px4-mission-feedback.schema.json` | 将 PX4 `MISSION_CURRENT` / `MISSION_ITEM_REACHED` 绑定到 executable Mission hash、MAVLink source identity、序列历史、单调接收时钟与 `<=1.5 s` freshness，并可作为 MissionProgress 的只读佐证门 | 只消费 wire feedback；换源、回退、越界、超前、stale/future 均 fail-closed；不上传/启动 Mission，不证明真实 Pixhawk/HIL/REAL 已通过 |
| `src/multidetect/px4_mission_feedback_runtime.py` + `src/multidetect/px4_mission_feedback_cli.py` + `configs/schemas/px4-mission-feedback-observation-v2.schema.json` | 将 v2 receive-only report 绑定到签名 RuntimeProfile、heartbeat vehicle identity、resolved endpoint/baud、Mission airframe 与 locked output decision；v1 继续只作历史诊断 | HIL 必须是无 signing identity 的 `hil` profile；REAL 必须是 `flight` profile但仍关闭 output opt-in；密钥不序列化，v1/换源/换端点/换 profile/解锁均不能进入 E7 |
| `src/multidetect/simulator/closed_loop.py` + `src/multidetect/simulator/scenario_runner.py` | 通过同一 authority → adapter → MAVLink sink → flight model 接收可选编译 Mission；`bind_mission()`/`start_mission()`/步骤 `mission_progress` 保持 hash-bound、确定性和三机型复用 | 运行器未显式 `start_mission()` 时只保留 `ready`；不隐式上传/启动 Mission、不创建 simulator-only UI，不把 deterministic software evidence 升级为 SITL/HIL/REAL 或人工验收 |
| `src/multidetect/simulator/scene_state.py` + `configs/schemas/simulator-scene-stream.schema.json` | 将 runner 同一 tick 的 `MissionProgressSnapshot` 作为可选字段写入 hash-bound 3-D scene stream；旧流省略字段保持 canonical hash/回放兼容，`scene_state` CLI 支持 `--mission`/`--start-mission` | scene stream 读取时再次执行进度 schema/序列/比例校验；只证明 truth-free software/SIM replay，不替代 PX4 `MISSION_CURRENT`、外部 3-D、真实 GNSS/Pixhawk、HIL/REAL 或人工验收 |
| `src/multidetect/px4_mission_acceptance.py` + `scripts/run_px4_airframe_transaction_acceptance.py` + `scripts/verify_px4_airframe_transaction_acceptance.py` | 让 VTOL、fixed-wing、multirotor 都经 `compile_executable_mission` → `MISSION_ITEM_INT` → journalized upload/readback/commit/start-permit；独立 verifier 重算三份 JSONL；E7 `release-gate` 可通过 `px4_airframe_transaction` 重新绑定 source 与 journals | software/SIM in-process contract；`physical_output_enabled=false`，不替代 Pixhawk、HIL、REAL、室外 GNSS 或人工 Fly View |
| `src/multidetect/px4_mission_feedback_binding.py` + `src/multidetect/release_gate_manifest.py` + `src/multidetect/release_gate.py` | 将 HIL/REAL × fixed-wing/multirotor/VTOL 六组 Mission + signed profile + v2 feedback、身份、文件 hash/bytes 和当前四模式 `px4_mission_path_hash` 直接绑定为 E7 子门；最终 release 强制要求完整矩阵 | 六份 profile/report 必须分别唯一；聚合记录的硬件/HIL/REAL/人工视觉 claims 固定为 false；当前无现场六源，子门 absent 且总门关闭，不把 fake、Docker 或历史软件证据升级为实机验收 |
| `src/multidetect/qgc_visual_acceptance.py` + `configs/schemas/qgc-operator-visual-acceptance.schema.json` + `configs/qgc-visual-review-checklist.json` | 将人工 12 项视觉 review 绑定到 exact PID/HWND machine report、PNG、QGC EXE、当前 UI contract 和 0 假点 geolocation 负控，并提供 create/verify CLI | 机器报告不得自行提升人工结论；内容 SHA 不是身份签名；accepted record 尚未生成且未接入 E7 时，`operator_visual_acceptance` 必须保持 `not_claimed` |
| `src/multidetect/px4_gazebo_qgc_router.py` | 一个 owned loopback SITL 的 QGC 只读路由与 identity/mission/item/command 有界 evaluator 路由 | QGC Mission/param/control 写入仍禁止；`MISSION_CLEAR_ALL` 禁止；无关帧计数并阻断 |
| `scripts/vtol_sitl_mission_acceptance.py` | 附着已有 container/router/PX4，执行 INT-only upload/readback/hash/commit/AUTO Mission 与飞行状态观察 | 不创建/停止 container；不拥有 UI；不读取 runtime 的 truth channel |
| `scripts/run_px4_gazebo_qgc_combined_sim_acceptance.ps1` | 组合现有 Gazebo pixels、RTSP/type23/type24、exact-client capture、VTOL Mission helper 与清理证据 | 单一 owned lifecycle；当前源码存在不等于真实 Mission gate 通过 |
| `src/multidetect/simulator_combined_evidence.py` + `scripts/verify_combined_sim_evidence.py` | 独立重算 Combined SIM source evidence 的动态端口、保护端口、生命周期、VTOL/Mission/QGC barrier 与声明边界；新 artifact 还会按 `hashes.mission_journal` 重新读取 durable JSONL，校验 SHA-256、字节数、hash-chain、恢复状态、最终状态和请求 sequence；支持 release-mode hash-bound 绑定 | 只关闭 software-only Combined SIM/S4 子门；历史 pre-journal 文件仍按原 33 项契约复核；不把 JSON 声明升级为桌面人工视觉、真实硬件、室外 GNSS、HIL/REAL 或实飞 |
| `src/multidetect/simulator/scenario_matrix.py` + `scripts/run_simulator_scenario_matrix.py` + `scripts/verify_simulator_scenario_matrix.py` | 读取三机型 manifest，使用共享 manual clock/flight model/AI authority 运行 truth-free 矩阵；`--cover-faults --fault-tail 1` 自动把帧预算扩展到最后一个有限时长事件，逐场景检查 `fired`、`active_frames`、覆盖率和重复 `evidence_hash`；当前 pre.8 证据别名为 `simulator-scenario-matrix-20260903-fault-coverage-pre8.json`，三机型 9 个故障事件覆盖率均为 `1.0` | 只关闭 deterministic software/SIM matrix 子门；truth-free、`external_runtime`/`qgc_visual_acceptance`/硬件字段仍保持 `not_claimed`，不替代外部 Gazebo 3-D、室外 GNSS、真实 Pixhawk/执行器、HIL/REAL 或人工 Fly View |
| `src/multidetect/simulator/scene_state.py` + `configs/schemas/simulator-scene-stream.schema.json` + `scripts/verify_simulator_scene_stream.py` | 在同一 runner tick 可选记录 hash-bound `SimulatorSceneSnapshot`/`SimulatorSceneStream`，绑定 WGS-84 origin、实测 3-D pose、telemetry/observation、AI/manual/authority/watchdog 与 active faults；schema、逐帧 hash、stream hash、round-trip replay 和独立 CLI verifier 均可复核 | `record_scene_stream` 默认关闭；只关闭 truth-free software/SIM 状态重放子门，不读取 evaluator truth，不替代外部 3-D、人工 QGC、真实硬件、室外 GNSS、HIL/REAL 或最终发布门 |
| `src/multidetect/simulator/three_d_control.py` + `scripts/run_simulator_3d_control_acceptance.py` + `scripts/verify_simulator_3d_control_evidence.py` | 为 VTOL、fixed-wing、multirotor 生成并独立复核 3-D world/model、WGS-84、stack-plan、PX4 launch preflight、QGC manual/AI authority、watchdog/failsafe 与三机型 hash binding；E7 `release_gate` 每次重新打开三份 source | deterministic protocol/asset/control 子门；`external_runtime_gate_passed=false`、视觉/硬件 `not_claimed`，不替代外部 PX4/Gazebo、Pixhawk、室外 GNSS、HIL/REAL 或人工 Fly View |
| `src/multidetect/simulator/terrain_truth.py` + `scripts/run_simulator_terrain_ranging_acceptance.py` + `configs/schemas/simulator-terrain-ranging-evidence.schema.json` | 严格解析独立非平坦 SDF 的 ENU transform/collision plane，恢复解析地形真值，并比较共享 DEM 的 41 个表面样本和 7 条射线（6 条跨单元 + 1 条垂直基准）；SDF、DEM 与当前五组 parity 均按 bytes/SHA-256 绑定 | 只关闭 deterministic asset-geometry truth 子门；不启动 Gazebo/PX4/QGC、不读取 evaluator channel，不替代外部 SIM、人工视觉、真实 GNSS/Pixhawk、HIL/REAL 或实飞 |
| `scripts/validate_qgc_desktop_capture.py` | 双帧 video-only ROI 与 transport/type24 绑定的 visible-video surface validator | temporal diff 只作诊断；不声明 exact source frame identity、完整 QML 语义或人工验收 |
| `src/multidetect/field_link_preflight.py` + `scripts/run_field_link_preflight.py` + `scripts/run_field_link_preflight.ps1` + `scripts/inspect_field_link_interfaces.ps1` + `scripts/verify_field_link_preflight.py` | 现场 Jetson SSH、GR01 TCP、RTSP TCP 与本机 metadata UDP 端口预检；可选 `--protocol-checks` 被动验证 SSH banner、MAVLink v1/v2 帧和 RTSP OPTIONS；`--samples/--interval-s/--stop-on-failure` 提供有限次数稳定性 watch；`--discover-cidr` 在显式 IPv4 范围内并发识别 SSH/MAVLink/RTSP 候选主机；PowerShell 入口先执行网卡 `-VerifyOnly` 快照，再把 `-JetsonHost/-Gr01Host/-CameraHost` 与五个端口参数原样转发给同一 Python 预检，并自动执行 `--allow-unready` 或 `--require-all` verifier；`inspect_field_link_interfaces.ps1` 在迁移后以只读方式枚举物理接口、IPv4、目标 CIDR 与直连 route，输出 `selection.status=ready|ambiguous|missing`；传入 `--interface-diagnostic`/`-InterfaceDiagnostic` 后，预检报告 hash-bind 该快照的路径、SHA-256、CIDR、候选接口和显式 alias，`verify_field_link_interface_diagnostic()` 与 `verify_field_link_report()` 会离线重开源文件并拒绝漂移；独立 verify CLI 默认要求 `hardware_link_ready=true`，`--allow-unready` 仅用于结构诊断 | 只证明端口/协议可达或可绑定，不证明 GNSS、传感器新鲜度、执行器或 REAL/HIL；发现结果不会自动选择端点或提升就绪，协议预检与 watch 不发送登录、心跳、视频或飞控控制数据；PowerShell 入口和接口诊断均不修改网卡/IP/路由；`missing|ambiguous` 诊断状态仍保持 fail-closed |
| `src/multidetect/replay_set_evaluation.py` + `scripts/evaluate_replay_set.py` | 对六类部署域分层回放集逐条执行共享 IoU evaluator，输出哈希绑定的 precision/recall、误报/漏检帧率、p95 延迟和独立复核状态；固定要求 `day`、`night`、`haze`、`reflection`、`smoke_only` 五个正向层各自拥有真值/预测计数，`no_fire` 只作负例控制，并供 E7 release-gate 复核 | 没有完整部署记录、预测流、阈值通过、每个正向层的非零计数或显式复核时保持 `accuracy_validated=false`；不把合成/实验室目录或未复核预测写成最终精度证据 |
| `src/multidetect/model_review.py` + `scripts/create_model_review_bundle.py` + `configs/schemas/model-review-bundle.schema.json` | 重新验证 fire/smoke 候选 manifest 的 `post_nms_N_x_6`/`N_x_6` contract，绑定精确模型 artifact、model-check 与部署域 replay accuracy，并记录四类独立治理审核；输出可被 E7 writer/evaluator 重新哈希和重算的 `model_review` envelope | `synthetic_hil`、缺失/篡改 evidence、模型 hash/ID/version 不一致或任一治理审核 pending/failed 都保持 `production_approval_eligible=false`；审核 envelope 永不打开飞控或物理载荷 |
| `scripts/create_gazebo_raw_replay_fixture.py` + `scripts/create_sim_replay_set_fixture.py` | 将 Gazebo raw capture 接入同一 replay-set；逐帧校验 NavSat 纬度 `(-90,90)`、经度 `[-180,180]`、海拔 `[-1000,100000] m`，非法帧计入 `invalid_navsat_count` 并降级为未知位置，再保留 synthetic 六层空基线以验证接线 | 只证明 raw transport/哈希/帧对齐，`source_kind=synthetic_demo`、空正例与未复核标注不能关闭部署精度、室外 GNSS、HIL/REAL 或最终发布门 |
| `src/multidetect/simulator/runtime_preflight.py` + `python -m multidetect.simulator runtime-preflight` | 只读发现 native PX4/Gazebo、Docker client/daemon 与 Windows WSL；可选绑定 `--repository-root`/`--scenario`/`--px4-workdir`，输出世界/模型/stack-plan SHA-256、`asset_preflight`、`px4_workdir_preflight`、`external_runtime`、`ready_to_attempt`、`execution_mode`、工具原因、限量 `docker_diagnostics` 和 hash-bound 证据 | 不启动进程/容器、不打开 UDP、不观察 telemetry/相机；`external_runtime.gate_passed=false` 且回退 `automatic=false/used=false`，backend 日志只保留有限错误摘要，工具可用不等于外部 SIM、视觉、硬件或 HIL/REAL 通过 |
| `scripts/recover_docker_desktop_stale_socket.ps1` | Windows Docker Desktop 陈旧 `dockerInference` reparse/socket 的只读盘点与显式恢复；默认不改状态，`-Apply` 先可逆移动，`-RemoveStaleEntry` 才开启精确 `fsutil` 清理，`-RestartDocker` 可在处理后重启 | 只触碰当前账户 Docker run 目录中的该单一条目；不删除镜像/容器/卷，不执行 factory reset；恢复成功仍必须重新跑 runtime-preflight 和外部 SIM gate |
| `scripts/run_px4_gazebo_qgc_combined_sim_acceptance.ps1` | 在 Docker `info`/镜像探测前调用同一 `runtime-preflight`，将 hash-bound 预检写入组合验收目录，外部 runtime 未就绪时 20 秒内带诊断 fail-fast；兼容 Windows PowerShell 5.1 的 quoted-Arguments fallback | 预检仍是环境诊断，不启动 PX4/Gazebo；只有 `ready_to_attempt=true` 才进入单容器/单 QGC 联合 gate |
| `src/multidetect/field_session_acceptance.py` + `scripts/validate_field_session.py` | 对现场 JSONL/AuditLog 信封重算 DET→TRK→LCK→TGT、取消后重捕获、GPS 原始质量与 RANGE_STATUS 双新鲜度，并校验事件时间单调性与 GPS/RANGE 同目标绑定；`--require-target-state-evidence` 还把样本绑定到最近状态上下文，要求 TGT 上下文内同时出现新鲜合格 GPS/RANGE；可选绑定现场链路报告并复核其 SHA-256/readiness | 离线证据契约 gate；不把 fixture、SITL 或软件日志升级为真实硬件/室外 GNSS 验收；指定链路报告未通过时保持 fail-closed |
| `src/multidetect/field_session_capture.py` + `scripts/capture_field_session.py` + `scripts/verify_field_session_capture.py` | 只读尾随现场 AuditLog、按会话/序号/时间筛选、在目标状态或超时收口并实时落盘，再调用同一现场 gate；`--link-preflight/--require-link-preflight` 把捕获绑定到通过的链路报告，`--require-target-state-evidence` 将严格 TGT 证据开关写入 capture report；复核器重算输出 gate、源消费前缀/输出 hash 与链路报告 hash | 采集器不发送 MAVLink/控制数据；空闲、损坏、前缀、输出 hash、链路文件改写、取消后的旧上下文或 readiness 失败都会保留证据但关闭 `gate_passed` |
| `src/multidetect/field_evidence_bundle.py` + `scripts/check_field_evidence_bundle.py` + `configs/schemas/field-evidence-bundle.schema.json` | 将上述现场链路、GNSS、执行会话、目标事件、raw media timeline 和独立 WGS-84 accuracy 六层重新验证并以输入 SHA-256/字节数交叉绑定；支持严格 REAL 域、`--allow-sim-domain` 诊断和 `--report-only` 待办输出；`release_gate_manifest.py`/`release_gate.py` 可用 `--field-evidence-bundle` 做 E7 hash-bound revalidation | 纯只读 join；默认拒绝 scenario-bound recording、stale/未来 telemetry、跨会话/跨目标或 sidecar 漂移；固定 `release_eligible=false`/硬件 `not_claimed`，不关闭 E7/HIL/REAL |
| `src/multidetect/geolocation_survey.py` + `scripts/build_target_geolocation_accuracy.py` | 先验证 target-event replay 的事件/媒体哈希，再按 `event_id/frame_id`、可选 `target_id` 将 fresh/qualified 估计坐标与独立真值连接，调用同一 WGS-84 精度 evaluator；输出结构由 `configs/schemas/target-geolocation-accuracy-report.schema.json` 固定 | 只生成离线 REAL/field 精度报告；stale/unknown GPS、漏配/多配、跨目标冲突和额外真值默认 fail-closed，`--allow-*` 仅用于明确的诊断子集 |
| `src/multidetect/geolocation_survey_template.py` + `scripts/create_target_geolocation_survey_template.py` + `configs/schemas/target-geolocation-survey-template.schema.json` | 先验证 target-event replay，再自动导出只含 fresh/qualified 事件的 CSV/JSON/JSONL 真值采集表；保留 event/frame/估计坐标/σ/时间与跳过原因，真值列留空并可选写选择摘要 | 模板标记 `contains_truth=false`、`gate_passed=false`，JSON envelope 有严格 schema，不进入实时、标定或飞控路径；没有 fresh GPS 时命令返回 `1`，防止空表被误当成现场证据 |
| `src/multidetect/fixed_wing_acceptance.py` + `scripts/run_fixed_wing_software_acceptance.py` | 生产目标池、Mode-3 确认和固定翼姿态执行器的确定性 software/HIL-preflight gate；记录 DET→TRK→LCK→TGT、限幅/slew、OFFBOARD、RC 取消与失锁恢复 | 内存 transport；不连接 Pixhawk、不发送真实 MAVLink、不替代真实 HIL/REAL |
| `ground-control/MultiDetectGCS/custom/src/FlyViewVideo.qml` | 上游 `PipState`/`FlightDisplayViewVideo` 内的 Multi-Detect 增量 overlay；PiP 只读、full view 交互 | 不创建第二视频 surface、`Window`/`ApplicationWindow`、独立 simulator UI 或自定义主题 |
| `ground-control/MultiDetectGCS/custom/src/FlyViewCustomLayer.qml` | 使用 QGC palette/controls/tool insets 组合任务信息 | 不复制上游 toolbar/map/PiP，不引入私有视觉系统 |

原生 UI hard constraint 对 REPLAY/SIM/HIL/REAL 一致：只使用 QGC `FlyView`、`PipState`、
`FlightDisplayViewVideo`、`QGCPalette`、QGC controls 与 `ScreenTools`。Computer Use、SendInput 或桌面
截图只允许操作/证明 exact PID/HWND 的同一个原生窗口，不能生成替代验收 UI。

exact-client capture 的 ownership 链为 exact QGC PID → owner HWND → Per-Monitor V2 → physical client
rect → one-monitor/occlusion/25-point `WindowFromPoint` checks → `CopyFromScreen` exact rect → 状态恢复。
visible-video validator 的硬门是双帧 nonblack/nonuniform video ROI + RTSP streaming/decoding + native
type 24 sequence advance；它最多声明 `transport_bound_visible_video_surface_proved`。

当前验收边界保持：

```text
pre8_windows_release_and_hashes=VERIFIED
pre8_combined_vtol_mission_run=VERIFIED
pre7_combined_vtol_mission_run=VERIFIED (historical)
exact_source_frame_identity_proved=false
qml_rendering_proved=false
operator_visual_acceptance=not_claimed
SIM_complete=false
HIL_complete=false
REAL_complete=false
outdoor_hardware_gnss_complete=false
```

这些门全部闭合前，版本继续使用 `v0.3.0-pre.N`，不能发布最终 `v0.3.0`。
