# Multi-Detect 下一批工作（2026-11-07）

以下任务按价值排序。每个任务都是可直接交给另一个 Codex lane 的完整提示；任务中的证据目录均按只读处理，任何新输出写入当前 worktree 的 `.tmp/` 或任务指定的新目录。

# 37. 复现并收敛 PX4/Gazebo clock stall 的证据边界

**目标**

把当前 `clock_stalled_alive` 从“PX4 时钟停止、Gazebo/PX4 仍存活但根因未知”推进到可区分的证据结论。重点验证 Gazebo runner 是否持续卡在 `PublishStats` 的 transport mutex，以及 topic 探针是否污染了锁状态；只有证据支持时才提出最小运行时修复，不能凭一次高负载快照改 timeout 或绕过 lockstep。

**证据路径**

- `docs/gz-idle-stall-20261107.md`：28-head stall、r9r capture、当前采集顺序和未知项。
- `docs/operator-abort-wall-20261107.md`：持续慢速推进与高负载对照。
- `artifacts/benchmarks/nightly-linux-r21idr3-r24plan-28-head/main/count-cones/campaign/gen_cnt2_w0000/episodes/episode-0000-4dec0c9f/`：只读目标 episode。
- `artifacts/r21id-r9rb/episodes/gen_r21id_r9r_w0003/ep-0/attempt-0/episodes/episode-0000-536f4b08/`：只读 r9r capture。
- `scripts/px4_clock_stall_capture.py`、`tests/test_px4_clock_stall_capture.py`。

**执行约束**

- 本任务需要 SITL：是；最多占用一个槽位，串行运行，设置 `MULTIDETECT_SITL_SLOTS=3`，只使用项目槽位 0–2，绝不访问槽位 3/4，也不要并行启动第二个 SITL。
- 只使用 owned disposable PX4/Gazebo SITL；不启动无关 Docker，不联网下载。不要做训练、release-parity、真实飞行或 HIL。
- 采集顺序必须先抓进程/线程和 PX4、Gazebo GDB，再运行 `gz topic` 探针；探针不得先于 GDB。对 `/world/default/clock` 与 `/world/default/stats` 使用有界超时，兼容 `pause`/`paused` 字段，未知必须保留为 unknown。
- `artifacts/` 只读；不得修改或重生成既有 episode、`configs/release-parity.json`、`docs/current-progress.json` 或 schemas。

**需要完成**

1. 审计现有 capture 的分类器、命令顺序和总时间预算；补齐能证明/不能证明的字段。
2. 用一个正常基线和一个有界的 `count-cones` 或 r9r 单槽 SITL 尝试验证：无探针污染时 runner 是否越过 `PublishStats`/`Step`，PX4 clock、WorldStatistics 和 camera/IMU 是否推进；若没有复现，记录停止条件，不能把“未复现”写成修复。
3. 如需改采集器或 parser，只改诊断路径并加针对性测试；不得向 world control service 发 pause、step、unpause。
4. 用 CPU-only onboard gate 和相关单测验证；报告所有 SITL 尝试的槽位、开始/结束时间、结果和证据文件。

**需要写入**

- 代码/测试（如确有必要）：`scripts/px4_clock_stall_capture.py`、`tests/test_px4_clock_stall_capture.py` 或同一诊断路径。
- 最终报告：`docs/gz-idle-stall-followup-20261107.md`。报告必须包含复现命令、capture 顺序、clock/stats/两进程栈的逐项证据、未知项、是否有足够证据修复以及未完成的验证；不得声称 GPU、渲染、PX4 lockstep 或某个外部服务是根因，除非新证据直接支持。

---

# 38. 建立 sensor timeout、frame wait 与主机压力的可审计时间线

**目标**

把 `Accel/MAG TIMEOUT`、15 秒 sensor stream 超时、PX4 时钟水位、frame server 水位和 `host_pressure.jsonl` 放到同一条可复核时间线中，区分“没有新相机帧”“帧时间戳未过门槛”“PX4/Gazebo 同时停滞”与“主机负载相关但未证因果”。保持现有 15 秒墙钟预算和 retry policy，除非新证据满足改码前提。

**证据路径**

- `docs/accel-timeout-crash-20261107.md`：四批 nightly 的 timeout 统计、失控 episode 和因果限制。
- `docs/sensor-stream-timeout-20261107.md`：28-head 第 81 步 obs_after 超时的精确水位。
- `docs/sensor-timeout-load-20261107.md`：`host_pressure.jsonl` 采样设计及历史缺口。
- `docs/timeout-host-trace-20261107.md`：r9r 与整机 trace 的墙钟对齐。
- `docs/achieved-delta-sensor-class-20261107.md`：当前 `sensor_stream_stalled` 分类。
- `scripts/collect_sim_teacher_episodes.py`、`scripts/analyze_sensor_timeouts.py`、`tests/test_analyze_sensor_timeouts.py`、`tests/test_collect_attach.py`。
- `artifacts/r21id-r9rb/episodes/` 及 `artifacts/benchmarks/nightly-linux-r21idr3-r24plan-28-head/`：只读 episode、MAVLink、frame 和 sidecar。

**执行约束**

- 本任务需要 SITL：是；最多占用一个槽位，串行运行，设置 `MULTIDETECT_SITL_SLOTS=3`，只使用槽位 0–2，绝不访问槽位 3/4。只做一条有界的 `comms-free-count` 或等价 sensor-stream 采集；无故障时保留成功时间线，不为制造 timeout 增加无界重试。
- 不改变 15 秒等待、PX4 timeout、retry 或任务 expected；不把相关性写成因果。不要训练、release-parity、真实飞行或 HIL，不联网下载。
- 共享 `artifacts/` 只读；新诊断输出只能写 `.tmp/` 或 SITL 本次新建的临时输出，不覆盖历史目录。

**需要完成**

1. 在 frame wait 的 start/end 诊断中记录 primitive 结束单调时钟、pose `time_boot_ms`、最后 frame sensor stamp、最后 PX4 `LOCAL_POSITION_NED`/`STATUSTEXT` 接收时间、最近 host-pressure 样本和失败分类；失败时尽力采集但不能覆盖原始错误。
2. 用现有 `analyze_sensor_timeouts.py` 对 r9r、28-head 和新采集结果做墙钟对齐，明确哪些 timeout 有 trace/pressure 覆盖，哪些没有。
3. 增加 synthetic/fixture 测试覆盖：帧有但时间戳未过水位、PX4 时钟停止、sidecar 缺失、gzip MAVLink、失败快照路径；确保 Windows 逻辑不变。
4. 运行相关单测、必要的单槽 SITL 验证和 CPU-only onboard gate；输出实际样本数、缺失字段和不能回答的问题。

**需要写入**

- 代码/测试：只写 collector/分析诊断和相应测试，不改任务语义。
- 最终报告：`docs/sensor-timeout-followup-20261107.md`。报告必须列出逐事件时间线、burst/quiet 统计的样本边界、sidecar 完整性、仍未证明的因果关系，以及是否有理由提出后续阈值变更；若没有足够证据，明确保持现状。

---

# 39. 验证重复爬升失败后的 onboard LAND 升级链

**目标**

闭合 `return_home` 在低空连续无高度增益 timeout 后的安全缺口：第二次符合条件的 climb timeout 后，系统必须停止重复发送相同的 `up` primitive，进入 `climb_failed_suspected_loss_of_control`，通过现有 supervisor 路径请求 `land_now`，并在 mission log、alerts、supervisor log 和最终状态中留下可审查证据。不得把一次 timeout、正常慢爬升、landing、hold 或已落地误判为失控。

**证据路径**

- `docs/onboard-climb-failure-20261107.md`：当前阈值、回放命中 step 41、已知 live 验证缺口。
- `docs/accel-timeout-crash-20261107.md`、`docs/operator-abort-wall-20261107.md`：step 38 坠落及其后的 27 次重复上升。
- `artifacts/benchmarks/nightly-linux-r21idr3-r24plan-28-head/main/operator-abort/campaign/gen_cnt2_w0003/episodes/episode-0000-06a1a47d/steps.jsonl`：只读正例。
- `src/multidetect/autonomy/mission_executor.py`、`src/multidetect/onboard_policy.py`、`scripts/run_onboard_fault_campaign.py`。
- `tests/test_mission_executor_remaining_gaps.py`、`tests/test_compare_nightly_arms.py` 及现有 onboard fault fixture。

**执行约束**

- 本任务需要 SITL：是；最多占用一个槽位，串行运行，设置 `MULTIDETECT_SITL_SLOTS=3`，只使用槽位 0–2。只运行一个有界的 owned disposable SITL 验证；不得启动真实飞行或 HIL。
- 优先复用现有 source-level fault campaign 和 replay fixture；不得伪造“PX4 已执行 LAND”的证据。若 live SITL 无法稳定注入两次无增益 timeout，保留可重复的边界测试并在报告中明确 live gap。
- 不改变 LAND 的 supervisor 优先级、不新增空中 disarm、不放宽阈值、不删除或放宽断言；不要训练、release-parity、联网下载或写 `artifacts/`。

**需要完成**

1. 检查 executor 将 primitive outcome 回灌 MissionExecutor 的路径，验证计数只接受纯上升、`return_home`、timeout、低空 airborne 且高度增益小于 0.5 m 的相邻 PX4 时间样本。
2. 增加一个确定性的两次失败 fixture，断言第二次后不再产生第三个相同 climb setpoint，终态、`land_now`、mission log、alerts、supervisor log 和 failure class 一致；覆盖单次 timeout 后恢复、取得进展、landing/hold、已落地、高空和时间戳倒退负例。
3. 在一个 owned SITL 任务中验证命令/事件边界；只记录实际可观察的 PX4 ack、mode、landed state 和是否继续发 up setpoint，不把 ack 当成已落地。
4. 运行相关单测、fault campaign、必要的 SITL 验证和 CPU-only onboard gate。

**需要写入**

- 代码/测试：只改 onboard executor、supervisor 或 fault fixture 的最小范围；不改 collector 的 vehicle-loss 规则。
- 最终报告：`docs/onboard-climb-land-followup-20261107.md`。报告必须给出状态机/事件时间线、SITL 与 fixture 的证据分界、未能验证的 PX4 LAND 最终执行部分，以及是否仍允许重复爬升。

---

# 40. 完成云台套件覆盖并证明角度与螺旋桨旋转被模拟和记录

**目标**

验证或修复 7 个 `x500_gimbal` 任务的真实回归覆盖：云台角度随控制变化被模拟、四个螺旋桨的角度/转速被模拟，且每个 step 的 `obs_before`/`obs_after` 都记录 `gimbal_joint_state` 与 `propeller_joint_state`。同时用至少一个 fixed-camera 对照确认普通飞机可以没有 gimbal，而任务断言、计数、survey、monitor、pause/resume 语义不能因云台而放宽。

**证据路径**

- `docs/gimbal-suite-expand-20261107.md`：7 任务、录制契约及当前验证范围。
- `docs/nightly-review-28-head-20261107.md`：28-head gimbal 的 sensor-stream、count 和 survey 失败。
- `docs/gimbal-source-assets.md`、`docs/gimbal-sitl-run.md`、`configs/onboard/suite-gimbal-r21idr3-r24plan.json`。
- `scripts/run_mission_suite.py`、`scripts/gazebo_depth_wire.py`、`src/multidetect/sim_teacher/streams.py`、`tests/test_propeller_gimbal_recording.py`、`tests/test_suite_airframe_camera_contract.py`。
- `artifacts/benchmarks/nightly-linux-r21idr3-r24plan-28-head/gimbal/`：只读历史 episode。

**执行约束**

- 本任务需要 SITL：是；最多占用一个槽位，串行运行 7 个任务，设置 `MULTIDETECT_SITL_SLOTS=3`，只使用槽位 0–2，禁止并行第二个 SITL。先做 `run_mission_suite.py --dry-run`，再按一个 worker 运行可承受的 7-task 套件或有界分批。
- 飞机可以有云台，也可以没有云台：不得给 fixed-camera 任务强加云台字段；但所有带云台的飞机都必须同时模拟螺旋桨旋转和云台角度。不得复制或放宽 expected 规则来掩盖失败。不要训练、release-parity、真实飞行或 HIL，不联网下载。
- 不修改或覆盖 `artifacts/`；新输出写入 `.tmp/` 或新 campaign 目录，保留 provenance、任务 ID、slot 和失败证据。

**需要完成**

1. 对 7 个任务执行配置校验，确认每个 `x500_gimbal` 任务都声明两个 per-step 字段，mission ID 唯一，普通任务仍可无 gimbal。
2. 运行回归并审计每个新 episode：字段存在、时间戳与 RGB 对齐、四个 propeller 角度/转速长度和有限性正确、至少有一个非零 spin，gimbal pitch/yaw/roll 在控制变化时有可解释变化。
3. 修复发现的 simulator/recording/contract 缺口并添加针对性单测；不能通过修改 `min_count_exact`、`min_survey_coverage`、`min_alert_recall` 或技能序列来过门。
4. 对照固定相机同语义任务，报告哪些差异是云台控制导致、哪些是基础设施失败；运行相关测试和 CPU-only onboard gate。

**需要写入**

- 代码/测试：只改云台/螺旋桨仿真、观测记录和契约校验的最小范围。
- 最终报告：`docs/gimbal-coverage-followup-20261107.md`。报告必须逐任务列出完成/失败、joint-state 样本完整性、角度/旋转证据、fixed-camera 对照、失败根因和未完成的 SITL 覆盖。

---

# 41. 完成 identity r10 的 CPU-only 数据入场审计

**目标**

在不训练、不启动 GPU annotate 的前提下，判断 r10 是否具备进入后续训练/评测 lane 的完整输入：r3 replay 80% + r9r additions 20% 的行数目标、train/val world 隔离、四视角与 pair 覆盖、每 cell 至少 3 个未见 val world、拒绝率和 false-accept 门槛，以及 full proposal/SFT 产物是否仍缺失。输出可执行的 go/no-go 清单，不能把 pilot CPU 重建当成最终模型证据。

**证据路径**

- `docs/identity-r10-plan-20261107.md`：基线、切分、配方和硬门。
- `docs/r9r-plan-20261106.md`、`docs/r9p-yield-20261106.md`、`docs/r9r-pilot-yield-20261107.md`、`docs/r9r-suv-postbox-positives-20261107.md`：r9r 采集、视角和正例修复证据。
- `configs/training/identity-r10-r21id.json`、`scripts/queue_r21id_round_r10.sh`。
- `artifacts/r21id-r3/pipeline/r15-sft/`、`artifacts/r21id-r9rb/gpu-annotate/`：只读现有 baseline/pilot。确认 `gpu-annotate-full` 当前是否存在；不得假设它存在。
- `.tmp/r9r-suv-postbox-r10-rebuild-20261107/` 若存在，只读核对其 manifest、pair-view counts 和 builder hash。

**执行约束**

- 本任务不需要 SITL；不得启动 SITL、Docker、GPU、模型服务或训练。
- 只读 `artifacts/`；所有重建、统计、manifest 和日志写入 `.tmp/identity-r10-admission/`。不得修改 `configs/training/identity-r10-r21id.json`、release-parity、progress、schema 或 product-version 文件。
- 允许运行 CPU builder 的 pilot/小样本重建和 `queue_r21id_round_r10.sh` 的 `DRY_RUN=1`；不得执行 `DRY_RUN=0`、不得下载权重、不得把新行写入共享数据。

**需要完成**

1. 复核 r3 replay 数据来源、r9r index 的 train/val split 和 world 泄漏；用 world×pair×view×polarity 统计有效 80/20 mix，记录重复/去重规则。
2. 对 pilot 运行或复核当前 CPU builder，报告四视角 pair/view 行数、独立 world 数、SUV 正例是否恢复、rejection cap=180 是否实际生效；区分 source_counts、最终去重行和 geometry/proposal 代理。
3. 检查 full `proposals.jsonl`/SFT 是否缺失，检查 queue dry-run 是否会把不存在的输入误当作可训练；必要时补一个 fail-closed 的 admission check 或单测。
4. 生成硬门表：v3 基线对照所需文件、四视角 test、r9r val cell、最少世界数、拒绝率、false accepts、尚缺证据；明确生产模型仍保持 r3。

**需要写入**

- 代码/测试：如发现 queue/admission 会在缺输入时继续，只做 fail-closed 的 CPU 校验和回归测试，不改训练配方。
- 审计产物：`.tmp/identity-r10-admission/` 下的 manifest/统计文件。
- 最终报告：`docs/identity-r10-admission-20261107.md`。报告必须给出可复现的 CPU 命令、输入输出 hash、split/mix/pair-view 表、缺失 full 产物、go/no-go 结论和进入训练前仍需另一 lane 完成的工作；不得声称 r10 已训练、已替换生产或已通过模型硬门。

---
