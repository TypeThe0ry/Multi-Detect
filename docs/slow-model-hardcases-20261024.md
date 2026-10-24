# SITL 慢模型身份判断难例（2026-10-24）

## 范围与方法

本次扫描 `artifacts/benchmarks` 下名称带 `20261023` 及以后日期的 benchmark，共 13 个目录、90 个 episode。脚本是只读处理器：逐个 episode 流式读取 `steps.jsonl`，只解析 `labels.onboard.tracks` 和 `labels.onboard.slow_calls`，不写入 `artifacts/`。每个可解析的慢模型调用在 `hardcases.jsonl` 中恰好一行；`kind` 表示该调用的主状态，`hardcase_types` 表示可重叠难例标签。按 `track.ground_ne` 与 truth 最近邻匹配，超过评分器默认 10 m 关联半径的调用标为 `unknown`，避免把远处物体误归因给最近 truth。

truth 角色来自对象字段和 `segmentation_labels`：`true_target`、`count_member`、`distractor`、`other`。count 任务中 `true_target/count_member` 为正；find/inspect 使用评分器的 `task_equivalent`（类别/语义词相同且颜色不冲突）；monitor/patrol 使用 onboard mission step 的 `params.query` 和评分器 `_object_matches_query`。显式 distractor 为负，其余无法确定的调用为 `expected_positive: null`，不进入难例统计。翻转定义为同一 episode 中、地面位置彼此不超过 2 m 的已知语义调用同时出现接受和拒绝。低置信正确定义为接受正样本且置信度不高于 0.70。一个调用可以同时属于“翻转”和“低置信正确”；JSONL 的 `hardcase_types` 保留所有标签，`kind` 取主标签。

运行命令：

```powershell
python scripts/mine_slow_model_hardcases.py `
  --benchmarks artifacts/benchmarks `
  --output D:/Projects/mdwt/_scratch/cx-hardcase-mining/hardcases.jsonl `
  --since 2026-10-23 --crops `
  --contact-sheet D:/Projects/mdwt/_scratch/cx-hardcase-mining/hydrant-false-reject-contact-sheet.jpg
```

产物位于 `D:/Projects/mdwt/_scratch/cx-hardcase-mining/`：`hardcases.jsonl`（2029 行）、`hardcases.summary.json`、`all-known-calls.csv`（2030 行含表头；已知语义调用 1846 行）、逐例 `crops/`，以及按类别拆分的假拒拼图。每张裁剪图在 bbox 外扩一倍宽高，并标注短边像素、truth 距离和 confidence；每张拼图最多 48 张。

## 统计

JSONL 共 2029 个调用记录，其中 1846 个 `expected_positive` 已知、183 个 unknown。至少命中一种难例的调用为 **597**；四类标签出现次数相加为 1087。由于一个调用可以同时有多个标签，下面的四类数字不是互斥调用数。任务表的“难例调用”是去重后的 JSONL 行数，后面的假拒/翻转/低置信列是标签次数。

| 难例标签 | 标签出现次数 | 占已知语义调用 |
|---|---:|---:|
| 真目标假拒（false_reject） | 125 | 6.77% |
| 同一实物判断翻转（decision_flip） | 678 | 36.73% |
| 干扰物假接受（false_accept） | 9 | 0.49% |
| 低置信正确接受（<=0.70） | 275 | 14.90% |

| 任务 | 可匹配调用 | 难例调用 | 难例比例 | 假拒 | 假接受 | 翻转 | 低置信正确 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 背包 | 266 | 106 | 39.85% | 61 | 0 | 140 | 17 |
| 橙色交通锥 | 733 | 239 | 32.61% | 41 | 0 | 328 | 99 |
| 消防栓 | 751 | 217 | 28.89% | 22 | 0 | 196 | 138 |
| 行人 | 4 | 4 | 100.00% | 0 | 4 | 0 | 0 |
| SUV | 16 | 5 | 31.25% | 0 | 0 | 0 | 5 |
| 邮筒 | 71 | 21 | 29.58% | 0 | 0 | 7 | 14 |

按统一口径，`hardcases.jsonl` 中 `hardcase_types` 含假拒的调用共 125：背包 61、消防栓 22、交通锥 41、邮筒 1。`kind` 为 `false_reject` 的记录只有 41 条，因为发生翻转的记录主状态被设置为 `decision_flip`；假拒标签计数必须读取 `hardcase_types`。消防栓假拒拼图包含全部 22 个关联样例；其余类别分别有独立拼图。原先报告的假接受数字来自旧版只写难例记录且把 `count_member` 当负样本；当前假接受仅 9 个，均为显式 distractor。

## 尺寸与距离对比

对 `all-known-calls.csv` 中 `expected_positive=true` 的调用按 scorer 的 accepted/rejected verdict 分组。假拒 125 条的 bbox 短边均值/中位数为 **15.49/13.00 px**，truth 距离均值/中位数为 **0.75/0.287 m**；正确接受 994 条的短边均值/中位数为 **18.03/14.60 px**，距离均值/中位数为 **0.25/0.195 m**。假拒短边更小、关联误差更大，支持“小目标和定位误差会增加假拒”的方向性证据，但两组范围明显重叠（假拒 10--47.1 px、正确接受 10--93.1 px），不能把距离单独当作充分原因。

按类别，消防栓假拒短边均值/中位数为 13.54/13.80 px、距离 0.22/0.142 m；背包为 12.31/11.20 px、距离 0.35/0.242 m；交通锥为 21.34/19.00 px、距离 1.65/1.422 m。CSV 保留每个调用的 bbox 宽高、短边、距离、confidence 和 expected_positive，可用于下一轮按类别做分层采样。

## 典型外观

`false-reject-contact-sheet-fire_hydrant.jpg` 是全部 22 个消防栓假拒的人工检查入口；另有 `...-backpack-1/2.jpg`、`...-traffic_cone.jpg`、`...-postbox.jpg`。原始 crop 在 `crops/` 中按 `campaign_episode_step_track` 命名。样例呈现出几种稳定模式：

* 近距离/较大框：消防栓主体颜色明显，但框经常贴近画面下沿，顶部或底座被裁切；红色框与蓝色管线、黄色设施相邻时容易出现身份不确定。
* 远距离/小框：目标只剩一个很小的红色块，背景占比高，像素高度不足以区分帽盖、立柱和相邻设施。
* 斜视和遮挡：消防栓侧面、建筑边缘或护栏遮挡时，能看到的可见比例变化很大；同一空间簇在不同航向的 crop 里外观差异明显。
* 视角翻转：失败架次中的 canonical 消防栓约在 north 8.76、east -15.02；先出现 0.98--1.00 的 `no_match`，后续换视角又出现 `match`，表明单次高置信拒绝不能作为终态。

这些图只能作为离线人工检查证据，不能反推未记录的深度或真实可见比例；距离字段来自 ground-ne 与 truth 的米制最近邻。

## r20 覆盖评估

当前 `docs/slow-system-r20-plan.md` 和 `configs/scenarios/generated-r20/index.json` 已有四组身份难例模板：

| 失败模式 | r20 现状 | 判断 |
|---|---|---|
| 小目标/远距离 | `backpack_small_oblique` 5 个 train 世界，约 32 m 斜距、19 m 高、10--13 px；`paired_confusers` 另有 16--20 px 锥桶 | 覆盖小尺寸，但主要是背包/锥桶；需加消防栓 10--20 px 分层和更宽距离分布 |
| 远距离斜视 | `fixed_wing_oblique` 5 个 train 世界，约 101 m 斜距、75 m 高、20--40 px | 覆盖固定翼车辆，不覆盖旋翼机消防栓的低角度/下沿裁切视角 |
| 遮挡/截断 | `hydrant_building_occlusion_truncation` 4 个 val 世界，建筑遮挡、画面下沿截断、18 m 高、-0.48 rad | 直接覆盖消防栓的遮挡和截断，但只有 val、单一高度/光照/干扰物，且未覆盖跨视角 verdict 翻转 |
| 特定外观/同世界干扰 | 成对 confuser 世界 6 个，消防栓配 cardboard_box；要求同帧正负配对 | 覆盖“同世界负样本”，不覆盖真实架次里的消防栓与黄色设施/蓝色管线/其它合法同类成员的实例关联问题 |

因此 r20 对“小目标、远距离、斜视、建筑遮挡/下沿截断”有几何覆盖，但真实失败信号应改为“目标假拒和跨视角翻转”，而不是此前误报的同类目标假接受。r20 仍没有直接覆盖“同一实物跨视角翻转”和“高置信拒绝后恢复”，也不能仅依赖 4 个 val 世界承担训练信号。

## r20/r21 补采建议

1. 把消防栓加入 train 的分层矩阵：相机高度 12/18/24/32 m，地面距离 12/20/30/40 m，短边 8--12、13--20、21--32 px；每层至少两个世界、两个航向。
2. 对同一 truth instance 固定位置生成 3--5 个视角序列（正俯、东北斜视、侧视、下沿裁切、部分建筑遮挡），每个序列输出同帧正例和 cardboard box/管线/护栏等拒绝框。训练记录保留 `instance_id`、可见比例、遮挡类型和视角角度。
3. 增加“拒绝后恢复”配对：第一帧高置信 `no_match`，后续帧同一空间簇出现可接受外观；标签要求模型输出可恢复状态，不能把单次拒绝永久写入身份记忆。r21 应单独统计恢复成功率和最大重试次数。
4. 扩展外观：清晨低太阳、阴天平光、轻雾、红色/锈色/褪色消防栓，以及邻近黄色设施、蓝色护栏和黑色阴影；保持世界级 held-out 组合约束，不把 test 组合回灌 train。
5. 评估门禁增加实例级指标：按 truth `target_id` 计算跨视角一致率、假拒率、假接受率和恢复率；保留 r18 干扰物拒绝率 >= 0.974 的硬门禁。仅调低 no_match 阈值不能解决“高置信拒绝后无法恢复”和错误实例关联。

本次只做离线整理和分析，没有训练、没有启动 SITL/Docker/llama-server，也没有修改 `artifacts/` 或受证据约束的配置文件。
