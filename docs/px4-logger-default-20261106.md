# PX4 SITL logger 默认关闭（2026-11-06）

从本版本起，`MULTIDETECT_PX4_LOGGER_MODE` 未设置时，SITL 默认使用 `off`，启动器通过 `SDLOG_BACKEND=0` 和 `PX4_PARAM_SDLOG_BACKEND=0` 关闭 ULog。需要 ULog 的调用方仍可设置 `MULTIDETECT_PX4_LOGGER_MODE=default`。

`--keep-px4-ulog` 是显式的 ULog 请求。为便于诊断，直接传入该选项且变量未设置时，采集器会在本次进程内选择 `default`；如果调用方显式设置了 `MULTIDETECT_PX4_LOGGER_MODE=off`，则请求被保留为关闭状态，并记录 `px4_ulog_status=disabled`。找不到 `.ulg`、复制失败或日志被关闭，都不会把 episode 标记为失败或 `px4_crashed`，而是记录 `px4_ulog_unavailable` 或 `px4_ulog_disabled`。

现有 `run_nightly_regression`、`run_mission_suite`、`eval_onboard_loop_campaign` 的 `--keep-px4-ulog` 只负责把选项传给采集器。采集任务评分、crash/stall 归因使用 episode、容器状态、PX4 进程状态和时钟诊断，不把 ULog 当作必需输入。现有 acceptance 脚本和 QGC combined acceptance 没有 ULog 必需断言，因此没有路径需要强制请求 `default`；需要离线 ULog 证据时应显式设置环境变量或使用 `--keep-px4-ulog` 的上述行为。

## 三臂对照

数据来自同一代码头、同一时间窗口；每个任务共 14 个 mission，**每个 mission 只运行一个 episode**。45 秒为 stall limit，表中 `pass` 是任务期望通过数，`px4_crashed` 是报告中的 crash 归因数。

| Arm | Logger | Stall limit | Pass | Stall | `px4_crashed` | 备注 |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| `r24plan-22-off45` | off | 45 s | 13/14 | 0 | 0 | 早先 logger-off 运行曾为 14/14 |
| `r24plan-23-def180` | default | 180 s | 10/14 | 4 | 4 | 同一批 14 个 mission |
| `r24plan-24-def45` | default | 45 s | 4/14 | 4 | 4 | 缩短 stall limit 后通过数下降 |

原始报告位于 `artifacts/benchmarks/nightly-linux-r21idr3-r24plan-{22-off45,23-def180,24-def45}`；`artifacts/` 是共享只读产物目录，本次未修改其中内容。

这组对照支持把 logger 默认设为 off，以减少当前 SITL 的失败风险。它仍是每个 mission 一次 episode 的观测，不能证明 logger 是崩溃的唯一机制，也不能证明关闭 logger 在所有场景、镜像或硬件上都稳定；该机制目前尚未被证明。
