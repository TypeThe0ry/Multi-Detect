# 夜间回归复核：nightly-linux-r21idr3-r24plan-29-head

输入：nightly-linux-r21idr3-r24plan-28-head、nightly-linux-r21idr3-r24plan-29-head

## nightly-linux-r21idr3-r24plan-28-head

完成任务：17/17

| 套件 | 完成 | 任务数 | 预期通过 | 失败类 |
| --- | ---: | ---: | --- | --- |
| gimbal | 3 | 3 | no | sensor_stream_stalled |
| main | 14 | 14 | no | clock_stalled_alive, px4_alive_other_failure, sensor_stream_stalled |

| 套件/任务 | 预期通过 | 失败类 | 失败文本/事件 | expected/reported | false findings | 时长(s) | RTF | Accel/Gyro/MAG TIMEOUT | stall capture |
| --- | --- | --- | --- | --- | ---: | ---: | ---: | --- | ---: |
| budget-reserve-return | yes | none | — | 3/— | 0 | 952.666 | 0.154 | 0/0/0 | 0 |
| comms-free-count | no | sensor_stream_stalled | TimeoutError: sensor stream produced no frame within 15.0s; skills; supervisor.final_decision; supervisor.reason; supervisor.return_home_started; supervisor.onboard_log_exists; supervisor.logged_decisions; supervisor.logged_reasons; supervisor.original_mission_completed; supervisor.ground_event_count; min_count_exact | —/— | — | 1865.596 | 0.153 | 1/0/0 | 0 |
| comms-free-patrol-timeout | yes | none | — | 3/— | 0 | 1068.364 | 0.159 | 0/0/0 | 0 |
| count-cones | no | clock_stalled_alive | TimeoutError: PX4 simulation clock stalled during primitive; step=6; wall_timeout_s=45; skills; min_count_exact | —/— | — | 334.707 | 0.064 | 0/0/0 | 1 |
| count-fire-hydrants | no | none | min_count_exact | 3/4 | 1 | 2348.952 | 0.18 | 0/0/0 | 0 |
| english-find-person | yes | none | — | —/— | — | 331.712 | 0.084 | 0/0/0 | 0 |
| find-suv-land | yes | none | — | —/— | — | 337.382 | 0.144 | 0/0/0 | 0 |
| gimbal/gimbal-count-cones | no | none | min_count_exact | 3/2 | 0 | 4159.53 | 0.107 | 0/0/0 | 0 |
| gimbal/gimbal-inspect-postbox | yes | none | — | —/— | — | 769.769 | 0.113 | 0/0/0 | 0 |
| gimbal/gimbal-survey-site-overlap | no | sensor_stream_stalled | TimeoutError: sensor stream produced no frame within 15.0s; skills; min_survey_coverage | —/— | — | 2260.111 | 0.147 | 0/0/0 | 0 |
| high-risk-monitor-link-loss | yes | none | — | 3/— | 0 | 1025.253 | 0.152 | 0/0/0 | 0 |
| inspect-postbox | yes | none | — | —/— | — | 661.813 | 0.147 | 0/0/0 | 0 |
| monitor-backpacks | yes | none | — | 3/— | 0 | 1912.319 | 0.182 | 0/0/0 | 0 |
| operator-abort | no | px4_alive_other_failure | TimeoutError: episode exceeded its hard wall-clock ceiling (4800s); skills; supervisor.final_decision; supervisor.reason; supervisor.return_home_started; supervisor.onboard_log_exists; supervisor.logged_decisions; supervisor.logged_reasons; supervisor.original_mission_completed; supervisor.abort_hover_before_return | —/— | — | 4933.314 | 0.144 | 1/1/1 | 0 |
| operator-pause-resume | yes | none | — | 3/— | 0 | 1430.063 | 0.167 | 0/0/0 | 0 |
| patrol-fence-backpacks | yes | none | — | 3/— | 0 | 4175.023 | 0.113 | 0/0/0 | 0 |
| survey-site-overlap | yes | none | — | 3/— | 0 | 1810.718 | 0.171 | 0/0/0 | 0 |

## nightly-linux-r21idr3-r24plan-29-head

完成任务：21/21

| 套件 | 完成 | 任务数 | 预期通过 | 失败类 |
| --- | ---: | ---: | --- | --- |
| gimbal | 7 | 7 | yes | 无 |
| main | 14 | 14 | yes | 无 |

| 套件/任务 | 预期通过 | 失败类 | 失败文本/事件 | expected/reported | false findings | 时长(s) | RTF | Accel/Gyro/MAG TIMEOUT | stall capture |
| --- | --- | --- | --- | --- | ---: | ---: | ---: | --- | ---: |
| budget-reserve-return | yes | none | — | 3/— | 0 | 683.768 | 0.213 | 0/0/0 | 0 |
| comms-free-count | yes | none | — | 3/3 | 0 | 1140.311 | 0.22 | 0/0/0 | 0 |
| comms-free-patrol-timeout | yes | none | — | 3/— | 0 | 1709.47 | 0.098 | 0/0/0 | 0 |
| count-cones | yes | none | — | 3/3 | 0 | 1193.147 | 0.214 | 0/0/0 | 0 |
| count-fire-hydrants | yes | none | — | 3/3 | 0 | 1305.12 | 0.208 | 0/0/0 | 0 |
| english-find-person | yes | none | — | —/— | — | 633.462 | 0.039 | 0/0/0 | 0 |
| find-suv-land | yes | none | — | —/— | — | 265.329 | 0.175 | 0/0/0 | 0 |
| gimbal/gimbal-count-cones | yes | none | — | 3/3 | 0 | 1181.801 | 0.241 | 0/0/0 | 0 |
| gimbal/gimbal-count-fire-hydrants | yes | none | — | 3/3 | 0 | 1541.356 | 0.232 | 0/0/0 | 0 |
| gimbal/gimbal-find-suv-land | yes | none | — | —/— | — | 270.199 | 0.171 | 0/0/0 | 0 |
| gimbal/gimbal-inspect-postbox | yes | none | — | —/— | — | 414.531 | 0.2 | 0/0/0 | 0 |
| gimbal/gimbal-monitor-backpacks | yes | none | — | 3/— | 1 | 1693.131 | 0.208 | 0/0/0 | 0 |
| gimbal/gimbal-operator-pause-resume | yes | none | — | 3/— | 0 | 1451.208 | 0.161 | 0/0/0 | 0 |
| gimbal/gimbal-survey-site-overlap | yes | none | — | 3/— | 0 | 2264.915 | 0.13 | 0/0/0 | 0 |
| high-risk-monitor-link-loss | yes | none | — | 3/— | 0 | 705.952 | 0.215 | 0/0/0 | 0 |
| inspect-postbox | yes | none | — | —/— | — | 483.635 | 0.202 | 0/0/0 | 0 |
| monitor-backpacks | yes | none | — | 3/— | 1 | 4456.781 | 0.078 | 0/0/0 | 0 |
| operator-abort | yes | none | — | 3/— | 0 | 679.493 | 0.212 | 0/0/0 | 0 |
| operator-pause-resume | yes | none | — | 3/— | 0 | 2303.418 | 0.098 | 0/0/0 | 0 |
| patrol-fence-backpacks | yes | none | — | 3/— | 0 | 2038.688 | 0.22 | 0/0/0 | 0 |
| survey-site-overlap | yes | none | — | 3/— | 0 | 1399.024 | 0.209 | 0/0/0 | 0 |

## 与上一架次对比

| 任务 | 预期通过变化 | 原因 |
| --- | --- | --- |
| comms-free-count | no → yes | — |
| count-cones | no → yes | — |
| count-fire-hydrants | no → yes | — |
| gimbal/gimbal-count-cones | no → yes | — |
| gimbal/gimbal-count-fire-hydrants | — → yes | — |
| gimbal/gimbal-find-suv-land | — → yes | — |
| gimbal/gimbal-monitor-backpacks | — → yes | — |
| gimbal/gimbal-operator-pause-resume | — → yes | — |
| gimbal/gimbal-survey-site-overlap | no → yes | — |
| operator-abort | no → yes | — |

## 结论与证据边界

29-head 使用代码 `8950a89929c9b97496400722cf27771ef82a37eb`，主套件 `14/14`、gimbal 套件 `7/7` 完成，两个套件的 `expectation_pass_rate` 都是 `1.0`。28-head 的 `17/17` episode 都结束了，但有 6 个任务没有达到预期：`comms-free-count`、`count-cones`、`count-fire-hydrants`、`operator-abort`，以及 `gimbal/gimbal-count-cones`、`gimbal/gimbal-survey-site-overlap`。下面的“通过”均指 suite-report 的 `expectations_passed`，不要与单任务 report 中较宽的 `success`/`task_equivalent` 字段混淆。

28-head 失败任务的 episode 证据如下：

| 任务 | episode 证据 | 为什么未达预期 |
| --- | --- | --- |
| `main/comms-free-count` | [`episode.json`](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-28-head/main/comms-free-count/campaign/gen_cnt2_w0000/episodes/episode-0000-33a49796/episode.json)、[`mission_summary.json`](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-28-head/main/comms-free-count/campaign/gen_cnt2_w0000/episodes/episode-0000-33a49796/mission_summary.json) | 15 秒内没有新 sensor frame；episode 在 step 81 结束，PX4 仍存活，摘要只有 lower-bound count `4`，`work` 与 `home` 都是 `unknown/no_recorded_completion`。该旧产物没有 `host_pressure.jsonl`，也没有 frame-wait timeline。失败时间为 2026-11-07 03:43–04:14 (+08:00)，早于 `/data/projects/Multi-Detect-env/runs/host-trace/20261107.jsonl` 的 10:25 起点，因此不能用整机 trace 归因；capture 的主机 load average 为 `265.71/274.75/275.98`，只能说明失败时现场很忙。 | 
| `gimbal/gimbal-survey-site-overlap` | [`episode.json`](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-28-head/gimbal/gimbal-survey-site-overlap/campaign/gen_cnt2_w0002/episodes/episode-0000-9670a9d2/episode.json)、[`mission_summary.json`](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-28-head/gimbal/gimbal-survey-site-overlap/campaign/gen_cnt2_w0002/episodes/episode-0000-9670a9d2/mission_summary.json) | 同样是 15 秒 sensor stream timeout；survey 已达到 `coverage=1.00`，但 `return_home` 没有记录完成。container inspect 在 5 秒内超时，失败诊断的 PX4 存活状态未知，清理时仍看到 PX4 running；capture load average 为 `471.01/326.17/269.71`。没有 episode pressure sidecar，且失败时间 05:57–06:34 (+08:00) 仍早于 host trace，故不能证明主机负载是根因。 |
| `main/count-cones` | [`episode.json`](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-28-head/main/count-cones/campaign/gen_cnt2_w0000/episodes/episode-0000-4dec0c9f/episode.json)、[`px4-clock-stall-capture.json`](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-28-head/main/count-cones/campaign/gen_cnt2_w0000/episodes/episode-0000-4dec0c9f/px4-clock-stall-capture.json)、[`px4-clock-stall-gdb.txt`](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-28-head/main/count-cones/campaign/gen_cnt2_w0000/episodes/episode-0000-4dec0c9f/px4-clock-stall-gdb.txt) | step 6 的 primitive 触发 45 秒 PX4 simulation-clock stall；摘要只保留 lower-bound count `1`，两个 mission step 都未完成。capture 显示 PX4 clock `49.116 s` 在 45 秒内未推进，`/world/default/clock` 没有 publisher、PX4 线程处于 sleep/futex/poll，Gazebo transport 线程在 poll；分析明确把 bridge/lockstep 边界作为可能方向，但 `messages_stopped_arriving=false`、`callback_blocked=false`、`scheduling_starvation=false`，所以不能把根因收窄到某个单一线程或主机争用。 |
| `main/count-fire-hydrants` | [`report.json`](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-28-head/main/count-fire-hydrants/report.json)、[`mission_summary.json`](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-28-head/main/count-fire-hydrants/campaign/gen_cnt2_w0001/episodes/episode-0000-6931667c/mission_summary.json) | 任务完整返航，但期望 `3`、报告 `4`，`false_findings=1`，因此 `min_count_exact` 失败。摘要把 count 标为 `lower_bound=true`、`reason=budget_exhausted`，并有一个 rejection revisit `no_clear_viewpoint`。28-head 的融合估计在约 `1.949 m` 分离、合并 sigma 约 `1.741 m` 时没有进入 duplicate 分支；这正是后续 dedupe calibration margin 要覆盖的边界。 |
| `gimbal/gimbal-count-cones` | [`report.json`](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-28-head/gimbal/gimbal-count-cones/report.json)、[`mission_summary.json`](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-28-head/gimbal/gimbal-count-cones/campaign/gen_cnt2_w0000/episodes/episode-0000-b9b2f9ff/mission_summary.json) | 任务完成并返航，但期望 `3`、报告 `2`，`count_is_lower_bound=true`、`reason=budget_exhausted`；一个小目标 revisit 在剩余预算耗尽时为 `interrupted`，所以 `min_count_exact` 失败。不是 PX4/传感器故障。 |
| `main/operator-abort` | [`episode.json`](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-28-head/main/operator-abort/campaign/gen_cnt2_w0003/episodes/episode-0000-06a1a47d/episode.json)、[`steps.jsonl`](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-28-head/main/operator-abort/campaign/gen_cnt2_w0003/episodes/episode-0000-06a1a47d/steps.jsonl) | abort 后进入 `return_home`，但 step 38 出现约 `18.7 m → 0.8 m` 的未命令坠落、roll 约 `171°`；随后 27 个 `up 15 m` primitive 连续 timeout，PX4 仍报告 airborne，最终耗尽 4800 秒墙钟。episode 当时没有 `vehicle_lost_control` 早停或 climb-protection event，故分类仍是 `px4_alive_other_failure`。完整失控时间线见 [`docs/accel-timeout-crash-20261107.md`](accel-timeout-crash-20261107.md)。 |

28-head 的两个 sensor timeout 都没有 `host_pressure.jsonl`；现有 `/data/projects/Multi-Detect-env/runs/host-trace/20261107.jsonl` 只覆盖 10:25–次日更晚时段，不能回填 03:43–06:34 的缺失窗口。29-head 每个 episode 都有 pressure sidecar；其 7 个 gimbal episode 的 load1 峰值约 `130.01–199.69`，CPU PSI `some.avg10` 峰值约 `38.33–55.41`，但全部完成，说明“高负载同时出现”不能单独解释 28-head 的失败，也不能证明 29-head 的成功由某个压力阈值造成。

## 28-head 到 29-head 的 onboard 变更对照

- **计数 dedupe margin（`230c280b`）**：新增 `count_dedupe_sigma_factor=1.15`，把不确定度分支从 `min(count_dedupe_m, sigma)` 改为 `min(count_dedupe_m, 1.15*sigma)`，仍受 `count_dedupe_m=4.0 m` 上限约束。29-head `main/count-fire-hydrants` 从 `3/4、1 false finding` 变为 `3/3、0`，`gimbal-count-fire-hydrants` 也达到 `3/3`；这与变更要修复的 1.949 m/1.741 m 边界一致，但单次 nightly 不能证明唯一因果。
- **climb protection（`e1d5a6bf`、`874719d2`）**：连续两次纯 `return_home up` timeout 且相邻 NED 高度增益小于 `0.5 m`、低于 `2.5 m` 仍 airborne 时，终止重复爬升，记录 `climb_failed_suspected_loss_of_control`，通过 supervisor 请求 `land_now`。这直接覆盖 28-head operator-abort 在 step 41 命中的重复无增益爬升模式；29-head operator-abort 在 `679.493 s` 完成，suite `expectations_passed=true`，未再耗尽 4800 秒。29-head 该正常任务没有触发保护 event，因此本 nightly 证明的是“不再重演失败”，不是一次 live climb-fault 注入验证。
- **lost-control stop（`551e9f2a`）**：collector 依据未命令高度损失或近地持续翻滚早停并标记 `vehicle_lost_control`。28-head operator-abort 的 step 38 正好满足回放规则，但旧 episode 是在该改动前生成，仍继续到 wall ceiling；29-head 14 个 main 与 7 个 gimbal 均无 `vehicle_lost_control`，因此没有误报证据。该改动主要改善 SIM collector 的失败归因/早停，不应被解释成 onboard runtime 已在 28-head 中执行过保护。
- **sensor/stall 诊断（`0e12af2c`）**：将 sensor-frame timeout 归为 `sensor_stream_stalled` 并保留 stall capture；同时修正 primitive 的实测位移口径。29-head 没有 sensor timeout，不能在本 nightly 直接验证新分类，但 28-head 两个旧 timeout 的证据缺口（无 pressure sidecar、无 frame-wait timeline）仍然存在。

## 29-head gimbal step 级状态审计

对七个 gimbal mission 的 `steps.jsonl` 全部 `obs_before`/`obs_after` 样本逐项检查：每个样本都有 `gimbal_joint_state` 和 `propeller_joint_state`，来源均为 `gazebo_joint_state`；三轴位置/速度均为有限数，joint `fresh=true`，gimbal feedback 没有 `out_of_limits=true`，四个 rotor joint 名称始终为 `rotor_0_joint`…`rotor_3_joint`，四路 `spin_rad_s` 均为有限数。共 `32` 个样本的高层 `gimbal_state.fresh=false`（见表），但对应 joint sidecar 仍 fresh；这应作为反馈年龄的观察项，而不是关节缺失或越界。

| gimbal 任务 | steps | joint samples | gimbal position range (pitch/roll/yaw, rad) | rotor spin range (rad/s) | 高层 gimbal stale / out-of-limit |
| --- | ---: | ---: | --- | --- | ---: |
| `gimbal-count-cones` | 89 | 178 | `[-0.000336,0.000288] / [-0.001394,0.001495] / [-0.027158,0.023842]` | `47.13..100 / 45.44..98.66 / -100..-49.72 / -98.55..-47.68` | `1 / 0` |
| `gimbal-count-fire-hydrants` | 108 | 216 | `[-0.000353,0.000308] / [-0.001434,0.001619] / [-0.024980,0.023727]` | `48.13..100 / 45.88..98.61 / -100..-47.98 / -98.53..-46.42` | `1 / 0` |
| `gimbal-find-suv-land` | 23 | 46 | `[-0.001115,0.000450] / [-0.000912,0.000088] / [-0.000028,0.009373]` | `15.17..91.05 / 15.00..89.24 / -85.52..-15.17 / -83.81..-15.00` | `1 / 0` |
| `gimbal-inspect-postbox` | 24 | 48 | `[-0.000228,0.000236] / [-0.001081,0.000931] / [-0.009424,0.016849]` | `55.21..98.47 / 53.65..96.65 / -100..-69.30 / -98.41..-67.91` | `1 / 0` |
| `gimbal-monitor-backpacks` | 107 | 214 | `[-0.000090,0.000533] / [-0.001170,0.001081] / [-0.024948,0.021625]` | `48.38..100 / 46.81..98.58 / -100..-45.37 / -98.52..-43.43` | `2 / 0` |
| `gimbal-operator-pause-resume` | 85 | 170 | `[-0.000099,0.000434] / [-0.001014,0.000875] / [-0.021534,0.022698]` | `49.69..100 / 47.78..98.51 / -100..-50.42 / -98.54..-49.06` | `7 / 0` |
| `gimbal-survey-site-overlap` | 48 | 96 | `[-0.000014,0.000178] / [-0.001019,0.000807] / [-0.013188,0.019954]` | `49.89..100 / 48.27..98.38 / -100..-53.33 / -98.50..-51.66` | `19 / 0` |

总计 `491` steps、`982` before/after joint samples，gimbal joint 缺失 `0`、joint stale `0`、rotor 格式/有限性错误 `0`、越界 `0`。高层反馈 stale 的最高比例出现在 survey（19/96），但该任务仍以 `coverage=1.00`、`return_home=arrived` 完成；应在后续专门验证高层反馈 age 与 joint sidecar 的一致性，而不能只看任务通过率。

## 未完成项与下一步

本次没有启动 SITL、Docker、GPU 训练或网络下载，也没有修改 artifacts。sensor timeout 的旧 28-head episode 缺少 sidecar/frame-wait 时间线，host trace 不覆盖其墙钟窗口；因此本报告不把高 load、Accel timeout、Gazebo transport 或 PX4 进程存活中的任何一个宣称为唯一根因。climb protection 与 lost-control stop 的代码和回放证据已对齐，但 29-head 没有专门故障注入，尚未验证真实 PX4 LAND 的最终执行和 mode/landed ACK。

## 建议的下一个任务

1. 运行 `.tmp/jobs/38-sensor-stream-trace.sh` 的单槽 `comms-free-count` SITL 采集，读取新 episode 的 `frame_wait_timeline`、`host_pressure.jsonl`、MAVLink 和 `/data/projects/Multi-Detect-env/runs/host-trace/20261107.jsonl` 对齐结果；只写新目录 `artifacts/benchmarks/sensor-timeout-followup-20261107-comms-free-count`，将逐事件分类和 pressure 覆盖写入 `docs/sensor-timeout-followup-sitl-20261107.md`，不要修改既有 nightly artifacts、15 秒等待、retry 或任务 expected。
2. 运行 `.tmp/jobs/41-onboard-climb-fault-sitl.sh` 的单槽 `operator-abort` 故障注入，核验两次低空无增益 climb timeout 后的 `climb_failed_suspected_loss_of_control`、停止第三次爬升、`land_now` handoff、mission log/alerts/final summary 与 PX4 LAND ACK；只写新目录 `artifacts/benchmarks/onboard-climb-fault-sitl-*`，将实测状态机时间线写入 `docs/onboard-climb-land-followup-20261107.md`，不要改 LAND 优先级、阈值、vehicle-loss 规则、既有断言或共享 artifacts。
