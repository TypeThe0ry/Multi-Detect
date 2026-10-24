# 机载决策离线 A/B：2026-10-24

## 范围与方法

比较基线 `a671ad99` 与当前集成 `e49b1248`，输入为指定的五个 SITL benchmark 目录及 `D:\Projects\mdwt\_scratch\vtol-smoke` 中所有含 `steps.jsonl` 的 episode。共覆盖 61 个 episode：queryfirst 9、hydrant10 10、lookgeometry 12、fullsuite 14、patrolrecall 10、固定翼/VTOL 6。每个 episode 用 `logged` 模式分别开环和半闭环 compare，共 122 次比较；均成功。没有启动 SITL、Docker 或模型服务。

命令语义遵循 `docs/onboard-episode-replay.md`：开环使用每行原始 `obs_before`，半闭环把日志位姿回灌到下一步。结果 JSON 保存在 `D:\Projects\mdwt\_scratch\cx-replay-ab\results`，按 `episodes.txt` 的 0-based 序号命名。

## 汇总

| 模式 | 比较数 | 首分歧 | 分歧字段 | slow-call/计数/告警/补看概况 |
|---|---:|---:|---|---|
| 开环 | 61 | 0 | 无；全部 trace 一致 | 各字段相同 |
| 半闭环 | 61 | 12 | 均只有 `action` | calls、counts、alerts、revisit_plan 在首次分歧步仍相同 |

半闭环 12 个首分歧都在当前版本触发 `count_rejection_revisit`，动作类型两侧同为 `turn`，差异是转向角参数。证据如下（序号对应 `results/episodes.txt`）：

| 序号 | Episode | 首分歧步 | 当前版本上下文 |
|---:|---|---:|---|
| 3 | `a337f041` | 59 | 小目标 `t6` 拒绝补看，目标 `[26.81, 22.28]` |
| 4 | `b188e816` | 66 | 小目标 `t6` 拒绝补看，目标 `[7.58, 23.66]` |
| 5 | `d1da46f0` | 93 | 小目标 `t25` 拒绝补看，目标 `[1.82, 25.07]` |
| 11 | `3314986c` | 64 | 小目标拒绝补看 |
| 12 | `47a42869` | 65 | 小目标拒绝补看 |
| 14 | `7166ed2b` | 73 | 小目标拒绝补看 |
| 15 | `81d4110a` | 81 | 小目标拒绝补看 |
| 16 | `83f0ad64` | 70 | 小目标拒绝补看 |
| 17 | `ac3bb412` | 72 | 小目标拒绝补看 |
| 18 | `f517d76c` | 70 | 小目标拒绝补看 |
| 23 | `3121ac38` | 62 | 小目标拒绝补看 |
| 35 | `2a54bd04` | 88 | 小目标拒绝补看 |

### 归因

这些差异属于已知行为改动，不是安全拒绝：当前 detail 明确标注 `count_rejection_revisit`，动作是有界角度的 `turn`，没有 `safety` 信息，也没有转为 `hold`/`stop`。其出现与基线之后对补看候选排序、快照和计数预算的改动同域；比较器记录的变更字段只有 `action`，补看计划及计数在该步一致，因此不能把首个 yaw 参数差异进一步唯一归因到其中某一个提交。它反映的是同一补看过程中的姿态/航向调整参数不同。

半闭环其他补看轨迹也有差别，例如若干 episode 当前重放在 52、61、72、76、85 步产生补看；这些属于单侧轨迹事件，未改变该步的 compare 字段集。重放只用日志执行后位姿，不模拟动作导致的物理位姿变化，后续轨迹不能视为闭环飞行结果。

## 统一安全出口筛查

### 本次复验（cx-replay-safety）

重放器现在默认按机载路径创建 `AirframeMotionAdapter` 和同一份
`SafetyEnvelope`：机型来自 `episode.json.airframe_profile.id`（普通架次回退
`x500_mono_cam`），机载 `onboard.json` 的 `mission_package`、显式
`safety_envelope`、任务包 `operating_area`/高度限制/禁飞区、任务包 home 和
episode 记录的绝对 `world_sdf` 都会合并；固定翼/VTOL 使用 profile dynamics 和
`max_step_distance_m`。开环与半闭环均把 `MissionExecutor.next_action()` 的结果
送入 `guard_command()`。每步 trace 的 `safety` 包含 status、reasons、
rejected、consecutive_rejections、guarded_action_kind 和
`changed_to_hold_stop`。`--disable-safety-exit` 可复现旧的旁路行为。

指定五个 benchmark（55 个 episode）和 `vtol-smoke`（6 个 episode）共 61 个
架次分别开环、半闭环重放，结果在
`D:\Projects\mdwt\_scratch\cx-replay-safety\all-open.json`、
`all-closed.json`，汇总在 `safety-summary.csv`。统计如下：

| 模式 | 架次 | 安全拒绝步数 | 主要原因 | 最大连续 hold/stop |
|---|---:|---:|---|---:|
| 开环 | 61 | 3281 | `min_altitude`、`altitude` | 135 |
| 半闭环 | 61 | 4348 | `min_altitude`、`altitude` | 136 |

逐步核对显示，拒绝均对应重放动作目标高度低于任务包最低高度（通常 5 m，或
当前高度已经低于该下限）或继续下降；原始 SITL 行的
`obs_before.pose.local_ned_m[2]`、`action.up_m`、`outcome.achieved_delta_ned`
可复核数值。未发现原始动作实际飞行正常且仍在包络内的明确误拒。固定翼/VTOL
初版统计曾出现每步 `altitude` 拒绝；根因是把首帧飞机位置误当 local-NED
home。已改为无任务包 home 时使用 `(0,0,0)`，并从 episode profile 读取机型；
修复后 `rc_cessna` smoke 开环拒绝为 0。旧行为 A/B 使用
`--disable-safety-exit`，只用于验证旁路开关。

原始 61 个架次的 `labels.onboard.safety` 没有可用的逐步安全字段；这正是本次
重放补上统一出口和 trace 的原因。旧 A/B 的“0 次安全拒绝”只代表旧重放未调用
出口，不能作为安全结论。

可确认的负面证据：122 条 replay trace 没有 safety rejection 字段分歧；12 个有差异步的动作都是 turn，没有出现由拒绝转成 `hold`/`stop` 的模式，未发现由这些分歧导致的原地反复 hold。样本内无可定位的 safety 拒绝步号。上述具体场景是否误拒仍是 SITL 复验前的未覆盖项，需带有 airframe adapter 和逐步 safety 元数据的架次日志才能判定。

代码侧改动位于 `af7b2cf6`（固定翼/VTOL envelope）及其后的集成 `e49b1248`（多旋翼 `ActionPrimitive` 经 SafetyEnvelope 检查）。已有定向测试覆盖区域边界、低高度拒绝、固定翼返航禁飞区绕行和搜索盘旋：`tests/test_motion_intent.py`、`tests/test_mission_executor.py`、`tests/test_fw_search.py`。这些单测验证规则本身，但不替代本次缺失的架次级 safety 观测。

## 结论与验证

离线 A/B 未发现明确代码 bug，本次只增加报告，没有修改运行时代码。定向验证：

```text
python -m pytest -q tests/test_replay_onboard_episode.py tests/test_motion_intent.py tests/test_mission_executor.py
123 passed
```

另行运行 `python -m pytest -q tests/test_fw_search.py`，9 passed。`run_onboard_gate.py` 未运行，因为没有代码修复；按任务约定，仅代码修复时要求执行完整 gate。
