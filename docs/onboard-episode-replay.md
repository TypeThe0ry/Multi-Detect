# 机载 episode 离线重放

`scripts/replay_onboard_episode.py` 读取 episode 的 `steps.jsonl`，将记录的姿态、时间戳、候选框和 slow-call 标签送入当前 checkout 的 `MissionExecutor`，输出逐步动作、调用统计、计数、告警及补看字段。

```powershell
$env:PYTHONPATH='src;.'
$env:PYTHONUTF8='1'
python scripts/replay_onboard_episode.py --mode logged D:\data\episode-0000-abc
```

`--mode logged` 不启动模型。`--mode cache --cache-file PATH` 从缓存取结果，未命中时用本次日志标签填充；`--mode real --cache-ram 0` 当前拒绝运行，因为此 CLI 尚未接入 YOLOE 与 llama-server provider。不要把 logged/cache 结果当作重新推理。

重放是开环：输出动作不会回灌到下一步，搜索动作使用 hold 占位，后续日志位姿仍然来自原架次。`--closed-loop-pose-approximation` 预留了把后续位姿视为动作执行结果的近似开关；它不等于物理闭环，也不能保证补看真正完成。

典型分析流程：先用 `--mode logged` 检查字段覆盖和当前动作差异，再通过稳定缓存重复比较，最后才对接真实模型 provider 做受控 A/B。比较时应分别在两个已有代码目录启动脚本并保存 JSON，再对齐 `trace` 中首个不同 step；当前 `--compare` 仅输出目录参数信息，不会切换代码目录，不能据此得出 A/B 结论。
