# PX4 慢速 lockstep 与降落等待调查（2026-11-03）

## 结论与修改范围

Linux 多旋翼降落中的飞行动作预算应按 PX4 的 `LOCAL_POSITION_NED.time_boot_ms`
计算，通信新鲜度、重试周期、诊断输出周期和进程兜底上限仍按主机单调时间计算。
已有日志同时包含慢速仿真和真正的遥测停止，不能把两种失败都解释为落地检测参数错误。

本次只修改 `scripts/collect_sim_teacher_episodes.py::_land_and_wait`：复用已有
`_SimClock`，将总降落预算、下降进展宽限、近地静止时长、地面稳定时长、自动解锁等待、
普通 DISARM 的心跳确认等待切换到 PX4 boot time。没有有效 boot stamp 时回退墙钟。
保留 `max(降落预算, LAND_HARD_CEILING_S=600)` 秒墙钟硬上限；PX4 时钟连续
`LANDING_TELEMETRY_STALE_S=30` 秒墙钟不前进时明确失败。这两项保护也覆盖自动解锁和
DISARM 确认的内部循环，持续收到重复时间戳的报文不能绕过保护。

命令重试、新鲜遥测判据保持墙钟。Windows 保留原来的墙钟预算和新鲜
`landed_state=1` 即完成的条件。没有改变 PX4 参数，没有 force-disarm；原来的普通
DISARM 后备路径仍需等待新收到的 disarmed heartbeat 才算完成。

## 只读日志证据

以下路径均相对于：

```text
/data/projects/Multi-Detect-env/Multi-Detect/artifacts/benchmarks/nightly-linux-r21idr3-r24plan-7/main/
```

原始产物没有修改。两次运行均在 2026-11-02，`episode.json` 记录的代码提交为
`09ed2913733baeb090345046d7b87f921bab98a1`，且运行目录当时有未提交修改；这些结果是
历史运行的观测，不能视为本次修复后的飞行结果。

| 运行 | 降落阶段有效位置区间（主机单调秒） | PX4 boot ms 区间 | 墙钟 / 仿真秒 | 仿真 / 墙钟 | 平均下降速度（墙钟 / 仿真 m/s） |
| --- | --- | --- | --- | --- | --- |
| `monitor-backpacks` | 30041.707224519–30269.813044238 | 363364–382716 | 228.106 / 19.352 | 0.08484 | 0.07816 / 0.92129 |
| `find-suv-land` | 35701.774534191–35759.138685010 | 42948–51044 | 57.364 / 8.096 | 0.14113 | 0.10400 / 0.73690 |

计算方式：`Δsim_s = Δtime_boot_ms / 1000`、`Δwall_s = Δreceive_monotonic_ns / 1e9`、
`下降速度 = Δz / Δt`（NED 的 z 向下为正）。它们是区间平均数，包含模式切换、速度变化或
触地段，不表示全过程都严格等于 `MPC_LAND_SPEED`。

### monitor-backpacks：采集器先于 PX4 的完整落地/解锁流程结束

文件：

```text
monitor-backpacks/campaign/gen_cnt2_w0003.log
monitor-backpacks/campaign/gen_cnt2_w0003/episodes/episode-0000-643321dd/mavlink.jsonl
```

- log 第 380 行：发送 LAND，初始预算为 149.1 秒墙钟，高度 17.827 m。
- MAVLink 第 155951、164404 行：上表的位置区间，高度从 17.826790 m 降到接近零。
- log 第 499–500 行：30259.6367 秒判定“近地静止”，随后发普通 DISARM；此时仍为
  `landed_state=4`、armed、高度 0.036 m、`|vz|=0.076 m/s`。10 秒墙钟近地等待
  在本段仿真速率下只有约 0.85 秒仿真时间。
- MAVLink 第 164064 行：30260.095112648 秒收到 `command=400, result=1`
  （TEMPORARILY_REJECTED）。前一位置报文第 164061 行为 boot 381924 ms。
- MAVLink 第 164382 行：30269.059222939 秒 PX4 发出 `Landing detected`。
  前一位置报文第 164379 行为 boot 382660 ms；从 DISARM 拒绝到检测到落地仅推进
  0.736 秒仿真时间。该消息比采集器发 DISARM 晚约 9.42 秒墙钟。
- log 第 501 行：30269.638486481 秒确认等待耗尽，仍没有新的 disarmed heartbeat。
  日志最后一条 HEARTBEAT 在 MAVLink 第 163978 行（30257.828748732 秒），最后一条
  EXTENDED_SYS_STATE 在第 163985 行，仍为 4。采集器只在 `Landing detected` 后再等了
  约 0.58 秒墙钟。PX4 的检测消息并不等价于已经收到解锁确认。

因此，这次运行并非“PX4 永远无法检测落地”：已有真实 `Landing detected`，采集器的
近地与解锁等待却提前耗尽。最后一次局部 z 略为正数时，旧 `_landing_altitude` 会回退到
40 m，也解释了失败日志的 `fallback_max_takeoff_altitude`；本次没有混入该独立修正。

### find-suv-land：仿真/遥测真的停止

文件：

```text
find-suv-land/campaign/gen_v2_w0035.log
find-suv-land/campaign/gen_v2_w0035/episodes/episode-0000-8148bb08/mavlink.jsonl
```

- log 第 80 行：35701.673481869 秒发 LAND，预算 120 秒墙钟，高度 7.873 m。
- MAVLink 第 23298、26829 行：上表的位置区间。按仿真秒计算下降约 0.737 m/s，
  接近名义 0.7 m/s；按墙钟计算约 0.104 m/s。
- 第 26829 行之后没有新位置，最后 boot 停在 51044 ms、高度 1.906811 m、
  `vz=0.682061 m/s`。最后 HEARTBEAT / EXTENDED_SYS_STATE 为第 26490 / 26491 行，
  接收时间约 35753.7355 秒，仍 armed / landing。
- log 第 109 行：35821.673304189 秒超时。最后位置距失败约 62.535 秒墙钟，
  最后心跳距失败约 67.938 秒墙钟；没有 `Landing detected` 或 DISARM 的 ACK。

这份证据只能确认遥测/时钟停止，不能只凭 MAVLink 文件确定是 Gazebo 停住、PX4 停住
还是链路停止。本次改动会将冻结的 boot clock 报为停滞，不能把这次运行变成成功落地。

## 镜像、x500 与参数核对

- `scripts/run_simulator_docker.py::PINNED_IMAGE` 和 `simulation/docker/gpu/Dockerfile`
  的基础镜像相同：
  `px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`。
  两次 MAVLink 的第 1 行实际记录的是 `multidetect/px4-sitl-gazebo:gpu` 标签。
  仅凭现有产物不能反向证明当时标签对应的镜像 ID 或 PX4 源码提交；本次没有调用 Docker。
- 多旋翼启动规划选择 `PX4_SYS_AUTOSTART=4001`、`PX4_SIM_MODEL=gz_x500_mono_cam`。
  两次命令都启用了 `--segmentation`，采集器使用只读的
  `simulation/gazebo/model-overlays/x500_mono_cam_seg`。该模型继承 `model://x500`，
  增加 RGB/depth/segmentation；仓库 overlay 没有替换 x500 的电机/推力模型。
- 启动环境只显式设置三个传感器参数：`SENS_EN_MAGSIM=1`、`SENS_GPS0_DELAY=0`、
  `EKF2_ASP_DELAY=0`。启动补丁处理 Gazebo magnetometer、wind 插件和 world 加载尝试次数，
  没有修改落地检测阈值，也没有设置 `PX4_SIM_SPEED_FACTOR`。
- x500 的 `_profile_landing_parameters` 为空；两次 `episode.json.sim_only_parameters`
  都只记录 `COM_RC_IN_MODE=4`，命令行没有 `--sim-param`。没有发现发射阶段覆盖
  `LNDMC_*`、`MPC_LAND_*`、`COM_DISARM_LAND` 的证据。
- 两个仓库 world 的物理配置目标为 `real_time_factor=1`、`max_step_size=0.004`。
  配置目标不代表加载相机与场景后真的达到实时；上表直接用车辆时间量出了差异。

仓库保存的固定镜像参数元数据是 `docs/reference/px4-pinned-fw-vtol-params.json`，
只有 326 项筛选记录，不能当作两次 x500 运行的完整参数快照：

| 参数 | 可核对值 | 证据和含义 |
| --- | --- | --- |
| `LNDMC_Z_VEL_MAX` | 默认 0.25 m/s | 元数据要求低于最低降落速度（`MPC_LAND_SPEED` 或 `MPC_LAND_CRWL`）；飞行中 `vz≈0.68` 尚高于此阈值，仍为 landing 并不反常。 |
| `LNDMC_XY_VEL_MAX` | 默认 1.5 m/s | 元数据中的落地水平速度阈值。 |
| `LNDMC_ROT_MAX` | 默认 20 deg/s | 元数据中的落地横滚/俯仰角速度范数上限。 |
| `LNDMC_ALT_GND` | 默认 2 m | 元数据标注为地效高度，不是“低于 2 m 就已经落地”。 |
| `COM_DISARM_LAND` | 默认 2 s | 元数据表示检测到落地后等待自动解锁；非正数才会禁用。不是 LAND 命令发出后立刻开始的倒计时。 |
| `MPC_LAND_SPEED` | 历史读数 0.7 m/s | `docs/evidence/sim-dynamics-parameters-i-20260908.json` 有固定镜像容器 `param show` 原文；并非这两次 x500 的实测参数。 |
| 其余 `MPC_LAND_*`、落地滞回/推力阈值 | 未完整采集 | 现有筛选元数据及两个 episode 的 MAVLink 均不足以核对实际值，不能用上游默认值冒充这次运行的读数。 |

结论是**没有证据支持为 x500 放宽落地检测参数**，而不是声称已经验证所有参数正确。
特别是最低爬行降落速度、推力阈值和检测滞回，若后续仍有“boot time 持续前进但迟迟不
落地”的案例，需要对应运行的完整 `LNDMC_*`、`MPC_LAND_*`、`MPC_THR_*`、
`COM_DISARM_LAND` 快照及 ulog 内部落地判据才能判断。现有 `Landing detected` 和
0.737 m/s 的仿真下降速度不支持直接调阈值来掩盖超时。

## 其它预算的边界

本次没有把整个采集器的墙钟统一替换：

| 预算/计时 | 当前行为与本次决定 |
| --- | --- |
| x500 降落中的物理等待 | 本次改为 PX4 boot time，独立的墙钟停滞检测与硬上限继续生效。 |
| 多旋翼 primitive | 已使用 `_SimClock`，另有 `PRIMITIVE_MAX_WALL_S=180`；不重复修改。 |
| 起飞与固定翼/VTOL 状态等待 | 仍为现有墙钟预算；同样可能受慢速仿真影响，但不是这两份降落失败的最小修复范围。 |
| `--max-episode-s` | 启动阶段 `_episode_budget` 仍按墙钟扣减；主循环 watchdog 允许车辆时间继续前进时超出名义预算，但最多到 `maximum + max(maximum, 600)` 秒墙钟。没有改变 CLI 语义。 |
| 启动、socket/帧请求、重试、新鲜度 | 保留墙钟，用于检测服务/通信失联；不能让停止的仿真时钟取消这些超时。 |

原有文档对 `--max-episode-s` 的“硬墙钟上限”描述并不完整；主循环早已有进展宽限。
本次没有重设全 episode 或其它机型的预算策略。

## 验证与限制

新增单元测试用可控 boot stamp 模拟 0.1 倍速，包括总降落预算、地面稳定、近地静止、
PX4 自动解锁、普通 DISARM 的延迟心跳、仍按墙钟重试、缺失/无效 boot stamp 回退、
重复时间戳下的停滞失败、三个等待阶段的墙钟硬上限，以及 Windows 原行为。
未启动 SITL、容器、GPU 或重新生成历史证据，不能据单元测试宣称夜跑已修复。

相关测试命令（在工作树内）：

```bash
mkdir -p .tmp
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python -m pytest \
  tests/test_sim_teacher_landing.py tests/test_sim_teacher_parallel_timing.py \
  tests/test_sim_airframe_smoke.py -q -p no:xdist -o addopts=''
```

结果：54 passed。另一次扩大验证包含 `test_sim_teacher_live_loop.py` 和
`test_simulator_docker.py`，其中真实 UDP burst 测试在创建 socket 时被执行沙箱以
`PermissionError: [Errno 1] Operation not permitted` 拒绝，其余测试通过；没有放宽或
删除该测试。启动规划测试还曾尝试只读 `docker network inspect`，同样未能访问 Docker；
没有启动容器。后续验证显式指定 `MULTIDETECT_SIM_LISTEN_HOST=127.0.0.1` 以避免网络探测。

CPU 回归闸门也已运行：

```bash
export MULTIDETECT_SIM_LISTEN_HOST=127.0.0.1
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python scripts/run_onboard_gate.py \
  -q -p no:warnings -p no:xdist -o addopts=''
```

结果：1639 passed、12 skipped、22 deselected、3 failed（pytest 耗时 506.14 s）。
三项失败均为创建 socket 时遭遇相同的 `PermissionError: [Errno 1] Operation not permitted`：

- `tests/test_llama_server_logs.py::test_port_wait_ignores_time_wait_after_server_close`
- `tests/test_onboard_main_operator_link.py::test_start_up_mission_is_bound_and_held_for_operator_approval`
- `tests/test_replay_set.py::test_replay_annotation_workbench_serves_verified_images_and_tokenized_mutations`

这些环境限制未通过跳过、删除测试或放宽断言处理；回归闸门在本沙箱内未能全绿。

`git diff --check` 通过。对两个修改的 Python 文件运行 Ruff，测试文件没有报告问题；
采集器脚本报告 11 项导入排序/行长问题，均位于本次未修改的代码区域，未混入格式化改动。
