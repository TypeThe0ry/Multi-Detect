# SIM 操作员端到端指南

本文对应 `wt/integration` 的 `4feb048`：从 Gazebo/PX4 会话、操作员 prompt，到机载规划、审批、飞行和结果下行。它描述的是 SIM 操作，不是实飞安全流程。

## 系统边界

```text
相机/深度 -> 快模型（候选框） -> 慢模型（身份判断）
                                      |
操作员 prompt -> GCS -> operator-link -> 机载 planner -> MissionSupervisor -> PX4/Gazebo
                                      |                         |
                                      +<-- plan/status/findings/final_summary --+
```

快模型逐帧找候选；慢模型按任务判断身份，并在任务开始时把 prompt-only 包编译成计划。`MissionSupervisor` 负责审批门、链路状态、预算/能量和返航策略；PX4 SITL 执行飞行，Gazebo 提供世界和相机。GCS 通过签名 MAVLink2 `TUNNEL` 承载 operator-link，并展示计划、状态和结果。

## 启动会话

先确认 Docker Desktop、Python、PX4/Gazebo 镜像已就绪；本地 worktree 必须有 `artifacts` junction，提供 Fuel 离线缓存和 onboard 所需的 `llama-server`：

```powershell
$env:PYTHONPATH="src;."
python scripts/run_sim_session.py --world gen_cnt2_w0000 --gcs-port 14560 `
  --snapshot-dir D:/Projects/mdwt/_scratch/e2e/shots `
  --session-log D:/Projects/mdwt/_scratch/e2e/session.jsonl
```

脚本的 `--world` 接受 `simulation/gazebo/worlds` 下的 SDF 名称或仓库内路径；`--airframe` 默认 `x500_mono_cam`，也可使用已有 profile（例如 `standard_vtol`、`rc_cessna_runway`）。PX4 会主动向宿主 `127.0.0.1:14560` 发送 GCS MAVLink；该端口不通过 Docker publish。浏览器打开 `http://127.0.0.1:6080/vnc.html?autoconnect=1`（`--viewer-port` 可改端口），手动控制端口仍是 `127.0.0.1:14540`。

首次使用或 viewer 镜像缺失时加一次 `--build-viewer`；后续会复用缓存。`--snapshot-dir` 配合 `--snapshot-interval`（默认 10 秒）保存 `DISPLAY=:99` 截图，世界 ready 和任务结束还会各保存一张。`--session-log` 写 JSON Lines 生命周期日志。`--check-heartbeat S` 会在宿主 GCS UDP 端口等待 HEARTBEAT 并写 `heartbeat.json`。

`--onboard-task "..."` 运行直接由会话启动的 onboard policy-chain smoke，输出在 `--onboard-out`（默认 `tmp/sim-session/onboard`）；`--exit-after-task` 在任务成功或失败后清理容器并返回任务 exit code；`--task-timeout-s` 默认 2400 秒。要走完整 prompt-to-flight operator-link，使用 `--operator-link`，并让 GCS 连接默认 `--operator-port 14580`。会话结束前会保持 SIM 电量，且等待 operator downlink 排空；这两个行为来自当前分支最新修复。

所有 Docker 操作都要持有共享 SITL 锁（Git Bash）：

```bash
. /d/Projects/mdwt/_scratch/sitl_lock.sh
sitl_acquire simgui
python scripts/run_sim_session.py --operator-link --world gen_cnt2_w0000
status=$?
sitl_release
exit $status
```

## 发送 prompt

prompt-only 包只需要任务文本和任务边界，不预置 `plan`。例如 `D:/Projects/mdwt/_scratch/e2e/package-count-cones.json` 的核心字段如下（完整文件可直接使用）：

```json
{
  "mission_id": "e2e-count-cones-001",
  "task_text": "数一数场地里有几个橙色交通锥，报告位置后返航",
  "risk_level": "low",
  "heartbeat_timeout_s": 5,
  "home": [0, 0],
  "zones": {"zone:site": {"centre_ne": [0, 0], "radius_m": 55}},
  "operating_area": {"north_min_m": -100, "north_max_m": 100, "east_min_m": -100, "east_max_m": 100, "min_altitude_m": 5, "max_altitude_m": 120},
  "budget": {"time_s": 3600, "reserve_time_s": 180, "energy_wh": 600, "reserve_energy_wh": 60}
}
```

会话打印出的 `operator-link.json` 给出端口和密钥。另开终端运行：

```powershell
python scripts/fake_gcs_console.py D:/Projects/mdwt/_scratch/e2e/package-count-cones.json `
  --link tmp/sim-session/onboard/operator-link.json `
  --auto-approve --log D:/Projects/mdwt/_scratch/e2e/gcs.jsonl --max-seconds 2400
```

省略 `--auto-approve` 时，收到 plan 会停在 `approve with Enter`；`--max-seconds 0`（默认）持续到 Ctrl-C。控制台会打印上传结果、type-29 onboard ACK、计划、约每秒一次的 status v2、每条 finding，以及拆页后的 `final_summary_page`；只有收齐所有页才是最终摘要。`--log` 追加每份完整 JSON 文档。

自写 GCS 必须满足这些协议义务：

- 建立 operator codec 时注册真实视频流几何；当前示例是 `camera-main`、1280×720，宽高和旋转必须与状态一致。
- 每秒发送 type-34 `MISSION_HEARTBEAT`，否则机载链路变为 down，下行日志会停止。
- 收到每个 type-35 telemetry chunk 后立即回 type-36 ACK（token、chunk index）；下行是 stop-and-wait，机载会重发未确认分片。
- type-28 上传按 20 burst、20/s 的消息类型限制分发；每片间隔至少约 0.06 秒，不能一次打满。
- 每次新上传生成新的奇数 upload token；复用旧 token 会被判为 expired or conflicting。收到 type-29 逐片 ACK，最后一片成功才算装载完成。

完整消息表和分片边界见 [GCS 任务包流程与 TUNNEL v2](gcs-mission-package-workflow.md)。

## 真实感选项

从源 SDF 生成不改源文件的变体：

```powershell
python scripts/augment_sim_world.py simulation/gazebo/worlds/gen_cnt2_w0000.sdf `
  --wind-mps 6 --gust-mps 3 --gust-period 4 --time-of-day dusk `
  --actors 2 --terrain procedural --camera-noise 0.01
```

可选项是 `--wind-mps`、`--gust-mps`/`--gust-period`、`--time-of-day dawn|noon|dusk`、`--actors N`、`--terrain dem|procedural`、`--camera-noise [STDDEV]`、`--fog` 和 `--register-index`。风只写 world `<wind>` 与阵风元数据；容器启动时依据 server config 注入 `WindEffects`，不能把 XML 存在当作动力学证据。相机噪声只修改世界中明确存在的 camera/depth/segmentation sensor；PX4 `mono_cam` 需要相应 model overlay。演员使用本地 mesh，不依赖 Fuel。具体限制和验证方法见 [仿真真实感变体](sim-realism.md)。

## 故障排查

| 现象 | 原因/处理 |
|---|---|
| 14560 无 HEARTBEAT | PX4 已改为主动发往 `host.docker.internal`；确认 GCS 绑定 `0.0.0.0:14560`、未被占用，并持有 SITL 锁。 |
| noVNC 404 或空白 | 首次加 `--build-viewer`，看 `docker logs`；确认浏览器访问 `/vnc.html?autoconnect=1`。 |
| 一直 Waiting for Gazebo world | 检查 Fuel/llama-server 的 `artifacts` junction；风世界不能在 SDF 声明插件，WindEffects 由 server config 注入。 |
| Duplicate mount point `x500_mono_cam` | 分割相机和风 overlay 不能同时挂同一目标；使用合并的 `x500_mono_cam_seg_wind`。 |
| 风一开就滑行、翻倒或无 heading reference | 当前实现把风逐步 ramp 到目标值并降低作用系数；用短 SITL 姿态/漂移观测确认，不以 SDF 代替实测。 |
| 上传 ACK 丢失、`expired or conflicting` | 上传超过 20 burst/20/s，或复用了 token；按约 0.06 秒 pacing，每次运行生成 fresh token。 |
| 有 plan 但没有 status/finding | GCS 没发 type-34，或未 ACK type-35；保持 heartbeat、逐片回 type-36。 |
| 任务提前返航 | 读取 session log 和 status 的 supervisor reason；历史回归曾因时间线缺飞机位置触发错误能量返航，当前 SIM 仍在修 energy reserve/battery-stale early return。 |
| 回归架次显示 missing | 先看磁盘门限和 `campaign/*.log`；10-22 relook 回归还遇到缺少 `artifacts/experiments/yoloe-v3-onboard-1280/train/weights/best.pt`，没有产生可评分 episode。 |

## 已知限制

SIM 的 energy reserve/battery-stale early return 仍在修复和复测；downlink throughput 也仍在调整。GCS UI 的截图验收尚未完成。`sim-mission-suite-regression-20261020.md` 中的结果表是当前参考基线，单架次结果不能当作稳定性或实机保证。
