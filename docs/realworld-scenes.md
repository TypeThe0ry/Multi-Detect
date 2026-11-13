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
| `rw_city_tokyo` | 东京台东区 | 2 km | GSI 影像缓存、OSM 建筑；PLATEAU 坐标变换未完成，暂不装入其 glTF |
| `rw_city_berlin` | 柏林 Tiergarten 周边 | 2 km | Berlin LoD2 CityGML 缓存、DOP 影像缓存；墙面为正射投影回退 |
| `rw_city_singapore` | 新加坡 Upper Thomson | 2 km | Sentinel-2、Copernicus DEM、OSM 分类地面 |
| `rw_city_shenzhen` | 深圳湾体育中心旁公园 | 2 km | Sentinel-2、Copernicus DEM、OSM 分类地面 |

天气（天空是 Poly Haven HDRI 贴的无光照穹顶，因为 Gazebo 忽略 `cubemap_uri`）：`clear_noon`、`partly_cloudy`、`morning`、`evening`、`overcast`、`rain`、`dusk`、`night`、`windy`。

## 场景里有什么

- 地形：美国地点使用 USGS 3DEP 2 m；瑞士使用 swissALTI3D 0.5 m；荷兰使用 AHN4 DTM 0.5 m。起飞点 25 m 内压平。视觉网格 4 m，碰撞网格 8 m；DEM 采样按实际网格间距读取。
- 地面贴图：每块 1 km 用 4096 px，起飞点 ±300 m 内优先使用 8192 px `core.png`，没有 `core.png` 时从图块拼接；美国 NAIP 约 0.6 m，SWISSIMAGE 10 cm，荷兰 HR 航拍约 5–8 cm，Sentinel-2 约 10 m。渲染总显存预算为 2 GiB；生成统计分别报告解码贴图（含 mipmap）和网格缓冲估算，截图另测容器内进程的显存。
- 建筑：OSM 轮廓和高度，荷兰有 3D BAG 高度时优先使用；缺高度时按类型估计。屋顶贴航拍，平屋顶带女儿墙和按面积布置的空调箱/水箱，住宅是人字顶，外墙贴图按真实层高绘制。
- 道路：OSM 路网贴在地形上，路面颜色按航拍路域像素中位数匹配，带车道线和斑马线；人行道沿用航拍。桥按 OSM 层级抬高，水面覆盖带法线贴图的暗青材质并以 0.55 透明度透出近岸颜色。
- 树：有 NIR 时从航拍 NDVI 和粗糙度找树冠；RGB-only 自动使用 VARI/ExG 和粗糙度。每个地点最多约 2500 棵，真实位置但树种是近似的；重复植被和道具合并进网格，模型/贴图/三角形预算写入 `scene.json`。
- 街道设施：路灯、停车标志、消防栓、邮筒、电线杆，优先用 OSM 位置，其余沿路摆放。住宅区每户路边一个邮箱。
- 动态物体：沿真实道路行驶的车和行走的行人（actor，不占物理计算）。
- 识别对象：起飞点附近的车、人、邮箱等约 100 个是独立模型；其余合并进地形模型，否则 RTF 会掉到 0.16。本线世界不插入 Label 插件，独立对象名称和 bounds 不等于 Gazebo 分割标签已生效。

## 东南亚和中国地点（global provider）

配置的地点如下。下载、构建和实测状态以文末 2026-11-07 验证记录为准：

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
- 本次大世界加载到第一帧约 41~122 秒（第二轮默认四相机实测，随整机负载变化），跑采集和飞行要设 `MULTIDETECT_SIM_FIRST_FRAME_WAIT_S=240`。
- `sim_teacher` 的障碍物读取只认 `bounds` 盒子，看不到合并进地形的建筑；建筑范围在 `scene.json` 的 `footprints`。
- `global` 使用 Sentinel-2 L2A（约 10 m）和 Copernicus DEM GLO-30；仅适合宏观环境，贴近地面无法辨认车辆、行人或小型设施。按云量挑选单景，不保证无云拼接。

## 重新生成

大文件不进 git。原始数据和资源库在 `/data/datasets/multidetect-sim-assets/`，默认模型输出在 `artifacts/cache/gz-fuel/resolved/rw_*`，每个地点约 0.9 GB。只做离线构建验证时，可设置 `RW_RESOLVED_ROOT=$PWD/.tmp/realworld-models RW_SCENE_ROOT=$PWD/.tmp/realworld-scenes`；兼容旧脚本的 `RW_MODEL_ROOT` 和 `REALWORLD_OUTPUT_ROOT` 也可用。

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

新增地点时在 `configs/sim-assets/realworld-sites.json` 加一条；`provider` 可选 `us`、`swiss`、`nl`、`global`、`plateau`、`berlin`，省略时仍走美国流程。来源 URL、产品年份、分辨率、许可证和署名记录在每个 raw 目录的 `provenance.json`。检查和飞行：

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
| 东京城市索引 | PLATEAU 台东区 3D Tiles | PLATEAU 利用规约，需出典表示；当前未启用其几何 |
| 东京影像缓存 | GSI seamlessphoto | 国土地理院内容利用规约，需出典表示；逐瓦片来源见 raw/imagery_downloads.jsonl |
| 柏林城市模型和影像缓存 | Berlin LoD2 / DOP20 | Datenlizenz Deutschland Zero 2.0；保留 Senatsverwaltung 来源记录 |
| 模型 | Gazebo Fuel | CC0 或 CC BY 4.0，需署名，逐项见清单；动态船为 RC Boat（CC BY 4.0） |
| 贴图 | ambientCG | CC0 |
| 天空和模型 | Poly Haven | CC0 |

每项下载的 URL、许可证和 SHA-256 在 `configs/sim-assets/realworld-library.json`。Fuel 只保留了可再使用的许可证，排除了 NC 和 ND。

道具白名单见 `configs/sim-assets/realworld-asset-sets.json`。未列入白名单的商品扫描、玩具、鞋、明显品牌车辆和影视版权物不会被实例化。Poly Haven 的 CC0 模型支持 glTF/glb 读取；高复杂度网格按类别预算抽样，透明叶片保留透明材质并抽稀，贴图最长边缩至 768 px。树目标约 1000 三角形，所有道具合计上限 4,000,000 三角形；`scene.json` 分开记录地形贴图预算和道具贴图字节数。模型处理依赖 realworld 工具 venv 中的 `trimesh`、`pycollada` 等库；没有登录或注册第三方资源站点。

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

## 历史后续计划（2026-11-06，当前状态见下一节）

重启后不用恢复什么：下载都已完成，数据在 `/data`，没有常驻服务。

1. 查爱荷华 VTOL 的高度估计问题和相机流卡顿。
2. 三角翼分支合并后跑 `delta_tailsitter_*` 飞行。
3. 把 `library` 里还没用上的模型接进场景（目前世界只用了约 20 种 Fuel 模型）。
4. 贴地视角的细节：路面沿用航拍的地方偏糊，正午屋顶偏亮。
5. 美国以外的地点需要付费影像，见上面的清单。

## 合并与验证记录（2026-11-07）

本次合入 `wt/cx-rw-life`、`wt/cx-rw-3dcity`、`wt/cx-rw-global`。global 分支是中断的 WIP：保留站点配置、Sentinel/Copernicus 来源信息与 OSM 地表分类，修复分类贴图核心范围错误后重新构建。东京原有 glTF 导入未应用 3D Tiles 层级变换、也未保留纹理 UV，不能作为可信地理定位模型使用；现在仅保存索引，构建明确使用 OSM 回退。柏林保留已缓存 CityGML，凹面在表面平面内三角化，视觉和碰撞使用同一表面，不再用整栋 AABB 填满庭院；正射影像墙面不等于实拍立面。

### 数据、依赖与资源约束

- 工具 Python：`/data/datasets/multidetect-sim-assets/tools/venv/bin/python`；已有依赖包含 numpy、scipy、Pillow、mapbox-earcut、shapely、pyproj、rasterio、pystac-client、planetary-computer、trimesh、pycollada、DracoPy、OpenCV。本次没有新增 Python 依赖。
- 新加坡共享 raw 在构建期间被发现原点是 `1.281614,103.854207`，与配置 `1.357442,103.823750` 不符。为避免覆盖别处正在使用的数据，本次通过 `fetch_site.py --raw-root /data/datasets/multidetect-sim-assets/validation-1107/sites` 重新下载匹配数据。原始文件 SHA-256、实际 STAC URL、许可证写入其 `raw/provenance.json`；OSM 为 ODbL，Sentinel 和 Copernicus 为开放数据。`build_scene.py --raw-root`、`scene.json.raw_dir` 让构建、世界和相机高度采样使用同一缓存；旧缓存原点不符时构建直接报错。
- `fetch_city_imagery()` 当前在线实现实际使用 Sentinel 回退，因此 provenance 明写 Sentinel，不冒充 GSI/Berlin DOP。现有东京/柏林高分影像来自合并前缓存，其逐瓦片记录保留在 `raw/imagery_downloads.jsonl`，需要完整官方下载器才能从空缓存复现同等质量。
- 贴图按 RGBA8 加 mipmap 的 `宽×高×4×4/3` 估算；旧统计错误地按浮点数多算约三倍。网格和贴图都不能仅以压缩文件大小衡量显存，2 GiB 是渲染总预算，须同时预留天空、相机、阴影、顶点/索引缓冲与驱动开销。模型统计会报告贴图和三角形数；实际运行是否越界仍以 GPU 测量为准。
- 并行构建的 glTF 临时贴图现在按 PID 隔离，防止读取另一个构建正在写的 PNG；重复道具归一化为水平居中、底部为零，摆放时只加一次地面高度。围墙按 2 m 加密采样地形。铁路偏移支持 MultiLineString。
- 独立道具仍保留 bounds；本线世界不插入 Label 插件，因此不能据独立模型名称声称 Gazebo 分割标签已经生效。船只有在 OSM 水域内收缩 8 m 后可容纳至少 80 m 航段时生成。

### 两轮渲染检查

所有图片、运行日志和指标保存在工作树 `.tmp/review/`，不进 git。首轮与第二轮分别在 `round1/`、`round2/`；交付拼图在 `final/`。截图相机为高空斜视、中空 45 m、起飞点俯视 40 m、低空 5 m；低空位置需落在实际道路，不能把田地镜头称为街景。`shoot_scene.py` 仅允许 GPU0/1，记录每个相机首帧秒数、稳定后 RTF 样本；容器名唯一，清理只限本次容器。

首轮已经目检的发现：

| 地点 | 观察 | 修改或限制 |
|---|---|---|
| SF | 起飞区目视空，但建筑阴面偏暗、树稀疏；飞行被粗障碍盒挡住 | 正午增加环境光；辅助盒避开起飞区，建筑原有碰撞保留 |
| Davis | 起飞区硬地可见，低空模糊，航拍车辆/树影与物体叠影 | 归一化和贴图修正后复拍；叠影是源航拍限制 |
| Iowa | 起飞区为开阔农田，没有楼遮挡；固定低空镜头不是街道 | 第二轮应使用真实道路镜头；不能把背景平滑当作高精细重建 |
| Boulder | 草地/停车场旁有长条灰墙，车辆疑似悬浮 | 围墙加密贴地；去掉道具重复高度补偿后复拍 |
| Santa Cruz | 码头和水域可辨，岸边航拍阴影与几何叠影明显 | 船路线改为水域内部；航拍中的船未逐个做几何 |
| Oakland | 起飞区可见，货柜分布与航拍不逐个一致，低空偏糊 | 货柜仍为估计分布，不能称为真实逐箱复原 |
| Zurich | 河道可见；桥/路面有层级交叠，路边车辆堆叠，岸边长墙明显 | 路线不再跨街闭合；道具居中与高度修正后复拍，桥层级继续核查 |

第二轮完整目检见后文，生成成功不等于目视通过。

### 飞行与相机问题

`x500_gimbal` 原始图像中 gimbal RGB 是 FOV 2.0、1280×720，冒烟按 FOV 1.74、1280×960 标定验证，采集器只有启用 `--segmentation` 才挂载已存在的校准 gimbal SDF。冒烟现在为该机型显式挂校准 overlay，没有放宽 CameraInfo 断言。`gimbal-fixed` 已通过完整起飞、重定位、降落，7 帧对齐；证据 `.tmp/flights/gimbal-fixed.log`、同名目录 `episodes/*/steps.jsonl` 与 `rotors_in_flight.json`。

`standard_vtol` 在 Zurich 的 `vtol-zurich` 已通过，7 帧对齐并降落；Iowa 的 `r1b-vtol-farm` 仍在解锁前报告 `height estimate not stable`，日志有 gyro/accel duplicate timestamp。需要独立运行的同机型、同设置 A/B 才能归因，不能仅凭并发时失败认定相机是根因。

SF 和 Berlin 的 x500 首次回归均在 `takeoff_reached` 超时，高度约 0.3 m 以内。SF 的 L 形楼粗 AABB 填满了真实轮廓中空出的起飞区；Berlin 首轮文件也包含覆盖原点的粗盒。辅助障碍盒现在避开起飞区，柏林城市碰撞使用实际表面，重跑结果单独记录。


后续首轮目检：鹿特丹发现货柜和树落在 OSM 水域上，已把水域及 10 m 岸线缓冲加入货柜禁放区，植被同样避水；其高分 core 和粗地形交叠产生矩形贴图块，细 core 现在替换对应粗地形三角形。阿姆斯特丹公园起飞区开阔，树影叠影和低空模糊仍明显。东京道路起伏、屋顶航拍拉伸明显，GLO-30 城市表面高程并非地面真值。柏林首轮大部分 LoD2 建筑被表面高程覆盖，远处局部悬浮：城市模型地面表面高程约 28–39 m，DEM 为 27–58 m；每栋建筑现在按其 GroundSurface 与本地地形的中位差整体对齐，这是近似修正，不是 DTM 精度提升，须复拍核对。


### 本次测试和飞行证据口径

- 地点测试：realworld venv 中 `tests/test_realworld_site_providers.py` 最新 9 项通过；主环境中冒烟、固定翼降落、gimbal overlay 相关测试共 44 项通过。全部 realworld 脚本及冒烟脚本 `py_compile` 通过；CPU-only onboard gate 于本次修改后通过（102 s）。日志：`.tmp/rw-providers-final3.log`、`.tmp/rw-flight-tests-final3.log`、`.tmp/onboard-gate-final2.log`。
- 冒烟起飞判据修正：`episode.takeoff_altitude_m` 是请求高度，不能充当实际爬升证据；现在只用 `LOCAL_POSITION_NED` 判断。新增低高度遥测回归用例，防止被障碍卡住的飞机被记录为已起飞。
- 当前已完成并通过的本次飞行：`gimbal-fixed`（Davis）、`vtol-zurich`（Zurich）、`tilt-rotterdam`（Rotterdam）、`cessna-farm`（Iowa）。其他排队飞行以各自最终报告为准。
- `.tmp/review/final/rotors-in-flight.csv` 只保留与遥测墙钟差小于 3 s、实测高度超过 2 m 的转子采样；转速来自 Gazebo rotor link 姿态差，是可见转子的仿真角速度，受采样混叠和 `rotorVelocitySlowdownSim` 影响，不是真实电机 RPM。采样不能证明采样间隔内连续转动。
- `vtol-zurich` 的 puller 在多旋翼阶段为零，此时 SERVO_OUTPUT_RAW 的第五路也为零；`tilt-rotterdam` 在 vtol_state=4 时 rotor_1/3 为零，第二/四路输出同为零，其他阶段恢复转动。不能把按设计停用的转子列为损坏，也不能声称每个转子在所有飞行阶段都转。
- 截图指标另记录 `rtf_sim_wall`（稳定采样窗口的仿真时间增量/墙钟增量）、瞬时 RTF 中位数、样本数、各相机首帧和本容器进程显存峰值。截图是 1280×960、1 Hz，飞行机载相机为 30 Hz；两者的 RTF 不应混用。显存每 2 s 采样，可能漏掉更短暂的峰值。

### 柏林第二轮提前检查与资源对照

已实际打开 `.tmp/review/round2/rw_city_berlin/sheet.jpg`：街道随 DSM 呈明显波浪，远处大量城市建筑只有屋顶、缺少竖墙，起飞点是人为清空/压平后的纪念碑区域，不能称为未改动的真实开阔地。四视角首帧为 83–84 s，200 样本的 `rtf_sim_wall=0.689`、瞬时中位数 0.785，采样时 loadavg≈146；本容器显存采样峰值 3598 MiB，超过 2 GiB，未通过预算。

缩小贴图的独立对照保存在 `.tmp/review/budget-berlin/`，模型为 `rw_city_berlin_budget`，也已打开拼图。核心贴图最长边从 8192 降至 4096，其余大贴图降至 2048，显存峰值仍有 3048 MiB；不能只缩贴图就宣称满足预算，未将这一试验替换默认产物。

缺墙已定位到 CityGML 读取器：竖墙投影为 XY 线段后，原 `Polygon.buffer(0)` 范围过滤把它丢弃；旧代码还只读一个 Polygon，父 Building 又重复包含 BuildingPart。现在按每个 Polygon 的外环/内环读取，竖墙按其投影凸包与范围相交判断，子 BuildingPart 单独生成。新增竖墙、多 Polygon、嵌套 BuildingPart 测试，地点测试现为 8 项通过（`.tmp/rw-providers-final5.log`）。原始柏林缓存保持不变，本次从已有 9 个 LoD2 ZIP 离线解析到 `/data/datasets/multidetect-sim-assets/validation-1107/sites/rw_city_berlin/raw/`，归一化记录、原 ZIP SHA-256 和输出 SHA-256 写入该目录 provenance；修复后已经构建并复拍，实际观察见后文第二轮表。

独立 Fuel 道具出现 `hatchback.png`、`fire_hydrant_diffuse.png` 等路径无法解析。`make_world.py` 现把独立源模型复制到 `rw_prop_<slug>`，在副本中修正 MTL/DAE 的相对贴图路径；部分车辆缺少的共用车轮贴图仅从已有本地模型缓存复制，不下载、不修改原模型。跨平台文件测试确认源 MTL 保持不变。此修复须以之后的渲染日志和图片核实。


道具包装首轮实渲染在 Grindelwald 暴露 COLLADA 加载器兼容问题：ElementTree 默认把根标签写为 `ns0:COLLADA`，Gazebo 报 `Missing COLLADA tag` 后崩溃。包装器现在注册默认命名空间，保留无前缀 `<COLLADA xmlns=...>`；回归测试同时检查根标签和纹理路径，8 项通过（`.tmp/rw-providers-final6.log`）。失败截图目录不能算渲染通过，已重新排队拍摄。

补齐首轮目检：Grindelwald 的四视角已打开，村落与山坡可辨，但建筑底部与航拍地表错层明显，局部像压在坡上或悬空，树影叠影仍在；新加坡起飞区为草地，森林区几乎只有程序地面，远处孤立建筑和粗高程明显，四相机显存峰值 3223 MiB；深圳起飞草地开阔，高楼轮廓可辨，但地表与屋顶被 Sentinel 低分颜色拉成大片色块，立面为程序近似，四相机显存峰值 3977 MiB。以上只能证明已经渲染并检查，不能判为接近真实世界或达到 2 GiB 预算。

默认 14 站×9 天气共 126 个世界已生成，`.tmp/review/final/world-audit.json` 检查没有 world `<plugin>`/`<include>`、没有网络 URI、本地资源引用存在。模型侧检查见 `resource-audit.json`，当前 14 站引用检查无缺失。该静态检查不证明网格内部贴图、画面或飞行正确。

柏林墙面修复后的 `x500-berlin-padfix` 已完整通过，实测最高 35.49 m，7 帧对齐并降落；飞行中仍记录 `Accel #0 fail: TIMEOUT`，因此该症状不只在 Iowa 出现，尚不能据此定位唯一原因。`plane-farm`（advanced_plane）也已完整通过，最高 40.83 m、7 帧对齐、返航并降落。最终 JSON 摘要在 `.tmp/review/final/flight-results.json`；原始遥测和报告以 `.tmp/flights/<标签>/` 及同名日志为准。

第二轮已打开 SF、Davis、Iowa、Boulder、Santa Cruz、Oakland 四视角：SF 街景有静态 T 姿势人物，楼间阴面仍暗；Davis 的车辆/树影叠影仍明显；Iowa 改为真实道路低空位置，开阔起飞点无楼/水遮挡；Boulder 长条围墙仍明显，2 m 贴地加密不足以改善外观；Santa Cruz 地面中的航拍船仍是平面，岸线/路面局部呈波浪；Oakland 货柜仍是估计分布，起飞区开阔但低空航拍拉伸明显。不能把生成和目检完成等同于视觉无问题。

`vtol-davis-control` 已完整通过，7 帧对齐并降落；本次 standard_vtol 在 Zurich、Davis 两个地点通过。`vtol-farm-alone` 则解锁前失败，日志显示 Gyro 数据缺失、健康检查拒绝，与此前 height estimate 失败表述不同；仅减少本线飞行并发未解决问题。整机其他 lane 持续运行，本线所谓串行并不代表整机独占。`vtol-farm-renderlite` 正在以保留物理/碰撞、移除场景 visual/actor、保留机载相机的方式做对照，不能提前下结论。

### 第二轮完整目检与新增修正

2026-11-07 已实际打开全部 14 站的第二轮四视角拼图，不把保存成功视作视觉验收。除前述六个美国站点外：

| 地点 | 第二轮实际观察 | 结论与处理 |
|---|---|---|
| Zurich | 桥、轨道、道路多层交叠；车辆挤在路边，岸边长墙突兀 | 道路/桥高程与道路设施摆放仍不合格，保留问题证据 |
| Grindelwald | 坡地村落可辨；房底与航拍地面错层、树影叠影 | 现有贴地近似不足，需地面高程和建筑基底联合处理 |
| Rotterdam（旧原点） | 水上货柜明显减少，但核心贴图边界仍呈矩形；俯视起飞点是船坞/船体影像 | OSM 水域包含原点，旧 tiltrotor 飞行 PASS 不构成起飞点合理性验证；正在移到西 225 m、北 25 m 的陆地硬地，并重新取数/构建/拍摄/飞行 |
| Amsterdam | 公园起飞区开阔；航拍的树影、人物和实景几何叠影；街景有车辆但立面重复 | 起飞点未见楼/水遮挡，低空真实感仍有限 |
| Tokyo | 起飞俯视是道路及低分地表，街道明显随 DSM 起伏，局部道路裂缝/交叠 | 公园名称不能代表真实空地；PLATEAU 尚未接入，不能作为真实街景验收 |
| Berlin（墙面修复后） | 建筑竖墙已出现，缺墙修复生效；墙面纹理严重竖向拉伸，道路波浪、低空人物局部悬浮 | 仍是正射墙面回退和粗 DSM 近似；纪念碑附近被程序清空，不能称为完整保真 |
| Singapore | 草地起飞区开阔；森林仅大面积程序纹理，道路/楼体重复，远处粗高程明显 | 全球数据只适合宏观场景；不能称为森林重建或低空高保真 |
| Shenzhen | 草地开阔，高楼轮廓可见；贴图色块、屋顶拉伸、稀疏植被及程序立面明显 | 原始 10 m 影像/30 m 高程限制未解除 |

新增构建防线：25 m 起飞盘与 OSM 水域相交时明确失败，要求重新选点和获取匹配 raw，不通过删除水域来伪造陆地。`tests/test_realworld_site_providers.py` 增加水上原点、岸线过近和安全陆地三种情况，现 9 项通过（`.tmp/rw-providers-final7.log`）。鹿特丹旧原点证据保留在 `.tmp/review/rotterdam-old-water-origin/`，候选陆地航拍在 `.tmp/review/rotterdam-launch-candidates.jpg`。

### Iowa 渲染负载对照

`vtol-farm-renderlite` 已 PASS：最高 34.24 m，7 帧对齐，完成转场/返航/降落。该世界只删除场景 `visual` 和 `actor`，物理、碰撞、原点与机载相机配置保持一致；移除同样标签后的完整世界 XML 与对照世界 XML 的 SHA-256 完全一致，证据 `.tmp/review/final/iowa-control-equivalence.json`。这支持场景视觉/actor 负载参与问题，但不是唯一根因证明：整机仍有其他 lane，未控制 CPU/GPU 全局负载，也未把 actor 更新、渲染和传感器调度分别隔离。完整视觉世界另做一次串行复验，结果随后记录。

已有两个完整视觉世界失败分别留下高度估计不稳、重复 IMU 时间戳与 Gyro 数据缺失证据；柏林 x500 成功飞行也出现 Accel TIMEOUT。因此不能把问题简单归为 Iowa 海拔、某机型完全不能飞，或只增加解锁等待即可修复。此次没有关闭 PX4 健康检查，也没有放宽飞行判据。


<!-- rw-final-measurements:start -->
### 最终测量表（2026-11-07）

下表为 GPU1、clear_noon、四个 1280×960/1 Hz 截图相机、稳定后 200 个 stats 样本的实测。RTF 用采样窗口仿真时间/墙钟时间，不用瞬时中位数代替；首帧为四相机最早—最晚。整机负载各次不同，这些数据不能替代 30 Hz 飞行相机性能或整机独占基准。峰值每 2 s 采样，只计本容器进程。

| 地点 | RTF | 首帧/s | 峰值/MiB | loadavg(1 min) |
|---|---:|---:|---:|---:|
| `rw_downtown_sf` | 0.583 | 122–122 | 3981 | 171.6 |
| `rw_suburb_davis` | 0.940 | 52–53 | 3672 | 180.7 |
| `rw_farm_iowa` | 0.917 | 55–55 | 3429 | 163.3 |
| `rw_mountain_boulder` | 0.969 | 42–43 | 3754 | 110.9 |
| `rw_coast_santacruz` | 0.615 | 107–107 | 4155 | 87.6 |
| `rw_port_oakland` | 0.899 | 46–46 | 3615 | 103.8 |
| `rw_city_zurich` | 0.648 | 100–100 | 3992 | 157.4 |
| `rw_alpine_grindelwald` | 0.762 | 70–71 | 2893 | 109.2 |
| `rw_port_rotterdam` | 0.968 | 46–47 | 3529 | 139.6 |
| `rw_canal_amsterdam` | 0.629 | 112–112 | 4230 | 137.4 |
| `rw_city_tokyo` | 0.926 | 52–52 | 3930 | 133.1 |
| `rw_city_berlin` | 0.979 | 44–45 | 3710 | 117.7 |
| `rw_city_singapore` | 0.698 | 75–75 | 3218 | 71.1 |
| `rw_city_shenzhen` | 0.960 | 41–41 | 4366 | 89.8 |

全部默认四相机样本的显存超过 2048 MiB，2 GiB 预算未通过；部分站点 RTF 低于 0.9。不能把程序估算、单相机预算或缩小贴图试验写成全站达标。原始指标 `.tmp/review/round2/<site>/metrics.json`，汇总 `.tmp/review/final/scene-metrics.csv`。鹿特丹迁移完成后本表改用新原点复拍数据；旧原点数据另存 `.tmp/review/rotterdam-old-water-origin/`。

### 本次飞行最终结果

每条 PASS 均包含实测爬升、7 帧对齐和落地。高度是 LOCAL_POSITION_NED 的最大相对原点高度，地形不同会影响该口径。

| 标签 | 机型 / 地点 | 结果 | 最高相对高度/m | 对齐帧 |
|---|---|---|---:|---:|
| `tilt-rotterdam-land-slot7` | tiltrotor / Rotterdam 新陆地原点槽7 | PASS | 54.24 | 7 |
| `tilt-davis-final` | tiltrotor / Davis | FAIL | 0.00 | 0 |
| `x500-sf-texturefix` | x500 / SF | PASS | 39.83 | 7 |
| `x500-berlin-padfix` | x500 / Berlin | PASS | 35.49 | 7 |
| `vtol-zurich` | standard_vtol / Zurich | PASS | 42.60 | 7 |
| `vtol-davis-control` | standard_vtol / Davis | PASS | 31.07 | 7 |
| `qts-singapore-texturefix` | quadtailsitter / Singapore | PASS | 73.03 | 7 |
| `tilt-rotterdam` | tiltrotor / Rotterdam 旧水上原点（设计无效） | PASS | 53.93 | 7 |
| `tilt-rotterdam-land` | tiltrotor / Rotterdam 新陆地原点 | FAIL | 0.07 | 0 |
| `plane-farm` | advanced_plane / Iowa | PASS | 40.83 | 7 |
| `cessna-farm` | rc_cessna / Iowa | PASS | 35.90 | 7 |
| `gimbal-fixed` | x500_gimbal / Davis | PASS | 15.81 | 7 |
| `vtol-farm-renderlite` | standard_vtol / Iowa 减视觉对照 | PASS | 34.24 | 7 |
| `vtol-farm-full-repeat` | standard_vtol / Iowa 完整视觉复验 | FAIL | 0.37 | 0 |

成功记录覆盖六个指定机型及 x500_gimbal；x500、standard_vtol 各在两个不同地点通过。失败和重试没有删除，完整列表 `.tmp/review/final/flight-results.json`，原始报告/遥测/相机帧 `.tmp/flights/<tag>/` 与同名日志。旧 SF/Berlin 被障碍盒卡住的记录来自起飞判据修复之前，其中 takeoff=true 是旧误判，应以实测不足 0.3 m 和最终 FAIL 解读。

旋翼检查：所有成功飞行都采到实际飞行阶段的 rotor link 非零角速度。x500、gimbal、quadtailsitter、两种飞机的采样均有旋转；standard_vtol 的四升力桨在固定翼阶段停转、推进桨在多旋翼阶段停转，tiltrotor 部分桨在固定翼阶段停转，与对应 SERVO_OUTPUT_RAW=0 一致。Davis/减视觉对照中升力桨的 0 也处于 vtol_state=4。表格是可见角速度采样，不是物理 RPM，不证明采样间隔连续旋转。证据 `rotors-in-flight.csv/png`、`rotors-by-flight-stage.csv`。
<!-- rw-final-measurements:end -->

### 天气复核与最新检查

Davis 已打开白天、多云、清晨、傍晚、阴天、雨、黄昏、夜间的同视角对比；亮度变化可辨，雨线可见，但雨是静态网格，不能从截图推断雨滴运动。夜景发现大片橙色棋盘状发光，原因是旧发光掩码把深色砖/灰墙也当玻璃。复拍确认 Ogre2 仍把独立 emissive_map 解释成过强的橙色棋盘；最终夜景不再给立面挂 emissive_map，改由天空、环境光和路灯表达夜间，保留 `_ensure_emissive_map` 单元测试防止深色墙被识别为窗。风只能通过动力学/运动验证，单张图不证明风效真实。

最新地点相关测试 10 项通过（`.tmp/rw-providers-final8.log`），冒烟/固定翼降落/gimbal overlay 44 项通过（`.tmp/rw-flight-tests-final4.log`）；CPU-only onboard gate PASS，140 s（`.tmp/onboard-gate-final3.log`）。realworld 全部脚本和冒烟脚本 py_compile 通过。未删除或放宽断言。

Iowa 完整视觉串行复验 `vtol-farm-full-repeat` 最终 FAIL，最高 0.37 m，0 对齐帧，解锁前 `xy_position_control` 检查超时并记录重复 gyro/accel 时间戳 3520000。与减视觉 PASS 共同支持渲染/actor负载相关性，但还不能隔离根因。本次提供有证据的结论，未声称已经修复该稳定性问题。

鹿特丹新原点为 51.88522464309306, 4.4297321931078555，公开 PDOK/OSM/3DBAG 匹配数据写入 `/data/datasets/multidetect-sim-assets/validation-1107/sites/rw_port_rotterdam/raw/`。每个下载响应 URL、许可、SHA-256 记在 `download-responses.jsonl`，原始文件 SHA-256 及源条款在 `provenance.json`；没有覆盖旧共享 raw。新原点重建已完成，OSM 水域间距见 `.tmp/review/final/rotterdam-origin-audit.json`。新原点 tiltrotor 首次飞行因 `xy_position_control` 健康检查超时 FAIL，不能把旧水上原点的成功移植为新点成功。为覆盖有效陆地场景，另跑 Davis tiltrotor，保留两者结果。

鹿特丹新陆地原点在 slot 7 重试后 PASS，最高 54.24 m、7 帧对齐并降落；该次是本线 tiltrotor 合格记录，Davis tiltrotor 尝试因系统 Gyro/电源等健康检查缺失而未起飞，不作为机型失败证据。最终飞行索引和旋翼/相机图在 `.tmp/review/final/`，默认世界四视角图以新陆地原点复拍覆盖旧图。

动态 actor 预算试验在同一类设备上仍受整机负载和不同场景内容影响，以下均为 GPU1 单个 1 Hz 中空相机、200 样本，不可和四相机地点表直接横比：

| 地点 | actor 数量倍数 | 相机 RTF | 首帧/s | 峰值/MiB |
|---|---:|---:|---:|---:|
| SF | 0 | 0.618 | 117 | 3671 |
| SF | 0.5 | 0.737 | 109 | 3772 |
| SF | 1.0 | 0.738 | 见原始指标 | 3802 |
| Davis | 0 | 0.694 | 96 | 3365 |
| Davis | 0.5 | 0.951 | 见原始指标 | 3472 |
| Davis | 1.0 | 0.897 | 见原始指标 | 3476 |
| Santa Cruz | 0 | 0.931 | 64 | 3847 |
| Santa Cruz | 0.5 | 0.737 | 见原始指标 | 3948 |
| Santa Cruz | 1.5 | 0.713 | 63 | 见原始指标 | 见原始指标 |

指标位于 `.tmp/review/budget/<site>_m<scale>/metrics.json`。采样期间 loadavg 从约 130 到 200 浮动，RTF 并不随 actor 数量单调变化。全静态 SF 仍只有 0.618；因此没有证据支持一个跨地点 `mover_scale` 能保证 RTF≥0.9，生成器默认仍为 1.0，不能宣称已找到满足该目标的通用默认数量。应先改善网格/贴图/物理负载，并在受控整机负载下重新测量。

## 消防栓簇计数世界（簇目标）

`scripts/realworld/hydrant_clusters.py` 在东京、旧金山、苏黎世、戴维斯的起飞点 30 m 外选一块开阔、干燥、平坦的空地（避开建筑、水面、道路、树和已有设施），摆放 3 个消防栓成等边三角形，边长 3/6/10 m，每个间距一个世界，同一站点三个世界的簇中心相同。世界 ID 形如 `rw_city_tokyo__clear_noon_clu6m`，场景包、truth（含 `segmentation_labels`）和索引在 `configs/scenarios/realworld-clusters/`，格式与 `generated-cluster-hydrants` 一致（`expected_count=3`，`task_kind=count`）。这些场景包、索引、侧车，以及 `simulation/terrain`、`simulation/maps` 下的 `*_clu*m.*` 和 `configs/onboard/suite-rw-hydrant-clusters-*.json` 都由脚本生成、被 `.gitignore` 忽略，不进仓库；世界 `.sdf` 同样被忽略。取出代码后需重新生成：

```bash
export RW_RESOLVED_ROOT=/data/projects/Multi-Detect-env/Multi-Detect/artifacts/cache/gz-fuel/resolved
/data/datasets/multidetect-sim-assets/tools/venv/bin/python scripts/realworld/hydrant_clusters.py   # 默认四个站点，须一次跑完，索引按本次站点重写
```

同时生成回归套件 `configs/onboard/suite-rw-hydrant-clusters-{x500,delta_tailsitter,delta_tailsitter_gimbal}.json`，用 `scripts/run_mission_suite.py --suite … --only rwclu-city_tokyo-d06` 选择；采集器用同一索引（`--index configs/scenarios/realworld-clusters/index.json`）。这些文件只经 `--dry-run` 与静态几何检查，没有跑过 SITL。

### 悬停三角翼教师（侧车 teacher_intent）

每个簇世界的侧车 `*.teacher_intent.json` 给专家教师定义近距离观察：`camera_altitude_m=20`、`camera_pitch_rad=-0.785`（对齐云台 -45°）、围绕簇的 6–12 个观察站（半径 8–22 m、高度 18/24 m 交替、避开建筑和水面）。`vtol_hover_floor_m` 只对悬停任务降低 VTOL 起飞高度下限；`estimate_motion_frame_offset` 缺省为 false（对照冒烟中开启它会让教师到不了观察站）。`--motion-offset-on <正则>` 可按世界打开，`RWCLU_MOTION_OFFSET=on|off` 可按运行覆盖。固定翼巡航任务用 `collect_sim_teacher_episodes.py --no-vtol-hover-mission --fw-cruise-observe`：转 FW、绕簇巡航和高空盘旋、转回 MC 后悬停观察。检测数据集构建用 `build_sim_detector_dataset.py --preset cruise`（负样本比例 0.3、读取未收尾的 episode）。
