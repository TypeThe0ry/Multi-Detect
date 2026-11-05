# find-suv-land 的 land 完成记录分析

## 结论

`nightly-linux-r21idr3-r24plan-19` 中目标确认成功，但 episode 没有记录 `s9` 落地完成。最后一条观测显示飞行器仍在空中；它只证明采集终止时尚未观察到落地，不能证明之后实际没有落地。现有证据更符合“落地命令发出后，运行时立刻结束任务，因此落地完成没有被记录”。摘要将 `s9` 标为 `unknown` 是忠实反映缺失的完成记录，不是把一个已记录的落地成功误判为失败；`mission_completion_rate: 0.0` 表示本次 mission 步骤尚无完整成功记录，而基于目标确认的 `success: true` 是另一项指标。

这不是适合通过摘要层窄修补的缺陷。执行器的 land 等待协议、运行时对 `stop` 的解释，以及落地后的遥测确认与 episode 结束边界需要一起调整。此处记录设计问题，不改执行器或测试，也未运行 SITL。

## 本次 episode 证据

目标识别与任务完成是两个不同的结果。`report.json` 第 25–37 行记载该 episode `confirmed: true`、`success: true`；同一报告第 22 行的 `mission_completion_rate` 为 `0.0`。episode 的 `mission_summary.json` 将 `s1`、`s2` 记为 `succeeded`，但 `s9` 为 `unknown`，并在 `not_done` 中给出 `no_recorded_completion`。

`steps.jsonl` 一共只有 7 条，最后一条是 `step_index: 6`。在这条记录的 `labels.onboard` 中，状态是 `phase: landing_commanded`、`step: s9`、`step_state: running`；`mission.history` 只包含 `s1` 和 `s2`。最后观测的 `obs_before` 与 `obs_after` 都是 `landed: false`、`flight_state.landed_state: 2`（空中）。动作是 `stop`，其 `outcome.status` 为 `success`，但该成功只表示动作调用成功，不代表落地完成。episode 的 `episode.json` 声明 `step_count: 7`、`status: complete`，这里的 complete 是采集 episode 已封存，不代表任务计划的每一步均已完成。

## 代码因果链

- [mission_executor.py](../src/multidetect/autonomy/mission_executor.py#L3696) 在收到 `landed is True` 时才以 `landed` 原因完成 land 步骤。若仍在空中，它第一次只设置 `landing_commanded` 并返回 `ActionPrimitive.stop()`（第 3706–3709 行）；后续调用才会返回 `landing_pending`，或按等待步数超时（第 3710–3717 行）。因此要记录最终落地，执行器必须在落地命令之后继续获得观测并再次执行。
- [runtime.py](../src/multidetect/onboard_runtime/runtime.py#L975) 将 `stop` 解释为 mission 完成，并立即发出配置的 `completion_action`；普通动作分支也在第 993–996 行对 `stop` 设置 `mission_complete`、发出完成动作并返回。默认 `completion_action` 是 `land`（第 40 行）。在 mission executor 用 `stop` 表达“落地正在开始/仍待确认”时，runtime 因而把中间动作当作终止信号，不再给 executor 后续观测。
- [mission_summary.py](../src/multidetect/autonomy/mission_summary.py#L74) 只从 executor history 生成计划步骤状态。没有 `s9` 的 history 记录时，状态保留为 `unknown`，并产生 `no_recorded_completion`（第 75–81 行）。该摘要逻辑没有丢弃任何已记录的 landing-success 事件；history 中根本没有这类事件。
- [eval_onboard_loop_campaign.py](../scripts/eval_onboard_loop_campaign.py#L490) 依据 episode 行的 `mission_completed` 字段计算 mission 完成率。因此目标确认成功率为 1.0 与任务完成率为 0.0 可以同时成立。

这也说明应避免把“调用 `land()`/发出 landing 命令成功”记成“车辆已落地”：完成判据必须来自后续可验证的 landed 状态。设计修复应让 runtime 区分“当前动作停止/不移动”和“任务终止”，并让 land 步骤在收到落地确认或有明确的超时结果前保持可继续观测；回归测试应覆盖空中命令、后续落地确认以及未确认超时三条路径。

## nightly 对照：最早可见时间

只把带有 episode `steps.jsonl` 的产物作为执行行为证据。现有 `nightly-linux-r21idr3-r24plan-*` 中，最早可读取到这一 land 截断模式的是 `-5`：7 条记录以 `landing_commanded` / `running` 收尾。`-7`、`-16`、`-17` 与 `-19` 的 episode 也有相同收尾；其中 `-5`、`-16`、`-17`、`-19` 的报告任务完成率均为 0.0。`-7` 虽有 episode 步骤记录，但报告统计没有完整 episode 可计，因此不据它推断完成率。`-1` 至 `-4`、`-6`、`-8` 至 `-15` 在该场景下没有可用于判断该行为的 episode 步骤记录，故只能说“最早可见于 `-5`”，不能据此断言代码问题恰在 `-5` 才引入。

## 运行来源与集成分支对照

episode `episode.json` 的 provenance 标明采集源码 worktree 为 `/data/projects/Multi-Detect-env/runs/src-nightly-h9`，提交 `ff8812eccbe54b3528a398718091019aceeb73e5`（`ff8812ec`），且该 worktree 当时为 dirty、记录了 2 个 changed files。当前集成 worktree HEAD 是 `cabfc2b27dd61a472a3093914460fdfb1cd064e8`。

对本问题涉及的 `mission_executor.py`、`mission_summary.py`、`runtime.py`、`tests/test_mission_executor.py` 和 `tests/test_mission_summary.py` 做 `ff8812ec` 到集成 HEAD 的路径差异检查，没有发现差异；本次的停止/完成语义在两边相同。源 worktree 的 tracked diff 在 `scripts/onboard_policy.py`：增加并逐帧更新搜索相机位姿 `_search_camera_pose`，与 land 完成协议无关。源 worktree 另有未跟踪的 `mobileclip_blt.ts`；它不属于已提交路径比较，episode provenance 的 dirty 标志也意味着不能把该次运行完全等同于干净的 `ff8812ec` 快照。

## 修改与验证

本次只新增本分析文档，没有代码改动或新增测试。检查使用已有 episode、报告和只读源码/提交差异；没有启动 SITL，也没有在 `artifacts/` 中写入内容。
