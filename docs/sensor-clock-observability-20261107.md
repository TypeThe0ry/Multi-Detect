# sensor timeout 与 PX4 clock stall 可观测性契约

日期：2026-11-07

本次固化失败证据的记录和离线解释边界。15 秒首帧等待、PX4 primitive timeout、retry、stall timeout、lockstep、world control 和任务 expected 均未修改。`artifacts/` 只读；离线汇总写入 `.tmp/sensor-clock-observability/`。

## 实现

- [scripts/collect_sim_teacher_episodes.py](../scripts/collect_sim_teacher_episodes.py) 在失败收尾写入 `failure_snapshot`，并把 primitive 结束时刻与实际 frame wait 开始时刻分开。原有 `failure_class`、`frame_wait_timeline` 和 stall capture 保留。
- [scripts/analyze_sensor_timeouts.py](../scripts/analyze_sensor_timeouts.py) 增加 schema v1 适配器。旧 episode 没有 snapshot 时按现有文件重建，缺字段显式写 `missing` 或 `unknown`；不会用默认值改变失败类。
- [src/multidetect/sim_teacher/recording.py](../src/multidetect/sim_teacher/recording.py) 在 `mark_finished` 时补齐 snapshot 的 episode 结束单调时钟。

## 契约字段

| 区域 | 字段 | 语义 |
| --- | --- | --- |
| `clocks` | `episode_start_monotonic_ns`、`episode_end_monotonic_ns` | episode 墙钟边界对应的单调时钟；旧目录缺失时为 `null`。 |
| `clocks` | `wait_start_monotonic_ns`、`wait_end_monotonic_ns`、`primitive_end_monotonic_ns` | 首帧等待实际开始/结束及 `obs_after` 前一个 primitive 的结束水位。 |
| `pose` | `time_boot_ms`、`requested_time_boot_ms` | 最后 PX4 pose 与等待门槛的 simulation clock。 |
| `frame` | `last_sensor_stamp_ns`、`required_sensor_stamp_ns`、接收时刻、`frames_received` | 最后 frame 是否存在以及是否过 pose watermark。 |
| `px4` | `LOCAL_POSITION_NED` / `STATUSTEXT` 接收单调时刻、墙钟时刻、最后文本 | MAVLink 接收证据；gzip 与普通 JSONL 都可读。 |
| `pressure` | `status`、`sample` | 最近 sidecar 样本；只有实际读取到样本才是 `present`。 |
| `probes` | `clock`、`stats`、`gdb_ordering`、`gdb_before_probe` | Gazebo clock/stats probe 状态和采集顺序；探针超时为 `timeout`，顺序未知为 `unknown`。 |
| `evidence` | `frame_wait_timeline`、`host_pressure`、`clock_capture`、`mavlink` | 文件/记录覆盖状态；gzip 标记为 `present_gzip`。 |
| 顶层 | `failure_class`、`final_failure_classification`、`offline_classification` | 原始失败类和离线证据类分开保存，不把相关性写成因果。 |

离线证据类稳定取值包括：`no_new_frame_observed`、`frame_timestamp_below_pose_watermark`、`px4_clock_and_telemetry_stalled`、`px4_gazebo_both_stalled`、`related_load_only`、`unknown`。`related_load_only` 只有在有 pressure 样本且没有更强的 frame/clock 证据时才使用。

## Synthetic fixture 结果

相关测试覆盖以下 fixture：

| fixture | 原始失败类 | 离线证据类 | 缺失/异常处理 |
| --- | --- | --- | --- |
| 正常成功 | `unknown`（无失败） | `unknown` | snapshot 仍存在，frame/clock 证据按 `missing` 表示。 |
| 有 frame 但 stamp 未过水位 | 保持调用方失败类 | `frame_timestamp_below_pose_watermark` | 保留最后 stamp 与 required stamp。 |
| PX4 clock/telemetry 停止 | 保持调用方失败类 | `px4_clock_and_telemetry_stalled` | 不推断 Gazebo 根因。 |
| sidecar 缺失 | 保持调用方失败类 | 原分类不变，证据降为 `missing`/`unknown` | 不伪造 pressure。 |
| gzip MAVLink | 保持调用方失败类 | 原分类不变 | `evidence.mavlink=present_gzip`。 |
| clock/stats probe 超时 | 保持调用方失败类 | 由已有 capture 分析决定，否则 `unknown` | probe 状态为 `timeout`。 |
| GDB 先于 probe | 保持调用方失败类 | 不改变原始失败类 | `gdb_before_probe=false`，仅记录顺序事实。 |
| 仅有 pressure 相关性 | `px4_alive_other_failure` | `related_load_only` | 仅在 pressure 实际存在时成立。 |

## 旧/新样本覆盖矩阵

离线重跑输出位于 `.tmp/sensor-clock-observability/`，包括 `28-head.tsv`、`29-head.tsv`、`r9r.tsv`、`sensor-followup.tsv`、`clock-followup.tsv` 和 `coverage-summary.json`。逐事件结果也写入 `failure-events.json`。

| 样本 | episode 数 | status | pressure sidecar | frame timeline | clock capture | MAVLink/说明 |
| --- | ---: | --- | --- | --- | --- | --- |
| 28-head | 17 | 13 complete / 4 failed | 0/17 | 0/17 | 1/17 | 4 个失败保留旧 raw class；只有 count-cones capture 存在，clock probe timeout，stats/GDB 顺序缺失。 |
| 29-head | 21 | 21 complete | 21/21 | 成功样本不要求失败 timeline | 0/21 | 全部完成；高 load 与通过同时出现，不能推出阈值因果。 |
| r9r (`artifacts/r21id-r9rb`) | 98 | 96 complete / 2 failed | 0/98 | 0/98 | 2/98 | 两个 clock stall 的 MAVLink 为 gzip；clock/stats probe 均 timeout，capture 没有 ordering 元数据。 |
| sensor follow-up | 1 | 1 complete | 1/1 | 1/1 | 0/1 | 新 episode 有 pressure 和 frame timeline，未触发失败。 |
| clock follow-up | 2 | 2 complete | 2/2 | 2/2 | 0/2 | baseline/repro 均完成，未触发 stall capture。 |

28-head 失败逐事件覆盖：

| 事件 | 原始失败类 | 离线证据类 | pressure | frame/trace | clock/stats/GDB |
| --- | --- | --- | --- | --- | --- |
| `main/comms-free-count` | `px4_alive_other_failure` | `unknown` | missing | frame timeline missing；host trace 未覆盖 03:43–04:14 (+08:00)；MAVLink present | missing / missing / unknown |
| `gimbal/gimbal-survey-site-overlap` | `px4_status_unknown` | `unknown` | missing | frame timeline missing；host trace 未覆盖 05:57–06:34 (+08:00)；MAVLink present | missing / missing / unknown |
| `main/count-cones` | `clock_stalled_alive` | `unknown` | missing | frame timeline missing；MAVLink present | timeout / missing / unknown |
| `main/operator-abort` | `px4_alive_other_failure` | `unknown` | missing | frame timeline missing；MAVLink present；没有 stall capture | missing / missing / unknown |

r9r 的两个 clock stall（`episode-0000-536f4b08`、`episode-0000-60ce09c6`）均为 `clock_stalled_alive`；MAVLink 是 `present_gzip`，clock/stats 两次均为 `timeout`，GDB 顺序字段缺失，因此最终证据类保持 `unknown`。这不是把 timeout probe 解释成 PX4/Gazebo 双停。28-head 的 4 个失败在离线适配后也都保持 `unknown`，因为旧 timeline 与 pressure sidecar 均缺失，capture 没有足够的 clock/stats 配对证据。

## 未知项与阈值结论

旧 28-head 和 r9r 产物没有可回填的 `host_pressure.jsonl` 或 frame-wait timeline；host trace 的起点晚于 28-head 两个 sensor timeout。旧 capture 也没有统一 ordering 字段，故 GDB 是否先于 probe 只能标为 `unknown`。这些缺口降低可判定性，不改写原始失败类。

29-head 与 follow-up 的成功样本都有 pressure，但压力水平与成功并存，不能据此建立压力阈值、clock 阈值或 sensor timeout 阈值。当前没有改阈值的证据；后续需要带完整失败 snapshot 的受控故障样本，再评估是否有阈值依据。

验证：

```text
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python -m pytest \
  tests/test_collect_attach.py tests/test_analyze_sensor_timeouts.py \
  tests/test_px4_clock_stall_capture.py -q -p no:xdist
...................................                                      [100%]
```

未启动 SITL、Docker、GPU、训练、release-parity、真实飞行或 HIL；未修改既有 artifacts、15 秒预算、PX4 传感器阈值、retry、stall timeout、lockstep、world control 或任务 expected。CPU-only onboard gate 未在本次变更后运行，原因是改动范围限于相关 collector/analysis 诊断和测试。

## 建议的下一个任务

1. 目标：运行一个受控的单槽 sensor timeout 故障样本，验证 collector 写出的 `failure_snapshot` 在失败路径包含 episode/等待/primitive 单调时钟、pose/frame 水位、LOCAL_POSITION_NED/STATUSTEXT 接收时刻、最近 pressure、MAVLink 来源和最终失败类；证据路径：`.tmp/jobs/38-sensor-stream-trace.sh`、`artifacts/benchmarks/sensor-timeout-followup-20261107-comms-free-count`、`docs/sensor-timeout-followup-sitl-20261107.md`；不要触碰：既有 artifacts、15 秒等待、PX4 sensor threshold、retry、stall timeout、task expected 和共享 evidence-bound 文件；写入：仅新 episode 目录及 `docs/sensor-timeout-followup-sitl-20261107.md`。
2. 目标：对 PX4/Gazebo clock stall 做一次完整 probe/GDB ordering 故障样本，确认 `clock/stats` timeout、GDB 顺序和 `unknown` 降级不会改变 `clock_stalled_alive`；证据路径：`artifacts/benchmarks/px4-clock-stall-followup-20261107`、`docs/gz-idle-stall-20261107.md`、`docs/gz-idle-stall-followup-20261107.md`；不要触碰：既有 artifacts、lockstep/world control、stall timeout、任务 expected 和任何阈值；写入：仅新的 `.tmp/sensor-clock-observability/` 证据与 `docs/px4-clock-stall-probe-followup-20261107.md`。
