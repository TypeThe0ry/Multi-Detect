# 已训练的无人机导航模型：能复用什么，不能冒充什么

查验日期：2026-10-08。本页补充[通用VLM／世界模型查验](onboard-pretrained-model-options-20261007.md)，重点找真正从画面与任务生成动作的模型。公开页面、论文、代码和权重元数据分别核对；没有下载这些大模型、接受访问协议、运行它们或替换默认模型。

## 结论

确实有可公开获取的无人机导航权重，不必把所有能力都从零训练。但目前查到的两套完整模型，在发布精度下仅权重文件就超过本机8GB显存；它们也不是当前Qwen3-VL-2B导航LoRA，可以直接叠加或无缝换掉。

当前最合理的两条并行路线是：继续训练现有小模型、补不同场景的原始三维画面和动作；把成熟导航模型作为独立对照或未来离线教师。若小模型的文本动作输出不够及时，就在同一视觉语言底座和已有LoRA上训练动作头，让下一段动作由模型预测，而不是程序写死。这里的取舍是当前硬件、延迟和迁移成本推导，不是已经证明某条路线更好。

## 权重发布是否真实

以下大小来自官方HF API的LFS元数据，不是实际显存需求，也不是本地已下载文件的哈希验收。GB为十进制。

| 项目 | 查验到的发布状态 | 权重体量 | 对当前工作的用途 |
| --- | --- | ---: | --- |
| [3DG-VLN](https://huggingface.co/fanfuxue/3DG-VLN) | 公开、非gated；模型卡标Apache-2.0；四个完整safetensors分片 | 16.58GB | 候选导航对照／教师，不是当前LoRA |
| [WorldVLN](https://huggingface.co/EmbodiedCity/WorldVLN) | 公开、非gated；室内与室外各有主干及动作decoder | 一套主干35.07GB；室外另有约1.06GB动作decoder、0.74GB VAE | 世界→动作闭环参考，不能声称已适配8GB机载 |
| [Qwen-VLA](https://github.com/QwenLM/Qwen-VLA) | 官方仓库当日只有说明与演示资产，未找到权重链接；Qwen官方HF的VLA名称查询返回空数组 | 未核到发布文件 | 动作decoder训练方法参考，不是已经拿到的模型 |
| [drone-vla-toolkit](https://huggingface.co/Sherlockhu/drone-vla-toolkit) | 当日只有README与训练／采集脚本等8个文件，没有权重文件 | 无 | 第三方工具包，不写成已训练模型 |
| [AutoFly](https://github.com/xiaolousun/AutoFly-VLA) | 官方模型地址匿名返回401；部署代码仍为占位说明 | 未核到可访问权重 | 等待产物公开，参考速度输出方法 |
| [VLA-AN](https://arxiv.org/html/2512.15258v2) | 论文报告Orin机载实验，未核到作者公开checkpoint／部署包 | 未核到发布文件 | 机载优化研究依据，不是已交付模型 |

3DG-VLN固定revision为`02e161fffd70fe8899a3b065dd41e45707221fcf`；WorldVLN为`f1ef44f63ab12af1fc745e6665e3c659916310ad`。逐分片大小及发布者SHA见[机器可核对的查验记录](evidence/onboard-pretrained-navigation-model-research-20261008-r1.json)。

## 3DG-VLN：已看见目标后的接近，不是未知位置全域搜索

[官方实现](https://github.com/xuefanfu/3DG-VLN/tree/0dfd54a68ca667f9bbada5ba7b7a834c1bf7ce09)用Qwen2.5-VL、前视／下视双RGB与粗方向提示，预测五个三维waypoint。每点是相对于同一个当前姿态的前／右／下位移，不逐点累加，也没有yaw输出。代码将其变换到AirSim的SI位置坐标，可据此确定该执行实现的位移为米；右侧位移不是右转机头。[转换源码](https://github.com/xuefanfu/3DG-VLN/blob/0dfd54a68ca667f9bbada5ba7b7a834c1bf7ce09/src/vlnce_src/eval_qwenvl_direction.py#L337)、[AirSim单位](https://github.com/microsoft/AirSim/blob/main/docs/apis.md#coordinate-system)。

[论文](https://arxiv.org/html/2606.20045v1)针对目标起始已可见的接近任务；基准初始方向由目标位置和无人机姿态构造。后续还有GroundingDINO更新方向，外部深度停止判据，以及GT目标距离参与的episode终止。它不是无目标先验的RGB-only搜索，也不能把这些外部判断写成模型已经学会的停止或任务完成。深度没有直接送进waypoint模型，不等于整个系统不需要深度。

可作为接近目标阶段的对照或未来教师，但先要用实际观测替代oracle方向与终止逻辑，再重验能力。没有查到Jetson实测或明确模型更新Hz；路径速度参数不等于推理频率。HF卡标Apache-2.0，代码README也写Apache-2.0，但固定树没有根LICENSE，依赖许可仍未核清。

## WorldVLN：与目标接近，但没有证明全机载

它根据语言与实际观测历史预测短段世界表示，再解码动作；执行后用新收到的真实画面更新历史，而不是一直相信自己预测的画面。[官方论文](https://arxiv.org/html/2605.15964v1)中的实飞平台有Jetson Orin NX 16GB和PX4，但高层模型运行在地面服务器，Jetson负责采集、通信与接口。这不是Jetson全机载推理证据。

[官方代码](https://github.com/EmbodiedCity/WorldVLN.code/tree/3409b82a16c6606b270b48d7ea4c8f7d08e43c38)的服务输出六个相对动作分量，以厘米／度表示，通常每轮16个动作。当前项目的动作是机体FRD米制位移或弧度yaw，不能仅改字段名就接飞控；坐标轴、旋转顺序、时间跨度和停止含义仍需验证。代码标CC-BY-4.0，HF模型元数据没有license值；代码许可不能代替权重及上游组件许可。

论文也没有证明任意长任务、任意开放环境或本项目的目标搜索／GPS记录／返航全流程。本仓库未运行它，量化、蒸馏或CPU offload的速度和质量均未测。

## Qwen-VLA：学习动作头的依据，不是可下载的现成替代

[官方说明](https://github.com/QwenLM/Qwen-VLA/tree/fa25ab1e69fd9d4318aa0fc5d3f2114a443f78d3)采用Qwen3.5-4B与1.15B动作decoder，联合学习动作和轨迹。它支持研究“视觉语言特征直接生成动作”的方向，但不是当前Qwen3-VL-2B的LoRA，也没有查到本项目需要的PX4无人机部署产物。官方名称查询为空只说明这次检查没有找到，不证明全网永久不存在。

## AutoFly与VLA-AN：不能把论文条件省略

AutoFly的官方模型地址当前匿名返回401，代码的训练／验证／部署目录只有占位README。[论文部署附录](https://arxiv.org/html/2602.09657v1)报告15 FPS，但使用远端RTX4090、LAN通信与专门优化；输出前进速度、yaw角速度和垂直速度，不是三个方向的位移。精确单位、符号与控制周期仍未核实，不能直接接当前动作契约。数据集元数据可读，也不能代替模型可获取证明。

[VLA-AN论文](https://arxiv.org/html/2512.15258v2)报告Orin NX 16GB、30W上的机载方案，配合量化与多项系统优化，机载推理2–3Hz、闭环异步执行；这不是已核到精确定义的端到端闭环频率。未核到公开checkpoint、完整部署实现或可复核的动作契约。它比远程服务器方案更符合机载目标，但这只是论文证据，不是本仓8GB硬件的实测，不据此承诺速度。

## 进入本项目之前必须通过的实际检查

1. 权重可取得、许可可用、来源固定；下载后重新计算文件SHA，不把发布者SHA当本地验证。
2. 输入只含当时能观测到的任务与图像／状态，不读未来路线或场景truth。
3. 弄清动作坐标、单位、预测跨度、停止语义；不把导航动作当电机PWM。
4. 用相同未训练场景，与现有模型比较成功率、失败方式、延迟和总内存。
5. 从Gazebo真实三维渲染闭环开始，再做Jetson、PX4和真实硬件验收；模拟成功不自动变成实飞成功。

研究记录不产生执行授权。默认模型、`0.3.0-pre.8`与全部质量晋升／硬件／最终发布门保持不变。
