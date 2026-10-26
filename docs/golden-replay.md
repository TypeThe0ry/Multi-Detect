# 黄金重放决策回归

## 匹配结果与解释

统计口径按 16 个黄金架次中 `labels.onboard.skill` 逐步归类；类型一致率比较动作 kind，精确一致率要求 kind 相同且各参数按 0.1 四舍五入后相同。修复前的旧报告口径为 `count` 17.2%（86/500）、`area_search` 61.7%（127/206）、`return_home` 26.6%（21/79）。旧 return_home 分母仅含当时筛选的 79 步；本次按完整 16 架次归类后为 63 步，二者不可直接当成同一分母。

| 技能 | 修复前精确一致率 | 修复后动作类型一致率 | 修复后精确一致率 |
| --- | ---: | ---: | ---: |
| count | 17.2%（86/500） | 99.2%（469/473） | 99.2%（469/473） |
| area_search | 61.7%（127/206） | 96.1%（197/205） | 96.1%（197/205） |
| return_home | 26.6%（旧口径 21/79） | 85.7%（54/63） | 77.8%（49/63） |

count/area_search 提升來自 logged 模式的 `search_action` 不再固定返回 `hold(1s)`，而是返回当前 `step_index` 对应的日志动作。trace 新增 `search_delegated`，用于区分“执行器本步委托搜索，动作取自日志”与“执行器自身决策”。`search_outcome` 在 logged 模式下从该步 mission history 的最后一条记录注入；闭环模式仍使用搜索策略自己的 outcome。

重放额外输出 `decision_agreement`、逐步离散/连续差异和 `search_delegated`。checker fingerprint 同时记录委托标志和动作/安全信息；执行器停止委托搜索会触发差异，即使 logged 动作回灌掩盖了搜索策略的连续轨迹差异。该回放不声称验证独立搜索策略本身。

逐步证据（step 为日志 `step_index`）：`query-cones-e73ea9f4` step 5–6 和 17–27 在修复前是搜索 turn/translate 被占位 hold 取代；修复后这些步骤 action 与日志精确相同，trace 标为 `search_delegated=true`。修复后 count 的 4 个残余错型位于非回灌/非匹配技能步骤（总分母 473），需按 trace 的 `decision_mismatch_steps` 和日志技能字段定位；参数精确率与类型率相同，说明本组没有额外连续参数误差。`query-postbox-1109ae34` 是主要 area_search 异常来源之一，整个混合架次 action type 57.7%、exact 38.5%；其中 inspect 15 步仅 5 步类型一致，连续差异不能归因于 search 占位。`fullsuite-budget-be3fc76b` 的返航差异仍集中 step 39–42，涉及约 22.3° vs 90° 转向及 replay hold vs 日志平移；原因是 executor 返航控制与记录轨迹不同，不能用搜索动作回灌解释，也未确认是已知代码变更或量化边界问题。

返航证据：`fullsuite-budget-be3fc76b` 的 step 39 replay 转向约 90°、日志约 22.3°；step 40 replay hold、日志前进 15m；step 41 replay 转向、日志前进 15m；step 42 replay hold、日志前进约 11.4m。相邻日志 `fullsuite-abort-f5dce4d9` 在 step 39–42 有同型差异。当前材料只能确认这些是连续动作/轨迹输入差异，不能将其归结为量化边界或执行器代码回归；姿态回灌和未做物理积分是可能来源，需保留为未决归因。

归因汇总：

| 原因 | 修复前 | 修复后/状态 | 证据 |
| --- | --- | --- | --- |
| 搜索占位 hold 造成假动作差异 | count 86/500；area_search 127/206 | count 469/473，area_search 197/205；logged 委托动作取日志，委托位纳入 fingerprint | query-cones step 5–6、17–27 |
| slow-call 结果/track 对齐 | 日志结果按当前 needing track ID 查找；ID 漂移可改变结果 | 仍按 step 注入已记录 verdict；缺少匹配的 track 时无法可靠注入，需后续改进 | count 架次 agreement 88.5–100% |
| 位姿/遥测近似、无物理积分 | 参数逐项 0.1 精确匹配 | 不适用于 logged search 回灌步骤；返航连续控制仍按日志逐步比对 | budget-return step 39–42 |
| 参数量化边界 | 统一 0.1 四舍五入 | 暂无边界差异的独立确证；保留严格值供定位 | 本轮抽查未发现可归因实例 |
| 执行器真实差异/已变更逻辑 | 被工具假差异淹没 | 尚未确认真实执行器 bug；不修改 autonomy 行为 | 无可独立复现证据 |
| checker/replay 其他缺陷 | logged search callback 固定 hold，fingerprint 不表示是否委托 | 按 step 回灌 logged action；fingerprint 加入委托位；mutation test 覆盖不再委托 | `tests/test_golden_replay.py` |

黄金集清单在 `docs/golden-replay-manifest.json`，基线在
`docs/golden-replay-baseline.json`。清单只保存架次相对路径和 `steps.jsonl` 的 SHA-256，
不复制图像、深度或模型文件。`logged` 模式直接使用架次记录中的 detector/slow-call 结果，
不会启动 llama-server、GPU、SITL、Docker 或真实模型；当前重放器的 `real` 模式明确拒绝执行。

每次修改 `mission_executor`、`onboard_policy` 或 `safety` 后运行：

```powershell
$env:PYTHONPATH='src;.'
$env:PYTHONUTF8='1'
python scripts/check_golden_replay.py --data-root D:\Projects\Multi-Detect
```

未显式提供 `--data-root` 时，脚本先检查仓库默认路径；默认路径没有架次数据时再检查
`MULTIDETECT_DATA_ROOT` 环境变量。这样可注入外置数据根，例如
`$env:MULTIDETECT_DATA_ROOT='D:\Projects\Multi-Detect'`；脚本只读架次数据。

数据不在本机时，架次会逐项打印 `WARNING` 并跳过；全部缺失时退出码仍为 0，方便没有现场
数据的 CI 使用。数据存在但 hash 不一致会报错并返回 1。默认只检查 logged/open-loop，预计应在
两分钟内完成。输出的 `DIVERGENCE` 给出架次、首个 step 和类型：`action_changed` 表示动作
类型或量化参数变化，`safety_rejection_changed` 表示安全出口拒绝/接受结果变化，
`task_termination_changed` 表示 trace 长度或终止状态变化。

只有确认行为变化是有意的、已经评审过任务/安全影响后才更新基线。更新必须显式写理由，理由会
保存在基线 JSON 的 `updated_reason`：

```powershell
python scripts/check_golden_replay.py --data-root D:\Projects\Multi-Detect `
  --update-baseline --reason "调整小目标补看转向阈值，已由安全评审确认"
```

基线动作参数量化到 0.1，安全字段保留拒绝标志、hold/stop 转换、状态和原因。连续两次运行应
得到相同汇总；若未修改代码，退出码为 0。黄金集包含 count、hydrant、inspect、返航/预算、
终止、monitor 输入以及 VTOL/固定翼安全样本。重放优先使用 `labels.onboard.mission.plan` 的完整
steps；旧采集仅有 `skills` 摘要时保留该技能链并补齐确定性参数。任务包快照用于恢复
operating area、禁飞区、高度和 home。只有计划与技能摘要均缺失时才使用 `plan_source: fallback_count`，
报告会计数。动作一致率按计划步骤类型输出。剩余差异通常来自慢模型判断时序、位姿近似和安全/搜索
边界参数；logged 模式不调用随机模型。
