# r10 四视角误收与跨视角不一致：可观察模式

复核日期：2026-11-08。目标是离线描述冻结四视角集中的误收与跨视角差异，不评价或调整准入门槛。

## 结论

r10 的显式干扰物误收为 **68/1,166（5.83%）**，r3 为 **110/1,166（9.43%）**。两模型共同误收 63 行，另有 47 行仅 r3 误收、5 行仅 r10 误收；r10 的 68 行中 59 个决策为 `confirm_target`、9 个为 `approach_track`。按接受类决策（`confirm_target`、`approach_track`）统计，r10 的残余误收更集中在 `proposal_distractor` 标签、车/SUV与皮卡任务，以及少数重复出现的目标 ID。误收焦点候选框的归一化面积中位数高于拒收干扰物样本；单候选框样本的误收率也高于多候选框样本。

跨视角方面，评测器计入的 30 个完整实例中，r10 有 **23/30（76.67%）** 一致、7 个不一致；r3 有 **22/30（73.33%）** 一致、8 个不一致。r10 修复了 r3 的两个不一致实例，同时在另一个实例的 `recovery` 视角新增接受。共同异常既有真目标在某一视角被拒，也有干扰物只在某一视角被接受；异常涉及多个视角和任务类别，但现有分组伴随任务文本变化，无法据此拆分视角效应与任务文本效应。

## 数据与计分口径

- 冻结测试集 [`test.jsonl`](../artifacts/r21id-r3/pipeline/r15-additions/test.jsonl) 共 2,204 行，`sample_id` 唯一。r3 和 r10 预测各 2,204 行，均与数据集 `sample_id` 完全对齐。
- 评测报告 [`identity-report.json`](../artifacts/r21id-r10/evaluation/r3-four-view/identity-report.json) 的显式干扰物指标为 68/1,166（r10）和 110/1,166（r3）。这 1,166 行是有 `target_id`、非空四视角、且 `label_type` 含 `distractor` 的样本；含 `distractor` 的预测标签分为 `distractor` 与 `proposal_distractor`。`confirm_target` 和 `approach_track` 均按接受计。
- 数据另有 93 行无 `target_id`、67 行有 `target_id` 但 `view` 为空；它们不进入上述四视角实例一致性或显式干扰物计分。r10 有一条无 `target_id`、无候选框的 `no_candidate` 行输出无效，r3 没有无效行；该行不进入这些计分。
- 一致性按 `target_id` 分组，每个视角保留迭代中最后一行；只有恰好覆盖 `front`、`oblique`、`edge_truncated`、`recovery` 的完整组计入 30 个实例。口径可在 [`eval_r21id_identity.py`](../scripts/eval_r21id_identity.py) 的 `calculate_metrics` 中核对。因此，下文的误收率覆盖 1,166 行，而四视角一致性描述的是评测器选出的 30 组四视角记录，二者不是同一个样本分母。
- 这 30 组中有 28 组在四视角记录间出现中英文两种 `task_text`；7 个 r10 不一致组也全都出现这种文本变化。因此，观测到的接受/拒绝差异与视角变化同时伴随任务文本变化，不能单独归因于视角。

## 直接证据

### 误收集中在哪些标签与任务

| 冻结集标签 | r10 误收 / 样本数 | r10 误收率 | r3 误收 / 样本数 |
|---|---:|---:|---:|
| `distractor` | 9 / 667 | 1.35% | 24 / 667 |
| `proposal_distractor` | 59 / 499 | 11.82% | 86 / 499 |
| 合计 | 68 / 1,166 | 5.83% | 110 / 1,166 |

按 `task_text` 中请求的类别合并中英文表达后，r10 的误收分布如下。分母仍只含上述有视角的显式干扰物行。

| 请求类别 | r10 误收 / 干扰物行数 | r10 误收率 | r3 误收数 |
|---|---:|---:|---:|
| car / SUV | 32 / 221 | 14.48% | 45 |
| pickup（皮卡） | 26 / 268 | 9.70% | 47 |
| person（行人） | 5 / 388 | 1.29% | 7 |
| postbox（邮筒） | 5 / 289 | 1.73% | 11 |

因此，r10 虽比 r3 少误收 42 行，但车/SUV 与皮卡仍占 r10 误收的 **58/68**。误收也集中在少数实例：`confuser_0002_00_00` 有 14 行、`confuser_0009_00_00` 有 13 行、`distractor_0010_01` 有 11 行，合计 38/68；这三组误收均出现在皮卡、SUV 或 car 的对应任务文本下。这是样本分布上的集中，不代表单一实例的因果解释。

按视角汇总 r10 误收为 `front` 28、`edge_truncated` 17、`oblique` 15、`recovery` 8。各视角分母不同，对应误收率依次为 6.95%（28/403）、5.57%（17/305）、5.64%（15/266）、4.17%（8/192）。四种视角都有误收；仅凭计数不能把差异归因于视角本身。

### 候选框与候选数量

以下框统计从每行 `candidates` 中按 `focus_track_id` 取焦点候选，使用其 `box_xyxy_normalized` 计算归一化面积；分母是 1,166 行显式干扰物。边缘框按归一化坐标任一端点距图像边界不超过 0.001 计。没有把框面积换算为真实物理尺寸。

| r10 结果 | 焦点框面积中位数（归一化） | 四分位范围 | 触及图像边缘 |
|---|---:|---:|---:|
| 误收（68 行） | 0.00409 | 0.00099–0.01122 | 7 / 68 |
| 拒收（1,098 行） | 0.00063 | 0.00025–0.00265 | 79 / 1,098 |

误收行中，20 行只有一个候选框，48 行有多个；按候选数分层，单候选框样本误收 **20/81（24.69%）**，多候选框样本误收 **48/1,085（4.42%）**。这是明显的描述性差异，但候选数量、目标类别、框大小和场景可能互相关联，不能据此认定候选数量是误收原因。

几个可由 `sample_id` 直接复核的 r10 误收例子：

| sample_id | `target_id` / `label_type` | `task_text` | view | r3 → r10 | 焦点框 `box_xyxy_normalized` |
|---|---|---|---|---|---|
| `11c9bf837c918a6182049640` | `confuser_0002_00_00` / `proposal_distractor` | `Find the pickup and record its position.` | front | confirm → confirm | `[0.6216, 0.7777, 0.6506, 0.8788]` |
| `08e4dda175953fb592b7dd53` | `confuser_0009_00_00` / `proposal_distractor` | `找到SUV并记录位置` | front | confirm → confirm | `[0.4691, 0.7478, 0.5059, 0.8889]` |
| `038e4b4682749947f289f069` | `distractor_0010_01` / `proposal_distractor` | `Find the car and record its position.` | edge_truncated | approach → approach | `[0.4821, 0.5334, 0.5037, 0.5449]` |

### 四视角不一致实例

下表按评测器选出的每个 `target_id` / view 记录汇总 r10 的接受或拒绝；`接受`包括 `confirm_target` 与 `approach_track`。前六个模式也是 r3 的不一致实例；最后一行是 r10 新增的不一致。

| target_id | 对齐的 task_text；逐视角 label_type | r10 四视角结果（front / oblique / edge_truncated / recovery） | 分歧视角 sample_id | 观察到的分歧 |
|---|---|---|---|
| `target_true_gen_r21id_r3_w0000` | front/oblique 英文 pickup；edge/recovery 中文皮卡；front/oblique/recovery `true_target`、edge `proposal_true` | 接受 / 接受 / 拒绝 / 接受 | `fc3b09161fd6cdd62367a44d` | 真目标仅 edge_truncated 被拒；焦点框触及底边（`y2=1.0`）。 |
| `target_true_gen_r21id_r3_w0001` | front/recovery 英文 pickup；oblique/edge 中文皮卡；四视角均 `true_target` | 接受 / 拒绝 / 接受 / 接受 | `fe87b2338d7527683cab5a7d` | 真目标仅 oblique 被拒。 |
| `target_true_gen_r21id_r3_w0002` | front/oblique 英文 pickup；edge/recovery 中文皮卡；front/oblique `true_target`、edge/recovery `proposal_true` | 拒绝 / 接受 / 拒绝 / 接受 | `fdd41335dbd313fcae1cfb31`; `fdf49f187ae2b057d66600de` | 两个接受、两个拒绝；edge_truncated 框触及底边。 |
| `confuser_0001_00_00` | front/edge/recovery 中文皮卡；oblique 英文 pickup；front/edge/recovery `distractor`、oblique `proposal_distractor` | 接受 / 接受 / 拒绝 / 拒绝 | `ffe37f7a581ef2ec27ddbb99`; `da267bda83fa44a120f5d071` | 干扰物在 front、oblique 被接受，在另外两个视角被拒。 |
| `confuser_0010_00_00` | front/edge/recovery 英文 car；oblique 中文 SUV；front/edge/recovery `distractor`、oblique `proposal_distractor` | 拒绝 / 接受 / 拒绝 / 拒绝 | `e2a1d6393fce8beaff3c408d` | 干扰物仅 oblique 被接受；该视角的候选列表只有一个框。 |
| `distractor_0010_01` | front/oblique/recovery 英文 car；edge 中文 SUV；front/oblique/edge `distractor`、recovery `proposal_distractor` | 拒绝 / 拒绝 / 拒绝 / 接受 | `e133f1e6ef1f596378ed451d` | 干扰物仅 recovery 被接受为 approach_track。 |
| `distractor_0000_04` | front/oblique/recovery 中文皮卡；edge 英文 pickup；front/oblique `distractor`、edge/recovery `proposal_distractor` | 拒绝 / 拒绝 / 拒绝 / 接受 | `bdea57dfd4b4fffb697b073f` | r10 在 recovery 新增 approach_track；r3 四视角均拒绝。 |

r10 的 7 个不一致实例中，4 个按 front 行分组标签为 `distractor`，3 个为 `true_target`；同一组的 `label_type` 也可能在 `distractor` / `proposal_distractor` 或 `true_target` / `proposal_true` 间变化。前三个真目标案例表现为单个视角拒绝或 2:2 分裂；干扰物案例表现为某些视角拒绝而另一个或两个视角接受。两个模型都出现的共同偏差包括真目标的 edge_truncated / oblique 拒绝，以及干扰物在 front、oblique 或 recovery 的接受。r10 修复了 r3 的 `confuser_0000_00_00` 和 `target_true_gen_r21id_r3_w0026` 两组不一致，新增的 `distractor_0000_04` 则抵消了其中一组改善。

框几何也没有给出跨视角结果的简单阈值解释：`target_true_gen_r21id_r3_w0000` 的 edge_truncated 框面积约 0.02196 且触底，但被拒；`distractor_0010_01` 的 recovery 框面积约 0.00028，反而被接受，而同组其他三个视角的框均更大并被拒。候选框大小与误收在总体上有关联，但不足以单独解释这些逐视角反转。

## 推测与证据边界

- **推测：** 车/SUV、皮卡任务上的高误收，以及误收在少数实例中的聚集，可能反映这些任务或特定候选样本更容易与请求类别混淆。当前材料仅有 `task_text`、候选框和标签，未提供可用于本次复核的图像语义核验；不能断言具体是哪类外观相似物导致。
- **推测：** 较大的候选框和单候选框组误收率较高，可能与候选裁剪中包含的信息量或缺少同帧对比有关；这些字段只显示框几何与候选数量，不能证明模型是按该线索做出接受决定。
- **限制：** 完整四视角组中常有中英文 `task_text` 交替，且标签的 `proposal_*` / 基础形式可能随视角而变；本次观察无法把误差分离为纯视角效应、语言效应或标签子类效应。
- **限制：** 本文不根据图像路径猜测目标外观、遮挡、光照或真实尺度，也不从预测结果解释模型内部原因。结果是冻结样本的关联描述，不能外推到其他数据集或飞行场景。

## 输入完整性与执行记录

| 输入 | SHA256 |
|---|---|
| `artifacts/r21id-r3/pipeline/r15-additions/test.jsonl` | `73014f4a969df7adde79dd10d3f4ea23d39a77e18b88d6a1ccbeb63db91fb6c1` |
| `artifacts/r21id-r10/evaluation/r3-four-view/identity-report.json` | `0dc5f75b56cd1528ca60835ec9e2a3e9163cbf82b007befeae4cb1e2a172fa73` |
| `artifacts/r21id-r10/evaluation/r3-four-view/identity-report.md` | `3d077e520f1d54ef5f18772de873c00d261ba4cb7c830086698720e42d634613` |
| `artifacts/r21id-r10/evaluation/r3-four-view/r21id-r3-predictions.json` | `6ee232620931d7a1e44a6d1dff5970f9db8119547bffca7bb9d6ffceb0f6260c` |
| `artifacts/r21id-r10/evaluation/r3-four-view/r21id-r10-predictions.json` | `b202ee4fedc857239eda06c93a5b22bdca380ec2d1aa513e876d336e867a691a` |

本次只读读取并按 `sample_id` 离线汇总现有 JSONL/JSON，没有运行推理、训练、评测服务或 pytest，没有写入 `artifacts/`、生产模型或准入门槛。没有对原始图像做视觉复核。
