# 固定翼与 VTOL 仿真冒烟飞行

采集器默认仍使用 `x500_mono_cam` 的多旋翼 OFFBOARD 流程。传入其它
`--airframe` 后，起飞、盘旋、重定位、返航和降落都走固定翼/VTOL 分支，
MAVLink 遥测和相机帧仍按 PX4 仿真时间对齐。

| 机型 | PX4 autostart | 起飞 | 转换 | 降落 |
| --- | ---: | --- | --- | --- |
| `rc_cessna_mono_cam` | 4003 | `runway`、`hand`、`catapult` | 无 | 跑道/机腹 |
| `advanced_plane_mono_cam` | 4008 | 滑跑 | 无 | 跑道 |
| `standard_vtol_mono_cam` | 4004 | VTOL | MC -> FW，再 FW -> MC | VTOL |
| `quadtailsitter_mono_cam` | 4018 | VTOL | MC -> FW，再 FW -> MC | VTOL |
| `tiltrotor_mono_cam` | 4020 | VTOL | MC -> FW，再 FW -> MC | VTOL |

## 降落流程

固定翼（`advanced_plane`、`rc_cessna_runway`）返航后不会重复发送多旋翼
`LAND` 命令。采集器根据当前高度和 `FW_LND_ANG`（代码默认 5 度；主线程
可在 SITL 参数中核对）计算下滑距离，在跑道入口反向生成进近航点，然后在
同一条 telemetry/GCS 连接上上传三个 `MISSION_ITEM_INT`：进近航点、
`DO_LAND_START`、`NAV_LAND`。上传完成后切换 `AUTO.MISSION`，等待
`EXTENDED_SYS_STATE.landed_state == 1`，并记录空速、着陆状态和失败原因。
机腹降落使用相同的任务协议，只改变档案中的着陆方法。

VTOL 返航后先发 `MAV_CMD_DO_VTOL_TRANSITION(param1=3)`，等待
`vtol_state == MC`，再发 `NAV_VTOL_LAND`，最后等待地面状态。转换和着陆
分别使用 90 秒和 420 秒状态预算，命令 ACK 仍从 reader 缓存读取。

伞降只做参数检查并标记“仿真未验证”，不会伪造成功。固定翼滑跑是真实
PX4/Gazebo 起飞；手抛/弹射用 Gazebo 力脉冲近似，见下文“手抛和弹射起飞”。

## 运行冒烟脚本

### 手抛和弹射起飞

`rc_cessna_mono_cam` 的 `hand` 和 `catapult` 模式会在 PX4 已解锁并接受固定翼起飞命令后，先确认 `/world/default/wrench/persistent` 和 `/world/default/wrench/clear` 出现在 `gz topic -l`（pinned 镜像里没有 `set_entity_state` 之类的设速度服务，只有 ApplyLinkWrench 的力话题），再对 `rc_cessna_mono_cam_0::base_link` 施加一次世界坐标（ENU）力脉冲（`gz.msgs.EntityWrench`，力写在 `wrench { force {...} }` 里）。总质量按约 1.7 kg 计算：手抛目标 12 m/s、0.25 s、82 N、抬头 20 度；弹射目标 17 m/s、0.15 s、193 N、抬头 10 度；脉冲期间另加与重量相等的向上力。施力和延时清除在同一次 `docker exec` 里**同时启动**（各自的 `gz topic` 启动开销相互抵消），否则顺序执行会让力持续约 3 秒。`gz topic` 构造消息失败时也会返回 0，所以输出里出现 “Unable to create message” 同样判失败。采集器只写入参考资料中确认存在的 `FW_LAUN_DETCN_ON=1`、`FW_LAUN_AC_THLD=30`、`FW_LAUN_AC_T=0.05`。

注入动作会记录话题、模型、力向量、时间戳和 PX4 `STATUSTEXT`。只有 PX4 报告起飞/发射检测且 `LOCAL_POSITION_NED` 证明飞机爬升，episode 才能通过。

**真实 SITL 结果（2026-10-18）**：手抛、弹射两种方式都完成了“发射 → 爬升 → 盘旋（空速在档案范围内）→ 返航 → 在 home 跑道降落”。但要如实说明：遥测显示手抛脉冲实际只让地速增加约 4 m/s（目标 12 m/s），飞机没有离地，随后靠电机推力在地面加速完成起飞——仿真里的“手抛”目前更接近“推一把 + 短距滑跑”，不是真正把飞机抛到空中。原因是经 `gz topic` 命令行施力，力的实际持续时间受进程启动和发现延迟影响，无法精确控制。它验证的是 PX4 发射检测参数、起飞检测、爬升和后续流程；手抛动作本身的物理逼真度需要真机或带精确时序的仿真插件来验证。

先设置工作目录环境变量，并确保 Docker/PX4 镜像可用。只查看步骤而不启动
容器时加 `--dry-run`：

```powershell
$env:TEMP=$env:TMP=$env:TMPDIR='D:\Projects\mdwt\af-land\.tmp'
$env:CUDA_VISIBLE_DEVICES='-1'; $env:PYTHONPATH='src;.'
python scripts/smoke_airframe_flight.py --airframe advanced_plane_mono_cam --dry-run
```

真实验证命令如下（每次使用独立输出目录和端口）：

```powershell
python scripts/smoke_airframe_flight.py --airframe rc_cessna_mono_cam --takeoff-method runway --out .tmp/smoke-cessna-runway --control-port 14540 --telemetry-port 14541
python scripts/smoke_airframe_flight.py --airframe rc_cessna_mono_cam --takeoff-method hand --out .tmp/smoke-cessna-hand --control-port 14542 --telemetry-port 14543
python scripts/smoke_airframe_flight.py --airframe rc_cessna_mono_cam --takeoff-method catapult --out .tmp/smoke-cessna-catapult --control-port 14544 --telemetry-port 14545
python scripts/smoke_airframe_flight.py --airframe advanced_plane_mono_cam --out .tmp/smoke-advanced-plane --control-port 14546 --telemetry-port 14547
python scripts/smoke_airframe_flight.py --airframe standard_vtol_mono_cam --takeoff-method vtol --out .tmp/smoke-standard-vtol --control-port 14548 --telemetry-port 14549
python scripts/smoke_airframe_flight.py --airframe quadtailsitter_mono_cam --takeoff-method vtol --out .tmp/smoke-tailsitter --control-port 14552 --telemetry-port 14553
python scripts/smoke_airframe_flight.py --airframe tiltrotor_mono_cam --takeoff-method vtol --out .tmp/smoke-tiltrotor --control-port 14554 --telemetry-port 14555
```

脚本输出 JSON 判定：起飞高度、VTOL 状态转换、VFR_HUD 空速范围、每次
重定位误差、是否进入 RTL、`landed_state`、相机帧数和帧/位姿时间对齐。
判定来自 `episode.json`、`mavlink.jsonl(.gz)` 和 `steps.jsonl`，不再用
collector 退出码代替真实状态。

固定翼帧时效门限默认为 100 ms，多旋翼仍为 250 ms。固定翼速度更高，旧帧
在一个动作周期内造成的位移误差更大，因此门限更窄；实际判定还要求帧的
仿真时间戳不早于对应 `LOCAL_POSITION_NED` 时间戳。

只验证采集器参数和场景、不启动容器：

```powershell
python scripts/collect_sim_teacher_episodes.py --airframe advanced_plane_mono_cam `
  --scenario configs/scenarios/airframes/advanced_plane_mono_cam_001.scenario.json `
  --teacher explore --episodes 1 --steps-per-episode 8 --smoke-flight --out .tmp/collector-fw --dry-run
```

## 手抛/弹射 SITL 核实

不同 PX4 镜像的 Gazebo 世界服务名不同，主线程必须在实际容器内核实：

```powershell
docker exec <px4-container> gz service -l | Select-String 'pose|state|world'
docker exec <px4-container> gz service -i -s /world/default/set_pose
docker exec <px4-container> gz topic -l | Select-String '/world/default/wrench'
docker exec <px4-container> px4-param show FW_LAUN_DETCN_ON
docker exec <px4-container> px4-param set FW_LAUN_DETCN_ON 1
```

若服务列表没有设置线速度/冲量的接口，记录为“替代方案：滑跑起飞或空中
出生”，不要把 hand/catapult 结果当成真实手抛/弹射。
