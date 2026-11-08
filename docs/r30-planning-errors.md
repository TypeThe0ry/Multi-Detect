# r30 规划失分归类

本次离线分析只读使用 `artifacts/evaluation/r24-with-pregate/` 的缓存。planning-v3 为
80 题（主机预检 8、模型处理 72），planning-v4 为 20 题（预检 8、模型处理 12），
planning-v5 为 126 题（预检 56、模型处理 70），返航 probe 为 15 题。v5 的 33 个
模型失分按每题一个首要原因归类如下；预检正确拦截的能力/未知地点题不作为模型错误
重新训练。

v5 按题目类别的模型失分为：`query_ungrounded` 11/14、`return_negated_land` 7/14、
`query_grounding` 6/14、`ref_alias` 5/14、`return_intent` 4/14；其余四个类别由 host
pregate 处理，模型没有失分。

| 首要家族 | v5 失分 | 说明 |
|---|---:|---|
| 禁止范围仍生成额外动作 | 11 | `query_ungrounded` 题把被明确排除的查询保留成可执行任务；这是额外动作，不复制为拒绝目标。 |
| 漏掉中间动作 | 11 | `goto`、`inspect`、`record` 等任务中已有的动作被吞掉，返航尾步通常仍在。 |
| 技能替换 | 6 | `record`/`inspect`/`patrol`/`area_search` 等相近技能被互换。 |
| 未落地引用 | 3 | 计划使用未知引用或因此返回空计划；只允许复制提示中的引用。 |
| 终步混淆（含漏返航） | 1 | “不要返航、就地落地”被反转成 `return_home`。 |
| 额外导航步骤 | 1 | 路线任务前多出不需要的 `goto`。 |
| **合计** | **33** | 与 v5 的模型失分数一致。 |

nightly ext-2 的 follow 失败是另一条直接证据：套件要求 `[follow, return_home]`，
r24 实际给出 `[area_search, follow, return_home]`，前置搜索耗尽了 180 步预算。因此
r30 单独加入“目标已经明确时首步必须是 `follow`、计划中不出现 `area_search`”的正例族。

r30 只继承 r24 的 replay、v3 覆盖和引用/当前位置正例；不继承 r25-r29 的拒绝目标或
大规模对照。新增四族均为可执行正例，默认 240 行，中文/英文各半：

| r30 family | 默认行数 | 针对的证据 |
|---|---:|---|
| `direct_follow` | 80 | follow 前置额外搜索；首步固定为 `follow`，终步为 `return_home`。 |
| `step_fidelity` | 80 | 漏掉 `goto`/`inspect`/`record` 或相近技能替换。 |
| `terminal_intent` | 40 | 明确区分就地 `land` 与 `return_home`，保留前序检查动作。 |
| `grounded_refs` | 40 | 别名和标准名都解析到提示中给出的同一 `poi:`，不发明引用。 |

生成器对所有 `plan-eval-tasks*.json`（覆盖 v3/v4/v5）、返航 probe 和
`suite-follow*.json` 的任务文本做 `normalize_task` holdout。r30 manifest 和统计文件记录
上述来源及碰撞数；本轮数据生成的泄漏碰撞应为 0。
