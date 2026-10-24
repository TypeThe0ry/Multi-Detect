# SITL 每步耗时分解与安全提速

本文基于以下近期产物统计：

* `D:\Projects\Multi-Detect\artifacts\benchmarks\mission-suite-queryfirst-20261023`
* `D:\Projects\Multi-Detect\artifacts\benchmarks\mission-suite-hydrant10-20261024`
* `D:\Projects\Multi-Detect\artifacts\benchmarks\mission-suite-fullsuite-20261023`

统计脚本逐个读取每个 episode 的 `steps.jsonl`。整步墙钟用同一文件相邻行的
`receive_monotonic_ns` 差值；原语用 `outcome.elapsed_s`（PX4/Gazebo 仿真时钟）；
检测器用 `labels.onboard.fast_latency_s`；慢模型用
`sum(labels.onboard.slow_calls[*].latency_s)`。历史文件没有独立的写盘计时，因此
“其它”包含前后帧同步、序列化/编码、文件系统和未观测调度开销，不能把它全部归因于
写盘。

## 历史基线

| 任务类型 | 步数 | 墙钟均值/中位数 (s) | 原语仿真均值 (s) | 检测均值 (s) | 慢模型均值 (s) | 其它残差均值 (s) |
|---|---:|---:|---:|---:|---:|---:|
| count-cones | 401 | 9.31 / 4.19 | 2.82 | 0.20 | 0.14 | 6.15 |
| count-fire-hydrants | 1229 | 10.06 / 4.89 | 2.59 | 0.19 | 0.07 | 7.21 |
| inspect-postbox | 107 | 9.66 / 4.58 | 2.77 | 0.24 | 0.08 | 6.57 |
| comms-free-count | 73 | 10.07 / 4.61 | 3.05 | 0.20 | 0.11 | 6.71 |
| comms-free-patrol-timeout | 31 | 16.60 / 20.24 | 5.26 | 0.20 | 0.04 | 11.10 |
| budget-reserve-return | 43 | 11.32 / 7.44 | 3.04 | 0.20 | 0.04 | 8.04 |
| english-find-person | 12 | 11.38 / 5.05 | 2.99 | 0.23 | 0.12 | 8.05 |
| find-suv-land | 6 | 11.15 / 11.88 | 3.12 | 0.23 | 0.21 | 7.59 |
| high-risk-monitor-link-loss | 45 | 10.40 / 7.30 | 2.96 | 0.21 | 0.07 | 7.16 |
| monitor-backpacks | 111 | 10.50 / 7.88 | 2.81 | 0.25 | 0.03 | 7.41 |
| operator-abort | 43 | 10.73 / 7.23 | 3.01 | 0.21 | 0.04 | 7.47 |
| operator-pause-resume | 84 | 8.88 / 5.00 | 2.41 | 0.17 | 0.03 | 6.27 |
| patrol-fence-backpacks | 68 | 18.94 / 21.87 | 5.85 | 0.26 | 0.04 | 12.79 |
| survey-site-overlap | 47 | 18.59 / 20.86 | 5.90 | 0.26 | 0.00 | 12.43 |
| **全部任务** | **2300** | **10.47 / 5.17** | **2.88** | **0.20** | **0.08** | **7.30** |

全部步骤共 712 次慢调用、总延迟约 183.8 s，平均每步只占约 0.8%。因此最大的
可压缩候选是前后帧同步与锁步/调度残差；巡检和重叠巡航的原语仿真时间是第二大项，
但改变它会改变飞行时序，不能默认开启。

## 已实现且默认不改变行为

### 异步 episode 元数据快照

采集器新增 `--async-metadata-writes`。它只把每步重写 `episode.json` 的小型快照放入
单消费者队列；RGB、segmentation、depth 和 `steps.jsonl` 仍同步写入，慢模型所需的
机载帧路径也不延后。`mark_finished()` 会等待队列完全排空并传播写线程异常，因此正常
结束时元数据和步骤数完整；已经同步追加到 `steps.jsonl` 的步骤不会因后台快照落后而丢失。
默认关闭，预计在慢磁盘上节省约 0.5–3%（需在目标主机实测；历史数据没有写盘计时）。

### 可选更紧的帧轮询

新增 `--sensor-poll-interval-s`，默认仍为 `0.02`。设为 `0.01` 只减少发现已满足条件
的 latest frame 时的调度延迟；返回条件未变，仍要求 `received_monotonic_ns > previous_ns`
且 `sensor_stamp_ns >= min_sensor_stamp_ns`。前后帧各一次，理论上最多减少约 20 ms，通常
约 0–0.5%；更高频轮询会增加主机调度开销。

## 仅方案/验证计划（不默认开启）

* 缩短 hold/turn/reposition 时长或放宽完成判据会改变轨迹和观测，必须按相同 seed 对比
  位置、姿态、完成状态和帧时间戳后才能评估。
* YOLOE 半精度、批量、降分辨率或改变 PNG/JPEG 输入可能改变框和后续动作；先做离线
  replay 一致性测试，再做 SITL A/B。
* 慢模型并行/预取可能改变调用顺序、超时和共享 runner 状态；需证明决策序列逐步一致。
* Gazebo 实时因子、物理步长、`PX4_SIM_SPEED_FACTOR` 会改变仿真时序和控制稳定性，
  只能作为独立实验开关。验证应记录相同 seed 的轨迹、原语状态、传感器时间戳、任务
  完成率和安全拒绝率。

## SITL 验证命令

以下命令只在专用 SITL 主机执行；本次改动未启动 SITL/Docker。

```powershell
$env:PYTHONPATH='src;.'
$env:PYTHONUTF8='1'
$out='D:\Projects\mdwt\_scratch\cx-sim-speed\async-meta'
python scripts/collect_sim_teacher_episodes.py `
  --scenario configs/scenarios/<scenario>.json `
  --teacher onboard --episodes 1 --steps-per-episode 70 `
  --out $out --async-metadata-writes --sensor-poll-interval-s 0.01
python -m multidetect.sim_teacher.recording $out
```

对照组省略 `--async-metadata-writes` 并保留 `--sensor-poll-interval-s 0.02`。比较
`steps.jsonl` 的动作、`outcome`、帧时间戳、`episode.json` 的 `step_count/status`，
再按上表方法计算均值和 p90。崩溃恢复测试应在已追加若干步骤后终止采集器，确认
`steps.jsonl` 中已完成行和对应帧仍可审计；异步元数据快照尚未排队完成的最后一份
`episode.json` 不应被当作步骤数据源。

## 新版逐步计时字段

采集器现在在每个 `steps.jsonl` 行写入可选的 `timing` 扩展对象。所有值都是该步骤
内以 `time.monotonic_ns()` 取样、四舍五入到毫秒的非负数；字段缺失表示旧采集器或
该步骤没有执行对应阶段，不应解释为零。字段名及含义如下：

| 字段 | 含义 |
|---|---|
| `frame_before_wait` | 等待前帧，包括新帧和水位条件 |
| `observation_pack` | 将前帧整理为策略输入 |
| `policy_detection` | 检测器执行 |
| `policy_tracking` | 航迹/关联 |
| `policy_slow_queue` / `policy_slow_execute` | 慢模型排队、调用执行 |
| `policy_planning` / `policy_safety` | 规划/执行器决策、安全出口 |
| `command_ack` | 命令下发到 PX4 及 ACK 等待 |
| `primitive_accept_wait` | 等待 PX4 接受原语 |
| `primitive_motion` | 原语运动中 |
| `primitive_settle` | 完成判定后的稳定/settle |
| `primitive_timeout` | 超时处理（无超时为 0 或省略，按实现约定） |
| `frame_after_wait` | 等待后帧 |
| `write_steps` | 追加该行 `steps.jsonl` 的写入 |
| `write_frames` | RGB/segmentation/depth 编码和写入 |
| `serialization` | JSON/其它序列化，不含文件系统写入 |
| `other` | 生命周期中尚未单独归因的时间 |

episode 的 `episode.json` 顶层扩展包含 `timing_enabled: true` 和
`timing_summary`。后者按上述阶段给出 `{count, mean_ms, p50_ms, p95_ms}`，单位均为
毫秒；汇总只使用实际存在的 `timing` 值，因此旧步骤不会被伪造为零。该扩展不改变
现有 episode schema 的必填字段，未开启计时时仍可读取旧日志。

### 下一次 SITL 跑完后的残差定位

1. 先确认 `episode.json` 的 `timing_enabled` 和 `timing_summary`，检查 `count` 是否
   接近步骤数；若为零，说明仍在使用旧采集器。
2. 用 `scripts/analyze_step_timing.py <campaign-or-episode>` 输出按任务和阶段的
   mean/p50/p95 表；优先查看 `frame_before_wait`、`frame_after_wait`、
   `primitive_*` 及 `write_*` 的 p95。
   脚本遇到没有 `timing` 的旧行时仍会输出兼容表：墙钟使用相邻
   `receive_monotonic_ns`，原语使用 `outcome.elapsed_s`，检测和慢模型使用历史
   `labels.onboard` 字段，其余明确标为未归因，避免把旧数据误报成精细阶段。
3. 对单个异常步骤回到 `steps.jsonl`，将各阶段毫秒值相加，与相邻行
   `receive_monotonic_ns` 差值比较；差额就是 `other` 或未覆盖的调度时间。
4. 将巡航类任务与短原语任务分组比较：若前后帧等待占主导，检查传感器水位/锁步；若
   `primitive_motion` 占主导，保持完成判定不变并单独评估仿真实时因子；若 `write_*`
   占主导，再比较 PNG/JPEG、深度格式和磁盘吞吐。
5. 保存分析表和对应 episode 路径到 `_scratch`，与本页历史基线并列；只有在动作、
   完成状态和帧时间戳逐步一致后，才把任何提速开关纳入后续 SITL 对照。
