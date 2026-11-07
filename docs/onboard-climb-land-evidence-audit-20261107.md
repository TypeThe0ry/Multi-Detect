# 爬升保护 SITL 结果审计与 LAND 证据边界（2026-11-07）

## 结论

v2 作业于 2026-11-07 23:04（Asia/Singapore）退出码 1。失败发生在 onboard mission loop 开始前：collector 等待 `sim_pre_abort_descent_reached` 45 秒超时。Episode 标记 `failed`、`step_count=0`，没有 `steps.jsonl`，没有 PX4 ULog，mission log 只有 admission/final-summary 记录。故本次既没有两次合格的低空 `return_home up` timeout，也没有第二次失败命中、第三次 climb 判定或保护链的 `land_now` handoff。live gap 保留；不得将这次启动/失败后的 PX4 遥测冒充保护触发证据。

本次 MAVLink 最后可见状态是 `EXTENDED_SYS_STATE.landed_state=2`（airborne），heartbeat `base_mode=145`（armed bit 仍置位）；没有 fresh landed state 1 加 disarm 证据。因此“最终 LAND 已执行”不通过。失败后 collector 进入 teardown 并删除 disposable container；这只是 collector teardown，不能证明 PX4 已着陆或已 disarm。

## 输入与哈希

以下产物均只读，审计期间没有修改 `artifacts/`。

| 输入 | SHA-256 |
| --- | --- |
| `/data/projects/Multi-Detect-env/runs/sitl_climb_fault2.out` | `e62812267e2978882fe57e3124947d41350b8d9f6bd19ff65db42b802abba5da` |
| `artifacts/benchmarks/onboard-climb-fault-sitl2-20261107T142627Z-3437231/suite-report.json` | `d3002da097114f68d121f43054247aad10701bf5fe9a7f9ead7fb59502e5d2a4` |
| `artifacts/benchmarks/onboard-climb-fault-sitl2-20261107T142627Z-3437231/suite-report.md` | `c04667cadf447b60413b2e3c10fb969f6fa9f02a9973edc19ea6c7aa476c9372` |
| `.../operator-abort/report.json` | `9c2ec4be390413a598f9f73e78582d118458c2a6aa59e5342e2a6fbecf925339` |
| `.../operator-abort/onboard.json` | `d58e81f4a2ffa7af8a36c9d4bc5d10609fee9b9ad0c7cf892d43fb50c7efc068` |
| `.../campaign/gen_cnt2_w0003.log` | `af6d0b0f8eca0fdda36c268a03aa43cef2f23b3722031ad77ff817f393d08849` |
| `.../episodes/episode-0000-39ce1a1b/episode.json` | `9b7bea01f4aa2576683f33a3b2f2ec336fd7192b3a3fe8e3448a83a64fe8358e` |
| `.../episodes/episode-0000-39ce1a1b/mission_summary.json` | `93253305c2a924418233b9d6b229f80bc6a42d5ed891598208dbc681722e28ce` |
| `.../episodes/episode-0000-39ce1a1b/mavlink.jsonl` | `f0d1951a769542cd7ab26b6715fb1e436b5f7097070d0a69db97b511a1926cd8` |
| `.../episodes/episode-0000-39ce1a1b/host_pressure.jsonl` | `37296a592c21c3354d3473d290ee5eb3d333a4ff1abed87f376bbe70ee58c528` |
| `.../episode-0000-39ce1a1b-onboard-frames/mission_log/mission.jsonl` | `c6f5b50960e2bc84633d3bcb55286e68c4c0dd048877d9b5125f22496d7fde5b` |
| `.../episode-0000-39ce1a1b-onboard-frames/mission_log/summary.json` | `f87cddb524e55019ee5b98c104d10dd4d1a776e93dc2f102177c997cac7eb929` |
| `docs/onboard-climb-failure-20261107.md` | `235216b2ad2d2451ece6c70970ac0037fc68350bdaabbcd4f8dfbb6947ca6532` |
| `docs/onboard-climb-land-followup-20261107.md` | `f07c3848c06a1254d0993ec6dfbd6a1f28672aad3e4a6cfeb88b949f7d7c92b4` |
| `docs/accel-timeout-crash-20261107.md` | `9d319c9d347da401422e46f0f66a370998c2a5f518f49bd1f4fed141f0306b5e` |
| `docs/identity-r10-train-job-20261107.md` | `c569694f35355d3fdf37ec2b87ca4f62eae0480eae907a4b49395de0160f4eec` |
| `scripts/run_onboard_fault_campaign.py` | `be47e05e73426ec8456f04680f726acc5c31adfaaec2887aacc53523a4628af5` |
| `scripts/onboard_policy.py` | `971934d916e11f756dee92ccdd9ee7d9684c460a1a4c1501a171577914235bdf` |
| `src/multidetect/autonomy/mission_executor.py` | `47336568cad362f978099037e4990ef87901d88b15040b48592174da23c492d2` |
| `src/multidetect/autonomy/mission_supervisor.py` | `df60048fafcda6725997abcab2f6f60315b000f3321c3ec0cafeff29ea938365` |
| `tests/test_mission_executor_remaining_gaps.py` | `3cc9334ad689495a604053157a6b4e9a64bffd78ae1d583e24dcfd668a26e91e` |
| `tests/test_mission_supervisor.py` | `ad418aa4a0b0ef6e3fc01c2c717a5a72ea848ae52ce71e6333b3b1210b0282ba` |
| `tests/test_onboard_fault_campaign.py` | `a54dc06b4f7a9a8214bac64f6ae5d5d627b9e8d034a33ab66fee46b5ce224e12` |

episode 目录没有 `steps.jsonl`，也没有 ULog 文件。MAVLink 没有 ULog 可交叉验证；本报告不以没有记录推断 PX4 内部事件。

## 退出原因

`episode.json` 的 takeoff 目标高度是 18.804595 m，来源 `auto_max_obstacle_top_plus_4m`。collector 在 takeoff 达成后开始 SIM 专用 pre-abort descent，目标 1.5 m；45 秒内未观测到不高于 1.75 m，于 `sim_pre_abort_descent_reached` 抛出 `TimeoutError`。作业 campaign log 还记录了 host load average `[65.44, 94.41, 120.12]`、PX4 进程仍存活、ULog `not_found_or_unavailable`。这些是失败背景，不足以单独认定负载就是下降超时的唯一成因。

根套件随后报告 `collector_exit=1`、`episodes=0`、suite 的 episode 状态 `missing`，预期通过率 0。episode 自身 `status=failed`、`step_count=0`。这不是一次 collector 正常完成的 `complete`；即便是 `complete`，也只代表数据采集完成，不能证明 landing。

## PX4 时间线与可观察字段

由于没有 primitive 记录，以下只列可从 MAVLink 还原的启动/失败时间线。PX4 `time_boot_ms` 的 LOCAL_POSITION_NED 从 3,992 ms（z=-0.019 m）推进到最后 37,712 ms（z=-3.405 m），证明 PX4 时钟推进。它并非 climb primitive 时间线；z 是 NED 向下坐标，末值约对应起飞点上方 3.4 m，仍高于 injector 的 2.5 m 触发上限。

| 事件 | 观测 | 审计解释 |
| --- | --- | --- |
| SITL 初始状态 | HEARTBEAT + `EXTENDED_SYS_STATE.landed_state=1` | 初始在地面；不是保护链触发 |
| Arm | COMMAND_ACK `command=400`, `result=0` | 解锁命令 ACK；不是 LAND ACK |
| 起飞 | LOCAL_POSITION_NED 时间从 3,992 ms 推进；之后 `landed_state` 变为 2 | 正常起飞遥测；无 mission step |
| pre-abort descent | campaign log 记目标 1.5 m；等待 45 s 后超时 | 没有到达 <=1.75 m 前置条件，正式 onboard mission loop 未开始 |
| 第一次/第二次合格 up timeout | 无 step 文件，零条 primitive outcome | 不可观察；不能称作两次 timeout |
| 第二次失败命中 | 无 mission event/alert | 未发生/无证据 |
| 第三次 up | 无 step 文件 | 不能声称“保护链阻止第三次 up”；只可说未进入生成 climb 的 mission loop |
| `land_now` handoff | mission log 无 escalation；无相关 host 请求 | 未观察到 |
| 最后 up setpoint | 无 onboard up primitive；MAVLink 中持续 POSITION_TARGET/stream telemetry 不能映射为该保护链的 up primitive | 无法判定保护链停止 setpoint 的时延 |
| COMMAND_ACK | 六条：命令 211 三条、400 一条、176 两条；无可关联 `land_now` 请求的 ACK | 命令 400 是 arm；176 是 mode 命令 ACK，但没有保护链 LAND 请求作为对应依据。ACK 不等于 landed |
| Heartbeat/current mode | custom_mode 从 `50593792` 变为 `393216`；末端 `base_mode=145` | 原样报告值。记录中未见可归因于保护链的 LAND mode handoff；base_mode 145 的 armed 位仍置位 |
| landed state | 早期为 1；起飞后为 2；最后可见仍为 2 | 末端 airborne，非 landed |
| disarm | 最后 heartbeat armed bit 置位；没有 disarm ACK/落地后状态 | 无 disarm 证据 |
| collector teardown | campaign log `cleanup removed=true` | 删除 disposable 容器，不代表着陆或 disarm |

MAVLink 解码器记录 `HEARTBEAT` 的 `base_mode=145`、`custom_mode=393216`，最后一条 heartbeat 与 landed state 2 时间接近。两条 command 176 ACK 和 custom mode 切换都发生在 campaign log 的 `sim_pre_abort_descent_started` 之前，属于前置起飞阶段。episode 没有显示它们对应保护链 `land_now` 命令，也没有命令关联字段可建立该因果关系。故此处不把它们判为保护链 LAND ACK，也不把 custom mode 数值单独翻译成“保护链已进入 LAND”。

## Live 与 deterministic fixture 分界

此次 v2 live 运行只到 pre-abort descent 前置阶段，没有开始 abort-driven `return_home` recovery。输入配置确实有 `sim_climb_timeout_injection=true` 和 abort at `t_s=0`，但注入器仍要求低于 2.5 m 的明确 airborne 样本及纯上升 primitive；配置存在不表示注入已触发。

此前 fixture/test 的证据仍独立成立，但只证明代码路径：正式 `MissionExecutor`、`MissionSupervisor`、`OnboardPolicy.report_primitive_outcome` 在合成 PX4 样本上把第二次合格失败转为失败终态、alerts 和 supervisor `land_now` host handoff；fixture 的 PX4 ACK、mode、landed state 均为 null。确定性 fixture 也明确将 host `stop`/`land_now` 请求与 PX4 执行分开。

负例由现有 `tests/test_mission_executor_remaining_gaps.py` 及 `tests/test_onboard_fault_campaign.py` 覆盖：一次 timeout 后有进展恢复/计数清零；实际增益恰好 0.5 m 清零；重复、相同、倒退、缺失、非法 boot timestamp 不连续计数；landing、hold、land、已落地、未知 airborne、高空、水平/yaw/下降动作、非 `return_home` 不计数。第二次失败后的 fixture 检查没有第三个相同 climb primitive；mutant 测试会拒绝缺失 policy outcome feedback 的实现。它们不是本次 live v2 的触发结果。

## LAND 证据等级

| 证据层级 | 本次结果 | 判定 |
| --- | --- | --- |
| 宿主收到 `land_now` 请求 | 无 | 未观察 |
| 与请求对应的 PX4 COMMAND_ACK | 无 | 未观察；其余命令 ACK 不可替代 |
| PX4 heartbeat mode 确认 LAND | 无可归因于 handoff 的确认；末端 raw mode 为 custom_mode 393216 | 未验证 |
| fresh `EXTENDED_SYS_STATE.landed_state=1` | 最后状态 2 | 未通过 |
| disarm | 最后 heartbeat 仍 armed；无 disarm 证据 | 未通过 |
| collector teardown | `cleanup removed=true` | 仅 teardown，不属于 PX4 LAND 证据 |
| 最终 PX4 landed | 必须同时有 fresh landed_state=1 和 disarm；两项均缺 | **未通过，live gap 保留** |

`action_ack` 代表位置 setpoint 执行器对动作记录的确认，不能冒充 MAVLink COMMAND_ACK；本次没有 `steps.jsonl`，因而也没有可误读的 primitive `action_ack`。既往 operator-abort 正常收尾或旧摘要不可移植为 v2 的保护链结果。

## 仍允许重复爬升

代码层确定性证据表明：在 outcome hook 收到两次相邻、低空、airborne、纯 `return_home up` timeout 且每次增益小于 0.5 m 后，executor 进入失败终态，supervisor 请求 `land_now`，后续没有第三次相同 climb primitive。一次 timeout 后恢复、增益达到阈值及无效/不连续样本仍按既有规则不触发保护。

但 v2 live 没有进入上述前置条件，不能用它确认现场链路已停止重复爬升。**当前结论：确定性 fixture 所覆盖的 onboard outcome 链在第二次合格失败后不允许再请求相同 climb；live v2 未触发，因而对该 SITL 运行没有重复爬升行为的实证结论，live gap 仍开着。** 不得为了制造触发重跑同一作业。

## 验证与范围

运行环境为仓库 `.venv`，设置 `PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp`，单进程：

```text
python -m pytest tests/test_mission_executor_remaining_gaps.py \
  tests/test_mission_supervisor.py tests/test_onboard_fault_campaign.py \
  -q -p no:xdist -o addopts=''
190 passed in 55.14s

python scripts/run_onboard_gate.py -q -p no:warnings
onboard gate: PASS (115s)
```

未启动新 SITL、Docker、GPU、模型服务、训练、release-parity、HIL 或真实飞行；未重跑 v2；未改运行时安全语义、阈值、vehicle-loss 规则或已有断言；未写入共享 `artifacts/`。本次只新增此审计文档。

## 建议的下一个任务

### 1. 修复低空 pre-abort descent 的 SITL 前置条件

目标：解释 v2 collector 在 `sim_pre_abort_descent_reached` 45 秒等待中为何未达到 1.75 m，修正低空场景编排，并通过配置/collector 测试证明 abort 前会取得 <=2.5 m 且明确 airborne 的 PX4 样本。证据路径：`docs/onboard-climb-land-evidence-audit-20261107.md`、`docs/onboard-climb-land-followup-20261107.md`、`scripts/collect_sim_teacher_episodes.py`、`tests/test_onboard_fault_campaign.py`、只读产物 `artifacts/benchmarks/onboard-climb-fault-sitl2-20261107T142627Z-3437231/`。不得修改 LAND 优先级、2 次/0.5 m/2.5 m 阈值、vehicle-loss 规则、任务 expected 或已有断言，不写共享 artifacts，不为验证而启动 SITL；写入诊断与测试、文档 `docs/onboard-climb-preabort-descent-followup-20261107.md`。后续 SITL 必须单独获批并最多执行一次。

### 2. 增强未进入 mission loop 的失败报告

目标：让 suite 对 `episode status=failed`、collector timeout、缺失 steps/ULog、cleanup 状态及 host/SITL 遥测末态明确报失败阶段，禁止 missing episode 报告掩盖已有 failed episode 诊断；保留任务期待和原断言。证据路径：`scripts/eval_onboard_loop_campaign.py`、`scripts/run_mission_suite.py`、`scripts/collect_sim_teacher_episodes.py`、`tests/test_onboard_fault_campaign.py` 及本次 v2 的只读 `suite-report.json`、`episode.json`、`gen_cnt2_w0003.log`。不得改安全运行时语义、阈值、vehicle-loss 规则或共享 artifacts；增加有针对性的 CPU-only 报告测试，文档写入 `docs/onboard-climb-sitl-failure-reporting-20261107.md`。

## v3 修复与作业（2026-11-08）

### v2 的下降实际发生了什么

原始证据 `artifacts/benchmarks/onboard-climb-fault-sitl2-20261107T142627Z-3437231/operator-abort/campaign/gen_cnt2_w0003/episodes/episode-0000-39ce1a1b/mavlink.jsonl` 显示，collector 发出了目标 `POSITION_TARGET_LOCAL_NED.z=-1.5`、`vz=1.5` 的持续 setpoint；这不是没有发下降命令。PX4 `LOCAL_POSITION_NED` 高度从约 18.8 m 下降到末端 3.405 m，末端仍为 `EXTENDED_SYS_STATE.landed_state=2`，heartbeat 为 `base_mode=145`、`custom_mode=393216`，仍在空中并保持 armed。PX4 还持续发送同一低空目标 setpoint，未见 LAND handoff 的可归因 ACK 或模式确认；`STATUSTEXT` 主要是重复的 `ODOMETRY: estimator_type 8 unsupported`。

campaign log 记录 `sim_pre_abort_descent_started` 的宿主单调时间为 `91209.046`，超时记录为 `91255.535`，所以原来的 45 秒预算按 wall time 消耗（约 46.5 s，包含轮询/收尾）。同一 MAVLink 文件的 PX4 `time_boot_ms` 从 3,992 ms 推进到 37,712 ms；对应接收 wall 时间从约 1,794,063,730.5 s 到 1,794,063,888.5 s，整体 RTF 约 0.21，下降窗口的可用仿真进度远小于 wall 秒数。故 45 s wall budget 在 RTF 约 0.1--0.2 时只给了约 4.5--9 s 仿真时间，车辆尚未到达 1.75 m 就退出；`step_count=0` 是这个前置等待失败的结果，不是保护链执行后的结论。

### 修复

`scripts/collect_sim_teacher_episodes.py` 的 SIM/onboard 注入前置下降现在使用 PX4 `LOCAL_POSITION_NED.time_boot_ms` 的锁步时钟计 45 s；同时保留 900 s 独立 wall ceiling，PX4 时钟冻结或遥测停止时仍会有界失败。该等待只由已有 `sim_pre_abort_descent_altitude_m` 配置门控，不改变 LAND 优先级、0.5 m 增益阈值、2.5 m 低空阈值、vehicle-loss 规则、takeoff/LAND 其他预算或既有断言。`tests/test_sim_teacher_parallel_timing.py` 新增慢 RTF 成功和 PX4 时钟冻结 wall ceiling 两个测试。

新增单槽位作业源码 `.tmp/jobs/NN-climb-fault-sitl3.sh`。作业使用 `/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python`，从 `suite-main-r21idr3-r24plan.json` 只选择 `operator-abort`，设置 `sim_ground_events=[{"t_s":0,"event":"abort"}]`、`sim_climb_timeout_injection=true`、`sim_pre_abort_descent_altitude_m=1.5`，并将输出写入新的 `artifacts/benchmarks/onboard-climb-fault-sitl3-<UTC>-<PID>/`。作业未在本次修改中启动；共享 `artifacts/` 未写入。
