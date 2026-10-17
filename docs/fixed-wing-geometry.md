# 固定翼航线几何纯函数

`multidetect.autonomy.fw_geometry` 是一层独立的几何工具。它只计算路径和判定，
不读地图、不发飞控指令，也不修改 `onboard_loop` 或 mission executor。输入位置使用
局部 NED：`(north, east, down)`；二维航线使用 `(north, east)`。角度参数以弧度为主，
名字带 `_deg` 的参数是度数。所有函数都是确定性的，不能计算的浮点输入会被压到有限
的安全值或返回明确的退化结果。

## Dubins 平面路径

```python
from multidetect.autonomy.fw_geometry import dubins_path, check_curvature

path = dubins_path(((0, 0), 0.0), ((100, 40), 0.5), 20.0)
assert check_curvature(path, 20.0)
for sample in path.waypoints:
    print(sample.north, sample.east, sample.heading_rad,
          sample.cumulative_length_m)
```

`dubins_path` 在 `LSL / RSR / LSR / RSL / RLR / LRL` 六个标准族中选择解析长度最短
的一条，返回离散的 `PathPoint` 序列、总长度、所选字母串和三个段长。圆弧采样的角度
步长不超过 `0.01 rad`，所以用相邻点的航向变化除以弦长检查时也不会超过 `1/Rmin`
（`max_path_curvature` 可查看实际最大值）。`dubins_path_lengths` 只计算解析长度，
适合做规划器比较或测试。起终点相同但航向不同会返回半径为 `Rmin` 的最短单圆弧，
不会抛异常。

## 割草机与方形搜索

```python
survey = lawnmower_search(
    (0, 120, 0, 80), footprint_width_m=24, overlap=0.25,
    min_turn_radius_m=15,
)
```

航带间距是 `footprint_width * (1 - overlap)`。正常情况下端头由 Dubins 圆弧连接；
当间距小于 `2 * Rmin` 时，结果的 `mode` 为 `"skip-lane"`，航带顺序采用隔行后补齐的
鱼尾/跳行策略，避免强行塞入半径不足的发卡弯。`coverage_fraction` 是基于航带宽度
与区域宽度的快速上界估计，`estimate_coverage` 则可对任意实际航带位置合并区间后
重新估计。`rounded_square_path((min_n,max_n,min_e,max_e), Rmin)` 用同一套 Dubins
连接将方形拐角圆弧化。

例如区域宽 80 m、相机有效 footprint 24 m、重叠 25% 时，间距为 18 m；`Rmin=15 m`
时 `18 < 30`，因此会自动进入 `skip-lane`。如果把半径降到 8 m，则 18 m 间距可以
用普通半圆端头。

## 盘旋、观察与掠过

`observation_orbit(target_ned, requested_radius, Rmin, camera_depression_rad,
vertical_fov_rad)` 把半径提升到至少 `max(requested_radius, Rmin)`。高度按视场中心
计算：`height = radius * tan(view_angle)`，目标位于相机垂直视场的中心（视场边界也
会被夹紧到有效角度）。返回的 `ObservationOrbit.center_ned` 已经把目标的 down 坐标
减去该高度，并给出闭合圆周采样点。

`fly_by_passes` 生成多条穿过目标的直线段。默认相邻航向至少 60°，每一段入口到出口
的距离由高度和垂直视场估算，`visible_time_s = 可见段长度 / 空速`。入口从当前位置
通过 Dubins 连接，下一段则从上一段出口继续规划。例如空速 15 m/s、可见段长 60 m
时，每次目标预计在画面中约 4 s；三次航向为 0°、60°、120°。

## 固定翼确认

```python
result = confirm_fixed_wing_passes(
    observations,
    min_passes=3,
    min_heading_separation_deg=60,
    position_tolerance_m=5,
    max_sigma_m=4,
)
```

观测可用 `PassObservation`，也可传字典。每次记录至少应有时间、相机 NED、视线、
身份票、通过航向、地面投影和三角化 `sigma_m`（三角化对象也可以放在
`triangulation` 字段，函数会读取它的 `position_ned`/`sigma_m`）。判定流程是：

1. 任意明确的负身份票或超出 sigma 阈值，立即返回 `reject`；
2. 正身份票数量或不同航向数量不足，返回 `need_more_passes`；
3. 所有通过的地面投影最大两两距离必须不超过 `position_tolerance_m`；
4. 全部通过后返回 `confirm`，并附带通过数、航向数、投影散布和最大 sigma。

## 思考期间盘旋

```python
loiter = thinking_loiter(
    ((north, east, down), heading_rad), airspeed_mps=18,
    min_turn_radius_m=25, nav_loiter_radius_m=40,
    segment_clear=lambda a, b: True,
)
```

半径至少为 `max(Rmin, NAV_LOITER_RAD)`。函数先试航向左侧，再试右侧；对从当前位置
到圆心的半径段和圆周每个采样段调用 `segment_clear(start_ned, end_ned)`。选中的一侧
在 `side` 中返回，`exit_point_ned` 是完成一圈后可切回原航向的位置（当前位置）；
两侧都被禁飞回调拒绝时返回 `clear=False, side="none"`，仍保留一个有限的安全结果。

这些函数只描述几何可行性。实际飞行仍需在上层加入空域、地形、失联、能量和飞控状态
门限，并在接入真实档案前重新校验空速、最小转弯半径与相机安装角。
