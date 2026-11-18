# 6 km 真实世界 RTF A/B 实验

本页描述 `rw_farm_iowa` 6 km 世界的实时性变体。Docker simulator plan 仅对名称匹配 `*_w6km__*` 的世界默认启用 V9 server config 和既有 V6 相机 overlay。实验变体仍写入指定的 `.tmp` 输出目录。

## 6 km 默认配置

planner 按 world 文件名（不含扩展名）匹配 `*_w6km__*`，分别解析两项配置：

| 配置 | 匹配时的默认路径 | 独立显式覆盖 | 容器只读挂载 |
|---|---|---|---|
| V6 相机 overlay | `simulation/gazebo/models-wide-area/` | `MULTIDETECT_RTF_MODEL_ROOT`（目录） | `/opt/multidetect-rtf-models` |
| V9 server config | `simulation/gazebo/server-wide-area.config` | `MULTIDETECT_RTF_SERVER_CONFIG`（文件） | `/opt/multidetect-rtf-server.config` |

每项的显式设置仅覆盖本项，不影响另一项；不匹配的世界在无显式设置时不启用这两项挂载。plan 的 `mounts.rtf_model_overlay.sha256` 记录 overlay 文件树 SHA-256，`mounts.rtf_server_config.sha256` 记录配置文件内容 SHA-256，两项挂载记录均纳入 plan evidence hash。

V6 overlay 将 RGB 和深度相机的 `update_rate` 设为 10 Hz，保持 1280×960 分辨率与原相机内参。V9 在此基础上移除 `GstCameraSystem` 和 `OpticalFlowSystem`；本采集路径仍使用 Gazebo Transport 的图像 topic，不依赖 PX4 的 GStreamer UDP 视频流，`rc_cessna_mono_cam` 不使用光流。V9 不采用 V7 的建筑碰撞改动，保留地形和建筑碰撞，不改物理步长或 PX4 lockstep。默认配置文件与 `.tmp/rtf-speedup-run/variants/V9/server.config` 逐字节一致；与原始配置的比对见 `.tmp/rtf-speedup-run/v9-default.md`。

## 变体矩阵

| 变体 | 变化 | 真实地形 | 独立输出 |
|---|---|---|---|
| V0 | 原物理、阴影、1280×960、30 Hz 相机、原 server config | 完整 6 km | `variant.json` 中 `overlay_root=null`；复现时须覆盖 6 km 默认项 |
| V1 | 作废：640×480 被 CameraInfo 标定校验拒绝 | 不运行 | 保留历史定义，不作为候选 |
| V2 | 关闭场景和太阳阴影；相机保持 1280×960、30 Hz | 完整 6 km | 独立 world、overlay |
| V3 | 旧文档中的预算 profile 方案；当前 `rtf_variants.py` 没有 V3 CLI 入口 | 保持原 DEM 和碰撞地形 | 仅历史记录 |
| V4 | 作废：包含被拒绝的 640×480 相机配置 | 不运行 | 保留历史定义，不作为候选 |
| V5 | `budget_6km`：视觉网格 8 m，中央 600 m 方区保留 2 m 网格；候选模型根在共享缓存前只读挂载 | 8 m DEM 碰撞保持原样 | `.tmp/rtf-speedup-run/variants/V5` |
| V6 | 1280×960、原标定，仅将 RGB/深度相机降至 10 Hz | 完整 6 km | 独立 world 和相机 overlay；`*_w6km__*` 默认项 |
| V6_5 | V6 子变体：1280×960、原标定，仅降至 5 Hz | 完整 6 km | 独立 world 和相机 overlay |
| V7 | 移除合并建筑碰撞，保留地形碰撞 | 地形精度不变 | 独立 world |
| V8 | V6 + V7：1280×960、原标定，RGB/深度 10 Hz，并移除合并建筑碰撞 | 完整地形碰撞保留 | `.tmp/rtf-speedup-run/variants/V8` |
| V9 | V6 + 移除 `GstCameraSystem`、`OpticalFlowSystem` | 完整 6 km，含建筑碰撞 | 独立 world、相机 overlay、`server.config`；`*_w6km__*` 默认组合 |

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

V9 可从基线 world 和 scenario 一步独立生成，不需要先生成 V6。沿用上面的环境变量；复现指定镜像的插件配置时，将 `BASE_SERVER_CONFIG` 指向从固定镜像导出的原始 `server.config`：

```bash
$PY scripts/realworld/rtf_variants.py --source-world "$BASE_WORLD" --scenario "$BASE_SCENARIO" --airframe rc_cessna_mono_cam --variant V9 --server-config-source "$BASE_SERVER_CONFIG" --out .tmp/rtf-variants/V9
```

生成器从输入配置移除这两个插件，缺少任一个则报错；省略 `--server-config-source` 时可离线使用内置模板，但不能据此断言它与固定镜像的原配置只有这两处差异（参见 `.tmp/rtf-speedup-run/v9-notes.md`）。输出包含相机 overlay、world、map、DEM、scenario 和 `server.config`；`variant.json` 记录 `overlay_root` 和 `server_config`。矩阵 manifest 的 `server_config` 应指向该次生成的文件，其他变体路径也应取自同一输出根。

## 测量矩阵

矩阵脚本使用 `RTF_MATRIX_SLOT` 指定 slot（仅允许 5/6/7，默认 7），`RTF_MATRIX_GPU` 选 GPU 1 或 3（默认 1），且要求 `MULTIDETECT_SITL_SLOTS>=8`。每个变体依次运行长航程固定翼，用只读 `sample_stats.py` 订阅 `/world/default/stats`，记录 RTF、主机 load/pressure、所选 GPU 总显存占用和可归因容器进程显存。容器名前缀带 `-slot-N-`；容器名会在结果中再次校验。默认在所选 GPU 有外部进程、显存占用过高或 CPU load 超阈值时等待；显式启用 `RTF_MATRIX_SHARE_GPU=1` 时以可用显存取代 GPU 独占检查，CPU 门限仍生效。不杀外部进程、不改 cgroup。变体 world/map/DEM 从显式 `.tmp` 根目录解析；V5 的 `variant_model_root` 只读挂载并排在 Fuel 缓存之前，不覆盖共享资产。不完整轮次保存在 `attempt-N/`，默认最多尝试 3 次。

manifest 示例（6 km `rw_*` 场景，最低距离 4000 m；将 `/path/to/…` 替换为实际基线资产）：

```json
[
  {"name":"V0_01","variant":"V0","scenario":"configs/scenarios/realworld/rc_cessna_mono_cam/rw_farm_iowa_w6km__clear_noon.scenario.json","resolved_root":"artifacts/cache/gz-fuel/resolved","variant_model_root":"/path/to/original-30hz-models","server_config":"/path/to/original-server.config","extent_m":6000,"minimum_max_distance_m":4000,"minimum_rtf":0.9},
  {"name":"V5_01","variant":"V5","scenario":".tmp/rtf-speedup-run/variants/V5/scenario/rw_farm_iowa_w6km__rtf_v5__clear_noon.scenario.json","resolved_root":"artifacts/cache/gz-fuel/resolved","overlay_root":".tmp/rtf-speedup-run/variants/V5/models","variant_model_root":".tmp/rtf-speedup-run/variants/V5/models","variant_world_root":".tmp/rtf-speedup-run/variants/V5/worlds","variant_map_root":".tmp/rtf-speedup-run/variants/V5/assets/maps","variant_terrain_root":".tmp/rtf-speedup-run/variants/V5/assets/terrain","server_config":"/path/to/original-server.config","extent_m":6000,"minimum_max_distance_m":4000,"minimum_rtf":0.9}
]
```

**复现历史对照时不能仅靠 `variant` 标签。** 新默认生效后，6 km world 省略 `server_config` 会使用 V9 配置，V0、V6 均需在该字段显式提供原始 server config。V0 还须通过 `variant_model_root` 指向原 30 Hz 模型根；V6 可继续使用默认的 10 Hz 相机 overlay。矩阵分别将这两个字段传给 `MULTIDETECT_RTF_SERVER_CONFIG` 和 `MULTIDETECT_RTF_MODEL_ROOT`。

历史 `.tmp/rtf-speedup-run/manifest.json` 以 `V0,Vx,V0,Vx,V0` 交错四个候选，共 17 个计划轮次。下例使用按上述规则补齐基线配置的复现 manifest；提高 CPU load 门限不会绕过 GPU 占用检查：

```bash
export MULTIDETECT_SITL_SLOTS=8
export RTF_MATRIX_SLOT=6
export RTF_MATRIX_CPU_MAX=100
export RTF_MATRIX_MAX_ATTEMPTS=3
scripts/realworld/rtf_matrix.sh --manifest .tmp/rtf-matrix-repro.json --out .tmp/rtf-matrix-repro
```

无人值守时可使用：

```bash
systemd-run --user --unit=multidetect-rtf-matrix --collect --working-directory="$PWD" scripts/realworld/rtf_matrix.sh --manifest .tmp/rtf-matrix.json --out .tmp/rtf-matrix-run
```

SITL 长航程脚本使用 `/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python`；变体生成和 stats 采样使用数据盘工具环境。脚本只结束自己记录的 flight PID。

## 判定标准与限制

合格条件是稳定窗口 `rtf_window_median >= minimum_rtf`（默认 0.9）、路线完成、最大距离至少 4000 m（本入口只接受 6 km `rw_*` 场景）。`rtf_matrix.sh` 会把低于阈值或缺失的 stats 轮次标为无效。同时应保存 `stats.json`、`result.json`、`summary.json`、完整 flight 输出、主机压力采样和显存采样。没有 Gazebo stats 窗口、显存峰值或完整路线时，结果只能标为未完成。

## V0–V9 实测结论

完整飞行和 stats 记录显示，V6 初测两轮为 RTF 0.706、0.799；随后在交错 V0 基线的夹击复测中，V6 达到 0.947、1.000，相邻 V0 为 0.359、0.545，第三个 V0 锚点为 0.615。两次夹击 V6 航线均完成，最远距离分别为 4086.7 m 和 4088.9 m。近场地形代理比较的 V6−V0 中位偏差为 −0.16 m。V0 的结果随运行负载波动，因此这里报告夹击值，不把它解释为所有机器上的固定倍数。

| 变体 | 实测结论 |
|---|---|
| V0 | 基线夹击 RTF 为 0.359、0.545、0.615；路线均完成。 |
| V1 | 640×480 会被 CameraInfo 标定校验拒绝，不作为候选。 |
| V2 | 本轮没有可用于比较的完整结果；关闭阴影方案不作为默认项。 |
| V3 | 当前 `rtf_variants.py` 没有 V3 生成入口，未测。 |
| V4 | 含有被拒绝的 640×480 配置，不作为候选。 |
| V5 | 两轮 RTF 为 0.355、0.335；航线完成，距离 4085.2 m、4106.5 m。 |
| V6 | 初测为 0.706、0.799；夹击复测为 0.947、1.000。夹击两次均完成且距离大于 4085 m，近场地形代理中位偏差 −0.16 m。保留为 6 km 默认相机配置；GPU3 上与 V9 交错实测的 V6 对照见下表。 |
| V6_5 | RTF 接近 1.000，但重试航线均未完成；不采用 5 Hz。 |
| V7 | 两轮 RTF 为 0.610、0.584，航线完成；因移除了建筑碰撞，不并入 V6 默认配置。 |
| V8 | 两轮 RTF 为 0.928、0.995，航线完成且距离大于 4085 m；它包含 V7 的建筑碰撞变化，作为独立实验保留。 |
| V9 | 两轮 `rtf_window_median` 为 0.937、0.906，航线均完成，最远 4084.8 m、4085.8 m，均通过 0.9 窗口门限。选为 6 km 默认 server config。 |

V0–V8 的历史数字为 `rtf_window_median`；历史 `valid_round` 标记不等同于通过当前 0.9 门限。原始数据见 `.tmp/rtf-speedup-run/measurements/matrix.jsonl`（初测）、`.tmp/rtf-speedup-run/measurements-v6/matrix.jsonl`（V0/V6 夹击）、`.tmp/rtf-speedup-run/measurements-v8/matrix.jsonl`（V0/V8 夹击）和 `.tmp/rtf-speedup-run/terrain-accuracy/report.json`（V6−V0 地形代理）。V6_5 的近 1.000 来自未完成航线的尝试，不能与完整飞行比较（`.tmp/rtf-speedup-run/v6_5-diagnosis.md`）；V2 缺可比结果，V1/V4 被拒绝、V3 未测。plan-only 结果不计入实测。

### GPU3 共享训练负载下的 V9/V6 对照

`.tmp/rtf-speedup-run/manifest-v9-ab2.json` 的前三轮按 V9 → V6 → V9 完成，实测见 `.tmp/rtf-speedup-run/measurements-v9ab2/matrix.jsonl`。仿真与训练共享 GPU3，背景主机 load1 约 60–160；这不是采样上限，三轮 `resource_samples` 中实际采样范围为 70.2–177.5。

| 轮次 | `rtf_window_median` | `rtf_delta_sim_over_delta_real` | 主机 load1 中位数 | 航线完成 / 距起点最远 | 0.9 窗口门限 |
|---|---:|---:|---:|---|---|
| `V9_z0` | 0.937 | 0.882 | 98.0 | 是 / 4084.8 m | 通过 |
| `V6_z1` | 0.477 | 0.467 | 138.2 | 是 / 4082.9 m | 未通过，仅 RTF 不达标 |
| `V9_z2` | 0.906 | 0.839 | 162.1 | 是 / 4085.8 m | 通过 |

门限判定取窗口中位数，不取累计 `Δsim/Δreal`；两次 V9 的后者均低于 0.9。计划中的末轮 `V6_z3` 仅有 `.tmp/rtf-speedup-run/measurements-v9ab2/V6_z3/attempt-1/stats.json`（窗口中位数 0.523），缺最终路线与距离结果，不计入完整轮次。早一批 `.tmp/rtf-speedup-run/measurements-v9ab/matrix.jsonl` 只有 V6 完整路线，没有同批 V9 完整结果；`.tmp/rtf-speedup-run/measurements-v9ab-bad/matrix.jsonl` 中的启动失败也不用于比较。

这组交错结果支持在该共享负载与航线下采用 V9；负载并未锁定，不能从中推导固定加速倍数，也不能把历史 GPU1 的 V0–V8 数字和本组 GPU3 数字当作同条件排名。Ogre2 的容器级参数与 PX4 lockstep 仍是独立控制项。
