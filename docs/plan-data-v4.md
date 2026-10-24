# 下一轮规划数据与评测

本轮只准备数据和离线评测，不训练模型。`build_plan_sft.py --r20-plan-intent` 生成 18 条审计型 SFT 样本，包含中英混合的返航正例与“无需返航/就地降落”否定例、未知地点拒绝、无 `payload_drop` 拒绝、固定翼悬停和定点降落拒绝、`ref_labels` 地点别名、以及查询目标必须来自任务文本的正反配对。每条正例在生成时通过 `plan_intent` 和 `capability_contract`；负例记录 `expected_findings`，并要求对应规则命中。

`configs/onboard/plan-eval-tasks-v4.json` 是 planning-v4 留出集，共 20 题，按 `category` 分组。它采用独立的地点组合和中英文措辞，不复用训练模板；生成器默认读取全部 `plan-eval-tasks*.json`，按规范化文本排除，测试同时检查训练与留出文本无交集。拒绝题使用 `expect_invalid: true`，能力题带 `capabilities` 上下文。

评测阶段 `planning-v4` 已接入 `run_model_round_eval.py`，底层仍调用 `plan_from_task.py`。结果摘要增加 `category_metrics`：每类序列正确率、拒绝正确率、意图违规率和能力违规率；`rows` 保留每题 category 及违规计数。

建议的下一轮训练命令（不执行）：

```pwsh
$env:PYTHONPATH='src;.'; $env:PYTHONUTF8='1'
python scripts/build_plan_sft.py --output D:/Projects/mdwt/_scratch/cx-planner-data/plan-sft-r20 --version v4 --rows 1800 --val-rows 150 --r20-plan-intent --require-skills
```

期望计划可用 `plan_intent.check_plan_intent` 与 `capability_violations` 重放验证；期望拒绝题必须得到 host 拒绝、安全 `return_home` 修复或无有效计划。
