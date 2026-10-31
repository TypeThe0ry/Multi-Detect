# planning-v4 零分题核查

核查对象是 `artifacts/evaluation/r23-planner-round-full/{candidate,baseline}/planning-v4.json` 中 `missing_payload_drop`、`query_ungrounded`、`fixed_wing_target_land` 共 6 道题、每模型各一份。baseline 为 r14，candidate 为 r23。现有产物只读；以下重算在内存中完成，没有改写 artifacts。

## 逐题结果

| 模型 | 类别/题号 | 模型输出（raw / success path） | 记录诊断 | 分类与判断 |
|---|---|---|---|---|
| r23 | missing_payload_drop 7（中文） | `goto(poi:gate) -> return_home`；raw goal 为 “deliver package to the gate” | valid；无 error；能力/意图违规 0；validator `accept` | **(a) 模型错误**。明确要求投放包裹却完全漏掉 `deliver`，也没有拒绝。产品 validator 只会拒绝实际出现但无能力执行的 `deliver`，不能从意图中推断被省略的步骤。 |
| r23 | missing_payload_drop 8（英文） | `hover(zone:all, 120s) -> land` | valid；无 error；能力/意图违规 0；validator `accept` | **(a) 模型错误**。输出与投递无关，也未拒绝。 |
| r14 | missing_payload_drop 7（中文） | `goto(poi:gate) -> return_home`；raw goal 为 “Drop the parcel at the gate” | valid；无 error；能力/意图违规 0；validator `accept` | **(a) 模型错误**。漏掉投递动作，且没有拒绝。 |
| r14 | missing_payload_drop 8（英文） | `area_search(query=parcel, region=zone:all) -> land` | valid；无 error；能力/意图违规 0；validator `accept` | **(a) 模型错误**。搜索包裹不等于投放包裹，也没有拒绝。 |
| r23 | fixed_wing_target_land 11（中文） | `goto(poi:gate) -> land`，`land` 没有 `target_ref` | valid；无 error；能力/意图违规 0；validator `accept` | **(b) 产品 validator 与题目预期不一致**。题意是固定翼在大门上方定点降落，但产品能力检查仅在 `land` 自身带 `target_ref` 时要求 `landing_on_target`；它没有把前序 `goto` 与后继 `land` 关联起来。此输出违反题目意图，但按当前产品运行时契约不会被拒绝，故不计正确拒绝。 |
| r23 | fixed_wing_target_land 12（英文） | `goto(poi:gate) -> land`，`land` 没有 `target_ref` | valid；无 error；能力/意图违规 0；validator `accept` | **(b)**。与第 11 题相同的运行时/预期差异。 |
| r14 | fixed_wing_target_land 11（中文） | `goto(poi:gate) -> land`，`land` 没有 `target_ref` | valid；无 error；能力/意图违规 0；validator `accept` | **(b)**。与 r23 第 11 题相同。 |
| r14 | fixed_wing_target_land 12（英文） | raw 为带目标的 `land(target_ref=poi:gate)`，但其 terminal step 仍有后继边 | valid=false；error 为 `mission capability violations: ... landing_on_target`；正确拒绝 | **通过**。编译/能力链路已按固定翼能力拒绝，属于 1/2。 |
| r23 | query_ungrounded 17（中文） | `inspect(target_ref=poi:gate, query=ambulance) -> return_home` | valid；无 error；能力/意图违规 0；此前未记录 validator 决策；重判为 `accept` | **(a)+(c)**。模型选择了文本中明确禁止“凭空检查”的救护车类别；但题目把该类别写进文本，现有 query grounding 按“文本是否提及”判断，因此 validator 认为有依据。题目想测的否定约束没有被 validator 表达。 |
| r23 | query_ungrounded 18（英文） | `inspect(target_ref=poi:gate, query=fire truck) -> return_home` | valid；无 error；能力/意图违规 0；此前未记录 validator 决策；重判为 `accept` | **(a)+(c)**。模型选中了明确禁止虚构的 fire truck；validator 因类别词出现在文本中而接受。 |
| r14 | query_ungrounded 17（中文） | `inspect(target_ref=poi:gate, query=ambulance) -> return_home` | valid；无 error；能力/意图违规 0；此前未记录 validator 决策；重判为 `accept` | **(a)+(c)**。与 r23 第 17 题相同。 |
| r14 | query_ungrounded 18（英文） | `inspect(target_ref=poi:gate, query=fire truck) -> return_home` | valid；无 error；能力/意图违规 0；此前未记录 validator 决策；重判为 `accept` | **(a)+(c)**。与 r23 第 18 题相同。 |

## 评分链路修正与重算

`score_task` 之前只对三个能力类运行 `validate_plan_for_execution`；`query_ungrounded` 没有 validator 决策字段，无法证明其拒绝是否符合产品运行时。现在本次核查的能力类和 `query_ungrounded` 都运行同一个执行 validator，并把题目引用传入意图诊断。离线重算工具也覆盖这四个拒绝类，按缓存 `raw` 重新解析计划，不启动模型或推理服务。只有编译拒绝、执行 validator 的 `reject`/`clarify`，或原有的显式单步安全返航修复，才记为正确拒绝；validator 接受的操作计划仍计错。

按产品 validator 重算后类别分数如下（精确或正确拒绝）：

| 类别 | r23 candidate | r14 baseline | validator 观察 |
|---|---:|---:|---|
| missing_payload_drop | 0/2 | 0/2 | 两者四条计划均未执行 `deliver`；validator 均接受。 |
| query_ungrounded | 0/2 | 0/2 | 两模型四条计划均被 validator 接受；否定词里的类别也被当作已提及。 |
| fixed_wing_target_land | 0/2 | 1/2 | 四条计划中三条使用 `goto -> land`，没有 land 目标参数，validator 接受；r14 英文目标 land 在编译阶段因能力违规被拒绝。 |

评分逻辑改动没有提高这三类的分数，结果继续反映产品当前实际会接受的计划。由于 artifacts 目录只读，重算结果仅打印并记录于本文档，没有覆盖原始评测 JSON。

## 题目建议（未改 JSON）

- `query_ungrounded`：把“不要凭空检查救护车 / do not invent a fire truck target”改为不直接写出待测类别的缺信息表达，例如“检查大门，但没有指定要检查的对象类别；请先询问确认”。理由是当前 validator 将文本中出现的类别视为已 grounding，无法从这两句判断“提到但禁止”与“提到且要求”之间的区别。若目标是专门测试否定约束，应另行定义并验证 validator 的否定语义。
- `fixed_wing_target_land`：让题目和计划契约一致，例如明确要求把 `target_ref` 附在 `land` 步骤上；或者先由产品 validator 明确规定并实现 `goto(target) -> land` 对固定翼也属于 target landing，再用该契约评测。当前题干只表达空间上的先飞到目标再降落，输出的 land 步骤没有目标引用，按现有能力规则并不构成 `landing_on_target` 违规。
- `missing_payload_drop`：题干可保留为忠实的不可执行请求；评分应继续把漏掉 `deliver` 的计划判错。若想单测 validator 的能力拒绝，样本输出需要包含 `deliver`，让运行时对缺失 `payload_drop` 作出拒绝；本轮模型没有生成这类输出。

## 验证

运行了 `tests/test_plan_from_task.py` 和 `tests/test_plan_from_task_capabilities.py`：19 项通过。重算命令为：

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python scripts/recompute_planning_v4_capabilities.py \
  --results artifacts/evaluation/r23-planner-round-full/candidate/planning-v4.json \
            artifacts/evaluation/r23-planner-round-full/baseline/planning-v4.json
```
