# 机载 episode 离线重放

`scripts/replay_onboard_episode.py` 读取 episode 的 `steps.jsonl`，将记录的姿态、时间戳、候选框和 slow-call 标签送入当前 checkout 的 `MissionExecutor`，输出逐步动作、调用统计、计数、告警及补看字段。

```powershell
$env:PYTHONPATH='src;.'
$env:PYTHONUTF8='1'
python scripts/replay_onboard_episode.py --mode logged D:\data\episode-0000-abc
```

`--mode logged` 不启动模型。`--mode cache --cache-file PATH` 从缓存取结果，未命中时用本次日志标签填充；`--mode real --cache-ram 0` 会明确报错“real 模式未实现”，因为此 CLI 尚未接入 YOLOE 与 llama-server provider。不要把 logged/cache 结果当作重新推理。

重放是开环：输出动作不会回灌到下一步，搜索动作使用 hold 占位，后续日志位姿仍然来自原架次。`--closed-loop-pose-approximation` 预留了把后续位姿视为动作执行结果的近似开关；它不等于物理闭环，也不能保证补看真正完成。

`--compare CODE_A CODE_B` 会在两个子进程中把对应目录的 `src` 放到 `PYTHONPATH`，对同一 episode 分别重放并输出 `comparison.first_divergence`。例如：

```powershell
python scripts/replay_onboard_episode.py EPISODE --compare D:\code\old D:\code\new --mode logged --output compare.json
```

典型分析流程：先用 `--mode logged` 检查字段覆盖和当前动作差异，再通过稳定缓存重复比较，最后才对接真实模型 provider 做受控 A/B。动作类型、完整动作参数、slow-call 列表、计数、告警和补看计划都保留在 `trace`；搜索占位会使覆盖路线和后续闭环动作必然不同，应单独归类。

## SITL 回归样本

2026-10-24 使用 `mission-suite-queryfirst-20261023` 的 `count-cones` 两个 episode，以及 `mission-suite-hydrant10-20261024` 的 `count-fire-hydrants` 两个 episode 做 `logged` 重放。step 0 的动作类型与参数均对齐；逐步打印见 scratch 下 `first10-stepwise.json`。汇总如下：

| Episode | Steps | 动作类型/参数一致 | slow-call 一致 | 计数 replay/log | search hold 占位差异 | 告警 |
|---|---:|---:|---:|---:|---:|---|
| queryfirst `e73ea9f4` | 85 | 18.8% | 100% | 3 / 3 | 69 | 空 / 空 |
| queryfirst `a6b18b04` | 107 | 16.8% | 96.6% | 3 / 3 | 89 | 空 / 空 |
| hydrant10 `08dcb017` | 68 | 19.1% | 88.5% | 3 / 3 | 55 | 空 / 空 |
| hydrant10 `1df239e1` | 90 | 14.4% | 100% | 3 / 3 | 77 | 空 / 空 |

动作一致率是全步数上的严格值；低值主要来自 `search_action` 固定 hold，日志实际仍执行覆盖路线。这个差异不能解释为机载决策回归。slow-call 一致率按 step、track id、映射后的 decision 对照。当前 episode 的补看事件均未触发；覆盖完成后的重访闭环不能由这些样本验证。

`b7dcac29` 对 `5fa1e8ea` 在 `e73ea9f4` 上 compare 无分歧。代码差异是 count budget 估算/限制；该架次的配置预算 140 步未耗尽，补看排序没有进入活动路径，故开环 trace 不产生可观察差异。验证 JSON 和比较日志位于 `D:\Projects\mdwt\_scratch\cx-replay-tool\`。
