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

## 仍需较大改造的项目

以下项目涉及仿真资产、录制协议或已绑定的证据生成器，本次没有直接改动：

1. **x500 专用碰撞/接触合同**
   * `src/multidetect/autonomy/sim_airframe_envelope.py`、
     `src/multidetect/autonomy/sim_navigation_reference.py` 和
     `src/multidetect/autonomy/sim_ground_contact_geometry.py` 要求固定的 x500/x500_base
     SDF 闭包、四个 rotor link 和 `x500_mono_cam_0`。这些检查不能直接接收 PX4
     `x500_gimbal` 的镜像模型。现已增加独立提取器和
     `multidetect.px4-x500-gimbal-collision-source-assets-v1` 哈希快照，包含组合模型、gimbal、
     x500、x500_base 和两份 airframe SDF/原文闭包；快照通过导出器从 pinned image digest 重建，
     未手工复制。消费者仍未接入此闭包：三个审计的 gimbal 输入/API、展开后的四 rotor + 云台
     camera collision 对比，以及云台全关节范围的源几何包络/接触计算还需实现。云台 collision
     sphere 必须按 SDF joint 位姿和实际限位计算，不能遗漏或以固定相机结果代替。固定 x500 合同
     与结果未修改。
2. **3-D 控制和仿真资产选择**
   * `src/multidetect/simulator/three_d_control.py` 的 multirotor 资产绑定仍是
     `x500` + `mono_cam`；`scripts/run_simulator_3d_control_acceptance.py` 也只验证
     `x500_mono_cam` 的模型文本。需要把 gimbal 资产作为独立 profile 纳入控制验收，确认
     camera link、gimbal link 和 PX4 4019 启动文件都来自同一 revision。
3. **Gazebo 传感器录制**
   * `scripts/gazebo_sensor_stream.py` 的图像、CameraInfo 和机体 odometry 对齐链路没有
     统一录制 gimbal joint state/角速度，也没有 propeller joint state。应加入独立的
     gimbal 状态时间戳和 freshness gate；缺少新鲜姿态时不能回退到固定机体相机。
4. **螺旋桨旋转与渲染**
   * `simulation/gazebo/models/x500_mono_cam` 及相关 world/overlay 目前只保证动力学 rotor
     link，数据采集和离线 Blender 渲染没有把每帧 propeller angle/spin 作为输入。
     `scripts/render_onboard_sdf_blender_dataset.py` 只读取静态 world primitive，
     `scripts/build_onboard_rendered_3d_dataset.py` 只消费 RGB/depth/telemetry manifest。
     需要在模型/插件、采集 sidecar 和 renderer 三处共同定义角度、角速度与时间语义，避免
     只在标签里增加一个未驱动的字段。
5. **离线 Blender 相机姿态（代码已补，未实际渲染）**
   * 默认随机采样保持兼容；`--camera-pose-json` 现在可接收 recorded composed pose，或
     `vehicle_pose`（NED 位置与姿态）加 `camera_mount`（fixed/gimbal 与稳定模式）。后者使用
     `CameraMount`/`compose_camera_pose()` 合成姿态。相机按完整旋转与中心写入 Blender 世界
     矩阵，逐帧 manifest 记录相机世界矩阵、输入来源和云台角。CPU 单测覆盖坐标转换、固定
     相机随机体转动以及 composed pose 精确回放。此环境未运行 Blender，故没有渲染图像或
     Blender API 集成结果；不能据此声称视觉输出已验证。
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
* 三个审计消费者尚未迁移至 gimbal 合同；本审计项保持开放，不将提取器完成误记为几何审计完成。

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
