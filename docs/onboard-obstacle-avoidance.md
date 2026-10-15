# 机载障碍物安全方案

本文记录 `ObstacleSource` 接口、SIM 离线评估和 Jetson Orin Nano 8GB 的选型边界。默认源仍是现有 SDF 静态地图；没有深度帧、深度过期或姿态不可用时，深度源必须退回安全结果（阻止飞行），而不是假定空间清空。

## 接口和运行策略

执行器和 `OnboardLoop` 只调用 `segment_clear(start_ned, end_ned, altitude_m)`。`StaticMapSource` 封装原来的 `load_sdf_obstacles`/`segment_clear_of_obstacles`，因此默认配置的返回值和调用顺序保持不变。`DepthGridSource` 将深度像素反投影到局部 NED 栅格，记录每格最高障碍高度和最近更新时间；`CompositeSource` 对多个源取逻辑 AND，适合静态地图与实时传感器叠加。源通过构造器注入，未提供源时使用原有 callable。

深度栅格是滚动局部地图，不是全局 SLAM。每帧需要深度数组、相机内参（fx/fy/cx/cy）、NED 位姿和相机俯视安装角；有效像素按量程、有限值和高度阈值过滤，按 `grid_resolution_m` 聚合。查询线段采样栅格并考虑 `clearance_margin_m`。超过 `stale_after_s` 的帧、无有效像素或缺姿态均返回 blocked（fail-safe）。

## SIM 离线评估

评估脚本读取 `artifacts/datasets/onboard-loop/**/episodes/*/depth/*.npy` 与每个 episode 的 `steps.jsonl`，从 `obs_before/obs_after` 取位姿、内参和时间戳，并用 `episode.json.world_sdf` 加载同世界 SDF。对静态障碍 footprint 周围的栅格计算 occupied-cell precision/recall/F1，同时按记录 action 重放每一条水平 leg，比较静态源和深度源的 blocked 决策。脚本只使用 NumPy/标准库，不启动 simulator、GPU、Docker 或模型。

运行示例（在仓库根目录）：

```powershell
python scripts/evaluate_depth_obstacle_source.py D:\Projects\Multi-Detect\artifacts\datasets\onboard-loop --max-episodes 20 --output output/depth-grid-eval.json
```

报告包含 episode/frame 数、occupied-cell TP/FP/FN、precision/recall/F1、双方 blocked leg 数和相同决策比例，以及 CPU `perf_counter` 的每帧构建延迟（平均、P50、P95、最大值）。SIM 深度是 320x240 UINT16 毫米；评估会转换为米。没有可读 world SDF 或没有有效深度的 episode 会计入 skipped 并说明原因。

当前工作树的离线结果见 `output/depth-grid-eval.json`（若该文件不存在，表示本机未提供对应 artifacts，不能伪造数字；运行上面的命令即可生成）。CPU 延迟只代表本机 Python/NumPy，不代表 Orin 实时预算。

在本次可读的 `dagger-cfs-r8-low` 前两个 episode 上实测：episode-0000（24 帧）occupied-cell precision **0.2354**、recall **0.6170**，CPU 平均/P50/P95 **62.2/56.4/93.8 ms/帧**；episode-0001（80 帧）precision **0.1979**、recall **0.4601**，平均/P50/P95 **60.3/58.1/79.8 ms/帧**。两者各自重放 10/5 条平移 leg，静态源和深度源均阻挡 0 条，决策一致率均为 **1.0**。这些数字受本机 CPU、栅格分辨率和记录视角影响，不能直接当作飞行安全指标。

## 真实飞机传感器选项

* **双目深度**：OAK-D Lite、Luxonis OAK-D Pro、RealSense D455 等可直接输出深度，户外需要红外抗阳光能力、同步和防振。优点是几何可解释、无需网络模型；缺点是纹理少、远距离和强日照会掉点。
* **ToF/LiDAR 深度**：短距 ToF 模块或 Livox/Benewake 类小型 LiDAR 可作为前向/下视距离源，延迟稳定、低纹理可靠，但视场和点数有限，通常要与 RGB/里程计融合。
* **PX4 距离传感器**：将可靠的单束或多束距离传感器发布为 `DISTANCE_SENSOR`，由 PX4 Collision Prevention (CP) 在飞行控制器内做最后一道刹车/限速。常见参数包括 `CP_DIST`（触发距离，单位米）、`CP_DELAY`（系统延迟补偿）、`CP_GO_NO_DATA`（无数据时是否允许继续）和 `CP_GUIDE_ANG`（引导方向，具体取值以目标 PX4 版本参数文档为准）。机载规划器仍应把无数据视为 blocked，并记录传感器健康状态；CP 不是局部地图替代品。

## 单目深度网络（仅作备选）

单个 RGB 相机没有绝对尺度；网络输出必须由已知高度、地面平面、气压计/视觉里程计或稀疏测距校准，否则不能直接用于安全距离。以下是工程估计，不是本仓库实测：

| 模型 | 规模/许可 | Orin Nano 8GB 延迟估计* | 备注 |
|---|---|---:|---|
| **Depth Anything V2 Small** | 约 24.8M 参数，Apache-2.0（代码/权重需逐项核对发行版） | 40–80 ms/帧（FP16 TensorRT，约 320x240） | 质量和速度折中，仍需尺度校准 |
| **MiDaS v3.1 Small** | 约 21M 参数，MIT（模型权重许可随发布包核对） | 35–70 ms/帧 | 生态成熟，远处障碍尺度不稳定 |
| **FastDepth** | 约 1.8M 参数，MIT | 10–25 ms/帧 | 速度优先，细小障碍召回较弱 |

\* 估计假设独占约一半 GPU、TensorRT FP16、无 YOLOE/VLM 并发争用；本机已有 YOLOE 和 2B VLM，因此实际延迟可能翻倍或出现抖动。按任务规则本次不下载、不运行预训练模型，部署前必须在目标板测量端到端 P50/P95、功耗和掉帧率，并核对具体权重许可证。

## 推荐

首选带深度输出的双目或 ToF/LiDAR，`CompositeSource(StaticMapSource, DepthGridSource)` 作为规划层，PX4 CP 作为独立硬实时兜底。若机体只能保留单目 RGB，先保留静态地图（若可维护）和 CP 距离传感器；单目网络只用于候选障碍提示，经过尺度校准、时延预算和失效注入测试后才能提升为阻挡依据。任何源失效、数据过期或时间同步超限都应保持 blocked 并触发返航/悬停策略。
