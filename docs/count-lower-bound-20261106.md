# 计数为何以 lower bound 结束（2026-11-06）

## 结论

指定产物中的 lower bound 有两种情况：搜索预算耗尽，或覆盖路线结束但仍有未确认/未补看的候选。`22-off45` 消防栓少计的直接原因不是路线漏覆盖，也不是去重误合并：第三个真值消防栓在回看时被检测到，但身份模型先判为 `approach`、随后判为 `no_match`，因此没有新增 finding。当前跳过规则对与已计数 finding 落在去重半径内的候选跳过；它并非按“同帧共视”一概跳过。证据不足以支持修改计数或跳过逻辑，本次不改代码。

coverage 一栏记录 count 步骤终止原因。`coverage_complete` 表示搜索路线报告覆盖完成；它不代表每个候选都已确认，也不单独证明计数精确。mission 的 `coverage` 字段在这些摘要里通常为空对象，故没有可据以报告的百分比。

## 指定产物中的 count missions

| 产物 / mission | 真值 / 报告 / 匹配 | lower bound 与原因 | 补看跳过目标及原因 | coverage |
| --- | --- | --- | --- | --- |
| `22-off45` `comms-free-count` | 3 / 3 / 3，精确 | 否 | `[10.00,-7.21]`、`[10.98,-6.68]`：已回看；`[6.10,-64.59]`：超出任务区域 | `coverage_complete` |
| `22-off45` `count-cones` | 3 / 3 / 3，精确 | 否 | `[10.11,-7.23]`：已回看 | `coverage_complete` |
| `22-off45` `count-fire-hydrants` | 3 / 2 / 2，少 1 | 是；`coverage_complete`，仍有未决候选 | `[8.88,-15.23]`：与已计数目标在去重半径内；`[-35.92,-32.36]`：已回看；`[1.91,24.52]`、`[16.90,19.87]`：无清晰观察点 | `coverage_complete`，79 步 |
| `23-def180` `count-cones` | 3 / 3 / 3，精确 | 是；`coverage_complete`，摘要仍保留未决候选 | `[-22.23,28.88]`、`[-25.00,31.38]`：与已计数目标在去重半径内；`[16.59,-100.80]`：区域外；其余 `[-46.34,-21.53]`、`[-45.31,-21.46]`、`[32.91,-20.87]`、`[31.38,-19.59]`、`[-45.06,-20.90]`：已回看 | `coverage_complete` |
| `23-def180` `count-fire-hydrants` | 3 / 3 / 3，精确 | 否 | `[8.45,-15.13]`：与已计数目标在去重半径内；`[18.00,21.31]`、`[2.42,23.69]`：已回看 | `coverage_complete` |
| `26-off45` `comms-free-count` | 3 / 3 / 3，精确 | 否 | `[-22.07,28.65]`：与已计数目标在去重半径内；`[8.95,-63.94]`：区域外 | `coverage_complete` |
| `26-off45` `count-cones` | 3 / 3 / 3，精确 | 是；`coverage_complete`，小目标复看结果仍不充分 | rejection 跳过列表为空；small-unjudged 候选 `[9.46,52.08]`：已回看 | `coverage_complete` |
| `26-off45` `count-fire-hydrants` | 3 / 3 / 3，精确 | 否 | `[2.30,23.81]`、`[19.79,21.84]`、`[19.94,22.19]`：已回看 | `coverage_complete` |
| `27-off45-e2` `count-fire-hydrants` | 真值 3；摘要报告 3；匹配数未生成 | 否；摘要为 `coverage_complete` | 完成 episode 摘要没有补看跳过明细 | `coverage_complete`；episode 已完整结束，顶层 `report.json` 尚未生成，故没有匹配指标 |
| `27-off45-e2` `count-cones`（仅完整 episode） | 3 / 3 / 3，精确 | 是；`budget_exhausted` | 没有记录跳过目标；完成摘要记 `not_done=full_search_not_established:budget_exhausted` | 搜索预算耗尽。另一个 episode 仍是 `unknown`，未纳入 |
| `gimbal-25-off45` `gimbal-count-cones` | 3 / 3 / 3，精确 | 是；`budget_exhausted` | 摘要没有补看跳过明细 | 搜索预算耗尽 |

`23-def180` 的 `comms-free-count` 没有 episode；`27-off45-e2` 只纳入已写出成功 count 与返航摘要的 episode。`27-off45-e2` 的 hydrant 结果来自完整 episode 的 `mission_summary.json`，而不是缺失的聚合报告。以上各任务的真值和报告/匹配指标来自各自只读 `report.json`；27 hydrant 真值另由该 episode 引用的 `gen_cnt2_w0001` 世界确认。

## `22-off45` 消防栓证据

世界 `gen_cnt2_w0001` 中三个真值位置（N,E，米）约为 `(8.758,-15.019)`、`(37.883,-19.555)`、`(-35.442,-31.800)`。mission 摘要只报告前两个：`frame_0005.png` 的 finding `(8.8,-15.0)` 与 `frame_0014.png` 的 finding `(38.2,-19.6)`。这两个帧也能看到各自的红色消防栓；结果为真值 3、reported 2、matched 2，无误报。

漏掉的第三个真值与回看候选 `[-35.94,-32.36]` 对应。`frame_0069.png` / episode step 69 记录了这个候选及 12.8 px 小框；`frame_0070.png` / step 70 又检测到它（约 14.5 px），identity verdict 是 `approach`。在 `frame_0071.png` / step 71，目标仍在画面中，检测框变大，但 identity verdict 为 `no_match`。随后 step 72 没有检测，计数仍为 2。于是回看确实发生了，失败点是身份拒判，而不是候选没进入路线或跳过规则漏掉了一次 recheck。`[-35.92,-32.36]` 的 `already_revisited` 记录的是之后重复的同地点候选；它不能说明首次回看成功确认了目标。

另一个被跳过的 `[8.88,-15.23]` 与已计数 finding `(8.8,-15.0)` 相距约 0.27 m，是同一个场景目标。其 `co_visible_with_counted` 原因由确认 finding 的空间距离触发，而不是仅因两个目标出现在同一帧。`[1.91,24.52]` 与 `[16.90,19.87]` 的无清晰观察点分别是未获安全/可见复看位置的其他 rejected-place 候选；它们不在第三个真值消防栓附近。任务仍以 lower bound 结束是保守表达：候选确认不充分，不能把两个已确认目标宣称为完整计数。

## 代码路径

- `src/multidetect/autonomy/mission_executor.py:1753` 开始构建远距/小框 rejection 补看候选。`1775-1785` 按区域、已回看、与已计数 finding 的 `count_dedupe_m` 距离检查候选，再调用 `plan_rejection_revisit`；找不到可行观察点时记录 `no_clear_viewpoint`。`src/multidetect/autonomy/onboard_loop.py:2270` 实现补看视点规划。
- `src/multidetect/autonomy/mission_executor.py:770-784` 设置 `count_is_lower_bound`：count 不是以 `coverage_complete` 结束、仍有 positive track 未解决，或仍有 pending rejection / small-unjudged place 时为真。`1958-2019` 与 `2021-2033` 分别定义 pending rejection 和 pending small-unjudged 目标；已在去重半径内、区域外或已回看的候选不会继续算 pending。
- 因此 `coverage_complete` 和 `count_is_lower_bound=true` 可以同时出现。补看成功执行但模型拒判，也不等于确认了一个新目标；当前 `22-off45` 日志显示该次复看为 `approach -> no_match`。是否应改变模型身份判定需用独立识别证据和专项评估，不能从一个 episode 推导出安全、窄范围的代码修复。

## 本次范围

只读了 `artifacts/benchmarks/nightly-linux-r21idr3-r24plan-{22-off45,23-def180,26-off45,27-off45-e2}` 与 `artifacts/benchmarks/nightly-linux-r21idr3-r24plan-gimbal-25-off45` 中可用的 count 结果、episode 摘要和帧；未写入产物软链接。没有代码改动，也没有重跑 benchmark。
