# 导航 AI 下一轮：先让模型真正选对动作

2026-10-07。本页记录当前输入、标签和实际输出的分析、已完成的恢复／抽样／共享输入准备，以及下一轮方案。
它不是新的训练结果，也不是 Gazebo 自动飞行验收。模型仍决定动作；独立的物理限制、
飞行授权和执行检查不替模型决定路线。

## 已经训练了，但还不能导航

当前导航 LoRA 确实完成了 86 次参数更新并保存了文件。独立审核确认保存的参数与训练后
参数一致。原始训练进程最后重新加载模型时发生 CUDA 内存错误，因此其退出码仍为 1。
新独立GPU worker已精确恢复112个FP32保存张量、原输入和4个生成token结果，退出0。
原恢复supervisor的Windows启动器／worker PID失败仍退出1；新增只读进程身份检查与
独立字节审计均退出0，未删除PID门或改写旧失败。恢复这一项通过不能变成动作质量通过。
依据：[训练和保存证据](evidence/onboard-navigation-trained-adapter-20261007-r2.json)、
[独立精确恢复证据](evidence/onboard-navigation-exact-reload-20261007-r1.json)。

原始四个诊断样本都参加过训练，不是独立测试。候选模型的实际表现是：

| 检查 | 当前结果 | 含义 |
| --- | --- | --- |
| JSON 语法 | 4/4 | 四条都是可解析的裸 JSON，不再带 Markdown 代码块 |
| 严格动作契约 | 1/4 | 只有前进那条满足字段及动作规则 |
| 动作类别 | 1/4 | 左转、右转、停止都被猜成前进 |
| 原始数值／尺度 | 0/4 | 前进标签是 3 米，模型输出 10 米，误差 7 米 |

现有 `strict_json_valid` 指标不仅检查 JSON 语法，还检查动作契约，因此它是 1/4，
不是 4/4。不能通过删字段、把 yaw=0 改成 null、放宽容差或换评分口径来提高历史成绩。
左转、右转和停止实际都输出了 `translate [1,0,0]` 和 `yaw_delta_rad=0.0`，既选错动作，
又违反了平移与转向字段的互斥规则。

依据：[实际候选输出](../artifacts/models/onboard-vlm-navigation-pilot-importpath-20261007-r2/candidate-probes.json)、
[实际 parent 输出](../artifacts/models/onboard-vlm-navigation-pilot-importpath-20261007-r2/parent-probes.json)、
[严格评分代码](../scripts/train_onboard_vlm_navigation_pilot.py#L576)。

## 数据为什么不够

原始 86 条样本只有 70 条前进、6 条左转、7 条右转、3 条停止。70 条前进标签全部是
`[3,0,0]` 米；左、右转分别只有 -π/6、+π/6 两种角度。所有样本来自同一个
`env_airsim_18` 场景，只有 3 个完整任务和 3 条不同的原文 Prompt。

因此，10 米不是训练数据中混入不同尺度造成的；当前数据也无法证明模型掌握了一般的
距离推理。单个场景中重复训练得更好，不等于到了另一条街还能选对路线。

当前模型已经收到系统输出契约，并非“没有告诉它输出 JSON”。实际编码也分别检查了
生成前缀、监督文本和图像张量的一致性。本轮没有找到标签错位或未来图像进入输入的证据。
更值得优先验证的是稀少动作训练不足、输出固定字段比动作选择更容易学，以及模型是否
能仅凭两张图像分辨多段任务的进度；这些是合理待验证因素，不是已证明的唯一根因。

输入只有当前和最近过去 RGB；原图 853×480，四个探针实际处理后每图 46,080 像素，
约 288×160。分辨率是否影响建筑细节识别也尚未单独验证。

依据：[原始样本](../artifacts/training/onboard-openfly-navigation-dataset-20261007-r2/samples.jsonl)、
[数据报告](../artifacts/training/onboard-openfly-navigation-dataset-20261007-r2/report.json)、
[训练消息与目标](../src/multidetect/autonomy/onboard_navigation_supervision.py#L1148)、
[实际输入张量](../artifacts/models/onboard-vlm-navigation-pilot-importpath-20261007-r2/prepared-inputs.json)。

## 下一轮怎么改：同一个输入，让模型学会不同选择

独立恢复检查、280次均衡抽样表与新的共享actor输入基础已完成；新trainer、导航live backend
和独立训练计划仍待实施。官方Qwen3VLProcessor在CPU处理全部86个保留样本，监督投影
与可观测输入构造的identity和四个实际张量字节完全一致，native0；129个来源前后不变，
最长453token／每图46080pixels，保持4096／50176上限。独立只读核验来源、actor identity、
保存tensor记录和预算通过；未二次运行processor。它仅验证保留离线RGB，不是独立实时
捕获，也不声称新system与旧训练prefix相同；CUDA未初始化，无模型加载或推理。
[共享输入一致性证据](evidence/onboard-navigation-shared-actor-inputs-20261007-r1.json)。
没有执行280次更新，没有产生新候选模型，也没有更改原100步预算。

[均衡续训实现方案](onboard-navigation-balanced-continuation-plan-20261007.md)已保存具体接线：
新输入下重测全部86个样本的对照、从已有LoRA权重用新优化器继续、逐draw记录280次更新，
并分进程保存／重载。该页仍是方案，不是已实现的新训练器或已完成的280步训练。

1. 共享导航 actor 已实现并通过上述离线一致性检查，接下来让新trainer和导航live入口调用
   同一套消息序列化与图像处理：
   原文 Prompt、过去到当前的真实 RGB 顺序、同一个 processor、同一个处理预算。
   不把任务编号、step index、pose、未来帧或正确答案放进模型输入。
   请求身份、时钟、帧 hash 和授权由外层绑定，不要求模型补造。
2. 继续使用现有三种导航原语输出，不硬塞旧任务接口的 confidence、任务阶段或北／东位移。
   新输入指令把互斥规则说清楚：平移时 yaw 必须 null；转向时平移必须 null；停止时两者
   都必须 null；未知距离或角度可以 null；只输出一个 JSON 对象。
   不在指令里规定“前进永远 3 米”，也不给某个诊断样本专用答案。
3. 当前已保存候选的独立恢复已验证；下一轮从该 checkpoint 继续训练，保留同一新 actor
   下的 base parent 对照及训练前 checkpoint 对照。更换输入后要重新测对照，不能直接拿旧
   输入的成绩证明新输入下的训练收益。当前候选不因恢复成功就获得飞行资格。
4. 已封存四类等量曝光的训练抽样表：每类70次，共280次draw，但还没有相应参数更新。70条前进全部
   覆盖，左转、右转、停止按预先记录的 seed 循环复用。它是 **86 个独立原始样本、280 次
   训练抽样**，不是“原始数据完整单 epoch”。表内记录重复cycle和样本累计曝光；两次独立
   生成字节一致，121个来源文件重验，独立审核重新推导抽样顺序。后续optimizer完成情况须另记。
   原先 100 次更新是上一实验的软预算，新实验必须明确写出自己的预算，不伪装沿用旧计划。
   [均衡抽样准备证据](evidence/onboard-navigation-balanced-schedule-20261007-r2.json)。
5. 保持原始单步 batch、BF16 base、FP32 LoRA、GPU 内存、图像像素和 token 安全上限不变。
   更多 draw 不需要扩大单步显存，不关闭应用、不改系统内存设置、不解冻视觉层来碰运气。

如果仍限制为 100 次更新，四类各 25 次只能覆盖部分前进样本，必须称为子样本诊断。
单样本 AdamW 加 gradient clipping 时，只给 loss 乘类别权重不等价于真正均衡抽样，
优先使用明确可核对的抽样表。

## 怎样证明是改善，不是改评分

- 同一 Prompt 和原始 RGB，经训练入口及推理入口得到的文本／图像绑定，以及
  `input_ids`、`attention_mask`、`pixel_values`、`image_grid_thw` 必须逐项一致。
  改变图像字节、顺序或 actor 版本应改变身份或被拒绝。
- 保留旧严格评分，另行报告 JSON 语法、契约合格率、各类动作混淆、原始米／弧度误差和
  null 比例。格式合法不等于动作正确，动作正确不等于距离正确。
- 不只检查四个例子，应检查全部 86 个训练内样本，仍明确标为训练内重测。
  保存后恢复必须再次检查参数 hash 和相同输入的生成 token，而非只确认文件存在。
- 训练内改善后，优先增加不同场景的完整任务及独立留出集，而不是继续堆同一场景的 epoch。
  复用已有场景／任务／视觉近重复分组，不能随机拆相邻帧冒充独立测试。
  当前全 3 米的标签不能验收连续尺度能力。

依据：[已有分组方法](../scripts/build_onboard_openfly_navigation_dataset.py#L295)。

## 不能把新模型路径换进去就叫同算法

当前 live 路径仍用另一套 v2 输出、额外事实文本和预先 LANCZOS 缩放，backend 也明确
没有加载导航 adapter。新导航 runtime 尚未接入；它必须复用上述已验证的实验actor，并绑定同一个 base、adapter、
processor 和预算，不能只因模型名称相同就宣布训练／推理或 SIM／REAL 一致。
详细接线缺口见：[导航 AI 到实际飞行](onboard-navigation-execution-gap-20261007.md)。

这组数据只能监督“下一导航动作”，没有教会完整任务状态、实际执行历史、GPS 恶化、
电量不足、障碍变化、返航或报告。那些能力仍要在同一模型路线中增加真实可观测输入、
实际执行回执和对应训练／闭环数据，不能由程序固定任务表替模型决定，也不能把 STOP
偷换成找到目标、任务成功或降落。

本页没有改核心代码、模型、默认策略、parity 或产品版本。当前仍为 `0.3.0-pre.8`；
模型晋升、飞行执行／transport 许可、HIL、REAL、室外 GNSS、实物性能和最终发布均未通过。
`candidate_promoted=false`、`physical_performance_claimed=false`、`hil_real_evidence=false`、
`outdoor_gnss_evidence=false`、`final_release_allowed=false`。
