# sensor timeout 与 PX4 clock robustness 受控负载对照（2026-11-08）

## 结论

本 lane 完成了同一输入的 baseline/受控负载 dry-run、串行调度和主机压力记录，但没有形成可解释的 SITL episode 对照。两个实际条件都在 collector 的 Docker 可达性检查处退出（exit code 1，stderr 为 `Docker daemon is not reachable; use --dry-run or start Docker`），因此本矩阵既未复现 sensor timeout，也未复现 PX4 clock stall；这不是修复证明，也不是“0 timeout”结果。

## 条件矩阵与输入

有界套件脚本为 [`scripts/run_clock_sensor_load_suite.sh`](../scripts/run_clock_sensor_load_suite.sh)，输出根目录为 `.tmp/clock-sensor-load-20261108/`。dry-run 产物在 `dry-run/`，实际串行尝试在 `actual2/`；`actual/` 是第一次发现记录器 bug 的不完整尝试，不计入矩阵。

| 条件 | 语义输入 | 负载 | slot / SITL cpuset | 预算 |
|---|---|---|---|---|
| baseline | `gen_cnt2_w0000.scenario.json` + 同一 `onboard.json`，`onboard`、seed 0、`x500_mono_cam`、segmentation、1280×960 | 无 lane 负载 PID | slot 0 / `66-75` | 6 steps，`timeout=300 s`，`max_episode=300 s`，heartbeat 240 s |
| loaded | 与 baseline 完全相同，输入 hash 相同 | `stress-ng --cpu 12 --taskset 0-11 --timeout 600s`，只结束本 lane 记录的 PID | slot 0 / `66-75` | 同 baseline |

dry-run 的 CPU isolation 均为 `MULTIDETECT_SITL_SLOTS=3`、slot 0、pool `66-95`、每 slot 10 CPU、`cpu_shares=4096`。场景 SHA-256 为 `b8bef5fa95c458b775ea458a672adafdefdc1b67da98df48007b014f3d5d7f70`，onboard 配置 SHA-256 为 `4582207b4cfe63530b5d11d570a5b99da513263028288293871dca5c38dfaa3d`；两个 `input.json` 都标记 `same_semantic_inputs=true`。

## 串行时间线与 PID

实际有效尝试位于 `.tmp/clock-sensor-load-20261108/actual2/`：

| 条件 | UTC 开始 | UTC 结束 | collector PID | 负载 PID | 监控 PID | 停止原因 |
|---|---|---|---:|---:|---:|---|
| baseline | 2026-11-07T23:34:03Z | 2026-11-07T23:34:04Z | 10 | 0 | 9 | `collector_exit_1` |
| loaded | 2026-11-07T23:34:04Z | 2026-11-07T23:34:06Z | 110 | 109 | 108 | `collector_exit_1` |

这些 PID 只用于本 lane 的记录和结束；进程结束后当前 PID namespace 未留下 `stress-ng`、collector 或压力监控进程。loaded 的 `load.log` 显示 12 个 CPU stressor 在 collector 失败后被停止，未按名称清理其他进程。

## 采集顺序与证据覆盖

dry-run 已核对两条件使用同一 collector 命令参数，slot 0 映射为 `66-75`。`scripts/px4_clock_stall_capture.py` 的固定顺序为：进程/线程与 transport procfs、PX4 GDB、Gazebo GDB、clock topic info、clock sample、WorldStatistics sample；相关测试覆盖该顺序和 `capture_budget.ordering`。本次没有进入 SITL failure path，所以没有生成 `capture_budget.ordering`、PX4/Gazebo GDB 或 clock/stats probe 文件。

两条件各有一个 `host_pressure.jsonl` 样本，覆盖 collector 启动前后的主机背景：

| 条件 | CPU PSI some avg10 | memory PSI some avg10 | io PSI some avg10 | loadavg 1m |
|---|---:|---:|---:|---:|
| baseline | 29.71% | 7.43% | 0.09% | 194.64 |
| loaded | 30.12% | 7.17% | 0.07% | 196.27 |

该覆盖只有一次采样，不能代表 episode 时间序列。实际 SITL 未启动，因此两条件均缺少 `episode.json`、`frame_wait_timeline`、MAVLink/JSONL、RTF、sensor timeout 事件、PX4/Gazebo clock/stats probe 和 GDB capture。分析器命令：

```text
.../.venv/bin/python scripts/analyze_sensor_timeouts.py \
  .tmp/clock-sensor-load-20261108/actual2/baseline \
  .tmp/clock-sensor-load-20261108/actual2/loaded
```

输出为 0 个 episode。因没有逐事件输入，`no_new_frame_observed`、`frame_timestamp_below_pose_watermark`、`px4_clock_and_telemetry_stalled` 和 `frame_wait_timeout_unclassified` 均**未分类**；不能把环境阻塞映射成任一 sensor/clock 故障类别。

## 复现判断与证据边界

本矩阵未复现 timeout 或 clock 停滞，原因是 SITL 在 Docker daemon 可达性检查前被阻塞。已有历史文档中的 timeout/clock 观察仍然只代表各自历史产物；本次没有新增可与它们比较的 episode 时间线。主机 load/PSI 快照显示当时机器处于高负载背景，但它们没有与 SITL episode 对齐，也不能支持负载是根因。

没有修改 15 秒 frame wait、PX4 timeout、retry、stall timeout、lockstep、world control、任务 expected 或安全规则；没有写入、移动或删除共享 `artifacts/`，没有训练、HIL、真机或网络下载。

## 验证结果

- dry-run：通过；baseline/loaded 输入 hash、slot、cpuset 和预算一致。
- `bash -n scripts/run_clock_sensor_load_suite.sh`：通过。
- `tests/test_collect_attach.py tests/test_analyze_sensor_timeouts.py tests/test_px4_clock_stall_capture.py`：35 passed。
- `scripts/run_onboard_gate.py -q -p no:warnings`：PASS，110 s，CPU-only。
- SITL：未执行成功；Docker API 在当前工作区返回 permission denied，collector 两条件均以 exit code 1 退出。

## 建议的下一个任务

1. 在 Docker daemon 可达且没有其他 sensor/clock/SITL lane 的单槽环境执行 `scripts/run_clock_sensor_load_suite.sh`，保留 `.tmp/clock-sensor-load-20261108/` 下 baseline/loaded 的 episode、压力、MAVLink、clock/stats、GDB 和 `capture_budget.ordering`；不得写入共享 `artifacts/`、修改 timeout/lockstep/world control 或改变任务输入，更新 `docs/clock-sensor-load-robustness-20261108.md` 的实际结果。
2. 若实际 episode 产生 timeout，在同一输出目录运行 `scripts/analyze_sensor_timeouts.py` 并逐事件核对四类分类和 trace 覆盖，写入 `docs/clock-sensor-load-event-classification-20261108.md`；不得把相关性写成根因，也不得因一次成功宣称修复或调整运行时阈值。
