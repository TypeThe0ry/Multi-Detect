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

## 2026-10-22 SITL 实测

## 鲁棒性回归套件

`configs/onboard/suite-robustness.json` 把 `count-cones`、`count-fire-hydrants` 和
`monitor-backpacks` 各扩展为中风加阵风、黄昏、相机噪声加雾三种任务变体。每个任务的
`variant` 字段由 `scripts/run_mission_suite.py` 在运行前传给
`augment_sim_world.py`；生成的 SDF 和临时 scenario index 都留在仓库内，原始世界和
原始 index 不会被改写。报告按“任务 × 变体”列出，并在每一行保留变体标签。

先做不启动 SIM 的命令检查：

```powershell
$env:PYTHONPATH='src;.'; $env:PYTHONUTF8='1'
python scripts/run_mission_suite.py `
  --suite configs/onboard/suite-robustness.json `
  --out D:\Projects\mdwt\_scratch\cx-sim-variants\robustness-dry-run `
  --dry-run
```

正式回归（每个任务一架次）命令如下；它会启动既有的 collector/SITL 流程，本文只给出
命令，不在文档验证阶段启动它：

```powershell
python scripts/run_mission_suite.py `
  --suite configs/onboard/suite-robustness.json `
  --out D:\Projects\Multi-Detect\artifacts\benchmarks\mission-suite-robustness `
  --episodes 1 --strict
```

世界生成通常是每个变体数秒；实际回归耗时由 SITL、模型冷启动和磁盘写入决定，按 9 个
任务单元线性增长，单架次预计约 5–15 分钟，完整套件预留约 1–3 小时。`--fog` 仍遵循
上文说明的 pinned Ogre2 renderer 限制，因此结果标签表示已请求雾变体，不把雾的视觉作用
误报成已验证的动力学或渲染证据。

风场变体前后踩了四个坑，都已修复：

| 现象 | 原因 | 修复 |
|---|---|---|
| Docker 报 `Duplicate mount point ... x500_mono_cam` | 分割相机覆盖层和风场覆盖层挂到同一目标 | 新增 `x500_mono_cam_seg_wind`（分割相机 + 模型级 `<enable_wind>`），开风时替换而不是叠加（`4678a40`） |
| PX4 一直 “Waiting for Gazebo world” | 世界里声明了 `WindEffects` 插件；Gazebo 世界一旦声明任何系统插件，就不再加载默认系统 | 世界只写 `<wind>` 和阵风注释，容器启动补丁按需把 WindEffects 加进 server config（`055674d`） |
| 停在地上被风推滑、“Preflight Fail: no heading reference”，解锁后翻倒 | 风力换算系数 1、5 s 内加满风，对 2 kg 的 x500 过大 | 系数 0.3，60 s 内逐渐加满（`610ba11`） |
| 相机噪声覆盖层挂载失败（make mountpoint） | 仓库模型根目录是只读挂载，里面没有 `mono_cam` 目录 | 改为覆盖镜像自带的 `/opt/px4-gazebo/share/gz/models/mono_cam`（`2413258`） |

数锥桶（gen_cnt2_w0000，各 1 架次）：

| 条件 | 结果 | 步数 | 原地转向时位置漂移均值 |
|---|---|---|---|
| 无风（10-22 三个架次） | 3/3，0 误报 | 87–122 | 0.08–0.10 m |
| 风 6 m/s + 阵风 3 m/s | 3/3，0 误报，覆盖完成后返航 | 127 | 0.26 m |
| 风 + 2 个移动物体 + 黄昏 + 相机噪声 0.01 | 3/3，0 误报 | 144 | — |

风确实作用在机体上（漂移约为无风时的 3 倍），任务仍能完成，代价是步数增加。每种条件只跑了 1 架次，还不能说明稳定性。
