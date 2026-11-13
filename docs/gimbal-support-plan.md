# 云台相机支持审计与实施计划

本文记录当前系统对“相机姿态 = 机体姿态 + 固定俯角”的依赖，并定义把固定挂载和可控云台同时纳入仿真、感知、规划和机载执行的接口。固定挂载保持原有路径；云台配置通过 `camera_mount` 选择并由执行器主动下发角度。

## 当前审计

| 领域 | 现状与隐含假设 | 位置 |
| --- | --- | --- |
| 相机杆臂和固定安装 | x500 相机杆臂固定为机体系 `(0.18, 0, -0.12)`，光轴固定向下 `0.18 rad`；`camera_center_ned_up` 只按机体 roll/pitch/yaw 旋转杆臂。 | `src/multidetect/simulator/camera_projection.py:1-7,19-21,131-155` |
| 像素反投影/地面交会 | 光学射线先用 `camera_pitch_rad` 旋到机体，再用机体姿态旋到 NED；`pixel_ground_point` 用该射线与平面或 `GroundModel` 求交。这是机体固定相机的核心假设。 | `src/multidetect/simulator/camera_projection.py:158-226` |
| 目标框真值投影 | 目标世界点按机体姿态反变换，再由固定平移和固定俯角转到光学坐标；没有独立云台状态。 | `src/multidetect/simulator/camera_projection.py:333-462` |
| 遮挡/可见性 | `visible_fraction` 的视线起点来自固定相机中心；遮挡射线不含云台方向。 | `src/multidetect/simulator/camera_projection.py:261-330` |
| 教师 FOV | `target_in_fov` 只使用位置差、车辆 yaw 和 `camera_pitch_rad`，水平/垂直投影近似为机头前视相机；教师观察站再把同一 scalar 俯角传给 coverage 和 FOV。 | `src/multidetect/sim_teacher/policies.py:274-315,440-500,895-915,1065-1090`; `scripts/collect_sim_teacher_episodes.py:1745-1785` |
| 教师视图站位 | 观察 waypoint 记录位置和 view 名称，但没有云台角度/角速度；前视目标的可见性依赖机体转向目标。 | `scripts/collect_sim_teacher_episodes.py:1758-1786`; `src/multidetect/sim_teacher/policies.py:430-505` |
| teacher intent | `camera_pitch_rad` 是 sidecar 中的唯一相机姿态字段，并且从 aircraft convention 的负俯角转换为投影使用的正 pitch-down。 | `scripts/collect_sim_teacher_episodes.py:1745-1751`; `configs/scenarios/generated-r21id-r3/gen_r21id_r3_w0000.teacher_intent.json:1-8` |
| 录制 pose/camera_info | SIM 订阅固定 `camera_link` 的 image/camera_info，以及 `base_link` 的 NavSat 和 odometry；`CameraInfo` 只校验内参/尺寸/时间，不携带每帧云台姿态。 | `scripts/collect_sim_teacher_episodes.py:683-687`; `scripts/gazebo_sensor_stream.py:328-376,1590-1632` |
| 录制姿态对齐 | 图像使用自身仿真时间，选择不晚于图像的 odometry；MAVLink 录制 step 用 `aligned_state(sensor_stamp_ns)`，状态字段仍只有 `LOCAL_POSITION_NED`、`ATTITUDE`、`GLOBAL_POSITION_INT` 等。 | `scripts/gazebo_sensor_stream.py:181-286,656-676,795-808`; `scripts/collect_sim_teacher_episodes.py:3840-3855,4104-4125,4234-4265` |
| onboard 地面交会 | `scripts/onboard_policy.py` 的 `ground_point`/`camera_ray` 最终调用 `pixel_ground_point`/`pixel_camera_ray`；没有 gimbal freshness、状态时间戳或姿态来源选择。 | `scripts/onboard_policy.py:1149-1205` |
| onboard profile 适配 | `profile_mount_pitch_down_rad` 从静态 body-to-camera 矩阵抽取一个 pitch scalar；assembly 再把它分别注入 MC/FW 的 `camera_pitch_rad`，因此无法表示动态 yaw/roll 或世界稳定。 | `src/multidetect/autonomy/motion_intent.py:48-93`; `src/multidetect/autonomy/onboard_policy_parts/assembly.py:73-94,145-168,348-366` |
| 其他地面/回放调用点 | replay、score 和 ranging/evaluation 脚本直接传固定 `camera_pitch_rad` 或读取静态 mount pitch；这些入口会随着核心 API 接入 `CameraMount` 而需要迁移。 | `scripts/replay_onboard_episode.py:397-399`; `scripts/score_onboard_loop.py:794-830`; `scripts/validate_step_triangulation_replay.py:200-205,473-494`; `scripts/evaluate_near_depth_replay.py:87-111`; `scripts/evaluate_onboard_ranging_reuse.py:143-156` |
| 覆盖率/固定翼几何 | coverage footprint、固定翼搜索和 FW clearance 只接受单个向下俯角，默认目标视场随机体/航线几何变化，不含云台姿态。 | `src/multidetect/search_coverage.py:32-65,220-240`; `src/multidetect/autonomy/fw_search.py:59-145`; `src/multidetect/autonomy/fw_geometry.py:753-778` |
| onboard 相机运行时 | `CameraCalibration.extrinsics` 只承载静态 mount pitch/yaw/roll 和杆臂，`CameraFrame` 只带图像、采集时间和 calibration。 | `src/multidetect/onboard_runtime/camera.py:14-67` |
| runtime projection/calibration | onboard projection 读取 `mount_pitch_down_deg` 等静态 extrinsics；标定和姿态运动模块也把 pitch/yaw/roll 当作一次性安装测量。 | `src/multidetect/onboard_runtime/projection.py:20-35`; `src/multidetect/camera_calibration.py:300-312,413-479`; `src/multidetect/attitude_camera_motion.py:120-135` |
| MAVLink telemetry | 观察器白名单和解析器处理 `ATTITUDE`、`ATTITUDE_QUATERNION`、`LOCAL_POSITION_NED`、`GLOBAL_POSITION_INT`、`ODOMETRY`，没有 `GIMBAL_DEVICE_ATTITUDE_STATUS` 或 `MOUNT_ORIENTATION`。 | `src/multidetect/mavlink_sensor_observer.py:24-40,161-218` |
| Airframe profile | 旧 profile 继续使用 `camera.mount` 和固定旋转；新增可选顶层 `camera_mount`，声明 `type`、`axes`、`limits`、`rates`、`default_mode`，可按机体选择 fixed 或 gimbal。 | `src/multidetect/autonomy/airframe_profile.py` |
| 机型/仿真模型 | x500 mono camera 是 `base_link` 的 fixed joint，pose 为 `0.18 0 -0.12 0 0.18 0`；尾座模型同样是 fixed camera，FW 方向另写死在 SDF。 | `simulation/gazebo/models/x500_mono_cam/model.sdf:10-29`; `simulation/gazebo/models/quadtailsitter_mono_cam/model.sdf:3-10` |
| world 生成 | `scripts/generate_r15_worlds.py` 使用相机内参进行目标尺寸/距离估算，但不生成云台关节、角度或云台标签。 | `scripts/generate_r15_worlds.py:15-28,97-103,208-225` |
| world teacher intent 生成 | r15 生成器把每个场景的 pitch 写入 `camera_pitch_rad`，生成的 JSON 只有标量俯角，没有云台状态。 | `scripts/generate_r15_worlds.py:474-506,521-544` |

上述依赖分成两类：几何层依赖固定安装角（必须统一修正），业务层依赖“机头转向目标即可看到目标”（可以在云台状态接入后逐步替换）。本次实现没有改变业务层默认行为。

## 第一步实现

新增 `src/multidetect/camera_mount.py`：

* `CameraMount(kind="fixed" | "gimbal")` 保存 pitch/yaw/roll、机体系杆臂、角度限位、最大角速度和稳定模式。
* 稳定模式为 `follow-body`、`lock-horizon`、`follow-yaw`。云台 pitch 的正方向是光轴向 NED `+Z`（向下），与现有 `camera_pitch_rad` 一致。
* `compose_camera_pose` 明确执行 `vehicle attitude × gimbal attitude × mount offset`，输出相机中心和 optical-to-world 旋转。`lock-horizon` 在世界坐标构造目标光学姿态，再乘机体旋转的转置，因此机体俯仰 90° 时光轴仍保持水平。
* `GimbalController` 统一 SIM 和 PX4 下发；`FixedGimbalController` 是 no-op，`SimGimbalController` 转发受限 joint 命令，`Px4GimbalController` 映射 `DO_GIMBAL_MANAGER_PITCHYAW`。PX4 vehicle 解析 `GIMBAL_DEVICE_ATTITUDE_STATUS` 并把反馈放入 `VehicleState.gimbal_attitude_rad`。
* MissionExecutor 在 inspect 指向目标、survey 使用 nadir、count/search/follow 追踪可见目标；gimbal 路径返回 `gimbal_command` 并跳过不必要的机体转向。fixed 或未配置云台时，原有 vehicle-yaw/pitch 行为和动作序列保持不变。
* `src/multidetect/simulator/camera_projection.py` 的 `camera_mount` 参数是可选的。未传参时保留旧的逐步三角函数路径；固定安装调用和旧 x500 几何在测试容差内一致，目标框投影也保持相同结果。`pixel_camera_ray`、`pixel_ground_point`、`project_target_bbox` 共用同一 composed pose，避免投影和地面交会各自实现一套姿态组合。

单测位于 `tests/test_camera_mount.py`，覆盖固定安装回归、固定安装目标框结果和尾座机体俯仰 90° 的锁地平线射线/地面交会。

## 运行时数据设计

### 实机 MAVLink

1. 首选 `GIMBAL_DEVICE_ATTITUDE_STATUS`：记录系统/组件、boot 时间、四元数、角速度、`flags` 和 `failure_flags`。四元数要转换到项目约定的 optical-to-body 或 optical-to-world 旋转，并保留原始消息用于审计。
2. 兼容只提供云台管理器的设备时读取 `MOUNT_ORIENTATION`，其 pitch/roll/yaw 为角度单位的遥测字段；解析器必须记录来源和时间，不能把它当作机体 `ATTITUDE`。
3. 相机帧必须选择不晚于曝光时间的最近云台状态，并设置独立的最大年龄和姿态跳变检查。缺少新鲜云台状态时，地面交会应 fail-closed，而不是回退为机体固定相机。
4. 机体姿态仍来自 `ATTITUDE_QUATERNION`/`ODOMETRY`，位置来自 `LOCAL_POSITION_NED` 或对齐的 odometry；相机状态与机体状态在同一 observer clock 上对齐。

### Gazebo / SIM

* 2/3 轴模型应以可观测 joint state 发布每个云台轴角和角速度，并把 joint state 时间纳入 image、CameraInfo、odometry 的 causal freshness gate。推荐在模型中使用 PX4 Gazebo 的 gimbal 变体（例如 `x500_gimbal`）作为基线；仓库自己的 custom airframe 需要显式加入 yaw/pitch/roll joint、控制插件和相机 link。
* 传感器主题继续使用 camera image/CameraInfo，但 frame id 必须指向云台相机 link；关节状态单独录制，不能伪装成旧 `attitude` 字段。
* 自定义机体还应补齐 propeller rotation visual/joint，使进入前飞或云台转动时视觉姿态与动力学姿态一致；这属于仿真资产工作，不在本次纯 Python 变更内。

## 规划器与教师命令

规划器/教师的内部动作应携带 `gimbal_pitch_rad`、`gimbal_yaw_rad`、`gimbal_roll_rad`、目标时间和稳定模式。实机执行器映射为 MAVLink `COMMAND_LONG` 的 `MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW`：pitch、yaw、pitch rate、yaw rate、控制 flags 和 gimbal device id 必须由执行器按设备能力/限位裁剪，并等待 command ACK 和后续 attitude status 收敛。

教师的 observation view 应从“站位 + 机体 yaw”扩展为“站位 + 相机世界姿态/云台目标”；`target_in_fov`、coverage footprint 和 standoff 统一调用 composed pose。飞行器转场时，tailsitter 应在 MC/FW 机体 profile 与云台稳定模式之间明确优先级，不能继续把 FW 机体姿态直接当作相机姿态。

## 已实现与后续步骤

1. 扩展 airframe/camera profile（不改变现有必填字段）：加入 mount kind、限位、速率、稳定模式和杆臂；把现有 tailsitter `attitude_rotations` 迁移为固定 mount 或 mode-specific body transform。
2. 已完成控制器、PX4 command sender、`GIMBAL_DEVICE_ATTITUDE_STATUS` 快照解析和 MissionExecutor 技能接入；后续可把 raw gimbal telemetry 纳入更严格的图像 freshness gate。
3. 扩展 Gazebo x500/尾座资产：引入 2/3 轴 gimbal joint、joint-state topic、控制插件、相机 frame 和 propeller rotation visual；更新 world/collector 的 topic 绑定。
4. 让 `target_in_fov`、教师 view stations、coverage、目标框真值投影和 onboard triangulation 接受 `CameraMount`/composed pose；加入转场中相机姿态的回放测试。
5. 规划器/教师仍可进一步把角度作为显式训练 intent；当前 executor 已产生统一 gimbal command，PX4 sender 已实现限位、锁定 flags 和角度下发，ACK/状态收敛策略可在实机联调阶段补充。
6. 设计并版本化新的 recorded-data sidecar（例如 `camera_mount_state`：timestamp、source、quaternion、Euler 展开、rates、flags、freshness）；完成旧数据读取兼容后再更新 schemas、生成器和证据文件。
7. 最后才更新 `camera_info`/pose 语义文档、回归闸门和实机/SITL 验证。当前任务明确不修改这些数据格式，也不运行 SITL、Docker 或 GPU。

## 第三步实现（teacher FOV 与录制相机姿态）

本步完成 teacher 与录制/标注链路的最小接入，保持旧数据和固定安装几何不变：

* `target_in_fov` 和 expert observation view 支持可选 `CameraMount`。固定安装继续使用原有逐步投影；gimbal 安装根据目标生成云台 pitch/yaw，并用 composed optical-to-world pose 做 FOV 判断。gimbal 观察站不再仅因目标方位转动车体，标签中附带可选 `gimbal_command`。
* recorded step 的 `obs_before`/`obs_after` 在有 `GIMBAL_DEVICE_ATTITUDE_STATUS` 时附加 `gimbal_state`；若调用方提供 mount，则同时附加 `camera_pose`（中心、optical-to-world 矩阵、来源和云台时间）。字段均为可选，旧 reader 忽略即可。
* `camera_projection`、SIM detector label 和 replay/autolabel 路径优先使用 observation 中的 `camera_pose`；缺少该字段时继续走原固定相机路径。没有修改 schema、版本或证据文件。

