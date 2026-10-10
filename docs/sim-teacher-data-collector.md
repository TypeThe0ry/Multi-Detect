# SIM 教师数据采集器

本工具在固定 digest 的 Gazebo + PX4 SITL 容器中采集真实的 `observation -> action -> next observation` 样本。当前只支持 `x500_mono_cam` 多旋翼。运行时只记录原始流；文件哈希和完整性检查由离线 auditor 完成。

## 运行

单个 5 步 explore episode：

```powershell
C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe `
  scripts/collect_sim_teacher_episodes.py `
  --scenario configs/scenarios/urban_inspection_multirotor_001.scenario.json `
  --teacher explore --episodes 1 --steps-per-episode 5 --seed 7 `
  --out artifacts/datasets/sim-teacher/urban-explore-r29 `
  --max-episode-s 300 --keep-container-on-failure
```

`--dry-run` 只打印计划，不启动 Docker。`--max-episode-s` 是单 episode 的硬墙钟上限；`--keep-container-on-failure` 仅用于调试，失败时保留容器。

## 实际控制流程

1. 启动 digest 固定的 PX4/Gazebo 容器并等待 world。
2. 建立唯一的 `udpin:127.0.0.1:14541` pymavlink 连接，source identity 为 system/component `255/190`。同一个 socket 接收 PX4 telemetry，并发送 GCS heartbeat、arm、模式切换和 offboard setpoint；不建立独立 `udpout` 控制连接。
3. 收到 heartbeat 后，通过 `docker exec` 设置 SIM-only 参数 `COM_RC_IN_MODE=4`，并写入 `episode.json.sim_only_parameters`。不使用 force-arm，也不关闭 PX4 failsafe。
4. 以 0.5 秒周期持续发送 GCS heartbeat。等待 EKF/home/GPS 和 pre-arm health；优先使用 `HEALTH_REPORT`，没有该消息时回退到 `SYS_STATUS`、最近没有 `Preflight Fail` 的 `STATUSTEXT`，并要求至少约 10 秒 heartbeat warm-up。stderr 中每 5 秒输出带单调时间戳的健康位名称。
5. 使用 `MAV_CMD_COMPONENT_ARM_DISARM` (400)，`param1=1`、`param2=0`，在拒绝时有限重试。随后在 OFFBOARD 中起飞、执行策略 primitive，最后 RTL、LAND，并等待 disarmed heartbeat。
6. RGB/depth 使用已有 MDGZSIM3/MDGZDEP1 解码器。没有 live frame 时 episode 失败，绝不写 placeholder observation。

## 数据布局

```text
<out>/
  episodes/<episode_id>/
    episode.json
    steps.jsonl
    mavlink.jsonl
    rgb/*.png
    depth/*.npy
  audit.json
```

每个 step 包含真实的 RGB、float32 米制 depth、camera info、NED pose、经纬高、姿态、速度、GPS fix、动作、ACK、执行结果、frame/pose 时间年龄和下一帧 observation。`mavlink.jsonl` 保存每条收到的 MAVLink 报文和接收时间。

离线审计：

```powershell
C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe `
  scripts/audit_sim_teacher_dataset.py `
  --dataset artifacts/datasets/sim-teacher/urban-explore-r29
```

审计生成 SHA-256 manifest，检查文件、step 配对、有限 pose、GPS、ACK、时间单调性、frame/pose skew、成功率、动作直方图、覆盖范围、episode 时长和 achieved-vs-commanded 误差。发现损坏时返回非零状态。

## teacher 策略和范围

`explore` 使用 seed 控制的随机平移、转向、hold 和 stop，并遵守场景边界与高度限制。`expert_search` 使用 scenario truth 做 lawnmower/expanding search、FOV 几何判断和 standoff approach，并记录 `target_in_fov`、`target_bearing`、`target_range` 等 privileged labels。

## 适用范围

这些样本只证明 Gazebo PX4 SITL 采集链路和 SIM 控制执行。它们不声明 Jetson/Pixhawk 实机、户外 GNSS、感知精度或 sim-to-real 性能。scenario truth 只供 privileged expert policy 使用，不会伪装成机载视觉观测。
