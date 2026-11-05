# Linux SITL PX4 仿真时钟冻结：根因调查

调查日期：2026-11-05。只读检查指定 benchmark 产物、campaign 日志及仓库中的
PX4/Gazebo 启动与 collector 代码；没有启动 SITL、Docker、llama-server、GPU 训练或
网络下载，也没有修改 `artifacts/`。

## 结论

**最佳支持的解释是 PX4 一侧的 lockstep 更新链路间歇性停止：PX4 不再处理新的仿真
传感器/时钟更新，因而 ULog 和 MAVLink 的 PX4 时间一起停住；Gazebo 相机传感器仍能
继续输出。** 最可能的边界在 Gazebo-PX4 bridge/lockstep 输入或 PX4 仿真调度路径。
这些产物不能区分 bridge 收不到/处理不了消息、PX4 内部线程阻塞或调度饥饿，不能把
其中任何一个写成已证实的单点根因。

关键证据是故障前 PX4 多个互不相同的 MAVLink/ULog topic 一同停在相同 boot time，
而帧的 sensor stamp 仍前进；模式跨越 RETURN_HOME、Follow 中途和 LAND，且 6 核与
10 核 cpuset 都出现过。r24plan-17 的 LAND 故障中 ULog 仍连续记录 IMU、姿态和局部
位置到 `330.62 s`，采集器随后在约 `3.739 m` 高度报告 PX4 时钟停滞；这不是单个
遥测类型或 host reader 的孤立丢包。

这比“整个 Gazebo physics/render loop 被 GPU 或 camera backpressure 卡死”更符合现有
证据，但不能排除资源压力是触发条件。`--gpu-render` 确实启用，Gazebo 使用
`--headless-rendering`/OGRE2；日志没有 GPU 利用率/显存或容器 cgroup memory/throttle
计数。PX4 ULog 的 `cpuload` 和 `ram_usage` 不是 host/container 资源快照。运行有
cpuset 隔离（样本中分别为 6 和 10 CPUs），因此单纯“未 pin CPU”不成立，但 pinning
不能排除调度/配额/内部锁问题。campaign 失败时抓到的容器日志尾部主要是重复的
`pxh>` 提示符，没有线程栈或 bridge 诊断。

本轮把主机资源证据与 ULog 墙钟窗口对齐后，结论需要再收窄：冻结时主机 load/run
queue 确实很高，但同一套配置下的健康任务也在高负载中完成；冻结窗口没有 kernel OOM、
memory cgroup OOM、hung task 或 GPU Xid 事件。保留的产物没有逐时刻 `cpu.stat`、
`memory.events`、`memory.pressure`、容器 RSS 或 GPU/container 绑定快照，因此不能把
高 load 写成已证实的 CPU starvation，也不能证明历史上没有短时 cgroup PSI 或 throttling。
本次证据不足以支持新的具体代码修复，故只更新本调查文档。

## 运行证据

| 运行/任务 | PX4 停止点与其他推进证据 | 资源/任务上下文 |
| --- | --- | --- |
| r24plan-7 `count-fire-hydrants` | MAVLink 的姿态、局部/全局位置和 setpoint 同时停在 `287.784 s`。step 81/82 各等待约 `180 s` 后超时，PX4 pose 不变；frame stamp 从 `308.224 s` 增至 `329.572 s`。故障阶段为 `return_home`，不是慢模型调用（失败前 `slow_model_ms=0`）。 | campaign 命令启用 `--gpu-render`。此 run 没有保留 `.ulg`，episode provenance/资源快照不足以确认 slot cpuset、GPU 负载或 cgroup 状态。 |
| r24plan-7 `budget-reserve-return` | 另一例 telemetry watchdog 停在 `122.988 s`；step 40–42 都超时，pose 停止，frame stamp 从 `340.200 s` 继续到 `384.088 s`。 | 同一 run；容器故障日志尾部无可用 PX4/bridge 状态。 |
| r24plan-ext-1 `follow-red-person-return` | ULog 中 `vehicle_attitude`、`vehicle_local_position`、`vehicle_global_position`、`sensor_combined` 等连续 topic 一同止于约 `392.84 s`；MAVLink pose 止于 `392.864 s`。随后三个 step 各等待约 `180 s`，frame stamp 继续经过 `422.600`、`451.444`、`481.044 s`。当时尚处 `area_search`，没有进入 Follow 控制阶段。 | slot 0 cpuset=`84-89`，6 CPUs；campaign 启用 `--workers 2 --gpu-render`。step inference 的 slow-model 时间很小，不与冻结重合。 |
| r24plan-17 `survey-site-overlap` | LAND 收尾时失败，高度 `3.739 m`。保留 ULog 的 `sensor_combined`、`vehicle_attitude`、`vehicle_local_position` 分别有约 79.5k、79.1k、40.1k samples，末时间均约 `330.62 s`；高频 topic 的最大仿真时间间隔约 `0.03–0.04 s`。末端 armed、`failsafe=0`、估计器 fault flags 为 0。 | slot 0 cpuset=`76-85`，10 CPUs；2 slots，campaign 启用 `--workers 2 --gpu-render`。末端 PX4 `cpuload=1.054`、PX4 `ram_usage=0.573`，不是 host cgroup 数值。 |

上述 frame stamp 是传感器仿真时间戳，不是墙钟帧率；现有 artifacts 足以确认它在 PX4
停住期间继续增长，但不足以重建准确 camera FPS。三种任务阶段出现相同停滞形态，说明
故障不是某一个任务 primitive/LAND 状态机本身造成的。

## ULog 覆盖与分析

共享 artifacts 当前可见 59 份 kept `.ulg`：r24plan-16 为 14 份健康日志，r24plan-17
为 14 份（含本次冻结的 `survey-site-overlap`），ext-1 和 ext-2 各 4 份；其余 gimbal
产物主要是模型、Docker 或 sensor stream 启动失败，不作为 clock-freeze 对照。本轮用
同一命令、`--gap-seconds 1.0` 重新分析四份代表性日志，报告写入工作树 `.tmp/`：
r24plan-17 的冻结 `survey-site-overlap`、健康 `count-fire-hydrants`，以及 ext-1
冻结 Follow、ext-2 健康 Follow。r24plan-7 的目标 count episode 没有 ULog，只能用
MAVLink/steps/episode 元数据。诊断脚本不能从 ULog 恢复容器 stdout，也不能观测 PX4
未记录的线程状态或 Gazebo transport queue。

四份报告的关键末端值如下；topic 的最大间隙是 PX4 仿真时间间隙，不是冻结期间的墙钟
停滞时长。

| 样本 | `sensor_combined` / `vehicle_attitude` / `vehicle_local_position` 最后时间（s） | 代表 topic 最大间隙（s） | 末端 PX4 `cpuload` / `ram_usage` | 末端状态 |
| --- | --- | ---: | --- | --- |
| r24plan-17 `survey-site-overlap`（冻结） | 330.620 / 330.624 / 330.620 | 0.040 / 0.032 / 0.036 | 1.0545 / 0.5731 | estimator fault changes=0，failsafe=0；末端附近无 console 消息 |
| r24plan-17 `count-fire-hydrants`（健康） | 430.096 / 430.096 / 430.096 | 0.052 / 0.036 / 0.040 | 1.0971 / 0.2629 | 429.100 s 已 `Landing detected` 并 `Disarmed by landing`，failsafe=0 |
| ext-1 Follow（冻结） | 392.840 / 392.844 / 392.844 | 0.048 / 0.036 / 0.040 | 0.9192 / 0.5373 | estimator fault changes=0，failsafe=0；末端附近无 console 消息 |
| ext-2 Follow（健康） | 621.556 / 621.572 / 621.572 | 0.052 / 0.032 / 0.036 | 1.0670 / 0.1939 | 620.920 s 已 `Disarmed by landing`，failsafe=0 |

冻结日志在停止前的高频 topic 仍然连续，且没有 estimator/failsafe 先行报警；这支持
“PX4 更新链路整体停住”，但不能从 analyzer 单独定位到 bridge、PX4 线程或调度器。

运行配置检查发现：`scripts/run_simulator_docker.py` 在 `--gpu-render` 下将容器绑到所选
GPU，导出 `GZ_SIM_HEADLESS_RENDERING=1` 和 `GZ_SIM_RENDER_ENGINE=ogre2`，并包装启动
命令补入 `--headless-rendering --render-engine ogre2`。启动环境没有显式设置
`PX4_SIM_SPEED_FACTOR`；生成 world 的 physics 通常为 `max_step_size=0.004`、
`real_time_factor=1.0`。这些是目标速率/渲染配置，不会证明运行中 lockstep 消息持续
到达。Docker command 配有 cpuset/cpu shares，但没有固定 memory limit，因此没有证据
支持本次是显式 cgroup memory cap 造成；内存压力仍因缺少 cgroup 计数而未排除。

## 新增资源关联证据

episode 的墙钟窗口和实际 cpuset 如下。时间为新加坡时间（UTC+08）。

| 样本 | 墙钟窗口 | cpuset | sysstat 对齐观察 |
| --- | --- | --- | --- |
| r24plan-17 `count-fire-hydrants`（健康） | 2026-11-04 10:08:55–10:39:39 | 86–95（10 CPUs） | 10:10–10:40 的 load1 为 121.23–127.36，run queue 121–127；10:40 可用内存约 307.9 GB |
| r24plan-17 `survey-site-overlap`（冻结） | 2026-11-04 11:19:33–11:48:53 | 76–85（10 CPUs） | 11:10/11:20 的 load1 为 216.77/181.95，run queue 206/152；可用内存约 279.9/254.3 GB，swap used 约 2.96 GB 且 swap in/out 很低 |
| ext-1 Follow（冻结） | 2026-11-03 16:59:23–17:41:51 | 84–89（6 CPUs） | 17:00/17:10/17:20 的 load1 为 228.27/162.47/181.36，run queue 301/88/120；可用内存约 202.0/166.0/204.9 GB |
| ext-2 Follow（健康） | 2026-11-03 21:00:38–21:51:27 | 76–85（10 CPUs） | 21:00–21:40 的 load1 为 143.19、116.22、114.86、119.13、86.70；可用内存约 324.2–329.9 GB |

这说明冻结时存在明显的 host-wide CPU contention 候选条件，但它不是充分条件：r24plan-17
健康 count 也在 load1 约 127 的窗口内完成，ext-2 健康 Follow 也在 load1 超过 100 的
窗口内完成。`sar` 只反映整机，不是容器 cgroup；不能把这些值直接解释为某个 PX4
进程被 quota 限制。ext-1 窗口虽有 16:50 的 swap-in 峰值，但可用内存仍超过 166 GB，
不能据此认定 memory pressure 是冻结根因。

### 内核日志与 cgroup 证据边界

- `journalctl -k` 在 r24plan-17 冻结窗口（11:19–11:49）和 ext-1 冻结窗口（16:59–17:42）
  未见 OOM、memory cgroup OOM、hung task、GPU Xid 或其他相关内核事件；11:12:45 的
  `containerd` dead-shim 清理告警早于 survey 冻结，不能作为因果证据。
- 2026-11-04 10:38:11 的 GPU0（`0000:01:00`）Xid 13/31 发生在健康的
  r24plan-17 `count-fire-hydrants` 仍运行时（10:08:55–10:39:39），该任务随后完成；
  它与 11:19:33 的 survey 冻结不重合，也没有产物证明 GPU PCI 地址与冻结容器绑定，
  所以只能列为同机干扰线索，不能写成冻结根因。2026-11-03 13:26 的 GPU `0000:81:00`
  Xid 31 也早于 ext-1 Follow 冻结。
- 2026-11-04 17:19:20–17:19:38 的全局 OOM（约 131 GB RSS，task memcg 为
  `sunshine-desktop.service`）发生在调查样本之后；不能归因于 survey 或 ext-1。
- artifacts 没有保存上述 episode 的 cgroup 时间序列。当前工作树服务 cgroup 的
  `cpu.stat` 累计 `nr_throttled=0`、`throttled_usec=0`，当前 `memory.pressure` 有值，
  但这些是调查结束时的累计/瞬时状态，不能回溯冻结时刻，也不能证明历史上没有短时
  PSI、throttling 或 memory reclaim。故“没有记录到 kernel OOM”不等于“已经证明没有
  memory cgroup pressure”。

## 假设排序

1. **PX4/Gazebo lockstep bridge 或 PX4 仿真调度路径停止：最符合证据。** PX4 全局仿真
   时间和多类数据源同步停住，传感器帧时间继续前进；具体停在哪个进程/线程未知。
2. **PX4 模块/线程死锁或饥饿：仍可能。** ULog 在停止前没有 estimator timeout/failsafe
   迹象，但冻结本身可能让 PX4 无法再写出诊断；缺少任务/线程快照。
3. **GPU render/backpressure：可能是负载触发器，证据较弱。** 故障运行启用 GPU
   headless render，但相机帧仍持续输出；没有 GPU 指标，也没有可用的 off-GPU 对照。
4. **主机 CPU contention / 容器 CPU 或 memory cgroup pressure：候选但未证实。** 冻结
   窗口的 host load/run queue 很高，但健康任务也在高负载下完成；缺少冻结时的
   `cpu.stat`、`memory.events`、RSS/oom、PSI 和 GPU/container 绑定快照。PX4 ULog
   `cpuload`/`ram_usage` 不能代替这些计数。

## 低风险缓解

Linux multirotor primitive 原先允许 **180 秒连续墙钟时间没有仿真时钟进展**，随后只把
超时作为一次 primitive 结果，episode 可能继续重复等待。现在连续无进展上限为
**45 秒**；若 primitive 返回 wall-timeout，collector 立即将其提升为明确的
`PX4 simulation clock stalled during primitive` 并结束该 episode，让容器清理和外层
campaign 失败处理及时接手。该值只限制连续“零时钟进展”时段，仿真时钟仍推进的慢
lockstep 不受总墙钟 cap 影响。Windows 保持 180 秒既有行为。

没有在本次实现自动重跑：当前外层 campaign 的 retry/episode identity 语义需要单独
确认，盲目重试可能覆盖或重复生成 evidence。若下一轮继续定位，优先在失败快照记录
容器内 PX4/Gazebo 进程状态、bridge/lockstep 接收计数、线程栈、`cpu.stat`、
`memory.events`、GPU busy/memory、Gazebo physics step 与 camera frame 计数，并做同一
场景、相同 CPU/slot 配置下的 GPU render 开/关对照。

## 验证

- `tests/test_sim_teacher_executor.py`
- `tests/test_sim_teacher_parallel_timing.py`
- `tests/test_sim_teacher_landing.py`
- `tests/test_sim_teacher_fw_landing.py`
- `tests/test_analyze_px4_ulog.py`：3 passed（本轮复核）

此前低风险缓解对应的定点回归为 61 项通过；本轮 analyzer 测试为 3 项通过。未运行完整
`scripts/run_onboard_gate.py`，也未运行 SITL、Docker 或 GPU。
