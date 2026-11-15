# 6 km 世界预算优化与长航程实测方案

本文记录 `rw_farm_iowa_w6km_budget__clear_noon` 这份独立实验场景的生成、CPU 侧可行性检查和沙箱外实测方法。它不改变既有 `gen_*`、`variants`、默认采集流程或默认范围 profile。大模型、贴图和原始数据放在 `/data/datasets/multidetect-sim-assets/`，不进入 git；`artifacts/` 仍只读。

## 预算变体

新增 `budget_6km` profile，只能与 `extent_m=6000` 和独立输出名一起使用。它把 6 km 场景的视觉开销限制在一个可复查的预算内：

| 项目 | `budget_6km` | 默认 profile 行为 |
|---|---:|---|
| 近/中/远场贴图 | 2048 / 1024 / 512 px | 仍由范围 profile 派生 |
| 起飞点核心贴图 | 4096 px | 默认仍为 8192 px |
| 视觉网格 | 8 m | 默认仍为 4 m |
| 道具三角形上限 | 2,000,000 | 默认上限保持 4,000,000 |
| 近场半径 / 中场半径 | 850 m / 2,000 m | 不改变默认 profile |
| 6 km 天空半径 | 5,242.64 m | 默认世界仍保持 2,600 m |

天空半径由 `extent_m / sqrt(2) + 1000 m` 派生。只有显式传入 `--scale-sky-to-extent` 的新世界才将半径缩放到该值；旧命令不传这个开关时仍生成原来的 2600 m 穹顶。

本次已离线生成：

```text
场景：rw_farm_iowa_w6km_budget
世界：rw_farm_iowa_w6km_budget__clear_noon.sdf
模型根：/data/datasets/multidetect-sim-assets/wide-area-budget-6km-v2/models
输出根：/data/datasets/multidetect-sim-assets/wide-area-budget-6km-v2/
```

静态统计如下。`render_memory_lower_bound_gib` 只包括解码贴图和网格缓冲，未包括天空、相机、阴影、Gazebo/Ogre2 和驱动分配，因此不能代替显存实测。

| 指标 | 数值 |
|---|---:|
| 场景三角形 | 5,414,168 |
| 道具三角形 | 2,000,000 |
| 道具实例（合并前统计） | 1,646 |
| 解码贴图估算 | 0.535 GiB |
| 网格缓冲估算 | 584,730,144 B |
| 静态渲染下限 | 1.079 GiB |
| world 中 Gazebo model / actor | 3 / 32 |
| `model://` 引用缺失 | 0（1222 个引用全部找到） |

生成和审计命令（只使用已有缓存，不联网）如下：

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
PY=/data/datasets/multidetect-sim-assets/tools/venv/bin/python
RAW=/data/projects/Multi-Detect-env/mdwt/cx-rw-wide-fly/.tmp/realworld-wide-6km
OUT=/data/datasets/multidetect-sim-assets/wide-area-budget-6km-v2

$PY scripts/realworld/build_scene.py rw_farm_iowa \
  --raw-root "$RAW" --scene-root "$OUT/scenes" --output-root "$OUT/models" \
  --extent-m 6000 --output-name rw_farm_iowa_w6km_budget \
  --budget-profile budget_6km --max-trees 1200 --grid 8

RW_RESOLVED_ROOT="$OUT/models" $PY scripts/realworld/make_sky.py
RW_RESOLVED_ROOT="$OUT/models" $PY scripts/realworld/make_movers.py
RW_SCENE_ROOT="$OUT/scenes" RW_RESOLVED_ROOT="$OUT/models" RW_WORLD_OUT="$OUT/worlds" \
  $PY scripts/realworld/make_world.py rw_farm_iowa_w6km_budget \
  --weather clear_noon --world-site-name rw_farm_iowa_w6km_budget \
  --scenario-airframe rc_cessna_mono_cam --extent-m 6000 --scale-sky-to-extent

/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python \
  scripts/realworld/validate_wide_area.py \
  "$OUT/scenes/rw_farm_iowa_w6km_budget/scene.json" \
  --world "$OUT/worlds/rw_farm_iowa_w6km_budget__clear_noon.sdf" \
  --resolved-root "$OUT/models"
```

## 长航程固定翼任务

`configs/scenarios/realworld/rc_cessna_mono_cam/rw_farm_iowa_w6km_budget__clear_noon.scenario.json` 是独立 scenario。其 `.long_range.json` sidecar 由 `scripts/realworld/long_range_flight.py --write-mission` 生成，固定翼航线从出生点出发，经 9 个远端航点和着陆进近点返回出生点。最远航点距离出生点约 **4,030.5 m**，高于 4 km 目标。

边界来自同一个 `extent_m=6000`：任务方框为东西/南北各 ±3000 m，geofence 半径为方框对角线的一半（约 4242.64 m），最大任务半径留出 50 m 安全余量（约 4192.64 m）。航点、DEM/map sidecar、scenario runtime 和 safety policy 都使用这份范围，不依赖默认 2 km 或旧 smoke 航线。起飞高度修正为 40 m，满足 collector 的固定翼高度上限；长航程 plan 不声明已经实际飞行。

CPU-only plan 验证命令：

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
export MULTIDETECT_SITL_SLOTS=8
/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python \
  scripts/realworld/long_range_flight.py 7 \
  configs/scenarios/realworld/rc_cessna_mono_cam/rw_farm_iowa_w6km_budget__clear_noon.scenario.json \
  .tmp/long-range-budget --plan-only
```

已验证该命令生成 collector plan，编译任务包含 12 个 PX4 wire items，最大规划距离为 4030.5 m；没有租用 slot、启动 Docker、Gazebo、SITL 或 GPU。若以后执行 live 路径，必须使用 `.venv-vlm/bin/python`，并保持 `MULTIDETECT_SITL_SLOTS=8`，只允许使用 slot 5、6、7。

## 同步采样器

`scripts/realworld/sample_stats.py` 是只读采样器：它等待并锁定一个已存在的唯一容器 ID，然后在窗口内并行收集：

- 容器内 `/world/default/stats` 的 `simTime` 和 `realTime`；
- `nvidia-smi -i 1 -q -x` 的 GPU1 总显存使用量，以及通过 `docker top` 归属到该容器的进程显存；
- 主机 load average、CPU/memory/IO pressure；
- 窗口起止 monotonic 时间、原始样本和错误列表。

输出 JSON 同时给出 `delta_sim_s`、`delta_gazebo_real_s`、`delta_host_monotonic_s`、`rtf_delta_sim_over_delta_real` 和 `rtf_delta_sim_over_host_monotonic`。采样器要求至少两条 stats 和两条资源样本；检测到时间回退、暂停、窗口不重叠、stats 覆盖不足 80%、显存缺失或容器进程显存无法归属时返回非零。退出 0 只表示采样完整，不表示 RTF 或 2 GiB 预算达标。

## 沙箱外实测命令（slot 5–7、GPU1）

以下命令由用户在能运行 Gazebo/PX4 Docker 的沙箱外环境执行。每次使用新的输出目录，三次运行建议串行进行。`MULTIDETECT_SITL_SLOTS=8` 是总槽位数，不能改成 1 或 2；slot 3、4 永远不使用。脚本中的 Python 固定为带 torch/ultralytics 的 `.venv-vlm/bin/python`。

### 6 km 固定翼长航程与同步 stats

```bash
(
set -euo pipefail
cd /data/projects/Multi-Detect-env/mdwt/cx-rw-wide-budget
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
export MULTIDETECT_SITL_SLOTS=8 MULTIDETECT_SIM_FIRST_FRAME_WAIT_S=240
export MULTIDETECT_SIM_GPU=1 MULTIDETECT_SIM_GPU_DEVICES=1 MULTIDETECT_SIM_NETWORK=bridge
unset MULTIDETECT_SIM_LISTEN_HOST
export MULTIDETECT_SIM_FUEL_RESOLVED_ROOT=/data/datasets/multidetect-sim-assets/wide-area-budget-6km-v2/models
OUT="$PWD/.tmp/flights/rw-farm-w6km-budget-cessna-slot7-$(date -u +%Y%m%dT%H%M%SZ)"
/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python \
  scripts/realworld/long_range_flight.py 7 \
  configs/scenarios/realworld/rc_cessna_mono_cam/rw_farm_iowa_w6km_budget__clear_noon.scenario.json \
  "$OUT" 2>&1 | tee "$OUT/flight.log" &
flight_pid=$!
/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python \
  scripts/realworld/sample_stats.py \
  --container-prefix md-rw-long-slot-7 --wait-s 300 --gpu 1 \
  --warmup-s 30 --window-s 120 --interval-s 2 --out "$OUT/stats.json" &
sample_pid=$!
set +e
wait "$flight_pid"
flight_rc=$?
wait "$sample_pid"
sample_rc=$?
set -e
test "$flight_rc" -eq 0
test "$sample_rc" -eq 0
)
```

这里的两个后台进程都由该命令启动，`wait` 使用记录下来的 PID；采样器等待对应 collector 容器出现，但不负责启动或停止 Gazebo/PX4。若容器名不是此前缀，应将 `--container-prefix` 改为实际唯一前缀。采样窗口建议覆盖固定翼巡航阶段，避免把首帧加载、起飞和收尾混入 RTF 结论。

### 6 km VTOL 对照（slot 5）

```bash
(
set -euo pipefail
cd /data/projects/Multi-Detect-env/mdwt/cx-rw-wide-budget
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
export MULTIDETECT_SITL_SLOTS=8 MULTIDETECT_SIM_FIRST_FRAME_WAIT_S=240
export MULTIDETECT_SIM_GPU=1 MULTIDETECT_SIM_GPU_DEVICES=1 MULTIDETECT_SIM_NETWORK=bridge
unset MULTIDETECT_SIM_LISTEN_HOST
export MULTIDETECT_SIM_FUEL_RESOLVED_ROOT=/data/datasets/multidetect-sim-assets/wide-area-budget-6km-v2/models
OUT="$PWD/.tmp/flights/rw-farm-w6km-budget-vtol-slot5-$(date -u +%Y%m%dT%H%M%SZ)"
/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python \
  scripts/realworld/leased_flight.py 5 standard_vtol_mono_cam \
  rw_farm_iowa_w6km_budget__clear_noon "$OUT" 2>&1 | tee "$OUT/flight.log"
)
```

### 2 km 基线（slot 6）

2 km 基线应使用项目已有的独立 2 km 模型根和 world；不要把预算 6 km 模型根冒充基线。当前工作站已有一份可复用缓存，路径为 `/data/projects/Multi-Detect-env/mdwt/cx-rw-wide-fly/.tmp/realworld-wide-2km/models`，对应 scenario 名为 `rw_farm_iowa_w2km__clear_noon`。命令模板如下，换机器时只替换模型根和名称：

```bash
(
set -euo pipefail
cd /data/projects/Multi-Detect-env/mdwt/cx-rw-wide-budget
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
export MULTIDETECT_SITL_SLOTS=8 MULTIDETECT_SIM_FIRST_FRAME_WAIT_S=240
export MULTIDETECT_SIM_GPU=1 MULTIDETECT_SIM_GPU_DEVICES=1 MULTIDETECT_SIM_NETWORK=bridge
unset MULTIDETECT_SIM_LISTEN_HOST
export MULTIDETECT_SIM_FUEL_RESOLVED_ROOT=/data/projects/Multi-Detect-env/mdwt/cx-rw-wide-fly/.tmp/realworld-wide-2km/models
OUT="$PWD/.tmp/flights/rw-farm-w2km-vtol-slot6-$(date -u +%Y%m%dT%H%M%SZ)"
/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python \
  scripts/realworld/leased_flight.py 6 standard_vtol_mono_cam \
  rw_farm_iowa_w2km__clear_noon "$OUT" 2>&1 | tee "$OUT/flight.log"
)
```

若只测 6 km 长航程和 6 km VTOL，可跳过这条基线；若要比较 RTF，必须固定 GPU、相机数量/频率、天气和采样窗口，并保留每次的 `flight.log`、episode、`stats.json` 和主机负载原始文件。

## 数据来源、许可证与限制

本次没有新增 Python 依赖，也没有下载新的地理数据。Iowa 原始数据沿用已有 provenance：USGS 3DEP（美国政府作品，public domain）、USDA NAIP（美国政府作品，public domain）和 OpenStreetMap（ODbL-1.0，需保留贡献者署名）。

预算模型根额外复制了已有缓存中的行人 actor：Gazebo Fuel OpenRobotics `Walking person`，来源 URL 为 `https://fuel.gazebosim.org/1.0/OpenRobotics/models/Walking%20person`，许可证按缓存记录为 CC BY 4.0（`https://creativecommons.org/licenses/by/4.0/`），模型文件 `meshes/walk.dae` 的 SHA-256 为 `49af0df3a319d1cb8ca2cebf02dbd00f625e5d5bec820bc5e109925b18b65c6e`。该文件只复制到独立数据盘模型根，没有改写 `artifacts/`。

这条线没有渲染画面，也没有执行实际 Docker/SITL/Gazebo/GPU 飞行；因此不能声称画面质量、实际 RTF、显存峰值或 4 km 飞行已经通过。CPU plan 只证明配置、边界和 PX4 mission wire 编译可行。实际验收应以采样器的同步 `Δsim/Δreal`、GPU1 显存峰值、主机负载和长航程 episode 为准；任何静态预算都不包含驱动、相机和阴影分配。
