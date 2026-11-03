# nightly ext-1：Follow 遥测停滞与 Cessna 降落超时分析

分析日期：2026-11-03。对象为只读产物
`artifacts/benchmarks/nightly-linux-r21idr3-r24plan-ext-1/`，重点读取合并
`nightly-report.md`、两个任务的 campaign 日志、episode 元数据、步骤、MAVLink
以及保留的 PX4 ULog。所有修改均在当前工作树；没有修改共享 artifacts 或受证据约束文件，
没有启动 SITL、Docker、llama-server 或 GPU，也没有联网下载。

## 结论

| 对象 | 已确认的事实 | 分类与处理 |
| --- | --- | --- |
| `follow-red-person-return` | PX4 ULog 的多个连续 topic 一起终止于约 `392.84 s`；MAVLink 位姿最终为 `392864 ms`，之后不再推进；相机时间戳却继续到 `481.044 s` | **PX4／仿真桥接链路停滞，具体阻塞点未确定**。不能归因为整个 Gazebo 时钟冻结，也不能仅凭 PX4 cpuload 认定六 CPU 池饱和。保留 `telemetry_stall` 分类，不盲目扩大 CPU 配置或延长 episode 预算 |
| `rc-cessna-w0016-ambulance-known-limit` | 降落任务已接受并执行；约 `424.279 s` 墙钟只推进 `72.808 s` PX4 时间，仍在下降且持续发遥测 | **固定翼 landing state 使用墙钟预算的代码缺陷**，不是允许失败的 known-limit。修复内层 `land_state` 和外层确认的时钟域，保持 suite 的成功期望与 `land_timeout` 分类 |

主 suite 在同一配置下通过 14/14 是本次任务提供的运行背景；ext-1 合并报告本身只列
robustness、follow、airframes，不能把这个背景当成 Follow 或固定翼路径已受覆盖的证明。

## ULog 解析与复查方法

Follow 只有一个保留的 ULog：

```text
follow/follow-red-person-return/campaign/gen_mov_w0000/jobs/
md-sim-teacher-slot-0-episode-0000-0645c034/px4-ulog/
opt__px4-gazebo__log__2026-11-03__08_59_39.ulg
```

Cessna 同样有一个：

```text
airframes/rc-cessna-w0016-ambulance-known-limit/campaign/gen_v2_w0016/jobs/
md-sim-teacher-slot-1-episode-0000-12638c28/px4-ulog/
opt__px4-gazebo__log__2026-11-03__09_42_47.ulg
```

先运行 `scripts/analyze_px4_ulog.py` 分析 Follow，再用同一模块的 `parse_ulog` /
`render_markdown` 重新分析两个日志，报告与摘要仅写入工作树 `.tmp/`。本环境没有
`pyulog`，使用仓库 minimal reader。离线命令如下，输出位置不在 artifacts 内：

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
mkdir -p "$TMPDIR"
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python \
  scripts/analyze_px4_ulog.py \
  artifacts/benchmarks/nightly-linux-r21idr3-r24plan-ext-1/follow/follow-red-person-return \
  --gap-seconds 1.0 -o .tmp/follow-ulog.md
```

这次还发现并修复 analyzer 的两处真实格式错误：

- `L` 是 `uint8 log_level + uint64 timestamp + message`；`C` 是
  `uint8 log_level + uint16 tag + uint64 timestamp + message`。此前把 timestamp
  放在最前面解码，导致 console 时间和级别错误。
- `B` 是 Flag Bits，不能标记为 dropout；真正 dropout 是 `O`，载荷是毫秒时长。

修复后两个日志均无 parser warning，也无 `O` dropout 记录；`logger_status.dropouts`
末值也都是 0。不过 `logger_status.message_gaps` 不为 0，不能把它解释为完全无消息丢失。
新增真实 `L/C/B/O` fixture 测试，既断言字段值，也断言 Flag Bits 不制造假 dropout。

## Follow：什么停止了，什么仍在推进

### PX4 端证据

| Topic | Sample 数 | 最后 PX4 时间（s） |
| --- | ---: | ---: |
| `vehicle_attitude` | 88956 | 392.844 |
| `vehicle_local_position` | 45662 | 392.844 |
| `vehicle_global_position` | 45290 | 392.844 |
| `sensor_combined` | 89047 | 392.840 |
| `estimator_status` | 38630 | 392.844 |
| `vehicle_angular_velocity` | 38817 | 392.848 |
| `actuator_motors` | 3899 | 392.832 |
| `cpuload` | 779 | 392.500 |

高频 attitude/local position 的最大 **仿真时间间隔** 分别为 `0.036/0.040 s`。
这说明冻结前日志内连续，但不能用这个数值排除几百秒的墙钟停滞：PX4 clock 停止时，
ULog 也不能记录一个不断增大的仿真时间间隔。

末端 `vehicle_status` 为 armed（`arming_state=2`）、OFFBOARD（`nav_state=14`）、
`failsafe=0`；`estimator_status.filter_fault_flags=0`、`timeout_flags=0`；
`vehicle_local_position.xy_valid=1`、`dead_reckoning=0`、`z≈-7.827 m`。
未见冻结点附近 console 错误，最后一条 console 是 `16.228 s` 的 takeoff。
这支持“PX4 时间／内部采样整体停止”，而不是仅 Python collector 丢了一个 MAVLink topic。

### 相机与步骤证据：不是整个 Gazebo 都停止

`steps.jsonl` 尾部如下；frame stamp 取真实 `frame_sensor_stamp_ns / 1e9`，
pose boot 取 `pose_time_boot_ms / 1000`：

| Step | Before / After pose boot（s） | Before / After frame stamp（s） | 原语结果 |
| --- | --- | --- | --- |
| 115 | 378.084 → 385.872 | 378.084 → 385.872 | success |
| 116 | 385.972 → 392.864 | 385.972 → 422.600 | timeout |
| 117 | 392.864 → 392.864 | 422.732 → 451.444 | timeout |
| 118 | 392.864 → 392.864 | 451.576 → 481.044 | timeout |

因此 Gazebo 相机传感器仍提供时间戳更新的帧，PX4 却不再前进。比“Gazebo 整体冻结”
更准确的调查边界是 PX4、PX4/Gazebo bridge、IMU/lockstep 输入链路。
不能据此区分 PX4 线程死锁、桥接回调阻塞或调度饥饿，更不能证明 actor 已停止运动。

campaign 中 step 117、118 各经过约 `180 s` 墙钟，原语 `elapsed_s=0`；这与
sim clock 已停止、primitive 的墙钟停滞保护先后触发吻合。最终 episode guard 报
`vehicle_time_boot_ms=392844, message_count=142782, stall=364.1s`。
独立 `mavlink.jsonl` 最后一条位置为 `392864 ms`，接收墙钟单调时间
`122480.726190430 s`。这比 guard 快照多 `20 ms`，不改变停滞结论。
ULog 复制发生在 `123027.219493255 s`，距最后位置约 `546.5 s`；
`364.1 s` 是 guard 自身轮询进度口径，不是所有原始遥测的精确停滞时长。

共有 119 个步骤，全部仍在 `area_search`；尚未进入 follow/return_home。
不能把这些未完成的 follow 指标解释为已经执行跟随后控制器精度不达标。

### Moving actor 与 CPU 分配

此任务实际启动的是 `scripts/gz_mover_driver.py` 外部 Python driver，而不是日志已证明
出错的某个 native actor plugin。driver 独立订阅 `gz topic -e -t /clock`，按仿真时间
调用 `/world/default/set_pose`；配置周期为 `0.1 s`。代码确实会反复启动 `gz service`
子进程，增加调度与 Gazebo transport 开销；`--timeout 500` 只传给 gz CLI，
外层 `subprocess.run` 没有 Python timeout。这些是待验证风险，现有产物不能证明它们触发了本次停滞。

`episode.json.provenance.simulator_cpu_isolation` 给出的真实分配：

| 任务 | 总 CPU 池 | slot / slot_count | 容器 cpuset | cpu_shares |
| --- | --- | --- | --- | ---: |
| Follow | 84–95 | 0 / 2 | 84–89 | 4096 |
| Cessna w0016 | 84–95 | 1 / 2 | 90–95 | 4096 |

每个容器是 **6 个逻辑 CPU**；没有证据可把它们等同于 6 个物理核心。`cpu_shares`
是竞争权重，不是占用率，也不是单独的 CPU quota。两个 slot 的集合按配置互不重叠。

Follow 的 PX4 `cpuload.load`：末值 `0.9192`，中位数 `0.9471`，最大 `1.0560`，
最后 60 s 仿真时间均值 `0.9503`。Cessna 中位数 `0.9696`，最大 `1.0789`，
最后 60 s 均值 `0.9695`。这些是 PX4 topic 读数，甚至出现大于 1 的值，
**不能换算为 Docker 六 CPU 总利用率**，也不能据此断言整个 cpuset 已饱和。

产物没有保存 cgroup `cpu.stat`、Gazebo/PX4/driver 的逐线程 CPU、运行队列、
桥接线程栈或 driver 的远端 `/tmp/mover_driver.log`。容器错误尾部只有重复 `pxh>`
提示符，不能定位故障线程。故本次不修改 CPU 默认值或 Follow suite 以制造未经验证的“修复”。

后续若获准做 A/B，可先独占一个 slot，比较同一世界的 6 与 12 个逻辑 CPU；
12 CPU 的配置应同时设置 `MULTIDETECT_SITL_SLOTS=1`、`MULTIDETECT_SIM_CPUSET=auto`、
`MULTIDETECT_SIM_CPU_POOL=84-95`、`MULTIDETECT_SIM_CPUS_PER_SLOT=12`。
不能保持两个 slot、仅增大每 slot 配额而仍使用原来的 12 CPU 池；分配器会正确拒绝。
需要同时采集上述负载、bridge/IMU 流以及 mover log，才能判断是否需要按世界配置更大 cpuset。
本次未执行这个实验。

## Cessna：known-limit 的真实含义与降落时钟

### Suite 期望没有允许降落失败

`configs/onboard/suite-airframes-r21idr3-r24plan.json` 明确要求：
`skills=[area_search, return_home]`、`confirmed=true`、`min_safety_pass=true`，
任务预算 `900 s`、160 steps，返航距离阈值 `300 m`。没有 xfail、expected landing error
或允许 incomplete 的配置。`known-limit` 只是沿用的任务 ID，不能由名字推导测试语义。

`docs/fixed-wing-search-timeout-20261025.md` 最初把 known-limit 记录为固定翼斜视目标
确认不稳定，曾使用 `confirmed=false`；但同文后续 2026-10-25 复验已确认目标并正常返航，
把期望恢复为 `confirmed=true`，还记录后续 skills/confirmed/safety 全通过。
所以本次降落超时属于非预期失败；保留现有 `land_timeout` 分类，不修改期望来接受失败。
已有成功历史说明不是一直允许的限制，但没有同条件版本 A/B，不能定位到某个新提交造成的回归。

### 遥测证明 420 s 墙钟过早用尽

- `policy_stop` 在墙钟单调时间 `123611.346419333 s`，step 30。
- RTL 确认后，`rtl_home` 于 `123871.976550924 s` 返回 `near_home`，距离约 `92.25 m`。
- landing upload 的 `MISSION_ACK.type=0`；第一次切 Mission 暂不可用，随后成功。
  ULog 在 `161.588 s` 记录 `Executing Mission`，最终 `nav_state=3`（Mission），
  `mission_result.seq_current=2/seq_total=3`、`valid=1`、`failure=0`、`finished=0`。
- 接近进入 landing Mission 的位置样本为 `time_boot_ms=161588`、`z=-98.808 m`，
  接收墙钟 `123873.085802979 s`；最后一个位置样本为 `234396 ms`、`z=-14.088 m`，
  墙钟 `124297.364771023 s`。两者差值为 `72.808 s sim / 424.279 s wall`，有效速率约 `0.172`。
- 末端 HEARTBEAT、EXTENDED_SYS_STATE 仍在刷新，`landed_state=4`（landing）、
  armed；ULog 的 `failsafe=0`、estimator fault/timeout flags 均为 0，land detector 仍未触地。

因此这不是命令被拒绝，也不是与 Follow 相同的遥测冻结；采集器在飞机仍执行进近、下降时
按墙钟结束 `land_state`。日志没有最终 touchdown，不能声称只要多等就一定能落地。

### 修复范围

`FixedWingVtolExecutor.STATE_TIMEOUT_S['land']=420` 本身合理，但 executor 把它传给
collector 注入的 `_wait_fw_state`；该回调此前直接使用 `time.monotonic()`。
外层 `_fixed_wing_or_vtol_land_and_wait` 也按墙钟等待，而且当内层先返回失败时，
外层原有的 telemetry stale 检查根本没有机会执行。
`e4b7945d` 只修改了多旋翼 `_land_and_wait`，没有覆盖这两层固定翼／VTOL 路径。

本次在 collector 中新增 `_wait_airframe_landed`，供实际 `land_state` 回调和外层确认复用：

- Linux 以现有 `_SimClock` 跟踪 `LOCAL_POSITION_NED.time_boot_ms`，维持 420 s 的
  **仿真时间**预算；没有 PX4 timestamp 时回退墙钟。
- 仿真时钟连续 30 s 墙钟不推进即失败，即使仍能收到重复 HEARTBEAT；
  HEARTBEAT 已停更 30 s 也独立报错。
- 固定翼进近设独立 `3600 s` 墙钟硬上限，不照搬多旋翼 `600 s` 上限。
  在本次约 `0.172` 的速率下，完整 420 s sim 大约需要 2447 s wall，600 s 仍会过早截断。
  极慢但不断推进的仿真也受这一小时上限约束，不能无限等待。
- 只接受本次等待起点之后、且未过期的 touchdown 或 disarmed 遥测，拒绝缓存的起飞前状态。
- Windows 保留墙钟预算，外层继续把 command/state wait 耗时计入原有墙钟窗口；
  command ACK、VTOL transition、takeoff、RTL、upload 等等待均未迁移到新时钟域。
- timeout 诊断同时包含墙钟与预算耗时；时钟停滞／心跳停更消息含 telemetry，
  nightly classifier 会归入 `telemetry_stall`；正常仿真降落预算耗尽仍是 `land_timeout`。

不需要改 `fw_executor.py` 的命令映射或 420 数值，问题位于 collector 注入的等待实现。
另外，本次 mission summary 虽把 area_search/return_home 标为 succeeded，实际 `findings=[]`，
step 30 `confirmed=null`，没有目标确认正证据。降落修复后仍可能不满足 `confirmed=true`；
不能据此宣称 airframes 下一次必然 3/3，也没有降低目标确认期望。

## 与 nightly-8 分析的对比

参考 `docs/nightly-8-ulog-analysis-20261103.md`：

- nightly-8 的 EKF 启动、takeoff 墙钟预算、sensor stream 无帧问题分别发生在不同路径，
  不能拿 landing 修复覆盖它们。本次也没有修改这些等待。
- nightly-8 的 `monitor-backpacks` 缺失 ULog，只有“慢 lockstep 下 LAND 预算先耗尽”的假设；
  本次 Cessna 有保留 ULog 和完整 MAVLink，直接测出了墙钟/仿真时间差，预算缺陷证据更强。
- nightly-8 的 `patrol-fence-backpacks` 是相机无帧且 PX4 topic 仍更新；本次 Follow 是相机
  stamp 继续推进、PX4 topic 停止，方向相反，不能合并为同一种故障。
- 旧文的 `F+A` 格式修复仍有效，真实 topic 的末端时间和状态字段可继续参考；
  但旧 reader 的 console timestamp 顺序错误，因此旧文约 `25953.280 s` 的
  `Accel #0 fail: TIMEOUT!` 时间解释不能作为可靠证据。本次修复 reader，未手改旧分析文档，
  若需重审旧 console 时间必须重新解析原日志。

## 验证与未完成项

新增单元测试覆盖慢速 PX4 下超过旧 420/600 s 墙钟仍成功、固定翼与 VTOL executor
内外层集成、ACK-only 外层、真实 sim budget 到期、冻结时钟但活跃心跳、心跳停更、
极慢时钟的硬墙钟上限、无时间戳回退、Windows 内外层墙钟预算、过期 touchdown/disarm、
reader/command 失败，以及修复后的 ULog 二进制格式。nightly 分类用例验证新诊断保持准确分类。

测试环境使用主仓库 `.venv/bin/python`，设置 `PYTHONPATH=src:.`、`PYTHONUTF8=1`、
`TMPDIR=$PWD/.tmp`，pytest 单进程运行：

```bash
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python -m pytest \
  tests/test_sim_teacher_fw_landing.py tests/test_analyze_px4_ulog.py \
  tests/test_fixed_wing_landing_and_smoke.py tests/test_fw_executor.py \
  tests/test_sim_teacher_landing.py tests/test_run_nightly_regression.py \
  tests/test_suite_airframes_resolve.py tests/test_sim_cpu_isolation.py \
  tests/test_gz_mover_driver.py -q -p no:xdist -o addopts=''

/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python \
  scripts/run_onboard_gate.py -q -p no:warnings -p no:xdist -o addopts=''
```

结果：定点测试 **169 passed（8.64 s）**；CPU-only onboard gate **PASS**，
**1686 passed、15 skipped、22 deselected（141.49 s）**。
`git diff --check` 通过。测试日志和 ULog 分析中间结果保存在工作树 `.tmp/`，不属于提交文件。

未进行真实 SITL 重跑、Docker 操作或资源负载实验。Follow 的 PX4/bridge 具体阻塞点、
增加 CPU 是否有效、Cessna 完整 touchdown 以及目标确认能力都仍需后续真实运行验证。
没有修改 suite 期望、CPU 分配默认值、历史产物报告或受证据约束文件；没有提交。
