# 真实地点仿真场景

用公开数据重建 6 个真实地点，生成可直接跑 PX4 SITL 的 Gazebo 世界。不改动现有世界和回归。

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

天气（天空是 Poly Haven HDRI 贴的无光照穹顶，因为 Gazebo 忽略 `cubemap_uri`）：`clear_noon`、`partly_cloudy`、`morning`、`evening`、`overcast`、`rain`、`dusk`、`night`、`windy`。

## 场景里有什么

- 地形：USGS 3DEP 2 m DEM，起飞点 25 m 内压平。视觉网格 4 m，碰撞网格 8 m，所以起飞点外的视觉和碰撞高度会差一点。
- 地面贴图：NAIP 航拍，每块 1 km 用 4096 px（源分辨率 0.6 m，部分州更低）。起飞点 ±300 m 内用 8192 px，叠加 CC0 细节纹理。解码贴图含 mipmap 的估算预算约 3 GiB；生成统计会打印估算用量，不改变 8192/4096 px 分辨率。
- 建筑：OSM 轮廓和高度，缺高度时按类型估计。屋顶内面贴航拍，平屋顶的女儿墙和按面积布置的空调箱/水箱单独着色；住宅是人字顶，外墙贴图按真实层高绘制。
- 道路：OSM 路网贴在地形上，路面颜色按航拍路域像素中位数匹配，带车道线和斑马线。人行道沿用航拍。
- 水面：OSM 水域上覆盖带法线贴图的暗青材质，SDF 透明度为 0.55，以透出近岸航拍颜色；Gazebo 实际透明混合仍需渲染目视确认。
- 树：从航拍 NDVI 和粗糙度找树冠位置，每个地点最多约 2500 棵，真实位置但树种是近似的。
- 街道设施：路灯、停车标志、消防栓、邮筒、电线杆，优先用 OSM 位置，其余沿路摆放。住宅区每户路边一个邮箱。
- 动态物体：车辆、行人和海岸/港口船使用 actor，不占物理计算。车辆从道路等级取速度、在路侧行驶并错开起始时间；船沿简化水上航线行驶。夜间建筑立面使用稀疏随机窗户发光贴图，路灯有发光灯头和最多 24 个附近点光源；车辆模型带前灯/尾灯发光几何。白天不挂窗户发光贴图。
- 标签：起飞点附近的车、人、邮箱等约 100 个是独立模型，带分割 label；其余合并进地形模型，否则 RTF 会掉到 0.16。

### 动态物体与夜景预算

默认 actor 数量按站点类型和可用道路数量变化。当前 Davis 约 25 车/24 人、SF 约 36 车/40 人、Santa Cruz 约 30 车/36 人/1 船。`--mover-scale` 同比例缩放车辆和行人数量，供 RTF 分档测量；建议同一世界、同一机型、同一相机设置分别测 `0.5`、`1.0`、`1.5`，同时记录每个世界 SDF 字节数、actor 数、稳定后 RTF。例：

```bash
export RW_RESOLVED_ROOT="$PWD/.tmp/rw-resolved"
export RW_WORLD_OUT="$PWD/.tmp/rw-budget"
$PY scripts/realworld/make_world.py rw_downtown_sf --weather clear_noon --mover-scale 0.5 --name-suffix _m05
$PY scripts/realworld/make_world.py rw_downtown_sf --weather clear_noon --mover-scale 1.0 --name-suffix _m10
$PY scripts/realworld/make_world.py rw_downtown_sf --weather clear_noon --mover-scale 1.5 --name-suffix _m15
```

`RW_RESOLVED_ROOT` 需是可写的站点模型副本/overlay：夜间生成会在站点 `textures/` 下写 `*_emissive.png`；`artifacts/` 为只读共享缓存时，不会写入其中，不能生成的贴图会跳过并保留有效世界。运行 Gazebo 时还需把 overlay 加入 `GZ_SIM_RESOURCE_PATH`，让 `model://<站点>/textures/*_emissive.png` 能解析。`RW_MOVER_OUT` 可把 `make_movers.py` 新生成的 actor 网格写到临时 overlay。Ogre 2 Forward3D 按屏幕网格单元收集局部光源，公开 API 有单元容量参数，没有可据此声明的统一总灯数硬上限；本场景将路灯点光源保守限制为 24 个，起飞点再加 4 个近距离点光源和 1 个太阳方向光，待 Gazebo 本机版本验证渲染器容量。路灯灯头和车灯是发光材质，不新增点光源。Gazebo/Ogre2 的实际画面和 RTF 由渲染/飞行侧测量，本轮只检查 SDF/XML 和大小，不宣称视觉验收。

建议查看视角（`shoot_scene.py` 的 `--cam name:x,y,z:tx,ty,tz`）：

| 世界 | 视角 | 重点 |
|---|---|---|
| `rw_downtown_sf` | `overview:0,-320,220:0,0,8` | 城市密度、天际线、夜间窗灯范围 |
| `rw_downtown_sf` | `street:45,-55,24:0,0,2` | 车道位置、车灯、路灯照地 |
| `rw_suburb_davis` | `overview:0,-300,190:0,0,5` | 郊区车辆/行人密度和住宅窗灯 |
| `rw_suburb_davis` | `street:35,-45,18:0,0,1` | 人行道偏移、行人比例和车辆间距 |
| `rw_coast_santacruz` | `overview:-650,-500,240:-450,-250,0` | 海岸线、船航线是否落在水面 |
| `rw_coast_santacruz` | `harbor:-850,-380,95:-900,-250,0` | 船体比例、海面和港口夜景 |

本地 XML 样本大小（字节，含场景实体，不含外部网格/贴图）：

| 站点 | 0.5 倍 | 1.0 倍 | 1.5 倍 |
|---|---:|---:|---:|
| SF downtown | 197590 | 258552 | 289287 |
| Davis suburb | 171891 | 245433 | 264367 |
| Santa Cruz coast | 192706 | 246861 | 273073 |

以上仅是本次生成的 XML/SDF 大小，不代表 Gazebo RTF。RTF 由渲染侧按同机型和固定相机实测后补录。

## 已知限制

- 树和街道设施没有碰撞体，建筑、货柜和地形有。
- 雨是静止的半透明线条网格：粒子系统要加 world `<plugin>`，而这会替换掉默认系统，世界起不来。要真粒子得改容器启动补丁。雾沿用现有相机增强方案，因为 Ogre2 不支持 SDF 雾。
- 航拍自带车辆和阴影，会和 3D 物体、实时光照叠在一起，贴近地面时能看出来。
- 货柜位置只按航拍里的货场区域估计，不是逐个对应。
- 大世界加载到第一帧要 40~60 秒，跑采集和飞行要设 `MULTIDETECT_SIM_FIRST_FRAME_WAIT_S=240`。
- `sim_teacher` 的障碍物读取只认 `bounds` 盒子，看不到合并进地形的建筑；建筑范围在 `scene.json` 的 `footprints`。
- 因本线禁止世界/模型内 `<plugin>`，`make_world.py` 不再给独立道具插入 Label system 插件；道具仍有几何和 bounds，但 Gazebo segmentation label 不由这些世界提供。
- 高分辨率免费航拍只覆盖美国，其他国家见下面的付费清单。

## 重新生成

大文件不进 git。原始数据和资源库在 `/data/datasets/multidetect-sim-assets/`，模型输出在 `artifacts/cache/gz-fuel/resolved/rw_*`，每个地点约 0.9 GB。

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

新增地点时在 `configs/sim-assets/realworld-sites.json` 加一条。检查和飞行：

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
| 建筑、道路、设施位置 | OpenStreetMap，经 Overpass | ODbL，需署名 © OpenStreetMap contributors，公开发布衍生数据库要同样授权 |
| 模型 | Gazebo Fuel | CC0 或 CC BY 4.0，需署名，逐项见清单；动态船使用 Fuel `RC Boat`（CC BY 4.0） |
| 贴图 | ambientCG | CC0 |
| 天空和模型 | Poly Haven | CC0 |

每项下载的 URL、许可证和 SHA-256 在 `configs/sim-assets/realworld-library.json`。Fuel 只保留了可再使用的许可证，排除了 NC 和 ND。

## 付费资源（未购买，价格是 2026-11 查到的公开报价，下单前需向厂商确认）

| 资源 | 价格 | 用途和注意 |
|---|---|---|
| 30~49 cm 卫星影像（SkyWatch） | 存档 $19/km²，新拍 $29/km²，最小 1 km² | 美国以外的地点。一个 2 km 场景约 $76 |
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
