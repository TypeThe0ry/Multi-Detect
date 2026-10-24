# 下一轮规划数据与评测

本轮只准备数据和离线评测，不训练模型。`build_plan_sft.py --r20-plan-intent` 是可扩展的审计型 SFT 生成器，`--r20-plan-intent-count N` 表示每个类别生成 N 个正负 pair。默认 N=40，当前 7 个类别共 560 条（每类 80 条，正负各 40 条）；例如 `--r20-plan-intent-count 60` 生成 840 条。类别包含中英混合的返航正例与“无需返航/就地降落”配对、未知地点拒绝、无 `payload_drop` 拒绝、固定翼悬停和定点降落拒绝、`ref_labels` 地点别名、以及查询目标必须来自任务文本的正反配对。每条正例在生成时通过 `plan_intent` 和 `capability_contract`；负例记录 `expected_findings`，并要求对应规则命中。固定 seed 下模板、地点、语言和 sortie 标记可复现，规范化文本仍排除全部评测题。

`configs/onboard/plan-eval-tasks-v4.json` 是 planning-v4 留出集，共 20 题，按 `category` 分组。它采用独立的地点组合和中英文措辞，不复用训练模板；生成器默认读取全部 `plan-eval-tasks*.json`，按规范化文本排除，测试同时检查训练与留出文本无交集。拒绝题使用 `expect_invalid: true`，能力题带 `capabilities` 上下文。

评测阶段 `planning-v4` 已接入 `run_model_round_eval.py`，底层仍调用 `plan_from_task.py`。结果摘要增加 `category_metrics`：每类序列正确率、拒绝正确率、意图违规率和能力违规率；`rows` 保留每题 category 及违规计数。

建议的下一轮训练命令（不执行）：

```pwsh
$env:PYTHONPATH='src;.'; $env:PYTHONUTF8='1'
python scripts/build_plan_sft.py --output D:/Projects/mdwt/_scratch/cx-planner-data/plan-sft-r20-special --version v4 --r20-plan-intent --r20-plan-intent-count 40 --require-skills
```

要直接合并常规 v4 任务，可加 `--r20-plan-intent-mix --rows 1800 --val-rows 150`；此时专项行先进入训练 split，常规行继续按原生成器补齐，manifest 会同时记录 `v4_backbone` 和各专项 family。

合并主 SFT 数据时建议把专项样本控制在训练集的 20% 左右：以常规 v4/v5 约 2,000 条为基准，使用 560 条专项样本时取 300--450 条（每类保持相同比例），其余保留常规任务；验证集单独保留专项的 20% pair，不与常规验证混排。若提高到 N=60，应先把常规主集扩到约 4,000 条，仍保持专项不超过 20%，避免模型只学会拒绝而损失常规技能序列能力。

期望计划可用 `plan_intent.check_plan_intent` 与 `capability_violations` 重放验证；期望拒绝题必须得到 host 拒绝、安全 `return_home` 修复或无有效计划。
