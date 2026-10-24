# 机载 episode 离线重放

`replay_onboard_episode.py` 将 `steps.jsonl` 的观测、姿态、候选框和 slow-call 标签送入当前 `MissionExecutor`。

```powershell
$env:PYTHONPATH='src;.'
$env:PYTHONUTF8='1'
python scripts/replay_onboard_episode.py --mode logged D:\data\episode-0000-abc
```

默认模式是开环：每行独立使用 `obs_before`，搜索动作保持旧的 hold 占位，因此历史 slow-call/count 指标不受半闭环策略影响。传入 `--closed-loop-pose-approximation` 后，当前行的 `obs_after`（缺失时回退到下一行 `obs_before`）作为下一轮位姿；仅此模式接入 `ExpertSearchPolicy`，并按对象尺寸、episode 起飞高度和障碍物净空复刻搜索高度。它是“日志观测 + 当前代码决策”的半闭环，不积分动作或模拟物理执行。trace 的 `diagnostics` 记录状态、focus、确认计数、搜索高度、航点和策略状态。

第一个非搜索类动作类型不一致时写出 `metrics.diverged_at_step`，`pre_divergence_steps` 和对应一致率只统计该步之前；搜索动作差异单独归类。`revisit_replay_steps` 与 `revisit_logged_steps` 用于核对补看事件。

新采集的 `episode.json` 顶层 `provenance` 保存 `code`（完整/短 commit、dirty、变更文件数、分支或 detached、worktree 根）、`config`（机载 JSON 路径与 SHA-256）、`config_snapshot`（架次目录 `config/` 内的快照路径）、`models`（模型路径、SHA-256、大小和缓存 mtime）、`worlds`（世界 SDF 路径与 SHA-256）及 `command`（采集器 argv）。GGUF/权重摘要通过架次目录 `.model-hash-cache.json` 按文件大小和 mtime_ns 复用；模型不复制进架次目录。旧 episode 可没有该字段。

Replay 输出的每个 episode 都含 `provenance.recorded_code`、`provenance.current_code` 与 `provenance.version_mismatch`。版本不同时该值为 `true`；旧 episode 或 Git 不可用时不会推断版本差异。

`--compare CODE_A CODE_B` 在两个 root 中分别运行同一 episode，并输出 `comparison.first_divergence`，例如：

```powershell
python scripts/replay_onboard_episode.py EPISODE --compare D:\code\old D:\code\new --mode logged --closed-loop-pose-approximation --output compare.json
```

## SITL 回归样本

2026-10-24 对 `mission-suite-queryfirst-20261023` 的 `e73ea9f4`、`a6b18b04` 和 `mission-suite-hydrant10-20261024` 的 `08dcb017`、`1df239e1` 做 logged 重放。完整 JSON 位于 `D:\Projects\mdwt\_scratch\cx-replay-search\open-final.json` 与 `closed-final.json`。slow-call 按 step、track id 和映射后的 decision 对照；动作一致率是首个非搜索分歧之前的类型一致率。

| Episode | 模式 | slow-call | 计数 replay/log | 首个非搜索分歧 | 分歧前动作 | 覆盖 replay/log | 补看 replay/log |
|---|---|---:|---:|---:|---:|---|---|
| `e73ea9f4` | 开环 | 100% | 3 / 3 | 4 | 100% (4) | 否 / 是 | 无 / 无 |
| `e73ea9f4` | 半闭环 | 19.6% | 1 / 3 | 34 | 100% (34) | 否 / 是 | 无 / 无 |
| `a6b18b04` | 开环 | 96.6% | 3 / 3 | 7 | 100% (7) | 否 / 是 | 无 / 87, 94 |
| `a6b18b04` | 半闭环 | 14.9% | 1 / 3 | 4 | 100% (4) | 否 / 是 | 无 / 87, 94 |
| `08dcb017` | 开环 | 88.5% | 3 / 3 | 9 | 100% (9) | 否 / 是 | 无 / 52, 57 |
| `08dcb017` | 半闭环 | 37.0% | 1 / 3 | 14 | 100% (14) | 否 / 是 | 52 / 52, 57 |
| `1df239e1` | 开环 | 100% | 3 / 3 | 8 | 100% (8) | 否 / 是 | 无 / 77 |
| `1df239e1` | 半闭环 | 32.0% | 1 / 3 | 无 | 100% (90) | 否 / 是 | 76 / 77 |

开环指标保持上一版范围（88.5%–100%，四架次均为 3/3）。e73 半闭环的首个分歧由第 4 步后移至第 34 步：第 0–3 步动作一致，第 4 步日志爬升 `3.293759m`、重放爬升 `3.293666m`。日志起飞高度为 `13.271817m`，世界障碍物最高 `9.271817m`，净空规则得到搜索高度 `11.271817m`；此前重放没有设置 Loop 的搜索高度，计数后错误转向。其余三架的早期分歧仍来自日志与当前决策输入状态差异；a6 在第 4 步、08d 在第 14 步，1df 在 90 步内没有非搜索类型分歧。半闭环尚未完成四架的整圈覆盖，计数均为 1/3；08d 补看第 52 步对齐日志第 52 步（日志另有第 57 步），1df 第 76/77 步相差一步，queryfirst 的日志补看未复现。episode 的 `episode.json`、`steps.jsonl` 和目录中没有生成代码提交/head 字段，因此不能归因于版本差异。

`dc917494`（历史重放版本）与当前 root 的半闭环 compare 在 step 2 首次出现 `action` 分歧，结果写入 `D:\Projects\mdwt\_scratch\cx-replay-search\compare-dc917494.json`。`b0da50e` 不含 replay 脚本，作为基线时本工具回退到当前脚本；e73 对比 trace 相同，因此不把它误报为有效行为差异演示。
