# 爬升故障 PX4 证据检查器（2026-11-08）

新增 `scripts/check_onboard_climb_px4_evidence.py`，用于离线、只读地检查 climb-fault SITL 产物。脚本不会启动 PX4/SITL，不发送 MAVLink，不改变运行时策略、保护阈值、vehicle-loss 规则或断言，也不会在 `artifacts/` 下生成报告文件。

检查器接受 artifact 根目录、episode 目录或包含 JSONL 的目录，自动读取 `mavlink.jsonl`、`steps.jsonl`、`mission_log/mission.jsonl` 和 `mission_log/summary.json`。默认输出中文摘要，`--json` 输出机器可读报告；核心函数为 `check_climb_fault_evidence(path)`，返回 `points` 和三类计数：

- `provable_from_px4_mavlink`：只使用 PX4 MAVLink 消息判定。
- `provable_only_from_onboard_logs`：只使用 steps/mission log 判定，不能冒充 PX4 执行证据。
- `missing`：没有满足判定所需的证据。

十个检查点为：PX4 boot time advancing、低空 airborne、两次无增益 climb timeout、无第三次 climb、failure event/alert、`land_now` handoff、LAND mode switch ACK、LAND mode、fresh `landed_state=1`、disarm。LAND 链使用时间顺序约束：`COMMAND_ACK(command=176,result=0)` 后须出现 LAND heartbeat；fresh `landed_state=1` 必须位于 LAND heartbeat 之后；disarm 必须是该 fresh 状态之后的 heartbeat armed 位清除。初始 `landed_state=1`、collector `landed_state_fresh` 摘要和 teardown 不会被当作 PX4 LAND 证据。

对只读产物 `artifacts/benchmarks/onboard-climb-fault-sitl3-20261107T172724Z-1332460/` 的结果是 6 项可由 PX4 MAVLink 证明、4 项只能由机载日志证明、0 项缺失。两次 timeout、无第三次 climb、failure event/alert、`land_now` handoff 来自 `steps.jsonl`/mission log；boot clock、低空 airborne、LAND ACK、LAND mode、fresh landed state 和 disarm 来自 MAVLink。

CPU-only 测试位于 `tests/test_onboard_climb_px4_evidence_checker.py`，覆盖目标产物的来源边界、缺失 LAND 证据不被机载 `land_now` 推断，以及检查器不写入输入目录。

## 验证

```text
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python -m pytest \
  tests/test_onboard_climb_px4_evidence_checker.py -q -p no:xdist
```

本次没有启动 SITL、Docker、GPU、模型服务或训练，也没有写入共享 `artifacts/`。

## 建议的下一个任务

### 1. 将 checker 接入失败报告审计

目标：在不改变运行时策略和安全断言的前提下，把 `check_onboard_climb_px4_evidence.py --json` 的三类结果接入离线 suite 失败报告；证据路径：`scripts/check_onboard_climb_px4_evidence.py`、`scripts/eval_onboard_loop_campaign.py`、`scripts/run_mission_suite.py`、`artifacts/benchmarks/onboard-climb-fault-sitl3-20261107T172724Z-1332460/`；不得写共享 `artifacts/`、不得启动 SITL/Docker、不得修改 LAND 优先级/阈值/vehicle-loss 规则或既有断言；新增 CPU-only 报告测试并写文档 `docs/onboard-climb-px4-evidence-report-integration-20261108.md`。

### 2. 扩展 PX4 MAVLink 模式映射夹具

目标：为不同 PX4 记录格式增加 `CURRENT_MODE`/标准模式字段的只读解析夹具，并验证在缺少 custom_mode 数值时仍能严格区分 LAND ACK、LAND mode、fresh landed state 和 disarm；证据路径：`scripts/check_onboard_climb_px4_evidence.py`、`tests/test_onboard_climb_px4_evidence_checker.py`、本次 SITL3 的 `mavlink.jsonl`；不得改变 runtime policy、阈值、安全断言，不写共享 `artifacts/`，不启动 SITL/Docker；新增 CPU-only 测试并写文档 `docs/onboard-climb-px4-evidence-mode-compat-20261108.md`。
