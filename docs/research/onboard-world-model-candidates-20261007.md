# OnboardAI：可获得权重的世界模型候选（2026-10-07）

结论：确有可获得预训练权重的世界模型，但本次没有找到可直接接入现有 PX4 Mission 链、已经适配本项目飞机动作域的通用飞行世界模型。当前最值得先验证的是 **V-JEPA 2.1 小型视频编码器作为离线视觉历史 teacher**；Cosmos 适合作为未来大显存离线生成/动作模型研究路线。不能用生成视频、机器人抓取成功率或权重已发布，代替 Gazebo/PX4 闭环与实机安全证据。

本页只记录官方论文、作者/厂商仓库及 model card 的检索结果与工程判断；未下载权重、安装依赖、运行模型、启动容器或训练。可获得不等于已在本机运行，也不等于无访问/许可条件。下文硬件数值是官方指定配置或明确标出的理论估算，不是本项目 benchmark。

## 先区分三件事

- **视频生成/外观迁移**：预测或生成 RGB 视频，可扩充训练外观；不保证对象身份、尺度、接触、碰撞、动力学或长期状态正确。
- **动作条件动态/规划**：输入过去观测及动作，预测未来像素/latent，或搜索动作序列。只有动作单位、车辆、时间和执行语义一致时才有控制意义；机器人关节/夹爪、汽车或游戏键盘动作不能直接改名成飞机动作。
- **Gazebo/PX4 物理与任务执行**：SDF 的惯性、碰撞、传感器及物理插件与 PX4 控制器/Mission 共同形成另一条验证链。漂亮视频不能替代这条链；SITL 通过也不自动证明实机安全。[Gazebo SDF/physics 教程](https://gazebosim.org/docs/harmonic/building_robot/)，[PX4 Gazebo 接入](https://docs.px4.io/main/en/sim_gazebo_gz/)。

## 候选与实际权重状态

下表含 NVIDIA、Meta、DIAMOND 三个独立来源家族；DreamerV3 单独列为算法，不凑作现成预训练模型。

| 候选 | 已确认的权重/能力 | 适合本项目的角色与限制 |
| --- | --- | --- |
| **Cosmos3-Edge / Edge-Policy-DROID** | 当前 Cosmos3 的 Edge 为 4B，公开 model card/权重入口；base 支持多模态理解、生成及列出的动作 embodiment。DROID policy 另有实际后训练权重，动作仅支持 DROID 8D。 | 可研究离线 teacher、视觉未来预测或后续动作域适配。不是现成 UAV policy；camera-motion 条件也不是飞行动力学。不能把 base 的广泛输入能力与 DROID policy 的狭窄动作域混在一起。[base card](https://huggingface.co/nvidia/Cosmos3-Edge)，[DROID policy card](https://huggingface.co/nvidia/Cosmos3-Edge-Policy-DROID)。 |
| **Cosmos-Predict2.5-2B** | base 是 text + image/video → video；另有 `robot/action-cond` 的真实机器人动作条件 checkpoint，官方文件列表可见 4.25GB BF16 权重文件。 | 可作为动作条件视觉预测的离线基线；必须重训/适配飞机动作与观测，不可把 base 文本视频生成当 action-conditioned 控制。2.5 是仍可获得的旧路线，当前 NVIDIA 主线已是 Cosmos3。[model matrix](https://docs.nvidia.com/cosmos/latest/predict2.5/model_matrix.html)，[动作权重目录](https://huggingface.co/nvidia/Cosmos-Predict2.5-2B/tree/main/robot/action-cond)，[仓库](https://github.com/nvidia-cosmos/cosmos-predict2.5)。 |
| **Cosmos-Transfer2.5-2B** | 真实预训练 world-to-world 权重；以 depth、segmentation、edge、blurred RGB 控制视频生成外观视频。不是以飞机控制命令推动状态的模型。 | 未来用本项目实际 SDF/Blender/Gazebo 渲染的控制视频做离线外观扩充；输出须标 `model_generated`。生成物体可漂移/变形，不能未经复核沿用原 bbox/mask，更不能据此赋飞行动作或成功标签。[model card](https://huggingface.co/nvidia/Cosmos-Transfer2.5-2B)。 |
| **V-JEPA 2.1 ViT-B/16；V-JEPA 2 ViT-L/16** | Meta 官方提供 2.1 ViT-B 80M、384px checkpoint 与 PyTorch Hub entry；2 的 ViT-L 300M、256px 另有正式 HF 权重。它们提供视频表征，不是开箱即用的语言任务规划器/像素视频生成器。 | **优先验证的离线视觉历史 teacher**：冻结抽特征、训练小 probe/student，与现有视觉模型对比遮挡、重现及身份一致性。表征本身不等于学会搜索/返回。[官方模型列表](https://github.com/facebookresearch/vjepa2#models)，[V-JEPA 2 HF card](https://huggingface.co/facebook/vjepa2-vitl-fpc64-256)，[2.1 论文](https://arxiv.org/abs/2603.14482)。 |
| **V-JEPA 2-AC** | 有官方 `vjepa2-ac-vitg.pt` 与 encoder + action predictor 加载接口，基于 ViT-g 编码器；论文的图像目标规划实验是 DROID 后训练、Franka 机械臂。 | 真正的 latent action-conditioned world model，值得参考训练/规划接口；本项目需要自己的 UAV action-aligned trajectories，不能直接复用机械臂动作含义。论文“少量无标注机器人视频”并不意味着没有交互动作/状态数据。[官方 AC entry](https://github.com/facebookresearch/vjepa2#v-jepa-2-ac)，[论文](https://arxiv.org/abs/2506.09985)。 |
| **DIAMOND Atari / CSGO** | 作者发布 Atari world model + policy、CSGO world model 权重。CSGO 已是可按输入动作递归生成画面的交互模型，不只是 text-to-video。[作者仓库](https://github.com/eloialonso/diamond)，[实际权重目录](https://huggingface.co/eloialonso/diamond/tree/main)。 | 适合独立验证“动作输入 → 视频变化 → 长时滚动误差”的工具链或研究基线，不作为飞机部署候选。权重训练于游戏、动作/地图/观测域不匹配，画面有 3D 透视不等于有本项目飞机的物理状态。 |
| **DreamerV3** | 作者 repo 提供学习世界模型、imagined actor-critic 的算法与任务训练配置。本次未确认作者官方入口提供可跨任务/UAV直接复用的通用预训练 checkpoint；不能把算法代码称为现成飞行模型。 | 未来真正闭环采集与 RL 训练的架构候选，当前不是“加载已训练模型即可飞”。固定超参数跨任务表现不等于同一组权重跨任务；环境特定第三方 checkpoint 也需重新核实。[作者 repo](https://github.com/danijar/dreamerv3)，[论文](https://arxiv.org/abs/2301.04104)。 |

权重入口的具体身份：Meta 官方列表链接的 2.1 小模型文件为 `vjepa2_1_vitb_dist_vitG_384.pt`，AC 文件为 `vjepa2-ac-vitg.pt`；DIAMOND HF 下实际有 `atari_100k/`、`csgo/` 目录；Predict2.5 的 robot/action-cond 目录可列文件，但内容下载需接受访问条件。将来获得授权下载时应固定 upstream revision、完整文件清单、bytes/SHA、许可及预处理版本，不能只记录可漂移的 `main` 或模型名。[Meta 官方 README](https://raw.githubusercontent.com/facebookresearch/vjepa2/main/README.md)，[Predict2.5 文件/访问入口](https://huggingface.co/nvidia/Cosmos-Predict2.5-2B/tree/main/robot/action-cond)。

## RTX 4060 8GB 与 Jetson Orin Nano：硬件判断

**大生成模型不作为当前 8GB 本地训练/实时主线。** 官方配置证据如下，不能由 checkpoint 磁盘体积推出 GPU 可用显存：

- Predict2.5-2B 的官方 **720p/16FPS Video2World** profile 为 **32.54GB VRAM**；Transfer2.5-2B 为 **65.4GB VRAM**，分割控制 720p/16FPS/93帧的 H100 NVL 示例约 719 秒。这些是对应 profile，不是对所有裁剪/offload/量化方案的数学下限；本次没有验证低显存替代方案。[Predict card](https://huggingface.co/nvidia/Cosmos-Predict2.5-2B)，[Transfer 硬件矩阵](https://docs.nvidia.com/cosmos/2.1.0/transfer2.5/model_matrix.html)。
- Cosmos3 官方矩阵为 Super **64B**（H200/B200/GB200）、Nano **16B**（RTX PRO 6000/H100/B200）、Edge **4B**（AGX Orin/Thor/RTX PRO 6000）。**Cosmos3-Nano 是模型规格，不是 Jetson Orin Nano 支持声明。** Edge card 仅测试 Linux/BF16，没有本项目 Windows/RTX4060/Orin Nano 的端到端部署证据。4B 按 BF16 仅权重约 8GB 是理论估算，尚不含两塔实现细节、缓存与视频 activation，不能据此承诺 8GB fit。[官方矩阵](https://docs.nvidia.com/cosmos/latest/cosmos3/model_matrix.html)，[Edge card](https://huggingface.co/nvidia/Cosmos3-Edge)。
- 不能误写“Cosmos 所有推理都要几十 GB”：官方 Cosmos Policy recipe 的 **不带 model-based planning** 示例是 LIBERO 6.8GB、RoboCasa 8.9GB、ALOHA 6.0GB；带规划最小串行方案是 10GB。低显存数字只对应该 recipe/机器人任务，不能外推为 Predict base、Cosmos3 或本项目 UAV 可实时运行。相同 recipe 的训练示例使用 8–64 张 H100 80GB。[官方 policy recipe](https://github.com/nvidia-cosmos/cosmos-cookbook/blob/main/docs/recipes/post_training/predict2/cosmos_policy/post_training.md)。
- V-JEPA 官方没有给本项目 8GB 的保证。80M encoder 按 FP32/BF16 的纯参数体积约 **0.32/0.16GB**，300M 约 **1.2/0.6GB**，均为理论估算，不含 predictor、token 数、clip 长度和 activation。冻结、batch=1 的小模型抽特征是优先做实测的候选，而不是承诺全量训练能装入 8GB。V-JEPA 2 HF 示例取 64 帧，不能把其官方预处理任意改为现有 8-frame tensor 就声称等价。[Meta 参数/配置列表](https://github.com/facebookresearch/vjepa2#models)，[HF 用法](https://huggingface.co/facebook/vjepa2-vitl-fpc64-256)。
- DIAMOND CSGO 作者记录权重磁盘约 1.5GB、higher-quality 配置在 RTX3090 约 10FPS、训练 RTX4090 约 12 天；**没有给 RTX4060 8GB 或 Orin Nano 的显存/FPS 保证**。该小型交互 demo 可作为后续隔离试验，不值得为飞机能力迁移投入当前主线资源。[作者 CSGO README](https://raw.githubusercontent.com/eloialonso/diamond/csgo/README.md)。

Orin Nano Super 官方规格是 **8GB LPDDR5 系统内存**，不是 8GB 独享桌面 GPU 显存；OS、CPU、相机、现有 detector 和模型共享资源。官方 INT8 TOPS 不能直接换算这些 BF16 video/diffusion 模型的实时帧率。端侧优先保留已有轻量视觉链与小 history student；以后再逐项验证图导出、精度、峰值内存、温度、p95/p99 延迟和丢帧/过期输入，不把 AGX Orin 或 Thor benchmark 转述成 Nano 实测。[Orin Nano 官方规格](https://www.nvidia.com/en-us/autonomous-machines/embedded-systems/jetson-orin/nano-super-developer-kit/)。

## 接现有 OnboardAI 的最短一致路径

这些是下一实验设计，不是已经接入/已经训练的结果，也不替换当前视觉 warm-start candidate、原 detector 或 r33 默认策略。

1. **先做冻结视觉历史表征对照。** 用来源可重开、逐帧 RGB SHA 与 clip 身份/时间可审计的原 train clips，固定 V-JEPA checkpoint/revision/预处理，抽取冻结特征；只训练独立小 probe/student。val 用于选择，test 不进 optimizer；已用过的开发诊断 test 只能称独立新像素评估/已知回归，不能包装成从未见过的 blind benchmark。先比较真实遮挡后重捕、误检、身份持续性、距/角联合条件，而不是只报 teacher loss。不要为默认 telemetry、缺 HOME_POSITION 或身份不一致的旧 staging 填值后声称动作学习。
2. **离线生成扩充是另一条支线。** 将来使用 Transfer 的 depth/segmentation 控制前，补齐同源同时间控制帧；保存渲染 source hashes、生成模型 identity/seed、生成后 RGB hash及人工/独立重新核验的标注。原 split 和 scene/geometry holdout 保持隔离。生成器的 mask/世界坐标仅作离线监督，detector/tracker/policy 不读它；模型生成图不冒充原 Gazebo capture。
3. **实时契约必须另行版本化。** 当前 [OnboardModelInput](/D:/Projects/Multi-Detect/src/multidetect/autonomy/onboard_agent.py:489) 是 truth-free 40 维结构化输入，不含 RGB；[history runner](/D:/Projects/Multi-Detect/src/multidetect/autonomy/onboard_history_onnx.py:54) 的四输入是 `features[1,8,40]` 与 `valid_mask/dt_s/reset_mask[1,8]`，输出 `policy_output[1,20]`。不要将 V-JEPA embedding 偷塞进这 40 维。若实验显示增益，再设计明确 RGB-history/embedding 输入、预处理/hash/时间/有效性 metadata 和训练一致的新 versioned runner；仍经 [scoped provider](/D:/Projects/Multi-Detect/src/multidetect/autonomy/onboard_agent.py:720) → 同一 executor/[Mission adapter](/D:/Projects/Multi-Detect/src/multidetect/autonomy/onboard_adapter.py:354)。SIM 与 Jetson 用同一受审模型/契约，PC-only teacher 不冒充已部署端侧模型。
4. **动作世界模型要先有真正的动作轨迹。** 最低数据单元是同一 session/车辆/profile/home/clock 域内，`RGB_t + 实测 state_t + 已实际执行并有回执的 action_t + dt + RGB/state_(t+1)`；区分 model proposal、Mission install ACK 与实际控制生效时间。需要真实 3D simulator 交互/实测采集，而不是把 Blender 相机平移、遮挡计划或人写正确答案当飞机动作。observation-only 表征训练不要求编造动作标签；action-conditioned dynamics/RL 则不能没有 action/state/time 语义。
5. **慢模型不阻塞快安全链。** 重模型只做离线 teacher 或隔离异步低频候选，结果绑定 source observation、deadline/freshness、session/prompt/task/airframe/vehicle/home/clock scope；断流、回退时钟及 scope 变化 fail closed/显式 reset。先以 shadow replay 验证，再经原 proposal/task-state/safety/planner/Mission 事务；不直接发送飞控动作，不新增固定任务答案，不将 `search_target` alias 为 `car`。现有 [clip replay](/D:/Projects/Multi-Detect/scripts/replay_onboard_visual_clips.py:141) 的运行时输入只有 RGB+dt，truth 在离线评估侧，这个隔离边界继续保留。

建议排序：**小 V-JEPA 冻结 teacher/probe 对照 → 有预算时 Transfer 外观扩充 → 获取 UAV 同步交互数据后适配 action-conditioned latent model → 真实 Gazebo/PX4 闭环任务与安全验证**。DreamerV3 是最后阶段可比较的学习算法；Cosmos3 与 DIAMOND 的现成权重提供先验/研究起点，均不是省略本项目动作域和验证环节的通行证。本页不提升 `training_ready`、candidate promotion、HIL/REAL 或发布状态。
