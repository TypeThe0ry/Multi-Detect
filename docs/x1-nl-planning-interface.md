# X1 机载规划器设计与算力交接

## 从自然语言到任务包

多旋翼链路见 `CHANGELOG.md`（2026-10-15）、`docs/onboard-multipurpose-mission-design.md`、`docs/onboard-loop-sim.md`、`docs/mission-suite-v2.md` 和 `configs/onboard/sim-loop-v3-r21idr3-r24plan-defaults.json`。X1 沿用 `onboard_policy.build_mission_from_text → compile_mission_plan → MissionPlan → 任务包和执行准入`。输入任务包提供一句 `task_text`、地图引用和作业限制，计划步骤与参数由机载小模型产生。`build_x1_plan_sft.py` 通过先抽样结构化槽位、再分别渲染指令和答案来制作监督数据。运行时没有调用这个生成器的入口。

`airframe:x1` 由机载配置显式开启，只在解析成 `delta_tailsitter_gimbal` 或 `delta_tailsitter_gimbal_cam` 的配置中保留，还须出现在任务包的能力交集中。任务包继续用 `plan_version: 1` 和原有步骤图。`configs/onboard/x1-plan.schema.json` 是启用云台时的解码 schema 快照，`mission_planner.plan_json_schema` 按有效能力生成运行时 schema。`configs/onboard/x1-planner.json` 管理规划包络，机型物理参数从 `configs/airframes/delta_tailsitter_gimbal.json` 读取。`configs/schemas/` 中的产品证据和 GCS 的 C++ 包校验维持当前版本。此阶段走 Python SIM 任务包入口。地面站改派、签名与监督的现有边界见 `docs/gcs-mission-package-workflow.md` 与 `docs/mid-mission-retask.md`。本阶段没有启用 X1 的飞行中改派。

| 计划技能 | 模型槽位 | 执行含义 |
| --- | --- | --- |
| `vtol_takeoff` | `altitude_agl_m` | 垂直起飞至声明 AGL |
| `fw_transition` | 空 | 确认 MC 到 FW 过渡 |
| `fw_goto_waypoint` | `target_ref`, `altitude_agl_m` | 固定翼飞越任务包里的 `poi:*` 或 `home` |
| `loiter_observe` | `target_ref`, `altitude_agl_m`, `radius_m`, `duration_s` | 盘旋观察，对应 `fw_loiter_observe` |
| `find_object_then_return` | `region`, `query`, `altitude_agl_m`, `radius_m`, `duration_s` | 在已登记 `zone:*` 内限时取证，随后沿图进入正常返航 |
| `gimbal_stare_at` | `target_ref`, `altitude_agl_m`, `radius_m`, `duration_s` | 围绕已登记点、在有 `gimbal_control` 时凝视 |
| `back_transition` | 空 | 确认 FW 到 MC 过渡 |
| `return_home` | 空 | X1 终点含 `mc_return_to_home` 和 `vtol_land`，两条边均为 `null` |

正常路径必须依序起飞、前过渡、执行至少一个 FW 任务技能、后过渡、返航落地。每条失败边直接到终点，表示交回飞控监督器按实时状态恢复。模型无法据此强制进入 MC 或跳过模式确认。拒绝与澄清沿用结构化 `PlanDecision`。受支持措辞明确允许最近合法高度时可以把 AGL 钳制到 40–120 m。共享编译和执行准入检查显式 AGL 的一致性，无该许可时拒绝模型擅自更改。这个文本检查只验证已有输出。

## 任务包、包络与执行交接

任务包 `x1_planning` 由场景或操作员填入 `world`、`max_distance_from_home_m`、`corridor` 四个本地 NE 边界及 `terrain_rel_home_m` 高程上下界。地点与区域继续使用 `places`、`zones` 和双语 `ref_labels`。`x1_planner_context` 把相同的几何、禁飞区和预算送进 SFT、评测与运行时提示。AGL 相对当前位置地面。任务包的 `operating_area` 高度相对起飞点。Primitives lane 的 DEM 转换负责从 AGL 得到真实 NED。

- 当前规划帽为 1,500 m 距 home，生成数据使用 1,200 m。两者是套餐边界，可以按场景收紧。机型文件中的 `max_step_distance_m` 是局部控制步长，不是整条固定翼航腿。
- 巡航 15 m/s，最大空速 18 m/s，最大坡度 35°，前过渡最低高度 30 m。常规带为 40–120 m AGL。最短 FW 腿按 `max(50 m, cruise × 3 s)` 为 50 m。
- 盘旋半径 50–150 m，由最大空速和坡度再检查下限。观察时长 5–120 s。禁飞和走廊检查计算扫掠余量，按 primitives lane 的 `75 + 10 + 20 × (18 + 3)` 预留 505 m 过渡距离。距 home 检查包含余量和盘旋半径。静态估算还使用风裕量、巡航和悬停功耗、起降及两次过渡，校验时间和能量储备。实际禁飞区、地形包络和机型状态仍需在适配器及运行时重新验证。
- `check_plan_against_package` 是编译回调和执行准入共享的入口。X1 的引用、步骤图和限制失败会返回原因。普通 `MissionExecutor` 会拒绝 X1 执行，直到 collector 接入 X1 适配器。
- `one_waypoint_primitives_input` 与 `cx-x1-nl-prims/.tmp/x1-nl-prims-DESIGN.md` 的临时 `{goto, return_home}` 合同对齐。它收已经准入的单 POI、同一 AGL 的五步图，收紧任务包到声明走廊，给 `build_x1_mission_steps` 传结构化适配计划和 home 半径。模型图和原包不变。CPU 夹具用 sibling 转换器、参考树的 `MotionCommand` 和平坦 DEM 得到 `vtol_takeoff → fw_transition → fw_goto_waypoint → back_transition → mc_return_to_home → vtol_land`，FW NED `(215.0, 20.0, -43.0)`。多航点、盘旋、找物、云台凝视已有规划合同和训练标签，需由 primitives/collector lane 扩展执行。

## 数据与评分

| 划分 | 行数 | 场景 | 用途 |
| --- | ---: | --- | --- |
| X1 train | 2,400 | Iowa、Rotterdam、Davis、Santa Cruz | 1,200 中文，1,200 英文 |
| X1 val | 90 | Amsterdam | 训练期间检查 |
| X1 holdout | 480 | Zurich、Oakland | 最终评测，240 中文与 240 英文 |
| 混合 train | 7,860 | 5,460 r24 replay + 2,400 X1 | 保留多旋翼训练分布 |

地点别名取自相应 `rw_*` 环境主题，NE 和地形是合成夹具。场景、句式和搜索词按 split 隔离。归一化任务文本与现有 plan eval/probe、X1 各 split 及训练时提供的 replay 文件交叉检查。15 类包含单/双航点、盘旋、限时找物、云台、上下界钳制和拒绝/澄清。每套产物都有 `manifest.json` 和 SHA-256。

评测用 `scripts/plan_from_task.py` 的实际模型入口，X1 留出使用一次模型回包，关闭文本 pre-gate，统计原始严格 JSON、计划准入、合法响应、正例槽位精确匹配、负例正确拒绝/澄清，以及被准入的负例数。精确槽位按图遍历比对有序技能与全部参数，忽略步骤 ID 和目标描述文字。独立 `scripts/x1_plan_eval.py` 从评测报告复算，校验完整的 sample ID、文本和来源。CPU 标签对拍只检验数据与评分逻辑。

## 算力线执行说明

在应用本补丁的 compute 工作树内运行。真实场景和模型文件沿用本机缓存。算力线开始前记录源版本、llama-server 和模型摘要、GPU/量化设置、数据 manifest。历史 r24 corpus 的当前副本可作为 replay。其训练哈希与历史 checkpoint 不匹配，因此使用 `--init-adapter` 开启新优化器，不使用历史 `--resume`。

```bash
MD=/data/projects/Multi-Detect-env/Multi-Detect
A="$MD/artifacts"
CPU="$MD/.venv/bin/python"
VLM="$MD/.venv-vlm/bin/python"
RUN="$PWD/.tmp/x1-r24-warmstart"
R24="$A/models/slow-system-lora-v2-r24-planner-merged-gguf/slow-system-v2r24planner-merged-Q5_K_M.gguf"
INIT="$A/models/slow-system-lora-v2-r24-planner/adapter"
BASE="$A/cache/huggingface-vlm/models--Qwen--Qwen3-VL-2B-Instruct/snapshots/89644892e4d85e24eaac8bacfd4f463576704203"
LLAMA="$A/cache/llama-cpp"
MMP="$A/cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/52d6c8ffea26cc873ac5ad116f8631268d7eb503/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf"
mkdir -p "$RUN/tmp" "$RUN/eval"
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR="$RUN/tmp"
sha256sum "$R24" "$INIT/adapter_model.safetensors" | tee "$RUN/reference-sha256.txt"
"$CPU" scripts/build_x1_plan_sft.py --output "$RUN/data" \
  --replay-train "$A/scratch/cx-planner-round/r24/dataset/train.jsonl"
"$VLM" scripts/train_slow_system_lora.py --dataset "$RUN/data" --model "$BASE" \
  --init-adapter "$INIT" --output "$RUN/train" --epochs 1 --accumulation 4 \
  --rank 16 --alpha 32 --learning-rate 2.5e-5 --seed 20261011 \
  --max-input-tokens 2048 --max-new-tokens 768 --max-side 448 --crop-policy candidate_zoom_v2
"$VLM" scripts/export_slow_system_r15.py --base "$BASE" --adapter "$RUN/train/adapter" \
  --output "$RUN/export" --llama-root "$LLAMA" --quantization Q5_K_M
for side in r24 candidate; do
  gguf="$R24"
  if [ "$side" = candidate ]; then gguf="$RUN/export/slow-system-Q5_K_M.gguf"; fi
  for v in 3 4 5; do
    "$CPU" scripts/plan_from_task.py --gguf "$gguf" --sorted-keys --plan-pregate \
      --mmproj "$MMP" --llama-server "$LLAMA/b11552/llama-server" \
      --tasks "configs/onboard/plan-eval-tasks-v${v}.json" --output "$RUN/eval/$side-v$v.json"
  done
  "$CPU" scripts/plan_from_task.py --gguf "$gguf" --sorted-keys --no-mmproj \
    --llama-server "$LLAMA/b11552/llama-server" --tasks "$RUN/data/x1-holdout.json" \
    --output "$RUN/eval/$side-x1.json"
  "$CPU" scripts/x1_plan_eval.py --tasks "$RUN/data/x1-holdout.json" \
    --predictions "$RUN/eval/$side-x1.json" --output "$RUN/eval/$side-x1-scored.json"
done
```

- 参考 GGUF SHA-256 `147f90d878b6251c58af5149c640271d013aaa65487ca8321682752514acb616`。r24 缓存证据在 `$A/evaluation/r24-with-pregate/r24/summary.json` 及 `planning-v{3,4,5}.json`。v3、v4、v5 的精确数分别是 64/80、18/20、93/126，合法响应数分别是 80/80、20/20、123/126。
- 多旋翼准入要求同参数下每套的精确数、合法数及拒绝召回均不低于同机重跑 r24。核对缓存值作为额外下限，若工具漂移改变 r24 结果，记录差异后再决定晋级。保留每语言和类别的结果以检查退化。
- X1 晋级门槛建议为总体合法 JSON ≥99%、正例合法计划 ≥95%、正例槽位精确 ≥85%、负例正确决策 ≥95%、准入负例为 0。中英文各自正例槽位 ≥80%。在同一 X1 留出上保存 r24 对照。默认启用 X1 还须另做真实模型、执行适配器与 SITL 验收。
