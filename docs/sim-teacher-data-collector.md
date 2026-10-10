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

## expert_search 教师行为（当前实现）

`expert_search` 使用固定的、与隐藏目标坐标无关的搜索先验。默认搜索半径为 120 m，航段长度不超过 15 m；策略沿有界 lawnmower 航线飞行，并在每个航点原地以 90° 增量扫描一整圈。搜索区域来自任务场景配置，不表示教师知道目标位置，也不会在目标尚未进入相机几何视场前直接朝目标飞行。

相机为前视相机，FOV 判断使用采集器的 1280×960 标定内参（`fx=fy=539.936368...`、主点 `(640,480)`），并限制探测距离为 80 m。目标投影进入图像后进入 `approach`：先把机头转到目标方位（允许 ±10°），再以不超过 15 m 的步长接近，直到默认 15 m standoff；随后 `hold` 2 s，最后 `done/stop`。每步标签包含 `phase`、`target_in_fov`、`target_bearing_deg`、`target_range_m`。

位姿执行器保存绝对高度和偏航的上一条**指令参考**，下一条 setpoint 从该参考叠加显式命令，因此测量噪声不会累积成高度或偏航漂移；记录中的 achieved 值仍来自实测遥测。当前几何 FOV 尚未建模遮挡和真实 line-of-sight，这是已知限制；这些样本只声明 Gazebo/PX4 SIM 控制链和 privileged teacher 标签，不声明真实相机、GNSS 或 sim-to-real 性能。
