# r21id-r10 训练作业（2026-11-07）

作业入口是 [`.tmp/jobs/r10-train.sh`](../.tmp/jobs/r10-train.sh)。它把已经通过 CPU admission 的 `/data/projects/Multi-Detect-env/mdwt/cx-identity-r10-full-build/.tmp/identity-r10-full-build/` 复制到主仓库 `artifacts/r21id-r10/data/`，然后把这两个预构建目录传给 `scripts/queue_r21id_round_r10.sh`：

本次仅准备作业并运行 `DRY_RUN=1` 命令检查；没有复制数据到共享 `artifacts/`、启动训练或运行 GPU 评测。

- `data/sft-rebuilt/`：full builder 的 manifest、train 和 val；
- `data/r15-additions-mix/`：80/20 additions 的 train、val、test。

作业会拒绝覆盖已有 `artifacts/r21id-r10/data/`。机器重启后使用 `RESUME=1 DRY_RUN=0` 重新进入同一个作业；训练器和 `run_r15_round.py` 会依据 `artifacts/r21id-r10/pipeline/adapter/checkpoint/state.json` 自动恢复，阶段状态和输出哈希也会复用。训练及评估只暴露物理 GPU 1：`CUDA_VISIBLE_DEVICES=1`；绝不使用 2 或 3。所有慢模型 Python 调用均使用 `/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python`。

作业顺序为：预构建数据校验与复制、r10 queue 的 merge/train、export、v1/v3 eval、r10 report，最后在 r3 冻结四视角 test 和 r9r val 上分别生成 r3/r10 成对预测，并写入 `artifacts/r21id-r10/evaluation/{r3-four-view,r9r-val}/`。重启后传 `RESUME=1` 会保留并续用已复制数据与 merged SFT，跳过合并后从记录的 train 状态恢复。最后一行固定为 `=== r10 ALL_DONE`。

预计耗时约 **4–8 小时**：训练约 1.5–3 小时，GGUF export 约 1–3 小时，v1/v3、r3 四视角和 r9r val 推理约 1–2 小时，merge、报告和启动/IO 留有余量。作业只允许同一阶段串行运行；评估阶段用 `/data/projects/Multi-Detect-env/runs/eval.lock` 与其他评估互斥。

结束后先阅读：

1. `artifacts/r21id-r10/pipeline/r15-report.md`、`eval-v1.json`、`eval-v3.json`；
2. `artifacts/r21id-r10/evaluation/r3-four-view/identity-report.md`、`artifacts/r21id-r10/evaluation/r9r-val/identity-report.md` 和各自两份 predictions；
3. `artifacts/r21id-r10/pipeline/round-state.json`、adapter checkpoint state、merged GGUF；
4. 本轮数据来源与 hash：`artifacts/r21id-r10/data/`，对照 `.tmp/identity-r10-full-build/output-sha256.txt`。

相对 `r21id-r3` 的验收 bar 固定为：v3 identity accuracy ≥ 0.9185；`<12 px` 真目标 recall ≥ 0.6216（首选 0.6757）；v3 distractor rejection ≥ 0.9828；116 条显式干扰物最多 2 条 false accepts，且逐条报告 task、候选类别、尺寸档、world、target_id、sample_id。四视角报告必须与 r3 使用同一 `r15-additions/test.jsonl`，同时列出真目标拒绝、干扰物误收、跨视角一致性及对应样本。r9r 新增的 `car→mailbox`、`suv→postbox` 四个 view 分格还需至少 3 个未见 val world、rejection rate ≥ 0.90、每格 false accepts ≤ 1，并报告行数和独立 world 数；缺失这些证据时只能标记 evidence gap。任何门槛不满足都保持生产模型 `r21id-r3`，不改生产模型选择。

## 建议的下一个任务

1. 读取 `artifacts/r21id-r10/pipeline/eval-v1.json`、`eval-v3.json`、`r15-report.md` 和 `artifacts/r21id-r10/evaluation/r3-four-view/identity-report.json`，按本文件验收 bar 对照 `r21id-r3`，补齐 false-accept 的逐样本/world/target_id 证据，并写 `docs/identity-r10-eval-admission-20261107.md`；不得修改训练 recipe、生产模型或受证据约束配置。
2. 使用 `artifacts/r21id-r10/evaluation/r9r-val/` 中 r3/r10 成对预测、`artifacts/r21id-r10/data/r15-additions-mix/val.jsonl` 和 `configs/scenarios/generated-r21id-r9r/index.json`，按 pair×view 审计独立 world、rejection rate、false accepts 与输入 hash，写 `docs/identity-r10-r9r-val-audit-20261107.md`；不得重跑训练、修改 `artifacts/r21id-r10/data/` 或改变生产模型选择。
