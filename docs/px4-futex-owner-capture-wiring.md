# 可选的失败阶段 futex owner 观测接线

本接线基于 `76736981` 与固定 observer 模块 `bca5ba82`（本分支直接继承固定原提交）。仅扩展失败现场证据，不修复 PX4/Gazebo，不改任务、45 秒 watchdog、通过断言、现役模型、SITL 锁、原两轮采样顺序或每轮 GDB 的 7.5 秒分配（默认总 30 秒）。未修改或启动任何运行中的 767 source/cohort。

## 显式启用

`capture_stall(..., futex_layout_proof_path=reviewed_path)` 优先于环境变量 `MD_PX4_STALL_FUTEX_LAYOUT_PROOF`。参数 `None` 采用环境配置；显式空字符串禁用该环境配置。默认没有 proof 时，不调用 owner 模块，也不读取进程 mem。

配置路径应指向已审查的单对象 layout proof JSON（不是包含多轮结果的 roundtrip.json）。单次读上限 64KiB；缺失、非文件、坏 JSON、错误形状和缺精确 libc SHA 均只记录 observation_error，不自动寻找 scratch 文件、不获取权限、不改变后续 GDB/任务处理。正常配置记录 exact path、完整文件 SHA、read_bytes、libc SHA；超限文件只记录 bounded_prefix SHA，明确拒绝，不假称完整文件哈希。模块另外校验目标实际映射 libc SHA/ABI/布局证明，不能仅凭文件存在或 glibc 版本字符串放行。

本轮 root 可审查的 b5 proof 独立副本（不作为代码默认值）：

```
/data/projects/Multi-Detect-env/runs/clock-new-budget-failure-20261109/analysis-owner-layout-proof.json
SHA256 14545b6d2c47f1097df14ec504c9688b7f96be7c14f3b3b065aeb1baf7d02f09
```

副本与原实测文件 `/data/scratch/procfs-futex-owner-20261109/b5-proof/layout-proof.json` 复制前后 SHA 完全相同；副本设为只读。b5 libc SHA 是 `8db37cf3f2169f59a0f07ef1fea308c35656668c64c8ff294e1860f4121eb161`。下一 cohort 必须由 root 明确设置 proof 路径并记录实际 source/image；本分支未启动 cohort，未宣称捕获真实 Gazebo owner。

## 观测边界和证据

`_read_host_threads` 在原有 task/stat 读取中增加字段22 `starttime_ticks`；不在失败后重新采集身份冒充旧样本。host fallback、缺 main/starttime、bool 或非正身份、多个 GZ PID、重复 main、未知命令都不调用 owner 模块。仅当前 sample 中唯一、明确识别的 `gz sim` host PID/main 可成为候选；该 PID 与随后 GZ GDB 选择一致。

每次 capture 只在首轮既有 procfs sample 之后、首次 GDB 之前调用一次。第二轮不调用。因此模块原上限每 capture 至多两次 20B（总40B）进程内存，地址只取该 main 的 kernel syscall；只读普通 recursive mutex 候选，无地址扫描/ptrace/信号/调用。结果保存在首轮 `procfs_futex_owner_candidate`；顶层 `futex_owner_configuration` 绑定 proof。模块的 errors、denied/unsupported、changed、unmapped、maps 256KiB/250ms 软预算拒绝等原始结果全部保留，不重试、不放宽，也不转为 pass。可读稳定候选仍是 candidate_only，NodeShared mutex 身份和死锁环均未证明。

模块250ms只在有界读取之间检查，不是内核 I/O/调度的硬 wall deadline。启用时会增加一次观测开销，原 GDB timeout/请求 sleep/45 秒判定不变；实际采样间隔仍以时间戳衡量，不把 after_3s 标签当作精确间隔。

## 验证边界

CPU tests 覆盖：原 task/stat starttime 与带右括号 comm；无配置零调用；API/env 优先级及精确 proof SHA；坏/超大 proof；host/unique main/starttime/命令拒绝；denied/unsupported 保留；仅首轮一次、原 GDB 分配及相对顺序；真实 observer 经纯临时文件 fake procfs 的 40B/namespace owner 完整往返。继承模块测试另使用新建自有 CPU pthread child 验证，正常释放退出，无 GPU/SITL、无现有现场进程读内存或 attach。

focused raw log：`runs/clock-new-budget-failure-20261109/analysis-owner-focused.log`。这不是生产修复资格，也不是新时钟失败已解决的证据。

最终检查：三个相关测试文件 **56 passed / 3.36s**；补强拒绝路径的零调用断言后，相关10例 **10 passed / 0.32s**，日志另存 `analysis-owner-refusal-focused.log`。`git diff --check` 通过。全文件 ruff 仍有45项继承诊断；按本次新增行核对为零新增诊断，原始及差分结果分别保留 `analysis-owner-ruff.json` 与 `analysis-owner-lint-delta.json`，不声称全文件 lint clean。
