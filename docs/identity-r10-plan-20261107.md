# r21id-r10 身份轮次计划（2026-11-07）

当前生产身份模型仍是 **r21id-r3**。r9t 没有替换 r3：其 v3 身份准确率为 0.9142，低于 r3 的 0.9185；`<12 px` 真目标召回为 0.6757，高于 r3 的 0.6216；v3 干扰物拒绝率为 0.9741，低于 r3 的 1.0000，并出现 3/116 条新增误收。r10 的目标是验证 r9r 的新混淆方向是否能在保持 r3 稳定性的同时改善难负例，而不是因为小目标召回上升就直接替换生产模型。

## 数据与切分

r10 以 `artifacts/r21id-r3/pipeline/r15-sft` 为基线数据，训练集完整 replay（`--r12-replay-fraction 1.0`），从 r3 adapter 热启动。r9r 使用 `configs/scenarios/generated-r21id-r9r/index.json` 的 world split：36 个 train world 只进 train，12 个 val world 只进 val；同一 world 的 episode、frame 和四视角不得跨 split。r3 的冻结 test 世界和 v3/v1 评测集保持原样，不把 r9r pilot 或 full collection 混入其中。

目标有效训练混合为 **r3 replay 80% + r9r additions 20%**。80/20 是行数目标，不是把相关 frame 当作独立样本的统计权重；如果 full builder 产出超过 20%，按 `world × pair × view × polarity` 分层截取 r9r additions，保留每个 pair 和视角的 world 覆盖，不抽取 r3 replay。r9r 的拒绝上限用每个 confusable pair/view 180 行，避免 front 视角耗尽全局预算。保留 `confusable-negatives`、`task-text-variants`、identity/confusable view balance，`max-upsample=1`，不对稀疏视角复制多份。

pilot 的旧 CPU builder 输出（`artifacts/r21id-r9rb/gpu-annotate/sft/train.jsonl`，8 个 world、4,372 行）按目标 pair/view 如下。这个旧目录是在 SUV 正例修复前生成的，因此 `suv→postbox` 没有正例。

| pair | view | positive | rejection | total |
|---|---|---:|---:|---:|
| car→mailbox | front | 45 | 30 | 75 |
| car→mailbox | oblique | 49 | 30 | 79 |
| car→mailbox | edge_truncated | 45 | 30 | 75 |
| car→mailbox | recovery | 68 | 30 | 98 |
| suv→postbox | front | 0 | 30 | 30 |
| suv→postbox | oblique | 0 | 30 | 30 |
| suv→postbox | edge_truncated | 0 | 30 | 30 |
| suv→postbox | recovery | 0 | 30 | 30 |

旧 pilot 的两个 pair 合计为 327 行 car→mailbox、120 行 suv→postbox。这些旧行不能证明 SUV 正例已经覆盖。

本轮已用当前 builder 在 CPU 上对同一 pilot proposals 做重建，输出只写入当前 worktree 的 `.tmp/r9r-suv-postbox-r10-rebuild-20261107/sft-rebuilt`；输入 index 是从 `configs/scenarios/generated-r21id-r9r/index.json` 恢复的 8-world train 子集，SHA256 为 `727ffe9c16a3ab3084a0facaa0dbf2c5278d486238d1194496fab54a3453b13e`，与原 pilot 一致，sidecar 通过同目录符号链接只读引用。复跑使用主 `.venv/bin/python`，其余参数与队列 full 重建相同（包括 per-view cap 180、两种 view balance、`max-upsample=1`）；只把 index、proposal 和输出路径换为 pilot 对应路径。

最终 train 为 **4,993 行、8 个 world**，val/test 均为 0，决策冲突为 0。下表统计最终 JSONL 的 `confusable_pair_type` 与 `label_type`，不是去重前生成次数；这是 CPU 数据预演，没有启动采集、GPU annotate 或训练。

重建 manifest 为上述输出目录内的 `manifest.json`，逐 pair/view 统计另存于 `.tmp/r9r-suv-postbox-r10-rebuild-20261107/pair-view-counts.json`；builder SHA256 为 `93138302b2979094b0c547b91c52bb6c3f256c3eeef5b711db57443fdac71488`。

| pair | view | positive | rejection | total |
|---|---|---:|---:|---:|
| car→mailbox | front | 44 | 68 | 112 |
| car→mailbox | oblique | 50 | 86 | 136 |
| car→mailbox | edge_truncated | 53 | 76 | 129 |
| car→mailbox | recovery | 68 | 110 | 178 |
| suv→postbox | front | 17 | 31 | 48 |
| suv→postbox | oblique | 27 | 41 | 68 |
| suv→postbox | edge_truncated | 21 | 37 | 58 |
| suv→postbox | recovery | 23 | 41 | 64 |

两个 pair 合计分别为 215 positive + 340 rejection、88 positive + 150 rejection。manifest 的 `source_counts` 记录正例生成数分别为 327、150；正例不受 rejection cap 限制，最终模型输入去重会合并相同正例并可能保留为普通 `true_target`，所以不能把生成数当作最终 pair-tag 行数（见 [SUV 正例诊断](r9r-suv-postbox-positives-20261107.md)）。当前 builder 已恢复四视角 SUV 正例；本次同时把 cap 从 30 提到 180，拒绝数量增加也包含 cap 变化，不能全部归因于类别修复。

## Builder 与训练配方

配置文件为 [`configs/training/identity-r10-r21id.json`](../configs/training/identity-r10-r21id.json)，单卡队列模板为 [`scripts/queue_r21id_round_r10.sh`](../scripts/queue_r21id_round_r10.sh)。full annotate 所在 worktree 固定在 SUV 正例修复之前，因此不能直接使用其 `gpu-annotate-full/sft`。队列先用当前 worktree 的 builder、主 `.venv/bin/python`（可用 `BUILDER_PY` 覆盖）在 CPU 上从 `gpu-annotate-full/proposals.jsonl` 和完整 r9r index 重建 `gpu-annotate-full/sft-rebuilt`，per-view rejection cap 为 180，再把该目录交给 `scripts/cap_identity_round_additions.py` 做 80/20 mix。

模板保留默认 `DRY_RUN=1`，只打印命令；显式设为 `DRY_RUN=0` 才会实际执行重建、mix、merge 和 runner。本轮只读共享 artifacts，CPU pilot 重建和队列 dry-run 日志均写本树 `.tmp`；检查时 `gpu-annotate-full/proposals.jsonl` 尚不存在，full 重建和 full mix 尚未运行，也没有启动训练或 GPU 任务。

训练参数沿用 r3/r9t 的慢模型入口：1 epoch、gradient accumulation 4、LoRA rank 16、alpha 32、学习率 `5e-5`、最大边 448、`candidate_zoom_v2`、r3 adapter hot-start，单卡 `cuda:0`。这些参数先固定，避免把数据变化和优化器变化混在一轮里；只有在 r10 失败且数据证据足够时再做独立超参实验。

## 验收门槛

先在 r3 使用过的同一套评测上比较候选与 r3：v3 身份准确率、`<12 px` 真目标召回、v3 干扰物拒绝率和 false accepts；再用 r3 冻结四视角 test 做跨视角报告。建议的替换硬门为：

- v3 身份准确率 `≥ 0.9185`（不低于 r3）；
- `<12 px` 真目标召回 `≥ 0.6216`，首选目标是恢复 r9t 的 `0.6757`；
- v3 干扰物拒绝率 `≥ 0.9828`，允许相对 r3 最多下降 2 个百分点；
- 116 条显式干扰物最多 2 条 false accepts，并逐条列出 task、候选类别、尺寸档、world、target_id 和 sample_id；首选目标是 0 条。

新增的 r9r 专项只在 r9r val worlds 上计算。对 `car→mailbox`、`suv→postbox` 的四个 view 分格分别报告行数、独立 world 数、拒绝率和误收；每格至少 3 个未见 val world，拒绝率至少 0.90，每格最多 1 条 false accept。若某格没有足够独立 world 或 `suv→postbox` 仍无正例，结果只能标记为 evidence gap，不能据此替换模型。

## 风险与处理

**样本量和相关性。** pilot 每方向只有 4 个 world，帧、episode、view 高度相关；“每视角 30 条”不是 30 个独立样本。报告同时给行数和独立 world/target 数，full collection 至少按多个未见 world 验收。

**pilot/full 泄漏。** pilot 的 8 个 world 不得进入 full 的 val/test；如果 full collection 重用同一 world、RGB 或 proposal cache，必须从 r9r val 中剔除并在 manifest 记录。尤其不能用 pilot builder 行调参后再把同一行作为最终 val 证据。

**视角标签可信度。** r9r pilot 曾出现 route label 与实际相机几何不一致，旧帧几乎都落在 front。full collection 必须使用修复后的 route，并检查每个 view 的目标方位和短边；否则 per-view gate 只是标签复现，不是几何视角覆盖。

**proposal/正例不对称。** 旧 pilot 的 SUV 正例缺失已由当前 builder 修复，本次 CPU 重建在四视角均有 SUV 正例。full collection 仍须用当前 builder 重建后审计 proposal→positive funnel 和每个 pair/view 的覆盖；pilot 恢复正例不能替代 full 的产出证据。

**固定集过拟合。** r3 v3/four-view 集是必要的同口径对照，不足以证明新 world 泛化。只有固定集门槛和 r9r 未见 world 分格都通过，才提交生产替换评审；否则继续保持 r21id-r3。
