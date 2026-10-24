# 机载 episode 离线重放

`scripts/replay_onboard_episode.py` 读取 episode 的 `steps.jsonl`，将记录的姿态、时间戳、候选框和 slow-call 标签送入当前 checkout 的 `MissionExecutor`，输出逐步动作、调用统计、计数、告警及补看字段。

```powershell
$env:PYTHONPATH='src;.'
$env:PYTHONUTF8='1'
python scripts/replay_onboard_episode.py --mode logged D:\data\episode-0000-abc
```

`--mode logged` 不启动模型。`--mode cache --cache-file PATH` 从缓存取结果，未命中时用本次日志标签填充；`--mode real --cache-ram 0` 会明确报错“real 模式未实现”，因为此 CLI 尚未接入 YOLOE 与 llama-server provider。不要把 logged/cache 结果当作重新推理。

默认模式仍是开环，便于保持历史结果。传入 `--closed-loop-pose-approximation` 后，重放会使用当前行的 `obs_before` 做感知和决策，再把该行 `obs_after`（缺失时回退到下一行 `obs_before`）作为下一轮位姿；搜索回调使用与机载路径相同的 `ExpertSearchPolicy`，因此航点索引、覆盖轨迹和 `coverage_complete` 可以推进。该模式是“日志观测 + 当前代码决策”的半闭环，不会积分动作或模拟物理执行。

半闭环会在第一个非搜索类动作类型不一致处写出 `metrics.diverged_at_step`，并给出 `pre_divergence_steps` 与 `pre_divergence_action_type_agreement`。分歧后的动作仍保留在 trace，但不混入分歧前指标；搜索动作差异单独保留为搜索策略/日志航线差异。`revisit_replay_steps` 和 `revisit_logged_steps` 用于核对补看是否在相同步附近发生。

`--compare CODE_A CODE_B` 会在两个子进程中把对应目录的 `src` 放到 `PYTHONPATH`，对同一 episode 分别重放并输出 `comparison.first_divergence`。例如：

```powershell
python scripts/replay_onboard_episode.py EPISODE --compare D:\code\old D:\code\new --mode logged --output compare.json
```

典型分析流程：先用 `--mode logged` 检查字段覆盖和当前动作差异，再通过稳定缓存重复比较，最后才对接真实模型 provider 做受控 A/B。动作类型、完整动作参数、slow-call 列表、计数、告警和补看计划都保留在 `trace`；搜索占位会使覆盖路线和后续闭环动作必然不同，应单独归类。

## SITL 回归样本

2026-10-24 使用 `mission-suite-queryfirst-20261023` 的 `count-cones` 两个 episode，以及 `mission-suite-hydrant10-20261024` 的 `count-fire-hydrants` 两个 episode 做 `logged` 重放。step 0 的动作类型与参数均对齐；逐步打印见 scratch 下 `first10-stepwise.json`。汇总如下：

| Episode | Steps | 分歧前动作类型一致 | 首个非搜索分歧 | slow-call 一致 | 计数 replay/log | 覆盖完成 replay/log | 补看重放 |
|---|---:|---:|---:|---:|---:|---:|---|
| queryfirst `e73ea9f4` | 85 | 100% (4) | 4 | 19.6% | 1 / 3 | 否 / 是 | 未到达 |
| queryfirst `a6b18b04` | 107 | 100% (4) | 4 | 14.9% | 1 / 3 | 否 / 是 | 未到达 |
| hydrant10 `08dcb017` | 68 | 88.9% (9) | 9 | 37.0% | 1 / 3 | 否 / 是 | 52, 66 |
| hydrant10 `1df239e1` | 90 | 87.5% (8) | 8 | 32.0% | 1 / 3 | 否 / 是 | 76 |

以上是半闭环运行结果；括号是分歧前步数。四架次均能执行真实搜索策略并消除 hold 占位；但在首个非搜索分歧后，当前决策轨迹没有完成日志中的覆盖，因此计数只复现到 1/3。两架 queryfirst 在第 4 步接近阶段已先分歧。hydrant10 的分歧前动作类型为 88.9%/87.5%，低于 90% 门槛，原因是第 0 步航线起始转向和接近阶段的 yaw/位姿差异先于后续搜索推进；这两架仍能进入补看，步骤 52、66 和 76 与日志事件落在同一搜索尾段附近（日志事件为 52、57、77）。slow-call 一致率按 step、track id、映射后的 decision 对照。

`--compare` 现在比较两个 root 的半闭环 trace，并报告第一个变更字段、步号及两侧动作。使用包含搜索/补看改动的 `b0da50e` 与当前 root 可看到分歧步；结果写入 `D:\Projects\mdwt\_scratch\cx-replay-search\compare-b0da50e.json`。旧的 `b7dcac29`/`5fa1e8ea` 比较只覆盖预算改动，不能作为搜索行为演示。
