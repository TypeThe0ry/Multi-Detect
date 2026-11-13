# r31 规划候选：冻结集重评通过

2026-11-08 22:44—22:47（北京时间），在集成代码 `59aaf51e`（新增实验作业后的
代码提交 `02419ff8`）重评已有 r31 GGUF。未修改任何评测题目、提示或标签；
两模型均启用现役规划前预检，不启用 decision contract。

| 模型 | planning-v5 | 返航探测 |
|---|---:|---:|
| r24 对照 | 93/126 | 15/15 |
| r31 候选 | **97/126** | **15/15** |

r31 达到本次用户指定的 `>93/126` 且返航 `15/15` 两项准入条件。
新建 `configs/onboard/sim-loop-v3-r21idr3-r31plan-defaults.json` 供集成回归，
仅替换 `planning_model` 为已存在的 r31 GGUF；身份模型仍为 r21id-r3。
本分支没有修改现役 r24 配置。主协调者可据此在完整任务回归中验证候选。

冻结 `plan-eval-tasks-v5.json` 的 SHA256 为
`cad5d5085c1f82cf5cf37d487ffb54877e1d3b5151bd368108c947ee033af288`，
与 r31 原始训练时 worktree `runs/src-r31` 的 HEAD 文件逐字节相同。
返航 15 题的 SHA256 为
`b5ef8aa72e467663db383e92aa97421a6d5ad7319e1bf4d6d8d014a758db51ef`。
对 r31 训练 1,620 行逐行检查，与这两组题的完整文本及 ID 均零交集；
评测结束再次校验冻结文件 SHA256 一致。检查不能证明任意语义改写的独立性。

证据均位于 `artifacts/evaluation/r31-current-20261108/`：
`candidate/planning-v5.json`、`baseline/planning-v5.json`、
`probe-candidate.json`、`probe-baseline.json`、`input-audit.json`、
`admission-current.json`。独立复现命令保存在运行区 `runs/r31_recheck_20261108.sh`；
GPU 固定为物理 GPU1，并使用 `runs/eval.lock` 串行化本项目评测。

已有旧评测另有回退：planning-v3 exact sequence r31 为 0.6875、r24 为 0.8；
旧准入器还要求 v4 类别提高 10 pp，所以旧 `replacement-decision.json` 拒绝 r31。
这不是本次两条指定门槛中的条件，因此不据此自行增加准入门槛；它作为候选风险
保留，需在实际回归中观察。此次只重跑 planning-v5 和返航探测，没有重跑 v3/v4。
