# r21-planner 一键训练评测

`scripts/run_planner_round.py` 将规划 SFT 数据、LoRA 训练、Q5_K_M GGUF 导出、planning-v3/v4 对比和 15 条返航探测串成可断点续跑的五阶段流水线。阶段输出写入 `round-state.json`，只有输出文件存在且 SHA-256 未变化才会跳过。训练和导出显式使用 `D:/Projects/Multi-Detect/.venv-vlm/Scripts/python.exe`；系统 Python 只负责数据构造与编排。低磁盘导出使用 `--drop-intermediates`，会保留导出账本并删除本轮归属的 merged-HF/F16 中间物。

## r21 准入故障与修复

失败根因在数据与准入契约不一致：r21 的能力审计 deliver 正例元数据声明了 `payload_drop`，但 prompt 没有该声明；负例 prompt 声明无载荷能力，却把 `deliver` 违规计划作为训练 gold。准入又对所有行使用空能力集，导致合法正例也被拒绝。另有能力/意图审计负例将违规候选直接作为 gold。

现在数据构建会按每行能力集生成真实规划 prompt。负例保留在数据中并记录 `rejected_candidate` 和预期 finding，但训练 gold 是结构有效的拒绝计划；训练准入按行能力集验证 gold，并核对拒绝候选与预期 finding 一致。不得通过跳过计划校验或删除审计负例绕过该契约。

如果旧数据阶段已完成，指定 `--force-stage data` 会在运行前清除 data 及后续阶段的完成状态，然后重新生成数据。为保留既有数据，建议输出到新目录：

```powershell
$env:PYTHONPATH='src;.'
$env:PYTHONUTF8='1'
python scripts/run_planner_round.py --stage data --force-stage data `
  --out D:/Projects/mdwt/_scratch/cx-planner-round/r21-fixed `
  --rows 2000 --val-rows 100 --intent-count 40
```

生成后可先单独运行 `train_slow_system_lora.py` 的离线准入检查；确认后再按正式运行章节启动完整流水线。`--force-stage data` 本身只运行 data 阶段，不会启动训练。

## Dry-run

```powershell
$env:PYTHONPATH='src;.'
$env:PYTHONUTF8='1'
python scripts/run_planner_round.py --stage all --dry-run --out D:/Projects/mdwt/_scratch/cx-planner-round/r21
```

该命令只打印阶段命令、解释器、输出路径、磁盘/内存门槛和锁路径，不训练、不导出、不启动 llama-server。可用 `--stage data --rows 10 --val-rows 2 --intent-count 1` 单独生成小样本数据验证 manifest/provenance。

## 正式运行

```powershell
$env:PYTHONPATH='src;.'
$env:PYTHONUTF8='1'
python scripts/run_planner_round.py --stage all `
  --out D:/Projects/mdwt/_scratch/cx-planner-round/r21 `
  --rows 2000 --val-rows 100 --intent-count 40 `
  --min-free-disk-gb 12 --min-memory-gb 8
```

预计数据构造少于 1 分钟；LoRA 训练约 2--4 小时（依数据量和 GPU）；导出约 1--3 小时；规划评测约 1--2 小时；报告少于 1 分钟。总墙钟约 4--9 小时。评测阶段会分别记录候选和 r14 的 planning-v3、planning-v4 结果以及 15 条返航探测，规划入口使用 `--cache-ram 0` 的默认设置。

## 替换判据

只有同时满足以下条件，报告才写“建议替换 r14”：候选 planning-v3 不低于 r14；planning-v4 的返航、未知地点或能力类别至少一个相对基线提升 10 个百分点；15 条返航探测全部产生 `return_home` 或被正确拒绝。报告同时列出总分、类别准确率/拒绝正确率、意图违规率、能力违规率和探测通过数。

## r21 结果（2026-10-25，数据修复后重跑：r21-run2）

运行链：data（按样本能力准入修复后 2560 条全部通过）→ train（约 2 h）→ export（Q5_K_M）→ eval（与 r14 同一 harness）→ report。期间修复的流水线问题：训练产物路径检查（05571e7b）、评测输出嵌套 junction（07741880）、WDDM 下 GPU 占用误判（49a73b59）、规划行把语义违规计入 JSON 有效率而触发 99% 门槛（2205925d，改为 0.95，传输错误仍由 `--max-transport-rate` 把关）。

| 指标 | r21 候选 | r14 |
|---|---:|---:|
| planning-v3 精确步骤序列率 | 0.525 | 0.662 |
| planning-v4 精确步骤序列率 | 0.400 | 0.400 |
| 返航探测 | 13/15 | 15/15 |

planning-v4 类别：ref_alias 0.50（r14 0.25）、return_intent 1.00（r14 0.50）提升；unknown_place 0.00（r14 1.00，拒绝正确率 1.00→0.00）严重回退；fixed_wing_hover / fixed_wing_target_land / missing_payload_drop 两者均为 0。

结论：**不替换 r14**（planning-v3 低于 r14，返航探测 13/15，且未知地点拒绝能力丢失）。下一轮数据需要：保持 unknown_place 拒绝样本的比例与权重；补固定翼悬停/降落的能力拒绝样本；在 v3 通用任务上不能退化（混入 r14 训练分布或降低学习率）。报告：`D:/Projects/mdwt/_scratch/cx-planner-round/r21-run2/planner-round-report.md`；评测：`artifacts/evaluation/r21-planner-round/`。

## r22 数据准备（2026-10-26）

r21 的未知地点能力回退有明确的数据解释：可见的 r14 planner 训练源是
`artifacts/datasets/plan-sft-v4/train.jsonl`，1500 条全是可执行 `mission_plan`，没有
`unknown_place` 或能力拒绝 gold；r21 另加的拒绝样本只有每族 40 条负例，unknown_place
仅占 40/2560 = 1.5625%，而 2000 条 v4 backbone 都是正常执行计划。拒绝信号被通用正例
稀释，且 r21 没有 replay r14 planner 分布，这与 unknown_place 从 1.00 降到 0.00 相符。

r22 数据生成器现在支持 `--replay-planner-train`。本轮生成结果如下；`r14_replay` 行逐行
保留原 prompt、task_text 和 assistant_target，新增行仍使用现有能力/意图校验。

| 训练集类别 | r14 | r21 | r22 |
|---|---:|---:|---:|
| r14 planner replay / v4 backbone | 1500 / 0 | 0 / 2000 | 1500 / 2000 |
| unknown_place 拒绝 gold | 0 | 40 (1.563%) | 80 (1.732%) |
| missing_payload_drop 拒绝 gold | 0 | 40 (1.563%) | 80 (1.732%) |
| fixed_wing_constraint 拒绝 gold | 0 | 40 (1.563%) | 80 (1.732%) |
| fixed_wing_target_land 拒绝 gold | 0 | 40 (1.563%) | 80 (1.732%) |
| 其它配对族（每族总行数） | 0 | 80 | 160 |
| train 总行数 | 1500 | 2560 | 4620 |

r22 的能力拒绝按 `src/multidetect/autonomy/capability_contract.py` 生成：无
`payload_drop` 时拒绝 `deliver`；固定翼带 `airframe:fixed_wing` 时拒绝 `hover`，并拒绝带
目标点的 `land`（缺少 `landing_on_target`）。负例的 assistant target 是现有格式的安全
拒绝计划，并保留 `rejected_candidate` 与 `expected_findings` 供离线准入检查。

holdout 仍读取全部 `configs/onboard/plan-eval-tasks*.json`，并用 NFKC、大小写、数字词和
标点归一化比较；r22 的 190 个 holdout 题与 train/val 零碰撞。生成产物：
`D:/Projects/mdwt/_scratch/cx-planner-round/r22/dataset/`，manifest 记录
`r14_replay=1500`、`v4_backbone=2000` 和各配对族计数。

只准备数据时运行：

```powershell
$env:PYTHONPATH='src;.'
$env:PYTHONUTF8='1'
python scripts/run_planner_round.py --stage data --force-stage data `
  --out D:/Projects/mdwt/_scratch/cx-planner-round/r22 `
  --rows 2000 --val-rows 100 --intent-count 80 `
  --replay-planner-train artifacts/datasets/plan-sft-v4/train.jsonl
```

r22 正式训练配方从 r14 adapter 热启动，保持 1 epoch、rank 16、alpha 32，学习率降为
`5e-5` 以保护 v3 通用任务；完整流水线命令（训练/导出/评测按需执行）为：

```powershell
python scripts/run_planner_round.py --stage all `
  --out D:/Projects/mdwt/_scratch/cx-planner-round/r22 `
  --rows 2000 --val-rows 100 --intent-count 80 `
  --replay-planner-train artifacts/datasets/plan-sft-v4/train.jsonl `
  --learning-rate 5e-5 --min-free-disk-gb 12 --min-memory-gb 8
```

本轮仅生成并核验数据，没有启动训练、GPU 推理、llama-server、SITL 或 Docker。
