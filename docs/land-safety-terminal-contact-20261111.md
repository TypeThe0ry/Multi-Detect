# land 任务终端触地的 safety_pass 修复（2026-11-11）

## 问题

`scripts/score_onboard_loop.py::_flight_safety_metrics` 用 `ground = min(alts) <= 1.0 m` 判定地面接触，
`safety_pass = not ground and ...`。最后一个技能是 `land` 且正常着陆的任务，终端触地必然使
`min(alts) <= 1.0`，因此所有含 find-suv-land 的 report.json 都是 `safety_pass=False`（评分伪影，
来源：`runs/candidate-8c630-nonpass-analysis-20261111.md` 第 2 项）。

## 改动

新增 `_terminal_landing_assessment`，仅当同时满足以下条件才启用，否则返回 `None`，走原有整段飞行规则（逐字节不变）：

- `mission_completed` 为真，计划最后一个技能是 `land`，且历史最后一项是 `land` 且 `succeeded`；
- 能在 `labels.onboard.skill == "land"` 的末尾连续行中找到 land 起点。

启用后把飞行分成两段：

- 着陆前：land 起点之前所有行的位姿，加上起点行的 `obs_before`。其最低高度 `<= ground_contact_altitude_m`
  仍算地面接触（`ground_contact_detected=True`，`safety_pass=False`）。
- 着陆段：起点行 `obs_after` 起的位姿，终端触地不计为接触，但必须同时满足：
  1. 最后一行 `flight_state.landed_state == 1`（ON_GROUND），缺失或其它值都失败；
  2. 触地窗口（高度 `<= touchdown_window_m`，默认 3 m）内的最大下降速率 `<= touchdown_max_descent_mps`。
     速率取 `velocity[2]`（NED 向下为正）与相邻位姿按 `pose_time_boot_ms` 的有限差分（间隔 >= 0.2 s）的较大者；
     窗口内没有任何速率数据时视为失败。

下降速率上限默认 1.5 m/s。依据：现有安全配置里没有下降速率阈值；4 个真实 episode 在 3 m 以下的最大下降速率为
0.706 至 0.715 m/s（对应 PX4 默认着陆速度约 0.7 m/s），1.5 m/s 约为其 2 倍，既不会误伤正常着陆，
也能挡住明显的硬着陆。可在任务的 `safety`/`expect.safety` 中用 `touchdown_max_descent_mps`、
`touchdown_window_m` 覆盖；未改任何任务期望、suite 文件和其它阈值。

`min_relative_altitude_m` 门限在 land 模式下只对着陆前的最低高度判定（否则终端触地使其永远不可达）；
未配置该门限时无影响。

land 模式下 `ground_contact_detected` 表示着陆前接触；报告新增字段（仅 land 模式出现）：
`terminal_landing_assessed`、`landing_pass`、`landing_on_ground_confirmed`、
`landing_touchdown_max_descent_mps`、`landing_touchdown_descent_limit_mps`。非 land 任务的报告结构和取值不变，
`negative_task` 对 `safety_pass` 的依赖（约 L1010）保持原语义。

## 验证

- 单元测试（tests/test_score_onboard_loop.py）：正例（正常着陆通过）；反例：着陆前触地失败、硬着陆失败
  （含阈值可配置）、无 ON_GROUND 确认失败、land 未完成/不是最后一步/无 land 标记时回退旧规则、
  非 land 任务低高度仍失败。
- 回放 `runs/clock-transport-candidate-8c-after-enospc-20261110/regression`（只读）：

| 任务 | episode | 旧 safety_pass | 新 safety_pass | 触地最大下降速率 m/s |
|---|---|---|---|---|
| find-suv-land (main) | 45180376 | False | True | 0.706 |
| find-suv-land (main) | 497cfad5 | False | True | 0.714 |
| gimbal-find-suv-land | 2eb7c4be | False | True | 0.715 |
| gimbal-find-suv-land | b2b9d44a | False | True | 0.707 |

  同批其余 19 个任务共 38 个非 land episode，新旧 `_flight_safety_metrics` 返回的整个字典完全相等。
- CPU gate：`scripts/run_onboard_gate.py -q -p no:warnings` PASS。

## 局限

回放时用空 truth 计算任务指标（安全判定只依赖 mission 历史，不依赖 truth）；未覆盖着陆后反弹、姿态与 disarm 心跳。
