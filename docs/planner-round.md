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

## r22 能力类 0 分核查

核查结论是 **(c) 为主因，兼有评测与产品 prompt 的 (b) 差异**。planning-v4 第 7--12 题分别是 `missing_payload_drop`（7--8）、`fixed_wing_hover`（9--10）和 `fixed_wing_target_land`（11--12）；题目元数据在 `configs/onboard/plan-eval-tasks-v4.json` 中声明了 `capabilities`，但原评测调用 `compile_mission_plan` 时没有传入该字段，产品运行时的 `scripts/onboard_policy.py` 则会传入能力并把能力写入 prompt。更关键的是，原 `score_task` 只把 `capability_violations` 作为诊断字段，能力负例仍要求模型自己返回 `None` 或单步 `return_home` 才算拒绝；它没有运行产品 validator，也没有把 validator 的 `reject`/`clarify` 作为正确拒绝。

逐题证据：r22 candidate 第 9、10 题均输出 `hover -> return_home`，`valid=true`、`capability_violations=1`，原 `correct_rejection=false`；第 12 题输出带 `target_ref` 的 `land`，同样触发能力违规；第 7、8 题均输出 `goto -> return_home`，没有 `deliver`，所以不存在可由 payload 能力门控拒绝的计划。该行为符合“忠实计划 + validator 门控”设计，不能把 hover 输出简单归类为模型错误计划；target-land 中文题还出现了 `goto -> return_home` 的非忠实输出，missing-payload 两题则是漏掉投放动作。

修复包括两部分：评测现在把每题能力传给 schema、planner prompt 和编译器；能力题的缓存输出再经过 `validate_plan_for_execution` 及统一 `capability_contract`，validator 拒绝或澄清计为正确拒绝，能力违规率只统计 validator 放行的违规计划。新增 `scripts/recompute_planning_v4_capabilities.py`，只读取已有 JSON 的 `raw` 输出离线重算，不启动推理服务。现有 artifacts 没有单独的 r14 `planning-v4.json`，报告中的 r14 结果与 r21/r22 baseline 使用同一 r14 基线缓存；因此下表的 r14 列标为 baseline 来源。

| 轮次/模型（来源） | fixed_wing_hover 拒绝正确率 / 能力违规率 | fixed_wing_target_land 拒绝正确率 / 能力违规率 | missing_payload_drop 拒绝正确率 / 能力违规率 |
|---|---:|---:|---:|
| r14（baseline 缓存） | 1.00 / 0.00 | 0.50 / 0.00 | 0.00 / 0.00 |
| r21 candidate | 1.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 |
| r21 baseline | 1.00 / 0.00 | 0.50 / 0.00 | 0.00 / 0.00 |
| r22 candidate | 1.00 / 0.00 | 0.50 / 0.00 | 0.00 / 0.00 |
| r22 baseline | 1.00 / 0.00 | 0.50 / 0.00 | 0.00 / 0.00 |

prompt 能力上下文的缺失需要后续训练/评测数据保持一致；本修复没有重训，也没有改动已有模型输出。能力类重算不会改变替换判断：**仍不替换 r14**，因为主判据 planning-v3 仍为 r22 `0.588` 对 r14 `0.662`，低 7.5 个百分点。

## r22 planning-v3 回退分析与 r23 方案（2026-10-28）

逐题对齐 `artifacts/evaluation/r22-planner-round/{candidate,baseline}/planning-v3.json` 与 r21 同名文件（80 题、中文 40/英文 40）后，r22 相对 r14 错 8 题；r21 相对 r14 错 14 题。r22 的 8 题为：第 22 题双 `survey -> survey -> land` 被截成 `survey -> return_home`；第 27 题 `goto -> hover -> return_home` 结构无效；第 29、69 题分别漏掉 `hover`；第 32 题把 `record -> hover -> land` 生成成错误的 `follow` 链；第 57 题漏掉 patrol 后的 `goto`；第 66 题漏掉第二个 `monitor`；第 71 题把“当前视野拍照”错成 `goto`。其中 6/8 是多步骤或终步参数敏感任务，4/8 为中文，4/8 为英文；错误集中在 `goto/hover/record/patrol/monitor/survey`，没有 count/area_search 基础题回退。

r22 数据的集中原因是配方稀释与分布偏移：训练集 4,540 行中有 1,500 条 r14 replay（33.0%）、2,000 条 v4 backbone（44.1%）以及 1,040 条 r20 能力/意图行（22.9%）。r20 能力行的终步主要是 `return_home`/`land`，`missing_payload_drop` 的 160 行虽包含 deliver 负例对，但不是面向 planning-v3 的自然任务覆盖；v4 与 replay 的 goal 中位长度分别为 57 与 56，而能力行多为长度 7 的拒绝目标。r22 train 的中英文比例约 50/50，故语言比例不是主因；主因是多步骤 v3 风格样本相对总量不足，且 `record`/`hover`/连续 monitor/survey 的参数与终步组合被能力拒绝模板稀释。

r23 在 `scripts/build_plan_sft.py` 增加显式 `--r23-recipe`（默认关闭，v4 默认输出字节不变）：保留 1,500 条 r14 replay 和原 v4/r20 行，附加 600 条 `r23_v3_coverage`，六个回归序列各 100 条：`goto -> hover -> return_home`、`record -> hover -> land`、`patrol -> goto -> return_home`、`monitor -> monitor -> return_home`、`survey -> survey -> land`、当前视野 `record -> return_home`。coverage 中英文为 307/293；另有 80 条自然语言多样化的 `r23_deliver_faithful`，其中 `deliver -> return_home` 60 条、`deliver -> land` 20 条，由 validator 按 payload 能力拒绝或放行。生成结果为 train 5,220、val 100；train family 计数和步骤序列表写入 `r23-data-stats.md`，`deliver` 步骤 240 条（其中新增忠实正例 80 条）。r23 专用输出还清理 replay/backbone 中的 `then then` 与 `最后然后`，默认 v4 路径不受影响。与 planning-v3/v4 评测题规范化碰撞均为 0。推荐 r14 adapter 热启动、1 epoch、lr `2.5e-5`；编排器 `run_planner_round.py` 在 `--r23-recipe` 下自动采用该 lr，也可显式覆盖。

仅生成数据：

```powershell
python scripts/run_planner_round.py --stage data --force-stage data `
  --out D:/Projects/mdwt/_scratch/cx-planner-round/r23 `
  --eval-root artifacts/evaluation/r23-planner-round `
  --rows 2000 --val-rows 100 --intent-count 80 `
  --replay-planner-train artifacts/datasets/plan-sft-v4/train.jsonl `
  --negative-target-mode rejected_candidate --r23-recipe `
  --r23-v3-count 600 --r23-deliver-count 80
```

完整训练/评测命令（本任务不执行）：

```powershell
python scripts/run_planner_round.py --stage all `
  --out D:/Projects/mdwt/_scratch/cx-planner-round/r23 `
  --eval-root artifacts/evaluation/r23-planner-round `
  --rows 2000 --val-rows 100 --intent-count 80 `
  --replay-planner-train artifacts/datasets/plan-sft-v4/train.jsonl `
  --negative-target-mode rejected_candidate --r23-recipe `
  --r23-v3-count 600 --r23-deliver-count 80 --learning-rate 2.5e-5
```

## r23 结果（2026-10-31，新开发机）

在新开发机（Linux，RTX 3080 20GB，GPU1）上跑完。数据与旧机器生成的一致（train 5,220 行、val 100 行，全部通过训练准入）；r14 热启动，lr 2.5e-5，1 epoch，训练约 36 分钟。

流水线的 eval 阶段在候选模型上中止：planning-v3 的 JSON 有效率 93.75%，低于 `--min-valid-rate 0.95`。无效的 10 题都是模型问题，不是环境问题：把 `poi:bridge` 写成不存在的 `zone:bridge`（5 题），或者把地图上没有的地点换成已知地点（`poi:shed`、`poi:tower` 等，被意图检查拦下）。为了拿到完整对比，放宽有效率门限（`--min-valid-rate 0`）补跑了候选和 r14 的 planning-v3/v4 与返航探测，结果在 `artifacts/evaluation/r23-planner-round-full/`。

r14 在新机器上的 planning-v3 为 0.6625，与旧机器的 0.662 一致，说明新环境的评测链路可信。

| 指标 | r14 | r22 | r23 |
|---|---:|---:|---:|
| planning-v3 精确序列 | 0.662 | 0.588 | 0.613 |
| planning-v3 JSON 有效率 | 0.938 | — | 0.875 |
| planning-v4 精确序列 | 0.55 | 0.500 | 0.60 |
| 返航探测（15 题） | 13/15 | 15/15 | 14/15 |

planning-v4 分类（精确或正确拒绝，r23 对 r14）：return_intent 2/2 对 1/2，ref_alias 2/4 对 1/4，unknown_place 2/2 对 2/2，fixed_wing_hover 2/2 对 2/2，fixed_wing_target_land 0/2 对 1/2，missing_payload_drop 和 query_ungrounded 两者都是 0/2。

说明：r14 的 planning-v4 是 0.55 而不是表里旧值 0.40，是因为能力类核查之后评测改为用产品 validator 判拒；返航探测 r14 13/15（旧记录 15/15），失败的 p01、p11 被现行的意图检查拦下，旧记录早于这些检查。

结论：**不替换 r14**。r23 修复了返航意图和别名，v4 和返航探测都比 r14 好，但 planning-v3 仍低 5 pp，而且 v3 有效率掉到 87.5%：模型会编造区域引用、拿已知地点顶替未知地点。下一轮应针对“只能使用给定引用”补数据（同名 poi/zone 的对照、未知地点必须拒绝），而不是继续加返航类样本。

## r24 数据准备

r24 在 r23 配方上增加“引用落地”数据族，目标是让模型只复制提示中给出的引用，并区分引用类型。`--r24-recipe` 会自动包含 r23 的 v3 覆盖和 faithful deliver 行；默认新增 240 条训练行：`r24_reference_type` 80 条、`r24_unknown_place` 80 条、当前位置/起飞点语义 80 条（引用对照的模板去重后不足 100 条，所以默认取 80）。这个规模约占本次 5,460 条训练集的 4.4%，足以重复暴露引用边界，又不会把 r23 的多步骤 v3 覆盖稀释掉。三个数量都可通过 `--r24-reference-count`、`--r24-unknown-count`、`--r24-context-count` 调整。

`r24_reference_type` 覆盖四种对照。已知列表只有 `poi:X` 时，计划先 `goto` 到该点，再在当前位置执行不带 `region` 的 `hover`；需要区域的全域测绘使用契约已有的 `zone:all`。当列表同时给出 `poi:X` 与 `zone:X` 时，`goto` 使用 `poi:X`，`area_search`、`monitor` 等区域技能使用 `zone:X`。数据中不把 `poi:` 填进 `region`，也不发明新的引用语义。该族 80 条，中英文混合。

`r24_unknown_place` 的任务会点名列表之外的青石码头、旧气象站等地点，并同时提供一个已知地点作为干扰。负例 target 保留未解析的 `poi:` candidate，由 `ungrounded_place` validator finding 拒绝；不会把未知地点替换成列表里的其它地点。80 条中英文各 40 条，生成清单记录 `known_ref_substitution_rows: 0`。

当前位置族覆盖“脚下”“当前位置”“起飞点”“home”等说法，分别生成不带 `target_ref` 的 `land` 或 `return_home`。共 80 条，着陆和返航各 40 条，中文/英文在每个子族内各 20 条。

本次实际生成（2026-10-31）使用：

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
export MULTIDETECT_PLANNER_SCRATCH=$PWD/.tmp/planner-scratch
python scripts/run_planner_round.py --stage data --force-stage data \
  --out .tmp/r24-data --eval-root .tmp/r24-eval \
  --rows 2000 --val-rows 100 --intent-count 80 \
  --replay-planner-train artifacts/datasets/plan-sft-v4/train.jsonl \
  --negative-target-mode rejected_candidate --r24-recipe
```

dry-run 只打印了上述 data 命令，没有训练或评测。实际结果为 train 5,500、val 100；r24 四个 family 分别为 120、80、40、40，r23 family 保持 600 条 coverage 和 80 条 faithful deliver。训练准入 `admit_train_rows` 接受全部 5,500 条，意图与结构检查全部通过；负例共检查 480 条，已知引用替代为 0。与 planning-v3/v4 评测题按 `normalize_task` 去重后碰撞为 0。统计文件位于 `.tmp/r24-data/dataset/r23-data-stats.md`，未写入 `artifacts/`。本轮没有启动训练、GPU 推理、llama-server、SITL 或 Docker。
