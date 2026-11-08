# PX4 lockstep 生命周期修复候选与验证

本变更提供独立候选镜像构建和组件级证据。现役 `multidetect/px4-sitl-gazebo:gpu` 与机载配置未替换。最终候选 **lifetime-r1 不是原样 27a21488**：保留上游的生命周期同步及无容量链表，移除经测试证实有通知回归的墙钟轮询。组件测试不能直接证明本项目时钟卡顿已修复；仍须同条件 w0000 连跑 10 次及 21 任务各 2 次的实景门禁。

## 精确来源

- 原镜像配置 ID：`sha256:445d17a7ae7e34dbddc826b68c08c705d4892a3b3997e3f3a0bb44072a5ef8f2`。
- 镜像内 `px4-ver all`：PX4 `83c4f4e580558816f0e398918f66358988004696`，`v1.18.0-beta2`，构建于 2026-08-09。官方同名 tag 解引用也指向该提交。
- [上游 27a21488](https://github.com/PX4/PX4-Autopilot/commit/27a21488b21f6334ec6eb9a89ddfe44016814500) 的原样两文件补丁保留作对照，SHA256 `6b1c90994cc170a04d4e88c56f96346217ab1b3eb8458db35423e640e53b15c1`。
- 最终 `lockstep-lifetime-r1.patch` 仅改调度器头文件/实现，SHA256 `43bfe32d5d7572b866b3d411c2394d941b6bcc8e19ace5bdd6e230845cdf0559`。
- 候选标签 `multidetect/px4-sitl-gazebo:lockstep-lifetime-r1`。脚本核对原镜像 ID，候选 Dockerfile 继承它且仅复制 `/opt/px4-gazebo/bin/px4`；不替换 world、插件、参数、库或启动入口。

## 缺陷与取舍

旧版在锁外保存原始 condition/mutex 指针，但已判超时的等待者可能先返回，使对象结束生命周期；同时单轮最多保存 32 个唤醒。候选串行化完整广播阶段，令等待者退出时等待广播完成，并使用无固定容量的临时链表。

原样上游另将等待改为反复执行 10 ms 墙钟 timedwait。通知者若持有 caller mutex 超过该周期，waiter 会因墙钟超时先出队、再阻塞重取 mutex。通知者在解锁前发出的广播可能已无人等待；waiter 取锁后忽略墙钟 ETIMEDOUT 并重新等待，导致合法外部通知丢失。这里模拟截止时间尚未到达。此机制已用持锁 30 ms 后广播的确定性测试证实，不能以再次跑过原随机测试掩盖。

因此最终候选恢复原来的 `pthread_cond_wait`：调用者从登记前一直持有 passed_lock，直到 pthread 原子释放并进入等待；setter 在广播前拿取相同 passed_lock，已封闭登记与等待之间的通知窗口。候选没有墙钟轮询，不提前推进模拟时钟，不修改任务/飞行 timeout 或 lockstep 屏障语义。

## 三方确定性对照

测试取精确原提交、精确上游修复提交和最终候选源码，使用同一套 pthread 时序包装器。scheduler 和 components 的生产源函数不插桩；POSIX 平台适配头仅提供日志、系统时钟、信号量，不启动 PX4、Gazebo 或 GPU。

| 测试 | 原镜像源码 83c4f4e | 原样上游 27a21488 | lifetime-r1 |
|---|---|---|---|
| 收集 timeout 后暂停广播，再外部唤醒；退出须等广播完成 | 提前返回（RED） | 等待广播（GREEN） | 等待广播（GREEN） |
| 同模拟时刻 40 个超时等待者 | 32/40 自行唤醒（RED） | 40/40（GREEN） | 40/40（GREEN） |
| 通知者持 caller mutex 30 ms 后正常广播，模拟截止时间未到 | 收到通知并返回 0（GREEN） | 通知丢失，需推进模拟时钟救回（RED） | 收到通知并返回 0（GREEN） |

生命周期测试保留实际 mutex/cond 到广播完成，以观察缺失屏障而不主动制造 use-after-free。超过 32 个等待者测试显式救回旧版其余线程，避免遗留进程。每个版本的生命周期时序另重复 10 次。

原 `lockstep_scheduler_test.cpp` 的 `LockstepScheduler.All` 内含 100 轮。旧版曾通过，原样上游有时通过、有一次挂在 `test_multiple_semaphores_waiting`，已捕获自家进程调用栈。其 [测试启动同步修正 #27798](https://github.com/PX4/PX4-Autopilot/pull/27798) 已存在于 83c4f4e，不能作为这次挂起的解释。最终候选要求原测试连续 10 次（1000 内部轮）通过；原样上游由确定性外部通知回归明确拒绝，不把其随机通过当作候选门禁。

这些是调度器组件证据，与 w0000 实景完成次数分别统计。上游记录提供背景，不能替代本项目实景因果验证。

## 重现

在 workstation 仓库工作树运行：

```bash
PX4_BUILD_JOBS=8 scripts/build_px4_lockstep_candidate.sh /data/scratch/px4-clock-build-20261108
```

脚本拒绝非 `/data` 源码目录、非预期镜像/源码版本、与调度器无关的已跟踪源码修改，以及超过 12 的编译并行度。它固定源码及必要子模块，建立独立编译镜像，在 64 GiB、默认最多 8 CPU 的无 GPU 容器中先跑三方测试再编译。原镜像只读作构建基底。

输出是源码树 `clock-candidate-artifacts/`，含测试日志、编译器/包版本、二进制 SHA、镜像 ID 和 `ldd` 依赖核验。运行镜像不继承编译镜像的私有库；最终 `ldd` 必须无 `not found`。仓库外 `runs/px4-lockstep-candidate-ready-20261108.json` 仅在完整编译、依赖检查和测试完成后写入，未出现 marker 不视为可运行。

本分支不修改 SITL 启动默认值。实景验收仍走全局 `scripts/sitl_lock.py`，仅 GPU0/1。三角翼槽位 3/4 与时钟验收槽位 0–2 分开。
