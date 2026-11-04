# Linux SITL PX4 仿真时钟冻结：根因调查

调查日期：2026-11-04。只读检查指定 benchmark 产物、campaign 日志及仓库中的
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

使用仓库 `scripts/analyze_px4_ulog.py` 对指定 runs 中每一份保留 `.ulg` 逐个分析，
阈值为 1 秒，输出仅放在工作树临时目录。覆盖为：r24plan-7 **0** 份、r24plan-8
**5** 份、ext-1 **4** 份、r24plan-17 **14** 份，共 **23** 份；r24plan-7 的目标
count episode 没有 ULog，只能用 MAVLink/steps/episode 元数据。所有日志均由 analyzer
处理，包含正常任务日志及失败任务日志；其最后 topic 样本、间隙、PX4 cpuload/RAM、
估计器 flags、failsafe 与 console 均纳入复查。诊断脚本不能从 ULog 恢复容器 stdout，
也不能观测 PX4 未记录的线程状态或 Gazebo transport queue。

运行配置检查发现：`scripts/run_simulator_docker.py` 在 `--gpu-render` 下将容器绑到所选
GPU，导出 `GZ_SIM_HEADLESS_RENDERING=1` 和 `GZ_SIM_RENDER_ENGINE=ogre2`，并包装启动
命令补入 `--headless-rendering --render-engine ogre2`。启动环境没有显式设置
`PX4_SIM_SPEED_FACTOR`；生成 world 的 physics 通常为 `max_step_size=0.004`、
`real_time_factor=1.0`。这些是目标速率/渲染配置，不会证明运行中 lockstep 消息持续
到达。Docker command 配有 cpuset/cpu shares，但没有固定 memory limit，因此没有证据
支持本次是显式 cgroup memory cap 造成；内存压力仍因缺少 cgroup 计数而未排除。

## 假设排序

1. **PX4/Gazebo lockstep bridge 或 PX4 仿真调度路径停止：最符合证据。** PX4 全局仿真
   时间和多类数据源同步停住，传感器帧时间继续前进；具体停在哪个进程/线程未知。
2. **PX4 模块/线程死锁或饥饿：仍可能。** ULog 在停止前没有 estimator timeout/failsafe
   迹象，但冻结本身可能让 PX4 无法再写出诊断；缺少任务/线程快照。
3. **GPU render/backpressure：可能是负载触发器，证据较弱。** 故障运行启用 GPU
   headless render，但相机帧仍持续输出；没有 GPU 指标，也没有可用的 off-GPU 对照。
4. **容器 CPU/memory cgroup throttling：未证实。** cpuset 隔离下仍复现；没有
   `cpu.stat`、`memory.events`、RSS/oom 或 host pressure 数据。PX4 ULog `ram_usage`
   不能代替这些计数。

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

共 61 项通过。未运行完整 `scripts/run_onboard_gate.py`，也未运行 SITL、Docker 或 GPU。
