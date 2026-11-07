# Accel timeout 与失控坠落调查（2026-11-07）

## 结论

28-head `operator-abort` 的可证明故障是：飞机在 step 38 从约 18.7 m 突然落到约 0.8 m，同时 roll 变为约 171°；之后保持倒置/翻滚姿态，连续 27 个 `up 15 m` primitive 均 timeout，飞机没有重新爬升。collector 现在能在这一类 telemetry 证据出现时以 `failure_class=vehicle_lost_control` 结束 episode。

不能仅凭现有日志证明“Accel timeout 是失控的唯一根因”。重复 Accel timeout 先出现；MAG timeout 首次约在坠落前 91 s wall，`Attitude failure (roll)` 约早 70 s，Compass 告警约早 45 s，之后这些告警还继续/复现。它们证明 PX4 观察到了传感器/姿态健康异常，并在时间上先于坠落；但日志不能证明故障消息所指的状态就是失控根因，也不能确认第一触发点。abort 事件发生在约 120 s 仿真时间，step 38 的坠落发生在约 188.5 s；因此 abort 早于坠落，但没有证据显示 abort 本身直接造成姿态跃变。

## 目标 episode 时间线

证据目录：`artifacts/benchmarks/nightly-linux-r21idr3-r24plan-28-head/main/operator-abort/campaign/gen_cnt2_w0003/episodes/episode-0000-06a1a47d/`。

| 阶段 | 证据 |
| --- | --- |
| 起飞与任务 | step 0 的高度约 18.44 m；step 0–37 是 turn/hold/水平 translate，姿态大体接近水平，`EXTENDED_SYS_STATE.landed_state=2`（in air）。 |
| abort | `mission.jsonl` 有 `sim_ground_event=abort`，仿真时间 120.000 s；之后 decision 是 `return_now`，execution 标签为 `return_home`。abort 已进入策略恢复链路，但没有证明返航完成。 |
| 失控跃变 | step 38 是水平 translate，`commanded_delta_ned.z=0`，before 高度 18.7305 m、roll 0.84°，after `alt_m=-0.6492`（近地面），roll 171.37°、pitch 12.94°，primitive timeout。`LOCAL_POSITION_NED.z` 从约 -18.62 m 变为 +0.76 m，即约 19.4 m 的向下位移。pose 的 `alt_m` 与 local NED 地面基准在该帧并不一致；高度跃变也由 NED 和姿态独立支持。 |
| 电机/状态 | step 38 前 `SERVO_OUTPUT_RAW` 四路约为 `[798,778,787,761]`；坠落窗口内四路反复出现 150/1000 饱和值并高度不对称。`HEARTBEAT.base_mode=145` 含 armed 标志，`custom_mode=393216`（PX4 main mode 6，OFFBOARD）；`landed_state=2`，所以 PX4 没有把它报告为已着陆。 |
| 失控后 | step 39–65 全部是 `translate(up_m=15)`，每步约 20 s 仿真时间 timeout；高度约 0.5–2.3 m，roll 多数接近 ±180°，`landed_state` 仍为 2。末步高度约 0.65 m，仿真时间约 740.424 s。 |
| 结束 | episode 因 4800 s wall ceiling 失败，容器和 PX4 仍存活；并非 collector 识别坠落后提前结束。 |

STATUSTEXT 的接收 wall 时间（相对首个 step 观测）包括：Accel timeout 在 -42.5、7.1、61.6、309.7、359.0、378.3、496.4、696.9 s，MAG timeout 首次在 701.7 s；第一次 `Attitude failure (roll)` 在 722.0 s，Compass 首次在 747.8 s。坠落 step 38 的 after 观测约在 +792.5 s；故上述 MAG、姿态、Compass 告警的首次记录都先于坠落，部分告警之后复现。日志也有 `High Gyro Bias` 和 `Imbalanced propeller detected`。这些时间戳来自记录接收时间，不是 PX4 内部生成时间。

### `achieved_delta_ned` 为什么错误

对普通 primitive，collector 采用 `PymavlinkPrimitiveExecutor` 返回值，而 executor 的 `start_pos` 是内部连续的 `_commanded_position`，不是本步 `obs_before.pose.local_ned_m`。step 38 后每个 `up 15 m` 都把上一目标继续向 NED z 的负方向推进；飞机实际停在地面附近，于是“当前实际位置 - 更早的累计目标位置”每步多出约 15 m，形成 16.6、31.7、46.6 … 408 m 的假增长。它不是实际位移，也不是相邻 pose 的差。对带 `MotionCommand` 的路径，collector 才会用 before/after pose 计算；两种口径混在同一字段中，是该字段不可靠的直接原因。

## 四批 nightly 的消息统计

计数口径是每个 episode 的 `STATUSTEXT`：`Accel/Gyro/MAG` 只计含 `TIMEOUT` 的文本，另计 `Attitude failure`。RTF 用 steps 中首尾 `pose_time_boot_ms` 增量除以 `episode.json` 生命周期；clock stall 按 episode failure class/文本统计。`26-off45` 有 4 个保留目录没有完成元数据，故只报告能读取的状态。

| nightly / tier | episodes | complete | complete 中含 timeout/attitude | Accel | Gyro | MAG | Attitude | clock stall | 回放检测到未命令坠落 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 22-off45 / main | 14 | 14 | 1 | 2 | 0 | 0 | 0 | 0 | 无 |
| 26-off45 / main | 16 | 12 | 0 | 0 | 0 | 0 | 0 | 0 | 无 |
| 27-off45-e2 / main | 28 | 27 | 23 | 98 | 0 | 0 | 0 | 0 | 无 |
| 28-head / main | 14 | 11 | 10 | 128 | 0 | 1 | 8 | 1 | `operator-abort`, step 38 |
| 28-head / gimbal | 3 | 1 | 1 | 25 | 0 | 1 | 0 | 0 | 无 |

因此，“complete 任务也有 Accel timeout”答案是肯定的：22-off45 有 1 个，27-off45-e2 有 23 个，28-head main 有 10 个，gimbal 有 1 个。timeout 文本不是充分的失控判据。对含 steps 的 71 个 main episode 加 3 个 gimbal episode 回放，新规则只命中目标 episode；没有发现 complete mission 中的未检测坠落。`27-off45-e2` 的 1 个失败 episode 没有回放命中；`28-head` 的另一个 clock-stall 也没有命中。

各个非零 episode 的完整消息计数如下；同一 nightly/tier 中未列出的 episode 四类消息计数均为零：

| nightly / tier | episode id（短 id） | outcome | Accel / Gyro / MAG / Attitude |
| --- | --- | --- | ---: |
| 22-off45 main | `d4bec1fb` | complete | 2 / 0 / 0 / 0 |
| 27-off45-e2 main | `18f46725`, `d552aeaa`, `6663efa3`, `67a9590a`, `5f31b861`, `6235aeef` | complete | 3, 5, 1, 1, 1, 1 / 0 / 0 / 0 |
| 27-off45-e2 main | `df410d78`, `6e02dfaf`, `7165818a` | complete | 9, 12, 8 / 0 / 0 / 0 |
| 27-off45-e2 main | `86c1ddc4`, `ec2ea30a`, `bddea408`, `754c9aad`, `82ac0e7e`, `b3b13ee8`, `b8b5dca0` | complete | 1, 1, 4, 4, 4, 4, 1 / 0 / 0 / 0 |
| 27-off45-e2 main | `2fece70f`, `8c39dcf5`, `a0eeaaca`, `4025450f`, `b19f7297`, `7e45a674`, `c440769e` | complete | 6, 9, 2, 7, 8, 3, 2 / 0 / 0 / 0 |
| 27-off45-e2 main | `5c2f92b7` | failed（pre-arm health timeout） | 1 / 0 / 0 / 0 |
| 28-head main | `44348ffe`, `56caa0ee`, `6931667c`, `41b18d2e`, `2386b857` | complete | 11, 11, 9, 3, 10 / 0 / 0 / 0 |
| 28-head main | `33a49796` | failed（sensor-frame failure） | 11 / 0 / 0 / 0 |
| 28-head main | `c539c5aa`, `da89b0f2`, `692e1bab`, `124953aa`, `14f110f8` | complete | 6, 6, 12, 14, 16 / 0 / 0 / 0 |
| 28-head main | `06a1a47d` | failed（4800 s ceiling） | 19 / 0 / 1 / 8 |
| 28-head gimbal | `b9b2f9ff`, `6a7ed3b0`, `9670a9d2` | 1 未完成、1 complete、1 failed | 9, 5, 11 / 0 / 0, 0, 1 / 0 |

表中 grouped row 按 episode id 顺序对应逗号分隔的 Accel 计数；四个 cohort 合计分别为 2、0、98、128 Accel timeout，另有 28-head main 的 1 MAG timeout、8 Attitude failure 和 gimbal 的 1 MAG timeout。失败记录里，`27-off45-e2` 是 count-cones pre-arm health failure，`28-head main` 另有 comms-free-count sensor-frame failure、count-cones clock stall、目标 ceiling failure；gimbal 的失败是 sensor-frame failure。唯一 clock stall 没有这些四类 STATUSTEXT。完整任务 RTF 中位数和范围见下表；timeout 同时出现在快慢不同的成功任务中，没有“timeout 数越多则 RTF 越低”的证据。

完整 episode 的 RTF（仅有完整 steps 且可计算者）为：22-off45 main 中位数 0.098、范围 0.045–0.134；26-off45 为 0.108、0.049–0.120；27-off45-e2 为 0.173、0.079–0.218；28-head main 为 0.154、0.084–0.182；28-head gimbal 仅 1 个为 0.113。Accel timeout 横跨正常和慢速任务，不能单独解释 28-head 的 4800 s 消耗。

## PX4 timeout 与 lockstep 的关系

本工作树和可读的相关目录没有 PX4 传感器驱动源码；也没有启动 Docker 去从 sim image 取源码。因此不能给出具体 PX4 文件/行号，也不能把 `Accel #0 fail: TIMEOUT!` 精确映射到某个驱动函数。能确定的语义边界是：这是 PX4 对加速度计实例未在预期期限内提供有效更新的健康/故障告警；`MAG #0 failed: TIMEOUT!` 同理指向磁力计实例，`Attitude failure` 是姿态健康检查失败。

在 Gazebo/PX4 lockstep 中，仿真时间推进依赖传感器和控制链按步交付消息。若 gz 生成的 IMU 消息晚到、消息时间戳不推进，或 PX4/Gazebo transport/调度出现饥饿，PX4 可能在自己的传感器检查期限内看到 timeout；这与 `docs/gz-idle-stall-20261107.md` 描述的 stall family 方向相容。但本 episode 的 PX4 clock 一直推进，且 camera/frame 也持续有记录，所以它不是已证实的完整 clock stall。现有证据只能说“late/missed IMU 是合理连接假设”，不能说已证明 gz 延迟导致了 IMU timeout，更不能由 timeout 文本反推出坠落因果。

## collector 早停实现

新增 `src/multidetect/sim_teacher/vehicle_loss.py`，collector 每步写入 `obs_after` 后检查：

1. 未处于 landing/已落地状态时，若单步实际高度损失减去命令允许的下降量至少 8 m，标记 `uncommanded_altitude_loss`。
2. 若高度不超过 2.5 m、姿态 roll 或 pitch 的绝对值至少 90°，并连续 3 个采样步保持，标记 `sustained_tumble_near_ground`。
3. `land`/`landing` primitive、PX4 `landed_state=1` 会清除并屏蔽近地面规则；正常的负 `up_m` 下降不会触发翻滚规则。

触发步完整保留在 `steps.jsonl`，并写入 `collector_vehicle_lost_control` MAVLink 记录；episode 以 `failure_class=vehicle_lost_control` 结束。该改动只在 simulation collector，未修改 onboard runtime 或 safety supervisor。

测试覆盖了正常 landing/下降不误报、近地翻滚需连续 3 步，以及只读目标 episode 回放在 step 38 命中。四批 nightly 有 71 个 main、3 个 gimbal episode 含 steps 可回放；规则结果为 1 个真阳性、0 个 false positive，没有 complete mission 被标记。另有 1 个 27-off45-e2 metadata 目录没有 `steps.jsonl`，无法做 pose 回放。

## 给 onboard 侧的待评审建议

不要把重复 `return_home up 15 m` 当作进展。若连续两个或更多 climb primitive 在近地面超时，且高度没有增加、姿态/传感器健康异常或 landed state 仍为 airborne，应停止继续发送同一 setpoint，转入一次有界的姿态/传感器健康确认；确认失控时请求 PX4 的受控降落/紧急 failsafe，并把“无法爬升、疑似失控”作为明确事件上报。这里仅是 review 建议，本次没有改 onboard runtime 或 safety supervisor。

## 验证

运行：

```text
PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp \
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python -m pytest \
tests/test_sim_teacher_vehicle_loss.py tests/test_sim_teacher_executor.py \
tests/test_sim_teacher_landing.py -q -p no:xdist
```

结果：43 passed；CPU-only onboard gate PASS（114 s）。另行扫描了 75 个指定 nightly episode 目录，其中 74 个有 steps 可回放；未启动 SITL、Docker、GPU 或网络下载。
