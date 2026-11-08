# r10/r3 r9r val 成对评测审计

审计日期：2026-11-08。评测已在 `artifacts/r21id-r10/evaluation/r9r-val/` 完成；本审计只读取已有产物，没有重跑评测。共享 `artifacts/` 按只读处理。

## 输入与分组核对

`artifacts/r21id-r10/data/r15-additions-mix/val.jsonl` 有 4,665 行，均为 `split=val`。index 有 48 个 world，其中 36 train、12 val；val 的 12 个 world 按 `r9r_direction` 分为 6 个 `car→mailbox` 和 6 个 `suv→postbox`。每个方向在 index 中的 6 个 val world 均进入对应四个 view cell。

评测器只接受 test split，因此作业目录 `/data/projects/Multi-Detect-env/runs/src-r10/.tmp/r9r-val-dataset/test.jsonl` 保存 val 内容并将 split 改为 `test`。逐行比较确认除 split 值外内容一致；评测输入说明见该目录的 `README.txt`。r3 与 r10 predictions 各有 4,665 行，sample_id 均唯一，和 val 的 sample_id 集完全相等，且 harness error 数为 0。

cell 口径：按 index `world_id → r9r_direction` 定 pair，按 val 行 `view` 定 cell；分母为 cell 中 `label_type=confusable_negative` 的行。拒绝率是预测 `reject_track` 的行数除以该分母；`approach_track` 和 `confirm_target` 均计 false accept。使用 index 的 world direction 而不直接用 `confusable_pair_type`，因为拒绝样本上该字段会记录反向或次级 confusable pair。11 条 `view=null` 的 confusable-negative 行不进入 8 个指定 cell。独立 world 数按 cell 内唯一 world 计算。

## 8 个 pair×view cell

| pair | view | 独立 world | rejection 样本 | r21id-r3 拒绝 | r21id-r3 rate | r21id-r3 false accepts | r21id-r10 拒绝 | r21id-r10 rate | r21id-r10 false accepts |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| car→mailbox | front | 6 | 81 | 81 | 1.0000 | 0 | 81 | 1.0000 | 0 |
| car→mailbox | oblique | 6 | 95 | 95 | 1.0000 | 0 | 95 | 1.0000 | 0 |
| car→mailbox | edge_truncated | 6 | 85 | 85 | 1.0000 | 0 | 85 | 1.0000 | 0 |
| car→mailbox | recovery | 6 | 118 | 118 | 1.0000 | 0 | 118 | 1.0000 | 0 |
| suv→postbox | front | 6 | 181 | 181 | 1.0000 | 0 | 181 | 1.0000 | 0 |
| suv→postbox | oblique | 6 | 169 | 167 | 0.9882 | 2 | 169 | 1.0000 | 0 |
| suv→postbox | edge_truncated | 6 | 154 | 154 | 1.0000 | 0 | 154 | 1.0000 | 0 |
| suv→postbox | recovery | 6 | 58 | 58 | 1.0000 | 0 | 58 | 1.0000 | 0 |

每格验收线为至少 3 个未见 world、rejection rate ≥0.90、false accepts ≤1。r10 在全部 8 格达到 6 个 world、rejection rate 1.0000、0 false accepts，r9r val cell 门通过。r3 仅 `suv→postbox / oblique` 未通过误收上限：167/169 拒绝，2 个 false accepts。

## r21id-r3 误收明细

| sample_id | world | target_id | view | task | 预测 | 说明 |
|---|---|---|---|---|---|---|
| `c3e47d4fb4030635d3e7c8c0` | `gen_r21id_r9r_w0045` | `distractor_0045_02` | oblique | Find the jersey barrier and record its position. | `approach_track` | 期望 `reject_track`；r10 对应预测为 `reject_track` |
| `dd0b0bf7980ac1a56725983e` | `gen_r21id_r9r_w0045` | `distractor_0045_02` | oblique | Find the jersey barrier and record its position. | `confirm_target` | 期望 `reject_track`；r10 对应预测为 `reject_track` |

两行分别是不同 sample_id；虽然处于 SUV→postbox world 的 oblique cell，数据标签的 pair 字段为 `jersey_barrier→person`。按上述 index 方向分组口径仍计入该 cell，保留了该方向 val world 中全部 hard-negative rejection 观察。

## 报告级指标与总体判定

预测文件全 val 汇总的 identity accuracy 为 r3 0.9601、r10 0.9886；全 val distractor rejection rate 为 r3 0.9773、r10 0.9995。identity report 的 target_id 汇总口径显示跨视角一致率均为 0.3333（n=27），真目标拒绝率 r3 0.0510、r10 0.0186，显式干扰物接受率 r3 0.0027、r10 0.0009。这些全局值和 pair×view cell 统计口径不同。

**最终判定：保留 r21id-r3 为生产模型，不准入 r10 替换。** r10 的 r9r val cell 门全部通过，但整体准入仍被冻结四视角硬门阻断：r10 跨视角一致率为 0.7667，低于 0.90；显式干扰物接受率为 0.0583，高于 0.026。此外，v3 `<12 px` recall 从 r3 的 0.6216216 增至 r10 的 0.7027027，提升 8.1081 pp，未达到 10 pp 的相对提升目标。生产模型选择没有变更。

## 输入与输出 SHA256

| 文件 | SHA256 |
|---|---|
| `artifacts/r21id-r10/data/r15-additions-mix/val.jsonl` | `683f698f675675347c56a6fd2c57bcf5e7e623dad49a1dbb5fddb63d3b821645` |
| `configs/scenarios/generated-r21id-r9r/index.json` | `933ebe8aab59eea706760177d0f351f2f9f1beb4e0199a6b38587f6bde12e6b8` |
| 作业目录 `r9r-val-dataset/test.jsonl`（重标 split 的评测输入） | `64224209ef99a426a82f5e1ab36b32e9116e6de7e49597758bc536f76d3c31f6c` |
| `artifacts/r21id-r10/evaluation/r9r-val/r21id-r3-predictions.json` | `033c0dee5f804f84fe9e7098d5b907aef42b29180ec6a7d39589e11e7ca58161` |
| `artifacts/r21id-r10/evaluation/r9r-val/r21id-r10-predictions.json` | `1aee8d2f63b9dab88177e997aaf539b344501411efc12be2da76ac3b6d083ea1` |
| `artifacts/r21id-r10/evaluation/r9r-val/identity-report.json` | `8810146e3938322e725978d405535ec85ba4ea2fe344c1375b744ab7cf2348c0` |
| `artifacts/r21id-r10/evaluation/r9r-val/identity-report.md` | `818b9cb569253bab7104b801d089fde0dd17c64473b08406c09bef31230b6722` |

本次仅运行只读 JSONL/JSON 解析、sample_id/world/cell 对照、预测计数与 SHA256 校验；未运行 pytest、onboard gate、模型评测或其他评测任务，也未改动 `artifacts/`、训练配方或生产模型选择。
