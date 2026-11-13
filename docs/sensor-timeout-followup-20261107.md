# Sensor timeout、frame wait 与主机压力时间线跟进（2026-11-07）

## 结论

本次只改 collector 的诊断记录和离线分析器，没有改变 15 秒墙钟等待、PX4 timeout、retry policy 或任务 expected。新的 `obs_before`/`obs_after` frame wait 会记录开始/结束 monotonic 与 wall clock、请求的 pose `time_boot_ms`、最后 frame sensor stamp、最后 `LOCAL_POSITION_NED`/`STATUSTEXT` 接收时间、最近 host-pressure 行和失败分类。失败时先写尽力诊断，再由原异常路径继续处理；诊断写入失败不会覆盖原始错误。

现有历史产物没有这些新字段，不能回填。当前证据仍不足以提出阈值变更，建议保持现状。

## 代码改动

- `scripts/collect_sim_teacher_episodes.py`：新增 `_frame_wait_diagnostics` 和 `wait_for_observation_frame`。等待起点以当前 host monotonic 记录；`obs_after` 使用 primitive 结束 monotonic 作为接收水位。失败分类区分 `no_new_frame_observed`、`frame_timestamp_below_pose_watermark`、`px4_clock_and_telemetry_stalled` 和未分类超时。首帧 readiness 仍使用原有 45 秒预算，15 秒 frame wait 未改变。
- `scripts/analyze_sensor_timeouts.py`：输出 `host_pressure_sidecar` 完整性、`frame_wait_timeline`、失败分类、timeout event 的 trace/pressure 覆盖和 burst/quiet 的 trace 样本边界。gzip MAVLink、递归 episode 读取和 Windows 分支行为保持不变。
- `tests/test_collect_attach.py`：fixture 覆盖有帧但 stamp 未过 pose 水位、无新帧且 PX4 时钟/遥测停滞、失败 snapshot 路径。
- `tests/test_analyze_sensor_timeouts.py`：fixture 覆盖 sidecar 缺失；原有 gzip MAVLink 与 trace 对齐测试继续通过。
- `.tmp/jobs/38-sensor-stream-trace.sh`：调度方可运行的一条有界 SITL 作业，只选择 `comms-free-count`，`MULTIDETECT_SITL_SLOTS=3`、`MULTIDETECT_SIM_GPU_AVOID=2,3`、单 worker，输出新目录 `artifacts/benchmarks/sensor-timeout-followup-20261107-comms-free-count`。本环境未启动该脚本。

## 历史样本与 sidecar 完整性

### r9r

当前只读目录有 **98 个 episode.json**，分析器统计 **83 Accel、0 Gyro、0 MAG timeout**。目录中 `host_pressure.jsonl` 为 **0/98**；新的 `frame_wait_timeline` 为 **0/98**。83 条 timeout 事件中 **7 条落在本次 trace 窗口，76 条没有 trace 覆盖**。这是旧 collector 产物，不能从缺失字段推断当时没有压力或没有 frame wait 事件。

整机 trace `/data/projects/Multi-Detect-env/runs/host-trace/20261107.jsonl` 在本次读取时有 **635 个样本**，覆盖 Unix wall `1794018323.7773263`–`1794037402.386895`（约 2026-11-07 10:25:23–15:43:22 +08:00）。其中 **59 个 r9r episode** 与 trace 窗口重叠，7 个重叠 episode 含 timeout，共 7 条 timeout event；7/7 有最近 trace 样本，0/7 有 episode pressure sample。按当前 trace 的 CPU PSI `some` 第 75 百分位 **45.42** 判 burst，59 个窗口的统计为：burst 6 timeout/33 no-timeout，quiet 1 timeout/19 no-timeout，Fisher 双侧 `p=0.40448`。这只是相关性统计，且事件样本少、sidecar 全缺失。

7 条可对齐 timeout 的 event（episode、Accel wall time、最近 trace CPU PSI some/load）如下：

| episode | wall time (+08:00) | CPU PSI some | load |
| --- | --- | ---: | ---: |
| `932cf359` | 2026-11-07 13:47:40.728 | 35.13 | 162.07 |
| `6bf881f2` | 2026-11-07 13:50:24.589 | 43.41 | 205.73 |
| `718d2c6d` | 2026-11-07 12:20:19.530 | 33.92 | 148.90 |
| `92a361ca` | 2026-11-07 14:24:50.819 | 47.97 | 215.89 |
| `f303e64c` | 2026-11-07 14:21:18.208 | 49.77 | 160.14 |
| `a921b212` | 2026-11-07 11:18:00.726 | 56.31 | 267.77 |
| `a704b2a6` | 2026-11-07 13:24:57.671 | 39.12 | 155.19 |

### 28-head

当前只读目录有 **17 个 episode.json**，分析器统计 **153 Accel、0 Gyro、2 MAG timeout**，与 main/gimbal 汇总一致。`host_pressure.jsonl` 与新 `frame_wait_timeline` 均为 **0/17**；这些 episode 均早于 trace 起点，故 timeout 事件没有 trace 覆盖。失败的 `comms-free-count` episode `episode-0000-33a49796` 有 11 条 Accel timeout，但旧 `episode.json` 的 `failure_class=px4_alive_other_failure`、`failure_record` 缺失；该记录没有可审计的 frame wait start/end。它只能作为旧快照，不能回答“无新帧”与“stamp 未过门槛”的区别。

### 新采集

由于调度环境明确禁止本 lane 启动 Docker/SITL，本次没有新 episode 样本，新增作业脚本只写入 `.tmp/jobs/`。预期成功结果是 1 个 `comms-free-count` episode，成功或失败都应保留 `episode.json` 中的 `frame_wait_timeline`；若发生 15 秒超时，失败行应同时包含最后 frame stamp、pose 水位、PX4/STATUSTEXT 接收时钟和最近 pressure sample。无故障时保留成功时间线，不增加重试。

## 逐事件时间线与可回答范围

对历史 timeout event，分析器现在输出：STATUSTEXT 的 `receive_wall_time`、最近整机 trace 样本、episode 是否完整落在 trace 窗口、最近 pressure sidecar 样本，以及 episode 的 frame wait timeline。当前 r9r 的 7 条可对齐 event 均有 trace 最近样本，但没有 episode pressure；28-head 失败 episode 没有 frame wait timeline。因而现阶段只能说部分 Accel timeout 与高 load/CPU PSI 同时出现，不能证明主机负载导致 timeout。

分类语义仅对新采集有效：

- `no_new_frame_observed`：等待期间没有收到更新的相机帧。
- `frame_timestamp_below_pose_watermark`：收到更新帧，但 sensor stamp 仍低于请求的 pose 水位。
- `px4_clock_and_telemetry_stalled`：未取得新帧，同时 PX4 pose 接收水位和 `time_boot_ms` 没有推进。
- `frame_wait_timeout_unclassified`：已有部分更新，但现有字段不足以支持上述分类。

这些分类是观测分类，不是根因判定；frame server 丢弃、Gazebo transport 停滞、PX4/Gazebo 同时停滞和主机争用仍需新采集字段或更细粒度 trace 才能区分。

## Burst/quiet 样本边界与 sidecar 缺口

burst 统计按 episode 窗口计数，不按 trace 行计数；边界写入分析器 `burst_quiet_sample_boundary`，包括 trace sample count、开始/结束 Unix wall time、CPU PSI cutoff 规则和覆盖 episode 数。当前 r9r 边界为 635 samples、59 covered episodes、cutoff 45.42；pressure sidecar 覆盖为 0/59。28-head 没有和当前 trace 做全目录重算的必要性证据，旧 episode 仍全部缺少 sidecar/timeline。

## 未证明的因果关系与后续阈值判断

尚未证明：Accel/MAG timeout 的直接原因；frame server 是否收到但拒绝了低水位帧；PX4 时钟是否在失败等待内持续推进；高 load、CPU PSI、F1/Gaius/Docker 或本 lane 服务是否是直接根因；PX4/Gazebo 同时停滞是否发生在所有 timeout episode。

现有样本不足以提出 15 秒阈值、PX4 timeout 或 retry policy 变更。保持现状，先运行一次有界 `comms-free-count` 采集，取得新时间线和 sidecar，再评估是否需要增加诊断而非调整行为。

## 验证

- `py_compile scripts/collect_sim_teacher_episodes.py scripts/analyze_sensor_timeouts.py`：通过。
- `tests/test_collect_attach.py tests/test_analyze_sensor_timeouts.py`：**13 passed**。
- `scripts/run_onboard_gate.py -q -p no:warnings`：**PASS (111s)**，CPU-only。
- 未启动 SITL、Docker、GPU 训练、HIL、真实飞行或网络下载；未写入共享 `artifacts/`。

## 建议的下一个任务

1. 运行 `.tmp/jobs/38-sensor-stream-trace.sh` 的单槽 `comms-free-count` 采集，检查成功/失败 episode 的 `frame_wait_timeline`、`host_pressure.jsonl`、MAVLink gzip/JSONL 和 `failure_class`；只写新目录 `artifacts/benchmarks/sensor-timeout-followup-20261107-comms-free-count`，完成后把逐事件统计写入 `docs/sensor-timeout-followup-sitl-20261107.md`。
2. 基于新采集目录与 `/data/projects/Multi-Detect-env/runs/host-trace/20261107.jsonl`，运行 `scripts/analyze_sensor_timeouts.py`，核对 frame stamp/pose 水位/PX4 接收时钟/pressure 覆盖，区分 `no_new_frame_observed`、`frame_timestamp_below_pose_watermark` 与 `px4_clock_and_telemetry_stalled`；不修改 15 秒等待、PX4 timeout、retry 或任务 expected，结果写入 `docs/sensor-timeout-followup-analysis-20261107.md`。

## 2026-11-07 实测回灌：comms-free-count

实际产物：`artifacts/benchmarks/sensor-timeout-followup-20261107-comms-free-count/comms-free-count/campaign/gen_cnt2_w0000/episodes/episode-0000-4470bdb1/`。运行完成且 `episode.json.status=complete`，共 93 步；`mavlink.jsonl` 中 Accel、Gyro、MAG timeout 均为 0，`failure_class`/`error` 为空。与文档原先按历史旧产物等待 timeout 的预测相比，本次没有传感器 timeout 事件，不能从“0”反推故障根因已经消失，只能说明该次有界采集没有触发。

frame wait 记录完整：`frame_wait_timeline` 共 372 行（每步的 started/satisfied 水位记录），0 行带 error，分类只有 `frame_wait_started` 和 `frame_wait_satisfied`；单次 wait 最大约 0.600 s，未接近 15 s 超时。PX4 `time_boot_ms` 从 26.996 s 推进到 309.576 s，相机 frame sensor stamp 从 26.996 s 推进到 309.576 s；93 步中有 3 个上升 primitive，但没有 timeout。由此可确认本次采集同时具备持续的 PX4 telemetry 和 frame 进展。

host-pressure sidecar 有 221 条样本，覆盖 `1794045522.049096`--`1794046622.7180965`；sidecar 统计的 CPU PSI some 平均约 35.70、load1 峰值 131.44。使用 `scripts/analyze_sensor_timeouts.py` 对 `/data/projects/Multi-Detect-env/runs/host-trace/20261107.jsonl` 于 2026-11-07 20:44 (+08:00) 重算得到 1234 个 trace sample、该 episode 覆盖 37 个 trace sample；本次输出的 burst cutoff 为 43.905，episode classified 为 quiet，`burst_vs_quiet` 为 0/0/0/1，Fisher 双侧 `p=1.0`。没有 timeout event，所以没有 event-level trace/pressure 对齐样本；这些压力值只能作为运行环境记录，不能证明或排除历史 timeout 的因果关系。

本次没有发现 `scripts/analyze_sensor_timeouts.py` 的明确缺陷，也没有调整 15 s frame wait、PX4 timeout、retry 或 expected。旧文档中 r9r/28-head 的缺失 sidecar/timeline 限制仍然成立；新 run 只证明采集链能够在无 timeout 的成功 episode 中落盘完整 frame timeline 和 pressure sidecar。
