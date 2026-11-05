# gimbal-count-cones 联合数据过计数归因（2026-11-05）

## 结论

三次运行使用的是同一个 `gen_cnt2_w0000` 世界：世界 SDF SHA-256 均为 `71ff6c6ede91d05fd0ea8e703095d61b2f8621d67c9531f7d62f687fb7022987`，模型 SHA-256 也相同。世界中有三个橙色锥，真值 N/E 位置约为：`[9.195,-15.402]`、`[19.771,23.886]`、`[-21.995,28.631]`。

已确认的根因是 **`camera_pose` 缺失时仍回退使用明确标记为 stale 的 `gimbal_state`**。`streams.py` 只在云台反馈新鲜时写入 composed `camera_pose`；旧投影代码却在相机姿态缺失时无条件用 `gimbal_state` 重新组合射线。计数器将这个错误交点当作新位置，位置差超过有效去重半径后就增加 count。

具体证据是 joint2 step 105：`camera_pose` 缺失、gimbal feedback age `1.315 s` 且 `fresh=false`，记录位置 `[-16.95,29.41]` 距最近真锥 `5.11 m`。step 29 同一锥在 `[-22.98,28.93]`，两点相距 `6.05 m`，超过 4 m 配置上限，因此去重允许了重复计数。step 116 和 129 同样在无新相机姿态的帧产生错误位置，分别离最近真值 `13.81 m` 和 `16.31 m`。

所有计数确认帧的 vehicle pose `sim_skew_ms` 都是 `0`；stale 帧的地面射线下视角约 `20.9°–44.0°`，没有接近水平。姿态时间错位和低掠射角不是主要原因。修复后固定路径记录事件回放得到 gimbal-9/joint/joint2/main-19 分别为 `3/3/3/3`；去重半径保持 4 m。

## 提交差异和 composed coverage pose

`git log --ancestry-path ff8812ec..00395b6b` 只有两个提交。另一个需要先校正的事实是：joint episode 自身的 provenance 是 `14dc89343689`，不是任务描述中写的 `00395b6b`；joint2 才是 `00395b6b`。

- `14dc8934`：发布 Gazebo rotor/gimbal joint state，并把它按 RGB 时间戳绑定到 frame metadata。
- `00395b6b`：记录 `rotorVelocitySlowdownSim=10`，并补充 joint 回归归因文档。

该范围没有 coverage planner、去重或几何代码变更。`40c56f64`（“Allow coverage geometry to use a composed camera pose”，14:25:33 +0800）在时间上确实位于 `ff8812ec`（11:25:07）和 `00395b6b`（15:24:24）之间，但它及其 merge `5b6fc4f2` 不在 joint/joint2 provenance 所指向的 source commit 祖先链中；用 `git show <commit>:<path>` 检查 `ff8812ec`、`14dc8934`、`00395b6b` 时，coverage geometry 都没有这次改动。因此，**它是一个需要排除的时间相关候选，不是已证实的 source-code 差异**。三份 provenance 只有 `dirty:true` 和 changed-files 数量，没有运行时 tracked-tree 快照，不能证明该候选是否以未提交代码进入某次运行。

三份 episode 的新鲜帧记录了 `camera_pose.source="composed"` 和 `gimbal_feedback_fresh=true`；过期帧省略 `camera_pose` 并保留 `gimbal_state.fresh=false`。`build_observation()` 先以 MAVLink `GIMBAL_DEVICE_ATTITUDE_STATUS` 生成 composed pose，再合并 Gazebo joint sidecar 到 `gimbal_joint_state`。joint sidecar 是附加元数据；本次修复不会把它当作缺失 composed camera pose 的替代品。三份运行的 provenance 都是 `dirty:true`，但对应 source worktree 的 tracked diff 为空，不能用 dirty 标志推断另一个 coverage 实现。

`camera_pose` 的逐帧组成是三次 episode 都记录到的共同观测字段；`40c56f64` 的 coverage-footprint 逻辑是否实际运行则没有 provenance 证据。episode 实际路径不同，不能视为逐帧 A/B；但 stale 回退缺陷能直接解释为什么没有新姿态的帧仍产生 count 位置。

## 每步路径、计数增量和去重

| 运行 | 步数 | 平移/转向/悬停 | `distance_flown_m` | 计数发生步骤（位置 N/E） |
|---|---:|---:|---:|---|
| 无 sidecar，`ff8812ec` | 92 | 45 / 32 / 14 | 414.1 | 4: `[19.6,23.5]`；8: `[10.2,-15.0]`；30: `[-22.3,28.7]` |
| joint，`14dc8934` | 130 | 58 / 55 / 16 | 524.9 | 6: `[17.8,21.0]`；12: `[9.2,-15.2]`；61: `[22.8,25.7]`；78: `[-19.2,29.1]`；88: `[-23.0,28.5]` |
| joint2，`00395b6b` | 140 | 53 / 73 / 13 | 470.5 | 4: `[19.8,23.7]`；8: `[9.1,-14.7]`；29: `[-23.0,28.9]`；105: `[-16.9,29.4]`；116: `[5.0,-2.2]`；129: `[12.6,9.2]` |

无 sidecar 的三个确认位置分别落在三个真值锥附近，报告为 `matched_count=3,false_findings=0`。它的前几步是 15 m 前移并下降 4 m；joint 的对应早期前移约 8.1 m、下降约 0.92 m，随后动作序列也多出 23 次转向和 13 次平移。这是实际闭环路径差异的直接证据，但不能单凭一次运行把差异归给某个单独输入。它在同一物体的后续 track 上仍看到重复，例如第一锥附近约 `[20,23]`、第二锥附近约 `[9,-15]`、第三锥附近约 `[-22,28]`，但 count 状态保留了一个空间结果。

joint 的五个位置里，step 6 的 `t1` 是 stale 帧，离第二锥 `3.48 m`；step 61 的 `t36` 是 fresh 帧，离同一锥 `3.53 m`。两点相距 `6.84 m`，大于 4 m 去重上限，所以旧行为把它们计成两个。step 78 的 `t44` 也是 stale 帧，离第三锥 `2.85 m`；step 88 的 fresh `t51` 离第三锥 `1.01 m`。后两点相距 `3.86 m`，但其有效半径受合并位置 sigma 限制（约 `1.74 m`），不能靠扩大配置半径处理。按 fresh-pose gate 重放后，保留 step 12、61、88 三个确认。

joint2 的前三项分别对应三个真锥，误差分别是 `0.17 m`、`0.70 m`、`1.03 m`，姿态新鲜。step 105 的 `[-16.9,29.4]` 是 stale 帧 ground 位置，离第三锥 `5.11 m`，比 step 29 的 `[-23.0,28.9]` 偏移 `6.05 m`。后两项也都在 stale 帧：

- `t35` 的 step 116 位置 `[5.0,-2.2]` 是 6-view fused estimate（baseline `10.77 m`），离最近锥 `13.81 m`；同一 track 的位置链路因此已无法代表一个稳定地面目标。
- `t67` 的 step 129 最终融合为 `[12.63,9.23]`，离最近锥 `16.31 m`。该帧同样没有 camera pose，gimbal feedback age 为 `1.121 s`。

所以要区分“物体层面”和“评估器层面”：joint 的两个额外 count 是已有物体的重复；joint2 的三个额外 count 中一个是第三锥的重复，两个是投影/身份链路产生的 false positives。报告的 `false_findings` 把所有未能一对一匹配的额外结果都算进去。

## 位置链路与数值证据

`scripts/onboard_policy.py` 用检测框底边中心调用 `pixel_ground_point()`，并将该地面点（启用多视角估计时为 `target_position()`）放入 count confirmation。`MissionExecutor._count_confirmation()` 对新旧位置调用 `_counted_at()`；默认 `count_dedupe_m=4.0`，存在 footprint 时有效半径为 `max(1.5 m, 0.75 × footprint, min(4 m, 合并 sigma))`。

旧 `camera_projection.py` 在 `camera_pose` 缺失时仍会从 `gimbal_state` 组合相机方向，没有检查 `fresh=false`。修复后，明确 stale 的 camera/gimbal pose 使 ray 和地面投影返回 `None`；count confirmation 没有当前位置时会暂不计数、不耗尽 track，等待新鲜位置。相机姿态缺失不会再回落成固定相机射线。

| 运行 / step / track | 记录位置 N/E | 最近真值误差 | feedback age / fresh | 地面射线下视角 | fresh-pose 回放 |
|---|---|---:|---|---:|---|
| gimbal-9 / 4 / t1 | `[19.61,23.48]` | 0.44 m | 0.125 s / true | 29.10° | 保留 |
| gimbal-9 / 8 / t9 | `[10.21,-15.03]` | 1.08 m | 0.023 s / true | 23.24° | 保留 |
| gimbal-9 / 30 / t20 | `[-22.34,28.72]` | 0.36 m | 0.160 s / true | 38.36° | 保留 |
| joint / 6 / t1 | `[17.82,21.00]` | 3.48 m | 1.180 s / false | 44.02° | 拦截 |
| joint / 12 / t7 | `[9.21,-15.19]` | 0.21 m | 0.040 s / true | 22.92° | 保留 |
| joint / 61 / t36 | `[22.82,25.67]` | 3.53 m | 0.615 s / true | 27.05° | 保留 |
| joint / 78 / t44 | `[-19.18,29.09]` | 2.85 m | 1.254 s / false | 44.00° | 拦截 |
| joint / 88 / t51 | `[-22.99,28.49]` | 1.01 m | 0.211 s / true | 33.41° | 保留 |
| joint2 / 4 / t3 | `[19.82,23.72]` | 0.17 m | 0.565 s / true | 28.67° | 保留 |
| joint2 / 8 / t9 | `[9.08,-14.71]` | 0.70 m | 0.442 s / true | 23.56° | 保留 |
| joint2 / 29 / t2 | `[-22.98,28.93]` | 1.03 m | 0.440 s / true | 36.43° | 保留 |
| joint2 / 105 / t52 | `[-16.95,29.41]` | 5.11 m | 1.315 s / false | 28.39° | 拦截 |
| joint2 / 116 / t35 | `[5.03,-2.24]` | 13.81 m | 1.384 s / false | 21.05° | 拦截 |
| joint2 / 129 / t67 | `[12.63,9.23]` | 16.31 m | 1.121 s / false | 20.90° | 拦截 |
| main nightly 19 / 2 / t1 | `[19.66,23.63]` | 0.28 m | 固定相机 / N/A | 29.77° | 保留 |
| main nightly 19 / 6 / t3 | `[-21.16,28.62]` | 0.83 m | 固定相机 / N/A | 21.09° | 保留 |
| main nightly 19 / 12 / t7 | `[9.28,-15.21]` | 0.21 m | 固定相机 / N/A | 42.72° | 保留 |

`sim_skew_ms` 在这些确认帧均为 `0.0`。旧投影在 stale 帧仍能产生约 `20.9°–44.0°` 的有效射线；这些并非接近水平的掠射线。已知计数位移与 freshness flag 一致，不支持把 pose 时间对齐或低掠射角定为主要故障。

固定路径记录事件重放命令：

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
python scripts/replay_count_position_stability.py \
  artifacts/benchmarks/nightly-linux-r21idr3-r24plan-gimbal-9/gimbal/gimbal-count-cones/campaign/gen_cnt2_w0000/episodes/episode-0000-3529c9ca \
  artifacts/benchmarks/nightly-linux-r21idr3-r24plan-gimbal-joint/gimbal/gimbal-count-cones/campaign/gen_cnt2_w0000/episodes/episode-0000-b21d3a0d \
  artifacts/benchmarks/nightly-linux-r21idr3-r24plan-gimbal-joint2/gimbal/gimbal-count-cones/campaign/gen_cnt2_w0000/episodes/episode-0000-41e24703 \
  artifacts/benchmarks/nightly-linux-r21idr3-r24plan-19/main/count-cones/campaign/gen_cnt2_w0000/episodes/episode-0000-c8fc332c
```

| 记录运行 | 原记录 count | stale pose gate 后 | 被拦截的确认步骤 |
|---|---:|---:|---|
| gimbal-9 | 3 | 3 | 无 |
| joint | 5 | 3 | 6、78 |
| joint2 | 6 | 3 | 105、116、129 |
| main nightly 19 固定相机 | 3 | 3 | 无 |

该脚本保持已记录的动作、track 和确认步骤，只用当前投影函数判断确认帧能否提供有效射线；它不重跑检测器、身份模型、动作或 track association。另用 `replay_onboard_episode.py` 做的 full open-loop policy replay 为修复前 `3/2/3/3`、修复后 `3/0/3/3`，joint 在重新运行 tracker 后出现路径/track 分叉，不能用它代表固定记录轨迹上的 count event；原 episode 记录的 count 仍是 `3/5/6/3`。

## sidecar、相机姿态和投影证据

无 sidecar 的 `gimbal_joint_state.source` 是 `GIMBAL_DEVICE_ATTITUDE_STATUS`，joint/joint2 则是 `gazebo_joint_state`，并带有速度；joint/joint2 还带 rotor `velocity_slowdown=10`。frame-server 统计显示 sidecar 传输本身正常：

- joint：`12876` 个 joint frames，`joint_state_invalid=0`、`joint_state_missing_or_stale=0`。
- joint2：`13216` 个 joint frames，invalid/missing/stale 均为 0。
- RGB、深度分辨率和内参三次相同（RGB 1280×960，`fx=fy=539.936...`；深度 320×240）。

这排除了“sidecar 损坏或错配导致所有帧投影错误”的解释。sidecar 数据有效，但相机投影 fallback 读的是另一份 stale MAVLink feedback；不能用有效的 joint sidecar freshness 替旧 `camera_pose` 投影背书。计数步骤中，joint 的 step 6/78 和 joint2 的 105/116/129 均没有 composed pose，gimbal feedback 年龄为 `1.12–1.38 s`。

`streams.py` 按 RGB frame freshness 决定是否产出 `camera_pose`；sidecar 后合并到 `gimbal_joint_state`。过期帧因此必须没有 count position，而不能拿 MAVLink stale state 作 fallback。该语义现由 `tests/test_camera_mount.py` 的记录 step 105 回归锁定。

## 延迟和竞争

从 `steps.jsonl` 计算的中位数/尾延迟如下：

| 运行 | total p50 / p95 / max | frame-before p50 | frame-after p50 | slow-model p50 | slow calls |
|---|---:|---:|---:|---:|---:|
| 无 sidecar | 9.19 s / 39.70 s / 49.06 s | 435 ms | 443 ms | 0 ms | 41/92 |
| joint | 8.82 s / 41.23 s / 45.17 s | 473 ms | 521 ms | 82.5 ms | 65/130 |
| joint2 | 10.58 s / 55.80 s / 122.81 s | 549 ms | 590 ms | 350 ms | 78/140 |

joint2 还有更高的 observation-pack 中位数（79 ms 对 59–61 ms）和更长 frame wait；这与任务描述的“同 GPU 旁边运行另一个 nightly”一致，也与主机/GPU 竞争假说相符。可是 joint 的 p50 total 并不比基线更慢，且 joint 已经从 3 变成 5，因此竞争不能单独解释初始过计数。产物没有 GPU utilization、显存、CPU load 或实际模型吞吐的硬件计数器，所以竞争的因果强度只能写成“合理但未证实”。

## 验证

- 本轮复核的 camera mount、onboard loop、step triangulation 和 MissionExecutor 四个测试文件：204 passed。
- `scripts/run_onboard_gate.py -q -p no:warnings`：PASS，CPU-only，134 s。
- 固定相机 main nightly 19：fresh-pose 事件回放前后都是 3；现有固定相机附近物体共视去重测试通过。
- 未运行 SITL、Docker、GPU 训练或联网下载。

## 只读证据位置

- [无 sidecar report](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-gimbal-9/gimbal/gimbal-count-cones/report.json)
- [joint report](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-gimbal-joint/gimbal/gimbal-count-cones/report.json)
- [joint2 report](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-gimbal-joint2/gimbal/gimbal-count-cones/report.json)
- 三份 episode 的 `campaign/gen_cnt2_w0000/episodes/*/steps.jsonl`、`mission_summary.json` 和 `episode.json`
- 三份 `report.json` 的 `provenance` 字段（含 source commit、world/model hashes 和 frame-server 命令）
