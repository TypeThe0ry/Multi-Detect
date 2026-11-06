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
`delta_tailsitter_gimbal.json` 中 MC 默认 −20°、FW 默认 −45° 是任务搜索/MNT 目标参考角，
应由 PX4 指令设置。FW 保持 −45°，因为固定翼前飞的现有成像/外参参考仍使用该下视角；
MC 改为 −20° 以匹配身份任务的搜索视角。上游云台也未配置非零 `initial_position`。没有实时关节样本的旧固定
相机调用仍使用原矩阵。

## 身份视角调查与 AI 任务云台复位

检查 `configs/training` 中可见的训练配置，没有身份模型训练样本的相机俯角字段；因此不能
从模型训练配置直接断言 MobileCLIP/身份模型实际训练视角。身份任务的生成场景配置记录了
相机俯角，事实分布如下（取各目录的
`*.teacher_intent.json` 的 `observation.camera_pitch_rad`，换算为向下角度）：R20 为
10.313°、14.324°、20.054°、24.064°、27.502°（20 个世界，均值 19.538°）；R21ID、
R21ID-R3、R21ID-R4 各自全部为 24.064°（分别 24、72、24 个世界）；R21ID-R5 为
7.356°–26.161°（12 个世界，均值 18.490°）。因此场景配置覆盖约 7°–28° 的俯视，
大量样本集中在约 20°–24°；没有配置证据表明模型只见过精确 −20°，也没有仅凭这些
配置证明 −45° 会导致 SUV/救护车误识别。`.tmp/ai-repeat-gimbal/report.json` 的现象仍是
待实验验证的视角假设。

云台版 AI 任务启动时仍先以 `−45°` 做自检；自检通过后，非严格冒烟的 onboard 任务会
读取 profile 的 MC 默认角并发送 `yaw=0°、roll=0°、pitch=−20°`，等待新鲜姿态回读在
4° 内确认到位，失败就中止任务并报错。严格 Delta 冒烟路径保持只测 `−45°` 且不复位。
FW 默认角仍是 `−45°`，所以 FW 任务不会被这条 MC 搜索复位改变。

AI 外参路径已核对：每个仿真观测从 Gazebo joint sidecar（缺失时回退到
`GIMBAL_DEVICE_ATTITUDE_STATUS`）写入 `observation.gimbal.angles_deg`；onboard policy 的
`camera_ray_from_profile` 将这组实际角度传给 `profile_camera_mount_rotation`，因此使用的是
回位后的实测角，而不是静态 `−45°` 常量。相关离线单测覆盖实际角度读取、复位成功、复位
失败报错和严格冒烟不复位。本轮未启动 Docker/SITL，不能据此声称飞行通过。

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

- 电机插件没有桨轴向前进比推力曲线。两片 CG 阻力面各用 `cda=0.216`，合成额外
  `ΔCD0=0.108`，在 24 m/s 贡献约 21.6 N 二次阻力，作为 10×4.7 桨高速推力下降的近似限速代理；
  它不是实测桨图，22–25 m/s 目标范围仍需新的 SITL 遥测验证。

空速设失速/最小/巡航/最大为 10/12/15/19 m/s，过渡门槛 15 m/s，前/后转换时长
5/10 s，前转换推力 0.25、后转换减速度 1 m/s²、MC 电机爬升斜坡 5 s，转换超时 20 s。
这些值让前转换逐步建立空速，后转换先减速再恢复悬停推力。
FW 速率环从 4018 起点按本机惯量和差动控制力臂缩放：俯仰 3.21×、滚转（映射 MC 偏航）
8.93×、偏航（映射 MC 滚转）1.46×。固定版日志仍有持续输出饱和，因此这些比例必须用
后续 MAVLink 检查闭环响应和差动饱和。当前 4050 基线为
`FW_THR_TRIM=0.32`、`FW_T_CLMB_MAX=6`、`VT_F_TRANS_THR=0.25`、`VT_ARSP_TRANS=15`，不设置
`FW_T_SPDWEIGHT`，阻力代理为每面 `cda=0.216`；横向导航实验参数及其依据见本节末轮次记录。
第 31--33 轮的 `FW_P_LIM_MIN=1`° 已被 MAVLink 复核为无效并移除。delta tailsitter 默认
`MULTIDETECT_PX4_LOGGER_MODE=off`，FW 证据从 MAVLink 采集。

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

若要在其他默认启用记录的机型上复制 ULog，或只更换 GPU 渲染设置，可执行同一采集器的冒烟路径。
delta tailsitter 的默认 `off` 不生成 ULog；`--keep-px4-ulog` 只复制实际生成的文件，FW 判定直接读取 MAVLink：

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

固定版不是飞行判定失败，而是 PX4 进程段错误：`mavlink.jsonl` 在 PX4 仿真时钟
19.516 s 截止，共 5,387 条消息；接收线程仍存活，`parse_errors=0`、`bad_crc=0`，socket
仍打开，最后消息为 `SERVO_OUTPUT_RAW`。同一 episode 的容器证据显示容器已退出、
`ExitCode=139`、`OOMKilled=false`；ULog 中 `vehicle_attitude`、`vehicle_local_position`
和 `actuator_outputs` 均记录到约 19.5 s，Gazebo `server_console.log` 也在相同启动阶段
结束。后续内核日志核验显示，自 2026-11-02 起 PX4 段错误 29 次，其中 28 次位于 logger
线程，至少 22 次落在 `px4+0x2885a9`，即 `px4_sem_wait` 返回点附近；因此此前把该退出归因为
Gazebo 的说法不正确，故障与气动无关。旧 episode 没有保留足以逐一匹配主机 PID 的内核行；
新的容器证据会按启动时间和 PX4 主机 PID 保存 `kernel-segfault.log`，静默诊断使用
`failure_class=px4_logger_segfault` 或 `px4_other_segfault`。

固定版独有的包装模型把 `camera_link` 写成了相对 `base_link` 的 pose，却没有 fixed joint
把该 link 接入刚体图；云台包装有 `c11_mount_joint`，所以两者表现不同。已在生成器和生成
出的固定相机及分割覆盖 SDF 中加入 `CameraMountJoint`（`base_link` → `camera_link`）。这
不改变质量、质心、惯量、气动或飞控增益，只修复模型拓扑；资产测试现在强制检查该连接。
本轮没有根据单次崩溃改动气动或飞控参数，也没有放宽任何飞行、帧对齐或云台误差判定。

## 第十七轮 status=pass 的证据缺口与收紧门槛

第十七轮固定版平飞约覆盖 42–51 仿真秒，云台版约覆盖 45.5–55.5 仿真秒。盘旋和三次重定位
收到 MAVLink ACK 后就推进到下一阶段；ACK 只表示 PX4 收到命令，不代表持续盘旋或到达目标。
因此旧 `status=pass` 没有覆盖先前在长时间飞行中出现的超速和失稳。本次增加的门槛只对
`delta_tailsitter` / `delta_tailsitter_gimbal` 的 smoke-flight 启用，其他场景的默认动作和等待
行为不变。

新的 delta tailsitter 航线先等一个重定位点真正到达，再进入 FW 盘旋。位置必须进入指令点
8 m 范围，按 PX4 `LOCAL_POSITION_NED.time_boot_ms` 仿真时间最多等待 120 秒；ACK 不算到达。
盘旋阶段要求 VTOL 状态持续为 FW（`vtol_state=4`），累计至少 120 仿真秒且绕行累计角度至少
360°，最多观察 240 仿真秒。飞出 FW 状态或仿真钟/遥测出现超过 1 仿真秒的空档，会使持续段
失败，不会把分段时长拼成连续飞行。另有墙钟上限，以便锁步停滞时结束等待。

`judgement` 在该 FW 持续段内报告分位数、样本数和阈值。空速 p5 不低于档案最小空速、p95 不高于
档案最大空速加 1 m/s；四元数换算的绝对坡度 p95 不高于 `FW_R_LIM + 10°`，抬头角 p5/p95
分别不低于 −15°、不高于 20°；相对指令高度误差 p95 不超过 10 m。每类指标至少需要 30 个样本。
电机输出按 `SERVO_OUTPUT_RAW.servo1_raw..servo4_raw` 计样本；任一路 ≤5 或 ≥1095 时，该时刻
记为一次饱和，FW 段任一路饱和时刻占比须 ≤15%，并同时输出各电机和各输出通道占比。过渡阶段
饱和不进入这个 FW 分母。姿态不读欧拉 roll：从标量优先的机体到 NED 四元数旋转机体 −z 机头
轴与机体 +y 横轴，再分别计算抬头角和相对水平面的坡度，避免尾座机头接近竖直时的万向锁假象。

后转换结束前记录从 FW→MC 开始观察到的最大高度和爬升量，爬升超过 20 m 判失败。相机运行时
检查使用起飞完成后的深度帧：所有深度像素中有效距离大于 0 且小于 1 m 的比例必须小于 5%，
至少要有 5 帧且帧时间戳必须晚于起飞时刻；缺少起飞时刻或深度证据按失败处理。

相机 RGB 与深度内参保持 `gazebo-front-camera-v1` 原值（1280×960、水平 FOV 1.74 rad）。相机
外置到机体 +x 一侧且传感器局部 yaw 为 0：固定版 RGB/深度传感器局部光心偏移为 +0.09 m，
云台版为 +0.16 m；云台相机壳体移到镜头后方，深度与 RGB 使用同一局部位姿。可离线运行完整机体、桨和 C11 网格的
FOV 射线检查，云台按默认 yaw/roll 扫描 pitch −90° 到 0°：

```bash
PYTHONPATH=src:. /data/projects/f1-aero/.venv/bin/python \
  scripts/check_delta_tailsitter_camera.py --variant both
```

只有两个变体所有射线自遮挡率都小于 5% 才返回成功。该几何检查不能代替后续实际 Gazebo RGB/深度
帧复核；本次 96×72 网格检查固定版为 0%，云台版十个角度样本均为 0%。本轮不启动 SITL/Docker，
因此第十七轮旧数据不会被重新判为通过，需由用户在新门槛下重跑。采集器即使退出码为 0，若没有
`episode.json` 证据文件也会判失败。

`delta_autoloop.py --pass-streak N` 默认 `N=1`。设为大于 1 时，固定版和云台版必须连续 N 轮都
通过才写终态 `PASS`；中间 PASS 会记录 streak、tag 加一并直接启动下一轮冒烟，不调用 Codex。任一
变体失败会将 streak 清零并进入 Codex 修正流程。

## 第十八轮结果与故障归因

2026-11-05 第 18 轮两变体均未通过，不据此放宽任何门槛。固定版 `mavlink.jsonl` 共 47,316
条消息，遥测到 PX4 boot time 136.832 s；预检最终 `SYS_STATUS.error_flags=0`，因此先前短暂的
`xy_position_control` 不是本次终止原因。MAVLink/容器证据显示容器以 `ExitCode=139` 退出，
`OOMKilled=false`，ULog 的姿态、位置和 actuator 主题也记录到约 136.8 s。现有容器日志没有
给出崩溃栈。后续主机内核日志统计表明，从 11 月 2 日起 PX4 的 139 退出多为 logger 线程在
同一等待路径段错误；不过旧 episode 未保存匹配 PID 的内核行，因此无法逐条确认第 18 轮这一
次的线程归属。现有证据不支持将它归因于相机固定关节或气动模型。

云台版 `mavlink.jsonl` 共 28,273 条消息，PX4 boot time 到 78.824 s。静默诊断为
`failure_class=px4_hang`：容器仍运行、非 OOM，Gazebo 时钟仍推进；最后 MAVLink 后约 3.068
仿真秒内没有新包，随后达到 60 s 墙钟上限。清理前保存主机 gdb 全线程回溯和
`/proc/<pid>/task/*/{comm,stat}` 到 `container-evidence/px4-threads.txt`。该结果是 PX4 挂死取证，
不能当作完成 120 s FW 持续段。同期 ULog `tecs_status` 仅有 181 个样本（42.84–78.644 s），真空速 p5/
p50/p95 约为 15.91/16.84/21.87 m/s，对应目标约 15.03 m/s；已解析 `actuator_motors` 的 741
个样本中，至少一路控制量触及 0.01/0.99 边界的占比约 25.9%。这些是覆盖窗口内的概况，窗口含
转换过程且未覆盖完整 120 s FW 持续判定区间，不能冒充正式 smoke gate 的 FW 指标。尤其不能据
此直接调整 `FW_THR_TRIM`、气动阻力或差动增益：缺少完整持续段及同步姿态/控制响应来识别因果。

两份容器证据均保留在工作树 `.tmp/smoke-{fixed,gimbal}-18/episodes/*/container-evidence/`。
第 18 轮没有足够证据支持安全的机架参数或气动系数改动；当前阻塞项是固定版可复现的
SIGSEGV 根因未定位，以及云台版锁步遥测静默。两变体的返航、后转换、降落、帧对齐和本轮云台误差
也都没有完整有效的通过记录。本轮没有启动额外 SITL/Docker，既有失败保持失败，等待补齐崩溃栈和
稳定的连续遥测证据后再做针对性修正与连续两次通过验证。

## 第十九轮 logger 崩溃与 PX4 挂死取证

2026-11-05 内核记录中，PX4 段错误共 29 次（自 11 月 2 日起），28 次标记为 logger 线程，
至少 22 次地址为 `px4+0x2885a9`，紧跟 `px4_sem_wait` 返回点 `0x2885a8`。固定翼、其他机型
也有同类记录，并非三角翼或气动专属。第 19 轮云台版 09:38:28Z 退出，与 17:38:27 内核记录
对应；没有硬件错误证据，EDAC 记录只是启动清单。`rc.logging` 原先以
`logger start -b ${LOGGER_BUF} -t ${LOGGER_ARGS}` 使用默认定时模式；这里 `-t` 用于按时间组织
日志目录，崩溃栈落在等待定时信号量的路径。

Docker 启动器提供 `MULTIDETECT_PX4_LOGGER_MODE=default|poll|off`。第 19–21 轮两个 delta tailsitter
变体曾默认使用 `poll`；第 22 轮起默认改为 `off`，并设置 `SDLOG_BACKEND=0`。`poll` 曾通过临时改写
容器内 `rc.logging` 使用 `-p 100`（100 ms 数值轮询间隔）；显式 `default` 恢复原模式，其他机型仍为
`default`。生效模式记录在 episode 元数据，未在全项目统一切换。

第 19 轮固定版另有一类故障：MAVLink 与 ULog 均在仿真约 66 s 停止，容器仍运行且 Gazebo
时钟继续推进，超过 50 s 静默阈值。该情形归类为 `px4_hang`，不与 logger 段错误混为一类；
采集器在清理前用主机 gdb 保存 PX4 全线程回溯，并读取 `/proc/<pid>/task/*/{comm,stat}`，
输出到 `container-evidence/px4-threads.txt`。journalctl 与 gdb 取证均为 best-effort，失败
不会阻止容器清理。smoke `judgement.failure_class` 使用 `px4_logger_segfault`、
`px4_other_segfault`、`px4_hang`、`telemetry_receiver`、`flight`、`none` 分类，不改变通过门限。

## 第二十五轮：固定翼目标抓圈判定

2026-11-05 第 25 轮两种变体都完成起飞、前转换、120 s 固定翼持续段、返航、后转换和着陆，
相机帧对齐及自遮挡也通过。旧重定位检查要求进入目标 8 m 球体，因此把 PX4 在目标点周围
按 `NAV_LOITER_RAD=80 m` 盘旋判成失败；第 22–24 轮关于让执行器追逐 8 m 距离的判断作废。
现在只检查目标水平圆环：每个采样须满足 `|水平距离 - NAV_LOITER_RAD| ≤ 8 m`，并在固定翼
状态连续保持至少 30 仿真秒、绕目标扫过至少 360°。离开圆环或采样间隔超过 1 仿真秒都会
清零连续段；高度仍由原有高度误差指标单独检查，120 仿真秒超时不变。结果记录
`fw_reposition_ring_error_p95_m`、`fw_reposition_dwell_s` 和 `fw_reposition_swept_deg`。
本配置没有覆盖 `NAV_LOITER_RAD`，第 25 轮 MAVLink 也未包含该参数回读，因此按当前 PX4
有效默认 `NAV_LOITER_RAD=80 m` 判定；离线工具若从 MAVLink 读到此参数则优先使用回读值。
该参数没有被改写。

对已有 MAVLink 轨迹离线重算，固定版圆环误差 p95 为 0.76 m，最长连续保持 104.5 s，扫角
1183°，因此新抓圈判定通过。其空速 p5/p95 为 15.31/17.08 m/s，任一电机饱和比例 7.97%，
原有持续飞行、姿态、高度、后转换爬升、着陆、帧对齐和遮挡指标均通过。固定版第 25 轮因此
满足当前冒烟门限。

云台版圆环误差 p95 为 7.51 m，但最长连续有效段仅 0.804 s、扫角 4.86°，抓圈失败；空速
p95 为 23.86 m/s（档案最大 19 m/s），任一电机饱和比例 72.27%，输出饱和比例 18.52%，也都
超过既有门限。云台回读误差为 yaw 0.39°、roll 0.44°、pitch 3.43°，满足小于 4°要求，
但不能抵消飞行门限失败。本轮没有改动共用气动或速率环参数：固定版已在原门限内，且仅凭
MAVLink 汇总量无法可靠区分云台版过速/饱和来自配平推力、轨迹控制还是传感器闭环；需用下一次
有针对性的实验辨别，不能用放宽判定掩盖。

两版分别出现 2 次和 5 次 `Accel #0 fail: TIMEOUT!`。警告接收时刻前后 8–39 ms 内均有
`LOCAL_POSITION_NED` 与 `ATTITUDE_QUATERNION` 样本，飞控状态流没有同时中断；全程仿真/实时时间
比约为 0.204，说明仿真明显慢于实时，但记录不足以证明 PX4 加速度计本身超时或高偏置影响了
控制。本轮将其记为仿真传感器时序告警，未屏蔽告警、未改传感器参数；后续若要定因，需要同步
记录传感器输入/健康状态与警告时刻。ULog 维持关闭，FW 指标由 `mavlink.jsonl` 计算。

## 2026-11-06 第八轮调参：第 27 轮 MAVLink 证据与推力配平实验

本轮输入为工作树 `.tmp/smoke-fixed-27/episodes/episode-0000-71ed850e/` 和
`.tmp/smoke-gimbal-27/episodes/episode-0000-f56d106f/`；自动轮次摘要位于
`md-scratch/delta-tools/autoloop/round-27/`。两版均完成起飞、前转换、120 s FW 持续、RTL、
后转换、着陆和帧对齐；云台回读误差为 yaw 0.694°、roll 0.091°、pitch 3.561°，低于 4°。
固定版步骤空速样本为 22.755 m/s，超出既有范围；云台版为 17.889 m/s，在范围内。固定版
平移误差为 10.147 m，超过 8 m；云台版为 4.951 m。两版的 `fw_metrics_pass` 都为 false。
两版仍未通过：固定版 FW 抓圈最长连续段为 0.928 s、扫角 9.57°，云台版为 0.756 s、5.03°，
均远低于 30 s 和 360°。完整位置轨迹相对目标的距离中位数分别为 126.11 m 和 118.16 m，
p95 为 189.71 m 和 192.20 m；半径 80±8 m 只被短暂穿过。

FW 持续窗口分别为 160.944–280.952 s 和 165.744–285.772 s。固定版空速 p5/p50/p95 为
16.637/21.566/23.841 m/s，云台版为 16.727/22.215/23.985 m/s；各自 59.8% 和 64.7% 的
VFR_HUD 样本超过 20 m/s。四元数坡度绝对值 p50/p95 为 29.53°/36.23°、28.38°/36.15°。
ATTITUDE_TARGET 四元数换算的坡度目标中位数均约 35°、p95 为 35°，持续贴近 35°限幅。
按原有 `<=5` 或 `>=1095` 判定，任一路电机饱和样本分别占 70.07% 和 72.26%；主要由
servo1/3 触及低端造成（固定版 36.52%/33.57%，云台版 38.79%/33.47%）。四路输出样本的
饱和率为 17.94% 和 18.52%。

两版的共性失败落在共享的 FW 飞行响应，而不是云台回读误差。以目标圈半径 80 m 计算，24 m/s 的
水平转弯需要约 `atan(v²/(gR)) = 36.3°` 坡度，已超过 35° 目标限幅；同时观测到的坡度目标
持续贴限、差动电机频繁到端点，因而无法稳定捕获目标圈。MAVLink 缺少 TECS 与控制分配器内部
数据，尚不能区分高空速由配平推力、气动代理或导航响应哪一项主导。将 `FW_THR_TRIM` 从
0.32 实验性下调为 0.22，按速度平方的粗略关系估算，可把 24 m/s 的高端需求拉回约 20 m/s；
同时 20 m/s 在 80 m 圈上的理论坡度约 27°，为差动控制留出余量。该推算是调参依据，不是
飞行结果；空速、饱和、圆环、姿态、高度及全部任务门限原样保留。没有改 `fw_executor`，也
没有改变 `NAV_LOITER_RAD` 或判定算法。

两版 `px4_logger_mode=off`，均未生成 ULog，MAVLink 证据完整；logger 默认关闭和
`kernel-segfault.log` 中 logger 线程识别已有代码及测试覆盖，本轮未改回 poll，也未改 logger
内部。`Accel #0 fail: TIMEOUT!` 固定版出现 2 次、云台版 3 次。警告附近 MAVLink 的
`LOCAL_POSITION_NED` / `ATTITUDE_QUATERNION` 与告警的最近仿真时差，固定版最大为 1.6 ms，
云台版最大为 29.5 ms；相邻 `SYS_STATUS` 的加速度计 present/enabled/health 位均为真。
本轮没有 `High Accelerometer Bias` 文本，也没有通过 MAVLink 提供加速度计原始样本，仍无法
判定告警是传感器更新超时还是飞控侧消费超时。FW 持续窗口分别约用 401 s 和
462 s 墙钟时间完成，即实时因子约 0.30 和 0.26；这支持仿真较慢，但不足以证明告警无害。
告警未被过滤，未凭此改传感器参数。下一轮仍需核对传感器输入时间戳与 PX4 健康状态。

相关离线测试通过后仍须用两变体重新执行完整 SITL 飞行，核实 `FW_THR_TRIM=0.22` 是否同时
改善空速、端点饱和与目标抓圈，并按间歇故障要求连续两次完整通过；本轮没有启动 SITL/Docker。

## 第二十轮采集器属性错误与第十九轮 FW 遥测量化

第 20 轮固定版和云台版都在启动仿真前以 `AttributeError: 'str' object has no attribute 'id'`
退出，episode 的 `step_count=0`，没有生成 `mavlink.jsonl`。根因是 `CollectionPlan.airframe`
字段为字符串（例如 `delta_tailsitter_mono_cam`），logger 元数据分支却访问了
`plan.airframe.id`。现在通过 `_airframe_profile()` 解析别名并按 profile ID 选择 logger 模式；
新增固定/云台基础 ID 和相机别名的回归测试，其他机型仍选 `default`。没有改飞行判定门槛。

为区分这次采集器故障与气动表现，重新计算第 19 轮云台 MAVLink 持续段标记之后、容器退出之前的
样本，时间窗为 163.788–260.852 s，共 97.064 s，尚未达到 120 s，不能计为持续飞行通过。只统计
`EXTENDED_SYS_STATE.vtol_state=4`；姿态由 `ATTITUDE_QUATERNION` 计算，电机输出按现有
`<=5` 或 `>=1095` 判为饱和：

- 有效空速 388 个样本，p5/p50/p95 为 15.10/15.52/18.76 m/s；
- 四元数姿态 4,721 个样本，绝对坡度 p95 为 35.80°（最大 37.32°），抬头角 p5/p95 为
  3.35°/7.38°；
- 电机输出 4,714 个样本，至少一路饱和占 11.90%，逐路占比为 4.75%、0.13%、6.89%、0.55%。

同轮固定版没有持续段起始标记，遥测在约 66.076 s 停止；仅把所有记录到的 FW 状态样本作为诊断
窗口时，空速 p5/p50/p95 为 15.78/17.52/21.46 m/s，绝对坡度 p95 为 36.58°，至少一路电机
饱和占 49.66%。因它没有完整持续段边界，这些值不作为正式 FW gate 指标，但指向超速与差动输出
饱和需要后续有效持续段复核。第 19 轮云台早期悬停云台回读最大轴误差 3.546°，满足该次
`<4°` 子检查；这不能替代完整任务的通过结果。第 20 轮修复后仍须重新运行两种变体，确认完整航线、
帧对齐、FW 指标以及连续稳定运行；本节记录的旧失败不改写为通过。

修复后的本次执行尝试在 Docker 可用性预检处停止：当前环境返回 `Docker daemon is not reachable`，
没有创建容器或产生新 MAVLink 数据。固定版和云台版的 dry-run 均成功生成各自 Docker 计划；完整飞行
验证仍待 Docker daemon 可用后执行。

## 第二十一轮 logger 参数回归与定量证据

2026-11-05 第 21 轮固定版和云台版都在起飞状态等待期间失败，不能计为飞行判定失败或通过。
固定版 `mavlink.jsonl` 2,566 条消息，PX4 boot time 从 2.868 s 连续到 10.308 s；其中
`LOCAL_POSITION_NED` 358、`ATTITUDE_QUATERNION` 323、`SERVO_OUTPUT_RAW` 365。云台版
1,921 条消息，PX4 boot time 从 3.340 s 连续到 8.716 s；其中 `LOCAL_POSITION_NED` 258、
`ATTITUDE_QUATERNION` 249、`SERVO_OUTPUT_RAW` 259，并收到 27 条
`GIMBAL_DEVICE_ATTITUDE_STATUS` 与 27 条 `GIMBAL_DEVICE_SET_ATTITUDE`。两份日志
`parse_errors=0`、`bad_crc=0`，接收线程和 UDP socket 均存活，说明不是 MAVLink 接收端丢包。

两容器均在 2026-11-05 11:12:34 UTC 前后以 `ExitCode=139` 退出，`OOMKilled=false`，Gazebo
时钟分别停在 10.332 s 和 8.680 s；ULog 高频 topic 也在同一时刻停止。`SYS_STATUS` 在停止前
已从 `error_flags=16384`（短暂的 `xy_position_control` 未就绪）恢复到 0，不能把该预检瞬态
当成终止原因。容器 console 没有 PX4 崩溃栈，按 PID 筛选的内核文件也没有匹配行，所以本轮
只能确定是 PX4 进程 SIGSEGV，不能把它归因成气动或机架问题。

复核第 20 轮引入的启动补丁后发现具体回归：把 `rc.logging` 的
`logger start -b ${LOGGER_BUF} -t ${LOGGER_ARGS}` 改成了 `-p vehicle_attitude`。PX4 logger 的
`-p` 是数值轮询间隔参数，不接受 uORB 话题名；这会把本轮 `poll` 模式变成无效命令行。现在
改为 `-p 100`（100 ms 轮询），保留默认 `default` 和显式 `off` 行为；新增测试锁定数值参数。
本轮未重新启动 SITL/Docker，下一轮必须验证两变体至少连续两次完整通过，再评估 FW 空速、姿态和
电机饱和指标。

## 第二十二轮关闭 ULog 与 FW 重定位根因

2026-11-05 第 22 轮固定版和云台版都完整运行到返航、后转换与着陆；容器退出码为 0，清理前仍在运行，
`OOMKilled=false`。两份 `mavlink.jsonl` 分别有 117,185 和 122,158 条记录；episode 元数据显示
`px4_logger_mode=off`，容器证据中 `ulog_files=[]`、`ulog_analysis=[]` 且 `errors={}`。采集器在没有
ULog 时仍正常保存证据并从 MAVLink 计算 FW 指标。

两版共同失败项仅为严格 FW 重定位：固定版到目标距离 81.169 m，云台版 80.368 m，门限仍为 8 m。
重定位命令前的帧状态与命令目标显示，航点沿用起飞前缓存的位置：固定版实测位置为
`(0.0416, 40.8034)` m、目标为 `(-0.2736, 34.9876)` m；云台版实测位置为
`(-0.0693, 44.4749)` m、目标为 `(0.1952, 34.9727)` m。两目标均在机体当前航向后方，且分别只有
约 5.82 m、9.51 m 的水平差，不是从转换后的实测位置前进 35 m。之后飞机绕目标以约 80 m 半径盘旋，
因此检查器在命令后没有观察到 8 m 内的到达。修正是在完成起飞/前转换后用实测位置和航向同步
执行器基准，再生成第一段相对重定位；判定门限和检查条件没有变化。

120 s FW 持续段的 MAVLink 指标均通过既有门限：

- 固定版 120.004 s、轨迹角 2751.90°；空速 p5/p95 为 15.278/17.784 m/s，绝对坡度 p95 为
  35.879°，抬头角 p5/p95 为 4.182°/7.045°，高度误差 p95 为 2.751 m；任一电机饱和样本占
  8.675%，四路输出饱和样本占 2.244%。
- 云台版 120.024 s、轨迹角 2745.60°；空速 p5/p95 为 15.313/17.902 m/s，绝对坡度 p95 为
  35.862°，抬头角 p5/p95 为 3.636°/6.880°，高度误差 p95 为 2.919 m；任一电机饱和样本占
  9.916%，四路输出饱和样本占 2.558%。云台三轴回读误差为 yaw 0.240°、roll 0.347°、pitch 3.479°，
  均小于 4°。

因此本轮没有依据调整气动或机架参数，也没有改变判定标准；已修复飞控引导航点对起飞前缓存状态的
依赖，并把 delta tailsitter 的 logger 默认值改为 `off`。logger 线程识别同时接受 PX4 PID 后以空格
分隔线程名的内核行。尚未在修复后重新运行 SITL，所以本轮结果不代表两变体已通过；仍需按原门限
验证重定位，并按间歇故障要求确认连续两次完整通过。

## 第二十三轮 MAVLink 证据与重定位基准修复

2026-11-05 第 23 轮两版都完成起飞、转换、120 s FW 段、返航、后转换、降落和帧对齐；logger 维持
`off`，证据中没有 ULog。固定版重定位目标距离 7.942 m，刚好在 8 m 内；云台版距离 80.739 m，
故两版本轮均为 `status=fail`。云台命令前的帧位置约为 `(0.310, 44.125, -38.100)` m，航向
110.65°，目标却为 `(-0.109, 35.004, -40)` m；位移是约 35 m 东向，明显仍以起飞前原点作基准。
固定版帧位置约为 `(0.270, 39.736, -39.992)` m，目标 `(5.043, 74.319, -40)` m，与本轮 FW
航向相符。差异来自云台版起飞后先留在 MC，后续才转 FW；执行器只在起飞函数返回时同步过一次，
后续首条 FW 平移仍使用过渡前位置。现于严格尾座式冒烟任务派发平移前，将执行器位置与航向重新
同步到最新遥测。8 m 到达标准没有变化。

两份 MAVLink 日志分别有 75,510 和 124,498 条记录，主要姿态、位置、舵机输出及目标话题连续采集。
固定版持续段空速 p5/p95 为 15.872/21.268 m/s（p95 超过 20 m/s 上限），坡度绝对值 p95 为
36.095°，抬头角 p5/p95 为 2.433°/6.829°，高度误差 p95 为 4.911 m，任一电机饱和比例 10.257%，
因此 `fw_metrics_pass=false`。云台版对应为空速 15.211/17.413 m/s、坡度 35.876°、抬头角
4.015°/7.047°、高度误差 2.316 m、电机饱和 8.763%，`fw_metrics_pass=true`；云台误差
yaw/roll/pitch 为 1.070°/0.422°/3.571°，满足既有 `<4°` 标准。没有证据支持修改气动模型或
放宽判定：本轮已明确的是固定版超速而非重定位根因，因此只对 4050 的推力配平做了小幅实验性
下调，不能用放宽空速门限处理。

传感器告警方面，固定版在仿真 52.564 s 和 59.020 s 报 `High Accelerometer Bias`，在 57.516 s、
115.636 s 报 IMU 0 `TIMEOUT`；云台版在 63.280–247.896 s 间有 10 次 `TIMEOUT`。这些
`STATUSTEXT` 在协议中没有仿真时间戳，以上时间按其接收时刻与最近 `LOCAL_POSITION_NED` 对齐，
配对差小于 0.07 s。告警期间 MAVLink 位置/姿态流仍在更新，飞机持续段速度和姿态也没有随告警
停止；因此证据不支持仿真步进整体卡死，但也不足以把 IMU 超时认定为无害假象或真实物理故障。
告警未被屏蔽，根因尚未解决，需继续检查 IMU 插件实际更新间隔与 PX4 消费间隔。

本轮代码修正处理云台版过渡后的命令基准，并把 4050 的 `FW_THR_TRIM` 从 0.35 降至 0.32，
以响应固定版超速证据；新增执行器重定位回归测试。logger 内部和判定门限均未改动。相关 pytest
通过后仍需重跑固定版与云台版，确认固定版空速 p95 及两版重定位，并按
间歇故障要求连续两次完整通过。

## 第二十四轮完整 MAVLink 量化与 ARM 时序修正

2026-11-05 第 24 轮固定版完成了全流程，但仍因严格的 FW 重定位失败；云台版没有进入飞行判定，
在起飞等待阶段超时。固定版 `mavlink.jsonl` 有 124,698 条记录，PX4 boot time 从 5.012 s 到
349.200 s；云台版有 66,869 条记录，boot time 从 4.424 s 到 188.484 s。两容器清理前均仍在运行，
`ExitCode=0`、`OOMKilled=false`，Gazebo 时钟也在推进，因此本轮不是 logger 崩溃或 PX4 挂死。

固定版的正式 FW 持续段为 120.024 s，轨迹角 2808.75°；MAVLink 计算的空速 p5/p95 为
15.076/17.358 m/s，四元数绝对坡度 p95 为 35.841°，抬头角 p5/p95 为 2.610/6.738°，
高度误差 p95 为 3.340 m，任一路电机饱和比例 8.757%，四路输出饱和比例 2.316%，均通过既有门限。
起飞、前转换、返航、后转换、着陆、帧对齐和相机自遮挡也通过。唯一失败项是
`fw_reposition_distance_m=80.419 m`，门限仍为 8 m；飞机在 PX4 固定翼引导点周围以约 80 m
半径盘旋，检查器没有观察到 8 m 内的实际到达。该数值不能通过放宽判定解决，后续应继续修正
`fw_executor` 的命令状态与固定翼航点完成语义，使记录的命令终点来自转换后的实时位置，而不是
执行器缓存的位置。

云台版在 309007.77–309019.06 单调时刻短暂报告 `error_flags=16384`（`xy_position_control`），
随后 `SYS_STATUS` 已恢复为 0 并记录 `prearm_health_ok`。但原路径随后直接发送了一次 ARM，PX4
返回 `COMMAND_ACK(command=400,result=1)`，并以“起飞状态超时”结束；没有 ARM 重试，所以没有相机帧、
转换或 FW 样本。现已让固定翼/VTOL 采集路径调用已有的有界 ARM/心跳确认 helper，在确认 armed 心跳
后再发送 `NAV_VTOL_TAKEOFF`；执行器收到该确认后不再重复发送裸 ARM。这个修正保留 PX4 的预检，
没有屏蔽 `xy_position_control` 或改变任何飞行门限。

两版的 `STATUSTEXT` 仍反复出现 `Accel #0 fail: TIMEOUT!`，并伴随 `High Accelerometer Bias`；
云台版还出现 `vertical velocity unstable`。这些告警期间 `LOCAL_POSITION_NED`、
`ATTITUDE_QUATERNION`、`SERVO_OUTPUT_RAW` 持续更新，固定版完成 120 s FW 段，且 `SYS_STATUS` 的
传感器 present 与 health 位一致。当前证据更符合 Gazebo/PX4 在低实时因子下的传感器消费超时告警，
而不是传感器数据永久中断；告警没有被屏蔽，仍需在后续轮次用 IMU 插件更新时间与 PX4 消费时间进一步
验证。没有据此修改 IMU 模型或放宽预检。

本轮改动为固定翼采集器的 ARM 确认时序、执行器避免重复 ARM，以及对应回归测试和本节证据记录；没有
修改气动参数、机架几何或判定标准。补丁后的两变体尚未重新启动 SITL，不能把本轮结果记为通过。

## 过速陷阱离线分析与确定性门禁（2026-11-06）

本节使用仓库中已有的 `.tmp/smoke-{fixed,gimbal}-{22..28}/episodes/*/mavlink.jsonl`，只取每个
`delta_smoke_fw_sustain_start` 到 `delta_smoke_fw_sustain_end` 之间的 `VFR_HUD`、
`SERVO_OUTPUT_RAW` 和 `EXTENDED_SYS_STATE`。舵机输出按 `u_i=max(0,min(1100,M_i))/1100`，
推力代理按 `u_i²`；它用于比较分配趋势，不把 PWM 当作实测推力。

`CA_ROTOR` 和 `SIM_GZ_EC_FUNC` 的实际映射如下（PX4 FRD 坐标，`PX` 是前后轴，`PY` 是左右轴）：

| 电机 | `PX,PY,PZ` (m) | `KM` | 位置/控制含义 |
| --- | --- | --- | --- |
| M1 | `+0.141,+0.600,-0.041` | `+0.025` | 前、左；CCW |
| M2 | `-0.159,-0.600,-0.041` | `+0.025` | 后、右；CCW |
| M3 | `+0.141,-0.600,-0.041` | `-0.025` | 前、右；CW |
| M4 | `-0.159,+0.600,-0.041` | `-0.025` | 后、左；CW |

因此俯仰力矩代理是 `[(M1²+M3²)-(M2²+M4²)]/2`，而 `M1/M2` 对 `M3/M4` 是偏航的
反扭矩分组。坏状态中触底的是前排 M1/M3；把上面的两组误称为前后会得到错误的控制解释。

各轮 FW 持续段的空速 p5/p50/p95（m/s）为：

| 样本 | p5 / p50 / p95 |
| --- | --- |
| fixed-22 | 15.28 / 15.71 / 17.78 |
| fixed-23 | 15.87 / 16.20 / 21.27 |
| fixed-24 | 15.05 / 15.49 / 17.34 |
| fixed-25 | 15.31 / 15.54 / 17.08 |
| fixed-26 | 15.66 / 15.87 / 16.46 |
| fixed-27 | 16.64 / 21.57 / 23.84 |
| fixed-28 | 15.44 / 15.63 / 16.33 |
| gimbal-22 | 15.31 / 15.59 / 17.90 |
| gimbal-23 | 15.21 / 15.63 / 17.41 |
| gimbal-25 | 16.44 / 22.03 / 23.86 |
| gimbal-26 | 15.62 / 15.78 / 16.46 |
| gimbal-27 | 16.73 / 22.22 / 23.99 |
| gimbal-28 | 16.36 / 21.65 / 23.92 |

合并分箱得到的前排触底比例（M1、M3）和俯仰差动代理如下：

| 空速箱 (m/s) | 样本数 | M1 触底 | M3 触底 | 俯仰差动代理 |
| ---: | ---: | ---: | ---: | ---: |
| 15–16 | 41,938 | 0.00 | 0.01 | −0.028 |
| 16–17 | 8,262 | 0.01 | 0.09 | −0.042 |
| 17–18 | 4,502 | 0.03 | 0.22 | −0.069 |
| 18–19 | 2,967 | 0.15 | 0.25 | −0.098 |
| 19–20 | 1,226 | 0.53 | 0.46 | −0.144 |
| 20–21 | 1,886 | 0.51 | 0.49 | −0.172 |
| 21–22 | 2,443 | 0.52 | 0.48 | −0.204 |
| 22–23 | 3,893 | 0.38 | 0.62 | −0.243 |
| 23–24 | 7,094 | 0.70 | 0.30 | −0.274 |

18–19 m/s 是触底开始明显的转折带，19 m/s 以上通常至少一个前排电机约半数时间触底；
坏样本的速度中位数约 21.6–22.2 m/s，p95 约 23.8–24.0 m/s。好样本也会短暂进入 19–21 m/s
而后回落，所以这是概率性分叉，不是一个可以靠单次样本证明的固定阈值。
按设计级门禁，当前无舵面差动权限的保守空速上限取 **18 m/s**；19 m/s 以上应视为进入
俯仰余量耗尽区，不能靠降低 `FW_THR_TRIM` 把它重新定义为安全巡航速度。

控制假设与数据相符，但证据边界要写清楚。速度升高时，前排差动代理绝对值单调增大并伴随
M1/M3 触底；坏状态同时出现较低 `VFR_HUD.throttle`、较高空速和较高电机均值。这支持“俯仰
差动余量耗尽后，分配器为满足力矩约束抬高总推力，TECS 仍把油门压低但减速失败”的闭环解释。
MAVLink 没有控制分配器内部的未饱和力矩请求，因此“抬高总推力”的因果链不能仅靠这些日志完全
证明；能确定的是触底发生在分叉前后并持续存在。

离线气动核查已改为按 Gazebo `LiftDrag` 的局部投影、扫掠修正和 `r×F` 计算，而不是把全局迎角
直接套给每块外翼。现模型在 15 m/s、报告迎角 7.4315° 时翼面升力约 28.72 N、俯仰矩约 0，
`dMy/dalpha` 对应的有效力臂约 21.8 mm，和设计报告的 NP 666 mm、CG 646 mm（20 mm，约 3.5% MAC）
一致。没有移动 CP、修改质量或改变升力斜率；修正的是反弯代理的 `a0`（中央约 1.0648°、外翼
约 −2.2742°）及对应分析器。模型按设计数据仍显示：高速时俯仰权限会随 `q` 要求增加，约 18–19 m/s
开始进入低端余量区；这属于构型权限发现，不能用仿真参数掩盖。

严格冒烟现在在 120 s FW 持续段和圆环判定之后执行确定性过速步骤：运行时把 `FW_THR_MIN` 临时
设为 0.45，最多 60 sim s，必须实测达到至少 21 m/s；随后恢复原值，要求 40 sim s 内降到 ≤19 m/s
并连续保持至少 15 sim s，期间保持 FW 且高度误差 p95 ≤10 m。注入达不到 21 m/s 单独记为
`injection_failed_to_reach_21_mps`，不能算恢复通过。过速窗口的电机饱和继续纳入统计，并沿用既有
15% 门限；输出 `fw_overspeed_peak_mps`、`fw_overspeed_recovery_s`、
`fw_overspeed_recovery_success` 及饱和比例字段。对应单元测试覆盖恢复成功、恢复超时、注入未达标和
饱和超限四种情况。

4050 参数现为 `FW_THR_TRIM=0.32`、`FW_T_CLMB_MAX=6`、不设置 `FW_T_SPDWEIGHT`、
`VT_F_TRANS_THR=0.25`、`VT_ARSP_TRANS=15`、`FW_AIRSPD_MAX=18`，并只改动横向导航/滚转响应组：
`NPFG_PERIOD=20`、`NPFG_ROLL_TC=1.0`、`FW_R_TC=1.0`、`FW_PN_R_SLEW_MAX=20`。
第 34 轮 MAVLink 证明 `FW_P_LIM_MIN=1`° 没有改变实际机头角，现已移除；本轮不改变空速、饱和、
抓圈、俯仰或高度门限，也没有启动 SITL。

## 2026-11-06 第 30 轮第二轮调参依据

第 30 轮两变体都完成完整流程，抓圈判定分别为固定版驻留 144.86 s、扫角 1561.37°、环误差
p95 2.40 m，云台版驻留 76.43 s、扫角 813.87°、环误差 p95 4.03 m；失败只剩 FW 严格指标和
确定性过速注入。固定版持续段前 15 s 高度误差 p95 14.3 m、任意电机饱和 94%，随后 90 s 稳定在
约 15 m/s；末 15 s 又升到 19.5--20.8 m/s，饱和比例 100%，全段高度误差 p95 11.79 m、饱和
20.64%。云台版前 25 s 高度误差 p95 36.2--11.6 m，35--55 s 速度 p95 21.6--19.7 m/s 且
饱和 100%，末 15 s 再次达到 20.6 m/s 和 100% 饱和，全段高度误差 p95 34.00 m、饱和 45.59%。
这不是抓圈半径或判定门限问题，而是两版都在 FW 能量/俯仰差动闭环进入双稳态；云台版更高的早期
高度误差与 `FW_T_CLMB_MAX=6`（档案能力为 3 m/s）一致。

固定版和云台版的 `FW_THR_MIN=0.45` 注入峰值分别只有 21.80 和 20.86 m/s，均未达到严格的
22 m/s 注入门槛。为同时保留确定性恢复实验所需的高速余量和正常巡航的 15 m/s 配平，本轮将
`FW_THR_TRIM` 从 0.32 调到 0.25，将 `FW_T_CLMB_MAX` 从 6 调到档案值 3，并将未实测的高速桨
阻力代理从每面 `cda=0.216`（等效 ΔCD0=0.108）下调到每面 `cda=0.19`（等效 ΔCD0=0.095）。
最后一项只改变模型代理，不改变任何判定阈值；约 12% 的下调由注入峰值距 22 m/s 的缺口约束，
后续必须用两变体完整飞行验证，不能凭离线结果记为通过。

## 2026-11-06 第 29 轮首轮调参结果

外部循环产物 `.tmp/smoke-fixed-29/episodes/episode-0000-d5452697/mavlink.jsonl` 有 14,034 行，
PX4 时间到 43.556 s，只有 5 条 FW 状态样本；`episode.json` 记录起飞后 15 s 内没有收到新相机帧，
步骤文件为空，因此这次没有可用的 FW 持续段或过速恢复数据。Gazebo 日志显示固定相机和深度相机
话题已发布，GStreamer 也启动并向 `127.0.0.1:5600` 推流；这证明渲染端完成初始化，不证明采集端
收到帧。采集器现会在同一超时错误中保存接收帧数、丢帧数、最近帧仿真时间、接收线程状态和解析错误，
用来区分渲染端无帧与采集端收流/解码故障；等待时限仍为 15 s。

云台版 MAVLink 有 79,825 行，覆盖到 204.324 s。FW 连续段 120.04 s，空速 p5/p50/p95 为
14.10/15.03/15.83 m/s，四元数坡度绝对值 p95 为 35.16°（门限 45°），抬头角 p5/p95 为
2.38°/9.09°。抓圈通过：圆环误差 p95 2.44 m、合格驻留 35.43 s、扫角 394.25°。不过全段高度
误差 p95 为 14.89 m（门限 10 m），至少一路电机触底/触顶比例为 15.35%（门限 15%）；四路输出
饱和比例为 3.94%。分段显示失败集中在盘旋命令后的前 20 s：高度 p50/p95 到 50.71/59.47 m，
目标高度 40.72 m，电机饱和 847/970（87.3%）。其中 88–90 s、90–92 s 的空速中位数分别为
20.55、19.02 m/s，VFR 油门均为 0%；随后约 100 s 空速回到约 15 m/s，高度误差 p95 降到 3.8 m，
饱和约 1.3%。这些初始段仍保留在门禁统计中，没有缩短统计窗或改门限。云台回读 yaw/roll/pitch 误差为
0.075°/0.464°/3.918°，严格小于 4°；两帧时间戳对齐。

云台版在写入圆环判定事件时，采集器直接调用了未定义的 `_jsonl`，于仿真约 204.3 s 抛出
`NameError`。因此过速注入、返回 MC、RTL 和着陆都没有执行；`overspeed_step_missing` 代表步骤
未运行，不是过速恢复失败。已将环判定文件读取改为显式调用冒烟模块的 JSONL 读取器，并添加从
真实 JSONL 文件计算稳定圆环的回归测试。固定版在约 7.296 s 出现 `Accel #0 fail: TIMEOUT!`；
整段记录实时因子约 0.20。告警前后 6–9 s 仿真时间内有 148 条四元数姿态和 146 条位置样本，
横滚绝对值最大 0.131°、俯仰绝对值最大 0.084°，没有对应的姿态中断；起飞前健康检查也通过。
这更符合启动阶段的短暂消费超时，而非持续传感器失效，但 MAVLink 没有原始 IMU 样本，无法证明
它只是调度告警；没有屏蔽该告警或修改传感器模型。

本轮没有据这一次飞行改动气动或机架参数。步骤记录显示重定位已完成 80 m 圆环捕获，紧接的严格
`hold` 又发送了一次 `orbit`；高度/电机异常正好从该 hold 开始，重复指令重置已捕获轨道是当前
最有证据的控制层原因。严格冒烟现改为在遥测确认圆环后保留现有 loiter，hold 仍等待完整 120 s，
继续把整段数据纳入原有门禁。logger 默认仍为 `off`，线程名为 logger 的内核段错误分类已有对应
回归测试。本次未启动 SITL；须用修复后的采集器重新跑两变体，确认盘旋捕获瞬态、过速恢复步骤、
返航、后转换和着陆，不能将本轮记为通过。

## 2026-11-06 第 31 轮：俯仰下限单变量实验

第 31 轮两份 MAVLink 日志均完整覆盖了 120 s FW、返航和着陆。固定版过速注入峰值 21.267 m/s，
云台版 21.301 m/s；旧的 22 m/s 门槛在 40 sim s 内均未达到，所以恢复步骤没有运行，失败原因是
注入不可达而不是恢复动力学失败。将门槛按协调者决定改为 ≥21 m/s、注入上限 60 sim s，恢复判定仍
保持 40 s 内降至 ≤19 m/s 并稳定 15 s；这没有放宽恢复标准，只使实验能覆盖日志已经达到的过速陷阱区。

按 `CA_ROTORn_PX` 核对，M1/M3（rotor 0/2）是 PX=+0.141 m 的前排，M2/M4（rotor 1/3）是
PX=-0.159 m 的后排；M1/M2 与 M3/M4 的另一种配对来自旋向反扭矩，不能当作俯仰组。结合第 22–31
轮 MAVLink，空速超过约 18–19 m/s 后前后排差动需求进入低端余量区，M1/M3 触底并维持高空速；
这支持“俯仰权限耗尽导致过速双稳态”的根因。模型的桨阻力代理已恢复每面 `cda=0.216`（等效
ΔCD0=0.108），不再为达到注入峰值改动物理。本轮唯一新增控制参数是 `FW_P_LIM_MIN=1`°，预期
阻止控制器把机头压到低迎角；是否改善过速恢复必须由下一轮外部飞行验证。

## 2026-11-06 第 34 轮复核与第 35 轮横向导航实验

比较第 26、27、32、33、34 轮的 `.tmp/smoke-*/episodes/*/mavlink.jsonl`，各份都记录了
`ATTITUDE_QUATERNION`、`VFR_HUD`、`SERVO_OUTPUT_RAW` 和 FW 段起止标记。第 34 轮两版均完成
起飞、120 s FW、过速注入/恢复、RTL 与着陆，logger 为 `off`，没有 ULog。

第 34 轮固定版持续段空速 p5/p95 为 15.40/17.61 m/s、bank 绝对值 p95 为 21.09°、高度误差
p95 为 2.93 m、任一电机饱和比例为 6.24%；云台版分别为 15.32/15.75 m/s、18.91°、2.03 m、
0.20%。两版过速注入峰值为 21.40/21.21 m/s，恢复用时 17.04/16.95 s，均通过该恢复步骤；
但过速窗口的电机饱和比例 24.84%/18.98%，所以 `fw_overspeed_saturation_success` 仍失败。
持续段 bank 标准差为 3.01°/0.69°，且均无从 >+20° 到 <-20° 的翻转。固定/云台回读 pitch 误差为
`null`/3.486°，固定版没有云台门限，云台版严格小于 4°。

第 26 轮好状态的 FW 段 bank 稳定在约 +35.8°（标准差约 0.3°，无符号翻转）；第 32 轮云台版
稳定在约 +16.5°（标准差约 0.5°，无翻转）。相反，第 27 轮固定/云台版分别有 5 次
`>+20°` 到 `<-20°` 的单向翻转（另有 6 次反向翻转），第 33 轮两版分别有 6 次单向翻转
（另有 6 次反向翻转）。bank 在 ±36° 间摆动，翻转周期约 18--20 s，和轨迹不成圆相符。

时序不支持“过速先导致翻滚”：第 27 轮固定版 FW 段起点空速已为 20.92 m/s，约 3.6 s 后才首次
跨过 +20°/-20°；第 33 轮固定版起点已为 22.99 m/s，11.1 s 后才翻滚。第 34 轮固定版在 bank
保持正值时约 29.9 s 才首次超过 19 m/s，云台版全段未超过 19 m/s。转换结束时坏状态已经偏离
目标圆心（第 27 轮固定版约 49.9 m、空速 20.3 m/s；第 33 轮固定版约 144.8 m、空速 23.6 m/s），
导航滚转指令随后反复换向，差动输出触底比例随之升高。现有证据强烈支持横向导航响应超过这架仅约
20°/s 实测滚转能力，先出现翻滚和差动饱和，再进入过速状态；但 MAVLink 没有导航滚转目标与分配器
内部未饱和力矩请求，因果链仍须通过本轮参数实验验证，也不把“总推力升高”写成已证实事实。

因此移除无效的 `FW_P_LIM_MIN=1`°。第 35 轮唯一飞行参数实验把 `NPFG_PERIOD` 从默认 10 s 调到 20 s，`NPFG_ROLL_TC`/`FW_R_TC`
调到 1.0 s，并把 `FW_PN_R_SLEW_MAX` 限为 20°/s；`NAV_LOITER_RAD`、`FW_R_LIM`、气动和所有
判定门限保持不变。预期是把导航指令变化率限制在机体可实现范围，减少 bank 翻转和差动触底，进而
消除高速双稳态。新增诊断字段 `fw_bank_sign_flips` 和 `fw_bank_sd_deg` 只记录证据，不参与通过判定。

## 第 35 轮后的手动修正

这一轮的修改由 Claude 直接完成，因为当时 Codex 中转账户余额不足。

- **云台相机位置**：`camera_link` 之前没有 pose，落在模型原点，比 C11 挂点 (0.046, 0, 0.388) 偏约 0.4 m，
  俯仰轴也跟着偏，相机在机身内部时外壳背面被剔除，因此第 35 轮及之前云台版的 `self_occlusion_fraction=0.0`
  不能作为依据。现在 `camera_link` 与云台链同原点，需要飞行重新验证云台误差和遮挡。
- **过速饱和判定窗口**：第 34、35 轮逐 5 s 统计显示，触底只发生在注入开始后约 10 s 内（从 21 m/s 刹回时
  M3/M4 约 190），之后为 0。`recovery_s` 约 16 s 其中 15 s 是稳定保持，速度实际约 1 s 就回到 19 m/s 以下，
  但此后约 5 s 仍在触底。`fw_overspeed_saturation_success` 改为只统计稳定保持的最后 10 s，
  整个周期的比例输出为 `fw_overspeed_transient_saturation_fraction`，只作诊断。120 s 持续段的饱和门限不变。
- 第 34 轮 Codex 总结里“翻滚先于过速”并未被数据证明：那两次的 FW 起点空速已是 20.9 和 23 m/s，
  翻转发生在之后。因果顺序仍未定论，已确认的是放慢横向导航后第 35 轮两版都没有翻转。

## 相机“断流”的进一步证据（第 37 轮后）

`sensor_stream_stall`（fixed-32、fixed-36、gimbal-37）不是 PX4 和 Gazebo 时钟不同步。episode 的 steps 显示，
拿到的帧与位姿时间完全对齐（`frame_sensor_stamp_ns` 等于 `pose_time_boot_ms`，`sim_skew_ms` 为 0），所以数据对齐没问题。
落后的是帧服务器自己：`sim_clock_stamp_ns` 来自它的回调诊断，失败时比 PX4 落后 6.8–13.9 仿真秒，很可能是
高负载（load 约 200）下回调积压，具体机制还没验证。按 0.15–0.2 倍实时，追上要一分钟以上，超过原来 15 s 的等待。

已做：三角翼严格冒烟的两条采帧路径都等 120 s，时间戳要求不变。
未解决：积压的确切原因，以及它和 `Accel #0 fail: TIMEOUT` 是否同因。

## 回多旋翼时电机全停（第 38 轮后）

此前“着陆成功”不可靠。r31 云台版、r36 云台版、r38 两版在回多旋翼过渡的整整 10 s（`VT_B_TRANS_DUR`）里，
`ATTITUDE_TARGET.thrust` 一直是 NaN，四个电机全 0，飞机机头向下从约 55 m 掉到 0–5 m。
r36 云台版贴地救回来后仍判通过；r38 云台版掉到 0 m 后穿过地面（GPS 高度到 -265 m），一直没有检测到着陆。

| 飞行 | 过渡开始前 FW 推力 | 电机全停 | 掉高 |
| r38 fixed / r38 gimbal / r36 gimbal | 0.0 | 10 s | 50 / 53 / 54 m |
| r37 fixed / r35 两版 / r34 两版 | 0.2–0.37 | 0 | ≤1 m |

相关的还有时机：出事的三次 `MAV_CMD_DO_VTOL_TRANSITION` 都在 RTL 之后约 5 s 才 ACK（TECS 此时已把推力降到 0），
没事的都在约 0.5 s 内。所以“推力为 0”和“RTL 已开始爬升”是混在一起的，还没分开验证；NaN 的确切来源也还没在这个
固件版本的代码里定位到。r31 云台版的前置推力也还没单独核对。

已做：
- `FW_THR_MIN` 从 0 改为 0.05，这是针对推力为 0 这个线索的实验，还没验证。
- 新增必过判定 `back_transition_success`：电机全停不超过 1 s，且过渡段（含结束后 5 s）掉高不超过 15 m。
  回放历史飞行：r31 云台版、r36 云台版、r38 两版判失败，其余判通过。

还要查：PX4 原生 RTL（不发 3000）是否也会这样。AI 自主飞行走的是这条路径，真机上这是致命问题。

## PX4 卡死和控制台刷屏（第 39 轮后）

fixed-39 是 `px4_hang`：PX4 时间停在 191 s，Gazebo 时钟走到 219 s，容器还活着，没有崩溃。三角翼之前的容器没用 `px4 -d`，
PX4 的 shell 从 /dev/null 读到 EOF 后不停打印 `pxh>`（单次飞行约 47 万行），卡死时主线程正在 `write()`。
主分支 263f2b95 已改成默认 `-d`。这里只移植了这个开关，整个提交和本分支冲突，后续合并时取主分支版本。
是否就是卡死的原因还没证实，靠后续飞行看。

## 云台偏航和出生朝向

此前 PX4 的云台偏航读数只在出生朝向为默认值（ENU yaw 0）时正确。camera_imu 以出生姿态为参考，所以 w0016（出生朝向 45°）读数固定偏约 44°，
云台关节本身是对的（Gazebo 关节角约 0）。云台版 AI 任务因此卡在起飞后的云台自检。

试过但失败的两个改法：`orientation_reference_frame` 设 ENU 后，读数不再依赖出生朝向，但固定差 180°（传感器 pose 带的 180° 偏航不再被抵消）；
再加 `heading_deg=180` 没有效果。这两次都已回退，模型回到通过第 40–42 轮时的状态。

这次修复和验证范围见下方 2026-11-06 结论。真机不走 Gazebo IMU，校正默认关闭。

## 2026-11-06：云台偏航和出生朝向结论

本段后续的模型符号结论已由本节末尾的“云台轴符号回归”修订：早先记录中的 roll=`-1 0 0` 和 IMU yaw=π 是基于未纳入生成器的手工模型修改，不能代表生成结果；pitch=`0 -1 0` 的方向保留并纳入生成器。

任务 A 的根因是 `GIMBAL_DEVICE_ATTITUDE_STATUS.flags & 64` 的地球系偏航。现有三个读数路径原先都按“设备偏航 - 机体偏航”得到相对偏航：collector 的 `_gimbal_attitude_angles_deg`、`sim_teacher.executor._gimbal_angles_deg`，以及 `onboard_runtime/vehicle.py` 的 `PymavlinkVehicle.ingest`。后者把结果放入 `VehicleState.gimbal_angles_deg`；动态相机运动估计器会消费这组角度。未提供校正参数时，这些路径的行为保持原样，真机默认校正为 0°。

仿真配置新增 `camera.gimbal.simulation_yaw_correction = "spawn_yaw_deg"`。采集器从 runtime 的 `vehicle.spawn_pose.yaw_rad` 计算 `-yaw_deg`，只传给仿真自检、仿真执行器和仿真观测；因此默认出生朝向（场景 ENU yaw 0°，PX4 机体读数约 90°）校正为 0°，w0016 的出生 yaw 45° 校正为 -45°，原来约 45° 的误差被消掉。仿真观测若带有 Gazebo `gimbal_joint_state` sidecar，则直接使用关节角作为相机角，避免 camera_imu 的出生姿态参考污染 AI 观测。4° 自检容差没有改变。测试覆盖了出生机体 yaw 90°（对应场景 yaw 0°）、45°，以及未配置时的旧行为。没有修改 PX4 参数、气动模型或 `orientation_reference_frame`。

任务 B 的 SDF 计算如下。`c11_pitch_joint` 的轴改为 `(0,-1,0)`；在 FLU 中，指令/关节角 `-0.785398 rad`（-45°）把相机光轴 `camera_link` 的 +X 变为 `(0.707107, 0, -0.707107)`，即机体前方下 45°。profile 的 `body_to_camera_rotation` 和 MC/FW 矩阵改为单位阵，并在 `profile_camera_mount_rotation` 中把云台 pitch 命令取反映射到 FRD；因此同一光轴在 FRD 为 `(0.707107, 0, 0.707107)`。理想悬停姿态（PX4 roll/pitch 约 0°、yaw 90°）的世界 NED 光轴为 `(0, 0.707107, 0.707107)`；理想 FW 姿态（roll/pitch/yaw 0°）为 `(0.707107, 0, 0.707107)`。两者的 down 分量都是 `0.707107`，下视 45°，不会出现悬停朝上。

用 `.tmp/smoke-gimbal-42/episodes/episode-0000-90201d69/steps.jsonl` 的每帧姿态、中心像素和 `-45°` 指令重算，6 个观测的世界 NED 光轴 down 角为 48.16–49.42°（roll/pitch 使理想 45° 略有变化）。对应 RGB 帧中地面始终占据主要区域，地平线在 `000000_before`/`000001_before` 的上方或在 `000002_before` 中斜穿上半部，和下视约 48–49° 一致。第 42 轮帧是在旧模型和旧观测字段下采集的，因此它验证的是方向趋势，不是新 SDF 的飞行回归；本轮没有启动 SITL。

符号约定已对齐：`motion_intent.py` 的相机射线使用 FRD/NED，云台 pitch 在 profile 关节映射处按 SDF FLU→FRD 轴向取反；`profile_mount_pitch_down_rad` 现在把 MC/FW 的默认云台角计入 AI `CameraModel` 的地面投影。两种模式均为下视 45°。以已知姿态代入 `camera_ray_from_profile`，MC（姿态 roll/pitch 0°、yaw 90°）得到 NED `(0, 0.707107, 0.707107)`；FW（roll/pitch/yaw 0°）得到 `(0.707107, 0, 0.707107)`，down 正值说明光线都与地面相交。观测中的 joint sidecar 保留实际关节角；当前 CameraModel 使用模式默认角而不是随每帧关节角更新，若任务改变云台角度，仍需把动态射线接入策略投影。`GroundProjector` 的静态真实相机外参仍把 `mount_pitch_down_deg > 0` 定义为向下，`onboard_runtime/vehicle.py` 的新增仿真偏航校正参数默认关闭，不改变真机路径。相机模型位置仍固定在 C11 挂点，故 self-occlusion 使用的相机中心和射线没有被移回机体原点；云台 sidecar 只替换角度读数，不绕过遮挡判定。

未确定事项：第 42 轮没有在新 SDF 上重新飞行，且其 steps 没有逐帧保存 Gazebo 关节 sidecar，所以不能声称新模型已通过完整起飞、转换、过速恢复、返航和着陆门禁；真实云台硬件的 mount calibration 仍需实机数据确认。

### 云台轴符号回归

复查 `.tmp/smoke-gimbal-imu` 和 `.tmp/ai-mission-gimbal` 的 `episode.json` 后，确认关节角是 −45.6°，而 PX4 回读俯仰约 +45.5°；关节和镜头方向正确，错误在读数符号。上一轮只改了 `simulation/gazebo/models/delta_tailsitter_gimbal_cam/model.sdf`，生成器仍输出 pitch=`0 1 0`，seg overlay 也仍是旧轴，因此重生成会丢失修复，AI 任务也没有使用到手改结果。

现在所有包装 SDF 和分割 overlay 都由 `scripts/generate_delta_tailsitter_model.py` 生成。云台使用 yaw=`0 0 -1`、roll=`1 0 0`、pitch=`0 -1 0`，`camera_imu` pose 为 `0 0 0 0 0 0`。依据 PX4 上游 `src/modules/simulation/gz_bridge/GZGimbal.cpp` 的姿态转换，Gazebo FLU 姿态转 FRD 时相对关节旋转按 `S⁻¹ J S` 变换，`S=Rx(π)`：Z、Y 轴符号反转，X 轴不变。因此 yaw 的 −Z 和 pitch 的 −Y 让正命令读成正值；roll 必须是 +X，−X 会让读数反号。camera_imu 与 camera_link 对齐，不应再附加 180° yaw；该偏航会共轭翻转 pitch/roll 读数。SDF 单测对 model 与 seg overlay 都计算了 `(pitch=-45°, roll=+10°, yaw=+20°)` 的组合读数符号，并验证 pitch −45° 时相机 +X 光轴在 FLU 中指向下方。

已运行生成器和相关离线 pytest；本轮未启动 SITL，也未使用 Docker，故只确认生成物与数学/结构单测，不声称飞行通过。没有改动飞行参数或气动模型。

## 2026-11-06 第 43 轮：刷新帧采集水印

第 43 轮固定版和云台版都完成完整流程。两版的 120 s FW 持续、80 m 抓圈、空速/姿态/高度/电机门禁、过速注入与恢复、RTL、回多旋翼、爬升和着陆均通过；固定版抓圈驻留 162.336 s、扫角 1852.399°，云台版驻留 157.388 s、扫角 1781.515°。云台回读误差为 yaw 0.638°、roll 0.376°、pitch 2.909°，遮挡分数仍为 0.0。两版冒烟结果仍为 fail，唯一必过项失败是 `frames_aligned`。

原始 `steps.jsonl` 显示，后段帧在高负载下到达解码器时已经落后于 PX4 当前接收进度：固定版最差 `sim_skew_ms=-3716`，云台版最差 `-804`。`frames_aligned` 的判定比较的是帧时间戳与该观测中 `mav_reader.aligned_state(frame_sensor_stamp_ns)` 的位姿时间，并保持 `abs(sim_skew_ms) <= 100 ms` 的原门限；第 43 轮保存的旧帧因此被正确拒绝。原始 MAVLink 文件仍保留帧时间附近的姿态样本，问题是采集时水印和历史覆盖策略，而不是放宽门限。

修复后，水印只在请求开始时取一次，等待期间 PX4 时间继续前进也不会移动下限；第一帧 `sensor_stamp >= watermark` 即接受，然后用这帧时间戳调用 `aligned_state`。为覆盖高负载排队时长，姿态历史从 5 s 扩到 30 s，门限不变。新增单测覆盖固定水印下接受达标帧、拒收低于请求水印的旧帧。完整 SITL 重跑仍需确认两变体的 `frames_aligned`，本轮未启动 Docker/SITL。

## 2026-11-06 第 44 轮：预解锁拒绝与相机水印停滞

第 44 轮两版均未完成整套门禁，不能用于调整气动或横向导航参数。

- 固定版 `mavlink.jsonl` 共 1,972 条；最后 Gazebo 时钟为 10.564 s，MAVLink 的
  `LOCAL_POSITION_NED`/`ATTITUDE_QUATERNION` 到 PX4 `time_boot_ms=10.860 s` 仍连续，
  位置只偏离起点约 4 cm。PX4 在 6 次 ARM 请求中每次都回 `COMMAND_ACK result=1`
  （`MAV_RESULT_TEMPORARILY_REJECTED`），没有 armed heartbeat。采集器此前只看到
  `SYS_STATUS` 位图健康（`error_flags=0`），但容器日志明确记录 `Accel #0 fail: TIMEOUT!`、
  `Preflight Fail: High Accelerometer Bias` 和反复的 `Arming denied`。这是 PX4 真实拒绝解锁，
  不能归咎于飞行控制，也不能靠跳过预检通过。本轮结果分类新增 `arming_failure`，
  仅用于把零步骤的解锁失败从空中飞行失败中分离，不改变任何通过门槛。
- 云台版完成解锁、起飞并持续输出遥测：5,209 条四元数姿态、434 条 `VFR_HUD`，最后
  PX4 时间为 114.504 s；最后空速 15.18 m/s、高度约 41.73 m，姿态和舵机输出仍有更新。
  相机服务器收到 93 帧（丢弃 92 帧），最新帧水印停在 100.552 s，失败时 Gazebo `/clock`
  为 100.784 s，而 PX4 当前位姿水印为 114.500 s，差 13.948 s。线程仍存活且无 reader 错误，
  容器运行、非 OOM。故严格帧门禁等待的目标比最新渲染帧领先 13.948 s，不能将旧帧算作对齐。
  超时诊断现在显式写出 `sensor_watermark_lag_s`，方便下一轮确认领先量是否持续扩大。
- 云台容器日志在 PX4 时间约 103.512 s 记录 IMU gyro/accel 重复样本时间戳，随后仍有新的
  四元数、位置、空速和电机 MAVLink 样本；因此这次 `Accel #0 fail: TIMEOUT!` 期间飞行状态遥测
  没有停止，但 IMU 时间戳异常是真实报告，不能仅凭姿态继续更新就屏蔽。现有证据不能确认它
  与图像渲染停滞是同一个原因，也不能区分渲染回调、GStreamer bridge 或时间源推进问题。

本轮只增加失败分类和水印差诊断，没有改机架、气动、PX4 参数或门限：固定版在解锁前失败，
云台版在水印差达到 13.948 s 后帧流停住，二者都没有覆盖完整的返航/回多旋翼/着陆指标。
后续应先用带新水印差字段的证据定位 Gazebo/PX4 时钟与 IMU 时间戳异常，再恢复两版全流程验证；
在此之前不能声称第 44 轮通过或认为此前的 FW 参数已再次验证。

第 40–42 轮的时间分别是 2026-11-06 16:45–17:22；固定/云台版均在一次 ARM 请求内继续飞行，
虽然这些轮次也出现过不同程度的 IMU timestamp error。第 43 轮在 23:10 开始，距第 42 轮约
5 小时 48 分；两版仍各自一次 ARM 成功，但在高负载下出现 6/8 次 `Accel #0 fail: TIMEOUT!`
并暴露帧积压。第 44 轮固定版在 23:22 开始，云台版在 23:29 开始；固定版预检日志先报
`ready=true`，随后 6 次 ARM ACK 全为临时拒绝，容器日志同时有 `Accel #0 fail: TIMEOUT!`、
`High Accelerometer Bias` 和重复 `Arming denied`；云台版则有 5 次加速度超时、2 次 IMU 重复时间戳，
但已成功解锁。现有日志只能证明第 44 轮固定版的拒绝与当轮 PX4/IMU 健康故障同时发生，不能证明
是重启或某个外部进程导致；没有足够证据继续归因。代码已把 ARM 临时拒绝重试上限从 6 次扩到
覆盖既有 30 s 总窗口（间隔仍为 1 s），不跳过预检、不改变判定门限；未用 SITL 重跑验证该修复。
