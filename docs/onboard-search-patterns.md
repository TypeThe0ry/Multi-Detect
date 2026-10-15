# 区域搜索航线评估

## 现状

`area_search` 和 `count` 最终都调用 `OnboardLoop._search_step`。该函数先分段下降，再调用注入的 `search_action`。当前 zone 航线是从中心开始、每两条边增长的四方向 expanding-square，每段先转向再平移。

## 离线模型

`src/multidetect/search_coverage.py` 是纯几何模拟，不启动 Gazebo、GPU 或网络。使用录制相机内参 1280x960、`fx=fy=540`、俯视角 10.3 度。

35 m 高度时 cone/backpack 的 `TYPICAL_SIZE_M=0.5` 只有约 7.7 px，低于 10 px proposal floor，所以高空换航线也不会提高看清率。

## 模拟结果

以固定 seed=7、200 个均匀目标计算：`seen` 是看清比例，`t90` 是秒数，`slow` 是慢调用次数。

| 目标 | 半径 | pattern | seen | t90 s | total s | slow |
|---|---:|---|---:|---:|---:|---:|
| cone/backpack | 25 | current | 0.00 | -- | 23.6 | 0 |
| cone/backpack | 25 | lawnmower | 0.00 | -- | 7.1 | 0 |
| cone/backpack | 25 | spiral | 0.00 | -- | 12.7 | 0 |
| cone/backpack | 25 | two_altitude | 1.00 | 4.1 | 239.9 | 200 |
| cone/backpack | 55 | current/lawnmower/spiral | 0.00 | -- | 40.0/55.7/23.3 | 0 |
| cone/backpack | 55 | two_altitude | 1.00 | 46.6 | 374.6 | 200 |
| cone/backpack | 100 | two_altitude | 0.82 | -- | 601.3 | 165 |
| car | 25 | current/lawnmower/spiral | 1.00 | 2.6/4.1/2.0 | 223.6/207.1/212.7 | 200 |
| car | 55 | current | 1.00 | 27.6 | 240.0 | 200 |
| car | 55 | lawnmower | 1.00 | 46.6 | 255.7 | 200 |
| car | 55 | spiral | 0.71 | -- | 165.3 | 142 |
| car | 100 | current | 0.81 | -- | 227.6 | 163 |
| car | 100 | lawnmower | 0.70 | -- | 263.4 | 141 |
| car | 100 | spiral | 0.47 | -- | 129.1 | 94 |

## 推荐

小物建议使用 `two_altitude`，接受更长的飞行和慢调用代价。大物在 25/55 m 区域优先 `current`；100 m 应分区或提高预算。

配置键 `search_pattern` 默认 `current`，可选 `current`、`lawnmower`、`spiral`、`two_altitude`，用于 zone-bounded 的 `area_search`/`count`。离线模拟的 `two_altitude` 包含高空扫描和低空复核；当前实机运行时低空下降由既有 `search_altitude_m` 处理。
