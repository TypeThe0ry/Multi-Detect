# gimbal-count-cones 联合数据过计数归因（2026-11-05）

## 结论

三次运行使用的是同一个 `gen_cnt2_w0000` 世界：世界 SDF SHA-256 均为 `71ff6c6ede91d05fd0ea8e703095d61b2f8621d67c9531f7d62f687fb7022987`，模型 SHA-256 也相同。世界中有三个橙色锥，真值 N/E 位置约为：`[9.195,-15.402]`、`[19.771,23.886]`、`[-21.995,28.631]`。

归因是组合因素，但主次可以区分：

1. `ff8812ec` 的无 sidecar 运行走了较短、较少重访的闭环路径，3 个物理锥各被计数一次。
2. `14dc8934` 的 joint 运行走了明显更长的路径，额外观察暴露了位置估计和空间去重的缺口；报告计数 5，其中 3 个是真锥，另外 2 个是已有锥的重复观察（评估器把它们记为 `false_findings`）。
3. `00395b6b` 的 joint2 延续了这种问题；计数 6，其中 3 个是真锥，`[-16.9,29.4]` 是第三个锥的偏移重复，`[5.0,-2.2]` 和 `[12.6,9.2]` 没有对应真值，属于真实的 false positives。其同一 track 在不同步骤跳到多个地面位置，说明投影/地面位置估计不稳定。
4. GPU/CPU 竞争可以解释 joint2 更差的等待时间和长尾，但不能单独解释 joint 运行已经出现的 +2。现有产物没有 GPU/主机利用率时序，不能把竞争定为唯一原因。

因此，最符合证据的解释是：**三次运行实际走了不同闭环路径，joint/joint2 的更多重访暴露了相机姿态时间对齐、地面投影和空间去重的不稳定；joint2 的并发负载进一步放大了这种不稳定。** 不能把这归因于一个只在 joint 版本引入的 composed-pose coverage merge：该 merge 不在两份 source commit 之间，且三次 episode 都已经记录 `camera_pose.source="composed"`。sidecar 本身目前没有证据表明它改变了 RGB/depth 或直接替换投影输入。

## 提交差异和 composed coverage pose

`git log --ancestry-path ff8812ec..00395b6b` 只有两个提交。另一个需要先校正的事实是：joint episode 自身的 provenance 是 `14dc89343689`，不是任务描述中写的 `00395b6b`；joint2 才是 `00395b6b`。

- `14dc8934`：发布 Gazebo rotor/gimbal joint state，并把它按 RGB 时间戳绑定到 frame metadata。
- `00395b6b`：记录 `rotorVelocitySlowdownSim=10`，并补充 joint 回归归因文档。

该范围没有 coverage planner、去重或几何代码变更。`40c56f64`（“Allow coverage geometry to use a composed camera pose”，14:25:33 +0800）在时间上确实位于 `ff8812ec`（11:25:07）和 `00395b6b`（15:24:24）之间，但它及其 merge `5b6fc4f2` 不在 joint/joint2 provenance 所指向的 source commit 祖先链中；用 `git show <commit>:<path>` 检查 `ff8812ec`、`14dc8934`、`00395b6b` 时，coverage geometry 都没有这次改动。因此，**它是一个需要排除的时间相关候选，不是已证实的 source-code 差异**。三份 provenance 只有 `dirty:true` 和 changed-files 数量，没有运行时 tracked-tree 快照，不能证明该候选是否以未提交代码进入某次运行。

但三份 episode 都记录了 `camera_pose.source="composed"` 和 `gimbal_feedback_fresh=true`（部分后续观测因姿态反馈年龄较大而没有 `camera_pose`）。代码路径也显示，`build_observation()` 先用 MAVLink `GIMBAL_DEVICE_ATTITUDE_STATUS` 调用 `compose_camera_pose()`，然后才把 sidecar 的 Gazebo joint 样本合并到 `gimbal_joint_state`；因此 joint sidecar 是附加观测元数据，未直接替换 `camera_pose` 的投影矩阵。三份运行的 provenance 都是 `dirty:true`，但对应 source worktree 的 tracked diff 为空，不能用 dirty 标志推断另一个 coverage 实现。

`camera_pose` 的逐帧组成是三次 episode 都记录到的共同观测字段；`40c56f64` 的 coverage-footprint 逻辑是否实际运行则没有 provenance 证据。episode 中的实际路径确实不同，不能把三次运行视为逐帧 A/B。路径差异只能由这些产物归因到闭环反馈、姿态可用性/时间对齐、sidecar 带来的观测时序和未记录的运行波动，不能进一步唯一化。

## 每步路径、计数增量和去重

| 运行 | 步数 | 平移/转向/悬停 | `distance_flown_m` | 计数发生步骤（位置 N/E） |
|---|---:|---:|---:|---|
| 无 sidecar，`ff8812ec` | 92 | 45 / 32 / 14 | 414.1 | 4: `[19.6,23.5]`；8: `[10.2,-15.0]`；30: `[-22.3,28.7]` |
| joint，`14dc8934` | 130 | 58 / 55 / 16 | 524.9 | 6: `[17.8,21.0]`；12: `[9.2,-15.2]`；61: `[22.8,25.7]`；78: `[-19.2,29.1]`；88: `[-23.0,28.5]` |
| joint2，`00395b6b` | 140 | 53 / 73 / 13 | 470.5 | 4: `[19.8,23.7]`；8: `[9.1,-14.7]`；29: `[-23.0,28.9]`；105: `[-16.9,29.4]`；116: `[5.0,-2.2]`；129: `[12.6,9.2]` |

无 sidecar 的三个确认位置分别落在三个真值锥附近，报告为 `matched_count=3,false_findings=0`。它的前几步是 15 m 前移并下降 4 m；joint 的对应早期前移约 8.1 m、下降约 0.92 m，随后动作序列也多出 23 次转向和 13 次平移。这是实际闭环路径差异的直接证据，但不能单凭一次运行把差异归给某个单独输入。它在同一物体的后续 track 上仍看到重复，例如第一锥附近约 `[20,23]`、第二锥附近约 `[9,-15]`、第三锥附近约 `[-22,28]`，但 count 状态保留了一个空间结果。

joint 的前两个位置和后一个 `[-23.0,28.5]` 对应三个真锥。`[22.8,25.7]` 距第一锥约 5 m；`[-19.2,29.1]` 距第三锥约 3 m，且同一运行稍后又确认 `[-23.0,28.5]`。这两项是同一物理锥的重复 track/位置估计，不是新增物体；评估器因此报 `matched_count=3,false_findings=2`。它们分别在 step 61（`t36`，只有 2 views、ground fallback）和 step 78（`t44`，较大 baseline 的 fused estimate）出现，随后第三锥再次被确认。

joint2 的前三项分别对应三个真锥。step 105 的 `[-16.9,29.4]` 只有两次 ground 视图，靠近第三锥，是偏移重复。后两项没有真值邻近物：

- `t35` 在早先步骤确认位置约 `[8,-16]`、`[9,-15]`，step 116 却融合为 `[5.0,-2.2]`；同一物体/track 的地面位置跳变，属于投影位置错误造成的 false positive。
- `t67` 先后出现 `[17.88,12.03]`、`[20.70,22.90]`、`[19.43,23.06]`、`[16.30,16.75]`，step 129 最终融合为 `[12.63,9.23]`；这同样是单物体多地面位置，且最终位置没有真值对应。

所以要区分“物体层面”和“评估器层面”：joint 的两个额外 count 是已有物体的重复；joint2 的三个额外 count 中一个是第三锥的重复，两个是投影/身份链路产生的 false positives。报告的 `false_findings` 把所有未能一对一匹配的额外结果都算进去。

## sidecar、相机姿态和投影证据

无 sidecar 的 `gimbal_joint_state.source` 是 `GIMBAL_DEVICE_ATTITUDE_STATUS`，joint/joint2 则是 `gazebo_joint_state`，并带有速度；joint/joint2 还带 rotor `velocity_slowdown=10`。frame-server 统计显示 sidecar 传输本身正常：

- joint：`12876` 个 joint frames，`joint_state_invalid=0`、`joint_state_missing_or_stale=0`。
- joint2：`13216` 个 joint frames，invalid/missing/stale 均为 0。
- RGB、深度分辨率和内参三次相同（RGB 1280×960，`fx=fy=539.936...`；深度 320×240）。

这排除了“sidecar 损坏或错配导致所有帧投影错误”的解释，但不能排除它改变了运行时可见的姿态时间/反馈行为。计数步骤中，joint 的额外结果有 `camera_pose` 缺失或姿态年龄较大的观测；joint2 的 step 129 (`t67`) 前后都没有 composed pose。相同动作指令下，首个 turn 后三次记录的云台 yaw 也不同，说明闭环实际观察姿态并未逐帧相同。

更关键的是，`streams.py` 的顺序是“先由 MAVLink gimbal attitude 组成 `camera_pose`，再合并 Gazebo sidecar 到 `gimbal_joint_state`”。因此，当前证据支持“姿态时间对齐/缺失会使 ground projection 不稳定”，但不支持“sidecar 直接替换投影矩阵并造成过计数”。

## 延迟和竞争

从 `steps.jsonl` 计算的中位数/尾延迟如下：

| 运行 | total p50 / p95 / max | frame-before p50 | frame-after p50 | slow-model p50 | slow calls |
|---|---:|---:|---:|---:|---:|
| 无 sidecar | 9.19 s / 39.70 s / 49.06 s | 435 ms | 443 ms | 0 ms | 41/92 |
| joint | 8.82 s / 41.23 s / 45.17 s | 473 ms | 521 ms | 82.5 ms | 65/130 |
| joint2 | 10.58 s / 55.80 s / 122.81 s | 549 ms | 590 ms | 350 ms | 78/140 |

joint2 还有更高的 observation-pack 中位数（79 ms 对 59–61 ms）和更长 frame wait；这与任务描述的“同 GPU 旁边运行另一个 nightly”一致，也与主机/GPU 竞争假说相符。可是 joint 的 p50 total 并不比基线更慢，且 joint 已经从 3 变成 5，因此竞争不能单独解释初始过计数。产物没有 GPU utilization、显存、CPU load 或实际模型吞吐的硬件计数器，所以竞争的因果强度只能写成“合理但未证实”。

## 能区分未决原因的实验

固定 `gen_cnt2_w0000`、seed、world SDF、模型和 onboard config，做成对交替重复：独占同一 GPU/CPU slot，sidecar off/on 各至少 10 次；再在同一独占条件下重复一组“明确固定 source commit、覆盖几何 composed-pose 开/关”的运行。每次记录：

- GPU 利用率、显存、CPU load、进程绑定和模型实际推理吞吐；
- 每个 RGB/depth frame 的 `camera_pose`、gimbal feedback age、sidecar source timestamp/skew；
- 完全相同决策步骤的检测框、track ID、ground position、confirm/reject 和 dedup reason；
- 最终把同一 frame 输入分别喂给“使用 composed pose”和“使用固定/记录 pose”的 projection，比较地面交点漂移；最好直接重放一份 episode 的相同 RGB/depth/pose 流，sidecar metadata 只做 on/off 对照。

若 sidecar 开关在独占资源和固定路径下仍改变 projection/计数，才可归因于 sidecar/姿态输入；若只在并发组出现长尾和额外误报，则是资源竞争；若两组都沿不同 coverage path 并出现同一 track 的位置跳变，则应归因于覆盖路径暴露出的投影与空间去重缺口。

## 只读证据位置

- [无 sidecar report](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-gimbal-9/gimbal/gimbal-count-cones/report.json)
- [joint report](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-gimbal-joint/gimbal/gimbal-count-cones/report.json)
- [joint2 report](../artifacts/benchmarks/nightly-linux-r21idr3-r24plan-gimbal-joint2/gimbal/gimbal-count-cones/report.json)
- 三份 episode 的 `campaign/gen_cnt2_w0000/episodes/*/steps.jsonl`、`mission_summary.json` 和 `episode.json`
- 三份 `report.json` 的 `provenance` 字段（含 source commit、world/model hashes 和 frame-server 命令）
