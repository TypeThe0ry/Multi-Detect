# Follow 前置 area_search 归因分析（2026-11-05）

## 结论

Nightly ext-1 和 ext-2 使用同一条 `follow-red-person-return` prompt，r24 都输出
`area_search → follow → return_home`，而 suite 期望严格序列是
`follow → return_home`。因此这两次运行中计划与 suite 不匹配为 **2/2**；ext-2
完整记录了前置搜索耗尽 180 步、沿失败边返航、follow 未开始。ext-1 的步骤记录也
显示该计划，但 PX4 遥测在任务完成前停滞，步骤状态全为 unknown，不能把它计为一次
由额外搜索导致的执行失败。当前可以确认的失败归因是 **ext-2：1/1 可判定执行中**。

证据只覆盖一个独立 prompt 的两次运行，不足以估计所有 follow 请求的发生率。离线
r24 planning-v3 candidate holdout 中含 follow 的任务为 **6/80**，每条期望序列均显式
以 `area_search` 开始；其中 **0/6** 是 suite 这种 follow-first 契约。因此该 holdout
没有测量“直接 follow 是否应省略 area_search”，也不能用它证明 nightly suite 的首步
约定正确或错误。六题中四题步骤序列精确，两题另有漏掉 `record` 的错误，与前置搜索
问题无关。

## 运行证据与 prompt

两轮 suite 的任务文本相同：

> Find the person in red, follow them for two minutes, then return home.

`configs/onboard/suite-follow-r21idr3-r24plan.json` 的期望技能为
`[follow, return_home]`，并要求 follow 轨迹达到在框比例、移动距离、连续性和重获等
阈值。两轮 episode 的 onboard 配置都使用该 prompt 和 r24 planning model；步骤标签
都记录 `[area_search, follow, return_home]`。

| 运行 | 计划相对 suite 不匹配 | 可确认的执行结果 |
|---|---:|---|
| nightly ext-1，2026-11-03 | 是 | PX4/仿真桥接链路约在 392.84 s 停滞；119 个步骤仍处于 area_search。summary 中三步状态均 unknown，不能归因成搜索预算失败。 |
| nightly ext-2，2026-11-03 | 是 | `s1 area_search=failed/step_budget`，预算及 runtime step limit 均为 180；随后沿 `on_fail` 返航。176 条记录中搜索阶段没有可用目标检测，follow 没有开始。 |

ext-2 报告的 follow 轨迹指标没有有效样本，不应据此评价跟随控制器的目标在框比例、
移动距离或连续性。可归因的直接原因是 planner 输出超出 suite 的首步契约，搜索未能
发现目标且耗尽预算；`step_budget` 是这一计划进入 executor 后的结果，不是 pregate 或
intent validator 拒绝。

## 离线 prompt 对照

对 `artifacts/evaluation/r24-planner-round/candidate/planning-v3.json` 的 80 行逐题筛选，
含 follow 的 6 条如下。candidate 对 6 条都输出了开头的 `area_search`，这与该评测集
本身的期望一致；两条 pickup/scooter 任务漏掉 `record`，不属于本报告讨论的问题。

| Prompt | 期望技能 | r24 输出 | 步骤精确 |
|---|---|---|---|
| 场地上穿蓝外套那人在哪儿？搜到就跟着他走一分半钟，结束回起飞点。 | `area_search → follow → return_home` | 相同 | 是 |
| 院里跑的黑狗给我找着，盯着它挪动两分钟，别只停在原地看，结束降落。 | `area_search → follow → land` | 相同 | 是 |
| 停车场替我寻出那辆银色皮卡，跟车走180秒，回来前存一下它的影像。 | `area_search → follow → record → return_home` | `area_search → follow → return_home` | 否，漏 record |
| Pick out the runner with a green cap in the field, then move along with that runner for four minutes before heading back to launch. | `area_search → follow → return_home` | 相同 | 是 |
| Locate the brown dog in the yard and stay with it as it moves for 75 seconds. Finish with a landing. | `area_search → follow → land` | 相同 | 是 |
| Somewhere in the parking lot is a blue scooter. Locate it, move with it for two minutes, and save its image before bringing the drone back. | `area_search → follow → record → return_home` | `area_search → follow → return_home` | 否，漏 record |

套件 prompt 是额外的、不同的契约：它把“Find …, follow …”映射为 follow 自身负责
发现/跟踪，不接受独立搜索步骤。r24 训练/评测记录可说明这种两种表达约定并存，不能
据六条全部允许 area_search 的 holdout 推算 suite prompt 上 planner 的成功概率。

## Executor 安全性与选项

Executor 的 `follow` 已具备自身的目标获取路径：没有可继承 finding 时，启动 follow
loop 并等待带新鲜地面位置的 track；没有有效目标时不会盲目追踪，并会在有限重获窗口
后以 `target_not_found` 结束。若前一 `area_search` 成功并确认了 finding，现有成功转移
也会把关联 loop/track 传给 follow。因此，发现目标后的 `area_search → follow` 是可执行
组合，并非重复步骤必然导致失败。

本次 ext-2 的搜索没有检测，也没有成功；搜索步耗尽预算后应按计划的 `on_fail` 返航。
executor 若在这种情况下自动跳过搜索或把失败边改成 follow，就会覆盖显式失败策略；若
不跳过而只延长搜索预算，则会进一步挤压 follow/返航可用预算，且不保证出现目标检测。
直接从原点执行 follow 虽可复用其受限 acquisition 逻辑，但属于在失败后重解释计划，
当前两轮数据无法证明这一回退对其他 `area_search → follow` 任务保持语义和预算安全。

选项：

- **当前建议：不改 executor。** 保留计划图的成功/失败转移与现有受限 follow acquisition；
  将 suite 的 follow-first 契约作为 planner 与执行闭环的待验证差异记录。host intent
  repair 已被拒绝，本分析不把它列为解决方案；planner 增量轮次暂停，也不建议以训练
  或改计划来规避这次归因。
- **后续可评估的 executor 方案：** 为 follow-first 产品契约增加明确、受限的执行策略，
  让 follow 自己完成 acquisition，并在运行前明确处理/拒绝与之冲突的独立 search 步骤；
  必须用任务包/套件契约表达该模式，并验证未确认目标、搜索失败、no-fly、预算尾留和
  返航边行为。它不是可由当前证据支持的通用隐式跳步修复。
- **不建议：** 在 `area_search` 的 `on_fail` 后无条件进入 follow，或仅增加搜索预算。
  前者跳过计划明确规定的失败动作，后者没有证据能避免相同的耗尽失败。

所以本次没有实施 executor 代码改动，也没有新增测试：能安全保留的成功路径已经存在；
要改变失败路径则需要更明确的运行契约及跨路径验证，超出“证据清楚支持的小修复”范围。
共享 `artifacts/` 全程只读。

## 依据

- `docs/nightly-ext-1-analysis-20261103.md`：ext-1 遥测停滞、119 个步骤仍在搜索，未进入 follow。
- `docs/nightly-ext-2-analysis-20261103.md`：ext-2 的额外搜索、180 步预算失败、目标检测与 suite 期望。
- `docs/planner-round.md`：r24/r30 规划评测背景、planner 轮次暂停及 follow-first 的既有讨论。
- `configs/onboard/suite-follow-r21idr3-r24plan.json`：follow suite 的任务文本及技能/轨迹期望。
- `artifacts/benchmarks/nightly-linux-r21idr3-r24plan-ext-{1,2}/follow/`：两轮只读 episode、summary 和 suite report。
- `artifacts/evaluation/r24-planner-round/candidate/planning-v3.json`：80 条 holdout 中 6 条 follow prompt 的逐题结果。
- `src/multidetect/autonomy/mission_executor.py`：`_start`、`_finish`、`_budget_end` 与 `_follow` 的现有获取、成功/失败转移和预算行为。
