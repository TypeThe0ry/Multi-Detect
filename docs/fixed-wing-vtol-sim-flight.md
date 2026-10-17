# 固定翼与 VTOL 仿真冒烟飞行

采集器的 `--airframe x500_mono_cam` 默认分支保持原来的多旋翼流程：同一条
`udpin:127.0.0.1:<telemetry-port>` 连接接收心跳、发送 GCS heartbeat、arm、
OFFBOARD setpoint 和降落命令。固定翼/VTOL 使用同一条连接，但由
`FixedWingVtolExecutor` 把动作映射成 PX4 `COMMAND_LONG`：滑跑或 VTOL 起飞、
`MAV_CMD_DO_REPOSITION` 重定位、`MAV_CMD_DO_ORBIT` 盘旋、返航以及降落。

## 机型和起飞方式

| airframe | PX4 autostart | 起飞 | 转换 | 降落 | 仿真说明 |
| --- | ---: | --- | --- | --- | --- |
| `rc_cessna_mono_cam` | 4003 | `runway`（默认） | 无 | runway/belly | 跑道出生点和朝向来自场景 |
| `advanced_plane_mono_cam` | 4008 | runway | 无 | runway | `advanced_plane_runway_400m` 提供滑跑距离 |
| `standard_vtol_mono_cam` | 4004 | VTOL | MC -> FW，返航前 FW -> MC | VTOL | 4+1 quadplane |
| `quadtailsitter_mono_cam` | 4018 | VTOL/multirotor | MC -> FW，返航前 FW -> MC | VTOL/multirotor | tailsitter 的相机姿态随状态记录 |
| `tiltrotor_mono_cam` | 4020 | VTOL/multirotor | MC -> FW，返航前 FW -> MC | VTOL | `VT_TILT_TRANS` 由 PX4 档案负责 |

`rc_cessna_hand` 和 `rc_cessna_catapult` 档案仍可用于采集器。PX4 的
`FW_LAUN_DETCN_ON`/`FW_LAUN_AC_THLD` 只负责检测加速度，静止出生点不会被伪装成
手抛或弹射。真实验证前需在镜像内确认 Gazebo world 的实体服务：

```powershell
docker exec <container> gz service -l | Select-String -Pattern 'pose|velocity|entity'
docker exec <container> gz service -i -s /world/default/set_pose
docker exec <container> gz service -i -s /world/default/set_entity_state
docker exec <container> gz topic -l | Select-String -Pattern 'model/.*/odometry|pose'
```

若镜像没有可用的 set-pose/set-entity-state 服务，采用明确的替代方案：跑道滑跑
起飞，或在场景中空中出生；不能把它报告成手抛/弹射成功。主线程还应检查：

```powershell
docker exec <container> /opt/px4-gazebo/bin/px4-param set FW_LAUN_DETCN_ON 1
docker exec <container> /opt/px4-gazebo/bin/px4-param show FW_LAUN_DETCN_ON
```

## 冒烟脚本

先设置 Windows 仿真环境：

```powershell
$env:TEMP=$env:TMP=$env:TMPDIR="D:\Projects\mdwt\af-simpath\.tmp"
$env:CUDA_VISIBLE_DEVICES="-1"; $env:PYTHONPATH="src;."
```

只做静态检查（不会启动 Docker）：

```powershell
python scripts/smoke_airframe_flight.py --airframe rc_cessna_mono_cam --takeoff-method runway --dry-run
python scripts/smoke_airframe_flight.py --airframe rc_cessna_mono_cam --takeoff-method hand --dry-run
python scripts/smoke_airframe_flight.py --airframe rc_cessna_mono_cam --takeoff-method catapult --dry-run
python scripts/smoke_airframe_flight.py --airframe advanced_plane_mono_cam --dry-run
python scripts/smoke_airframe_flight.py --airframe standard_vtol_mono_cam --dry-run
python scripts/smoke_airframe_flight.py --airframe quadtailsitter_mono_cam --dry-run
python scripts/smoke_airframe_flight.py --airframe tiltrotor_mono_cam --dry-run
```

主线程在 Docker/PX4/Gazebo 环境中执行实际冒烟：

```powershell
python scripts/smoke_airframe_flight.py --airframe rc_cessna_mono_cam --takeoff-method runway --out .tmp/smoke-cessna-runway
python scripts/smoke_airframe_flight.py --airframe rc_cessna_mono_cam --takeoff-method hand --out .tmp/smoke-cessna-hand
python scripts/smoke_airframe_flight.py --airframe rc_cessna_mono_cam --takeoff-method catapult --out .tmp/smoke-cessna-catapult
python scripts/smoke_airframe_flight.py --airframe advanced_plane_mono_cam --out .tmp/smoke-advanced-plane
python scripts/smoke_airframe_flight.py --airframe standard_vtol_mono_cam --out .tmp/smoke-standard-vtol
python scripts/smoke_airframe_flight.py --airframe quadtailsitter_mono_cam --out .tmp/smoke-quadtailsitter
python scripts/smoke_airframe_flight.py --airframe tiltrotor_mono_cam --out .tmp/smoke-tiltrotor
```

每次输出 JSON 都包含起飞、VTOL 转换、盘旋空速区间、三次重定位误差、返航、
`EXTENDED_SYS_STATE` 的 `vtol_state/landed_state`、相机帧数及时间戳对齐结果。
固定翼帧必须满足 `sensor_stamp_ns >= LOCAL_POSITION_NED.time_boot_ms * 1e6`；
固定翼默认时效门限为 100 ms，多旋翼为 250 ms。高速固定翼采用更窄门限是为了
避免运动期间使用过期画面，锁步过慢时会在 JSON 中给出失败原因而不是丢弃状态。

采集器等价的 dry-run 用法：

```powershell
python scripts/collect_sim_teacher_episodes.py --airframe advanced_plane_mono_cam `
  --scenario configs/scenarios/airframes/advanced_plane_mono_cam_001.scenario.json `
  --teacher explore --episodes 1 --steps-per-episode 8 --smoke-flight --out .tmp/collector-fw --dry-run
```
