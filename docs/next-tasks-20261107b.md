# Multi-Detect 下一批工作（2026-11-07b）

以下任务按价值排序。每个任务都是可直接交给另一个 Codex lane 的完整提示；证据目录只读，新输出写入当前 worktree 的 `.tmp/` 或任务指定的新目录。

## 54. 完成爬升保护 SITL 结果审计并闭合 LAND 证据边界

**目标**

消费已经运行或已完成的 climb-fault SITL v2 产物，确认两次低空、无高度增益的 `return_home up` timeout 后，onboard 链是否停止第三次爬升并请求 `land_now`；把宿主请求、PX4 COMMAND_ACK、进入 LAND mode、fresh `landed_state=1` 和最终 disarm 分开报告。不得把正常 operator-abort 收尾、fixture 的 `stop`、或位置 setpoint 的 `action_ack` 当作 PX4 LAND 已执行。若 v2 没有触发故障，必须保留 live gap，不得为了制造触发再跑同一作业。

**证据路径**

- `docs/onboard-climb-failure-20261107.md`、`docs/onboard-climb-land-followup-20261107.md`：阈值、状态机、fixture 与已有单次 SITL 结果。
- `docs/accel-timeout-crash-20261107.md`：step 38 坠落和重复上升的历史正例。
- `docs/identity-r10-train-job-20261107.md`：当前并行作业背景，仅用于确认不要碰训练。
- 最新 `artifacts/benchmarks/onboard-climb-fault-sitl-*` 或 `artifacts/benchmarks/onboard-climb-land-followup-*`：只读 `job.log`、`episode.json`、`steps.jsonl`、mission log、`summary.json`、MAVLink 和 ULog。
- `scripts/run_onboard_fault_campaign.py`、`scripts/mission_executor.py`、`scripts/mission_supervisor.py`、`scripts/onboard_policy.py` 及 `tests/test_mission_executor_remaining_gaps.py`、`tests/test_mission_supervisor.py`、`tests/test_onboard_fault_campaign.py`。

**约束**

- 不启动新的 SITL、Docker、GPU、模型服务、训练、release-parity、真实飞行或 HIL；不重跑已经在运行的 climb-fault v2。若找不到已完成的 v2 产物，只报告证据缺失并完成离线审计。
- `artifacts/` 只读；不得改 LAND 优先级、2 次/0.5 m/2.5 m 阈值、vehicle-loss 规则、任务 expected 或已有断言。
- 不得把任何 ACK 推断成 landed；必须区分 `COMMAND_ACK`、heartbeat mode、`EXTENDED_SYS_STATE.landed_state`、armed 状态和 collector teardown。
- SITL 作业脚本若被审计或修正，脚本中的 Python 只能是 `/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python`；本任务不应启动该脚本。

**执行与验收**

1. 对最新 v2 产物建立按 PX4 `time_boot_ms` 的事件线：两次合格 timeout、第二次命中事件、第三次 up 是否出现、`land_now` handoff、最后 up setpoint、ACK、mode、landed state 和 disarm。
2. 用现有 deterministic fixture 和 fault campaign 结果补齐负例：一次 timeout 后恢复、增益达到 0.5 m、时间戳倒退/重复、landing/hold、已落地、高空；确认第二次失败后没有第三个相同 climb primitive。
3. 若发现日志或解析器把 collector complete、`action_ack` 或旧摘要误判成 LAND 成功，只修复诊断/报告和针对性测试，不改变运行时安全语义。
4. 验收为：所有可观察字段逐项列出；触发 live 证据满足“无第三次 up + land_now 请求”，PX4 最终 landed 仅在 fresh `landed_state=1` 且 disarm 证据存在时标为通过；否则明确 gap。运行相关 pytest 和 CPU-only onboard gate。

**需要写入**

- 必要时只改诊断解析/测试；不得修改共享 artifacts。
- 最终报告：`docs/onboard-climb-land-evidence-audit-20261107.md`，包含输入路径/hash、事件时间线、live 与 fixture 分界、负例结果、LAND 证据等级、未完成项和是否仍允许重复爬升。

## 55. 固化 sensor timeout 与 PX4 clock stall 的失败可观测性契约

**目标**

把传感器首帧等待、PX4 clock stall 和 Gazebo stall 的诊断输出变成可审计契约，消除 28-head 旧 episode 没有 `host_pressure.jsonl`、`frame_wait_timeline` 或有效 clock/stats 证据的缺口。保持现有 15 秒墙钟等待、PX4 timeout、retry、任务 expected 和 stall 分类语义；本任务只改善失败时记录、离线分类和缺字段的明确标记，不把相关性写成因果。

**证据路径**

- `docs/accel-timeout-crash-20261107.md`、`docs/sensor-stream-timeout-20261107.md`、`docs/sensor-timeout-load-20261107.md`、`docs/timeout-host-trace-20261107.md`：当前 timeout、压力和墙钟对齐边界。
- `docs/gz-idle-stall-20261107.md`、`docs/gz-idle-stall-followup-20261107.md`：clock/stats/GDB 采集顺序和 unknown 语义。
- `docs/nightly-review-28-head-20261107.md`、`docs/nightly-review-29-head-20261107.md`：旧失败与新成功样本对照。
- `scripts/collect_sim_teacher_episodes.py`、`scripts/analyze_sensor_timeouts.py`、`scripts/px4_clock_stall_capture.py`、`tests/test_collect_attach.py`、`tests/test_analyze_sensor_timeouts.py`、`tests/test_px4_clock_stall_capture.py`。
- 新近 sensor/clock 作业产物若已完成：只读其 `frame_wait_timeline`、`host_pressure.jsonl`、MAVLink、clock/stats probe 和 capture ordering。

**约束**

- 不启动 SITL、Docker、GPU、训练、release-parity、真实飞行或 HIL；不重跑正在执行的 sensor/clock 作业。
- `artifacts/` 只读；新 fixture、汇总和日志写入 `.tmp/sensor-clock-observability/`。
- 不修改 15 秒预算、PX4 传感器阈值、retry、stall timeout、lockstep、world control 或任务 expected；未知字段必须保持 `unknown`/`missing`，不能用默认值伪造推进。
- Windows 行为保持不变；所有 Python 分析使用主 `.venv`，只跑相关 pytest 和 CPU-only gate。

**执行与验收**

1. 定义并实现 failure snapshot schema：单调时钟 start/end、primitive end、pose `time_boot_ms`、最后 frame sensor stamp、最后 PX4 `LOCAL_POSITION_NED`/`STATUSTEXT` 接收时刻、最近 pressure 样本、clock/stats probe 状态、GDB 顺序和最终失败分类。
2. 对正常成功、frame 有但 stamp 未过水位、PX4 clock 停止、sidecar 缺失、gzip MAVLink、探针超时和 GDB 先于 probe 的 synthetic fixture 做解析测试；缺失证据只能降低可判定性，不能改变原始失败类。
3. 对现有 28-head、29-head、r9r 和已完成新作业重跑离线分析，逐事件列出 pressure/trace 覆盖，区分“无新帧”“帧时间戳未过门槛”“PX4/Gazebo 同时停滞”和“仅相关负载”。
4. 验收为：每类 fixture 有稳定分类、字段缺失显式可见、旧目录分析结果不被改写、无任何因果过度声明；相关测试和 gate 通过。

**需要写入**

- 只改 collector/分析诊断、schema 适配和针对性测试；不改任务语义或共享 evidence-bound 文件。
- 最终报告：`docs/sensor-clock-observability-20261107.md`，包含契约字段表、fixture 结果、旧/新样本覆盖矩阵、未知项和后续是否有改阈值依据。

## 56. 完成 identity r10 训练后准入与 r9r 未见世界审计

**目标**

在当前 r10 GPU 训练作业结束后，使用其已经生成的评测产物完成 CPU-only admission；严格对照 r3 基线和 r10 计划的 v3/v1、冻结四视角 test、r9r 12 个 val world 以及 8 个 pair×view cell 的硬门。输出 go/no-go，任何门槛或证据缺失都保持生产模型 `r21id-r3`。本任务不训练、不重跑评测、不修改生产选择。

**证据路径**

- `docs/identity-r10-plan-20261107.md`、`docs/identity-r10-admission-20261107.md`、`docs/identity-r10-full-build-20261107.md`、`docs/identity-r10-train-job-20261107.md`：配方、数据入场、作业输出和硬门。
- `artifacts/r21id-r3/pipeline/r15-sft/`：冻结 r3 对照文件，只读。
- `artifacts/r21id-r10/pipeline/`、`artifacts/r21id-r10/evaluation/r3-four-view/`、`artifacts/r21id-r10/evaluation/r9r-val/`、`artifacts/r21id-r10/data/`：若作业已完成则只读检查；若文件缺失，记录为 evidence gap。
- `configs/scenarios/generated-r21id-r9r/index.json`、`scripts/eval_r21id_identity.py`、`tests/test_identity_r10_admission.py` 及相关评测报告生成器。

**约束**

- 不启动 GPU、训练、模型服务、SITL、Docker 或网络下载；不重跑 r10 作业或生成新的 proposals/SFT。
- 不修改 `configs/training/identity-r10-r21id.json`、`configs/release-parity.json`、`docs/current-progress.json`、schemas、product-version、生产模型或 `artifacts/`。
- 所有临时统计写入 `.tmp/identity-r10-eval-admission/`；输入和输出 hash 必须记录。CPU 分析使用主 `.venv`，只跑相关 pytest。

**执行与验收**

1. 读取 r10 `eval-v1.json`、`eval-v3.json`、r3/r10 四视角和 r9r val 成对 predictions，核对 identity accuracy ≥ 0.9185、`<12 px` recall ≥ 0.6216、distractor rejection ≥ 0.9828、116 条固定 false-accept ≤ 2。
2. 对 `car→mailbox`、`suv→postbox` 的 front/oblique/edge_truncated/recovery 逐 cell 统计独立 val world、有效行数、rejection rate ≥ 0.90、false accepts ≤ 1；列出 sample_id、world、target_id 和输入 hash。不得把行数当作模型 rejection rate。
3. 检查 r10 pipeline state、merged/export 产物、评测脚本版本和数据 mix provenance；任何缺失、越界或 split 泄漏都给出 NO-GO 和可复现缺口。
4. 验收为：固定集与 r9r val 各有逐条证据，结论唯一且可复核；只有所有硬门通过才提出候选替换，否则明确生产仍为 r3。运行相关单测，不做训练或模型修改。

**需要写入**

- 统计/manifest/hash：`.tmp/identity-r10-eval-admission/`。
- 最终报告：`docs/identity-r10-eval-admission-20261107.md`，包含门槛表、逐 cell 结果、false-accept 清单、输入 hash、缺失证据和生产 go/no-go。

## 57. 扩大 follow/track 在固定相机与云台飞机上的任务覆盖

**目标**

建立一组与 nightly main/gimbal 套件不同的 follow/track 回归：至少两个不同生成 world、一个 fixed-camera 任务和一个 `x500_gimbal` 变体，验证 `follow -> return_home`、目标在框比例、移动 track 连续性、丢失重获和最大丢失次数。普通飞机可以没有 gimbal；带 gimbal 的飞机必须同时模拟云台角度和四个螺旋桨的角度/转速，并在每个 `obs_before`/`obs_after` 记录。不得用添加 `area_search`、降低 follow 断言或修改任务预算来掩盖失败。

**证据路径**

- `configs/onboard/suite-follow-r21idr3-r24plan.json`、`configs/onboard/suite-follow.json`、`configs/onboard/suite-gimbal-r21idr3-r24plan.json`、相关 `configs/airframes/x500_mono_cam.json` 与 `configs/airframes/x500_gimbal.json`。
- `docs/nightly-review-29-head-20261107.md`：7 个 gimbal 任务通过但高层 gimbal feedback stale 的观察。
- `docs/gimbal-coverage-followup-20261107.md`、`docs/gimbal-suite-expand-20261107.md`：关节记录契约和 fixed-camera 对照。
- `docs/planner-round.md`、`docs/r30-planning-errors.md`：follow 首步必须直接为 follow、不能凭空插入 area_search 的规划风险。
- `scripts/run_mission_suite.py`、`scripts/collect_sim_teacher_episodes.py`、`src/multidetect/sim_teacher/streams.py`、`tests/test_mission_suite.py`、`tests/test_propeller_gimbal_recording.py`、follow 相关 scorer/测试。

**约束**

- 本任务需要 SITL：是；只允许一个串行 slot，设置 `MULTIDETECT_SITL_SLOTS=3`，只使用项目槽位 0–2，绝不访问槽位 3/4；不要与其他 SITL 并行。先 `--dry-run`，再运行有界的小套件，不重复 nightly 29/30 的同一任务集合。
- SITL 作业脚本内所有 Python 必须使用 `/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python`；`.venv` 只用于 gate 和 pytest。不得启动无关 Docker、训练、release-parity、真实飞行或 HIL，不联网下载。
- `artifacts/` 只读；新结果写入 `.tmp/campaigns/follow-track-fixed-gimbal-20261107/` 或新 campaign 目录，保留 slot、任务 ID、world、provenance 和失败证据。
- 不修改普通任务和 gimbal 任务的 expected、技能序列、follow 阈值或安全 supervisor 规则来过门。

**执行与验收**

1. 从现有 follow suite 复制语义到临时配置，明确 fixed-camera 与 `x500_gimbal` 的一一对应 mission ID，选择至少两个不同 world；配置校验必须证明 fixed-camera 没有 gimbal 字段，而 gimbal 任务声明 `gimbal_joint_state` 与 `propeller_joint_state`。
2. 串行运行每个任务一个有界 episode，审计 planner/mission 首步是否为 `follow`、无多余 `area_search`，并核对目标在框比例、连续 track、lost/reacquire、return_home 完成和 false confirm。
3. 对 gimbal episode 检查 RGB 时间戳对齐、pitch/yaw/roll 随控制有可解释变化、四 rotor 角度/转速有限且至少一个 spin 非零；对 fixed-camera 只要求其合法的相机字段，不强加 gimbal。
4. 验收为：每种 airframe 至少一个 world 通过完整 follow contract；失败必须归类为规划、track、sensor、clock/PX4 或基础设施，并保留原始证据。运行相关 pytest 和 CPU-only onboard gate。

**需要写入**

- 如确有缺口，只改 follow/gimbal 仿真、记录或临时 suite 适配的最小范围；不改 expected 阈值。
- 最终报告：`docs/follow-track-fixed-gimbal-20261107.md`，逐任务列出 world、airframe、规划首步、track 指标、joint-state 证据、失败根因、SITL slot 和残余覆盖。

## 58. 审计 gimbal 高层反馈年龄与 joint sidecar 的一致性

**目标**

解决 29-head 中 gimbal joint sidecar 全部 fresh、但高层 `gimbal_state.fresh=false`（尤其 survey 和 pause/resume）的剩余可解释性问题。明确控制器、观测记录和任务 scorer 在高层反馈过期而底层 joint state 新鲜时各自应做什么：不得把 stale pose 当作当前云台角度，也不得因缺高层字段而伪造 fixed-camera 或放宽任务断言。保持飞机可有或无 gimbal，且带 gimbal 时 propeller rotation 与 gimbal angle 必须真实模拟并记录。

**证据路径**

- `docs/nightly-review-29-head-20261107.md`：491 steps、982 joint samples 及 32 个高层 stale 样本。
- `docs/gimbal-coverage-followup-20261107.md`、`docs/gimbal-suite-expand-20261107.md`：关节采集拒绝条件与 per-step 契约。
- `docs/mission-metric-check-20261106.md`：fixed/gimbal scorer 误把非 count 任务纳入 count summary 的历史边界。
- `src/multidetect/sim_teacher/streams.py`、`scripts/collect_sim_teacher_episodes.py`、`scripts/eval_onboard_loop_campaign.py`、`tests/test_propeller_gimbal_recording.py`、`tests/test_gazebo_sensor_stream.py`、`tests/test_score_onboard_loop.py`。
- 只读 `artifacts/benchmarks/nightly-linux-r21idr3-r24plan-29-head/gimbal/` 的 `steps.jsonl` 和 reports。

**约束**

- 不启动 SITL、Docker、GPU、训练、release-parity、真实飞行或 HIL；使用已有 29-head 982 个 joint 样本和 synthetic fixtures，不重跑 gimbal 套件。
- `artifacts/` 只读；不修改 `min_count_exact`、`min_survey_coverage`、`min_alert_recall`、技能序列、普通 fixed-camera 配置或任何 evidence-bound 文件。
- 只能修复 simulator/observation freshness、字段契约和 scorer 统计；Windows 行为保持不变。Python 使用主 `.venv`，相关 pytest 后跑 CPU-only gate。

**执行与验收**

1. 建立高层 gimbal feedback、底层 joint sidecar、RGB sensor stamp 和控制命令的对齐表，区分“高层反馈过期但 joint fresh”“joint 过期/缺失”“fixed-camera 合法无 gimbal”。
2. 增加 fixture：stale 高层反馈不能覆盖 fresh joint 角度；joint 缺失时 `x500_gimbal` 帧被拒绝；fixed-camera 不因缺 gimbal 字段失败；四 rotor 角度/转速和三轴角度必须有限、长度正确且 spin/控制变化可审计。
3. 检查 survey/monitor/count summary 是否仍把不含 `count` 的任务计算入 count 指标；只修统计过滤，不改变任务通过语义。对已有 29-head 报告离线重算并说明差异。
4. 验收为：每个 stale/fresh 组合有明确行为和测试；旧 29-head 任务语义结果不被放宽；报告能解释 32 个 stale 样本是否影响任务决策，并由 gate 与相关 pytest 通过。

**需要写入**

- 代码/测试：只改 gimbal feedback/recording freshness 和 report 统计的最小范围。
- 临时审计表写入 `.tmp/gimbal-feedback-age-audit/`。
- 最终报告：`docs/gimbal-feedback-age-audit-20261107.md`，包含样本计数、四类对齐表、fixture 结果、改动、旧报告差异和残余风险。

