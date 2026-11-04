# 折叠尾座式三角翼接入说明

本机档案 ID 为 `delta_tailsitter`，Gazebo 传感器包装为
`delta_tailsitter_mono_cam`，PX4 自启动编号为 **4050**。资料来自只读的
`md-scratch/px4-upstream/delta_tailsitter_design_report.txt` 全文和镜像中的
`4018_gz_quadtailsitter`。质量采用报告的**壳体化优化方案 E**，不是待交付实体 CAD
直接打印的质量。当前使用参数化占位外观，**未经本机 SITL 飞行或实飞验证**。

## 启动链路

沿用现有链路：场景 runtime → `multidetect.simulator.stack` →
`scripts/run_simulator_docker.py` → 容器原有 `rcS` → 自启动脚本 → Gazebo。

- `PX4_SYS_AUTOSTART=4050`（rcS 中对应 `SYS_AUTOSTART`）；
  `PX4_SIM_MODEL=gz_delta_tailsitter_mono_cam`。
- 仓库 `simulation/gazebo/models` 仍只读挂载在 `/opt/multidetect-gazebo-models`，
  其路径在 `GZ_SIM_RESOURCE_PATH` 中优先于镜像模型。`PX4_GZ_MODELS` 不另起一套路径。
- 仅本机额外挂载 `simulation/px4/airframes/4050_gz_delta_tailsitter` 到
  `/opt/px4-gazebo/etc/init.d-posix/airframes/4050_gz_delta_tailsitter`，只读。
  不改镜像、不替换 4018；4050 在本仓库/已有上游参考编号中未占用。
- `delta_tailsitter` 是独立完整的动力学模型，不 include 上游 quadtailsitter。
  仅空速传感器继续引用镜像中的 `airspeed` 资源。IMU、气压计、磁力计、GNSS
  频率/名称保持上游约定，继续使用现有启动脚本中的 SIM 磁力计修补。
- RGB、深度、里程计、可选分割相机维持现有链节名/传感器名和默认话题规则。
  `delta_tailsitter_mono_cam_seg` 按现有采集器 overlay 机制只读覆盖包装。
  C11 的 100 g 已在整机预算内，所以 RGB/深度链节只保留各 1 mg 数值质量，
  没有再次 include 会增加 50 g 的通用 mono_cam。光学参数相同。
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
主体自身 CG 和惯量用平行轴定理反算；正常相机包装的显式链节合计为 2.929 kg，
合成 CG 在原点（上游空速资源的微小数值质量未再重新建模）。

| 项目 | CAD mm | Gazebo base_link m |
|---|---|---|
| C11 | `(0,258,-55)` | `(0.046,0,0.388)` |
| 电机中心 | `(±600,629,±150)` | `(-0.159 或 0.141, ±0.6, 0.017)` |
| 桨盘 | `(±600,605,±150)` | `(-0.159 或 0.141, ±0.6, 0.041)` |
| 支脚最低接触点 | `(±600,1055,±150)` | `(-0.159 或 0.141, ±0.6, -0.409)` |

相机现在安装在 C11 三轴云台上。STEP 转换器会将 `C11` 单独导出为
`meshes/c11.stl`，四个桨分别导出为 `rotor_0..3.stl`；`visual.stl` 不再包含桨，
桨网格随 `rotor_*_joint` 旋转。云台关节为 `c11_yaw_joint`、`c11_roll_joint`、
`c11_pitch_joint`，Gazebo 控制话题沿用 PX4 上游：
`command/gimbal_yaw`、`command/gimbal_roll`、`command/gimbal_pitch`。RGB 和深度
传感器挂在标准的 `camera_link` 和 `depth_camera_link`，因此现有采集器话题路径保持不变。

角度约定是云台底座坐标中的 yaw、roll、pitch（弧度话题值），相机外参按
`机体→云台底座 + 实时关节角` 计算。模型初始俯仰为 −45°；`delta_tailsitter.json`
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
  `Cm0/cma=0`，未编造反弯翼型的力矩数据。所有系数来源及推导也写在 SDF 注释中。

空速设失速/最小/巡航/最大为 10/12/15/19 m/s，过渡 13 m/s，前/后转换初值
2.5/5 s，转换超时 20 s。档案最低过渡高度 30 m，现有采集器另加 10 m 裕量。
MC/FW 增益沿用 4018 起点；`FW_THR_TRIM=0.1` 是阻力估计加差动余量的初值。

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

# 真正起飞 -> 转平飞 -> 盘旋/重定位 -> RTL -> 转 MC -> 垂直降落。
"$PY" scripts/smoke_airframe_flight.py \
  --airframe delta_tailsitter_mono_cam --takeoff-method vtol \
  --out .tmp/smoke-delta-tailsitter \
  --container-prefix md-smoke-delta --control-port 14570 --telemetry-port 14571
```

若要保留 ULog 或使用 GPU，可直接执行同一采集器的冒烟路径：

```bash
"$PY" scripts/collect_sim_teacher_episodes.py \
  --scenario configs/scenarios/airframes/delta_tailsitter_mono_cam_001.scenario.json \
  --airframe delta_tailsitter --teacher explore --smoke-flight \
  --episodes 1 --steps-per-episode 8 --keep-px4-ulog --no-gpu-render \
  --out .tmp/smoke-delta-ulog --container-prefix md-delta-ulog \
  --control-port 14570 --telemetry-port 14571 --sim-network host
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
高迎角/桨洗/失速阻力和未知 `Cm0`；4018 控制增益、差动饱和、转换时间和固定翼配平。
报告 NP 自身误差约 ±15 mm，已经接近 20 mm 的静稳定距离。没有舵面、零升力力矩
未知时，巡航所需总推力可能接近维持差动俯仰配平的下限，应优先看上下排饱和。

离线测试检查 XML、相机矩阵、质量/CG/惯量正定、四点接触、总升力斜率/静稳定裕度、
电机编号和机架几何、场景哈希、只读挂载及 STEP 转换。它们不能证明真实 Gazebo
接触求解或闭环飞行稳定；本机未装 `gz` CLI，因此没有声称执行过 `gz sdf -k`。
