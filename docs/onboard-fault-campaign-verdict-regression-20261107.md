# Onboard fault campaign verdict 时间线回归（2026-11-07）

## 目的

本回归把 handoff 文档中确认的三条 verdict 时间线固定成只读 JSONL fixture，直接调用 `scripts/run_onboard_fault_campaign.py` 的 `observed_from_observation()` 和 `verdict()`。测试不启动 SITL、Docker、模型服务或 GPU，也不修改生产 runtime、supervisor、collector。

三条时间线分别是：

1. watchdog 在响应截止时间前已经写入故障决策；
2. terminal mode 在响应截止时间前已经由 vehicle telemetry 确认，并在窗口剩余时间持续；
3. 异步 policy worker 在 watchdog 已响应后才写入普通 `continue`，该迟到结果不能覆盖故障 verdict 的 action/reason。

## Fixture 设计

Fixture 位于 `tests/fixtures/onboard_fault_campaign_verdict/`，每行是一个 mission decision JSON object。

### watchdog-before-deadline.jsonl

```jsonl
{"kind":"decision","timestamp":10.55,"action":"hold","reason":"stale_camera watchdog: frame age exceeded","source":"runtime_watchdog"}
{"kind":"decision","timestamp":10.90,"action":"continue","reason":"healthy frame recovered","source":"policy"}
```

故障时间是 `10.0`，截止时间是 `10.6`。`10.55` 的 watchdog 响应在截止时间内；后续恢复决策位于响应窗口之外。测试同时提供跨越整个 3 秒评估窗口的 OFFBOARD telemetry 和连续 accepted setpoint，避免把时间线回归混同为 output-gap 检查。

### terminal-before-deadline.jsonl

```jsonl
{"kind":"decision","timestamp":20.40,"action":"land_now","reason":"energy threshold: battery sag","source":"runtime_handoff"}
```

故障时间是 `20.0`，截止时间是 `20.6`。vehicle history 在 `20.5` 首次确认 `LAND`，之后到 `22.9` 保持 `LAND` 且仍 armed。该 fixture 固定了“终态可以在 deadline 前确认，持续性从切换时刻开始检查”的边界；切换前的 OFFBOARD setpoint 不会被误判为 terminal switch 之后的输出。

### late-policy-worker.jsonl

```jsonl
{"kind":"decision","timestamp":30.55,"action":"hold","reason":"autonomy_timeout watchdog","source":"runtime_watchdog"}
{"kind":"decision","timestamp":30.90,"action":"continue","reason":"delayed policy worker result after recovery","source":"policy"}
```

故障时间是 `30.0`，截止时间是 `30.6`。`autonomy_timeout` watchdog 在 deadline 前响应；`30.90` 的 policy worker 结果明确晚到。verdict 的 action/reason 只从 fault 到 deadline 的响应记录选择，因此结果仍为 `conservative` / `autonomy_timeout`。

## 保留的 mutant rejection

原有 `test_campaign_rejects_defective_runtime` 参数化测试保持不变，仍覆盖 no-op、always-hold、always-return、忽略 package policy、释放 latched policy、prefault RTL、fence 越界和释放 GCS loss latch 等 mutant。新增 fixture 只验证 verdict 取样边界，没有删除、放宽或改写这些拒绝断言。

## 测试结果

运行环境：

```text
PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python
```

回归 fixture：

```text
python -m pytest tests/test_onboard_fault_campaign.py -q -p no:xdist \
  -k verdict_timeline_regression_fixtures
=> 3 passed
```

包含 mutant rejection 的完整目标文件：

```text
python -m pytest tests/test_onboard_fault_campaign.py -q -p no:xdist
=> 24 passed（包含原有 mutant rejection 与 climb fixture）
```

本次没有运行整套 onboard gate；没有启动 SITL、Docker、HIL、真机、模型服务或训练，也没有写入 `artifacts/`。
