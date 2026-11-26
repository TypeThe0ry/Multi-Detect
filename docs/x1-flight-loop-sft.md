# X1 连续飞行：Altair SFT 与离线评测

这套脚本只生成并回放合成数据，不改变运行时。Altair 选择工具与动作；执行侧仅准入或拒绝原提案，不替换、不修补动作。PX4 原生安全机制仍是最后一层。使用的接口是 [在途决策](x1-contingency-interface.md)、[感知工具回合](x1-perception-tools-sft.md) 和 [X1 自然语言规划](x1-nl-planning-interface.md)。

## 生成与评分

在包含 #72 的树根执行，使用 CPU Python 环境：

```bash
mkdir -p .tmp
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR="$PWD/.tmp"
python scripts/build_x1_flight_loop_sft.py --output .tmp/x1-flight-loop-sft-data
python scripts/x1_flight_loop_eval.py \
  --tasks .tmp/x1-flight-loop-sft-data/x1-flight-loop-holdout.jsonl \
  --reference-smoke --output .tmp/x1-flight-loop-sft-data/oracle-smoke.json
python scripts/x1_flight_loop_eval.py \
  --tasks .tmp/x1-flight-loop-sft-data/x1-flight-loop-holdout.jsonl \
  --predictions .tmp/model-replies.jsonl --output .tmp/model-scored.json
python -m pytest tests/test_x1_flight_loop_sft.py -q -p no:xdist
```

旧基线工作树可用 `--reference-root` 指定包含对应配置的本地快照，同时将该快照的 `src` 放在 `PYTHONPATH` 最前。需要 `configs/airframes`、`configs/onboard/x1-planner.json` 和 X1 场景/包络表；无需模型、网络、Gazebo、Docker 或飞控。

## 文件格式与划分

| 文件 | 架次 | 已准入票据 | 回合 | 内容 |
| --- | ---: | ---: | ---: | --- |
| `train.jsonl` | 168 | 720 | 1044 | 每行一个 `system/user/assistant` 监督回合 |
| `val.jsonl` | 42 | 180 | 261 | 同训练格式，独立架次 |
| `x1-flight-loop-holdout.jsonl` | 56 | 240 | 348 | 仅 `system/user`，固定顺序，无答案 |
| `sample.jsonl` | 训练子集 | 完整票据 | 200 | 用于人工审查的完整架次，不再纳入留出 |

每行的 `sample_id` 为 `request_id:call_index`，`episode_id`、`split`、`category`、`source` 是外部索引。**推理只传 `messages`**：`user.content` 解码为 `{"task": "单句任务", "request": {...}}`；`assistant.content` 是一个工具调用或一个最终决策 JSON 字符串。工具结果由生产 `ToolRunner` 发给模型；同票保留状态摘要和结果顺序。`mission_progress.recent_receipts` 只含前序票据，不含当前答案或回执。提示不包含场景类别、运动证明、预期动作或审计真值。

`manifest.json` 记录固定种子、分区计数和文件 SHA-256；`rejected-tickets.jsonl` 记录筛掉的票据，不是训练数据。架次先分区再铺平，分区使用不同地理原点、任务模板和种子。各分区共享政策及场景族结构；这不是未见故障族或异机型留出。

模型预测文件每行仅含 `{"sample_id":"…","source":"model","raw":"模型原始回复"}`；未生成回复可用 `raw:null`，算失败。评分校验固定留出的全集、顺序和原文。`--reference-smoke` 直接评分政策答案，只用于验证管线，不能当作模型成绩。不要将留出回灌训练，也不要用留出选择政策或超参数。

## 合成政策与准入

政策 `policy(request)` 只使用单次请求中的遥测、事件、进度、可用 Hold 与同票工具结果，不读场景键、包络证明、真实位置或预期标签。规则及优先级如下，数值是合成数据分界，不是飞行 SOP：

1. **正常节拍、过渡和交回飞控。** 爬升在契约中报告 `phase=takeoff`。爬升、过渡、巡航、进近正常时输出 `continue` 和 `{}`，不为节拍而动作。过渡异常但无已证新动作、已有后缀仍获准时，同样不派发新指令；PX4 若已接管，执行器拒绝 `continue`，将该票包括工具前缀整体剔出并记录 `px4_owns_or_flight_state_unknown`，不会教模型覆盖 PX4。没有把拒绝重写为其他动作。
2. **能量。** 导航剩余能量低于 40 Wh 或电量至多 25% 时先问 `battery_status`。用新鲜结果的 `remaining_wh - absolute_error_wh` 作下界；28 至不足 40 Wh 标返航；4 至不足 24 Wh、MC、距起飞点至多 12 m 且新鲜地形无冲突，标 `land-now`。工具失败不是低能量证据；既有后缀仍独立通过时标 `continue`。工具拿不到续航下界的夹具，宿主可另有用于准入既有后缀的独立下界，政策不可见也不使用。
3. **链路与风。** GCS 明确断链、心跳 ≥5 s 且遥测下界 ≥28 Wh 时标返航。FW 有已提供的盘旋点、能量 ≥40 Wh、水平风 ≥8 m/s 时先查 `terrain_ahead` 和 `route_status`。本夹具直线段点采样余量在 0–60 m 且路线非空时标 `loiter`，由独立 Hold 证明准入；轻风对应 `continue`，故障事件名本身不选动作。地形点采样不是扫掠体认证。
4. **目标与证据。** FW 可见或有丢失事件便先 `look`、`target_status`；两项新鲜且轨迹关联后再取 `route_status`。在正北飞行夹具中，标记方位偏离 ≥20° 只是可能偏离测线的线索；要求同票新鲜、误差 ≤12 m 的地理定位、匹配轨迹、巡航完成边界、至少 55 Wh 与 300 s，再提出指向 `finding:1` 的接近、盘旋观察、后过渡及 `return_home`，由真实包络基于位置绑定校验。`return_home` 实际降解为 MC 返航和终端着陆。事件票据本身不改派，后一张节拍必须重新读取定位。移动样例先在原测线上见到目标，随后丢失、偏轴重获。失败或过期值一律不可用；预算耗尽、仍丢失或证据不足时不给坐标补值，不更换路线。

工具票据走真实 `ToolRunner` 的索引、预算（最多三次，也有单次夹具）、结果失效和最终决策解码。所有纳入的决策走真实 `X1ContingencyExecutor` 和 `X1Envelope`，拒绝则丢整票。改派还走 `X1AirbornePlanAdapter`、`LiveContinueEvidence` 的实际后缀降解与扫掠验证，以及 `AirborneRouteSlot` 的安装；其他动作的 `MotionProof` 是独立的 CPU 包络夹具，并非实测。回放车端只记录命令，不连接 PX4；模拟 ACK 不等于模式或动作已读回。场景观测取材于 `configs/simulation/` 的 contingency、evidence-follow 和 native-handoff 表，改派边界使用 Iowa `ASSUMED_SIM` 假设在平坦合成 DEM 上重放，不声明实地证据。

## 如何读分

- `schema_valid_rate` 使用真实解码器，ID 错误另计 `identity_valid_rate`；正确格式不是合法动作。
- `action_accuracy_rate` 在期望最终动作回合上算，Hold 还比较引用。`executor_admission_rate` 同一分母，错误、缺失、工具回复和拒绝均不准入。
- `unnecessary_action_rate` 在安静巡航上计可解析的非 `continue` 动作提案，即使执行器拒绝也计入；额外工具另计。无效输出仍由格式和动作正确率扣分。
- `tool_use_correct_rate` 检查是否该调用、选择、ID/索引和预算；该最终决策时多问工具也算错。它不把错误动作判成正确动作。
- `retask_plan_valid_rate` 的分母是提出改派的回合，要求通过真实重放并安装；`required_retask_valid_rate` 的分母是应改派的回合，且要求确实接近、观察同一 `finding:1`，这样只输出 `continue` 或只返航不会得到满分。
- 报告含逐回合和分类指标；无分母输出 `null`。`ticket_reference_path_exact_rate` 要求同票参考路径的工具与最终动作均正确且获准。后续提示使用参考工具历史，即教师强制，不是让模型闭环决定后续输入。

## 已知限制与下一步采集

固定翼姿态、航迹接续、时间和能源包络都是 CPU 假设；样本不证明真实鲁棒性、工具端到端延迟、PX4 保底或着陆结果。新数据应保存每张票据送入后端的完整逐回合请求和原始回复、工具结果与源时间戳、取消/超时、换装边界与前后计划摘要、动作准入及 PX4 ACK/模式读回。由独立记录对齐真实姿态、航迹、感知误差、风、能量区间与着陆证据，分开标注 `synthetic_cpu`、SITL 和机载来源。用不同日期、航线、场地、任务与天气分组留出，再做闭环评测；不把事后真值写进当拍提示。
