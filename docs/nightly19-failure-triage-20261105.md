# Nightly 19 main 失败分诊（2026-11-05）

## 结论

main 套件均为 14 个任务。run16、run17、run19 的预期通过率分别为 `14/14 (100%)`、`13/14 (92.9%)`、`9/14 (64.3%)`；采集完成率分别为 `9/14`、`8/14`、`6/14`。run19 有 5 个架次未完成，错误为 3 次仿真时钟停滞、1 次 wire stream 提前 EOF、1 次等待 `takeoff_reached` 超时。

代码变化是可能原因之一，不能把下降全部归给 crash 或资源争用。run19 引入 Linux primitive 等待的 45 秒时钟停滞截止；`count-fire-hydrants` 与 `operator-abort` 正好以这条新错误退出。这能解释两条任务为何比 run17 更早结束，但不能说明时钟为何停滞。`patrol-fence-backpacks` 的 landing stall 与 logger segfault 只差 32 秒，是 crash 候选；`comms-free-count` 的 wire EOF 前 46 秒有 dataman segfault，但 PX4 logger segfault 晚于 EOF 9 秒。没有容器 ID/PID 映射，不能把候选写成确认归因。

run19 main 用时约 5 小时 19 分，run16/17 main 约 2 小时 34 分和 2 小时 57 分。r6/r6b 训练、r7 重试和 joint2 nightly 提供了资源竞争的可能背景，但可用记录没有失败时的负载、GPU 显存或 PX4 存活采样。现有证据支持“新截止 + 个别 crash 候选 + 竞争可能性”的组合解释，不足以证明 crash 和竞争单独解释了全部 5 个失败。

## 五个失败与 segfault

episode 时间取自 run19 的 `episode.json` `finished_wall_time`，转换为 +08:00。segfault 时间取自 `runs/px4-segfaults-20261105.txt`；Δ 定义为 segfault 时间减失败时间，负数表示 segfault 较早。该清单只有任务名和宿主机 PID，没有容器或 episode 对应关系。

| 任务 | episode 失败时间 | 失败文本 | 附近 segfault | 判断 |
| --- | --- | --- | --- | --- |
| `count-fire-hydrants` | 14:03:36 | primitive 时仿真时钟停滞，step 69，45 秒 | 13:26:38 `px4+0x2885ac`，Δ=-36:58 | 时间相距较远；新 45 秒截止与失败文本直接吻合。 |
| `patrol-fence-backpacks` | 15:58:24 | landing 等待期间仿真时钟停滞 | 15:57:52 logger `px4+0x2847f7`，Δ=-0:32 | 最强 logger 时间候选；没有 PID/容器匹配，不能确认同一 PX4。 |
| `comms-free-count` | 17:11:58 | wire stream 提前 EOF，期望 276 字节、收到 0 | 17:11:12 dataman segfault，Δ=-0:46；17:12:07 logger `px4+0x2885a9`，Δ=+0:09 | dataman 事件是时间候选；logger 事件晚于记录的失败，不能解释先前 EOF。 |
| `operator-abort` | 18:05:58 | primitive 时仿真时钟停滞，step 22，45 秒 | 17:38:27 logger `px4+0x2885a9`，Δ=-27:31 | 不构成近邻；新 45 秒截止与失败文本直接吻合。 |
| `budget-reserve-return` | 18:39:37 | 等待 `takeoff_reached` 超时（154 秒） | 17:38:27 logger `px4+0x2885a9`，Δ=-1:01:10 | 没有近时 segfault；底层原因未知。 |

历史 episode JSON 中这 5 条的 `simulator_container`、`container_failure_diagnostics` 和 `failure_class` 均为空，因此不能判断失败时容器是否还活着、PX4 是否已退出或被 OOM kill。仅看错误字符串，crash 后的 stream EOF/等待超时可能与时钟冻结呈现相似表象。

## 代码与运行时对照

三个 main 产物记录的提交分别是 `c0701059`（run16）、`d6839660`（run17）和 `ff8812ec`（run19）。在 run16/run17 的 collector 中，primitive 等待上限仍为 180 秒；run19 将 Linux 上的上限改为 45 秒，并在等待超时时抛出 `PX4 simulation clock stalled during primitive`。这不是纯粹的新标签：若时钟暂停后 45 秒内不恢复，collector 会把架次提前判失败。两条对应失败之前 run16 和 run17 均成功完成了这两个任务，所以此改动能解释 run19 新增的失败风险；不能从现有日志判定它发现了永久冻结，还是把较长的暂时停顿变成了失败。

其他主要代码差异包括通用 airframe/gimbal 路径。上述 5 个任务使用默认 x500 multirotor 路径；当前证据没有把这些通用路径变化和具体失败联系起来。episode provenance 里的 `dirty=true` 在采集源 worktree 检查时对应未跟踪的 `artifacts/` 与 `mobileclip_blt.ts`，`git diff --name-only` 为空，未发现另一份未提交的跟踪代码变更。

| main | 产物时间范围 | 用时 | 预期通过 | 采集完成 | 提交 |
| --- | --- | ---: | ---: | ---: | --- |
| run16 | 11-03 14:22:59 至 16:57:25 | 2:34 | 14/14 | 9/14 | `c0701059` |
| run17 | 11-04 10:08:31 至 13:05:31 | 2:57 | 13/14 | 8/14 | `d6839660` |
| run19 | 11-05 13:20:16 至 18:39:43 | 5:19 | 9/14 | 6/14 | `ff8812ec` |

## 并发活动

`runs/watchdog.log` 和 `journalctl -u md-*` 记录了下列时段。时间均为 +08:00。

- r7 retry 从 09:25:44 到 12:30:42；nightly 外层任务在 11:25:10 启动，因此两者重叠约 65 分钟。main 在前序 gimbal nightly 于 13:20:16 结束后开始，晚于 r7 retry 结束。
- r6 与 r6b GPU 训练在 00:42:52 启动。对应输出文件只见启动和 `train try 1`，journal 中未找到结束事件；不能据此确认它们是否覆盖了 run19 main 全程。
- joint2 nightly 从 15:58:43 到 17:30:23。它在 patrol 失败 19 秒后启动，与 `comms-free-count` 的失败时间重叠；没有覆盖 14:03 的 count 失败，也没有覆盖 18:05 和 18:39 的两项失败。
- run19 每槽记录为 10 个 CPU、4096 shares，CPU 集为 `66-75`；run17 每槽也是 10 个 CPU、4096 shares，集为 `86-95`。槽位与 CPU 编号不同，单凭 provenance 不能断言资源条件相同或不同。

main 的耗时显著增长说明这一轮运行条件或任务进度与前两轮不同，但不是争用的直接测量。r7 retry 已在 main 启动前结束；joint2 只与部分 main 时段重叠。训练任务的结束时间以及失败时的 CPU/GPU 负载仍未知，所以无法据此量化争用对时钟停滞的贡献。

## 未知项与后续记录

- 内核 segfault PID 是否属于 5 个容器中的某个 PX4，以及 15:57:52、17:12:07 等事件与 episode 的真实对应关系。
- 每个失败和清理时的容器 `ExitCode`、`OOMKilled`、`FinishedAt`、PX4 存活状态，以及可用时的宿主机 PID。
- 失败时系统 load、GPU 显存和 r6/r6b 实际结束时间；历史产物没有这些数据。
- 45 秒 cutoff 触发时仿真时钟后来是否恢复。历史 run 无法补采这一点。

本次 collector 会在失败和 teardown 采集容器状态、PX4 存活状态及容器 ID/PID 映射；失败快照另采 load average 和可用的 `nvidia-smi` 显存数据。episode failure record 和 nightly 逐任务表会展示这些值及容器失败类，后续运行可用 PID/容器身份与内核日志做严格匹配。run19 的共享 artifacts 只读且不会被回写，因此它的历史状态仍无法补齐。详细的既有 segfault 关联边界见 [`px4-logger-segfault-correlation-20261105.md`](px4-logger-segfault-correlation-20261105.md)。
