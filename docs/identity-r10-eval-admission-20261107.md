# r21id-r10 模型准入对比（基于 2026-11-07 评测产物）

**最终结论：不准入 r10 替换生产模型；继续使用 r21id-r3。** 2026-11-08 已有 r9r val 成对推理产物；r10 通过 8 个 r9r pair×view cell 门，但冻结四视角绝对门槛仍失败（跨视角一致率 0.7667 < 0.90、显式干扰物接受率 0.0583 > 0.026），且 v3 `<12 px` recall 相对 r3 提升 8.11 pp，未达到要求的 10 pp。因此泛化 cell 结果不能覆盖其余硬门失败。本审计只对照现有只读产物，不修改生产模型选择。

## 评测证据

固定集结果来自 [`r15-report.md`](../artifacts/r21id-r10/pipeline/r15-report.md)、`eval-v1.json` 和 `eval-v3.json`；四视角结果来自 [`identity-report.md`](../artifacts/r21id-r10/evaluation/r3-four-view/identity-report.md) 及其 JSON。r3 和 r10 使用同一 v1/v3 数据集与同一冻结四视角 test 集。

### pipeline 报告中的全部指标

表中差值为 r10−r3；百分数值按百分点解释。`—` 表示此项没有单独定义准入线。

| 指标 | r21id-r3 | r21id-r10 | 差值 | 门槛 / 判定 |
|---|---:|---:|---:|---|
| v1 identity accuracy | 0.9458 | 0.9595 | +0.0137（+1.37 pp） | 描述性指标；r10 更高 |
| v1 distractor rejection | 0.9932 | 0.9825 | −0.0107（−1.07 pp） | 描述性指标；r10 较低 |
| v3 identity accuracy | 0.9184549 | 0.9313305 | +0.0128755（+1.29 pp） | ≥0.9185：r3 按未舍入值低 0.0000451；r10 通过 |
| v3 `<12 px` true-target recall | 0.6216216 | 0.7027027 | +0.0810811（+8.11 pp） | ≥0.6216：两者通过；首选 ≥0.6757：仅 r10 通过；提升 ≥10 pp：r10 失败 |
| v3 distractor rejection | 1.0000 | 1.0000 | 0 | ≥0.9828：两者通过 |
| v3 显式干扰物 false accepts | 0/116 | 0/116 | 0 | ≤2/116：两者通过 |

两模型在 v3 的 116 条显式干扰物上都拒绝了 116 条。r10 的 `<12 px` recall 比 r3 提高 8.11 个百分点，未达到报告列出的相对提升 10 个百分点目标（还差 1.89 pp）；它超过绝对底线，并达到 r9t 的首选 recall 目标。r3 相对自身的提升为 0，未达到该提升目标。v1 distractor rejection 虽有下降，但 v1 没有本轮单独列出的绝对准入线；v3 对应硬门通过。

### 冻结四视角报告中的全部指标

置信区间为报告的 target_id bootstrap 95% 区间；`n` 是该项计分观察数。消防栓小目标在该测试集没有样本。

| 指标 | r21id-r3 | r21id-r10 | 差值 | 绝对门槛 / 判定 |
|---|---:|---:|---:|---|
| 实例级跨视角一致率 | 0.7333 [0.5667, 0.9000], n=30 | 0.7667 [0.6000, 0.9000], n=30 | +0.0334 | ≥0.90：两者失败 |
| 拒绝后恢复率 | 1.0000 [1.0000, 1.0000], n=3 | 1.0000 [1.0000, 1.0000], n=2 | 0 | ≥0.85：两者通过；观察数很少 |
| 平均恢复观察次数 | 1.0000 [1.0000, 1.0000] | 1.0000 [1.0000, 1.0000] | 0 | ≤2：两者通过 |
| 真目标总体拒绝率 | 0.0859 [0.0362, 0.1574], n=838 | 0.0788 [0.0298, 0.1404], n=838 | −0.0071 | ≤0.08：r3 失败，r10 通过 |
| 消防栓小目标拒绝率 | 无样本（n=0） | 无样本（n=0） | 无法比较 | ≤0.12：不可评估；实现未将缺样本列作失败 |
| 显式干扰物接受率 | 0.0943 [0.0497, 0.1454], n=1166 | 0.0583 [0.0279, 0.0954], n=1166 | −0.0360 | ≤0.026：两者失败 |

四视角绝对门槛并非只有 r10 失败：**r3 基线自身也未通过跨视角一致率和干扰物接受率门槛**。r10 在这两项上相对改善，但仍分别低于/高于绝对门槛；r10 的总体真目标拒绝率则刚好通过。报告中跨模型干扰物比较项通过（0.0583 ≤ 0.0943），但这不能替代 0.026 的绝对上限。消防栓指标无观察值，不能据此声称模型通过该专项。

四视角汇总样本情况：两模型各有 55 个带 target_id 实例、30 个完整四视角实例、46 个不完整实例；93 行没有 target_id，另有 67 行 view=null。r3/r10 的恢复率计分样本分别只有 3/2 个。CI 和这些样本数提示四视角指标应与固定集及后续未见 world 结果合并判断。

验收状态按评测器使用的点估计判定，置信区间用于展示不确定性，并不替代报告中的硬门。pipeline 的 v1/v3 总计数分别为 1,900 和 233 行；v3 `<12 px` bucket 为 77 行，显式干扰物为 116 行。

### r9r val pair×view 成对结果

评测产物位于 [`r9r-val`](../artifacts/r21id-r10/evaluation/r9r-val/)。评测输入是 `r15-additions-mix/val.jsonl` 的 4,665 行，仅将 `split` 改为 `test` 后供评测器读取；外部输入说明见 `/data/projects/Multi-Detect-env/runs/src-r10/.tmp/r9r-val-dataset/README.txt`。当前 val 与评测用 `test.jsonl` 按行完全相同（只有 split 值从 `val` 变为 `test`）。index 有 48 个 world（36 train、12 val）；12 个 val world 按 `r9r_direction` 分成每方向 6 个。预测文件均有 4,665 个唯一 sample_id，与 val 的 ID 集完全相同，且无 harness error。

cell 按 index 中的 world 方向及 val 行的 `view` 分组；分母是该 cell 中 `label_type=confusable_negative` 的行数，rejection rate = `decision=reject_track` 行数 / 分母。`approach_track` 和 `confirm_target` 均按误收计数。数据的 `confusable_pair_type` 对部分拒绝行记录次级或反向类别对，因此方向归属以 index 为准。11 条 `view=null` 的拒绝行不属于这 8 个 view cell。下表的独立 world 数按 cell 中唯一 world 计，行数不作为独立样本数。

| pair | view | 独立 world | 拒绝行 | r21id-r3 rejection | r21id-r3 false accepts | r21id-r10 rejection | r21id-r10 false accepts |
|---|---|---:|---:|---:|---:|---:|---:|
| car→mailbox | front | 6 | 81 | 81/81 (1.0000) | 0 | 81/81 (1.0000) | 0 |
| car→mailbox | oblique | 6 | 95 | 95/95 (1.0000) | 0 | 95/95 (1.0000) | 0 |
| car→mailbox | edge_truncated | 6 | 85 | 85/85 (1.0000) | 0 | 85/85 (1.0000) | 0 |
| car→mailbox | recovery | 6 | 118 | 118/118 (1.0000) | 0 | 118/118 (1.0000) | 0 |
| suv→postbox | front | 6 | 181 | 181/181 (1.0000) | 0 | 181/181 (1.0000) | 0 |
| suv→postbox | oblique | 6 | 169 | 167/169 (0.9882) | 2 | 169/169 (1.0000) | 0 |
| suv→postbox | edge_truncated | 6 | 154 | 154/154 (1.0000) | 0 | 154/154 (1.0000) | 0 |
| suv→postbox | recovery | 6 | 58 | 58/58 (1.0000) | 0 | 58/58 (1.0000) | 0 |

`≥3` 未见 world/cell、rejection rate `≥0.90`、false accepts `≤1/cell`：r10 的 8 格全部通过；r3 仅在 `suv→postbox / oblique` 因 2 个误收未通过。该 cell 的 r3 两条误收都来自 `gen_r21id_r9r_w0045`、`target_id=distractor_0045_02`、`view=oblique`、`task_text=Find the jersey barrier and record its position.`：sample `c3e47d4fb4030635d3e7c8c0` 预测 `approach_track`，sample `dd0b0bf7980ac1a56725983e` 预测 `confirm_target`；两条期望均为 `reject_track`。r10 对这两个 sample 均预测 `reject_track`。详见 [r9r val 专项审计](identity-r10-r9r-val-audit-20261107.md)。

评测报告另列的全 val 汇总值为：identity accuracy r3 0.9601、r10 0.9886；总体 distractor rejection rate r3 0.9773、r10 0.9995。`identity-report.json` 的 target_id 口径为：跨视角一致率两者均 0.3333（n=27），真目标拒绝率 r3 0.0510 / r10 0.0186，显式干扰物接受率 r3 0.0027 / r10 0.0009。它们是全 val 诊断值，不替代上表的 pair×view cell 门或冻结四视角硬门。

本次 r9r 输入/输出 SHA256：val JSONL `683f698f675675347c56a6fd2c57bcf5e7e623dad49a1dbb5fddb63d3b821645`；r9r index `933ebe8aab59eea706760177d0f351f2f9f1beb4e0199a6b38587f6bde12e6b8`；评测用重标 split 的 `test.jsonl` `64224209ef99a426a82f5e1ab36b32e9116ede7e49597758bc536f76d3c31f6c`；r3 predictions `033c0dee5f804f84fe9e7098d5b907aef42b29180ec6a7d39589e11e7ca58161`；r10 predictions `1aee8d2f63b9dab88177e997aaf539b344501411efc12be2da76ac3b6d083ea1`；`identity-report.json` `8810146e3938322e725978d405535ec85ba4ea2fe344c1375b744ab7cf2348c0`；`identity-report.md` `818b9cb569253bab7104b801d089fde0dd17c64473b08406c09bef31230b6722`。重标 split 的输入在评测运行目录，只读核对，未复制或修改。

## 准入项逐模型判定

| 验收项 | 门槛 | r21id-r3 | r21id-r10 |
|---|---:|---|---|
| v3 identity accuracy | ≥0.9185 | **失败（极小差距）**：0.9184549；报告四舍五入显示 0.9185 | **通过**：0.9313305 |
| v3 `<12 px` recall | ≥0.6216；首选 ≥0.6757 | **通过底线**：0.6216216；未达首选 | **通过**：0.7027027，且达首选 |
| v3 `<12 px` recall 相对 r3 提升 | ≥0.10 | **失败**：基线提升为 0 | **失败**：+0.0811，差 0.0189 |
| v3 distractor rejection | ≥0.9828 | **通过**：1.0 | **通过**：1.0 |
| v3 false accepts | ≤2/116 | **通过**：0/116 | **通过**：0/116 |
| 四视角一致率 | ≥0.90 | **失败**：0.7333 | **失败**：0.7667 |
| 四视角恢复率 | ≥0.85 | **通过**：1.0（n=3） | **通过**：1.0（n=2） |
| 四视角平均恢复观察次数 | ≤2 | **通过**：1.0 | **通过**：1.0 |
| 四视角真目标拒绝率 | ≤0.08 | **失败**：0.0859 | **通过**：0.0788 |
| 四视角消防栓小目标拒绝率 | ≤0.12 | **不可评估**：n=0 | **不可评估**：n=0 |
| 四视角显式干扰物接受率 | ≤0.026 | **失败**：0.0943 | **失败**：0.0583 |
| r9r val pair×view 泛化门 | ≥3 个未见 world/cell；rejection ≥0.90；false accepts ≤1/cell | **失败 1 格**：suv→postbox/oblique 为 167/169，2 个误收 | **通过**：8 格均 6 个 world、rejection=1.000、0 误收 |

## 证据状态与最终建议

截至 2026-11-08，r9r val 已在指定只读产物目录完成成对评测；本次审计只读取已有结果，没有重跑评测，也未启动 v1/v3 或四视角评估。8 格的逐项结果、误收清单及 SHA256 见上文和 [r9r val 专项审计](identity-r10-r9r-val-audit-20261107.md)。

复核输入 SHA256：`r15-report.md` `27ab3f8005ff9354b6246bebfdaaf25b783d9a6c62092bac11f9be640fba0f8b`；四视角 `identity-report.md` `3d077e520f1d54ef5f18772de873c00d261ba4cb7c830086698720e42d634613`；`eval-v1.json` `d97ca138d509b96d43f9afe9647ff3e66f685dd9ab835fc12319f0cc64d51270`；`eval-v3.json` `e2b30ef6d0bda3f740f151071fe52d0334ca171c18050249310332eea964e89c`；四视角 `identity-report.json` `0dc5f75b56cd1528ca60835ec9e2a3e9163cbf82b007befeae4cb1e2a172fa73`。

维持生产模型 **r21id-r3**，不切换到 r10。r10 的 8 个 r9r val cell 均通过，但冻结四视角的一致率与干扰物接受率硬门仍失败，且 v3 `<12 px` recall 相对提升未达 10 pp。生产模型选择未作修改。
