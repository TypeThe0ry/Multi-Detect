# 规划拒绝与澄清契约

## 为什么需要

规划模型以前只能返回 `MissionPlan`。当任务要求飞机没有的能力（例如投放载荷），解码约束会排除 `deliver`，模型可能改写成另一个可执行任务。飞机随后会执行与操作员意图不同的任务。现在规划结果可以明确拒绝或请求澄清，安全默认是“不执行该任务”。

## 输出格式

旧的 `MissionPlan` 输出继续有效。新增决策对象如下：

```json
{
  "plan_version": 1,
  "decision": "reject",
  "reason_code": "missing_capability",
  "reason": "Aircraft has no payload drop capability.",
  "missing_capabilities": ["payload_drop"],
  "unresolved_refs": [],
  "question": null
}
```

`decision` 只能是 `reject` 或 `clarify`。`reason_code` 为 `missing_capability`、`unknown_place`、`ungrounded_query`、`ambiguous_task`、`unsafe_request` 之一。澄清结果必须提供 `question`；拒绝结果的 `question` 必须为 `null`。拒绝对象不能同时包含 `goal`、`steps`、`report` 等任务计划字段。规划 schema 使用旧计划、reject、clarify 三个互斥的闭合对象分支（`anyOf`），适配 llama.cpp 的约束解码。

## 机载与地面站

机载 host 先严格校验决策对象。`reject`/`clarify` 不会创建返航兜底计划，也不会初始化或驱动 `MissionExecutor`。结果写入现有 `mission_refused` 记录，字段 `decision` 内含原因、缺少能力、未解析引用和澄清问题；记录沿用已有 operator link、分片、认证和 HMAC 流程。飞机处于空中时不新增动作，继续使用既有安全策略。

地面站任务面板读取 `mission_refused.decision`，显示拒绝原因、`reason_code` 和需要澄清的问题。Linux 环境不保证 QGroundControl/C++ 工程可编译；负责人应在 Windows 的现有 GCS 构建环境编译 custom 插件并通过任务包审批 UI 验证：收到拒绝时不出现可批准的执行计划，收到澄清时显示问题文本。

## 评测与数据

`plan_from_task.py` 将带合理 `reason_code` 的拒绝/澄清计为正确拒绝。`build_plan_sft.py --r26-recipe` 在 r24 基础上加入能力不足、未知地点、无依据查询和含糊任务的结构化目标，并保留具备 `payload_drop` 时的 `deliver` 正例；r26 不使用 r25 的 `rejected_candidate` 负目标。训练准入会严格校验决策字段和标签一致性。`run_planner_round.py` 透传 `--r26-recipe`。数据生成只写入 `.tmp/r26-data/`，不执行训练。
