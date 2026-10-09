# UAV action-aligned models/data for OnboardAI — 2026-10-07

## 结论

下一轮最值得验证的是 **OpenFly 官方 train 数据 → 当前 Qwen3-VL 2B 的独立导航动作监督 adapter**，先做来源、因果顺序和动作单位审计，再决定 optimizer。公开 UAV 权重确实存在，但以下三个候选都不是当前 Qwen 底座的可直接合并权重；不能把加载 OpenVLA/Llama 系 adapter 称为“沿当前 Qwen 增量训练”。外部 teacher 的预测也不等于飞机已执行、专家示范或成功任务。

本次仅检索作者论文、作者仓库及其明确链接的官方 Hugging Face；读取小型代码/JSON 和文件 metadata，四个代表性权重 URL 的匿名 HTTP HEAD 均返回 200。没有下载权重或大数据包，没有安装、推理、训练、申请 gated 权限或上传数据。表中的 bytes/LFS SHA256 是发布端 metadata，不是下载后本地重算。没有本机 RTX 4060 8GB 或 Jetson Orin Nano 实测。

## 三个最相关候选

| 候选及确切发布身份 | 公开实物 / 许可 | 动作语义与数据域 | 本仓库适用角色 / 不直接接入的原因 |
| --- | --- | --- | --- |
| **OpenFly-Agent 7B**；`IPEC-COMMUNITY/openfly-agent-7b` | HF 非 gated，三个 safetensors 分片合计 **15,082,600,824 B**；代码/模型卡 MIT，数据卡 Apache-2.0；继承 OpenVLA/Llama-2 的许可不能仅凭 MIT 卡覆盖。 | 语言 + 当前图像 + 历史关键帧 → 离散导航幅度；原论文 100k 轨迹、18 场景，UE/AirSim、GTA、Google Earth、3DGS real-to-sim。是模拟/重建场景中的规划导航监督，不是本项目 PX4 执行动作日志。 | 优先复用 train RGB/指令/动作事实；可另作离线 teacher/baseline。不是 Qwen 权重；示例 loader 的未来帧与单位差异必须修正后才可复用。 |
| **AeroVLA**（原名 AerialVLA）；作者 HF 仍叫 `XuPeng23/AerialVLA` | 非 gated，`aero_vla/adapter_model.safetensors` **462,655,064 B**；LoRA rank 64，依赖 `openvla-7b`；代码/卡 Apache-2.0，基座继承限制及 TravelUAV 数据许可仍需独立处理。另有 **257,288,037 B** 的训练 JSON，不含完整 RGB。 | 前视 + 下视图像；99-bin `fwd/down/yaw` + `LAND`。官方 AirSim client 将前进/向下量解释为位置增量，yaw 为弧度增量，再转为 AirSim 旋转/速度 API；不是三个原始速度控制轴。评估域是 TravelUAV/AirSim，硬件部署仍列为待办。 | 可参考动作 token、response-only loss 和双视角数据格式。现 wrapper 从 `target_positions` 构造方向提示，不能原样成为无 truth 的任意 Prompt live policy；缺下视相机也不能补造画面。 |
| **TravelUAV / LLaMA-UAV**；`wangxiangyu0814/llama-uav-7b` + `traveluav-traj-model` | 两个非 gated HF 实物：LoRA `.bin`、projector、non-LoRA tensors，另 `model_5.pth` **1,982,506,481 B**。HF 卡缺失；作者模型子目录 code Apache-2.0，`DATA_LICENSE`/`WEIGHT_LICENSE` 为 **CC BY-NC 4.0**，不能写成无条件商用。 | Vicuna-7B/EVA-CLIP/Q-former + 层次轨迹 decoder：下一目标 pose，再细化 pose 序列。12,149 条人工在模拟器操作的轨迹，22 场景；多视角 RGB/depth/state。包含不同级别 assistant/oracle guidance，不能把 L1/L2 真值路径提示当机载可观测信息。 | 比 OpenFly 更接近动力学/人操示范，可作为后续研究级动作数据/teacher。许可、数据包体积、多相机和辅助提示限制，使它不是本轮最小路线。`.bin/.pth` 后续加载需独立安全审查，不能执行未经审核的 remote code。 |

表内来源：[OpenFly 官方仓库](https://github.com/SHAILAB-IPEC/OpenFly-Platform)、[OpenFly 论文](https://arxiv.org/abs/2502.18041)、[OpenFly 模型卡](https://huggingface.co/IPEC-COMMUNITY/openfly-agent-7b)、[OpenFly 数据卡](https://huggingface.co/datasets/IPEC-COMMUNITY/OpenFly)、[AeroVLA 官方仓库](https://github.com/XuPeng23/AeroVLA)、[AerialVLA 官方权重卡](https://huggingface.co/XuPeng23/AerialVLA)、[TravelUAV 官方仓库](https://github.com/buaa-colalab/TravelUAV)、[TravelUAV 模型准备说明](https://github.com/buaa-colalab/TravelUAV/blob/5730117a572cc80c6dc6c47bda0e4a6043e56c9e/Model/LLaMA-UAV/README.md)、[TravelUAV 论文](https://arxiv.org/abs/2410.07087)。

## 已核验的发布身份（以后下载必须重新核验）

下列 commit 与 LFS 值来自 2026-10-07 匿名官方 API；HEAD 只证明本次可以访问，不是权重内容/能力审核。代码 commit 与模型 revision 分别锁定，不能用可移动的 `main` 代替。

| 官方发布 | Revision / 可审计文件 |
| --- | --- |
| OpenFly code | `c075075497a7122bad82f5b76b9be926ad5a81b3` |
| OpenFly model | `21dcce235f1f2d40fc23c76998051abc5434cc99`；三个分片大小 `6948961960 / 6971232040 / 1162406824`；LFS SHA256 分别 `a40777127f28bb99a18107c8145057b9ca3762f8e65da5ed67a3bb8bd84b171d` / `e6b9e972ba51d6920403670dd54e0b305f34315a492ce874120792bbd7a8c7a3` / `4a33348f85fc6c25d5f4fef6abb4000593bb2bb5d9b99cfdd9d8869bb04e43c1` |
| OpenFly data | `a12316d56a4e35a32ad626fb725ed7089937a1c4`；`Annotation/train.json` 405,815,796 B，LFS SHA `1a4c5a5ed1561ea5b3b79b6c25da7648ce4085b7d8489d62022a9bd1244100df`；另有 `seen.json`、`unseen.json`。HF 显示全库约 2.97 TB，不应一次全拉。API 返回的 100000 文件列表不是 100000 个独立 episode 的本地复算。 |
| AeroVLA code/model | code `2c5ae0987a484ab92f00dd9d9ed493cb3e98e492`；HF `196f2f3253b69df6e90ac10b6ae041c7b3a9569e`；adapter LFS SHA `05b11cf66d2a7bba9af3fefc5afa9e9af4fe1f2a567ddeca4fca73192060b846`；训练 JSON LFS SHA `1c1e32787cedb096fe5df2511e5bf3cc81901b8a30d261df73ebcb800c868a67` |
| TravelUAV code/model | code `5730117a572cc80c6dc6c47bda0e4a6043e56c9e`；LLM adapter repo `6cd1a273ba26338a1be4eb93b43489ee0099fbce`，`adapter_model.bin` 319,970,957 B / LFS SHA `b6a7f8c7a4c43092b863b210a6f9f23b3076ce80c32c2f5f45eb6b108ea4c1ca`；`mm_projector.bin` 763,949,522 B、`non_lora_trainables.bin` 654,613,560 B 也必需，不能只下载 LoRA。 |
| TravelUAV trajectory/data | trajectory repo `583a3f6b9111fd80e76d6905eaa0f1793f3fd03f`，`model_5.pth` LFS SHA `f8607cb31434adc7973688c23e14c9e1f440ea2c2980c297c2b1df2e4c29b425`；dataset `faa8f2514156455ea7423464cc1295e6f92575cb`，约 483 GB 多卷 ZIP，未下载/解压/逐图重开。 |

官方 metadata 入口：[OpenFly 模型 API](https://huggingface.co/api/models/IPEC-COMMUNITY/openfly-agent-7b?blobs=true)、[OpenFly 数据 API](https://huggingface.co/api/datasets/IPEC-COMMUNITY/OpenFly?blobs=true)、[AerialVLA API](https://huggingface.co/api/models/XuPeng23/AerialVLA?blobs=true)、[LLaMA-UAV API](https://huggingface.co/api/models/wangxiangyu0814/llama-uav-7b?blobs=true)、[trajectory API](https://huggingface.co/api/models/wangxiangyu0814/traveluav-traj-model?blobs=true)、[TravelUAV 数据 API](https://huggingface.co/api/datasets/wangxiangyu0814/TravelUAV?blobs=true)。

许可不能混为一个字段：OpenVLA 作者明确提醒其模型继承 Llama-2 条款；AeroVLA 的训练 JSON 源自 TravelUAV，发布在 Apache 标记的模型库不自动解决原数据范围。TravelUAV 子目录的非商业文件不等于已明确覆盖所有地图资产、第三方基座和根数据包。此处是读取到的条款事实与准入缺口，不是法律授权结论。[OpenVLA 官方许可说明](https://github.com/openvla/openvla#pretrained-vlas)、[TravelUAV DATA_LICENSE](https://github.com/buaa-colalab/TravelUAV/blob/5730117a572cc80c6dc6c47bda0e4a6043e56c9e/Model/LLaMA-UAV/DATA_LICENSE)、[TravelUAV WEIGHT_LICENSE](https://github.com/buaa-colalab/TravelUAV/blob/5730117a572cc80c6dc6c47bda0e4a6043e56c9e/Model/LLaMA-UAV/WEIGHT_LICENSE)。

## 会改变接入方案的具体风险

1. **OpenFly 不可照抄历史/动作 adapter。** 锁定代码的 `vln_dataset_builder.py` 中 `idx == 1` 用 `image_array[-1]`/`[-2]`，其它分支也会取尾帧；关键点还从完整未来动作序列扫描。这些分支可能让“历史”看见未来。该示例还人为复制尾图、把最后两步改成 stop，不能把这些转换结果冒充真实新传感器样本/执行事实。论文 action 定义是上/下 3 m、左/右旋转 30°，代码 action dictionary 却写 2 与 15，并额外有侧移维度；需要按固定 revision 的原 pose 增量、动作值、yaw 确认真实单位，不能挑一套数值直接发飞机。[锁定示例 builder](https://github.com/SHAILAB-IPEC/OpenFly-Platform/blob/c075075497a7122bad82f5b76b9be926ad5a81b3/train/dataset_builder/vln/vln_dataset_builder.py#L69)、[论文动作定义](https://arxiv.org/html/2502.18041v3#S5.SS3.SSS3)。

2. **OpenFly 有动作监督，但未证明有可用 wall/sensor-clock dt。** 实际重开官方 12,010 B `pose.jsonl` 样本，看到 `action.imageid/pos/type/value/yaw` 与 `aim_landmark`，文件没有 `timestamp/time/dt/clock/epoch` 字段。图片文件名日期不是已标定 sensor dt；该样本不是全库审计结论。先做 ordered-step 监督而非速度/动力学 next-state 学习；`aim_landmark.position` 等 truth 保留离线，不输入模型。若以后要 history runner 的秒级 `dt_s`，必须另外取得真实来源，不能填默认值。[本次重开的小型原 pose 文件](https://huggingface.co/datasets/IPEC-COMMUNITY/OpenFly/blob/a12316d56a4e35a32ad626fb725ed7089937a1c4/Image/env_airsim_16/astar_data/high_average/2025-01-18_23-38-59_526011/pose.jsonl)。

3. **AeroVLA 不能按宣称直接当无 oracle teacher。** 锁定 wrapper `prepare_inputs` 实际接收 `target_positions[i]`，用目标位置减当前位置生成方向提示，而且截取既定英文模板，不保留任意原 Prompt。未经证实的目标坐标不能从 scenario truth 填进本项目 runtime。它的 action parser 还宽松提取数字并在失败时返回零量，零量又能触发 landing；这是不能移植到本项目 strict JSON/no-repair/无执行权限边界的行为。仅复用离线监督设计，运行时继续严格拒绝 malformed 输出。[锁定 wrapper](https://github.com/XuPeng23/AeroVLA/blob/2c5ae0987a484ab92f00dd9d9ed493cb3e98e492/src/model_wrapper/aerovla_wrapper_ui.py#L78)、[动作到 AirSim 的转换](https://github.com/XuPeng23/AeroVLA/blob/2c5ae0987a484ab92f00dd9d9ed493cb3e98e492/airsim_plugin/AirVLNSimulatorClientTool_AeroVLA.py#L312)。

4. **TravelUAV 的 human-in-simulator ≠ live PX4 动作 receipt。** 作者描述人工先记录姿态轨迹、随后采集图像；assistant L1/L2 使用 GT 路径，DAgger backtracking 会回退碰撞前状态。此类数据可训练导航，但不能直接标成一次不可逆真实飞行的 executed→nextRGB episode，也不能证明本项目 Mission 事务成功。优先保留其原 Train/Test Seen/Unseen Map/Unseen Object，不能按帧随机切分。[论文采集/assistant/DAgger](https://arxiv.org/html/2410.07087v2#S4)、[官方数据预处理](https://github.com/buaa-colalab/TravelUAV/blob/5730117a572cc80c6dc6c47bda0e4a6043e56c9e/Model/LLaMA-UAV/tools/generate_merged_json.py)。

## RTX 4060 8GB / Jetson Orin Nano：能够与不能够推断的事

OpenFly 的 BF16 发布 tensors 已约 15.08 GB，仅权重便超过 8GB；AeroVLA 的 0.46 GB adapter 不包含 OpenVLA 7B 基座。TravelUAV 同样另需 Vicuna7B、视觉 encoder 等。因而这些原生 BF16/FP16 完整 GPU 路径不是当前 8GB 卡的直接方案。4-bit、CPU offload 或远程 teacher 只是待验证选项，不能由参数量推定可用显存、吞吐或 deadline。AeroVLA 官方 trainer 明确 native BF16；OpenFly 官方训练 launcher 是 8 GPU，这些配置不是“最小硬件要求”。[AeroVLA trainer](https://github.com/XuPeng23/AeroVLA/blob/2c5ae0987a484ab92f00dd9d9ed493cb3e98e492/src/train_aerovla.py)、[OpenFly launcher](https://github.com/SHAILAB-IPEC/OpenFly-Platform/blob/c075075497a7122bad82f5b76b9be926ad5a81b3/train/train.sh)。

这三个具体 checkpoint 均未在本次所查作者来源中提供 Orin Nano 端侧延迟/显存/质量证据。OpenFly 论文还明确指出当前大模型不适合 UAV 实时部署。不要把 AirSim 服务、作者的模拟器吞吐或其它型号 Jetson 结果借给当前 Nano。后续离线数据变换可 CPU 进行；沿现有 Qwen 2B 做有动作监督的小规模 adapter，硬件/optimizer 可行性也必须由新预算和实测验证，不能沿用 grounding 原生训练成功就宣称导航动作已学会。[OpenFly limitations](https://arxiv.org/html/2502.18041v3#S7)。

## 最值得下一轮执行的一条路线

**限定 OpenFly train 的 train-only 外部导航监督试验，不立即换部署模型，也不先加载 7B teacher。**

1. 固定上述 dataset/code revision，先小批导入完整 train episode（建议首批 64 个是开发预算，不是现成已审核数量），保留原 split。完整保存原 instruction、step/image IDs、原动作、pose/yaw、原 RGB bytes/SHA 与来源许可；`seen/unseen` 不进入 optimizer 或选样。为新 dev validation 按整个 episode/scene 分组从 train 留出，明确不是新的 blind benchmark。不要下载全 2.97 TB；先生成所需 RGB 的 allowlist，逐文件限量读取并重算发布 LFS/Git 身份。

2. 使用自己审核的因果 adapter：每个训练输入只取 current 与已发生的前帧，禁止负索引/future keyframe；不读目标绝对坐标、完整未来路线、mask/amodal、oracle direction。先训练 **原语言 + RGB → 可审计导航 primitive/位移幅度**，label 明记 `external_planned_navigation_supervision`，不是动作已执行。用相邻 pose/yaw 复算单位与方向，单位不一致样本隔离；无真实 dt 不做速度、飞行状态或动力学损失。这已经比五字段 grounding 多了实际动作监督，仍不能称任务全自主成功。

3. 基于当前 Qwen 底座建立新独立 adapter/小动作 head；当前失败 grounding adapter 可保留为对照，不自动晋升，也不将它当已合格飞行 warmstart。OpenFly/OpenVLA/LLaMA-UAV 的 LoRA tensors 不合并进 Qwen。外部 7B teacher 留到有独立硬件预算后，只作为带来源的 proposed soft labels/baseline，与原 planned action 分开。

4. 先离线衡量动作方向/幅度、stop 混淆、未见场景、历史消融、非有限/格式拒绝和 grounding 遗忘，不能只看 JSON 合法率。`STOP` 不是 LAND/任务 completed；本仓库没有 yaw-only typed flight action，不能把转头 label 硬塞成移动/降落。需要新增版本化导航 intent/head 或明确拒绝不支持的动作，不能用手写任务答案代替学习。

5. 以后接现有 shared loop 时，body-frame 动作到 `OnboardFlightPlan.north_offset_m/east_offset_m/altitude_m` 必须使用 fresh measured pose/yaw、相机标定和 source→current 重投影；缺测就不执行。现 [runtime v2 parser](D:/Projects/Multi-Detect/src/multidetect/autonomy/onboard_vlm_runtime_contract.py:402) 仍禁止无校准 `reference_id` 和未被 compiler 消费的 `speed_mps/duration_s`。[live request](D:/Projects/Multi-Detect/src/multidetect/autonomy/onboard_live_vlm_contract.py:293) 与 [episode recorder](D:/Projects/Multi-Detect/src/multidetect/autonomy/onboard_live_episode.py:106) 保留真实 RGB/state/原 Prompt/receipt；[continuation compiler](D:/Projects/Multi-Detect/src/multidetect/autonomy/onboard_continuation.py:617) 与既有 PX4 pause/upload/readback/commit/resume 安全事务不被外部 action code 绕过。原始 proposed、accepted、received、executed、completed 分开，实际后继 RGB/state 才能形成后续本域行为监督。

目前没有证据支持把上述模拟/重建数据或公开模型直接视为 Gazebo/PX4 闭环、实机安全、Jetson 部署通过，或将当前候选设置为 training-ready/promoted/release。下一步是可审核动作语料准入，不是继续重复 formatter 训练，也不是本文件已经完成导航训练。
