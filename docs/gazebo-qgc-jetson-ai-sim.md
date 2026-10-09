# Gazebo + MultiQGC + JetsonAI 模拟飞行

这个入口把真实的便携版 MultiQGC、PX4 SITL/Gazebo、相机/深度/GPS/里程计和仓库里的生产感知工厂放在同一条一次性模拟链路中。它用于自动飞行与自动推理联调，不等同于 HIL、实机或室外 GNSS 验收。

## 3-D 主线与 OnboardAI 优先级

Gazebo 主线使用 `configs/scenarios/gazebo-3d-mainline-v1.manifest.json`：多旋翼、VTOL、
固定翼分别绑定 `x500_mono_cam`、`standard_vtol_mono_cam`、`rc_cessna_mono_cam`，world
使用实体 SDF 几何、碰撞、风场、WGS-84 原点和真实 Gazebo camera/depth/telemetry topic。
红车/绿车等小场景只用于感知适配器回归，不是主体验收，也不作为训练标签真值注入模型。

当前训练/推理引用固定在
`configs/training/onboard-policy-current-reference-v1.json`（r33，40 维共享 OnboardAI）。
先用 4060 做同模型增量训练和 3-D 传感器回归，再把同一个 hash-bound artifact 接到 PX4/Gazebo
和未来 Jetson；不会为模拟环境另造一套“演示 AI”。

当前开发顺序是先训练和独立验证共享 OnboardAI，再把同一 ONNX artifact 接到 3-D PX4/Gazebo
飞行循环。r14 证据见 `docs/evidence/onboard-policy-r14-autonomy-transitions-20261006.json`；
它仍是 truth-free software replay/holdout，尚未宣称外部 3-D 自动飞行、Jetson、Pixhawk、
HIL/REAL 或室外 GNSS。

## Prompt 不是颜色示例

用户输入的是任务意图，不是固定的“红车/绿车”答案。当前共享 Prompt 路径要求在起飞前把
自然语言绑定到一个经过批准、可审计的任务模板；模板可以描述车辆、人员、设备、区域搜索、
巡检或其他已登记目标类别。解析结果冻结为 `PromptTaskResolution`，并绑定 Prompt、候选任务、
Mission runtime 和 SHA-256；之后 Gazebo、4060 和 Jetson 都走同一条 `OnboardAIExecutor →
planner → PX4 Mission` 路径。未登记或飞机类型不匹配的 Prompt 会 fail-closed，不能直接把
任意文字变成飞控命令。

主线三类 3-D 场景使用中性实体目标 `multidetect_search_target_v1`。红车/绿车只保留为旧
感知适配器和回归测试夹具，不能作为主线 Gazebo 训练标签，也不能成为模型固定答案。

训练场预检命令为 `python scripts/run_onboard_3d_training_preflight.py`。当前报告明确是
`blocked`：本机 Docker daemon 未启动且没有本地 `gz/gazebo/px4`，所以没有把离线 3-D 资产审计
冒充成 Gazebo 飞行。只有实际 3-D RGB/Depth/telemetry 采集完成后，才进入同一 OnboardAI 的
渲染数据构建和 RTX 4060 增量训练。

## 运行方式

先构建便携版 QGC，并确认下面两个文件存在：

```text
build/windows-v0.3.0-pre.8-release/portable-v0.3.0-pre.8/bin/MultiDetectGCS.exe
build/windows-v0.3.0-pre.8-release/portable-v0.3.0-pre.8/bin/MultiDetectReplayRtspPublisher.exe
```

然后在仓库根目录运行组合验收入口。`-JetsonSafetyModel` 和 `-JetsonSafetyModelManifest` 要成对提供；路径可以是仓库相对路径：

```powershell
pwsh -File .\scripts\run_px4_gazebo_qgc_combined_sim_acceptance.ps1 `
  -JetsonSafetyModel .\artifacts\models\coco-yolo26n-traditional\yolo26n-traditional.onnx `
  -JetsonSafetyModelManifest .\configs\models\ultralytics_yolo26n_coco80_v56_quarantined_candidate.manifest.json
```

脚本会启动一个隔离的 PX4/Gazebo 容器、真实便携版 MultiQGC 的原生 FlyView、RTSP 视频和相机/深度/GPS/里程计传输；感知参数通过 `multidetect.cli integrated-sim-qgc --perception-args` 进入与 Jetson 路径相同的 `build_production_perception`。
高层实时判断使用 `ModelBackedDecisionProvider`：Gazebo/4060 和 Jetson 只替换同一个
`infer()` runner，输入都是 truth-free 的 `ContextSnapshot + OnboardRuntimeEvent`，输出都是
`response/reason/confidence`。因此模拟器不会用另一套演示规则冒充 Jetson 决策；模型提案
之后仍由共享安全监督器和 PX4 Mission 事务路径裁决。

## 算法边界

- 主 fire/smoke fixture 仍保留，用于维持现有目标选择、QGC adoption 和共享测距链路的确定性回归。
- Jetson 参数绑定的是 safety object evidence 分支；COCO raw ONNX 通过 host-side NMS 产生 `car/person/...` 证据，不能直接授权 payload、飞控或 arm。
- 选择任务后仍必须经过已有 planner、Mission upload/readback/commit/permit、watchdog 和清理流程。
- 模型提案只表达高层意图，不拥有 MAVLink、arm/disarm、PWM 或 Mission upload 权限；模型
  故障、超时、格式错误和安全数据不合格时必须 fail-closed。
- 起飞阶段不是由第一帧画面推断。PX4/Gazebo、HIL 和 Jetson/Pixhawk 适配器必须先用合格
  `ContextSnapshot` 调用共享 `mark_takeoff_complete`，确认 `relative_home` 高度达到门槛后
  才能把后续相机事件交给 `SEARCH`/`MATCH_TARGET` 状态链。
- `production_approved=false` 的模型只能用于联调证据，不能宣称生产模型质量，也不能提升 `0.3.0` 发布门。

## 当前模型状态

仓库内的 `ultralytics_yolo26n_coco80_v56_quarantined_candidate` manifest 标记为 `quarantined`，治理、数据权利、部署域指标和生产批准均未完成。因此它可以验证“真实相机帧进入同一生产推理工厂并输出证据”，但不能作为最终 Jetson 部署模型。替换为正式 Jetson fire/smoke manifest 时，仍使用同一命令行和同一生命周期，不需要另写模拟器。

## 真实 Gazebo RGB/Depth 进入 OnboardAI

2026-10-07 已用历史真实 Gazebo 地面采集验证输入链：3 帧 RGB/Depth/telemetry 可无损绑定为
raw recording 和 1 个连续轨迹 episode。绑定器保留实际零基序号，兼容 MDGZDEP1 v1/v2；
旧采集缺少当前 mainline 绑定时只用于 replay/staging，不能自动获取训练资格。FLOAT32 深度
只读视图的光轴转 ray-slant 崩溃已修复。实际 CUDA 训练门拒绝了该未复核小样本，没有新模型
或 promotion，详见 `docs/evidence/onboard-real-3d-input-chain-20261007.json`。

真实 3-D 主线捕获完成后，先把现代 RGB/Depth frame-set 绑定成统一 raw recording，再进入
轨迹审查和增量训练。不要把旧的颜色正控夹具或结构化事件直接当成训练集。绑定命令如下：

```powershell
$env:PYTHONPATH = "src"
python scripts/build_gazebo_raw_recording_manifest.py `
  --rgb-manifest artifacts/evaluation/<rgb-capture>/gazebo-bgr24-frame-set.manifest.json `
  --depth-manifest artifacts/evaluation/<depth-capture>/gazebo-depth-companion-set.manifest.json `
  --output artifacts/evaluation/<recording-id> `
  --recording-id <recording-id> `
  --airframe-profile-hash <airframe-profile-sha256> `
  --scenario-seed <seed>
```

该命令只绑定实际 Gazebo 相机、深度和传感器遥测：原始 BGR24 字节作为来源证据保留，并用无损
PNG 生成现有 replay 的同像素载体；深度文件与 RGB 帧按 `MDGZDEP1`/`MDGZSIM3` hash 配对，回放事件按“同一帧的 telemetry → image”排序。输出的
`recording.manifest.json` 可以交给 `build_onboard_trajectory_staging.py --recording-manifest`，
`gazebo_capture_binding.json` 则保留 RGB/Depth 来源、场景 hash 和每帧 pair hash。输出始终
标记 `training_ready=false`；还必须经过不同身份的轨迹标注/复核，然后才能由
`run_onboard_3d_training_pipeline.py --train` 从当前 r33 parent 做增量微调。该绑定器不读
Gazebo truth、不生成目标标签，也不改变 Jetson/模拟共用的 40 维 OnboardAI 输入输出契约。

如果使用 `scripts/run_external_onboard_model_acceptance.py` 进行外部 PX4/Gazebo capture，可以
直接追加 `--recording-output-directory`；capture 成功后 runner 会在同一轮 evidence 中调用上述
绑定器。还需要提供 `--sensor-frame-output-dir`、`--sensor-depth-output-dir` 和至少两个
`--sensor-frames`；可用 `--recording-id` 与 `--recording-scenario-seed` 固定 recording 身份。
这一步仍不会自动训练或 promotion，只把真实 3-D capture 接到既有训练门。

如果要验证“相机帧而不是结构化假事件”已经进入高层任务模型，使用车辆正控场景。旧场景用于
验证非红车辆保持搜索；需要验证红车颜色匹配和深度 companion 时，使用新增的
`gazebo_red_car_positive_control_001`：

```powershell
$env:PYTHONPATH = "src"
python scripts/run_external_onboard_model_acceptance.py `
  --scenario configs/scenarios/validation/gazebo_red_car_positive_control_001.scenario.json `
  --execute --sensor-frames 2 --sensor-width 1280 --sensor-height 960 `
  --use-actual-detector `
  --sensor-frame-output-dir artifacts/evaluation/external-red-car-actual-rgb `
  --sensor-depth-output-dir artifacts/evaluation/external-red-car-actual-depth `
  --evidence artifacts/evaluation/external-red-car-actual-detector.json
```

这条入口使用真实 Gazebo wire-v3 BGR24 帧、FLOAT32 Depth、仓库内 YOLO26n 候选模型、共享
颜色/深度适配器和同一个 `OnboardRealtimeModelBridge`。红车场景的 r2 证据曾检测到
`car → red`，颜色匹配和约 `5.057 m` 的深度读数通过；但最新 r3 被动复核发现该入口没有
arm/start，PX4 一直在地面，因此 `mark_takeoff_complete` 没有消费任何 RGB 帧。摘要见
`docs/evidence/external-red-car-actual-detector-20261005-r3.json`。被动命令尚不代表完整
自动任务、Mission 提交、GPS 目标落图、RTL 或报告；候选模型仍为 `quarantined`。

要在 disposable loopback SITL 中显式练习完整生命周期，必须额外开启三个开关：

```powershell
python scripts/run_external_onboard_model_acceptance.py `
  --scenario configs/scenarios/validation/gazebo_red_car_positive_control_001.scenario.json `
  --execute --execute-mission-transactions --execute-autonomous-flight `
  --sensor-frames 3 --use-actual-detector `
  --sensor-frame-output-dir artifacts/evaluation/external-red-car-autonomous-rgb `
  --sensor-depth-output-dir artifacts/evaluation/external-red-car-autonomous-depth `
  --evidence artifacts/evaluation/external-red-car-autonomous.json
```

该开关只允许 Docker loopback SITL，执行顺序是已许可 Mission 后
`AUTO.MISSION → ARM → 真实起飞 telemetry gate → RGB/Depth 推理 → 模型请求 RTL →
Home telemetry → REPORT`。它不会在默认入口启用，也不会把模型直接变成 MAVLink/执行器权限；
最新 opt-in r13 已实际通过容器、heartbeat、qualified GPS、ARM 和起飞高度门；随后在
实时任务重规划处由共享能源规划器正确 `abstain`（约 51% 电量不足以同时保留 40% 返航余量
和 15% 安全余量），安全 HOLD 的 POSCTL 心跳确认也未通过，因此该轮没有继续推理、记录
红车 GPS、RTL 或 REPORT。证据为
`artifacts/evaluation/external-red-car-autonomous-20261005-r13-structured.json`。
此外，自动入口会明确拒绝起飞前缓存帧；真正的飞行中 RGB/Depth worker 仍是下一阶段，
所以 r13 不能宣称外部自主飞行、Jetson/Pixhawk、HIL、REAL 或室外 GNSS 已通过。

### r28：飞行中真实帧已进入同一任务链，但 detector 仍需增量训练

随后使用同一场景和同一模型启动了显式自动生命周期 r28。该轮已经通过 Docker/PX4/Gazebo、
qualified GPS、ARM、起飞高度门，以及 20 次 Mission upload/readback/commit/permit；20 个传感器
帧中 19 个完成了真实 RGB/Depth 处理，1 个起飞前缓存帧被严格丢弃。真实 YOLO 在约 22 m 处检测
到了 `car`，颜色适配器判定为 `red`，深度也能给出约 22 m 的量测。

但 raw detector confidence 只有约 `0.01–0.02`，低于可进入 `MATCH_TARGET/RECORD_LOCATION` 的
校准门 `0.25`，所以执行器保持 `SEARCH`，不会把低置信候选写成 GPS、不会 RTL、也不会 REPORT。
这是安全链路按设计拒绝不充分证据；下一步是在 4060 上使用同一 YOLO 架构对远距离小目标帧做
增量训练，再以独立场景、多帧和距离误差门复验。当前证据：
[`external-red-car-autonomous-r28-structured.json`](../artifacts/evaluation/external-red-car-autonomous-r28-structured.json)。
该证据仍只属于外部软件 SIM，不等同于 Jetson Orin、Pixhawk 实机、HIL、REAL 或室外 GNSS。

### r29：v2 detector 完成“红车”软件 SIM sortie

r28 暴露的是训练标签问题，不是 PX4、GPS、Depth 或颜色链路问题：v1 的合成背景中还留着一个
没有标注的原始车辆。修复后的 v2 数据生成器先遮掉背景目标，再用 20% 的原始位置正样本训练；
训练继续使用原有 YOLO26n 权重，未创建新的飞控/决策模型。

用 v2 raw ONNX 重跑同一 disposable loopback SITL，r29 的实际任务链结果为：

- 20 个真实 Gazebo RGB 帧，1 个起飞前缓存帧被拒绝；
- 6 个有效 `car` 观测，置信度约 `0.891–0.912`，目标距离约 `22.23–22.39 m`；
- 共享颜色适配器匹配 `red`，连续观测后才进入记录位置；
- GPS 目标记录、6 次 Mission upload/readback/commit/permit、AUTO.MISSION、RTL、Home 到达和
  REPORT 全部通过；
- detector 只提供感知，`policy_applied_to_actuator=false`，飞控仍通过既有 planner、Mission
  和安全监督器路径执行。

可复核摘要：[`docs/evidence/external-red-car-autonomous-20261005-r29-v2.json`](evidence/external-red-car-autonomous-20261005-r29-v2.json)，
原始 evidence：`artifacts/evaluation/external-red-car-autonomous-r29-v2-structured.json`。
这仍是 truth-free 外部软件 SIM，不是 Jetson/Pixhawk、HIL、室外 GNSS 或实飞验收；v2 candidate
仍未 promotion，最终 `0.3.0` 发布门保持关闭。

## 证据位置

每次运行会在 `artifacts/evaluation/combined-sim-qgc-<run-id>/` 保存 QGC、容器、传感器、Mission 和推理日志。最终 `combined-evidence.json` 会记录 Jetson 模型路径、SHA-256、manifest SHA-256、`production_perception_factory=true` 以及 `authorization=evidence_only_no_flight_control`。
