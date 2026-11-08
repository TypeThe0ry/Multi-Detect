# r21id-r9q 邮箱拒绝帧 pilot 计划（2026-11-06）

## 目的与范围

r9p 的 `car→mailbox` 产生 0 张几何合格帧：邮箱 mesh 的窄轴仅 0.133 m，历史路线记录中的投影短边为 1.40–7.46 px，低于 builder 的 8 px 下限；r9p 的 confuser intent 也没有实际进入 confuser-centered 路线。本 pilot 只覆盖 `car→mailbox`，使用 `generate_r15_worlds.py --profile r21id-r9q` 生成 4 个 world，每个 world 后续采集 2 个 `--teacher-target confuser` episode。未启动 SITL，也未运行采集脚本。

## 几何与路线

资产检查使用 fuel_v1 mailbox 的 resolved mesh、COLLADA 坐标和 catalog bounds。源 mesh 总体 bounds 为 X=0.133416 m、Y=0.355085 m、Z=1.067241 m；它是立柱邮箱，local +Y 作为投递门正面法向。r9q 保持 mesh source dimensions 和 SDF mesh scale 为 **1 / 1 / 1**，truth、collision bounds 与 mesh 尺寸一致。world 中 yaw=π/2，使 local +Y 正面朝向从西侧接近的 teacher 路径。

四个 confuser-centered observation waypoint 都在正面半球，横向偏移为 -0.30、-0.10、+0.10、+0.30 m。高度 3 m，斜距 5.4–6.0 m，max detection range 留 2 m 余量。四个 view 都指向邮箱中心；相机朝向由 teacher 目标方向确定，邮箱 truth bounds 使用 `project_target_bbox` 的相机内参和姿态进行离线投影。预测不包含飞行、渲染或 detector proposal 结果。

## 离线预测

Builder 几何 gate 为短边 ≥8 px、bbox 面积 ≥4 px²、可见比例 ≥0.3。相机为 1280×960，fx/fy=539.936 px。以下短边像素来自生成 truth、teacher waypoint 和 `camera_projection.project_target_bbox`；每 view 计划采集 8 帧。表内“合格帧”是 CPU 几何预测，不代表最终 RGB 分割或 YOLOE proposal 已命中。

| world | 邮箱斜距 | front px | oblique px | edge_truncated px | recovery px | 预测合格帧/view |
|---|---:|---:|---:|---:|---:|---:|
| `gen_r21id_r9q_w0000` | 5.5 m | 17.48 | 15.82 | 15.82 | 17.48 | 8 / 8 / 8 / 8 |
| `gen_r21id_r9q_w0001` | 5.8 m | 16.14 | 14.69 | 14.69 | 16.14 | 8 / 8 / 8 / 8 |
| `gen_r21id_r9q_w0002` | 5.4 m | 17.98 | 16.24 | 16.24 | 17.98 | 8 / 8 / 8 / 8 |
| `gen_r21id_r9q_w0003` | 6.0 m | 15.36 | 14.04 | 14.04 | 15.36 | 8 / 8 / 8 / 8 |

四个世界、16 个 view 预测的最小短边是 **14.04 px**，没有 view 低于 8 px，因此不需要缩短任何视点距离。预测 bbox 均在画面内、面积达到 4 px² 门槛。这里的 bbox 检查是投影可见性，不包含场景遮挡或渲染测试。每个 episode 每个 view 计划采集 8 帧；若两个 episode 都完整执行，每个 world/view 预计 16 个几何合格帧。对应断言位于 `tests/test_r21id_r9q_mailbox_yield.py`，同时校验 truth/SDF mesh scale、源 mesh 尺寸、front 法向与 approach 向量。

## 采集与判定

脚本 `queue_r21id_r9q_collect.sh` 每 world 配置 2 episodes，使用 `run_r15_round.py --stage collect` 的 confuser teacher 目标，logger mode 关闭。采集后应逐 world/view 检查实际 mailbox truth bbox 短边、RGB 内容可见性和 proposal 覆盖；再按去重 RGB 统计 builder 最终 paired reject 行。CPU 几何通过只说明路线和 truth 尺寸具备产出条件，不替代实际数据门槛。
