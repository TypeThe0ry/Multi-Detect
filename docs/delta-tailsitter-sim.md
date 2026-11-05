# 折叠尾座式三角翼接入说明

本机固定相机档案 ID 为 `delta_tailsitter`，云台档案 ID 为
`delta_tailsitter_gimbal`。Gazebo 传感器包装分别为
`delta_tailsitter_mono_cam`（固定相机，4050）和
`delta_tailsitter_gimbal_cam`（三轴云台，4051）。资料来自只读的
`md-scratch/px4-upstream/delta_tailsitter_design_report.txt` 全文和镜像中的
`4018_gz_quadtailsitter`。质量采用报告的**壳体化优化方案 E**，不是待交付实体 CAD
直接打印的质量。当前使用参数化占位外观，**未经本机 SITL 飞行或实飞验证**。

## 启动链路

沿用现有链路：场景 runtime → `multidetect.simulator.stack` →
`scripts/run_simulator_docker.py` → 容器原有 `rcS` → 自启动脚本 → Gazebo。

- 固定相机使用 `PX4_SYS_AUTOSTART=4050`、`PX4_SIM_MODEL=gz_delta_tailsitter_mono_cam`；
  云台版使用 `PX4_SYS_AUTOSTART=4051`、`PX4_SIM_MODEL=gz_delta_tailsitter_gimbal_cam`。
- 仓库 `simulation/gazebo/models` 仍只读挂载在 `/opt/multidetect-gazebo-models`，
  其路径在 `GZ_SIM_RESOURCE_PATH` 中优先于镜像模型。`PX4_GZ_MODELS` 不另起一套路径。
- 固定版额外挂载 `simulation/px4/airframes/4050_gz_delta_tailsitter`；云台版额外挂载
  `simulation/px4/airframes/4051_gz_delta_tailsitter_gimbal`，两者均只读。
  不改镜像、不替换 4018；4050 在本仓库/已有上游参考编号中未占用。
- `delta_tailsitter` 只含机体、桨、电机、气动、标准飞行传感器和固定显示的 C11 外壳，
  不含相机与云台关节，也不 include 上游 quadtailsitter。两个包装模型 include 同一底座：
  `delta_tailsitter_mono_cam` 固定相机，`delta_tailsitter_gimbal_cam` 增加 C11 三轴关节和传感器。
  两种包装的 RGB/深度都使用共享标定 `gazebo-front-camera-v1`：1280×960、水平视场 1.74 rad。
  仅空速传感器继续引用镜像中的 `airspeed` 资源。IMU、气压计、磁力计、GNSS
  频率/名称保持上游约定，继续使用现有启动脚本中的 SIM 磁力计修补。
- RGB、深度、里程计、可选分割相机维持现有链节名/传感器名和默认话题规则。
  `delta_tailsitter_mono_cam_seg` 和 `delta_tailsitter_gimbal_cam_seg` 按现有采集器 overlay 机制只读覆盖对应包装。
  C11 的 100 g 已在整机预算内；包装相机/云台链节合计分配 20 g 并从底座扣除，
  没有再次 include 会增加 50 g 的通用 mono_cam。20 g 是当前缺少 CAD/称重数据时的保守下限，
  到位后应更新预算。
- `scripts/run_sim_session.py` 和采集器均接受档案 ID 或 `_mono_cam` 别名。
  `derive_fixed_wing_scenario.py --airframe delta_tailsitter` 可为既有生成世界派生
  AI 场景；首次最小场景已附在 `configs/scenarios/airframes/delta_tailsitter_mono_cam_001.*`。

`--camera-noise-overlay` 的通用 mono_cam 资源覆盖不作用于本地 C11 链节；需要测试
相机噪声时应在 C11 包装 sensor 的 `<camera><noise>` 中另行设置。

## 坐标、质量与地面接触

CAD 轴为 `x` 展向、`y` 向尾、`z` 翼面上方，单位 mm；CG 为 `(0,646,-9)` mm。
Gazebo 的 `base_link` 按上游尾座 MC **FLU** 方向，原点选在报告 CG：

```text
x_base = -(z_CAD + 9) / 1000
y_base = x_CAD / 1000
z_base = (646 - y_CAD) / 1000
PX4_FRD = (x_base, -y_base, -z_base)
```

旋转行列式为 +1，不镜像。机头在 Gazebo `+Z`，悬停时向天；前飞时 PX4 俯仰约
−90°。原 CAD 惯量 `(0.097,0.373,0.431)` 按轴换算为整机 CG 惯量
`diag(0.431,0.097,0.373)` kg·m²。四个 18 g 桨叶链节从主体质量与惯量中扣除，
主体自身 CG 和惯量用平行轴定理反算；动力学底座扣除包装占位后为 2.928996 kg，
两种包装组合均为 2.929 kg，CG 在原点（上游空速资源的微小数值质量未再重新建模）。

| 项目 | CAD mm | Gazebo base_link m |
|---|---|---|
| C11 | `(0,258,-55)` | `(0.046,0,0.388)` |
| 电机中心 | `(±600,629,±150)` | `(-0.159 或 0.141, ±0.6, 0.017)` |
| 桨盘 | `(±600,605,±150)` | `(-0.159 或 0.141, ±0.6, 0.041)` |
| 支脚最低接触点 | `(±600,1055,±150)` | `(-0.159 或 0.141, ±0.6, -0.409)` |

固定版相机沿用 `quadtailsitter_mono_cam` 的固定俯仰和 `body_to_camera_rotation` /
`attitude_rotations` 约定；云台版相机安装在 C11 三轴云台上。STEP 转换器会将 `C11` 单独导出为
`meshes/c11.stl`，四个桨分别导出为 `rotor_0..3.stl`；`visual.stl` 不再包含桨，
桨网格随 `rotor_*_joint` 旋转。云台关节为 `c11_yaw_joint`、`c11_roll_joint`、
`c11_pitch_joint`，Gazebo 控制话题沿用 PX4 上游：
`command/gimbal_yaw`、`command/gimbal_roll`、`command/gimbal_pitch`。RGB 和深度
传感器挂在标准的 `camera_link` 和 `depth_camera_link`，因此现有采集器话题路径保持不变。

云台角度约定是云台底座坐标中的 yaw、roll、pitch（弧度话题值），相机外参按
`机体→云台底座 + 实时关节角` 计算。SDF 云台关节零位为中立位，不预置 −45°；
`delta_tailsitter_gimbal.json` 中 MC 默认 −45°、FW 默认 +45° 是任务/MNT 目标参考角，
应由 PX4 指令设置。上游云台也未配置非零 `initial_position`。没有实时关节样本的旧固定
相机调用仍使用原矩阵。

碰撞仅一个机身盒与四个半径 12 mm 的接触球；球的最低点在同一平面，机身盒比它们
高 5 mm。场景将 CG 放在地面上方 0.411 m，初始离地 2 mm 后落稳。地面摩擦初值
0.8。无风场景用于首轮测试；报告给出的窄支脚侧风倾覆风险仍然存在。

## 电机和气动初值

电机编号/旋向沿用 4018：0/1 对角 CCW，2/3 对角 CW，四路输出功能 101–104，
无舵面。展向力臂 ±0.6 m；计入 CG 的 −9 mm 偏移后，两排实际俯仰力臂为
0.141/0.159 m，而不是丢掉偏移强行写成 ±0.15 m。较下排需要约 13% 的额外悬停推力。

`maxRotVelocity=1100 rad/s`，`motorConstant=16.2/1100²`，每电机峰值 16.2 N。
`momentConstant=0.025 m` 与 `CA_ROTORn_KM` 同值、符号按旋向取值。
`THR_MDL_FAC=1` 补偿平方推力模型，使 44% 表示归一化推力而非 44% 的转速；
整机理想悬停推力比为 `2.929×9.80665/(4×16.2)=0.4433`。
持续 1.4 kg 和短时峰值 1.65 kg 的热约束没有独立热模型；不要把无限时长满推力当成实机能力。

气动使用 `gz-sim-lift-drag-system`：中央面左右拆为各 0.1995 m²、CP 展向 y=±0.262 m，
四个外翼面各 0.052 m²，
法向按 ±35° 反角设置。局部 `cla` 用投影权重
`0.399+0.208 cos²(35°)` 修正，保证整体小迎角 `CLα=2.82/rad`。
中央 CP 采用 CAD y=612 mm（报告中央翼 NP 距机头 412 mm），外翼 CP 反解到
整体 NP y=666 mm：比 CG 靠尾 20 mm，静稳定裕度 `20/569=3.51%`。

简单 LiftDrag 没有独立 CD0 和二次诱导阻力字段：

- 诱导阻力用 `AR=1.25²/0.607`、`e=0.8`，在 15 m/s 配平迎角处将
  `CL²/(πeAR)` 近似为线性 `cda |α|`。它不能完整复现整个空速/迎角范围的极线。
- 两个各半面积、`cla=0`、`a0=±0.5` 的阻力面，在 `|α|<0.5 rad` 时合成恒定
  `CD0=0.032`，放在 CG 以免引入虚构配平力矩。
- `CLmax` 在报告 0.7–0.9 范围中取 0.8；失速后斜率暂取 −1/rad。
  反弯等效使用中央翼 `a0=+1.4684°`、四块外翼 `a0=-3.4387°`：两者在巡航点保持总升力不变，
  并抵消原有配平力矩。离线检查按插件角度符号得到 `dMy/dα_plugin≈+7.08 N·m/rad`；
  插件角度与常规固定翼迎角符号相反，因此对应常规迎角下的稳定负斜率。按 LiftDrag 的
  CP 局部速度 `v_link + ω×r_CP` 计算，15 m/s、7.43° 时基座 +Z 滚转率 1 rad/s 产生约
  −1.98 N·m 阻尼力矩。旧离线检查器把升力向量取反，原先记录的负斜率是检查器符号错误。
  这是假设的等效气动配平，不代表已经获得真实翼型剖面力矩数据。所有系数来源及推导也写在 SDF 注释中。

- 电机插件没有桨轴向前进比推力曲线。新增两片 CG 阻力面，额外 `ΔCD0=0.108`，在
  24 m/s 贡献约 21.7 N 二次阻力，作为 10×4.7 桨高速推力下降的近似限速代理；它不是实测
  桨图，22–25 m/s 目标范围仍需新的 SITL 遥测验证。

空速设失速/最小/巡航/最大为 10/12/15/19 m/s，过渡门槛 15 m/s，前/后转换时长
5/10 s，前转换推力 0.25、后转换减速度 1 m/s²、MC 电机爬升斜坡 5 s，转换超时 20 s。
这些值让前转换逐步建立空速，后转换先减速再恢复悬停推力。
FW 速率环从 4018 起点按本机惯量和差动控制力臂缩放：俯仰 3.21×、滚转（映射 MC 偏航）
8.93×、偏航（映射 MC 滚转）1.46×。固定版日志仍有持续输出饱和，因此这些比例必须用
后续 ULog 检查闭环响应和积分饱和。4050 当前 `FW_THR_TRIM=0.35`，用于阻力估计和无舵面
差动余量。

电量只用于能力/规划估计和 PX4 电池参数：4S5000 = 74 Wh，保留 20%，悬停
500 W 得 7.1 min，巡航 160 W 得 22.2 min；另外留 6 Wh 过渡预算。报告约 17 min
混合任务续航不是这里验证出来的飞行结果，PX4 电池模拟也没有按每个电机电流重新标定。

## 用户执行的冒烟飞行

以下命令需要用户自行执行；本次接入只做离线测试，不启动 SITL/Docker。
使用已有固定版本镜像，输出放当前工作树 `.tmp`，不写共享 `artifacts`：

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR="$PWD/.tmp"
mkdir -p "$TMPDIR"
PY=/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python
export MULTIDETECT_SIM_NETWORK=host
export MULTIDETECT_SIM_GPU=off  # CPU 渲染；已有 GPU 镜像可改用 GPU1/2

# 仅查看动作，不启动仿真。
"$PY" scripts/smoke_airframe_flight.py --airframe delta_tailsitter_mono_cam --dry-run
"$PY" scripts/smoke_airframe_flight.py --airframe delta_tailsitter_gimbal_cam --dry-run

# 真正起飞 -> 转平飞 -> 盘旋/重定位 -> RTL -> 转 MC -> 垂直降落。
"$PY" scripts/smoke_airframe_flight.py \
  --airframe delta_tailsitter_mono_cam --takeoff-method vtol \
  --out .tmp/smoke-delta-tailsitter \
  --container-prefix md-smoke-delta --control-port 14570 --telemetry-port 14571

# 云台版稳定悬停后通过 gimbal v2 下发 pitch=-45°、yaw=0°，并等待
# GIMBAL_DEVICE_ATTITUDE_STATUS 三轴误差均不超过 4°；判定写入 smoke judgement。
"$PY" scripts/smoke_airframe_flight.py \
  --airframe delta_tailsitter_gimbal_cam --takeoff-method vtol \
  --out .tmp/smoke-delta-gimbal --container-prefix md-smoke-delta-gimbal \
  --control-port 14572 --telemetry-port 14573
```

若要保留 ULog 或使用 GPU，可直接执行同一采集器的冒烟路径：

```bash
"$PY" scripts/collect_sim_teacher_episodes.py \
  --scenario configs/scenarios/airframes/delta_tailsitter_mono_cam_001.scenario.json \
  --airframe delta_tailsitter --teacher explore --smoke-flight \
  --episodes 1 --steps-per-episode 8 --keep-px4-ulog --no-gpu-render \
  --out .tmp/smoke-delta-ulog --container-prefix md-delta-ulog \
  --control-port 14570 --telemetry-port 14571 --sim-network host

"$PY" scripts/collect_sim_teacher_episodes.py \
  --scenario configs/scenarios/airframes/delta_tailsitter_gimbal_cam_001.scenario.json \
  --airframe delta_tailsitter_gimbal_cam --teacher explore --smoke-flight \
  --episodes 1 --steps-per-episode 8 --no-gpu-render \
  --out .tmp/smoke-delta-gimbal-collector --container-prefix md-delta-gimbal \
  --control-port 14572 --telemetry-port 14573 --sim-network host
```

有现成 viewer 镜像时，`scripts/run_sim_session.py --airframe delta_tailsitter` 会选中
相同场景。AI 自主飞行可以继续用该会话的 `--onboard-task`、`--onboard-out`，
或给采集器 `--teacher onboard --onboard-config <现有模型配置>`；模型/服务沿用
当前环境，本接入不启动推理服务。默认 VTOL AI 作业仍是 MC 悬停搜索，冒烟路径才
强制测试 FW 转换；若要 AI 前飞搜索，按现有配置设置 `vtol_hover_ops: false`。

## STEP 到位后

工具无网络依赖，使用已有 OCP/build123d 环境。输入坐标必须是报告中的 CAD mm。

```bash
/data/projects/f1-aero/.venv/bin/python scripts/step_to_gazebo_mesh.py \
  '/实际路径/折叠尾座式三角翼（带电设）11.3.stp' \
  --output simulation/gazebo/models/delta_tailsitter/meshes/visual.stl \
  --collision .tmp/delta-collision.stl --report .tmp/delta-cad-report.json
```

也支持 `.dae`；单文件严格小于 20 MB，默认最多 300000 个三角形。缺少额外简化库时
使用确定性顶点聚类。报告打印装配部件名称、体积、实体数、CAD/变换后包围盒和简化
前后面数/包围盒。可选碰撞仅导出凸包，**不替换四支脚碰撞**。

当 `meshes/visual.dae` 存在时优先选 DAE，否则选 `visual.stl`；二者都没有时使用
参数化外观。工具写入默认目录后同步替换 SDF 外观，并在目标模型目录生成
`rotor_0..3.stl` 和 `c11.stl`。若手动复制网格，Docker 规划器也会在作业目录生成外观
SDF 并只读挂载，不修改仓库。删除 CAD 网格后，可执行下面命令恢复占位：

```bash
"$PY" scripts/generate_delta_tailsitter_model.py
```

PX4/MAVLink 控制使用标准 gimbal v2：先用 `MAV_CMD_DO_GIMBAL_MANAGER_CONFIGURE` (1001)
取得主控，再用 `MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW` (1000) 下发角度。`MNT_MODE_IN=4`
时不发送旧的 `MAV_CMD_DO_MOUNT_CONTROL` (205)，因为它可能返回 ACK 但不驱动云台。
例如 pymavlink：

```python
master.mav.command_long_send(
    target_system, target_component,
    mavutil.mavlink.MAV_CMD_DO_GIMBAL_MANAGER_CONFIGURE,
    0, 255, 190, -1, -1, 0, 0, 1,
)
master.mav.command_long_send(
    target_system, target_component,
    mavutil.mavlink.MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW,
    0,  # pitch, yaw, pitch rate, yaw rate, flags, reserved, device id
    -45, 0, float("nan"), float("nan"), 44, 0, 1,
)
```

PX4 状态优先从 `GIMBAL_DEVICE_ATTITUDE_STATUS`（必要时同时记录
`GIMBAL_MANAGER_STATUS`）读取，采集器把原始状态放入每个 `flight_state.gimbal_status`；
Gazebo 关节话题仍可用于离线校验实际关节角。

## 第十三轮日志定量结论与修正

2026-11-05 第 13 轮固定版的 `mavlink.jsonl` 有 40,087 行，PX4 `LOCAL_POSITION_NED`
最后时间戳为 110.292 s；容器清理前仍为 running，Gazebo 仿真时钟为 109.86 s 且继续
推进，ULog `04_23_14.ulg` 也记录到约 36.8 s 的 `vehicle_attitude`、`vehicle_local_position`
和 `actuator_outputs`。因此第 12 轮的静默不是 PX4/Gazebo 停止：PX4 仍在记录和仿真仍在走，
但 UDP 接收端在高负载下出现丢包或链路静默。第 12 轮静默诊断的精确值是：最后 MAVLink
后墙钟 18.99 s、PX4 时间前进 5.016 s、容器仍运行、`OOMKilled=false`；ULog 的控制器和
传感器主题没有同步停止。采集器现在把 `MavlinkReader.stats`（线程存活、收包总数、按
消息类型计数、解析异常、坏 CRC、最后消息类型/时间戳）写入 `telemetry-silence.json`，
便于区分接收线程异常、解析异常和 UDP 丢包。

固定版第 13 轮的姿态消息 5,327 条，ATTITUDE 的 roll/pitch 范围约 −19.72°..93.28°、
−85.55°..16.75°（含尾座转换段），舵机输出四路分别有约 10.6%、5.6%、10.4%、5.6%
样本达到 0/1100 饱和；VFR_HUD 空速范围 −2.46..21.41 m/s，盘旋样本为 18.88 和
17.78 m/s，返航和降落完整。这说明后段大姿态/输出变化与转换和差动分配饱和有关，
不是新的云台故障；本轮没有改气动和飞控增益。

云台版第 13 轮明确验证了旧命令路径：`MNT_MODE_IN=4` 下 `COMMAND_ACK(command=205,
result=0)`，但 `GIMBAL_MANAGER_STATUS.primary_control_sysid=0`，197 条
`GIMBAL_DEVICE_SET_ATTITUDE` 中目标始终为中性四元数（其余为 NaN），三个 Gazebo 关节
角始终约 0 rad，回读 pitch 误差 45.21°。这表示 ACK 只确认 PX4 接收了不适用的旧协议。
现已将冒烟采集器、SIM 执行器和 onboard vehicle 统一为 gimbal v2：先发送
`MAV_CMD_DO_GIMBAL_MANAGER_CONFIGURE` (1001) 取得主控，再发送
`MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW` (1000)，并保留 4° 回读门槛及关节/四元数记录。
`motion_intent` 也在命令元数据中标注协议和两个命令 ID，避免 AI 动作路径回退到 205。

## 第十四轮日志结论与 gimbal v2 参数修正

第 13 轮固定版 MAVLink 连续到 110.292 s（40,087 行）；ULog 姿态、位置和执行器话题记录到
110.464 s，采证时容器仍运行且仿真时钟继续推进。第 14 轮固定版 MAVLink 连续到
`LOCAL_POSITION_NED.time_boot_ms=114.136 s`（40,791 条记录），ULog 姿态、位置和执行器输出
持续到 114.232 s，CPU `cpuload.load=1.0`，容器仍运行。这两轮都没有发生遥测静默。
第 12 轮确有 18.99 墙钟秒静默、5.016 仿真秒间隔，容器/Gazebo 仍活，最终 ULog 写至约
58.7 s；但旧诊断没有接收线程、socket 或 PX4 UDP 发送侧数据，无法确认是接收线程退出、解析
卡住还是网络丢包。本轮诊断加入线程存活、最后异常、socket、按类型计数和最后 16 条消息，
待下一次静默现场判断。5 仿真秒阈值需同时满足至少 50 墙钟秒，能容忍 0.1×实时；仿真时钟
停滞阈值为 15 墙钟秒，因此不会因慢仿真而按 5 秒墙钟误杀。

第 14 轮云台版检查发生在稳定 MC 悬停后：持续 3 s，最大 |roll|=0.306°、|pitch|=2.630°。
1001 ACK 为 ACCEPTED，但管理器主控仍为 sysid 0；177 条 `GIMBAL_DEVICE_SET_ATTITUDE` 的四元数
均为 NaN（仅速率/标志流），关节 pitch 约 0.00543 rad，命令前后没有转动，所以没有到达角度
命令阶段。第 14 轮当时推断配置参数应为 `param1=device_id`，该推断不正确；第 15 轮数据
证明了这个错误编码会把主控登记成 `sysid=1, compid=255`。ACK 只证明命令被接收，未证明
控制权交给 GCS。MAVLink 定义的配置顺序是 `param1=primary_sysid, param2=primary_compid,
param3/4=secondary_sysid/compid, param7=gimbal_device_id`。第 15 轮前，采集器、SIM 执行器和
onboard vehicle 都使用了错误顺序，现已统一修正；管理器确认等待上限为 30 s，以兼容
0.1×实时下低频状态报告。

第 14 轮固定版全程 MAVLink/ULog 连续覆盖到降落；FW 横向状态在约 57.8 s 后结束并进入转换，
本次没有复现“后段姿态与舵机输出大幅变化”。缺少该异常复现的 ULog，故不调整气动或飞控
增益。第 13 轮以旧参数顺序发出的 1001 同样不能证明控制权成功，先前将问题仅归因于 205
不适用并不完整；实际阻止 manager 获得主控的是 CONFIGURE 参数编码错误。

## 第十五轮日志分析与 gimbal v2 配置修正

固定版完整通过起飞、前转换、盘旋、返航、后转换和着陆；盘旋空速样本为 18.500 和
18.296 m/s，均在档案 12–19 m/s 范围内，7 帧对齐且云台项对固定相机按既有规则不参与门控。
此结果没有给出要求调整气动或飞控增益的证据，本轮不改这些参数。

云台版在稳定悬停测量（3 s，最大 |roll|=0.294°、|pitch|=3.138°）后配置云台。原始
`mavlink.jsonl` 共记录 221 条 `GIMBAL_DEVICE_SET_ATTITUDE`、22 条 `GIMBAL_MANAGER_STATUS`、
4 条 `COMMAND_ACK`。`COMMAND_ACK(command=1001,result=0)` 后管理器状态从
`primary_control_sysid=0` 变成 `sysid=1, compid=255`；配置调用参数当时实际是
`(device_id=1, primary_sysid=255, primary_compid=190, ...)`，依 MAVLink 定义被解释成
`primary_sysid=1, primary_compid=255`。因此采集器的所有权检查正确拒绝继续，没有发送
`MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW(1000)`：SET_ATTITUDE 的四元数持续为 NaN，关节 pitch
命令前后约为 0.005426/0.005316 rad，没有发生可测转动。根因是 CONFIGURE 参数顺序编码错误，
不是安装变换、云台关节零位或 4° 判定阈值。

现已在冒烟采集器、SIM executor 和 onboard vehicle 统一使用
`(primary_sysid=255, primary_compid=190, secondary_sysid=-1, secondary_compid=-1,
reserved=0,0, gimbal_device_id=1)`，并要求读回的主控 sysid/compid 都匹配 255/190，增加测试
断言实际 COMMAND_LONG 参数位置。云台命令、
关节响应和严格小于 4° 回读仍须下一次冒烟实测确认；本轮不能记为云台通过。未启动 SITL，
也没有修改模型或飞控参数。

## 未验证的关键量

最需要日志/台架核验的是：10 寸桨的 `momentConstant`、最大转速/推力曲线及时间常数；
高迎角/桨洗/失速阻力和真实翼型 `Cm0`；4018 控制增益、差动饱和、转换时间和固定翼配平。
报告 NP 自身误差约 ±15 mm，已经接近 20 mm 的静稳定距离。没有舵面、零升力力矩
未知时，巡航所需总推力可能接近维持差动俯仰配平的下限，应优先看上下排饱和。

离线测试检查 XML、相机矩阵、质量/CG/惯量正定、四点接触、总升力斜率/静稳定裕度、
电机编号和机架几何、场景哈希、只读挂载及 STEP 转换。它们不能证明真实 Gazebo
接触求解或闭环飞行稳定；本机未装 `gz` CLI，因此没有声称执行过 `gz sdf -k`。

## 第四轮冒烟日志分析与修正

对 `.tmp/smoke-delta-4/` 中 2026-11-04 的 ULog 和 MAVLink 全程记录做了离线解析。PX4
在约 29.212 s 进入 `vehicle_vtol_status.vehicle_vtol_state=4`；空速约 11.4 m/s，
30.0 s 已升到 21.1 m/s，31--50 s 维持约 23--32 m/s，而 TECS 的真空速目标约
16.46 m/s。油门目标在 31 s 后降到 `FW_THR_MIN=0.05`，同时
`control_allocator_status.thrust_setpoint_achieved=0`，未分配推力约 0.05--0.23，
说明 PX4 已在减速但四电机总推力/差动分配受限，超速不是 TECS 主动加速造成的。

记录中的机体速度方向与 `base_link +Z` 长时间接近反向，机体系相对风迎角从约
−80° 到 ±180° 跳变；因此该段不是稳定的 15 m/s 平飞配平，不能用姿态俯仰角直接
当作气动迎角。SDF 的中央面 `forward=+Z`、`upward=-X`，外翼 `upward` 的 ±Y
符号成对对称，和本尾座坐标约定一致。离线检查在 15 m/s、7.43° 迎角时得到升力
30.37 N、阻力 4.24 N、L/D=7.15；零速返回有限零力，失速/大迎角升力有上界。

本轮将 4050 的 `FW_THR_TRIM` 调到 0.35，并补齐 `VT_FW_DIFTHR_S_R/P=1`，其余
巡航速度档案仍为 10/12/15/19 m/s；该量级沿用镜像 4018 的无舵面 tailsitter 初值，
最终配平仍需用户重新冒烟确认。云台、相机和深度/分割链节从 1e-12 kg 级占位改为
0.001--0.010 kg、惯量不低于 1e-8 kg·m²；桨链节没有碰撞体。这样保持两个包装总质量
2.929 kg、CG 和基准惯量不变，并降低 DART 在极端气动状态下数值发散的风险。

第五轮把 15 m/s、7–8° 巡航配平写入 LiftDrag 的 `a0` 等效项，并将离线检查扩展为
12/15/19/25 m/s、−5…20° 扫描。采集器对 `delta_tailsitter*` 保持 300 s 的 PX4
仿真时间预算，墙钟兜底提高到至少 3600 s；其他机型仍沿用原来的 900 s（300 s 预算）上限。

采集器现在把 SIGTERM/SIGINT 转成异常，经过已有容器、遥测线程和
`sitl_lock_context` 的清理路径；冒烟包装器只转发并等待自己创建的子进程。预期下一次
FW 段应在过渡后逐步收敛到约 15 m/s，油门不长期贴最小值，迎角应是有限且连续的相对风
估计；这仍不能替代真实 Gazebo 运行，尤其是差动推力俯仰配平和 LiftDrag 的大迎角模型
尚未由飞行日志重新验证。

## 第七轮冒烟离线结论与修正

固定版 `smoke-fixed-7` 的 FW 段在约 355.3 s 开始前转换，384.7 s 进入 FW；空速从
385 s 的 15.1 m/s 在 390--420 s 升到 20.5--28.6 m/s，随后长时间稳定在
37.4--37.6 m/s。此时油门已降到 5% 附近，但姿态同时出现约 49--84° 横滚，四路
`SERVO_OUTPUT_RAW` 多次进入 0/1100 饱和，说明主要问题是无舵面 tailsitter 的差动
控制在转换后失稳，气动反弯配平没有问题：离线 SDF 复算在 15 m/s、7.431° 得到
升力 30.367 N、阻力 4.195 N、L/D 7.24、俯仰矩 3.3e-12 N·m。回转命令在
1582.9 s，约 1589.5 s 切回 MC，但空速仍 37.2 m/s、横滚约 78°，随后继续饱和；
落地等待末端空速 41.64 m/s，心跳间歇是失稳后的结果，不是单独的 land-state 判定错误。

4050/4051 共用参数已改为 `VT_ARSP_TRANS=15`、`VT_ARSP_BLEND=10`、
`VT_F_TRANS_DUR=5`、`VT_F_TRANS_THR=0.25`、`VT_F_TR_OL_TM=8`、
`VT_B_TRANS_DUR=10`、`VT_B_TRANS_RAMP=5`、`VT_B_DEC_MSS=1`，并将
`FW_THR_MIN` 设为 0，给前转换和后转换保留减速余量。下一次冒烟应看到 FW 空速主要在
14--19 m/s，前转换不应在前 5 s 突然越过 20 m/s；回转换后应先降到约 10--15 m/s，
再进入 `vtol_state=3` 并落地，不能出现 0/1100 的持续差动饱和。

云台版 `smoke-gimbal-7` 的 `GIMBAL_DEVICE_ATTITUDE_STATUS.flags=64` 表示 yaw 使用
earth frame；原判定把它直接当 vehicle yaw，导致约 49.4° 的固定 yaw 误差。模型还把
PX4 上游的 yaw/roll 关节轴符号反了，并把相机传感器的带平移 pose 复制给
`camera_imu`，使 IMU 在 `camera_link` 下重复偏移。生成器现改为 yaw=`0 0 -1`、
roll=`-1 0 0`、pitch=`0 1 0`，`camera_imu` 使用零平移、yaw=π；判定按 flags=64
先减去机体 yaw，再比较云台 yaw/roll/pitch。下一次命令 pitch −45° 时三轴误差应小于
4°，其中 yaw 不再把机体航向直接计入误差。

## 第八轮冒烟离线结论与修正

2026-11-05 两个变体均未通过完整飞行。固定版盘旋空速为 47.30 m/s，超出档案 12–19
m/s；位置短时采样误差为 26.05、13.68、0、65.44 m，返航和着陆均未确认。云台版空速
约为 −0.04–1.89 m/s，未确认固定翼状态，采集器以 `takeoff_state` 超时结束，未取得
云台回读角。固定版有 5 帧且时间戳对齐，仅能证明采集链工作，不能代表飞行通过。

固定版 `mavlink.jsonl` 的空速中位数约 45.7 m/s、最大约 49.2 m/s；姿态横滚从接近 0°
发展到约 90°，电机输出有 4720/6356 个样本至少一路到达 0 或 1100。该证据表明固定翼
段发生了差动控制饱和和姿态失稳。离线核对的四个 `CA_ROTORn_P*` 坐标、`KM` 符号、SDF
电机位置和旋向与 FLU→FRD 坐标变换及 4018 上游布局一致，因此本轮没有据此盲改电机
几何或控制符号；缺少对应 ULog/控制分配状态，当前不足以把主因归结为符号、增益或气动
模型中的某一项。云台版只在 MC 状态运行，约 20.6 s 仿真时间内高度升至 11.55 m，未
取得固定翼/云台命令证据，不能把这次超时归因于云台角度判定。

冒烟判定此前只要求记录中出现过 FW 状态和 MC 状态，因此起飞前的 MC 状态可被误计为
已完成后转换。现要求先观测到 FW，再观测到后续 MC，判定标准没有放宽。固定版本轮仍会
因空速、返航和着陆失败而失败；两个变体的气动闭环、返航、降落与云台误差仍待取得完整
仿真遥测后继续整定。

## 第九轮冒烟遥测分析与修正

读取 `.tmp/smoke-fixed-9/episodes/episode-0000-f84b1424/mavlink.jsonl`（174,150 条）和
`.tmp/smoke-gimbal-9/episodes/episode-0000-e85d2eaf/mavlink.jsonl`（14,982 条）。固定版
`VFR_HUD` 空速中位数 44.48 m/s、峰值 49.37 m/s；已确认 FW 的 280 个空速样本中位数
38.75 m/s、峰值 48.76 m/s。该 FW 段 3,453 个姿态样本的横滚中位数 84.32°、最大 89.64°，
3,453 个舵机样本中 2,941 个至少一路到达 0/1100，返航后仍有横滚与饱和，最终心跳过期、
未着陆。它确认了高速与横滚失稳并存；不能单凭这份 MAVLink 日志判定控制环符号有错，且没有
ULog/分配器状态可区分外环增益、执行器饱和与动力学误差。

按协调者提出的四点逐项核验：

1. 中央翼确实曾只有一个 `y=0` CP；其左右对称抵消滚转力臂。LiftDrag 在 CP 处使用刚体局部
   速度，因此拆成 y=±0.262 m 后能由角速度形成差动迎角。本轮复核后离线模型在 15 m/s、
   7.43°、+1 rad/s 得到 −1.98 N·m 的阻尼力矩；总翼面积 0.607 m²、整体 `CLα=2.82/rad`、
   15 m/s 升力约 30.37 N、俯仰配平矩约 0。外翼原本已按左右/上下四块分面，展向 CP 为
   ±0.4925 m。原离线工具升力向量符号取反，已按插件向量公式修正；静稳定斜率符号随插件
   角度约定一并纠正，没有放宽数值门限。
2. 镜像电机模型没有轴向桨前进比推力衰减输入；`rotorDragCoefficient` 不等价于桨轴向推力曲线。
   增加 `ΔCD0=0.108` 的二次阻力代理，24 m/s 时约 21.7 N，旨在让速度接近目标区间；未运行
   新 SITL，故不能宣称 22–25 m/s 已达到。
3. CA 四路位置、`KM` 符号及大小与机架旋向一致：0/1 CCW、KM 正；2/3 CW、KM 负，PX4 与 SDF
   均为 0.025。按本机/4018 惯量和力臂比例更新 `FW_PR_*`、`FW_RR_*`、`FW_YR_P`；FW 日志仍有
   严重输出饱和，下一轮要用 ULog 检查新增益是否改善响应，当前不能说滚转闭环已稳定。
4. 云台 SDF 原先给 pitch 关节加了 −45° 初始位置，上游没有此项；现移除，由 PX4/MNT 指令设置
   目标。日志在 hover 起始回报近单位四元数，但进入短暂 FW 时机体姿态已约 roll −62°、pitch
   −74°，随后云台误差测量约 roll 66°、pitch 99°。该段机体已严重失稳，不能据此把误差归因于
   底座安装旋转或单独的云台坐标系；本轮没有盲加 90° 安装角。需要在稳定 hover 下独立发送
   云台命令，记录稳定的机体姿态、设备四元数和 Gazebo 关节角，再确定安装旋转。<4° 尚未验证。

本轮改动没有通过固定版或云台版的完整冒烟门槛。返航、转回悬停、降落、帧对齐和云台 <4°
误差都还不能据本轮结果确认；帧采集也不能替代飞行判定。本轮仅完成离线模型/检查器和参数
修正。固定版冒烟启动命令因 Docker daemon 不可达而退出，未创建容器；云台版未启动，故两个
变体都没有取得本轮飞行验证。

## 第十轮冒烟遥测分析

读取 `.tmp/smoke-fixed-10/episodes/episode-0000-b4e14f96/mavlink.jsonl`（4,819 条）和
`.tmp/smoke-gimbal-10/episodes/episode-0000-17285077/mavlink.jsonl`（14,781 条）。固定版
仅有 54 个 `VFR_HUD` 空速样本，范围 −0.040 至 1.340 m/s；`LOCAL_POSITION_NED.z` 最低
−4.77 m，`EXTENDED_SYS_STATE` 未出现 FW 状态。外部 arm 和起飞命令已接受，但起飞状态等待
超时；记录有 “no heading reference”，没有空速建立或前转换证据。这个失败不能归因到 FW
横滚环，因为本次固定版没有进入 FW。

云台版空速范围 −0.284 至 20.477 m/s，曾观测到 3 个 `vtol_state=4` 样本，但其后没有
`vtol_state=3` 样本，因此按“先 FW、后 MC”的既有判定未完成往返转换。水平距离最大
102.64 m。约 37–44 s 仿真时间，姿态从近水平发展到最大横滚 1.289 rad（73.8°）和俯仰
−1.515 rad（−86.8°）；此段不能视作稳定 FW 配平。云台命令收到成功 ACK，但误差为 yaw
2.16°、roll 71.12°、pitch 93.04°。命令前后机体正处在起飞/转换姿态变化中，设备状态四元数
不足以区分机体姿态贡献、云台关节响应和安装坐标误差；本轮不据此宣称安装角根因已确定。

协调者四项假设复核：

1. **横滚阻尼**：现有模型已经把中央翼分成面积各 0.1995 m²、CP 为 y=±0.262 m 的两面，
   外翼也是左右分面。离线 CP 局部速度计算在 15 m/s、配平迎角、+1 rad/s 滚转率下给出
   −1.985 N·m 阻尼矩（12/19 m/s 分别为 −1.587/−2.515 N·m）。因此“中央面仍在 y=0、没有
   气动滚转阻尼”不符合当前 HEAD。云台版后段出现 73.8° 横滚，说明整体闭环仍未稳定；仅凭
   MAVLink 姿态不能把它归因于阻尼大小、FW 增益或差动执行器饱和。本轮未改动这些参数。
2. **桨前进比**：本地 MulticopterMotorModel SDF 只有转速平方推力参数，没有前进比/空速输入。
   现有模型用 `ΔCD0=0.108` 二次阻力作桨效下降近似，24 m/s 约增加 21.7 N 阻力；这不是桨图。
   云台版峰值 20.477 m/s 来自起飞/转换过程，不是稳态盘旋，不能据此估计最大平飞速度；固定版
   没有进入 FW。尚无新速度平衡证据，未改阻力系数。
3. **FW 控制分配**：当前四路 `CA_ROTORn_KM` 的符号/大小与 0/1 CCW、2/3 CW 的 SDF 旋向一致，
   反扭矩均为 0.025 m；资产测试也核对 PX4 FRD 坐标与 SDF 电机位置。云台版起飞前段
   `|yawspeed|` RMS 约 0.00193 rad/s，但没有独立偏航命令/响应激励，不能据此测出偏航环带宽或
   符号；高横滚段也没有 ULog 分配器数据。本轮不盲调 `FW_RR_*`、`FW_PR_*`、`FW_YR_*`。
4. **云台坐标**：当前零位是各关节 0，`camera_imu` 零平移、yaw=π；云台 yaw/roll/pitch 轴与
   上游定义相同。模型确实没有额外尾座安装旋转，但仅从“局部相机光轴到 base_link +Z”的
   几何映射，不能证明 PX4 在悬停姿态下所需的水平视线变换。试加 ±90° 会依赖未验证的
   DO_MOUNT_CONTROL 角度语义和 base-to-earth 姿态关系；本轮已撤回该试改，没有改变模型。

下一次应先在稳定 MC 悬停段单独发送云台目标，并同一时间记录 `ATTITUDE`、
`GIMBAL_DEVICE_ATTITUDE_STATUS` 四元数/flags、三个 Gazebo 关节角和关节速度；再在同一位置做
小幅偏航命令以验证 MC 偏航轴方向/带宽。固定版应同时记录可确认前转换的 ULog 或至少持续
`VFR_HUD`、`EXTENDED_SYS_STATE`、姿态和四路实际电机输出。取得这些数据前，本轮两个变体
仍未通过；转换往返、盘旋档案空速、RTL、悬停降落、帧对齐和云台误差 <4° 均未确认。

## 本轮日志结论与采集修正（autoloop round-11）

读取 `.tmp/smoke-fixed-11/episodes/episode-0000-aa853641/mavlink.jsonl`（33,599 条）和
`.tmp/smoke-gimbal-11/episodes/episode-0000-b6aaac32/mavlink.jsonl`（2,915 条）。固定版遥测
覆盖约 3.41–94.20 s 仿真时间，包括 4,444 个姿态、4,471 个本地位置和 4,492 个舵机输出样本。
3–11.5 s 内横滚约 −0.44–0.06°、俯仰 0–0.18°，仍在地面附近；11.5–30 s 的 NED 高度
最低 z=−37.11 m。30 s 后姿态范围扩大到横滚 −25.09–166.33°、俯仰 −85.49–45.32°，四路
输出均曾到达 0 或 1100。尽管该次 `status=pass`，它仍不是稳定姿态或执行器裕量的证据。
判定记录的盘旋空速为 18.936、18.977 m/s，均在档案范围内；返航、着陆和 7 帧时间对齐通过，
但 `reposition_success=false`，该项仍应单独看待。

云台版只覆盖约 3.40–11.30 s 仿真时间，含 379 个姿态、390 个位置和 389 个舵机输出样本；
横滚约 −0.07–0.03°、俯仰 −0.18–0.10°，NED z 约 −0.00–0.19 m，离原点最大距离
0.085 m。墙钟约 48.07 s 后遥测停止；末条 MAVLink 到 `takeoff_state` 超时相隔约 1,785.1 s
（29 分 45 秒），说明采集器确实在长时间等不到新遥测后才结束。此次未到达云台测量、固定翼转换
或返航阶段；帧数为 0，因此没有稳定悬停云台误差数据。当前 `episode.json` 也没有容器证据索引，
说明这两份日志产生于自动留存实现启用之前。不能用固定版后段饱和去推断云台版的失效原因，也
没有足够证据支持本轮再改阻力、控制增益或安装角。

采集器已在容器清理前为成功和失败 episode 保存 `container-evidence/`：容器内
`/opt/px4-gazebo/log` 下的 ULog、`/root/.gz/sim/log` 下的 Gazebo 日志、容器原始日志和去除
PX4 `pxh>` 提示符刷屏后的控制台尾部。`index.json` 记录清理前运行状态、仿真时钟新鲜度、清理后
退出码及其来源、`OOMKilled`、复制清单和逐份 ULog 分析状态；ULog 由
`scripts/analyze_px4_ulog.py` 的读取器处理。单项留存失败会进入错误清单，外层 `finally` 仍执行
容器清理。

MAVLink 静默现在按双时钟失败关闭：仿真时钟继续前进但 5 仿真秒没有 MAVLink 时结束；仿真时钟
停滞 15 墙钟秒时结束；另设 60 墙钟秒上限。对仍在前进的仿真时钟，至少等待
`5 / 0.1 = 50` 墙钟秒后才按仿真时间判静默，以容忍 0.1 倍实时。错误记录包含容器是否运行、
仿真时钟值和是否仍在前进，不再等到起飞状态的长超时才暴露断流。

云台检查已移到起飞到位后的 MC 稳定悬停：机体横滚和俯仰须同时小于 5° 并持续至少 3 仿真秒，
再发送目标并按原有 4° 误差容差判定。命令前后均将机体姿态、设备状态四元数与 flags、三个
Gazebo 关节角及其时间戳写入 episode；测量通过后再请求 FW 转换。这个阶段条件没有放宽云台
容差，但本轮失败发生在到达该阶段之前，云台误差仍未验证。

## 第十二轮日志核验

`.tmp/smoke-fixed-12/episodes/episode-0000-600de4fe/` 留有 11,897 条 MAVLink 记录，消息墙钟
跨度 138.4 秒；最后有效遥测在墙钟起点后 119.3 秒、PX4 boot time 36.776 秒，随后 19.0 秒
没有 MAVLink。静默时 Gazebo 仿真时钟仍前进至 41.452 秒，Docker 容器仍运行且未 OOM；
采集器因此在旧的 5 仿真秒阈值触发。ULog 中姿态、位置、推力/扭矩设定点持续到约 36.8 秒，
不足以将遥测中断归因为气动或飞行器失稳。现已把继续前进情况下的墙钟门槛按 0.1 倍实时
计算为 50 秒，绝对墙钟上限仍为 60 秒；仿真时钟完全停滞仍按 15 秒单独报错。

云台第十二轮在稳定 MC 悬停后测量：稳定段 3.008 秒，最大横滚/俯仰分别 0.433°/3.787°；
命令前后姿态均接近水平，但 pitch 关节从 0.314° 仅变到 0.319°，设备姿态 pitch 回读仍约
−0.4°，而目标为 −45°，误差约 45.15°。因此稳定悬停测量条件已满足，但云台执行链路仍失败；
当前数据不足以区分 PX4 输入命令路由与安装/关节轴映射问题，不能据此虚构安装角修正。
本轮没有启动 SITL，固定翼完整往返、着陆和帧对齐也尚未复验。

## 第十六轮冒烟证据与修正

2026-11-05 第 16 轮云台版完整通过：起飞、前后转换、盘旋空速、返航、转回悬停、降落和
7 帧对齐均满足判定；盘旋空速为 17.068、17.110 m/s，云台回读误差为 yaw 0.968°、
roll 0.122°、pitch 3.318°，仍使用 4° 门槛。MAVLink 中有 570 条
`GIMBAL_DEVICE_ATTITUDE_STATUS` 和 `GIMBAL_DEVICE_SET_ATTITUDE`，命令路径为
gimbal v2；`MAV_CMD_DO_MOUNT_CONTROL` (205) 没有回退。云台设备四元数、flags 和三轴
关节状态均已写入 episode，说明本轮云台控制链和安装坐标在稳定悬停工况下有效。

固定版不是飞行判定失败，而是 Gazebo 进程崩溃：`mavlink.jsonl` 在 PX4 仿真时钟
19.516 s 截止，共 5,387 条消息；接收线程仍存活，`parse_errors=0`、`bad_crc=0`，socket
仍打开，最后消息为 `SERVO_OUTPUT_RAW`。同一 episode 的容器证据显示容器已退出、
`ExitCode=139`、`OOMKilled=false`；ULog 中 `vehicle_attitude`、`vehicle_local_position`
和 `actuator_outputs` 均记录到约 19.5 s，Gazebo `server_console.log` 也在相同启动阶段
结束。因此这次静默不是 pymavlink 线程异常、解析失步或 PX4 自身遥测停止，而是仿真器
崩溃后的结果。静默诊断新增 `failure_class=container_exit`，保留线程、socket、计数、
最后消息和容器退出状态，避免把容器崩溃误报为接收端断流。

固定版独有的包装模型把 `camera_link` 写成了相对 `base_link` 的 pose，却没有 fixed joint
把该 link 接入刚体图；云台包装有 `c11_mount_joint`，所以两者表现不同。已在生成器和生成
出的固定相机及分割覆盖 SDF 中加入 `CameraMountJoint`（`base_link` → `camera_link`）。这
不改变质量、质心、惯量、气动或飞控增益，只修复模型拓扑；资产测试现在强制检查该连接。
本轮没有根据单次崩溃改动气动或飞控参数，也没有放宽任何飞行、帧对齐或云台误差判定。
