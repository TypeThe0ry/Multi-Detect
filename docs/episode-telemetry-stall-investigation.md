# Linux mission suite：episode telemetry stalled 调查

## 结论

这两次 `TimeoutError: episode telemetry stalled after wall-clock budget` 都是
PX4/MAVLink 的 vehicle clock 停止推进，而不是主机 `MavlinkReader` 解析线程单独
丢包。看门狗在 nominal episode wall-clock budget 用尽后，继续观察
`MavlinkState.vehicle_time_boot_ms`；当该值连续 180 秒没有变化时才抛出异常。
本次证据中，MAVLink 多种消息同时停止，`MavlinkReader.error` 没有记录；与此
同时，Gazebo/frame server 的 sensor timestamp 仍继续增长。因此当前问题应在
容器内 PX4/SITL 或 PX4 的控制/遥测路径中继续定位，不能通过放宽主机超时来修复。

## 异常路径

- `scripts/collect_sim_teacher_episodes.py:3154` 的
  `_EpisodeProgressWatchdog.check()` 抛出 `TimeoutError`。
- episode step 循环在同一文件约 3977 行调用 watchdog。
- `src/multidetect/sim_teacher/streams.py:376` 的 `MavlinkReader` 接收线程负责
  更新 `MavlinkState.vehicle_time_boot_ms` 和 message count；本次没有 reader
  error 或解析异常。
- `src/multidetect/sim_teacher/streams.py:210` 的 `SensorStreamReader` 是独立的
  二进制传感器流读取线程。
- `scripts/sim_teacher_frame_server.py:547` 的容器内 GET/frame 循环仍可输出
  帧；它没有在两次 telemetry stall 中报告 EOF 或 odometry rejection。

## r24plan-7：count-fire-hydrants

episode：
`nightly-linux-r21idr3-r24plan-7/main/count-fire-hydrants/campaign/`
下的 `gen_cnt2_w0001/episodes/episode-0000-e2ad46be/`。

- `episode.json`：失败时间为 **2026-11-02 14:51:30.474（Asia/Singapore）**，
  failure 为 `vehicle_time_boot_ms=287784`、`message_count=129232`、
  `stall=182.2s`。
- `mavlink.jsonl` 有 129233 行。最后一条记录的主机 wall time 是
  **2026-11-02 14:45:36.913326（Asia/Singapore）**，vehicle clock 是
  **287784 ms**；最后的 HEARTBEAT 在 **14:45:33.931827**，vehicle clock
  **287784 ms**。
- ATTITUDE、GLOBAL_POSITION_INT、LOCAL_POSITION_NED、
  POSITION_TARGET_LOCAL_NED、SERVO_OUTPUT_RAW 等类型在同一时段一起停止，
  不是单一 MAVLink 类型被过滤。
- step 80 在 host monotonic **26040.7505 s** 完成；step 81 在约
  **26223.1334 s** 超时，step 82 在约 **26405.2197 s** 超时。
- 传感器输出没有同步停止：step 81 的 frame timestamp 为 **308224 ms**，
  step 82 为 **329572 ms**；两步对应的 pose timestamp 仍为约 **287780 ms**。
  也就是说相机/frame server 继续产生数据，而 PX4 pose/telemetry clock 冻结。

这个组合排除了“主机 reader 线程完全停止接收”以及“camera bridge 不再消费导致
Gazebo 全局 lockstep 停止”作为首要解释。它更符合 PX4/SITL 进程、PX4 控制链或
其 MAVLink 输出路径内部卡住或饥饿。

## r24plan-5：inspect-postbox

episode：
`nightly-linux-r21idr3-r24plan-5/main/inspect-postbox/campaign/`
下的 `gen_v2_w0038/episodes/episode-0000-8738a459/`。

- `episode.json`：失败时间为 **2026-11-02 09:53:42.471（Asia/Singapore）**，
  failure 为 `vehicle_time_boot_ms=58532`、`message_count=23456`、
  `stall=2004.5s`。
- `mavlink.jsonl` 有 23457 行。最后一条记录的主机 wall time 是
  **2026-11-02 09:24:51.491571**，vehicle clock 是 **58532 ms**。
- 从 step 12 起后续 step 都以约 180 秒间隔超时；frame timestamp 继续推进到
  **283440 ms**，而 pose 一直是 **58528000 ns**（58.528 s）。

该样本重复了 r24plan-7 的模式，且冻结持续时间更长，进一步说明 watchdog 报告
的是实际 vehicle-clock stall，而非偶发的 host socket read 延迟。

## 独立的 sensor-stream failure

`nightly-linux-r21idr3-r24plan-5/main/operator-pause-resume/campaign/` 的
`episode-0000-b57c6b87` 是另一类故障，不能与 telemetry stall 混为一谈：

- frame server 在 **2026-11-02 10:49:28.192（Asia/Singapore）** 记录
  `sim_teacher_frame_server_finished`，`emitted=32`、`dropped=2964`。
- 终止原因是 `Odometry rejected: wire Odometry velocity exceeds the safety bound`。
- 随后主机 `SensorStreamReader` 在 **10:49:28.405** 报
  `wire stream ended early: expected 276 bytes, received 0`。

这里确实是容器 frame server 主动结束，主机随后正确地把 pipe EOF 转成错误；它
与前两次 PX4 vehicle clock 冻结相互独立。

## 对候选原因的判断

失败 step 的 `slow_model_ms` 为 0；r24plan-7 冻结前最近 step 的 policy 约 271 ms，
r24plan-5 step 12 约 129 ms。现有记录没有显示慢模型调用与冻结点重合，因此没有
证据支持“onboard policy 慢调用直接让 camera bridge 停止消费”这一解释。

也没有证据表明 Gazebo 因 camera/sensor lockstep 停止：在两次失败的冻结窗口内，
frame timestamp 仍增长。更可能的方向是容器内 PX4 进程或 MAVLink/control path
的 CPU 饥饿、内部等待或锁死；仅凭当前 `container_logs_on_failure` 的 tail
无法区分这些 PX4 内部原因。

## 修复建议

主机侧当前行为保持不变：

1. 保留 `_EpisodeProgressWatchdog` 的 vehicle-clock 优先判定和硬墙钟上限；它已经
   把“墙钟预算耗尽但仿真仍在推进”和“vehicle clock 真正冻结”区分开。
2. 下一轮 nightly 默认启用 `--keep-px4-ulog`，把每个容器的 `.ulg` 复制到
   `jobs/<container>/px4-ulog`，并在失败事件中记录文件清单。
3. 为容器侧增加 PX4 进程/线程状态、CPU 使用率、MAVLink 输出计数和 Gazebo/PX4
   lockstep 状态的失败快照；同时保留 frame server 的 emitted/dropped/rejected
   计数。这样可以区分 PX4 scheduler 饥饿、控制回路等待和 UDP 输出路径异常。

容器侧需要单独处理 operator-pause-resume 的 odometry 异常：运行期出现异常速度
时不要直接关闭整个 sensor stream。可选方案是丢弃该样本并重置 source buffer，
或发送明确的可恢复诊断后继续等待下一组 RGB/depth/odometry；具体阈值和恢复策略
应在容器/frame-server 代码中实现，并配套其单元测试。

## 验证范围

本调查未启动 SITL、Docker、GPU 或网络下载，也未修改 `artifacts/`。当前仓库已有
watchdog 单元测试，下面的 CPU-only 测试用于确认调查涉及的主机路径未回归。
