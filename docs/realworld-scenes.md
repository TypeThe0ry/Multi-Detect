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
- 本次首轮大世界加载到第一帧约 47~77 秒（随整机负载变化），跑采集和飞行要设 `MULTIDETECT_SIM_FIRST_FRAME_WAIT_S=240`。
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

第二轮及未目检站点的结论将在实际打开相应图片后补录，不能以生成成功代替目视通过。

### 飞行与相机问题

`x500_gimbal` 原始图像中 gimbal RGB 是 FOV 2.0、1280×720，冒烟按 FOV 1.74、1280×960 标定验证，采集器只有启用 `--segmentation` 才挂载已存在的校准 gimbal SDF。冒烟现在为该机型显式挂校准 overlay，没有放宽 CameraInfo 断言。`gimbal-fixed` 已通过完整起飞、重定位、降落，7 帧对齐；证据 `.tmp/flights/gimbal-fixed.log`、同名目录 `episodes/*/steps.jsonl` 与 `rotors_in_flight.json`。

`standard_vtol` 在 Zurich 的 `vtol-zurich` 已通过，7 帧对齐并降落；Iowa 的 `r1b-vtol-farm` 仍在解锁前报告 `height estimate not stable`，日志有 gyro/accel duplicate timestamp。需要独立运行的同机型、同设置 A/B 才能归因，不能仅凭并发时失败认定相机是根因。

SF 和 Berlin 的 x500 首次回归均在 `takeoff_reached` 超时，高度约 0.3 m 以内。SF 的 L 形楼粗 AABB 填满了真实轮廓中空出的起飞区；Berlin 首轮文件也包含覆盖原点的粗盒。辅助障碍盒现在避开起飞区，柏林城市碰撞使用实际表面，重跑结果单独记录。


后续首轮目检：鹿特丹发现货柜和树落在 OSM 水域上，已把水域及 10 m 岸线缓冲加入货柜禁放区，植被同样避水；其高分 core 和粗地形交叠产生矩形贴图块，细 core 现在替换对应粗地形三角形。阿姆斯特丹公园起飞区开阔，树影叠影和低空模糊仍明显。东京道路起伏、屋顶航拍拉伸明显，GLO-30 城市表面高程并非地面真值。柏林首轮大部分 LoD2 建筑被表面高程覆盖，远处局部悬浮：城市模型地面表面高程约 28–39 m，DEM 为 27–58 m；每栋建筑现在按其 GroundSurface 与本地地形的中位差整体对齐，这是近似修正，不是 DTM 精度提升，须复拍核对。


### 本次测试和飞行证据口径

- 地点测试：realworld venv 中 `tests/test_realworld_site_providers.py` 6 项通过；主环境中冒烟、固定翼降落、gimbal overlay 相关测试共 44 项通过。全部 realworld 脚本及冒烟脚本 `py_compile` 通过；CPU-only onboard gate 于本次修改后通过（102 s）。日志：`.tmp/rw-providers-final3.log`、`.tmp/rw-flight-tests-final3.log`、`.tmp/onboard-gate-final2.log`。
- 冒烟起飞判据修正：`episode.takeoff_altitude_m` 是请求高度，不能充当实际爬升证据；现在只用 `LOCAL_POSITION_NED` 判断。新增低高度遥测回归用例，防止被障碍卡住的飞机被记录为已起飞。
- 当前已完成并通过的本次飞行：`gimbal-fixed`（Davis）、`vtol-zurich`（Zurich）、`tilt-rotterdam`（Rotterdam）、`cessna-farm`（Iowa）。其他排队飞行以各自最终报告为准。
- `.tmp/review/final/rotors-in-flight.csv` 只保留与遥测墙钟差小于 3 s、实测高度超过 2 m 的转子采样；转速来自 Gazebo rotor link 姿态差，是可见转子的仿真角速度，受采样混叠和 `rotorVelocitySlowdownSim` 影响，不是真实电机 RPM。采样不能证明采样间隔内连续转动。
- `vtol-zurich` 的 puller 在多旋翼阶段为零，此时 SERVO_OUTPUT_RAW 的第五路也为零；`tilt-rotterdam` 在 vtol_state=4 时 rotor_1/3 为零，第二/四路输出同为零，其他阶段恢复转动。不能把按设计停用的转子列为损坏，也不能声称每个转子在所有飞行阶段都转。
- 截图指标另记录 `rtf_sim_wall`（稳定采样窗口的仿真时间增量/墙钟增量）、瞬时 RTF 中位数、样本数、各相机首帧和本容器进程显存峰值。截图是 1280×960、1 Hz，飞行机载相机为 30 Hz；两者的 RTF 不应混用。显存每 2 s 采样，可能漏掉更短暂的峰值。

### 柏林第二轮提前检查与资源对照

已实际打开 `.tmp/review/round2/rw_city_berlin/sheet.jpg`：街道随 DSM 呈明显波浪，远处大量城市建筑只有屋顶、缺少竖墙，起飞点是人为清空/压平后的纪念碑区域，不能称为未改动的真实开阔地。四视角首帧为 83–84 s，200 样本的 `rtf_sim_wall=0.689`、瞬时中位数 0.785，采样时 loadavg≈146；本容器显存采样峰值 3598 MiB，超过 2 GiB，未通过预算。

缩小贴图的独立对照保存在 `.tmp/review/budget-berlin/`，模型为 `rw_city_berlin_budget`，也已打开拼图。核心贴图最长边从 8192 降至 4096，其余大贴图降至 2048，显存峰值仍有 3048 MiB；不能只缩贴图就宣称满足预算，未将这一试验替换默认产物。

缺墙已定位到 CityGML 读取器：竖墙投影为 XY 线段后，原 `Polygon.buffer(0)` 范围过滤把它丢弃；旧代码还只读一个 Polygon，父 Building 又重复包含 BuildingPart。现在按每个 Polygon 的外环/内环读取，竖墙按其投影凸包与范围相交判断，子 BuildingPart 单独生成。新增竖墙、多 Polygon、嵌套 BuildingPart 测试，地点测试现为 8 项通过（`.tmp/rw-providers-final5.log`）。原始柏林缓存保持不变，本次从已有 9 个 LoD2 ZIP 离线解析到 `/data/datasets/multidetect-sim-assets/validation-1107/sites/rw_city_berlin/raw/`，归一化记录、原 ZIP SHA-256 和输出 SHA-256 写入该目录 provenance；修复后构建和复拍仍需完成验证。

独立 Fuel 道具出现 `hatchback.png`、`fire_hydrant_diffuse.png` 等路径无法解析。`make_world.py` 现把独立源模型复制到 `rw_prop_<slug>`，在副本中修正 MTL/DAE 的相对贴图路径；部分车辆缺少的共用车轮贴图仅从已有本地模型缓存复制，不下载、不修改原模型。跨平台文件测试确认源 MTL 保持不变。此修复须以之后的渲染日志和图片核实。


道具包装首轮实渲染在 Grindelwald 暴露 COLLADA 加载器兼容问题：ElementTree 默认把根标签写为 `ns0:COLLADA`，Gazebo 报 `Missing COLLADA tag` 后崩溃。包装器现在注册默认命名空间，保留无前缀 `<COLLADA xmlns=...>`；回归测试同时检查根标签和纹理路径，8 项通过（`.tmp/rw-providers-final6.log`）。失败截图目录不能算渲染通过，已重新排队拍摄。
