# Nightly ext-2 失败分析（2026-11-03）

本报告只读检查共享产物
`artifacts/benchmarks/nightly-linux-r21idr3-r24plan-ext-2/`，对应代码提交
`d6839660`、r21id-r3 identity / r24 planner、每个 slot 10 核 cpuset。
所有 episode 均完成，没有基础设施失败。本次没有改动 artifacts、套件期望或生产代码。

## 结论

| Mission | 直接失败 | 分类 |
|---|---|---|
| `follow-red-person-return` | 计划多出 `area_search`；它耗尽 step budget，follow 未开始 | r24 planner 额外插入技能的模型错误；budget 是后续执行结果，不是 pregate 拒绝 |
| `rc-cessna-w0016-ambulance-known-limit` | 仅 `confirmed` 未达成 | 本次搜索轨迹没有进入目标确认距离，不能归因于身份模型错误；`confirmed: true` 仍有历史 SITL 证据支持 |
| `rc-cessna-w0031-ambulance-negative` | `min_safety_pass` 中返航距离子项失败 | scorer 使用最后一条机载步骤 pose；该值不同于 campaign 的 RTL 近家遥测。没有证据表明 landing-wait 改变搜索路线或触发失败 |

## Follow

套件期望严格技能序列 `[follow, return_home]`。ext-2 的
`mission_summary.json` 和评分报告实际记录 `[area_search, follow, return_home]`：
r24 多规划了一个 `area_search` 前置步骤。其后没有 pregate 或 intent rejection
记录；这是 planner 输出不符合套件期望的证据，不是 validator 把 `follow` 拒绝或替换。

该搜索步骤以 `step_budget=180`、`runtime_step_limit=180` 执行。步骤结果为
`s1 area_search=failed/step_budget`，之后沿失败边进入返航；`s2 follow` 为
`unknown/no_recorded_completion`，`s3 return_home=succeeded/arrived`。
176 条 step 记录中，搜索阶段没有可用目标检测；只有返航阶段出现一次检测。
因此目标在框比例、移动距离、跟踪连续性和重获时间均没有 follow 轨迹可评分，
不能把这些指标单独解释成 follow 控制器退化。预算规则给 `area_search` 留有
返航尾部余量，但不保证额外规划出的搜索步骤能在 180 步内找到目标并进入 follow。

Windows-era 对照有限：`a0027ea7` 于 2026-10-24 新增原始
`suite-follow.json`，定义 `[follow, return_home]` 和同一组跟随门槛；
`docs/e2e-operator-sim.md` 记录 Windows PowerShell 运行方式。仓库未找到该
suite 的 Windows campaign/episode 结果，故不能据此声称 Windows 曾通过。模型切换
配置由 `21a54889` 复制出 r21id-r3/r24 版本，expect 未改。`docs/r29-planning-errors.md`
也记录过 planner 在多步任务中漏掉或替换步骤，但不是本任务的 Windows 实跑证据。

## Cessna w0016 known-limit

ext-2 计划为 `area_search → return_home`，mission 完成且安全项通过，但
`confirmed=false`。该 episode 只有 7 条记录、0 次慢身份调用、0 条 track，
没有 r21id-r3 identity decision。救护车真值约为 N/E `(-54.98, -85.89)`，
搜索轨迹到目标的水平距离约 179–273 m；配置 `confirm_max_range_m=40`。
本次证据说明的是搜索几何没有到达确认范围，无法用它评估 identity model。

历史上将 `confirmed` 设为 true 有真实运行依据。`docs/fixed-wing-search-timeout-20261025.md`
记载此前 known-limit 曾设为 false；2026-10-25 的 fixed-wing SITL 复验在修复
固定翼盘旋/覆盖完成判定后，于第 8 步确认目标（位置误差 0.43 m），随后返航。
`20da8505` 记录了这次确认并恢复 `confirmed: true`；后续 `d99d0050` 复验也记载
w0016 确认成功。当前套件期望因此仍有正向历史证据，不应因 ext-2 的一次远距离
搜索轨迹改成 false。

ext-1 同名 campaign 中 w0016 实际有 31 条记录并出现 identity 决策：早期小框
候选收到 `confirm_target` 提案，后续多次 `approach_track`/`confirm_target`，
同时也有 `reject_track`；这些决策来自约 12–63 px 的候选框（实际轨迹框在
步骤记录中可复核），飞机位置与目标仍相隔数十米，最终没有 `confirmed`。
这些记录说明 r21id-r3 在小目标/远距离观察下存在确认限制，但 ext-2 本身没有
此类判决。不能仅据模型 verdict 推断范围是唯一原因：框尺寸、观察角度、目标
分辨率和几何搜索位置都会影响确认质量。

## Cessna w0031 negative

ext-2 唯一失败期望是 `min_safety_pass`。报告的高度门通过：最低相对高度
28.210 m、最高 30.866 m、未检测到地面接触；失败子项是返航距离，记录为
343.128 m（门槛 300 m）。mission summary 表明 area_search coverage complete、
return_home autopilot_return；campaign log 记录 RTL 到 near-home 时约 91.978 m。
逐步 safety labels 均 `accepted`、`reasons=[]`，没有 altitude floor 或 geofence
拒绝。

`scripts/score_onboard_loop.py::_flight_safety_metrics` 以 `steps.jsonl` 最后一条
记录 pose 计算 `return_home_distance_m`，不是读取 campaign 的 RTL 终点遥测。
ext-2 最后机载步骤 pose 约为 N/E `(-278.42, 45.75)`，距 home 343.128 m；RTL
完成后，campaign 的终点遥测又靠近 home。因此报告里的失败是 scorer 采用机载步骤
终点，而 mission 的后续自动返航没有进入该距离指标。

ext-1 同 mission 有 44 条 step 记录，报告 `safety_pass=true`，返航距离
190.742 m，高度门也通过。ext-2 只有 12 条记录，area_search 仍报告
`coverage_complete`，但最后记录位置更远。这是两次搜索轨迹/episode 记录终点不同，
造成 scorer 距离子项不同；现有证据不能把差异归因于 landing wait。

`d6839660` 合并的 `7806c2c6` 增加固定翼/VTOL 降落等待：在策略停止后等待 PX4
仿真时钟下的新鲜落地遥测，并保留墙钟硬上限。ext-2 campaign 确认 RTL/landing
完成。等待发生在 onboard policy 已停止之后，不会改变最后一条 `steps.jsonl`
记录的 pose，也没有迹象显示它把 area_search 提前结束或使飞机偏离搜索路线。
因此本项暂不修改 scorer 或 suite 门槛；若 safety 的预期语义要覆盖 collector
后续 RTL，应另行定义并测试统一的返航终点来源，不能以本次报告单独推翻现有口径。

## 处理

- 不改 suite expectations：follow 失败是 planner 多插入 `area_search`，w0016 的
  `confirmed: true` 有历史实跑支撑，w0031 失败是当前报告取值口径与后续 RTL 状态
  不同。
- 不改生产代码：本次未发现能够由 ext-2 证据确认的代码缺陷，因此没有新增代码测试。
- Windows 对照仅能确认 suite 定义和运行文档存在，缺少该 follow mission 的
  Windows 实际结果。
- artifacts 全程只读；未运行 SITL、Docker、GPU 任务或网络下载。
