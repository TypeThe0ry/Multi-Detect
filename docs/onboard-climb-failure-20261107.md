# Onboard 爬升失败保护（2026-11-07）

## 改动

返航恢复现在把已执行 primitive 的结果回灌给 `MissionExecutor`。只有当前步骤是 `return_home`、动作是纯上升 `translate`、结果为 `timeout`，并且相邻 PX4 NED 观测的高度增益小于 0.5 m 时，才会累加失败次数。连续两次满足条件后，不再继续发同一类爬升；执行器进入 `climb_failed_suspected_loss_of_control` 终态。事件同时写入 mission log、`alerts` 和 onboard policy 的 supervisor log。

触发前会读取本次和上一次 after 观测中的姿态、`landed_state` 与 `SYS_STATUS` 传感器健康位。只有高度不超过 2.5 m 且 PX4 仍报告 airborne 时才允许触发；倒置/翻滚（roll 或 pitch 绝对值至少 90 度）、传感器 present/health 不一致或连续两次没有高度增益，任一证据成立即可确认。确认后通过现有 supervisor 的 `land_now` 升级路径请求 PX4 LAND；没有新增空中 disarm 路径，也没有改变 supervisor 的优先级。

gimbal 和 fixed camera 共用同一 primitive/outcome 边界，判定不读取相机挂载类型。landing、pause/hold、水平动作和普通 mission 步骤不进入计数。

## 阈值与理由

- 连续失败次数：2。一次 timeout 可能只是通信或控制器暂时没有在 deadline 内达到目标；两次连续且无高度增益才停止重复，能覆盖报告中的 27 次重复上升，同时保持一次 timeout 后恢复的正常路径。
- 有意义高度增益：0.5 m。它高于位置噪声和记录量化误差，但远低于 15 m 上升 setpoint；慢速真实爬升只要持续取得进展就会清零计数。
- 近地面：2.5 m。只有低空时才把“没有爬升”与倒置/传感器异常组合解释为失控，避免高空的普通慢爬升被误判。
- 健康确认窗口：最近两次 after 观测，且要求 PX4 boot time 不倒退。所有时间和进度都来自 `pose_time_boot_ms` 与 NED 高度，不使用 host wall time，因此低 real-time factor 不会缩短有效爬升窗口。

## 回放与扫描

指定正例 `nightly-linux-r21idr3-r24plan-28-head/main/operator-abort/campaign/gen_cnt2_w0003/episodes/episode-0000-06a1a47d/steps.jsonl` 在第二个连续无增益 timeout、`step_index=41` 命中；第一次对应的 timeout 不命中。四个 nightly 根目录扫描结果如下（含根目录下的 gimbal 子目录）：

| nightly | 可回放 episode | 规则命中 |
| --- | ---: | --- |
| `r24plan-22-off45` | 14 | 无 |
| `r24plan-26-off45` | 16 | 无 |
| `r24plan-27-off45-e2` | 27 | 无 |
| `r24plan-28-head` | 17 | 仅 `operator-abort/episode-0000-06a1a47d`，step 41 |

## 尚未验证

本次没有启动 SITL、Docker 或真实飞行，也没有验证 PX4 在各硬件/参数组合下对 LAND failsafe 的最终执行时延。回放验证了指定 operator-abort、低 real-time factor 的 sim-time 口径、单次 timeout、取得进展、landing/hold 负例和 gimbal 目录扫描；尚未覆盖真实传感器位图差异、飞行中 LAND 命令被 PX4 拒绝或 supervisor 与已有 PX4 failsafe 同时接管的现场时序。
