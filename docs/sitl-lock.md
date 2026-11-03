# SITL 锁嵌套约定

`scripts.sitl_lock.sitl_lock_context` 是所有 SITL 锁持有者和获取者共用的入口。上下文进入时，如果当前进程的 `MULTIDETECT_SITL_LOCK_HELD` 已经是 `"1"`，则跳过获取；否则调用传入锁对象的 `acquire`。上下文期间统一设置该变量为 `"1"`，退出时恢复原值，并释放本上下文实际获取的锁。

并发运行时由 `MULTIDETECT_SITL_SLOTS`（默认 `1`）声明可用槽位。每个槽位使用 `<lock>.slot-N` 目录作为租约，调用者可从 `SitlLock(..., slot=N)` 获取指定槽位；未指定时自动从 `0..N-1` 选择。成功获取后 `MULTIDETECT_SITL_SLOT` 会随子进程继承。槽位的端口块、容器名前缀和 GPU 由上层 worker 绑定，旧配置默认单槽，因此 K=1 保持原行为。

仓库中存在以下三处 `SitlLock` 实现：共享实现一处，两个旧脚本各自保留一处兼容实现。

| 实现 | 使用脚本 |
| --- | --- |
| `scripts/sitl_lock.py:SitlLock`（共享目录锁，带 Docker 和内存门禁） | `run_nightly_regression.py`、`run_sim_session.py`、`run_operator_demo.py`、`run_crash_resume_sitl.py`、`run_planner_round.py`、`run_r15_round.py`、`collect_sim_teacher_episodes.py` |
| `scripts/run_e2e_operator_suite.py:SitlLock`（旧版目录锁，`acquire(timeout)`） | `run_e2e_operator_suite.py` |
| `scripts/run_e2e_operator_acceptance.py:SitlLock`（旧版目录锁，`acquire(timeout_s)`） | `run_e2e_operator_acceptance.py` |

`run_nightly_regression.py` 只负责启动套件并合并报告；mission worker 自己租用槽位并把 `MULTIDETECT_SITL_LOCK_HELD=1` 传给 campaign/collector 子进程。r15 worker 采用相同的逐 worker 租约方式，因此两类任务可以在不同槽位共存。
