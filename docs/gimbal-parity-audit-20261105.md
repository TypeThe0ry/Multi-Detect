# 云台与无云台一致性审计（2026-11-05）

本审计覆盖 `src/`、`scripts/`、`configs/`，先阅读了 `docs/gimbal-support-plan.md` 和
`docs/gimbal-sitl-run.md`。目标是确认普通固定相机和 `x500_gimbal` 都能走同一套能力解析、
仿真启动、相机姿态和数据记录边界。

## 本次已修复

* `scripts/run_sim_session.py`
  * 为交互式会话注册 `x500_gimbal` 的默认 world 和 scenario。
  * 构建 Docker 计划时传递规范化的 `airframe_profile_id`。此前直接调用交互式会话时，
    `x500_gimbal` 可能沿用普通 x500 的 PX4 autostart/model 环境；现在会保留 `4019` 和
    `gz_x500_gimbal`。
* `scripts/collect_sim_teacher_episodes.py`
  * `_validate_runtime()` 使用 `airframe_profile_category()` 判定请求 airframe 的类别，
    保留对只有运行时模型名、没有仓库 profile 的输入的字符串回退。
* `src/multidetect/simulator/scenario_runner.py`
  * `_infer_mode()` 优先使用 `airframe_profile_category()`，因此显式 gimbal profile 不再
    依赖 `x500` 字符串命中；未知 profile 仍按运行时模型名回退。
* `tests/test_sim_session.py`
  * 普通 x500 与 x500 gimbal 共用 airframe/scenario 选择测试，并增加 gimbal PX4 环境断言。

## 已关闭的审计项

1. **x500 专用碰撞/接触合同（2026-11-05 已关闭）**
   * `sim_airframe_envelope.py`、`sim_navigation_reference.py` 和
     `sim_ground_contact_geometry.py` 都接受独立 `gimbal_sources` 闭包。新解析器要求提取清单、
     六个原始资产同目录绑定，并逐文件核对 pinned image、image path 和 SHA-256；不接受固定
     x500 与 gimbal 输入混用。原 x500 三模型闭包和固定相机结果路径保持不变。
   * Envelope 与 contact 消费者从真实组合 SDF 展开四 rotor/base 的九个碰撞体和
     `camera_link/cgo3_camera_collision` 球体，并将生成 SDF 的十个碰撞定义逐项对比。关节轴、
     child-relative SDF joint pose 和原始限位用于扫过连续范围；1 度采样以关节杠杆臂上界外扩，
     不用固定相机几何代替。Navigation reference 也重算同一源闭包，并记录关节限位、碰撞数和
     范围包络。
   * 数值结果：gimbal 单独全姿态半径上界为 `0.187232 m`；x500 原包络为 `0.390646 m`，所以
     它没有扩大整体包络（水平半径、顶部和底部扩展均为 `0 m`）。指定停机坪场景中，关节范围
     最坏 gimbal 球底点为 `0.061624 m`，停机坪顶面为 `0.080 m`，新增潜在支撑几何重叠最多
     `0.018376 m`。最低总碰撞点仍是原 x500 机体/旋翼的 `0.016 m`，故 gimbal 不改变最低点，
     但增加了一个可能接触停机坪的碰撞体；这只是几何可能性，不代表 Gazebo 接触力或求解器已验证。

## 仍需较大改造的项目

以下项目涉及仿真资产、录制协议或已绑定的证据生成器，本次没有直接改动：

2. **3-D 控制和仿真资产选择**
   * `src/multidetect/simulator/three_d_control.py` 的 multirotor 资产绑定仍是
     `x500` + `mono_cam`；`scripts/run_simulator_3d_control_acceptance.py` 也只验证
     `x500_mono_cam` 的模型文本。需要把 gimbal 资产作为独立 profile 纳入控制验收，确认
     camera link、gimbal link 和 PX4 4019 启动文件都来自同一 revision。
3. **Gazebo 传感器录制（本次完成 gimbal freshness gate，并接入旋翼 sidecar）**
   * `scripts/gazebo_sensor_stream.py` 对模型名含 `gimbal` 的机型订阅
     `/world/{world}/model/{model}/joint_state`，使用 sidecar 相同的
     `cgo3_camera_joint`、`cgo3_vertical_arm_joint`、`cgo3_horizontal_arm_joint` 键和
     `position_rad`/`velocity_rad_s` 字段。按 Gazebo 仿真时间选取不晚于图像的最近样本，
     独立保留 joint source timestamp，允许的最大偏差为 100 ms；缺样、未来样本、超时或
     缺少任一 gimbal 关节时拒绝该帧。通过帧会写入含帧时间、源时间、角度及角速度的
     `gazebo_sensor_stream_gimbal_frame_state` 诊断事件，不使用固定机体相机姿态回退。
     非 x500 的旧模型没有 sidecar 时仍可继续出帧。x500 的四个旋翼 joint 名称为
     `rotor_0_joint` 到 `rotor_3_joint`，且插件声明 `rotorVelocitySlowdownSim=10`。
4. **螺旋桨旋转与渲染（本次已接入）**
   * `scripts/gazebo_sensor_stream.py` 对 x500 订阅同一个 joint-state topic，按不晚于 RGB
     仿真时间的最近样本绑定。四个旋翼全部存在时，depth companion 写入同一 RGB 时间戳、
     独立 source timestamp、`angles_rad` 和 `spin_rad_s`；未来样本、超过 100 ms 或不完整
     样本不会绑定。旋翼状态是可选 enrichment，固定/旧 episode 没有 topic 时不阻断 RGB。
   * `scripts/render_onboard_sdf_blender_dataset.py` 接受每帧四个记录角度，按
     `rotor_i_joint` 索引直接设置 propeller 对象的 Z 旋转，并保留角速度在 manifest；标量
     旧输入仍保留原有交替旋向兼容行为。
   * 真实旋翼速度相对渲染帧率会产生 alias，因此采用逐帧冻结记录角度，不将角速度外推到
     曝光区间，也不打开 Blender motion blur。`spin_rad_s` 仅作为可追溯状态保存，未来若
     需要曝光积分可另行增加明确合同。
5. **离线 Blender 相机姿态（代码已补，未实际渲染）**
   * 默认随机采样保持兼容；`--camera-pose-json` 现在可接收 recorded composed pose，或
     `vehicle_pose`（NED 位置与姿态）加 `camera_mount`（fixed/gimbal 与稳定模式）。后者使用
     `CameraMount`/`compose_camera_pose()` 合成姿态。相机按完整旋转与中心写入 Blender 世界
     矩阵，逐帧 manifest 记录相机世界矩阵、输入来源和云台角。CPU 单测覆盖坐标转换、固定
     相机随机体转动以及 composed pose 精确回放。此环境未运行 Blender，故没有渲染图像或
     Blender API 集成结果；不能据此声称视觉输出已验证。环境没有 `blender` 可执行文件，
     已用 CPU 数值测试解析 SDF joint axis，并核对固定 `pi` yaw 后的有效轴为 pitch=`-Y`、
     yaw=`-Z`、roll=`+X`，同时验证 `compose_camera_pose()` 的 RPY 矩阵输出。
6. **生成 world 与 teacher intent**
   * `src/multidetect/simulator/world_gen.py`、`scripts/generate_r15_worlds.py` 及生成的
     teacher intent 仍以单个 `camera_pitch_rad` 描述多旋翼相机，没有 gimbal yaw/roll、稳定
     模式或 prop spin。生成器需要版本化新的 sidecar 字段后再更新受证据约束的 schema/index。
7. **回放与评分入口**
   * `scripts/score_onboard_loop.py`、`scripts/replay_onboard_episode.py`、
     `scripts/validate_step_triangulation_replay.py` 等入口仍从静态 calibration 读取
     `camera_pitch_rad`/`mount_pitch_down_deg`。迁移时应优先读取 observation 的
     `camera_pose`/`gimbal_state`，旧固定相机数据才走兼容回退。
8. **固定翼/coverage 几何**
   * `src/multidetect/search_coverage.py`、`src/multidetect/autonomy/fw_search.py` 和
     `src/multidetect/autonomy/fw_geometry.py` 仍以单个向下俯角表达视场。云台接入后需要
     统一调用 composed pose，否则固定翼和 tailsitter 的业务层仍会把机体姿态误当相机姿态。

## 本次续作

* `scripts/extract_px4_x500_gimbal_collision_assets.py` 生成独立的六文件源闭包，覆盖碰撞与导航
  检查需要的 SDF 和 PX4 airframe 原文。
* `tests/test_px4_x500_gimbal_collision_asset_extraction.py` 检查 pinned 来源、六个文件的哈希
  记录和输出目录覆盖保护。
* 本次新增 `sim_gimbal_collision.py` 作为闭包校验和 SDF 关节/碰撞展开器，并为三个消费者增加
  gimbal 专项 CPU 测试，包含资产/加载碰撞漂移拒绝与地面支撑范围检查。

## 验证

本次运行：

```text
tests/test_sim_session.py
tests/test_sim_airframe_smoke.py
tests/test_airframe_profile.py
tests/test_simulator_scenario_runner.py
tests/test_camera_mount.py
tests/test_sim_teacher_policies.py
tests/test_gimbal_controller.py
```

结果：`192 passed, 1 skipped`。Docker 网络探测失败时的既有 warning 仍会出现，测试没有
启动 Docker、SITL、GPU 或网络下载。

回归闸门 `python scripts/run_onboard_gate.py -q -p no:warnings`：`PASS (135s)`。

没有修改 `artifacts/` 软链接内容、`cx-delta-*` 工作、受证据约束的 JSON 文件，也没有
修改 schemas 或 release/progress 证据。

### 2026-11-05 本次续作验证

* 单进程相关测试：`test_sim_gimbal_collision_consumers.py`、
  `test_sim_airframe_envelope.py`、`test_sim_navigation_reference.py`、
  `test_sim_ground_contact_geometry.py` 全部通过；Navigation reference 中一个可选只读诊断因本地
  证据不存在而按既有规则跳过。
* `PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp /data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python scripts/run_onboard_gate.py -q -p no:warnings`：
  `PASS (111s)`，CPU-only。
* 没有启动 Docker、Gazebo/SITL、GPU 任务或网络下载；没有改动 `artifacts/`、schemas、release/progress
  证据文件。
