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

固定翼（`advanced_plane`、`rc_cessna_runway`）的降落顺序是：

1. 先 RTL，等飞机回到 home 附近（`2 × 盘旋半径 + 50 m` 以内，最多 300 s）；
2. 在 home 的跑道上直线进近降落：着陆点就是 home，跑道方向取 PX4
   `HOME_POSITION` 里的航向（PX4 在解锁时记下机头方向，滑跑起飞就等于跑道方向）；
3. 上传三项任务：`DO_LAND_START`（`MAV_FRAME_MISSION`，固定版本的 PX4 不接受全局坐标系的这一项）、
   在跑道反方向、离 home 至少 `盘旋半径 + 1.3 × 20 m / tan(下滑角)`（且不少于 250 m）处
   `NAV_LOITER_TO_ALT` 盘旋降到 20 m、`NAV_LAND` 落在 home；
4. 切 `AUTO.MISSION`。PX4 刚收到任务时会先校验，太早切换会回“currently not available”，
   所以一直重试到心跳显示 `AUTO.MISSION`（最多 15 s）；
5. 等 `EXTENDED_SYS_STATE.landed_state == 1` 或已上锁。若 30 s 收不到心跳，
   直接报“遥测中断”（多半是仿真被本机负载卡死），不再空等 420 s。

**没有测距仪的降落参数**：飞机只有单目相机、没有测距仪。PX4 默认
`FW_LND_USETER=1`、`FW_LND_ABORT=3`，找不到地形就中止降落，并在
`MIS_LND_ABRT_ALT`（30 m）上空盘旋（日志 “Holding at 30 m above landing waypoint”）。
固定翼机型档案的 `landing.parameters` 因此写入 `FW_LND_USETER=0`、`FW_LND_ABORT=0`，
采集器在 SITL 里按档案下发；真机也必须这样设置，拉平高度按 GPS/气压高度判断。
机腹降落使用相同的任务协议，只改变档案中的着陆方法。

**坐标约定**：策略和执行器都用本地 NED（米）。PX4 通过 `COMMAND_LONG` 收到的
`DO_REPOSITION`/`DO_ORBIT`/`NAV_TAKEOFF`/`NAV_LAND` 位置是 WGS84 经纬度和
**海拔（AMSL）**高度。`local_ned_to_global` 以 PX4 home（经纬度、海拔、本地坐标）为锚点换算；
没有 home 的地理坐标时命令直接失败，不会把“米”当成“度”发出去。此前的仿真冒烟
把本地米数直接当经纬度、把相对高度当海拔发送，只因 Gazebo 世界海拔约为 0 才没坠机，
重定位实际飞向几千公里外——已修复，冒烟判定也新增“不得飞出 1.5 km 范围”。

已在真实 PX4/Gazebo SITL 验证（2026-10-17，`advanced_plane` 滑跑）：起飞、盘旋空速
14.6–14.9 m/s（档案范围内）、RTL、在 home 跑道降落并检测到着地（“Landing detected”/
“Mission finished, landed”），全程最远离 home 325 m，帧与位姿时间对齐。

VTOL 返航后先发 `MAV_CMD_DO_VTOL_TRANSITION(param1=3)`，等待
`vtol_state == MC`，再发 `NAV_LAND`（PX4 不接受以命令形式发送的 `NAV_VTOL_LAND`，会回 UNSUPPORTED；它只能作为任务项），PX4 原地降落，最后等待地面状态。RTL 与转换命令几乎同时得到 ACK，采集器按命令号分别记录 ACK，避免后到的 ACK 覆盖先到的。

已在真实 PX4/Gazebo SITL 验证（2026-10-18，`quadtailsitter`）：VTOL 起飞、转固定翼（vtol_state 4）、盘旋空速 17.4–21.9 m/s、RTL、转回多旋翼、原地降落并检测到着地，全程最远离 home 137 m，帧与位姿对齐。转换和着陆
分别使用 90 秒和 420 秒状态预算，命令 ACK 仍从 reader 缓存读取。

伞降只做参数检查并标记“仿真未验证”，不会伪造成功。固定翼滑跑是真实
PX4/Gazebo 起飞；手抛/弹射用 Gazebo 力脉冲近似，见下文“手抛和弹射起飞”。

## 最终回归（2026-10-18，真实 PX4/Gazebo SITL，全部通过）

判定项：起飞、VTOL 转换、盘旋空速（下限严格，上限允许转换后 10% 瞬时超调）、进入 RTL、起飞后着地、帧与位姿对齐、不飞出 1.5 km。固定翼/VTOL 至少在 30 m 飞行（VTOL 为转换高度 + 10 m），不再用多旋翼默认的 5 m。

| 机型 | 起飞方式 | 盘旋空速 (m/s) | 离 home 最远 (m) |
|---|---|---|---|
| advanced_plane | 滑跑 | 19.7 / 17.8 | 336 |
| rc_cessna | 滑跑 | 13.6 / 12.1 | 329 |
| rc_cessna | 手抛（力脉冲近似） | 14.0 / 11.5 | 331 |
| rc_cessna | 弹射（力脉冲近似） | 12.7 / 10.9 | 332 |
| standard_vtol（4+1） | VTOL | 20.5 / 18.4 | 142 |
| quadtailsitter | VTOL | 22.1 / 18.6 | 125 |
| tiltrotor | VTOL | 23.9 / 22.3 | 128 |

这一轮过程中修掉的问题：返航与降落任务抢先（改为确认进入 RTL 后再上传）；VTOL 返航后先回到 home 附近再降落；倾转旋翼在 5 m 高度转换时坠地（改为最低 30 m）；手抛力脉冲在高负载下可能清不掉、飞机失控飞到 3.5 km（改为两个发布进程结束后再补一次清除）。

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

## 有目标场景的固定翼闭环（2026-10-19）

场景：把已有生成世界 `gen_v2_w0031`（任务“找红色消防车”，场地里还有轿车、SUV、邮筒等干扰物，最高障碍是 23.3 m 水塔）派生成固定翼版本（`scripts/derive_fixed_wing_scenario.py`，`configs/scenarios/fixed-wing-v2/`）：世界、真值、任务不变，只换飞机，并自动挑一条穿过 home、离所有物体最远的跑道方向（035°，离最近物体 28.5 m）。rc_cessna 跑道起降，机上身份模型 r18。

| 架次 | 确认 | 位置误差 | 判分 | 飞行距离 | 降落 |
|---|---|---:|---|---:|---|
| 1（修复前） | 第 54 步，确认的是消防车 | 11.1 m | 失败（超 10 m 半径） | 3,366 m | 着陆 |
| 2（修复前） | 第 28 步 | 8.2 m | 成功 | 3,814 m | 着陆 |
| 3（修复后） | 第 2 步 | 5.7 m | 成功 | 4,571 m | 着陆 |

三架次都确认了正确目标（消防车），身份判断对轿车、SUV 都说“不是”。途中修了两个真问题：
- 机上策略和判分器对所有机型都按 x500 的相机下俯 0.18 rad 投影，而 rc_cessna 的相机是平视的——真值框偏约 50 px、地面点算近；现在从机型档案读相机安装角（`231a289`），x500 不变（档案里就是 0.18 rad），advanced_plane / tiltrotor 档案改成与仿真模型一致的 25°。
- 每一步都按“当前高度”下发航点，转弯掉的高度永远补不回来（从 29 m 降到 18 m，还在 23 m 水塔旁盘旋过）；现在保持上一次指令的高度，修复后全程 28–33 m（`bedbfa9`）。

还没解决：检测框相对真值投影有约 20 px 的固定偏移（约 4°，带来 8–15 m 定位误差，原因排查中）；每次看到目标轨迹编号都换了，三角化用不上，位置都退回地面相交；飞过头后相机看不到目标，只能下一条航线再接近（没有绕目标盘旋）；确认后返航前在场地上空多飞了 30 多步。

### 飞过头与返航绕圈的根因与修复（2026-10-19，待 SITL 复核）

逐步回放 w0031 三个架次（`artifacts/datasets/fw-target/w0031-ep1..ep3`）：
- **飞过头**：循环的“转向/前进一步”对固定翼都变成 ≤27 m 的重定位，飞机约 2 s 就到点，决策周期却是 3–9 s（中位约 7 s），余下时间 PX4 绕这个点盘旋（全程滚转约 30°），每步实际飞出 70–90 m，前视相机被甩开；跟丢 4 步后放弃焦点回到搜索，ep1 因此要 5 次重新接近。
- **返航绕圈**：返航同样用“转向 + 一步”，飞机在 home 外约 80 m 一圈圈绕，永远进不了 3 m 到达半径，直到采集器 60 步上限（ep2 31 步、ep3 57 步）。

修复（`9b62c10`）：
- **过顶航线盘旋**：以目标为圆心盘旋时目标始终在机头 90° 方向，水平半视场只有 49.85°，平视前向相机永远看不到（几何验证见 `tests/test_fw_geometry.py`）。改为让盘旋圆的边经过目标、圆心在视线右侧一个半径处的顺时针 `orbit`：飞机先沿当前视线直飞目标，过顶后绕一圈再沿同一入航弧回来，每圈正对目标看一次。rc_cessna 30 m 高：R ≈ 90 m（坡度约 14°，大于最小转弯半径 21 m），目标在 120 m 进入视场、45 m 离开画面底部，每 38 s 一圈约 5.7 s 可见。焦点在盲圈期间按地面位置保留同一航迹，预算按圈数（默认 3 圈），过顶最近视角按多旋翼“最近视角”规则确认。盘旋圆先查静态障碍、安全包络围栏和任务禁飞区，放不下就缩小半径；最终仲裁对整个圆和入圆航段做同样检查。
- **返航交给自动驾驶仪**：非多旋翼确认后（或步数用完、任务 return_home）直接发 stop，由 RTL 直飞 home 并执行降落；直线回家穿过禁飞区时仍走绕行航点。

SITL 复核时看：`labels.onboard.fw_orbit`（圆心、半径约 90 m、laps）；确认前同一航迹号跨盲圈保持；确认那一步就是 stop（`return: autopilot`），之后紧接 RTL/降落，没有 returning 步。

### 过顶盘旋接近 + 自动驾驶仪返航（2026-10-19，`3d45741`）

同一场景、同一机型，换成“过顶盘旋”接近（圆心在视线右侧约 90 m，目标在圆上，每圈正对目标看一次）与确认后直接交给 PX4 RTL 降落：

| 架次 | 确认 | 位置误差 | 飞行距离 | 降落 |
|---|---|---:|---:|---|
| 1 | 第 2 步（一次盘旋后） | 5.78 m | 108 m | 着陆（PX4 “Landing detected”） |
| 2 | 第 3 步 | 5.89 m | 122 m | 着陆 |

之前同场景要 28–54 步、飞 3.4–4.6 km、误差 5.7–11.1 m，确认后还在场地上空空飞 30–57 步。注意：这个场景的目标离跑道近，起飞后很快就能看到，这两架次没有考验长距离搜索；还缺目标离得远、需要多圈盘旋才能确认的场景。

### 远目标场景与 EKF 姿态偏差根因（2026-10-19 晚）

**远目标场景**：把 `gen_v2_w0016`（找救护车，目标离 home 102 m，场地里还有公交车、SUV、邮筒、锥桶等干扰物）派生成固定翼版本。修复前代码跑第 1 架次：第 19 步确认救护车，定位误差 4.5 m，确认后 RTL 着陆（`landed_state=1`）。

**EKF 姿态偏差的根因**：分析 PX4 ulog，对比 `vehicle_attitude` 与 `vehicle_attitude_groundtruth`。原先怀疑生成世界没设磁场，这一条不成立：gz-sim 8.14 按经纬度自己算 WMM 磁场。实际是两项仿真侧失配叠在一起：
1. **镜像里 Gazebo Harmonic 的磁力计把 roll 和 pitch 对调了**，等价于用绕 yaw 转 90° 的共轭姿态去旋转磁场；对 sensor_mag 的拟合残差只有 1e-4 G。平飞时读数正确，倾斜 15–40° 时偏 20–40°。EKF2 把这部分误差吸收进了地磁和磁偏状态：学出的地磁倾角是 -1°，实际应为 -13°。
2. **传感器延迟参数与仿真不符**：仿真里 GNSS 和空速都没有延迟，PX4 默认却按 110 ms（GNSS）和 100 ms（空速）补偿。在每秒 20° 以上的转弯中，这会产生约 3° 的航向误差，而且转完弯也不消失。

修复（`a44ae78`，只影响仿真，真机保留实测延迟）：
- 改用 PX4 自带的仿真磁力计（`SENS_EN_MAGSIM=1`）；Gazebo 用去掉磁力计系统的 server.config 副本启动。两个磁力计的 device id 相同，所以不能靠校准优先级只关掉一个。
- 设 `SENS_GPS0_DELAY=0`、`EKF2_ASP_DELAY=0`。

server.config 的结构如果变了，容器启动会直接报错退出，不会悄悄失效（`1edb294`）。同一条 cessna 航线修复前后对比：

| 运行 | yaw 均值 / p95 | 直线段 yaw | roll / pitch p95 |
|---|---|---|---|
| 修复前 | +1.88° / 6.3° | +2.9° | 0.9° / 1.3° |
| 只修磁力计 | +2.6° / 4.0° | +3.1° | — |
| 两项都修 | -0.09° / 1.1° | +0.04° | 0.60° / 0.29° |

修复对所有机型都生效。x500 没有单独做前后对比，正在跑的数锥桶 3 架次（x500）同时用来确认多旋翼在新配置下飞行正常；固定翼 w0016、w0031 各 2 架次复测定位误差（`artifacts/datasets/fw-target/*-ekf-ep*`）。

**EKF 修复后的 SITL 复测（2026-10-19 21:00 前后，`1edb294` / `32d4f9f`）**：

| 场景 | 架次 | 确认 | 位置误差 | 定位来源 | 降落 |
|---|---|---|---:|---|---|
| w0016 救护车（102 m） | 修复前 ep1 | 第 19 步 | 4.5 m | — | 着陆 |
| w0016 | 修复后 ep2 | 第 16 步 | **0.7 m** | 多视角融合 | 着陆 |
| w0016 | 修复后（误确认，见下） | 第 2 步 | 152.8 m（公交车） | 单视角 | 着陆 |
| w0031 消防车 | 修复后 ep1 | 第 2 步 | 6.5 m | 单视角地面相交 | 着陆 |
| w0031 | 修复后 ep2 | 第 2 步 | 6.3 m | 单视角地面相交 | 着陆 |

- w0031 的 6 m 误差几乎全在视线方向（偏近 5.8–6.4 m，横向 < 1 m），修复前后都一样，不是姿态问题：检测框底边是车辆近端，消防车长 12.4 m，单视角地面相交量到的是车头/车尾，不是车辆中心。多视角融合时（w0016 ep2）沿视线误差只有 0.5 m。这在 10 m 判定半径内，暂时如实记录。
- **误确认**：上一圈被判“不是”（公交车）的物体，下一圈换了航迹编号，又被一张滚转 36° 的画面判成救护车。过顶规则允许最近视角单次确认就算数，于是确认在离真值 153 m 的公交车上。修复（`32d4f9f`）：固定翼过顶时，同一位置之前有拒绝记录的，单次确认不能推翻，必须两次确认。多旋翼不变：多旋翼远处判“不是”、飞近后单次确认是计数小锥桶的正常路径。修复后 w0016 再跑 2 架次复测（排在计数之后）。

## eVTOL 有目标闭环：悬停作业（2026-10-19/20）

目标：让 `standard_vtol`（4+1 复合翼）和 `quadtailsitter`（尾座式）在有目标的生成世界里跑通机上闭环“找目标 → 确认 → 返航降落”，与多旋翼/固定翼同一套机上 AI。场景用 `scripts/derive_fixed_wing_scenario.py --airframe standard_vtol|quadtailsitter` 从固定翼版 w0031（消防车，离 home 45 m）和 w0016（救护车，离 home 102 m）派生（`configs/scenarios/vtol-v2/<机型>/`，世界、真值、任务不变，只换飞机，在 home 垂直起飞）。

**改之前的实际行为**（读代码 + 第 1 架次）：VTOL 被当成“不能悬停的飞机”——割草机搜索、接近时“转向/前进一步”被换成 22 m 的 FW 重定位（就是固定翼“飞过头”的老问题），而过顶盘旋只对 `fixed_wing` 开；悬停相关的命令也有缺陷：`mc_hold` 被执行成按固定翼盘旋半径绕圈，`stop` 被换成“在目标处降落”。

**方案**（VTOL 能悬停，所以接近和确认用多旋翼那套）：
- 能悬停的 VTOL 默认 `vtol_hover_ops=true`：搜索（多旋翼的扫视 + 扩展方形）、接近（转向/前进一步、最近视角、下降）、确认（两次确认/最近视角规则）都跑多旋翼的机上循环，命令在 MC 下执行：转向和前进是 `mc_reposition`（带航向、5 m/s），等待是原地悬停的 `mc_hold`；确认后发 `stop`，由 PX4 RTL 返航并垂直降落（supervisor 的 land-now 仍原地降落）。设为 false 回到原来的 FW 行为。
- 遥测报告 FW 时，策略先发 `transition` 转回 MC，这一步不跑循环；采集器等 `vtol_state` 真的变成 MC 再拍下一帧。
- 悬停作业的 VTOL 在采集器里用 `NAV_TAKEOFF` 按多旋翼起飞并留在 MC（`NAV_VTOL_TAKEOFF` 爬升后会自己转 FW）。第 1 架次按旧流程先转 FW 再转回 MC，尾座式回转时从 30 m 爬到 60 m、漂出 80 m。搜索高度设为任务高度，被抬高后先降回来。
- 采集器对 MC 命令等到“到点、航向误差 ≤ 6°、速度 ≤ 0.6 m/s 并保持 0.5 s”再拍下一帧，按 PX4 仿真时钟计时（之前 `mc_reposition` 沿用固定翼“沿航迹剩余 < 5 m 就算到”，原地转向会立即返回、拍到转了一半的画面）。

**尾座式相机核对**：悬停实拍帧（ep1 第 2、3 步，俯仰约 0°）画面大半是天空，地面只在下方——光轴比水平**抬高** 20°，与 `docs/fixed-wing-vtol-simulation.md` 的几何推导一致（地面远角的竖直位置：实拍约 v=400–420，按抬高 20° 预测 397–419，按下俯 20° 预测 194–210）。档案矩阵原来是按 `camera_ray_from_profile` 的转置读法写的，而 `profile_mount_pitch_down_rad`（x500 等档案都按它和 SDF 核对）把它读成下俯 20°。现在统一成一种约定（矩阵各列是相机各轴在机体系中的方向），尾座式矩阵按这个约定改写；前飞时这个安装方向朝下偏前。另外修了尾座式全姿态投影的一个 bug：射线原点是 N/E/down，地面求交时把负的 down 值当成离地高度，地面点永远是 None——ep2 因此确认了消防车却没有位置。

**真实 PX4/Gazebo SITL 结果**（机上身份模型 r18，`artifacts/datasets/vtol-target/`；误差 = 确认位置与真值的水平距离，10 m 内算成功；航迹为 LOCAL_POSITION_NED 水平路径长度，含返航）：

| 机型 | 场景 | 架次（代码） | 确认步 | 位置误差 | 着陆 | 水平航迹 | 说明 |
|---|---|---|---:|---:|---|---:|---|
| quadtailsitter | w0031 | ep1（`b22c34e`） | — | — | 着陆 | 403 m | 旧起飞流程转 FW 再转回，爬到 60 m；相机朝天，40 步 0 检测 |
| quadtailsitter | w0031 | ep2（`4c2b88e`） | 33 | 无位置 | 着陆 | 150 m | 确认的是消防车，但地面点全为 None（投影 bug，已修） |
| quadtailsitter | w0031 | ep3（`9424bb6`） | 22 | 5.0 m | 着陆 | 149 m | |
| quadtailsitter | w0031 | ep4（`9424bb6`） | 22 | 6.1 m | 着陆 | 179 m | |
| quadtailsitter | w0016 | ep1（`9424bb6`） | 8 | 4.4 m | 着陆 | 45 m | 接近时从 29 m 降到 17 m |
| standard_vtol | w0031 | ep1（`4c2b88e`） | 2 | 3.8 m | 着陆 | 3 m | 在 home 悬停扫视就两次确认（39 m 外） |
| standard_vtol | w0031 | ep2（`9424bb6`） | 2 | 3.0 m | 未判定 | 3 m | 降落到 3.6 m 时仿真冻结（PX4 ulog 也停在 58 s），非代码问题 |
| standard_vtol | w0031 | ep3（`9424bb6`） | 2 | 3.2 m | 着陆 | 2 m | |
| standard_vtol | w0016 | ep1（`9424bb6`） | 5 | 1.7 m | 着陆 | 19 m | 先拒绝两个干扰物，再接近救护车 |

修复后的 7 架次全部确认了正确目标、误差 1.7–6.1 m（全在 10 m 内），6 架次真实着陆（另 1 架次仿真冻结）；全程 `vtol_state` 一直是 MC，RTL 在 MC 下飞回 home 并垂直降落。w0016 另有 2 次启动时 120 s 收不到 PX4 心跳（环境问题，重跑正常）。尾座式的 EKF 姿态与真值对比：yaw 偏差中位 0.16°、p95 0.44°，位置 p95 0.37 m。

**还没解决**：
- 尾座式悬停时相机看地平线以上 20°，30 m 高度只能看到约 75 m 以外的地面：w0031 要搜 20–30 步才看到目标，确认都发生在 90–122 m 外、框只有 13 px 左右。根本办法是云台或下视相机；当前机型只适合先远后近的接近。
- 多旋翼的“两次确认”规则不看距离，VTOL 继承后会在 40–120 m 外用 13–14 px 的框确认（x500 同一规则）。如需更稳，可给 VTOL 配 `confirm_max_range_m`，代价是多飞接近步。
- 大范围搜索仍是 MC 扩展方形；“FW 割草机搜索、有焦点后转 MC 接近”没有实现。`vtol_hover_ops=false` 只回到旧的 FW 行为（无过顶盘旋）。
- 尾座式前飞（FW）模式的相机几何没有用前飞实拍帧重新核对：ep1 第 0 步（遥测已是 FW 姿态）的画面像悬停视角，原因未查。
- 真机运行时（`OnboardRuntime`）的 `transition`/`stop` 分派与 MC setpoint 泵只有单元测试；MC 起飞方式（`NAV_TAKEOFF`）目前只在仿真采集器里切换。

**混合搜索选项（尚未 SITL 验证）**：能悬停的 VTOL 可设置 `vtol_search_mode: "fw"`，先用固定翼割草机搜索，发现焦点后请求回切 MC，再用多旋翼接近和确认；候选被拒后留在 MC，并用多旋翼搜索重新扫视。默认值 `"mc"` 保持现有全程悬停作业行为。过渡进行中不会推进机上 loop；相机俯仰按当前 MC/FW 模式读取档案矩阵。

### 2026-10-20 复测汇总

**固定翼 w0016（救护车，距 home 102 m），按位置计票的确认规则（`e6b84c7` 之后）**：

| 架次 | 确认 | 误差 | 降落 |
|---|---|---:|---|
| r4 ep1 | 第 25 步 | 1.7 m | 着陆 |
| r4 ep2 | 第 39 步 | 6.4 m | 着陆 |

之前同一场景：一次误确认公交车（单次确认推翻了上一圈的拒绝）；一次只要有拒绝就不准单次确认，飞了 11 km 都没确认。现在两次都确认了正确目标。

**固定翼 w0031，跨航迹融合（`2d967a2`）之后**：两架次都在第 2 步（第一圈）确认，误差 7.3 m / 6.4 m，着陆。第一圈就够两次确认，没有第二圈的观测可融合，所以约 6 m 的“框底在车头”偏差依旧（沿视线偏近，横向 < 1 m）。大型车辆需要“沿视线补半个车身”这类修正，暂未做。

**单视大目标地面锚点修正（2026-10-20）**：机上 loop 现在复用任务典型尺寸，把大型目标的框底近边地面命中沿底部射线推向预期中心；关联仍使用原始 ground 锚点，锥桶等小目标不变。该改动尚未 SITL 验证。

**eVTOL 回归（审查修复、材质本地化之后）**：

| 机型 | 场景 | 确认 | 误差 | 降落 |
|---|---|---|---:|---|
| standard_vtol（4+1） | w0031 | 第 2 步 | 3.1 m | 着陆 |
| quadtailsitter | w0031 | 第 20 步 | 4.8 m | 着陆，全程 MC |
| standard_vtol 混合搜索（`vtol_search_mode=fw`） | w0016 | 第 48 步 | 1.7 m | 着陆 |

混合搜索那一架起飞转固定翼后很快看到候选，回切多旋翼接近；候选被拒后按设计留在多旋翼里继续搜索，所以固定翼割草机航线只用了开头一小段。若想大范围搜索更多走固定翼，需要“被拒后再转回固定翼”的规则（待定，涉及频繁转换的代价）。

**PX4 启动时收不到心跳（10-19 / 10-20 共 6 次）的根因**：容器失败时保留的日志显示 PX4 在等 Gazebo 加载世界时超时；Gazebo 卡在联网下载 Fuel 模型——hatchback / ambulance 的 `.mtl` 材质把贴图写成 `https://fuel.gazebosim.org/...` 网址，每次加载用到它们的世界都要下载，网络慢就卡住。修复：缓存构建把这些网址改成模型自带贴图的相对路径（模型里没有的两张轮子贴图去掉，渲染为无贴图），共享缓存已重建（`aba95e1`）；PX4 等 Gazebo 世界的次数从 30 次放宽到 180 次（`4a5f0d2`），采集器等首个心跳 240 s。之后 3 架 VTOL 与固定翼都正常启动。
