# 云台覆盖跟进（2026-11-07）

## 结论

离线配置校验和 CPU 测试通过；修复了 gimbal 采集入口可在 rotor 或云台关节缺失时接收帧的问题。当前环境禁止启动 Docker/SITL，因此本次没有新 episode，不能声称已证明七个任务中的角度响应、螺旋桨旋转或逐 step 记录。可执行的串行作业位于 `.tmp/jobs/01-gimbal-coverage-20261107.sh`。

配置 `configs/onboard/suite-gimbal-r21idr3-r24plan.json` 含七个唯一 mission ID，全部声明 `gimbal_joint_state` 与 `propeller_joint_state` 两个 per-step 字段。四个新增变体与 `suite-main-r21idr3-r24plan.json` 的普通任务保留完全一致的 `expect`、场景索引和 world；暂停/恢复变体使用自己的唯一 `mission_id`。普通主套件任务没有被加上 airframe 或 recording 字段。配置检查没有修改技能序列、`min_count_exact`、`min_survey_coverage` 或 `min_alert_recall`。

采集器现在为 `x500_gimbal` 的 RGB 帧要求同一因果关节样本含齐三个 gimbal 轴和四个 rotor。关节不足或过期时拒绝该帧，避免用缺状态的 RGB 假装完成录制；普通/旧机型仍可使用不完整或缺失的可选关节样本。完整 rotor 状态保留四个角度和四个角速度，完整 gimbal 状态保留 pitch/yaw/roll 角度和角速度。已有 recorder 会把 `obs_before` 和 `obs_after` 各自保存的 joint-state sidecar 写入 `steps.jsonl`；本次单测覆盖了这两侧的写入。

## 七任务状态

| 任务 ID | 本次状态 | 新 episode 样本完整性与运动证据 | 已知历史结果 |
| --- | --- | --- | --- |
| `gimbal-inspect-postbox` | SITL 未运行；配置/代码门禁通过 | 无新 episode，未审计 before/after 字段、RGB 时间戳、四 rotor 有限值/非零 spin 或控制后的角度变化 | 28-head 报告为任务通过，但无 episode 文件可供 joint-state 审计 |
| `gimbal-survey-site-overlap` | SITL 未运行；配置/代码门禁通过 | 同上，覆盖证据待作业运行 | 28-head 为 `sensor_stream_stalled`，且 survey/技能断言未通过；需从新 stall capture 与 episode 判断基础设施原因或控制相关影响 |
| `gimbal-count-cones` | SITL 未运行；配置/代码门禁通过 | 同上，覆盖证据待作业运行 | 28-head 计数期望 3、报告 2，`min_count_exact` 未通过；不能由该日志归因于关节记录问题 |
| `gimbal-find-suv-land` | SITL 未运行；配置/代码门禁通过 | 无新 episode，未证明关节完整性或角度/旋转变化 | 不在 28-head 三项 gimbal 任务中，尚无历史对照 |
| `gimbal-monitor-backpacks` | SITL 未运行；配置/代码门禁通过 | 同上，覆盖证据待作业运行 | 不在 28-head 三项 gimbal 任务中，尚无历史对照 |
| `gimbal-operator-pause-resume` | SITL 未运行；配置/代码门禁通过 | 同上，覆盖证据待作业运行 | 不在 28-head 三项 gimbal 任务中，尚无历史对照 |
| `gimbal-count-fire-hydrants` | SITL 未运行；配置/代码门禁通过 | 同上，覆盖证据待作业运行 | 不在 28-head 三项 gimbal 任务中，尚无历史对照 |

本次只在历史只读目录 `artifacts/benchmarks/nightly-linux-r21idr3-r24plan-28-head/gimbal/` 找到三份任务报告/日志，没有 `steps.jsonl`。因此没有可复核的历史 joint-state 样本完整性、RGB 同步、spin 或控制变化数据。`nightly-review-28-head-20261107.md` 同时记录普通任务 `count-fire-hydrants` 也出现 3/4 计数，且 `comms-free-count` 有 sensor stream stall；所以 gimbal count/survey 差异不能仅凭现有历史证据归咎于云台。要区分控制影响与基础设施失败，必须比较本次新 episode 的状态轨迹、失败诊断和普通 fixed-camera 同语义任务结果。

## 对照与验证

- 固定相机对照使用主套件中的 `find-suv-land`、`monitor-backpacks`、`operator-pause-resume`、`count-fire-hydrants`。静态测试核对这些任务没有 `airframe`/`recording` 字段；四个 gimbal 变体的规则和场景输入与对应普通任务一致。
- 静态对照能证明没有把云台字段强加给普通任务，也没有放宽语义断言；本次未重新运行普通任务 SITL，无法给出新鲜的 pass/fail 对照结果。
- 已运行 `run_mission_suite.py --suite configs/onboard/suite-gimbal-r21idr3-r24plan.json --workers 1 --dry-run`：成功生成全部七个任务命令和配置。
- 相关测试：`tests/test_gazebo_sensor_stream.py`、`tests/test_sim_teacher_segmentation_transport.py`、`tests/test_propeller_gimbal_recording.py`、`tests/test_suite_airframes_resolve.py`、`tests/test_mission_suite.py`，全部通过。
- CPU-only gate：`scripts/run_onboard_gate.py -q -p no:warnings`，通过，129 秒。
- `git diff --check` 通过。测试期间 Docker 网络探测给出不可用警告并回退到 loopback；这不影响测试结果，也与禁止 Docker/SITL 的环境约束一致。

作业脚本以一个 worker 串行执行七个 mission，每项一个 episode，设置 `MULTIDETECT_SITL_SLOTS=3`、`MULTIDETECT_SIM_GPU_AVOID=2,3`、只选择 GPU 0/1，并将输出写到 `.tmp/campaigns/gimbal-coverage-followup-20261107`（可通过 `GIMBAL_COVERAGE_OUT` 改写）。因 `artifacts/` 是只读共享目录，脚本不会向其中写输出。运行后应逐 episode 核对 `episode.json`、`steps.jsonl`、RGB 图像、失败日志与 report：每个 before/after 都应有两类 joint state，状态 timestamp 应等于对应 RGB 的 sensor timestamp；gimbal 三轴与 rotor 四角度/四 spin 必须有限，至少一个 spin 非零；gimbal 控制分发前后 pitch/yaw/roll 应有与命令和机型坐标轴一致的可解释变化。将同语义 fixed-camera 普通任务结果与 gimbal 变体对照，分别记录任务语义差异和 sensor/clock/PX4 基础设施失败。不要根据单个 count/survey 失败改写 expect 规则。

## 建议的下一个任务

1. 在允许 Docker/SITL 的 workstation 上运行 `.tmp/jobs/01-gimbal-coverage-20261107.sh`，单 worker 串行执行 `configs/onboard/suite-gimbal-r21idr3-r24plan.json` 七项任务。审计输出中的全部 episode：核对 `obs_before`/`obs_after` 的 gimbal/propeller 状态、时间戳与 RGB 对齐、三轴角度和四 rotor 角度/转速有限且至少一个 spin 非零，并将角度变化与 gimbal 控制分发关联；保留每项任务 ID、slot、provenance、report 和失败证据。不要修改任务 expected 阈值/技能序列，不要写入 `artifacts/`，不要训练或运行 HIL/真实飞行。更新 `docs/gimbal-coverage-followup-20261107.md`，逐项写入实际通过/失败与原因。
2. 完成第一项回归后，针对任何未闭合项复核 gimbal 与 fixed-camera 同语义对照，区分传感器/时钟/PX4 基础设施故障和角度控制造成的任务差异。只修复仿真、记录或契约范围内的最小缺陷并添加针对性单测；不得放宽计数、survey、alert 或 pause/resume 断言，不得给普通固定相机任务强加 gimbal 字段，也不得修改 `artifacts/`。重新运行相关单测和 CPU-only onboard gate，并把根因、改动及残余 SITL 覆盖写入 `docs/gimbal-coverage-followup-20261107.md`。
