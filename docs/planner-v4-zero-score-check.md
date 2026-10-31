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

## 校验器修正

产品校验器已补上两处语义规则。下列重算结果按修正后的当前代码重新解析缓存 `raw`，不启动模型，也不写入只读 `artifacts/`。

**固定翼目标降落**：能力检查沿计划 `on_success` 成功路径追踪最近一次 `goto(target_ref)`。如果之后到达 `land` 前没有经过 `return_home`，则视为在该目标点降落；固定翼缺少 `landing_on_target` 时拒绝。`return_home` 会清除目标关联。该规则只作用于 `airframe:fixed_wing`，多旋翼/VTOL 保持原有能力行为。

| 机型 | 成功路径 | 判定 | 原因 |
|---|---|---|---|
| 固定翼 | `goto(poi:gate) -> land` | 非法 | 将目标点降落作为 target landing；缺少 `landing_on_target`。 |
| 固定翼 | `goto(poi:gate) -> inspect -> land` | 非法 | 中间步骤未返航，降落仍关联最近的 goto 目标。 |
| 固定翼 | `goto(poi:gate) -> return_home` | 合法 | 任务到达 home，由返航流程完成正常回场/跑道降落。 |
| 固定翼 | `land`（无前序 goto） | 合法 | 显式降落任务；没有把任意已访问目标解释成落点。 |
| 多旋翼/VTOL | `goto(poi:gate) -> land` | 原行为不变 | 本次固定翼专属规则不增加能力要求。 |
| 任意机型 | `land(target_ref=poi:gate)` | 按既有规则检查 | 显式目标降落仍需要 `landing_on_target`。 |

**否定范围内的 query grounding**：query 本身或其类别只在明确否定片段中出现时，不构成 grounding。覆盖中文“不要/别/禁止/不是/无需/无须”等否定，以及英文 `do not`、`don't`、`never`、`not a/an/the + category`，并能识别 `do not invent ...`。判断按类别词的出现位置限定否定范围，因此“找红车，不是蓝车”仍将红车视为 grounded、蓝车视为未 grounded；正常肯定提及不变。

| 文本 | query | grounding |
|---|---|---|
| `检查大门，不要凭空检查救护车。` | `ambulance` | 未落地 |
| `Inspect the gate; do not invent a fire truck target.` | `fire truck` | 未落地 |
| `Inspect the gate, don't inspect a fire truck.` | `fire truck` | 未落地 |
| `找红车，不是蓝车。` | `red car` / `blue car` | 红车落地；蓝车未落地 |
| `检查大门，并检查救护车。` | `ambulance` | 落地 |

| planning-v4 类别 | r23 candidate（修正后；修正前） | r14 baseline（修正后；修正前） | 变化 |
|---|---:|---:|---|
| `fixed_wing_target_land` | 2/2；0/2 | 2/2；1/2 | r23 两条 `goto -> land` 从放行变为正确拒绝；r14 一条同类输出新增拒绝，原有显式目标降落拒绝保留。 |
| `query_ungrounded` | 2/2；0/2 | 2/2；0/2 | 两模型中英文否定类别计划均从放行变为正确拒绝。 |
| `missing_payload_drop` | 0/2；0/2 | 0/2；0/2 | 不变；漏掉的 `deliver` 不能从文本推定为计划动作。 |

另以 `repair_plan_document`、`repair_plan_intent`、`capability_violations` 和 `check_plan_intent` 按编译时修复路径重验 planning-v3 已有预测：r23 70 条有效输出、r14 75 条有效输出，新规则造成的有效转无效均为 0 条。无需逐条例外处理。

验证覆盖 `test_capability_contract.py`、`test_plan_validation.py`、`test_plan_intent.py`、`test_plan_from_task.py`、`test_plan_from_task_capabilities.py` 及相关 onboard policy 测试。定向计划/意图测试通过；onboard 子集有 1 项因共享 artifacts 缺少 `onboard_policy_r37.onnx` 未通过。完整回归闸门运行到 100%，但 3 项既有测试因运行环境禁止创建 socket 失败（`test_port_wait_ignores_time_wait_after_server_close`、`test_start_up_mission_is_bound_and_held_for_operator_approval`、`test_replay_annotation_workbench_serves_verified_images_and_tokenized_mutations`）。
