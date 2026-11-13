# nightly-8 失败分析：保留的 PX4 ULog

分析对象是 `nightly-linux-r21idr3-r24plan-8`，代码 `471242c0`，运行参数包含
`--keep-px4-ulog`。读取范围为只读的
`artifacts/benchmarks/nightly-linux-r21idr3-r24plan-8/main/`；没有重新运行 SITL、Docker
或 GPU。`count-cones`、`count-fire-hydrants` 成功，`survey-site-overlap` 是按计划中断，以下只分析四个失败任务。

## 先修复 analyzer 的真实 ULog 解析问题

真实 PX4 ULog 的 `A`（Add Logged Message）记录格式是 `multi_id + msg_id + topic_name`，字段定义在此前的 `F`（Format）记录中。原 analyzer 把 `A` 的剩余字节误当成 `name:fields`，于是所有真实 topic 的 rows 都是空字典，时间戳、CPU、failsafe 和 land detector 结论全部失真。

本次在 `scripts/analyze_px4_ulog.py` 中保存 `F` 的字段定义，并让只含 topic 名称的 `A` 复用该定义；同时保留对带内嵌字段的旧 fixture 的兼容。新增测试覆盖真实 `F` + `A(topic name)` + `D` 序列。修复后真实 ULog 可读出 `timestamp`、`cpuload`、`vehicle_status`、`vehicle_land_detected`、`estimator_status` 和 `failsafe_flags`。

## 结论总表

| 任务 | campaign 最终异常 | ULog/遥测证据 | 分类 |
| --- | --- | --- | --- |
| `find-suv-land` | `TimeoutError: timed out waiting for ekf_ok after 90.0s; still missing: ['GLOBAL_POSITION_INT']` | ULog 仅到 PX4 `6.684 s`；`vehicle_global_position` 0 行，`vehicle_local_position.xy_valid=0`、`dead_reckoning=1`；CPU load `0.965`，`estimator_status.timeout_flags=0`，未见 failsafe | **仍未解决**：后续 landing/executor/supervisor 修复不覆盖启动期 EKF/GLOBAL_POSITION_INT 等待 |
| `inspect-postbox` | `TimeoutError: timed out waiting for takeoff_reached after 154.0s` | ULog 到 `10.164 s`；PX4 已 armed (`arming_state=2`) 且 OFFBOARD (`nav_state=14`)，`vehicle_global_position` 持续；末端高度约 `18.467 m`、NED `z=-0.217 m`，landed=0；CPU load `0.892`，estimator `timeout_flags=0`、failsafe=0 | **仍未解决**：后续提交修复 primitive/landing 的 sim-time 预算，`_takeoff_wait` 仍是墙钟等待 |
| `monitor-backpacks` | `TimeoutError: timed out waiting for land after 153.3s; mode=100925440; landed_state=4; altitude_m=18.153; retries=5` | campaign 的 `px4_ulog_copied` 明确为 `files: []`，没有保留 `.ulg`；episode 已执行 108 steps，失败发生在旧 LAND 等待。现有 episode timing 显示 executor waits 很长，但无法确认 PX4 内部状态或仿真时钟速率 | **后续提交覆盖该等待路径，根因匹配待验证**：`e4b7945d` 将 Linux LAND 预算绑定 PX4 `time_boot_ms`；若本次是慢 lockstep 下墙钟预算过早耗尽，此修复覆盖该机制，但现有证据不足以证明本次就是该根因 |
| `patrol-fence-backpacks` | `TimeoutError: sensor stream produced no frame within 15.0s` | ULog 到 `212.936 s`；`vehicle_attitude`、`vehicle_local_position`、`estimator_status` 持续更新，末端 CPU load `1.064`、`failsafe=0`、`timeout_flags=0`；`vehicle_land_detected.landed=0` 只是任务中止时的飞行状态，不是根因 | **仍未解决**：这是 RGB/depth sensor stream 无帧，和 sim-time landing、primitive wall cap、supervisor vehicle clock 三个修复不同路径 |

## 逐任务证据

### `find-suv-land`

campaign 日志的最终 traceback 是：

```text
TimeoutError: timed out waiting for ekf_ok after 90.0s; still missing: ['GLOBAL_POSITION_INT']
```

对应 ULog：

```text
.../find-suv-land/.../px4-ulog/opt__px4-gazebo__log__2026-11-03__04_53_05.ulg
```

修复后的 analyzer 显示日志只覆盖约 `6.684 s` PX4 时间。`vehicle_global_position` 没有任何 sample；`vehicle_local_position` 最后为 `xy_valid=0`、`dead_reckoning=1`，而 `vehicle_land_detected` 却在启动期报告 landed。`vehicle_status` 仍为 disarmed，`failsafe=0`；`estimator_status` 的 `filter_fault_flags=0`、`timeout_flags=0`，因此没有证据表明是估计器 failsafe 或 CPU 饱和。更符合“GLOBAL_POSITION_INT 尚未形成/发送，启动等待窗口先到期”。

### `inspect-postbox`

最终异常：

```text
TimeoutError: timed out waiting for takeoff_reached after 154.0s
```

对应 ULog：

```text
.../inspect-postbox/.../px4-ulog/opt__px4-gazebo__log__2026-11-03__04_47_30.ulg
```

PX4 已成功进入飞行阶段：`vehicle_status.arming_state=2`、`nav_state=14`，`pre_flight_checks_pass=1`；`vehicle_global_position` 有 501 个 sample，末端 `alt=18.467 m`，但 `vehicle_local_position.z=-0.217 m`，仍接近地面参考，`vehicle_land_detected.landed=0`。CPU load 约 `0.892`，估计器 `timeout_flags=0`、`filter_fault_flags=0`，`failsafe=0`。这说明日志中没有 estimator/failsafe/CPU 过载信号；失败点是 collector 的 takeoff completion 等待超时。当前 `_takeoff_wait` 仍使用 `time.monotonic()` 墙钟，后续提交没有把它迁移到 PX4 sim clock。

### `monitor-backpacks`

最终异常：

```text
TimeoutError: timed out waiting for land after 153.3s; mode=100925440; landed_state=4; altitude_m=18.153; retries=5
```

本任务没有实际保留下来的 ULog：campaign 记录的复制事件是
`{"phase":"px4_ulog_copied","files":[]}`。因此不能声称 land detector、failsafe 或 CPU 的 ULog 结论。可用的 episode 证据是任务已运行 108 steps，失败发生在收尾 LAND；timing 中 `executor_wait_ms` 平均约 `20.9 s`、P95 约 `64.3 s`，与慢 lockstep 下按墙钟计 LAND 等待相符。

后续 `e4b7945d`（已在当前代码历史中）对 Linux x500 LAND 使用 PX4 `time_boot_ms` 作为预算时钟，同时保留墙钟停滞检测和硬上限。它覆盖“仿真时钟慢、墙钟预算先耗尽”的 LAND 等待机制；但本次没有 ULog，不能确认这个机制是否就是本次失败原因。因此分类为代码路径已由后续提交覆盖、根因匹配仍待验证，且需要后续真实运行确认落地状态能稳定收敛。

### `patrol-fence-backpacks`

最终异常：

```text
TimeoutError: sensor stream produced no frame within 15.0s
```

对应 ULog：

```text
.../patrol-fence-backpacks/.../px4-ulog/opt__px4-gazebo__log__2026-11-03__04_10_27.ulg
```

日志覆盖到 `212.936 s` PX4 时间；`vehicle_attitude`、`vehicle_local_position`、`estimator_status` 都有持续 sample，末端 `vehicle_status.failsafe=0`，`estimator_status.timeout_flags=0`，CPU load 约 `1.064`。唯一明显的 console fault 是很晚出现的 `Accel #0 fail: TIMEOUT!`（约 `25953.280 s` PX4 时间），晚于主要飞行数据且不能解释前面的 sensor-stream 无帧；同时 campaign 的错误发生在 frame server 等待阶段。故障根因应定位在 RGB/depth frame stream 或其消费者，而不是 PX4 vehicle clock freeze、land detector 或 estimator failsafe。

## 后续提交的适用边界

- `e4b7945d`：修复 Linux landing wait 的时钟域；可能覆盖 `monitor-backpacks` 的超时机制，但其缺失 ULog 使具体根因无法确认。
- `a866a5da`：primitive wall cap 只限制仿真时钟停止推进的时段，避免慢但仍推进的动作被总墙钟预算截断；对 `inspect-postbox` 的 takeoff wait 不构成修复。
- `5b0b8d6d`：supervisor budget timeline 绑定 PX4 vehicle clock，避免相机时钟领先时错误触发 reserve/return；本 nightly 失败发生在 EKF/takeoff/sensor stream/LAND 等更早或更底层路径，不能据此标记为已修复。

## 复现的离线命令与限制

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python \
  scripts/analyze_px4_ulog.py <episode-or-ulg> --gap-seconds 1.0
```

本报告只读取 nightly 产物和仓库代码历史。`monitor-backpacks` 没有 `.ulg`，因此其 PX4 内部状态只能标为证据缺口；没有把 campaign 的 `landed_state=4` 或 mode 数值单独解释成 PX4 根因。
