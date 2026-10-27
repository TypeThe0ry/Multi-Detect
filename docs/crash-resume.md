# 机载进程崩溃恢复

每个已完成的监督器控制步可写入 `crash_resume/checkpoint.json`。文件通过临时文件、`fsync`、`os.replace` 原子替换，最大 64 KiB。写入失败只记录 `checkpoint_error`，控制循环继续由既有安全层负责。默认只接受最近 60 秒的检查点；同时比较 PX4 `time_boot_ms`，PX4 重启或 boot time 回退即拒绝恢复。

检查点格式为 `multidetect.crash-resume.v1`，包含 `mission_id`、`package_digest`（准入任务及计划摘要）、`step_id`、`phase`、单调递增 `sequence`、`saved_at`、`boot_time_ms`，以及 `counts`、带位置的 `alerts`、`visited`、`home`、`elapsed_s`、`energy` 和 `provenance`。飞行模式只取 MAVLink HEARTBEAT 的 `custom_mode`，由 PX4 main/sub mode 解码为 `HOLD`、`OFFBOARD`、`RTL`、`LAND` 等；boot time 取 `LOCAL_POSITION_NED` 或 `ATTITUDE` 的 `time_boot_ms`，不从 HEARTBEAT 读取不存在的字段。恢复时身份、模式、boot time、时间和序号均须有效；截断 JSON、任务包摘要不符和过期记录都不可信。告警与计数在恢复时回灌执行器，位置记录继续用于空间去重。

## 启动决策

| 车辆状态 | 检查点 | 决策 | 中文审计原因 |
|---|---|---|---|
| 地面、未解锁 | 任意（旧文件保留） | 新任务 | 飞机在地面，按正常新任务流程启动 |
| 空中 | 有效且 mission_id/摘要一致、未过期，boot time 有效，模式为 `HOLD`/`LOITER`/`AUTO_LOITER`/`OFFBOARD` | 恢复 | 飞机在空中且检查点和 PX4 状态均允许恢复；不得 arm/takeoff |
| 空中 | PX4 boot time 未知、飞行模式未知/无法解码、或模式不在允许集合 | 只记录与监视 | onboard 不接管，等待/记录 PX4 状态 |
| 空中 | PX4 已处于 RTL/LAND/FAILSAFE | 只记录与监视 | PX4 失效保护已接管，onboard 不重新接管；`resume_after_failsafe: true` 只是签名任务包的显式 opt-in，仍需通过身份、摘要、序号、时间和 boot 校验；未知模式仍只监视 |
| 空中 | 缺失、损坏、序号回退、摘要不符或过期 | RTL | 无法证明任务状态，返航 |
| 空中 | 同上，且机型无 RTL | 降落 | 按机型 recovery policy 降落 |
| 状态未知 | 任意 | RTL/降落 | 状态未知，无法安全恢复 |

启动先等待有效的 armed/landed telemetry；等待超时且没有检查点时按原有地面准入失败，不发送 RTL。每个分支写入 mission log，结束时写 `final_summary`（含中文原因）。旧检查点只归档，不删除。`resume_after_failsafe` 是可选任务包字段，默认 `false`，由签名任务包绑定。

## SITL 验证

真实验证编排脚本为 `scripts/run_crash_resume_sitl.py`。Windows 主机不使用 systemd：脚本为每个场景创建一个 `md-crash-resume-<场景>` PX4/Gazebo 容器，记录自己 `Popen` 返回的 onboard PID，等待 checkpoint 序号达到门槛且收到 armed 心跳后只终止该 PID，再用完全相同参数拉起 `scripts/onboard_main.py real`。脚本只读取自己架次的 `mission_log/mission.jsonl`、checkpoint 和 MAVLink `HEARTBEAT`/`COMMAND_ACK`，结束时只 `docker rm -f` 自己记录的容器名。

在 Git Bash 中运行（共享锁必须包住每个架次，不能并发）：

```bash
. /d/Projects/mdwt/_scratch/sitl_lock.sh
sitl_acquire cxcrash
python scripts/run_crash_resume_sitl.py \
  --mission-package <signed-package.json> --operator-key <operator-key> \
  --camera <gstreamer-pipeline-or-index> --camera-backend gstreamer \
  --calibration <camera-calibration.json> --cases valid corrupt digest expired rtl
status=$?
sitl_release
exit $status
```

结果目录固定为 `artifacts/evaluation/crash-resume-sitl-20261027/`，汇总为 `summary.json`，每个场景另有 `result.json` 和原始 onboard/PX4 证据。判据是：`valid` 必须 `crash_resume.action=resume` 且 step、counts、alerts 延续，重启后的 arm/takeoff 命令数为零；`corrupt`、`digest`、`expired` 必须分别出现中文拒绝原因并 `return_home`/降落，命令数为零；`rtl` 必须 `monitor_failsafe` 且不发送重复 RTL。

  测试相机使用仓库允许的 `MULTIDETECT_ONBOARD_TEST_HOOK=scripts.onboard_real_test_hook`，只在 loopback endpoint 生效；camera = test hook（does not affect crash-resume criteria），标定 JSON 由 harness 生成。最终实现让 onboard 直接连接 PX4 映射的 `udpin:127.0.0.1:<telemetry_port>`，不再依赖 relay。

  参数事务竞态已在 `PymavlinkVehicle` 中修复并用同步零延迟回包测试覆盖。发送前记录 `_clock()` 并进入 `awaiting`；接收时间也是 reader 回调进入 `ingest` 时调用同一 `_clock()` 得到的 monotonic 时间。接收端仍要求 `stamp > pending_since`，所以事务开始前已排队的旧回复不能完成新事务。之前的实现发送期间处于 `arming`，接收端直接丢弃；而且 pending 时间在发送后记录，发送期间到达的包也会被按旧包丢弃。读、写事务都有相同窗口。

  修复后按要求以 `cxparam` 锁执行真实验证，telemetry 使用 `127.0.0.1:14542`（端口 `14541` 未触碰），测试 hook 和 `MULTIDETECT_CRASH_RESUME_TEST=1` 均启用。`valid` 架次未能通过起飞前检查：日志中 `NAV_DLL_ACT` 的 `PARAM_VALUE` 超时，`COM_RC_IN_MODE`、`COM_LOW_BAT_ACT`、`COM_OBL_RC_ACT` 也未读回；因此没有 arm/takeoff、没有 checkpoint，也没有重启后的恢复命令计数。脚本按自己记录的 PID 结束 onboard，并确认容器 `md-crash-resume-valid-r2` 已清理。由于每个场景都依赖同一 preflight 起飞门槛，`corrupt`、`digest`、`expired`、`rtl` 未运行，不能据此判定恢复逻辑通过或失败。证据在 `artifacts/evaluation/crash-resume-sitl-20261027/valid/attempt-2/` 和 `summary.json`；本轮未发现锁遗留或触碰非本任务进程。

  后续 trace 发现并修复了第二个真实缺陷：PX4 SITL 的参数流可以早于首个 HEARTBEAT 到达。旧过滤器只看 `self.target_system`，在 heartbeat 选择完成前把 `source_system=1/component=1` 的参数全部丢弃；修复后在目标尚未由 heartbeat 选定时使用 pymavlink connection 已知的 `target_system/target_component`，仍对明确不匹配的来源丢弃。`MULTIDETECT_PARAM_TRACE=<path>` 只在显式设置时写 JSONL，记录发送、接收、解码、事务状态、pending、期望值和每个 drop 原因。

  trace 还证明 SITL INT32 参数使用 MAVLink bytewise：例如 `NAV_DLL_ACT` 的 `param_type=6`、原始 `2.80259692864963e-45` 解码为 `2`，没有 `type_mismatch` 或 `value_mismatch`。原先看到的 `stale_stamp` 是 Windows monotonic clock 量化造成的：发送和立即回复会有完全相同的时钟 tick，原先严格 `stamp > pending_since` 将新回复误丢。默认时钟改为 `time.monotonic_ns()/1e9`，pending 过滤拒绝严格早于事务的回复、允许相同 tick；事务开始前已接收的旧回复仍被严格过滤。同时请求并读取 `AUTOPILOT_VERSION` capability，支持 bytewise/C-cast 两种 INT32 编码并有单元测试。

  后续 trace 中已经看到修复后的即时回复被接受：`COM_DL_LOSS_T` 的 `PARAM_SET` 和 `PARAM_REQUEST_READ` 均为 `accepted`，`NAV_DLL_ACT` 的 raw `2.802596928649634e-45`、ptype 6 解码为 `2.0` 并被 `accepted`；`AUTOPILOT_VERSION` capability `59647` 明确声明 bytewise。参数 apply 已越过此前的 INT32/竞态失败，随后发现并修复了 float32 wire 量化比较和 apply 失败路径中未序列化 `PreflightCheck` 的日志 bug。

### 最后一轮五场景架次（2026-10-27）

按要求获取 `cxcrash5` 锁，等待可用内存从约 1 GiB 恢复到约 14 GiB 后启动。14540/14542 已被无关进程占用（PID 72388/46216），未触碰；启动前用 `netstat` 确认 14560/14561 空闲并使用该端口对。`valid` 的 r1 因遗留同名容器被 runner 拒绝，自动重试 r2；r2 容器成功启动，但 onboard 在任务准入阶段退出：`RuntimeError: HOME_POSITION in local NED is required before mission startup`。因此没有 arm/takeoff、没有 checkpoint、没有重启恢复，也没有可分析的参数 trace。r2 容器已按本任务 PID/容器名清理；r1 遗留容器未操作。

本轮结果写入 `artifacts/evaluation/crash-resume-sitl-20261027/final-run/summary.json`：`valid=fail`（基础遥测/启动时序阻塞），`corrupt/digest/expired/rtl=not_run`，因为它们都要求 valid 先写出 checkpoint。该阻塞与参数读写竞态修复无关，不能据此推断五个恢复分支的逻辑结果；没有在剩余时间堆叠未经证实的 runner 修复。

后续修复验证：real 模式现在在地面遥测建立后调用 `request_home_position()` 并等待 HOME，默认最多 120 秒；超时错误为 `PX4 未在 N 秒内建立 HOME（GNSS/EKF 未就绪）`。空中 crash-resume 若 HOME 缺失则记录 `monitor_failsafe`，不接管。真实 valid 重试中日志已从 HOME 缺失推进到 `home.passed=true`，随后暴露出任务包未声明 `px4_offboard_loss_action`（PX4 返回 `COM_OBL_RC_ACT=0`）以及传感器/EKF 尚未就绪；该架次没有 arm/takeoff 或 checkpoint。五场景因此仍未取得有效恢复结果，证据保存在 `final-run/valid/attempt-1/onboard-process.log`。

  修复后的第二个 `valid --apply-px4-params` 架次尚未能启动：共享锁脚本因主机可用内存只有约 1.1--1.3 GiB（门槛 5 GiB）持续等待，未创建容器；随后按要求释放 `cxparam` 锁。五个 crash-resume 场景因此仍未获得新的真实结果，已有阻塞证据保留在 `artifacts/evaluation/crash-resume-sitl-20261027/`。

### 最终真实结果（2026-10-27，`final-run2`/`final-run3`）

本轮任务包在签名文档中显式声明 `px4_offboard_loss_action=3`，即 PX4 `COM_OBL_RC_ACT=3`（Return/RTL）。选择该值是因为任务包的 offboard 丢链必须返航，且 schema 允许值为 3/4/5；产品仍保持字段缺失即拒绝。启动前用 `netstat` 检查并使用空闲的 control/telemetry/operator 端口 `14560/14561/14690`，没有触碰被占用的 14540/14541。

传感器/EKF 不是解析错误：早期报告为 `present=33587215, enabled=35768351, health=50364463`，required mask `39` 的交集分别为 `7,7,39`；估计器早期 flags 为 `165 (0xA5)`，缺少速度和水平位置位。等待 PX4 收敛后报告变为 `health=52463679`、`estimator flags=959`，sensor_health 与 estimator 均通过。preflight gate 在有界时间内等待新鲜 SYS_STATUS/ESTIMATOR_STATUS，超时仍 fail-closed。

| 场景 | 结果 | 关键证据 |
|---|---|---|
| valid | PASS | `crash_resume.action=resume`，`step_id=visit_gate`，重启后 arm/takeoff 命令数 `0`。见 `final-run2/valid/attempt-1/result.json`。 |
| corrupt | PASS | `action=return_home`，原因含“检查点缺失/损坏”，重启后命令数 `0`。 |
| digest | PASS | `action=return_home`，原因含“任务包身份不匹配”，重启后命令数 `0`。 |
| expired | PASS | `action=return_home`，原因含“过期”，重启后命令数 `0`。 |
| rtl | PASS（`final-run5`，2026-10-27 23:27） | 恢复进程写出 `crash_resume.action=monitor_failsafe`，原因“PX4 失效保护已接管，onboard 只记录与监视”；重启前 PX4 遥测 `mode=RTL, custom_mode=84148224, armed=true`；重启后 arm/takeoff/RTL 命令数 `0`。此前 `final-run3`/`final-run4` 失败分别源于：恢复路径先等待地面就绪条件（已由 `_recovery_ready` 修复）；harness 在 onboard 重启后仍占用同一 telemetry `udpin` 端口观察 RTL 且成功路径不关闭连接，导致 onboard 收不到心跳（已改为先确认 RTL 并关闭连接，再重启 onboard）。见 `final-run5/rtl/attempt-1/result.json`。 |

`final-run4` 同时用新恢复路径复跑 valid：PASS（`resume`，重启后命令数 `0`），确认 `_recovery_ready` 不影响正常恢复。五个场景均已在真实 PX4/Gazebo SITL 中通过。

RTL 失败是测试编排的剩余问题，不是放宽安全准入的理由：runner 必须在发出 RTL 后立即让恢复进程读取 airborne 状态，并确保新进程能继续收到 telemetry；本轮已保留完整日志和参数 trace 供后续修复。

### RTL 恢复边界与启动顺序

real 模式的 crash-resume 现在先等待新鲜 HEARTBEAT 以及 `armed`/`landed` 两个状态字段，再调用 `decide_recovery`。该路径不等待相机帧、局部位置、姿态、HOME、传感器健康或地面准入；这些条件只在确认需要继续执行 `resume` 或启动新任务后才检查。这样 PX4 处于空中 RTL/FAILSAFE 时可以及时记录 `monitor_failsafe`，不会因地面启动条件而超时。

测试边界也明确：如果 PX4 在恢复进程启动前已经完成 RTL 并落地（`armed=false`, `landed=true`），这是“地面 → new mission/no takeover”状态，不应判为 airborne monitor-failsafe 失败。要验证 airborne monitor-failsafe，harness 必须把触发点放在距离/高度足够大的位置，使 RTL 持续时间明显长于恢复进程启动时间，并要求重启后 arm、takeoff、mode-change 命令数为零。
