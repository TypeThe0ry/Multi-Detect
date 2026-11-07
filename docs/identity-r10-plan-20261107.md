# r21id-r10 身份轮次计划（2026-11-07）

当前生产身份模型仍是 **r21id-r3**。r9t 没有替换 r3：其 v3 身份准确率为 0.9142，低于 r3 的 0.9185；`<12 px` 真目标召回为 0.6757，高于 r3 的 0.6216；v3 干扰物拒绝率为 0.9741，低于 r3 的 1.0000，并出现 3/116 条新增误收。r10 的目标是验证 r9r 的新混淆方向是否能在保持 r3 稳定性的同时改善难负例，而不是因为小目标召回上升就直接替换生产模型。

## 数据与切分

r10 以 `artifacts/r21id-r3/pipeline/r15-sft` 为基线数据，训练集完整 replay（`--r12-replay-fraction 1.0`），从 r3 adapter 热启动。r9r 使用 `configs/scenarios/generated-r21id-r9r/index.json` 的 world split：36 个 train world 只进 train，12 个 val world 只进 val；同一 world 的 episode、frame 和四视角不得跨 split。r3 的冻结 test 世界和 v3/v1 评测集保持原样，不把 r9r pilot 或 full collection 混入其中。

目标有效训练混合为 **r3 replay 80% + r9r additions 20%**。80/20 是行数目标，不是把相关 frame 当作独立样本的统计权重；如果 full builder 产出超过 20%，按 `world × pair × view × polarity` 分层截取 r9r additions，保留每个 pair 和视角的 world 覆盖，不抽取 r3 replay。r9r 的拒绝上限用每个 confusable pair/view 180 行，避免 front 视角耗尽全局预算。保留 `confusable-negatives`、`task-text-variants`、identity/confusable view balance，`max-upsample=1`，不对稀疏视角复制多份。

pilot 的 CPU builder 输出（`artifacts/r21id-r9rb/gpu-annotate/sft/train.jsonl`，8 个 world、4,372 行）按目标 pair/view 如下。该 manifest 是 pilot annotate 的既成 CPU builder 结果；本工作树尝试重跑时发现 `run.json` 指向的临时 worktree `pilot-index.json` 不在当前树，且全 campaign 重建超出本轮 dry-run 时限，因此以下数字以产物 JSONL 和 manifest 交叉核对为准。

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

因此 pilot 的两个 pair 合计为 327 行 car→mailbox、120 行 suv→postbox。`suv→postbox` 没有 identity positive，不能把它当作已覆盖的正例方向；full collection 必须确认同类 SUV 正例是否进入 proposals 和 builder。

## Builder 与训练配方

配置文件为 [`configs/training/identity-r10-r21id.json`](/data/projects/Multi-Detect-env/mdwt/cx-identity-r10-prep/configs/training/identity-r10-r21id.json)，单卡队列模板为 [`scripts/queue_r21id_round_r10.sh`](/data/projects/Multi-Detect-env/mdwt/cx-identity-r10-prep/scripts/queue_r21id_round_r10.sh)。模板默认 `DRY_RUN=1`，显式设为 `DRY_RUN=0` 才会调用 runner；本轮没有启动训练。

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

**proposal/正例不对称。** pilot 的 suv→postbox 四视角只有 reject，没有 SUV identity positive；这可能是 detector proposal 缺失或 builder 配对失败。full annotate 后先审计 proposal→positive funnel，再决定是否把该方向用于训练；不能用其他 pair 的正例替代。

**固定集过拟合。** r3 v3/four-view 集是必要的同口径对照，不足以证明新 world 泛化。只有固定集门槛和 r9r 未见 world 分格都通过，才提交生产替换评审；否则继续保持 r21id-r3。
