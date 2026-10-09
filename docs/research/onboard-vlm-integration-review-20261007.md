# OnboardAI：VLM、记忆与结构化飞行意图接入审查

审查日期：2026-10-07。本文是开发架构建议，不是部署、训练完成或飞行验收证据。仅新增本研究文件；未修改现有核心、训练入口、模型引用或主进度文档，未运行 VLM、容器或飞控。

## 结论

目标应是 **Prompt + 摄像头/telemetry + 已执行任务记忆 → 动态下一步意图 → 原 planner/PX4 执行链**。建议先做“轻量预训练 VLM + 外置有证据的记忆 + 结构化意图”的独立 development/shadow 候选，复用现有执行边界；之后根据离线证据，将其有用能力增量训练/蒸馏到共享 history policy。不要为了保留 40 维输入而把语言、图像和持续任务状态再次压成固定颜色规则。

现有 r33 的 15,188 个参数和 20 维 task/flight/regression heads 可以在增量路线中精确迁移，但不能声称这些权重可直接装入架构不同的 VLM。r4 是单类 `search_target` 辅助检测器训练；即使静态召回改善，也不证明 Prompt 理解、时序策略学习、多任务自主推理或飞行成功。

## 1. 可直接复用的仓库边界

| 当前实现与直接证据 | 保留的价值 | 必须正视的限制 |
|---|---|---|
| [PromptEnvelope](D:/Projects/Multi-Detect/src/multidetect/autonomy/contracts.py:219) | Prompt 文本、operator/aircraft 身份及 canonical hash 已存在 | 文本尚未进入当前 policy model input |
| [OnnxRawYoloDetector](D:/Projects/Multi-Detect/src/multidetect/vision.py:867)、[UnifiedTargetPool](D:/Projects/Multi-Detect/src/multidetect/unified_tracking.py:629)、[OnboardFramePerceptionAdapter](D:/Projects/Multi-Detect/src/multidetect/autonomy/perception_adapter.py:90) | 真实 RGB 检测、短期 track、depth/pose 到 observation/event 的边界 | 检测和同 ID 恢复不是任务决策；当前辅助模型只有一个类别，adapter 的类/颜色 vocabulary 也有限 |
| [OnboardModelInput.feature_vector](D:/Projects/Multi-Detect/src/multidetect/autonomy/onboard_agent.py:513) | 已验证的 40 维规范化遥测/事件基底，可保留为兼容分支 | 无原始 RGB、图像 embedding、Prompt embedding、完整目标/进度记忆 |
| [HistoryPolicyNet](D:/Projects/Multi-Detect/scripts/train_onboard_history_model.py:168)、[initialize_history_from_parent](D:/Projects/Multi-Detect/scripts/train_onboard_history_model.py:223) | 原 body/heads 严格迁移；8 帧 full-window Conv1d residual，当前 temporal width=64 | 零 residual scaffold 保持父输出，不是学会记忆；窗口长度不是永久设计上限 |
| [OnboardInferenceScope](D:/Projects/Multi-Detect/src/multidetect/autonomy/onboard_history.py:39)、[OnboardHistoryOnnxModelRunner](D:/Projects/Multi-Detect/src/multidetect/autonomy/onboard_history_onnx.py:43) | session/Prompt/airframe/home/task/clock 身份隔离、dt/reset、成功后提交历史 | 短期感知 buffer 不是长期任务 ledger；换 task 重置感知历史不能抹去整个任务的已执行证据 |
| [OnboardModelRunner / ModelBackedDecisionProvider](D:/Projects/Multi-Detect/src/multidetect/autonomy/onboard_agent.py:637)、[OnboardModelOutput.from_mapping](D:/Projects/Multi-Detect/src/multidetect/autonomy/onboard_agent.py:595) | 可替换模型 backend；typed、有限值、受限字段输出；model SHA/latency provenance | 当前 infer 入参只有 context/event；不能靠 runner 偷读全局最新图像冒充已绑定输入 |
| [OnboardFlightPlan](D:/Projects/Multi-Detect/src/multidetect/autonomy/onboard_agent.py:316)、[compile_model_flight_plan](D:/Projects/Multi-Detect/src/multidetect/autonomy/onboard_navigation.py:43) | bounded N/E 意图 → WGS-84 waypoint → 原能量/围栏/高度 planner/compiler | 当前 model-navigation seam 仅启用 multirotor；不能外推 fixed-wing/VTOL 或把 image bbox 当米制坐标 |
| [OnboardMissionAdapter._bind_model_flight_plan](D:/Projects/Multi-Detect/src/multidetect/autonomy/onboard_adapter.py:632) | 独立重编译、任务/plan/mission/wire hash 核对及 transaction callback | VLM 不得获得 transport callback、arming 或 MAVLink 权限 |
| [Px4MissionTransaction](D:/Projects/Multi-Detect/src/multidetect/px4_mission.py:2092)、[Mission replacement transaction](D:/Projects/Multi-Detect/src/multidetect/mission_replacement.py:1) | upload/readback/commit/start permit；替换时 pause/upload/readback/commit/resume 有独立状态链 | 编译成功不等于外部执行成功，更不等于物理安全/实飞验收 |

## 2. 物理安全与历史语义必须拆开

必须保留：operator/aircraft/session 权限和身份、输入 freshness、GPS/标定/真实障碍证据、坐标系及不确定性、airframe/geofence/energy/altitude envelope、planner revalidation、任务/线缆 hash 与 PX4 transaction/permit。模型的自报 confidence 或生成的解释不能替代这些证据。

不应作为永久“智能边界”的历史实现：

- [PromptTaskResolver.resolve](D:/Projects/Multi-Detect/src/multidetect/autonomy/prompt_resolution.py:183) 按注册 `prompt_id` 查模板，未理解任意 Prompt.text。这是当前安全 catalog 路径，不是自然语言能力。未来可学习 Prompt → typed goal/capabilities/constraints，但 operator 授权和物理 runtime 不由模型伪造。
- [task phase table](D:/Projects/Multi-Detect/src/multidetect/autonomy/onboard_task_state.py:152) 与 [ONNX phase compatibility](D:/Projects/Multi-Detect/src/multidetect/autonomy/onboard_onnx.py:79) 固定了搜索、匹配、记录、返航、报告生命周期。它们同时包含“真实观测/到达后才能记账”的必要约束与单一任务流程的历史假定；后者应迁移成每项任务的声明式完成条件/能力，而非强迫所有任务走同一链。
- `task_action` 目前既涉及提出下一步又涉及 receipt/到达状态。后续版本应明确区分 proposed intent、accepted/committed intent、observed completion，不能把“VLM 说已到达/已记录”当完成证据。

本轮不移除任何现有 gate。要改的是独立开发输入/语义层，再经测试选择明确的版本化接入，不能直接绕过旧核心。

## 3. 两条路线

| 维度 | A：连续增量共享 history policy | B：轻量 VLM + 外置记忆 + typed intent |
|---|---|---|
| 输入 | 保留 40 维支路，增加 Prompt/goal、视觉 embedding、执行记忆与 missingness 分支 | VLM 原生读取 Prompt、受限采样的真实 RGB/视频，配同时间 telemetry/目标轨迹和任务 ledger |
| 权重 | 精确复用 r33 body/heads，新增融合/temporal/residual；有监督地解冻 | 使用预训练 VLM 的图文能力；r33 保留为 baseline/后续蒸馏 student，不能假称 VLM 继承 r33 参数 |
| 训练 | 需要足量跨任务、跨观测条件且已审核的 action episodes；无动作数据时只做表示目标 | 先 zero-shot shadow 检查真实能力，再以已审核的 grounded intent 数据做 LoRA/adapter；视觉描述数据与动作数据分别准入 |
| 成本 | 小模型可预测延迟、ONNX/CPU 路径熟悉；新模态输入/export 需新版本 | 更快得到语言/图像能力，但 token/KV/视觉编码开销、幻觉、JSON失败和延迟风险明显 |
| 主要失败 | 不加入真实图文输入就只能拟合标量；缺动作数据时新增分支不会变成任务策略 | 会描述画面却不完成任务、把计划当已执行、跨对象/任务身份串记忆、幻觉距离/完成状态 |
| 近期选择 | 作为长期轻量化、增量 fine-tune/蒸馏路线 | 推荐作为下一轮最小可验证的语义候选，严格 shadow、无 physical output |

路线 B 不是“另建规则模型”。模型负责语义选择，确定性代码负责契约、来源、单位、权限和可执行性。当前几何/安全拒绝仍可返回 HOLD，但不能偷换成固定事件→RETURN 表来假装 VLM 推理成功。

## 4. 最小接入包：版本化输入、真实记忆、同一输出 seam

以下是拟议开发契约，尚未实现或注册为当前模型接口：

- **输入包**绑定 PromptEnvelope/hash、当前 ContextSnapshot/hash、RGB bytes/hash/frame_id/实际尺寸/采样时间、同帧 pose/depth/calibration、已观测 track/reference、dt、missingness、OnboardInferenceScope，以及前序已提交 intent/receipt 的 ledger hash。原始 Prompt 必须实际传入，不只给 prompt_id。
- **双层记忆**：task-scoped 短感知窗口保留真实时序；mission-scoped ledger 保留子任务、未完成目标、已接受动作和实际完成证据。换 task 重置前者，但后者保留跨任务进度；换 Prompt/飞机/session 则明确新任务边界。文本摘要只能索引证据，不可替代 receipt。
- **输出**先用现有 OnboardModelOutput/OnboardFlightPlan 的明确子集，通过严格 JSON/未知字段/NaN/单位/范围校验。开发 receipt 另记模型/tokenizer/processor/quantization hash、输入与记忆 hash、实际 latency、拒绝原因、引用的 observation ID；不能把 receipt 扩展字段直接塞入当前严格 decoder。
- **Grounding**：VLM 可选择“哪个已观测对象、是否需要观察/跟随/报告”，但 NED/GPS/距离来自真实 depth/标定/pose。VLM 自报 box 或“在左侧 20 米”不是度量测量。未知目标引用、不明障碍/坐标或过期帧不能形成移动许可。
- **执行**：shadow 阶段只 parse/validate/compile dry-run，不配置真实 installer、transaction transport 或 safety-mode 发送回调；以 proposals 和 rejection receipt 作为输出。复用原 planner 不等于启用实飞。

拟议链路：

```text
Prompt + 实际 RGB/telemetry + 证据化记忆
                 ↓
       独立 slow VLM semantic worker
                 ↓
   strict typed intent + evidence/source freshness
                 ↓
   原 planner / safety envelope / Mission compiler
                 ↓
      原 PX4 transaction / operator permit

快 telemetry/安全检查独立持续运行；VLM 无 transport/arming/actuator 权限。
```

[ModelBackedDecisionProvider._propose](D:/Projects/Multi-Detect/src/multidetect/autonomy/onboard_agent.py:753) 当前同步执行 infer，只在返回后测耗时；[executor proposal path](D:/Projects/Multi-Detect/src/multidetect/autonomy/onboard_agent.py:1248) 会等待 provider。轻量 VLM 也不能未经验证直接放入这个同步快循环。开发候选需 bounded worker、真实 deadline/cancellation、过期结果拒绝、队列/采样背压、最大图像与 token 预算；不能让长生成延迟 GPS/障碍安全响应。

## 5. 现成 VLM / 世界模型应该做什么

完整候选、运行边界及 license 比较见同轮 [VLM 候选审查](D:/Projects/Multi-Detect/docs/research/onboard-vlm-candidates-20261007.md)。模型型号不是永久架构约束；文本 agent 的 Jetson 实测不能替代图像/视频任务的实测。使用教师输出训练其他模型之前，还需逐项检查相应权重和输出 license/归属条件，不假定所有 Cosmos 版本相同或无条件允许蒸馏。

[Qwen3-VL-2B-Instruct 官方 model card](https://huggingface.co/Qwen/Qwen3-VL-2B-Instruct) 标明 Apache-2.0，具备图文、视频时间对齐和空间 grounding 能力，可作为第一项 Prompt+实际画面的语义候选。它适合目标语义匹配、场景描述、观察选择与 typed 下一步建议；这些能力描述不等于米制定位或无人机控制保证。权重、processor 和运行库要固定具体 revision，不安装“latest”后直接记为可复现。

[SmolVLM2-500M-Video-Instruct 官方 model card](https://huggingface.co/HuggingFaceTB/SmolVLM2-500M-Video-Instruct) 是 Apache-2.0 的图像/视频候选，官方标注 English；可作低资源对照，但必须测中文 Prompt、grounding 和时序任务，不假定更小就足够可靠。本文未实测任一模型的本机/Jetson 显存、p95/p99 视觉延迟或量化精度。

世界模型要区分三个角色：

1. **有证据的状态/scene model**：现有 depth/pose/geolocation/tracks + 任务 ledger，记录实际看见的世界。这是最小记忆层，不要求先训练视频生成模型。
2. **视频/物理推理 VLM**：解释已观测的变化、遮挡和事件，不能因为名称含“physical”就当已校准 flight dynamics。
3. **action-conditioned predictive model**：预测候选动作后的场景/状态，适合离线反事实、数据增强与风险假设。要成为 planner 的物理依据，必须额外拥有受绑定的真实/物理模拟 action-state transitions、误差和不确定性验收。生成视频只能标 synthetic，不能冒充 observed completion、Gazebo capture、距离真值或动作专家标签。

当前最短路线优先 1 + VLM；不先以昂贵世界生成模型替代 PX4 dynamics、几何或安全编译器。

官方现成实现也支持这种分工：[Cosmos3-Edge model card](https://huggingface.co/nvidia/Cosmos3-Edge) 将 text/image/video→text 的 Reasoner 与生成 video/action 的 Generator 分开；Generator action 只支持列出的 embodiment，未列 PX4/UAV。官方仅测试 Linux/BF16，[model matrix](https://docs.nvidia.com/cosmos/latest/cosmos3/model_matrix.html) 的 Edge 推荐包括 AGX Orin/Thor 等，不能从“Edge”名称推断 Orin Nano 8GB 实时可用。[Meta V-JEPA 官方仓库](https://github.com/facebookresearch/vjepa2) 提供视频 encoder/checkpoints，可考虑离线表征 teacher；action-conditioned robotic planning 不能未经适配与验证外推为飞机控制。这里列的是能力边界，不是当前仓库已接入这些模型的声明。

各世界模型的 embodiment、训练与 hardware 边界另见同轮 [世界模型候选审查](D:/Projects/Multi-Detect/docs/research/onboard-world-model-candidates-20261007.md)；本文不把某个生成配置的显存需求泛化成整个 Cosmos 家族的需求。

## 6. 数据与有效收益门

[现有 trajectory loader](D:/Projects/Multi-Detect/scripts/onboard_sequence_windows.py:138) 复用严格 transition/parser，要求独立 review、动作标签、planner proof 与真实来源绑定。[3D staging builder](D:/Projects/Multi-Detect/scripts/build_onboard_trajectory_staging.py:573) 明确写 null proposed labels；这些行不是专家动作训练集，planned clip condition 更不是 action。

开发可以继续，不必把所有训练绑在 Gazebo/Docker 上：

- 使用显式 Blender 来源的实际 RGB 与像素标注，先做视觉 grounding/描述/目标消歧数据；任何合成目标答案保留生成来源，不更改动作 readiness。它支持感知预训练，不证明 physical dynamics。
- Prompt+图像+任务状态→下一步意图的 SFT/LoRA 数据需可审查的任务目标、实际观察及动作理由/适用范围。教师 VLM 输出保留 teacher-generated 状态，经审核才用于有监督动作；原 MLP、计划标签或 VLM 自己的答案都不能自动成为 gold action。
- 正式动作 fine-tune 继续使用 reviewed episodes。无这些数据时允许 observation 表示学习/grounding 或 shadow zero-shot，报告相应范围，不能登记为 action learning。

有效评估不能只有 JSON parse pass 或静态检测召回：

- **同画面换目标 Prompt**：不局限红绿车；改变目标类型、观察/跟随/巡查/报告要求、禁止事项与未完成条件，验证意图真的随 Prompt 改变。
- **同 Prompt 换观测/历史**：目标不存在、遮挡重现、多个相似对象、相机运动、遥测退化、子任务完成后切换；检验仅当前帧/T1、完整历史、打乱有效历史、reset/no-memory 对照。Prompt-only、image-only 与 r33 是独立 baseline。
- **完成证据**：区分 proposed/accepted/executed/completed；禁止提前报告/假记录、重复执行和跨任务 ID 串用；同 ID 恢复按实际遮挡前后的已有 track 比较，新建 ID 不能算恢复。
- **拒绝与安全**：timeout、非法 JSON、未知 ID、图像中的指令注入、坐标单位错误、GPS/depth/obstacle 缺失、过期帧、airframe/Prompt/scope 变更均不得获得执行权。
- **分区与指标**：先按物理 episode/world/geometry/recording 分独立 train/evaluation/test，再造窗口；不可拆重叠帧。分别报告 semantic goal satisfaction、grounding error、history benefit、premature completion、planner acceptance/rejection、unsafe proposal 与 latency/memory。固定预算与阈值；development holdout 不称 untouched blind test。

## 7. 下一轮可在本地执行的最小步骤

1. **先实现独立 shadow input/runner/parser 小切片**，不切当前 r33 reference。沿上述 seam 绑定 Prompt+真实 frame+context+ledger，使用无 transport 的 compile dry-run。契约 fixture 先覆盖身份/reset/freshness/非法输出/late result，明确它们只验证接口，不验证模型智能。
2. **固定一个预训练 VLM revision，冻结 processor、采样、token/图像预算和 generation 参数**。待当前 r4 GPU 工作完成再运行本地少量真实 pixels 推理，输出 source-bound proposal receipt；同时记录 p95/p99 延迟和显存。本文未启动该步骤，也未下载/部署模型。
3. **做跨任务的最小真实对照集**：至少涵盖多对象消歧、不同任务目标、遮挡重现、任务切换和无法观察/不安全条件，并将动作 gold 保持待审。优先证明 Prompt 和记忆对下一步决定有因果影响，而不是仅生成通顺 reason。
4. **通过 zero-shot shadow 的收益门后再训练**：有 grounding 数据就做视觉/语言 grounding adapter；有 reviewed intent/episode 才做意图 LoRA/history fine-tune；可随后蒸馏到 route A 的共享小模型。不得以 fixture、零残差迁移或同数据自评称学会自主判断。
5. **保持旧接口回归**。以下是已有本地 CPU 契约测试，不是新 VLM 验收，也不发送 PX4：

   ```powershell
   .venv-train/Scripts/python.exe -B -m pytest tests/test_onboard_history_onnx.py tests/test_onboard_history_loop.py tests/test_onboard_agent.py -q -p no:warnings
   ```

新开发 adapter 的测试文件/CLI 尚未存在，不能提供假装已可运行的训练命令。下一次实现应先交付这些具体接口与小样本 replay，再交付经准入的训练入口；实飞、HIL、Jetson 和外部 Gazebo 验收仍分别需要真实证据与授权。
