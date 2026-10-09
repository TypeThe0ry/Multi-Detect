# 导航训练输入恢复：先验证图像，再进入训练

## 本轮继续推进

最新07:30：完整267训练及fresh saved adapter reload均已实际native0，v2终态审计与独立
复核通过，已封存[完整终态](evidence/onboard-navigation-recovery267-terminal-20261008-r2.json)。
候选／重载86条raw、token、actor、prefix和诊断逐条相同；动作仍27／86，不能晋升。
NEW59跨场景开发评估及分析已真实native0，独立复算通过；动作16／59、数值0、STOP0／2，
质量未过，[实际成绩记录](evidence/onboard-navigation-original59-actual-evaluation-20261008-r1.json)已封存。下方“重载运行中／
59未运行”是历史阶段观察，[新的状态说明](onboard-navigation-terminal-and-dev-evaluation-20261008.md)
覆盖当前状态，不改旧sealed记录。

最新状态：完整训练phase已实际native0，267四阶段完成、累计280、旧进程退出，candidate86
与闭合report保存并独立复算；[新的完整训练记录](evidence/onboard-navigation-recovery267-train-closed-20261008-r1.json)
取代下方历史“candidate86仍运行”状态，旧证据不改写。格式69→86，但动作55→27／86、
数值0→8、STOP仍0／3；86图仅同scene的3条prompt，尚不能证明会导航。独立fresh reload
新进程正在运行，完整v2终态审核和59条跨场景开发模型结果尚未完成，不晋升默认模型。
下一轮先扩充真实多场景方向／停止覆盖，并比较同底座的动作语义学习目标，不盲加重复更新。

新的[逐类结果分析](onboard-navigation-result-interpretation-20261008.md)已通过56项作者／root
软件测试、Ruff和独立审查。实际59评估闭合后从raw／gold重算每类／macro／稀少动作与
metriccoverage，预算内成对比较不让cap或超时输出算成功。固定前进参照54／59只是数学
诊断，不是模型结果；当前实际59缺失时CLI拒绝，没有分析目录。冻结评估／训练入口未改。

实际267个更新已全部完成，最终模型保存；[最终更新快照的独立检查](evidence/onboard-navigation-recovery-v2-checkpoint267-20261008-r1.json)
核112F32原始参数摘要与1068行immutable prefix，267四计数／累计旧13＋新267＝280通过，
四核心保存文件前后SHA相同。进程仍继续candidate86，没有终态报告，尚无fresh reload或
完整结束来源检查／质量结论。PEFT真实目录另有README，r1审计exact4 fixture遗漏已由
[新v2软件入口](evidence/onboard-navigation-recovery267-terminal-audit-software-20261008-r2.json)
修复，作者／root50项测试、Ruff及独立审查通过；README明确绑定和末次重验，不忽略未知
extra。实际active拒绝、不读live WAL；旧失败／已封存r1保留，不以软件或部分snapshot
通过代替完整terminal通过。

完整267训练／重载的[终态审计工具](evidence/onboard-navigation-recovery267-terminal-audit-software-20261008-r1.json)
已通过50项软件全回归、Ruff和独立审查；活动锁存在时实际CLI拒绝，不读live WAL。
只有真实phase终态之后才核全部快照／日志／FP32保存参数及四组86原始输出，不以文件audit
替代实际原生退出和进程记录；当前仍未完成这项真实终态验收。

另已完成新的[59条跨场景离线评估入口](onboard-navigation-original59-evaluation-20261008.md)：
83项作者／主智能体软件测试与独立审查通过，完整raw先保存后评分，budget eligible分栏。
缺少完整train＋fresh reload真实回执时，实际CLI拒绝、没有评估输出目录；不是实际59条
模型结果。训练继续完成最终候选与重载后，才能加载它走这条新评估路径。

新的82帧跨场景[开发样本已实际生成](evidence/openfly-original82-supervision-20261008-r1.json)：
转换原生退出0、62项测试和独立终态审核通过，23／17／42固定角色，79 successor＋3 STOP。
201仓库来源前后复核及正常source session退出通过，六实际图像库入口绑定；独立复查82个
typed样本／202小输入SHA，5大文件当前仅stat，没有第二次像素identity重解码。
仅原任务／当前和最近过去RGB进入Actor；姿态／gold／future留在监督侧。新样本与旧86分开，
尚不授予训练准入，不把开发划分当官方unseen，也不证明模型会升降或自主飞行。

实际训练已恢复：独立v2通过100项软件测试，真实旧loader报告与小header验证也通过。
新v2计划／CPU准入原生退出0，GPU已完成至少8次更新并保存候选；标准库独立重算
112个FP32张量的原始字节摘要和32行阶段日志前缀，确认保存的是实际变化的参数。
[首个checkpoint证据](evidence/onboard-navigation-recovery-v2-checkpoint008-20261008-r1.json)
只封存8步里程碑；训练仍运行，尚未证明完整267、candidate86、fresh reload或质量提升。

完整CPU86入口通过32项软件测试，实际全86条验收也已原生退出0：86会话verified、196来源
前后相同、178观察，保存的完整metadata与旧表逐字节一致；独立只读审核通过。
[CPU86结果](evidence/onboard-navigation-closed-cpu86-20261008-r1.json)只验输入，不证明训练／质量。

新的267次训练r1入口通过58项软件测试并完成真实来源计划／admit-cpu；GPU阶段加载模型后，
constructor报告没有pid而验收代码访问该字段，实际KeyError退出1，尚未进行反向预检或optimizer。
[失败原样保留](evidence/onboard-navigation-recovery-loader-failure-20261008-r1.json)。原测试没有
覆盖真实loader报告，这是回归缺口；独立v2已修正该schema门并通过实际报告正负例回归，
实际GPU训练已恢复。r1失败状态不因v2继续推进而改成成功。

训练仍从旧已保存的warm检查点开始，用新的optimizer；每8次完成更新保存独立adapter及
不可变日志前缀。模型文件不等于精确续跑状态，不能宣称optimizer、scheduler或随机数状态
已恢复。旧失败消耗13次、r1字段检查失败消耗0次分别保留；正在运行的v2更新另有日志，
不能把r1的零更新数误读为当前v2状态，也不能把部分更新写成完整训练成功。

并行完成的[学习型动作头实验方案](onboard-navigation-learned-head-experiment-20261008.md)
研究如何在同一视觉语言底座上直接预测行动，减少逐token文本生成；目前仅是已核实接口的方案，
不是已实现的head或实测加速，不改变这次冻结训练和默认模型。

另一次实际三维数据检查已通过：[82原图几何与视觉检查](evidence/openfly-original82-geometry-visual-audit-20261008-r1.json)。
79个相邻姿态／动作与3个终末STOP核验，三条新路线和旧场景按现有重复规则没有匹配边；
这只是形成新数据的前置检查。新数据仍主要为3米前进与30°转向，没有左转，仅各1升降，
没有形成已验收的跨场景训练样本或独立质量结果。

## 这次解决什么

上一轮导航模型完成底座与旧 LoRA 的各 86 条推理对照，正式续训在第 13 次更新后失败。
准备第 14 条输入时，来源校验读取文件块触发主机 `MemoryError`；没有保存新模型，也没有
证据表明这是 GPU 显存不足。原始失败和已完成的基线分别保留，不改成成功。

这一轮先解决输入处理的生命周期问题：每次只处理一个真实样本，把任务文本、历史图像和
动作标签的来源验证放进同一个会话；图像用完就关闭，整个会话正常完成最终来源检查后，
才允许结果进入训练。输入的含义、图像内容、标签和 tokenizer 输出不能因修复而变化。

## 已实现的改变

- 新增独立的单样本编码入口，不改已经执行过的训练代码、计划或回执。
- 原 actor 和标签 serializer 共用一个单样本验证会话，不跨 optimizer 更新保留会话。
- 测试发现旧标签 serializer 丢弃验证图像的返回值，却没有显式关闭；新会话会立即关闭这些
  仅供来源验证的图像。实际送进 processor 的图像仍由原编码器独立创建并关闭。
- 最终来源验证失败、异常被 context 抑制，或完整输入 metadata 与已验收的 CPU 记录不同时，
  都不能把临时结果交给训练。没有通过降低来源要求绕过错误。
- 新增 Windows 只读内存采样，分别记录当前进程驻留内存、private commit 和主机可用内存。
  OS 峰值是进程整个生命周期的峰值，不冒充某个编码阶段的峰值。

新入口 50 项测试与内存采样 10 项测试已通过；其中包含真实临时来源文件和 PIL 图像，
processor/tensor 部分使用测试替身。这是软件生命周期验证，不是官方模型训练成功。

## 已完成的实际检查

用原失败的第 14 条样本，加上原计划预先指定的前进、左转、右转、停止四条样本，进行有界
CPU 对比。使用本地原官方 processor，同一个样本分别走旧路径和新路径，比较完整四类输入
tensor 的摘要、actor 身份、标签摘要、token 数和 response-only mask。

记录编码和会话边界的真实耗时、内存快照以及来源前后摘要。只在 CPU 上准备输入，不加载
模型、不初始化 CUDA、不执行 optimizer。固定旧→新顺序会受到文件缓存和 warm-up 的影响，
不能把这一次对比写成公平性能基准，也不能回溯证明旧 `MemoryError` 的根因。

实际 processor 对比已完成，原生退出 0：5 个不同样本各走两条路径，共 10 次编码、5 组配对。
完整 metadata 都与原官方 CPU 记录相同，新路径的 5 次会话最终验证通过；192 项来源前后相同。
运行保留 93 次观察，CUDA 从未初始化，未加载模型或创建 optimizer。进程及其 venv 启动器
已正常退出。独立只读审核又核对了保存的 metadata、计时嵌套、资源范围和小文件摘要，
没有重新编码图像或重新散列全部大来源文件。

本批次旧路径平均 32.58 秒，新路径平均 16.74 秒；这是完整 CPU 编码及来源校验的计时，
不是 VLM 推理或相机到飞行执行的延迟。固定旧→新且每条路径只有一次运行，不将差异写成
普遍性能提升。OS 记录的整个进程生命周期峰值 working set 为 786,944,000 字节，commit 为
2,330,398,720 字节；不能把二者相加、称为显存，或当成每个阶段的独立峰值。

[实际诊断证据](evidence/onboard-navigation-closed-sample-cpu-profile-20261008-r1.json)
绑定原报告、计划、输入入口与测试。后续完整86条新路径已通过；GPU更新与部分候选保存已
实际发生，完整267更新、最终candidate86与新进程重载仍待终态验收。

## 恢复训练的边界

未保存的 13 次更新不是可恢复的 checkpoint。下一轮只能从原来保存的 warm checkpoint
重新开始，并使用新的 optimizer；不能称为精确续跑。已消耗更新不能从预算账里消失。

若继续保持此轮累计最多 280 次更新，新的 run 最多可以尝试 267 次。原抽样规则每类各 70 次
只适用于 280 次，不能伪装成 267 次规则。保留全部 86 个样本时，可事先声明覆盖优先的
70／66／66／65 配额和独立顺序，再用新的合同与日志验收；这不是原均衡实验的自动通过。
新的独立抽样／预算模块已实现并通过 63 项纯软件测试，约束累计尝试数，并用每类 seeded
循环抽样与轮次交错保证全部 86 个来源 ID 的覆盖。该预算模块本身不读取或复制旧 journal，
也不授予optimizer权限。后续独立v2已完成真实来源计划和CPU准入，并实际进入GPU更新；
完整训练日志只能在phase终态后验收，不能把正在增长的journal封存成完整证据。

## 仍未证明的事情

新会话只能关闭旧验证器成功返回的图像。如果旧验证器在多帧解码中途抛异常、尚未返回
图像 tuple，新入口无法显式关闭其中未交出的图像；该路径仍禁止结果进入训练，不把它写成
完整异常清理已证明。此前错误没有故障时刻的内存记录，不能凭当前内存或测试结果断言原因。

这一阶段不涉及默认飞行策略切换、PX4 指令或 Gazebo 飞行。模型质量、实际 3-D 闭环、Jetson、
HIL/REAL、室外定位和最终 `0.3.0` 发布门继续未通过。

原始依据：[训练失败](evidence/onboard-navigation-streamed-training-failure-20261008-r1.json)、
[完整基线](evidence/onboard-navigation-streamed-baseline-phase-20261008-r1.json)、
[新多场景原图](evidence/openfly-parquet-original-images-20261008-r1.json)。
