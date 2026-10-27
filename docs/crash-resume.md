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

  修复后的第二个 `valid --apply-px4-params` 架次尚未能启动：共享锁脚本因主机可用内存只有约 1.1--1.3 GiB（门槛 5 GiB）持续等待，未创建容器；随后按要求释放 `cxparam` 锁。五个 crash-resume 场景因此仍未获得新的真实结果，已有阻塞证据保留在 `artifacts/evaluation/crash-resume-sitl-20261027/`。
