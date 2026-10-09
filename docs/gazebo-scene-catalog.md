# Gazebo 场景资源清单

这份清单用于选择后续图像、深度、目标检测和自主任务策略的模拟场景。外部资源在进入仓库前必须固定版本/提交、记录许可证、做资源扫描并绑定 scenario manifest；不直接把运行时在线下载当作发布依赖。

## 当前仓库可复现的场景

### 3-D 主线资产集

`configs/scenarios/gazebo-3d-mainline-v1.manifest.json` 是当前 Gazebo 自动飞行主线的
资产入口。它只收录仓库拥有的 SDF world、实体 vehicle composition 和 pinned PX4
model provider，并在每个 world 中绑定同一个可复用的实体目标模型
`multidetect_search_target_v1`；明确排除 `billboard` 感知夹具。主线包含：

- `urban_inspection_multirotor_001` + `x500_mono_cam`：多旋翼动力学、RGB、Depth、PX4 telemetry；
- `river_rescue_vtol_001` + `standard_vtol_mono_cam`：VTOL 动力学、RGB、Depth、PX4 telemetry；
- `coastal_patrol_fixed_wing_001` + `rc_cessna_mono_cam`：固定翼动力学、RGB、Depth、PX4 telemetry/airspeed。

实体目标模型由底盘、座舱、轮组、传感器桅杆和传感器头组成，带有碰撞几何和多个遮挡/深度层次。
它使用中性材质，不用红/绿颜色或对象名称表达任务答案；离线标注可以使用场景资产，运行时
OnboardAI 只能读取相机 RGB、米制 Depth 和 PX4/GPS telemetry。三类场景分别放置一份目标，
因此采集时可以改变航向、距离、光照、风和目标可见程度，而不是重复同一张图片。

Manifest 会固定每个 scenario/runtime/world/model/config 的字节数和 SHA-256，并要求运行时
输入不包含目标真值。`tests/test_gazebo_3d_mainline_manifest.py` 只验证资产绑定、3-D SDF
结构和 fail-closed 边界，不把资产审计写成已经完成的外部 Gazebo 飞行。

| 场景 | 当前用途 | 传感器 | 状态 |
|---|---|---|---|
| `river_rescue_vtol_001` | VTOL 任务、RGB+Depth、GPS、目标策略 | RGB + `FLOAT32_M` Depth | 已通过 external sensor SIM |
| `urban_inspection_multirotor_001` | 多旋翼城市/地面纹理与深度路径 | RGB + Depth | 仓库资产，待独立 external run |
| `coastal_patrol_fixed_wing_001` | 固定翼巡逻与策略/任务路径 | RGB + Depth | 仓库资产，待独立 image campaign |

`gazebo_car_positive_control_001`、`gazebo_red_car_positive_control_001` 和
`gazebo_red_green_search_001` 不在上面的 3-D 主线表中。它们是旧感知适配器/回归夹具，不能
作为 OnboardAI 训练集、Gazebo 自动飞行主线或最终验收证据。当前真实 urban 3-D 采集的首批
3 帧已经用同一个 COCO ONNX 做过 detector smoke，但目标没有进入视场，结果明确记录为
[`sensor-only detector smoke`](evidence/onboard-rendered-3d-detector-smoke-20261007.json)，
不是训练完成。

## 官方外部候选

PX4 官方维护的 `PX4-gazebo-models` 仓库集中保存 PX4 支持的 models/worlds，并提供 `simulation-gazebo` 下载与启动脚本：[PX4/PX4-gazebo-models](https://github.com/PX4/PX4-gazebo-models) 和 [PX4 Gazebo models 文档](https://github.com/PX4/PX4-Autopilot/blob/main/docs/en/sim_gazebo_gz/gazebo_models.md)。官方 PX4 文档列出的可直接试验世界包括：

- `baylands`：水域/海岸巡逻，适合 GPS、地面/水面目标和远距离视觉；
- `lawn`：草地与低纹理，适合低对比度目标和光流鲁棒性；
- `walls`：障碍物与避障；
- `windy`：风场下的轨迹与策略稳定性；
- `aruco`：视觉标记与精确降落控制。

来源：[PX4 Gazebo worlds 官方列表](https://github.com/PX4/PX4-user_guide/blob/main/en/sim_gazebo_gz/index.md)。

Gazebo 官方文档说明，Gazebo Fuel 提供大量可复用模型，可以通过 Fuel URI 临时引用，也可以下载到本地并通过 `GZ_SIM_RESOURCE_PATH` 固定资源路径：[Gazebo SDF worlds 官方教程](https://github.com/gazebosim/docs/blob/master/garden/sdf_worlds.md)。

## 引入标准

后续优先顺序：`baylands` → `lawn` → `walls` → `windy` → `aruco`。每个新场景都要满足：

1. 有固定的 SDF/world、模型资源、提交或压缩包 SHA-256 和许可证记录；
2. 有 camera/depth/GPS/odometry topic binding，能生成同一 wire-v3 frame manifest；
3. 图像算法只读 RGB/Depth/telemetry，不读取 Gazebo truth；
4. 目标检测、距离估计和任务策略继续走现有核心接口；
5. 场景级证据明确区分 machine transport、detector smoke、QGC 人工视觉、HIL 和 REAL。

当前推荐先扩展 `baylands`，但在 pinned 容器中完成下载/许可证/资源哈希和 camera topic 适配前，不把它计入 release parity 或最终 `0.3.0` 验收。
