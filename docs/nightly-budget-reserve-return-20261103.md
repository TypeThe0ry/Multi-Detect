# budget-reserve-return nightly 失败调查（2026-11-03）

## 结论与范围

本次失败的直接原因是 **PX4/MAVLink 遥测停止推进，collector 最终抛出超时**。
不是仅仅因为仿真速度低于实时，也不是 suite 的返航期望过期。同时发现一个可独立
修复的 SIM 时钟缺陷：supervisor 原先根据相机帧时间扣任务预算，在飞控时间已经
冻结、相机时间继续增长的情况下，错误地把任务判成时间 reserve 耗尽。

本次修复 SIM supervisor 的时钟来源，并补充确定性 CPU 单元测试；保留预算阈值、
suite 断言、primitive 墙钟上限和 episode watchdog。现有产物不能进一步区分
PX4 进程内部停顿、调度饥饿或遥测输出链路停滞，不能宣称底层停滞已修复，也不能
把旧 episode 改判为通过。

调查仅只读以下目录，未启动 SITL、Docker、模型服务或 GPU 工作：

```text
/data/projects/Multi-Detect-env/Multi-Detect/artifacts/benchmarks/
  nightly-linux-r21idr3-r24plan-7/main/budget-reserve-return/
```

下文路径均相对于这个目录。episode 为
`campaign/gen_cnt2_w0003/episodes/episode-0000-473ac08b/`；onboard log 为
`campaign/gen_cnt2_w0003/episode-0000-473ac08b-onboard-frames/mission_log/`。
原运行 provenance 的 commit 是 `09ed2913733baeb090345046d7b87f921bab98a1`，
且记录了 dirty worktree；本次基于当前工作树修复，旧产物未重写。

## 原始错误和遥测证据

- `report.json` 的 `summary.episodes=0`、`summary.complete=0`，`episodes=[]`。
- `episode.json` 记录 `status="failed"`、`step_count=43`，failure 为：

  ```text
  TimeoutError: episode telemetry stalled after wall-clock budget
  (vehicle_time_boot_ms=122988, message_count=57455, stall=1637.2s)
  ```

- `campaign/gen_cnt2_w0003.log` 第 182–193 行给出 traceback，异常来自
  `_EpisodeProgressWatchdog.check()`；step 循环在调用 watchdog 时失败。
- `mavlink.jsonl` 第 57454 行的最后一个 `LOCAL_POSITION_NED`：
  `time_boot_ms=122988`，主机接收 monotonic 为 `47152.460703828 s`。
  `GLOBAL_POSITION_INT` 的最后时间戳同为 `122988 ms`。HEARTBEAT、姿态、位置、
  电池和控制输出消息均在同一时段停止，后续没有新的 MAVLink 记录。
- campaign log 第 151–152 行：step 33 的 15 m translate 从主机 monotonic
  `47139.317031659 s` 持续到 `47319.371773479 s`，约 180 s 墙钟后超时，
  primitive 计时仅前进 `2.684 s`。step 34–42 连续超时，各步 primitive
  `elapsed_s=0.0`。这不是 vehicle clock 仍持续推进的正常慢速仿真。

## supervisor 的实际决策

`mission_log/mission.jsonl` 的 sequence（本文件也对应行号）记录：

| sequence | 任务时间 / s | 事实 |
| --- | ---: | --- |
| 3 | 0 | `continue`，`operator policy permits autonomous execution` |
| 217 | 134.940 | escalation 到 `return_now`，原因 `remaining time at or below reserve` |
| 218 | 134.940 | decision 保持上述动作及原因 |
| 219 | 134.940 | `final_summary.status=ended_early`，原任务及 recovery history 均为空 |
| 221 | 134.940 | 派发 `hold` 制动，`supervisor_action=return_now` |
| 225 | 167.444 | 派发 `skill=return_home` 的 `turn` |
| 229–253 | 198.960–342.608 | 重复派发同一返航转向，遥测位置不再变化 |

sequence 217 的 energy model 自身仍是 `action=continue`：剩余电量约
`88.934 Wh`，energy reserve 为 `40 Wh`，扣返航后 action margin 约
`42.676 Wh`。本次 latch 的原因确实是任务时间，而不是电量不足、链路策略或
低置信度感知。`return_home_started` 仅证明技能已派发，不证明飞机实际移动或返航完成。

## 失败期望与代码路径

`configs/onboard/suite-main-r21idr3-r24plan.json` 的该 mission 要求技能顺序
`[monitor, return_home]`，并检查以下 supervisor 证据：

- `final_decision=return_now`、`reason="remaining time at or below reserve"`；
- `return_home_started=true`、`onboard_log_exists=true`；
- `logged_decisions` 包含 `return_now`，`logged_reasons` 包含上述 reserve 原因；
- `original_mission_completed=false`。

对旧产物实际调用 `scripts/run_mission_suite.py::build_table()`：由于 report
没有 scored episode，suite 生成 `status=missing`、`supervisor={}` 的行。
`skills` 和上述 7 项 supervisor 检查全部为 false。特别地，缺少证据不等同于
`original_mission_completed=false`，该断言也不能通过。

对同一 episode 单独调用
`scripts/score_onboard_loop.py::_supervisor_evidence()` 提取 durable log，再核对
同一组期望，8 项全部满足。这仅说明原期望与日志相符；campaign 因失败没有把该
episode 纳入 report，仍必须判失败。其过滤路径在
`scripts/eval_onboard_loop_campaign.py::episode_is_complete()` 及评分循环中。

预算链路为：

1. `scripts/onboard_policy.py::_act_impl()` 用观测推进 `SimGroundTimeline`；
   `_supervisor_link_options()` 把 `timeline.now` 注入 supervisor 的 clock。
2. 原 `src/multidetect/autonomy/onboard_policy_parts/simulation.py::advance()`
   仅使用 `frame_sensor_stamp_ns`，以第一帧为时间原点。
3. `src/multidetect/autonomy/mission_supervisor.py::evaluate()` 计算
   `remaining = time_s - (now - started)`，在 `remaining <= reserve_time_s` 时
   latch 返航。`next_action()` 先制动，再交给 recovery executor。
4. 执行器入口通过 `onboard_policy_parts/helpers.py::_observation_timestamp_s()`
   获取时间，SIM battery estimator 则通过
   `scripts/onboard_policy.py::_energy_vehicle_estimator()` 绑定时间；两者均优先
   采用 pose/PX4 时间。相机和飞控分离后，原预算时钟与它们不一致。

原始 `steps.jsonl` 的关键观测如下，step_index 从 0 开始：

| step_index（obs_before） | 相机时间 / s | pose/PX4 时间 / s | 原预算已用 / s | 飞控实际推进 / s |
| --- | ---: | ---: | ---: | ---: |
| 0 | 21.616 | 21.616 | 0 | 0 |
| 33 | 120.188 | 120.188 | 98.572 | 98.572 |
| 34 | 156.556 | 122.988 | 134.940 | 101.372 |
| 42 | 364.224 | 122.988 | 342.608 | 101.372 |

因此首次 reserve 决策时，原逻辑算出剩余 `15.060 s`；按飞控推进应剩余
`48.628 s`，仍高于 `30 s` reserve。相机推进不能替代飞控推进。

## 修复与验证

`SimGroundTimeline` 在首个观测上按以下优先级绑定时钟源，并保留第一观测为原点：

1. `pose_time_boot_ms`；
2. `pose_sim_stamp_ns`；
3. 无飞控时间戳时，兼容原有 `frame_sensor_stamp_ns` 观测。

绑定后不切换时钟域。所选字段缺失、非法或倒退均明确报错，避免通过切回继续
增长的相机时钟消耗预算。真实飞行的 host-clock 路径未修改。

在 `tests/test_onboard_policy_package.py` 新增 10 个参数展开后的用例，覆盖：
真实数值下的相机/飞控分离、两种 pose 时间字段、120 s reserve 精确边界、
制动和返航启动、非法时间戳、飞控时间倒退、暂停/恢复/中止事件的顺序与去重。
原有仅相机观测、链路超时及真实飞行电池陈旧检查保持通过。

验证命令的共同环境：

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
PY=/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python

"$PY" -m pytest tests/test_onboard_policy_package.py \
  tests/test_mission_supervisor.py tests/test_energy_estimator.py \
  tests/test_mission_suite.py tests/test_supervisor_evidence.py \
  tests/test_sim_teacher_energy_telemetry.py \
  tests/test_sim_teacher_parallel_timing.py -q -p no:xdist -o addopts=''

"$PY" scripts/run_onboard_gate.py -q -p no:warnings -p no:xdist -o addopts=''
```

首批新增 9 项回归在修复前全部失败，其中两项在相机时间 `156.556 s` 时错误
得到 `return_now`。修复并补充事件顺序测试后，相关测试 **224 passed**。

另将原 `steps.jsonl` 的全部 43 个 `obs_before` 只读输入修复后的 timeline 和
真实 supervisor，仅重放任务时间预算检查，不执行动作、感知或电池重放。43 次
检查均为 `continue`，最后已用 `101.372 s`、剩余 `48.628 s`；旧相机计时则为
已用 `342.608 s`。这验证了时钟修复的作用范围，不代表仿真任务重跑通过。

完整 CPU 闸门结果为 **7 failed、1645 passed、12 skipped、22 deselected**
（pytest 473.05 s，wrapper 489 s），未将其标记为通过。失败明细：

| 测试 | 观测到的失败 |
| --- | --- |
| `test_llama_server_logs.py::test_port_wait_ignores_time_wait_after_server_close` | 创建本地 socket 时 `PermissionError: [Errno 1] Operation not permitted` |
| `test_onboard_main_operator_link.py::test_start_up_mission_is_bound_and_held_for_operator_approval` | 同上，socket 创建被当前沙箱禁止 |
| `test_replay_set.py::test_replay_annotation_workbench_serves_verified_images_and_tokenized_mutations` | 同上，HTTP server 的 socket 创建被禁止 |
| `test_onboard_main.py::test_replay_cli_uses_shared_policy_and_latency_backends_without_hardware` | `timing_passed=false`；实测 setpoint 约 8.52 Hz、最大间隔约 0.2685 s |
| `test_onboard_test_hook_security.py::test_non_whitelisted_hook_module_is_rejected_before_import` | CLI 子进程超过原有 10 s timeout |
| `test_onboard_test_hook_security.py::test_whitelisted_hook_is_rejected_for_non_loopback_endpoint` | 同上 |
| `test_onboard_test_hook_security.py::test_no_hook_environment_does_not_activate_hook` | 同上 |

这些堆栈没有报告预算或 reserve 断言失败。保留 socket 及实时频率断言，不添加
skip，也不提高子进程 timeout。检查 hook 测试时发现两处 `PYTHONPATH="src;."`
写死 Windows 分隔符，依照跨平台要求改为 `os.pathsep.join(("src", "."))`：Linux
得到 `src:.`，Windows 仍为 `src;.`。这是测试环境构造的修正，未修改安全断言。

修正分隔符后，单独复查上述 7 个失败项，并运行同文件原有的
`test_loopback_endpoint_forms_are_accepted`，结果为 **6 failed、2 passed**
（59.09 s）。replay 频率测试和 endpoint 形式检查通过；3 项 socket 权限错误
及 3 项 CLI timeout 仍存在。不能据此宣称 CLI timeout 已修复；其超时原因尚未
进一步定位，完整闸门也仍未通过。未运行 Windows 实机测试，Windows 分隔符
行为由 `os.pathsep` 保持不变。

## 只读产物校验

下列 7 项主要原始文件在结束前复核，SHA-256 均与调查开始时一致。受证据约束的
配置文件也没有 diff：

| 文件 | SHA-256 |
| --- | --- |
| `report.json` | `0d7e4fea411e381b69faaafcff8bb9f9654570ab2757466edeb7cbb040cf36bf` |
| `onboard.json` | `2dadb94f05f1d938b7e4f859b11f50bbb247dd715e3cf6f6cba7d557a5a40fb3` |
| `campaign/gen_cnt2_w0003.log` | `4fb4eac10d3fe188a6806bbcdf71ea486bf2a1393e5feacb2373db78b13b9ac0` |
| episode `episode.json` | `c9f78ff451b2b1ed23465e93c6df2c152e20443b825bd5e95f86bbb2f745714e` |
| episode `steps.jsonl` | `679ba58c528a36616863db9afd73756f4f950ac23f640ba2b5d52eb0a6931257` |
| episode `mavlink.jsonl` | `5b29b99f91c51b7a35e4aa05044fcf3fef0980fa46f767c39f635e4ab9186e80` |
| onboard log `mission.jsonl` | `f189a7d6987019c1a1aacebf08df3493008ea6d31bc3e25ffeb2ed9cfe332d17` |
