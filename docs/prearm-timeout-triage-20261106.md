# PX4 pre-arm 超时分诊（2026-11-06）

## 结论

`_wait_prearm_health` 原先将 45 秒预算完全按主机单调时钟计算。`nightly-linux-r21idr3-r24plan-24-def45` 的四个目标架次都在等待约 45.3 秒后超时；从 episode 创建到失败快照总计约 105.6–132.9 秒。等待期间 `LOCAL_POSITION_NED.time_boot_ms` 只增长 2.852–3.476 秒，相当于仿真速度约为墙钟的 6.3%–7.7%。这证明该预算在仿真时钟很慢时先到期，不能据此断定 PX4 的位置估计永远不会收敛。

四个架次分别为 `comms-free-count`、`comms-free-patrol-timeout`、`english-find-person` 和 `patrol-fence-backpacks`。每个架次的 `SYS_STATUS` 在整个等待期都报告 `xy_position_control` 未健康；同期仍收到 `LOCAL_POSITION_NED`，位置值靠近起点。后者只证明位置遥测在流动，不代表 EKF 的水平位置有效。保留的 `.ulg` 文件未能在本环境解码（未安装 `pyulog`），因此无法从 estimator 状态确认具体未满足的 EKF 条件。最稳妥的诊断是：墙钟预算过早结束了健康检查，超时前 PX4 仍报告水平位置控制不健康；是否在更长仿真时间后收敛，现有证据不能确定。

四个失败架次均为 `px4_logger_mode=default`，失败快照的 load average（1/5/15 分钟）为：

| 任务 | PX4 仿真时间增量 / 墙钟等待 | Load average |
| --- | ---: | --- |
| comms-free-count | 2.852 s / 45.26 s | 152.63 / 165.12 / 155.52 |
| comms-free-patrol-timeout | 2.884 s / 45.24 s | 159.66 / 165.06 / 157.00 |
| english-find-person | 3.140 s / 45.25 s | 169.27 / 172.16 / 155.83 |
| patrol-fence-backpacks | 3.476 s / 45.39 s | 129.78 / 134.08 / 143.24 |

失败快照显示 PX4 进程仍存活。并行的 `r24plan-22-off45` 与 `r24plan-23-def180` 报告中 `preflight_health` 均为 0，与 `-24-def45` 的 4 次形成对照；不过 `-24-def45` 的 14 个 episode 元数据都标为 slot 0，因此这组文件本身不能还原同一时刻的三个 worker 分配。记录的负载与仿真变慢一致，但这些数据不能证明负载是唯一原因。

## 历史匹配

按完整错误文本搜索先前 nightly 与 r9p/r9q 产物，找到 3 条先前失败：

| 产物 | 失败数 | Logger mode | Load average |
| --- | ---: | --- | --- |
| nightly `r24plan-5` | 1 | 未记录 | 未记录 |
| r9p | 1 | default | 130.08 / 124.41 / 119.60 |
| r9q | 1 | off | 298.24 / 276.91 / 246.35 |

因此，指定历史范围内与当前四条合计 7 次同文本失败：logger mode 为 default 的 5 次、off 的 1 次、mode 未记录的 1 次。r9q 的高负载且 logger 关闭，r9p 的较高负载且 logger 默认开启；样本量不足以把失败归因于 logger 模式或某个负载阈值。r9p/r9q 只保留 episode 元数据，没有足够的逐时钟 MAVLink 记录来比较仿真推进速率。

## 代码调整

Linux pre-arm 等待现在以 PX4 `time_boot_ms`/仿真时钟累计默认 45 秒，并受独立 900 秒墙钟上限约束；调用处还将该上限限制在 episode 剩余墙钟预算内。没有仿真时间戳时 `_SimClock` 按墙钟推进，因而旧适配器仍能超时。Windows 仍使用既有墙钟预算。测试覆盖慢速推进下等待健康状态变更，以及仿真时钟冻结时硬上限仍会终止。

## `r24plan-22-off45` 消防栓计数

`count-fire-hydrants` 任务完成且安全指标通过，但 `min_count_exact` 失败：场景真值为 3 个消防栓，系统确认并报告 2 个（匹配 2 个，漏计 1 个，`count_exact=false`、`mean_abs_count_error=1`）。计数步骤以 `coverage_complete` 成功结束，但结果被标为 lower bound；复查候选中有一个因与已计数目标共视而跳过，另两个因没有清晰观察点而跳过。没有 false finding。也就是说，这是搜索覆盖/确认不足造成的少计，不是采集超时或 PX4 故障。
