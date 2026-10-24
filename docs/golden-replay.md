# 黄金重放决策回归

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
终止、monitor 输入以及 VTOL/固定翼安全样本。由于当前 replay executor 固定构造
`count -> return_home` 计划，monitor/patrol/follow/search 仅作为输入/安全轨迹候选，不能被
解释为这些 skill 的真实策略覆盖。
