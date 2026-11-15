# rw_* 数公里级真实地形范围

本线为固定翼巡航、长航时和大范围搜救提供显式的数公里级范围入口。现有世界的默认行为保持不变：不传新参数时，仍使用 `configs/sim-assets/realworld-sites.json` 中的 `size_m`，不会改写已有 `rw_*` 默认产物。

## 范围参数

范围是以起飞点为中心的正方形边长，单位为米。`scripts/realworld/range_profile.py` 从边长派生半边长、近场障碍范围、中场范围和贴图分辨率。

| profile | 边长 | 适用场景 | 近场贴图 | 中场贴图 | 远场贴图 |
|---|---:|---|---:|---:|---:|
| `default_2km` | 2,000 m | 兼容现有城市世界 | 4096 px | 2048 px | 2048 px |
| `default_3km` | 3,000 m | 兼容现有郊区/农田世界 | 4096 px | 2048 px | 2048 px |
| `wide_6km` | 6,000 m | 数公里固定翼巡航 | 4096 px | 2048 px | 1024 px |
| `wide_10km` | 10,000 m | 大范围搜救和长航时 | 4096 px | 2048 px | 1024 px |

范围必须先下载、再构建；不同边长不能共用同一个 raw 缓存：

```bash
export PY=/data/datasets/multidetect-sim-assets/tools/venv/bin/python
export RW_RAW=$PWD/.tmp/realworld-wide-6km
export RW_SCENE_ROOT=$PWD/.tmp/realworld-wide-6km/scenes
export RW_RESOLVED_ROOT=$PWD/.tmp/realworld-wide-6km/models
$PY scripts/realworld/fetch_site.py rw_farm_iowa --raw-root "$RW_RAW" --extent-m 6000
$PY scripts/realworld/build_scene.py rw_farm_iowa --raw-root "$RW_RAW" --scene-root "$RW_SCENE_ROOT" --output-root "$RW_RESOLVED_ROOT" --extent-m 6000
$PY scripts/realworld/make_world.py rw_farm_iowa --weather clear_noon --world-out-dir .tmp/worlds-wide --extent-m 6000 --skip-scenarios
```

`fetch_site.py` 会把实际边长和 profile 写入 `raw/provenance.json`；`build_scene.py` 在范围不匹配时拒绝构建。这样可以避免把 2 km DEM 与 6 km OSM/影像拼在一起。

## LOD、合并和预算

- 近场使用 4096 px tile，中场 2048 px，远场 1024 px；近场核心仍保留高精度地面和起飞点净空圈。
- 地形视觉网格默认 4 m、碰撞网格 8 m；重复树木、道路设施和远景道具沿用合并网格。只有需要 AI 区分的附近对象保留独立模型。
- 道具三角形上限为 4,000,000；`scene.json.stats` 记录 `triangles`、`prop_triangles`、`prop_instances`、`texture_decoded_gib` 和 `mesh_buffer_estimate_bytes`。2 GiB 是总渲染预算，不能只看压缩文件大小。
- 任务边界、DEM 方框和 GeoJSON `operating-area` 都由同一个 `extent_m` 派生；GeoJSON 还记录 `near_radius_m`、`mid_radius_m`。生成器的默认近场障碍盒仍限制在 350 m 内，避免大世界把 teacher bounds 数量线性放大。
- 起飞点水域检查的净空圈也属于 profile：默认 2–3 km 世界保持 25 m，6–10 km 世界按范围缓慢增至最多 60 m；既有 25 m 建筑/对象过滤保持兼容，避免改变旧世界默认布局。

## CPU 侧验证

不需要启动 Gazebo 的静态检查：

```bash
export PYTHONPATH=src:.
export PYTHONUTF8=1
export TMPDIR=$PWD/.tmp
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python -m pytest tests/test_realworld_wide_area.py tests/test_realworld_site_providers.py -q -p no:xdist
/data/datasets/multidetect-sim-assets/tools/venv/bin/python scripts/realworld/validate_wide_area.py \
  "$RW_SCENE_ROOT/sites/rw_farm_iowa/scene.json" --world .tmp/worlds-wide/rw_farm_iowa__clear_noon.sdf --print-command
```

验证脚本会检查 world 没有 `<plugin>`、`<include>` 或网络 URI，并输出范围、模型数、三角形数和贴图预算。它不能替代画面目检、RTF 或飞行验收。

## 独立 SITL 验证（由用户启动）

slot 0–2 和 3–4 不属于本线；验证只使用 5–7，且总槽位必须设为 8。SITL 作业脚本固定使用带 `torch/ultralytics` 的 `.venv-vlm`：

```bash
export MULTIDETECT_SITL_SLOTS=8
export MULTIDETECT_SIM_FIRST_FRAME_WAIT_S=240
/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python \
  scripts/realworld/leased_flight.py 5 standard_vtol_mono_cam \
  rw_farm_iowa__clear_noon .tmp/flights/rw-farm-6km-vtol
```

固定翼可将机型替换为 `rc_cessna_mono_cam` 或 `advanced_plane_mono_cam`，并在独立目录重复一次。RTF 对比应使用 `shoot_scene.py` 输出的 `rtf_sim_wall`、稳定窗口中位数、首帧时间和容器显存峰值；扩展前后必须使用同一 GPU、天气、相机数量和 mover scale。当前没有在本任务中启动 Docker/GPU/SITL，因此文档只提供待测命令，未声称已有 RTF 或飞行结果。

## 已知限制

- 6–10 km 下载量、DEM/影像处理时间和显存压力显著增加，必须使用独立 `raw-root`、`scene-root` 和 `resolved-root`。
- 远场贴图降采样不增加原始数据精度；全球 provider 的 Sentinel-2/Copernicus 数据仍只适合宏观环境。
- 生成成功不等于画面真实感、RTF 达标或固定翼任务成功，最终结论需要用户在独立 slot 5–7 实测并记录负载。
