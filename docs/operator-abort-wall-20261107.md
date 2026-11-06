# operator-abort 4800 s wall ceiling 调查（2026-11-07）

## 结论

28-head 的 `operator-abort` 不是因为 policy 推理耗尽了 4800 s，也不是 PX4 进程退出。episode 在 66 步、episode wall 4933.3 s 后失败；失败快照显示容器仍运行、PX4 进程仍存活，主机 load average 为 `[261.95, 251.49, 248.08]`。

时间主要花在 primitive/executor 等待，尤其是 abort 之后反复超时的上升 primitive。按 `steps.jsonl` 的 66 条 `timing` 相加：`command_and_primitive_wait_ms` 为 **4539.544 s**（`total_ms` 的 94.8%），其中 `timeout_ms` 为 **4017.842 s**；前后取帧等待合计 **228.354 s**（4.8%），policy 合计 **5.870 s**（0.12%），PX4 ack 等待为 **0 s**。这些阶段有包含关系，不能把它们再相加当作独立 wall 时间；`total_ms` 合计为 **4787.157 s**，与记录中从首个观测到末个观测的 wall 间隔 4782.940 s 一致到记录/收尾开销范围。

abort 已被送到 mission policy：仿真地面事件在 mission 时间 120.000 s 记录，随后 124.148 s 的 decision 为 `return_now`，原因是 `explicit operator abort`，后续 execution 标签为 `recovery` / `return_home`。因此“指令进入并切换了恢复状态”是已证明的；“返航动作成功完成”不是，episode 在完成前触发 ceiling。

## 1. 每步 wall 时间去了哪里

证据目录：
`artifacts/benchmarks/nightly-linux-r21idr3-r24plan-28-head/main/operator-abort/campaign/gen_cnt2_w0003/episodes/episode-0000-06a1a47d/`。

| timing 字段/归类 | 66 步合计 | 占 `total_ms` | 说明 |
| --- | ---: | ---: | --- |
| frame wait = `pre_frame_wait_ms + post_frame_wait_ms` | 228.354 s | 4.8% | 等待 before/after 相机帧；均值 1.725 s + 1.735 s/步 |
| primitive/executor wait = `command_and_primitive_wait_ms` | 4539.544 s | 94.8% | 最大项；均值 68.781 s/步 |
| 其中 `timeout_ms` | 4017.842 s | 83.9% | 28 步为 timeout，主要集中在 abort 后 |
| 其中 `motion_ms` | 663.548 s | 13.9% | 是 primitive wait 的子项，不应再与其相加 |
| policy = `policy_total_ms` | 5.870 s | 0.12% | detector 2.143 s、slow model 1.028 s 已包含在 policy 相关记录中 |
| ack = `px4_ack_wait_ms` | 0 s | 0% | 66 步均为 0；step ack 记录为 `action_ack=true` |

前 38 步（abort 生效前后边界前）`command_and_primitive_wait_ms` 合计 521.702 s，均为成功 primitive；step 38–65 合计 **4017.842 s**，28 步全部 `timeout`。最后五步的 executor wait 分别为 174.209、182.671、200.433、170.230、167.633 s，已经远高于 policy 和 frame wait。

## 2. PX4 仿真时间与 wall 时间

用第一步 `obs_before.pose_time_boot_ms=28580` 到最后一步 `obs_after.pose_time_boot_ms=740424`，PX4 仿真时间前进 **711.844 s**。同一观测窗口的 wall 时间为 **4782.940 s**，real-time factor（sim/wall）为 **0.149**。

它是持续慢速，而不是一次长时间完全没有样本的空洞：

- abort 前边界（step 0–37）：wall 688.834 s、sim 139.292 s，RTF **0.202**。
- abort 后（step 38–65）：wall 3990.527 s、sim 572.156 s，RTF **0.143**。
- 10 步窗口 RTF 从早期约 0.20–0.22 下降到末段约 0.13–0.14；step 间 PX4 时间戳持续增加。
- 最后 10 个 step（56–65）的 `px4_clock_diagnostics` 都有递增的 `px4_clock_s`（从 558.392 到 740.404），且 `clock_moved_again_after_45s=false`。这表示记录到的是很慢的持续推进；不能据此断言 Gazebo 每个内部组件都没有短暂停顿。

因此可证明的是“高负载下 lockstep/PX4 仿真极慢，abort 后更慢”；不能仅凭这些 episode 文件证明具体是 Gazebo 渲染、CPU 调度、传感器链路或某一单独进程造成。

## 3. 历史 operator-abort 对照

下表的 `wall` 来自各自 `episode.json` 的 `finished_wall_time - created_wall_time`；`sim` 为该 episode 首个 before 到最后一个 after 的 PX4 时间增量；RTF = `sim/wall`。27-off45-e2 有两次保留的 episode，均列出。

| nightly | steps | wall | PX4 sim 增量 | RTF | 结果 |
| --- | ---: | ---: | ---: | ---: | --- |
| 22-off45 | 46 | 1721.1 s | 145.5 s | 0.085 | complete |
| 26-off45 | 48 | 1301.6 s | 145.6 s | 0.112 | complete |
| 27-off45-e2（episode 1） | 42 | 1112.1 s | 145.6 s | 0.131 | complete |
| 27-off45-e2（episode 2） | 42 | 739.9 s | 145.1 s | 0.196 | complete |
| 16 | 44 | 758.3 s | 145.3 s | 0.192 | complete |
| 17 | 44 | 836.8 s | 144.9 s | 0.173 | complete |
| 19 | 22 | 576.0 s | 65.3 s | 0.113 | failed：PX4 clock stalled |
| 20 | 46 | 1094.9 s | 144.9 s | 0.132 | complete |
| 21-loggeroff | 46 | 1530.6 s | 145.6 s | 0.095 | complete |
| 7 | 44 | 1041.5 s | 144.1 s | 0.138 | complete |
| 23-def180 | 0 | 65.5 s | n/a | n/a | failed：sensor wire stream |
| 24-def45 | 0 | 262.1 s | n/a | n/a | failed：takeoff wait |
| 5 | 0 | 135.9 s | n/a | n/a | failed：offboard wait |
| **28-head** | **66** | **4933.3 s** | **711.8 s** | **0.144** | **failed：4800 s ceiling** |

历史成功 episode 通常在约 145 s PX4 sim 时间完成；本次在 abort 后没有结束 return-home，而是继续到 740 s PX4 sim 时间和 66 步。RTF 0.144 本身落在历史慢 episode 的范围内，异常之处是动作/恢复阶段持续了远多于历史的仿真时间和步数。23、24、5 的 episode 在开始记录 step 之前就失败，故无 PX4 step 时间戳可算 RTF。该表覆盖 `artifacts/benchmarks/` 下有保存 `operator-abort` episode 记录的历史 nightly；不代表未保留的运行。

## 4. 是否在取得任务进展，及重复上升动作

step 0–37 的 monitor 路径有 38 个 `success` primitive，包括前进、转向和 hold，水平位置确实随前进动作改变。step 38–65 则是 28 个连续的：

```text
translate: forward_m=0, right_m=0, up_m=15, yaw_delta_deg=0
```

它们不是仍在执行 monitor 计划，而是 abort 后 `return_home` 恢复动作。每一步都 `outcome.status=timeout`，安全标签为 `status=clipped`、`reasons=["max_vertical_step"]`、`rejected=false`。

这些动作没有产生所要求的稳定上升：例如 step 64 的 NED z 从约 `-0.481 m` 到 `-0.441 m`，step 65 从 `-0.442 m` 到 `-0.533 m`，都不是 15 m 的垂直位移；同时 x/y 仍有数米级变化。末步 after 的高度 `alt_m=0.648`，而 episode 开始时约 18.44 m。故可以说飞机在低空继续漂移/运动，但不能说这些重复命令把飞机按目标拉升或完成了返航。

operator abort 的链路证据如下：

1. `onboard` 配置声明 `sim_ground_events: [{"t_s":120,"event":"abort"}]`。
2. `mission.jsonl` 记录 `sim_ground_event`（`event=abort`，mission 时间 120.000 s）。
3. 之后 decision 为 `action=return_now`、`reason=explicit operator abort`，execution 为 `step=recovery`、`skill=return_home`、`supervisor_action=return_now`。
4. `steps.jsonl` 的每步 ack 仍是 `action_ack=true`，但 `px4_ack_wait_ms=0`；这只能证明 setpoint 流记录为已接受，不能证明返航目标已达到。

## 5. 4800 s 如何得到，以及 28-head 其他 mission

本次 episode 的 provenance command 明确传入 `--max-episode-s 2400`。collector 的 `_EpisodeProgressWatchdog` 使用：

```text
ceiling = maximum_s + max(maximum_s, 600.0)
        = 2400 + max(2400, 600)
        = 4800 s
```

该 watchdog 在车辆时间仍有进展时允许超过 nominal 2400 s，但最多到 4800 s；因此“PX4 还在发消息”不会无限延长 episode。失败文本和 episode 元数据都记录为 `episode exceeded its hard wall-clock ceiling (4800s)`。episode 全生命周期为 4933.3 s；watchdog 在 step 边界检查，最后一个 170.1 s 的 primitive 先越过 4800 s，下一次检查才抛出异常，随后还有清理开销。失败快照发生在 episode wall 约 4930.3 s 时。

28-head `main` 下其余 13 个 mission 的同一推导 ceiling 为 4800 s；`count-cones` 的命令使用 `--max-episode-s 5400`，故其 ceiling 为 10800 s。按 `episode.json` 生命周期比较：

| mission | steps | wall | ceiling | ceiling 余量 | status |
| --- | ---: | ---: | ---: | ---: | --- |
| budget-reserve-return | 40 | 952.7 s | 4800 s | 3847.3 s | complete |
| comms-free-count | 81 | 1865.6 s | 4800 s | 2934.4 s | failed：sensor frame 15 s |
| comms-free-patrol-timeout | 40 | 1068.4 s | 4800 s | 3731.6 s | complete |
| count-cones | 6 | 334.7 s | 10800 s | 10465.3 s | failed：PX4 clock stalled |
| count-fire-hydrants | 144 | 2349.0 s | 4800 s | 2451.0 s | complete |
| english-find-person | 11 | 331.7 s | 4800 s | 4468.3 s | complete |
| find-suv-land | 25 | 337.4 s | 4800 s | 4462.6 s | complete |
| high-risk-monitor-link-loss | 44 | 1025.3 s | 4800 s | 3774.7 s | complete |
| inspect-postbox | 31 | 661.8 s | 4800 s | 4138.2 s | complete |
| monitor-backpacks | 107 | 1912.3 s | 4800 s | 2887.7 s | complete |
| operator-pause-resume | 83 | 1430.1 s | 4800 s | 3369.9 s | complete |
| patrol-fence-backpacks | 86 | 4175.0 s | 4800 s | 625.0 s | complete |
| survey-site-overlap | 48 | 1810.7 s | 4800 s | 2989.3 s | complete |
| **operator-abort** | **66** | **4933.3 s** | **4800 s** | **-133.3 s** | **failed：ceiling** |

这说明 operator-abort 是 28-head 中唯一超过 4800 s hard ceiling 的 episode；`patrol-fence-backpacks` 最接近但仍剩约 625 s。其他 mission 的失败不是 ceiling 触发。

## 已证明 / 未证明

已证明：wall 主要消耗在 primitive/executor wait；28 步上升 primitive 全部 timeout；PX4 仿真时间持续前进但 RTF 低；abort 事件被记录并切换到 `return_now` / `return_home`；重复上升动作没有完成目标垂直位移；4800 s 来自 2400 s nominal budget 加同等长度 hard extension；operator-abort 超过 ceiling，而其他 13 个 mission 没有。

未证明：高 load 的直接因果来源；是 Gazebo physics、渲染、CPU 调度、传感器 back-pressure 还是 PX4/Gazebo lockstep 内部等待；为什么 `return_home` 恢复在当前仿真中持续发出相同的 15 m 上升 primitive；也不能仅凭 `action_ack=true` 断言 PX4 已完成返航。

本次没有修改代码、没有提高 ceiling，也没有启动 SITL/Docker；只新增本调查文档。
