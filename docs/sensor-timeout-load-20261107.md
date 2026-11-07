# PX4 传感器 timeout 与主机负载记录（2026-11-07）

## 结论

已证明四批 nightly 的 Accel `STATUSTEXT` 总数依次为 2、0、98、128（来源与计数口径见 [accel-timeout-crash-20261107.md](accel-timeout-crash-20261107.md)）。这些是 PX4 在 MAVLink 中报告的消息计数，不等同于 2、0、98、128 次独立硬件故障，也不能单独证明主机负载造成了 timeout。

现有材料**不能重建 26-off45 与 27-off45-e2 每个 episode 期间的 CPU 压力时间序列**。episode 保存了创建/结束时间、PX4 `time_boot_ms`、RTF 和少量失败快照中的 load average；但 75 个保留 episode 目录都没有 `host_pressure.jsonl`，也没有等间隔的宿主机 PSI、逐核忙碌率或 loadavg 记录。nightly 级别开始/结束时间和按 episode 计算的 RTF 可恢复，主机负载是否是 26→27 timeout 跳升的原因则仍未证实。

## 已查到的历史记录

| nightly | 开始时间（新加坡时间） | provenance commit | 保留 episode | Accel timeout | 现有 RTF 摘要（complete） |
| --- | --- | --- | ---: | ---: | --- |
| 22-off45 | 2026-11-06 06:26 | `fdcbff7aa935` | 14 | 2 | 中位数 0.098，范围 0.045–0.134 |
| 26-off45 | 2026-11-06 21:18 | `cb8244a32096` | 16（12 complete） | 0 | 中位数 0.108，范围 0.049–0.120 |
| 27-off45-e2 | 2026-11-06 21:52 | `cb8244a32096` | 28（27 complete） | 98 | 中位数 0.173，范围 0.079–0.218 |
| 28-head | 2026-11-07 02:24 | `64bc76d47fcf` | main 14、gimbal 3 | main 128、gimbal 25 | main 中位数 0.154；gimbal 1 个 complete 为 0.113 |

时间来自 artifacts 的 nightly `started_at` 和 `runs/watchdog.log`；RTF 来自先前逐 episode 分析，定义为 PX4 仿真时间增量 / episode 墙钟寿命。28-head 的 RTF 汇总和 timeout 数在 [Accel timeout 调查](accel-timeout-crash-20261107.md) 中有明细。旧 episode 不含新分析工具使用的 pressure sidecar，因此重跑 `scripts/analyze_sensor_timeouts.py` 只能得到 timeout/RTF，pressure 会显示缺失。

watchdog 记录显示，26-off45 结束约 34 分钟后 27-off45-e2 开始；27 开始前后有其他 lane 工作，27 期间还有 `r9r-worlds`、interrupted-suite、count-lower-bound 等 lane 启动。28-head 运行期间又有 `gz-idle-stall`、`r9r-pilot-gate`、`r9r-pilot2`、sensor-stream-timeout、operator-abort 等工作记录。它们证明同机有其他工作活动，但 watchdog 只记任务启动，不记录 CPU 使用率或精确运行区间，不能据此量化争用。`nightly_t/v/w.sh` 使用相同的 3-slot cpuset 配置 `66-95`、每 slot 10 个逻辑 CPU；这些是 simulator slot 配置，不是全机资源隔离证明。

已查询 `runs/nightly_t/v/w.out`、`runs/watchdog.log`、journald 时间窗和 nightly artifacts。journal 当前保留内容没有给出这些历史 episode 的逐时段负载曲线；指定四批 artifacts 中没有 host load trace 或 `host_pressure.jsonl`。部分失败快照确有瞬时 load average（例如 24-def45 的 triage 表），但不是这四批每个 episode 的采样，且 1/5/15 分钟平均值无法反推 episode 全程负载。因此无法公平比较 26 与 27 的 episode 负载或按时段做因果归因。用户提到的约 230 load / 96 线程是背景假设，本次找到的材料不能将该数值对应到这些 episode。

## pinned commit 间的代码变化

以每个 episode 的 `provenance.code.commit` 为准，而不是工作树当前 HEAD：22 为 `fdcbff7aa935dd94af1ee280c04213c7dc4d33b0`，26 与 27 都为 `cb8244a32096e377afb6785647ce3fe0e7888e90`，28 为 `64bc76d47fcf3325703185d1f798cd06b5ed260a`。nightly scripts 对应工作树分别是 `runs/src-nightly-r`（26/27）和 `runs/src-nightly-w`（28）；22 的 episode provenance 指向 `runs/src-nightly-m`。

### 22 → 26/27

范围内有 `263f2b95`（默认以 daemon 模式运行 PX4，避免 `pxh` prompt loop）、stall capture 的 GDB 增强、r9q 生成世界、nightly arm 比较工具。直接改变 PX4 运行状态的只有 daemon 模式：它能改变 PX4 线程调度与自身 CPU 消耗，理论上可能影响传感器更新时间；这是独立于外部项目负载的合理候选。但 26 与 27 使用同一个 commit，因而该变化不能解释 **26→27** 从 0 到 98 的差异。r9q worlds 不属于 `r21idr3-r24plan` nightly suite；arm 比较及 stall capture 代码不进入正常 episode 传感器数据路径。

### 26/27 → 28

范围内的运行时代码变化及与 timeout 的关系：

| 提交 | 代码变化 | 对 Accel timeout 的独立影响判断 |
| --- | --- | --- |
| `52b2c89a` | PX4 logger 默认改为 off，并增加 ULog 不可用状态记录 | logger 开关可能改变 PX4 自身 CPU 与 I/O，因此属于潜在影响因素。但 27 脚本显式设置 logger off，28 未设置但默认已改为 off；两批有效 logger 模式一致，未发现设置差异可解释跃升。 |
| `ec8e6606` | Linux pre-arm health 的 45 秒预算改按 PX4 仿真时钟计，增加 900 秒墙钟上限 | 改变 pre-arm 等待和任务所处时间窗口，可能改变 episode 启动/终止及传感器暴露时长；没有改 IMU 更新、PX4 sensor timeout 判定或 Gazebo 传感器配置。属于间接时序候选，不能排除，也不能据此证明其导致 timeout 增多。 |
| `e57c879a` | clock stall 失败时增加 Gazebo transport 与重复 GDB 诊断 | 仅失败诊断路径；采集信息本身会增加失败清理开销，但不是正常 episode 的传感器链路。 |
| `b0f2ab17` | 新增 postflight MAVLink 分析器 | 离线分析，不影响采集。 |
| `d3150654` | interrupted episode 可重试 | 可能改变失败后重新采集数量；28 的报告未显示这造成 Accel 统计重复。 |
| `29ce4843` | 新增 r9r 世界和 hard-negative cap | r9r 世界不是该 nightly 的 r21idr3-r24plan 输入；cap 属于离线数据/评估路径。 |
| `e57d03bd`、`13b86fdb` 及合并提交 | r9q/r9r/r9t 审计、场景和身份分析 | 没有改 PX4/Gazebo IMU 发布或 collector 传感器处理。 |

因此可明确排除“26 和 27 使用了不同 collector/PX4 设置”作为解释：其 pinned commit 和 logger off 配置相同；两批的 episode 数不同，采样时段和背景作业也不同。28 相较 27 有上表的间接运行时差异，但没有发现改动 PX4 Accel 驱动、Gazebo IMU 发布频率或传感器超时逻辑的代码。由于没有同期负载序列、PX4 驱动源码和受控重复试验，不能把差异归因于负载或某个代码提交。

## 新增采样与分析

collector 为每个实际启动的 episode 在 episode 目录创建 `host_pressure.jsonl`，以 5 秒墙钟间隔写入 `/proc/loadavg`、CPU/memory/I/O PSI 的 `some`/`full` avg10/avg60/avg300 与 total、采样 CPU 集合上的逐核忙碌率均值与最大值、PX4 `vehicle_time_boot_ms`，以及 wall/monotonic 时间戳。启用了模拟器 cpuset 时使用其 CPU 列表；否则使用当前有效 cpuset/进程 affinity。首次采样没有前一组 `/proc/stat` 计数，因此 busy 字段为 null；后续采样使用两次计数的差计算。MAVLink reader 启动前 PX4 时钟为 null。采样是尽力而为，缺少 `/proc`、写 sidecar 失败或线程故障都不会让 episode 失败。

运行分析器：

```bash
PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp \
  .venv/bin/python scripts/analyze_sensor_timeouts.py <episode-dir> [<episode-dir> ...]
```

每行输出 Accel/Gyro/MAG timeout 数、仿真时间 / episode 墙钟时间的 RTF，以及有 sidecar 时的 host load 和 CPU busy 汇总。旧目录没有 sidecar 时该列显示 `-`。分析器不改写原始 episode 数据。

## 未完成的验证

新采集逻辑尚未通过真实 SITL episode 运行验证；本次遵守环境要求，没有启动 SITL 或 Docker。回归应在后续正常采集时确认 sidecar 持续追加、异常路径可关闭线程，并比较新的逐 episode pressure 与 RTF/timeout 时间线。该采样提供相关性证据，不单独构成 CPU 争用因果证明。
