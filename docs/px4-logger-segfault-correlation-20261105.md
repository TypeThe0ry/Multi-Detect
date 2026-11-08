# PX4 logger segfault 与 episode 失败关联（2026-11-05）

## 结论

内核清单有 34 条 segfault，时间为 2026-11-02 11:25:13 至 2026-11-05 17:38:27（均为 +08:00）。其中多条是 `px4+0x2885a9`，也有 `px4+0x2847f7`、`px4+0x2885ac`、libc 和 dataman 记录。

**没有任何一条能按 PID 或容器 ID 与本项目 episode 直接匹配。** 内核行只有任务名和 PID；目标运行日志没有记录可对应的宿主机 PX4 PID 或 PID namespace 映射。机器上还有其他项目使用 SITL，包括 slots 3、4 的 delta airframe loop，因此单靠时间邻近无法判定所有者。

r7 的十个失败 key 中，多个 attempt 在 `px4+0x2885a9` 记录后约 45–48 秒报告 PX4 clock stall；这与采集器的 45 秒无时钟进展阈值吻合，是有力的时间候选，但仍不是容器匹配或因果证明。另有 pre-arm、takeoff 超时的时间候选。**已核实的 logger-crash 解释数为 0/10。**

r7 的失败文档描述的是一个失败快照。稍后的 round-state 将 30/30 个逻辑 episode 标为 `complete`，累计记录 97 次尝试；数据审计最终接受 28 个，另排除 `w0013` 的两个架次是几何/观察质量问题。也就是说，r7 的重试消除了最终逻辑状态中的采集失败；不能据此断言早期失败就是 logger crash。

## 口径与输入核对

时间使用日志和 artifact 的 `+08:00` 墙钟值。下表的 `Δ` 定义为 `segfault 时间 - 失败记录时间`，负数表示 segfault 先发生，正数表示 episode 失败先发生。以 5 分钟作为筛查近邻的窗口，不是因果阈值。attempt 时间来自 episode JSON 的 `finished_wall_time`；episode key 的最终失败文本沿用 [r7-collection-failures-20261105.md](r7-collection-failures-20261105.md)。

用户提到的 `queue_r21id_r4c_collect.out` 与现存记录不符：该文件记录 2026-11-01 02:49:08 开始、06:10:36 `exit 0`，没有 `exit 139`、`segfault` 或 `signal 11`；它也早于内核清单的首条记录。可见的 `queue_r21id_r6_collect*.out` 为 0 个，因此 r6 没有可核对的 collection 输出。r4/r5 的其它收集日志中也没有可用于关联 logger PID 的容器/PID 映射。

## r7 十个失败 key

“最近 segfault”是相对于表内文档记载的最终失败记录计算；另列更早 retry 中的时间候选，避免把 attempt 错当成最终 episode 失败。

| Episode | 最终失败文本与时间 | 最近 segfault | 关联判定与依据 |
| --- | --- | --- | --- |
| `w0000:1` | clock stalled，step 13、45 s；02:25:20 | 02:26:03，`px4+0x2885a9`，Δ=+43 s | 最终失败早于此 segfault，不能由它引起。另一个 attempt 在 01:10:27 clock stalled，01:09:42 有同偏移 segfault，Δ=-45 s；仅为 attempt 级时间候选。 |
| `w0002:1` | 等待 `takeoff_reached` 190 s 超时；02:52:35 | 02:50:53，`px4+0x2885a9`，Δ=-102 s | 时间近邻但无 PID/容器证据。更早 attempt 有 02:27:32 `ekf_ok` 超时（02:26:03 segfault 后 90 s），以及 02:42:28 clock stall（02:41:42 后 47 s）；均只能列候选。 |
| `w0005:0` | clock stalled，step 2、45 s；05:18:29 | 05:10:11，`px4+0x2885a9`，Δ=-498 s | 最终失败不近。两个早期 attempt 分别在 04:49:44、05:10:57 clock stalled，最近 segfault 分别早 48 s、46 s；attempt 级时间候选。 |
| `w0006:0` | clock stalled，step 70、45 s；06:13:17 | 05:29:45，`px4+0x2885a9`，Δ=-2612 s | 最终失败不近。早期 attempt 在 04:38:51 和 05:30:30 clock stalled，segfault 分别早 45 s、46 s；attempt 级时间候选。 |
| `w0006:1` | clock stalled，step 26、45 s；06:47:53 | 06:47:56，`px4+0x2885a9`，Δ=+3 s | segfault 发生在该失败记录之后，不能解释它。另一个 attempt 的 pre-arm health 失败在 06:19:52，06:19:05 有 libc 内 logger segfault，Δ=-47 s；这是不同 attempt 的时间候选。 |
| `w0007:0` | pre-arm `xy_position_control` 未通过；06:22:04 | 06:19:05，libc 内 segfault，Δ=-179 s | 最终失败时间较远。另一个 attempt 在 06:19:51 clock stalled，距 06:19:05 logger segfault 46 s；时间候选。 |
| `w0007:1` | clock stalled，step 3、45 s；06:29:35 | 06:19:05，libc 内 segfault，Δ=-630 s | 最终失败时间较远。另一个 attempt 在 06:19:52 出现 pre-arm health 失败，距同一 segfault 47 s；时间候选。一次 segfault 邻近两个 worker 的失败，尤其需要 PID/容器映射才能区分归属。 |
| `w0008:0` | sensor stream 15 s 无帧；08:03:55 | 07:55:00，`px4+0x2885a9`，Δ=-535 s | 最终失败不近。另一 attempt 在 07:55:45 clock stalled，segfault 早 45 s；attempt 级时间候选。 |
| `w0008:1` | 等待 `takeoff_reached` 200 s 超时；06:44:21 | 06:36:33，libc 内 segfault，Δ=-468 s | attempt-2 的 200 秒等待在 06:39:06 结束；06:36:33 segfault 落在该等待期间，且早于超时 153 s。仅是区间重叠候选，无法证明它属于该 PX4。 |
| `w0009:0` | 等待 `ekf_ok` 90 s 超时，缺 `GLOBAL_POSITION_INT`；07:01:07 | 06:58:11，`px4+0x2885a9`，Δ=-176 s | 最终失败不符合 45 秒间隔。此前三个 attempt 在 06:48:41、06:53:17、06:58:57 clock stalled，分别在对应 segfault 后 46–47 s；attempt 级时间候选。 |

按 attempt 而不是最终 key 计数，6/10 key 有 clock-stall attempt 在 `px4+0x2885a9` segfault 后约 45–48 秒结束；`w0007:0` 另有一次 clock-stall attempt 在 libc 内 logger segfault 后 46 秒结束。`w0006:1` 和 `w0007:1` 另有 pre-arm attempt 落在 libc segfault 后约 47 秒窗口内；`w0008:1` 的较长 takeoff 等待中也有一次 libc segfault。它们都没有容器身份链，故不能写成“segfault 解释了这些失败”。`w0006:1` 的最终失败在最近 segfault 之前 3 秒，明确不符合该因果方向。

## r4、r5 与 nightly 的近邻

| Episode | 失败文本与时间 | 最近 segfault | 关联判定与证据 |
| --- | --- | --- | --- |
| r4g `gen_r21id_r4_w0005:0` | 等待 land 217.9 s 超时；11-02 11:27:23 | 11:25:13，`px4+0x2847f7`，早 130 s | 时间候选；偏移不同，PID/容器未知。 |
| r5 `w0000:0` | `BrokenPipeError`；11-02 22:02:29 | 22:00:53，libc 内 segfault，早 96 s | 时间候选，错误文本不指明 PX4。 |
| r5 `w0002:0` | sensor stream early EOF；11-02 23:18:57 | 23:18:56，logger `px4+0x2885a9`，早 1 s | 时间与故障表现最相符的候选；仍无 PID/容器对应，不能确认为同一容器。 |
| r5 `w0001:1` | `BrokenPipeError`；11-02 23:37:08 | 23:34:39，logger `px4+0x2885a9`，早 149 s | 时间候选，无法区分同机其它 PX4。 |
| r5 `w0002:1` | land 208.7 s 超时；11-03 00:14:19 | 最近记录是 00:17:10 的 segfault，晚 171 s | 时间方向相反，不能由该 segfault 引起。 |
| r5 `w0001:1` | telemetry stalled；11-03 00:18:29 | 00:17:10，logger `px4+0x2885a9`，早 79 s | 近邻但超过 r7 的 45 s stall 特征；只作候选。 |
| r5 `w0003:0` | `BrokenPipeError`；11-03 00:20:07 | 00:17:10，logger `px4+0x2885a9`，早 177 s | 时间候选。 |
| nightly-7 `comms-free-patrol-timeout` | `BrokenPipeError`；11-02 19:21:13 | 19:18:41，logger `px4+0x2885a9`，早 152 s | 报告中该 episode 为 missing；时间候选，但没有容器对应。 |
| nightly-8 `monitor-backpacks` | land 等待 153.3 s 超时；11-03 12:08:06 | 12:05:21，logger `px4+0x2885a9`，早 166 s | 时间候选，不能当作 crash 归因。 |
| nightly-14 `count-cones` | `BrokenPipeError`；11-03 13:47:24 | 13:44:18，logger `px4+0x2885a9`，早 187 s | 仅时间候选。 |
| nightly-19 `patrol-fence-backpacks` | land 等待时 PX4 clock stalled；11-05 15:58:24 | 15:57:52，logger `px4+0x2847f7`，早 32 s | 近邻且故障类型相容，但地址不是 `0x2885a9`，容器身份未知。 |
| nightly-19 `comms-free-count` | sensor stream early EOF；11-05 17:11:58 | dataman segfault 17:11:12，早 46 s；logger `px4+0x2885a9` 在 17:12:07，晚 9 s | dataman 记录是时间候选；17:12 的 logger segfault 晚于失败，不能造成该失败。 |

另外，r7 `w0012:0` 的一次 clock-stall artifact 在 08:24:25 结束，最近 logger segfault 是 08:25:20，晚 55 s。r7 `w0013`/`w0014` 和 nightly gimbal-7 同时段有 Docker 网络启动失败，错误明确为 host UDP 14580 已占用；容器启动未成功，不能由 PX4 logger crash 解释。nightly gimbal-7 08:29:15 的最近 PX4 segfault 是 08:25:20，早 260 s，但失败发生在 Docker 网络创建阶段。

### 无 episode 匹配的 segfault

按 PID/容器严格匹配时，**34 条记录全部未匹配**。以下记录在目标 collection/nightly artifacts 中没有找到 ±5 分钟内可对应的 PX4 episode 失败；“其他近邻”是上表已标为方向相反或非 PX4 启动错误的情形，不算匹配。

| 时间（+08:00） | 内核任务与地址 | 备注 |
| --- | --- | --- |
| 2026-11-04 13:58:44 | `logger[1096425]`, libc `+0xa00d4` | 未发现近邻 episode 失败。 |
| 2026-11-05 08:47:00 | `logger[3633257]`, `px4+0x2885a9` | 未发现近邻 episode 失败。 |
| 2026-11-05 13:26:38 | `logger[3595246]`, `px4+0x2885ac` | 未发现近邻 episode 失败。 |
| 2026-11-05 17:38:27 | `logger[1951020]`, `px4+0x2885a9` | 未发现近邻 episode 失败。 |

此外，08:25:20 与 08:33:35 的 segfault 虽接近 Docker 启动错误，但那些记录没有启动 PX4；17:12:07 的 logger segfault 比 `comms-free-count` 的 stream EOF 晚 9 秒；这些均不构成匹配。

## 和 2026-11-04 clock-freeze 调查的关系

[px4-clock-freeze-root-cause-20261104.md](px4-clock-freeze-root-cause-20261104.md) 里的 freeze episode 没有与本清单时间重合：

- r24plan-7 `count-fire-hydrants` freeze 在 11-02 14:51 左右；最近 logger segfault 是 11:25:13，早约 3 小时 26 分。
- r24plan-7 `budget-reserve-return` freeze 在 11-02 21:07 左右；最近相关 segfault 在 22:00:53，晚约 53 分。
- ext-1 Follow freeze 在 11-03 17:41 左右；最近 segfault 在 13:44:18，早近 4 小时。
- r24plan-17 `survey-site-overlap` freeze 在 11-04 11:48 左右；最近 segfault 在 13:58:44，晚约 2 小时 10 分。

两者目前应视为**不同的观测故障模式，未证明是同一事件**。freeze 调查看到多个 PX4 MAVLink/ULog topic 停止推进而传感器帧时间继续前进；当时 PX4 进程是否仍存活没有证据。logger segfault 则是进程内线程发生致命内存访问的内核记录。二者可能共享未识别的触发条件，但现有时间线不支持将 logger segfault 写作 freeze 根因。

## 崩溃对 PX4 和 episode 的影响

Linux `signal(7)` 将 SIGSEGV 的默认动作列为 `Core`，即终止进程并生成 core；信号处置是进程级的。硬件内存异常可以定向送到发生异常的线程，但未处理的默认动作仍终止整个进程。因此 logger 线程发生未捕获 SIGSEGV 时，**PX4 进程会退出，不是只留下一个继续飞行但没有 ULog 的 PX4**。本机观测到的容器 exit 139 与这一行为一致。这里的标准依据是 Linux man-pages `signal(7)` 和 `pthreads(7)`。

PX4 退出后，episode collector 可能先看到 sensor stream EOF/BrokenPipe，或在 MAVLink/仿真时钟等待中超时；r7 的零时钟进展 watchdog 会在约 45 秒后把它记成 clock-stall attempt。队列收集器随后可重试，重试耗尽才会令该 logical episode 失败。nightly 的 suite 命令默认不传 `--retry-failed-episodes`，因此其未完成 episode 会进入 completion 统计；这只能说明“若 crash 落在该 episode，失败会被计入”，不能证明表中任何候选真的属于该 crash。

## 重试和已发布指标

- r7 的队列日志给每个 episode 最多 3 次重试。最终 [round-state.json](../artifacts/r21id-r7a/pipeline/round-state.json) 报告 30/30 `complete`，总 attempt 数为 97；[r7-data-audit-20261105.md](r7-data-audit-20261105.md) 将 w0013 两个 episode 排除在可用数据之外，原因是采集几何和观察视角不合格，不是 segfault 归因。
- nightly-7 的汇总 `completion_rate` 为 0.5，并把 `comms-free-patrol-timeout` 列为 missing；nightly-19 的 `completion_rate` 为 0.4286。相邻的 BrokenPipe/clock-stall 会成为该轮的未完成 outcome，但 PID/容器未关联，所以不能说这些已发布比例把“logger crash”计成 mission failure。
- 当前证据下，没有一个已发布指标可确认地把某条 logger segfault 计作 mission failure。r7 的失败 attempts 被后续成功重试从最终 logical status 中消除；nightly 的近邻失败仍留在 completion 统计中，但其 crash 归属未证实。

## ULog 与 logger polling workaround

本项目的任务采集、episode 评分和训练数据整理不消费 `.ulg` 作为必要输入。采集器的 `--keep-px4-ulog` 是可选复制开关；`scripts/analyze_px4_ulog.py` 是独立的离线诊断工具，clock-freeze 调查也用保留的 ULog 做过 postmortem。因而 logger polling workaround 对任务数据本身没有必要输入成本。

`wt/cx-delta-fw` 分支的 `MULTIDETECT_PX4_LOGGER_MODE` 改动仅在启动器中改变 PX4 logging 配置：`poll` 增加 `-p vehicle_attitude`，缩小记录 topic；`off` 关闭 SDLOG。该分支文档称 tailsitter 默认 poll，其他机型仍 default。若使用 `poll`，代价是丢失其他 ULog topic 的诊断覆盖；若用 `off`，则失去 ULog postmortem。`poll` 的稳定性/运行期开销和当前关联分析没有经过真实 SITL 验证，本次没有验证或运行 SITL。

