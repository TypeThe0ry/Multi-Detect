# 6 km 真实世界 RTF A/B 实验

本页描述 `rw_farm_iowa` 6 km 世界的实时性变体。Docker simulator plan 仅对名称匹配 `*_w6km__*` 的世界默认挂载 V6 相机 overlay；其他世界和机型配置不变。实验变体仍写入指定的 `.tmp` 输出目录。

## 6 km 默认配置

版本管理的模型根为 `simulation/gazebo/models-wide-area/`，内容取自 V6 overlay。没有设置 `MULTIDETECT_RTF_MODEL_ROOT` 时，planner 只在 world 文件名匹配 `*_w6km__*` 时自动选择该目录；显式设置该变量时，显式目录优先。plan 的只读 mount 记录包含 overlay 文件树 SHA-256，并纳入 plan evidence hash。

overlay 将 RGB 和深度相机的 `update_rate` 设为 10 Hz，保持 1280×960 分辨率与原相机内参；其他模型和场景属性不变。V6 不包含 V7 的建筑碰撞移除，地形与建筑碰撞均保留。

## 变体矩阵

| 变体 | 变化 | 真实地形 | 独立输出 |
|---|---|---|---|
| V0 | 原物理、阴影、1280×960、30 Hz 相机 | 完整 6 km | `variant.json` 中 `overlay_root=null` |
| V1 | 作废：640×480 被 CameraInfo 标定校验拒绝 | 不运行 | 保留历史定义，不作为候选 |
| V2 | 关闭场景和太阳阴影；相机保持 1280×960、30 Hz | 完整 6 km | 独立 world、overlay |
| V3 | 旧文档中的预算 profile 方案；当前 `rtf_variants.py` 没有 V3 CLI 入口 | 保持原 DEM 和碰撞地形 | 仅历史记录 |
| V4 | 作废：包含被拒绝的 640×480 相机配置 | 不运行 | 保留历史定义，不作为候选 |
| V5 | `budget_6km`：视觉网格 8 m，中央 600 m 方区保留 2 m 网格；候选模型根在共享缓存前只读挂载 | 8 m DEM 碰撞保持原样 | `.tmp/rtf-speedup-run/variants/V5` |
| V6 | 1280×960、原标定，仅将 RGB/深度相机降至 10 Hz | 完整 6 km | 独立 world 和相机 overlay；`*_w6km__*` 默认项 |
| V6_5 | V6 子变体：1280×960、原标定，仅降至 5 Hz | 完整 6 km | 独立 world 和相机 overlay |
| V7 | 移除合并建筑碰撞，保留地形碰撞 | 地形精度不变 | 独立 world |
| V8 | V6 + V7：1280×960、原标定，RGB/深度 10 Hz，并移除合并建筑碰撞 | 完整地形碰撞保留 | `.tmp/rtf-speedup-run/variants/V8` |

V5 使用现有 `budget_6km` 生成结果，profile 元数据要求 extent=6000 m、视觉网格 8 m；模型中的中心 600 m 方区仍用 2 m 网格，地形碰撞仍用 8 m DEM 网格。它降低视觉几何/纹理预算，不改变地形高度数据。当前 3 km 原始下载不能重建 6 km，故本轮复制并校验已有的 6 km profile 模型到 `.tmp`。矩阵将 V5 模型根只读挂到 `/opt/multidetect-rtf-models`，并放在共享 Fuel 模型缓存前，避免同名基线模型遮住候选 LOD。PX4 lockstep、传感器协议和默认 airframe 参数没有在本任务中改写。

V6/V6_5 仅改 RGB 和深度传感器的 `update_rate`；宽高、FOV、噪声、可视化状态及相机 wrapper 均保持原值。采集器逐帧按尺寸并按 K/P、R、D 矩阵校验 CameraInfo，频率变化不改标定。V7 只去掉 `rw_terrain/buildings` 的合并碰撞，完整保留 `rw_terrain/ground`；近场地形和跑道接地面不变，建筑物碰撞交互会消失，起降是否正常仍须真实飞行确认。

## 生成方式

先准备已有 6 km world、scene 和模型缓存。所有大文件写入数据盘或 `.tmp`，不要写 `artifacts/`：

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
export PY=/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python
export BASE_WORLD=$PWD/simulation/gazebo/worlds/rw_farm_iowa_w6km__clear_noon.sdf
export BASE_SCENARIO=$PWD/configs/scenarios/realworld/rc_cessna_mono_cam/rw_farm_iowa_w6km__clear_noon.scenario.json
$PY scripts/realworld/rtf_variants.py --source-world "$BASE_WORLD" --scenario "$BASE_SCENARIO" --airframe rc_cessna_mono_cam --variant V2 --out .tmp/rtf-variants/V2
export VARIANTS=.tmp/rtf-speedup-run/variants
$PY scripts/realworld/rtf_variants.py --source-world "$BASE_WORLD" --scenario "$BASE_SCENARIO" --airframe rc_cessna_mono_cam --variant V5 --budget-model-source /data/datasets/multidetect-sim-assets/wide-area-budget-6km-v2/models/rw_farm_iowa_w6km_budget --out "$VARIANTS/V5"
$PY scripts/realworld/rtf_variants.py --source-world "$BASE_WORLD" --scenario "$BASE_SCENARIO" --airframe rc_cessna_mono_cam --variant V6 --out "$VARIANTS/V6"
$PY scripts/realworld/rtf_variants.py --source-world "$BASE_WORLD" --scenario "$BASE_SCENARIO" --airframe rc_cessna_mono_cam --variant V6_5 --out "$VARIANTS/V6_5"
$PY scripts/realworld/rtf_variants.py --source-world "$BASE_WORLD" --scenario "$BASE_SCENARIO" --airframe rc_cessna_mono_cam --variant V7 --out "$VARIANTS/V7"
```

V1/V4 保留为历史定义，不作为运行候选；V3 没有 CLI 变体。生成器会在各自输出目录写独立 world、map、DEM 和 scenario channels。V5 还会复制经过 profile 校验的地形模型和未修改 airframe wrapper；V6/V6_5 的相机模型只变 `update_rate`。实际运行前检查 manifest 中 world、map、DEM、相机 overlay 和 V5 `variant_model_root` 均指向同一变体。

## 测量矩阵

矩阵脚本使用 `RTF_MATRIX_SLOT` 指定 slot（仅允许 5/6/7，默认 7），固定 GPU 1，且要求 `MULTIDETECT_SITL_SLOTS>=8`。每个变体依次运行长航程固定翼，再用只读 `sample_stats.py` 订阅 `/world/default/stats`，记录 `Δsim/Δreal`、主机 load/pressure、GPU1 显存和可归因容器进程显存。容器名前缀带 `-slot-N-`；容器名会在结果中再次校验。变体之间有间隔；GPU1 有任何外部进程或 CPU load 超过阈值时等待，不杀进程、不改 cgroup。变体 world/map/DEM 从显式 `.tmp` 根目录解析；V5 的 `variant_model_root` 额外只读挂载到专用 Gazebo 搜索目录并排在 Fuel 缓存之前，不在共享资产目录安装或覆盖文件。不完整轮次会保留在 `attempt-N/` 并最多重跑 3 次。

manifest 示例（当前长航程入口固定使用 6 km `rw_*` 世界、最低距离 4000 m）：

```json
[
  {"name":"V0_01","variant":"V0","scenario":"configs/scenarios/realworld/rc_cessna_mono_cam/rw_farm_iowa_w6km__clear_noon.scenario.json","resolved_root":"artifacts/cache/gz-fuel/resolved","extent_m":6000,"minimum_max_distance_m":4000,"minimum_rtf":0.9},
  {"name":"V5_01","variant":"V5","scenario":".tmp/rtf-speedup-run/variants/V5/scenario/rw_farm_iowa_w6km__rtf_v5__clear_noon.scenario.json","resolved_root":"artifacts/cache/gz-fuel/resolved","overlay_root":".tmp/rtf-speedup-run/variants/V5/models","variant_model_root":".tmp/rtf-speedup-run/variants/V5/models","variant_world_root":".tmp/rtf-speedup-run/variants/V5/worlds","variant_map_root":".tmp/rtf-speedup-run/variants/V5/assets/maps","variant_terrain_root":".tmp/rtf-speedup-run/variants/V5/assets/terrain","extent_m":6000,"minimum_max_distance_m":4000,"minimum_rtf":0.9}
]
```

本轮 manifest 以 `V0,Vx,V0,Vx,V0` 顺序交错四个候选，共 17 个计划轮次。主机 load1 较高时仍记录每轮 load1，以相邻 V0 归一化；运行时可提高绝对 load 门限，不影响 GPU 占用检查：

```bash
export MULTIDETECT_SITL_SLOTS=8
export RTF_MATRIX_SLOT=6
export RTF_MATRIX_CPU_MAX=100
export RTF_MATRIX_MAX_ATTEMPTS=3
scripts/realworld/rtf_matrix.sh --manifest .tmp/rtf-speedup-run/manifest.json --out .tmp/rtf-speedup-run/measurements
```

无人值守时可使用：

```bash
systemd-run --user --unit=multidetect-rtf-matrix --collect --working-directory="$PWD" scripts/realworld/rtf_matrix.sh --manifest .tmp/rtf-matrix.json --out .tmp/rtf-matrix-run
```

SITL 长航程脚本使用 `/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python`；变体生成和 stats 采样使用数据盘工具环境。脚本只结束自己记录的 flight PID。

## 判定标准与限制

合格条件是稳定窗口 `rtf_window_median >= minimum_rtf`（默认 0.9）、路线完成、最大距离至少 4000 m（本入口只接受 6 km `rw_*` 场景）。`rtf_matrix.sh` 会把低于阈值或缺失的 stats 轮次标为无效。同时应保存 `stats.json`、`result.json`、`summary.json`、完整 flight 输出、主机压力采样和显存采样。没有 Gazebo stats 窗口、显存峰值或完整路线时，结果只能标为未完成。

## V0–V8 实测结论

完整飞行和 stats 记录显示，V6 初测两轮为 RTF 0.706、0.799；随后在交错 V0 基线的夹击复测中，V6 达到 0.947、1.000，相邻 V0 为 0.359、0.545，第三个 V0 锚点为 0.615。两次夹击 V6 航线均完成，最远距离分别为 4086.7 m 和 4088.9 m。近场地形代理比较的 V6−V0 中位偏差为 −0.16 m。V0 的结果随运行负载波动，因此这里报告夹击值，不把它解释为所有机器上的固定倍数。

| 变体 | 实测结论 |
|---|---|
| V0 | 基线夹击 RTF 为 0.359、0.545、0.615；路线均完成。 |
| V1 | 640×480 会被 CameraInfo 标定校验拒绝，不作为候选。 |
| V2 | 本轮没有可用于比较的完整结果；关闭阴影方案不作为默认项。 |
| V3 | 当前 `rtf_variants.py` 没有 V3 生成入口，未测。 |
| V4 | 含有被拒绝的 640×480 配置，不作为候选。 |
| V5 | 两轮 RTF 为 0.355、0.335；航线完成，距离 4085.2 m、4106.5 m。 |
| V6 | 初测为 0.706、0.799；夹击复测为 0.947、1.000。夹击两次均完成且距离大于 4085 m，近场地形代理中位偏差 −0.16 m。选为 6 km 默认相机配置。 |
| V6_5 | RTF 接近 1.000，但重试航线均未完成；不采用 5 Hz。 |
| V7 | 两轮 RTF 为 0.610、0.584，航线完成；因移除了建筑碰撞，不并入 V6 默认配置。 |
| V8 | 两轮 RTF 为 0.928、0.995，航线完成且距离大于 4085 m；它包含 V7 的建筑碰撞变化，作为独立实验保留。 |

以上 RTF 为 `rtf_window_median`。完整轮次按 `route_completed=true`、有效最远距离和无 stats 错误筛选；plan-only 结果不计入实测。原始结果和地形代理报告位于 `.tmp/rtf-speedup-run/`。渲染引擎 Ogre2 的容器级参数和 PX4 lockstep 仍是独立控制项。
