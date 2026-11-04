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

## r24 结果（2026-10-31，新开发机）

r24 = r23 配方 + “引用落地”数据族（同名 poi/zone 对照 80、未知地点忠实负例 80、当前位置/起飞点 80），r14 热启动，lr 2.5e-5，1 epoch，GPU1 约 35 分钟。训练集 5,460 行、val 100 行，全部通过准入。评测用本次评测链路（含新的固定翼 goto→land 与否定句 query 校验规则），r14 与 r24 同一套代码重跑。

| 指标 | r14 | r23 | r24 |
|---|---:|---:|---:|
| planning-v3 精确序列 | 0.6625 | 0.6125 | **0.7375** |
| planning-v3 JSON 有效率 | 0.9375 | 0.875 | **0.95** |
| planning-v4 精确序列 | 0.60 | 0.60 | **0.75** |
| 返航探测（15 题） | 13/15 | 14/15 | **15/15** |

planning-v4 分类（精确或正确拒绝，r24 对 r14）：return_intent 2/2 对 1/2，ref_alias 3/4 对 1/4，其余类别持平（unknown_place、fixed_wing_hover、fixed_wing_target_land、return_negated_land、query_grounding 两者都满分；missing_payload_drop、query_ungrounded 两者都是 0/2）。

替换判定（`artifacts/scratch/cx-planner-round/r24/replacement-decision.json`）：v3 不低于 r14、v3 有效率不低于 r14、v4 返航类提升 ≥10 个百分点、15 条返航探测全过——**全部通过，r24 替换 r14 作为机载规划模型**。

模型：`artifacts/models/slow-system-lora-v2-r24-planner-merged-gguf/slow-system-v2r24planner-merged-Q5_K_M.gguf`（sha256 `147f90d8…`），adapter 在 `artifacts/models/slow-system-lora-v2-r24-planner/`。推荐机载组合配置：`configs/onboard/sim-loop-v3-r18id-r24plan-defaults.json`（r18 身份 + r24 规划 + YOLOE v3 1280）。各回归套件配置暂时仍指向 r14，等 Linux 上 SITL 闭环跑通、用 r24 跑一轮任务套件后再切换。

剩余问题：missing_payload_drop（缺载荷仍规划投递以外的动作，没有拒绝）和 query_ungrounded 仍为 0/2，下一轮规划数据应补这两类。

## r25 数据准备

r25 在 r24 配方上增加两类正负对照，--r25-recipe 自动包含 r23 和 r24 的全部内容。
--r25-missing-payload-count 与 --r25-negated-query-count 都按“对数”计数，默认各 40
对：缺载荷族新增 40 条无 `payload_drop` 能力、保留 `deliver` 的 validator 负例，以及 40 条
能力声明齐全的有效对照；否定 query 族新增 40 条许可类别正例和 40 条把否定类别当 query
的 validator 负例。中英文句式交替并组合多个目标地点、类别、动作表达和结尾。默认合计新增
160 行，约占本次 5,620 行训练集的 2.8%；保留对照密度，同时避免再次稀释 r23 多步骤覆盖。
缺载荷模板的组合容量超过 1,000 对，否定 query 模板超过 4,000 对；构造器对每次尝试都前进，
并在 2,400 次仍无法凑齐时明确报模板池耗尽，不会无限循环。

缺载荷负例 prompt 声明不具备 `payload_drop`，gold/rejected_candidate 仍包含用户要求的
`deliver`，`expected_findings` 为 `missing_capability`；配对正例使用相同能力上下文之外的
payload-capable 声明，仍然规划 `deliver`。否定 query 正例的任务文本包含许可类别和被排除
类别，target query 选许可类别；负例保留相同否定任务的被排除类别 query，由
`plan_intent.query_grounding` 产生 `ungrounded_query`。例如“检查大门的入口标志，不要凭空检查
救护车”可用 `entrance sign` 作正例 query，`救护车` 作负例 query。结构、意图、能力和
rejected_candidate finding 均由训练准入检查。

本次实际生成（2026-10-31）使用：

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
export MULTIDETECT_PLANNER_SCRATCH=$PWD/.tmp/planner-scratch
python scripts/run_planner_round.py --stage data --force-stage data --out .tmp/r25-data --eval-root .tmp/r25-eval --rows 2000 --val-rows 100 --intent-count 80 --replay-planner-train artifacts/datasets/plan-sft-v4/train.jsonl --negative-target-mode rejected_candidate --r25-recipe
```

`--init-adapter` 默认仍指向 r14 adapter，`--baseline-gguf` 默认仍指向 r14 GGUF；
r25 训练/评测时分别传入 r24 adapter 和 GGUF，并用 `--baseline-label r24` 让替换判定按
r24 命名：

```bash
--init-adapter artifacts/models/slow-system-lora-v2-r24-planner/adapter
--baseline-gguf artifacts/models/slow-system-lora-v2-r24-planner-merged-gguf/slow-system-v2r24planner-merged-Q5_K_M.gguf
--baseline-label r24
```

runner dry-run 会透传这些路径；本次只运行 data 阶段，没有训练或评测。

实际结果：train 5,620 行、val 100 行，其中 `r25_missing_payload` 40、
`r25_payload_capable` 40、`r25_negated_query` 80；其余 r23/r24 配方计数保持原值。
`admit_train_rows` 接受全部 5,620 条训练行，含能力、意图和 rejected_candidate 检查；
负例共 560 条，全部 finding 匹配，已知引用替代为 0。生成的 r25 负例和正例均使用当前
`plan_intent.query_grounding` 可判定的类别映射。校验器对某些未列入词表的跨语言复合类别
（例如中文“消防车”对应英文 query `fire truck`）可能返回 `undecided` 而不是
`ungrounded_query`；本轮按现有契约避开这类映射，未改动校验器。统计位于
`.tmp/r25-data/dataset/r23-data-stats.md`，manifest 记录各 family 数量。train 内容摘要为
`6d51b1d7dc1593a372adabc9be61acf3141d7cd40e75fa4e186a2dd1febc7e91`。

## r25 结果（2026-11-01）

r25 = r24 配方 + 缺载荷投递（负例 40 + 有能力正例 40）+ 否定句查询（正负例各 40），r24 热启动，以 r24 为基线。训练 5,620 行。

| 指标 | r24（基线） | r25 |
|---|---:|---:|
| planning-v3 精确序列 | 0.7375 | 0.6875 |
| planning-v3 有效率 | 0.95 | 0.95 |
| planning-v4 精确序列 | 0.75 | 0.75 |
| 返航探测 | 15/15 | 15/15 |

结论：**不替换 r24**（v3 下降 5 个点，v4 目标类别没有提升）。

两个目标类别的情况不一样：
- **missing_payload_drop 仍是 0/2，是模型问题**：r25 在“本机没有载荷投送能力”的题上仍输出 `goto→return_home` 或 `follow→land`，没有输出 `deliver`。r25 新增的训练行没能迁移到评测题，下一步要先对比训练行与评测题的提示词格式（尤其能力上下文怎么写进提示），再决定是否加数据。
- **query_ungrounded 的 0/2 是评测打分问题，不是模型问题**：r24、r25 都输出了 `inspect(query=救护车/fire truck)`，产品的意图检查（`query_grounding`，已能识别否定句）会判为 `ungrounded` 并拒绝；用 `scripts/recompute_planning_v4_capabilities.py` 离线重算，这两题是 2/2 正确拒绝。但评测链路实时打分仍记 0/2，说明实时打分没有走到同一条意图检查路径，需要修评测。

## r26 结果（2026-11-01）

r26 = r24 配方 + 结构化拒绝/澄清（每类 reason_code 60 条拒绝 + 60 条对照正例，r24 的未知地点行改为结构化澄清），r24 热启动，以 r24 为基线；候选和基线都在拒绝契约开启下评测（`docs/plan-refusal-contract.md`）。

| 指标 | r24（契约开启） | r26 |
|---|---:|---:|
| planning-v3 精确序列 | 0.6875 | 0.4875 |
| planning-v3 有效率 | 0.9875 | 1.00 |
| planning-v4 精确序列 | 0.70 | 0.80 |
| 返航探测 | 15/15 | 14/15 |

planning-v4：缺载荷投递 0/2→**2/2**、未知地点 0/2→2/2（结构化拒绝正是为这两类设计的，确实奏效）；但固定翼悬停 2/2→1/2、否定句查询 1/2→0/2。

结论：**不替换 r24**。主要问题是**过度拒绝**：planning-v3 的 80 题里 r26 对 24 题输出了拒绝，其中大量是正常可执行的任务（“就在脚下这个位置着陆吧”被判 missing_capability、“直接回咱们放飞的位置”被判 ambiguous_task、多步复合任务被判 unsafe_request），拒绝原因与任务也常常对不上。说明 r26 的拒绝数据比例偏高、触发特征过于依赖关键词（如“受限区域”“未登记”“两个任务”），模型学成了“看到复杂句式就拒绝”。

下一轮（r27）方向：拒绝样本总量降到训练集的 2–3%（r26 约 5%），只保留 missing_capability 和 unknown_place 两类有明确依据的拒绝；unsafe_request/ambiguous_task 暂不训练（由主机侧规则处理）；大幅增加“看起来复杂但可执行”的正例（多步、口语化、否定词在其它从句里的任务）作为对照；并在数据审计中加一项：用 r24 在新数据正例上的输出检查有没有被标成拒绝的可执行任务。

## r27 数据准备

r27 在 r24 配方上开启结构化拒绝契约，但只训练两类有直接证据的拒绝：missing_capability（提示明确写出无 payload_drop，任务要求投递）和 unknown_place（任务点名地点不在 Known places 列表）。每类默认 40 条拒绝，每条配一条只改变能力或地点事实的可执行对照正例。unsafe_request、ambiguous_task、ungrounded_query 不再训练拒绝，理由分别是安全边界、缺字段澄清和查询依据由主机规则判断更可靠。

复杂正例默认 400 条，覆盖多步任务、口语化表达、作用域否定（例如“人别跟出去”“别满场找”）、“起飞点/脚下/当前位置”、拼图与测绘重叠、计数和持续监视；目标计划均经过 validate_mission_plan、check_plan_intent 和训练准入检查。生成器在拒绝占比超过 2% 时自动补普通可执行正例，确保硬上限。

本轮实际生成命令（只运行 data 阶段，未训练）：

~~~bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
export MULTIDETECT_PLANNER_SCRATCH=$PWD/.tmp/planner-scratch-final2
python scripts/run_planner_round.py --stage data --force-stage data \
  --out .tmp/r27-data --eval-root .tmp/r27-eval \
  --rows 2000 --val-rows 100 --intent-count 80 \
  --replay-planner-train artifacts/datasets/plan-sft-v4/train.jsonl \
  --negative-target-mode rejected_candidate --r27-recipe
~~~

结果：train **5,940** 行、val **100** 行；训练准入 admit_train_rows 接受 **5,940/5,940** 行。r27 新增拒绝 80 条（missing_capability 40、unknown_place 40），每类另有 40 条正例对照；拒绝占训练集 **1.347%**。复杂正例 400 条，族计数为 r27_multi_step 80、r27_colloquial 80、r27_negation 80、r27_mapping 40、r27_monitoring 40、r27_counting 40、r27_local_position 40。与 planning-v3/v4 holdout 归一化文本碰撞 **0**。统计和 manifest 位于 .tmp/r27-data/dataset/；训练集准入摘要 SHA-256 为 f1933b693a5856005e75e147756c4ee65772efbb358cf743dae3d951bd11a673。

24 条误拒绝、r26 模板相似度和触发模式详见 docs/r27-refusal-analysis.md。

## r27 结果（2026-11-01）

r27 = r24 配方 + 窄拒绝（只训练 missing_capability、unknown_place 各 40 条，占 1.3%）+ 400 条“看起来复杂但可执行”的正例，r24 热启动，候选与基线都在拒绝契约开启下评测。

| 指标 | r24（契约开启） | r24（现役，契约关闭） | r27 |
|---|---:|---:|---:|
| planning-v3 精确序列 | 0.6875 | 0.7375 | 0.7375 |
| planning-v3 有效率 | 0.9875 | 0.95 | 1.00 |
| planning-v4 精确序列 | 0.70 | 0.75 | 0.75 |
| 返航探测 | 15/15 | 15/15 | 14/15 |

过度拒绝已解决：r27 在 planning-v3 的 80 题里只拒绝了 8 题，逐条核对全部是该拒绝的（地图上没有的地点 4 题、没有投放能力却要求投放 4 题），r26 那种把正常任务拒掉的情况没有再出现。planning-v4：缺载荷投递 2/2、未知地点 2/2（r24 都是 0/2）；但返航意图 1/2、固定翼悬停 1/2、固定翼定点降落 1/2 各掉一题。

结论：**按门槛不替换 r24**——返航探测 14/15（“场地里的电瓶车给我报个总数，凑够十二辆就停手。”规划成 `count→land`，应为返航）。r27 在契约开启下的 v3/v4 已经追平现役 r24（契约关闭），并且新增了正确拒绝能力，是目前最接近可替换的候选。下一步：(1) 用更大的 planning-v5 评测集（正在建，v4 每类只有 2 题，单题波动 50 个百分点）重新比较 r24 与 r27；(2) r28 在 r27 基础上补“计数达到上限后停止”类任务的返航结尾，以及固定翼悬停/定点降落的能力对照。

## planning-v5 评测集

`configs/onboard/plan-eval-tasks-v5.json` 是 planning-v5 人工留出集，共 126 题：
return_intent、return_negated_land、unknown_place、missing_payload_drop、
fixed_wing_hover、fixed_wing_target_land、ref_alias、query_grounding、
query_ungrounded 九类各 14 题，中文和英文各 63 题。题目加入多步口语任务、
作用域否定、当前位置/起飞点和复杂但可执行的组合，期望步骤按当前
`plan_validation`、`plan_intent` 与 `capability_contract` 契约自检。

`build_plan_sft.py` 默认读取全部 `configs/onboard/plan-eval-tasks*.json`，因此 v2/v3/v4/v5
都会自动把 v5 作为 holdout；按 `normalize_task` 去重。用标准 v4 规模和 r24 配方做的
加入前后 SHA-256 核对未发生变化，旧训练输出保持字节一致。

模型 round runner 已支持 `planning-v5` 阶段。直接运行
`scripts/run_model_round_eval.py --stages planning-v5` 可单独评测；
`scripts/run_planner_round.py` 只有在传入 `--include-planning-v5` 时才会把 v5 加入候选
和基线评测，默认仍只跑 v3/v4，旧轮次的比较口径不变。

### planning-v5 首次对比：现役 r24（契约关闭）vs r27（契约开启）

结果在 `artifacts/evaluation/planning-v5-r24-vs-r27/`（126 题，精确或正确拒绝）：

| 类别 | r24 | r27 |
|---|---:|---:|
| 合计 | 66/126 | **81/126** |
| missing_payload_drop | 3/14 | **14/14** |
| unknown_place | 2/14 | **12/14** |
| query_ungrounded | 2/14 | 3/14 |
| fixed_wing_hover | 14/14 | 13/14 |
| fixed_wing_target_land | 11/14 | 8/14 |
| return_intent | 10/14 | 7/14 |
| return_negated_land | 7/14 | 7/14 |
| query_grounding | 8/14 | 8/14 |
| ref_alias | 9/14 | 9/14 |

r27 总分高 12 个百分点，提升全部来自结构化拒绝的两类；但返航意图（-3）和固定翼定点降落（-3）退步，与 r27 在返航探测、v4 上掉的题一致。r28 正好针对这两类补数据。v5 证实了 v4 的判断方向，且单类 14 题让差距可信得多。之后的替换判定把 v5 纳入（`--include-planning-v5`）。

## r28 数据准备

r28 在 r27 配方上增加三组小缺口：达到数量/搜索/巡逻条件后停止的任务（默认 80 条，中文英文各半，约四分之一明确要求降落，其余默认返航）、固定翼能力对照（`fixed_wing_hover` 与 `fixed_wing_target_land` 各 30 条，正例和固定翼缺能力拒绝成对出现），以及 30 条返航意图表达。`r28` 自动包含 r27 的两类结构化拒绝和 400 条复杂正例；r27 未启用时的输出路径保持不变。

生成器对每条新增正例运行 `validate_mission_plan`、`check_plan_intent` 和能力契约校验；固定翼拒绝使用 `missing_capability` 决策对象，候选计划保存在审计字段中。holdout 读取继续使用 `configs/onboard/plan-eval-tasks*.json`，若 v5 文件合入会自动纳入排除集合；未修改任何评测配置。

本轮实际生成命令（只运行 data 阶段，未训练）：

~~~bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
export MULTIDETECT_PLANNER_SCRATCH=$PWD/.tmp/planner-scratch-r28
python scripts/run_planner_round.py --stage data --force-stage data \
  --out .tmp/r28-data --eval-root .tmp/r28-eval \
  --rows 2000 --val-rows 100 --intent-count 80 \
  --replay-planner-train artifacts/datasets/plan-sft-v4/train.jsonl \
  --negative-target-mode rejected_candidate --r28-recipe
~~~

结果：train **6,110** 行、val **100** 行；训练准入 `admit_train_rows` 接受 **6,110/6,110** 行。r28 新增 **170** 行：`r28_stop_threshold` 80、`r28_return_intent` 30、`r28_fixed_wing_hover` 30、`r28_fixed_wing_target_land` 30；新增固定翼结构化拒绝 30 条。叠加 r27 后结构化拒绝共 **110** 条，占训练集 **1.800%**（不超过 2%）。与当前所有 `plan-eval-tasks*.json` holdout 的归一化文本碰撞 **0**。数据统计和 manifest 位于 `.tmp/r28-data/dataset/`，准入摘要 SHA-256 为 `ad7842ef87d24accbd5484465d8b439351348409dcb7e3a2851e08c698970a75`。

随机抽取的 10 条新增族原文：

1. `Patrol the north route while watching for motorcycles; stop after completing 10 loops.`
2. `在院子搜索行人并报数，凑够二十辆就停手。`
3. `在场地上空保持悬停六十秒观察。`
4. `把飞机降落到仓库这个目标点。`
5. `在农田搜索卡车并报数，累计到二十辆就停止计数。`
6. `Patrol the fence route while watching for cars; stop after completing 20 loops.`
7. `Proceed to the tower and touch down on the marked point.`
8. `到院子上空定点悬停一分钟。`
9. `Tally the electric scooters in the yard; quit when the count gets to 15.`
10. `Search the field for vehicles and report the tally; count up to 12 and stop.`

## r28 结果（2026-11-01）

r28 = r27 + 计数/巡逻达到条件后停止（80）+ 返航意图（30）+ 固定翼悬停/定点降落能力对照（各 30，含结构化拒绝），r24 热启动，候选与基线（r24）都在契约开启下评测，首次纳入 planning-v5。

| 指标 | r24（契约开启） | r28 |
|---|---:|---:|
| planning-v3 | 0.6875 | 0.55 |
| planning-v4 | 14/20 | 13/20 |
| planning-v5 | 81/126 | 70/126 |
| 返航探测 | 15/15 | 7/15 |

结论：**不替换 r24，r28 明显退步**。返航探测有 7 题输出了拒绝（“到大门看一下，然后返航。”“去大门，然后回到起飞点。”这类最简单的任务），说明固定翼能力对照里的结构化拒绝样本让模型把“到某地 + 降落/返航”泛化成了拒绝；“凑够十二辆就停手”仍是 `count→land`。

另一个重要发现：**现役 r24 只要打开拒绝契约，planning-v5 就是 81/126**（契约关闭时 66/126），与 r27 持平。也就是说 r27 在 v5 上的提升主要来自提示和输出格式本身，而不是训练；而 r24 打开契约后 v3 会从 0.7375 降到 0.6875。规划拒绝这条线的下一步应该先弄清楚这个取舍：契约提示怎样写才能不伤 v3（例如只在任务包里的能力/地点列表能直接判定时才给拒绝选项），而不是继续堆拒绝训练数据。

## r29 数据准备

r29 基于 r24 的 replay、v3 覆盖和引用/当前位置正例配方，关闭结构化拒绝训练目标；不继承 r25、r26、r27 或 r28 的缺载荷、未知地点、固定翼和拒绝/澄清族。`--r29-recipe` 自动启用 r23、r24 的正例配方，并把 r24 未知地点数量置为 0；同时把 `configs/onboard/plan-eval-tasks*.json` 和 `configs/onboard/plan-probe-tasks-return-home.json` 纳入 holdout。runner 的 r29 分支不自动加入 `--r20-plan-intent`，评测建议显式使用 `--plan-pregate --include-planning-v5`。

新增五族，每族默认 72 行（中文 36、英文 36），每种语言各 18 条目标正例与 18 条同句式对照：

- `query_ungrounded`：否定范围内的类别不进入 query；对照明确要求同时查询该类别。所有 target 都是正常可执行计划。
- `return_negated_land`：`不要返航/不回起飞点` 仍保留当地 `land`；对照改为 `不要降落` 并以 `return_home` 结束。
- `query_grounding`：明确给出的对象、地点、搜索和记录动作全部保留；对照使用同一地点的别名/标准名。
- `ref_alias`：别名与标准引用都落到同一 `poi:`，保留 `goto -> inspect -> return_home`；对照使用 canonical 名称。
- `return_intent`：明确返航的 count 任务以 `return_home` 结束；对照明确就地降落，不产生返航。

每条新增行在生成时运行 `validate_mission_plan` 和 `check_plan_intent`，训练准入再运行完整 admission；r29 结构化拒绝行数为 0。完整生成命令（只生成数据，不训练）为：

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
export MULTIDETECT_PLANNER_SCRATCH=$PWD/.tmp/planner-scratch
python scripts/run_planner_round.py --stage data --force-stage data \
  --out .tmp/r29-data --eval-root .tmp/r29-eval \
  --rows 2000 --val-rows 100 --intent-count 80 \
  --replay-planner-train artifacts/datasets/plan-sft-v4/train.jsonl \
  --negative-target-mode rejected_candidate --r29-recipe \
  --r29-query-ungrounded-count 72 \
  --r29-return-negated-land-count 72 \
  --r29-query-grounding-count 72 \
  --r29-ref-alias-count 72 \
  --r29-return-intent-count 72
```

实际结果：train **4,700** 行、val **100** 行；r29 新增 **360** 行，五族各 72，中文/英文 **180/180**。其余 train 行为 replay 3,500、r23 v3 覆盖 600、r23 faithful deliver 80、r24 reference 80、r24 context land/return 各 40。`admit_train_rows` 接受 **4,700/4,700** 行；r29 结构化拒绝 **0**；与全部 planning-v3/v4/v5 评测题及 15 条返航探测按 `normalize_task` 碰撞 **0**。产物位于 `.tmp/r29-data/dataset/`，未写入 `artifacts/`，没有启动训练、GPU 推理、llama-server、SITL 或 Docker。

随机抽取的 12 条新增原文：

1. `Check the lot for bicycles and fire trucks; then head back to the launch point.`
2. `在塔区查看红色轿车并记下位置，飞回起飞点。`
3. `Go to the tower and inspect the sign from 5 views; do not land, then head home.`
4. `在停车场巡查红色轿车，并查找救护车，飞回基地。`
5. `Check the tower for bicycle, note its position, and then fly home.`
6. `清点农田的自行车，数到三个就停，返场。`
7. `Go to the warehouse and inspect the sign from 3 views; do not return home, land there when done.`
8. `清点场地的自行车，数到十个就停，然后在这里降落。`
9. `到塔区附近检查标牌，从六个角度拍摄后返航。`
10. `在仓库查看自行车并记下位置，返回出发地。`
11. `Go to the warehouse and inspect the sign from 7 views; do not return home, land there when done.`
12. `Go to the warehouse and inspect the sign from 6 views; do not return home, land there when done.`

## r29 结果（2026-11-01）

r29 = r24 基础数据（不变）+ 针对“r24 + 预检”剩余失分的正例（否定查询、否定降落→返航、查询落地、别名、返航意图，各 72 条），r24 热启动；候选与基线都开主机预检，并纳入 planning-v5。

| 指标 | r24 + 预检（现役） | r29 + 预检 |
|---|---:|---:|
| planning-v3 | 0.80 | 0.7125 |
| planning-v4 | 18/20 | 17/20 |
| planning-v5 | 93/126 | 96/126 |
| 返航探测 | 15/15 | 14/15 |

结论：**不替换**。v5 小幅提升（query_grounding 8→11），但 v3 降 9 个点、探测掉 1 题。从 r25 到 r29 连续五轮，凡是在 r24 上追加训练数据，v3 都会下降，说明在当前 2B 模型和数据规模下，规划模型已接近这条路线的上限；现役维持 **r24 + 主机预检**（v3 0.80、v5 93/126、探测 15/15）。规划这条线暂停新训练轮次，后续更值得做的是：用 Linux SITL 任务套件验证现役组合的闭环表现、在真实任务文本上核对预检，以及把剩余错误类型（否定查询、否定降落）优先考虑用主机侧意图检查兜底而不是加训练数据。

## r30 数据准备（离线）

nightly ext-2 的 `follow-red-person-return` 暴露了新的可执行错误：r24 在已经明确
目标的 follow 任务前插入 `area_search`，耗尽 180 步预算，套件期望的
`follow -> return_home` 没有开始。对 r24 + 主机预检的 planning-v5 缓存逐题复核后，
33 个模型失分归为“禁止范围额外动作”11、“漏掉中间动作”11、“技能替换”6、
“未落地引用”3、“终步混淆”1 和“额外导航步骤”1；预检正确处理的能力/未知地点题
不作为训练错误。逐题归类和 follow 证据见 [`docs/r30-planning-errors.md`](r30-planning-errors.md)。

`--r30-recipe` 只在 r24 基础上增加四族可执行正例，默认新增 240 行：
`direct_follow` 80、`step_fidelity` 80、`terminal_intent` 40、`grounded_refs` 40，
各族中英文各半。direct-follow 行的首步固定为 `follow`，不含 `area_search`；其余族
分别保留显式中间动作、区分 `land`/`return_home` 终步和解析已给引用。r30 不继承
r25-r29 的结构化拒绝或拒绝候选，避免把 host pregate 逻辑重新教给模型。

只生成数据的命令如下（不训练、不评测）：

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
export MULTIDETECT_PLANNER_SCRATCH=$PWD/.tmp/planner-scratch-r30
python scripts/run_planner_round.py --stage data --force-stage data \
  --out .tmp/r30-data --eval-root .tmp/r30-eval \
  --rows 2000 --val-rows 100 --intent-count 80 \
  --replay-planner-train artifacts/datasets/plan-sft-v4/train.jsonl \
  --negative-target-mode rejected_candidate --r30-recipe \
  --r30-direct-follow-count 80 \
  --r30-step-fidelity-count 80 \
  --r30-terminal-intent-count 40 \
  --r30-grounded-refs-count 40
```

holdout 同时覆盖 v3/v4/v5、15 条返航 probe 和 follow suite 文本；生成器会在 manifest
记录 family、首技能、语言和碰撞数，`dataset/r23-data-stats.md` 会写出四族计数及泄漏
检查。r30 数据构造仍使用 CPU 准入检查，不启动 SITL、Docker、llama-server 或 GPU。

## r30 结果与决定（2026-11-04）

r30 是 r24 配方加 240 条定向正例（`direct_follow` 80、`step_fidelity` 80、`terminal_intent` 40、`grounded_refs` 40）。本轮只读比较报告和评测缓存，没有改动 artifacts。

| 指标 | r30 candidate | r24 baseline | 差值 |
|---|---:|---:|---:|
| planning-v3 精确步骤序列率 | 0.750 | 0.800 | -5.0 pp |
| planning-v4 精确/正确拒绝率 | 0.950 | 0.900 | +5.0 pp |
| planning-v5 精确/正确拒绝率 | 0.746（94/126） | 0.714（90/126） | +3.2 pp |
| 返航 probe | 15/15 | 15/15 | 持平 |

决定：**不替换，继续使用 r24**。v3 是主回归闸门，r30 低 5 个百分点；v4 的提升不足以抵消 v3 回退，probe 也没有新增收益。

逐题翻转方面，v3 有 6 题由错变对、10 题由对变错。提升覆盖 `goto→count→land`、补回 `hover`、`monitor`、英文 `record→land` 等；回退包括 `count→return_home` 变 `count→land`、重复 `record`/`patrol`、漏掉 `goto`/`hover`/`survey`，以及 `record`↔`inspect`、`record`↔`monitor`、`monitor`↔`hover` 的相近技能替换。v4 只有 1 个翻转：英文 `query_ungrounded` 从 `goto→return_home` 改为 `inspect→return_home`，因此正确率提升 0.90→0.95。v5 有 7 题提升、6 题回退：`query_grounding` 0.571→0.786、`return_negated_land` 0.500→0.643；但 `return_intent` 0.714→0.571、`ref_alias` 0.643→0.571、`query_ungrounded` 0.214→0.143。

可复核的逐题索引（JSON rows 的 0-based index）为：v3 提升 `2,5,24,28,46,47`，回退 `0,7,26,31,32,56,60,64,69,71`；v5 提升 `7,17,18,20,100,104,106`，回退 `1,6,11,22,96,112`。v3 提升族主要是 `step_fidelity`/`terminal_intent`，但同两族也包含大部分回退；v5 提升集中在 `return_negated_land` 和 `query_grounding`，回退集中在 `return_intent`、`ref_alias`、`query_ungrounded`。这些索引对应的 `text`、`expected`、`predicted` 均保留在 candidate/baseline JSON，避免在文档中复制长任务文本。

按 r30 目标族看，`direct_follow` 在 v3/v4/v5 没有可直接验证的 follow 题，不能宣称已修复；`step_fidelity` 只部分生效，补回若干中间步骤但仍有大量漏步和相近技能替换；`terminal_intent` 在 v5 的“不要返航、就地落地”上有收益，却在 v3 引入终步漂移；`grounded_refs` 使 v4 的 query 拒绝变正确，但 v5 `ref_alias` 反而回退。因此四族都没有达到“目标错误已稳定修复”的标准。

回归更符合数据组成与模板重叠：r30 训练集 5,620 行中，r24 replay 1,500 行（26.7%），r23 扩展 680 行，r24 定向行 160 行，r30 定向行 240 行（4.3%），其余为既有 backbone/能力数据。新增族使用高度规则化的中英文短模板，`goto`、`record`、`hover`、`monitor`、`land/return_home` 反复共现；模型在通用 v3 长链上出现动作复制、漏首步/终步和相近技能替换。holdout collision 为 0，只表示没有文本泄漏，不表示模板组合没有分布重叠。

follow mission check：`configs/onboard/suite-follow-r21idr3-r24plan.json` 期望 `[follow, return_home]`。r30 artifacts 下没有该 suite 的缓存 plan、`mission_summary.json` 或 `steps.jsonl`，所以本项为**离线未验证**；不能声称首步已经是 `follow`。已有 r24 ext-2 证据仍为 `[area_search, follow, return_home]`，正是 `direct_follow` 的目标错误。

### r31 配方

先保护 r24 的通用 v3 分布，再做小规模验证：保留 1,500 条 r24 replay，把四族缩小为 120 条（`direct_follow` 40、`step_fidelity` 40、`terminal_intent` 20、`grounded_refs` 20），中英文各半；按 `record/inspect`、`monitor/hover`、`patrol/goto`、`land/return_home` 分层，并使用与 holdout 不同的句式模板。不要加入新的拒绝目标，继续由 host pregate/validator 处理 `query_ungrounded` 和未知引用；按 family×skill×language 分桶采样，避免短模板挤压多步骤 v3 数据。r24 热启动、1 epoch、学习率不高于 `2.5e-5`。评测除 v3/v4/v5 和 15 条 probe 外，必须补跑 follow suite 的 planner-only 检查，断言首步为 `follow` 且计划不含 `area_search`；若 v3 仍低于 0.80 或 probe 低于 15/15，停止替换尝试。
