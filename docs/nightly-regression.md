# SIM 夜间回归总控

`scripts/run_nightly_regression.py` 按固定顺序串行运行主套件、固定翼/VTOL、跟随和鲁棒性套件，并把每套的 `suite-report.json` 汇总到一个夜间报告。端到端操作员套件只有在配置和脚本同时存在时才加入计划。

```powershell
$env:PYTHONPATH='src;.'
$env:PYTHONUTF8='1'
python scripts/run_nightly_regression.py --dry-run --out D:\Projects\mdwt\_scratch\cx-nightly\dry-check
python scripts/run_nightly_regression.py --out D:\Projects\mdwt\_scratch\cx-nightly\nightly-$(Get-Date -Format yyyyMMdd-HHmmss)
```

默认每套 1 架次。可以用 `--suite-episodes robustness=3` 增加某套架次；消防栓变体在鲁棒性套件中，可用 `--fire-hydrant-episodes 3` 快速覆盖该套的架次参数。`--suites main airframes` 可只跑选定套件。`--min-free-disk-gb` 会在每套前后执行与 `run_mission_suite.py` 相同的磁盘门槛检查；可用内存必须达到 5000 MB。

脚本持有 `D:\Projects\mdwt\_scratch\sitl.lock` 的兼容 mkdir 锁（测试或隔离运行可用 `--lock-path` 指向临时路径），owner 内容为 `label pid ISO-8601-time`。持有者异常退出超过 120 秒后才回收陈旧锁，正常退出和 Ctrl-C 都会释放自己创建的锁。

输出目录包含每套原始 `suite-report.md/json`，以及合并的 `nightly-report.md/json`。JSON 保存每套起止时间、退出码、错误、预期通过率、计划准确率、完成率、计数精确率、告警召回、安全判据、跟随指标和变体 × 任务汇总表。若上一次报告存在（默认读取输出父目录的 `nightly-report.json`，也可用 `--previous-report` 指定），合并报告会列出通过率/计划准确率下降和误报增加。

夜间报告另含逐任务分诊表和失败分类直方图。表中每条记录对应套件报告的一条任务 × 架次记录，保留世界和架次编号：

- `complete` 表示套件行的采集状态为 `complete`；“任务完成”单独对应 `mission_completed`。符合预期的安全中止可能采集完整，但任务未完成，不计为失败。
- 最终异常取 `<套件目录>/<任务 id>/campaign/<世界>.log` 最后一段 Python traceback 的异常类和完整消息，不使用较早的链式原因或 JSON 事件中的错误。日志路径和读取错误保存在 JSON 中。日志缺失会在表中明确标注。
- 同一 campaign 的日志覆盖该世界的多架次和重试，表中共享其最终异常，不能据此声称异常属于其中某个特定架次。已通过的记录即使日志含早期重试异常，也不计入失败分类。
- 失败预期列出 `expectation_checks` 中未通过的键。采集不完整或预期未通过的记录计入直方图，每条记录只计一次；成功记录的分类为空，Markdown 显示 `—`。
- 分类为 `offboard_timeout`、`land_timeout`、`disarm_rejected`、`telemetry_stall`、`sensor_stream_eof`、`preflight_health`、`plan_refused`、`other`。优先按最终异常分类；没有异常时可用结构化的计划拒绝证据。传感器请求路径的 `BrokenPipeError` 归入 `sensor_stream_eof`，普通管道异常不作此推断。仅等待 DISARM 后心跳超时不能证明命令遭拒，归入 `other`。

可以从既有套件输出只读重建报告，不启动采集器、不获取 SITL 锁、不执行资源门禁。必须用独立的 `--out`，不得在输入套件目录内输出；多个输入目录的名称必须唯一。此模式成功生成报告返回 0，历史失败通过表格和 JSON 表达；不会虚构原始起止时间或退出码。`--previous-report` 可显式指定比较基准。

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR="$PWD/.tmp"
mkdir -p "$TMPDIR"
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python scripts/run_nightly_regression.py \
  --report-only /data/projects/Multi-Detect-env/Multi-Detect/artifacts/benchmarks/nightly-linux-r21idr3-r24plan-7/main \
  --out .tmp/nightly-report-triage
```

JSON 在每套的 `missions` 中保存分诊行，顶层 `failure_categories` 保存八类计数（含零值）。现有汇总指标和正常采集流程的退出码语义保持不变。

`suite-report.json`、夜间报告、e2e suite 结果行和训练轮 `manifest.json` 附加 `provenance`。字段包含代码 commit/dirty/分支/worktree、输入配置路径及 SHA-256、命令行 argv；架次记录另含配置快照、模型摘要和世界文件摘要。所有字段均为新增字段，不替换既有指标。训练轮 manifest 以顶层 `provenance` 记录生成该轮数据的代码状态。

预计耗时取决于每套任务数和 `max_episode_s`：默认主套件 14 条加 airframes 3 条、follow 1 条、robustness 9 条，单架次通常需要数小时；dry-run 不启动 SITL、Docker 或 llama-server，只打印完整命令。
