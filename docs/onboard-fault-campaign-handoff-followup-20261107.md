# 通用 onboard fault campaign 修复交接（2026-11-07）

## 结论

通用 campaign 已从修复前 **1/16 通过、15/16 失败** 变为 **16/16 通过**；爬升失败 fixture 也通过。修复范围只涉及 `scripts/run_onboard_fault_campaign.py` 的 campaign 证据检查和场景配置，没有修改 `src/multidetect/onboard_runtime/runtime.py`、爬升阈值、LAND 优先级或 collector vehicle-loss 逻辑。

最终证据：

- campaign 汇总：`.tmp/current-campaign-final-verified/report.json`
- 每例 mission log：`.tmp/current-campaign-final-verified/runs/*/mission.jsonl`
- 爬升 fixture：`.tmp/current-campaign-final-verified/runs/climb-failure-1794055913015676160/fixture-report.json`
- 修复前汇总：`.tmp/current-campaign/report.json`
- 设计与限制依据：`docs/onboard-climb-land-followup-20261107.md`

## `gcs_heartbeat_recovery` 时间线核实

最终 run 的 `mission_started` 单调时钟为 `83251.763822661`，场景配置的恢复窗口从启动后 `0.05 s` 开始。启动初始决策在 `83251.771548784`，此时 `connected=false`；第一条 heartbeat 在 `83251.823838286` 到达，约为启动后 `0.060 s`，随后同一时刻的 recovery 决策已为 `connected=true`。之后 heartbeat 约每 50 ms 持续到至少 `83252.131730294`，没有 `escalation`、runtime handoff 或第三次返航动作。

修复前的 `.tmp/current-campaign/runs/gcs_heartbeat_recovery-1794054484132811956/mission.jsonl` 显示，恢复后 campaign 仍在每个策略循环发送 heartbeat，触发 supervisor 的 `20/10 s` rate limit；heartbeat 被限流后才出现 `operator policy silence/pause timeout` 和 RTL。这是 campaign synthetic source 的配置问题，不是生产 supervisor 的恢复逻辑缺陷。campaign supervisor 现在使用专用 `rate_limit=1000`，只避免测试源洪泛限流。

## 逐例诊断与修复前后结果

| 场景 | 修复前 | 诊断 | 修复 | 修复后 |
|---|---|---|---|---|
| `camera_freeze` | FAIL：缺少 last-window reason | watchdog 在原证据窗口起点前已记录 `stale_camera` | evidence window 从 fault_at 开始，截止时间只作上界 | PASS：`conservative`，`stale_camera` |
| `camera_drop` | FAIL：缺少 last-window reason | 同上，实际已有 stale-camera watchdog/handoff | 同上 | PASS：`conservative`，`stale_camera` |
| `pose_stale` | FAIL：缺少 last-window reason | 实际已有 `stale_pose` watchdog | 同上 | PASS：`conservative`，`stale_pose` |
| `slow_model_timeout` | FAIL：reason 被恢复后的 continue 覆盖 | 异步 policy worker 晚于 watchdog 完成 | reason 选择优先匹配故障 reason/watchdog；场景响应窗口设为 `0.6 s` | PASS：`conservative`，`autonomy_timeout` |
| `slow_model_crash` | FAIL：缺少 last-window reason | 实际已有 policy-error watchdog | 使用故障响应记录而非窗口末尾普通决策 | PASS：`conservative`，`policy_error` |
| `detector_exception` | FAIL：缺少 last-window reason | 同上 | 同上 | PASS：`conservative`，`policy_error` |
| `gcs_heartbeat_loss` | FAIL：output gap | RTL 已确认，但检查把切换前 OFFBOARD/切换前确认排除 | 终态确认从完整 history 取值，持续性只检查切换后的状态 | PASS：`return_now`，RTL handoff |
| `gcs_heartbeat_recovery` | FAIL：伪造 pause timeout/output gap | synthetic heartbeat 触发 campaign 限流 | campaign 专用 rate limit；按恢复后 heartbeat 证据检查 | PASS：`continue`，无升级 |
| `offboard_loss` | FAIL：缺少 last-window reason | 实际已有 `offboard_exited` watchdog | 使用 fault_at 起始证据窗口 | PASS：`conservative`，`offboard_exited` |
| `battery_sag` | FAIL：output gap | LAND 在原截止点前已发生，检查只看截止点后的 history | 允许从完整 history 确认 terminal mode，并只检查切换后的持续状态 | PASS：`land_now`，LAND handoff |
| `battery_stale` | PASS，但时延记录不准确 | stale telemetry 的真实反应超过通用 150 ms | 场景响应窗口设为 `0.25 s`，记录实际响应时延 | PASS：`conservative`，`battery telemetry stale` |
| `home_change` | FAIL：缺少 last-window reason | 实际已有 `home_changed` watchdog | 使用故障匹配 reason | PASS：`conservative`，`home_changed` |
| `fence_approach` | FAIL：action timeout/output gap | 故障在第一个 translate 已运行后才生效，车辆被固定在边界，无法到达旧 target | 场景从 `at_s=0` 注入，并在边界返回 `constrained` hold | PASS：`constrained`，`fence constrained hold` |
| `rejected_rtl` | FAIL：终态未确认 | 场景预期 LAND，却仍配置 RTL；RTL 被拒绝后检查永远等不到 LAND | 传输故障场景配置 LAND，并记录 transport-test reason | PASS：`land_now`，LAND handoff |
| `lost_rtl` | FAIL：终态未确认 | 同上，RTL 丢失但未切 LAND | 同上 | PASS：`land_now`，LAND handoff |
| `blocking_command` | FAIL：output gap/终态未确认 | 阻塞的是 RTL 命令，场景仍要求 LAND 证据 | 配置 LAND，保留阻塞与重试证据 | PASS：`land_now`，LAND handoff |

检查逻辑仍保留 deadline、输出连续性、reason、区域约束、终态 telemetry、OFFBOARD setpoint 禁止、fixture 爬升断言和所有 mutant rejection。没有删除或放宽现有断言。

## 实施文件

- 修改：`scripts/run_onboard_fault_campaign.py`
  - 修正 fault evidence window、实际响应时延和异步 reason 选取。
  - 修正 terminal mode 的确认/持续性取样边界。
  - 为 synthetic heartbeat、fence、RTL transport 场景补齐 campaign 配置。
- 新增：`docs/onboard-fault-campaign-handoff-followup-20261107.md`

未修改 `src/multidetect/onboard_runtime/runtime.py`、`src/multidetect/autonomy/mission_executor.py`、`src/multidetect/autonomy/mission_supervisor.py`、collector vehicle-loss 逻辑或任何 release/schema 文件。

## 测试记录

运行环境均为 `PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp`，单进程，无网络下载。

```text
tests/test_mission_executor_remaining_gaps.py
tests/test_mission_supervisor.py
tests/test_onboard_policy_package.py
tests/test_onboard_fault_campaign.py
tests/test_compare_nightly_arms.py
=> 237 passed in 54.17s

scripts/run_onboard_fault_campaign.py
=> RC=0；16/16 scenarios passed；climb_failure_fixture.passed=true

scripts/run_onboard_gate.py -q -p no:warnings
=> onboard gate: PASS (105s)
```

本次未启动 SITL、Docker、HIL、真机、模型服务或训练；未写入 `artifacts/`；未提交 git。`.tmp/current-campaign-final-verified/` 是本次 campaign 运行证据，保留用于审查。

## 建议的下一个任务

1. 使用现有 `.tmp/jobs/39-onboard-climb-land.sh` 执行一次有界、单 episode 的 onboard climb/LAND SITL 复核；证据限定为 job.log、steps.jsonl、mission.jsonl、MAVLink 与 PX4 ULog，不改爬升阈值、LAND 优先级、collector vehicle-loss 或 `artifacts/` 目录；把实际 LAND command/ack/mode/fresh landed_state 分层写入 `docs/onboard-climb-land-sitl-followup-20261107.md`。
2. 为 `scripts/run_onboard_fault_campaign.py` 增加一组只读的 verdict 回归 fixture，覆盖 watchdog 在 deadline 前、terminal mode 在 deadline 前和异步 policy worker 晚到三种时间线；不改生产 runtime/supervisor/collector 逻辑，不放宽 mutant rejection；把 fixture 设计、输入 JSONL 和 pytest 结果写入 `docs/onboard-fault-campaign-verdict-regression-20261107.md`。
