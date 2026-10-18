# 新组合仿真闭环验收运行手册

本手册验收 YOLOE v3 候选框、r12 身份、r14 规划、步间三角化，以及可选近距深度的组合。验收脚本只编排现有评测入口，不修改规划或身份 prompt，也不启动 Docker/Gazebo。

## 配置

测试世界配置：

- `configs/onboard/sim-loop-v3-r12id-r14plan-triangulation.json`
- `configs/onboard/sim-loop-v3-r12id-r14plan-triangulation-near-depth.json`
- `configs/onboard/sim-loop-v3-r12id-r14plan-no-triangulation.json`（三角化对照）

任务套件对应配置：

- `configs/onboard/mission-suite-v2-v3-r12id-r14plan-triangulation.json`
- `configs/onboard/mission-suite-v2-v3-r12id-r14plan-triangulation-near-depth.json`
- `configs/onboard/mission-suite-v2-v3-r12id-r14plan-no-triangulation.json`

任务套件配置是基线套件的声明式包装，验收脚本会把它们物化为完整 suite JSON 后传给 `scripts/run_mission_suite.py`。

## Windows dry-run

```powershell
New-Item -ItemType Directory -Force .tmp | Out-Null
$env:TEMP=$env:TMP=$env:TMPDIR="D:\Projects\mdwt\simeval\.tmp"
$env:CUDA_VISIBLE_DEVICES="-1"
$env:PYTHONPATH="src"
python scripts/run_final_sim_acceptance.py --dry-run --out output/final-sim-acceptance
```

当前工作区拒绝创建 `.tmp` 时，可临时使用仓库已有的 `tmp` 目录运行 dry-run；这不会改变验收逻辑。

dry-run 会打印 3 个变体的完整命令：6 个测试世界各 2 架次、完整 v2 任务套件的 14 个任务、1 个地面高度专项，共 81 个调度单元。按 `CHANGELOG.md` 记录的 12 架次历史闭环耗时，保守按每单元 20 分钟估算，总计约 27.0 小时，未含失败重试和模型冷启动。

## 执行与断点续跑

```powershell
python scripts/run_final_sim_acceptance.py --out output/final-sim-acceptance
```

每个测试世界架次、每个任务和高度专项结束后写入 `complete` 标记及 JSON 报告。再次运行会跳过已完成标记。脚本在每次调度前检查磁盘、可用内存，并用 `.gpu.lock` 保证同一时刻只有一个 GPU 任务。当前约束要求 CPU dry-run，实际仿真主机再执行上述无 `--dry-run` 命令。

## 坡地近似专项

现有 held-out 测试世界按平地生成。专项复制三角化配置，将 `ground_height_sigma_m` 调为 5 m，并把机上地面模型设成比真实地面高 2 m 的平面（`ground_model: {type: flat, ground_up_m: 2}`；早期版本写的 `ground_height_bias_m` 没有任何代码读取，等于没注入），评估地面高度未知带来的退化。该方法是仿真近似，不等价于真实坡地或真实地形传感器误差；报告单独列出，不混入正常成功率。

## 报告和判定

输出：

- `final-report.json`：结构化结果、来源字段定位误差、历史基线说明；
- `final-report.md`：中文表格，列出成功率、误确认率、ground/triangulation/fused 定位误差、计数准确度和平均耗时。

三角化默认开启的判定条件是：相对 no-triangulation 对照成功率不下降、误确认率不升高、triangulation/fused 定位误差改善，并且计数准确度无回归。near_depth 只有在近距样本成功率改善、耗时满足平台预算且没有误确认回归时才默认开启。dry-run 只验证命令和矩阵，不产生性能结论；报告会保留待填写的结论栏位。

## 验证

```powershell
python -m pytest -q tests/test_final_sim_acceptance.py
python scripts/run_onboard_gate.py -q -p no:warnings
```
