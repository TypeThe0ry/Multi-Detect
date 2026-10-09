# 缺项预训练动作候选补充（2026-10-08，r3）

本次补三个此前研究未覆盖的 family：MiniCPM-Robot、MolmoAct2、Isaac GR00T。只读取官方网页、固定源码、小配置／许可和 HF 文件元数据；没有下载权重、接受 gated 条款、加载模型或训练。文件大小不是实际显存／本地权重验真。旧 r2 研究不改；本机仍以 root 实测 RTX 4060／8188 MiB 为容量条件。

状态纠正：按 root 本轮闭合观察，R2 已得到 16 份 plain MC、disarmed、on-ground、GPS／STANDBY context v2 资格。仍缺 planning capability／耗能预算、原点与净空证明和真实 Mission 行动数据；不能继续称“STANDBY 未通过”，也不能因此称可飞。

## 实际发布物与许可

| Family／固定 checkpoint | 当前官方权重元数据 | 许可与实质边界 |
| --- | --- | --- |
| [MiniCPM-RobotTrack](https://huggingface.co/openbmb/MiniCPM-RobotTrack)、[RobotManip](https://huggingface.co/openbmb/MiniCPM-RobotManip) | Track `80d32d0b091c3b340a1e4a6c78d441e8af19590e`，单文件 **931,428,452 B**；Manip `29cb257541e9f96f493abe21174f8892e5dffe3f`，单文件 **3,544,229,208 B**；均 public、非 gated | 卡与代码标 Apache-2.0；Track 是地面目标跟随，Manip 是机械臂。上游视觉资产须另外核许可与体积。 |
| [MolmoAct2](https://huggingface.co/allenai/MolmoAct2) | `e432d85f6e039edca44afb93c262f3084ab72a9c`，public、非 gated；5 shards 合 **21,768,955,912 B** | HF card 的 license 字段未填；官方固定仓库明确 model Apache-2.0。是多 embodiment 动作微调底座，不是无人机 policy。 |
| [GR00T-N1.7-3B](https://huggingface.co/nvidia/GR00T-N1.7-3B) | `2fc962b973bccdd5d8ce4f67cc63b264d6886495`，public、非 gated；2 shards 合 **6,910,499,416 B** | **官方来源冲突，许可门未清理**：fixed LICENSE 限非商业研究／评估；代码 Apache-2.0、当前宣传与模型卡不能替代它。 |

可复核固定 API：[Track](https://huggingface.co/api/models/openbmb/MiniCPM-RobotTrack/revision/80d32d0b091c3b340a1e4a6c78d441e8af19590e?blobs=true)、[Manip](https://huggingface.co/api/models/openbmb/MiniCPM-RobotManip/revision/29cb257541e9f96f493abe21174f8892e5dffe3f?blobs=true)、[MolmoAct2](https://huggingface.co/api/models/allenai/MolmoAct2/revision/e432d85f6e039edca44afb93c262f3084ab72a9c?blobs=true)、[GR00T](https://huggingface.co/api/models/nvidia/GR00T-N1.7-3B/revision/2fc962b973bccdd5d8ce4f67cc63b264d6886495?blobs=true)。API 返回 publisher LFS SHA，不是本机完整文件 SHA。

## 1. MiniCPM-Robot：最值得补的小型动作／历史接口研究

Track 接收语言和 DINOv3＋SigLIP 融合特征，输出 **8 个 `[x,y,yaw]` waypoint**；示例有 31 历史帧的 coarse tokens 与当前 fine tokens。它针对地面人物跟随，不支持升降，不等于完整三维导航／任意任务。0.93GB 只是主 checkpoint，不能漏掉视觉编码器。[官方 Track 卡](https://huggingface.co/openbmb/MiniCPM-RobotTrack)。

官方实际部署对象明确是 **Go2 EDU＋Orin NX 16GB＋D435i**，文档明确排除 Orin Nano／NX 8GB；DINOv3 另有 gated 许可。卡里的约 180ms、5+FPS 是该地面平台作者成绩，不是本机或无人机验收。优点是已有真正“真实 RGB→动作→后继图像”的地面回路，可研究历史压缩与 DAgger 难例采集；不能把 waypoint 经固定缩放后就称无人机学习控制。[固定部署条件](https://github.com/OpenBMB/MiniCPM-Robot/blob/f666593066641d2c0cef06bc17bfb1e954547674/MiniCPM-RobotTrack/docs/GO2_DEPLOYMENT.md)。

Manip 的 RGB／语言／embodiment 接口要求 80 维 state 容器，返回 **30×80** action chunk；轴、padding、归一化与单位需按 embodiment 验证，不是 80 个通用飞行轴。卡示例缺 state 会补零，本仓航空适配必须拒绝这一捷径。其 H100 BF16 120ms 只计单帧 model forward，明确排除任务 autoregressive decoding，不能当端到端成绩。[官方 Manip 卡](https://huggingface.co/openbmb/MiniCPM-RobotManip)。

**8GB 判断**：Manip 3.54GB 主权重有容量上的研究价值，但不能推出实际峰值可装；Track 全视觉栈也未有 8GB 支持证明。若另起小型航空 post-training 对照，优先核 Manip 的单相机／真实 state／动作 head 与完整依赖，而不是把地面 Track 升格成航空 teacher。隔离环境：Track 卡要求 Transformers 4.56–<5，Manip 要求 5.7.0；不得升级封存的当前环境。

## 2. MolmoAct2：离线数据／学习输出方法教师，不是 8GB 首跑

输入 RGB、语言和机器人状态；VLM 经逐层 KV 条件接 flow-matching 连续动作专家，带 `norm_stats.json`。官方有预训练、post-training、深度推理 variant、机器人微调 checkpoint 与 LeRobot v3 数据；DROID／YAM 发布 policy 是关节目标，不能改字段名成为 FRD 位移。深度预测也不是传感器测得深度。[模型卡](https://huggingface.co/allenai/MolmoAct2)、[固定模型／数据／许可说明](https://github.com/allenai/molmoact2/blob/6070080a20321b4f498ab30f28e1d09ac465edb7/README.md)。

**8GB 判断**：原发布 21.77GB 权重不能原样驻留；官方 YAM BF16 的“低于 16GB”不是低于 8GB。仓库服务器还记录 dtype 补丁和 CUDA graph 额外显存，不能以 auto device／INT4 猜测冒充已验路径。先研究公开 action expert、训练数据组织和失败 rollout 标注；若以后作离线教师，需另有资源、许可和任务适配准入。本轮不运行，不把其生成深度或解释当真实行动结果。

## 3. GR00T N1.7：真实动作 head，但有容量、依赖、许可三道门

RGB＋proprioception＋语言＋embodiment → continuous action chunk；固定 config horizon 40、state/action 最大容器 132，并非每种机器人有 132 个真实轴。官方对象为机械臂、双臂和人形；本次未见 UAV／PX4 embodiment。当前代码 main 的 GA 声明不能无条件覆盖 fixed 模型卡仍标 EA 的权重。[固定官方接口与硬件](https://github.com/NVIDIA/Isaac-GR00T/blob/d2b7e75b937e3ec9aa5dbc798f08b89692c49734/README.md)。

官方推理要求 **16GB+ VRAM**；reference 首次加载还依赖 gated Cosmos-Reason2-2B，不能因公开 GR00T 主文件约 6.91GB 就宣称完整 8GB 可装。量化／offload／CPU 不是本轮已验路径。其 relative EEF 表示可作动作结构研究，但不是已有无人机 FRD 训练。

特别保留许可原件差异：fixed [LICENSE](https://huggingface.co/nvidia/GR00T-N1.7-3B/raw/2fc962b973bccdd5d8ce4f67cc63b264d6886495/LICENSE) **4,063 B**，SHA `fdc54058d8b52bbb3b06c924326ff95a15687b08b15f92bc63adabc291638c89`，section 3.3 明确非商业研究／评估；README 的 Apache／NVIDIA Open Model License 与商业宣传不一致。暂列 `conflicting_primary_sources`，不标商业可用，不接受条款或下载来绕过澄清。

## 具体取舍与下一步

- **首个航空质量对照不变**：已固定的 OpenFly 有界量化离线对照；以上新 robot 候选不凭尺寸取代它。小 VLM 行为微调是另一路，不复用或重置旧预算。
- **新小型底座研究**：MiniCPM-RobotManip 优先做完整资产／输入语义审计，再决定独立零更新参考 smoke；必须先解决 state 与航空动作监督，不能称 zero-shot drone quality。Track 的历史与交互采集设计可单独参考。
- **离线教师／方法候选**：MolmoAct2 的开放训练链优先于 GR00T 的未清理许可链；只能生成待审辅助标签／解释，不能生成“真实”后继观测、认证、净空或 flight grant。
- **Gazebo 数据连接**：在已有 ground context 资格上补保守 planning capability／SIM 能量来源、同帧完整姿态／原点与净空，再通过既有安全 Mission 获取命令、ACK、真实前后 RGB／Depth／pose 和失败恢复。教师意图、实际运动、模型 raw 三份分别保存；只有真实回流是执行反馈。按完整 episode／场景划分，开发数据不重称 untouched test；模型输入排除 gold／未来帧，不用想象视频补实测。

本次并未新增能替代动力学／传感器回流的 world-model 验收。公开动作权重、机器人闭环、本机可加载、无人机任务质量、Jetson 实时和飞行权限仍是独立门；全部 promotion／flight／release 门不因此改变。
