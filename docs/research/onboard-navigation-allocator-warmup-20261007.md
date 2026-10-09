# 导航模型加载：整块缓存预热的兼容实验

2026-10-07，本页只说明本机代码和真实失败结果，不是训练或飞行验收。

新增逐张量读取入口的第一次完整加载已退出 1。两个 checkpoint 头部打开成功，
每次 76,248 字节；真实 tensor 读取为零，底座尚未完成初始化。
报告为 `OutOfMemoryError`：申请约 3.96 GiB、当时报告设备可用约 6.93 GiB，
PyTorch 已分配和保留均为零。没有 optimizer journal，更新次数保持未知，不能填零。
此前访问异常和 Windows 1455 的失败原件单独保留。

## 找到的可选分配

实际绑定的 Transformers 4.57.6 `modeling_utils.py` 在加载 shard 前调用
`caching_allocator_warmup`（本机行 5432；函数定义 6025；整块分配 6105）。
函数先计算将驻留设备的参数总字节数，再进行一次大块 `torch.empty`，目的只是减少
后续逐个分配的加载耗时。它不是模型前向、参数更新、设备授权或物理安全检查。
本机版本没有跳过它的环境变量分支。不得把 `factor=2` 看成字节减半：该分配使用
float16，每个元素仍占两个字节。

[固定版本官方源码](https://raw.githubusercontent.com/huggingface/transformers/v4.57.6/src/transformers/modeling_utils.py)
支持这个性能优化的作用说明；本机实际文件的 SHA 另由实验 manifest 绑定，
不能仅凭版本号假设本机文件逐字节等于上游。

## 下一次实验改变什么

新入口仅在自己的子进程关闭该可选预热，仍使用原 BF16 底座、原 CUDA 设备映射、
FP32 语言 LoRA、86 个样本、一轮以及原像素/显存预算。没有量化、CPU 降级、
更换模型、删除数据、关闭其它应用或改动页面文件。原封存入口与失败记录不改。

关闭必须由实际调用记录、已绑定的函数/源码、进程身份和最终回执证明，不能只靠
配置声明；加载仍需同时通过原 supervisor 和逐张量读取器的结果门。
完整初始化通过后才能开始同一计划的训练，随后分别检查更新、保存和精确重载。

## 尚不能得出的结论

当前失败日志没有捕获该异常的完整调用帧；报告数值和零 payload 与预热路径相符，
但不能据此证明碎片化、WDDM/host commit 的确切原因或此前两种错误的根因。
跳过预热也不能保证后续单 tensor 分配、完整驻留或训练激活能放进现有资源。
不得把 CPU 测试、header 打开或独立留证审核写成完整模型/训练/自主飞行成功。

## 后续真实结果：完整加载、训练保存及独立精确重载已验证

新入口先遇到早期导入顺序错误；该失败原件保留。仅在子进程构造模型前加入仓库
`src` 路径后，完整 load-only 原生退出 0：625 个 BF16 底座张量全部读入 CUDA，
FP32 q/v LoRA 初始化完成，实际预热调用被跳过且钩子已恢复。原模型、精度、输入和
预算未改变。[完整加载证据](../evidence/onboard-navigation-full-load-only-20261007-r2.json)
记录三个验收层与独立审核，不用头部打开次数代替张量完成数。

随后原 86 样本、一轮训练的 86 次参数更新完成，344 行四阶段 journal 与独立字节
审核一致；冻结底座不变，112 个 FP32 LoRA 张量变化并保存。但同进程第二次 fresh
底座加载在第 498 个张量转移时出现 CUDA 分配失败，训练原报告仍退出 1，不能改成
验收成功。[训练保存与重载失败证据](../evidence/onboard-navigation-trained-adapter-20261007-r2.json)
保持这个边界。后续新独立GPU worker完整加载BF16底座，112个FP32保存adapter张量
逐项精确恢复；原4个输入和生成token一致，worker退出0，没有参数更新。原恢复supervisor
因Windows venv启动器／worker PID不同退出1，失败仍保留。新增只读身份跟进检查核对
真实进程链、命令和已绑定运行文件，退出0；独立字节审计也退出0，没有删除PID门或重跑模型。
[独立精确重载证据](../evidence/onboard-navigation-exact-reload-20261007-r1.json)单独记录通过结果，
不把旧失败改写为成功，也不据此确立OOM根因。

现有日志不能证明具体碎片化原因，也不能证明旧模型被持续引用；此次成功仅说明
该兼容入口在本次完整加载与训练中可用。4 个训练内探针的合法输出和动作各 1/4、
距离尺度 0/4，仍没有独立场景、实际飞行、Jetson 或硬件质量验收。

后续共享导航actor输入基础已实现：官方本地processor在CPU验证全部86个保留样本，
监督投影与可观测输入构造的identity及四个实际tensor字节一致，native0，129源不变。
来源、actor identity与保存tensor记录的独立只读核验通过，未二次运行processor。
CUDA未初始化、没有模型加载／推理或optimizer；新system不是旧训练prefix的字节复现，
也不是独立实时捕获。[共享输入证据](../evidence/onboard-navigation-shared-actor-inputs-20261007-r1.json)
不改变本页旧失败或OOM结论；新trainer／导航live backend／280次更新和默认飞行接入仍未完成。
