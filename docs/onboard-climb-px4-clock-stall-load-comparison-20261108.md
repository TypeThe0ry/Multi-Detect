# PX4 时钟停滞与宿主负载离线对照（2026-11-08）

## 结论

现有样本不能用 host load 或 PSI 单独区分 PX4 时钟停滞与正常运行。成功与失败 episode 的 CPU PSI、RTF 均有重叠；可见的停滞特征是 PX4 `LOCAL_POSITION_NED.time_boot_ms` 长时间不推进，而不是某个已经验证过的宿主压力阈值。这个判断仍是有限样本的离线观察：负载可能是风险背景，但不能由本次记录认定为根因。

## 口径与证据

主失败证据为 `/data/projects/Multi-Detect-env/runs/src-followup2/.tmp/jobs/climb-fault-sitl4-1169782/artifacts/onboard-climb-fault-sitl4-20261108T002732Z-1169782/operator-abort/campaign/gen_cnt2_w0003/episodes/episode-0000-540aee44/` 下的 `steps.jsonl`、`mavlink.jsonl`、`host_pressure.jsonl`、`episode.json` 与 `px4-clock-stall-capture.json`。同机健康负载对照取自 `artifacts/benchmarks/clock-sensor-load-20261108b/{baseline,loaded}/`，均为同一 slot、同一 SITL cpuset 和相同语义输入；加载组由 condition 元数据记录为 CPU 0–11 上的 stress-ng。

RTF 在此按首末 MAVLink `LOCAL_POSITION_NED.time_boot_ms` 差值除以其接收 monotonic 时间差计算。单个 climb primitive 的 RTF 另外按 `steps.jsonl` 中仿真时间差除以 `command_and_primitive_wait_ms` 计算。frame wait 表只汇总 `frame_wait_satisfied` 事件；pressure 使用各样本的 PSI `some avg10`。因此整段 RTF 与某一个 action 的 RTF 不可互换。

## 已完成的 baseline / loaded 对照

两项 `condition.json` 都是 `complete`，collector exit code 为 0，slot 都是 2、SITL cpuset 都是 `86-95`，任务输入 hash 相同。baseline 时间为 08:59:43–09:09:49 UTC；loaded 时间为 09:09:49–09:19:28 UTC，stress-ng 报告 12 个 CPU worker 成功运行 9 分 38.68 秒。episode 两边均为 `complete`，各有 2 个成功 step，没有 timeout；两边 `LOCAL_POSITION_NED` 最大接收间隔分别为 0.981 s 和 1.074 s。

| 条件 | episode wall span | PX4 时间 / wall 时间 | RTF | CPU PSI some avg10（中位 / p95 / 最大） | Memory PSI（中位 / p95 / 最大） | I/O PSI（中位 / p95 / 最大） |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline | 533.84 s | 41.660 / 533.836 s | 0.0780 | 20.51 / 24.34 / 26.14% | 1.16 / 7.20 / 8.83% | 0.12 / 2.27 / 6.85% |
| loaded | 512.14 s | 41.716 / 512.140 s | 0.0815 | 25.61 / 29.53 / 31.36% | 0.21 / 2.65 / 4.47% | 0 / 0 / 0% |

在这组对照里，CPU 0–11 的 stress-ng 使 CPU PSI 中位数从 20.51% 升至 25.61%，但没有带来 PX4 时钟长间隔或 mission timeout；RTF 也近似。宿主在 baseline 时本身已有明显压力，所以这不是“空闲机器与高负载机器”的对比。

| 条件 | 第 0 步 obs 前 / 后 frame wait | 第 1 步 obs 前 / 后 frame wait |
| --- | ---: | ---: |
| baseline | 1.055 / 1.011 s | 1.121 / 0.771 s |
| loaded | 0.770 / 0.975 s | 0.697 / 0.650 s |

四组 frame wait 均有对应的 `frame_wait_satisfied` 事件；loaded 条件没有比 baseline 更长的整组等待。原目录 `artifacts/benchmarks/clock-sensor-load-20261108/{baseline,loaded}` 记录的两次 collector 都在容器启动时因 `127.0.0.1:14560` 端口已占用而退出，未形成有效对照；这里采用已完整结束的 `clock-sensor-load-20261108b`。

## climb 失败记录

目标 episode 的 `step 1`（0-based）是 `return_home` 中注入 `return_home_climb_timeout` 的 `translate`：PX4 pose clock 从 39.436 s 到 59.632 s，primitive wall wait 为 95.335 s，primitive RTF 为 0.212。整段 MAVLink 位置记录有 3,300 条，首末 `time_boot_ms` 前进 66.856 s、monotonic span 为 299.430 s，整段 RTF 为 0.223。捕获前最后一条位置消息是 69.672 s；capture 的 `deadline` 与 `after_3s` 分别距该消息约 48.8 s 和 68.0 s，且诊断记录 45 s 无位置时钟进展。也就是说，长尾停滞被 position 消息的静默窗口捕获，不能只看最后一个已收到样本之前的平均 RTF。

这份 episode 标记为 `clock_stalled_alive`，但其触发背景包含显式的 climb timeout fault injection，不能视为自然负载下的独立故障复现。宿主压力为 82 个样本，load1 中位数 233.03、CPU core busy 均值中位数 97.36%；CPU PSI `some avg10` 中位数 / p95 / 最大为 25.07 / 53.54 / 55.53%，memory 为 7.75 / 10.79 / 12.27%，I/O 为 3.59 / 22.21 / 23.90%。step 期间已有 5 次 frame wait 满足，等待中位数 0.691 s、最大 1.177 s；这不能证明 watchdog 后相机持续工作。

失败 episode 的 CPU PSI 中位数 25.07% 与健康 loaded 的 25.61% 几乎相同。失败窗口的高分位更高，但样本仅一份，不能从中设阈值；后续 follow A/B 中的五个停滞窗口也有四个处于较低 CPU PSI，见 [A/B 对照报告](follow-w0000-stall-ab-results-20261108.md)。

## 离线分析和测试

`scripts/analyze_px4_clock_stall_timeline.py` 现在也报告 LOCAL_POSITION_NED 的整段 RTF 与接收间隔分布、capture 时距最后一条位置消息的 monotonic 时间、step outcome 计数、episode frame-wait 事件及逐条 host pressure 时间线。它们是描述性指标，不输出负载阈值或根因判定。新增 CPU-only 合成测试验证高压力值与时钟速率/消息间隔分开报告、frame wait 事件保留，以及 watchdog capture 和最后位置消息的时间差。

只运行了指定测试文件；没有启动 SITL、Docker、GPU 或新的压力任务，没有写入 `artifacts/`，也没有修改 timeout、安全语义、SITL 输出或既有断言。
