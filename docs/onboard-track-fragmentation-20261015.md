# 机载跟踪 ID 碎片化：诊断、修复与离线回放

日期：2026-10-15。

主要原因是扫描复访间隔超过 `lost_after_steps=4`：原有实现虽然在 `_tracks` 中保留证据，却只允许最近 4 步出现过的 track 参加关联。目标转出视野后再出现时，即使地面位置几乎没变，旧 ID 也没有匹配资格。修复利用现有任务内历史 track，增加有期限、有限空间半径的地面位置重识别。

## 数据与方法

- 输入为 `D:/Projects/Multi-Detect/artifacts/benchmarks/mission-suite-v1-r8`、`mission-suite-v1-r9` 下的 `campaign/**/steps.jsonl`，共 30 个 episode（r8：14；r9：16）。
- 共 2,000 行记录，覆盖此次分析技能的感知帧为 1,705 帧；日志中原始检测数量合计 2,567，保存的当前 track 框为 2,199，其中 1 个无法投影到地面。
- **日志没有保存原始 detection 框和 score**。回放以筛选后、保留到 0.1 像素的当前 track 框作为检测输入，等分数保持日志中的顺序，使用逐帧相机内参、位姿与姿态重建地面位置。
- 复用 `replay_alert_votes.py` 的 episode、JSONL、地面投影 helper，以及 `replay_place_verdict_memory.py` 的输入发现和任务边界 helper。任务切换时重置关联器。
- 每个任务内按 4 米最近质心聚类地面点。这里的“对象”是**几何位置代理**，不代表人工标注的物理对象真值；近邻物体可能合并，一个漂移 ID 也可能跨簇。
- 基线为关闭重识别的新关联器；另将 Git HEAD 原版关联器加载到内存，在同一组输入上比较 `(track_id, bbox, ground, hits)`，全部 2,199 框一致。基线与日志 ID 标签一致的框为 2,169 / 2,199（98.64%）；不能把本回放解释为完整重现原任务状态机。
- 调用成本使用实际 `tracks_needing_verdict` 调度，每个候选写入中性 `reject_track`，同时保留首次判定与 TTL 到期规则。不虚构正例、接近动作、焦点复核或计数耗尽。因此下表是**固定轨迹、固定候选框下的关联与调用候选估计**，不是新飞行的实际调用预测。monitor/patrol 的旧候选数量恰好与日志调用数一致；count/find/inspect 明显不同。

完整证据见 [逐 episode / 逐位置簇 JSON](evidence/onboard-track-association-r8-r9-20261015.json)，包含输入路径、`steps.jsonl` SHA256、逐事件原因和逐簇 ID 列表、调用候选数量。

## 成因量化

对“某 ID 首次出现在已有位置簇”事件，查找最近的空间兼容旧 ID；若没有兼容 ID，则使用簇内最近历史样本，并检查旧 ID 后续是否漂移到了其他位置。原地面分数为 `1 - distance / gate`，关联最低分为 0.2，因此有效地面距离界为 `0.8 * max(4, 0.15 * range)`。这部分诊断来自日志位置与年龄，不声称有未记录的完整检测匹配矩阵。

| 归因 | 事件数 | 占比 | 含义 |
| --- | ---: | ---: | --- |
| 旧 track 已 lost，但仍在原有效地面 gate 内 | 417 | 86.5% | 年龄超过 4 步，被候选集合直接排除 |
| 同一帧同一位置簇出现多个框 | 59 | 12.2% | 一对一关联不能让一个 ID 占用两个框；其中可能含重复 proposal 或实际近邻物体 |
| 旧 ID 的最新地面锚点已漂移到其他位置 | 5 | 1.0% | 簇内历史位置不能代表该 ID 当前锚点 |
| 尚未 lost，但超过有效地面 gate | 1 | 0.2% | 活跃 track 的地面位置不兼容 |
| 合计 | 482 | 100%（舍入） | 其中 3 个同帧多框事件属于 find；连续任务事件为 479 个 |

417 个 lost 事件的间隔中位数为 7 步，P90 为 17，P95 为 22，最大为 64；地面距离中位数为 0.38 米，P90 为 1.23，P95 为 1.59，最大为 3.16。常见扫描复访已经超出 4 步窗口。

以下是可重叠的伴随标记，不能相加，也不能直接当作独立根因：边缘裁切 48 次（其中 lost 39 次）；高度变化超过 1 米 14 次（全部为 lost）；pitch 变化超过 2 度 95 次（其中 lost 90 次）。唯一活跃地面 gate 未通过的事件位于 r9 `patrol-fence-backpacks/episode-0001-be93bbf0` 第 58 行：年龄 1 步，距离 3.944 米，有效 gate 3.2 米，同时伴随裁切与 pitch 变化。因此没有证据支持普遍放宽活跃关联 gate 或更改 pitched-camera yaw 预测。

## 实现与默认值

1. 保持原活跃 track 关联、yaw 像素预测、ground gate 和 4 步 lost 判定，先完成全部活跃关联。
2. 对未匹配框，从现有 `_tracks` 中寻找 `lost_after_steps < age <= ground_reid_max_steps` 的旧 track。静态目标的预测位置就是最后地面锚点。
3. 新旧锚点都必须有限，新旧框都必须离四个图像边缘超过 `border_margin_px`。只用地面距离，不用陈旧像素位置补分。
4. 距离不超过 `ground_reid_radius_m` 的候选按距离排序，做一对一贪心分配，活跃匹配优先。
5. 复活原 `Track` 对象，保留身份投票、确认历史、首次观察时间、verdict 时间与 `exhausted`。复活不会伪造新判断，也不会刷新 TTL。已计数耗尽目标复访后不重复计数、不新增慢调用。

配置可通过 `LoopConfig` 或 `scripts/onboard_policy.py` 的 options 设置：

```json
{
  "ground_reid_max_steps": null,
  "ground_reid_radius_m": 1.5
}
```

## Current replay and conservative identity bounds (2026-10-16)

The current 30-campaign replay is **653 -> 268 track IDs**.  Slow-call
candidates are **426 -> 426**: a revived track is deliberately marked as
`needs_fresh_verdict`, so its prior confirmation history remains diagnostic but
cannot satisfy a new decision without another slow verdict.

The older comparison table below is retained as historical context; its
current-result row is superseded by the replay totals above.

Ground revival uses a strict `<` test with
`max(1.5 m, configured minimum, 1.5 * estimated footprint)` as the radius.
The default `memory_radius_m` changed from **4.0 m to 1.5 m**.  This reduces
merging of nearby objects in place memory; the footprint factor still expands
the radius for genuinely large objects.

Residual cases are explicit: no ground projection means no ground re-ID;
edge-cut boxes cannot provide a reliable anchor; observations outside the
re-ID window create a new track; and an object swap inside the accepted radius
remains indistinguishable when the objects were never co-visible.  The latter
is accepted because ground projection error makes a larger radius unsafe; the
co-visibility veto and appearance gate cover the observable cases.

`ground_reid_max_steps=null` 自动对 `stop_on_confirm=False` 的 count/patrol/monitor 启用 60 步，对默认 find/inspect 关闭。`MissionExecutor` 对移动目标 follow 将自动值解析为 0，保留其既有 7 步失踪规则。显式 `0` 可恢复旧关联；显式正整数覆盖自动值（也可为 find/follow 手工开启）。半径必须有限且大于 0，年龄必须是非负整数或空值。

60 步以有限时窗覆盖常见扫描和航线复访，并比 32 步多保留一部分证据身份；3 米固定半径比原最小有效地面 gate 3.2 米更严，避免在远距离使用更宽的 range gate 复活旧目标。同帧多框不在本次修复中合并。裁切框、长期离场、锚点漂移仍会产生新 ID；没有外观特征时，同一小范围内目标替换仍可能误合并，故 follow 默认关闭此机制。

参数扫描保持 find/inspect 关闭，轨迹与框不变：

| 扫描项 | 参数 | 全部 ID 数 | 慢调用候选数 |
| --- | --- | --- | --- |
| 年龄（半径固定 3 米） | 8 / 16 / 32 / 60 步 | 354 / 296 / 257 / 251 | 351 / 350 / 350 / 350 |
| 半径（年龄固定 60 步） | 1 / 2 / 3 / 4 米 | 299 / 255 / 251 / 251 | 360 / 351 / 350 / 350 |

诊断聚类半径改为 3 / 4 / 5 米时，lost 事件为 416 / 417 / 418，主因结论稳定。聚类半径只影响诊断与逐簇统计，不改变关联器。

## 修改前后

ID 数按任务各自累计新建次数；调用候选含 TTL 复核，不只是新 ID 首次调用。

| 技能 | 旧 → 新 ID | 日志实际慢调用 | 旧 → 新慢调用候选 | 旧 → 新首次判定候选 |
| --- | ---: | ---: | ---: | ---: |
| count | 197 → 93 | 310 | 216 → 191 | 164 → 81 |
| area_search / find | 30 → 30 | 66 | 21 → 21 | 21 → 21 |
| inspect | 23 → 23 | 14 | 18 → 18 | 15 → 15 |
| monitor | 337 → 62 | 139 | 139 → 88 | 139 → 35 |
| patrol | 66 → 43 | 32 | 32 → 32 | 32 → 23 |
| 全部 | **653 → 251** | **561** | **426 → 350** | **371 → 175** |

全部 ID 减少 61.6%，调用候选减少 17.8%；monitor 分别减少 81.6%、36.7%。仅 count/patrol/monitor：ID 600 → 198，候选 387 → 311。r8：ID 235 → 104，候选 158 → 134；r9：ID 418 → 147，候选 268 → 216。patrol 虽保住更多 ID 和证据，但复访较晚仍需 TTL 复核，所以候选总数未下降。

逐位置簇示例（坐标为 north/east，单位米；完整列表在证据 JSON 的 `episodes[].clusters`）：

| monitor episode | 位置簇中心 | 日志 / 旧 / 新 ID 数 | 日志调用 | 旧 → 新候选 |
| --- | --- | ---: | ---: | ---: |
| r8 `0000-a33f3e2e`，题述电箱对应位置 | (33.748, 19.422) | 19 / 19 / **1** | 18 | **18 → 9** |
| r8 `0000-a33f3e2e` | (21.499, 0.871) | 19 / 19 / 1 | 19 | 19 → 10 |
| r9 `0000-2be9612a`，对应电箱位置 | (33.763, 19.416) | 19 / 19 / 2 | 17 | 17 → 9 |
| r9 `0001-cbbc86c1`，对应电箱位置 | (33.666, 19.603) | 19 / 19 / 2 | 10 | 10 → 6 |
| r8 `0000-a33f3e2e`，未改善的保守匹配样本 | (17.489, 16.318) | 8 / 8 / 8 | 8 | 8 → 8 |

19 个 ID 并不等于 19 次调用：有的框不满足最小判定尺寸或未进入当帧调度。电箱合并为一个 ID 后仍有 9 次候选，因为原 12 步 TTL 规则保留。

| 连续任务技能 | 位置簇数 | 旧 → 新簇内 ID 成员数合计 | 多 ID 位置簇数：旧 → 新 |
| --- | ---: | ---: | ---: |
| count | 72 | 210 → 106 | 39 → 25 |
| monitor | 27 | 337 → 62 | 23 → 18 |
| patrol | 30 | 68 → 45 | 19 → 12 |

簇内成员合计不是唯一 ID 数：一个 ID 可以跨多个簇，因此不能与上表的任务 ID 总数混加。

## 验证与复现

全部操作为 CPU 离线逻辑、文件读取与测试；没有启动模型、GPU、Docker 或模拟器，没有下载，也没有提交。

```powershell
python scripts/replay_track_association.py --source D:/Projects/Multi-Detect/artifacts --output artifacts/track-association/replay.json
python -m pytest -q -p no:warnings --basetemp .tmp-pytest-track-fragmentation tests/test_onboard_ground_reid.py tests/test_replay_track_association.py tests/test_onboard_loop.py tests/test_mission_executor.py tests/test_onboard_policy_plan.py tests/test_replay_place_verdict_memory.py tests/test_place_verdict_memory.py tests/test_place_memory_integration.py
python -m ruff check src/multidetect/autonomy/onboard_loop.py src/multidetect/autonomy/mission_executor.py scripts/onboard_policy.py scripts/replay_track_association.py tests/test_onboard_ground_reid.py tests/test_replay_track_association.py tests/test_mission_executor.py
```

上述测试共 141 项，全部通过；Ruff 与本次代码改动的 `git diff --check` 通过。新增回归覆盖 4/5/60/61 步边界、严格半径、全部图像边缘、无效地面点、活跃优先、一对一与近邻目标、证据和 TTL 保留、find 默认与显式关闭、follow 自动关闭、计数耗尽目标复活，以及回放任务隔离、成因分类、候选成本和输入不变性。原有 loop、mission、policy 和位置记忆测试一并运行。

改动文件：`src/multidetect/autonomy/onboard_loop.py`、`src/multidetect/autonomy/mission_executor.py`、`scripts/onboard_policy.py`、新增回放脚本 `scripts/replay_track_association.py`、`tests/test_mission_executor.py`、新增两个测试文件，以及本文和证据 JSON。工作区原有 `src/multidetect/simulator/__main__.py` 改动未触碰；用户列出的禁止编辑文件未修改。
