# Primitive 位移与传感器停流分类修复（2026-11-07）

## `achieved_delta_ned`

`PymavlinkPrimitiveExecutor` 会保留连续 setpoint 目标以便下一 primitive 从命令位置继续规划，但此前也用该目标作为实际位移的起点。飞机没有跟上目标时，这会把先前的目标误差带入后续 primitive。现在 executor 以本 primitive 开始时测得的 pose 计算返回位移；collector 写入 outcome 时统一以 `obs_after.pose.local_ned_m - obs_before.pose.local_ned_m` 作为最终值。字段现在表示相邻观测之间实际发生的 NED 位移，不再混合命令目标进度。

回归测试使用 `docs/accel-timeout-crash-20261107.md` 中 step 38 的 NED z 值（约 `-18.62 m` 到 `+0.76 m`，位移约 `+19.38 m`），并验证后续近地面 pose 对只记录本步微小变化，不会继续呈现约 15 m 的累计假增长。

字段消费核查：

- Planner：没有读取 `achieved_delta_ned` 的路径。
- Executor：产生 `ExecutionResult.achieved_delta_ned`；现在以本步开始测得的位置为基准。
- Collector/reports：`scripts/collect_sim_teacher_episodes.py` 写入 primitive outcome；`src/multidetect/sim_teacher/recording.py` 的 `audit_dataset` 使用该值计算位置误差报告；`scripts/smoke_airframe_flight.py` 用它生成 smoke 飞行重定位误差。
- Training data builders：未发现训练 rollout 或数据构建器读取该字段。审计器只报告误差，不构造训练标签/样本。

不重写、不迁移、不重新生成任何已有 episode 或训练数据。历史记录中的 `achieved_delta_ned` 保留原值；重新采集的记录采用修复后的口径。

## 传感器流超时

`TimeoutError: sensor stream produced no frame within 15.0s` 现在归为 `sensor_stream_stalled`，不再因 PX4 进程存活或状态未知而归入 `px4_alive_other_failure` / `px4_status_unknown`。失败时复用 clock-stall capture，保存 `px4-clock-stall-capture.json`，并在失败诊断和 `failure_record` 中记录证据路径。capture 仍是尽力采集：失败不会覆盖原始超时结果。

15 秒墙钟等待上限和 retry policy 均未改变。nightly arms 比较器接受并保留 `sensor_stream_stalled` 分类。

## 验证

- `tests/test_sim_teacher_executor.py`：覆盖 executor 不再以累计 setpoint 目标计算实测位移。
- `tests/test_sim_teacher_live_loop.py`：覆盖调查文档中的 NED pose 数值及近地面相邻位移。
- `tests/test_collect_attach.py`：覆盖新故障分类及 stall capture 的路径/样本数记录。
- 未改动 artifacts/ 中的共享产物或任何既有训练数据。
