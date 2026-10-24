# 黄金重放决策回归

本次以 16 个黄金架次的逐步动作类型及 0.1 参数量化精确匹配统计（按步骤加权）：`count` 17.2%（86/500）、`monitor` 94.9%（223/235）、`patrol` 100%（48/48）、`area_search` 61.7%（127/206）、`return_home` 26.6%（21/79）。这是当前 executor 在离线重放下与日志动作的匹配度，不是任务完成率；差异主要来自 slow-call 时序、位姿近似和未做物理积分。

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
