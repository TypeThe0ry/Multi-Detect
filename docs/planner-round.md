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

重新审查 planning-v4 的逐题输出后，修正了上一版的“拒绝样本被稀释”解释。r21 评测文件
`artifacts/evaluation/r21-planner-round/candidate/planning-v4.json` 与
`artifacts/evaluation/r21-planner-round/baseline/planning-v4.json` 同时包含 r21 candidate
和 r14 baseline 的 unknown_place 两题。r21 candidate 两题都输出了结构合法的单步
`return_home`（goal 为 `Reject: ...`），`valid=true`、`error=null`、`correct_rejection=false`；
不是格式错误，也没有生成陌生地点 ref。r14 baseline 则输出了 `goto poi:raven pier`，另一题
把 Raven Pier 错映射到 `poi:gate`；两者均被 validator 拒绝，`correct_rejection=true`。
因此 1.00→0.00 的直接差别是 r14 给出了 validator 会拒绝的计划，r21 学会输出无操作但
validator 接受的安全终止计划。

训练数据审计进一步定位了标签不一致：r21 的 40 条 unknown_place negative 都有
`rejected_candidate`，并都触发 `ungrounded_place`，但 `assistant_target` 被替换成合法
`return_home`。故模型实际学到的不是一个会被 validator 判拒的 candidate。r21 的 v4
backbone 2000 条和 unknown_place family 40 条 positive 中，按每行 Known places/refs 和
`check_plan_intent` 检查，没有发现 gold 把任务中的未知地点替换为可执行 ref；r14 的
1500 条 planner gold 同样为 0。special unknown negative 的 `rejected_candidate` 有 40/40
会把未知地点替换成行内已知的 gate/pad 并触发 `ungrounded_place`，但这些候选未进入 gold。
因此“backbone 教模型编造地点”不受这批数据支持；更直接的问题是 negative gold 与评测
所需的 validator 拒绝行为不一致。r21 的 unknown 正负对确实只差是否声明地点不存在/禁止
替代，属于最小对照；r22 保留这个对照，同时把负例 gold 改成可由 validator 判拒的候选计划。
数据原则是：**先忠实表达用户请求，再依靠 validator gating 拒绝不安全或不可解析的计划**。
未知地点必须使用原始地点文字归一化得到的不可解析 `poi:` ref；禁止用行内已知地点替代。
能力负例保留用户请求的动作（deliver/hover/land）；query_grounding 没有负例，因为缺少用户
提供的 query 时无法构造忠实、结构合法且可由 validator 拒绝的 inspect target。这样安全性不依赖
substitution 检测永远不漏报。

r22 数据生成器现在支持 `--replay-planner-train`。按本轮复核修正后，最终生成结果如下；`r14_replay` 行逐行
保留原 prompt、task_text 和 assistant_target，新增行仍使用现有能力/意图校验。

| 训练集类别 | r14 | r21 | r22 |
|---|---:|---:|---:|
| r14 planner replay / v4 backbone | 1500 / 0 | 0 / 2000 | 1500 / 2000 |
| unknown_place（正/负） | 0 | 80（负例 gold 为 safe `return_home`） | 80 / 80（忠实 unknown-ref 负例；负例占 train 1.762%） |
| missing_payload_drop（正/负） | 0 | 80（负例 gold 为 safe `return_home`） | 80 / 80（忠实 deliver 负例；负例占 1.762%） |
| fixed_wing_constraint（正/负） | 0 | 80（负例 gold 为 safe `return_home`） | 80 / 80（忠实 hover 负例；负例占 1.762%） |
| fixed_wing_target_land（正/负） | 0 | 80（负例 gold 为 safe `return_home`） | 80 / 80（忠实 land 负例；负例占 1.762%） |
| ref_alias（正/负） | 0 | 80 | 80 / 80（未知 ref 负例；负例占 1.762%） |
| query_grounding | 0 | 80（正/负配对含编造 query 负例） | 80 正例 / 0 负例 |
| return_intent（正/负） | 0 | 80 | 80 / 80 |
| train 总行数 | 1500 | 2560 | 4540 |
| validator-negative gold 总数 | 0 | 0（240 条 negative 均为 safe `return_home`） | 400 |

r22 的负例 target 使用经审计的违规 candidate，由 `src/multidetect/autonomy/plan_validation.py`
对应的 intent/capability 契约拒绝；生成/训练准入均检查 expected finding，并只对负例 target
做结构校验。**忠实计划 + validator gating** 是数据准则：target 保留用户请求的地点、动作和对象，validator 再因未知 ref 或机体能力不足而拒绝；训练 target 不把请求改写成安全动作，也不悄悄替换地点、对象或动作。
能力负例按 `src/multidetect/autonomy/capability_contract.py` 生成：无
`payload_drop` 时拒绝 `deliver`；固定翼带 `airframe:fixed_wing` 时拒绝 `hover`，并拒绝带
目标点的 `land`（缺少 `landing_on_target`）。每个负例与正例尽量复用同一请求句式，仅改变
Known places 或能力上下文。`query_grounding` 只保留正例：`inspect` 需要 query，而“检查大门并返航”
没有给出对象类别；留空会使计划结构不合法，填入任何类别都会教模型编造 query，因此该族没有忠实且可表达的拒绝 target。准入会拦截特制 family target 中无法从任务文本/refs 追溯的地点、query 和 payload，并拦截未知地点被映射到已知 ref。负例同时保留 `rejected_candidate` 与
`expected_findings` 供离线准入检查。

holdout 仍读取全部 `configs/onboard/plan-eval-tasks*.json`，并用 NFKC、大小写、数字词和
标点归一化比较；r22 的 190 个 holdout 题与 train/val 零碰撞。总计 4,640 行（train 4,540、val 100），
400 个拒绝 target；admission 全量通过。生成产物：
`D:/Projects/mdwt/_scratch/cx-planner-round/r22/dataset/`，manifest 记录
`r14_replay=1500`、`v4_backbone=2000` 和各配对族计数。

只准备数据时运行：

```powershell
$env:PYTHONPATH='src;.'
$env:PYTHONUTF8='1'
python scripts/run_planner_round.py --stage data --force-stage data `
  --out D:/Projects/mdwt/_scratch/cx-planner-round/r22 `
  --eval-root artifacts/evaluation/r22-planner-round `
  --rows 2000 --val-rows 100 --intent-count 80 `
  --replay-planner-train artifacts/datasets/plan-sft-v4/train.jsonl `
  --negative-target-mode rejected_candidate
```

r22 正式训练配方从 r14 adapter 热启动，保持 1 epoch、rank 16、alpha 32，学习率降为
`5e-5` 以保护 v3 通用任务；完整流水线命令（训练/导出/评测按需执行）为：

```powershell
python scripts/run_planner_round.py --stage all `
  --out D:/Projects/mdwt/_scratch/cx-planner-round/r22 `
  --eval-root artifacts/evaluation/r22-planner-round `
  --rows 2000 --val-rows 100 --intent-count 80 `
  --replay-planner-train artifacts/datasets/plan-sft-v4/train.jsonl `
  --negative-target-mode rejected_candidate `
  --learning-rate 5e-5 --min-free-disk-gb 12 --min-memory-gb 8
```

为保留旧 r21 命令的可复现行为，runner 默认仍是输出 `r21`、intent count 40、无 replay、
safe-rejection negative target、学习率 `1e-4`，评测目录仍为
`artifacts/evaluation/r21-planner-round`。r22 的 replay、validator-negative 标签和较低学习率
均通过上述显式参数启用。

本轮仅生成并核验数据，没有启动训练、GPU 推理、llama-server、SITL 或 Docker。

## r22 结果（2026-10-28）

r22：r14 热启动，lr 5e-5；数据 = 完整 r14 planner replay + v4 backbone + “忠实计划 + validator 门控”负例（unknown_place/能力拒绝各 80）。报告 `D:/Projects/mdwt/_scratch/cx-planner-round/r22/planner-round-report.md`；评测 `artifacts/evaluation/r22-planner-round/`。

| 指标 | r14 | r21 | r22 |
|---|---:|---:|---:|
| planning-v3 | 0.662 | 0.525 | 0.588 |
| planning-v4 | 0.400 | 0.400 | 0.500 |
| unknown_place 拒绝正确率 | 1.00 | 0.00 | 1.00 |
| return_intent | 0.50 | 1.00 | 1.00 |
| ref_alias | 0.25 | 0.50 | 0.50 |
| 返航探测 | 15/15 | 13/15 | 15/15 |

结论：**不替换 r14**（planning-v3 仍低 7.5 pp）。r22 修复了 r21 的未知地点拒绝丢失，v4 提升；但三类能力拒绝（fixed_wing_hover、fixed_wing_target_land、missing_payload_drop）在 r14 与 r22 上都是 0，且 fixed_wing_hover 能力违规率 1.0——说明模型按设计输出了忠实计划，但评测链路没有让 validator 以机型能力拒绝它，需要核查评测是否传入机型能力上下文。
