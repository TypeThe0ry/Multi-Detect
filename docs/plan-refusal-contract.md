# 规划拒绝与澄清契约

## 为什么需要

规划模型以前只能返回 `MissionPlan`。当任务要求飞机没有的能力（例如投放载荷），解码约束会排除 `deliver`，模型可能改写成另一个可执行任务。飞机随后会执行与操作员意图不同的任务。启用拒绝契约后，规划结果可以明确拒绝或请求澄清；不确定或拒绝时的安全策略仍是“不执行该任务”。

## 开关与默认值

拒绝契约默认关闭。关闭时，提示词和 `plan_json_schema` 保持 legacy `MissionPlan` 的字节级格式：不包含顶层 `anyOf`，也不包含拒绝/澄清说明或示例；机载和评测都沿用旧路径。这是因为尚未学习拒绝格式的现役 r24 模型在新契约提示下出现回归：planning-v3 从 `0.7375` 降至 `0.6875`，`unknown_place` 从 `2/2` 降至 `0/2`，`query_ungrounded` 从 `0/2` 变为 `1/2`，返航探测仍为 `15/15`。

机载配置键为 `planning_decision_contract`，未设置等同于 `false`。评测脚本 `plan_from_task.py`、`run_model_round_eval.py` 和 `run_planner_probe.py` 使用 `--decision-contract` 开启。`run_planner_round.py` 在 `--r26-recipe` 时自动同时为候选模型和基线开启；可用 `--decision-contract` 或 `--no-decision-contract` 覆盖。

只有 r26 或之后的轮次在替换门槛中验证通过，并且模型已经学习了拒绝/澄清格式后，才应在推荐机载配置中打开该键。

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

## 地面站验证步骤（Windows）

1. 在仓库根目录打开 PowerShell，确认 Qt、Visual Studio/MSVC 和地面站依赖已按现有 Windows 环境配置，然后执行：

   ```powershell
   .\scripts\build_multidetect_windows.ps1 -Configuration Debug -BuildSelfTests
   ```

   若已有匹配配置的 CMake 构建目录，可按脚本约定追加 `-SkipConfigure`；构建输出应无 C++/QML 错误。

2. 启动构建出的 QGroundControl，连接一套 SIM operator-link 测试链路。用 `scripts\run_sim_session.py --operator-link` 建立本地机载会话，再按 `scripts\fake_gcs_console.py --help` 运行仓库的假 GCS 控制台上传签名任务包；测试配置须开启 `planning_decision_contract`，并让测试 planner 返回下列拒绝/澄清 JSON。`fake_gcs_console.py` 是地面站端客户端，不负责伪造机载下行；拒绝事件应由 SIM 机载策略通过现有 mission log/downlink 发回：

   ```json
   {"plan_version":1,"decision":"reject","reason_code":"missing_capability","reason":"Aircraft has no payload drop capability.","missing_capabilities":["payload_drop"],"unresolved_refs":[],"question":null}
   ```

   澄清用例将 `decision` 设为 `clarify`、`reason_code` 设为 `ambiguous_task`，并提供非空 `question`。

3. 在任务面板的机载日志中确认记录标题显示为“任务规划已拒绝”，正文显示 `reason_code`、原因和（clarify 时）问题文本；字段缺失、`question: null` 或空数组时面板仍可打开，不崩溃。

4. 先让面板收到一个带 `requires_approval: true` 的旧计划，再发送同一任务的 `mission_refused`。确认旧计划和“批准计划”按钮被清除，不可批准拒绝的任务；收到 `clarify` 时只显示澄清文本并提示修改任务后重新提交。

5. 用一条 `decision: "reject"` 且 `question: null` 的拒绝记录和一条 `decision: "clarify"` 且非空 `question` 的记录各验证一次。中英文 `reason` 均应原样保留显示；必要时展开原始审计字段核对 JSON 键名和嵌套层级。

## 评测与数据

`plan_from_task.py` 将带合理 `reason_code` 的拒绝/澄清计为正确拒绝。`build_plan_sft.py --r26-recipe` 将应拒绝或澄清的 assistant target 统一设为结构化 `decision`；r24 未知地点和无载荷能力的 deliver 也按此转换，原计划只保存在 `rejected_candidate` 审计字段中。未启用 r26 时，r23/r24 生成路径保持原样。

r26 的 `--r26-refusal-count` 表示每个 `reason_code` 的拒绝/澄清行数，默认 60；同类会生成等量、逐行通过 validator 与意图检查的可执行正例。无继承数据时五类各 60+60，共 600 行；若 r24 默认的 80 条未知地点拒绝被继承，该类按 80+80 配平，总新增 640 行。默认 60 是便于训练中形成稳定类别信号、同时将新增数据控制在完整 5,500 行级别数据集约 11% 的折中；中英文各半。正例涵盖能力具备时的 deliver、已知地点的检查、查询有据和位置明确的任务，以及安全区域内的正常搜索。

模板通过有限笛卡尔组合枚举，并按归一化任务文本去重、排除 planning-v3/v4 评测题；若可用唯一模板不足以满足请求，生成器会报错退出，不会无限重试。准入检查 decision 字段、标签一致性、保留候选的 finding 和正例意图。`run_planner_round.py` 透传该数量参数。生成数据写入指定输出目录（例如 `.tmp/r26-data/`），本配方不执行训练。

本次按 v4 完整配方实际生成到 `.tmp/r26-data-final6/`：train 6,020 行、val 100 行。r26 共 640 行：`missing_capability`、`ungrounded_query`、`ambiguous_task`、`unsafe_request` 各 60 条拒绝/澄清与 60 条正例；`unknown_place` 包含继承的 80 条 r24 结构化澄清与 80 条正例。训练准入接受全部 6,020 条；模板生成阶段的正例 validator 和意图检查均通过。未启动训练。
