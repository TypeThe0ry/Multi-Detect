# gen_mov_w0000 停滞复现 A/B 结果（2026-11-08）

后续[只读时序复核](clock-stall-lifetime-attribution-20261108.md)更正：旧五次失败并非位置消息一直不恢复；watchdog 后存在恢复，且静默窗口覆盖全部原始 MAVLink。恢复阶段的 Gazebo/IMU 采样不能证明此前全程健康。以下保留本次 A/B 的实测结果，旧故障的解释以复核报告为准。

## 结论

本次 moving-target 与 stationary-target episode 都以 collector `complete` 结束，MAVLink 位置时钟在各自记录内连续前进，没有重现 45 秒停滞。但两组都在 `area_search` 用尽 step budget，随后返航；mission summary 将 `follow` 记为未完成/未知。因此该配对说明本次没有复现早先的 `clock_stalled_alive`，不能说明两分钟跟踪任务成功，也不能凭一对 clean run 排除间歇性停滞。

早些时候的 `follow-track-fixed-gimbal-20261108-v2` 有 6 次尝试，其中 5 次停滞（详见 [前序离线分析](follow-w0000-stall-20261108.md)）。这些 failure 的关键差别是位置时钟有 50–59 秒的消息空窗；A/B clean episode 的最大相邻位置消息间隔分别是 0.790 s 和 0.704 s。整体 RTF 在旧失败组与 A/B clean 组重叠，host pressure 也有重叠，故较有区分力的是 PX4 位置时钟是否实际持续推进，而非 load/PSI 或整段平均 RTF。

## 运行身份与并发背景

旧 v2 的五个停滞 episode 都在 worker/slot 2 上运行；A/B moving 与 stationary 也都在 slot 2、`cpuset_cpus=[86..95]` 上运行。episode `host_pressure.jsonl` 的 cpuset 一致，A/B 同一 fixed-camera airframe、同一 w0000 world 初始位置和同一 onboard 配置，仅 stationary 组不启动 mover driver。因 slot 和 SITL cpuset 一样，它们没有形成 slot/cpuset 隔离效应的对照。

只读的 `runs/host-trace/20261108.jsonl` 在失败和 clean 窗口都记录到并行 CPU 活动：重复出现 `user.slice/user-0.slice`、`sunshine-desktop.service`、`md-r10-train.service`、`f1aero-loops` 与 F1/Gaius scope。在五个旧失败的最后位置消息静默窗口附近，4/5 的 host-trace 样本可见 3 个 Docker cgroup（含该 episode 自身）；高 memory/CPU PSI 的一次重试窗口只看到 0–1 个 Docker cgroup。A/B moving/stationary 整段窗口通常可见 2–3 个 Docker cgroup（该字段只列每次采样的 top cgroup，不能代表全部作业）。pressure trace 没有将其他 Docker ID 绑定到业务 job 名称或 SITL slot，因此无法从中给所有并发 job 可靠地命名；它也没有显示“失败时独占、clean 时无并发”的对照。A/B 窗口仍有并发宿主任务。

## PX4 位置时钟、RTF 和 PSI

RTF 按 `LOCAL_POSITION_NED.time_boot_ms` 首末进展除以接收 monotonic span 计算。停滞 run 的这个整体值只描述最终静默前实际收到的消息区间，不包含最后一条消息之后的 watchdog 空窗。旧失败数值汇总自 v2 五份 episode 的 `mavlink.jsonl` 与 `host_pressure.jsonl`；A/B 取自 moving/stationary 的对应 episode 文件。

| 组 | episode | slot / SITL cpuset | PX4 RTF | 最大位置消息间隔 | CPU PSI `some avg10` 中位 / 最大 | Memory PSI 中位 / 最大 | I/O PSI 中位 / 最大 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| 旧失败 | 5 次 `clock_stalled_alive` | 2 / 86–95 | 0.197–0.231 | 50.1–58.5 s | 中位 18.7–68.0%，最大 30.7–71.5% | 中位 0–84.5%，最大 13.2–88.0% | 中位 0.1–54.2%，最大 4.8–59.1% |
| A/B moving | `episode-0000-9389323f` | 2 / 86–95 | 0.1999 | 0.790 s | 32.34 / 44.79% | 0.12 / 21.04% | 0 / 3.67% |
| A/B stationary | `episode-0000-30b927c6` | 2 / 86–95 | 0.2458 | 0.704 s | 19.88 / 37.44% | 0.40 / 12.72% | 0 / 3.82% |

旧失败的 CPU PSI 区间覆盖了 clean A/B 两边；其中几次停滞发生在 CPU PSI 中位约 19–26% 的窗口。反过来，moving clean 的 CPU PSI 中位数高于其中几次失败记录。因此这些 host load 值不能作为可靠停滞分类阈值。旧五次均触发约 45 秒无进展 watchdog，但随后确有下一条位置消息，相邻位置接收空窗为 50.102–58.525 秒；全部原始 MAVLink 接收也有 50.100–58.521 秒空窗。这与 A/B clean 记录里最高不到 0.8 秒的位置消息间隔相异，不能将事后恢复解释为此前没有故障。

## 帧等待时间线与任务阶段

帧等待按 episode `frame_wait_timeline` 的 `frame_wait_satisfied` 记录计数，同时列出 `steps.jsonl` 中每步 pre/post frame wait 的中位数和 p95。两组 A/B 运行都保留了完整的 frame wait 事件，不以平均 RTF 代替传感器等待质量。

| episode | steps / step timeout | 满足的 frame wait 数 | satisfied wait 中位 / p95 / 最大 | step pre-frame wait 中位 / p95 | step post-frame wait 中位 / p95 | RTF |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| moving target | 177 / 0 | 354 | 0.535 / 0.783 / 1.438 s | 0.526 / 0.778 s | 0.554 / 0.848 s | 0.1999 |
| stationary target | 176 / 0 | 352 | 0.466 / 0.655 / 0.891 s | 0.466 / 0.639 s | 0.468 / 0.688 s | 0.2458 |

两组 mission summary 都显示 `s1:area_search` 因 step budget 失败，`s2:follow` 未记录完成，`s3:return_home` 成功；不能将 episode 的 `complete` 状态解读为跟踪技能完成。旧 v2 五个失败中，前四个故障点仍在 `area_search`，最后一个 episode 在 `return_home` 恢复阶段失败，`area_search` 已先因 step budget 结束。停滞并非只出现在一个任务阶段，但大多数旧失败发生于目标跟随尚未开始时。

## 解释边界与下一步

这是一对单次配对实验，moving 和 stationary 组不仅 mover driver 不同，目标是否移动的任务语义也不同；两组的 step budget 都不足以完成计划的两分钟 `follow`。结果削弱“本次一定由移动目标 driver 触发停滞”的说法，但无法区分负载贡献、场景阶段/步数差异或低概率系统问题。可支持的结论是：连续 MAVLink 位置时钟时间线能识别这五份旧记录的静默窗口，而聚合 host load/PSI 和整段 RTF 不能单独做出可靠分类。

本报告只读读取 `artifacts/benchmarks/follow-w0000-stall-ab-20261108T072730Z-2298436/`、旧 v2 artifacts、`runs/host-trace/20261108.jsonl` 与现有报告；没有启动新的 SITL/Docker/GPU 任务，没有写入共享 artifacts，也未改变 timeout、安全语义、SITL 输出或既有断言。
