# 机载障碍物安全方案

执行器和 `OnboardLoop` 使用 `ObstacleSource.segment_clear(start_ned, end_ned)` 做局部 NED 线段检查。默认仍由已有的 `segment_clear` callback 或 `StaticMapSource` 提供静态 SDF clearance；没有 callback 和 source 时 `OnboardLoop` fail-closed，返回 blocked。`CompositeSource` 对所有 source 取逻辑 AND。

`DepthGridSource` 和 `AsyncDepthGridSource` 当前均为 experimental，未经 flight-safety qualification。`DepthGridSource` 的参数包括 `cells`、`resolution_m`、`max_age_s`（必须大于零）、`min_depth_m`、`max_depth_m`、`height_tolerance_m`、`ground_tolerance_m`、`ground_model`、`fallback` 和 `timestamp_unit`（`s/ms/us/ns`）。输入 `update()` 时必须显式指定 `encoding="FLOAT32_M"` 或 `encoding="UINT16_MM"`；不会根据数值大小猜单位。深度帧会按完整相机姿态与旋转后的机身安装偏移投影到滚动局部栅格。查询只读取一个原子快照；更新先完成整代栅格再交换。freshness 只使用本机 monotonic receipt age，传感器 timestamp 仅用于诊断。

地面返回和障碍返回分层保存。传入 `GroundModel` 时，落在局部地面面片附近（小于 `ground_tolerance_m` 的上方高度）的点进入 `terrain` 层，不参与障碍阻挡；高出地面足够距离的点进入 `occupied` 层。3-D clearance 的语义是：线段在 N/E 平面采样到 occupied cell，且线段 NED down 高度落在该 cell 的观测高度范围加 `height_tolerance_m` 内，则 blocked。没有新鲜深度时默认 blocked；只有显式 `stale_policy="static_fallback"` 才调用 `fallback`，并设置 `degraded` 标志。

## SIM 离线评估

评估器 `scripts/evaluate_depth_obstacle_source.py` 使用 `rglob("steps.jsonl")` 发现 episode；找不到有效 episode 会明确报错。它读取 `episode.json`、`steps.jsonl` 和 depth `.npy`，验证 `UINT16_MM`/`FLOAT32_M` 编码，复用 `camera_projection.py` 的 roll/pitch/yaw、安装俯角和安装偏移 `(0.18, 0, -0.12)`，并用旋转 SDF box footprint 与真实高度生成 truth。

评估输出包括 occupied-cell TP/FP/FN、precision/recall/F1、near-miss、late-block、leg TP/FP/FN、静态和深度 blocked leg、一致率、ingestion/query 延迟，以及无效 episode 原因。输入数据会注入 dropped、stale 和 out-of-order frame，并记录处理结果；无效编码、缺 pose、缺内参、缺 world SDF 的 episode 会跳过并报告。

运行：

```powershell
$env:PYTHONPATH = "src"
python scripts/evaluate_depth_obstacle_source.py `
  D:\Projects\Multi-Detect\artifacts\datasets\onboard-loop\dagger-cfs-r8-low `
  --max-episodes 20 --output tmp/depth-grid-eval-low.json
```

本次 CPU 评估（完整相机姿态与旋转后的机身安装偏移、旋转 SDF footprint、注入丢帧/过期/乱序帧）结果如下。`dagger-cfs2-r12-fast` 只有 16 个有效 episode，已全部纳入，不能声称有 20 个。

| 数据集 | episode | occupied TP/FP/FN | precision / recall / F1 | legs（静态/深度阻挡） | leg 一致率 | ingestion mean/p50/p95 ms | query mean ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| dagger-cfs-r8-low | 20 | 35668 / 83148 / 81382 | 0.300 / 0.306 / 0.303 | 364（1 / 9） | 0.973 | 222.4 / 201.4 / 389.0 | 0.0676 |
| dagger-cfs2-r12-fast | 16（全部） | 34958 / 79025 / 58834 | 0.307 / 0.373 / 0.336 | 260（1 / 12） | 0.950 | 192.0 / 160.0 / 333.0 | 0.2002 |

合成 crossing/grazing/above-top/clear probe 的汇总分别是 low `TP=24, FP=3, FN=180, near-miss=180`，fast `TP=17, FP=0, FN=151, near-miss=151`；这是利用每个 episode 最后一帧深度图的探针，不是飞行安全证明。每个 episode 注入 stale 和乱序（有足够帧时），并按记录序号周期性注入 dropped frame；输出的 `data_quality` 同时记录 `invalid_frames_skipped`。实际 recorded legs 中静态地图只出现 1 个 blocked leg，因此 leg 统计主要反映保守的 depth-only block，不能替代覆盖真实碰撞场景的测试。

## 真实飞机选项

优先使用带深度输出的双目（OAK-D、RealSense D455 等）或 ToF/LiDAR；户外需要评估阳光、纹理、同步、振动、视场和量程。PX4 Collision Prevention 通过 `DISTANCE_SENSOR` 提供硬实时兜底，常用参数包括 `CP_DIST`、`CP_DELAY`、`CP_GO_NO_DATA`、`CP_GUIDE_ANG`，具体默认值必须按目标 PX4 版本确认。机载规划器仍应把无数据视为 blocked。

单目 RGB 只能作为备选提示源，因为网络深度没有绝对尺度。可评估 Depth Anything V2 Small（约 24.8M 参数，代码/权重许可证需逐项核对）、MiDaS v3.1 Small（约 21M，MIT 代码许可，权重需核对）和 FastDepth（约 1.8M，MIT）。在 Orin Nano 8GB 上的 320x240 TensorRT FP16 延迟只能作为估计：约 40–80 ms、35–70 ms、10–25 ms；本机已有 YOLOE 和 2B VLM 并发，实际 P95 可能更高。本次不下载、不运行这些模型。

推荐使用 `CompositeSource(StaticMapSource, DepthGridSource)`，并把 PX4 CP 作为独立硬件兜底。任何 source 失效、数据过期、时间同步失效或输入编码不明都保持 blocked，并触发悬停/返航策略。
\n


AsyncDepthGridSource 使用单 worker 和容量为 1 的 latest-frame-wins 队列；`frame_budget_s` 超时计入 `deadline_misses`，该帧不会被视为 fresh，深度摄取必须保持 opt-in，不能放在 20 Hz setpoint 或 detector path；Jetson Orin Nano 上应以 PX4 Collision Prevention / DISTANCE_SENSOR 作为硬实时后备。默认 stale depth 始终 blocked；只有显式 stale_policy=static_fallback 才使用 fallback，并设置 degraded=True。
\n