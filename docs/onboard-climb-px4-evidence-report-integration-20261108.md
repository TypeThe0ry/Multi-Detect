# Climb-fault 证据分类接入 Suite 报告（2026-11-08）

## 行为

当 campaign 的 onboard 配置显式启用 `sim_climb_timeout_injection` 时，
`eval_onboard_loop_campaign.py` 会对已评分 episode 目录调用只读
`check_onboard_climb_px4_evidence.py`，并将完整三类分类报告附加到 episode 行的
`climb_fault_evidence`。checker 的异常记录为该字段中的错误信息，不覆盖原有 episode
评分、任务 expectation 或进程退出码。未启用注入的 campaign 不运行 checker，也不改变
原报告结构。

`run_mission_suite.py` 将该字段透传至 `suite-report.json` 的对应 mission/episode 行，
并在 `suite-report.md` 输出三类计数、证据目录及逐项分类和说明。JSON summary 同时提供
`summary.climb_fault_evidence` 汇总。分类只描述证据来源：PX4 MAVLink 可证明、只能由机载
日志证明、缺失；它不会变成任务预期或安全策略判定。

## v3 只读证据复核

输入目录：
`artifacts/benchmarks/onboard-climb-fault-sitl3-20261107T172724Z-1332460/`。

episode 的 checker 结果为 PX4 MAVLink 6 项、机载日志 4 项、缺失 0 项。两次爬升 timeout、
没有第三次 climb、failure event/alert 与 `land_now` handoff 均保持机载日志分类；boot
时间推进、低空 airborne 前置样本、LAND 模式切换 ACK、LAND heartbeat、fresh
`landed_state=1` 和后续 disarm 归为 PX4 MAVLink 证据。此次集成不重新解释 checker 的判定，
也不把 collector/harness 摘要升级为 PX4 证据。

## CPU-only 验证

使用主 `.venv`、`PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp` 单进程运行：

```text
python -m pytest tests/test_onboard_climb_px4_evidence_checker.py tests/test_mission_suite.py -q -p no:xdist
python scripts/run_onboard_gate.py -q -p no:warnings
```

测试覆盖注入开关、只读调用、suite JSON/Markdown 透传及真实 v3 证据分类。没有启动
SITL、Docker、GPU 作业或模型服务，也没有写入共享 `artifacts/`；没有触及运行时策略、
LAND 优先级、阈值、vehicle-loss 规则或既有断言。

## 建议的下一个任务

1. 目标：为 climb-fault suite 报告增加阶段型失败诊断，能区分 episode failed、collector timeout、steps/ULog 缺失、cleanup 状态和 host/SITL 遥测末态，并确保已有 failed episode 诊断不会被 missing episode 行掩盖。证据路径：`scripts/eval_onboard_loop_campaign.py`、`scripts/run_mission_suite.py`、`scripts/collect_sim_teacher_episodes.py`、`tests/test_onboard_climb_px4_evidence_checker.py`、`tests/test_mission_suite.py`，以及只读 `artifacts/benchmarks/onboard-climb-fault-sitl3-20261107T172724Z-1332460/`。不得启动 SITL/Docker、写共享 `artifacts/`、改运行时策略、安全阈值、LAND 优先级、vehicle-loss 规则或既有断言；新增有针对性的 CPU-only 报告测试；文档写入 `docs/onboard-climb-sitl-failure-reporting-20261108.md`。
