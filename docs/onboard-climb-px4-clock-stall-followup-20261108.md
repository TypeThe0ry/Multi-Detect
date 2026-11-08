# v4 第二次 climb 的 PX4 仿真时钟停滞诊断（2026-11-08）

## 结论

本次 live 证据支持两个相邻但不同的现象：目标 `return_home` climb primitive 在严重变慢的 SITL 中耗时约 95.3 秒墙钟，PX4 时间仍从约 39.4 s 推进到 59.6 s，primitive 以注入的 timeout 结束；随后 PX4 时钟最后推进到 69.672 s，再连续 45 秒没有进展，collector 才报 `PX4 simulation clock stalled during primitive; step=2` 并采集故障快照。故障快照期间 Gazebo clock 仍前进，而 PX4 的传感器和 vehicle status 时间戳保持不变。这证明 watchdog 触发时有 PX4/Gazebo 时钟域分离，但不能把“PX4 在 climb primitive 执行期间从头到尾冻结”当作事实。

host 确实处于重负载：episode 的 82 条 host-pressure 样本记录 load1 从约 219.8 升到最高 366.8，10 核 slot 的繁忙率多数接近满载，CPU PSI `some avg10` 最高约 55.5%。这使 CPU contention 成为可信的降速/触发候选；但故障快照的容器 `cpu.stat` 两次均为 `nr_throttled=0`、`throttled_usec=0`，`memory.events` 的 OOM 计数也是 0。该快照只覆盖采样时点，且没有故障窗口的逐时 cgroup 计数，故不能据此排除历史短时调度饥饿，也不能认定 CPU 或内存压力是单点根因。

最强的机制线索位于 PX4 仿真时钟/lockstep 更新边界：PX4 进程仍存活，Gazebo clock 有推进；PX4 listener 时间戳没有推进，PX4 主循环及多个工作队列呈静止/等待状态。PX4 GDB 的 Gazebo clock reception callback 可见 `px4_clock_settime` 和条件变量广播栈，但这不是死锁或 bridge 丢消息的证明。Gazebo GDB 两次都分类为 `SimulationRunner::Step`，没有观察到 `PublishStats` mutex wait；world stats 的 `paused` 未提供，camera 进展未在该 capture 中探测。不能定位单一根因。

## 证据与边界

输入均为只读：

- `/data/projects/Multi-Detect-env/runs/sitl_chain3.out`
- `/data/projects/Multi-Detect-env/runs/src-followup2/.tmp/jobs/climb-fault-sitl4-1169782/artifacts/onboard-climb-fault-sitl4-20261108T002732Z-1169782/operator-abort/campaign/gen_cnt2_w0003/`
- `docs/onboard-climb-land-evidence-audit-20261107.md`（用于区分前次 v2 前置失败与本次 v4 live 证据）

本轮只读 episode `episode-0000-540aee44` 的 `episode.json`、`steps.jsonl`、`mavlink.jsonl`、`host_pressure.jsonl`、`px4-clock-stall-capture.json`、PX4/Gazebo GDB 文本、mission log 与 campaign log。以下时间均为 UTC；UTC+08 新加坡时间比表中时间加 8 小时。`sitl_chain3.out` 首行记录任务于 2026-11-08 08:27:32 +08 开始，末行记录 08:36:55 +08 以退出码 1 结束。

## 时间线

| UTC 时间/窗口 | 观测 | 可下结论 |
| --- | --- | --- |
| 00:29:55 | onboard mission log 启动；00:32:53 收到 operator abort 并做出 `return_now` 决策 | abort 是任务编排事件，不是 clock stall 的证据。 |
| 00:34:35.67 | `steps.jsonl` 的 climb `obs_before`：高度约 1.70 m，PX4 boot time 39.436 s、帧 sensor stamp 39.436 s | 满足低空场景条件；此刻 PX4 时间仍推进。 |
| 00:34:35–00:36:11 左右 | 唯一记录的 climb primitive 请求 `up_m=15`；fault injection 标记 `return_home_climb_timeout`、`attempt=1`。outcome 为 timeout，动作 elapsed 20.016 s；计时项 `command_and_primitive_wait_ms=95334.65` | 这个 primitive 内 PX4 时间从约 39.4 s 推进至约 59.6 s，按记录估算 RTF 约 0.21。它超时是注入的 primitive outcome；不是在全过程中时钟均冻结。 |
| 00:35:23.246 | 最后一条 MAVLink `LOCAL_POSITION_NED` 的接收时间，`time_boot_ms=69672`；capture 记录 last progress 单调时间 125490.397 s | PX4 时间最后确认推进到 69.672 s。 |
| 00:36:12.087、00:36:31.262 | clock-stall capture 的 deadline 与约 19.175 s 后快照。Gazebo `/world/default/clock` 为 81.484 -> 84.284 s；PX4 listener `sensor_combined.timestamp` 保持 69.672 s，`vehicle_status.timestamp` 保持 69.488 s | watchdog 确认的连续 PX4 无进展期内，Gazebo clock 仍推进约 2.8 s；PX4 listener 两次采样的时间戳未变。两者不是同一时钟域，且 capture topic probe 是有限样本。 |
| 同一 capture | PX4/Gazebo 进程均 `present`；Gazebo stats 有 iteration、报告 RTF 约 0.02，`paused=null`；Gazebo 两个栈都分类为 `step`，无 `PublishStats` mutex wait | 支持 Gazebo 有极慢的推进，但不能据缺失 paused 值断言 physics unpaused，也不能据 step 栈断言 Gazebo 正常或无阻塞。 |
| 故障 capture 后至 episode 收尾 | campaign 在 00:36:48 左右记录清理 disposable container；根 `sitl_chain3.out` 以 collector exit 1 收尾 | cleanup 只表示容器 teardown，不表示 PX4 已正常着陆或 disarm。 |

`steps.jsonl` 只有两条记录：一步 hold 成功，随后一步 climb timeout。`attempt=1` 是本产物里唯一带故障注入标记的 climb；证据目录没有第二条 climb outcome。因此标题中的“第二次 climb”若指第二次失败命中或重复 climb，本产物无法证明发生了它；若指本次目标的第二段 climb primitive，也只能确认上述一个 climb outcome。这里保留用户给定的故障简称，不把未记录的第二次尝试补成事实。

## Host load 与 PX4/Gazebo 状态

- episode provenance 记录 slot 2/3，cpuset `86-95`（10 CPUs），CPU shares 4096。host pressure 样本从 UTC 00:30:01.94 采到 00:36:47.46，共 82 条；load1 范围约 219.77–366.78。capture 的 96 核 host load1 为 341.72 和 347.69，即约 3.56 和 3.62 load/core。
- 10 核 cpuset 上的 core busy mean/max 样本最高约 99.84%/100%；该指标是宿主采样得到的 CPU 忙碌程度，不是 PX4 cgroup CPU 使用率。CPU PSI `some avg10` 的 episode 样本最高 55.53%，`full avg10` 样本为 0；memory PSI `some avg10` 最高 12.27%。PSI 是宿主压力背景，不可归因到某个容器。
- capture 两个快照间隔约 19.18 s。容器 cgroup `nr_throttled`、`throttled_usec` 均为 0，`memory.events` 的 `oom`/`oom_kill` 均为 0。它们是该约 19 秒窗口两端的累计值，并非完整的逐时资源轨迹。
- 同一窗口中，PX4 `mavlink_rcv_if0` 线程从 `S` 变为 `R`，记录约 1.96 s run-queue wait、约 0.045 s on-CPU；其他 clock 相关 PX4 接收路径栈可见 `px4_clock_settime` 回调。由于栈采集和 procfs delta 都只是窗口快照，不能单独区分 CPU 饥饿、回调内同步等待或 lockstep 输入链路异常。
- `px4-work_queue status` 的前后快照显示，多数控制、传感器、EKF、`gz_bridge` 队列频率由正常的数十至数百 Hz 变成 0.0 Hz；少数电池/系统周期任务仍显示频率。这与 PX4 仿真更新时间停住相符，但该 CLI 状态不提供线程阻塞原因。
- PX4 末端 MAVLink 显示 `EXTENDED_SYS_STATE.landed_state=2`、heartbeat `base_mode=145`，仍 airborne/armed。任务失败及 collector teardown 不是 landing 证据。

## 已证实与推测

**已证实**：爬升 primitive 以 20.016 s 的 PX4 elapsed timeout 结束，但其约 95.3 s 墙钟等待期间 PX4 boot time 仍推进约 20.2 s；之后 boot time 最后到 69.672 s，并在 45 s 无进展监控后触发 stall 错误。故障快照时 PX4/Gazebo 进程存活，Gazebo clock 有限样本从 81.484 s 推进到 84.284 s，PX4 IMU 与 vehicle status listener 时间戳不变。host 高负载和 10 核近满载样本，与故障窗口有重叠。

**较可信但未证实**：host CPU contention/极低 RTF 可能延长 climb 的墙钟时间，并可能为随后 lockstep/PX4 clock 更新中断创造条件。Gazebo 在 PX4 时钟不动时仍有少量 clock 进展，指向 PX4 clock 消费/更新边界；既有栈同时显示 PX4 clock callback 的 `px4_clock_settime` 和 Gazebo runner `Step`。

**现有证据不能证明**：host 负载是唯一或直接根因；PX4 callback 已死锁；Gazebo `PublishStats` mutex 卡死；GPU 导致该 stall；camera 在 stall 窗口继续工作；world 明确处于 unpaused；本次发生了第二次注入 climb/第二次 timeout 命中；LAND 已执行。`gpu_memory_mib` 的一次 snapshot 显示 GPU0/1 有显存占用，但没有容器到 GPU 的因果绑定或 GPU 利用率时间序列，不能从显存占用归因。capture 明确将 camera 进展标记为 unknown。

## 可验证的 CPU-only 检查

不需要再启动 SITL 即可先做以下离线核验；所有输出放工作树 `.tmp/`，不写共享 `artifacts/`：

1. 用仓库 Python 对 `steps.jsonl`、MAVLink `LOCAL_POSITION_NED.time_boot_ms`、frame sensor stamp、capture `captured_wall_time` 与 `host_pressure.jsonl` 做统一的单调时间表，逐段计算 PX4 RTF、最长无进展间隔、host load/core-busy/PSI 分位数，并断言事件顺序为“climb timeout -> PX4 最后进展 -> 45 s watchdog -> capture”。对照 `px4_clock_stall_capture.py` 的字段定义，避免把 capture 命令采样时间错当 stall 起点。
2. 对 `px4-clock-stall-capture.json` 做只读 consistency checker：核对 `samples[].label`/墙钟顺序、两次 GZ clock、两次 PX4 listener timestamp、GDB stack classification、work queue 前后差异、cgroup 计数；验证有限证据只会导出“观察到/未观察/unknown”，不会从缺字段推断 paused、camera、mutex owner 或因果关系。
3. 将本次同 cpuset 的压力轨迹与已有健康 live episode 的 host pressure、PX4 boot time 和 frame stamp 离线对齐，计算同指标分布；只有同负载健康对照才能评估高 load 的区分力。若需进一步验证调度饥饿，应在未来一次经单独调度的 live 运行中保留 slot cgroup 的定时 `cpu.stat`、PSI、`schedstat`/run-queue wait，而不是用事后当前值替代历史计数；这一步不属于本轮，也不执行 SITL。

本轮未添加脚本或测试：指定证据可直接支持上述判断，现阶段新 parser/test 的价值低于先做一次可复核的离线时间表；没有必要改变任何已有断言或运行时语义。未启动 SITL、Docker、GPU 训练或下载；未写入 `artifacts/`。

## 验证

本轮进行只读 JSON/JSONL、日志与 GDB 文本检查，未运行 pytest 或 onboard gate（没有代码改动）。`git status` 应仅显示本新增文档。

## 建议的下一个任务

1. 目标：编写 CPU-only 离线时间线分析器与测试，读取 v4 climb fault 目录中的 `steps.jsonl`、`mavlink.jsonl`、`host_pressure.jsonl`、`px4-clock-stall-capture.json`，统一对齐单调时钟/墙钟/PX4/Gazebo 时间并报告事实、缺失值和计算出的 RTF；证据路径：`/data/projects/Multi-Detect-env/runs/sitl_chain3.out`、`/data/projects/Multi-Detect-env/runs/src-followup2/.tmp/jobs/climb-fault-sitl4-1169782/artifacts/onboard-climb-fault-sitl4-20261108T002732Z-1169782/operator-abort/campaign/gen_cnt2_w0003/`、`scripts/px4_clock_stall_capture.py`、`docs/onboard-climb-px4-clock-stall-followup-20261108.md`；不得启动 SITL/Docker/GPU 任务，不得写入 `artifacts/`，不得修改 timeout、安全语义、SITL 输出或现有断言；只新增离线分析与有意义的 pytest，结论写入 `docs/onboard-climb-px4-clock-stall-offline-analysis-20261108.md`。
