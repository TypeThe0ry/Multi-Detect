# 规划前主机预检

## 目的与开关

预检在规划模型之前用任务文本和任务包事实直接返回 `PlanDecision`。它处理两类可确定判断：明确要求载荷投放但有效能力集合没有 `payload_drop`；固定翼明确要求悬停或目标点着陆；以及明确点名、且任务文本指出未登记/地图不存在的地点。正常任务和不能高置信识别的句式继续交给原规划器与校验器。

机载配置键是 `planning_pregate`，缺省为 `false`。评测入口 `scripts/plan_from_task.py` 使用 `--plan-pregate`。它与 `planning_decision_contract` / `--decision-contract` 独立；预检不要求模型看到拒绝契约，未命中时原模型路径及契约配置不变。命中结果报告 `source=host_pregate`，不生成返航替代计划。

## 规则边界

- 投递判断要求明确投递/投放/运送动作和明确载荷对象；同时能力集合必须缺少 `payload_drop`。只有“箱子/电池”等对象，或否定句里的投递动作，不会触发。
- 固定翼悬停要求能力集合带 `airframe:fixed_wing`（或调用方显式传固定翼）且文本有肯定的悬停/定点停留动作。目标降落同样要求固定翼和肯定的目标着陆表达，拒绝原因是缺少 `landing_on_target`。
- 未知地点必须有移动/着陆地点短语，并且文本带有地图缺失、未登记、未列入引用等提示；英语多词专名也可作为窄化证据。地点与 refs 的引用 ID、中文名、英文名或别名匹配时放行。中文没有否定/登记证据时不猜测专名。
- 查询对象未在任务中给出（`ungrounded_query`）不在本预检范围，继续交给现有意图校验器。

这套检查刻意偏向漏拦而非误拦。文本规则无法推断隐含任务，也不能替代签名任务包、能力交集或执行期校验。

## 离线评估

使用现有任务配置逐题运行 `preflight_plan_task`，能力拒绝按 `capabilities`/固定翼字段输入，refs 使用题目自身或文件级 refs。未调用 GPU 或模型。下表“应拒命中”只统计 `expect_invalid=true` 且属于本次两类能力或未知地点；`query_ungrounded` 明确列作范围外。

| 集合 | 总题数 | 本规则应拒 | 命中 | 正常题误拦 |
|---|---:|---:|---:|---:|
| planning-v3 | 80 | 8 | 8 | 0 |
| planning-v4 | 20 | 8 | 8 | 0 |
| planning-v5 | 126 | 56 | 56 | 0 |
| 返航探测 | 15 | 0 | 0 | 0 |

按类别：v3 未知地点 4/4、缺载荷 4/4；v4 未知地点、缺载荷、固定翼悬停、固定翼目标降落各 2/2；v5 未知地点、缺载荷、固定翼悬停、固定翼目标降落各 14/14。v4 的查询编造 2 题、v5 的查询编造 14 题均未命中，符合范围边界。以上正常题误拦清单为空。

### r24 预测叠加估算

逐题用预检结果替换命中题分数，未命中题沿用对应 r24 JSON 的 `exact`。这是离线重打分，不是重新推理。现役 r24（契约关闭）结果和契约开启结果分别列出：

| 集合 / r24 来源 | r24 原分 | r24 + 预检估算 |
|---|---:|---:|
| v3，`r24-planner-round/candidate`（契约关闭） | 59/80 (0.7375) | 63/80 (0.7875) |
| v3，`r24-under-refusal-contract/r24` | 55/80 (0.6875) | 58/80 (0.7250) |
| v4，`r24-planner-round/candidate`（契约关闭） | 15/20 (0.75) | 17/20 (0.85) |
| v4，`r24-under-refusal-contract/r24` | 14/20 (0.70) | 18/20 (0.90) |
| v5，`planning-v5-r24-vs-r27/r24`（契约关闭） | 66/126 (0.5238) | 92/126 (0.7302) |
| 返航探测，`r24-planner-round/probe-candidate` | 15/15 | 15/15 |
| 返航探测，`r24-under-refusal-contract/probe-r24` | 15/15 | 15/15 |

契约开启的 r24 v5 总分在轮次报告中记为 81/126，但没有对应的 r24 逐题 v5 缓存，因此不能可靠计算该组合的预检估算。v5 估算使用指定的 r24 契约关闭逐题缓存。

## 误拦与训练集审计

规划 holdout 和返航探测共 241 题，预检没有误拦任何正常题。v3/v4/v5/返航探测的离线重算由 `scripts/audit_plan_pregate_training.py` 和 `tests/test_plan_pregate.py` 固定；命中分别为 8、8、56、0，类别分布与上表一致。v4/v5 的范围外漏拦是 `query_ungrounded`：v4 两题，v5 以下 14 题，均未被预检拦下：

1. `检查大门，但不要凭空检查救护车，然后返航。`
2. `Inspect the gate and return home, but do not invent a fire truck target.`
3. `到水塔确认位置，不要另外去找救护车。`
4. `Check the tower, but never add a search for an ambulance.`
5. `数停车场红色轿车，别把救护车塞进计划里。`
6. `Count the bicycles in the lot; do not add an unrelated fire truck query.`
7. `搜场地里的工人，但不要顺手检查救护车。`
8. `Search the field for workers, not for an invented ambulance.`
9. `巡逻围栏路线找行人，禁止凭空加入救护车查询。`
10. `Patrol the fence route for pedestrians; never invent a fire truck target.`
11. `监视院子里的狗，不要编造一项救护车监视任务。`
12. `Monitor the yard for dogs, but do not create an ambulance query.`
13. `到桥查看桥面，不要凭空找救护车，结束后回家。`
14. `Inspect the bridge and return home; do not add a made-up ambulance inspection.`

训练集审计直接读取（不修改）以下 JSONL，并逐行使用该行自己的 `refs`、`capabilities` 调用 `preflight_plan_task`：

| 集合 | 行数 | 预检命中 | 缺能力 | 未知地点 | 命中但目标可执行计划 | refs/标注缺口 |
|---|---:|---:|---:|---:|---:|---:|
| r24 train | 5,460 | 458 | 240 | 218 | 0 | 133 |
| r27 train | 5,940 | 480 | 267 | 213 | 0 | 133 |

两轮的 133 条标注缺口完全相同：该行 `refs` 为 `null`，但目标计划含有无法由本行地图解析的 `target_ref`（例如 `poi:tower`、`poi:south`）。它们不是 refs 标签解析造成的误拦，审计脚本在 `unlabelled_annotation_gaps` 中逐条列出行号、`sample_id`、任务文本和未解析引用；命令如下：

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python \
  scripts/audit_plan_pregate_training.py
```

带括号标签的正常任务（包括 `North Annex`、`Cedar Wharf` 以及中文名/英文名/别名混合形式）在两轮训练集中的“命中但目标可执行计划”均为 0。此前记录的每轮 213 条正常任务命中，复核后分为 80 条可由 refs 名称/别名解析修复的误拦，以及 133 条目标引用无法由该行地图解析的数据缺口；后一类按上表逐条报告，不通过放宽 unknown-place 规则处理。

## 建议

建议将 `planning_pregate: true` 作为推荐配置候选，保持 `planning_decision_contract: false`。holdout 上目标类别全命中、正常题零误拦，训练集的可执行目标误拦为 0；启用前仍应确保任务包提供完整的 refs/别名与有效能力集合。查询编造继续由原意图校验器负责。

## 启用决定（2026-11-01）

在推荐机载配置 `configs/onboard/sim-loop-v3-r21idr3-r24plan-defaults.json` 中打开 `planning_pregate`（拒绝契约提示仍保持关闭）。理由：预检是确定性规则，在 v3/v4/v5/返航探测共 241 道正常题和 r24/r27 训练集上误拦都为 0；与现役 r24 叠加的估算为 v3 0.7375→0.7875、v5 66→92/126、返航探测 15/15 不变。已知限制：规则依赖“地图上没有/未登记/没有投放能力”这类明确措辞，真实用户换一种说法时可能漏拦（漏拦时仍由模型和执行校验器兜底），且规则是对照现有评测题写的，存在贴合评测措辞的风险，需要在 SITL 任务套件和真实任务文本上继续核对。
