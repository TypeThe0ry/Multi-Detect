# identity r10 CPU-only 数据入场审计（2026-11-07）

本审计判断 r10 是否具备进入后续训练/评测 lane 的输入条件。审计没有启动 SITL、Docker、GPU annotate、模型服务或训练；`artifacts/` 只读。CPU pilot 重建只作为 builder 和标签路径的预演，不能作为最终模型证据。当前生产模型仍为 **r21id-r3**。

结论是 **NO-GO：暂不进入训练/评测 lane**。r9r index 的 48-world 规划和 r3 replay 输入存在，但 full `proposals.jsonl` 只有 proposal cache，full SFT/proposals→SFT 产物尚未生成；当前可复核的 SFT 只有 8 个 train world、没有 val。因而 80/20 有效行数目标、未见 val world、每 cell 至少 3 个 val world、拒绝率和 false-accept 门槛都还没有可验收的 full 证据。

## 已核对的输入与 hash

统计和重建产物均写入 `.tmp/identity-r10-admission/`。完整路径、SHA256、pilot manifest 摘要在 [input-hashes.json](../.tmp/identity-r10-admission/input-hashes.json)。关键输入如下：

| 输入 | 状态 / SHA256 |
|---|---|
| `configs/scenarios/generated-r21id-r9r/index.json` | 48 worlds；`train=36`、`val=12`；`car→mailbox` 各 18/6，`suv→postbox` 各 18/6；SHA256 见 JSON |
| r3 `pipeline/r15-sft/train.jsonl` | 22,843 行；SHA256 `b240baf9973bedcf47ee346c2e9da4a34e60491fd16668c1025c398e6890b0fe` |
| r3 `pipeline/r15-sft/val.jsonl` | 2,378 行；SHA256 `fcbfeb6229d4054a78e1dfe4bd3f129c6dd5a335fd08db21a9ba2dfb6fd8084f` |
| r3 `pipeline/r15-sft/test.jsonl` | 2,447 行；SHA256 `7d1cc8758675a05b948caf0477f36bbcd3fa15f3d62df0a8ed451712822ed37e` |
| `gpu-annotate-full/proposals.jsonl` | 存在，9,764 JSONL rows，SHA256 见 JSON |
| `gpu-annotate-full/sft/` | **缺失** |
| 旧 pilot `gpu-annotate/sft/train.jsonl` | 4,372 行、8 worlds，SHA256 `6b4a248ac20877dc4d6a331f2cd55e51f794007acba45d7970352b85d90c3583` |

旧 pilot SFT 的 `world` 字段确实记录为 `gen_r21id_r9r_w0000` 等 8 个 train world；r3 replay 的 world 集来自旧 campaign，不能把 r3 行的 `world` 作为 r9r split 证据。r9r split 以 index 为准，同一 world 的 episode、frame 和四视角固定在同一 split。

## r3 replay + r9r additions 的 80/20 入场条件

配置要求按有效 train 行数计算 **r3 replay 80% + r9r additions 20%**，只对 additions 做 `world × pair × view × polarity` 分层截取，不抽取 r3 replay。r3 train 是 22,843 行；因此 additions 的目标上限为 `floor(22843 × .2/.8) = 5,710` 行，合并后目标为 28,553 行。这个比例尚未实际验收，因为 full builder 输出和 full mix 尚不存在。

旧 pilot 的 4,372 行不是 full additions，且没有 val/test。当前 CPU 重建输出 4,993 行，仍然是 8 个 train world；不能用它计算 full 80/20，也不能把 pilot 的 8 worlds 计入 12 个 val worlds。

## world split 与四视角 / pair 覆盖

r9r index 的规划 split 为：

| pair | train worlds | val worlds | 四视角要求 |
|---|---:|---:|---|
| `car→mailbox` | 18 | 6 | `front`, `oblique`, `edge_truncated`, `recovery` |
| `suv→postbox` | 18 | 6 | 同上 |

CPU pilot 重建命令如下，使用主 `.venv`，输入仍是旧 pilot 的 8-world train 子集和旧 proposal cache；输出只写当前 worktree 的 `.tmp`：

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python scripts/build_slow_system_sft.py \
  --campaign artifacts/r21id-r9rb/episodes \
  --index .tmp/identity-r10-admission/input/pilot-index.json \
  --output .tmp/identity-r10-admission/pilot-rebuild3 \
  --proposals artifacts/r21id-r9rb/gpu-annotate/proposals.jsonl \
  --confusable-negatives --task-text-variants --crop-policy none \
  --min-visible-fraction 0.3 --confirm-min-side-px 8 \
  --balance-identity-views --balance-confusable-negative-views \
  --max-upsample 1 --confusable-negative-view-cap 180
```

该重建的 index SHA256 为 `727ffe9c16a3ab3084a0facaa0dbf2c5278d486238d1194496fab54a3453b13e`，builder SHA256 记录在 manifest/input JSON。输出 manifest SHA256 为 `7a995a39c93ed99e4202ee7bd0b1d66a54599d616097706136a2228745df1baf`，train JSONL SHA256 为 `b825f92b925abb62646893708c5ddfa6ced67783403f043e698c525192888c2c`。

最终去重后的 pilot pair/view 行数如下；`positive`、`rejection` 是最终 JSONL 的 `confusable_pair_type` + `label_type`，不是 source generation 次数：

| pair / view | positive | rejection | total | 独立 world |
|---|---:|---:|---:|---:|
| car→mailbox / front | 44 | 68 | 112 | 4 |
| car→mailbox / oblique | 50 | 86 | 136 | 4 |
| car→mailbox / edge_truncated | 53 | 76 | 129 | 4 |
| car→mailbox / recovery | 68 | 110 | 178 | 5* |
| suv→postbox / front | 17 | 31 | 48 | 4 |
| suv→postbox / oblique | 27 | 41 | 68 | 4 |
| suv→postbox / edge_truncated | 21 | 37 | 58 | 4 |
| suv→postbox / recovery | 23 | 41 | 64 | 4 |

`*` car→mailbox recovery 的第 5 个 world 是另一方向 world 中的同帧辅助 pair 标签，不能当作 car→mailbox pilot world 计数。按方向主 world，pilot 每个方向仍只有 4 个独立 world。旧 pilot 的 suv→postbox 四视角 positive 为 0/0/0/0；当前 builder 已恢复为 17/27/21/23，但这仍只证明 pilot builder 路径，不证明 full val。

## source_counts、去重、geometry/proposal 代理和 cap

当前 pilot manifest 的关键统计：

- `segmentation_frames=499`，`eligible_frames=499`；`near_duplicate_frames=1006`。
- `duplicate_rows=391`，其中 `confusable_positive→confusable_positive=124`、`confusable_positive→true_target=133`、`true_target→confusable_positive=134`。最终 train 是 4,993 行，不能把 327/150 等 positive source generation 数直接当作最终 pair-tag 行数。
- `proposal_true=284`、`proposal_distractor=485`、`proposal_background=30`、`proposal_scenery=1`、`proposal_ambiguous_box=25` 是 proposal 分支统计；它们不是 geometry-qualified RGB 数，也不是最终 confusable pair 行数。
- source generation 记录 `car→mailbox` positive 327、`suv→postbox` positive 150；`suv→postbox` 的四视角 rejection source counts 为 31/41/37/41。最终 JSONL 的 SUV pair-tag positive 为 17/27/21/23，差异来自同帧重复配对、普通 `true_target` 合并和最终去重。
- manifest 写入 `confusable_negative_view_cap=180`。本 pilot 每个目标 pair/view 的 rejection 都低于 180，因此 cap 参数已实际生效但没有达到上限；旧 pilot manifest 的 cap 是 30，旧 30 行不能作为本轮 cap 证据。

因此必须区分三层口径：geometry/segmentation 是可见性代理；proposal cache 是 detector-like proposal 代理；最终 SFT JSONL 才是模型输入。任何一层都不能单独声称 full rejection 或模型 false-accept 已通过。

## full 产物与 queue fail-closed

已确认：

- `artifacts/r21id-r9rb/gpu-annotate-full/proposals.jsonl` 存在并可读，但 `artifacts/r21id-r9rb/gpu-annotate-full/sft/` 不存在。
- queue 原先只定义了命令，`DRY_RUN=1` 时即使 full proposals 或 builder 输出缺失，也会打印后续 mix/merge/train 命令并以 0 退出。这会把“可打印命令”误显示成“可训练输入已入场”。
- 新增 [`scripts/check_identity_r10_inputs.py`](../scripts/check_identity_r10_inputs.py)，只做 CPU preflight：r3 train/val/test JSONL 非空且可解析、r9r campaign/index/proposals 存在且可读、index 必须是 36 train + 12 val，并验证两个方向各 18 train + 6 val；缺失或空文件直接以 `admission rejected` 退出。
- [`scripts/queue_r21id_round_r10.sh`](../scripts/queue_r21id_round_r10.sh) 在任何 builder/mix/merge/train 命令前调用 preflight。它没有修改训练配方，也没有在 `DRY_RUN=1` 下执行生成或训练。

queue dry-run 复现：

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
DRY_RUN=1 OUT=.tmp/identity-r10-admission/queue-dry-run \
  bash scripts/queue_r21id_round_r10.sh \
  > .tmp/identity-r10-admission/queue-dry-run.log 2>&1
```

当前 dry-run 通过 preflight 后只打印命令，日志和输入统计在 `.tmp/identity-r10-admission/queue-dry-run.log`；没有执行 builder、mix、merge 或训练。`tests/test_identity_r10_admission.py` 覆盖完整输入通过、缺失 proposals 拒绝、错误 split 拒绝。

## 硬门表与 go/no-go

| 硬门 | 目标 | 当前证据 | 状态 |
|---|---|---|---|
| v3 基线对照文件 | r3 v3 eval、v1 eval、四视角 test 文件完整可读 | r3 pipeline 与配置已声明；本审计未运行模型评测 | **待评测** |
| r3 replay | 完整 replay，作为 80% 分母 | 22,843 train rows，hash 已记录 | **输入具备** |
| r9r world split | 36 train / 12 val；方向各 18/6；world 不跨 split | index 通过 CPU preflight | **结构通过** |
| r9r additions 20% | full builder 后 additions ≤5,710，合并有效行达到 80/20 | full SFT 缺失；仅有 8-world pilot 4,993 rows | **不通过 / 缺证据** |
| 四视角 pair coverage | 两 pair × 四 view 均有最终行；每 cell 分别报告 | pilot 有行；full val 无行 | **full 不通过** |
| r9r val cell worlds | 每 cell 至少 3 个未见 val world | 当前 val JSONL 为空；pilot 全是 train | **不通过** |
| rejection rate | 每 r9r val cell ≥0.90 | 没有 full val prediction/eval | **待评测** |
| false accepts | v3 ≤2/116；每 r9r val cell ≤1，首选 0 | 没有候选模型评测结果 | **待评测** |
| rejection cap | 每 confusable pair/view 上限 180 | pilot manifest 参数为 180，实际各 cell低于 180 | **pilot 结构通过；full 待验** |
| full proposals/SFT | full proposal cache、builder SFT、manifest、mix manifest | proposals 存在；full SFT 和 mix 缺失 | **不通过** |
| 生产替换 | 只有固定集和 r9r 未见 world 模型门都通过才替换 | 当前无训练/评测，生产仍 r3 | **NO-GO** |

本报告没有声称 r10 已训练、已替换生产或通过模型硬门。进入训练前仍需另一 lane 完成 full r9r annotate/proposals→builder SFT、val-only world 审计、80/20 mix manifest，以及训练后的 v3/v1/four-view/r9r val 评测；这些产物完成前保持 r21id-r3。

## 建议的下一个任务

1. 在不修改训练配方、release-parity、progress、schema 或 product-version 文件的前提下，完成 r9r full proposals→CPU builder SFT lane：使用 `configs/scenarios/generated-r21id-r9r/index.json` 的 48-world split、当前 `scripts/build_slow_system_sft.py`、每 pair/view rejection cap=180；把 manifest、pair/view/world 统计和 80/20 additions mix 写入 `.tmp/identity-r10-full-build/`，并提交 `docs/identity-r10-full-build-20261107.md`，不得启动训练或 GPU annotate。
2. 在 full SFT/mix 产物就绪后，运行 CPU-only admission/evaluation lane：只使用 12 个 r9r val worlds 和冻结 r3 v3/v1/four-view 文件，逐 cell 报告独立 world 数、四视角行数、rejection rate、false accepts 与输入 hash；不得修改生产模型或训练配方，把结果写入 `docs/identity-r10-eval-admission-20261107.md`。
