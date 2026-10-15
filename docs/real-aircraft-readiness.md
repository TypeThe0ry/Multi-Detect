# 真机首飞就绪度评估（2026-10-15）

基于集成分支（已合入真机运行时骨架、任务包与监督器、能量估计、地面模型、签名）的只读分析，由独立审查生成、经主线复核。结论：当前是真机运行骨架，还不能放行首次真实飞行。

## 1. 首次真实飞行的剩余 blockers

- **运行证据仍是 SIM/延迟桩，不是真实闭环。** 文档明确说尚未在 Orin、CSI 相机和真飞机验证（`docs/onboard-runtime-real-aircraft.md:3-5`）。Replay 以 1x 墙钟运行，但 detector 和 slow model 都是延迟桩，不加载模型、不跑 GPU/Gazebo/PX4，`ReplayVehicle` 只记录命令，轨迹不受动作影响（`:160-178`）；Windows 上“70 ms + 1.5 s”只证明线程还能发 setpoint（`:182-198`）。Gazebo frame server、SITL 启停、深度和场景 truth 仍限于 SIM 路径（`:200-207`）。

  具体改动：在目标 Orin 上用真实相机、真实 detector/VLM、真实串口做 1x 长跑；记录输入来源、frame age、模型 deadline、MAVLink 延迟和闭环动作。Replay 只能作为离线回归，不能作为放飞证据。

- **真实运行没有深度或实时避障。** `observation_for()` 把 `depth` 固定为 `None`（`src/multidetect/onboard_runtime/runtime.py:66-84`）；真机入口传 `world_path=None`，因此障碍集合为空（`scripts/onboard_main.py:189-195`、`scripts/onboard_policy.py:425-426`）。搜索和航段 clearance 仍是已知静态地图上的 expanding-square/SDF 检查（`src/multidetect/autonomy/onboard_loop.py:274-280`；`src/multidetect/autonomy/mission_executor.py:198-203`）。

  具体改动：接入实时 depth、rangefinder 或其他可验证的障碍传感器，维护带时间戳的动态 occupancy/safety layer，并在规划输出前做碰撞检查；数据缺失、过期或不确定时只能 hold/return/land，不能继续执行静态地图任务。live avoidance 完成前，首飞任务包必须限制在空旷、已检查环境。

- **地面模型默认仍是平地。** `ground_projection_from_config(None)` 返回默认模型，flat 类型默认 `ground_up_m=0`（`src/multidetect/autonomy/ground_model.py:170-190`）；`GroundProjector` 只与水平平面相交（`src/multidetect/onboard_runtime/projection.py:18-65`）。文档明确真实飞行需要注册 PX4 local reference 或有效 terrain/AGL，不能从 SIM 文件隐式推断（`docs/onboard-ground-projection.md:23-27,103-113`）；AGL 默认关闭（`:140-151`）。

  具体改动：把 PX4 local origin/home-ground 注册、DEM 水平配准和 AGL/rangefinder freshness 纳入起飞前检查；无有效地面数据时 fail-closed，禁止用 `z=0` 继续投影。

- **Orin 运行时性能没有实测，慢调用缺少飞行级 deadline。** Orin 延迟和 5–5.5 GB 内存占用仍是估算；完整判断约 1.7–2.2 s，单 token 约 0.6–1.0 s（`docs/orin-nano-feasibility.md:7-23`）。setpoint pump 最低 10 Hz、默认 20 Hz（`runtime.py:87-135`），但当前 replay gate 只是平均 ≥10 Hz、最大间隔 <0.5 s（文档 `:176-178`），不能证明真实模型、热降频、串口抖动或相机 pipeline。慢 worker 工作时主循环会等待结果（`runtime.py:456-489`）。

  具体改动：在目标 JetPack/电源/散热配置上测 p50/p95/p99 detector、slow call、端到端 action latency、RSS、GPU/RAM、温度和 throttle；为 detector、slow call、输入年龄和 policy action 定义硬 deadline，超时丢弃结果并升级策略；把 setpoint 最大 gap、MAVLink TX 延迟和 watchdog 事件纳入放飞 gate。

- **相机标定、外参和时间同步未完成。** calibration 必须包含显式 extrinsics（`src/multidetect/onboard_runtime/camera.py:44-78`），但时间戳只是本机 receipt time，不是曝光时间（`:98-103`）；实际输出尺寸必须与标定完全一致（`:124-151`）。同时真实入口强制 `mount_yaw_right_deg=0`、`mount_roll_clockwise_deg=0`，因为控制坐标转换尚未支持任意安装姿态（`scripts/onboard_main.py:157-161`）。

  具体改动：用目标镜头、分辨率和安装位置实测 intrinsics、五项 distortion、pitch/yaw/roll、forward/right/down lever arm；加入硬件 timestamp 或相机/PX4 时钟对齐；要么实现完整 yaw/roll/lever-arm 的控制转换，要么把前向安装写成硬件验收约束，并用已测地面点做投影误差门槛。

- **PX4 failsafe、任务包 policy、geofence 和电池策略还未闭合。** 入口不读取或配置 PX4 RC/data-link/OFFBOARD loss 参数（`docs/onboard-runtime-real-aircraft.md:136`）；车辆适配器只有 stream、setpoint、arm、mode、LAND/RTL 命令，没有参数 readback、geofence 或 estimator health gate（`src/multidetect/onboard_runtime/vehicle.py:283-316`）。`COM_DL_LOSS_T` 是 PX4 data-link 超时，不等同于任务包的 `heartbeat_timeout_s/timeout_s`（`docs/operator-manual.md:48-52`）。监督器虽然只允许策略变得更保守（`src/multidetect/autonomy/mission_supervisor.py:168-191,272-327`），但还没有证明与 PX4 动作顺序一致。

  具体改动：起飞前读取并核对 `COM_OF_LOSS_T`、`COM_DL_LOSS_T`、RC loss、`NAV_DLL_ACT`、geofence 和 battery warning/critical actions；建立“任务 policy 与 PX4 action 只能同向收紧”的矩阵，参数不匹配直接拒飞。任务包目前只校验本地 operating-area 数值（`mission_package.py:158-214`），还要把水平边界上传/读取回 PX4 fence 并核对 home、估计器和 GNSS/定位健康。

- **能量模型尚未用真实电池校准。** 默认功率 360/400/500 W 是未校准 engineering floors（`src/multidetect/autonomy/energy_estimator.py:22-40`）；文档明确 SIM 日志不能证明真实 hover/translate/climb 功耗（`docs/onboard-energy.md:95-105`）。需要台架和实飞记录 V/I/Wh、温度、风、阶段功耗和返航 margin（`:117-136`）。

- **无 GCS 的任务包入口不够明确。** `onboard_main.py` 仍要求 `--policy-config` 和 `--task`（`:52-60`）；mission package 只能由 policy config 间接加载（`scripts/onboard_policy.py:380-411`）。应增加显式、签名、机载可读的 `--mission-package`，在无链路时完成 signature、风险、policy、energy、geofence、terrain 和资源校验；签名要求已有文档依据（`docs/mission-security.md:27-35`）。

## 2. 分阶段测试计划

1. **Desk bench：Orin + 相机，无桨。**  
   Entry：目标 JetPack/模型/CSI pipeline 已部署；实测 calibration、外参和电源散热文件审核通过；PX4 可断开或接台架，禁止产生飞行输出。  
   测量：连续 30–60 分钟；camera FPS、frame age、丢帧率、硬件/receipt timestamp 差；detector/slow/policy p50/p95/p99；RSS、GPU/RAM、温度、throttle；模拟 MAVLink 下 10/20 Hz setpoint、最大 gap、TX latency；注入 camera loss、stale pose、model timeout。  
   Exit：无泄漏/过热；所有 stale/timeout 都进入保守动作；setpoint gap、输入年龄和 deadline 达到预设门槛；mission package 签名、单调收紧、energy/geofence/terrain admission 可审计。

2. **HIL：PX4 SITL/HIL + 真 Orin，仍无桨。**  
   Entry：bench 通过；真实 Orin 接真实 PX4 链路；参数、fence、home、estimator、battery telemetry 已 readback。  
   测量：1x real-time 覆盖 arm/OFFBOARD/takeoff/mission/return/land；分别测试 `continue`、定时 return、`hold_then_return`、`return_now`、`land_now`；注入 camera/pose/heartbeat loss、OFFBOARD/data-link loss、slow timeout、battery stale/low、home change、fence edge。记录每个 action 的时间戳、PX4 mode、handoff confirmation、位置误差、setpoint gap。  
   Exit：所有故障都执行预期或更保守动作；PX4 不反向抢先覆盖任务 policy；连续多次运行无 watchdog/pump failure；行为与真实参数 readback 一致。

3. **系留/低空场测：全程无 GCS link。**  
   Entry：HIL 通过；真实电池/功耗和 fence 批准；物理系留、安全员、明确终止条件和确认降落区就位；任务包、模型、日志全部在机上。  
   测量：先 hover、短直线、短 search，再逐步扩大；记录 PX4 mode/armed/home、MAVLink loss、camera age、setpoint gap、V/I/Wh、温度、位置/高度误差、fence margin、实际 return/land。  
   Exit：断链时任务仍能完成或安全 return/land；任何传感器、模型、链路故障只收紧行为；没有 live avoidance 前，只批准空旷环境，不做障碍密集自主穿越。

## 3. 接下来建设优先级

1. **实时传感器安全层**：depth/rangefinder/AGL、动态障碍图、碰撞检查和 fail-closed。  
2. **PX4 preflight/failsafe/geofence/battery 协调**：参数 readback、策略矩阵、拒飞门槛。  
3. **Orin 真机运行时集成**：真实模型、CSI、串口、硬 deadline、热/内存/延迟基准。  
4. **相机—坐标—地形标定链**：intrinsics/extrinsics、时间同步、local origin/DEM/AGL 配准。  
5. **无 GCS mission package 与故障注入 harness**：显式签名入口、机载 admission、断链 HIL 自动化、可审计日志；同时清理当前未合并工作树状态。

### 终端切换与事件记录

RTL/LAND 终端命令发送前，运行时先把 setpoint pump 的目标锁定在当前位置并持续发送。命令每秒重试，只有车辆遥测确认已经进入 RTL 或 LAND 后才停止 pump；命令丢失、拒绝或发送阻塞时继续保持新鲜的 hold 流，并在达到重试超时后按 RTL、LAND、保持 hold 的顺序升级。事件 sink 在独立线程中投递，慢或抛异常不会阻塞安全线程，失败只计数并记录一次。
