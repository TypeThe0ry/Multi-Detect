# Multi-Detect

Multi-Detect 是一套面向无人机的自主感知与任务执行系统。它把相机、AI、GPS、测距、目标跟踪、PX4 飞控和 MultiQGC 地面站连接起来，目标是让无人机从一次任务描述开始，自主完成搜索、识别、跟踪、记录、返航和异常处理。

当前版本：**`v0.3.0-pre.8`**。最终 `0.3.0` 尚未发布，因为真实 Jetson、Pixhawk、室外 GNSS、距离精度和人工视觉验收尚未全部完成。

最新：[教师架次基础](docs/onboard-teacher-sortie-foundation-20261008.md)：预算／账本接线，5轮拒绝保留，未飞行／训练；[出生高度](docs/onboard-teacher-live-checkpoint-and-spawn-20261008.md)、[前轮](docs/onboard-teacher-origin-window-and-normal-mission-20261008.md)。

[公开模型补查](docs/onboard-pretrained-action-research-20261008-r3.md)：新增小型VLA／动作教师候选，权重可用不等于无人机可飞。

[真实相机与AI](docs/onboard-navigation-gazebo-shadow-20261008.md)：8帧实际推理已接通，全部输出STOP，飞行未通过。

### Prompt 与 3-D 主线边界

用户输入的是任务意图，不是固定的“红车/绿车”答案。目前实现仍按注册Prompt绑定任务模板，
再进入OnboardAI、planner、PX4 Mission和安全监督路径；未注册Prompt或飞机身份不匹配时
fail-closed。这是当前能力限制，不是最终需求：下一步用视觉语言模型读取实际图像、任务文本
和记忆，动态决定行动，保留物理安全与执行权限检查。主线Gazebo使用仓库自有
的中性实体 3-D 目标模型和多旋翼/VTOL/固定翼场景。红车/绿车只保留为历史软件回放和感知适配器
回归夹具，不是 Gazebo 主线训练标签，也不是模型固定答案。

### 当前飞行策略默认引用（未被 VLM 候选替换）

当前默认 SIM／飞行策略及未来 Jetson 接入显式绑定同一份策略引用：
[`configs/training/onboard-policy-current-reference-v1.json`](configs/training/onboard-policy-current-reference-v1.json)。
它当前选择 r33，并逐字节校验 ONNX、训练配置、训练报告和 3-D Gazebo manifest 的 SHA-256；引用加载
不会把候选模型变成已发布模型，也不会把软件验证写成 Jetson、Gazebo 外部运行或实飞证据。历史
r4 等路径只保留给旧证据回放，不作为当前训练/部署的隐式默认值。

要在共享推理链中使用当前引用：

```powershell
python scripts/run_onboard_policy_chain.py `
  --model-reference configs/training/onboard-policy-current-reference-v1.json
```

外部 Gazebo/PX4 runner 也支持同一引用：

```powershell
python scripts/run_external_onboard_model_acceptance.py `
  --model-reference configs/training/onboard-policy-current-reference-v1.json
```

两条命令仍然不会替代真实 Gazebo 运行；Docker/PX4 可用且显式 `--execute` 后才会产生外部
软件 SIM 证据。

### 此前视觉事实训练（2026-10-07）

研究模型不必伪称从 r33 权重继承：VLM 有自己的固定公开基座、训练计划和 adapter 身份，
目前是开发候选，不改变上述默认策略。最终受审部署包仍须让 SIM 与 Jetson 使用同一模型
及输入／执行契约；增加新能力与沿用已经验证的执行路径并不冲突。

已继承 Qwen3-VL-2B 的 r1 adapter 完成单轮 154 次更新；新增 128 个三维 RGB 样本
独立审核通过，原 84 个留出输入不变。训练／精确重载／独立审计退出 0，但 strict73→70、
exact61→53，质量门失败、不晋升、不调参重试。原失败报告保留；这是视觉事实训练，
不是动作学习，r33 与原 COCO 不替换。见
[本轮进度与动作训练缺口](docs/onboard-vlm-grounding-increment.md)。

图文模型已在独立可取消进程运行，等待不清空 Mission，原目标不会随飞机移动而漂移。
新增“当前位置→目标→有限驻留”的在途航段，不带起飞和降落；仍计算返航、电量与等待
余量，核验原始飞控状态、HOME、任务读回、当前序号与原任务／安全配置身份。这是软件编译／准入，不是已接通
的飞行指令。真实替换、净空、在线回流和动作数据未完成，默认模型不变，尚不能宣称
Gazebo 自主飞行。见[在途航段](docs/onboard-continuation.md)和[运行时状态](docs/onboard-vlm-runtime.md)。

当前优先训练 OnboardAI，红车、绿车只是任务例子。三维数据来自有实体结构、遮挡物和空间
关系的 SDF 场景；颜色或预写状态不能代替图像推理。Gazebo 是最终飞行训练场，Blender
则是明确标记来源的离线三维视觉开发工具，两者的证据不会混用。

第四轮已完成12轮RTX 4060增量微调与ONNX导出，继承第三轮708/708参数项，原生退出0。
合并近远实体三维课程及原train连续帧，train/val/test=832/256/256；64张原clip验证帧参与
checkpoint选择，测试帧不移入train。可见mask、标签、世界和测试几何隔离及清单封存均核验。

| 同像素、同接口开发留出 | r3 命中／误报／漏检 | r4 命中／误报／漏检 |
| --- | ---: | ---: |
| 远距离静态 | 33 / 5 / 16 | 24 / 2 / 25 |
| 旧静态 | 27 / 8 / 24 | 28 / 5 / 23 |
| 远距离连续画面 | 11 / 18 / 24 | 10 / 6 / 25 |
| 旧连续画面 | 6 / 2 / 6 | 2 / 0 / 10 |

误报减少，但远距离与旧连续召回退步；遮挡后同ID恢复仍为0，未通过预注册门，不晋升。
不能把少检测带来的ID切换减少当作跟踪已解决。阈值保持confidence=0.25、匹配IoU=0.5。
[预注册计划](docs/evidence/onboard-visual-replay-training-plan-20261007-r4.json)与
[完整结果](docs/evidence/onboard-visual-replay-increment-20261007-r4.json)分开保留。

需要分清两个模型能力：

- 感知模型回答“看见什么、在哪里、是否还看得见”。现有视觉组件已完成三轮实体三维
  像素微调及一轮混合回放微调，但连续误报、遮挡后身份恢复及旧场景回归仍未全部通过。
  当前候选是单类辅助 `search_target`，不能自动当作car/person或通用任务语义。
- 任务模型回答“为了这个任务，结合刚才发生的事，接下来怎样行动”。当前选用的r33
  只读取结构化当前状态，不直接理解图像、任意Prompt或多目标历史。新增8次观察的
  历史候选已实际导出和运行，但没有训练出时序能力，也未替换r33。40输入/20输出是
  当前实现，不是产品硬约束；后续必须把实际视觉表征、任务目标和任务记忆接入共享模型。

现有Gazebo观测审计完成：403张源payload可读，303张通过完整身份核验；urban r1的seed
与raw来源不符被拒绝。合格来源仍只有train，并有默认状态、时钟域与真实home缺口，不用于
策略或历史表示优化。不会补造动作标签或飞机状态，也不把常量损失下降称为自主判断改善。

三名子智能体已并行完成[VLM选型](docs/research/onboard-vlm-candidates-20261007.md)、
[世界模型选型](docs/research/onboard-world-model-candidates-20261007.md)和
[接入审查](docs/research/onboard-vlm-integration-review-20261007.md)。研究阶段回执单独保留。
后续已下载固定官方revision的Qwen3-VL-2B，建立独立本机推理环境，实际读取三维RGB、
原始Prompt与证据化记忆；8个用例比较同图不同任务及连续遮挡观察。当前是没有飞控输出的
开发旁路，缺失GPS、HOME和飞行状态仍为未知，原始回答不修补；这组历史基线不等于VLM微调、Gazebo
飞行或Jetson验收。实现、运行结果与下一步训练安排见
[图像与任务接入状态](docs/onboard-vlm-shadow-status.md)。Qwen3.5与SmolVLM仍为后续比较
候选；V-JEPA小视频编码器用于离线历史表征研究，不假称已有PX4无人机世界模型。
本机8次实际生成退出0，7次结构合格均为HOLD／空飞行计划；遮挡记忆与未知飞机状态
判断仍有错误。单帧8.43–13.69秒、6帧19.82秒，尚非可用的实时自主飞行智能体。

完整训练记录保留在[当前训练状态](docs/onboard-training-status.md)与[当前进度](docs/current-progress.md)：
[第一轮实体像素](docs/evidence/onboard-sdf-blender-visual-training-20261007.json)、
[第二轮新物体](docs/evidence/onboard-sdf-blender-curriculum-training-20261007-r2.json)、
[第三轮远距离](docs/evidence/onboard-far-visual-increment-20261007-r3.json)、
[历史策略接口](docs/evidence/onboard-history-policy-development-20261007.json)。
结构化状态上的r90开发分数不作为三维视觉或自主飞行证明。原COCO检测器和r33引用保持不变，
所有新模型都是未晋升的开发候选；Gazebo/PX4、Jetson、HIL/REAL、室外GNSS和最终发布分别验收。

正式Gazebo标注数据仍保持staging，缺少完整独立录制与审核轨迹。最近运行时诊断及恢复边界见
[运行时阻塞记录](docs/evidence/gazebo-3d-capture-runtime-blockers-20261007.json)；不以离线训练
替它通过正式数据或飞行验收门。

### 当前执行顺序：先训练同一套 OnboardAI

第一目标是沿现有感知模型和任务策略扩展共享 OnboardAI，而不是把 40/20 接口当作硬边界。
视觉开发训练可以使用明确登记来源的实体三维离线渲染；正式连续任务训练仍须绑定实体 3-D
Gazebo 的 RGB、Depth、CameraInfo、姿态、GPS/telemetry 和连续审核轨迹。红车/绿车只做旧夹具；
数据不足时正式入口保持 `blocked`，结构化 SDF 不能冒充视觉训练，Blender 也不能冒充 Gazebo 采集。
当前 r33 训练入口已经用这批真实采集做了只读 preflight 和一次严格训练尝试：preflight 发现可训练
帧为 0，训练因缺少第二个独立 3-D 场景、完整深度几何、独立 test episode、健康事件和 review
receipt 退出码为 `1`，没有创建 candidate，也没有改动 r33。回执见
`artifacts/evaluation/onboard-3d-training-pipeline-current-20261007.json` 和
`artifacts/evaluation/onboard-3d-training-pipeline-train-attempt-20261007.json`。

### 当前 OnboardAI 几何训练状态

此前 r80–r88 的结构化几何和导航候选都被保留为研究证据，没有 RGB/Depth/CameraInfo/pose/telemetry
像素 episode，因此没有一个候选替换 r33。结构化 SDF 只用于 planner/模型接口回归，不能代替真实
3-D 渲染监督；最新增量 smoke 统一记录在 r89 evidence 中。

当前训练重点是同一套 OnboardAI 的连续导航能力，不是把“红车/绿车”写死进模型。Gazebo 主线使用
中性实体 3-D SDF 模型和真实的 RGB、Depth、CameraInfo、姿态、GPS/telemetry 输入契约。相机没有完成
安装角、机体姿态和 heading 标定时，输入只标记为 `camera_relative`，不会被当成 NED 坐标去记录 GPS
或生成绝对航点；完成标定后，适配器把同一帧深度射线投影为 NED 目标向量。

训练时 OnboardAI 负责判断继续搜索、跟踪、重规划、返航、悬停或降落；已测得的 NED 几何由物理标定和
深度处理提供，再由 planner、geofence、能量和 PX4 Mission 路径复核。这样 SIM、未来 Jetson 和
Pixhawk 使用同一个模型决策契约，同时不让回归头重复猜测传感器已经测出的坐标。r67/r68/r69 是从
r66 继续训练的研究候选，独立全圆角度/8–420 米距离评估仍未通过；随后 r70 的中性 3-D 课程在
结构化 holdout 退化而被拒绝，训练器因此加入可选父模型蒸馏，r71 从 r69 继续训练并保住独立
任务 `7/7`、结构化 3-D task/flight/compatible=`0.9944/0.9878/0.9933`，但仍未 promotion。
r33 继续是当前 reference；完整成对证据见
[`docs/evidence/onboard-policy-r70-r71-parent-distillation-20261006.json`](docs/evidence/onboard-policy-r70-r71-parent-distillation-20261006.json)。

### 当前 3-D 训练场预检（2026-10-06）

训练优先入口是：

```powershell
python scripts/run_onboard_3d_training_preflight.py `
  --output docs/evidence/onboard-3d-training-preflight-20261006.json
```

它只检查当前 r33 引用、实体 3-D world/model manifest、Docker daemon 和本机工具可见性，
不会启动仿真或飞行；同时会自动审计 `artifacts/evaluation` 中的 RGB/Depth 采集是否属于当前
实体 3-D 主线。当前预检结果为 `blocked`：Docker Linux daemon 不可达，本机也没有
`gz/gazebo/px4`；三套实体 3-D 资产审计和模型引用校验已经通过。预检生成的三条采集命令
会使用实际 Gazebo RGB、米制 FLOAT32 Depth 和同帧 telemetry，之后才能进入
`build_onboard_rendered_3d_dataset.py` 和 `train_onboard_model.py`。当前没有可用的外部
Docker/PX4 运行时；历史远程探针和本机诊断均只记录为阻塞，没有把软件预检冒充成 Gazebo
运行，也没有生成 RGB、Depth、telemetry 或训练数据证据。
`training_ready=false`，不代表模型训练完成，也不代表外部 Gazebo、Jetson、Pixhawk 或实飞通过：
[`docs/evidence/onboard-3d-training-preflight-20261006.json`](docs/evidence/onboard-3d-training-preflight-20261006.json)。
预检还会盘点仓库内旧的渲染数据和轨迹暂存；当前最大主线数据只有 25 行、0 个有效 Depth 行、
0 个 detector hit，轨迹暂存只有 3 个 episode/9 个 transition 且未绑定 recording，因此不会被
偷偷送进下一轮训练。

最新带采集库存绑定的预检报告为
`artifacts/evaluation/onboard-3d-training-preflight-20261006-r10.json`（SHA-256=
`9e1b34877eb8196117f8b83b63a917d6124082cff29deac696aeb9d96260fd8b`）。它扫描 122 份历史
RGB manifest，但当前主线合格的 RGB+Depth capture 为 `0`；这表示需要真实 3-D Gazebo 采集，
不是可以用颜色示例、旧帧或结构化 teacher 行补齐。`training_ready=false`，r33 仍是唯一引用。
以下是 2026-10-06 的恢复历史，最新故障是上述空间不足：当时 Docker backend 的故障为残留 AF_UNIX reparse point；预检会从 backend 错误报告动态提取
实际路径并 fail-closed，不会自动删除系统文件或重置 Docker 数据。本轮已对
`docker-secrets-engine\\engine.sock` 做可逆备份，但 Docker 重启后又在 `Docker\\run\\sailor-ingest.sock`
处失败，Windows 对最后一级 reparse point 的 WinError 1920 清理被拒绝；因此不能声称外部 Gazebo 已启动。
仓库恢复入口默认只读：`scripts/recover_docker_desktop_stale_socket.ps1 -EntryName sailor-ingest.sock`。
本次恢复回执见 [`docker-desktop-socket-recovery-20261006.json`](docs/evidence/docker-desktop-socket-recovery-20261006.json)。

现在的统一训练编排入口是：

```powershell
python scripts/run_onboard_3d_training_pipeline.py `
  --output artifacts/evaluation/onboard-3d-training-pipeline-current.json
```

它只绑定当前 OnboardAI、三套中性 3-D 场景、检测器和采集库存，不启动 Gazebo，也不生成假数据。
拿到真实 RGB+FLOAT32 Depth、同帧 telemetry 和 recording-bound 审核轨迹后，才把路径传给同一个
入口并显式加入 `--train`；入口会以 r33 为父模型调用既有训练器做增量微调，生成的目录仍是未
晋升候选：

```powershell
python scripts/run_onboard_3d_training_pipeline.py `
  --rendered-dataset <actual-rgb-depth-dataset.json> `
  --trajectory-dataset <reviewed-recording-bound-trajectory.json> `
  --candidate-directory artifacts/models/onboard-policy-next-3d-candidate `
  --device cuda `
  --train
```

这条入口明确区分“用仓库 3-D SDF 做结构化软件预训练”和“用 Gazebo 真正渲染的 RGB/Depth 做
像素级微调”；当前后者仍因 Docker/Gazebo daemon 不可用而未开始。

### 外部 PX4 Mission 与共享 OnboardAI 的接入边界

`scripts/vtol_selected_task_campaign.py` 已把同一个 `SharedTaskPolicy` 接到现有 PX4 Mission
上传、回读、提交、permit、telemetry、journal 和清理路径。Prompt 解析结果必须与最终编译的
Mission、planner hash、wire hash 和 policy artifact hash 完全一致；GPS fix 不合格时会在进入
Mission transaction 前停止。完整 fixture 回归已经通过，但这不等于当前机器已经运行外部
Gazebo/PX4，也不等于 Jetson、Pixhawk、HIL/REAL 或室外 GNSS 验收通过。对应证据见
`docs/evidence/shared-policy-external-campaign-integration-20261006.json`。
通用 Mission campaign 现在也覆盖 `multirotor`、`vtol` 和 `fixed-wing`，VTOL 复用现有
`VtolMissionGeometry` 与共享安全策略，不再走 unsupported vehicle 分支；这仍只是本地契约验证。

### 当前 OnboardAI 训练状态（2026-10-06）

#### OnboardAI 增量训练与 3-D 数据边界

当前训练始终从现有 OnboardAI 父模型增量开始，保留同一 40 维输入、任务/飞行动作头、
ONNX 推理入口和 SIM/Jetson 共享契约。红车/绿车只是任务叙述示例，不是 Gazebo 标签或
模型类别。真实训练样本必须来自中性 3-D 模型的 RGB、FLOAT32 Depth、CameraInfo、机体
姿态和 PX4 telemetry episode；没有标定或深度的帧只能做 plumbing 检查，不能进入训练集。

r63 及之后的候选只通过了结构化软件 SIM 的部分检查，独立全圆角度/8–420 米距离评估仍
未达到替换门槛，当前 reference 仍为 r33。完整的 r67/r68/r69 指标和哈希见
[`docs/evidence/onboard-navigation-geometry-20261006.json`](docs/evidence/onboard-navigation-geometry-20261006.json)。
当前没有把候选 promotion 为 Jetson/REAL 模型，也没有把尚未运行的外部 Gazebo 说成已验收。

#### 几何标签先修正，再接真实 3-D 数据

红车/绿车只是任务例子，不是 Gazebo 的固定答案。审查 r46/r47 后发现，合成 teacher 曾把
随机目标方位和固定 N/E 位移混在一起，导致 `bearing-right` 退化；中性目标的距离与位移
也不一致。现在已在现有 40 维 OnboardAI 上修正：中性与跟随样本按同一距离、bearing、
N/E 几何生成，方位锚点使用多角度独立分组。对应训练契约测试全部通过，但 r46/r47 仍
保持拒绝，当前 reference 不变为 r33。

真实 RGB/Depth 数据集构建器也已改为复用 OnboardAI 运行时的 CameraInfo、相机标定、机体
姿态、Depth 和 `_target_rays` NED 变换生成标签，不再用固定方向数字。没有同步标定或深度
的帧只能用于 plumbing 检查，不能进入 `training_ready` 的 3-D 微调。当前预检仍为
`training_ready=false`：主线旧数据只有 25 行、0 个有效 Depth、0 个 detector hit，且
Docker/PX4/Gazebo 外部运行时不可用，所以没有声称完成 Gazebo 训练或替换 Jetson 参考模型。

#### 最新候选 r50：几何方向约束改善，但仍不替换 r33

在 r48/r49 之后，我从 r33 父模型直接训练了 r50，仍使用 RTX 4060、同一个 40 维输入、
同一个任务/飞行动作输出和同一套 SIM/Jetson 入口。r50 训练 172,400 行、48 epochs，
独立结构化 holdout 为 `7/7`，22 个通用案例的 guarded 与导航方向均为 `22/22`，900 行
三类实体 3-D manifest 结构化传感器 holdout 为 task/flight/compatible
`98.67%/98.56%/98.33%`。这证明当前训练链对左右方向的结构化回归有所改善，但其中 5 个
案例仍由兼容性/阶段安全 mask 修正；训练中 `rendered_rgb_depth_rows=0`，没有真实 Gazebo
像素和连续审核 episode，所以 r50 仍不 promotion，r33 继续是唯一 reference。完整 hash
绑定见 [`docs/evidence/onboard-policy-r50-geometry-direction-candidate-20261006.json`](docs/evidence/onboard-policy-r50-geometry-direction-candidate-20261006.json)。

#### 候选 r48/r49：几何标签修复后再次增量训练，但仍不替换 r33

我在 RTX 4060 上从 r33 继续训练了 r48，再从 r48 做了 r49 的 bearing 符号微调；两轮都
保持同一套 40 维输入、20 维输出和 SIM/Jetson 共用模型入口。两轮独立结构化 holdout 都是
`7/7`，900 行三类 3-D 场景结构化传感器 holdout 分别为 r48 `98.33%/97.56%/98.44%`、
r49 `99.33%/99.00%/99.00%`（task/flight/compatible）。但通用方向测试仍各有
`bearing-left` 失败，训练数据也还不是 Gazebo RGB/Depth 像素，因此两个候选都拒绝
promotion，当前 reference 仍是 r33。完整模型、评估哈希和边界见
[`docs/evidence/onboard-policy-r48-r49-geometry-label-candidate-comparison-20261006.json`](docs/evidence/onboard-policy-r48-r49-geometry-label-candidate-comparison-20261006.json)。

随后做了 r45 低权重对照：只保留 3,000 条实体 SDF 几何教师和 6,000 条 bearing 锚点，仍从 r33
继承同一个 40 维模型。它的独立保持集为 `7/7`，但泛化仍在 `bearing-right` 失败（`21/22`），
所以同样拒绝。这个结果结束了继续盲调合成结构化样本的路线；下一轮必须使用实际 Gazebo/PX4
连续 RGB/Depth/telemetry 和审核轨迹，才有资格继续微调当前 OnboardAI。证据：
[`docs/evidence/onboard-policy-r45-geometry-low-weight-rejection-20261006.json`](docs/evidence/onboard-policy-r45-geometry-low-weight-rejection-20261006.json)。

新增 r37 开发候选：从当前 r33 父权重继续增量训练，使用三类实体 3-D world/model/physics/sensor
组合的结构化教师样本，不使用红车/绿车身份。RTX 4060 完成 57,700 行、35 epochs；3,000 行
独立 sensor-contract holdout 的 task/flight/兼容输出为 `0.979333/0.984333/0.993333`，通用回归
raw=`19/22`、guarded=`22/22`、左右方向守门通过。r36 因 `bearing-right` 回归被拒绝。
这仍不是渲染像素训练：r37 的 rendered rows=`0`、reviewed trajectory rows=`0`、test=`0`。
因此当前引用仍保留 r33，r37 未 promotion。完整证据：
[`docs/evidence/onboard-policy-r37-3d-model-teacher-20261006.json`](docs/evidence/onboard-policy-r37-3d-model-teacher-20261006.json)。

当前选定参考为 **r33**（r34/r35 后续候选因 bearing 方向回归被拒绝），仍然是在现有
OnboardAI/ONNX 模型上增量训练，不是新建一套飞控 AI。
它继续使用同一 40 维输入、同一任务/飞行动作头、同一安全适配器和同一 PX4 Mission 边界，训练
样本来自仓库绑定的多旋翼、VTOL、固定翼 3-D Gazebo world/model manifest。r33 修复了两个实际
训练问题：目标匹配的东向回归必须跟随 bearing 正负，以及 phase augmentation 必须覆盖完整
生命周期而不是只生成 TAKEOFF；另外把左右 bearing 锚点按混合 episode group 切分，避免整侧只
进入 train 或 evaluation。RTX 4060 训练 45,600 行、50 epochs。

固定的 22 个 truth-free generalization case 现在是 **raw 19/22、guarded 22/22、导航方向 22/22**；
900 行独立结构化 3-D sensor-contract holdout 为 **task 0.976667、flight 0.977778、兼容输出
0.994444**。这些是模型/契约回归结果，不是像素级视觉成绩；r33 的 test split 仍为 0，训练源中
没有 reviewed RGB/Depth episode，且本轮没有启动外部 Gazebo/PX4。因此 r33 仍 quarantined，不能
替换参考模型、不能宣称 Jetson/Pixhawk/HIL/REAL 或室外 GNSS 通过。完整记录见
[`docs/evidence/onboard-policy-r33-balanced-bearing-3d-20261006.json`](docs/evidence/onboard-policy-r33-balanced-bearing-3d-20261006.json)。

本轮还修复了 3-D 渲染数据构建器的直接命令入口：现在从仓库根目录运行
`python scripts/build_onboard_rendered_3d_dataset.py` 会正确加载仓库内的 manifest 校验器。
它会先核对 RGB/Depth wire-v3 provenance 与当前 `gazebo-3d-mainline-v1` 的 world/model hash；
当前保留的旧采集帧与新 manifest 不一致时会 fail-closed，不会被偷偷喂进训练。下一批数据必须
重新从当前三类实体 3-D world 采集，再经过独立标签审核和 pixel-level test 才能训练下一候选。

历史 r34/r35 只作为方向回归对照保留：它们在结构化 holdout 提升时分别出现
`bearing-right`/`bearing-left` 失败，因此没有替换 r33。它们和 r33 一样没有读取 Gazebo truth，
没有 reviewed RGB/Depth episode、外部 Gazebo 重跑或硬件验收；详见
[`onboard-policy-r34-r35-rejection-20261006.json`](docs/evidence/onboard-policy-r34-r35-rejection-20261006.json)。

本轮已经把下一步训练入口收紧到真实 3-D 采集：
`scripts/run_vtol_mission_sensor_capture.py` 可在同一架 PX4/Gazebo 任务飞行中显式保存配对的
RGB wire-v3 和米制 `FLOAT32_M` Depth，并保留任务、时间、GPS、机体与容器绑定。训练命令增加
`--require-real-3d-capture`；它会拒绝少于 100 个唯一连续帧、没有有效深度/检测结果、没有至少
两个 3-D 场景、没有 recording-bound 轨迹或仍含仿真真值的数据。当前 r26 的 9 帧/0 个有效深度/
0 个 detector hit/未审核轨迹会被明确拒绝，所以不会再把红车/绿车示例或重复帧称为 3-D
OnboardAI 训练。当前 Docker/Gazebo runtime 没有重新启动，真实采集门仍保持未通过。

主线 3-D 目标不再使用“红车/绿车”作为验证逻辑。三类 Gazebo world 现在都绑定仓库自有的
`simulation/gazebo/models/multidetect_search_target_v1` 实体模型：它包含底盘、座舱、轮组、
传感器桅杆、碰撞体和深度遮挡，模型的材质、名字和场景坐标都不进入 OnboardAI 输入。这个
模型和每个 world 的 include pose 已写入并由
[`configs/scenarios/gazebo-3d-mainline-v1.manifest.json`](configs/scenarios/gazebo-3d-mainline-v1.manifest.json)
逐字节校验；这只是训练资产准备，不代表外部 Gazebo runtime 已运行。

## 历史训练日志

早期 r1–r26 以及历史外部 SIM 的逐轮记录已移到
[`docs/history/README-development-log-through-v56.md`](docs/history/README-development-log-through-v56.md)。
当前入口页只保留现行架构、最新 3-D 训练边界和可复核的当前状态，避免把旧颜色夹具或旧
候选模型误读成当前训练目标。

<!-- historical development log retained in docs/history; current status continues below -->

旧候选的逐轮训练数字、短帧 plumbing 结果和历史外部 SIM 记录保留在归档；它们都没有替换当前
r33 reference，也不构成像素级视觉、Jetson、Pixhawk 或实飞证据。当前主线只接受下面定义的
真实 3-D capture → review → 增量训练路径。

审核连续轨迹的 loader 已支持 `materialized_sources=true`：会重新打开并校验 RGB/Depth manifest、
frame_id、时间戳和 payload SHA；不能复现原始 3-D 录制的数据不会进入 training-ready 集合。

当数据真正达到 `training_ready=true` 时，还必须附带独立的
`multidetect.onboard-trajectory-label-review-v1` 审核回执：它把实际 3-D 传感器记录、连续
episode、人工/独立复核标签、planner/Mission 证据以及 train/evaluation/test 分区绑定到同一份
canonical JSONL；source manifest 还必须绑定可由 `RawSensorReplayRecording` 重新打开的完整
raw recording manifest，逐帧重验事件流、时间戳、遥测和场景/机体身份。回执明确禁止 Gazebo 世界
坐标、模型 ID、scenario truth 和自标注进入训练。当前
仓库里的 3-D 资料仍以短帧 plumbing/fixture 为主，尚未形成足够的长连续 RGB+Depth+telemetry
轨迹和完整任务标签，因此没有把 r24 或后续候选描述成视觉训练完成。

训练分区也按真实 episode 语义保存：reviewed trajectory 明确声明 `train/evaluation/test` 后，
训练器固定使用这些分区，`test` 不会混入训练或 evaluation；旧的 synthetic/plumbing 数据没有
episode split 时才使用 source-group 兼容切分。这个修复保证了后续模型评估不会把同一段 3-D 飞行
轨迹随机拆开，也不把测试轨迹当训练数据。

旧 r18–r25 试验的完整内容位于历史归档；它们只是结构化或 plumbing 候选，没有视觉训练
资格，因此当前入口不再重复这些过期数字。

当前索引与旧运行证据的最新 parity 过渡审计保存在
[`docs/evidence/release-parity-transition-20261006-r27-training-r2.json`](docs/evidence/release-parity-transition-20261006-r27-training-r2.json)。这是新增的不可覆盖 retained-file audit：它只重绑路径、字节数和哈希，不把旧运行伪装成新运行；`runtime_reexecuted=false`、`release_allowed=false`。

当前三机型 Mission 与共享 policy 证据索引已重新绑定到
[`docs/evidence/external-mission-matrix-20261005-current-parity-audit-r3.json`](docs/evidence/external-mission-matrix-20261005-current-parity-audit-r3.json)。这是保留文件审计，不是新一轮外部 SIM；固定翼、多旋翼、VTOL、四模式 runtime matrix、训练回执、sensor campaign 和 AI-flight parity receipt 已按当前 parity 生成新的 retained copies。
完整 `current-progress` 审计仍会在更早的 field-link 历史索引处 fail-closed，不把该历史现场证据改写成新运行。
维护入口为 [`scripts/rebind_external_mission_matrix_current_r1.py`](scripts/rebind_external_mission_matrix_current_r1.py)，脚本拒绝覆盖既有证据，并明确保持 `runtime_source_parity_verified=false`、`external_sim_rerun_performed=false`。

实时事件闭环已接入共享骨架：`OnboardMissionEventLoop` 用锁串行化传感器/遥测事件，统一经过 `SharedTaskPolicy`、planner 重验证和 Mission observer 替换；重复事件幂等，载荷冲突、时间戳倒退和 Mission 冲突 fail-closed。它不拥有 PX4/MAVLink/执行器权限，也不会自动启动 Mission；外部 PX4 的 pause/upload/readback/commit/permit 仍由 transport adapter 负责。

本轮新增的执行边界有三点：一是每个目标候选都经过类别、连续跟踪、有效深度和任务属性检查；颜色只有在 Prompt 明确要求时才参与过滤，3-D 中性实体默认不绑定颜色，并从有 metric range 的候选中选择最近的已观测目标；没有覆盖搜索证明时只报告 `nearest_observed`，不冒充全局最近。二是两帧确认要求同一检测框在时间窗内保持足够 IoU，避免把不同实体串成一个目标；Gazebo `optical_axis_z_m` 深度也会先转换为 ray-slant。三是模型生成的 `PlannedMissionCompilation` 现在可以通过显式 `MissionTransactionDispatcher` 交给 PX4 外部事务适配器；默认 `--execute-mission-transactions` 只执行 upload/readback/commit/permit，不会 arm 或启动飞行；另有必须显式同时提供 `--execute --execute-mission-transactions --execute-autonomous-flight` 的 disposable-loopback-SITL 生命周期入口，才允许 `AUTO.MISSION → ARM → RTL → Home telemetry → REPORT`。

现在还补上了任务级执行器 `OnboardAutonomousSortie`：一次 Prompt 进入后，执行器会在同一实时事件链中完成合格起飞确认、非匹配候选后的搜索重规划、中性 3-D 目标确认、GPS 位置回执、返航完成回执和最终报告，不需要操作员逐步点击。`mission_start_dispatcher`、返航和报告回调分别接到 Gazebo/PX4/Jetson 的生命周期适配器；模型仍不能直接访问 arm、MAVLink 或执行器。`scripts/run_onboard_policy_chain.py` 现在使用无颜色绑定的中性目标回放，状态应为 `search → match_target → record_location → return_home → report`；当前 r33 重执行回放为 `artifacts/evaluation/onboard-policy-chain-neutral-r33.json`，SHA-256=`bb8cbb7a117b58f821d0a3e0a8a062a8d8ab05be8ffca8ffe086544256b88320`。这仍是 software SIM，不是外部 Gazebo 自动起飞或实飞通过。

目标定位上游可以把经过 CameraCalibration、AircraftPose 和地面射线求解的 N/E 偏移提交给共享事件；任务状态机会优先使用校准后的 NED 偏移记录 GPS，避免把相机相对方位角直接当作绝对北向角。旧 bearing/distance 输入暂时保留兼容，但不应作为室外高精度定位证据。

事件回执按 revision 序列单调应用：旧事件的幂等重试只返回历史回执，不会回退当前任务、恢复已清除的安全 observer，或覆盖最新状态。初始 Mission 负责受约束的起飞段，随后新增模型导航 seam 会把 N/E 偏移经 WGS-84 投影、planner、PX4 multirotor compiler 重新生成 Mission，并用 plan/wire/compilation hash 拒绝旧 Mission 冒充新航迹；任务状态机还会记录 `search → match_target → record_location → return_home → report`，记录 GPS 前要求合格 fix、精度、目标方位和距离。当前这条新证据仍是确定性软件回放，不等于 Gazebo 外部运行、HIL、REAL 或 Jetson。

## 项目最终要达到的效果

用户在 MultiQGC 中输入一次任务，例如：

> 搜索前方一公里区域中的车辆和人员，发现目标后记录位置、距离和图像，目标丢失后继续搜索，电量不足时自动返航。

系统应当自动完成：

```text
任务理解 → 规划航线 → PX4 执行 → 实时 AI 推理 → 目标跟踪
       → 突发情况处理 → 目标记录 → 返航/降落 → 任务报告
```

任务开始后，用户不需要逐个确认航点、反复点击继续、手动切换模式或重新上传 Mission。Prompt 只负责描述初始任务和限制，不负责逐帧控制飞机。真正持续运行的是 OnboardAI 和 PX4 的安全系统。

## Gazebo 和真实飞行必须使用同一套 AI

Gazebo PX4 SITL 与真实 Pixhawk PX4 + Jetson Orin Nano 使用同一套检测、跟踪、测距、定位、任务判断、自动重规划和安全处理。两边只更换数据来源和设备连接方式：Gazebo 提供模拟相机、GPS 和飞控，真实环境提供真实相机、GNSS、Pixhawk 和 Jetson。

Gazebo 不是另一套演示算法，而是实际系统的可重复训练场。

## 当前已经具备的能力

- 修改版 MultiQGC 原生 Fly View、视频、目标框、TRK/LCK/TGT 状态和证据卡片；
- PX4/Gazebo 多机型软件模拟，包含 RGB、Depth、CameraInfo、NavSat 和 Odometry；
- YOLO、目标跟踪、ReID、测距、GPS/DEM 目标定位和不确定性传播；
- 共享任务规划、PX4 Mission 编译、上传、回读、提交、执行和清理；
- `PolicyArtifact`、`SharedTaskPolicy` 和 planner 重验；
- Jetson 等价模型通过同一 production perception factory 接入 Gazebo/QGC 组合运行；
- 目标事件、模型 hash、场景 hash、Mission hash 和运行日志保存；
- GPS、测距、模型或传感器不合格时 fail-closed，不生成假坐标或假成功。

这些能力目前主要是软件 SIM 和外部 PX4/Gazebo 证据，不等于真实 Jetson、真实 Pixhawk、室外 GNSS 或实飞通过。

## 当前模型状态

### 2026-10-06：OnboardAI 3-D 几何课程（r64/r65/r66）

本轮不把红车/绿车示例当作 Gazebo 判断。训练直接使用仓库的中性多部件 SDF、三种机型和同一
40 维 OnboardAI 契约，覆盖 3 场景 × 7 事件 × 9 阶段 × 5 语义状态。r66 从 r65 增量训练，
在 RTX 4060 上使用 112,600 行、左右 bearing 对称采样、有限 sign loss 与 geometry loss。
两个独立 1,200-case structured holdout 的 task=`0.995833/0.993333`、flight=`0.991667/0.990000`、
compatible=`0.993333/0.993333`；22-case 泛化经过安全门后 guarded、方向、距离均为 `22/22`。

r66 只是结构化 3-D candidate，`candidate_promoted=false`，r33 继续是 SIM 与未来 Jetson 的
唯一 reference。候选和完整哈希见
[`docs/evidence/onboard-policy-r66-bearing-symmetry-20261006.json`](docs/evidence/onboard-policy-r66-bearing-symmetry-20261006.json)。
这不是像素视觉成绩：当前 `rendered_rgb_depth_rows=0`，没有外部 Gazebo/PX4、Jetson/Pixhawk、
HIL/REAL、室外 GNSS 或最终 `0.3.0` 证据。下一步是恢复 Gazebo 后采集真实中性 3-D 模型的
RGB+Depth+CameraInfo+PX4 telemetry 连续 episode，再在同一 OnboardAI 契约上做像素级增量微调。

SIM 与未来 Jetson 的唯一 reference 仍是 `onboard-policy-r33-balanced-bearing-3d-dev`。
更早的 r51 结构化候选因 `bearing-left` 失败（guarded=`21/22`）而拒绝；其
`rendered_rgb_depth_rows=0`，不代表像素视觉、Gazebo 自动飞行、Jetson、Pixhawk 或室外 GNSS。
完整绑定见
[`docs/evidence/onboard-policy-r51-3d-pretrain-rejection-20261006.json`](docs/evidence/onboard-policy-r51-3d-pretrain-rejection-20261006.json)。

历史 r27、r26、r17 以及更早模型只作为训练对照保留。红车/绿车只保留为旧感知回归夹具，
不作为 Gazebo 主体验收或 OnboardAI 视觉训练标签。正式连续任务训练必须先通过
[`onboard-3d-capture-inventory-20261006.json`](artifacts/evaluation/onboard-3d-capture-inventory-20261006.json)
的当前实体 SDF、RGB、Depth、telemetry 和审核轨迹门；该审计目前 `capture_eligible=0`。
本轮 Blender 辅助视觉微调单独登记为离线开发实验，不用于替这条 Gazebo 门通过。

本机已有 YOLO26n COCO ONNX/PT，可用于本机 GPU 训练、Gazebo 联调和通用目标检测。

Jetson 原来使用的 fire/smoke TensorRT 模型登记在：

```text
/home/jetson/Multi-Detect/models/fire-smoke-v5-trt86/
```

本机当前没有这份 Jetson TensorRT engine，因此不能把 COCO 候选模型冒充 fire/smoke 生产模型。正式部署前必须恢复原模型 artifact、manifest、版本和验证记录。

还要区分实时感知模型和飞行决策模型：当前仓库已经有 YOLO、深度、语义、ReID 等感知路径，
也有共享 OnboardAI 执行骨架，但 `DeterministicSimulatorAiPolicy`、低层比例控制以及事件到
`RETURN/HOLD` 的默认映射只是兼容性安全基线，不再是目标运行模式。仓库现在提供
`OnboardDecisionProvider`/`OnboardModelProposal` 和 `ModelBackedDecisionProvider`，允许
Jetson/4060 的实时模型直接对突发事件提出 `HOLD/RETURN/LAND/REPLAN`；最终目标是
让 4060/Gazebo 和 Jetson/Pixhawk 加载同一份经过审核的实时策略模型，由模型根据检测、跟踪、
深度、GPS、姿态、电量和环境风险提出继续、跟踪、重规划、返航、悬停或降落；独立安全监督器
只负责对链路、GPS、`HOME_POSITION`、电量硬门、障碍和动作包络做不可绕过的否决。模型故障、
超时或输出不合法时必须 fail-closed。当前 r4 高层策略模型沿用现有 OnboardAI 结构，在本机
RTX 4060 上做 synthetic bootstrap 扩展，输入从 38 维扩展为 40 维 truth-free 结构化事件，
增加了检测类别、颜色及其匹配结果字段，输出仍然是任务阶段、飞行动作和有界 N/E/高度/速度
意图。它不直接读取像素；“红/绿车”仍由现有感知模型编码为结构化属性，再交给高层
OnboardAI。r4 不是从零另训：训练脚本先校验 r3 模型 SHA，再按旧特征名继承旧权重，新增
类别/颜色匹配列才进入微调。软件链路证据见
[`docs/evidence/onboard-policy-chain-20261004-r4.json`](docs/evidence/onboard-policy-chain-20261004-r4.json)，
可复现入口为 `scripts/run_onboard_policy_chain.py`。当前外部 AI flight 证据仍使用
`synthetic_perception_fixture`，尚未完成 Jetson TensorRT 运行、独立真实数据评估或外部
PX4/Gazebo 中的同模型事件重规划，不能写成 Jetson OnboardAI 实时模型已经通过；适配器和
软件回放已落地，但模型仍是 synthetic bootstrap，不能替代原 Jetson 生产模型。

在此基础上，r8 候选模型通过 RTX 4060 增量训练加入红车任务样本和 task/flight 联合兼容
约束；最新 r9 软件回放又启用了任务阶段屏蔽、飞行动作屏蔽和 120 m 有界高度解码。它能在
同一个实时事件链中完成“绿车不匹配 → 左右搜索 → 红车匹配 → 记录 GPS → 返航 → 报告”的
五阶段回放，证据见
[`docs/evidence/onboard-policy-r9-candidate-20261005.json`](docs/evidence/onboard-policy-r9-candidate-20261005.json)。
候选仍未 promotion。外部 PX4/Gazebo r13 已通过容器、GPS、ARM 和起飞门，但被现有能源
预算以及 POSCTL 心跳确认安全门阻断；另外，起飞前缓存图像会被拒绝，真正飞行中的相机 worker
仍在实现中。因此不能把 r9 回放写成外部自主飞行、Jetson/Pixhawk、HIL、REAL 或室外 GNSS
通过。

另外，`scripts/run_external_onboard_model_acceptance.py --use-actual-detector` 已把仓库内
隔离的 YOLO26n 候选模型接到真实 Gazebo wire-v3 RGB 帧：检测框、颜色判断和后续
`OnboardPerceptionObservation` 现在走同一条 OnboardAI/Mission 链路。车辆正控场景的最新
最新证据见 [`docs/evidence/external-car-actual-detector-20261004-r3.json`](docs/evidence/external-car-actual-detector-20261004-r3.json)：
两帧实际 RGB 都检测到 `car`，但颜色为 `unknown`，所以模型保持 `search`，没有伪造“红车命中”。
这证明的是实际 Gazebo 像素已经接入共享推理边界；该场景没有红色目标和深度 companion，
因此不宣称红车匹配、距离精度、GPS 目标落图、Jetson、HIL、REAL 或最终发布。

2026-10-05 新增独立的红车正控场景
[`gazebo_red_car_positive_control_001`](configs/scenarios/validation/gazebo_red_car_positive_control_001.scenario.json)。
固定 digest 的 PX4/Gazebo external SIM 实际抓取了 2 帧 RGB 和 2 帧 FLOAT32 Depth；真实
YOLO 候选模型检测到 `car`，共享颜色适配器判定为 `red`，`target_color_match=true`，Depth
中位距离为 `5.057119846343994 m`，bearing 为 `-0.40285431095441143°`，RGB/Depth 配对、
PX4 GPS telemetry 和容器清理全部通过。原始证据摘要见
[`docs/evidence/external-red-car-actual-detector-20261005-r2.json`](docs/evidence/external-red-car-actual-detector-20261005-r2.json)。
但该轮的被动 probe 仍为 failed：它没有 arm 或启动 Mission，PX4 一直在地面，
`mark_takeoff_complete` 因而正确拒绝消费相机帧；实际推理结果数为 0，没有提交 GPS 目标坐标、
RTL 或报告。原始 r3 证据见
[`docs/evidence/external-red-car-actual-detector-20261005-r3.json`](docs/evidence/external-red-car-actual-detector-20261005-r3.json)。
这证明了被动入口的安全边界和真实传感器门控，不证明完整自动任务；新的显式生命周期适配器已落地，
但尚未把一次外部自动起飞运行升级为通过，也不证明 Jetson/Pixhawk、HIL、REAL、室外 GNSS 或最终
`0.3.0` 发布。

2026-10-05 的自主外部运行 r28 又把完整生命周期推进了一步：Docker/PX4/Gazebo、GPS、ARM、
起飞高度门和 Mission upload/readback/commit/permit 均通过，20 个相机帧中 19 个完成了真实 RGB
与 Depth 配对推理，另 1 个起飞前缓存帧被拒绝。模型在约 22 m 处确实识别到了 `car`，颜色适配器
也判定为 `red`；但是原始 YOLO 分数只有 `0.01–0.02`，低于“可落图/可返航”的校准门 `0.25`。
因此共享任务链正确保持 `SEARCH`，没有写入假 GPS、没有请求 RTL、没有生成 REPORT。证据见
[`artifacts/evaluation/external-red-car-autonomous-r28-structured.json`](artifacts/evaluation/external-red-car-autonomous-r28-structured.json)。
这不是把阈值偷偷调低就能解决的问题：下一步是在 RTX 4060 上对同一 YOLO 架构做远距离小目标
增量训练，并用独立场景验证误报、连续帧和距离误差，然后再重跑同一外部生命周期；Jetson 只替换
推理后端，不另写一套决策算法。

软件回放中的任务状态现在明确经过
`search → match_target → record_location → return_home → report`：
记录坐标和请求返航不是返航完成的证明，必须再收到独立的返航完成事件；模型计划回调还必须
绑定选中的任务、模型计划、planner/wire hash 和最终 Mission，无法用旧 Mission 只改 ID 冒充。
当前回放会完整记录模型给出的 N/E/高度/速度意图；其中 N/E/高度已经进入 WGS-84 waypoint
和 Mission 重编译，速度目前仍是结构化意图字段，尚未写入 PX4 Mission wire item，不能声称
速度控制已经在飞行器上执行。

### 红车自主任务 r29：同一 OnboardAI 链路完成软件 SIM sortie

针对“帮我找到最近的红车”，本轮没有把低置信度检测硬放行，而是先修正训练数据：旧 v1 的
背景里残留了一个没有标签的原目标，模型因此会压低真实帧中的目标。v2 生成器会遮掉背景中的
原目标，并用一部分正确标注的原始位置样本做增量训练；模型仍是仓库原有 YOLO26n 的继续训练，
不是新飞控模型。v2 训练集为 `600/120` 张、10% 负样本，审计结果 clean，候选 ONNX 为
`artifacts/models/red-car-detector-v2/yolo26n-red-car-v2/weights/best.onnx`。

在同一 PX4/Gazebo Docker 外部软件 SIM 中，r29 使用 v2 detector 和现有 OnboardAI policy 完成：

```text
起飞 → RGB/Depth 实时推理 → car → red 匹配 → 连续帧确认 → GPS 记录
→ RTL → Home 到达 → REPORT
```

20 个真实 Gazebo RGB 帧中 1 个起飞前缓存帧被丢弃，6 个有效观测的检测置信度约 `0.891–0.912`，
目标距离约 `22.23–22.39 m`，颜色匹配和 GPS 目标记录通过；6 次 Mission 事务也通过。原始结果与
边界见 [`docs/evidence/external-red-car-autonomous-20261005-r29-v2.json`](docs/evidence/external-red-car-autonomous-20261005-r29-v2.json)。

这仍然只证明外部软件 SIM：没有 Jetson、真实 Pixhawk、HIL、室外 GNSS 或实飞性能声明，v2
仍是 `candidate_promoted=false`，最终 `0.3.0` 发布门保持关闭。

## 当前运行入口

Gazebo + MultiQGC + Jetson 等价推理说明见 [`docs/gazebo-qgc-jetson-ai-sim.md`](docs/gazebo-qgc-jetson-ai-sim.md)。组合入口：

```powershell
pwsh -NoProfile -File .\scripts\run_px4_gazebo_qgc_combined_sim_acceptance.ps1 `
  -JetsonSafetyModel .\artifacts\models\coco-yolo26n-traditional\yolo26n-traditional.onnx `
  -JetsonSafetyModelManifest .\configs\models\ultralytics_yolo26n_coco80_v56_quarantined_candidate.manifest.json
```

Jetson 生产启动脚本：

```text
scripts/run_jetson_fire_patrol.sh
```

## 目标和完整路线

面向人的产品目标、阶段目标、完成标准和发布条件见：

[`docs/product-goals-and-roadmap.md`](docs/product-goals-and-roadmap.md)

路线核心是：

```text
可重复 Gazebo 训练场
→ 恢复 Jetson 原有模型
→ 实时 OnboardAI
→ Prompt 到自动飞行
→ 突发情况自动重规划
→ 目标落图和证据回放
→ Gazebo/真实设备一致性
→ Jetson/Pixhawk/室外/实飞验收
```

## 文档入口

| 内容 | 文档 |
|---|---|
| 产品目标与完整路线 | [`docs/product-goals-and-roadmap.md`](docs/product-goals-and-roadmap.md) |
| 分阶段工程验收清单 | [`docs/implementation-roadmap.md`](docs/implementation-roadmap.md) |
| 当前进度与已知阻断 | [`docs/current-progress.md`](docs/current-progress.md) |
| 机器可读当前状态 | [`docs/current-progress.json`](docs/current-progress.json) |
| 2026-10-04 外部 SIM fresh rerun 摘要 | [`docs/evidence/external-sim-current-reruns-20261004.json`](docs/evidence/external-sim-current-reruns-20261004.json) |
| Prompt/OnboardAI/Gazebo VTOL r6 | [`docs/evidence/prompt-onboard-vtol-gazebo-20261004-r6.json`](docs/evidence/prompt-onboard-vtol-gazebo-20261004-r6.json) |
| Gazebo 实际 RGB → YOLO → 颜色 → OnboardAI（红车正控场景） | [`docs/evidence/external-car-actual-detector-20261004-r3.json`](docs/evidence/external-car-actual-detector-20261004-r3.json) |
| Gazebo 实际 RGB + Depth → YOLO → 红车命中（阶段门拒绝） | [`docs/evidence/external-red-car-actual-detector-20261005-r2.json`](docs/evidence/external-red-car-actual-detector-20261005-r2.json) |
| 红车 v2 detector → PX4/Gazebo 自主 sortie r29 | [`docs/evidence/external-red-car-autonomous-20261005-r29-v2.json`](docs/evidence/external-red-car-autonomous-20261005-r29-v2.json) |
| OnboardAI r14 训练与独立 holdout | [`docs/evidence/onboard-policy-r14-autonomy-transitions-20261006.json`](docs/evidence/onboard-policy-r14-autonomy-transitions-20261006.json) |
| Gazebo 3-D 主线资产 manifest | [`configs/scenarios/gazebo-3d-mainline-v1.manifest.json`](configs/scenarios/gazebo-3d-mainline-v1.manifest.json) |
| OnboardAI 任务链软件回放（r3） | [`docs/evidence/onboard-policy-chain-20261004.json`](docs/evidence/onboard-policy-chain-20261004.json) |
| 后续更新记录规范 | [`docs/development-update-policy.md`](docs/development-update-policy.md) |
| 系统架构 | [`docs/architecture.md`](docs/architecture.md) |
| 自主任务架构 | [`docs/autonomy-architecture.md`](docs/autonomy-architecture.md) |
| Gazebo/MultiQGC/Jetson 模拟 | [`docs/gazebo-qgc-jetson-ai-sim.md`](docs/gazebo-qgc-jetson-ai-sim.md) |
| Gazebo 场景目录 | [`docs/gazebo-scene-catalog.md`](docs/gazebo-scene-catalog.md) |
| 发布门 | [`docs/release-gate.md`](docs/release-gate.md) |
| 版本历史 | [`CHANGELOG.md`](CHANGELOG.md) |
| 历史开发流水 | [`docs/history/README-development-log-through-v56.md`](docs/history/README-development-log-through-v56.md) |

## 发布边界

在生产模型、Gazebo 自动飞行、实时推理、QGC 人工视觉、HIL/REAL、Jetson Orin Nano、Pixhawk PX4、室外 GNSS、实际距离、故障处理、证据回放和清理审计全部完成前，不发布最终 `0.3.0`。

历史证据不会被删除，也不会把软件模拟写成硬件或实飞通过。
