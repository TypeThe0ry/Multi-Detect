# 公开预训练候选增补核查（2026-10-08，r2）

结论：OpenFly-Agent 有真实无人机导航 checkpoint，值得做独立离线对照；OpenVLA 和 Cosmos Policy 有真实动作权重，但不是现成无人机控制器。能下载、能装入显存、能实时安全执行，是三个不同门。本次仅查官方网页、源码与文件元数据，未下载权重、接受门控条款或运行模型；未改变默认模型、训练预算或飞行权限。既有 SmolVLA/WorldVLN 调查不在本次重复验收。

## 真实发布物

| 候选 | 固定 HF revision / 权重元数据 | 可复用边界 |
| --- | --- | --- |
| [OpenFly-Agent-7B](https://huggingface.co/IPEC-COMMUNITY/openfly-agent-7b) | `21dcce235f1f2d40fc23c76998051abc5434cc99`；公开、非 gated；3 个 safetensors 合计 **15,082,600,824 B** | OpenVLA 衍生、Llama-2 + DINO/SigLIP；语言、当前与历史 RGB → 导航动作 |
| [OpenVLA-7B](https://huggingface.co/openvla/openvla-7b) | `47a0ec7fc4ec123775a391911046cf33cf9ed83f`；公开、非 gated；同样 3 个 shards / **15,082,600,824 B** | 机械臂动作，不是无人机 FRD |
| [Cosmos-Predict2.5-2B](https://huggingface.co/nvidia/Cosmos-Predict2.5-2B) | `85f8ae7bfe8f5525c8d103429524dcf12f98bf7b`；`gated=auto`；`robot/policy/libero/model.pt`、`robocasa/model.pt` 各 **4,118,532,207 B** | 普通 base 是视频生成；Policy 是另外训练的机器人动作策略 |
| [Cosmos3-Edge-Policy-DROID](https://huggingface.co/nvidia/Cosmos3-Edge-Policy-DROID) | `a7c7288f9b6ac1684e993007b0f9703dd26e58ef`；公开、非 gated；5 个 safetensors 合 **9,136,972,464 B**（含 VAE、vision encoder、11KB 框架记录，不等于最小动作推理驻留量） | 4B、DROID 8 维动作块，非 drone；OpenMDW-1.1 |

以上大小是服务器声明的文件字节数，不是本机下载校验或运行显存。可复核固定 [OpenFly 元数据](https://huggingface.co/api/models/IPEC-COMMUNITY/openfly-agent-7b/revision/21dcce235f1f2d40fc23c76998051abc5434cc99?blobs=true)、[OpenVLA 元数据](https://huggingface.co/api/models/openvla/openvla-7b/revision/47a0ec7fc4ec123775a391911046cf33cf9ed83f?blobs=true)、[Cosmos 元数据](https://huggingface.co/api/models/nvidia/Cosmos-Predict2.5-2B/revision/85f8ae7bfe8f5525c8d103429524dcf12f98bf7b?blobs=true)。OpenFly 卡标 MIT；OpenVLA 官方同时提醒 Llama-2 底座许可限制，不能把 MIT 标签当衍生许可已清理。Cosmos 使用 NVIDIA Open Model License。[官方许可说明](https://github.com/openvla/openvla#pretrained-vlas)、[Cosmos 模型卡](https://huggingface.co/nvidia/Cosmos-Predict2.5-2B)。

## 动作与闭环不能混称

OpenFly 官方仓库固定 `c075075497a7122bad82f5b76b9be926ad5a81b3`。`train/eval.py:404–499` 将 8 维输出匹配为 10 个 primitive：STOP、前进 3/6/9、左右 yaw、升降、左右平移；不是任意尺度连续速度。编码 yaw 为 15、执行却转 30°；升降编码 2、横移编码 5，位姿更新却统一用 3 的步长。未匹配输出直接落到 STOP；这些不能照抄成合法动作。`vln_norm` 与评估所用 `vlnv1` 统计也不同，必须锁定。Airsim 分支用 `simSetVehiclePose` 更新位姿，目标真值在评估侧算成功，因此是渲染/运动学导航闭环，不是 PX4 动力学、避障安全或实飞反馈。[官方评估源码](https://github.com/SHAILAB-IPEC/OpenFly-Platform/blob/c075075497a7122bad82f5b76b9be926ad5a81b3/train/eval.py#L404-L499)。

OpenVLA 输出机械臂 `(x,y,z,roll,pitch,yaw,gripper)`，需按机器人/数据集统计反归一化；官方明确不零样本泛化到未见 embodiment。不能去掉 gripper 后冒充无人机动作。[官方模型卡](https://huggingface.co/openvla/openvla-7b#uses)。

Cosmos base 的未来视频不等于执行反馈，Action-Cond 是“给动作预测图像”，不是“给图像选动作”。Policy 确实输出动作，但发布对象为 LIBERO/RoboCasa 等机器人任务，不是无人机控制率、坐标系或碰撞恢复已合格。[官方卡](https://huggingface.co/nvidia/Cosmos-Predict2.5-2B)、[官方 Policy 教程](https://github.com/nvidia-cosmos/cosmos-cookbook/blob/main/docs/recipes/post_training/predict2/cosmos_policy/post_training.md)。

更新不能停在 2.5：Cosmos3-Edge-Policy-DROID 已于 2026-07-20 发布。官方动作仅支持 DROID 8D；“Edge/实时”不是 Orin Nano 或无人机证明。固定 [文件元数据](https://huggingface.co/api/models/nvidia/Cosmos3-Edge-Policy-DROID/revision/a7c7288f9b6ac1684e993007b0f9703dd26e58ef?blobs=true) 可复核，未下载。[官方卡](https://huggingface.co/nvidia/Cosmos3-Edge-Policy-DROID)。

## 8GB 的现实门

本轮 root 的只读 `nvidia-smi` 查询（native0，chunk `becdc3`）确认本机为 **RTX 4060，总显存 8188 MiB**，不是此前口述的 4060 Ti；该观测由 root 提供，本研究未独立运行硬件查询。以下 8GB 容量门也适用于所询问的 4060 Ti 8GB，瞬时空闲显存不能当稳定预算。

OpenFly/OpenVLA 原 BF16 权重已超过显存，需独立量化/卸载方案；桌面型号或算力不能消除容量门。OpenVLA 作者曾给出 BF16 约 16GB、INT4 约 6GB，但这不是 OpenFly 三帧或 Jetson 的验收。[作者答复](https://github.com/openvla/openvla/issues/152)。Cosmos 默认 Video2World 官方约 32.54GB，不能原样驻留 8GB。[模型卡硬件要求](https://huggingface.co/nvidia/Cosmos-Predict2.5-2B#evaluation)。Policy 无规划版本官方 LIBERO 6.8GB、ALOHA 6.0GB、RoboCasa 8.9GB，带模型规划至少 10GB。前两者可能值得 8GB 独立实验，不能推成 drone 实时可用。[硬件表](https://github.com/nvidia-cosmos/cosmos-cookbook/blob/main/docs/recipes/post_training/predict2/cosmos_policy/post_training.md#hardware-requirements)。

Cosmos3 官方 Thor T5000 的 PyTorch 中位端到端 1.528s 是一次 **32 步 × 8D** 动作块：四步 UniPC、640×540 输入映射到 544×736、单请求持久连接。15Hz 的判定用整块 2.133s 预算，不是每步视觉反馈 15Hz；Thor T2000 16GB 同表为 5.195s。不能外推到 8GB Orin Nano/4060 Ti，也不能当无人机反应时延。[官方测量条件与表](https://huggingface.co/nvidia/Cosmos3-Edge-Policy-DROID#real-time-pytorch-policy-droid-with-32-8-action-chunks)。

Orin Nano 的 8GB 是 CPU/GPU/系统共享内存，不等于桌面卡的独立 VRAM；Super 的 67 TOPS 是稀疏 INT8 指标，不能换算 BF16 导航频率。还需核 ARM64 依赖、量化后端、相机/安全进程内存与热功耗；本次未找到这些候选的官方 Orin Nano 无人机实测。[NVIDIA 规格](https://developer.nvidia.com/blog/nvidia-jetson-orin-nano-developer-kit-gets-a-super-boost/)。

下一步优先固定 OpenFly revision/processor/动作统计，独立有界量化实验：保留实际 raw、非法输出拒绝，与现候选同真实 RGB/history/prompt 比较，不按开发结果挑 checkpoint；测包含采集、编码、推理、准入的端到端 p50/p95/p99、峰值内存和拒绝率。动作坐标/单位与 shared Mission 编译、安全监督仍须另验；未完成这些门前不接 transport 或设默认。
