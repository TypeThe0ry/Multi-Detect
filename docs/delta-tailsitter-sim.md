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
`机体→云台底座 + 实时关节角` 计算。模型初始俯仰为 −45°；`delta_tailsitter_gimbal.json`
记录 MC 默认 −45°、FW 默认 +45° 的稳定参考约定，用于 tailsitter 两种姿态相差约
90° 时保持地平线下的合理前视。没有实时关节样本的旧固定相机调用仍使用原矩阵。

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

气动使用 `gz-sim-lift-drag-system`：中央面 0.399 m²，四个外翼面各 0.052 m²，
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
  并抵消原有约 0.877 N·m 的低头力矩；`dMy/dα≈-6.759 N·m/rad` 保持为负。
  这是假设的等效气动配平，不代表已经获得真实翼型剖面力矩数据。所有系数来源及推导也写在 SDF 注释中。

空速设失速/最小/巡航/最大为 10/12/15/19 m/s，过渡门槛 15 m/s，前/后转换时长
5/10 s，前转换推力 0.25、后转换减速度 1 m/s²、MC 电机爬升斜坡 5 s，转换超时 20 s。
这些值让前转换逐步建立空速，后转换先减速再恢复悬停推力。
MC/FW 增益沿用 4018 起点；4050 当前 `FW_THR_TRIM=0.35`，用于阻力估计和无舵面差动余量。

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

# 云台版起飞后发送 DO_MOUNT_CONTROL(pitch=-45°, roll=0°, yaw=0°)，并等待
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

PX4/MAVLink 控制使用标准 gimbal v2 或兼容的 `DO_MOUNT_CONTROL`。例如 pymavlink：

```python
master.mav.command_long_send(
    target_system, target_component,
    mavutil.mavlink.MAV_CMD_DO_MOUNT_CONTROL,
    0,  # pitch_deg, roll_deg, yaw_deg, yaw lock, 0, 0, 0
    -45, 0, 0, 2, 0, 0, 0,
)
```

PX4 状态优先从 `GIMBAL_DEVICE_ATTITUDE_STATUS`（必要时同时记录
`GIMBAL_MANAGER_STATUS`）读取，采集器把原始状态放入每个 `flight_state.gimbal_status`；
Gazebo 关节话题仍可用于离线校验实际关节角。

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
