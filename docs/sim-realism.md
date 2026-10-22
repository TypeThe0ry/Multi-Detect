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

`--actors N` 生成使用本地 `actor_box.dae` 的非 skinned `<actor>`，轨迹由 `<script>` 循环驱动，
因此没有 Fuel/network 依赖。`--terrain dem` 会生成一个本地 PGM heightmap；`procedural` 使用
确定性的平面基座。对象不会自动重算 z，高程 DEM 变体应在任务需要时重新放置对象。

可用 `--register-index configs/scenarios/.../index.json` 将一个源条目复制为变体条目；原条目
不会被替换，变体通过 `variant_of` 保留来源关系。
