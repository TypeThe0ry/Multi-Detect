# 预训练导航模型：本轮网络复查

后续三路并行已补实际PX4版本／17参数和原事件CRC解码，正常预飞与行动反馈仍未取得；
日志超限失败保留。官方SmolVLA／WorldVLN／AeroVLA页面本轮再次核查，未下载新权重或
升级环境。下一步首先形成合格航空状态／动作与真实回流；模型候选不当现成Jetson飞行包。
见[本轮接线与诊断](onboard-teacher-px4-diagnostics-20261008.md)。

## 最新并行复核：先获得可训练的行动回流，再做小型 VLA 对照

本轮主线程与三个子智能体并行推进真实 Gazebo 会话、遥测来源资格和独立审核，另查
官方模型与运行代码。真实观察R3现已并行保存6RGB／6Depth与5943遥测，最后九类消息
新鲜，但实际待命资格仍未通过，P95尾延迟也未达实时门；没有动作模型加载或新训练。
当前重点是查明预飞状态、完整表示缺测和取得可靠行动回流；数据链失败本身不证明
底座无价值，原记录保持封存。见
[真实观察进度](onboard-teacher-gazebo-observation-20261008.md)。

当前最值得独立对照的是 SmolVLA，但不能只看“450M／906MB”就直接升级现环境：

- 官方 [固定 LeRobot 依赖](https://github.com/huggingface/lerobot/blob/ca69a2068462a37f7cdcb74180927a2f863d2bf7/pyproject.toml)
  要求 Torch 2.7+ 与 Transformers 5.4+；应新建锁定的独立环境，不能覆盖封存的 Torch 2.6。
- [固定构造器](https://github.com/huggingface/lerobot/blob/ca69a2068462a37f7cdcb74180927a2f863d2bf7/src/lerobot/policies/smolvla/smolvlm_with_expert.py)
  在 `load_vlm_weights=true` 时还读取 VLM 底座和 processor。初始化的下载与内存预算不
  只有主 checkpoint；不能静默改配置来冒充官方同一模型。
- [官方硬件参考](https://huggingface.co/docs/lerobot/hardware_guide)的 BS8／AdamW SmolVLA
  训练范围约 10–16GB，是粗略参考，不是本机 8GB 或 Jetson 的通过证明。小批次／不同
  optimizer 的结果不能混成同一个实验。
- [模型卡](https://huggingface.co/lerobot/smolvla_base)的连续机器人动作不是本项目飞行
  语义。必须定义真实航空 state、FRD 动作、chunk 时间和独立 STOP；缺状态不能填零，
  缺相机不能复制一张图伪装多视角。

若需要替换对照，先单独锁定一次加载、最多八次动作输出、零 optimizer 更新、零飞行
发送的 smoke；先验收真实输入、完整依赖、host／VRAM 和超时，再判断是否开展航空
post-training。该 smoke 尚未执行，不重置此前训练消耗，也不改变默认 r33。

[MiniVLA](https://github.com/Stanford-ILIAD/openvla-mini)的 VQ／多图部署还有独立动作
码本和兼容缺口，当前不是更短迁移路线。
[WorldVLN](https://github.com/EmbodiedCity/WorldVLN.code)与
[公开权重](https://huggingface.co/EmbodiedCity/WorldVLN/tree/main)可研究航空闭环／离线
教师，但完整资产约 36.9GB，六维厘米／度输出不能直接作为本项目米制飞行指令。
[AeroVLA](https://github.com/XuPeng23/AeroVLA)航空 LoRA 还依赖 OpenVLA-7B；小 adapter
不是可独立运行的小模型。以上均未下载、加载、训练或获得飞行权限。

本次并行工程期间再次打开三项官方来源：[SmolVLA模型卡](https://huggingface.co/lerobot/smolvla_base)
明确它是需按具体用途微调的连续动作底座；[MiniVLA](https://github.com/Stanford-ILIAD/openvla-mini)
提供小主干、历史图像与动作chunk配置，但原任务是机器人操作；
[WorldVLN](https://github.com/EmbodiedCity/WorldVLN.code)提供按会话接收当前图像／首轮指令、
输出下一段动作再接真实后继图像的航空导航闭环，官方[权重页](https://huggingface.co/EmbodiedCity/WorldVLN)
也可读取。WorldVLN当前文档的六维输出为厘米／度，不能直接映射本项目米制FRD／弧度。
小VLA独立航空训练对照、世界模型先作离线教师仍是工程判断，不是部署成绩；
本次没有下载、加载或训练第二个模型，也没有证明Jetson容量或实时延迟。

最新根线程复查[SmolVLA官方模型卡](https://huggingface.co/lerobot/smolvla_base)、
[MiniVLA官方实现](https://github.com/Stanford-ILIAD/openvla-mini#minivla)与
[WorldVLN官方实现](https://github.com/EmbodiedCity/WorldVLN.code)：小型VLA具备已有权重和
动作学习入口，WorldVLN提供带历史会话的视觉→动作闭环，但这些接口都需要独立航空
语义适配与本机／Jetson测量。技术判断仍是小型VLA独立对照、大型世界模型先作方法或
离线教师；不是已经训练、适配或能飞。没有下载或运行第二个模型。
现有候选新R3加载失败见[本轮状态](onboard-navigation-live-feature-attempt-20261008.md)，
不改写下方旧R2实际8帧事实，也不以加载故障本身断言模型路线没有价值。

本轮真实Gazebo来源已接精确候选与保存动作头：8帧完成实际融合／读出，但全是STOP；
飞行命令0，导航质量仍未通过。公开替换路线不能据此跳过航空数据与独立验收。
公开[Qwen3-VL-2B](https://huggingface.co/Qwen/Qwen3-VL-2B-Instruct)、
[SmolVLA](https://huggingface.co/lerobot/smolvla_base)和
[WorldVLN](https://huggingface.co/EmbodiedCity/WorldVLN)官方页再次只读核验：SmolVLA
输出连续动作、定位为需按用途微调的底座；WorldVLN发布世界模型主干与动作decoder
权重。未下载或运行第二个模型，不拿公开权重可获得性当本项目的质量、Jetson或飞行验收。

## 当前决策：训练数值已稳定，先诊断接地与数据，公开动作模型保留对照

### 本轮并行研究新增结论：航空动作语义必须单独适配

[WorldVLN官方接口](https://github.com/EmbodiedCity/WorldVLN.code#autoregressive-io-contract-how-clients-talk-to-the-server)
接受RGB历史、首轮语言与会话身份；[论文A.9](https://arxiv.org/html/2605.15964v1#A9)
中的MoCap／GPS／LiDAR供低层执行与记录，不是模型输入。论文高层在地面服务器运行，
不能把出现Orin NX16GB解读为整套模型已机载。当前权重发布与代码许可也不能替代
完整权重许可、显存与本项目闭环验收。

[固定客户端源码](https://github.com/EmbodiedCity/WorldVLN.code/blob/3409b82a16c6606b270b48d7ea4c8f7d08e43c38/infer/client.py#L154)
输出`[dx,dy,dz,droll,dyaw,dpitch]`、cm／deg，存在world／body和转移顺序变体。
body的dx／dy为forward／right，但dz仍Z-up；`tsformer_latent`是4个macro再由客户端
拆成16子步，不能统一声称每次模型输出16动作。转换本项目FRD需要验证姿态、yaw
符号、动作时间及chunk／STOP含义，不能静默丢弃roll／pitch；这是待验证的适配判断。

[SmolVLA配置](https://huggingface.co/lerobot/smolvla_base/blob/main/config.json)
当前是6维state、三路RGB与语言到50步6维action，state／action采用MEAN_STD；内部32
是padding上限，不是32个真实动作轴。不能用零状态假装真实无人机pose，或直接假定
这些六数是FRD米／弧度／STOP。其官方定位仍是
[需按应用微调的小型底座](https://huggingface.co/docs/lerobot/smolvla)，不是已完成航空训练。

SmolVLA可在本地同进程调用策略；[异步服务器](https://huggingface.co/docs/lerobot/async)
是可选，同机localhost也可。并行研究实际只读官方metadata／固定配置HTTP200、native0
`66222e`，没有下载权重、接受gated协议、运行第二模型或改变训练环境。基于体量与迁移
成本，优先考虑SmolVLA独立航空适配对照，WorldVLN先做方法／离线教师研究；这是技术
判断，不是本机8GB、Jetson实时或任务能力已验证。现有小VLM仍须公平对照和新三维数据。

官方[WorldVLN权重页](https://huggingface.co/EmbodiedCity/WorldVLN)、
[SmolVLA模型页](https://huggingface.co/lerobot/smolvla_base)和
[AeroVLA仓库](https://github.com/XuPeng23/AeroVLA)再次读取，确认世界模型／动作decoder、
连续动作基础模型，以及依赖OpenVLA-7B的航空LoRA三种不同路线；未下载新模型。

旧800次动作头实验在59条开发样本上仅2次STOP正确、训练损失严重震荡，历史失败保留。
后继208条实际缓存与11头／2200更新稳定配方已闭合native0，最终loss均下降；但按完整
episode留出的合法动作仍只有46／208，joint5且均STOP。新122已纳入这轮训练，不把
输入验收或数值改善当飞行能力。下一步优先做原底座、旧warm与candidate相同输入的
公平对照，再补多场景、多prompt反事实、三维变尺度及受阻恢复的真实Gazebo任务数据。

连续改进：保留现有小VLM及共享输入、用学习型动作输出修复训练；迁移较小，但需新的
实际数据与未见场景验收。替换对照：SmolVLA小型VLA值得独立航空post-training；已有
连续动作接口不等于航空动作语义已匹配。WorldVLN更适合先研究闭环／离线教师，完整
权重更大，当前8GB和Jetson能否满足质量与延迟尚未测。若稳定训练与公平对照仍失败，
应比较或更换路线，不能仅为保留已有代码而坚持。每项实验单独锁预算，不由论文成绩晋升。

只读实际cache timing：old86融合forward中位0.1835s／P95 0.2355s，new122中位0.1699s／
P95 0.2007s；整条离线source＋encode＋feature中位分别15.5285s／0.5030s。两种耗时
不能混用，P95为nearest-rank；这不证明差异根因、直播端到端延迟或Jetson实时。下一步
同Actor真实Gazebo相机只观察运行应分开量测来源检查、编码、推理、帧龄和姿态锚定，
不能为了快取消身份／陈旧输入门，也不把离线监督与大文件审核搬成飞行循环。

2026-10-08根线程再次读取上述三个官方页面，独立研究复核相关接口；未下载或启动
第二个模型。现v1机体动作与旧v2地理NE任务输出的接线缺口另见
[执行缺口](onboard-navigation-execution-gap-20261007.md)，不能补造confidence或将STOP偷换为返航。

## 07:30补充：更小的公开动作模型与下一次训练决策

三路并行查验后，当前候选NEW59实际结果已闭合：动作16／59、数值0，验证15／17而
测试1／42；不能继续用格式正确或训练内成绩支持它飞行。下一次先做真实prefix特征的小
动作头对照，新的原始多路线数据须核RGB／姿态／标签后才训练。这个优先级是基于当前
质量与迁移成本的判断，不是小头已证明优越；现有路线也不是不可改变的硬约束。

| 可获得的对照 | 当前一手来源与体积 | 合适用途与缺口 |
| --- | --- | --- |
| [SmolVLA-450M](https://huggingface.co/lerobot/smolvla_base) | 官方完整主权重906,712,520B，Apache-2.0、non-gated；revision `5e8d12a6e2975b0e5e5fce7c8caf47c371d257b6` | 图像／指令／机器人状态→连续动作；可作小型独立VLA对照，但需无人机post-training，动作归一化、chunk与本项目FRD／STOP不直接兼容，8GB与Nano未实测。 |
| [MiniVLA官方实现](https://github.com/Stanford-ILIAD/openvla-mini)及[小底座](https://huggingface.co/Stanford-ILIAD/prism-qwen25-extra-dinosiglip-224px-0_5b) | 官方底座checkpoint2,630,986,501B，MIT、non-gated；revision `5cfd2cc6da00c06e0be7abf35d43ec792d8e9498` | 可对照更小底座＋同导航头；它是视觉语言预训练，不自带已验证无人机能力；双视觉编码器与输入接口须验证。 |
| [MiniVLA history2](https://huggingface.co/Stanford-ILIAD/minivla-history2-vq-libero90-prismatic) | 公开LIBERO动作checkpoint5,554,882,540B，MIT、non-gated | 有历史／动作训练，但针对机器人操作；文件大小不证明实际显存、optimizer状态或飞行适用性。 |
| [AeroVLA LoRA](https://huggingface.co/XuPeng23/AerialVLA) | adapter462,655,064B，revision `196f2f3253b69df6e90ac10b6ae041c7b3a9569e`；另需OpenVLA-7B完整底座15,082,600,824B | 无人机相关，但不是可单独跑的0.46GB模型，不能接到Qwen2B；量化＋LoRA兼容未测，landing不等于序列STOP。 |

上述HF metadata实际只读请求native0 `53fa11／23d6d4`，未下载权重、加载、训练或接受
访问条款。publisher revision／字节数不是本地SHA验收或显存测量。
[SmolVLA官方配置](https://github.com/huggingface/lerobot/blob/main/src/lerobot/policies/smolvla/configuration_smolvla.py)
说明action chunk和normalization；需要真实航空动作数据适配，不能把机器人状态／动作
字段改名就接PX4。AeroVLA adapter与代码许可也不代表整套底座许可；
[OpenVLA官方说明](https://github.com/openvla/openvla#pretrained-vlas)明确继承Llama-2条件，
许可仍需按完整模型组件核验。

[LightVLN论文](https://arxiv.org/html/2610.05024v1)的完整policy约1.276B，0.5B只是语言
主干；NX16GB实验使用stationary／disarmed飞机上的计算、AirSim SetPose虚拟位置回流。
这不是Nano或实际飞行证据。历史token压缩和8类学习头值得比较，但本次仍没有核到官方
导航checkpoint，不把“未核到”写成“肯定不存在”。

当前执行顺序：同底座冻结特征的小头诊断→验收真实多路线监督→必要时SmolVLA小型对照；
同时保留pristine base／旧warm／新candidate相同条件对照，避免只测已退步LoRA的表征。
三模型不得同时占用本机GPU；后续阶段各自锁定预算和来源，不根据已观察59挑最好
checkpoint，也不再把这59条开发数据称为untouched final test。没有新增飞行权限或晋升。

## 07:06补充：把小型机载路线和公开大模型分开比较

继续搜索官方论文／仓库，新增四项对照，不把演示或论文成绩当本项目验收。

| 对照 | 当前直接核到的内容 | 决策意义与未完成条件 |
| --- | --- | --- |
| [LightVLN论文](https://arxiv.org/abs/2610.05024v1) | 10月4日发布；0.5B语言主干、压缩视觉历史；作者报告Orin NX 16GB闭环HIL | 小型历史记忆值得比较，但不是全模型只有0.5B；本次没有核到官方可下载权重／实现入口，也不是Nano或实飞验证。 |
| [VLN on the Fly官方实现](https://github.com/peDrontonio/VLNontheFly) | INT4 Qwen-3.5-2B图像目标接地，机载规划；作者报告NX查询中位0.79秒 | 可参考小模型部署；验证依赖RGB-D、OptiTrack和有限目标，不代表未知环境搜索／全任务自治，许可仍TBD，不直接复制代码或替换飞控。 |
| [AeroVLA官方实现](https://github.com/XuPeng23/AeroVLA)与[LoRA](https://huggingface.co/XuPeng23/AerialVLA) | UAV连续控制训练／推理代码和LoRA发布；底座为OpenVLA-7B | 可以作有动作监督的研究对照；小LoRA不等于小完整模型，不能装到现有Qwen2B上直接运行，真实硬件仍列为后续工作。 |
| [AutoFly官方实现](https://github.com/xiaolousun/AutoFly-VLA) | RGB＋简短指令预测无人机速度；README发布状态中训练／验证／部署仍Preparing | 与自主探索目标相关，但本次没有验证可运行的完整预训练模型包；项目页Model链接读取失败不推断权重不存在。 |

当前选择：保留现有小底座作实测对照，完成59条跨场景诊断，接着验证同底座学习型动作
读出与真实多路线数据。小模型的历史缓存／视觉压缩值得独立实验，但收益和完整模型身份
尚未实测；不为兼容现有代码排除更好路线，也不因论文看起来更快就跳过迁移和质量比较。
WorldVLN保留为有公开权重的闭环导航对照／潜在离线教师，不把大型服务器推理当机载部署。

本补充只做网络查验和架构比较，没有下载新模型、加载第二个GPU模型、修改默认引用或
飞行执行权限。官方成绩均为作者报告，硬件、任务和评估条件不与本项目直接等同。

## 06:39补充复核：有公开权重，但还不是本项目可飞的模型

本轮再次实际读取官方GitHub与HF API；两个HF请求原生退出0（`969ec2`），revision、
非gated状态和分片大小与下方记录一致。3DG-VLN四分片合计16,584,414,544字节；
WorldVLN室外backbone合计35,073,577,024字节，另有1,059,195,322字节动作decoder和
738,674,708字节VAE。API里的publisher SHA不是本地下载验收，未下载或运行这些权重。

WorldVLN官方代码可核到连续真实画面驱动的动作闭环；Qwen-VLA官方仓库本次仍仅核到
Qwen3.5-4B＋1.15B动作decoder的说明，未核到权重入口。浏览工具直接读取3DG-VLN
模型页面失败，故其发布文件身份以实际API返回为依据，不把读取失败当不存在。

现有小模型完整267训练已正常结束，但训练内动作命中55→27／86、STOP仍0／3。
这使“继续堆相同数据更新”缺少依据。下一对照优先真实多场景动作数据与同底座学习型动作
输出；公开导航模型可独立作对照／教师，但没有证明其量化／offload能满足当前8GB设备和
Jetson延迟。不会因研究结果自动替换默认模型，或把服务器推理当全机载推理通过。

以下为前次元数据观察，保留原有范围与结论。

2026-10-08，在完成输入恢复检查后重新查询官方来源。固定版本网页的浏览工具读取受限，
没有把读取失败当成模型不存在；改用公开 Hugging Face API 取得权重元数据，实际请求退出 0
（执行记录 `14a0b2`）。只读取元数据，没有下载权重、接受访问协议或运行模型。

## 核到的权重

| 模型 | 本次官方 API 结果 | 发布权重体积 |
| --- | --- | ---: |
| [3DG-VLN](https://huggingface.co/fanfuxue/3DG-VLN) | revision `02e161fffd70fe8899a3b065dd41e45707221fcf`，`gated=false`，4 个完整 safetensors 分片 | 16,584,414,544 字节，约 16.58GB |
| [WorldVLN](https://huggingface.co/EmbodiedCity/WorldVLN) | revision `f1ef44f63ab12af1fc745e6665e3c659916310ad`，`gated=false`，室外与室内均有主干和动作 decoder | 一套主干 35,073,577,024 字节；室外另有动作 decoder 1,059,195,322 字节、VAE 738,674,708 字节 |

实际查询：[3DG-VLN 元数据](https://huggingface.co/api/models/fanfuxue/3DG-VLN?blobs=true)、
[WorldVLN 元数据](https://huggingface.co/api/models/EmbodiedCity/WorldVLN?blobs=true)。
这些是发布者元数据与文件体积，不是本地文件 SHA 验收、显存需求或下载成功。

## 与用户目标的关系

[WorldVLN 官方代码](https://github.com/EmbodiedCity/WorldVLN.code)明确实现“图像与任务→下一段动作→
执行→新真实图像→继续预测”的闭环，输出六维相对动作，使用厘米／度。这与实时行动目标相关，
但不能直接改字段名就接当前米制 FRD／弧度协议，也不是本仓库或 Jetson 的运行验收。

[Qwen-VLA 官方仓库](https://github.com/QwenLM/Qwen-VLA)公开说明 Qwen3.5-4B 与 1.15B 动作 decoder
的组合；本次搜索返回的官方 README／assets 清单没有提供可核对的权重下载。它可作为学习型
动作头的研究依据，不把方法说明当成已经拿到或运行的模型。

3DG-VLN 的任务边界、WorldVLN 的机载／服务器条件与许可问题仍参考
[已封存的深入查验](onboard-pretrained-navigation-model-options-20261008.md)。本次 API revision
与那份记录一致，没有改写旧研究回执。

当前机器实际为 RTX 4060、8GB。发布精度的模型权重体积明显较大；量化、offload、蒸馏或换
运行器能否满足本机／Jetson 的延迟与质量目标，均未实测。继续现有小模型训练，同时把已训练
导航模型保留为独立对照／可能的离线教师；是否更换路线要根据实测，不维护历史选择本身。

默认模型、执行权限、质量晋升、硬件与最终发布门没有变化。
