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

`suite-report.json`、夜间报告、e2e suite 结果行和训练轮 `manifest.json` 附加 `provenance`。字段包含代码 commit/dirty/分支/worktree、输入配置路径及 SHA-256、命令行 argv；架次记录另含配置快照、模型摘要和世界文件摘要。所有字段均为新增字段，不替换既有指标。训练轮 manifest 以顶层 `provenance` 记录生成该轮数据的代码状态。

预计耗时取决于每套任务数和 `max_episode_s`：默认主套件 14 条加 airframes 3 条、follow 1 条、robustness 9 条，单架次通常需要数小时；dry-run 不启动 SITL、Docker 或 llama-server，只打印完整命令。
