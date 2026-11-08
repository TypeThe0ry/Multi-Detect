# 上海与新加坡城市场景回归

三个地点使用独立的站点和世界 ID，原有 Upper Thomson 新加坡世界保持原有位置。

| 站点 | 起飞原点 WGS84（纬度，经度） | 数据 |
| --- | --- | --- |
| `rw_shanghai_lujiazui` | 31.23951455, 121.50182646 | 陆家嘴绿地；新原点重新获取栅格与 OSM |
| `rw_shanghai_century_park` | 31.21626409, 121.54835820 | 世纪公园；新获取栅格与 OSM |
| `rw_singapore_marina_bay` | 1.281614, 103.854207 | 滨海湾中央岬 The Promontory；复用同原点、同范围的旧 raw 缓存 |

每个世界为 2 km 方形范围，首次接入晴天正午版本。起飞点按 OSM 公园/草地区域选择，扣除已映射水域和建筑。陆家嘴旧缓存原点离水域仅 11 m，不符合 25 m 起飞盘要求；新点距所选干地区域边界约 73.5 m。世纪公园选点距所选草地区域边界约 38.7 m。构建还会单独拒绝与水域相交的起飞盘。这些检查只证明当前仿真数据的几何条件。

世界由真实地点的 OSM 建筑轮廓、水域、路网，Sentinel-2 L2A（原始约 10 m）和 Copernicus DEM GLO-30（原始约 30 m）生成。重采样到高像素纹理并不会增加源数据细节。建筑缺失高度、立面、树种、车辆及街道设施使用程序或可再利用道具近似；没有导入陆家嘴或滨海湾标志建筑的实拍三维扫描。不得把这组结果当作对真实城市低空视觉效果或小目标识别能力的验证。

原始数据保存在 `/data/datasets/multidetect-sim-assets/cities-20261108/sites/<站点>/raw/`，逐文件 SHA-256、STAC 产品/日期/资源地址及来源条款见各目录 `provenance.json`。滨海湾保留原来源哈希，并记录 `copied_from_site`；没有覆盖 Upper Thomson 数据。

OSM 为 ODbL-1.0，署名 © OpenStreetMap contributors；Copernicus 影像与 DEM 的开放数据使用和修改说明随 raw provenance 保留。道具沿用 `configs/sim-assets/realworld-library.json` 与 `realworld-asset-sets.json` 的逐项许可和白名单。生成的世界、模型与大纹理放在 `/data`，不加入 Git。公开派生数据库时应随附 OSM 署名及 ODbL 要求。

世纪公园选点核查期间 Overpass 曾返回 502/504，因此额外读取了一次 [OSM map API](https://api.openstreetmap.org/api/0.6/map?bbox=121.540,31.206,121.566,31.226) 小范围数据用于选点；最终 raw/OSM 数据仍由 `fetch_site.py` 正常获取，未以不完整地图替代最终世界。

## 生成与执行

先按 `docs/realworld-scenes.md` 准备共享资源库、天空和动态模型。工具 Python 沿用已安装的 realworld 工具环境：

```bash
/data/datasets/multidetect-sim-assets/tools/venv/bin/python scripts/realworld/prepare_city_regression.py \
  --raw-root /data/datasets/multidetect-sim-assets/cities-20261108/sites --fetch-missing
```

入口检查配置、raw 与 scene 的原点和范围一致后调用已有构建器。存在匹配 scene 时复用模型；用 `--rebuild` 显式重建。它生成三组 runtime/truth/expectations/scenario、地图、DEM 以及 `configs/scenarios/realworld/city-regression-index.json`。身份 truth 为空，因为没有建立与渲染标签一致的目标标注；本组只评估 survey 和 return_home。

回归配置默认将这三个生成模型的纹理最长边限制到 512 px（`--max-texture-px`），保留 UV 和几何，不改源栅格、共享资源库及其他场景。`scene.json` 更新解码纹理统计和 `render_profile`；重跑可进一步缩小，恢复更大纹理必须使用 `--rebuild`。显存仍需为网格、天空、相机及驱动预留空间，静态纹理估算不能代替运行峰值测量。

从仓库根目录运行下面的 custom suite。必须保留项目全局锁；调度器通过 `scripts/sitl_lock.py` 在 0–2 槽位中申请空位，控制端口为 `14560 + 10×槽位`，不使用三角翼槽位 3/4。

```bash
export PYTHONPATH=src:. PYTHONUTF8=1
export CUDA_VISIBLE_DEVICES=0 MULTIDETECT_SIM_GPU=1
export MULTIDETECT_SIM_GPU_DEVICES=0 MULTIDETECT_SIM_GPU_AVOID=1,2,3
export MULTIDETECT_SITL_SLOTS=3 MULTIDETECT_SIM_FIRST_FRAME_WAIT_S=240
export MULTIDETECT_SITL_LOCK=/data/scratch/multidetect-sitl.lock
/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python scripts/run_nightly_regression.py \
  --model-set r21idr3-r24plan \
  --custom-suite cities=configs/onboard/suite-cities-r21idr3-r24plan.json \
  --suites cities --episodes 2 --workers 1 --gpu-render \
  --out /data/projects/Multi-Detect-env/Multi-Detect/artifacts/benchmarks/cities-r21idr3-r24plan-20261108-e2
```

套件从现役默认配置固定 r21id-r3、r24、YOLOE 和规划前预检。三个任务都是半径 35 m、重叠率 70% 的区域测绘后返航，要求规划技能正确且测绘覆盖率至少 0.7。它是额外回归组，不改变现有 21 项基线，也不改变 planning-v5 或返航探测集。每地点两架次全部完成才能声称本组 6/6 通过；静态检查或 dry-run 不能替代飞行证据。

## 付费资源备选（未购买）

| 资源 | 补充用途 | 采购前需确认 |
| --- | --- | --- |
| [Airbus Pléiades Neo / OneAtlas](https://space-solutions.airbus.com/imagery/our-optical-and-radar-satellite-imagery/pleiades-neo/) | 原生 30 cm 影像，补充三地点地面纹理 | 精确 AOI、云量、日期、离线缓存、派生纹理再分发及机器学习许可；[官方 API](https://api.oneatlas.airbus.com/getting-started/apis-overview/)提供存档和任务入口 |
| [Vantor Hub](https://vantor.com/product/platform/hub/) | 30 cm 存档影像、可询价的三维地图 | 三地点覆盖、实际产品分辨率、垂直精度、Gazebo 离线转换和分享权 |
| [SkyWatch EXPLORE](https://skywatch.com/explore/) | 比较多个传感器的存档和新拍产品 | 同上，另确认最小订单和最终供应商许可；[报价入口](https://skywatch.com/data-pricing/)不等于本项目 AOI 的最终报价 |

本次没有创建付费订单、购买、启动新拍或导入付费资源。没有从论坛推算价格，也没有把登录受限的地图瓦片作为可再利用影像。

## 验证记录

2026-11-08：`test_city_regression.py` 与 `test_realworld_site_providers.py` 共 14 项通过。实际 SITL 架次结果和生成资源统计另记到 `runs/progress-20261108-cities.md`；生成耗时与场景显存估算不代表飞行通过。后继城市作业等待 `runs/r31_regression_20261108.out` 出现 `r31_regression_20261108 ALL_DONE` 后才申请槽位，进程内存上限为 64G。
