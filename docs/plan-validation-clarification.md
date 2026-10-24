# Prompt→任务计划校验与澄清

## 语料观察

离线只读检查了 `r16-round-eval-planning`、`r17-round-eval-planning`、`r18-round-eval-planning` 和 `pipeline-eval-planning` 的 `planning-v3.json`，每组 80 条。r14 基线样本中，未知 skill 为 0；r18/r17/r16/pipeline 的有效率分别为 1.000/0.9625/0.9875/0.9875，精确步骤序列率为 0.6125/0.5375/0.575/0.6875。逐行复核得到的结构问题为：悬空步骤边分别 2/1/0/0，缺少返航且末步不是 land 分别 0/2/4/1，无效结构或步骤数错误分别 0/3/1/1。`altitude_m`、区域坐标和禁飞区样本均为 0，因此几何类统计不能从真实语料推断，使用合成边界测试覆盖。

主要语义偏差是提示要求与 success path 不一致，例如要求“找紫色箱子并回来”却省略 record、要求拍摄天线却生成 area_search、要求 inspect 校车却 land。`follow` 缺目标描述在这四组中为 0。时长字段出现 15–900 秒，必须结合任务包 time/energy budget 判断，不能只按一个固定秒数拦截。

## 决策规则

| 决策 | 条件 | 地面站行为 |
|---|---|---|
| accept | 结构、引用、步骤图、意图和任务包约束全部通过 | 允许进入已有执行路径 |
| repair | 高度仅轻微超界且可钳制；缺少返航收尾且可唯一补边 | 记录每处修改后执行修复后的计划 |
| clarify | 数量词与 `count.max_count` 不一致、目标类别有多个解释、区域/跟随目标不明确 | 显示 1–3 个中文问题，等待 operator/GCS 确认，禁止起飞 |
| reject | 未知技能/能力、作业区外、进入禁飞区、预算超限、不可达终点或结构不合法 | 记录拒绝原因；飞机不起飞，空中任务由 operator link 决定 hold/return |

已有 `validate_mission_plan` 继续负责 schema、参数范围、引用和图无环/终结；`check_plan_against_package` 继续负责 operating_area、禁飞区、包络高度、能力和时间/能源预算。本层只做纯函数编排、轻微修复和中文决策，不复制这些规则。

## 示例

* `goto poi:gate` 后没有终点：补 `return_home`，返回 `repair`，报告包含“缺少安全返航收尾”。
* 高度 62 m，任务包络 5–60 m：钳制为 60 m，返回 `repair`；高度 70 m：返回 `reject`。
* “统计停车场内所有锥桶”但步骤设置 `max_count=12`：返回 `clarify`，问题为“是否移除上限或指定具体数量”。
* 区域或航线与禁飞圆相交：返回 `reject`，中文原因为“任务区域或航线进入禁飞区”。
* 通过的正常计划：返回 `accept`，计划对象和步骤不变。

## GCS 交互

`validate_plan_for_execution(plan, package, prompt)` 是无副作用的 admission 函数，输出 `decision`、`reasons_zh`、`questions_zh`、`repairs` 和可执行的 `plan`。`scripts/onboard_policy.py` 在构造 `MissionExecutor` 前调用它，并将结果写入 `mission_plan_report.validation`。`clarify` 和 `reject` 通过现有 `mission_refused` 记录并抛出，因而不会到达起飞/执行入口；operator link 可以据报告选择补充任务后重新提交，空中任务则由既有 hold/return 策略接管。

修复记录使用中文描述且与原报告的 `repairs` 合并。正常 accept 路径不改变既有计划，也不改变已有 evaluator 的通过行为。
