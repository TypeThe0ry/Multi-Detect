# 移动目标世界

`configs/scenarios/generated-movers/` 中的六个 `gen_mov_w*.sdf` 是不依赖 Fuel 下载的移动目标场景。使用下面的命令按固定 seed 重生成世界、truth 和 runtime 文件：

```powershell
python -c "from multidetect.simulator.world_gen import generate_mover_worlds; generate_mover_worlds()"
```

## 当前实现

移动目标是普通的 static Gazebo model，包含 box visual 和 label 1。目标初始位置和中心高度来自 truth。`scripts/gz_mover_driver.py` 在容器内读取 `/clock`，使用与 `world_gen.trajectory_position` 相同的恒速闭合折线算法，然后以 `/world/default/set_pose` 按仿真时间更新模型。每次更新记录仿真时间、ENU 坐标、航向角、set_pose 是否成功和 wall latency。

不要使用 native actor。之前的 actor link visual 在 Harmonic 相机的 RGB 和 segmentation 中不可见。带 OpenRobotics `male_visitor` skin 的 actor 会让 headless Ogre2 初始化占满 CPU；单骨 skinned box actor 虽然能加载，也没有出现在 RGB 或 segmentation 中。也不要使用 `gz-sim-trajectory-follower-system`：它通过力和力矩推动模型，没有速度参数，实际速度无法与 sim-time truth 对齐。

truth 和画面现在共享同一套纯数学轨迹，模型的实际位姿由 driver 按 `/clock` 更新。driver 不依赖 Gazebo Python bindings，只使用容器内的 `/usr/bin/python3` 标准库。

## Hover teacher

hover teacher 读取真实 telemetry 的 NED 位置和 yaw。它计算轨迹 waypoint 的 ENU centroid，取半尺寸的一点六倍加六米，并至少保持十五米距离；飞机先飞到轨迹 centroid 南侧的 standoff 点，再转向 centroid，最后保持高度。NED 位移会按当前 yaw 转换成 body forward/right，避免 PX4 x500 实际朝向 EAST 时把 north/east 交换。

## 采集器闭合

当 truth 中的 true target 有 `trajectory` 时，collector 在 `gz_world_loaded` 后、等待 PX4 heartbeat 前启动：

```text
docker exec -d <container> python3 /workspace/scripts/gz_mover_driver.py ... --log /tmp/mover_driver.log
```

采集结束时，collector 在容器清理前复制日志到 episode 目录，写入 driver 命令、更新数、成功更新数、set_pose wall latency 的中位数和最大值。如果前十秒仿真时间没有成功的 set_pose，episode 会失败。

真实验证命令：

```powershell
$env:TEMP='D:\Projects\mdwt\movers\.tmp'; $env:TMP=$env:TEMP
$env:PYTHONPATH='src;.'
python scripts/verify_mover_world.py --world gen_mov_w0000 --out artifacts/verify-movers-w0000
python scripts/verify_mover_world.py --world gen_mov_w0001 --out artifacts/verify-movers-w0001
```

一次只运行一个 SITL，运行前确认至少有 6 GB 可用内存，只清理名称以 `codex-movers` 开头的容器。验收要求 RGB 和 segmentation 可见比例都大于 0.8，并检查投影框、质心误差、driver 日志和 `collector_returncode`。

## 真实验证结果（2026-10-18）

验证器只评估飞机悬停稳定时的帧（动作为 hold、横滚/俯仰 ≤ 3°、该步内偏航变化 ≤ 3°）：转弯和刚转完偏航还在摆动时，图像与配对位姿对不上，投影的真值会落在目标旁边，这与移动目标无关。通过条件：分割与 RGB 可见比例都 > 0.8，且投影中心与分割质心的中位误差 ≤ 25 px。

| 世界 | 目标 | 评估帧 / 跳过 | 分割可见 | RGB 可见 | 质心误差中位 / 最大 |
|---|---|---|---|---|---|
| gen_mov_w0000 | 行人 1 m/s | 11 / 4 | 91% | 91% | 4.5 / 6.1 px |
| gen_mov_w0001 | 车辆 3 m/s | 10 / 5 | 100% | 100% | 15.9 / 19.8 px |

驱动器约 98% 的 `set_pose` 调用成功，单次调用墙钟约 0.32 s；命令位姿与生效时刻的仿真时间差中位 0.09 s、最大 0.28–0.46 s（车辆 3 m/s 时最大约 1.4 m），车辆的质心误差因此比行人大。
