# 规划评测一致性与缺载荷投递诊断

## A. query_ungrounded 实时评分与离线重算

根因是评分输入不同，不是任务引用或能力集合漏传，也不是 `validate_plan_for_execution` 缺少意图检查。

实时 `run_model_round_eval.py` 调用 `plan_from_task.py`。`compile_mission_plan` 会检查 raw 输出中的 query grounding；最后一次尝试会按 `repair_plan_intent` 删除不受任务支持的 `inspect` 步骤，返回只含 `return_home` 的修复计划。实时 `score_task` 原先只收到这个修复后的计划，因此执行 validator 对它给出 `accept`，而它也不满足“经 validator 拒绝/澄清”的无效题计分规则。

离线 `recompute_planning_v4_capabilities.py` 则从结果 JSON 的 `raw` 重新解析计划，再交给相同的 `score_task`。原始候选仍含 `inspect(query=救护车/fire truck)`；执行 validator 调用 `check_plan_intent` 后给出 `clarify`，所以正确拒绝计为通过。两边 refs 都来自 v4 任务定义，能力集合也按任务传入；query 检查发生在 `validate_plan_for_execution` 中。

修复后，`score_task` 对 `expect_invalid` 任务可按 raw 重新解析并评分；实时入口和离线重算入口都传入同一个 raw。修复计划仍用于正常编译/执行流程，但不能替代原始候选作为拒绝类题目的评分证据。新增回归测试覆盖“编译结果已删掉 inspect、raw 仍含不落地 query”的情况，并要求实时评分与离线评分字段相同。

用共享 artifacts 中的 r24/r25 candidate JSON 执行离线重算（只读、未覆盖文件）：两个版本的 `query_ungrounded` 都为 2/2，`intent_violation_rate=1.0`；两个版本的 `missing_payload_drop` 都为 0/2。实时评分已改为与 raw 重算同一入口，因此这两题在相同 raw 上会得到相同的 validator 决策与类别分数。

## B. missing_payload_drop 为什么没有学会

### 提示词对照

训练输入由 `slow_system_runtime.chat_messages` 直接发送数据行的 `prompt` 字段；模型监督目标来自 `assistant_target`。r25 的 40 条 `family=r25_missing_payload` 训练行中，提示词都由规划 prompt 模板构造，每行根据目标地点和语言变化。

评测提示由 `plan_from_task.py` 的 `plan_prompt` 构造。对中文题“把包裹投放到大门，但本机没有载荷投送能力。”，已知地点是 `poi:gate (大门 / gate)`，任务文本逐字一致。训练样例的 system/rules/examples/ref/task 结构与评测的默认模板一致；单行中的 task 和 refs 自然随样本改变。两边 `capabilities=[]`，因此均不输出 `Available aircraft capabilities` 说明段，也都从技能列表和 `plan_json_schema` 中排除 gated 的 `deliver`。所以主要问题不是提示模板或能力上下文格式不一致。

### 训练目标与解码约束

40/40 条 missing-payload 负例均为 `negative_target_mode="rejected_candidate"`。`admit_train_rows` 将 `rejected_candidate` 作为负例审计记录，核实它确实触发 `missing_capability`；但训练 token 标签仍由 `encode_response` 对 `assistant_target` 做 response-only 监督。该族的 `assistant_target` 就是带 `deliver` 的候选计划。训练目标因此是“输出 `deliver` 候选”，不是输出拒绝或安全替代计划。

这与评测时的受限解码相冲突：空能力的 schema 不允许 `deliver`，prompt 也明确要求不发出能力缺失的技能。模型无法按训练目标复现 `deliver`，于是可能生成 `goto -> return_home`、`follow -> land` 等 schema 可接受但没有交付的替代计划；validator 看不到 `deliver`，只能接受这些计划。评测题期望拒绝，而当前 mission-plan 输出格式没有显式 reject/clarify 决策，因此“正确拒绝”也没有一个与训练和解码均兼容的目标表示。

结论：首要问题是负例监督目标设计与评测解码契约冲突；提示格式不是主因。40 条样本数量有限，可能影响跨措辞泛化，但即使增加同配方样本，目标 token 仍被评测 schema 禁止，不能解决这个结构性矛盾。

### 建议配方

不要继续把 rejected candidate 直接用作该无效能力行的 assistant target。应先给规划输出契约增加明确的结构化 `reject`/`clarify` 决策，并让提示、解码 schema、host 处理和评分共同接受该决策；然后把 `rejected_candidate` 保留为审计用输入/标签，训练 assistant target 改为结构化拒绝及原因（如缺少 `payload_drop`）。这样训练的目标能在评测时生成，host 也能将它解释为拒绝。未完成这个契约改动前，不应通过放宽空能力 schema 让模型生成 `deliver`，因为这会让违反能力约束的动作进入可执行计划路径。

数据扩充应在契约对齐后进行：覆盖中文/英文、同义表达（包裹/药箱、投放/递送）、多个目标地点、不同能力列表写法；按地点/措辞划分 holdout，防止同模板近重复泄漏。保留有载荷能力的正例，验证同一任务在具备 `payload_drop` 时输出合法 `deliver`。40 条负例对这些变化的覆盖较窄，但数量不是当前 0/2 的首要解释。

## 验证

- `tests/test_plan_from_task.py` 与 `tests/test_plan_from_task_capabilities.py`：20 项通过。
- `scripts/recompute_planning_v4_capabilities.py` 对 r24/r25 candidate JSON 离线重算：两者 `query_ungrounded` 均 2/2、`missing_payload_drop` 均 0/2；源 artifacts 未写入。
- 未运行 GPU 训练；未修改任务配置、`plan_intent.py`、`capability_contract.py`、数据生成脚本或 `build_slow_system_sft.py`。
