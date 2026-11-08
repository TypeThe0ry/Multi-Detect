# r9t、r6 与 r6b 未超过 r3 的逐样本分析（2026-11-07）

## 结论

r9t 的 v3 身份准确率为 0.9142，低于 r3 的 0.9185；小于 12 px 的真目标召回从 0.6216 升到 0.6757，但 v3 干扰物拒绝率从 1.0000 降到 0.9741。r6 和 r6b 也呈现相同的取舍：小目标召回虽有提高，身份准确率与干扰物拒绝率都低于 r3。固定集上的少数行差异不足以证明稳定改进，也不能证明回退只会发生在这些样本。

| 轮次 | v3 身份准确率 | <12 px 真目标召回 | v3 干扰物拒绝率 | 相对 r3 的主要取舍 |
|---|---:|---:|---:|---|
| r3 | 0.9185 | 0.6216 | 1.0000 | 基线 |
| r6 | 0.9013 | 0.6757 | 0.9483 | 小目标召回 +5.4 pp，身份与拒绝退化 |
| r6b | 0.9056 | 0.6757 | 0.9569 | 小目标召回 +5.4 pp，身份与拒绝退化 |
| r9t | 0.9142 | 0.6757 | 0.9741 | 小目标召回 +5.4 pp，身份略降且新增误收 |

## r9t 与 r3 的逐样本差异

按 `scripts/report_identity_errors.py` 将 v3 冻结集 `flight-identity-eval-v2-v3prop/test.jsonl` 与两份 `eval-v3.json` 按 `sample_id` 对齐。两模型均为 233 行，其中真目标 117 行、干扰物 116 行。误差报告所用短边尺寸是从候选框重新计算的；下表直接列像素值，均落在脚本对应尺寸档内。

### r9t 新增误收

以下 3 条在 r3 被拒绝、r9t 被接受；3 条都计入 116 个干扰物样本的误收增量（0/116 → 3/116）。评测数据没有 `view`、`view_name`、`observation_view` 或 `sequence_view` 字段，因此无法可靠还原视角，按工具口径标为 `unknown`，不能据此声称特定视角回退。

| Target pair（任务 → 候选真类） | View | 候选短边 | 评测尺寸档 | World / target_id | Sample ID |
|---|---|---:|---|---|---|
| car → postbox | unknown | 13.5 px | 12-16 | gen_v2_w0035 / distractor_0035_01 | `af88675786b1042568960b86` |
| person → jersey_barrier | unknown | 11.7 px | 8-12 | gen_v2_w0036 / distractor_0036_00 | `43aefe9b8ca70564f15ba7ea` |
| pickup → unknown（truth 类别缺失） | unknown | 6.1 px | <8 | gen_v2_w0037 / obstacle_03 | `3d0f48d01f9d9e177bd12d60` |

误收率差为 +2.59 个百分点。若把 116 行视为独立伯努利样本，3/116 的 95% Wilson 区间约为 0.9%–7.3%；该区间不含 0 但很宽，r3 的 0/116 本身也有不确定性。样本来自少数固定世界，且数据没有视角标注，独立同分布假设并不成立；区间仅用于展示行级小样本不确定性，不是对新世界泛化的保证。

### r9t 新增命中的 <12 px 真目标

按 v3 指标口径，<12 px 真目标召回为 23/37 → 25/37（提升 2 行，即 +5.41 pp）。工具逐行核对发现两行增益均来自同一 `pickup → pickup` 真目标、同一世界和同一 `target_id`；二者只是两条小框观测，不是两个独立目标。数据没有视角字段，故 view 为 unknown。

| Target pair | View | 候选短边 | 评测尺寸档 | World / target_id | Sample ID |
|---|---|---:|---|---|---|
| pickup → pickup | unknown | 11.0 px | 8-12 | gen_v2_w0037 / target_true_gen_v2_w0037 | `b94b4d75232e75b4db2831e8` |
| pickup → pickup | unknown | 9.7 px | 8-12 | gen_v2_w0037 / target_true_gen_v2_w0037 | `a22343b06b4e76858e90ad69` |

按 37 行独立计算，2/37 的增益比例之 95% Wilson 区间约为 1.5%–17.9%；点估计虽为 +5.4 pp，区间很宽。更关键的是两行聚集于一个目标/世界，因此有效独立目标数只有 1，不能据该区间将增益推广到其他目标或世界。v3 总体身份准确率的净变化则是 199/233 → 192/233，下降 7 行（-3.00 pp）：两条新增真目标命中没有抵消三条新增误收及其余样本上的差异。

## 下一轮数据与门槛

`docs/r9r-plan-20261106.md` 计划新增 `car→mailbox` 和 `suv→postbox` 两个方向；pilot 为每方向 4 个 world、每 world 2 个 confuser episode，路线覆盖 front、oblique、edge_truncated、recovery，且按方向分别检查总 rejection 至少 96 条、每视角至少 8 条。它能直接增加 car/mailbox 与 SUV/postbox 的难拒绝数据，并补齐 `car→mailbox` 的 recovery 构建缺口；对本次 `car→postbox` 回退最多是相邻类别的间接覆盖，不是同一 pair 的复现。计划没有覆盖 `person→jersey_barrier`、`pickup→unknown/obstacle_03`，也没有直接增加本次 `pickup→pickup` 的 <12 px 真目标例。因此 r9r 对这些回退不能充分作答。下一轮评测还应保留这些已回退 pair，增加同 pair 的正负配对与小目标正例，并在数据生成时记录 view、target_id 和候选尺寸，以便判断差异是否跨视角、跨目标复现。

r9r 的“总量 ≥96 且每视角 ≥8”适合作为 pilot 是否有足够采集/builder 输出、是否扩采的操作门槛；它不应被解释为模型验收已有统计把握。计划的 pilot 每方向只有 4 个 world，而同一 world 的帧、episode 和视角相关；96 行不能等同于 96 个独立样本，且每视角至少 8 仍可能集中在少数 world。统计上更有意义的后续验收应按独立 world/target 汇总、报告区间，并让每个关键 pair 在多个未见 world 中出现。此分析不修改任何验收门槛；只指出现有行数门槛回答的是数据产出/扩采问题，不足以单独回答模型是否稳定优于 r3。

本次只读现有 CPU 评测产物并运行错误分层脚本；没有模型推理、训练或 GPU 工作。
