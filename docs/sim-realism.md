# 仿真真实感变体

`scripts/augment_sim_world.py` 从现有 SDF 派生世界，不改写源文件。输出固定在
`simulation/gazebo/worlds/variants/`，并保持源世界的 `spherical_coordinates`、ENU 原点和实体
pose。示例：

```powershell
python scripts/augment_sim_world.py simulation/gazebo/worlds/gen_cnt2_w0000.sdf `
  --wind-mps 6 --gust-mps 3 --gust-period 4 --time-of-day dusk `
  --actors 2 --terrain procedural
```

风同时写入标准 `<wind><linear_velocity>` 和 Harmonic `WindEffects` 系统。`gust` 使用系统的
随机速度标准差、时间常数及显式元数据，最终动力学是否生效必须在锁定的 PX4 镜像中用
`gz sdf -k` 和短 SITL 观测姿态/漂移确认；不能把 XML 存在当成动力学证据。

相机传感器由 PX4 的 `model://mono_cam` vehicle composition 提供，不在多数世界 SDF 中。
脚本只会给世界中明确存在的 camera/depth/segmentation sensor 添加 Gaussian noise，并在报告中
返回实际修改数量；外部 include 不会被猜测修改。`--fog` 当前被跳过，因为 pinned Harmonic
Ogre2 的 SDF fog 支持依赖渲染器配置，已有 appearance 的 haze fallback 更可复现。

要让 RGB 相机真正带噪声，使用 `simulation/gazebo/model-overlays/mono_cam_noise` 覆盖 pinned
镜像中的 `model://mono_cam`。该目录保留官方相机几何和 topic，仅增加
`camera/camera/noise`（Gaussian, stddev 0.01）；默认资源路径不包含这个 overlay，因此默认
仿真行为不变。它还给相机 link 标记 `enable_wind`；pinned `windy.sdf` 明确使用这个 link
开关，只有同时在受影响的气动 link 上启用时 WindEffects 才会产生力。当前仓库的 x500
气动 link 来自镜像 `model://x500`，所以完整风动力学仍需在容器中确认；若 pose/attitude
证据没有响应，应再制作包含 PX4 x500 link 的 wind overlay，而不是只依赖 world `<wind>`。

`--actors N` 生成使用本地 `actor_box.dae` 的非 skinned `<actor>`，轨迹由 `<script>` 循环驱动，
因此没有 Fuel/network 依赖。`--terrain dem` 会生成一个本地 PGM heightmap；`procedural` 使用
确定性的平面基座。对象不会自动重算 z，高程 DEM 变体应在任务需要时重新放置对象。

可用 `--register-index configs/scenarios/.../index.json` 将一个源条目复制为变体条目；原条目
不会被替换，变体通过 `variant_of` 保留来源关系。

`--px4-world NAME --px4-world-root DIR` 只导入已经从 pinned image 离线导出的世界，并拒绝
任何缺少本地 `model://` 目录的世界，避免运行时回退 Fuel。锁内检查发现镜像包含
`baylands.sdf`、`lawn.sdf`、`walls.sdf`、`windy.sdf` 和 `aruco.sdf`，但它们依赖镜像资产；
本仓库没有把不完整副本提交到 variants。导入前必须把对应模型复制到同一个本地 cache。

两个 suite 的 mission config 都启用 `wind_vehicle_overlay`。collector 会额外挂载
`model-overlays/x500_mono_cam_wind` 到 `x500_mono_cam`，通过合并后的 `base_link` 和
`camera_link` 设置 `enable_wind=true`；没有该选项的 calm airframe 不挂载此 overlay。

验证记录：pinned 镜像对带风/演员的提交变体执行 `gz sdf -k` 返回 `Valid.`；镜像的
`windy.sdf` 明确包含 `<enable_wind>true</enable_wind>`，说明仅写 world `<wind>` 不足以证明
动力学作用。首次 mission suite 在共享锁等待期间未完成，未产生可引用的 calm/wind steps 或
PX4 ulog；因此本文不宣称风对飞行器已产生实测漂移，也不伪造 actors+dusk+noise 结果。
