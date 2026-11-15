# rw_* 数公里级真实地形范围

本线为固定翼巡航、长航时和大范围搜救提供显式的数公里级范围入口。不传新参数时仍使用 `configs/sim-assets/realworld-sites.json` 的 `size_m`；现有世界、采集和回归默认行为保持不变。

## 范围参数

范围是以起飞点为中心的正方形边长，单位为米。`scripts/realworld/range_profile.py` 从边长派生半边长、近场障碍范围、中场范围和贴图分辨率。

| profile | 边长 | 适用场景 | 近场贴图 | 中场贴图 | 远场贴图 |
|---|---:|---|---:|---:|---:|
| `default_2km` | 2,000 m | 兼容现有城市世界 | 4096 px | 2048 px | 2048 px |
| `default_3km` | 3,000 m | 兼容现有郊区/农田世界 | 4096 px | 2048 px | 2048 px |
| `wide_6km` | 6,000 m | 数公里固定翼巡航 | 4096 px | 2048 px | 1024 px |
| `wide_10km` | 10,000 m | 大范围搜救和长航时 | 4096 px | 2048 px | 1024 px |

范围必须先下载、再构建；不同边长不能共用同一个 raw 缓存。下载和构建使用 realworld 工具环境；大文件放 `/data/datasets/multidetect-sim-assets/`，本工作树已经存在的两份 `.tmp/realworld-wide-*km` 缓存继续原位使用，不复制进 git。

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
export PY=/data/datasets/multidetect-sim-assets/tools/venv/bin/python
export RW_RAW=/data/datasets/multidetect-sim-assets/wide-area-6km
export RW_SCENE_ROOT=$RW_RAW/scenes RW_RESOLVED_ROOT=$RW_RAW/models
$PY scripts/realworld/fetch_site.py rw_farm_iowa --raw-root "$RW_RAW" --extent-m 6000
$PY scripts/realworld/build_scene.py rw_farm_iowa --raw-root "$RW_RAW" --scene-root "$RW_SCENE_ROOT" --output-root "$RW_RESOLVED_ROOT" --extent-m 6000
```

以上是新缓存构建示例，不是下文飞行的前置命令。`fetch_site.py` 把实际边长和 profile 写入 `raw/provenance.json`；`build_scene.py` 在范围不匹配时拒绝构建。来源 URL、许可证、SHA-256 沿用各 raw 的 provenance；本次仅使用已有缓存，没有下载或新增依赖。

## 工作树中的飞行资产与重生成

2026-11-15 已生成两套独立世界及三个 scenario bundle：

| 边长/机型 | world/scenario ID | scene.json | 运行时 resolved-root |
|---|---|---|---|
| 2 km VTOL | `rw_farm_iowa_w2km__clear_noon` | `.tmp/realworld-wide-2km/scenes/sites/rw_farm_iowa/scene.json` | `.tmp/realworld-wide-2km/models` |
| 6 km VTOL、rc_cessna | `rw_farm_iowa_w6km__clear_noon` | `.tmp/realworld-wide-6km/sites/rw_farm_iowa/scene.json` | `.tmp/realworld-wide-6km/models` |

`make_world.py --world-site-name` 只改变 world/scenario 文件名前缀；两份隔离模型根内部仍使用 `model://rw_farm_iowa/...`。SDF 放在 `simulation/gazebo/worlds/`，map/DEM 放在 `simulation/maps/` 和 `simulation/terrain/`，scenario 放在 `configs/scenarios/realworld/<airframe>/`。这些位置符合 stack 与 collector 的 builtin 路径解析；仅在 `.tmp/worlds` 生成 SDF 不能直接飞。

运行时必须显式设置 `MULTIDETECT_SIM_FUEL_RESOLVED_ROOT`。Docker planner 将它只读挂载到 `/opt/multidetect-gz-fuel-resolved` 并置于资源搜索路径首位；相对路径按 repository root 解析，缺失/非目录明确报错。未设置时仍使用原 `artifacts/cache/gz-fuel/resolved` 默认路径。`RW_RESOLVED_ROOT` 只控制生成器，不能代替运行时变量。本线没有写共享 `artifacts/`。

SDF 被 gitignore，换工作树后需要重新生成；当前缓存齐全时，从本工作树根目录粘贴以下整段即可，完全离线：

```bash
cd /data/projects/Multi-Detect-env/mdwt/cx-rw-wide-fly
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
export PY=/data/datasets/multidetect-sim-assets/tools/venv/bin/python
export RW_SIM_OUT=$PWD/simulation RW_WORLD_OUT=$PWD/simulation/gazebo/worlds
export RW_SCENE_ROOT=$PWD/.tmp/realworld-wide-2km/scenes RW_RESOLVED_ROOT=$PWD/.tmp/realworld-wide-2km/models
$PY scripts/realworld/make_world.py rw_farm_iowa --weather clear_noon --extent-m 2000 --world-site-name rw_farm_iowa_w2km --scenario-airframe standard_vtol_mono_cam
export RW_SCENE_ROOT=$PWD/.tmp/realworld-wide-6km RW_RESOLVED_ROOT=$PWD/.tmp/realworld-wide-6km/models
$PY scripts/realworld/make_world.py rw_farm_iowa --weather clear_noon --extent-m 6000 --world-site-name rw_farm_iowa_w6km --scenario-airframe standard_vtol_mono_cam
$PY scripts/realworld/make_world.py rw_farm_iowa --weather clear_noon --extent-m 6000 --world-site-name rw_farm_iowa_w6km --scenario-airframe rc_cessna_mono_cam
```

原有未跟踪 `rw_farm_iowa__clear_noon__6km.*.json` 是此前试验，保持原样；本次命令仅使用新命名 bundle。换机器或清理 `.tmp` 后，需要先恢复匹配的 raw/scene/model 缓存，代码本身不包含数 GB 模型。

## CPU 侧验证

`leased_flight.py --plan-only` 走相同 scenario、机型、端口和容器前缀的 collector `--dry-run`，生成 Docker argv；不租锁、不启动采样线程、Docker 或 SITL。下列命令使用 bridge 网络；仅在 CPU plan 进程设置 `MULTIDETECT_SIM_LISTEN_HOST=127.0.0.1` 作为计划占位地址，避免 Docker network inspect。实飞命令清除该占位，由项目发现实际 bridge gateway。

```bash
cd /data/projects/Multi-Detect-env/mdwt/cx-rw-wide-fly
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
export MULTIDETECT_SITL_SLOTS=8 MULTIDETECT_SIM_FIRST_FRAME_WAIT_S=240
export MULTIDETECT_SIM_GPU=1 MULTIDETECT_SIM_GPU_DEVICES=1 MULTIDETECT_SIM_NETWORK=bridge
export MULTIDETECT_SIM_LISTEN_HOST=127.0.0.1
export MULTIDETECT_SIM_FUEL_RESOLVED_ROOT=$PWD/.tmp/realworld-wide-2km/models
/data/datasets/multidetect-sim-assets/tools/venv/bin/python scripts/realworld/validate_wide_area.py .tmp/realworld-wide-2km/scenes/sites/rw_farm_iowa/scene.json --world simulation/gazebo/worlds/rw_farm_iowa_w2km__clear_noon.sdf --resolved-root "$MULTIDETECT_SIM_FUEL_RESOLVED_ROOT" > .tmp/realworld-wide-2km/flight-audit.json
/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python scripts/realworld/leased_flight.py 6 standard_vtol_mono_cam rw_farm_iowa_w2km__clear_noon .tmp/plans/rw-farm-2km-vtol --plan-only > .tmp/realworld-wide-2km/collector-plan.json
export MULTIDETECT_SIM_FUEL_RESOLVED_ROOT=$PWD/.tmp/realworld-wide-6km/models
/data/datasets/multidetect-sim-assets/tools/venv/bin/python scripts/realworld/validate_wide_area.py .tmp/realworld-wide-6km/sites/rw_farm_iowa/scene.json --world simulation/gazebo/worlds/rw_farm_iowa_w6km__clear_noon.sdf --resolved-root "$MULTIDETECT_SIM_FUEL_RESOLVED_ROOT" > .tmp/realworld-wide-6km/flight-audit.json
/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python scripts/realworld/leased_flight.py 5 standard_vtol_mono_cam rw_farm_iowa_w6km__clear_noon .tmp/plans/rw-farm-6km-vtol --plan-only > .tmp/realworld-wide-6km/collector-plan-vtol.json
/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python scripts/realworld/leased_flight.py 7 rc_cessna_mono_cam rw_farm_iowa_w6km__clear_noon .tmp/plans/rw-farm-6km-cessna --plan-only > .tmp/realworld-wide-6km/collector-plan-cessna.json
```

2026-11-15 CPU 结果：两个 audit 的 world 均无 `<plugin>`、`<include>` 和网络 URI；model URI 文件缺失为 0。2 km 检查 729 个文件引用，6 km 检查 1362 个。三个 collector plan 均构建成功，分别绑定端口 14620/14621、14610/14611、14630/14631，GPU 都为 1，模型根来自各自工作树 `.tmp`，世界只读挂载为容器 `default.sdf`。bridge 模式保持每个容器独立的网络命名空间；没有用 host 网络让多个 PX4 instance 0 共享内部端口。CPU plan 的 loopback 地址只证明 argv 可构建，实飞的 bridge gateway 发现仍由用户环境执行。检查不替代网格内材质完整性、画面、RTF 或飞行验收。

相关回归：主 `.venv` 单进程运行 `tests/test_realworld_wide_area.py`、`tests/test_realworld_leased_flight.py`、`tests/test_simulator_docker.py`，46 项通过，日志 `.tmp/wide-flight-tests.log`。主环境没有 mapbox-earcut，直接运行 provider 文件会跳过；仍用主 `.venv` 执行 pytest，并仅在该测试进程向 `sys.path` 末尾追加 realworld 工具环境已有的 `lib/python3.12/site-packages` 后，`tests/test_realworld_site_providers.py` 11 项通过，日志 `.tmp/wide-provider-tests.log`，没有安装依赖或更改环境。新增测试验证独立 world 名仍引用源模型、scenario channel hash 可加载、DEM 范围正确；另修复 `obstacle_boxes()` 对既有 400 m 离线测试场景的多余 profile 校验，仍保持所有受支持世界原有 350 m cap，没有改断言。CPU-only onboard gate 通过（109 s），日志 `.tmp/wide-onboard-gate.log`。

最后对 `leased_flight` 和 `validate_wide_area` 的定向回归 14 项通过，日志 `.tmp/wide-final-focused-tests.log`；文档六段 Bash 命令均通过 `bash -n`。三条最终 bridge plan 的 argv 已核对 GPU1、slot 5–7 端口和各自只读模型挂载。

## 一键实测命令（由用户在沙箱外运行）

只使用 slot 5–7，总槽位必须为 8。每段是独立、完整的 Bash 命令，固定 GPU1、clear_noon、默认 mover scale=1；建议按顺序单独运行，避免三次测试相互争用同一 GPU。每次使用新的时间戳输出目录，smoke 终端输出保存在该目录的 `flight.log`；smoke 原有逻辑只转出 collector 输出尾部，因此该文件不是完整 collector 日志。Python 固定为带 torch/ultralytics 的 `.venv-vlm`；正常路径继续使用项目共享 SITL 租约。不要把共享锁改到 `.tmp` 来绕过其他作业。

2 km 基线，slot 6，standard_vtol_mono_cam：

```bash
(
set -euo pipefail
cd /data/projects/Multi-Detect-env/mdwt/cx-rw-wide-fly
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
export MULTIDETECT_SITL_SLOTS=8 MULTIDETECT_SIM_FIRST_FRAME_WAIT_S=240
export MULTIDETECT_SIM_GPU=1 MULTIDETECT_SIM_GPU_DEVICES=1 MULTIDETECT_SIM_NETWORK=bridge
unset MULTIDETECT_SIM_LISTEN_HOST
export MULTIDETECT_SIM_FUEL_RESOLVED_ROOT=$PWD/.tmp/realworld-wide-2km/models
RW_FLIGHT_OUT="$PWD/.tmp/flights/rw-farm-w2km-vtol-slot6-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$TMPDIR" "$RW_FLIGHT_OUT"
/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python scripts/realworld/leased_flight.py 6 standard_vtol_mono_cam rw_farm_iowa_w2km__clear_noon "$RW_FLIGHT_OUT" 2>&1 | tee "$RW_FLIGHT_OUT/flight.log"
)
```

6 km 扩展，slot 5，standard_vtol_mono_cam：

```bash
(
set -euo pipefail
cd /data/projects/Multi-Detect-env/mdwt/cx-rw-wide-fly
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
export MULTIDETECT_SITL_SLOTS=8 MULTIDETECT_SIM_FIRST_FRAME_WAIT_S=240
export MULTIDETECT_SIM_GPU=1 MULTIDETECT_SIM_GPU_DEVICES=1 MULTIDETECT_SIM_NETWORK=bridge
unset MULTIDETECT_SIM_LISTEN_HOST
export MULTIDETECT_SIM_FUEL_RESOLVED_ROOT=$PWD/.tmp/realworld-wide-6km/models
RW_FLIGHT_OUT="$PWD/.tmp/flights/rw-farm-w6km-vtol-slot5-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$TMPDIR" "$RW_FLIGHT_OUT"
/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python scripts/realworld/leased_flight.py 5 standard_vtol_mono_cam rw_farm_iowa_w6km__clear_noon "$RW_FLIGHT_OUT" 2>&1 | tee "$RW_FLIGHT_OUT/flight.log"
)
```

6 km 固定翼，slot 7，rc_cessna_mono_cam：

```bash
(
set -euo pipefail
cd /data/projects/Multi-Detect-env/mdwt/cx-rw-wide-fly
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
export MULTIDETECT_SITL_SLOTS=8 MULTIDETECT_SIM_FIRST_FRAME_WAIT_S=240
export MULTIDETECT_SIM_GPU=1 MULTIDETECT_SIM_GPU_DEVICES=1 MULTIDETECT_SIM_NETWORK=bridge
unset MULTIDETECT_SIM_LISTEN_HOST
export MULTIDETECT_SIM_FUEL_RESOLVED_ROOT=$PWD/.tmp/realworld-wide-6km/models
RW_FLIGHT_OUT="$PWD/.tmp/flights/rw-farm-w6km-cessna-slot7-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$TMPDIR" "$RW_FLIGHT_OUT"
/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python scripts/realworld/leased_flight.py 7 rc_cessna_mono_cam rw_farm_iowa_w6km__clear_noon "$RW_FLIGHT_OUT" 2>&1 | tee "$RW_FLIGHT_OUT/flight.log"
)
```

请保留整份输出目录（`flight.log`、`episodes/`、`rotors_in_flight.json`）以及实测指标原始文件。`leased_flight.py` 当前不自动产生 `rtf_sim_wall`；若另外采样 Gazebo stats，请一并提供采样窗口、相机数量/频率、首帧时间、显存峰值、主机负载和 metrics 路径。现有 `shoot_scene.py` 固定读共享 resolved-root，不能直接用它为这两个隔离世界测量；需先增加显式模型根入口。静态拍摄 1 Hz 和机载 30 Hz 的 RTF 不可混为同一个结果。此冒烟只验证已有起飞/重定位/返航/降落链路，不证明覆盖完整 6 km 航线。

## LOD、预算与已知限制

- 地形视觉网格默认 4 m、碰撞网格 8 m；重复树木、道路设施和远景道具合并进网格。`prop_instances` 是合并前道具实例数，两份 world 实际均只有 3 个 model（含嵌套模型）和 32 个 actor，不能把约 2200 个道具实例误称为独立 Gazebo 模型。
- 任务边界、DEM 方框和 GeoJSON operating-area 由同一 `extent_m` 派生。辅助 teacher 障碍盒保留历史 350 m cap；较远合并建筑仍有实际网格碰撞，却不自动变成 teacher 的 bounds。
- 2 km：4,818,826 三角形，道具 3,999,989，地形/道具贴图估算 1.077 GiB，网格缓冲估算 520,433,208 B；两者合计下限 1.562 GiB。
- 6 km：12,692,321 三角形，道具 3,999,991，贴图估算 1.314 GiB，网格缓冲估算 1,370,770,668 B；两者合计下限 **2.590 GiB，已超过 2 GiB 目标**，尚未计天空、相机、阴影和驱动。`budget_ok=true` 仅表示道具三角形不超过 4,000,000，不代表显存通过。2 km 也未证明实际显存通过。当前保留既有生成物作对照，未做预算优化。
- 天空穹顶仍是既有半径 2600 m，6 km 场景边缘可能越出穹顶；完整范围的地平线和低空细节需要后续渲染检查。
- 远场贴图降采样不提高源精度；全球 provider 的 Sentinel-2/Copernicus 仍只适合宏观环境。10 km 尚未构建验证。
- 本任务未启动 Docker、Gazebo、SITL 或 GPU 工作，没有声称看到画面、飞行通过或取得 RTF。Iowa 既有高度估计不稳定和相机流超时记录仍需以本次独立实测判断。
