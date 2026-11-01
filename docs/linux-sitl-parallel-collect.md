# Linux 并行 SITL 采集失败分析

## 结论

这批 r21id-r4c 失败的首要原因是采集器的解锁确认时序，而不是 PX4 拒绝解锁。采集器把 `COMMAND_ACK(command=400, result=0)` 和带 armed 位的 `HEARTBEAT` 放进同一个 6 次、约 6 秒的重试窗口；在软件渲染和 6 worker 并发下，PX4 经常先接受 ARM，再过约 5.8--6.0 秒才在下一次心跳中反映 armed。采集器在这之前继续重复发送 ARM，窗口耗尽后报错。日志中的 `Preflight Fail: No connection to the GCS` 是启动期/GCS 门控文字，不能单独解释为 ARM 被拒绝；同一条记录随后出现 `Armed by external command`，且 ACK 结果为 accepted。

第二个问题是单架次的 2400 秒纯墙钟上限。14 次墙钟超时的 MAVLink 都已经出现 `COMMAND_ACK 400=0`、`Armed by external command` 和 `Takeoff detected`，说明架次已经解锁并起飞；失败发生在软件渲染锁步过慢，墙钟预算先耗尽。3 次着陆超时也有 `landed_state=4`，更符合落地状态已经产生但遥测确认晚到，而不是飞机没有落地。

## 产物统计

对 `artifacts/r21id-r4c/episodes/**/episode.json` 及同目录 MAVLink 流的只读统计得到 111 条尝试记录：

| 结果 | 次数 | 解释 |
| --- | ---: | --- |
| `complete` | 13 | 正常完成 |
| 解锁确认超时 | 78 | 采集器报 `PX4 did not confirm armed heartbeat after 6 bounded attempts` |
| `episode wall-clock timeout` | 14 | 已 ACK/起飞，但 2400 秒墙钟先到 |
| `timed out waiting for land` | 3 | 落地状态或解锁位确认晚于等待窗口 |
| 半截元数据 | 3 | 记录在进程/压缩流结束处不完整 |

用户汇报中的解锁失败数为 62；共享产物当前可读记录按错误文本归类为 78。两者可能对应不同截取时点或只统计了队列尾部，本文以当前产物为准，保留该差异以便复核。

在可读的解锁失败 MAVLink 中，`COMMAND_ACK` 对 `MAV_CMD_COMPONENT_ARM_DISARM (400)` 全部是 `MAV_RESULT_ACCEPTED (0)`，没有 `MAV_RESULT_DENIED (2)`。失败流常见的 `STATUSTEXT` 包括：

- `Preflight Fail: No connection to the GCS`；
- `no heading reference`；
- 少量启动期 `ekf2 missing data`、`Found 0 compass` 或传感器未就绪文字。

这些文字与 `SYS_STATUS` 的启动期健康变化相符。采集器已有预检等待 `xy_position_control` 等条件，不能把这些瞬时文字等同于最终 ARM 拒绝；判断最终结果应以 ACK、armed `HEARTBEAT.base_mode` 和后续飞行状态联合为准。

## 时序证据

失败样本的共同顺序是：

1. 采集器发送第 1 次 ARM，并以约 1 秒间隔重复发送，最多 6 次。
2. PX4 返回 `COMMAND_ACK(command=400, result=0)`，并发送 `Armed by external command`。
3. 约 5.8--6.0 秒后，新的 `HEARTBEAT` 才把 `base_mode` 的 `MAV_MODE_FLAG_SAFETY_ARMED (128)` 置上。
4. 旧采集器在第 6 次窗口结束时先抛出超时，因而把“PX4 已接受但心跳尚未到达”记录成解锁失败。

单 worker 冒烟日志也显示了相同的慢确认，只是当时在窗口内成功：`sitl-smoke-5.log` 在第 4 次发送后确认 armed，`r4-smoke-w0000.log` 在第 5 次发送后确认 armed。并发采集时调度和软件渲染抖动把确认推过了固定窗口。

## 世界、worker 和并发交叉检查

世界和 worker 计数按 episode provenance 中的 `--worker-index`、目录中的 world id 与 episode 状态交叉汇总；它是尝试数，不是 24 个世界的完整覆盖。可读状态如下：

| 世界 | complete | failed | 无状态/半截 |
| --- | ---: | ---: | ---: |
| `w0000` | 0 | 8 | 2 |
| `w0001`、`w0002`、`w0007`、`w0021`、`w0022` | 各 2 | 0 | 0 |
| `w0003`、`w0004` | 0 | 各 16 | 0 |
| `w0006` | 0 | 8 | 1 |
| `w0008` | 2 | 2 | 0 |
| `w0009`、`w0010`、`w0012`、`w0013`、`w0023` | 0 | 各 8 | 0 |
| `w0020` | 1 | 5 | 0 |

其余世界在这批产物中没有记录。混合结果在 `w0008`、`w0020` 最明确：同一世界既有 complete 也有 failed，说明世界配置不足以解释失败。worker 0--5 的状态分别为：`0: 3 complete / 20 failed / 1 半截`、`1: 0 / 16 / 1`、`2: 1 / 24 / 1`、`3: 2 / 8 / 0`、`4: 3 / 6 / 0`、`5: 4 / 21 / 0`。失败跨越全部 worker；worker 1 没有成功记录，其它 worker 则有成功和失败。

按 episode 元数据的创建/结束时间区间计算，峰值重叠为 **6 个架次**，和队列配置的 6 worker 一致；因此确认出现过满并发时段。现有记录不能证明某个特定墙钟时刻直接触发失败，但失败跨 worker 分布、以及满并发时存在多架次长时间运行，支持 CPU/渲染争用造成仿真进度与确认时序长尾的判断。队列命令使用独立端口：worker `n` 的 control/telemetry 端口为 `14560+10*n` / `14561+10*n`。失败 MAVLink 中看到的 PX4 autopilot/type 和 source system 一致，没有发现另一实例的心跳、跨端口 ACK 或消息源混入，因此没有证据支持端口/实例串扰是主因。

所有 r21id-r4 runtime 记录的多旋翼出生姿态为贴地 `z_up_m=0.2`、`roll=pitch=yaw=0`。这排除了旧 r20 经验中“空中斜着出生导致拒绝解锁”的问题。

## 对候选原因的判定

| 候选原因 | 判定 | 证据 |
| --- | --- | --- |
| 预检未过 | 不是主因 | ACK=accepted，无 DENIED；GCS/heading/EKF 文字多为启动期，后续仍有 armed/起飞 |
| 仿真慢、EKF/位置未收敛 | 是并发放大的背景因素 | 慢锁步使 ACK 到 armed 心跳、落地确认和整架次完成都延迟；少量 EKF/compass 文字只出现在启动期 |
| 出生姿态错误 | 排除 | 所有 runtime 出生姿态贴地、水平、零姿态 |
| 端口/实例串扰 | 未发现证据 | 端口按 worker 隔离，消息源一致，同 worker 有成功和失败 |
| 采集器自身时序 | 主因 | ACCEPTED 后仍重复 ARM，armed 心跳刚好越过固定 6 秒窗口 |

## 代码修复

`scripts/collect_sim_teacher_episodes.py` 做了两处小修：

1. `_arm()` 收到 `MAV_RESULT_ACCEPTED` 后停止重复发送 ARM，继续发送 GCS heartbeat 并等待新的 armed `HEARTBEAT`，直到该阶段 deadline。没有 accepted ACK 时仍保持原来的最多 6 次 bounded retry；没有放宽 DENIED，也没有强制解锁。
2. episode 主循环加入 `_EpisodeProgressWatchdog`。超过 nominal `max_episode_s` 后，只要 `MavlinkState.vehicle_time_boot_ms` 持续推进就继续运行；没有该字段时退回 MAVLink message count。只有遥测连续停滞 180 秒才报 `episode telemetry stalled`。这让墙钟预算随 PX4 lockstep 进度退让，而不是简单把 2400 秒改大。

没有修改 `run_simulator_docker.py` 的网络部分，也没有放宽预检条件。

## 并发建议

这台机器虽然有 96 线程和 404 GiB 内存，但本轮是 CPU 软件渲染、1280x960、segmentation，并且每个 worker 同时运行 PX4/Gazebo、相机和压缩写盘。6 worker 已经把锁步速度和遥测确认推入长尾，不能作为稳定采集并发数。

推荐先用 **2 worker** 重新跑一小组连续世界；确认没有解锁确认超时、墙钟超时和落地确认超时后，再逐步试 **3**，最多到 **4**。每次升档都应按连续多个世界的成功率和仿真/墙钟进度判断，不能只看单个 smoke。当前配置下不建议 6 worker。

## 验证和限制

- 已运行 `tests/test_sim_teacher_parallel_timing.py`：6 passed。
- 已运行 `tests/test_sim_teacher_live_loop.py -k 'not drains_real_udp_mavlink2_burst'`：13 passed；被排除的 UDP 测试受当前沙箱禁止创建 socket 影响。
- 已运行 `tests/test_sim_teacher_landing.py`：7 passed。
- `python -m py_compile scripts/collect_sim_teacher_episodes.py tests/test_sim_teacher_parallel_timing.py` 通过。
- `tests/test_round_collect_preflight.py` 前 6 项通过，但在 `test_deterministic_collector_config_failure_is_not_retried` 卡住，使用 20 秒外部超时终止；未把它改成通过。
- 未运行 `run_onboard_gate.py`，未启动 Docker、SITL、GPU 训练或网络下载。
- 共享压缩流有 3 条半截记录；结论只使用可完整解析的 ACK、STATUSTEXT、HEARTBEAT、SYS_STATUS 和 episode 元数据。

## 落地后仍解锁的收尾边界

多旋翼降落命令的目标是先确认 PX4 已进入 `landed_state=1`（`ON_GROUND`），再完成架次收尾。旧逻辑在收到这个状态后立即返回，即使 `HEARTBEAT` 仍显示 `armed=true`；它没有等待或保证解除锁定。`_EpisodeProgressWatchdog` 只在策略动作循环中检查，落地 helper 使用独立的下降期限，因此不能单凭看门狗推断落地 helper 会无限等待。episode 的整体超时可能因飞行遥测持续推进而延期，但每个等待阶段仍应由本阶段的目标和固定期限收束。持续的 `HEARTBEAT` 只说明遥测活跃，不能替代落地或解除锁定条件。

当前 Linux 多旋翼收尾按三个有界阶段处理：`ON_GROUND` 连续稳定 3 秒后，等待 PX4 自动上锁最多 30 秒；窗口内仍保持解锁时，采集器发送一次 `MAV_CMD_COMPONENT_ARM_DISARM`（参数 `1.0` 改为 `0.0`）并最多等待 10 秒确认新的未解锁 `HEARTBEAT`。两种结果都会结束架次：自动上锁记录 `disarm_source=px4_auto`，主动解除锁定记录 `disarm_source=collector`。若没有确认到未解锁心跳，则按明确的阶段超时失败。Windows 继续沿用收到 `ON_GROUND` 即完成的旧条件；固定翼/VTOL 路径也未改变。主动解除锁定仅用于一次性 SITL 采集的容器收尾。
