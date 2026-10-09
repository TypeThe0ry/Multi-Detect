# OnboardAI 小型视觉语言模型选型调查

调查日期：2026-10-07。范围是已经公开、具有可获取权重的模型，不是宣布最终部署型号。本轮只查官方模型卡、论文、项目代码和 NVIDIA 部署资料；没有下载权重、安装运行环境或占用正在训练的 GPU。

## 结论

推荐先以 **Qwen3.5-2B 为通用实时决策主候选**，以 **Qwen3-VL-2B-Instruct 为视觉定位与空间推理对照**，同时保留 **SmolVLM2-500M 为低内存基线**。这个优先级是工程判断，不是本仓库已完成的性能排名。最终选择要由同一批三维连续观测、同一任务 Prompt、同一输出约束的实测决定，不能只比较模型名或通用排行榜。

理由是任务需要“持续看环境，结合用户目标与已发生的事情，决定下一步”，不是只给检测框增加颜色规则。Qwen3.5 的官方权重包含视觉输入，且已有 Orin Nano 文本 Agent 部署资料；Qwen3-VL 则有明确的多帧、时间定位和空间 grounding 设计。两者都需要验证量化后的视觉能力与响应时间。[Qwen3.5-2B 模型卡](https://huggingface.co/Qwen/Qwen3.5-2B)、[Qwen3-VL-2B-Instruct 模型卡](https://huggingface.co/Qwen/Qwen3-VL-2B-Instruct)、[Jetson AI Lab 的 Orin Nano 教程](https://www.jetson-ai-lab.com/tutorials/openclaw/)。

**Cosmos-Reason2-2B 值得作为物理/视频推理研究对照，但暂不作为“已适配 8GB 的机载默认模型”。** 它的官方最低 GPU 内存要求是 24GB；要在 RTX 4060 8GB 或 Orin Nano 上使用，必须另外证明小输入、量化和运行时组合可行。[Cosmos-Reason2-2B 模型卡](https://huggingface.co/nvidia/Cosmos-Reason2-2B)。

## 五个可获取权重的实际候选

“官方能力”表示作者公布的接口、设计或评测，不表示无人机任务已通过。所有模型本轮均未在本仓库执行推理或微调。

| 候选及官方权重 | 参数规模 | 权重许可证/获取条件 | 多帧与定位能力 | 在本项目的角色及未验证点 |
| --- | --- | --- | --- | --- |
| [Qwen3.5-2B](https://huggingface.co/Qwen/Qwen3.5-2B) | 名义 2B；模型卡列出视觉编码器及语言模块 | Apache-2.0；公开权重 | 图像、视频、空间任务评测及工具调用；支持 non-thinking/thinking | 通用 RGB + Prompt 决策主候选；量化视觉输入、多帧延迟、中文任务指令、8GB 并行检测仍待测 |
| [Qwen3-VL-2B-Instruct](https://huggingface.co/Qwen/Qwen3-VL-2B-Instruct) | 名义 2B，不把型号数字当精确总参数量 | Apache-2.0；公开权重 | 多图/视频、文本与时间戳对齐、2D/3D grounding | 空间定位与时间理解对照；三维输出不是已标定的世界坐标，更不是 GPS 精度保证 |
| [SmolVLM2-500M-Video-Instruct](https://huggingface.co/HuggingFaceTB/SmolVLM2-500M-Video-Instruct) | 0.5B | Apache-2.0；公开权重 | 图像、多图、视频和文字可交错输入；模型卡主要语言为 English | 低内存 shadow 基线；复杂空间决策、中文 Prompt 与遮挡记忆能力需验证，不默认小模型足够完成任务 |
| [InternVL3_5-1B](https://huggingface.co/OpenGVLab/InternVL3_5-1B) | 官方表为约 1.1B 总参数：视觉 0.3B + 语言 0.8B | Apache-2.0；公开权重 | 多图、视频示例、grounding 与 embodied 任务评测 | 第二种小型空间推理基线；图像切块数量、多帧内存和 Orin 运行时支持待测 |
| [Cosmos-Reason2-2B](https://huggingface.co/nvidia/Cosmos-Reason2-2B) | 官方精确总参数 2,438,696,960；Qwen3-VL-2B-Instruct 底座 | NVIDIA Open Model License；HF 当前 gated，需接受条款及联系信息分享 | 视频/图像物理与时空推理；输入视频建议 4fps | 物理推理研究候选或离线 teacher；不是 action-conditioned 飞行动力学模型；8GB 量化与实时部署未证明 |

公开时间也不是推测：Qwen 官方记录 2025-10-21 发布 Qwen3-VL 2B，2026-03-02 发布 Qwen3.5 2B；Cosmos 卡列出 2025-12-19 首发及 2026-03-10 更新。InternVL3.5 和 SmolVLM 的官方论文均在 2025 年公开，但这里不以论文日期冒充每个具体 checkpoint 的发布日期。[Qwen3-VL 官方发布记录](https://github.com/QwenLM/Qwen3-VL)、[Qwen 官方 Qwen3.5 发布记录](https://raw.githubusercontent.com/QwenLM/Qwen3.5/main/README.md)、[Cosmos 模型卡](https://huggingface.co/nvidia/Cosmos-Reason2-2B)、[InternVL3.5 论文](https://arxiv.org/abs/2508.18265)、[SmolVLM 论文](https://arxiv.org/abs/2504.05299)。

Qwen 项目地址可能随新版本重命名或重定向；实施时必须绑定上表精确模型 ID、权重 revision、处理器配置及文件 SHA-256，不能用“当前 main”代替模型身份。

## 显存与速度：事实、推算和未知分别记录

### 作者公布的资料

- SmolVLM2-500M 模型卡报告视频推理使用约 **1.8GB GPU RAM**。这是作者的报告，不是本机实测；本轮没有获得足以复现其内存数字的完整帧数、token 数和硬件配置，不能据此承诺 Jetson FPS。[官方模型卡](https://huggingface.co/HuggingFaceTB/SmolVLM2-500M-Video-Instruct)。
- Jetson AI Lab 报告 Orin Nano 上 **Qwen3.5-2B Q8_0** 的文本/tool-calling Agent：16,384 token 上下文，约 **4.6GB** 内存、**20 token/s**、warm 首响应约 **3 秒**。这是官方教程报告的文本路线，不是本仓库测量，不是摄像头推理 FPS，也不包含本项目检测、深度、录像、飞行通信的并行负载。教程还建议增加 swap；swap 不是额外高速 GPU 内存。[官方教程](https://www.jetson-ai-lab.com/tutorials/openclaw/)。
- Cosmos-Reason2-2B 的卡片声明只测试 BF16 与 Linux，并列出 Blackwell/Hopper 兼容性；推理测试栏另列 H100/A100，最低内存为 **24GB**，平台延迟尚待公布。这不是 RTX 4060/Orin 已验证兼容性。卡中的简短示例也不能覆盖这些测试边界。[官方模型卡](https://huggingface.co/nvidia/Cosmos-Reason2-2B)。

### 只用于预算的算术

若总参数数为 `P`，且全部参数都以同一精度存储，BF16/FP16 的纯权重载荷约为 `2P` 字节，4-bit 约为 `P/2` 字节。因此 0.5B / 1.1B / 2.0B 参数的纯权重分别约为 1.0 / 2.2 / 4.0 GB，4-bit 分别约为 0.25 / 0.55 / 1.0 GB。这里 GB 为十进制；这些只是示例，不是上表每个权重文件的精确体积，更不是运行所需内存。

量化比例、未量化视觉编码器、量化尺度、KV cache、视觉 token、激活、处理器、运行时分配与同时运行的检测模型都会增加内存。Orin Nano 的设备内存还需要容纳系统和其他进程，不能直接当作台式机独立 8GB 显存使用。模型宣传的 256K 级上下文不能当作 8GB 配置承诺；Qwen3.5 的卡也明确提示 OOM 时缩短上下文。[Qwen3.5 内存提示](https://huggingface.co/Qwen/Qwen3.5-2B)、[NVIDIA 的 Tegra 共享内存说明](https://docs.nvidia.com/cuda/cuda-for-tegra-appnote/)。

本轮没有测量任何 VLM 的本机峰值显存、TTFT、端到端决策时间、Jetson 功耗/温度或视频吞吐。**不能用 token/s 替代“看图到产生可执行飞行意图”的延迟。**

## 微调与量化路径

| 候选 | 已核验的训练/压缩入口 | 不能直接作出的承诺 |
| --- | --- | --- |
| Qwen3-VL | 官方 `qwen-vl-finetune` 支持图像、视频、grounding 数据与 LoRA，并提供冻结视觉模块的配置；[训练说明](https://raw.githubusercontent.com/QwenLM/Qwen3-VL/main/qwen-vl-finetune/README.md) | 例子不是 8GB 最小配方；控制帧数、像素和 token 后还需实际内存检查 |
| Qwen3.5 | 官方卡将小模型用于任务微调；ModelScope SWIFT 提供 Qwen3.5 模型族的多模态 LoRA 训练脚本；[模型卡](https://huggingface.co/Qwen/Qwen3.5-2B)、[框架维护者的训练脚本](https://github.com/modelscope/ms-swift/blob/main/examples/models/qwen3_5/transformers.sh) | 当前示例针对更大型号/多 GPU，不等于 2B 的 8GB 配方已验证；新架构的量化与训练算子也要锁版本 |
| SmolVLM2 | 作者提供视频/图像 LoRA/PEFT 入口，可冻结语言或视觉子模块；[作者训练 README](https://github.com/huggingface/smollm/blob/main/vision/smolvlm2/README.md) | 小模型不等于中文或空间任务已经可靠；模型卡也将 critical automated decision-making 列为非适用用途，不能当作唯一飞行安全判断器 |
| InternVL3.5 | 官方卡链接 InternVL/SWIFT/XTuner 微调，并给出 BNB 8-bit 示例；[官方入口](https://huggingface.co/OpenGVLab/InternVL3_5-1B) | 8-bit 示例以 8B 型号展示；既不保证任意 4-bit 后端，也不证明 ARM/JetPack 已兼容 |
| Cosmos-Reason2 | 官方项目提供 embodied 数据的 SFT 和 RL 示例；[官方项目](https://github.com/nvidia-cosmos/cosmos-reason2) | 不是 8GB 训练或量化保证；teacher 的输出也不能未经审查直接充当真实飞行动作标签 |

8GB 开发起点应是：冻结视觉编码器、短窗口、小 batch、限制视觉 token，再测 LoRA/QLoRA 的实际可行性。这是拟采用的工程方案，并非本轮训练结果；若仍不满足内存要求，应调度更大训练设备，而不是声称“已经训练了 OnboardAI”。

Cosmos 的权重不是 Apache-2.0。其 NVIDIA 许可证允许商用和派生，但有分发许可、归属展示以及 guardrail 等条件；使用模型或其输出训练/改进其他模型也有 Cosmos 归属要求。实施前要封存实际接受的许可证版本并核对条件，本轮未替用户接受 gated 下载条款。[NVIDIA Open Model License](https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-open-model-license/)。

## 首轮实际验证应回答什么

下面是验证设计，不是已经通过的验收。

1. **直接视觉决策**：输入真实渲染/摄像头 RGB、任务 Prompt、有限历史帧及可追溯任务记忆。让模型给出下一段有界飞行意图，不要求只能解释固定颜色案例。冻结的 40 维策略可以作为对照或蒸馏对象，不应成为语义模型永远看不到 RGB 的理由。
2. **连续性而不是单张答题**：独立三维场景中检查目标遮挡/重现、相似物混淆、视角变化、搜索尚未完成、环境改变、重复时间戳与旧帧。任务记忆要绑定飞机、任务、帧时间和证据；模型支持视频或聊天 history 不等于已有可靠的长期记忆。
3. **最小公平硬件对照**：先统一 1/4/8 帧、限定像素预算、短上下文和输出 token；记录实际输入 token、首 token 时间、完整输出时间、峰值内存、非法结构比例与超时比例。先单独推理，之后再测和检测模型并行，避免把排队等待当模型能力。
4. **看得懂还要做得对**：单独评分目标匹配、空间/时间关系、证据引用和下一步行动；量化前后使用同样的 holdout。模型的自述 confidence 不是标定概率。“最近”依赖实际深度/测距和 GNSS 质量，不能靠 VLM 随口估算距离或编造 GPS。
5. **输出可执行但不越界**：模型负责“继续搜索、改变观察位置、前往已证实坐标、保持、返航”等意图；输出必须经既有契约、规划可行性和 PX4 事务路径。不是让文本直接变成未校验的 MAVLink/PWM 指令。空间 grounding 本身也不赋予解锁/飞控权限。
6. **先 shadow，再微调，再闭环**：先保留模型原始输出与实际输入证据，检查可用性；再用独立 train/val/test 的审查过视觉—任务—行动轨迹微调。当前 VLM 调查不替代 Gazebo 飞行动力学验收，更不替代 Jetson/HIL/REAL 验收。

初版使用短结构化 non-thinking 输出，设置 token 与时间上限。尤其 Qwen3.5-2B 的官方卡提示 thinking 可能陷入循环；Cosmos 的推荐长 reasoning 配置也不能直接当作实时控制预算。是否缩短输出会损害物理任务能力必须通过测试，而不是假设不会。[Qwen3.5 模型卡](https://huggingface.co/Qwen/Qwen3.5-2B)、[Cosmos 模型卡](https://huggingface.co/nvidia/Cosmos-Reason2-2B)。

## 本轮完成与未完成

完成的是官方资料核验、五个可获取权重候选比较、部署边界与首轮验证方案。没有完成：权重下载、量化推理、VLM 微调、VLM 驱动 Gazebo 飞行、Jetson 部署、HIL/REAL 或室外 GNSS 验收。本文件不会提升候选、修改模型 reference 或发布版本。

```text
vlm_weights_downloaded = false
vlm_local_inference_measured = false
vlm_finetuning_performed = false
vlm_gazebo_closed_loop_accepted = false
jetson_vlm_runtime_verified = false
hil_real_evidence = false
outdoor_gnss_evidence = false
candidate_promoted = false
final_release_allowed = false
```
