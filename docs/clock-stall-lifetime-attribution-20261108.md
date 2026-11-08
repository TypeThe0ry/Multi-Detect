# w0000 时钟停滞与 lockstep 生命周期机制的只读归因复核（2026-11-08）

## 复核结论

“45 s watchdog 是由位置时间戳无进展触发”成立；将其扩展成“**仅位置遥测停滞，而 PX4/Gazebo 全局钟在整个静默窗始终前进**”则过强。五份原始 MAVLink 日志都存在超过 50 s 的**全部入站原始 MAVLink 消息空窗**，且五份都在 watchdog 之后恢复了位置、姿态等消息。首次 GDB 前的 listener 还能反推 PX4 当前 lockstep hrt 时间仅比静默前位置钟高 4–20 ms。后续 IMU 与 Gazebo 探针前进是恢复阶段的证据，不能倒推此前 45 s 全程健康。

这些现象把关联集中到 **PX4 的 clock 分发/唤醒路径间歇阻塞**，与 lifetime-r1 修复的广播对象生命周期窗口具有较强的一致性；但尚未取得具体 cond/mutex 地址、对象失效过程及持有者，且首次恢复与 GDB 干预同处一个时间区间，因此仍是优先机制假设，**没有闭合为现场根因证明**。本报告不统计新的候选实景运行，不给出十次零卡顿或 21×2 已验收结论。

## 口径与实际探针顺序

仅只读 `artifacts/benchmarks/follow-track-fixed-gimbal-20261108-v2/` 下五份失败 capture、episode 元数据、`mavlink.jsonl` 与同目录两种 GDB 文本。主日志的源码 provenance 为 `3838612cde1dec6d427642a69c6f419152fb1faa`，dirty=true、changed_files=1；本复核查阅对应 `runs/src-followup2/scripts/px4_clock_stall_capture.py`，没有用当前启动框架的执行顺序替代旧现场。两份 retry-history 的 JSON 内 GDB 路径仍指旧 episodes 目录，本复核直接读取 capture 同目录的实际归档文本，没有改写原 JSON 或把悬空路径误判为缺栈。

以下每份独立取 `t0 = capture.px4_clock_diagnostics.last_progress_at_monotonic_s`，即 primitive 最后一次**观察到**位置钟进展的 host monotonic 时刻。最后原始位置消息的接收略早于 t0（8.5–96.7 ms），两者不得混用。表内 `+秒` 均相对该 t0。北京时间由 `episode.created_wall_time - created_monotonic_ns/1e9` 锚定，五份锚点一致到微秒量级；展示毫秒不代表探针有毫秒级同步精度。

源代码的真实顺序是：

```text
最后位置进展 → 45 s 无进展/失败快照
→ P1：procfs（样本标签 deadline）
→ work_queue、IMU listener 1、vehicle_status listener 1
→ PX4 GDB 1 → Gazebo GDB 1
→ gz topic info → Gazebo clock 1 → stats 1 → sleep(3 s)
→ P2：procfs（样本标签 after_3s）
→ work_queue、IMU listener 2、vehicle_status listener 2
→ PX4 GDB 2 → Gazebo GDB 2
→ gz topic info → Gazebo clock 2 → stats 2 → 捕获/清理结束 E
```

`deadline`/`after_3s` 是**混合收集容器的标签**，不是同一时刻的原子快照。`captured_wall_time` 取自 procfs 阶段，不能直接赋给稍后的 listener、GDB 或 clock 命令。P1→P2 实际为 15.494–25.490 s，不能当作精确 3 s 间隔。

GDB 命令没有单独记录开始/结束 monotonic 时间。因此下表只给源码可保证的保守区间：GDB1 位于 P1 与 P2 之间，GDB2 位于 P2 与 episode 结束 E 之间。Gazebo clock 消息的 `system` 是**发布者墙钟**，不是探针接收时间；它用于标注观测消息，不能擅自当作严格 GDB 结束上界。GDB 文件 mtime 是第二段追加后的写盘时间，也没有被当作首个 attach 时间。

## 五份位置/全部遥测空窗及恢复

`W` 为异常后的 `failure_state.captured_wall_time`，其在容器 inspect 前取得，接近实际 watchdog 触发；理论 deadline 为 t0+45。`L→R` 是原始位置相邻接收时间；全部 MAVLink 空窗另从 `mavpackettype` 原始消息流相邻接收计算。

| episode | t0 北京时间（11-08） | 最后位置钟 / L相对t0 | W | 首个恢复位置钟 / R | 位置空窗 | 全部原始 MAVLink 空窗 |
|---|---|---|---:|---|---:|---:|
| `f7cc935c` | 09:09:02.359 | 297.572 s / -0.069 s | +45.041 s | 297.592 s / +58.456 s | 58.525 s | 58.521 s |
| `10e0b0ed` | 09:41:35.996 | 399.708 s / -0.009 s | +45.003 s | 399.816 s / +50.581 s | 50.589 s | 50.588 s |
| `6d19f0b0` | 10:12:07.829 | 406.320 s / -0.074 s | +45.021 s | 406.344 s / +50.028 s | 50.102 s | 50.100 s |
| `74245a75` | 11:07:10.728 | 629.736 s / -0.026 s | +45.086 s | 629.760 s / +50.268 s | 50.295 s | 50.134 s |
| `51cf7021` | 11:44:42.493 | 466.764 s / -0.097 s | +45.001 s | 466.788 s / +50.520 s | 50.617 s | 50.615 s |

五份在共同可比的 `(t0+0.2, t0+44.9)` 窗内，所有 `receive_monotonic_ns` 消息计数均为 0。74245a75 在 t0+0.134 s 还收到最后一小批 ATTITUDE/SERVO，故不能将**从 t0 开始每一毫秒**都说成没有任何消息；其随后全部 MAVLink 空窗仍达 50.134 s。日志能证明接收记录中没有消息，单凭这一点不能区分 PX4 未发、传输未达、接收/记录线程暂停。

`clock_moved_again_after_45s=false` 是 primitive 抛出时保存的 `_SimClock` 快照。抛出后该 primitive 不再调用时钟观察器，而后台 MAVLink 记录继续，因此它**不是“直到 capture 结束都没有恢复”的证明**。[旧 A/B 报告](follow-w0000-stall-ab-results-20261108.md) 中“五次最后进展后都没有下一条位置消息”应撤回；原始文件确有上表恢复消息。失败判定仍有效：已经发生的 45 s 无进展不会被事后恢复抹去。

## GDB 与探针逐份对齐

B 表示 `pthread_cond_broadcast → … → px4_clock_settime → … → TriggerCallbacks/RecvMsgUpdate/RunReceptionTask`；M 表示同一 clock-settime 链上的 `pthread_mutex_lock`；P 表示 `poll → zmq_poll → RunReceptionTask`。

| episode | P1 | 首 PX4 GDB 状态及可保证区间 | P2 | 第二 PX4 GDB 状态及可保证区间 | E |
|---|---:|---|---:|---|---:|
| `f7cc935c` | +51.348 s | B，[51.348, 76.838] s | +76.838 s | P，[76.838, 127.224] s | +127.224 s |
| `10e0b0ed` | +48.637 s | B，[48.637, 65.598] s | +65.598 s | P，[65.598, 78.946] s | +78.946 s |
| `6d19f0b0` | +48.402 s | B，[48.402, 63.895] s | +63.895 s | P，[63.895, 75.773] s | +75.773 s |
| `74245a75` | +48.712 s | M，[48.712, 64.489] s | +64.489 s | B，[64.489, 77.616] s | +77.616 s |
| `51cf7021` | +48.617 s | B，[48.617, 65.286] s | +65.286 s | P，[65.286, 79.239] s | +79.239 s |

四份 B→P；74245a75 是 **M→B，并非后样本 poll**。五份首次位置恢复均落在首 GDB 的上述大区间内，且早于 P2，但这些区间还包括 listener、Gazebo GDB 和 topic probes，不能精确断言“detach 后第几毫秒恢复”，更不能仅凭时序相关断言 GDB 导致恢复。

74245a75 还存在第二次位置空窗：`+50.435 → +65.941 s`，15.506 s；全部原始 MAVLink 第二空窗为 15.460 s。其恢复再次落在第二 GDB 的大区间内，而第二 clock callback 又位于广播。它不支持“第一次恢复后持续健康”的单阶段叙述；也仍缺少无调试器的连续跟踪来区分重复阻塞与调试扰动。

### listener 读到了什么

精确 PX4 基底源码 `83c4f4e` 中，`platforms/common/uORB/uORB.cpp::orb_print_message_internal` 的 `now = hrt_absolute_time()`，打印 `age = now - topic_timestamp`；lockstep 的 `drv_hrt.cpp::hrt_absolute_time()` 读取 scheduler 的绝对模拟时间。因此可由 `timestamp + seconds_ago` 反推**当次 listener 执行时**的 PX4 hrt，而非仅知道某个旧消息 timestamp。这里首次两个 listener 都先于首次 GDB，且均晚于 watchdog。

| episode | IMU1 timestamp + age = PX4 hrt | 比最后位置钟高 | IMU2 timestamp + age = PX4 hrt | vehicle_status timestamp 1→2 |
|---|---|---:|---|---|
| `f7cc935c` | 297.588+0.004=297.592 s | 20 ms | 310.232+0.016=310.248 s | 297.100→309.840 s |
| `10e0b0ed` | 399.704+0.016=399.720 s | 12 ms | 412.436+0.000=412.436 s | 399.460→412.316 s |
| `6d19f0b0` | 406.300+0.024=406.324 s | 4 ms | 418.020+0.004=418.024 s | 405.848→418.052 s |
| `74245a75` | 629.724+0.032=629.756 s | 20 ms | 633.536+0.004=633.540 s | 629.440→633.492 s |
| `51cf7021` | 466.764+0.012=466.776 s | 12 ms | 476.880+0.004=476.884 s | 466.536→476.800 s |

这使“只有位置流停住，PX4 模拟钟一直正常跑”更难成立：在 45 s 无位置进展后、调试前的离散观测中，PX4 全局 hrt 仍接近静默前钟值。它仍不是 45 s 的连续采样，不能证明钟在每一瞬间都完全不动。IMU/status 的 5/5 前进描述的是 listener1→listener2 的**捕获阶段**，兼容“先阻塞、捕获中恢复”。

### Gazebo clock 读到了什么

| episode | clock1 模拟值 / 消息 system 相对t0 | clock2 模拟值 / 消息 system 相对t0 | 对整个静默窗的覆盖 |
|---|---|---|---|
| `f7cc935c` | 309.512 s / +69.775 s | 无有效消息 | 无连续覆盖 |
| `10e0b0ed` | 410.788 s / +58.848 s | 413.168 s / +73.572 s | 无连续覆盖 |
| `6d19f0b0` | 416.500 s / +56.575 s | 418.740 s / +71.571 s | 无连续覆盖 |
| `74245a75` | 638.784 s / +57.818 s | 640.568 s / +72.692 s | 无连续覆盖 |
| `51cf7021` | 475.568 s / +58.375 s | 477.548 s / +73.587 s | 无连续覆盖 |

“Gazebo 4/5 前进”只表示四份在这两次 **post-watchdog、post-GDB1** 观测之间的模拟值增加，不证明 t0→45 s 始终运行。f7cc935c 的首 Gazebo GDB 超时，第二段文本写明目标进程已是 zombie；虽命令元数据记 `status=ok`，不能据此认定第二次 attach 有有效栈或 Gazebo 正常。该份只有一条有效 clock 消息。`paused` 与静默窗内相机帧进展仍无充分观测。

## 与 lifetime-r1 的关联强度

除了函数名，针对已导出的原 gpu 镜像 PX4 ELF 做了只读 ASLR 归一化：ELF SHA256 `bcd023dfa6b77ee737b531891b0c64ee24bcf1ba2251eab979f6271712ef9cdc`。动态符号 `px4_clock_settime` 起点为 `0x338ab0`，五份栈中该函数返回点都匹配 ELF `0x338af7`；扣除各自页对齐加载偏移后：

- 四份首次 broadcast 的匿名调用返回点都为 `0x341201`；74245a75 第二次 broadcast 也相同。
- 74245a75 首次 mutex 匿名调用返回点为 `0x3411f8`。
- wrapper 在 `0x338af2` 调入 `0x3410e0`；该段反汇编在 `0x3411f3` 锁定每项 wakeup 的 caller mutex，`0x3411fc` 广播 condition，返回点依次为上述两个值。这与旧 scheduler 的同一广播循环相符，**mutex 样本更具体地落在 wakeup 的 passed_lock 获取点**，不是仅看到任意线程拿锁。

这将五份现场重复收敛到 lifetime-r1 所改变的 clock-settime 广播/退出同步路径。源码中 `_time_us` 在进入唤醒广播前就更新；若此处阻塞，首次 listener 读到“比最后位置钟多几毫秒、随后长时间无进展”的现象与该机制一致。组件级确定性 RED/GREEN 又证明旧实现确实允许已超时等待者先返回、候选补上该生命周期屏障；原样上游 10 ms 墙钟轮询的外部通知回归已被单独拒绝，候选保留 native cond_wait。组件证据详见 [候选构建记录](px4-lockstep-backport-20261108.md)。

但以下证据仍缺失，不能跳过：

1. **对象级链条**：旧 GDB 只有 `bt 16`，没有 cond/mutex 参数、寄存器、对象存活期或锁 owner；尚未证明现场确是退栈后失效/复用对象，也不能由单张 broadcast 栈排除合法竞争或宿主调度延迟。
2. **持续时间**：大部分第二栈已为 poll，原 capture 分类器的重复阻塞判据未达成；“不满足该判据”不排除首次现场候选，也不能反过来伪造持续 45 s 都在同一 libc 指令。
3. **观测干预**：GDB attach/detach 会停止/恢复线程并改变调度和条件变量时序；同时还执行 CLI listener、Gazebo GDB、transport discovery/topic probes。恢复可能自然发生、由调试/调度扰动促发，或包含缓冲消息突发，现记录无法分离。
4. **映像身份**：ELF 是本轮从原 gpu 镜像导出的匹配样本；旧五份未各自保存 PX4 ELF hash 和完整内存映射，地址匹配是强化路径定位，不能代替逐进程二进制/对象取证。
5. **反事实与外推**：这五份没有运行 lifetime-r1，当前文档没有新的同条件 old/new 实景对照；构建/组件测试通过，不等于 w0000 十次零卡顿或最终 21×2 验收通过。移动目标、世界偏移及压力仍可能影响触发概率，尚不能据此单独归因于某个 world 插件。

## 对旧结论的修订与复核记录

保留“触发器基于 LOCAL_POSITION_NED”“采样未证明 Gazebo 全局停住”“未证实固定 waypoint/碰撞触发”等有限结论；撤回“仅位置流停滞、其他钟全程正常”和“五份最后位置进展后均无下一条消息”的扩展。当前更合适的表述是：

> 五次出现约 50–59 s 的接收端位置空窗，全部 MAVLink 流也同时存在约 50–59 s 空窗；首次调试前离散 listener 的 PX4 hrt 仍近乎停在静默前值，clock callback 的现场栈位于 scheduler 的 passed_lock/condition 广播循环。捕获过程中遥测与时钟观测恢复，但受探针和 GDB 干预、缺失对象级锁证据限制，lifetime-r1 是有组件复现支撑的优先修复候选，尚不能宣称现场根因和实景验收已经闭环。

原始输入未修改。只读计算脚本与逐行数值/原始文件 SHA256 存于项目父目录 `runs/clock_attribution_readonly_20261108.py`、`runs/clock-attribution-20261108.json`；脚本对读取前后五类原始文件 hash 作一致性检查，并断言五份中央静默窗计数为 0、首次恢复晚于 45 s。只新增本说明，执行 `git diff --check`；没有重新跑测试、Docker、GPU、SITL、没有 attach 活进程、没有操作任何作业。

原始路径（共同前缀为 `artifacts/benchmarks/follow-track-fixed-gimbal-20261108-v2/follow-track-v2/`）：
- `f7cc935c`：`follow-w0000-fixed-camera/campaign/gen_mov_w0000/retry-history/episode-0000-f7cc935c-94489785/`。
- `10e0b0ed`：`follow-w0000-fixed-camera/campaign/gen_mov_w0000/episodes/episode-0000-10e0b0ed/`。
- `6d19f0b0`：`follow-w0000-fixed-camera/campaign/gen_mov_w0000/episodes/episode-0000-6d19f0b0/`。
- `74245a75`：`follow-w0000-gimbal/campaign/gen_mov_w0000/retry-history/episode-0000-74245a75-7246032d/`。
- `51cf7021`：`follow-w0000-gimbal/campaign/gen_mov_w0000/episodes/episode-0000-51cf7021/`。
