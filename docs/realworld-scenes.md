# 真实地点仿真场景

用公开数据重建真实地点，生成可直接跑 PX4 SITL 的 Gazebo 世界。不改动现有世界和回归。美国地点继续默认使用美国数据源；新增地点可在配置中显式设置 `provider`。

## 世界

文件名是 `simulation/gazebo/worlds/<地点>__<天气>.sdf`，对应场景在 `configs/scenarios/realworld/`（x500 在根目录，其他机型在同名子目录）。世界文件被 `.gitignore` 的 `*__*.sdf` 规则忽略，取出代码后要跑一次 `make_world.py`。

| 地点 | 位置 | 范围 | 特点 |
|---|---|---|---|
| `rw_downtown_sf` | 旧金山市中心 | 2 km | 高楼街区、街道峡谷 |
| `rw_suburb_davis` | 加州戴维斯 | 2 km | 独栋住宅、路边邮箱、校园 |
| `rw_farm_iowa` | 爱荷华 | 3 km | 农田、乡村公路 |
| `rw_mountain_boulder` | 科罗拉多博尔德 | 3 km | 山坡、针叶林、住宅 |
| `rw_coast_santacruz` | 加州圣克鲁斯 | 3 km | 海岸、港口、沙滩 |
| `rw_port_oakland` | 奥克兰港 | 3 km | 货柜场、仓库、铁路 |
| `rw_city_zurich` | 苏黎世老城与湖滨 | 2 km | swisstopo 10 cm 正射、0.5 m DEM |
| `rw_alpine_grindelwald` | 格林德瓦山村 | 2 km | swisstopo 10 cm 正射、0.5 m DEM |
| `rw_port_rotterdam` | 鹿特丹港区 | 2 km | PDOK 航拍、AHN DTM、3D BAG 建筑高度 |
| `rw_canal_amsterdam` | 阿姆斯特丹运河区 | 2 km | PDOK 航拍、AHN DTM、3D BAG 建筑高度 |

天气（天空是 Poly Haven HDRI 贴的无光照穹顶，因为 Gazebo 忽略 `cubemap_uri`）：`clear_noon`、`partly_cloudy`、`morning`、`evening`、`overcast`、`rain`、`dusk`、`night`、`windy`。

## 场景里有什么

- 地形：美国地点使用 USGS 3DEP 2 m；瑞士使用 swissALTI3D 0.5 m；荷兰使用 AHN4 DTM 0.5 m。起飞点 25 m 内压平。视觉网格 4 m，碰撞网格 8 m，所以起飞点外的视觉和碰撞高度会差一点。
- 地面贴图：每块 1 km 存为 4096 px，起飞点 ±300 m 可单独保存 8192 px `core.png`，构建时优先直接使用，不把 1 km 图块放大。影像源分辨率：NAIP 约 0.6 m，SWISSIMAGE 10 cm，荷兰 HR 航拍约 5–8 cm，Sentinel-2 约 10 m。
- 建筑：OSM 轮廓和高度，缺高度时按类型估计。屋顶贴航拍，住宅是人字顶，外墙贴图按真实层高绘制。
- 道路：OSM 路网贴在地形上，带车道线和斑马线。人行道沿用航拍。
- 树：有 NIR 时从航拍 NDVI 和粗糙度找树冠位置；RGB-only 自动使用 VARI/ExG 和粗糙度，每个地点最多约 2500 棵，真实位置但树种是近似的。
- 街道设施：路灯、停车标志、消防栓、邮筒、电线杆，优先用 OSM 位置，其余沿路摆放。住宅区每户路边一个邮箱。
- 动态物体：沿真实道路行驶的车和行走的行人（actor，不占物理计算）。
- 标签：起飞点附近的车、人、邮箱等约 100 个是独立模型，带分割 label；其余合并进地形模型，否则 RTF 会掉到 0.16。

## 东南亚和中国地点（global provider）

已实际下载并构建配置的地点：

| 地点 | 起飞点（WGS84） | 起飞点地表 | 原始分辨率 | 生成栅格/统计 |
|---|---:|---|---|---|
| `rw_city_singapore` | 1.357442, 103.823750 | OSM `landuse=grass` 开阔草地，约 1.42 ha；距离 PX4 默认家点 1.3521, 103.8198 约 0.7 km | Sentinel-2 L2A 约 10 m；Copernicus DEM GLO-30 约 30 m；OSM 矢量 | 影像存储网格约 0.244 m/px，DEM 采样网格 2 m；这些只是重采样网格，不能视作原始精度；统计写入 `scene.json` |
| `rw_city_shenzhen` | 22.523177, 113.942624 | 深圳湾体育中心旁 OSM `leisure=park` 公园地块，约 0.45 ha | Sentinel-2 L2A 约 10 m；Copernicus DEM GLO-30 约 30 m；OSM 矢量 | 同上；统计写入 `scene.json` |

两地都对起飞点 25 m 内地形压平。道路使用 OSM 中心线和宽度程序生成路面、车道线与斑马线；`ground_core` 先按 OSM 的 `landuse`、`leisure=park`、`natural=water/wood/grassland` 分类，再叠加 CC0 ambientCG 细节贴图，Sentinel 只提供低频颜色参考。重复树木和街道设施按已有合并网格逻辑处理，只有附近需要识别的对象保留独立模型。

生成验证时可把输出放进工作树临时目录，并从只读共享缓存查找 Fuel 模型：

```bash
export RW_RESOLVED_ROOT=$PWD/.tmp/realworld-models
export RW_SOURCE_ROOT=$(readlink -f artifacts/cache/gz-fuel/resolved)
$PY scripts/realworld/build_scene.py rw_city_singapore --max-trees 2500
$PY scripts/realworld/build_scene.py rw_city_shenzhen --max-trees 2500
$PY scripts/realworld/make_sky.py
$PY scripts/realworld/make_movers.py
$PY scripts/realworld/make_world.py rw_city_singapore --weather clear_noon --world-out-dir .tmp/worlds --skip-scenarios
```

每个 raw 目录的 `provenance.json` 记录 STAC item、实际选中的资源 URL、原始分辨率和所有下载文件 SHA-256。global DEM 的 `dem.npy` 仍以 2 m 网格保存，避免 Gazebo 地形接口改变；`dem_res_m` 明确记录 30 m 原始分辨率。

### 更高分辨率公开影像核查

- 新加坡附近的 OpenAerialMap STAC 返回一幅 `ODM mapping rainforest`，元数据声明 `CC-BY-4.0`、GSD 约 0.0238 m，来源为 HOTOSM imagery STAC；范围约 350×415 m，仅覆盖 Upper Thomson 一小块，且元数据的生产者/许可方为 `Test / Andreas Zwanenburg`。来源记录：`https://api.imagery.hotosm.org/stac/collections/openaerialmap/items/64a6d83764adbc00012e09a8`。它没有覆盖本项目 2 km 场景完整范围，因此未拼接进默认地面纹理；若将场景缩小到该覆盖范围，应保留 CC BY 署名和原始文件哈希。
- 深圳湾同一 STAC 范围搜索未返回许可证明确的可再用正射影像，因此没有使用来源不明的地图瓦片、天地图服务或需要账号/受限机器访问的影像。天地图等中国在线地图服务的再分发、批量机器访问和离线缓存条款未能核实为本项目所需的自由再使用许可，故仅使用 Copernicus、OSM 和程序几何。
- Sentinel-2 L2A 与 Copernicus DEM GLO-30 都可按 Copernicus free and open data policy 使用，但 10 m/30 m 的原始分辨率决定了低空无法辨认车辆、行人和小型设施；最应补充的是覆盖完整场景、明确 CC BY/CC0 许可的亚米级正射影像，优先是新加坡政府或 HOTOSM 发布的完整航测块，以及深圳市公开许可的航测数据。

## 已知限制

- 树和街道设施没有碰撞体，建筑、货柜和地形有。
- 雨是静止的半透明线条网格：粒子系统要加 world `<plugin>`，而这会替换掉默认系统，世界起不来。要真粒子得改容器启动补丁。雾沿用现有相机增强方案，因为 Ogre2 不支持 SDF 雾。
- 航拍自带车辆和阴影，会和 3D 物体、实时光照叠在一起，贴近地面时能看出来。
- 货柜位置只按航拍里的货场区域估计，不是逐个对应。
- 大世界加载到第一帧要 40~60 秒，跑采集和飞行要设 `MULTIDETECT_SIM_FIRST_FRAME_WAIT_S=240`。
- `sim_teacher` 的障碍物读取只认 `bounds` 盒子，看不到合并进地形的建筑；建筑范围在 `scene.json` 的 `footprints`。
- `global` 使用 Sentinel-2 L2A（约 10 m）和 Copernicus DEM GLO-30；仅适合宏观环境，贴近地面无法辨认车辆、行人或小型设施。按云量挑选单景，不保证无云拼接。

## 重新生成

大文件不进 git。原始数据和资源库在 `/data/datasets/multidetect-sim-assets/`，默认模型输出在 `artifacts/cache/gz-fuel/resolved/rw_*`，每个地点约 0.9 GB。只做离线构建验证时，可设置 `RW_MODEL_ROOT=$PWD/.tmp/realworld-models` 将模型输出留在工作区临时目录。

```bash
PY=/data/datasets/multidetect-sim-assets/tools/venv/bin/python
$PY scripts/realworld/fetch_site.py rw_suburb_davis
$PY scripts/realworld/fetch_library.py
$PY scripts/realworld/make_facades.py
$PY scripts/realworld/make_sky.py
$PY scripts/realworld/make_movers.py
$PY scripts/realworld/make_rain.py
$PY scripts/realworld/build_scene.py rw_suburb_davis
$PY scripts/realworld/make_world.py rw_suburb_davis
```

新增地点时在 `configs/sim-assets/realworld-sites.json` 加一条；`provider` 可选 `us`、`swiss`、`nl`、`global`，省略时仍走美国流程。来源 URL、产品年份、分辨率、许可证和署名记录在每个 raw 目录的 `provenance.json`。检查和飞行：

```bash
$PY scripts/realworld/verify_prop_spin.py
$PY scripts/realworld/shoot_scene.py simulation/gazebo/worlds/rw_suburb_davis__clear_noon.sdf --out .tmp/shots/x --cam top:0,-1,60:0,0,0
```

`scripts/realworld/leased_flight.py <槽位> <机型> <世界> <输出目录>` 在租用的 SITL 槽位里跑冒烟飞行，同时记录飞行中的螺旋桨转速。槽位 0-2 归 r15，3-4 归三角翼循环，这里用 5 以上，需设 `MULTIDETECT_SITL_SLOTS=8`。

## 来源和许可证

| 数据 | 来源 | 许可证 |
|---|---|---|
| 地形 | USGS 3DEP | 公有领域 |
| 航拍 | USDA NAIP（Microsoft Planetary Computer 镜像） | 公有领域 |
| 瑞士地形/航拍 | swisstopo swissALTI3D / SWISSIMAGE STAC | swisstopo Open Government Data 条款；署名 © swisstopo。当前 STAC collection `license` 字段仍标为 proprietary，使用时需保留署名并核对产品条款 |
| 荷兰地形 | PDOK AHN4 DTM | CC0-1.0；2020–2022 测绘 |
| 荷兰航拍 | PDOK Luchtfoto `Actueel_orthoHR` | CC BY 4.0；注明 Beeldmateriaal Nederland / Kadaster |
| 荷兰建筑高度 | 3D BAG `pand` | CC BY 4.0；署名 © 3DBAG by tudelft3d and 3DGI；产品版本 2023-10-08 |
| 全球影像/地形 | Sentinel-2 L2A / Copernicus DEM GLO-30（Microsoft Planetary Computer STAC） | Copernicus free and open data；所选影像日期和云量应写入 provenance |
| 建筑、道路、设施位置 | OpenStreetMap，经 Overpass | ODbL，需署名 © OpenStreetMap contributors，公开发布衍生数据库要同样授权 |
| 模型 | Gazebo Fuel | CC0 或 CC BY 4.0，需署名，逐项见清单 |
| 贴图 | ambientCG | CC0 |
| 天空和模型 | Poly Haven | CC0 |

每项下载的 URL、许可证和 SHA-256 在 `configs/sim-assets/realworld-library.json`。Fuel 只保留了可再使用的许可证，排除了 NC 和 ND。

## 付费资源（未购买，价格是 2026-11 查到的公开报价，下单前需向厂商确认）

| 资源 | 价格 | 用途和注意 |
|---|---|---|
| 30~49 cm 卫星影像（SkyWatch） | 存档 $19/km²，新拍 $29/km²，最小 1 km² | 高分辨率公开源未覆盖的地区。一个 2 km 场景约 $76 |
| Vantor（原 Maxar）30 cm | 经销商报价，存档约 $22~55/km²，常见最小 25 km² | 同上，25 km² 约 $550~1400 |
| Cesium ion Commercial | $149/月（个人），$524/月（团队） | 3D Tiles 托管和处理 |
| Google Photorealistic 3D Tiles | 论坛报告约 $6/千次根请求，未核实 | 不建议：论坛称条款禁止缓存、离线和训练机器学习，需核对原条款 |

## 验证结果（2026-11-06）

螺旋桨：`verify_prop_spin.py` 不接 PX4，直接发电机指令。x500、x500_gimbal、rc_cessna、standard_vtol、tiltrotor、quadtailsitter、advanced_plane 和三角翼两版全部通过：每个转子有转速，相机画面也有变化。飞行中的转速记录在每次飞行的 `rotors_in_flight.json`，rc_cessna、x500_gimbal 和三角翼没有飞过这些世界。

PX4 冒烟飞行，机器负载约 200，实际 RTF 约 0.2~0.3（现有世界的 VTOL 基线同样慢）：

| 机型 | 世界 | 结果 |
|---|---|---|
| standard_vtol | 戴维斯 clear_noon | 通过 |
| standard_vtol | 博尔德 partly_cloudy | 通过 |
| quadtailsitter | 爱荷华 clear_noon | 通过 |
| tiltrotor | 圣克鲁斯 overcast | 通过 |
| advanced_plane | 爱荷华 morning | 飞完并自动着陆，之后的 land 命令报错，判失败 |
| x500 | 爱荷华、戴维斯 | 起飞、重定位（误差约 0.5 m）、降落都正常；冒烟门禁是给固定翼/VTOL 写的（要求 RTL 和盘旋空速），判失败 |
| standard_vtol | 爱荷华 | 3 次都失败：2 次起飞前 `height estimate not stable`，1 次飞行中相机流 15 s 无帧 |

未解决：
- 爱荷华世界的 VTOL 解锁失败，原因未查清。同一世界 quadtailsitter 通过，海拔 318 m 不像主因。
- 约 10 次飞行里有 2 次相机流 15 s 无帧（x500 戴维斯 1 次，VTOL 爱荷华 1 次）。不确定来自世界还是整机负载，项目里也在查卡顿。

## 接下来

重启后不用恢复什么：下载都已完成，数据在 `/data`，没有常驻服务。

1. 查爱荷华 VTOL 的高度估计问题和相机流卡顿。
2. 三角翼分支合并后跑 `delta_tailsitter_*` 飞行。
3. 把 `library` 里还没用上的模型接进场景（目前世界只用了约 20 种 Fuel 模型）。
4. 贴地视角的细节：路面沿用航拍的地方偏糊，正午屋顶偏亮。
5. 美国以外的地点需要付费影像，见上面的清单。
