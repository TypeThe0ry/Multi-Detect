# Multi-Detect 下一批工作（2026-11-08）

以下任务按预期产品价值排序。每项均可独立交给 Codex lane；SITL 作业必须串行且每项最多占用一个项目槽位。任务不包含训练、release-parity、真实飞行或 HIL。

## 66. 增加多目标搜索与计数任务覆盖

**目标**

设计并验证一个同时包含多个同类目标、相邻异类干扰物和部分遮挡的多目标搜索/计数任务，重点评估 track association、重访去重、漏检后重获和最终 exact count。该任务应扩展现有任务能力，不复跑 nightly 30-head-e2，也不复跑当前 follow/track 或 climb-fault 作业。验收要区分真目标覆盖、重复确认、误收干扰物和因感知证据不足而保留的 lower bound。

**证据路径**

- `docs/count-lower-bound-20261106.md`、`docs/comms-free-extra-finding-20261106.md`、`docs/hydrants-false-finding-20261107.md`：计数 lower bound、重复发现与额外 finding 的历史证据。
- `docs/mission-metric-check-20261106.md`：count 与 survey/monitor summary 的语义边界。
- `configs/onboard/suite-main-r21idr3-r24plan.json`、`configs/onboard/suite-robustness-r21idr3-r24plan.json`、对应 scenario/truth/expectation 配置。
- `src/multidetect/` 中任务规划、track、融合与 scoring 实现；`scripts/run_mission_suite.py`、`scripts/collect_sim_teacher_episodes.py`；相关 count/track/scorer 测试。
- 只读历史证据：`artifacts/benchmarks/nightly-linux-r21idr3-r24plan-29-head/` 下相关 count episode 和 reports。

**约束**

- 本任务需要 SITL：是。先做静态配置检查与 `--dry-run`；若可运行，再以一个 worker、一个 episode/配置串行执行，设置 `MULTIDETECT_SITL_SLOTS=3`，只使用项目槽位 0–2，绝不访问槽位 3/4；不得与其他 SITL 并行。每次只占一个 slot。
- SITL 作业内所有 Python 使用 `/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python`；`.venv` 仅用于 pytest 和 CPU-only gate。
- 输出只写 `.tmp/` 下新目录；`artifacts/` 只读。不得修改既有任务 expected 来过门，不启动 GPU 训练、模型服务或与本 SITL 任务无关的 Docker 服务，不做 release-parity、HIL 或真机，不联网下载。
- 不修改 `configs/release-parity.json`、`docs/current-progress.json`、`configs/schemas/*.json`、`configs/product-version.json`。

**执行与验收**

1. 选取或构造一个最小可复现多目标场景，场景中至少有 3 个真目标、1 个近邻干扰物和一次遮挡/重访；记录 world、目标 ID、真值位置、相机视角与任务预算。
2. 配置校验证明任务可解析、truth 与 expected 一致，且规划首步符合搜索/计数意图；确认 scorer 不把非 count episode 混入 count 汇总。
3. 对每个保留 episode 逐目标关联 truth、track、confirmation、fused location 和最终 count，量化 duplicate、false finding、漏检/重获；不得只用 summary success rate 代替逐目标核查。
4. 验收要求：场景至少覆盖两个不同生成 world；计数结果和每个 finding 均可回溯到目标/track；重复目标不重复计数、干扰物不误收；失败或 lower-bound 结果必须保留原始 episode 并分类为规划、感知、关联、几何或基础设施问题。运行相关 pytest 和 CPU-only onboard gate。

**需要写入**

- 必要时只增加最小 scenario/suite/test 支持，不放宽 scorer 或任务通过规则。
- 最终报告：`docs/multi-target-count-coverage-20261108.md`，列明配置与输入 hash、world/目标矩阵、逐目标结果、SITL slot/时长、失败分类、测试结果及剩余缺口。

## 67. 在受控负载下验证 sensor timeout 与 PX4 clock robustness

**目标**

针对仍未按需复现的 PX4 clock stall、首帧/传感器 timeout，建立一个有界的 baseline 与受控主机负载对照。使用已有 frame-wait、host-pressure、clock/stats/GDB 顺序和失败分类，判断 timeout/clock 停滞是否在负载条件下出现、证据是否完整；不把相关性写成根因，也不把一次成功写成修复。不得重复当前正在运行的 sensor/clock 作业、nightly 30-head-e2、follow/track 慢模型或 climb-fault 作业。

**证据路径**

- `docs/gz-idle-stall-20261107.md`、`docs/gz-idle-stall-followup-20261107.md`：无探针污染采集顺序、Gazebo/PX4 栈和 unknown 边界。
- `docs/accel-timeout-crash-20261107.md`、`docs/sensor-stream-timeout-20261107.md`、`docs/sensor-timeout-load-20261107.md`、`docs/timeout-host-trace-20261107.md`、`docs/sensor-timeout-followup-20261107.md`：历史 timeout、主机负载与现有诊断契约。
- `scripts/collect_sim_teacher_episodes.py`、`scripts/analyze_sensor_timeouts.py`、`scripts/px4_clock_stall_capture.py`、`.tmp/jobs/01-px4-clock-stall-followup.sh`、`.tmp/jobs/38-sensor-stream-trace.sh`。
- `tests/test_collect_attach.py`、`tests/test_analyze_sensor_timeouts.py`、`tests/test_px4_clock_stall_capture.py`；只读历史 28/29-head 与已完成 sensor/clock 产物。

**约束**

- 本任务需要 SITL：是。只运行一个新的、有界、串行对照套件：至少一个 baseline 和一个受控负载条件，每条件最多一个 episode；设置 `MULTIDETECT_SITL_SLOTS=3`，只用项目槽位 0–2，绝不访问槽位 3/4，每次最多一个 slot，不与其他 SITL 并行。不得重跑正在运行或已完成的同一 sensor/clock job。
- 负载必须由可审计、有限时长的仓库脚本或现有 harness 产生；记录 PID、cpuset、开始/结束 UTC、CPU PSI/load、slot 和停止原因。禁止按名称清理进程，只能结束本 lane 自己记录的 PID。
- SITL 作业内所有 Python 使用 `/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python`；pytest/分析和 CPU-only gate 用主 `.venv`。
- 新输出只写 `.tmp/clock-sensor-load-20261108/`；不得写入、移动或删除共享 `artifacts/`。不改 15 秒 frame wait、PX4 timeout、retry、stall timeout、lockstep、world control、任务 expected 或安全规则；不训练、不做 release-parity、HIL/真机或网络下载。

**执行与验收**

1. 先 dry-run 并检查采集顺序确实是进程/线程与 GDB 先于任何 `gz topic` probe；确认 baseline 与负载条件使用同一 mission/world/模型和相同预算。
2. 运行对照并保留每个 episode 的 `frame_wait_timeline`、`host_pressure.jsonl`、MAVLink/JSONL、clock/stats probe、GDB capture、`capture_budget.ordering`、RTF 与任务结果；负载条件不得改变任务语义输入。
3. 用分析器逐事件分类 `no_new_frame_observed`、`frame_timestamp_below_pose_watermark`、`px4_clock_and_telemetry_stalled` 或 `frame_wait_timeout_unclassified`，并比较 timeout 数、clock/frame 推进、CPU PSI/load 和 trace 覆盖。若未复现，只报告“本矩阵未复现”；若复现，仍只写可观察栈和时间线，不指定根因。
4. 验收为：两条件均有完整或明确缺失的诊断字段；不以 0 timeout 证明修复；任何差异可由输入 hash、负载 PID/cpuset 和逐事件证据复核。运行相关 pytest、分析命令和 CPU-only onboard gate。

**需要写入**

- 只改必要的诊断/分析测试或有界 job 适配，不改运行时阈值和共享证据。
- 最终报告：`docs/clock-sensor-load-robustness-20261108.md`，包含条件矩阵、slot/PID/UTC、每 episode 时间线、pressure/trace 覆盖、分类结果、复现与否、证据边界和测试结果。

## 68. 建立 Jetson 级 CPU-only onboard 性能基线

**目标**

建立可重复的 CPU-only profiling 基线，识别 Jetson-class CPU 预算下 onboard perception、任务规划、几何融合、日志与传感器帧处理的主要耗时和内存峰值，并给出可验证的优化机会。不得用桌面服务器成绩冒充 Jetson 实测；若没有 Jetson 设备，产出主机可复现基线和 Jetson 验证方案，标记目标设备门槛尚未通过。

**证据路径**

- `docs/operator-abort-wall-20261107.md`：primitive/executor wait 占用与低 RTF 证据，说明 wall time 不能简单归因于模型推理。
- `docs/sensor-timeout-load-20261107.md`、`docs/timeout-host-trace-20261107.md`、`docs/sensor-timeout-followup-20261107.md`：主机负载和 timeout 的证据边界。
- `src/multidetect/onboard_runtime/`、`src/multidetect/sim_teacher/`、`scripts/eval_onboard_loop_campaign.py`、CPU-only profiling 或 timing 工具及对应 tests。
- `docs/nightly-review-29-head-20261107.md`：仅作任务时长/RTF 参考，不把 SITL 总 wall time 当 CPU inference latency。

**约束**

- 不需要 SITL：只允许 CPU-only replay、benchmark fixture 和 profiling；不得启动 SITL、Docker、GPU、模型服务、训练、release-parity、HIL、真实飞行或网络下载。
- profiling 运行使用主 `.venv`；任何模型运行仅限仓库已有本地 CPU 权重，不下载、不触碰训练进程或 GPU1 的 r10 作业。
- 输入产物只读，临时输出写 `.tmp/onboard-cpu-profile/`；不得改 `artifacts/`、任务 expected、安全阈值、timeout/retry 或 evidence-bound 文件。

**执行与验收**

1. 从固定 episode/replay 与合成输入各建立至少 3 次可重复测量，分别统计 detector/preprocess、planner/policy、geometry/association、serialization/logging；报告 p50/p95、输入规模、线程数、CPU 型号、版本/hash 和 RSS 峰值。
2. 将推理/计算耗时与 PX4 primitive wait、frame wait、任务 wall time 分开；不得将相关性写成 sensor timeout 或 clock stall 根因。
3. 识别最多三个热点；若做优化，只做有 profile 证据的局部 CPU 优化，并在相同输入上比较输出等价性、p95 和内存。不得降低检测质量、减少安全检查或改变任务门槛换速度。
4. 验收为：基准脚本/命令可复现；计时口径、设备、热身、线程与重复次数完整；优化若有则语义输出保持不变且改善超过测量噪声。没有 Jetson 实机时，明确列出不能据当前主机数据得出的结论，并提供 Jetson 上复测步骤。运行相关 pytest。

**需要写入**

- 必要时新增 CPU-only benchmark/helper 和窄范围测试；不修改训练、release-parity 或任务阈值。
- 最终报告：`docs/onboard-cpu-budget-20261108.md`，给出基线表、方法、输入 hash、热点、优化前后数据、Jetson 实测状态和后续验证命令。

## 69. 固化可选云台与螺旋桨仿真/记录契约

**目标**

用代码与 fixture 明确验证 aircraft 可有或无 gimbal：固定相机机型不要求云台字段；带 gimbal 的机型必须在仿真中具有可变化的三轴云台角度和四个 propeller 的角度/转速，并在观测记录中与 RGB sensor timestamp 对齐。消费正在运行的 follow/track slow-model 结果（若已完成）作为补充证据，但不等待或重跑该作业；建立不依赖该作业即可执行的模型/SDF、stream、recording contract 检查。

**证据路径**

- `docs/gimbal-coverage-followup-20261107.md`，尤其 2026-11-08 v2 复核及其非零云台命令证据缺口。
- `docs/gimbal-feedback-age-audit-20261107.md`、`docs/gimbal-suite-expand-20261107.md`、`docs/follow-track-fixed-gimbal-20261107.md`：freshness、任务覆盖与 slow-model 作业状态。
- `configs/airframes/x500_mono_cam.json`、`configs/airframes/x500_gimbal.json`、对应模型/SDF、`src/multidetect/sim_teacher/streams.py`、`scripts/collect_sim_teacher_episodes.py`。
- `tests/test_propeller_gimbal_recording.py`、`tests/test_gazebo_sensor_stream.py` 与现有 gimbal recording/fixture 测试。
- 若已生成，只读 `artifacts/benchmarks/follow-track-fixed-gimbal-20261108-v2/`；不得把旧报告里的静态角度抖动当作命令响应证据。

**约束**

- 不需要 SITL；不启动 follow/track v2、nightly、SITL、Docker、GPU、模型服务、训练、HIL、真实飞行或下载。
- `artifacts/` 只读；不得修改任务 expected、follow 阈值、技能序列、15 秒 frame wait、PX4 timeout 或 supervisor 安全规则。fixed-camera 不得被强制添加 gimbal 字段。
- 只改最小仿真模型/stream/记录契约与测试；Windows 行为保持不变。Python 测试用主 `.venv`，CPU-only gate 按改动范围运行。

**执行与验收**

1. 静态检查 fixed-camera 与 gimbal airframe 配置、模型关节、joint state 发布与 stream/recording 路径，证明角度/转速来自模拟状态而非手写常量或后处理伪造。
2. 增加正反 fixture：fixed-camera 缺 gimbal 状态仍有效；gimbal 缺三轴或四 rotor 状态、非有限值或 RGB/joint 时间戳错位时 fail closed；有效 gimbal 样本有有限角度/转速，至少一个 rotor spin 非零。
3. 用可控模拟输入证明非零 gimbal 控制能产生可解释的三轴状态变化，且记录的状态与 RGB 时间戳一致；分开报告“模型可动/记录契约通过”和“真实 SITL 命令响应尚未观察”。
4. 如 v2 结果已落盘，审计非零 `GIMBAL_DEVICE_SET_ATTITUDE` 命令、joint state、RGB stamp 和 rotor 状态；若未落盘只报告缺失，不重跑。
5. 验收为所有 airframe 变体语义清楚、测试拒绝伪造/缺失状态，gimbal 物理模拟具备角度与旋转，fixed-camera 无 gimbal 仍可运行。运行相关 pytest 和 CPU-only gate。

**需要写入**

- 对应模型/stream/recording 的最小修改及测试。
- 最终报告：`docs/gimbal-simulation-contract-20261108.md`，列出两类 airframe 的字段矩阵、模型关节/插件证据、fixture 结果、可控角度响应证据、slow-model 作业引用状态及未取得的 live SITL 证据。

## 70. 补齐新操作员的首飞前仿真操作指南

**目标**

把现有 operator/manual 与 runbook 整理成新操作员可独立完成的离线仿真工作流：环境与模型前置条件、选机型/是否带云台、选择任务/world、启动前检查、任务执行和状态解释、中止/暂停/返航、结果与证据位置、常见 frame/clock/sensor 超时排查。文档必须说明云台机型的云台角度和 propeller rotation 是仿真状态，普通飞机可以没有 gimbal；不得把未证实根因写成确定故障原因。

**证据路径**

- `docs/operator-manual.md`、`docs/operator-demo.md`、`docs/final-sim-acceptance-runbook.md`、`docs/e2e-operator-sim.md`、`docs/gcs-mission-package-workflow.md`。
- `docs/gz-idle-stall-20261107.md`、`docs/gz-idle-stall-followup-20261107.md`、`docs/accel-timeout-crash-20261107.md`、`docs/timeout-host-trace-20261107.md`：已知 clock/sensor 失败表现与未证明因果。
- `docs/nightly-review-29-head-20261107.md`、`docs/gimbal-coverage-followup-20261107.md`：当前通过矩阵和可选 gimbal 的记录要求。
- 实际 CLI help、受支持的 suite/airframe 配置、mission lifecycle 和 supervisor 状态输出；核对源代码，不凭旧示例臆造参数。

**约束**

- 不需要 SITL；只读检查 CLI、配置和现有文档，不启动仿真、Docker、模型服务、GPU、训练、release-parity、HIL、真实飞行或下载。
- 不改产品代码、任务门槛、evidence-bound 文件或共享 `artifacts/`；只改/新增操作员文档及确有必要的文档链接。
- 说明安全相关动作时逐项区分 operator abort、return-home 请求、PX4 ACK/mode 和 fresh landed/disarm 证据；不得声称 abort 即已落地，也不得把 `action_ack` 当作目标完成。

**执行与验收**

1. 以一个全新操作员的顺序组织内容：环境确认、模型/机型选择、任务选择、dry-run/启动、运行状态判读、暂停/恢复/中止、产物检查、失败分诊。
2. 每条命令对照真实 CLI help 和配置 schema/validator 验证；列出可复制的只读检查命令及结果目录的实际命名规则。无法验证的命令明确标为示例或待确认。
3. 故障表至少覆盖 pre-arm、无首帧/15 秒 frame wait、PX4 clock stall、Gazebo stall、主机负载相关性、任务语义失败；每项给出“观察到什么、下一步看哪里、什么证据仍不足以判断”。
4. 验收为新操作员无需口头补充即可按文档完成 dry-run 并找到成功/失败 episode 的关键证据；所有状态与命令对照当前代码，云台选项与 fixed-camera 路径不混淆。用链接检查或静态脚本检查文档链接，并人工核验命令与 CLI help 一致。

**需要写入**

- 更新 `docs/operator-manual.md`，必要时只补少量现有导航文档链接。
- 完成说明：`docs/operator-manual-update-20261108.md`，列出章节变更、命令核验结果、源代码/CLI 依据、文档链接检查与尚待现场确认的内容。
