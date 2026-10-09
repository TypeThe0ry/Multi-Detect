# 公开模型复核：航空动作、小型 VLA 与视觉历史要分开

2026-10-08，三个并行工作方向之一。仅查询官方模型卡、固定配置／代码、公开元数据和一次 checkpoint HEAD。没有下载新权重、运行 GPU、训练、接飞控或晋升。既有候选的失败成绩仍保留。

## 推荐角色

| 候选 | 本仓用途 | 不能声称的能力 |
| --- | --- | --- |
| OpenFly-Agent-7B | 第一组航空导航离线对照 | 已通过 PX4 动力学、可以在 8GB 上直接全量训练 |
| SmolVLA-450M | 小型航空动作头与训练工具链研究 | 机械臂预训练等于无人机零样本自主飞行 |
| V-JEPA 2.1 ViT-B/16 | 冻结视频历史表征与小 probe 对照 | 小视频编码器本身就是可飞的世界策略 |

### OpenFly

固定模型 revision `21dcce235f1f2d40fc23c76998051abc5434cc99`，当前公开、非 gated；元数据给三份 BF16 权重共 15,082,600,824 字节，不代表已经本地下载或验证完整权重 SHA。模型读取任务和当前／历史图像，适合作为航空动作输出对照。[官方模型卡](https://huggingface.co/IPEC-COMMUNITY/openfly-agent-7b)、[固定元数据](https://huggingface.co/api/models/IPEC-COMMUNITY/openfly-agent-7b/revision/21dcce235f1f2d40fc23c76998051abc5434cc99?blobs=true)。

原评估中包含 primitive 编码、步长转换及 AirSim 的 `simSetVehiclePose`，不能把位置直接更新的成绩当 PX4 闭环成绩。适配必须锁定反归一化统计、历史选择、坐标、单位和合法动作；非法输出明确拒绝。模型卡 MIT 与 OpenVLA／Llama-2 衍生条款需分别核对。[固定评估源码](https://github.com/SHAILAB-IPEC/OpenFly-Platform/blob/c075075497a7122bad82f5b76b9be926ad5a81b3/train/eval.py#L404-L499)。

### SmolVLA

固定 revision `5e8d12a6e2975b0e5e5fce7c8caf47c371d257b6`，Apache-2.0，主权重元数据 906,712,520 字节。配置是三相机和 6 维机器人状态到 6 维动作，50 步动作块及 10 次 flow-matching 推理步骤；不能把字段改名就当成无人机 FRD 动作。[官方卡](https://huggingface.co/lerobot/smolvla_base)、[固定配置](https://huggingface.co/lerobot/smolvla_base/resolve/5e8d12a6e2975b0e5e5fce7c8caf47c371d257b6/config.json)。

官方硬件指南的 batch 8＋AdamW 示例峰值约 10–16GB；权重不足 1GB 不说明本机 8GB 可以完整训练。若进入实验，先做 batch 1、冻结视觉、独立有界资源检查，再决定是否训练航空 head。需要航空实测状态与监督，缺测值不能填零。[硬件指南](https://huggingface.co/docs/lerobot/hardware_guide)、[适配文档](https://huggingface.co/docs/lerobot/smolvla)。

### V-JEPA

固定代码 `204698b45b3712590f06245fbfba32d3be539812` 发布 80M、384px 的 ViT-B/16 编码器，官方仓库 MIT。公开 checkpoint HEAD 返回 200，声明 1,664,223,428 字节；没有读取 body 或验完整 SHA，文件大小也不是实际推理驻留量。该编码器输出视频表征，不输出动作。[固定模型列表](https://github.com/facebookresearch/vjepa2/blob/204698b45b3712590f06245fbfba32d3be539812/README.md#models)、[checkpoint](https://dl.fbaipublicfiles.com/vjepa2/vjepa2_1_vitb_dist_vitG_384.pt)。

动作条件的 V-JEPA 2-AC 是另一组 ViT-g＋机器人 predictor，不能把小编码器当现成航空 world-policy。[官方 AC 接口](https://github.com/facebookresearch/vjepa2#v-jepa-2-ac)。

## 受审基线与实际工作顺序

当前 r33 默认、原感知链和 Qwen3-VL 的 pristine／warm／候选均保留，研究不等于部署。既有动作头 LOEO 合法动作 46/208、joint 5/208 且全 STOP，新 Gazebo 八帧也全 STOP；这些是反例，不能通过换名变成成功。详见[实际动作头状态](onboard-navigation-action-readout-status-20261008.md)。

先固定同输入对照原底座／旧 warm／候选／OpenFly，零更新，保存原输出、非法率、动作类型与尺度、端到端延迟和内存。不根据已看过的开发场景挑最终模型。模型输出明确机体 FRD 位移单位、yaw 单位、时间尺度和 validity；监督要覆盖升降、横移、不同距离及受阻恢复，不能把固定 3m／30°标签称为任意尺度。

动作训练的数据链必须是：真实 RGB／历史／任务／状态 → 提议动作 → 实际 Mission 与 ACK → 实际后继 RGB／深度／位姿／时间／失败。按完整 episode 和 scene 划分留出，不把生成视频或计划坐标当后继观察。

本机当前按 RTX 4060 8GB 条件规划。Orin Nano 的 8GB 为 CPU／GPU／相机／系统共享，桌面能跑、TOPS 指标或其他 Orin 型号成绩都不等于本机 Jetson 验收。[NVIDIA 规格](https://www.nvidia.com/en-us/autonomous-machines/embedded-systems/jetson-orin/nano-super-developer-kit/#specifications)。未来 SIM／Jetson 共用受审 student、预处理和输出契约；PC-only teacher 不冒充已部署端侧模型。

查询原生退出均为 0：官方元数据 `d53015`，V-JEPA 固定代码 `205644`，HEAD-only `a25557`。这些是查询证据，不是模型加载、训练或飞行成绩。
