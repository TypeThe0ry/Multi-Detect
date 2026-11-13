# r29 规划失分分析

## 逐题归类

分析范围是 `artifacts/evaluation/r24-with-pregate/r24/planning-v5.json` 和
`planning-v3.json` 中 `source=model` 且 `exact=false` 的行。预检行不计入模型错误，也不把模型再训练成拒绝缺能力或未知地点；这里的目标都是可执行任务的忠实解析。

### planning-v5

| 类别 | 正确 / 总数 | 错误行 | 逐题错因归类 |
|---|---:|---:|---|
| `query_ungrounded` | 3 / 14 | 11 | 任务在否定子句中提到救护车、消防车等类别，模型经常仍输出有效的主任务步骤，然而该类别的期望是空步骤（expected 为空），不是“执行主任务并忽略被排除类别”。这是任务整体被限定为不得凭空追加该类查询的否定范围题。r29 用完全明确的主查询正例与明确肯定的类别对照，gold 只在否定版本中省略被排除类别。 |
| `return_negated_land` | 7 / 14 | 7 | “不返航/不回起飞点，但在当地落地”常被反转成 `return_home`；另有漏 `goto`、检查动作被 `land` 吞掉，或多余返航。否定只作用于返航，明确落地仍需保留。对照题保留相同地点与检查上下文，改为“不落地、返起飞点”。 |
| `query_grounding` | 8 / 14 | 6 | 主要是地点/对象已经明确时漏 `goto` 或 `record`，以及把 `count` 改成 `record`、`area_search` 改成全域搜索；个别输出编造地点引用并触发 `ungrounded_place`。不是缺 query 后应拒绝，而是需要按已给的对象、地点和证据动作还原步骤。 |
| `ref_alias` | 9 / 14 | 5 | `入口`/gate、库房/warehouse 等别名指向已提供 POI 时，模型把地点当成全域对象搜索、漏掉显式 `goto`，或漏掉地点上的 `inspect`。正例把别名、标准名和引用标签同时列入 refs，并保留“标准名/别名”成对对照。 |
| `return_intent` | 10 / 14 | 4 | 返航步骤之外还出现多余 `goto`、漏掉 `record`、把 `inspect` 换成 `area_search`，以及将明确的中间动作吞并。返航意图本身通常识别正确；对照重点是稳定保留前序任务动作和正确终步。 |

重点错行逐条可由原 JSON 的 `text`、`expected`、`predicted`、`raw` 字段复核。例如 `query_ungrounded` 中“监视院子里的狗，不要编造一项救护车监视任务”的 `expected=[]`；`return_negated_land` 中“到北线巡一圈找行人，不要返航，巡逻结束就地落下”被预测成 `patrol -> return_home`；`ref_alias` 中“到大门看入口标志”缺少 `goto`/`inspect` 的其中一步。无效候选如错用 `route:fence`、`zone:dock` 也计入相应类别，因为这些 refs 不在题目 refs 内。

### planning-v5 模型错误逐题清单

**query_ungrounded**
- `Inspect the gate and return home, but do not invent a fire truck target.`：expected `[]`，predicted `['inspect', 'return_home']`；否定范围内类别不得进入 query；expected 为空。
- `Check the tower, but never add a search for an ambulance.`：expected `[]`，predicted `['goto', 'return_home']`；否定范围内类别不得进入 query；expected 为空。
- `数停车场红色轿车，别把救护车塞进计划里。`：expected `[]`，predicted `['count', 'return_home']`；否定范围内类别不得进入 query；expected 为空。
- `Count the bicycles in the lot; do not add an unrelated fire truck query.`：expected `[]`，predicted `['count', 'return_home']`；否定范围内类别不得进入 query；expected 为空。
- `搜场地里的工人，但不要顺手检查救护车。`：expected `[]`，predicted `['area_search', 'return_home']`；否定范围内类别不得进入 query；expected 为空。
- `Search the field for workers, not for an invented ambulance.`：expected `[]`，predicted `['area_search', 'return_home']`；否定范围内类别不得进入 query；expected 为空。
- `巡逻围栏路线找行人，禁止凭空加入救护车查询。`：expected `[]`，predicted `['patrol', 'return_home']`；否定范围内类别不得进入 query；expected 为空。
- `Patrol the fence route for pedestrians; never invent a fire truck target.`：expected `[]`，predicted `['patrol', 'return_home']`；否定范围内类别不得进入 query；expected 为空。
- `监视院子里的狗，不要编造一项救护车监视任务。`：expected `[]`，predicted `['monitor', 'return_home']`；否定范围内类别不得进入 query；expected 为空。
- `Monitor the yard for dogs, but do not create an ambulance query.`：expected `[]`，predicted `['monitor', 'return_home']`；否定范围内类别不得进入 query；expected 为空。
- `Inspect the bridge and return home; do not add a made-up ambulance inspection.`：expected `[]`，predicted `['inspect', 'return_home']`；否定范围内类别不得进入 query；expected 为空。

**return_negated_land**
- `Inspect the tower; do not return home, just land here after the check.`：expected `['goto', 'land']`，predicted `['inspect', 'land']`；否定返航与明确落地的作用域混淆，或漏掉前序动作。
- `Fly to the bridge and inspect it; instead of coming back, touch down at the bridge.`：expected `['goto', 'inspect', 'land']`，predicted `['goto', 'land']`；否定返航与明确落地的作用域混淆，或漏掉前序动作。
- `到起降坪确认灯光，不必回到起飞点，最后就在坪上着陆。`：expected `['goto', 'inspect', 'land']`，predicted `['goto', 'land']`；否定返航与明确落地的作用域混淆，或漏掉前序动作。
- `到北线巡一圈找行人，不要返航，巡逻结束就地落下。`：expected `['patrol', 'land']`，predicted `['patrol', 'return_home']`；否定返航与明确落地的作用域混淆，或漏掉前序动作。
- `Count the boats near the dock, then land on the spot rather than returning home.`：expected `['count', 'land']`，predicted `None`；否定返航与明确落地的作用域混淆，或漏掉前序动作。
- `去围栏路线找行人，完成后不回基地，直接在脚下落地。`：expected `['patrol', 'land']`，predicted `['goto', 'area_search', 'land']`；否定返航与明确落地的作用域混淆，或漏掉前序动作。
- `Inspect the entrance sign at the gate, and land here instead of flying back.`：expected `['goto', 'inspect', 'land']`，predicted `['goto', 'land']`；否定返航与明确落地的作用域混淆，或漏掉前序动作。

**query_grounding**
- `去场地找工人，找到后留一张影像，再回到起飞点。`：expected `['area_search', 'record', 'return_home']`，predicted `None`；已命名对象、地点或证据动作漏步、技能替换，或编造地点。
- `Inspect the gate's entrance sign from three views, then head back to base.`：expected `['goto', 'inspect', 'return_home']`，predicted `['inspect', 'return_home']`；已命名对象、地点或证据动作漏步、技能替换，或编造地点。
- `把屋顶的太阳能板搜一遍，找到就记录，做完回基地。`：expected `['area_search', 'record', 'return_home']`，predicted `['area_search', 'return_home']`；已命名对象、地点或证据动作漏步、技能替换，或编造地点。
- `Survey the farm, count the tractors, and come back to the launch point.`：expected `['survey', 'count', 'return_home']`，predicted `['survey', 'record', 'return_home']`；已命名对象、地点或证据动作漏步、技能替换，或编造地点。
- `到水塔检查天线，别把它误当成车辆，随后返航。`：expected `['goto', 'inspect', 'return_home']`，predicted `['goto', 'return_home']`；已命名对象、地点或证据动作漏步、技能替换，或编造地点。
- `Check the dock for boats, keep watching for sixty seconds, and then come back.`：expected `['goto', 'monitor', 'return_home']`，predicted `['area_search', 'monitor', 'return_home']`；已命名对象、地点或证据动作漏步、技能替换，或编造地点。

**ref_alias**
- `到入口那边核对标志，然后回到home。`：expected `['goto', 'inspect', 'return_home']`，predicted `['goto', 'area_search', 'return_home']`；别名未绑定到已给 poi，导致漏 goto、全域搜索或漏 inspect。
- `在桥的位置确认桥面，别满场找车，随后回家。`：expected `['goto', 'inspect', 'return_home']`，predicted `['goto', 'return_home']`；别名未绑定到已给 poi，导致漏 goto、全域搜索或漏 inspect。
- `到大门看入口标志，回到起飞点前不用再去起降坪。`：expected `['goto', 'inspect', 'return_home']`，predicted `['goto', 'return_home']`；别名未绑定到已给 poi，导致漏 goto、全域搜索或漏 inspect。
- `Check the warehouse alias for a ladder, record it, and fly back to base.`：expected `['goto', 'record', 'return_home']`，predicted `['area_search', 'record', 'return_home']`；别名未绑定到已给 poi，导致漏 goto、全域搜索或漏 inspect。
- `Inspect the gate alias and its entrance sign, count the red cars in the lot, and return to the launch point.`：expected `['goto', 'inspect', 'count', 'return_home']`，predicted `['inspect', 'count', 'return_home']`；别名未绑定到已给 poi，导致漏 goto、全域搜索或漏 inspect。

**return_intent**
- `到水塔绕一圈确认位置，记录看到的工人，事情办完返航。`：expected `['goto', 'record', 'return_home']`，predicted `['goto', 'inspect', 'return_home']`；返航终步识别但前序 goto/record/inspect 被吞掉或替换。
- `Fly to the north route, patrol it for two loops watching for cyclists, and come back home.`：expected `['patrol', 'return_home']`，predicted `['goto', 'patrol', 'return_home']`；返航终步识别但前序 goto/record/inspect 被吞掉或替换。
- `Check the shed for a cardboard box, note its position, and make your way back home.`：expected `['area_search', 'record', 'return_home']`，predicted `['area_search', 'return_home']`；返航终步识别但前序 goto/record/inspect 被吞掉或替换。
- `去桥边确认路线，接着巡查围栏路线上的行人，完成后回家。`：expected `['goto', 'patrol', 'return_home']`，predicted `None`；返航终步识别但前序 goto/record/inspect 被吞掉或替换。

### planning-v3 模型错误逐题清单

- `先到桥头就位，农田里拖拉机一共几台帮我算清，收工落起降坪。`：expected `['goto', 'count', 'land']`，predicted `['goto', 'count', 'return_home']`；多步骤中漏掉或替换中间动作/终步。
- `停车场有个紫色行李箱，帮忙瞅瞅在哪儿，认准后留张影像作凭据。`：expected `['area_search', 'record', 'return_home']`，predicted `['area_search', 'return_home']`；多步骤中漏掉或替换中间动作/终步。
- `院子里替我寻一下断掉的树枝，定位到就存一张图，原处等25秒再回起飞点。`：expected `['area_search', 'record', 'hover', 'return_home']`，predicted `['area_search', 'record', 'return_home']`；多步骤中漏掉或替换中间动作/终步。
- `到仓库那边去，搜搜有没有绿色垃圾桶，有的话把画面存档，完事落地。`：expected `['goto', 'area_search', 'record', 'land']`，predicted `['goto', 'area_search', 'land']`；多步骤中漏掉或替换中间动作/终步。
- `先往大门去，把黄色校车找着，保持十二米间距转一圈查3个角度，然后回来。`：expected `['goto', 'area_search', 'inspect', 'return_home']`，predicted `['goto', 'area_search', 'return_home']`；多步骤中漏掉或替换中间动作/终步。
- `停车场替我寻出那辆银色皮卡，跟车走180秒，回来前存一下它的影像。`：expected `['area_search', 'follow', 'record', 'return_home']`，predicted `['area_search', 'follow', 'return_home']`；多步骤中漏掉或替换中间动作/终步。
- `道路路线替我走2圈查骑车的人，再回大门就位，最后返场。`：expected `['patrol', 'goto', 'return_home']`，predicted `['patrol', 'return_home']`；多步骤中漏掉或替换中间动作/终步。
- `先在大门就位，再观察场地上的工人六分钟，随后返场。`：expected `['goto', 'monitor', 'return_home']`，predicted `['goto', 'hover', 'return_home']`；多步骤中漏掉或替换中间动作/终步。
- `挪到仓库，原处等一分二十秒，收尾回起飞点。`：expected `['goto', 'hover', 'return_home']`，predicted `['goto', 'return_home']`；多步骤中漏掉或替换中间动作/终步。
- `屋顶航测按75%重叠做完，盯屋顶的鸽子两分钟，把它们的画面存档，随后落地。`：expected `['survey', 'monitor', 'record', 'land']`，predicted `['survey', 'monitor', 'land']`；多步骤中漏掉或替换中间动作/终步。
- `There is a striped umbrella somewhere in the yard. Locate it and keep a still image for the incident log.`：expected `['area_search', 'record', 'return_home']`，predicted `['area_search', 'return_home']`；多步骤中漏掉或替换中间动作/终步。
- `Make your way to the gate, pick out the orange delivery van somewhere in the area, and archive its image before landing.`：expected `['goto', 'area_search', 'record', 'land']`，predicted `['goto', 'area_search', 'return_home']`；多步骤中漏掉或替换中间动作/终步。
- `Locate the blue drum in the farm; the paperwork needs a saved photo. Hold position for 55 seconds after that and bring the aircraft back.`：expected `['area_search', 'record', 'hover', 'return_home']`，predicted `['area_search', 'monitor', 'return_home']`；多步骤中漏掉或替换中间动作/终步。
- `Somewhere in the parking lot is a blue scooter. Locate it, move with it for two minutes, and save its image before bringing the drone back.`：expected `['area_search', 'follow', 'record', 'return_home']`，predicted `['area_search', 'follow', 'return_home']`；多步骤中漏掉或替换中间动作/终步。
- `Save a still of the green toolbox already in view for the maintenance record, then bring it back. No search is needed.`：expected `['record', 'return_home']`，predicted `['record', 'record', 'return_home']`；多步骤中漏掉或替换中间动作/终步。
- `Collect the roof mosaic at 78% overlap, observe that roof for crows for four minutes, archive an image of the crows, and set down.`：expected `['survey', 'monitor', 'record', 'land']`，predicted `['survey', 'monitor', 'land']`；多步骤中漏掉或替换中间动作/终步。

### planning-v3

16 条错误均为 `source=model`。逐行对齐 expected 与 predicted 后，主要错因是：

- `record` 漏步：7 条任务要求定位/跟随/监视后保存证据或影像，但计划直接返航或落地；另有 1 条重复生成 `record`。
- 多步骤中间动作丢失或技能替换：漏 `hover`、`inspect`、`goto`、`follow`，以及把 `monitor` 写成 `hover`。这些题的 query、地点、时长或证据动作在任务里都有明确文本依据。
- 终步混淆：1 条把“落起降坪”写成返航；1 条把“落地”写成返航。

因此 r29 里的 query/引用/返航样本保持完整任务链：显式地点操作保留 `goto`，需要证据时保留 `record`，明确落地与返航分别用对应终步。所有新增行都是 `expected_valid=true` 的正例；没有拒绝、澄清或“缺能力”训练目标。
