# OnboardAI：哪些现成模型值得复用

查验日期：2026-10-07。只核对官方模型卡、仓库、HF API 元数据与文件 HEAD；本次未下载权重、运行 GPU、修改默认模型或验收旧记录。

## 要实现的目标

用户给出任务后，无人机持续看相机、理解任务与现场变化，决定下一段往哪飞；发现不匹配目标会继续寻找，匹配后记录位置并返航汇报。模拟与实机共用训练后的模型和决策契约，PX4 负责稳定飞行，执行前仍检查可飞范围与状态。不是写死每一步，也不是让视频生成模型替代真实传感器。

## 官方模型查验

以下为当日官方 HF API 的 revision 与全部 `.safetensors` 文件总字节数，不是显存需求。模型卡链接可进入官方文件列表；公开可下载不等于已经完成部署验收。

| 模型 | 发布状态及许可 | revision | 权重文件总字节 |
| --- | --- | --- | ---: |
| [Qwen3-VL-2B-Instruct](https://huggingface.co/Qwen/Qwen3-VL-2B-Instruct) | 无访问门槛；Apache-2.0 | `89644892e4d85e24eaac8bacfd4f463576704203` | 4,255,140,312 |
| [OpenFly-Agent 7B](https://huggingface.co/IPEC-COMMUNITY/openfly-agent-7b) | 无访问门槛；模型卡标 MIT，另须核对底座许可 | `21dcce235f1f2d40fc23c76998051abc5434cc99` | 15,082,600,824 |
| [Cosmos-Reason2-2B](https://huggingface.co/nvidia/Cosmos-Reason2-2B) | `gated=auto`，须按页面获取权限；NVIDIA Open Model License | `9ce19a195e423419c349abfc86fd07178b230561` | 4,877,470,304 |
| [Cosmos3-Edge](https://huggingface.co/nvidia/Cosmos3-Edge) | 无访问门槛；OpenMDW 1.1 | `344d602b128d1bbdacb43b08d0a3626f46343e29` | 9,127,516,064 |

API 可复查格式：`https://huggingface.co/api/models/<模型ID>?blobs=true`。代表性 Qwen、OpenFly、Cosmos3 权重 HEAD 返回 200；Reason2 未接受访问协议或下载。使用时仍需完整哈希与许可核对。

## 推荐：现有 Qwen 底座加导航 LoRA

保留已绑定的 Qwen 底座，新增导航适配器，让“原始任务 + 当前/历史相机画面”学习输出下一段运动方向与幅度。复用已有 YOLO 检测、跟踪和任务执行链，不另起飞控。通用 VLM 能看懂画面，不等于已经会飞。

仓库已有同一场景的 3 条 OpenFly 轨迹、86 个样本，全部 train，没有独立 holdout。当前只能验证训练链路和训练集回代，不能宣称泛化、真实自主飞行或候选晋升；见 [数据证据](evidence/onboard-openfly-navigation-dataset-20261007-r2.json)。下一步需要不同场景、未见任务及失败情况的独立测试，再进入 Gazebo 闭环。

## OpenFly：优先复用监督数据，不直接换底座

它确实是语言和图像输入、无人机导航动作输出的模型，但底座为 OpenVLA/Llama-2 与 DINO/SigLIP，不是 Qwen，权重不能作为现有 Qwen 的 LoRA 合并。官方 BF16 用法的原始权重已约 14.05 GiB，超过本机 8 GiB；量化后的适配与速度尚未验收。[官方项目](https://github.com/SHAILAB-IPEC/OpenFly-Platform)可复用数据与训练方法；[OpenVLA 许可说明](https://github.com/openvla/openvla#pretrained-vlas)明确底座模型另受 Llama Community License 约束，不能只看 MIT 标签。

## Cosmos-Reason2：同家族物理推理参考

官方说明它由 Qwen3-VL-2B 后训练而来，架构相同，适合未来做物理场景判断的对照或离线教师。但发布的是完整派生 checkpoint，不是可直接叠到现有模型的 LoRA；更换后必须重验 tokenizer、权重、契约与既有能力。模型卡列最低 24 GB GPU 内存，这是官方配置条件，不是本机低精度运行已获证明。这里没有完成它在 RTX4060/Orin 8GB 上的速度或动作能力验收。

## Cosmos3：世界/动作生成研究，不是可直接部署的无人机控制器

4B Edge 已公开，能够预测视频和动作，但架构不同于 Qwen；公开动作适配覆盖机器人、车辆等，不等于 PX4 无人机接口。模型卡仅验证 BF16/Linux，官方 AGX Orin **64GB** 的图像推理例约 **10.81 秒**，不是 Orin Nano 8GB 的实时结果。全部权重组件约 8.50 GiB，也不能把文件大小直接当运行内存。

可用于后续离线场景扩充或研究对照。官方明确其并非精确物理模拟：生成的未来画面不能当真实相机、物理真值或已执行飞行证据；训练标签仍需几何、动力学及独立审核。

## Jetson 部署路线与还缺的证据

[TensorRT-Edge-LLM 支持表](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/main/docs/source/user_guide/getting_started/supported-models.md)列有 Qwen3-VL-2B、Reason2 与 Cosmos3-Edge 的推理分支，不表示覆盖其全部生成/动作运行时；[平台矩阵](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/main/docs/source/user_guide/getting_started/support-matrix.md)列 Orin 的 JetPack 7.2/CUDA 13.2，使用 FP16/INT8/INT4，不运行 FP8/FP4 引擎。支持列表不保证所有模型在 8GB 都能运行，目标设备 SDK 仍需核实。

建议先完成导航 LoRA，受控合并后评估 INT4 导出，再为实际 Jetson SDK/硬件构建引擎。必须测量与 YOLO、跟踪、深度同时运行的总内存、延迟分布、过期决策拒绝及量化前后动作一致性。官方 [Orin Nano 8GB 示例](https://github.com/NVIDIA-AI-IOT/jetson-ai-lab/blob/main/src/content/tutorials/model-optimization/tensorrt-edge-llm.mdx)是 Qwen3-4B **纯文本**，不能借它宣称多图 VLM 飞行链通过。同一模型不要求 RTX 与 Jetson 的引擎文件相同，要求训练产物、输入含义、动作契约与行为经成对核验。

本页不批准候选晋升、飞控授权、HIL/REAL、室外 GNSS 或最终版本发布；也未要求升级 JetPack、重刷设备或替换现有默认方案。
