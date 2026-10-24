# 飞行中改派设计

改派由已认证的地面站任务包触发。机载先记录 `retask_received`，在安全保持位异步规划；请求立即返回“已受理”，规划线程超过超时即记录 `retask_planning(timeout)` 和 `retask_rejected`，原计划继续执行。候选计划沿用任务包准入校验、引用校验、禁飞区和高度校验，并检查剩余时间/能量加返航储备。

状态机为：`received -> planning -> validated -> awaiting_approval -> queued -> switched`，任何阶段都可以进入 `rejected`；规划期间 `abort`、`pause`、链路失联和安全出口优先级不变。规划触发的 onboard 模型切换标记为计划内切换，暂停身份判断和慢模型健康升级，飞机按原计划保持安全行为；切换失败清除标记并按慢模型失效保护处理，改派被拒绝。`append` 在当前图走到终端后启动新计划，`replace` 在当前步骤完成后丢弃剩余步骤并启动新计划。切换是执行器锁内的单次提交，失败不会改动原计划。

| 决策 | 条件 | 结果 |
| --- | --- | --- |
| 接受 | 签名、计划、包络、预算均通过 | 排队并切换 |
| 审批 | 候选要求审批 | 等待 `approve_retask`，摘要保留原计划 |
| 拒绝 | 规划超时、校验失败、包络扩大、能量/时间不足、审批超时或拒绝 | 原计划不变，记录中文原因 |

每次改派审计至少包含 `retask_received`、`retask_planning`、`retask_validated`、`retask_approval`、`retask_switched` 或 `retask_rejected`，以及 `mode`、`plan_version`、`plan_sha256`、中文 `reason`。执行器的计数、发现、报警和步骤历史不清空；新步骤历史带 `plan_version`。最终 `final_summary.plan_history` 列出所有版本及切换模式。

当前地面站上传协议没有改派模式字段，飞行中地面站入口固定使用 `replace`。`append` 仅供机载 API 调用；需要地面站 append 时应先升级协议并同步本文件。
