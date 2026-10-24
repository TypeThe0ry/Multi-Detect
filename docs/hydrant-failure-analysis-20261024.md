# 消防栓计数 SITL 失败分析（2026-10-24）

## 结论与样本范围

用户提供的“10 架次、7/10 通过”对应 `mission-suite-hydrant10-20261024/count-fire-hydrants`，不是 `mission-suite-hydrant10b-20261024`。前者 report 有 10 条：7 条成功、3 条失败。后者实际只有 8 条 episode 目录和 8 条 report：6 条成功、1 条 `false_confirm`、1 条因磁盘低于 8 GiB 在 0 步中止；其 `report.json` 的 summary 数值与任务所述 10 架次/7 通过并不相符。以下三架失败按用户所述 7/10 批次分析，`10b` 另作范围说明，不混入三失败统计。

失败的直接共同原因是慢模型身份判定把场景中的 canonical `true_target` 消防栓（约 north 8.76 m、east -15.02 m）判为 `no_match`。三个架次同时把约 north 38 m、east -19.5 m 的另一个合法 `count_member` 交给 `t4` 做 approach/confirm；它也是消防栓，但不能替代 canonical 目标的定位命中。后续轨迹对 canonical 簇有正向 verdict，只有一架最终将其正确纳入 finding。另两架只报告 2/3；第三架报告数值 3，但 canonical 目标仍未被正确定位，评分给出约 30 m 误差和 IoU=0。任务均以 `coverage_complete`、`return_home arrived` 正常结束，步数 83–88，没有达到任务或预算上限。

## 失败表

| Episode | 结果 | 直接证据 | 归类 |
|---|---|---|---|
| `episode-0000-5416eb1a` | 报告 2/3；canonical 目标未计入，评分位置误差 29.91 m，IoU 0 | canonical 簇约 (8.72, -15.05)：第 4 步 `t3` 判 `no_match`，置信度 0.997923；第 55–57 步 `t9` 又判 `match / approach_track`（0.943–0.967），第 57 步确认。约 (38, -19.5) 的 `t4` 是合法 count member，却被当成 canonical 确认；最终 `mission_summary` 只有两个 finding。 | 身份误判与实例关联 |
| `episode-0000-7166ed2b` | 报告 2/3；canonical 目标未计入，评分位置误差 29.91 m，IoU 0 | canonical 簇约 (8.7, -15.0)：第 4–5 步 `t3` 判 `match / approach_track`（0.970、0.831），第 6 步转为 `no_match / reject_track`（0.984）；第 59–61 步 `t10` 再判 `match / approach_track`（0.930–0.971）。约 (38, -19.5) 的 `t4` 是合法 count member，最终却承担了错误的 canonical 确认；finding 只有 2 个。 | 身份判定不稳定与实例关联；非预算截断 |
| `episode-0000-ac3bb412` | 数量 3/3，但 canonical 定位仍 false confirm；位置误差 29.83 m，IoU 0 | canonical 簇约 (8.7, -15.0)：第 4–5 步 `t3` 判 `match`（0.977、0.609），第 6 步转为 `no_match`（0.997）；第 40–41 步 `t8` 后续确认并形成该簇 finding。与此同时约 (38, -19.5) 的 `t4` 作为合法 count member 被 approach/confirm，评估仍将 canonical 关联判错。 | 身份误判 / 实例关联；计数去重不是主因 |

距离坐标使用 `ground_ne` 的 north/east 米制坐标，坐标来自当步 onboard track 记录；以上位置簇与 episode 真值的 `(north=8.758, east=-15.019)` 相符。步骤号按 `steps.jsonl` 的 `step_index`（0 起始）报告。报告生成器对轨迹与真值的最近邻匹配是离线归因线索，不等价于 detector 的原始逐框输出；身份结论以记录的 onboard `tracks` 和 `slow_calls` 为据。

## 分类、证据与建议

| 类别 | 判定与证据 | 最小改进建议 | 影响面 |
|---|---|---|---|
| 感知漏检 | 非主因。canonical 目标附近多步有 detector track，位置簇反复出现；失败发生在身份筛选，而非完全没有检测到。 | 为候选保留多视角身份历史，避免单次慢模型 verdict 覆盖后续观测。 | 中：消防栓及小尺寸计数目标。 |
| 身份误判 | 主因。canonical 簇 `(8.7, -15.0)` 出现 `no_match` 与 `match` 翻转；第 4/6 步高置信拒绝后未可靠恢复。合法 count member `(38, -19.5)` 的 `t4` 则稳定被确认。 | 对候选 `no_match` 降低终结性；要求跨视角一致拒绝，或同簇出现相反 verdict 时重新进入确认队列。检查单 token verdict 与 confidence 校准。 | 高：所有依赖慢模型身份过滤的 count 任务。 |
| 未补看 | 拒绝后的恢复不足是次因。后续轨迹再次看到 canonical 簇并出现正向 verdict；前两架仍未形成第三个 finding。第三架虽形成该簇 finding，但评分匹配仍失败。 | 将拒绝候选后续正向 verdict 作为重新确认条件；只对冲突候选定点补看并设置重试上限。 | 中：增加少量候选定点观察。 |
| 预算截断 | 排除。84、88、83 步后均正常完成 count 和返航；mission reason 为 `coverage_complete`，不是预算结束。 | 无需增加全局步数预算；优先修复候选生命周期。 | 无。 |
| 规划问题 | 次要/未证实。三架都正常完成规划动作与返航；航线访问了目标区域附近，轨迹中存在目标 track。不能据此认定规划导致失败。 | 暂不改覆盖规划；修复拒绝候选的状态管理后再复测。 | 低，避免无证据扩大航线。 |
| 去重合并错误 | 暂无直接证据认定为首因。前两架只有两个 finding；证据更直接指向 canonical 候选身份拒绝和恢复未闭环。 | 增加空间簇 verdict 冲突诊断；修复候选身份生命周期后再检查去重结果。 | 中，需后续单独验证。 |
| 其他：确认定位关联 | 三架均有约 29.8–29.9 m 误差、IoU=0；前两架漏掉 canonical finding，第三架数值虽为 3 仍被评失败。`t4` 位于合法 count member `(38, -19.5)`，其确认与 canonical true_target 分属不同实例，需核对 finding 到实例真值的评估匹配。 | 联合核对 `mission_summary.findings`、confirmed track、truth target ID 和评估匹配语义，避免将同类别 count member 与 canonical 实例混淆。 | 中到高：影响同类任务的实例定位评分与失败统计。 |

canonical 目标被拒绝时慢模型置信度很高（约 0.997–1.000），后续重新识别置信度变化很大（约 0.50–0.98），且同一空间簇存在翻转。合法 count member 的 `t4` 则反复得到高置信 match 并确认。因此简单调阈值不是充分修复；需要解决跨视角身份状态，并验证实例级评分匹配。

## 成功对照

生成并查看了成功 episode `08dcb017`、`1df239e1`、`3314986c` 的 HTML 报告。三者均 3/3 计数成功并安全返航，分别 69、91、78 步。它们也能在部分消防栓候选上出现高置信 `no_match`，但最终均成功形成三个 findings；其中 `08dcb017` 和 `1df239e1` 对目标簇约 `(8.7, -15.0)` 记录了有效 `match` 并确认。这个差异支持“身份拒绝后能否恢复/确认”的状态问题，而不是固定航线覆盖不到该目标。

六份离线报告位于 `D:/Projects/mdwt/_scratch/cx-hydrant-failures/reports/`：三个失败 episode 和上述三个成功 episode 各一份。报告对应旧的 7/10 批次，以 `--no-thumbnails` 生成以减少内存和磁盘占用。

## 批次差异与其他线索

`mission-suite-hydrant10b-20261024` 有六条成功、一个 `episode-0000-5ed7f598` false confirm，以及一个 `episode-0000-37897290` 因可用磁盘 7.37 GiB 低于 8 GiB 在 0 步中止。该 false confirm 的 report 字段显示 3/3 计数、位置误差约 29.58 m、IoU=0；这与旧批次共享“错误确认/位置关联”特征，但不是本文三架次之一。中止属于基础设施前置检查，不是飞行任务失败。

## 修复

根因位于 `OnboardLoop.apply_verdict` 的候选生命周期：`reject_track` 会清空当前 track 的正向票，并把 ground/appearance 写入 `_rejections` 与 `PlaceVerdictMemory`。后续新 track（失败架次中 canonical 簇为 `t3` -> `t10`，第 4--6 步拒绝后第 59--61 步正向）仍命中同一空间拒绝记忆；`remembered_verdict`/`unresolved_positive_tracks` 因而把它当作已否决位置，正向结果没有重新进入确认队列。

修复在 `onboard_loop.py` 增加了有界的空间拒绝恢复：count loop 开启恢复后，同一地点的新正向 verdict 才能清除地点抑制；低于 `rejection_recovery_min_confidence` 的翻转继续被压住；每个拒绝地点最多 `rejection_recovery_max_attempts` 次。恢复只解除抑制，不直接计数，原有 `closest_view`/`alert_min_identity_matches` 等确认门槛不变。patrol/monitor 默认关闭该策略，避免改变干扰物告警语义。

评分核查：`score_onboard_loop.py` 对 count 任务同时输出 `reported_count`、`matched_count`、`count_exact`，而通用 `success` 仍要求 canonical true target 的位置半径和确认框 IoU。`run_mission_suite.py` 的 hydrant expectation 明确是 `min_count_exact: 1`，因此计数任务成功口径是 `count_exact`；canonical 定位保留为旧的 `success`/false-confirm 指标，不能拿它替代数量语义。旧 hydrant10 为 `count_exact=7/10`；本次修复目标架次的计数成功应按 3/3 记录，批次汇总保留旧 `success_rate=0.7` 字段并另报 `count_exact_rate`，不改受证据约束的 suite 产物。

## 代码检查与修复状态

只读检查了 episode 报告、logged replay 与 timing 脚本及相关数据。检查中曾怀疑 `replay_onboard_episode.py` 的 mission 路径父目录索引，但按真实目录层级验证后确认索引正确，配置可被找到。没有发现可确认、少于 50 行且能直接修复本次根因的代码缺陷，因此未改代码、未新增测试，也未建立临时 worktree。由于未改代码，不需要运行测试；没有启动 SITL/Docker/llama-server，也没有运行 onboard gate。`artifacts/` junction 全程只读。

## SITL 复验（2026-10-24 晚，代码 25f4902f，含拒绝恢复修复）

| 批次 | 架次 | 数量完全正确 | 误计 | 备注 |
|---|---:|---:|---:|---|
| 修复前 hydrant10（9eb5a463） | 10 | 7 | 0 | 3 架漏计（canonical 消防栓被高置信拒绝后未恢复） |
| 修复后 countfix-hydrant10 | 10 | 9 | 0 | 剩 1 架报告 2/3；步数 97–125，均正常返航 |
| 修复后 countfix-cones3 | 3 | 1/1 完成架次 | 0 | 另 2 架因主机内存紧张导致 episode wall-clock timeout，未飞完，不计入 |

结果目录：`artifacts/benchmarks/mission-suite-countfix-hydrant10-20261024/`、`artifacts/benchmarks/mission-suite-countfix-cones3-20261024/`。锥桶任务有干扰物，修复后完成架次没有误计，说明拒绝恢复没有放宽对干扰物的拒绝；样本量仍小，下一轮夜间回归继续观察。
