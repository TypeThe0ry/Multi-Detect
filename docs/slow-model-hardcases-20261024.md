# SITL 慢模型身份判断难例（2026-10-24）

## 范围与方法

本次扫描 `artifacts/benchmarks` 下名称带 `20261023` 及以后日期的 benchmark，共 13 个目录、90 个 episode。脚本是只读处理器：逐个 episode 流式读取 `steps.jsonl`，只解析 `labels.onboard.tracks` 和 `labels.onboard.slow_calls`，不写入 `artifacts/`。每次慢模型调用按 `track.ground_ne` 与 episode 对应 truth 的目标位置做最近邻匹配；任务文本与 truth 的 `true_target/role` 共同决定该邻近物是否为任务目标。输出记录保留架次、步号、track、bbox、判断、置信度、truth 类别、距离和 RGB 帧路径。

翻转定义为同一 episode 中、地面位置彼此不超过 2 m 的调用同时出现接受和拒绝。低置信正确定义为接受目标且置信度不高于 0.70。一个调用可以同时属于“翻转”和“低置信正确”；JSONL 的 `hardcase_types` 保留所有标签，`kind` 取主标签。可匹配 truth 的调用作为比例分母；无 truth 的 episode 单独计数，本次为 0。

运行命令：

```powershell
python scripts/mine_slow_model_hardcases.py `
  --benchmarks artifacts/benchmarks `
  --output D:/Projects/mdwt/_scratch/cx-hardcase-mining/hardcases.jsonl `
  --since 2026-10-23 --crops `
  --contact-sheet D:/Projects/mdwt/_scratch/cx-hardcase-mining/hydrant-false-reject-contact-sheet.jpg
```

产物位于 `D:/Projects/mdwt/_scratch/cx-hardcase-mining/`：`hardcases.jsonl`、`hardcases.summary.json`、逐例 `crops/`，以及只含消防栓假拒的 `hydrant-false-reject-contact-sheet.jpg`。裁剪图通过 RGB 路径和 bbox 逐张读取、写出，未一次性装载图片。

## 统计

总计 1995 个可匹配慢模型调用，至少命中一种难例的调用 854 个，占 42.81%。四类主标签计数如下；类别会因翻转/低置信重叠而不互斥。

| 难例 | 数量 | 占可匹配调用 |
|---|---:|---:|
| 真目标假拒（false_reject） | 49 | 2.46% |
| 同一实物判断翻转（decision_flip） | 342 | 17.14% |
| 干扰物假接受（false_accept） | 361 | 18.10% |
| 低置信正确接受（<=0.70） | 102 | 5.11% |

| 任务 | 可匹配调用 | 难例调用 | 难例比例 | 假拒 | 假接受 | 翻转 | 低置信正确 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 背包 | 324 | 147 | 45.37% | 45 | 31 | 70 | 1 |
| 橙色交通锥 | 710 | 315 | 44.37% | 3 | 122 | 165 | 25 |
| 消防栓 | 870 | 362 | 41.61% | 1 | 204 | 100 | 57 |
| 行人 | 4 | 4 | 100.00% | 0 | 4 | 0 | 0 |
| SUV | 16 | 5 | 31.25% | 0 | 0 | 0 | 5 |
| 邮筒 | 71 | 21 | 29.58% | 0 | 0 | 7 | 14 |

按最近邻类别汇总，消防栓有 204 次假接受、100 次翻转、57 次低置信正确和 1 次假拒；背包假拒 45 次，交通锥假接受/翻转分别为 122/165。消防栓假拒的绝对数量低于背景报告中的三架 hydrant10 失败，是因为这份扫描把所有 10/23 以来架次合并，并把同一物体后续翻转单独标为翻转；不能用全量假拒数替代那三架的故障归因。

## 典型外观

`hydrant-false-reject-contact-sheet.jpg` 是消防栓假拒的人工检查入口，原始 crop 在 `crops/` 中按 `campaign_episode_step_track` 命名。样例呈现出几种稳定模式：

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

因此 r20 对“小目标、远距离、斜视、建筑遮挡/下沿截断”有几何覆盖，但对本次最突出的“同一实物跨视角翻转”和“高置信假拒后恢复”没有直接数据契约。r20 也不能仅依赖 4 个 val 世界承担训练信号。

## r20/r21 补采建议

1. 把消防栓加入 train 的分层矩阵：相机高度 12/18/24/32 m，地面距离 12/20/30/40 m，短边 8--12、13--20、21--32 px；每层至少两个世界、两个航向。
2. 对同一 truth instance 固定位置生成 3--5 个视角序列（正俯、东北斜视、侧视、下沿裁切、部分建筑遮挡），每个序列输出同帧正例和 cardboard box/管线/护栏等拒绝框。训练记录保留 `instance_id`、可见比例、遮挡类型和视角角度。
3. 增加“拒绝后恢复”配对：第一帧高置信 `no_match`，后续帧同一空间簇出现可接受外观；标签要求模型输出可恢复状态，不能把单次拒绝永久写入身份记忆。r21 应单独统计恢复成功率和最大重试次数。
4. 扩展外观：清晨低太阳、阴天平光、轻雾、红色/锈色/褪色消防栓，以及邻近黄色设施、蓝色护栏和黑色阴影；保持世界级 held-out 组合约束，不把 test 组合回灌 train。
5. 评估门禁增加实例级指标：按 truth `target_id` 计算跨视角一致率、假拒率、假接受率和恢复率；保留 r18 干扰物拒绝率 >= 0.974 的硬门禁。仅调低 no_match 阈值不能解决“高置信拒绝后无法恢复”和错误实例关联。

本次只做离线整理和分析，没有训练、没有启动 SITL/Docker/llama-server，也没有修改 `artifacts/` 或受证据约束的配置文件。
