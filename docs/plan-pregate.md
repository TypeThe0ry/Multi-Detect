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

## 误拦与数据限制

规划 holdout 和返航探测共 241 题，预检没有误拦任何 `expect_invalid=false` 的题。误拦清单为空。v4/v5 的范围外漏拦是 `query_ungrounded`：v4 两题，v5 以下 14 题，均未被预检拦下：

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

仓库中没有 r24 配方生成的完整 5,460 行训练集（该轮数据记录的是临时目录 `.tmp/r24-data`）。因此不能声称已完成用户要求的“r24 训练集全部任务文本”检查。当前可读的 `artifacts/datasets/plan-sft-v4/train.jsonl` 是 1,500 行 replay 基集，不是 r24 全量训练集；对这 1,500 条任务文本运行同一规则，命中 0 条。推荐配置启用前，应对实际 r24 train JSONL 重跑审计并确认全部命中都属于拒绝样本。

## 建议

建议将 `planning_pregate: true` 作为推荐配置候选，保持 `planning_decision_contract: false`。holdout 上目标类别全命中、正常题零误拦，且不依赖拒绝提示词；预检仍需保持代码默认关闭。由于 r24 完整训练集暂不可读，正式切换推荐配置前先完成该训练集审计，并确保任务包提供完整的 refs/别名与有效能力集合。查询编造继续由原意图校验器负责。
