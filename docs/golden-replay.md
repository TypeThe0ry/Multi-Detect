# 黄金重放决策回归

## 匹配结果与解释

旧版严格指标（动作类型 + 所有参数按 0.1 量化，按步骤加权）为：`count` 17.2%（86/500）、`monitor` 94.9%（223/235）、`patrol` 100%（48/48）、`area_search` 61.7%（127/206）、`return_home` 26.6%（21/79）。它把连续控制量与离散执行决策混在一起，不能单独作为执行器逻辑回归率。

重放现在额外输出 `decision_agreement`、`decision_mismatch_steps` 和 `continuous_mismatch_steps`。决策一致率只比较动作类型；严格参数一致率仍保留，便于发现控制量变化。checker 继续以完整 fingerprint 比较作为门禁，避免放宽参数后静默吞掉 executor 的行为变化。开放环 replay 将搜索回调固定成 `hold(1s)`，会制造搜索动作假差异；半闭环 pose rebound 可供诊断，但不是 checker 默认值，因为重新关联 track 会改变 slow-call track ID。

逐步抽查证据（step 为日志 `step_index`）：`query-cones-e73ea9f4` 的 step 4 是日志爬升 translate vs replay hold，step 5–6 日志 turn vs replay hold；step 17–27 同类搜索占位差异。`hydrant10-08dcb017` step 9、14 是日志 8m 爬升 vs replay hold，step 15–17 又是搜索占位差异。上述差异属于重放工具缺陷/连续参数诊断，不是已确认的 executor bug。该架次 slow-call agreement 为 88.5%，另一个 query-cones 架次为 95.0%，hydrant10 两架均为 100%；因此 slow-call 不是 count 低分的主因。

返航证据：`fullsuite-budget-be3fc76b` 的 step 39 replay 转向约 90°、日志约 22.3°；step 40 replay hold、日志前进 15m；step 41 replay 转向、日志前进 15m；step 42 replay hold、日志前进约 11.4m。相邻日志 `fullsuite-abort-f5dce4d9` 在 step 39–42 有同型差异。当前材料只能确认这些是连续动作/轨迹输入差异，不能将其归结为量化边界或执行器代码回归；姿态回灌和未做物理积分是可能来源，需保留为未决归因。

归因汇总：

| 原因 | 修复前 | 修复后/状态 | 证据 |
| --- | --- | --- | --- |
| 搜索占位 hold 造成假动作差异 | count 86/500 严格匹配；大量搜索步骤错误 | 增加决策/连续参数分层指标；checker 默认行为未再冒充搜索策略 | query-cones step 5–6、17–27；hydrant10 step 15–17 |
| slow-call 结果/track 对齐 | 日志结果按当前 needing track ID 查找；ID 漂移可改变结果 | 仍按 step 注入已记录 verdict；缺少匹配的 track 时无法可靠注入，需后续改进 | count 架次 agreement 88.5–100% |
| 位姿/遥测近似、无物理积分 | 参数逐项 0.1 精确匹配 | 作为连续差异单列，未放宽严格门禁 | budget-return step 39–42 |
| 参数量化边界 | 统一 0.1 四舍五入 | 暂无边界差异的独立确证；保留严格值供定位 | 本轮抽查未发现可归因实例 |
| 执行器真实差异/已变更逻辑 | 被工具假差异淹没 | 尚未确认真实执行器 bug；不修改 autonomy 行为 | 无可独立复现证据 |
| checker/replay 其他缺陷 | 只报告首差异；开放环搜索回调固定 hold | 输出按步连续/离散差异索引；闭环仅作为可选诊断 | `replay_onboard_episode.py` metrics |

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
