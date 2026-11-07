# 重复爬升失败后的 onboard LAND 升级复核（2026-11-07）

## 结论与证据边界

离线确定性验证证明：低空 airborne 的 `return_home` 连续两次纯上升 primitive timeout、每次实际高度增益均小于 0.5 m 后，失败执行器进入 `climb_failed_suspected_loss_of_control`，不会再请求第三个 climb primitive。生产 policy 回灌会通过现有 supervisor `_escalate` 请求并锁存 `land_now`，保留失败执行器证据；公开 `OnboardPolicy.act()` 随后输出 `stop` 交给宿主执行 LAND。计数阈值仍为 2、0.5 m、2.5 m，supervisor 优先级未改变。

**本 lane 未启动 Docker、SITL、HIL、真实飞行或模型服务，没有 PX4 LAND ack、LAND 模式或最终 landed state 的新增 live 证据。** 调度方追加的沙箱限制覆盖了原任务中的 live 执行要求；已经准备一个有界 SITL 作业，尚待调度。不能把 fixture 中的 `stop/land_now`、历史 `action_ack=true` 或未来收到的 COMMAND_ACK 解释为已落地。

通用 source-level fault campaign 的整体验收仍失败；新增爬升 fixture 单独通过。因而本报告不能作为“全 onboard fault campaign 已通过”或“PX4 LAND 最终执行已验证”的签署。

## 实际 outcome 回灌路径

指定的 `src/multidetect/onboard_policy.py` 在本工作树不存在；实际适配层是 `scripts/onboard_policy.py`。

1. `scripts/collect_sim_teacher_episodes.py` 等待 primitive 执行结束，再采集对齐的 `obs_after`，调用 `policy.report_primitive_outcome(step_action, result.status, before, after)`。使用实际执行结果及前后 PX4 NED 样本，不读取累计目标差作为实际高度增益。
2. `OnboardPolicy.report_primitive_outcome` 委托 supervisor；无 supervisor 的兼容路径仍委托 executor。
3. `MissionSupervisor.report_primitive_outcome` 在 terminal recovery 活跃时选择 `_recovery`，否则选择主 executor。命中后记录事件、alert 和失败快照，通过已有 `_escalate("land_now", reason)` 升级，立即同步 action 并写失败摘要。
4. 若宿主再次调用 policy，supervisor 从失败的 `return_home` 切换为 `land` recovery，第一次输出 `stop`，后续只能是着陆等待类动作，无新 up。
5. collector 的既有分支在命中后记录当前 step、退出 primitive 循环，正常路径进入 `_land_and_wait`。本次没有改 collector，也没有改其 vehicle-loss 规则。vehicle-loss 检查仍在退出判断之前运行，可能以独立失控异常先结束 episode，不能假定每次都到正常 LAND 收尾。

另一条原生 `src/multidetect/onboard_runtime/runtime.py` 路径没有调用该 outcome hook；它在 action timeout 时使用自己的 watchdog/handoff。其通用 campaign 不能用作“两次 PX4 样本 timeout 回灌”的 live 证明。本次没有扩展该运行时的结果语义。

## 状态机与计数条件

`return_home / count=0 → 第一次合格 timeout / count=1 → 第二次合格 timeout / failed → supervisor land_now → 宿主 LAND 请求边界`。

每次计数必须同时满足：

- 活跃步骤为 `return_home`；primitive 为 `translate`、`up_m>0`，且 forward、right、yaw delta 均为零。
- outcome 严格为 `timeout`；before/after NED z 和 PX4 boot time 都为有限数值，排除 bool、缺失和 NaN。
- `0 <= before_boot < after_boot`；后一 primitive 的 before 不早于前一次合格 primitive 的 after，拒绝重复、重叠、倒退的 PX4 样本。
- before 与 after 均不超过 2.5 m，且均有 airborne 证据；显式已落地或 PX4 landing/takeoff 等状态优先于可能陈旧的 `landed=False`。未知状态不能确认 airborne。
- `before_z - after_z < 0.5 m`。达到 0.5 m 即清零；不使用 host wall time 或伪增长的 `achieved_delta_ned`。

不合格 outcome 会清空连续计数与健康窗口。最近两次 after 的姿态、SYS_STATUS present/health 异常连同 PX4 时间、实际 gain、height 一起保留；连续两次无增益本身就是触发依据，不要求伪造姿态或传感器异常。首次 after 的健康异常也会保存在最终事件中。

## 确定性 fixture 时间线

`scripts/run_onboard_fault_campaign.py::run_climb_failure_fixture` 复用正式 MissionExecutor、MissionSupervisor、OnboardPolicy outcome 适配和 OnboardMissionLog。只在观测/primitive completion 输入边界提供静止的低空样本，不运行 FCU，也不替换失效判定或升级方法。

| PX4 fixture 时间 | 输入或输出 | 可证明内容 |
| --- | --- | --- |
| 1.000 s | abort 已锁存，输出 hold | 沿用现有返航前 brake |
| 2.000→22.000 s | `up 15`，timeout，0.8 m→0.8 m | 第一次失败，未升级 |
| 22.000→42.000 s | 第二次相同 `up 15`，timeout，gain=0 | executor 终态 failed；事件与 alert 出现 |
| 第二次回灌返回前 | `land_now` escalation、失败摘要 | failure class 与 reason 一致，LAND 执行标记 unverified |
| 42.000 s | `stop`，`supervisor_action=land_now` | LAND 宿主请求边界；无第三个 climb |
| 43.000 s | `continue`，仍为 land_now | 着陆等待，没有 up；不能解释为已落地 |

fixture 的 `px4_ack`、`px4_mode`、`px4_landed_state` 均明确为 null。这里统计的是 climb primitive 请求，不是实际 MAVLink 高频 setpoint 流；真实流是否及时停止、LAND mode 是否确认、何时 landed 仍须 live 检查。

14 项检查包含：第二次才命中、恰好两次相同 climb、LAND handoff、无第三个 climb、失败执行器终态、mission event、alerts、持久化 event/alert/escalation、最终失败类别、PX4 执行未验证标记，以及摘要只修订一次。关闭 policy outcome 回灌的 mutant 必须被拒绝。公开 policy `act()` 测试另外覆盖完整构造与 safety envelope，FAST/SLOW stub 一旦被调用就失败。

## 最终摘要与审查证据

原 supervisor 在第一次 `return_now` 时已经写入 `ended_early` 摘要。原实现之后即使升级 LAND，也不会更新摘要，且切换 land recovery 会覆盖失败的 `_recovery`。

本次保留历史摘要，在爬升失败时追加一次新的 `final_summary`，同时原子更新 `summary.json`。最新摘要包含：`status=recovery_failed`、`action=terminal_action=land_now`、`terminal_reason=failure_class=climb_failed_suspected_loss_of_control`、失败的 `recovery_history`、alerts、两个样本以及 `land_execution=unverified`。普通 early-return 摘要仍维持原有单条行为；重复 finish 不再修订失败摘要。

失败 recovery 的 mission event 与 alerts 也复制到主任务，避免 teardown 摘要和 report 丢失告警。`tests/test_compare_nightly_arms.py` 用正式 fixture 生成的 supervisor 日志测试现有 failure-class 识别器，输入布局为明确的合成 episode；这证明生成事件能被识别，不证明 live collector 的每一种落盘布局都会自动归类。独立的 collector `episode.json.status=complete` 表示采集收尾，不能替代 supervisor 的任务失败状态。

## 只读历史回放

输入：`artifacts/benchmarks/nightly-linux-r21idr3-r24plan-28-head/main/operator-abort/campaign/gen_cnt2_w0003/episodes/episode-0000-06a1a47d/steps.jsonl`。

SHA-256：`38c7de8b3d8a7e2bbc996f0400741033ae2bf084de0fdaa884869c447ba2dedd`。未写入、移动或删除共享 artifacts。

| step | PX4 before→after（ms） | 实际 gain（m） | 结果 |
| --- | --- | ---: | --- |
| 39 | 189488→210544 | 2.343740 | 有进展，清零 |
| 40 | 211236→232420 | 0.043755 | 第一次合格 timeout |
| 41 | 232620→252880 | 0.064410 | 第二次，命中失控疑似终态 |

因此保留原 step 41 命中。step 38 的坠落、之后的 27 次重复 up 是历史故障证据；这次回放没有重新执行它们，也没有生成新的 PX4 LAND 证据。

## 验证记录

所有 Python 运行均使用 `PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp`，单进程；未联网下载。

```bash
PY=/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
"$PY" -m pytest tests/test_mission_executor_remaining_gaps.py \
  tests/test_mission_supervisor.py tests/test_onboard_policy_package.py \
  tests/test_onboard_fault_campaign.py tests/test_compare_nightly_arms.py \
  -q -p no:xdist -o addopts=''
"$PY" scripts/run_onboard_fault_campaign.py \
  --work-dir .tmp/climb-fault-campaign-final --output .tmp/climb-fault-campaign-final/report.json
"$PY" scripts/run_onboard_gate.py -q -p no:warnings
bash -n .tmp/jobs/39-onboard-climb-land.sh
bash .tmp/jobs/39-onboard-climb-land.sh --prepare-only
```

- 相关单测：**234 passed，54.20 s**，日志 `.tmp/climb-related-verified.log`。进一步补强旧 fixture 的 before airborne 样本及实际计数断言后，executor 文件重跑 **64 passed，1.36 s**，日志 `.tmp/climb-negative-final.log`。此前 233 passed、1 failed 的失败是 campaign 异步日志半行读取；已用写入锁保护 fixture 快照读取，没有删除或放宽断言。
- 单次 timeout 后恢复、timeout 但 gain 达标（含恰好 0.5 m）、慢仿真时间爬升、非 return_home、纯度不符、landing/hold、已落地、高空、未知 airborne、boot 不推进/倒退/重复/缺失/非法数值均有负例。
- 爬升 fixture：14/14 检查通过；证据在 `.tmp/climb-fault-campaign-final/runs/climb-failure-*/fixture-report.json`、同目录 `mission.jsonl` 和 `summary.json`。
- 通用 campaign：修复日志快照后重跑仍为 **FAIL，16 个场景中 1 个通过（battery_stale），15 个失败**，原始报告 `.tmp/climb-fault-campaign-final/report.json`，完整输出 `.tmp/climb-fault-campaign-final.log`。缺失最后窗口内 decision reason、output gap、终态模式未确认/未持续等断言均原样保留。第一次运行的 `.tmp/climb-fault-campaign/report.json` 也得到 1/16：camera_freeze 的 watchdog/handoff 记录早于判定窗口；rejected_rtl 记录三次拒绝的 RTL，未观察到 LAND。现有日志不足以把所有失败归因于机器负载；本次未修改这些场景的 deadline、模式断言或 native runtime handoff。
- 默认 campaign bench key 原为未加前缀的可打印文本，正式 key loader 拒绝；已改为受支持的 `hmac:` 编码并设 0600。另修复 CampaignSession 读取异步追加日志时的半行竞争。
- CPU-only onboard gate：首次 **PASS，156 s**；全部改动后再次 **PASS，152 s**，最终日志 `.tmp/climb-onboard-gate-final.log`。该 gate 不等同于通用 fault campaign 的 16 场景验收。
- 作业脚本语法与 `--prepare-only`：通过；只生成 `.tmp/jobs/climb-land-*/suite.json` 与 dry-run 配置，没有启动 SITL 或写 artifacts。

## 待调度的唯一 SITL 作业

脚本：`.tmp/jobs/39-onboard-climb-land.sh`。调度方在本工作树执行：

```bash
bash .tmp/jobs/39-onboard-climb-land.sh
```

它复用 `run_mission_suite.py` 和现有 r21idr3/r24plan 的 `operator-abort` mission，不添加不稳定物理故障钩子，不降低期待结果。只收紧采集 envelope：64 steps、600 s nominal wall、现有 hard ceiling 1200 s；外层 2700 s 有界 timeout 覆盖启动/排队/收尾，另给 120 s 退出时间。一个 episode、一个 worker、无重试，由现有全局锁申请一个槽位；`MULTIDETECT_SITL_SLOTS=3`，仅槽位 0–2，未借用他人租约。设置 `MULTIDETECT_SIM_GPU_AVOID=2,3`、GPU 池与 CUDA 可见卡均仅 0/1。

作业只在调度方执行时创建新目录 `artifacts/benchmarks/onboard-climb-land-followup-20261107-<UTC>-<PID>/`；不会复用旧目录。启动前检查本地 image 和模型缓存，离线环境变量禁止下载。启动 PID 保存在 `.tmp/jobs/climb-land-*/launcher.pid`；没有按名称终止其他进程或清理其他容器的命令。`--keep-px4-ulog` 保留实际日志。

预期正常 abort 会进入 return_home 并完成既有降落收尾；不保证自然出现两次低空无增益 timeout。若没有触发，必须保留“正常 LAND 命令/事件边界可观测，但重复失败 live gap 未闭合”的结论，不自动再开 SITL。

后续分析应按同一 PX4 时间轴关联：

1. `job.log`、`main/operator-abort/report.json`、episode metadata：租约、退出原因、是否实际只有一个 episode，以及是否在预算内退出。
2. `steps.jsonl`：每个 up 的 before/after NED、PX4 boot、landed state、outcome；是否出现第二次合格 timeout、命中后有没有第三个 climb primitive。
3. `<episode>-onboard-frames/mission_log/mission.jsonl` 与 `summary.json`（用实际路径定位）：abort/decision、失败事件、alert、LAND escalation、最新 final_summary；保留原早期 return 摘要。
4. `mavlink.jsonl`、collector landing diagnostics、PX4 ULog：只报告实际出现的 command/ack、heartbeat main/sub mode、EXTENDED_SYS_STATE、armed 与最终状态；COMMAND_ACK 的 command/result 必须与请求对应，不把 `SET_POSITION_TARGET_LOCAL_NED` 的合成 `action_ack` 当 PX4 COMMAND_ACK。
5. 若有 setpoint 流日志，检查最后一次 up target 与 LAND mode 确认/streamer 停止的顺序。若只有 step 级 primitive 记录，明确不能证明流级停止时延。ack、进入 LAND、fresh landed_state=1 分别报告；只收到 ack 时保持 landed 未验证。

## 改动范围与未完成项

代码：`mission_executor.py` 收紧计数和保留样本；`mission_supervisor.py` 接管 outcome 路由、失败升级与摘要修订；`scripts/onboard_policy.py` 委托 supervisor；`run_onboard_fault_campaign.py` 添加确定性 fixture、接入总体 verdict、修复 bench key 与日志快照。测试修改仅在四个相关文件。新增本报告、`.tmp/jobs/39-onboard-climb-land.sh` 和仅保留作业源码可见的 `.tmp/.gitignore`。

未改变 LAND 优先级，未新增空中 disarm，未修改 collector vehicle-loss、训练/release-parity/版本/进度/生成 schema；未提交 git。

是否仍允许重复爬升：在已验证的 outcome 回灌链中，第二次合格失败之后**不允许再请求 climb primitive**。单次失败、进展达标或不合格样本后仍允许正常返航爬升。原生 runtime 的独立 watchdog 路径、collector vehicle-loss 提前退出路径和真实 PX4 setpoint/LAND 最终执行尚不能由本 fixture 保证。

## 建议的下一个任务

### 1. 分析单次 owned SITL 的 LAND 命令与事件边界

目标：运行或分析 `.tmp/jobs/39-onboard-climb-land.sh` 安排的唯一一次 owned disposable SITL，核对 return_home 爬升 outcome 回灌、land_now 请求、真实 PX4 ack/mode/landed state 和 up setpoint 停止边界。证据路径：`docs/onboard-climb-land-followup-20261107.md`、`docs/onboard-climb-failure-20261107.md`、`.tmp/jobs/39-onboard-climb-land.sh`、`artifacts/benchmarks/onboard-climb-land-followup-20261107-*/`、`scripts/collect_sim_teacher_episodes.py` 与 `scripts/onboard_policy.py`。若调度方已运行只分析已有输出；尚未运行时最多执行该脚本一次，串行占一个槽位，设置 MULTIDETECT_SITL_SLOTS=3、SIM_GPU_AVOID=2,3、GPU0/1，仅用 slot 0–2，不自动重试。不得修改 collector vehicle-loss、LAND 优先级、阈值或断言，不启动真机/HIL、不下载、不训练、不改共享 artifacts 既有文件。写清状态/事件时间线，分开 ack、LAND mode 和 landed；无两次稳定故障注入时明确 live gap，不能以正常降落替代失败链验收。写入 `docs/onboard-climb-land-sitl-review-20261107.md`。

### 2. 修复通用 onboard fault campaign 的证据与 handoff 验收缺口

目标：使 source-level onboard fault campaign 的正常实现通过证据验收，并继续拒绝现有 mutants；区分窗口筛选错误、异步事件落盘问题与真实 terminal handoff 未升级 LAND。证据路径：`docs/onboard-climb-land-followup-20261107.md`、`scripts/run_onboard_fault_campaign.py`、`tests/test_onboard_fault_campaign.py`、`src/multidetect/onboard_runtime/runtime.py`、`.tmp/climb-fault-campaign-final/report.json` 与 `.tmp/climb-fault-campaign-final/runs/*/mission.jsonl`（若本地日志不可用，重新运行离线 source campaign 到新 `.tmp/` 目录）。重点核对 camera_freeze 等缺失窗口内 reason、gcs/fence output gap，以及 rejected_rtl/lost_rtl/blocking_command 的三次 RTL 后没有 LAND。只改必要的 fault fixture、runtime 与相关测试；不得放宽/删除 deadline、连续输出或终态断言，不改 supervisor LAND 优先级、爬升阈值、collector vehicle-loss，不新增空中 disarm，不启动 SITL/Docker/HIL/真机、训练或下载，不写 artifacts。运行相关单测、通用 campaign、CPU-only onboard gate；报告每个失败是否由源码证据解释及修复后的实测结果，写入 `docs/onboard-fault-campaign-handoff-followup-20261107.md`。
