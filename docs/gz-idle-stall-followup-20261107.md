# PX4/Gazebo clock stall 跟进（2026-11-07）

## 结论

本轮没有在当前沙箱启动 SITL；调度方需执行 `.tmp/jobs/01-px4-clock-stall-followup.sh`。因此本报告不把“未复现”写成修复，也没有修改 timeout、lockstep 或 world control。已有 r9r capture 的两次 Gazebo GDB 快照都显示 runner 位于 `pthread_mutex_lock -> NodeShared::CheckSubscriberInfo -> Node::Publisher::Publish -> SimulationRunner::PublishStats -> SimulationRunner::Step -> Run`，可确认快照时等待 transport mutex；不能确认锁持有者、死锁或根因。由于旧 capture 先运行 `gz topic`，探针可能改变 transport discovery/mutex 状态，尚不能证明无探针时也会持续卡在该路径。

当前证据足以提出下一步“无探针污染的复现采集”，不足以提出运行时修复。

## 现有证据边界

只读证据：

- 28-head episode：`artifacts/benchmarks/nightly-linux-r21idr3-r24plan-28-head/main/count-cones/campaign/gen_cnt2_w0000/episodes/episode-0000-4dec0c9f/`
- r9r capture：`artifacts/r21id-r9rb/episodes/gen_r21id_r9r_w0003/ep-0/attempt-0/episodes/episode-0000-536f4b08/`
- 依据文档：`docs/gz-idle-stall-20261107.md`、`docs/operator-abort-wall-20261107.md`

28-head capture：PX4/Gazebo 仍存活，PX4 最后可见时钟约 `49.116 s`；两次 clock 探针没有消息，故 `gz_clock_advanced=unknown`。旧 capture 没有有效 WorldStatistics，也没有 Gazebo GDB，不能定位 Gazebo runner。

r9r capture：两次 `/world/default/clock` 均由命令内 `timeout 5s` 超时，两次 `/world/default/stats` 均由命令内 `timeout 3s` 超时；`paused` 因无输入保持 unknown。PX4 reception thread 两次均在 `poll -> zmq_poll -> NodeShared::RunReceptionTask`；没有 callback 栈。Gazebo 两次 GDB 都可见 `PublishStats`、`Step` 和 mutex wait。该栈证明“采样瞬间等待 mutex”，不证明锁竞争来源或永久停顿。渲染线程的 barrier/条件等待与无新帧相容，但不能证明反向阻塞 runner；栈中没有 CUDA、EGL、GPU driver 或 PX4 lockstep 调用。

operator-abort 对照：28-head operator-abort 的 PX4 仿真时间持续推进，RTF 约 `0.144`，主要时间消耗在 primitive/executor wait。它是慢速推进对照，不是 `clock_stalled_alive` 的同一证据，也不能把高负载直接归因给 Gazebo、渲染、CPU 调度或 lockstep。

## 采集器审计与改动

采集顺序现在固定为：

1. 读取进程/线程、procfs transport socket、cgroup CPU/memory；
2. 采集 PX4 GDB；
3. 采集 Gazebo GDB；
4. 对 `/world/default/clock` 运行 `gz topic -i`；
5. 对 clock 运行有界 `timeout 5s gz topic -e -n 1`；
6. 对 stats 运行有界 `timeout 3s gz topic -e -n 1`。

clock/stats 探针都保留 return code、timeout、stdout/stderr；未知字段保留 unknown。WorldStatistics parser 同时接受 `paused` 和 `pause`，并提取 `sim`/`sim_time`。`gz topic -i` 仍被标为有状态探针，不能把其 discovery 线程当作 stall 根因。

本轮只改诊断路径：

- `capture_budget` 记录 GDB、topic probe、delay 的预算与顺序；预算是 subprocess timeout 上界，不掩盖 attach/调度/清理开销。
- 分析结果新增 `gz_runner_stack_classification`，仅按可见 GDB frame 标出 `publish_stats_mutex_wait`、`publish_stats`、`step` 或 `not_observed`；新增字段不推断 mutex owner/deadlock。
- 新增 `imu_progress`；若 PX4 listener 有可解析 timestamp，报告前后是否递增，否则 unknown。
- 新增 `camera_progress`，明确当前 capture 不直接探测 camera topic，因此保持 unknown。
- `supports.gz_publish_stats_mutex_wait` 只有至少两个 Gazebo 快照都显示 PublishStats+mutex 时才为 true。

没有向任何 world control service 发送 pause、step 或 unpause。

## 调度方可执行的 SITL 尝试

脚本：`.tmp/jobs/01-px4-clock-stall-followup.sh`

脚本串行执行两个单槽任务，每次 `--workers 1`，设置 `MULTIDETECT_SITL_SLOTS=3`、`MULTIDETECT_SIM_GPU_AVOID=2,3`、`MULTIDETECT_SIM_GPU_DEVICES=0,1`，输出目标为新目录 `artifacts/benchmarks/px4-clock-stall-followup-20261107/`：

```bash
.tmp/jobs/01-px4-clock-stall-followup.sh
```

第一段是正常 `count-cones` 基线，第二段是同一 world/config 的有界 `count-cones` 尝试；两段均只使用项目槽位 0–2 中的一个槽位。预期分析：

- 若两段都完成：记录 clock、WorldStatistics、IMU 与 camera 的可见进展，并将 stall 记为本轮未复现；不能称修复。
- 若第二段失败且触发 capture：先看 `capture_budget.ordering`，确认 GDB 时间早于任何 `gz topic`；再逐项读取 PX4/Gazebo 栈、clock/stats timeout、pause 字段、IMU/camera 状态。
- 只有在无探针污染的两个 Gazebo GDB 快照都显示 `publish_stats_mutex_wait`，且 clock/stats 不推进时，才可把“runner 持续等待 PublishStats transport mutex”写成强证据；仍不能写成死锁或根因，除非取得锁持有者证据。

调度方需补记每次尝试的槽位、UTC 开始/结束时间、退出码、episode 目录、capture JSON/GDB 文件和停止条件。本沙箱未执行这些尝试。

## 验证

- `PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp /data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python -m pytest tests/test_px4_clock_stall_capture.py -q -p no:xdist`：17 passed。
- `PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp /data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python scripts/run_onboard_gate.py -q -p no:warnings`：PASS（170 s，CPU-only）。
- 未启动 SITL、Docker、GPU 训练或联网下载；未写入既有 `artifacts/` episode，也未修改 release parity/current progress/schema 文件。

## 未完成验证与修复判断

未完成：调度方的正常基线和有界 count-cones/r9r SITL；无探针污染条件下的复现；真实 WorldStatistics pause 值；camera frame 的直接推进证据；Gazebo mutex 持有者定位。

当前没有足够证据做最小运行时修复。特别不应因一次高负载快照提高 timeout、绕过 transport mutex 或改变 PX4 lockstep。下一步应先执行脚本并用新增字段核对证据边界。

## 建议的下一个任务

1. 在单槽 SITL 上执行 `.tmp/jobs/01-px4-clock-stall-followup.sh`，记录两个串行任务的槽位、UTC 开始/结束、退出码和所有 capture 文件；只读分析 `capture_budget.ordering`、PX4/Gazebo GDB、clock/stats、IMU/camera 字段，写入 `docs/gz-idle-stall-sitl-results-20261107.md`，不得修改 `artifacts/`、timeout、lockstep 或 world control。
2. 若无探针污染复现仍显示 Gazebo `PublishStats` mutex wait，设计只读锁持有者证据采集（例如在现有 GDB 快照中增加 owner/锁地址线索），写入 `docs/gz-idle-stall-transport-owner-20261107.md`；不得据此直接改运行时锁、PX4 lockstep 或任何 release-parity/schema 文件。
