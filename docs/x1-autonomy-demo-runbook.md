# X1 一句话自主飞行演示

本 lane 的验收覆盖 CPU 上的规划、任务包和实际 DEM 准入、collector 配置与回执解析。首次联跑使用 `delta_tailsitter_gimbal_cam`、一个已登记 POI、一次固定翼过点和垂直返航着陆。模型决定引用、AGL 和步骤图。任务包只提供场地事实、地名与约束。当前 `x1_mission` 执行适配器接收单航点和相同的起飞/航点 AGL。多航点、盘旋观察、找物和云台凝视需要各自的执行适配器。

## 算力线交付物

- 提供本地可供 `llama-server` 文本推理的 Altair/r24 风格合并 GGUF、SHA-256、起始 checkpoint、SFT 数据和 prompt/schema 版本，以及可执行文件路径与版本。此 runner 使用无 mmproj、排序键、结构化 decision 合同和一次受 schema 约束的请求。
- 保存 `scripts/plan_from_task.py --no-mmproj --sorted-keys --decision-contract` 的独立 X1 holdout 原始结果和 `scripts/x1_plan_eval.py` 的汇总。覆盖单 POI 五步合法链、显式 AGL、未知 POI、越界任务及 `reject` / `clarify`。交付本页 Iowa 句子的实际模型输出及校验结果。模型推理建议先在 CPU 运行，使渲染 GPU admission 与规划分离。
- 若 r24 base 未生成 X1 技能，先检查加载的 GGUF SHA、`airframe:x1`、`x1_planning`、POI 名称、排序 schema 和 decision 合同。再用同样的上下文检查 raw JSON，区分缺技能、错引用、错 AGL 和几何拒绝。缺技能时用 `scripts/build_x1_plan_sft.py` 生成版本匹配的样本，独立评测新 checkpoint。若模型 raw 合法而 dry-run 拒绝，查看 `validator.code`、`validator.findings`、站点/DEM SHA、runout 与当前适配器的技能范围。

## 首轮 Iowa 命令

在合入补丁的工作树根目录运行。把本 lane 的 `iowa-context.json` 放到该树的 `.tmp/x1-demo-inputs/`。其中 `poi:yard` 为示范用场地引用，距 home 向北 320 m。它与算力线的模型 checkpoint 无关。

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR="$PWD/.tmp"
mkdir -p "$TMPDIR/x1-iowa-world"
site=/data/datasets/multidetect-sim-assets/wide-collect/assets/rw_farm_iowa_w6km
world=rw_farm_iowa_w6km__clear_noon
cp "$site/x1-baseline/worlds/$world.sdf" "$TMPDIR/x1-iowa-world/"
printf '%s  %s\n' 4e09aa4febff6b78568201a94bc4552ac5bbc6420f2c128d0784e1845bb9999c \
  "$TMPDIR/x1-iowa-world/$world.sdf" | sha256sum -c -
export MULTIDETECT_RTF_WORLD_ROOT="$TMPDIR/x1-iowa-world"
export MULTIDETECT_SIM_FUEL_RESOLVED_ROOT="$site/restored/models"
py=/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python
checkpoint=/ABSOLUTE/PATH/TO/DELIVERED-X1-PLANNER.gguf
server=/ABSOLUTE/PATH/TO/llama-server
common=(--world rw_farm_iowa_w6km
  --scenario "configs/scenarios/realworld/delta_tailsitter_gimbal_cam/$world.scenario.json"
  --mission-package "$TMPDIR/x1-demo-inputs/iowa-context.json"
  --sentence 'Fly over the yard at 85 m AGL, then return home and land.'
  --checkpoint "$checkpoint" --llama-server "$server" --planner-device cpu
  --planner-port 9957 --slot 7)
"$py" scripts/run_x1_autonomy_demo.py "${common[@]}" --plan-only --output "$TMPDIR/x1-iowa-model-plan"
jq '{validator,plan_json,prepared:.prepared.steps}' "$TMPDIR/x1-iowa-model-plan/result.json"
```

检查 `accepted`、POI、AGL、模式顺序和场地预检。确认对应起降点和巡航走廊的场地条件，随后在验证飞行释放资源并获准进行 SITL 后执行。

```bash
set -o pipefail
"$py" scripts/run_x1_autonomy_demo.py "${common[@]}" --output "$TMPDIR/x1-iowa-first-demo" \
  2>&1 | tee "$TMPDIR/x1-iowa-first-demo.log"
jq '{validator,flight,safety,markers,gpu_admission}' "$TMPDIR/x1-iowa-first-demo/result.json"
```

runner 重新调用模型，核对 `gz-rendering-830-r1-gimbal-horizon4` 镜像并取得 X1 flight lock、slot 与 GPU UUID 租约，随后执行 collector。命令行 slot 只能选 5–7，默认 7。端口和输出目录应未占用。回执用 `fixed_wing_takeoff_complete`、固定翼过点插值证据、回到 home 的稳定位置和 `collector_landing_complete` 的 MC/landed 状态判定。保存 `result.json`、日志和该目录中的 episode 遥测。`safety.observed` 的 AGL、走廊、半径与禁飞裕量来自固定翼段采样。预检覆盖该单航点走廊的 DEM、围栏、runout、禁飞和任务预算。该估计不提供障碍物全量证明，也不构成实飞授权。

CPU 重放可以把 checkpoint 三项替换为 `--recorded-output "$TMPDIR/x1-demo-inputs/recorded-plan.json" --plan-only`，并选用空闲的输出路径。真实 Iowa SDF 和 DEM 的本轮记录保存在 `.tmp/x1-iowa-cpu/result.json`。
