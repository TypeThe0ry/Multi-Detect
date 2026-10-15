# Multi-Detect Autonomy 与同核模拟器架构

当前源码候选为 `v0.3.0-pre.8`：release label `0.3.0-pre.8`、Python `0.3.0rc8`、
CMake/PE numeric `0.3.0`。最终 `v0.3.0` 尚未发布。

## 1. 产品目标

`Multi-Detect Autonomy` 将现有机载感知、目标跟踪、多源测距、GPS 条件化落图、
MultiDetectGCS 证据界面和有界固定翼目标观察能力，扩展为固定翼与垂直起降固定翼共用的
任务级自主系统。

系统的控制边界固定为：

```text
人工 RC / 模式拨杆
>
PX4 failsafe
>
Jetson Safety Supervisor
>
Mission Executive
>
Task Compiler / 语言模型建议
```

语言模型只能把操作者输入编译为严格的 `ResolvedTaskSpec`。航线几何、能量、地形、
地理围栏、飞行包线、起降、VTOL 转换和 MAVLink Mission 项必须由确定性代码产生并验证。
语言模型不得输出或触发任意 MAVLink、PWM、舵面、电机、姿态目标、解锁或模式切换。

## 2. 同核模拟器硬约束

```text
Same UI
Same Algorithms / Core
Same Contracts
Same Safety
Same Mission Path
Only Runtime Backend / World Changes
```

`SIM` 不是独立 Demo。`REPLAY`、`SIM`、`HIL` 和 `REAL` 必须复用：

- 同一个 MultiDetectGCS 应用和同一个 Fly/Plan UI；
- 同一组任务契约、canonical hash 和授权绑定；
- 同一个 Task Compiler、固定翼/VTOL Planner、Safety Supervisor 和 Mission Executive；
- 同一套 Detector、Tracker、Ranging、Target Geolocation 和 Evidence 链路；
- 同一个 PX4 Mission transaction 路径；
- 同一种审计事件和 Episode Bundle 格式。

允许变化的只有 `RuntimeBackend` 及其承载的 `Clock`、传感器来源、世界/动力学、场景资产和
故障源。Detector、Tracker、Ranging、Geolocation、Planner、Safety 和 Mission 算法不随运行模式
切换；否则即使 UI 相同，也不能称为同核。
任何 `if mode == SIM: skip_safety`、`SimPlanner`、`SimMissionController` 或 `FlyViewSim.qml`
都属于架构违规。

### 2.1 QGC 原生 UI 约束与 pre.3 视觉证据

Multi-Detect 不创建仿真专用的第二套界面。视频和地图始终进入 QGC 原有的
`FlightDisplayViewVideo`、`FlyView`、`PipState`、toolbar、map PiP 和 `ScreenTools` 布局；
Multi-Detect 目标/深度/运行状态叠加只使用 QGC `QGCLabel`、`QGCButton`、`QGCPalette` 等原生
控件。pre.3 的真实画面在 `build/e4-qgc-visual-20260825-224648/`，通过独立临时 QGC profile
设置 `QGCQml/MainFlyWindowIsMap=false` 让视频直接成为同一个 Fly View 的主面板；这不是新的
QML view，也不改变产品启动默认值。该画面与签名 RTSP/metadata barrier 的结果分别取证，
机器 `operator_visual_acceptance` 字段继续保持 `not_claimed` 以避免自动流程越权宣称人工验收。

#### 2.1.1 v55 PX4-reported position-response 子门

v55 没有新增模拟器专用 UI 或第二套控制器。一次性 x500 PX4/Gazebo 容器中的合成目标只经公开
perception seam 进入生产 `SimulatorTelemetryBridge`，再沿共享 `SimulatorAiExecutor`、authority、
watchdog 和 `SimulatorMavlinkControlAdapter` 产生 loopback MAVLink `MANUAL_CONTROL`；响应只从 PX4
`GLOBAL_POSITION_INT` 重新计算 WGS-84 位移。该路径不读取 Gazebo truth，也不调用 `set_pose`、teleport
或其它直接改写机体状态的接口。

该 opt-in 子门要求 `--execute --exercise-arm --exercise-target`，并额外给出
`--exercise-flight-seconds 1..30` 和 `--minimum-flight-displacement-m`。它只在本次 disposable SITL
内设置 `COM_RC_IN_MODE=1`，通过同一 MAVLink socket 请求 `POSCTL`，并持续确认 heartbeat 仍为 armed
POSCTL。第一个 accepted TRACK 才锁定最新位置基线；其后目标刷新、heartbeat 连续性、当前模式、AI/
target rejection、failsafe/watchdog 和位移门都必须同时合格，任何后续模式丢失都会撤销通过状态。

当前 canonical evidence 为
`artifacts/evaluation/terminal-continuation-v55/terrain-ai-flight-response-v55-pre8-v4.json`
（661,936 bytes，SHA-256
`B56F20AB40B3E8AF9DF169FAAED9D4F326FFCDFDD052ED0FC6FF804FD7C44073`）：8.172 s 内 68 次目标刷新、
0 拒绝、最大刷新间隔 0.485 s，557 次 AI/control step 无 rejection/failsafe/watchdog，171 个
`GLOBAL_POSITION_INT` 样本给出最大水平位移 0.818282 m（门限 0.5 m），模式丢失为 0，DISARM 和
container stop/rm 均完成。它只关闭单个 multirotor 的 headless software-SIM position-response
machine 子门；没有经过 camera → detector → ranging → geolocation → evidence card → Fly View，也不证明
actuator telemetry、airborne flight、fixed-wing/VTOL 动态对等、人工视觉、硬件、室外 GNSS、HIL 或 REAL。
当前代码全量回归为 2,907 tests：`2906 passed, 1 skipped, 0 failed, 0 errors`；JUnit 411,702 bytes、
SHA-256 `A2C2595C49D0B663468D09274D22976CC1B4D9128425BB23458445B1C5397F51`，唯一跳过为 Windows symlink
权限项。

### 2.2 profile-bound HIL/REAL launcher（pre.6 引入，pre.8 当前）

`src/multidetect/runtime_launcher.py` 将 Pixhawk heartbeat identity、可选的隐私保护 firmware identity、
airframe profile hash、resolved endpoint binding 和 REAL 的 MAVLink signing identity 绑定到同一个签名
`RuntimeProfile`。launcher 先用只读 provider 做 probe，再校验 profile/HMAC/身份；当前 provision 生成的
REAL profile 带五项绑定，且只有它们和显式 output opt-in 全部通过时才创建
`ProfileBoundFlightControlProvider`。该 provider 在每次重新建立合格连接前重新计算绑定，
endpoint、vehicle 或已绑定 firmware 变化会立即锁死输出。v47 进一步把 legacy profile 限定为无输出
诊断用途：实际 launcher 总是要求 firmware binding，REAL output opt-in 也自动触发相同要求，底层 flight
provider 构造器拒绝 firmwareless profile。legacy 文件不能通过 v46 的 Mission-feedback E7，也不能
成为 REAL 输出旁路。

HIL 的 config 必须带 `scenario_id`、scenario manifest SHA-256 与非负 seed，且强制
`output_locked=true`；HIL 不能携带 signing identity，也不能开启 flight-control output。CLI
profile-bound 选项若没有 `--runtime-mode hil|real` 会直接失败；启用模式时必须同时给出
profile、HMAC key env、key id、airframe hash 和 Pixhawk endpoint。REAL signing key 只从环境变量
读取后在本地计算 SHA-256，原始 key 不进入参数、日志或 runtime descriptor。任何 launcher 异常
都会关闭刚创建的 frame source 与 Pixhawk provider。

这条门不改变 QGC 表面：HIL/REAL 仍进入同一个原生 `FlyView`、`FlyViewVideo`、`PipState`、
toolbar 与 map PiP。自定义 Plan/Select QML 不再引入未使用的 `QtQuick.Controls`，而
`FlyViewCustomLayer.qml` 只为原生 `Dialog` 枚举保留该导入。CLI、launcher 与 QGC 原生风格静态
测试共同覆盖这组不变量。

### 2.3 实际连接独立 live attestation（v48）

静态 profile 校验只说明“这份配置可以申请对应能力”，不能说明随后新建的发送连接仍是 probe 看到的
同一飞控。v48 因此要求实际 `ProfileBoundFlightControlProvider` 从锁定状态开始，使用自己的 receive-only
数据重新证明：transport connected、新鲜 heartbeat、合格 PX4 车辆身份、非未来且同源的完整
`AUTOPILOT_VERSION`，以及 vehicle/firmware/airframe/endpoint/signing 全绑定相等。输出函数只能消费已经
建立的 attestation，不能自行建立它。

该状态是动态安全输入，而不是启动常量：heartbeat freshness 丢失、稳定身份变化、固件错配或关闭连接
都会撤销认证并清除缓存版本；同 source 重连仍需新版本帧。固定翼姿态执行器、Mode-3 approach 状态、
CLI 启动/审计输出都引用同一动态状态。未 attested 时，控制消息、模式消息以及会诱发远端流量的测距/RC
stream request 均保持阻断。SIM/HIL/REAL 仍复用既有 QGC UI、任务契约、安全策略和 PX4 Mission 路径；
v48 没有新增模拟器专用控制分支。

### 2.4 模式 3 UI 消费同一动态权限（v49）

QGC 不从目标状态单独推断“正在瞄准”。`APPROACH_STATUS` 中的动态
`flightControlEnabled` 映射为 `fixedWingAimControlEnabled`，而 UI 的 active 派生值同时要求 approach active
和该权限为 true。目标仍为 LCK 但 provider 未 attested、heartbeat 已过期或身份发生变化时，Fly View
进入“飞控输出锁定”分支，只展示姿态建议；stale/abort/cancel 也分别显示不会发送控制的状态。

该变化只修正同一原生 Fly View 的状态解释，不创建新的 SIM 页面、不改变任务契约，也不绕过 v48 的
provider 输出门。机器测试和 offscreen boot 可以证明 QML 构建/初始化与 fail-closed 字符串契约，人工
视觉验收继续为 `not_claimed`。

### 2.5 三轴相机安装误差共核传播（v50）

Ranging 核心把 `boresight_sigma_deg` 定义为安装 pitch、yaw、roll 三个独立测量轴各自的共同 `1σ`，
分别生成正负 calibrated ray。三轴端点同时用于 camera-ground、direct-range ground projection 和
absolute-bearing 误差；其中任何一个端点不能安全落地时，整个结果 fail-closed。这样固定翼滚转、俯仰及
离轴目标不会因只考虑 mount pitch 而发布过窄误差圆。

该实现位于共享 `multimodal_ranging.py`，不读取 runtime mode。REPLAY、SIM、HIL 与 REAL 只提供不同
来源的相机、姿态和距离样本，算法、目标证据、QGC 地图和安全边界保持一致。

## 3. Ground Truth 隔离

Gazebo 或其他仿真器的真实目标位置、无噪声飞机状态和预期答案只允许进入独立 evaluator：

```text
Simulator World
├── sensor channel     -> RuntimeBackend -> Multi-Detect Core
└── ground-truth channel -> ScenarioEvaluator
```

`ScenarioManifest` 加载后必须拆成不同类型：

- `ScenarioRuntimeInput`：传给仿真后端的世界、飞机、传感器、天气和故障输入；
- `ScenarioTruth`：只传给 evaluator；
- `ScenarioExpectations`：只用于验收判定。

`RuntimeBackend` 和任务核心的接口不得暴露 `truth`、真实目标坐标或 expected answer。
正式端到端 SIM 必须使用仿真相机画面经过真实感知、测距和定位链路，不能把目标标签或坐标
直接注入任务状态机。

### 3.1 当前回放边界与目标回放边界

`v0.3.0-pre.1` 的兼容 `ReplayBackend` 明确等同于
`ObservationReplayBackend`。记录项是已经包含 Detection 与 `VehicleTelemetry` 的
`FrameObservation`，因此它只覆盖 detection 之后的任务核心：

```text
Replay JSONL
-> FrameObservation(detections + normalized telemetry)
-> ObservationReplayBackend
-> MissionController / contracts / state machine / audit
```

它不经过 `FrameSource`、production detector 或完整 `LiveMissionRunner`，所以不能作为
same-detector、raw-camera 或完整 UI launcher 已验收的证据。

`v0.3.0-pre.2` 在 pre-detection 边界新增 `RawSensorReplayBackend`：

```text
raw-sensor manifest(started_at_utc + recording/event/image hashes)
-> image references + normalized read-only VehicleTelemetry
-> RawSensorReplayBackend(ManualClock)
-> FrameSource + TelemetryProvider
-> shared build_production_perception(...) factory
-> LiveMissionRunner.from_runtime_backend(...)
-> complete-recording gate + audit/result/run identity
```

raw manifest 必须用 RFC 3339 UTC 的 `started_at_utc` 锚定回放时间域，并绑定 event
stream、每个 image reference、recording identity、Airframe Profile 与可选 Scenario 身份的
SHA-256/字节数/顺序。路径必须为 manifest 目录内的相对路径；穿越、绝对路径、drive
path 或 symlink escape 均失败闭锁。记录中不允许 Detection、truth、expectations 或
未知字段，因此 backend 无法从场景答案绕过 production perception 边界。

最终同核集成路径必须是：

```text
recorded pixels / Gazebo camera / real camera
-> FrameSource
-> shared production Detector
-> LiveMissionRunner <- normalized read-only TelemetryProvider
-> FrameObservation
-> shared MissionController / tracking / ranging / geolocation / safety
```

新 `RawSensorRuntimeBackend` 只暴露 `RuntimeDescriptor`、`Clock`、`FrameSource` 和只读
`TelemetryProvider`。Detector、预计算 Detection、Ground Truth 和 expectations 都不属于 world
backend。`LiveMissionRunner.from_runtime_backend(...)` 使 backend 独占绑定上述三个 runtime
组件；确定性 descriptor 若未携带 `ManualClock` 将被拒绝。成功读帧后 frame source 将
共享 clock 推进到 capture timestamp，telemetry provider 只返回该时刻之前最新且
未过期的样本，UTC 始终为 `started_at_utc + monotonic`。

该决定性范围限于已绑定的 runtime/core 时间读取。Replay 的默认独立静态
performance clock 会返回 `0 ms` capture/inference latency；这是为了避免性能计时推进业务
clock 的确定性占位，不得当作 benchmark。可选异步 metric-depth、semantic context、ReID
组件仍可能使用自身 OS clock/thread scheduling；在统一 clock 注入与确定性
executor/barrier 前，不声称启用它们的全链已严格 bit-for-bit。

headless `integrated-replay` 已将上述链路绑定为软件 `E2`。它与 `live-camera`
共用 `build_production_perception(...)`，所以主 detector 和可选 verifier/safety/priority/
environment detector 使用同一构建、manifest role gate 和 model-set identity 逻辑。launcher
强制校验每个已配置模型、禁止 Pixhawk lifecycle/display/flight-control executor，并要求
`processed_frames == verified frame_count`，因此不能把前缀处理冒充完整录制验收。

run identity canonical 绑定 RuntimeDescriptor hash、recording hash、mission config SHA-256 和
perception model-set SHA-256；session 内的 `RuntimeStatusMessage`、mission audit 与结构化
`LiveRunResult` 都带有该运行身份语义。headless 产生 status 对象不等于已完成 QGC
签名传输验收。为了不把 OS thread scheduling 写成确定性，当前 launcher 明确
禁止 asynchronous semantic-context runner。这个 headless `E2` 仍不包含同步 QGC RTSP/Fly UI；
后者仍必须作为独立 `E4 INTEGRATED` 场景验收。

## 4. 五个系统平面

```text
Mission Authoring Plane / MultiDetectGCS
    自然语言、地图消歧、计划预览、确认、取消、证据、审计
                     |
Autonomy Plane / Jetson
    Task Compiler、Context、Planner、Energy、Safety、Executive、Contingency
             |                              |
Perception Plane                     Flight Integration Plane
    Detect/Track/Range/Geo              Airframe/PX4/Mission transaction
             |                              |
             +---------- RuntimeBackend ----+
                         |
Digital Twin Plane
    Replay / PX4 SITL + Gazebo / HIL / REAL / Scenario / Evaluator
```

## 5. 任务工件链

任何任务都必须经过不可变、可 hash 的中间工件：

```text
PromptEnvelope
-> ContextSnapshot
-> ResolvedTaskSpec | ClarificationRequired
-> PlatformNeutralPlan
-> ExecutableMission
-> ValidationReport
-> OperatorGrant
-> MissionTransaction
-> DurableMissionJournal checkpoint
-> EpisodeBundle
```

授权至少绑定 prompt、context、task、platform-neutral plan、executable mission、validation
report、airframe profile、vehicle identity、geofence 和 runtime descriptor 的 SHA-256。这样 SIM
授权不能跨模式复用于 REAL。上传 PX4 后必须重新下载 canonical mission items，只有回读 hash
与已批准 executable mission hash 完全一致时才能 commit。上传成功不等于允许启动。

### 5.1 确定性 Platform-Neutral Planner（pre.8）

`src/multidetect/autonomy/planner.py` 现在提供唯一的任务规划入口
`plan_task(task, context, vehicle_class)`，把已经消歧的 `ResolvedTaskSpec` 编译成
`PlatformNeutralPlan`。四类任务的主动作固定映射为：

```text
AREA_SEARCH             -> SEARCH_AREA
CORRIDOR_VISUAL_SEARCH  -> SEARCH_CORRIDOR
TARGET_OBSERVATION      -> OBSERVE_TARGET
SURVEY                  -> SURVEY
```

每个计划都按 `TAKEOFF -> TRANSIT -> 主任务 -> on_detection -> RETURN/LAND` 生成连续的
零基序列；目标观察、证据采集、定位上报和 revisit 是唯一允许进入 `on_detection` 的反应动作。
`PlatformNeutralPlan` 的 `plan_id`、task hash、context hash 和步骤不携带 vehicle-specific
分支，因此同一份 task/context 在 VTOL、fixed-wing、multirotor 上的计划可以逐字节比较，
再交给各自的 PX4 Mission compiler 处理起降、转换、转弯半径和进近包线。

Planner 在构建步骤前先 fail-closed 检查：任务是否仍有未解决假设、几何解析置信度、
`GPS fix_type`/水平精度、地理围栏、相机/感知/地图/DEM 资源、返航电量和剩余续航。航段距离
使用 WGS-84 inverse，且取声明 `length_m` 与采样 centerline 长度的较大值，防止稀疏地图点
低估能源和时间。逆风按风速向量降低有效规划速度，VTOL 额外预算前后转换，最后用安全系数
同时约束任务最大时长和返航储备上方的可用续航。该模块只生成不可变计划和审计用估算，不
打开飞控、不发送 MAVLink，也不读取 Scenario truth。
主任务 `PlanStep.quantity` 与同一保守 task distance 保持一致，因此下游审计不会看到比安全
预算更短的搜索航段。

需要把任务送入现有 PX4 Mission compiler 时，调用
`multidetect.mission_planning.compile_planned_task_mission(...)`。它在同一次调用中保留
platform-neutral plan/estimate，再按 runtime geometry、safety policy 和 vehicle class 选择
现有 VTOL、fixed-wing 或 multirotor compiler，并校验 executable mission 已绑定该 plan hash。
返回值只携带 canonical artifacts，不创建 MAVLink 链路或发送控制输出；
`tests/test_mission_planning.py` 覆盖三机型和跨上下文绑定拒绝。

文件化 authoring 入口使用 `MissionCompileRuntime v1`：
`configs/schemas/autonomy-mission-compile-runtime.schema.json` 严格约束 runtime
`home_position`、机型 geometry 和 safety policy。`parse_mission_compile_runtime(...)` 先验证
schema、未知字段、truth 通道与机型一致性，再实例化对应的 geometry/policy；
`compile_planned_task_mission_from_runtime(...)` 随后调用同一个 typed seam。命令
`multidetect-mission-compile` 会从 task/context/runtime 三个 JSON 和 airframe SHA-256 生成
可由 `PlannedMissionCompilation.from_mapping()` 重载的完整 artifact，包含 plan、estimate、
executable mission 与 `MISSION_ITEM_INT` wire hash。该文件入口仍不创建网络连接、不上传 Mission，
所以可由 REPLAY、SIM、HIL 和 REAL 的 authoring 工具共用；真实硬件输出依旧只能由后续
profile-bound transaction/runtime 层显式授权。
该 seam 已纳入 `configs/release-parity.json` 的 `px4_mission_path` hash，planner 本身纳入
`core_algorithm` hash；因此后续 SIM/HIL/REAL 的计划编译必须与同一份源码 parity 一起审计。
v37 的 VTOL 文件入口 smoke 已在本地生成并通过重载：
`artifacts/evaluation/terminal-continuation-v37/mission-compile-smoke-20260904.json`（当前
manifest SHA-256 `BEA3FFBE5D42464534E366FF3BEEAB661EA474A6AEC9CB281D959E93E7ED0B1C`）。同一
源码的完整 Python 回归为 `2713 passed, 1 skipped`、退出码 `0`；日志 SHA-256 为
`7A02B4E7C4D10D5E7656FA3B91234A5766B5D0A8D0FBECA681902EF55A534BB8`，路径为
`artifacts/evaluation/terminal-continuation-v37/full-regression-v37-mission-compile-20260904-rerun4-summary.log`。
这些是 local
software-only authoring 证据，真实 GNSS、Pixhawk、HIL/REAL 和人工 Fly View 仍由发布门单独验收。

### 5.2 编译 Mission 的同核进度观测（pre.8 v38）

`src/multidetect/mission_progress.py` 把 `ExecutableMission` 和任意具有只读遥测字段的
`TelemetryLike` 绑定为 `MissionProgressSnapshot`。它不是第二个 Mission controller：只维护
一个零基游标和确定性证据，绝不生成 `MANUAL_CONTROL`、姿态、模式、解锁或任意 MAVLink。
`SimulatorClosedLoop(mission=...)`、`bind_mission()` 与 `start_mission()` 只是把该观察器挂在
已经存在的 `authority -> adapter -> MAVLink sink -> flight model` 路径上；HIL/REAL 可在相同
遥测字段适配后复用，不需要另写 SIM 算法或 UI。

推进规则是固定的：坐标项用共享 WGS-84 inverse 计算水平距离，用项内 `param2`/`param3` 或
统一半径与相对高度误差判定接受；`LOITER_TIME` 只有在接受圆内连续驻留满 `param1` 才完成；
`VTOL_TRANSITION` 只在观察到 `FW`/`MC` 状态时完成；RTL 必须回到任务首个坐标项推导的 home。
坐标项还必须通过全局 GPS 质量门（`fix_type>=3`、卫星数 `>=6`、水平/垂直精度上限、GPS
age `<=1.5s`），因此 stale/future/极点坐标会进入 `waiting_for_gps` 或构造阶段闭锁；链路
不健康或未解锁分别进入 `waiting_for_link` / `blocked`。快照把 mission canonical hash、已完成
序列、当前命令、距离、高度误差、驻留时间和门状态一起 hash，重载时会拒绝未知字段、越界序列
和篡改的进度比例。

契约 schema 为 `configs/schemas/mission-progress.schema.json`，回归位于
`tests/test_mission_progress.py`。这一步把 Planner -> PX4 Mission 的静态产物接到了可观察的
SIM tick 边界，但仍是 local software-only 证据；PX4 SITL 的真实 `MISSION_CURRENT` /
`MISSION_ITEM_REACHED`、HIL/REAL、室外 GNSS 和人工 Fly View 仍由各自发布门验收，不能由该
观察器单独升级为硬件完成。
v38 已将该模块与 schema 加入 `core_algorithm` parity；当前
`configs/release-parity.json` SHA-256 为
`A95880A967954CFD4F8F11A6C8C35B12C5F1AFB1E0A1FE5D656DBA31012A8CD0`，
`core_algorithm_hash=0f29c28f30c581f292fe9496ca5f14b7ee3b12bfd6ae72bc19300b82cf67031e`。

### 5.3 Scenario runner 的 Mission 观测绑定（pre.8 v39）

`SimulatorScenarioRunner` 现在允许构造时传入 `ExecutableMission`，或在第一帧前调用
`bind_mission()`；可选的 `MissionProgressConfig` 经过同一类型检查后交给
`SimulatorClosedLoop`。`start_mission()` 是显式的 `MAV_CMD_MISSION_START` 生命周期动作，runner
不会因为绑定了 authoring artifact 就自动启动、上传或改变 authority。`clear_mission()` 只移除
观察器，不触碰飞行模型和控制状态。

场景步骤映射与总 evidence 都携带同一份 `MissionProgressSnapshot`，因此 manifest/seed/clock
确定性和 mission canonical hash 在 runner、closed-loop、SITL 适配器和后续 HIL/REAL 复用时保持
可追溯。没有显式启动时快照为 `ready`；启动后仍受 armed、link、GPS freshness、WGS-84 距离、
高度、VTOL 状态和驻留门约束。新增 runner 回归只证明本地 software/SIM 接线，不能替代 PX4
`MISSION_CURRENT`/`MISSION_ITEM_REACHED`、真实 GNSS、人工 QGC 验收或最终发布门。

同一能力可由 `python -m multidetect.simulator scenario --mission <artifact.json>` 使用；输入既
可以是裸 `ExecutableMission`，也可以是 `PlannedMissionCompilation`。只有同时给出
`--start-mission` 才会通过既有 sink 记录 Mission start，省略该开关的运行只生成 `ready` 观测。

### 5.4 3-D scene stream 的 Mission 进度绑定（pre.8 v40）

启用 `SimulatorScenarioRunnerConfig(record_scene_stream=True)` 后，
`SimulatorSceneSnapshot` 会把同一 tick 的 `MissionProgressSnapshot` 作为可选字段写入场景流。
该字段不是第二套状态机：它直接复用 closed-loop 的 snapshot 和 Mission canonical hash，因而
3-D renderer/headless replay 与 runner evidence 的序列、GPS/link freshness、WGS-84 距离、驻留和
安全门状态一致。未绑定 Mission 的历史快照省略字段，保持旧 canonical hash；带字段的流在读取时
再次调用 `MissionProgressSnapshot.from_mapping()`，对未知字段、非法比例/序列和嵌入 truth 保持
fail-closed；整条 stream 还要求各帧 Mission ID/hash 一致，禁止任务混流或半缺帧。

`python -m multidetect.simulator.scene_state --mission <artifact.json>` 也支持直接导出带进度的
场景流；只有显式附加 `--start-mission` 才会沿既有 sink 记录 Mission start。对外
`simulator-scene-stream.schema.json` 内联 Mission progress 定义，所以验证不需要网络或外部 schema
解析。该能力仍是 software/SIM replay 子门，不能替代 PX4 `MISSION_CURRENT`、真实 GNSS/Pixhawk、
HIL/REAL、人工 Fly View 或最终 `v0.3.0` 发布门。

### 5.5 PX4 Mission wire feedback 绑定（pre.8 v41）

几何进度不能证明 PX4 实际正在执行同一份任务。`src/multidetect/px4_mission_feedback.py` 因此提供
独立的 receive-only `Px4MissionFeedbackTracker`：构造时绑定 `ExecutableMission`，运行时只接受
`MISSION_CURRENT` 和 `MISSION_ITEM_REACHED`，并把任务 ID/hash/item count、source system/component、
current/reached 历史、事件数量和接收时间写入 canonical snapshot。最大 freshness 为 `1.5 s`；
来源变化、序列回退、越界项、reached 超前、接收时间倒退和 stale 数据都不合格。解析器与
`px4-mission-feedback.schema.json` 会重新验证派生 phase/reason/qualified 和 snapshot SHA-256。

`PixhawkReadOnlyTelemetryProvider` 将 current/reached 的缓存与 HEARTBEAT 分开计时。它可以显式注入
该 tracker，读取完成后通过 `mission_feedback_snapshot(now_s=...)` 产生与当前决策时刻一致的证据；
generic telemetry 游标超过 provider stale timeout 后返回 `None`。`MissionProgressTracker.update()`
接受同 Mission、同 `now_s` 的可选 feedback；`require_px4_feedback=True` 时，已启动任务在反馈缺失或
不合格时保持 blocked。wire feedback 是佐证门，不是控制器，也不能用 `MISSION_CURRENT` 跳过 GPS、
位置、接受半径、驻留或 lifecycle 检查。

现场入口 `px4_mission_feedback_cli.py` 直接复用该 tracker/provider，并使用共享 Mission loader 接受裸
`ExecutableMission`、`CompiledPx4Mission` 和完整 `PlannedMissionCompilation`。它在有界窗口内只接收
MAVLink，原子写入 `px4-mission-feedback-observation.schema.json` 报告；独立 verifier 重算 Mission
identity、feedback/evidence SHA-256、消息计数、read-only/zero-transmit 状态和 gate reasons。该报告的
硬件、HIL、REAL、发布和人工视觉 claims 固定为 false，因此通过 feedback 子门也不会越权关闭总门。

旧 MissionProgress/scene-stream 快照省略 `px4_feedback`，canonical hash 不变；旧 raw/post-detection
replay 也只有在实际带有新时间戳时才序列化这些字段。因此 v41 能重开历史 replay-set 而无需重写
旧证据。该能力已纳入 `core_algorithm` 和 `px4_mission_path` parity，但当前验证仍来自 fake MAVLink
与本地回归；真实 PX4 Mission 执行、HIL/REAL、室外 GNSS 和人工 Fly View 必须现场另行通过。

### 5.6 HIL/REAL 三机型 feedback 发布矩阵（pre.8 v42）

单份合格 wire snapshot 只证明一个 runtime、一个机型、一个 Mission 和一个 MAVLink 来源的有限时间
窗口。E7 因此在原 feedback tracker 外增加聚合层，但不增加控制路径：

```text
HIL  fixed-wing  ─ Mission + feedback report ┐
HIL  multirotor  ─ Mission + feedback report │
HIL  VTOL        ─ Mission + feedback report │
REAL fixed-wing  ─ Mission + feedback report ├─ px4_mission_feedback ─ E7
REAL multirotor  ─ Mission + feedback report │
REAL VTOL        ─ Mission + feedback report ┘
                                      │
                                      └─ current four-mode px4_mission_path_hash
```

`px4_mission_feedback_binding.py` 对每个叶节点调用共享 Mission loader 和严格观测报告 verifier，再绑定
源文件 SHA-256/bytes、Mission/adapter/source/runtime identity、feedback hashes、read-only 和
zero-transmit。聚合结果本身不把 `hil_acceptance` 或 `real_acceptance` 置真；它只是最终发布需要的一个
子门。prerelease 可以没有该矩阵，最终 release 则必须完整、无重复且全部合格。当前 v42 E7 快照没有
真实六源，所以 `px4_mission_feedback` 明确 absent，HIL/REAL、硬件、室外 GNSS、人工 Fly View 和
最终 `v0.3.0` 继续 pending。

### 5.7 feedback 会话的签名运行身份（pre.8 v43）

v42 的 `Mission + report` 仍缺少“这一份反馈来自哪一个经过批准的运行配置和端点”的直接绑定。
v43 在不增加发送能力的前提下，把每个 E7 叶节点扩展为：

```text
HIL/REAL mode
  + vehicle class
  + ExecutableMission
  + signed RuntimeProfile document
  + receive-only feedback report v2
        │
        ├─ complete PX4 heartbeat -> vehicle_identity_hash
        ├─ resolved endpoint + baud -> endpoint_binding_hash
        ├─ Mission -> airframe_profile_hash
        └─ profile loader -> signature/identity/output-lock decision
```

`px4_mission_feedback_runtime.py` 直接复用 launcher 的 identity 派生和 RuntimeProfile loader。HIL 只接受
精确 `hil` profile，且 observed identity 不得携带 REAL signing hash；REAL 只接受精确 `flight` profile，
但观测器固定以 `allow_flight_control_output=false` 加载。因此合格 v2 报告必须同时具备签名有效、身份
匹配、PX4 heartbeat 完整、feedback source 与 heartbeat source 相同、Mission airframe 一致、输出关闭且
锁定。HMAC 和 REAL signing key 只从环境变量进入进程，报告只保存 key ID 与不可逆 hash。

`px4_mission_feedback_binding.py` schema v2 要求 HIL/REAL × 三机型六个
`MODE VEHICLE MISSION PROFILE REPORT` 五元组；六份 profile 和六份 report 必须各自不同。旧 observation
v1 reader 继续用于重验历史诊断，但 E7 会因缺少 runtime binding 明确拒绝。当前 v43 E7 未填入任何
虚构现场叶节点，故 `px4_mission_feedback.present=false`、总门仍关闭；saved SIM 六源的 parity
revalidation 仍只是 software-only 证据。

### 5.8 从实际 heartbeat provision 签名 profile（pre.8 v44）

v43 的 observer 能验证 profile，却不能解决首次生成 profile 所需 identity 的 bootstrap。v44 在同一
CLI 中加入一个严格的 receive-only provision 阶段：

```text
Mission adapter ───────────────→ vehicle class
Mission airframe_profile_hash ─→ airframe identity
fresh PX4 HEARTBEAT ───────────→ vehicle identity
resolved endpoint + baud ──────→ endpoint identity
environment-only keys ─────────→ signed HIL/flight RuntimeProfile
```

provisioner 只构造 `PixhawkReadOnlyTelemetryProvider`，并把 `request_ranging_streams` 固定为 false。
它要求 `messages_transmitted=0`、fresh heartbeat、PX4 autopilot、匹配的 system/component、Mission 对应的
MAV_TYPE 集合、有效 MAVLink version 和 `STANDBY/ACTIVE`；任何一项不成立都不写 profile。HIL profile
不能携带 signing identity，REAL flight profile 必须携带 signing identity hash。输出先通过生产
`load_runtime_profile_document()` 以同一 observed identity 重验，且必须保持 output locked，之后才原子
落盘。

这个阶段只建立后续 observer 的批准输入，不建立“设备通过”事实。真正的 v2 observation 仍会对新鲜
heartbeat、实际 endpoint、Mission、profile 文件 bytes/HMAC、feedback source 和 Mission progress 再做
一次独立绑定。因此 profile 文件被复制、端点被更换或 Pixhawk/airframe 发生变化时不会继承先前会话的
通过状态。

### 5.9 firmware-bound profile 与六叶 E7（pre.8 v46）

v46 将 v45 的 same-source `AUTOPILOT_VERSION` 身份接入 v44/v43 的 profile/observer 链。provision
必须在当前 probe 窗口内看到版本帧，不能继承进程启动前的缓存；它把规范 identity SHA-256 写入新签发
profile。observer 随后从实时 provider 独立派生 firmware hash，并要求与 profile 完全一致：

```text
same-source version frame in current provision window
  -> signed profile firmware_identity_hash
  -> live observation runtime firmware_identity_hash
  -> report leaf firmware_identity_hash
  -> all_firmware_identities_bound == true
```

泛用 schema/loader 允许旧 HIL/REAL profile 缺少该字段，用于重开历史诊断；E7 并不继承这项宽容。
profile 缺失、实时版本缺失或不一致都会产生独立 fail-closed reason，六叶 binding 还会重开 report，防止
只修改外层 release JSON 伪造固件绑定。UID 原值在整个链上都不会序列化。这一软件门已完成，真实 V6X
采集和 HIL/REAL 六机型会话仍待现场执行。

v46 完整回归为 `2816 passed, 1 skipped`（2,817 tests），当前 parity SHA-256 为
`3A85B02436CD545E1409B842D28AC3A600DC02749B5F4A5D2130A525D4A21F95`。E7 report-only/strict
分别返回 `0`/`2`，报告保持 `px4_mission_feedback.present=false`；因此测试闭合的是 software contract，
不是 V6X/HIL/REAL 验收。

### 5.10 REAL output firmware hard lock（pre.8 v47）

v47 关闭 generic loader 的 legacy flight output 缝隙，同时保留历史诊断：

```text
diagnostic read + no output opt-in
  -> legacy profile may remain readable, output locked

HIL/REAL launcher or REAL output opt-in
  -> require profile firmware hash
  -> require live observed firmware hash
  -> require exact match
  -> otherwise profile invalid and output locked
```

`ProfileBoundRuntimeLauncher` 无条件打开 strict firmware binding，REAL observer 仍只读；
`ProfileBoundFlightControlProvider` 自身也拒绝 firmwareless profile，并在每次输出连接上重新派生实时身份。
完整回归为 `2820 passed, 1 skipped`（2,821 tests），当前 parity SHA-256
`B40053E5EE74D09FA890E506BEACF7392288778AAE22F449016FC88D1563BBCF`，PX4 Mission path hash
`63674e1adedc5884561ffa62f6c6b4f35d1e4a067bb5114e29ab3a6d305917c5`。E7 report-only/strict 仍为
`0`/`2`，`px4_mission_feedback.present=false`，所以这里只完成 software fail-closed contract。

回归入口：

```powershell
$env:PYTHONPATH = 'src'
python -m pytest -q tests\test_autonomy_planner.py
```

也可以从两个严格 JSON 文档直接生成计划和独立估算（输出的 plan JSON 可继续交给现有
`compile_executable_mission`；plan 与 estimate 都能由契约层重新解析并拒绝未知字段）：

```powershell
$env:PYTHONPATH = 'src'
python -m multidetect.autonomy.planner_cli `
  --task .\task.json `
  --context .\context.json `
  --vehicle-class vtol `
  --out .\plan.json `
  --estimate-out .\plan-estimate.json
```

这组测试覆盖四类任务、三种机型的 byte-identical 计划、WGS-84 路程/逆风预算、别名归一化、
GPS/资源/围栏/能源/时长门和非法检测动作。完整 Dubins/覆盖搜索、VTOL 转换轨迹、多旋翼
姿态控制和真实 PX4/Gazebo runtime 仍属于后续外部验收门，不能由这个 authoring-side planner
单独宣称完成。

## 6. 运行模式

| 模式 | 数据/飞行来源 | Ground Truth | 硬件输出 | 当前 `v0.3.0-pre.8` 候选状态 |
| --- | --- | --- | --- | --- |
| `REPLAY` | 检测后 `FrameObservation`，或 raw image references + telemetry | 不进入核心 | 禁止 | pre.3 `E4 INTEGRATED` 历史证据与当前 pre.8 共享同一生产感知/任务边界；人工视觉仍单独记账 |
| `SIM` | PX4 SITL + Gazebo | evaluator-only | 禁止真实硬件输出；一次性 SITL opt-in 可发送仿真控制 | pre.8 的 900-frame combined machine gate 仍覆盖 QGC/Mission；v54 关闭 image/NavSat/3-D Odometry 与 DEM-derived AGL runtime 子门；v55 关闭单个 x500 的共享 AI/MAVLink → PX4-reported position-response 子门。camera→检测→测距→落图→Fly View 的同次坡地闭环、fixed-wing/VTOL 动态对等、人工视觉、HIL/REAL/室外 GNSS 仍 pending |
| `HIL` | 真实 Pixhawk + 仿真世界/传感器 | evaluator-only | 由明确硬件 profile 限定 | 尚未完成 |
| `REAL` | 真实 Pixhawk、相机和飞机 | 不存在 | 按签名 Flight profile 限定 | 尚未进入 v0.3.0 实飞验收 |

缺少或过期的签名运行状态必须显示 `UNKNOWN`；本地期望模式与签名远端模式不同必须显示
`MISMATCH`。两种状态都不得静默回退为 `REAL`。

### 6.0.1 HIL/REAL RuntimeProfile 文件边界

HIL/REAL 启动器通过 `src/multidetect/runtime_profiles.py` 读取版本化
`runtime-profile-document`，而不是在 QGC 或 UI 层拼接运行身份。文档的 `profile` 仍严格服从
`configs/schemas/runtime-profile.schema.json`；文档可带 `key_id` 与
`signature_hmac_sha256`，签名覆盖 `schema_version`、`key_id` 和完整 `profile` 映射。

- `HIL` 必须使用签名且绑定 `vehicle_identity_hash`、`airframe_profile_hash` 和
  `endpoint_binding_hash`；v46 provision 还会绑定 `firmware_identity_hash`，v47 launcher 将该字段设为
  强制项。即使签名和设备身份都匹配，加载结果仍固定为
  `flight_control_enabled=false`、`output_locked=true`，只能进入同核算法和任务状态路径。
- `REAL` 的 `observer` profile 只允许签名的只读观察；要暴露飞控输出，必须是带四项身份绑定的
  `flight` profile；v46 provision 生成的新 profile 额外绑定 firmware hash，v47 要求所有实际输出请求都
  同时通过签名、设备/端点/固件 hash 比对和调用方显式 `allow_flight_control_output` opt-in。任何一项失败
  都返回锁定结果；legacy profile 只可在无输出诊断路径读取。
- loader 只返回结构化 `RuntimeProfileLoadResult`，不创建链路、不打开端口、不发送 MAVLink，
  因此 UI 继续从同一个签名 `RuntimeStatus` 显示 `HIL`/`REAL`，不会出现 simulator-only 页面或
  第二套主题。
- v43 的 PX4 feedback observer 使用同一个 loader，但总是关闭 output opt-in；它把 profile 文档、
  heartbeat vehicle、endpoint、Mission airframe 和 locked decision 写入 observation v2，供 E7 重新
  打开来源文件。该证据不会反向给 launcher 或 QGC 增加控制权限。
- v44 的 `px4_mission_feedback_cli.py provision` 只负责从当前 receive-only heartbeat/endpoint 建立一份
  经过签名和临时文件重验的 profile；它不启动 observer、Mission 或 launcher，不产生验收 claim。
- v46 的 provision 还要求 `AUTOPILOT_VERSION` 在本次 probe 窗口内从 heartbeat 同一 source 到达，并把
  sanitized firmware hash 写进 profile。generic legacy profile 仍可读取，但 PX4 feedback E7 强制要求
  profile、live observation 和六叶 release binding 三侧固件身份一致。
- v47 将 launcher 与直接 REAL output load 都设为 firmware-strict，并在 flight provider 构造边界再次
  检查；因此 generic legacy readability 不会赋予控制权限。

### 6.1 证据等级与当前最高等级

| 等级 | 需要的证据 | 本开发线当前含义 |
|---|---|---|
| `E0 DESIGN` | 架构、Schema、验收门 | 后续 SIM/HIL/REAL 目标可处于此等级 |
| `E1 SOURCE` | 当前工作树中的实现与配置 | pre.8 profile-bound launcher、WGS-84 定位核心、原生 QGC 风格约束和 Mission/capture 源码均已进入工作树；这不代表测试、构建或运行成功 |
| `E2 TESTED` | 可重复的单元、静态和契约测试 | pre.8 项目/QGC 回归、compileall、Ruff、PowerShell AST 与 diff check 已实跑通过；精确数量以当前回归日志为准 |
| `E3 BUILT` | 原生制品路径、大小、SHA-256、自检和 portable 启动冒烟 | 当前 pre.8 portable 主 EXE `46,576,128` bytes，SHA-256 `06E7B1BBEC1B96DC6E5A231B12B88725122476075ED86A24BBEF554CB899E5FE`；Release/portable 字节一致，headless 启动冒烟退出码 `0`。v55 只改 Python acceptance、测试与文档，没有重新链接或改变该 EXE。2026-09-02 历史全量 Python 回归为 `2449 passed`、退出码 `0`；补齐 CPU `onnxruntime` 后已无 skip，原始日志 `artifacts/evaluation/pytest-full-20260902-onnxruntime.log` 的 SHA-256 为 `4F5360DEC8E64B9C8B71CDF9BEF3ACEF7FB8C7FE87C7CD4FF7361B4B2C296012`。严格 goal-acceptance 快照 `artifacts/evaluation/multidetect-goal-acceptance-20260902-onnxruntime-credentials.json`（31,175 bytes，SHA-256 `F17D535362B2F18351178A7F9865766A3EAA3D015AC3392C2C437129C0750A52`）确认 operator credentials `ready=true` 且 `gate_overridden=false`；测试通过不等同于硬件/视觉证据。QA22 工作树 snapshot（HEAD `7f6e6aeb7c00902fb8084bd6e96a0250df685e67`，295 dirty 条目，status SHA `851a1abde3f79abd859ad032242cee0ee9bfd790663f615c3fe73b6b67d97c74`），不是干净提交发布包 |
| `E4 INTEGRATED` | 同 UI、同算法、同 Mission 路径的 REPLAY/SIM 端到端场景 | 2026-08-31/09-01 combined SIM/QGC/Mission machine gate `gate_passed=true`；同一次 run 通过 900 帧 integrated SIM/RTSP、VTOL Mission、原生 QGC QQuickItem video/PiP/fixed-wing 抓帧和 toolbar 抽屉机器 smoke。v54 又关闭真实加载非平地 world 后的 image/NavSat/3-D Odometry 与 DEM AGL runtime 子门；v55 独立关闭单个 x500 的共享 AI/MAVLink → PX4-reported position-response 子门。二者尚未合并为同一次 camera→ranging→geolocation→evidence card→Fly View 坡地场景，fixed-wing/VTOL 动态对等也未关闭。人工 Fly View/HIL/REAL/室外 GNSS 不由机器门自动完成，`operator_visual_acceptance=not_claimed`、`qml_rendering_proved=false` |
| `E5 HIL` | 真实 Pixhawk/Jetson 硬件互锁台架或 HIL | 尚未完成 v0.3.0 门 |
| `E6 FLIGHT` | 受限真实飞行和外场传感器验收 | 尚未进入 v0.3.0 门 |
| `E7 RELEASE` | 本版本声明的全部发布门 | 最终 `v0.3.0` 尚未发布 |

当前权威的 SIM combined machine evidence（pre.8，2026-09-01，Docker Desktop 恢复后
重新执行 900 帧 Mission 余量）位于
`artifacts/evaluation/combined-sim-qgc-20260831T234111Z-53232/combined-evidence.json`
（SHA-256
`5194CC77F45572483B6A17A21FC8FC0DBE222BE62B9A8EB1CC14777C1B464F6C`）；其
`gate_passed=true`，完成 900 frames、传感器 helper `exit=0`、VTOL MC/FW/MC、7-item INT-only
Mission upload/readback/active、同核 RTSP/runtime-status ACK、容器清理、原生 QQuickItem
video/PiP/fixed-wing 抓帧和 toolbar 抽屉机器 smoke。它是 software-only disposable
PX4/Gazebo/SIM 证据，`operator_visual_acceptance=not_claimed`、`qml_rendering_proved=false`，
不覆盖真实 Pixhawk/Jetson/相机/室外 GNSS、执行器、HIL/REAL、实飞或生产检测准确率；最终
`v0.3.0` 仍保持阻塞。

该 run 已由 `release-mode-sim-20260901-pre.8-rerun.json`（SHA-256
`9862E26984487822DF9DCCB1D0673889C28ACD50DE1DFB02385AC829E11D516F`）做 SHA-256 绑定，并接入当前四模式
E7 清单 `artifacts/evaluation/release-gate-pre.8-current-20260901-field-rerun.json`（SHA-256
`A13D0223504221C986645F7EE73C6F3B15F8B1656687CE66421FCA8BD80F0A94`）。聚合报告
`artifacts/evaluation/release-gate-pre.8-current-20260901-field-rerun-report.json`（SHA-256
`746FBF1CD8D144D532E67B7AAF0E0E615A03B11397801DC190A88E5767F4AD43`）确认 parity 五项一致、
SIM runner source gate 为真；由于人工视觉、REAL/HIL、现场链路和 replay-set 仍未闭合，
报告保持 `gate_passed=false`、`release_allowed=false`。

历史可复用的 SIM combined machine evidence（pre.6 基线）位于
`artifacts/evaluation/combined-sim-qgc-20260826T125250Z-62292/combined-evidence.json`（309,023 bytes，
SHA-256 `7D6FCD1976242C85EF783E9D812D98475E2A723BAB396241801CACDDBCA0EDFC`）；它记录
600 frames、7-item VTOL Mission、MC/FW/MC、land/disarm、GPS/geofence 与原生 QGC surface。
pre.4 的 `combined-sim-qgc-20260826T081624Z-50796/combined-evidence.json` 继续作为历史记录保留。
历史 pre.3 机器可读 evidence 清单位于
`build/windows-v0.3.0-pre.3-release/verification/v0.3.0-pre.3-evidence.json`（SHA-256
`865E321F0F618220181BE45E2F7C7B48E467F47BC88A781DA5BA8BAE4B8F776B`）；pre.2 的历史 `E3`
清单位于
`build/windows-v0.3.0-pre.2-release/verification/v0.3.0-pre.2-evidence.json`，SHA-256 为
`D9D0EE425AEE6976A077B69B3EB8AD4C1C8F34FC69E1F41286AB28C9CCF5FC86`。该清单闭合的是
历史 Windows 原生构建证据；pre.3 的 E4 transport/画面证据位于
`build/e4-qgc-20260825-222239/` 与 `build/e4-qgc-visual-20260825-224648/`，不会上推为
Gazebo、HIL 或 REAL。

### 6.2 pre.3 signed PX4 SITL 软件门

`artifacts/evaluation/px4-sitl-qgc-operator-acceptance-20260825T190317Z-5732.json
SHA-256: `CE79F04FABC985D7BE38CC34EAF79EA526B0284C61466142F9DA0C316526E358`` 记录了同一
便携 QGC 与 pinned `px4io/px4-sitl@sha256:bab4270c4849b7027df4bd760c79d743d738c81d7830dde14c4cc5714f781216`
固定翼 SIH 的 software-only 集成。QGC exit `0`、PX4 autopilot heartbeat `5`、signed metadata
`37`、tracking `30 @ 20 Hz`、目标池 `3 tracks / 2 pages`、`SELECT_TRK` 和 challenge-bound
`APPROVE` 均完成；禁止 PX4 消息、file-mutating FTP、system-time、operator tunnel-to-PX4、
parameter/mission/flight/payload writes 均为 `0`，保护端口 `14550` 未改变，临时端口全部释放，
真实 V6X/Jetson/G20/GR01 未接触。operator metadata 使用严格 MAVLink 2 signing，不存在 unsigned
fallback。

UI 仍是 QGC 原生 Fly View：acceptance-only timer 位于 `custom/src/FlyViewCustomLayer.qml` 的
现有 layer，调用生产 `MultiDetectState`，并复用 `FlightDisplayViewVideo`、`PipState`、
`QGCLabel`、`QGCButton`、`QGCPalette`、`ScreenTools`；没有第二窗口或 simulator-only QML view。
机器 `operator_visual_acceptance` 依旧为 `not_claimed`，人工截图检查独立留证。这个 software SIM
门不等于 Gazebo 视觉闭环、完整 fixed-wing/VTOL Mission path、真实 HIL 或 REAL flight。
历史 pre.3 palette rebuild 的 native-QGC style smoke 位于
`build/e4-qgc-visual-20260826-024914/qgc-flyview-video-stable.png`；RTSP helper 提前断开，
因此它只证明当前 binary 的 QGC palette/surface 复用，不提升 E4 transport 或人工 `operator_visual_acceptance` 门。

### 6.3 Gazebo Harmonic 相机子门（pre.3）

`scripts/run_gazebo_sitl_camera_acceptance.ps1` 使用 digest 固定的
`px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`
启动 headless `gz sim` + PX4 `gz_bridge`，随后在容器内由
`scripts/gazebo_camera_capture.py` 订阅：

```text
Gazebo camera image  -> RGB frame hash / timestamp / PPM preview
Gazebo CameraInfo    -> calibration geometry
Gazebo NavSat        -> latitude / longitude / altitude
```

2026-08-25T19:35Z 的结果为 12/12 帧、`1280×960 RGB`、严格递增时间戳，camera-info 与 NavSat
消息计数均为 12；证据为
`artifacts/evaluation/gazebo-sitl-camera-acceptance-20260825T193511Z-3908.json`，SHA-256
`63EC2715ABDC54929DABE6975D1014693E0C69761394F63101B0803367E88C2D`。该路径不读取 Scenario
Ground Truth、不接触真实相机/Pixhawk/Jetson，也不产生硬件输出。它只提升 SIM 的传感器来源
证据；下面的 6.4 子门再把持久化 Gazebo image 接入 Windows QGC transport。

### 6.4 Gazebo image 到 QGC 同帧 transport 子门（pre.3）

`--persist-frames` 会把每一帧 Gazebo RGB payload 以哈希绑定的 PPM 落盘。随后
`scripts/create_gazebo_raw_replay_fixture.py` 只将这些像素转成 raw-only PNG recording，并携带
最新 NavSat 作为只读 `VehicleTelemetry`；它不引入检测输出、Scenario truth 或执行器通道。该
recording 再经 shared production perception、`LiveMissionRunner`、RTSP helper 和真实便携版
QGC 进程完成同帧 barrier：

```text
fixture: build/qgc-replay-fixture-gazebo-v0.3.0-pre.3/
manifest SHA-256: 87334899FB51950CE6BF2D82108BC5B7A43267B4538D8900CB0087B8F56421A4
result: build/e4-gazebo-qgc-20260826/e4.result.json
result SHA-256: 520B1BD6732E8FB35B1FE876E457A3C95111AB751C338287252E7E7ED3529926
```

机器结果为 12/12 video、12/12 metadata barrier、`complete=true`、最大 ACK→metadata 偏差
`0.0 ms`、RTSP helper exit `0`、EOS/summary 均收到，且 `ground_truth_isolated=true`、
`hardware_outputs_present=false`。这闭合了 **Gazebo image → shared detector/core → QGC RTSP/metadata
transport** 子门；`operator_visual_acceptance` 仍为 `not_claimed`，因此不把它上推为人工 Fly View
验收。完整 fixed-wing/VTOL SIM、真实 HIL、室外 GNSS/REAL 仍是后续门。


`artifacts/evaluation/px4-fixed-wing-sitl-auto-mission-acceptance-20260825T170332Z-62660.json`
另外闭合了 fixed-wing SIH 的 Mission transaction 子门：3 个 item 经 `MISSION_COUNT` 和
`MISSION_REQUEST_INT` readback 后，`items_match=true`、`hash_match=true`，canonical
upload/readback SHA-256 为
`14acad2d86fa047ae8af8388f7f792a018793fa65d30c9be62e425c33b484018`。PX4 的 TAKEOFF optional
pitch/NaN yaw 存储默认只在 hash canonicalization 层归一化；这不把软件 SIH 子门升级为
Gazebo visual、VTOL 或真实 HIL 证据。

一个等级的证据不能上推到下一等级。尤其是 `ObservationReplayBackend` 的 `E2` 只能证明
检测后任务核心可重复回归；headless raw-sensor/production perception/Live core 的 `E2` 只能证明
raw-only 输入、完整性、模型身份、runner 时间域契约与完整录制消费。它仍不是
同步 QGC RTSP/Fly UI 之外的 Gazebo、HIL 或实飞证据，也不是
Gazebo、HIL 或实飞证据。详细字段和当前构建记录见根 README 与
[`qgc-windows-release.md`](qgc-windows-release.md)。

### 6.7 benchmark 时钟与原生 QGC UI 约束

统一跟踪、短时图像跟踪和单目避障的 benchmark 同时记录 wall-clock 与 process-CPU clock。门禁取二者较小值作为 contention-adjusted algorithm cost，避免 Windows 桌面调度暂停把算法误判为超时；`*_wall_*`、`*_cpu_*`、`benchmark_effective_elapsed_s` 和 `scheduler_contention_ratio` 仍写入结果，便于单独审阅实际桌面负载。该测量隔离不改变运行时算法、GPS/测距资格或控制门。

模拟器和真实数据都进入同一个 QGC 原生 Fly View layer。视频、地图/PiP、toolbar、`QGCButton`、`QGCLabel`、`QGCPalette` 和 `ScreenTools` 不复制为 simulator-only 组件；人工画面点验完成前，证据 JSON 的 `operator_visual_acceptance` 必须保持 `not_claimed`。

## 7. 固定翼与 VTOL 适配

平台中立 primitive 初期只允许：

```text
TAKEOFF
TRANSIT
SEARCH_AREA
SEARCH_CORRIDOR
SURVEY
OBSERVE_TARGET
CAPTURE_EVIDENCE
REVISIT
RETURN
DIVERT
LAND
```

固定翼软件验收由 `src/multidetect/fixed_wing_acceptance.py` 提供确定性入口，并由
`scripts/run_fixed_wing_software_acceptance.py` 保存 JSON evidence。该入口不另造一套
算法：它直接串联生产 `UnifiedTargetPool`、`LiveApproachHilCoordinator`、
`FixedWingAimController` 和 `FixedWingAimExecutor`，验证 `DET → TRK → LCK → TGT`、
姿态限幅/slew、prestream/`OFFBOARD`、RC 接管和失锁恢复。

验收 transport 只记录 setpoint 与 mode request，不连接 Pixhawk、不发真实 MAVLink，故其
证据等级是 software/HIL-preflight，不等价于真实台架 HIL 或 REAL 飞行。UI 字段明确绑定
`native_qgc_flyview`；REPLAY/SIM/HIL/REAL 继续复用同一个上游 QGC `FlyView`、视频/PiP、
toolbar/map 和 QGC controls/palette，禁止第二窗口、第二视频 surface、独立 FixedWing HUD
或 simulator-only 主题。Computer Use 若可用，只在这个唯一原生窗口内进行人工点验，机器
证据不得把 `operator_visual_acceptance` 自动提升为已完成。

传统固定翼由 `Px4FixedWingAdapter` 编译为带最小转弯半径、风、地形、围栏、盘旋、进近和
备降约束的 Mission。当前 `FixedWingAimExecutor` 只保留为受限的
`OBSERVE_TARGET_FIXED_WING` 行为，不作为通用航线执行器。

VTOL 由多旋翼起降 primitive、固定翼巡航 planner 和显式转换状态机组合：

```text
VTOL_TAKEOFF
-> MC_CLIMB
-> FORWARD_TRANSITION
-> FW_TRANSIT/TASK/RETURN
-> BACK_TRANSITION
-> MC_APPROACH
-> VTOL_LAND
```

转换必须验证走廊、地形、高度、空速、风、升力系统健康和包含返航/转换/垂直降落的能量
余量；失败只能进入有限的确定性 fallback，禁止无限转换重试。

## 8. 室外目标定位第二代链路

SIM fallback flight model 与目标证据现在共用同一 WGS-84 direct geodesic：局部 NED 状态仍
用于确定性控制积分，但 telemetry endpoint 不再使用固定 `111,320 m/degree` 的
equirectangular 近似。公里级航线与下一代目标定位继续使用统一 WGS-84/ECEF/ENU-NED
geodesy，并明确区分 AMSL、AGL、relative-to-home、ellipsoid、DEM 与 local-NED 高度。

```text
frame timestamp
-> PoseBuffer 时间插值
-> 相机内参与 T_body_camera
-> pixel/segmentation -> camera ray
-> direct range / stereo / DEM / triangulation / metric depth
-> NED/ECEF/WGS-84
-> 完整 covariance
-> 多帧同 Track 融合
-> 坐标 + 误差椭圆 + Evidence
```

GPS、姿态、外参、像素、距离、DEM 或时间同步任一关键门不合格时输出正式的
`valid=false` 与 reason code，GCS 显示“不可定位”，不得生成假点。

## 9. `v0.3.0` 发布门

开发期版本统一为 `v0.3.0-pre.N`。只有 Replay、SIM、HIL 和 REAL 对应的既定验收门全部
通过，且同 UI、同核心、同契约、同安全规则和同 Mission 路径有可重复证据时，才能发布最终
`v0.3.0`。源码存在、单元测试通过、桌面程序构建、Gazebo 场景运行、HIL 和实飞是不同证据层，
不得互相替代。

`v0.3.0-pre.1` 是版本、运行模式、数据契约、场景隔离、ObservationReplay 和签名 UI 状态的
基础 prerelease。`v0.3.0-pre.2` 候选增加 raw-only Replay、UTC anchor、共享 `ManualClock`、
 production perception factory 与 headless `integrated-replay`；`v0.3.0-pre.3` 在此基础上
完成真实 QGC RTSP/签名 metadata barrier、原生 Fly View 画面 `E4` 和 fixed-wing SIH Mission
readback/hash 子门；`v0.3.0-pre.4` 增加 exact-client 可见视频表面验收链和完整 VTOL Mission
  contract/router/helper 源码；该段是 pre.4 源码阶段的历史记录。随后 600-frame combined
  Mission run 已在第 14 节封存并通过，但仍不承诺 HIL、REAL、室外硬件/GNSS 或完整泛化场景。
后续只递增 `pre.N`；任何字段仍为 PENDING 时都不得去掉 prerelease 后缀。

### 6.4 VTOL vehicle-dynamics SIM 子门（pre.3）

`artifacts/evaluation/px4-vtol-sitl-scenario-acceptance-20260826-final.json` 由 `scripts/run_px4_gazebo_vtol_scenario_acceptance.ps1` 生成。它使用 digest 固定的 `px4io/px4-sitl-gazebo`，通过单一映射 UDP 端口接收 PX4 MAVLink；容器 label、镜像 digest、无 host mount、非 privileged 和端口绑定均在启动后复核。

运行边界是：runtime 只得到 `load_runtime_scenario(..., RuntimeMode.SIM)` 的 `ScenarioRuntimeInput`；evaluator 在 runtime 绑定后才单独加载 truth/expectations。容器内只改 SITL health-check 参数，发送的 command 是 GCS heartbeat、ARM、两个 `MAV_CMD_DO_VTOL_TRANSITION` 和 DISARM，`flight_control_output_enabled=false`、`real_pixhawk_contacted=false`。

本轮通过的证据是 vehicle type `22`、四个 command ACK、`fixed_wing` 与 `multicopter` 两个 VTOL state、GPS `fix_type=3`/10 satellites/374 position samples、最大样本跳变 `0.011 m`、`false_geolocations=0`。scenario 中的 camera/battery fault 进入 evaluator timeline 并按 `0.01` 缩放，不声称已经给 `standard_vtol` 提供真实相机；相机像素与 QGC 同帧 transport 仍由 Gazebo camera fixture 子门覆盖。

所以该历史子门描述应与第 14 节合并阅读：VTOL vehicle-dynamics 与 pre.4 `river_rescue_vtol_001`
600-frame Mission machine gate 已通过，但 camera/perception/Fly View 人工验收、HIL、REAL 和室外
GNSS 仍 pending。QGC UI 仍严格复用原生 Fly View，不创建仿真专用窗口。

### 6.5 固定翼 QGC/PX4 续验与 UI 边界（pre.3）

固定翼 SIH 的 QGC/operator 验收再次通过：`artifacts/evaluation/px4-sitl-qgc-operator-acceptance-continue-20260826T090000Z.json`
（SHA-256 `8B4271BE2FDEA1A62576EE77392B458774C65440BA9D1985F23AEFEADF18F2F7`）。该结果只
确认 pinned PX4 SIH、signed metadata、target-pool 分页和只读路由仍闭合；它没有把模拟的 Jetson
metadata 提升为真实硬件，也没有打开飞控/任务/载荷写入。

Windows wrapper 现在解析 PATH 中的 `docker.exe`，找不到时回退到
`C:\Program Files\Docker\Docker\resources\bin\docker.exe`，从而把环境发现和验收逻辑解耦。
UI 仍只有 QGC 原生 `FlyView`/`FlightDisplayViewVideo`/`QGCButton`/`QGCLabel`/`QGCPalette`；
`operator_visual_acceptance` 继续由人工画面检查单独声明，机器流程保持 `not_claimed`。
## 10. pre.3 原生 QGC UI 与 E4 post-adoption 架构补充（2026-08-26）

### 10.1 一个 UI、一个状态模型、一个算法核心

```text
REPLAY recording ─┐
SIM sensors/PX4  ──┼─> RuntimeChannel ─> shared perception/autonomy core
HIL devices      ──┤                            │
REAL devices     ──┘                            v
                                      signed RuntimeStatus type 23
                                                 │
                                                 v
                           native QGC MultiDetectOperatorController
                                                 │
                         native FlyView / PipView / FlightDisplayViewVideo
```

模式差异止于 runtime channel、传感器/动力学实现和时钟。`REPLAY` 与 `SIM` 不拥有独立 UI，后续
`HIL` / `REAL` 也不会切换到另一套状态机或算法。所有模式都进入 QGC 原生 `FlyView` / `PipView`：

- 不创建第二窗口；
- 不绘制自定义 FixedWing HUD；
- PiP 只渲染只读 annotations，目标交互仅在 full view 开放；
- 弹窗、滑块与避让边界使用 `QGCPopupDialog`、`QGCSlider` 和 `QGCToolInsets`；
- 标签、按钮、颜色与缩放继续复用 `QGCLabel`、`QGCButton`、`QGCPalette`、`ScreenTools`。

这保证模拟器的“同样算法、同样 UI”不是主题近似，而是复用同一 QML surface 和同一 controller state。

### 10.2 E4 同帧因果链

```text
publisher appsrc push succeeds
        │
        ├─ frame_accepted(frame_id)                  [真实 push ACK]
        v
launcher sends signed RuntimeStatus type 23
        v
QGC native controller authenticates and adopts state
        v
QGC sends signed RuntimeStatusAck type 24            [post-adoption ACK]
        v
launcher authenticates and matches frame/session
```

计数语义必须分开：

| 字段/事件 | 可以证明 | 不能证明 |
| --- | --- | --- |
| `frame_accepted` | 对应 wire frame 已被当前 appsrc 实际接受 | QGC 已采纳 metadata 或 QML 已显示 |
| type 23 send count | 本地签名 metadata send 已完成 | QGC 已收到或采纳 |
| authenticated matching type 24 | native controller 已在采纳对应 type 23 后回 ACK | QML render、人工可见性、runtime policy acceptance |
| `operator_visual_acceptance` | 只有人工 Fly View 点验可以声明 | 任何机器 barrier 都不能自动提升 |

因此机器证据继续固定：

```text
operator_visual_acceptance=not_claimed
qml_rendering_proved=false
runtime_mode_policy_acceptance_proved=false
```

### 10.3 RTSP reconnect generation

RTSP server 为每个 media/appsrc 会话分配单调递增 generation，并以 appsrc identity 绑定清理：

1. QGC 可在首帧前或流中因原生 PiP/startVideo 生命周期断开；publisher 等待新的可用 generation；
2. 迟到的旧 `unprepared` 只清理自己的 generation，不能覆盖或置空已建立的新 appsrc；
3. 同一输入 frame 在切换 generation 时可以重试，但 `frame_accepted` 位于成功 push 之后且位于重试
   循环之外，所以每个 wire frame 最多只接受一次；
4. 客户端超时、push 失败、非法帧、以及整个会话从未出现客户端仍保持 fail closed。

### 10.4 当前发布门

该历史 pre.3 portable rebuild 之前，不复用更早 pre.3 的 EXE 大小或 SHA-256；当时身份以
`build/windows-v0.3.0-pre.3-release/verification/v0.3.0-pre.3-evidence.json` 为准。
本节不声称 combined 300-frame gate 已通过。完整 REPLAY/SIM、PX4 Mission、HIL、REAL、户外
GNSS 和人工 Fly View 验收仍未全部完成，故最终 `v0.3.0` 仍被阻塞。
## 11. 历史 pre.3 权威 300-frame combined SIM/QGC gate（2026-08-26）

本节记录历史 pre.3 的权威运行状态，取代第 10 节中“combined gate 尚未声明、最终 hash 待重建”的
临时状态，同时保留所有历史证据。

### 11.1 制品身份与同帧闭环

```text
MultiDetectGCS.exe
  46,561,280 bytes
  SHA-256 4C3584F984592FDDF9114EC9C08EC3C68F86C768BFAD4EB3D0F505A263FD55FE
MultiDetectReplayRtspPublisher.exe
  79,872 bytes
  SHA-256 AB3E0043DA4A124270BEE1D5EC75A717A9845CF37CB63A8ADC8A50B4752D206F
```

`artifacts/evaluation/combined-sim-qgc-20260826T013505Z-64000/combined-evidence.json`
（`87,623` bytes，SHA-256
`6374DB67E985BBC003682BFC9F764B416604A9C3566A4A33D0164728D44219AA`）记录
`gate_passed=true`。完整因果链为：

```text
300 Gazebo frames
 -> 300 real appsrc frame_accepted
 -> 300 contiguous signed RuntimeStatus type 23 (1..300)
 -> native QGC controller adoption
 -> 300 contiguous authenticated matching RuntimeStatusAck type 24 (1..300)
 -> helper exit 0 + EOS + summary
```

### 11.2 原生 PiP 的状态证明

普通 QGC PiP swap 的证据不是另写一个按钮或直接修改配置：orchestrator 锁定 exact QGC PID
`40048` 与 HWND `0x1C534FC`，在原生 PipView 几何点 `(286,937)` 上用 `WindowFromPoint` 复核
命中的 HWND/PID，再用 `SendInput` 点击。`QGCQml/MainFlyWindowIsMap` 随后从
`true(default)` 变为显式 `false`：

| | UTC mtime | QSettings SHA-256 |
| --- | --- | --- |
| before | `2026-08-26T01:35:38.3155174Z` | `BD2E993A77172B0668F6ADBF3DCA9F4F3C0083235F9F1B7258734F111CF9B578` |
| after | `2026-08-26T01:35:40.4183819Z` | `764677D852C516ACE28A070886277565D0282E8D09756B1A6A56EF96401C52B8` |

普通 PiP swap 不重启 video receiver；同一运行的 300 个 type 23/type 24 sequence 保持连续。
这继续证明所有模式使用原生 FlyView/PipView surface，没有第二个 simulator window。

### 11.3 PX4/SIM 与证据上限

PX4 阶段为 `multicopter_armed → transition_to_fixed_wing(vtol_state=4) →
transition_to_multicopter(vtol_state=3) → completed`。GPS 为 qualified 3D fix：`fix_type=3`、
`10` 星、`506` samples、最大跳变 `0.011 m`、`false_geolocations=0`。运行后 UDP `14550`
保持不变，所有测试端口释放，自有 container 停止并删除。

`PrintWindow(PW_RENDERFULLCONTENT)` 截图只是 machine smoke；GPU-backed video surface 可以呈现黑面，
不能上推为 QML render 或人工视觉证明：

```text
operator_visual_acceptance=not_claimed
qml_rendering_proved=false
```

因此最终 `v0.3.0` 仍由真实 HIL、REAL、室外硬件/GNSS、完整 PX4 Mission 和人工 Fly View 验收阻塞。
## 12. pre.3 唯一权威 combined run 与普通 UI 启动证明（2026-08-26T02:08Z）

第 11 节的 `T013505Z-64000` / `4C3584F9...` 现在是历史 pre.3 run；以下仅记录该历史段的成功证据：

```text
run: D:\Projects\Multi-Detect\artifacts\evaluation\combined-sim-qgc-20260826T020829Z-57624
combined-evidence.json: 87,530 bytes
SHA-256: 62CFCA2B42EE1A9A69676B91220476609D1505DA46DE36C187E2A85BBA14EB6F
gate_passed: true
QGC: 46,561,280 bytes / 7A4D8B90F0DF0680536B9A91BEF36177542153F97745049D83D25B13BF52554D
RTSP helper: 79,872 bytes / AB3E0043DA4A124270BEE1D5EC75A717A9845CF37CB63A8ADC8A50B4752D206F
```

### 12.1 生产 UI 路径隔离

这次不是 acceptance-only auto driver。launcher 用 `Remove-Item Env:` 删除
`MULTIDETECT_DISABLE_VEHICLE_LINK`、`MULTIDETECT_HIL_UI_AUTO_EXERCISE` 与
`MULTIDETECT_HIL_UI_AUTO_EXIT`；不能用空字符串代替删除，因为 Qt 会把空值当作 environment present。
运行证据为 `acceptance_only_environment_absent_before_launch=true`（`env_absent=true`）、
`hil_ui_auto_exercise_log_count=0`、`hil_ui_auto_exercise_disabled=true`。

300 帧 stream timeout 从固定截止改为按 frame budget 计算，本次为 `180 s`。历史 run
`combined-sim-qgc-20260826T020108Z-52536` 因旧固定 `100 s` timeout 截断 stream 而失败；它只记录
失败与重试原因，不能升级为成功 gate。

### 12.2 状态、传输与视觉边界

当前 QGC PID/HWND 为 `55056 / 0x18D20DE`；`WindowFromPoint(286,937)` 复核相同 owner PID/HWND
后才 `SendInput`。QSettings `MainFlyWindowIsMap` 由 `true(default)` 变为显式 `false`：before
`2026-08-26T02:09:02.8061918Z` / `D78F50C640BD5883DD07EDBF4BB2333888F669CF3DCAE47893C9371E1D5D763C`，
after `2026-08-26T02:09:04.6911767Z` /
`FF664083835554D9A320712D307EFA3FEC9B757771354A7207B7820AE1F4357C`。普通 PiP swap 不重启视频；
300 个 type 23 与 300 个 post-adoption type 24 sequence 连续，helper exit `0`、EOS/summary 完整。

机器截图语义检查读到 badge `环境 SIM · HEALTHY`；它证明 controller 公开的 SIM/HEALTHY 状态进入
原生 QGC surface，但 `PrintWindow` 的 GPU 视频面可能为黑，不能证明 QML video render 或人工可见性：
`operator_visual_acceptance=not_claimed`、`qml_rendering_proved=false`。

PX4 transition/return 与 GPS qualified 通过；UDP `14550` 未改变，端口释放、container 删除。最终版本
仍等待真实 HIL/REAL、室外硬件、完整 PX4 Mission 与人工视觉门。

## 13. pre.4 原生桌面取证与 VTOL Mission 架构

### 13.1 原生 QGC surface 不可分叉

pre.4 将“贴合 QGC”从视觉偏好固定为架构约束：

- 唯一操作界面是现有 QGC `FlyView`/`FlyViewVideo`/`PipState` 与
  `FlightDisplayViewVideo`；地图、toolbar、gimbal/tracking input chain 与 PiP 行为继续由上游 QGC
  拥有。
- Multi-Detect 只在该 surface 内增加 overlay，并且使用 `QGCPalette`、`QGCButton`、`QGCLabel`、
  `QGCSlider`、`QGCPopupDialog`、`QGCToolInsets` 与 `ScreenTools`。PiP 只能显示只读 annotation；
  full video state 才可框选、LCK/TRK 或取消目标。
- 禁止创建 `Window`、`ApplicationWindow`、第二个视频 surface、独立模拟器 UI、
  simulator-only QML、自绘 FixedWing HUD 或 Multi-Detect 私有主题。允许 Computer Use 或自动截图
  不改变这一点：自动化必须操作并证明同一个原生 QGC 窗口，不能用替代 UI 生成验收画面。
- REPLAY/SIM/HIL/REAL 共用同一 UI、controller、perception/autonomy core、contract、安全策略与
  Mission path；只有 RuntimeBackend、world、clock、只读 sensor source 与故障源可以变化。

### 13.2 exact PID/HWND exact-client 桌面捕获

pre.3 的 `PrintWindow(PW_RENDERFULLCONTENT)` 截图作为历史 machine smoke 保留；GPU-backed
Qt/GStreamer surface 可能在该 API 中为黑，因此不能用它证明视频渲染。pre.4 Stage A 由 combined
wrapper 直接捕获真实桌面像素：

```text
exact QGC PID
-> MainWindowHandle / owner PID equality
-> Per-Monitor V2 DPI context
-> GetClientRect + ClientToScreen(two corners)
-> exact physical client rect on one monitor
-> restore + foreground + temporary topmost + DwmFlush
-> external top-level occlusion enumeration
-> 25-point WindowFromPoint owner grid
-> CopyFromScreen(exact client rect, SourceCopy|CaptureBlt)
-> restore cursor/window placement/topmost/foreground/DPI context
```

任何 PID/HWND 不一致、跨显示器、client rect 非法、外部窗口遮挡或 ownership grid 不匹配都
fail closed。捕获结果不裁入另一个 UI，也不把 desktop screenshot helper 变成产品 renderer。

`scripts/validate_qgc_desktop_capture.py` 使用归一化 video-only ROI，明确排除 toolbar、PiP、
crosshair、runtime badge、action、depth 和 status chrome。硬门组合为：

1. 两张 exact-client frame 的 video ROI 都是 nonblack 且 nonuniform；
2. RTSP helper/QGC 日志同时证明 streaming 与 decoding；
3. 两帧之间 native-controller post-adoption type 24 sequence 至少推进 5。

changed-pixel ratio、MAD 与 dHash distance 只作 temporal 诊断；近静止的 Gazebo 画面不要求大幅
像素变化。这一组合只允许形成 `transport_bound_visible_video_surface_proved`，不等价于 exact
source frame identity、完整 QML 语义或人工验收：

```text
exact_source_frame_identity_proved=false
qml_rendering_proved=false
operator_visual_acceptance=not_claimed
```

### 13.3 确定性 VTOL Mission contract

`src/multidetect/px4_mission.py` 是纯 contract/compiler/transaction 模块，不做网络 I/O，也不接触
simulator truth。`VtolMissionGeometry` 只从 runtime home、waypoints、takeoff altitude、loiter
duration/radius 和 landing point 构建；landing 缺省回 home。任何 `truth`、`expectation` 或 evaluator
channel 混入 runtime geometry 都被拒绝。

adapter manifest 固定为：

```text
adapter_id: px4-vtol-mission-int-v1
protocol: MAVLink2/MISSION_ITEM_INT
mission_type: MAV_MISSION_TYPE_MISSION (0)
target: PX4 system 1 / autopilot component 1
max_items: 64
```

`Px4MissionSafetyPolicy` 将 geofence center/radius、海拔/盘旋/时长/落点偏移/item 数上限及其 hash
纳入编译。可执行 Mission 必须按唯一 lifecycle 排列：VTOL takeoff → forward transition →
waypoint/search route → loiter → back transition/multicopter → 唯一 terminal VTOL land。缺步骤、
重复 terminal land、超围栏、超高度、非法 frame/command/coordinate 或多余 item 都 fail closed。

`MissionTransactionBindings` 将 prompt、task spec、plan、context、airframe、geofence、vehicle identity、
runtime descriptor 与 runtime provenance hash 绑定到同一 transaction。`Px4MissionTransaction` 的门为：

```text
begin upload
-> PX4 requests every sequence with MISSION_REQUEST_INT
-> accepted MISSION_ACK for primary Mission type
-> exact-source INT-only readback
-> canonical protocol hash equality
-> commit
-> exact PX4 AUTO Mission start permit
```

legacy `MISSION_REQUEST`、未请求 sequence、错误 mission type/source、readback 缺项或 hash mismatch
均拒绝；不存在可绕过 transaction 的 generic production command path。

#### 13.3.1 掉电恢复与事务 journal

`src/multidetect/mission_journal.py` 在上述纯事务外包了一层可复用的
`DurableMissionJournal`。首行保存 transaction、executable mission、validation report、PX4
wire 和全部 `MissionTransactionBindings` 的 canonical hash；后续每个
`AUTHORIZED → UPLOADING → UPLOADED → READBACK_VERIFIED → COMMITTED → START_PERMITTED`
状态、`MISSION_REQUEST_INT` sequence、ACK 来源和 readback hash 都是独立的 fsync JSONL 记录，
并通过 `previous_record_hash` 串成不可跳跃的链。SIM/HIL/REAL 只替换 RuntimeBackend，journal
格式和 `PersistentPx4MissionTransaction` 适配器不变。

进程在写入过程中掉电时，未以换行提交的尾记录会保留在原文件并将恢复结果标记为
`recovery_required=true`；不会截断、补 ACK、跳过 readback 或自动发出 `AUTO.MISSION`。显式
`repair_truncated_tail()` 会先把尾字节隔离到 `.recovery-tail`，再写入带操作者备注的
`journal_recovery_acknowledged` 记录。`resume_plan()` 只给出下一步（补齐 item、等待 ACK、
重新读回、commit 或人工确认），`auto_start_allowed` 永远为 `false`。完整记录、绑定漂移、
重复/乱序 sequence、未知状态和 hash-chain 破坏都会 fail closed。

检查入口：

```powershell
$env:PYTHONPATH = 'src;.'
python -m multidetect.mission_journal path\to\mission.jsonl
# 截断尾巴必须先人工核对，再显式隔离并记录备注
python -m multidetect.mission_journal path\to\mission.jsonl `
  --repair-tail --operator-note 'power loss reviewed by operator'
```

机器可读契约位于
`configs/schemas/mission-transaction-journal.schema.json`；这个 journal 是事务证据与恢复
边界，不会把软件/SIM 状态提升为 HIL、REAL、室外 GNSS 或人工 Fly View 通过。
联合 `run_px4_gazebo_qgc_combined_sim_acceptance.ps1` 会把 journal 作为同一 run 的 source
artifact 输出；`verify_combined_sim_evidence.py` 对新字段重新打开 JSONL 并复核文件摘要、
hash-chain、恢复状态、`START_PERMITTED` 终态和完整 item sequence。历史 artifact 没有该字段
时保持原有兼容路径，不能由源码改动倒推补齐。

### 13.4 多旋翼 Mission contract

plain PX4 多旋翼现在有独立的 `MultirotorMissionGeometry`、
`Px4MultirotorSafetyPolicy` 和 `compile_multirotor_executable_mission(...)`。对上层调用方，
`compile_executable_mission(vehicle_class=...)` 是三机型统一入口：它归一化 `x500`/
`multicopter`/`fixedwing` 等别名，并拒绝把一类 geometry/policy 交给另一类生命周期。三种
专用编译器仍复用上面的
runtime-only 坐标、围栏、海拔、盘旋、落点偏移和 home-match 校验，但把生命周期明确收敛为：

```text
TAKEOFF → waypoint/search route → LOITER_TIME → 唯一 terminal LAND
```

编译结果仍然是同一个 `ExecutableMission` 和 `MISSION_ITEM_INT` wire contract，adapter id 为
`px4-multirotor-mission-int-v1`，不引入第二套上传、readback、commit 或 AUTO.MISSION start 路径。
多旋翼 geometry 只接受 runtime channel；`truth`、`expectations` 和 evaluator 字段会在解析阶段
拒绝。VTOL 的 transition item 不能被误塞进 plain multirotor lifecycle；缺 takeoff/land、重复
terminal、超围栏/高度/盘旋/落点或 item budget 都 fail closed。这样 `x500` SIM、后续 HIL 和 REAL
均可通过同一事务门，只由 airframe adapter 决定生命周期差异。

为把“共用路径”从编译器声明推进到可重放证据，
`scripts/run_px4_airframe_transaction_acceptance.py` 现在按 `vtol`、`fixed-wing`、
`multirotor` 各写一份 journal，并对三者执行同一条
`AUTHORIZED → UPLOADING → MISSION_REQUEST_INT → ACK → READBACK_VERIFIED → COMMITTED →
START_PERMITTED` 状态链。`scripts/verify_px4_airframe_transaction_acceptance.py` 会重新打开每份
JSONL，比较文件字节数/SHA-256、hash-chain、请求序列和 identity binding；任一 journal 被追加、
替换或恢复状态异常，整份三机型 smoke 都 fail closed。该产物是 software/SIM contract evidence，
不会被解释成真实飞控、室外 GNSS、HIL/REAL 或人工视觉通过。

### 13.5 单一 owned router 与 headless helper

`src/multidetect/px4_gazebo_qgc_router.py` 仍服务一个 disposable、loopback-only owned SITL：

- QGC channel 保持原 read-only allowlist，Mission/parameter/FTP/system-time/control 写入继续阻断；
- evaluator 只获得四个既定 lifecycle/transition command，以及绑定 system/component、Mission type、
  item count、sequence、command、frame、coordinate 范围的 Mission 协议；
- `MISSION_COUNT`、`MISSION_ITEM_INT`、`MISSION_REQUEST_LIST`、`MISSION_REQUEST_INT`、
  `MISSION_ACK`、`MISSION_SET_CURRENT` 和 exact AUTO Mission heartbeat 在各自方向按 contract 放行；
  `MISSION_CLEAR_ALL` 明确为 `evaluator_mission_clear_all_allowed=false`，无关帧被阻断并计数。

`scripts/vtol_sitl_mission_acceptance.py` 是 test-only、headless adapter。它不创建/停止 container，
只附着 combined wrapper 已拥有的 router/PX4；用相同 Scenario loader/runtime input 生成并 hash 绑定
prompt、task、context、platform plan、compiled mission、validation、operator grant、runtime descriptor/
provenance、airframe、geofence 与 vehicle identity。helper 要求初始 disarmed + multicopter + on-ground，
执行 INT-only upload/readback/commit，进入 exact AUTO Mission 后 arm，观察 `MISSION_CURRENT`、
`MISSION_ITEM_REACHED`、takeoff/in-air、FW/MC 状态、terminal land、disarm、GPS 与 geofence。任务活动时
只发 ready file，让外层对同一个原生 QGC Fly View 做截图；它不拥有第二套 UI 或 lifecycle。

combined PowerShell wrapper 已串接 Mission helper、active-ready screenshot 和最终 Mission evidence 槽位。
本节前半段保留的是源码接入时的架构状态；真实 pre.4 运行结果以第 14 节的权威 combined
evidence 为准。真实 HIL、REAL、室外硬件/GNSS 与人工 Fly View 仍不因该 machine gate 自动完成。

## 14. pre.4 authoritative combined SIM/QGC gate（2026-08-26）

### 14.1 运行身份与范围

在该 pre.4 历史段内，唯一成功的 combined 运行是：

```text
command:
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_px4_gazebo_qgc_combined_sim_acceptance.ps1 `
  -Frames 900 -RouterDurationSeconds 420 -FixedWingHoldSeconds 12 `
  -MissionTimeoutSeconds 240 -MissionSetupAllowanceSeconds 120 `
  -MissionCaptureSequenceAdvance 5

evidence:
artifacts/evaluation/combined-sim-qgc-20260826T081624Z-50796/combined-evidence.json
```

该 evidence 的 `gate_passed=true`，scenario 为 `river_rescue_vtol_001`，frame count `600`，Git
HEAD 为 `7f6e6aeb7c00902fb8084bd6e96a0250df685e67`。PX4/Gazebo 使用 pinned digest、loopback-only
owned router、只读 QGC channel 和 evaluator bounded Mission allowlist；Ground Truth 只留在
evaluator，product runtime 没有真实 Pixhawk、Jetson、相机或 actuator output。

### 14.2 VTOL Mission 事务结果

`vtol_mission.gate_passed=true`，7 个 `MISSION_ITEM_INT` 完成 INT-only upload、PX4 request、ACK、
exact-source readback、canonical hash equality、commit 和 `start_permitted`。committed protocol
SHA-256 为：

```text
becf854f1c2027f1b6ad2338d861af813839f6746155cff9d7d9dabab167eb18
```

MC 起飞、固定翼观察、回到 MC、落地和最终 disarm 均通过；最大离家距离 `143.154 m`、最大相对
高度 `25.094 m`。live context 的 GPS fix type 为 `3`、卫星数 `10`、`GPS_RAW_INT.h_acc` 为
`0.9 m`。这些数字是该 SIM 场景的运行证据，不是室外 GNSS 精度承诺。

### 14.3 QGC 原生 UI 与证据边界

QGC 仍只有一个原生 surface：

```text
QGroundControl FlyView/FlyViewVideo/PipView/FlyViewCustomLayer
```

同一运行收到 `600` 个 signed type-23 runtime status 和 `600` 个 native post-adoption type-24
ACK，序列连续；没有创建第二个 simulator window。原生 QQuickItem video pixels、visible-video
surface、toolbar/drawer machine smoke 均通过。Multi-Detect 只作为 QGC layer，继续使用
`QGCLabel`、`QGCButton`、`QGCPalette`、`ScreenTools` 和原生 `PipState`/`VideoOutput`。

证据仍明确保持：

```text
desktop_capture_gpu_surface_proved=false
exact_source_frame_identity_proved=false
qml_rendering_proved=false
operator_visual_acceptance=not_claimed
```

因此该 gate 只升级到本场景的 SIM machine/E4 集成证据；人工 Fly View 点验、真实 HIL、REAL、
室外相机/GNSS 和最终 `v0.3.0` release gate 继续保持 pending。

### 14.4 pre.4 Windows 制品身份

```text
Release EXE:
build/windows-v0.3.0-pre.4-release/Release/MultiDetectGCS.exe
portable EXE:
build/windows-v0.3.0-pre.4-release/portable-v0.3.0-pre.4/bin/MultiDetectGCS.exe
size: 46551552 bytes
sha256: B798CEE669252B3DD6F1A8F07E3D77C737C04B4D128941331CBFAFDB17B44142
RTSP helper sha256: F6F8AC6DD87DAA6BA7CF477AC62BDE5889B724885AE5C9DB285A485050FEFE37
```
# 返航禁飞区重规划性能

返航使用膨胀后的禁飞圆可见图。圆的多边形顶点和顶点间可见性在任务准入/执行器构造时只计算一次；每次重规划只把当前点和 home 接入静态图，再运行 Dijkstra，因此运行时成本为 `O(V·N)` 的端点连接加图搜索，而不是重新构图。禁飞区最多 64 个，每圆 16 个顶点；单次重规划预算默认 50 ms。超时先保持悬停并在下一控制周期重试，连续失败达到上限后才进入原有就地降落流程。

在 Orin Nano 上，预期缩放主要由静态图构建阶段的顶点数平方项和运行时端点连接的线性项决定；缓存图后，连续控制周期不会重复顶点间相交测试。设备上的实际耗时仍应以启动时记录的 `visibility_graph_build_ms` 和运行时重规划计时为准，不能把开发机的毫秒数直接当成飞行保证。
