# SITL 槽位公平租约（2026-11-07）

本次改动把 `/data/scratch/multidetect-sitl.lock.slot-N` 从“进程全生命周期占用”调整为按 episode 租约。r15 worker 每个 episode 开始前通过共享 `SitlLock` 重新申请槽位，episode 结束或异常退出时在 `finally` 中释放；夜间回归沿用同一入口，因此不会被一个长时间采集 worker 持有的旧槽位阻塞数小时。

`SitlLock` 现在维护同名的 `.queue/` 等待目录。等待者写入带优先级、纳秒时间和随机尾缀的票据，优先级数值越大越先服务；同优先级按 FIFO 顺序。数据采集默认优先级为 `MULTIDETECT_SITL_COLLECTION_PRIORITY`（默认 `0`），nightly 由 `run_nightly_regression.py` 设置 `MULTIDETECT_SITL_NIGHTLY_PRIORITY`（默认 `100`），也可直接用 `MULTIDETECT_SITL_PRIORITY` 覆盖锁实例默认值。槽位数仍由 `MULTIDETECT_SITL_SLOTS` 控制，当前项目只配置槽位 0–2，槽位 3、4 不会被候选范围访问。

租约 owner 文件继续记录 PID 和进程启动时间。持有期间由守护线程定期刷新 owner 的 mtime（间隔不超过 30 秒，默认约 40 秒），因此 120 秒 stale grace 只会回收真正停止心跳的租约。释放会先停止心跳，再校验 owner 内容后删除目录；进程崩溃时不会执行释放，但下一个客户端会在 120 秒后按 PID/启动时间校验并回收。等待票据也使用同样的 PID 校验，死进程票据会被清除。

为兼容正在运行的旧代码，多槽新客户端在尝试槽位前检查无后缀的旧目录（例如 `multidetect-sitl.lock/`）。只要旧 owner 仍存活，就把它视为占用并等待；不会把旧目录当作新槽位抢走。旧 owner 超过 120 秒且 PID/启动时间不再匹配时，才允许 stale reclaim。测试全部使用 pytest 的临时目录，不访问 `/data/scratch`，也不启动 SITL、Docker 或 GPU。

相关文件：

- `scripts/sitl_lock.py`：公平队列、优先级、心跳、旧租约兼容与 stale reclaim。
- `scripts/run_r15_round.py`：采集 episode 租约使用采集优先级。
- `scripts/run_nightly_regression.py`：向子 suite 传递 nightly 优先级。
- `tests/test_sitl_lock.py`：跨进程槽位、优先级队列、心跳生命周期、陈旧回收及旧代码共存测试。
