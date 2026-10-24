# Multi-Detect 操作员手册

这份手册面向飞手、地面站操作员和 SIM 联合验收负责人，覆盖任务包准备、签名与审批、飞行中监控、最终摘要和 SIM 演示。当前证据主要来自 Gazebo/PX4 SIM；SIM 通过不等于真实飞机、Jetson Orin Nano、真实相机或真实降落区已获准飞行。

完整端到端流程见 [SIM 操作员端到端指南](e2e-operator-sim.md)，协议和生命周期细节见 [GCS 任务包流程](gcs-mission-package-workflow.md)。负责人先看 [机载自主任务安全总览（2026-10-24）](autonomy-safety-overview-20261024.md)。

## 1. 写任务包

### 1.1 字段和边界

地点使用机载本地坐标系的 `[north_m, east_m]` 米，不填原始经纬度；地图原点由地面站明确设置。任务包的核心字段如下：

| 字段 | 写法与检查 |
| --- | --- |
| `mission_id` | 稳定且唯一；审批和控制命令都绑定它。 |
| `task_text` 或 `plan` | 至少一个。只有 `task_text` 时，机载上传后编译计划并回传 `kind=plan`。 |
| `places` | 命名地点到坐标数组，例如 `{"poi:gate": [40, 0]}`。风险标签放在 `zones`。 |
| `zones` | `centre_ne`、`radius_m`，可带 `risk_tag`、`oversight_required`。 |
| `routes` | 命名航路点集合，供 `patrol` 等步骤引用。 |
| `ref_labels` | 地点/区域的人类可读标签和别名，参与意图检查。 |
| `operating_area` | 北/东最小最大值和 `min_altitude_m`、`max_altitude_m`；每步高度必须在内。 |
| `home` | `[north_m, east_m]`，必须在 `operating_area` 内。 |
| `budget` | `time_s`、`reserve_time_s`、`energy_wh`、`reserve_energy_wh`，总量必须大于储备量。 |
| `risk_level` | `low`、`medium`、`high`，只是策略建议。 |
| `link_loss_policy` | `continue`、`continue_then_return`、`hold_then_return`、`return_now`、`land_now`。带计时的策略还要给非负 `timeout_s`。 |
| `heartbeat_timeout_s` | 地面心跳静默判定，至少 `0.001` 秒；不是 `timeout_s` 的替代品。 |
| `oversight_steps` | 必须保持地面监督的步骤 ID。 |
| `capabilities` | 可选的收紧声明；只能是机载 `AirframeProfile` 能力的子集，不能增加能力。 |

计划步骤使用 `goto`、`area_search`、`count`、`inspect`、`patrol`、`survey`、`follow`、`monitor`、`record`、`deliver`、`hover`、`return_home`、`land`。每步有合法的 `on_success`/`on_fail` 引用，最终走返航、降落或最终悬停；`query` 最多 8 个词。

### 1.2 地点依据、返航意图和能力

`goto`、带目标的 `land`、`deliver` 的 `target_ref` 必须由任务文字或标签别名提供依据。未知引用会带已知列表拒绝。别名用 `/` 分隔，例如：

```json
"ref_labels": {
  "poi:gate": "门 / 大门 / 入口 / entrance / gate"
}
```

“飞到门口”和“到 gate”都可依据 `poi:gate`；同一别名属于多个 ref 时会产生 `place_alias_conflict` 并停用该别名。任务说“返航/回家/RTL”而没有明确降落时，终止步骤不能凭空变成 `land`。`area_search`、`count`、`inspect`、`record`、`follow` 的查询对象必须在任务中出现依据；`patrol`/`monitor` 没有对象时按任务是否确实要求巡逻/监视分别告警或拒绝。

能力只能收紧：`capabilities` 与机载能力取交集。`deliver` 需要 `payload_drop`，带目标的 `land` 需要 `landing_on_target`，固定翼不具备 `hover` 或定点降落能力。最后一次仍违反时计划被拒，报告包含 `intent_violations`，不会改写成别的动作。

### 1.3 失联策略、预算和可飞时长

风险默认建议是低风险 `continue`、中风险 `continue_then_return`、高风险 `return_now`。`hold_then_return` 先悬停，`land_now` 仅在确认安全降落区并获负责人批准时使用。监督器只能收紧策略，返航/降落锁定后恢复链路也不能改回继续。

命令行工具的演示估算为 `min(time_s - reserve_time_s, (energy_wh - reserve_energy_wh) * 3600 / 500)`。`500 W` 是 SIM 保守功率下限，不是实机续航标定。预算、电量、监督区域和连续低置信度都可能提前返航；没有新鲜能源样本时界面显示“尚未收到”。

## 2. 下发、签名与审批

### 2.1 本地生成、校验和摘要

```powershell
$env:PYTHONPATH='src;.'
$env:PYTHONUTF8='1'
```

`new` 的 `--zone` 格式是“名称:北,东,半径[:标签]”，`--budget` 是“任务秒,预留秒,能量 Wh,预留 Wh”：

```powershell
python scripts/mission_package_tool.py new --id cones-demo --prompt '数交通锥后返航' `
  --zone field:25,0,20:open_field --home 0,0 --budget 600,180,120,30 `
  -o D:\Projects\mdwt\_scratch\cx-operator-manual\cones-demo.json
python scripts/mission_package_tool.py validate configs/onboard/mission-package-count-cones-example.json
python scripts/mission_package_tool.py show configs/onboard/mission-package-count-cones-example.json
```

`validate` 调用机载 `load_mission_package`，有内置 `plan` 时还调用 `check_plan_intent`；只证明结构、边界和意图通过，不证明目标一定能识别。`show` 显示风险、策略、地点/区域/路线计数、预算和 500 W 估算。

### 2.2 签名、上传和拒绝原因

真实运行和飞行/HIL 只接受已签名任务包。签名可用会话 `operator-link.json` 的 `operator_package_key_hex`，或 32 字节 HMAC 十六进制 key；默认另存，覆盖需显式 `--in-place`：

```powershell
python scripts/mission_package_tool.py sign configs/onboard/mission-package-count-cones-example.json `
  --key-hex ('00' * 32) `
  -o D:\Projects\mdwt\_scratch\cx-operator-manual\cones-demo.signed.json
python scripts/mission_package_tool.py validate D:\Projects\mdwt\_scratch\cx-operator-manual\cones-demo.signed.json `
  --key-hex ('00' * 32)
```

生产签名的 Ed25519 必须使用 MSVC 版 OpenSSL，不能用 MSYS2 MinGW OpenSSL；便携包的 `libcrypto-3-x64.dll` 要和地面站一起保留。不要把 key 写入日志。

在地面站 Fly 或 Plan 页点击“任务包”选择文件。上传“已接收，机载编译中”不等于批准、解锁或起飞；要等最终 ACK 和 `kind=plan`。当 `requires_approval=true` 时，负责人必须在真实地面站界面核对步骤、查询、地点、范围、预算和 `plan_sha256`，再点击“批准计划”。批准命令绑定相同 `mission_id`/`plan_sha256`，并通过签名、序号和时间戳检查后才执行。点击“拒绝”会写入 `plan_rejected`，锁定 `land_now`；未起飞时保持地面。窗口关闭不等于批准。

拒绝原因按三类排查：

1. **意图**：返航/降落冲突，查询对象无文本依据，或地点未被任务提及。
2. **能力**：需要机体没有的 `payload_drop`、`landing_on_target`、`hover` 等；包声明不能放大能力。
3. **未知地点/引用**：`target_ref` 不在引用表，或别名冲突后失去依据。

地面站没有自动审批；`MULTIDETECT_UI_AUTO_APPROVE_PLAN` 仅用于离屏测试。

## 3. 飞行中能看到和能做的事

任务卡约每秒更新 `status`：任务编号、步骤/技能、进度、监督器动作与原因、剩余时间/能量、有效失联策略和链路状态。每个 `finding`、`alert` 单独下行，发现带标签、置信度、位置和证据帧 ID。`count.lower_bound=true` 或 `count_is_lower_bound=true` 必须显示为“至少 N”。状态超过 1,200 字节会截短并标 `truncated=true`，超过 5 秒无更新则过期；未知能量不以名义预算代替。

负责人只能在真实地面站界面点击“暂停”“恢复”“中止”。暂停保持悬停并受暂停上限、能量和失联策略约束；恢复仅恢复仍在等待的任务；中止进入安全恢复。失联按包策略继续、定时继续后返航、悬停后返航、立即返航或请求降落。统一 `SafetyEnvelope` 会约束运动；链路不健康进入 `waiting_for_link`，GPS 过期进入 `waiting_for_gps`，未解锁可能为 `blocked`。恢复链路不能解除已锁定的返航/降落。

离线不暂停机载日志或执行；恢复后按任务和序号去重补发。最终摘要是分页的 `final_summary_page`，每页最多 1,536 个 UTF-8 字节，必须收齐后才可引用。

### 3.1 计划需要澄清时怎么答

地面站收到 `decision=clarify` 时不会起飞。先阅读 `questions_zh`（最多 3 个问题），在任务描述或计划中补足目标类别、跟随对象或数量上限，再用同一任务编号重新提交签名任务包；不要直接点击批准旧的 `plan_sha256`。常见问题是“是否移除上限或确认具体数量？”和“需要跟随什么目标？请描述类别、颜色或可识别特征。”重新提交仍会经过准入、计划校验和审批。规则和示例见 [计划校验与澄清](plan-validation-clarification.md)。

任务卡会同时列出 `reasons_zh` 和问题；机载以 `mission_refused.violations` 回传时，使用其中的 `message_zh` 与 `questions_zh`。澄清时“批准计划”按钮是禁用状态，只保留“修改任务并重新提交”；拒绝时显示拒绝原因且同样不能批准。`decision=repair` 会列出 `repairs` 自动修复项并允许负责人核对后批准。旧版机载没有 `validation` 或字段类型不正确时，地面站继续按原有 `requires_approval` 流程处理。

### 3.2 飞行中怎么发改派

当前地面站改派入口固定为 `replace`：发送已签名的候选任务包，等待 `retask_received`、`retask_validated` 和规划结果；需要审批时，核对候选 `plan_sha256` 后发送签名 `approve_retask`，拒绝则发送 `reject_retask`。改派规划期间飞机保持原计划的安全行为；规划/校验/审批超时或拒绝时原计划继续，不要重复点击造成并发候选。`append` 仅供机载 API，当前地面站没有模式选择。切换成功后摘要的 `plan_history` 会保留版本、模式和摘要，计数/发现/告警不会清空。详见 [飞行中改派设计](mid-mission-retask.md)。

任务卡状态依次显示“已受理”（收到 `retask_received`）、“规划中”（`retask_planning`）、“校验结论”（`retask_validated`）、“待审批”（`retask_approval` 且等待批准）、“已切换”（`retask_switched`）或“已拒绝”（`retask_rejected`，同时显示中文原因）。待审批时卡片显示候选 `plan_sha256`，批准或拒绝均绑定这一摘要；摘要不一致时机载会拒绝命令。上传入口明确写作“上传改派任务包（替换剩余步骤）”，仅在飞行中出现。最终摘要卡片的 `plan_history` 用于核对每个版本的序号、模式和摘要，不能把改派后的计数当成新任务。

### 3.3 机载崩溃恢复时地面站会看到什么

重启后地面站先看到 `crash_resume`/恢复审计事件和当前任务状态；有效检查点且 PX4 boot time、模式均允许时会继续原步骤，不能重新 arm/takeoff。PX4 已在 RTL/LAND/FAILSAFE，或 boot time/模式未知时，机载只记录与监视，显示 PX4 的失效保护状态；检查点缺失、损坏、过期或任务摘要不符时进入 RTL（无 RTL 的机型按 recovery policy 降落）。最终会有一条带中文原因的 `final_summary`，应结合 `recovery_history`、`terminal_action` 和 `terminal_reason` 判断，不要把“只记录与监视”误读为任务已恢复。详见 [机载进程崩溃恢复](crash-resume.md)。

## 4. 最终摘要怎么读

任务目录有 `mission.jsonl`、`summary.json`，可能有 `downlink-cursor.json`。先看 `status`、`terminal_action`、`terminal_reason`，再看 `findings`、`alerts`、`counts`、`history`、`recovery_history`、`not_done`、`omitted`。机载日志是完整审计，游标只表示地面站确认位置。

| 字段/标记 | 含义 |
| --- | --- |
| `completed` | 计划正常结束，例如完整覆盖 `coverage_complete`。 |
| `ended_early` | 监督器已锁定返航/降落，包括中止、时间/能量储备和失联。 |
| `plan_rejected` | 审批拒绝、摘要不匹配或意图/能力/引用检查失败；未起飞时留在地面。 |
| `stopped` | 宿主停止任务。 |
| `count_is_lower_bound=true` | 未证明完整覆盖或仍有候选未补看，只能读“至少”。 |
| `coverage_partial` | 有跳过航点或覆盖未完整；与计数下限一起读。 |
| 补看跳过 | `outside_region`、`already_revisited`、`attempts_exhausted`、`revisit_cap`、`no_clear_viewpoint`、`too_small_to_resolve`、`budget`、`energy_margin`；不是“没有目标”。 |
| 补看结果 | `judged_*`、`counted`、`still_unjudged`、`not_seen`、`descent_blocked`、`fly_blocked`、`timeout`、`interrupted`；拒绝补看写 `rejection_revisit_skipped`。 |
| 慢模型不可用 | 超时、无效 JSON、OOM 或进程故障；可能有 `planning_fallback_to_identity`，保留安全 sub-goal 并悬停/返航。 |
| 遥测断档 | `vehicle_telemetry_gap`/`vehicle_telemetry_gaps_s`；不计入电池时效，恢复后 5 秒内仍无遥测则返航。 |
| 长时监视 | `duration_elapsed` 表示请求时长到达；`tracking_budget_exhausted` 表示触及默认 4096 跟踪预算。 |

其他常见 `terminal_reason`：`budget_exhausted`/`step_budget`（预算用完，未证实完整覆盖）、`search_exhausted`、`max_count`、`target_lost_timeout`、`region_covered` 和 `*_blocked`。计数必须结合 `count_lower_bounds`、证据帧和告警复核。

## 5. SIM 演示与联合验收

### 5.1 不启动 SITL 的检查

```powershell
$env:PYTHONPATH='src;.'
$env:PYTHONUTF8='1'
python scripts/mission_package_tool.py --help
python scripts/run_operator_demo.py --help
pwsh scripts/run_operator_demo.ps1 --dry-run
```

`--dry-run` 只打印锁、SIM/GCS 命令和脱敏环境变量，不启动 SITL、GCS、Docker 或 noVNC。日志目录是 `D:\Projects\mdwt\_scratch\cx-demo-launcher\`。

### 5.2 一键启动和必须人工操作的步骤

真实联合验收（会启动 SIM，本次未执行）在仓库根目录运行：

```powershell
$env:PYTHONPATH='src;.'
$env:PYTHONUTF8='1'
pwsh scripts/run_operator_demo.ps1 --gcs-exe build/windows-v0.2.0-release/portable-v0.2.0/bin/MultiDetectGCS.exe
```

启动器拿 `D:\Projects\mdwt\_scratch\sitl.lock`，等待 `operator-link.json` 后启动 GCS。浏览器打开 `http://127.0.0.1:6080/vnc.html?autoconnect=1` 看 noVNC。传 `--task-package <包.json>` 会用会话 key 生成签名副本，但仍需负责人在地面站点击“任务包”。负责人必须人工：加载并核对计划、点击批准；另载包点击拒绝；执行中点击暂停/恢复/中止；确认 ACK、状态、发现/告警和收齐后的摘要；保存 `mission.jsonl`、`summary.json` 和会话日志。

启动器只清理自己记录的 session/GCS PID、子进程和唯一容器名；按 PID 检查现场，不要按名称批量结束 `python`、`bash` 或 `docker`。

### 5.3 离屏生命周期

不连接 SITL/Docker 的审批、分页和断链流程见 [GCS 任务包流程](gcs-mission-package-workflow.md) 的“离屏全生命周期联调”。离屏测试可设置 `MULTIDETECT_UI_AUTO_APPROVE_PLAN=1`，但它不替代真实地面站人工操作。

## 6. Jetson 配置边界

`planning_model` 是可选 planner GGUF 路径；planner 完成后关闭，再在同一端口启动 identity，不并发驻留。`--cache-ram 0`（`llama_cache_ram_mib=0`）用于避免 prompt cache 占满主机 RAM；部署前在目标机运行 `llama-server --help` 确认支持。Orin Nano 8 GB 表格是规划包络而非实测峰值；详见 [Jetson 部署说明](../deploy/jetson/README.md)。

## 7. 常见问题

| 现象 | 处理 |
| --- | --- |
| 缺字段/未知字段 | 对照字段表，保留 `budget`、`oversight_steps`、策略和签名字段。 |
| 返航意图被拒 | 检查是否错误生成 `land`；未明确降落应走 `return_home`。 |
| 地点无依据 | 检查 `target_ref`、任务文字和唯一别名“门 / 大门 / 入口 / gate”。 |
| 能力被拒 | `capabilities` 只能收紧，核对机型能力。 |
| 数量偏少 | 看 `coverage_partial`、`count_is_lower_bound`、`not_done` 和补看跳过原因，必要时暂停/返航人工复核。 |
| 摘要不完整 | 等所有 `final_summary_page` 收齐，检查链路、序号和 `downlink-cursor.json`。 |
| 状态过期/断档 | 超过 5 秒按过期处理；恢复后等按序补发，不能当作“没有发现”。 |
| 慢模型不可用 | 查 `planning_fallback_to_identity`、模型运行时、OOM/超时，保持安全 sub-goal。 |
| 演示找不到 GCS | 显式传 `--gcs-exe`，先跑 `run_operator_demo.py --dry-run`。 |
| 端口或锁忙 | 只按 PID 清理自己的会话，不删别人的锁。 |
| Orin 慢/热/重启 | 结束任务并返航，检查供电、散热、磁盘和 `--cache-ram 0`。 |

## 8. 证据入口

- [机载自主任务安全总览（2026-10-24）](autonomy-safety-overview-20261024.md)
- [任务包协议、审批、分页摘要和离屏生命周期](gcs-mission-package-workflow.md)
- [多用途任务设计、意图/地点/能力检查](onboard-multipurpose-mission-design.md)
- [SIM 操作员端到端流程](e2e-operator-sim.md)
- [自主架构中的安全出口和长时监视预算](autonomy-architecture.md)
- [2026-10-20 SIM 任务套件回归](sim-mission-suite-regression-20261020.md)
- [Jetson/llama 配置](../deploy/jetson/README.md)
- [一键演示与任务包工具说明](operator-demo.md)
