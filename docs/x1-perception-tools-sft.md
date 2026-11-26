# Altair SFT：X1 工具回合

沿用 `llama_decisions` 的 system/user/assistant 格式：user 是 `{"task": 原任务句子, "request": 宿主请求}`。模型仅生成一个 JSON 对象：感知工具请求或当前票据的最终行动。Altair 决定是否观察、查什么和采取什么行动；只读工具结果由宿主生成，不是 assistant 标签。执行回执不应成为模型做决定时可见的输入。

## 样本展开

每张票据是一个完整分组，允许零到三次观察。按宿主真实发给推理端的每个请求展开成一条独立 SFT 样本，不必创建新的 tokenizer 角色：

| 次序 | user 中的请求 | assistant 监督标签 |
| --- | --- | --- |
| 0 | 原任务、全量 `request`、`perception.call_index=0`、`results=[]` | 直接输出动作，或输出例如 `{"schema":"x1.contingency.tool.v1","tool":"terrain_ahead","args":{},"request_id":"sortie7:1","call_index":0}` |
| 1 | 同一任务和请求 ID，`state` 及 `state_sha256` 不变，`call_index=1`；服务端携带真实工具结果 | 可请求下一个必要的观察，或输出现有 `x1.contingency.decision.v1`（回显 `request_id/event_id/mission_id/revision`） |
| 终轮 | `results` 保持调用顺序，`age_ms` 是本次提交时的年龄；`calls_remaining=0` 时 `tools=[]` | 必须是唯一的正常动作；不能返回工具、解释、替代动作列表或多个对象 |

保留每个结果的 `source/age_ms/error`；`ok=false` 表示资料不可用，不是没有检测到目标、航段安全或电量耗尽。例如一个可训练的输入结果是：

```json
{"schema":"x1.contingency.tool.v1","tool":"battery_status","ok":false,"data":null,"source":"battery_bounded_energy","age_ms":null,"error":"bounded_energy_unavailable"}
```

## 采样与验收

- 覆盖无需工具、一次取得判断所需事实、多工具互补、调用次数耗尽、读数冲突、缺源、超时和过期。动作标签须有独立依据：不把故障名硬映射成某动作，不把过期／缺失值补成数值，也不把同一行为的模拟与实测证据混标。
- 将完整决策票据及同一 episode／任务变体分在同一 train/eval 分区。单独评分工具选择、严格 JSON、ID／索引回显、最终动作、失败时不虚构事实、调用次数和端到端延迟。无最终动作的窗口进入负例／评测集，不作为合格动作监督样本。
- 以宿主当次实际发送的请求为训练输入，不用事后最新值重建。当前 collector 回执不记录完整工具对话；采集线需显式保存逐回合请求、返回和来源时间戳，分别标注 CPU 夹具、SITL 与机载证据。本次未生成训练集、启动训练或测量真实推理延迟。
