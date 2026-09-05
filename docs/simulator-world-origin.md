# SIM 世界坐标与确定性飞行模型

状态：`v0.3.0-pre.8`。

确定性 `SimulatorScenarioRunner` 现在会把仓库内 Gazebo 世界的
`<spherical_coordinates>` 作为 fallback flight model 的初始 WGS-84 原点。这样
SIM 的 NavSat、目标落图和 QGC 地图使用同一个地理参考，不会在场景已经位于室外
坐标时偷偷从 `0°, 0°`（Null Island）开始。

## 规则

- `ScenarioRuntimeInput.world.world_asset` 为
  `builtin://gazebo/<scenario>` 且调用方保留旧的 `initial_latitude_deg=0`、
  `initial_longitude_deg=0` 默认值时，runner 解析对应的
  `simulation/gazebo/worlds/<scenario>.sdf`。
- 只有有限、范围合法的 `latitude_deg`、`longitude_deg` 和 `elevation` 才会被采纳；
  解析失败时保留配置坐标，并在 evidence 的 `world_origin.reason` 中记录原因。
- 纬度必须严格位于 `(-90, 90)`；`±90°` 虽然是协议层可表示的 GNSS 值，但在本地
  North/East 切平面和经度方向上是奇异点。runner 配置、SDF 原点读取和 Gazebo v1
  传感器 wire 都在入口处拒绝极点，保持与目标落图/QGC 地图相同的 fail-closed 域。
- 非零 `initial_*` 坐标是显式覆盖；`use_world_origin=False` 是有意使用本地/Null-Island
  fixture 的明确开关。
- 传入完整 `FlightModelConfig` 时，该 config 拥有初始位置，evidence 标记
  `source=model_config`。
- runner 只读取 runtime channel 和 world SDF。`truth.json`、目标位置和 evaluator
  结果仍不会进入感知、AI 或控制路径。

## Flight-model geodesy

`DeterministicFlightModel` 保存控制积分所需的局部 `east_m`/`north_m` 位移，但每次
telemetry snapshot 都通过 `multidetect.target_geolocation.wgs84_forward_from_ned()` 从
场景原点求 WGS-84 endpoint。这样 SIM 与目标落图使用同一椭球直解，在高纬度和跨日期变
更线时不会重新引入固定 `111,320 m/degree` 的平面近似；经度输出统一归一化到
`[-180, 180]`。N/E 位移穿越地理极点时在入口处 fail-closed，不把绕极后的经度翻转点
当作有效本地坐标。

## Evidence

每次 scenario run 的 `evidence.world_origin`（以及顶层 `world_origin` 别名）包含：

```json
{
  "latitude_deg": 1.3,
  "longitude_deg": 103.8005,
  "elevation_m": 5.0,
  "source": "gazebo_world_sdf",
  "resolved": true,
  "world_asset": "builtin://gazebo/river_rescue_vtol_001",
  "world_path": "simulation/gazebo/worlds/river_rescue_vtol_001.sdf"
}
```

`source=runner_config_override`、`runner_config_opt_out`、`model_config` 或
`runner_config_default` 时，`resolved=false`，表示该次运行没有把 SDF 原点当作输入。

## 回归命令

```powershell
Set-Location D:\Projects\Multi-Detect
$env:PYTHONPATH = 'src;.'
$python = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'

& $python -m pytest -q `
  tests\test_simulator_scenario_runner.py `
  tests\test_simulator_stack.py

& $python -m multidetect.simulator scenario `
  --scenario configs\scenarios\river_rescue_vtol_001.scenario.json `
  --frames 600 `
  --out artifacts\evaluation\simulator-scenario-river-origin.json
```

该回归只验证软件/协议和资产坐标绑定；它不等同于 Gazebo 桌面渲染、PX4 SITL
运行、真实 GNSS、Pixhawk/Jetson、HIL/REAL、实飞或人工 QGC Fly View 验收。
