# 区域搜索航线评估

## 运行时基线

`area_search` 和 `count` 最终都由 `OnboardLoop._search_step` 驱动。默认 `search_pattern=current` 保留 `ExpertSearchPolicy` 的历史 expanding-square：每两条边增加边长，转向和位移分开发出，位移最多 15 m。离线工具调用同一策略的路线公式；当前路线的多半径、多高度等价性由测试覆盖。所有结果都是平面几何估计，不包含遮挡、地形和真实检测器误差。

`two_altitude` 已在运行时实现：高空 lawnmower 扫描完成后，状态机以最多 8 m 一次的垂直动作下降到 `low_search_altitude_m`，然后执行低空 lawnmower 确认 pass。它是 opt-in，默认仍是 `current`。

相机来自记录/运行时的 `DEFAULT_CAMERA_INFO`：1280x960、`fx=fy=539.936368`、`cx=640`、`cy=480`，安装俯角为 `0.18 rad`（10.3132°）。模拟器也可用 `CoverageCamera.from_runtime` 从同一 mapping 构造。

## 三道尺寸 gate

模拟器分别报告：candidate filter（`LoopConfig.min_box_side_px`，默认 6 px）、slow-verdict eligibility（`min_verdict_side_px`，默认 10 px）和 confirmation（`confirm_min_side_px`，默认 20 px）。`CoverageConfig` 同时接受这些运行时名称，并映射到 `proposal_floor_px`、`slow_verdict_min_px`、`confirmation_min_px`。

## 修正后的比较

固定 seed=7、200 个均匀目标、速度 8 m/s、转向 2 s、每个 slow call 悬停 1 s、`R=25/55/100 m`、高空 35 m、低空 12 m。表中 `C/S/F` 分别为 candidate/slow-verdict/confirmation 覆盖率；`T90` 是 slow-verdict 达到 90% 的时间，`--` 表示该 gate 未达到 90%；`T` 是飞行、转向、下降和 slow-call 悬停总时间。

| 目标 | R | pattern | C/S/F | T90(s) | T(s) | slow calls |
|---|---:|---|---:|---:|---:|---:|
| cone/backpack | 25 | current | 1.00/0/0 | -- | 23.7 | 0 |
| cone/backpack | 25 | lawnmower | 1.00/0/0 | -- | 7.1 | 0 |
| cone/backpack | 25 | spiral | 1.00/0/0 | -- | 7.9 | 0 |
| cone/backpack | 25 | two_altitude | 1.00/.99/.99 | 39.4 | 239.4 | 198 |
| cone/backpack | 55 | current | 1.00/0/0 | -- | 40.2 | 0 |
| cone/backpack | 55 | lawnmower | .97/0/0 | -- | 38.9 | 0 |
| cone/backpack | 55 | spiral | .78/0/0 | -- | 16.3 | 0 |
| cone/backpack | 55 | two_altitude | 1.00/.99/.99 | 136.0 | 350.4 | 198 |
| cone/backpack | 100 | current | .92/0/0 | -- | 64.9 | 0 |
| cone/backpack | 100 | lawnmower | .99/0/0 | -- | 119.3 | 0 |
| cone/backpack | 100 | spiral | .54/0/0 | -- | 29.4 | 0 |
| cone/backpack | 100 | two_altitude | 1.00/1.00/1.00 | 359.2 | 603.2 | 200 |
| car | 25 | current | 1/1/1 | 1.6 | 223.7 | 200 |
| car | 25 | lawnmower | 1/1/1 | 3.1 | 207.1 | 200 |
| car | 25 | spiral | 1/1/1 | 1.3 | 207.9 | 200 |
| car | 25 | two_altitude | 1/1/1 | 3.1 | 241.4 | 200 |
| car | 55 | current | 1/1/1 | 26.6 | 240.2 | 200 |
| car | 55 | lawnmower | .97/.97/.97 | 36.9 | 232.9 | 194 |
| car | 55 | spiral | .78/.78/.78 | -- | 171.3 | 155 |
| car | 55 | two_altitude | 1/1/1 | 36.9 | 350.4 | 200 |
| car | 100 | current | .92/.92/.92 | 62.9 | 248.9 | 184 |
| car | 100 | lawnmower | .99/.99/.99 | 91.8 | 317.3 | 198 |
| car | 100 | spiral | .54/.54/.54 | -- | 137.4 | 108 |
| car | 100 | two_altitude | 1/1/1 | 91.8 | 603.2 | 200 |

## 建议

修正后结论与旧文档相反的部分是：低空恢复小目标确实可实现，但代价是完整第二次扫描和大量 slow calls；旧文档把 10 px 当 proposal floor、并把未实现的低空 pass 归给 `two_altitude`，因此其数字不能使用。

- 默认保持 `current`，因为它是历史路径；R=25 m 时 spiral 的 T90（1.3 s）快于 current（1.6 s），不能把 current 称为最快。
- 需要小目标确认时，显式选择 `two_altitude`，并设置合理的 `low_search_altitude_m`；它能把 0.5 m 目标从 candidate（约 7.7 px）推进到 slow/confirmation，但时间约为单次高空扫描的数倍。
- `lawnmower` 是区域覆盖率稳定的通用候选路线；`spiral` 飞行时间短，但在大半径边缘的覆盖明显下降。
- 100 m 区域应拆分 zone 或提高预算；任何 pattern 都会因步数/slow-call预算成为瓶颈，不能只看单次几何覆盖率。\n
