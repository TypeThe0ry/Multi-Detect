# r21id-r10 full SFT 与 80/20 additions CPU 构建报告（2026-11-07）

本轮按 [`identity-r10-plan-20261107.md`](identity-r10-plan-20261107.md) 和 [`identity-r10-admission-20261107.md`](identity-r10-admission-20261107.md) 执行，只做 CPU 输入检查、当前 builder 重建和 additions 截取。`artifacts/` 只读；没有启动 GPU annotate、训练、模型服务、SITL 或 Docker，也没有修改训练配方、生产模型或受证据约束的配置。

## 执行命令与输入

环境为主 `.venv`，运行前设置了 `PYTHONPATH=src:.`、`PYTHONUTF8=1`、`TMPDIR=$PWD/.tmp` 和空 `CUDA_VISIBLE_DEVICES`。先运行：

```bash
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python scripts/check_identity_r10_inputs.py \
  --base-dataset artifacts/r21id-r3/pipeline/r15-sft \
  --index configs/scenarios/generated-r21id-r9r/index.json \
  --campaign artifacts/r21id-r9rb/episodes \
  --proposals artifacts/r21id-r9rb/gpu-annotate-full/proposals.jsonl
```

preflight 通过：r3 train/val/test 为 22,843/2,378/2,447 行；r9r index 为 48 worlds，其中 train 36、val 12，`car→mailbox` 和 `suv→postbox` 各为 train 18、val 6；full proposals 为 9,764 行。

当前 builder 使用 `scripts/build_slow_system_sft.py`，参数为 `--confusable-negatives --task-text-variants --crop-policy none --min-visible-fraction 0.3 --confirm-min-side-px 8 --balance-identity-views --balance-confusable-negative-views --max-upsample 1 --confusable-negative-view-cap 180`。输出写入 `.tmp/identity-r10-full-build/sft-rebuilt/`。随后用 `scripts/cap_identity_round_additions.py --addition-fraction 0.2` 从 builder train 截取 additions，输出写入 `.tmp/identity-r10-full-build/r15-additions-mix/`。

输入、脚本和输出 SHA256 见：

- [preflight.json](../.tmp/identity-r10-full-build/preflight.json)
- [input-sha256.txt](../.tmp/identity-r10-full-build/input-sha256.txt)
- [output-sha256.txt](../.tmp/identity-r10-full-build/output-sha256.txt)

## 构建和 mix 结果

builder 最终行数为 train 18,351（36 train worlds）、val 4,665（12 val worlds）、test 0。manifest 为 [manifest.json](../.tmp/identity-r10-full-build/sft-rebuilt/manifest.json)，记录 `confusable_negative_view_cap=180`、`conflicting_decision_groups=0`、`segmentation_frames=2720`、`eligible_frames=2720`、`near_duplicate_frames=6896` 和 `duplicate_rows=1459`。SUV 正例修复在 full 中可见：`suv→postbox` 四个 view 的 source positive 均有产出（manifest source count 每格 819），最终 train/val 也都有正例。

r3 train 有 22,843 行，因此 additions 上限为 `floor(22843 × 0.2 / 0.8) = 5,710`。cap 脚本输入 18,351 行，实际选取 5,710 行；mix train 只有这 5,710 行 additions，val 保留 full builder 的 4,665 行，test 为 0。若把 r3 replay 22,843 行与 additions 5,710 行合并，additions 占比为 19.9979%，由整数上限造成的舍入误差为 0.0021 个百分点。cap 输出和日志见 [cap.log](../.tmp/identity-r10-full-build/cap.log)。分层键为 `world × pair × view × label_type × decision`，没有抽取 r3 replay。

## r9r 目标 pair 的行数

下表按 r9r world 的方向（car→mailbox 或 suv→postbox）统计四个 view；`positive` 是 `label_type=confusable_positive`，`rejection` 是 `label_type=confusable_negative`，`other` 是该方向/view 下的其他最终 SFT 标签（例如 proposal、普通 distractor 或 true target）。builder 对拒绝行记录的是候选→任务的反向 `confusable_pair_type`，例如 car→mailbox world 中的 mailbox→car 仍计入该方向的 rejection。括号内为该 polarity 覆盖的独立 world 数。train 应覆盖 18 个 world，val 应覆盖 6 个 world。

| pair | view | train positive | train rejection | train other | val positive | val rejection | val other |
|---|---|---:|---:|---:|---:|---:|---:|
| car→mailbox | front | 756 (18) | 699 (18) | 1,031 (18) | 226 (6) | 81 (6) | 305 (6) |
| car→mailbox | oblique | 780 (18) | 686 (18) | 910 (18) | 259 (6) | 95 (6) | 279 (6) |
| car→mailbox | edge_truncated | 694 (18) | 662 (18) | 827 (18) | 230 (6) | 85 (6) | 291 (6) |
| car→mailbox | recovery | 1,190 (18) | 924 (18) | 1,386 (18) | 330 (6) | 118 (6) | 345 (6) |
| suv→postbox | front | 334 (18) | 611 (18) | 505 (18) | 104 (6) | 181 (6) | 151 (6) |
| suv→postbox | oblique | 467 (18) | 817 (18) | 531 (18) | 137 (6) | 169 (5) | 170 (6) |
| suv→postbox | edge_truncated | 443 (18) | 813 (18) | 656 (18) | 146 (6) | 154 (3) | 217 (6) |
| suv→postbox | recovery | 524 (18) | 924 (18) | 642 (18) | 167 (6) | 58 (0) | 217 (6) |

完整可复核 JSON 表在：

- [sft-rebuilt-train-pair-view.json](../.tmp/identity-r10-full-build/sft-rebuilt-train-pair-view.json)
- [sft-rebuilt-val-pair-view.json](../.tmp/identity-r10-full-build/sft-rebuilt-val-pair-view.json)
- [r15-additions-mix-train-pair-view.json](../.tmp/identity-r10-full-build/r15-additions-mix-train-pair-view.json)

80/20 additions train 的目标 pair/view 统计保持分层覆盖；例如 `car→mailbox` 四 view 为 767/740/685/1,083 行，`suv→postbox` 四 view 为 452/570/610/638 行。mix val 与 full builder val 完全相同，未从 val 抽样。

## 去重、冲突和 cap 解释

builder manifest 的 source-level 去重记录 1,459 行，其中 `confusable_positive→confusable_positive=233`、`confusable_positive→true_target=607`、`true_target→confusable_positive=619`；manifest 的最终 `conflicting_decision_groups` 为 0。对最终 train+val 按 `sample_id` 重新审计得到 23,016 个唯一 sample_id、0 个重复行、0 个重复组、0 个决策冲突组，结果见 [dedupe-conflicts.json](../.tmp/identity-r10-full-build/dedupe-conflicts.json)。

`180` 是 builder source generation 的每个 confusable pair/view rejection cap，manifest source_counts 中目标 pair/view 的生成 cap 均按此参数运行。最终 JSONL 还包含 task-text variants、identity/confusable view balance 等派生行，因此最终 `confusable_negative` 行数不应直接当作 source cap；本次 val 的 `suv→postbox/front` 最终 rejection 为 181，是派生行口径，不是把 source cap 改成 181。

## 入场判定

| 条件 | 结果 | 依据 |
|---|---|---|
| r3 replay 输入可读 | 通过 | preflight；train 22,843 行 |
| r9r index split | 通过 | 36 train + 12 val，两个方向各 18/6 |
| full proposals 可读 | 通过 | 9,764 JSONL 行 |
| 当前 builder full CPU 重建 | 通过 | train 18,351、val 4,665；manifest 可复核 |
| additions 上限与 80/20 | 通过 | 5,710 行上限，实际 5,710，比例 19.9979% |
| train pair/view 覆盖 | 通过 | 两个 pair 的四 view 均有 18 个 train worlds 和正/拒绝行 |
| val 正例覆盖 | 通过 | 两个 pair 的四 view 均有 6 个 val worlds 的正例 |
| val 数据四视角覆盖 | 通过 | 两个方向 × 四个 view 均有 6 个 val world；每 cell 均有正例和最终 SFT 行 |
| val rejection rate | 待评测 | 这是候选模型在 12 个 val worlds 上的推理指标；本轮没有训练或推理 |
| 每 val cell 至少 3 个未见 world | 通过（数据结构） | 每个方向/view cell 都覆盖 6 个 val world；模型误收仍待评测 |
| 模型固定集硬门 | 未评测 | 没有训练或推理，v3/v1/four-view 结果不存在 |
| false accepts 门槛 | 未评测 | 没有候选模型预测 |
| 生产替换 | NO-GO | 仅数据产物完成，r21id-r3 保持生产模型 |

因此，“每一个 admission criterion 是否通过”的答案要分两层：**CPU 数据入场条件全部通过**（输入完整性、split、full builder、四视角/12 val world 覆盖、去重冲突、180 cap 记录和 80/20 mix）；**模型准入条件尚未评测**（rejection rate、false accepts、v3/v1/four-view 硬门）。所以生产替换仍为 NO-GO，r21id-r3 保持不变；当前产物可以作为后续训练/评测 lane 的数据输入。不能把数据行数直接当作模型拒绝率。

## 测试与限制

已运行并通过：

```bash
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python -m pytest tests/test_identity_r10_admission.py -q -p no:xdist
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python scripts/run_onboard_gate.py -q -p no:warnings
```

结果分别为测试全通过、`onboard gate: PASS (171s)`。没有运行训练、模型评测或 GPU 工作；没有修改 `artifacts/`、训练 recipe、production model、release-parity、progress、schema 或 product-version 文件。

## 建议的下一个任务

1. 在不修改训练配方、生产模型、release-parity、progress、schema 或 product-version 文件的前提下，接收 `.tmp/identity-r10-full-build/` 的 CPU 数据产物，运行后续候选模型的 identity r10 评测：使用 12 个 r9r val worlds 和冻结的 r3 v3/v1/four-view 文件，报告每格 rejection rate、false accepts、sample_id/world/target_id 和输入 hash，并写入 `docs/identity-r10-eval-admission-20261107.md`；不得启动 GPU annotate 或修改生产模型。
2. 对候选模型评测结果做固定集与 r9r val 的统一准入审计：复核 v3 identity accuracy、<12 px recall、v3 distractor rejection、116 条 false accepts 及 8 个 r9r view cells，给出 go/no-go 和逐条证据；只写 `.tmp/identity-r10-eval-admission/` 与 `docs/identity-r10-eval-admission-20261107.md`，不得修改训练 recipe、生产模型或本报告。
