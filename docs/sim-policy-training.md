# 同一策略模型的模拟训练与实际部署

## 2026-10-07：主线改为真正的 3-D 视觉训练，不用颜色示例代替场景

Gazebo 主线的训练对象是仓库中的实体 3-D 模型和场景，而不是“红车/绿车”这类固定答案。
运行时 OnboardAI 只读取 Jetson 也会读取的 RGB、米制 Depth、CameraInfo、机体姿态、PX4
遥测和 Prompt/任务状态；Gazebo world 的模型 ID、世界坐标和离线真值不会进入实时策略。
为了给视觉检测器提供可审核的监督，后续会单独建立 capture-only 标注场景，输出 2-D/3-D
框或分割标签，并在离线 manifest 中与 RGB/Depth/pose 通过同一帧 hash 绑定。标注 topic 不会
接入 `OnboardFramePerceptionAdapter`，也不会被打包进 Jetson runtime。

当前已捕获的实体 Gazebo 3-D 数据只能算采集/管线证据：VTOL 一轮保存了 100 帧 RGB 和 100
帧 FLOAT32 Depth；urban 一轮可以把 100 个连续 transition 与外部 OnboardAI bridge 连接起来，
但完整 Mission 结束门、独立标签审核和 review receipt 尚未齐全。通用 detector 的类别只代表
候选观测；若 Prompt、连续跟踪、属性匹配或姿态标定不成立，就保持 `camera_relative`，不产生
NED/GPS 标签。训练器会继续保持 `training_ready=false`，直到所有真实数据和审核证据满足门槛。

当前 proposal 导出入口：

```powershell
python scripts/export_onboard_sortie_transitions.py `
  --acceptance <external-acceptance.json> `
  --trajectory-staging <trajectory-staging.json> `
  --output <sortie-transition-proposal.json>
```

它只做真实 bridge observation/revision 与帧的精确绑定；缺少完整 decision 的帧标为 `pending`，
不会创建 reviewer、合成标签或 `training_ready=true`。只有独立审核 receipt 生成后，才允许把
同一 r33 父模型送入增量微调。

## 2026-10-07：3-D 连续轨迹必须包含实时健康事件

OnboardAI 训练不再只按“检测到目标/没有目标”构造帧。轨迹构建器直接读取同一帧的 PX4
遥测，在 GPS fix 或水平精度异常、电量低、障碍距离危险时生成共享的
`OnboardRuntimeEvent`；事件覆盖回执与 RGB、FLOAT32 Depth、CameraInfo、机体姿态绑定，
不从 Gazebo truth 取标签。真实 3-D 训练门要求 `target_moved`、`target_lost` 加至少一个
健康事件，保证模型能学到丢目标后的搜索和突发故障下的返航/悬停路径。没有外部运行时产生的
连续 episode 和独立审核，训练仍然拒绝，不会把结构化示例或颜色 fixture 充当训练结果。

严格训练门还会拒绝带有 `red_car`、`red_green`、`color` 或 `colour` 标记的验证场景。颜色只是
任务示例，不是训练标签；真正的训练样本必须来自仓库实体 3-D 模型运行时输出的 RGB、FLOAT32
Depth、CameraInfo、位姿与 PX4 遥测。

## 2026-10-07：训练采集入口按 3-D 机型拆开，仍保持同一 OnboardAI

训练预检现在给仓库的多旋翼、VTOL、固定翼实体 3-D 场景生成不同的真实采集入口：多旋翼走
自主任务与 RGB-D，VTOL 走已有同容器任务传感器采集，固定翼先走明确标注的被动 RGB-D/遥测
采集。三条入口共享当前 r33 模型引用、同一 40/20 ONNX 契约、同一深度/姿态/遥测数据门，
固定翼被动采集不会被算作自主飞行证据。当前预检结果见
[`onboard-3d-training-preflight-20261007-r1.json`](evidence/onboard-3d-training-preflight-20261007-r1.json)。

## 2026-10-07：严格真实 3-D 微调不再混入合成 teacher

`run_onboard_3d_training_pipeline.py --train` 现在从现有 r33 父模型开始，但训练样本只来自
通过真实 3-D gate 的 Gazebo RGB/Depth 数据和 recording-bound reviewed trajectory。入口会传入
`--no-synthetic-bootstrap`，不再把结构化 SDF、phase、bearing 或旧颜色 fixture 混进这轮视觉微调；
训练配置和候选回执会检查 `synthetic_rows_included=false`。这仍然保持 SIM 与未来 Jetson 的同一
40/20 ONNX 契约，不代表外部 Gazebo、Jetson、Pixhawk、HIL/REAL 或最终发布已通过。

## 2026-10-06：r79 对数距离损失候选仍拒绝

在现有 r33 OnboardAI 父模型上加入了一个很小的训练改进：FOLLOW_TARGET 的几何辅助损失增加
对数距离项，专门减少远距离样本被近距离样本压制。输入仍是同一套 40 维观测，输出头、ONNX
契约、planner 和 PX4 Mission 路径都没有改变；训练在本机 RTX 4060 上完成，使用仓库实体 3-D
SDF 场景的结构化 sensor-contract curriculum，不读取 Gazebo truth，也没有把红车/绿车示例当作
目标类别。

r79 的 7/7 独立结构化保持集通过；匹配目标的方向误差下降到约 `20.17°/21.63°`（INSPECT/SEARCH），
但距离误差仍约 `136.85m/134.06m`，没有达到 `angle_p95<=20°` 且 `range_mae<=30m` 的几何门。
因此 r79 仍是隔离候选，当前 r33 reference 不变。更重要的是，本轮 `rendered_rgb_depth_rows=0`、
`reviewed_trajectory_rows=0`，所以这不是 Gazebo 像素视觉训练结果，不能写成 Jetson、Gazebo、
Pixhawk、HIL/REAL 或室外 GNSS 通过。完整候选拒绝和哈希见
[`onboard-policy-r79-log-range-rejection-20261006.json`](evidence/onboard-policy-r79-log-range-rejection-20261006.json)。

下一轮训练不再继续堆颜色或结构化 teacher 行，必须先恢复 Docker/Gazebo，采集当前三类实体 3-D
场景的连续 RGB、FLOAT32 Depth、CameraInfo、机体姿态、GPS/telemetry 和审核轨迹，再从同一 r33
父权重做像素级增量微调。

## 2026-10-06：r70/r71 验证增量训练保持能力的必要性

本轮没有使用红车/绿车标签，也没有另建一套飞控 AI。r70 从现有 r33 继续加入中性 3-D SDF
课程，但在独立 900-case 结构化 holdout 上退化到 task/flight/compatible=`0.8889/0.8800/0.9589`，
因此拒绝。训练器随后增加可选的 `--parent-distillation-weight`，让新课程学习同时保持父模型
的完整输出。r71 从 r69 继续训练，蒸馏权重为 `0.5`；独立 7-case 任务 holdout=`7/7`，
结构化 3-D holdout=`0.9944/0.9878/0.9933`。

r71 仍是隔离 candidate：没有 rendered RGB/Depth training-ready 数据，匹配状态下的连续几何
诊断也未通过。实际 N/E 向量由同一帧 RGB/Depth/CameraInfo/姿态/heading 感知链计算，模型只
选择搜索、跟随、记录、返航等高层意图，再经过 planner/PX4 Mission 安全校验。完整证据见
[`onboard-policy-r70-r71-parent-distillation-20261006.json`](evidence/onboard-policy-r70-r71-parent-distillation-20261006.json)。

## 2026-10-06：r63 几何一致性候选通过软件 SIM 门，但仍未 promotion

从当前 r33 reference 继续训练 `onboard-policy-r63-geometry-lr-dev`，仍使用同一套 40 维
`OnboardModelInput`、任务/飞行动作头和高层导航计划输出。训练数据修复为独立的
场景×事件×生命周期×通用感知状态课程；FOLLOW_TARGET 额外使用观测 bearing/range 的几何
一致性损失，让模型学习从实时感知射线产生 North/East 计划，而不是把目标类别或颜色写成规则。

本机 RTX 4060 训练 93,200 行、30 epochs、学习率 `0.002`。独立结构化 holdout=`7/7`；900
条三类实体 SDF 结构化 sensor holdout 为 task=`0.997778`、flight=`0.996667`、compatible
=`0.998889`；22-case 泛化 raw/guarded=`22/22`，导航方向/距离=`22/22`；模型驱动软件链
保持 `search -> match_target -> record_location -> return_home -> report`，planner 重验为真。

候选证据：[`onboard-policy-r63-geometry-lr-20261006.json`](evidence/onboard-policy-r63-geometry-lr-20261006.json)。
这些指标只覆盖 truth-free 的结构化输入和确定性软件 SIM，不是 Gazebo RGB/Depth 视觉准确率；
当前 `rendered_rgb_depth_rows=0`、`external_gazebo_runtime=false`，因此不替换 r33 reference，
也不宣称 Jetson/Pixhawk/HIL/REAL/室外 GNSS 或最终 `0.3.0` 通过。真实 Gazebo daemon 恢复后，
必须先采集当前三类 3-D world 的连续 RGB+Depth+CameraInfo+PX4 telemetry episode，再做像素级
微调和独立轨迹审核。

在等待外部运行时时，本轮用同一 seed、三场景 manifest 和同一 ONNX runner 做了 r33/r63 成对
复测：900 条结构化 3-D holdout 的 task/flight/compatible 从
`0.982222/0.990000/0.993333` 提升到 `0.996667/0.993333/0.996667`；22-case 实时压力集的
guarded pass 从 `20/22` 提升到 `22/22`。这只支持继续保留 r63 作为候选研究结果，不能绕过
RGB/Depth episode、独立轨迹审核或真实 Gazebo/PX4 验收。完整成对哈希见
`docs/evidence/onboard-policy-r33-r63-compare-20261006.json`。

训练编排已经收口到 `scripts/run_onboard_3d_training_pipeline.py`。它先运行同一份 preflight，
再把真实 rendered dataset 和 recording-bound trajectory 交给已有 `train_onboard_model.py`；
训练器显式绑定 r33 的模型 SHA，使用原有 40 维输入与 ONNX 输出，不另起一套飞控 AI。没有完整
RGB/Depth/轨迹时，`--train` 会 fail-closed，只生成阻断回执，不会把结构化 3-D teacher 行或
历史颜色 fixture 当作像素训练结果。

## 2026-10-06：r52 3-D curriculum 训练完成，但不替换 r33

本轮用本机 RTX 4060 从现有 r33 权重继续训练 r52，输入仍是同一套 40 维 OnboardAI 契约，
teacher 来自仓库实体多旋翼、VTOL、固定翼 SDF 的几何/传感器/生命周期组合；颜色保持未知，
不是训练目标。93,200 条样本、18 epochs 后，独立保持集为 `7/7`，结构化 3-D sensor holdout
为 task=`0.9967`、flight=`0.9944`、compatible=`0.9978`。

候选通用方向集仍失败 `bearing-left`（guarded=`21/22`），并且 rendered RGB/Depth 行数仍为
`0`。因此 r52 只作为训练研究产物保留，r33 继续作为 SIM/Jetson reference；下一轮优先级是
恢复 Gazebo daemon、采集真实 3-D RGB+Depth+CameraInfo+PX4 telemetry 连续 episode，再做同一
模型的像素级微调和独立轨迹审核，而不是继续堆颜色 fixture 或结构化合成行。证据：
`docs/evidence/onboard-policy-r52-3d-curriculum-rejection-20261006.json`。

## 2026-10-06：r51 3-D 几何预训练候选被拒绝

从当前 r33 父模型在 RTX 4060 上完成了 60,400 行、24 epochs 的同契约增量训练。r51 使用
当前三类实体 SDF manifest 和生命周期/方向 augmentations，独立结构化 holdout 为 `7/7`，
三类 3-D structured sensor holdout 的 task/flight/compatible 为 `0.9978/0.9967/0.9989`。
但通用方向回归仍在 `bearing-left` 失败：raw=`19/22`、guarded=`21/22`、direction=`21/22`。
因此 r51 被拒绝，SIM 和未来 Jetson 继续使用 r33；这轮也没有 rendered RGB/Depth 行，不能
把 structured 结果写成 Gazebo 视觉训练准确率。完整绑定见
`docs/evidence/onboard-policy-r51-3d-pretrain-rejection-20261006.json`。

## 训练入口的当前硬门

OnboardAI 训练现在只接受当前实体 3-D Gazebo mainline 的传感器数据。新增的
`scripts/audit_onboard_3d_capture_inventory.py` 会先检查 RGB manifest 是否仍绑定当前
场景哈希、是否有完整 FLOAT32 depth companion、是否有同帧 wire telemetry，以及是否明确
没有读取 simulator truth。2026-10-06 的审计扫描了 122 份历史 RGB manifest，合格数为 0，
所以没有把历史红车/绿车示例、过期场景或无深度帧送进训练。

真正进入增量训练还要满足：起飞后的连续 episode、独立审核的任务/飞行轨迹、跨场景 holdout，
并继续从当前 r33 权重、40 维契约和同一 ONNX 入口开始。审计报告是
`artifacts/evaluation/onboard-3d-capture-inventory-20261006.json`；Docker/Gazebo daemon
恢复前只能准备入口，不能声称已经完成 3-D 视觉训练。

## 2026-10-06：r50 几何方向候选训练完成，仍保留 r33 reference

本轮继续优先训练现有 OnboardAI，而不是把红车/绿车例子做成 Gazebo 答案。从 r33 父权重
直接增量训练 r50，仍使用相同的 40 维 `OnboardModelInput`、任务/飞行动作输出、实时
OnboardAI executor 和安全/PX4 Mission 适配器；RTX 4060 训练 172,400 行、48 epochs。

验证结果为：独立结构化 holdout `7/7`；通用 22 案例 guarded=`22/22`、导航方向=`22/22`；
900 行三类实体 3-D manifest 结构化传感器 holdout 的 task/flight/compatible
为 `0.9867/0.9856/0.9833`。这只说明修正后的距离、bearing、N/E 几何教师和方向损失在
结构化契约上改善了回归；5 个通用案例仍依靠阶段/兼容性安全 mask，不能把它写成模型单独
学会了真实视觉或低层飞控。

r50 的 `rendered_rgb_depth_rows=0`、`pixel_level_rendered_training=false`，没有使用 Gazebo
truth，也没有外部 Gazebo/PX4、Jetson、Pixhawk、HIL/REAL 或室外 GNSS 证据，所以候选仍
`candidate_promoted=false`，SIM 与未来 Jetson 的 reference 继续是 r33。证据及全部 SHA-256
见 [`docs/evidence/onboard-policy-r50-geometry-direction-candidate-20261006.json`](evidence/onboard-policy-r50-geometry-direction-candidate-20261006.json)。

## 2026-10-06：r45 低权重几何对照也拒绝

为了排除“r41–r44 只是几何教师权重过大”的解释，r45 从 r33 重新开始，只加入 3,000 条
实体 SDF 几何教师和 6,000 条 bearing 锚点。独立语义保持集仍为 `7/7`，但 22-case 泛化仍在
`bearing-right` 失败（`21/22`）。因此 r45 也不替换当前引用；继续调整合成结构化权重没有
足够证据改善方向泛化，训练主线应转向真实 Gazebo RGB/Depth/trajectory 数据。

完整 artifact/config/report/evaluation hash 见
[`docs/evidence/onboard-policy-r45-geometry-low-weight-rejection-20261006.json`](evidence/onboard-policy-r45-geometry-low-weight-rejection-20261006.json)。

## 2026-10-06：r41–r44 方向泛化候选全部拒绝

在 r33 基础上又完成了四轮增量训练。它们都保持同一个 40 维 OnboardAI 契约，只从实体 3-D
目标 SDF 提取几何统计，不使用目标 pose、Gazebo truth、颜色答案或 actuator command。独立
语义保持集每轮都是 `7/7`，但通用 22-case 方向/生命周期集每轮都是 `21/22`：r41/r42/r43
失败在 `bearing-left`，r44 失败在 `bearing-right`。r42/r43 的可选 navigation-sign loss
未解决问题，r44 的延长训练则出现过拟合。因此没有更换当前引用，仍使用
`onboard-policy-r33-balanced-bearing-3d-dev`。

证据和每个模型、配置、训练报告、评估文件的 SHA-256 见
[`docs/evidence/onboard-policy-r41-r44-geometry-bearing-candidate-comparison-20261006.json`](evidence/onboard-policy-r41-r44-geometry-bearing-candidate-comparison-20261006.json)。
这些是结构化训练结果，不是 Gazebo 像素训练结果；在外部 Gazebo/PX4 运行时采集到足量真实
RGB/Depth/telemetry 之前，不允许 promotion 或声称 Jetson/Pixhawk/REAL/HIL/室外 GNSS 通过。

## 当前训练引用（SIM 与 Jetson 共用）

当前继续训练和后续接入必须从同一份引用开始：
`configs/training/onboard-policy-current-reference-v1.json`。它选择 r33，固定 40 维输入、任务头、
飞行动作头、3-D 场景 manifest 和父模型身份；加载时会重新计算 ONNX、训练配置、训练报告以及
Gazebo manifest 的 SHA-256。这样 4060 上的训练回归、软件 SIM 和未来 Jetson 运行不会因为默认路径
不同而悄悄换模型。引用仍是 development reference，不代表 candidate promotion 或硬件验收。

当前引用链可以先做不启动外部飞行的回归：

```powershell
python scripts/run_onboard_policy_chain.py `
  --model-reference configs/training/onboard-policy-current-reference-v1.json
```

等真实 3-D Gazebo/PX4 传感器帧完成采集后，再用同一引用跑外部入口：

```powershell
python scripts/run_external_onboard_model_acceptance.py `
  --model-reference configs/training/onboard-policy-current-reference-v1.json `
  --sensor-frames 100 `
  --sensor-frame-output-dir artifacts/training/gazebo-rgb-v1 `
  --sensor-depth-output-dir artifacts/training/gazebo-depth-v1 `
  --execute
```

如果 Docker/PX4/Gazebo 不可用，命令必须停在 blocked/preflight；不能用旧红车/绿车 fixture、
结构化 teacher 行或手写像素冒充 3-D Gazebo 视觉训练数据。

## 2026-10-06：实体 SDF 几何教师候选 r38/r39/r40（未 promotion）

训练优先级仍是现有 OnboardAI，不是重新做一套 Gazebo 演示策略。训练器从主线实体目标 SDF
提取部件数量、径向尺寸和高度，生成距离/可见尺度相关的结构化观测；这些统计不包含目标
世界坐标、truth、颜色或固定答案，颜色槽仍保持未知，模型仍是同一 40 维 ONNX 契约。

三轮 RTX 4060 候选都从 r33 继承：r38 结构化 holdout `0.990333/0.988000/0.992000`，
但 `bearing-left` 失败；r39 加大左右锚点后 `0.993000/0.991333/0.994333`，但 `bearing-right`
失败；r40 提高回归损失权重后 `0.996000/0.996000/0.996667`，仍 `bearing-left` 失败，且
evaluation flight accuracy 降到 `0.8999907313`。所以三轮都拒绝，当前 reference 仍是 r33。
这不是 Gazebo 像素级视觉训练结果；没有 rendered RGB/Depth、reviewed trajectory 或外部
Gazebo runtime 时，不得 promotion。比较证据见
[`docs/evidence/onboard-policy-r38-r40-geometry-candidate-comparison-20261006.json`](evidence/onboard-policy-r38-r40-geometry-candidate-comparison-20261006.json)。

## 2026-10-06 r27：中性 3-D 目标语义 OnboardAI 增量训练（未 promotion）

这轮先把训练目标从示例颜色中抽离出来。三维场景 teacher 仍使用同一个 40 维
`OnboardModelInput`，但不再把红/绿等颜色当作 Gazebo 目标身份；颜色特征保持为未知值，
目标确认只由类别/跟踪一致性、通用匹配分数、深度和实时事件共同决定。模型继续从 r26
权重增量训练，没有新建第二套飞控或第二套 Jetson 模型。

```text
model: artifacts/models/onboard-policy-r27-gazebo3d-generic-dev/onboard_policy_r27.onnx
device: NVIDIA GeForce RTX 4060
rows: 35100 (structured 3-D scene teacher rows: 3000)
evaluation: task=0.873445, flight=0.880355
neutral 3-D holdout: 7/7
generic independent holdout: 7/7
test split: 0 (not release evidence)
candidate_promoted=false
```

独立保持集是 truth-free 的结构化政策回归，不是像素级视觉准确率。它覆盖不同目标类别、
颜色未知、目标丢失、GPS 退化、电量不足和障碍物风险；因此可以检查模型没有把“红车/绿车”
当成固定身份，但不能证明已经在 Gazebo 摄像头上学会目标检测。r27 的完整绑定见
[`docs/evidence/onboard-policy-r27-generic-3d-20261006.json`](evidence/onboard-policy-r27-generic-3d-20261006.json)。

当前仍缺少真实外部 Gazebo 运行产生的足量 RGB/Depth、有效 detector/depth 结果、审核连续
episode 和独立像素 test；本机 Docker daemon 当前不可达，所以本轮没有启动 Gazebo/PX4，
也没有宣称 Jetson、Pixhawk、HIL/REAL、室外 GNSS 或最终 `0.3.0`。

## 2026-10-06 r34/r35：候选比较后保留 r33（未 promotion）

为了避免只看 aggregate 指标，继续从 r33 做了两轮增量训练并同时跑固定 bearing 方向压力集。
r34 的结构化 3-D holdout 更高（task=`0.983333`、flight=`0.993333`、compatible=`0.996667`），
但 `bearing-right` 方向回归；r35 使用更低学习率后改为 `bearing-left` 回归。由于候选不能破坏
已经通过的左右导航行为，当前参考继续使用 r33，不把 r34/r35 写成新默认模型。

候选对比和 SHA 见 [`docs/evidence/onboard-policy-r34-r35-rejection-20261006.json`](evidence/onboard-policy-r34-r35-rejection-20261006.json)。
这两轮仍是结构化传感器契约训练，不是像素级 3-D 视觉训练；没有外部 Gazebo、Jetson、Pixhawk、
HIL/REAL、室外 GNSS 或最终发布证据。

## 真实 3-D 训练硬门

训练器新增 `--require-real-3d-capture`。启用后，训练只接受同一 PX4/Gazebo 任务生命周期中
落盘的 RGB/Depth 连续序列，以及已经绑定 raw recording、独立复核、planner/Mission 重验证的
轨迹标签。门槛包括至少 100 个唯一帧、至少两个实体 3-D 场景、至少 100 个有效米制深度样本、
实际 detector 输出和完整 episode 绑定；仿真 truth、重复帧权重、红车/绿车 fixture 都不能满足
该门槛。当前仓库数据会被 fail-closed 拒绝，这个拒绝是预期行为，不是训练失败。

RGB/Depth 的任务绑定入口：

```powershell
python scripts/run_vtol_mission_sensor_capture.py `
  --frame-output-directory artifacts/evaluation/<run>/rgb `
  --depth-output-directory artifacts/evaluation/<run>/depth `
  --frame-count 100 `
  --execute
```

只有这条同容器采集完成，并在后续生成 training-ready trajectory/review receipt 后，才允许：

VTOL 任务采集器固定绑定 `river_rescue_vtol_001`；要补齐同一训练批次的 x500 多旋翼或固定翼
场景，使用同一个底层容器/传感器生命周期的多场景入口，并分别保存 RGB/Depth manifest：

```powershell
python scripts/run_simulator_docker_acceptance.py `
  --scenario configs/scenarios/urban_inspection_multirotor_001.scenario.json `
  --sensor-frames 100 `
  --sensor-frame-output-dir artifacts/evaluation/<urban-run>/rgb `
  --sensor-depth-output-dir artifacts/evaluation/<urban-run>/depth `
  --execute
```

固定翼场景使用 `configs/scenarios/coastal_patrol_fixed_wing_001.scenario.json`，参数保持相同；
若 Docker/PX4/Gazebo 不可用，命令只能作为 preflight，不能写成已采集。

```powershell
python scripts/train_onboard_model.py `
  --rendered-3d-dataset <training-ready-rendered.json> `
  --trajectory-dataset <training-ready-trajectory.json> `
  --require-real-3d-capture `
  --parent-model <现有OnboardAI.onnx> `
  --parent-sha256 <现有SHA256> `
  --device cuda
```

这仍然是同一个 OnboardAI 模型契约的增量训练，不会新建一套飞控模型；训练完成也不会自动
晋级到 Jetson/Pixhawk 或最终 `0.3.0`。

训练数据的来源边界也必须能复核：渲染数据默认使用
`configs/scenarios/gazebo-3d-mainline-v1.manifest.json`，并把 world、载具、传感器栈和实体目标模型
的 SHA-256 写进来源绑定；training-ready 的 episode 必须同时声明 `recording_bound=true`，并能重新打开
原始 RGB/Depth 录制。渲染帧与轨迹帧的 RGB payload SHA-256 不能重叠，否则训练器会直接拒绝，避免同一
个 3-D 观测同时出现在训练和评估路径。审核回执只证明标签经过独立复核，不会把 Gazebo truth 变成模型输入。

## 2026-10-05 r26：真实 3-D 场景契约上的 OnboardAI 增量训练（未 promotion）

r26 从现有 r25 ONNX 权重继续训练，保留 Jetson/Pixhawk/SIM 共用的 40 维 `OnboardModelInput`、
任务/飞行动作输出和安全适配器。训练使用三类 3-D vehicle/world 场景契约，加上已经落盘的 RGB、
Depth 和遥测绑定；没有把红车/绿车示例当成 Gazebo 场景标签，也没有把 Gazebo world truth 送进模型。

```text
model: artifacts/models/onboard-policy-r26-gazebo3d-scene-dev/onboard_policy_r26.onnx
device: NVIDIA GeForce RTX 4060
rows: 34344 (structured_3d=12000, rendered_unique_frames=9, rendered_repeat=16)
holdout: 2100 cases, task=0.746190, flight=0.779524, compatible=0.950000
candidate_promoted=false
```

9 个渲染源帧的 detector hits 和可用深度中值都为 0，尚无审核连续轨迹、独立 pixel test 或新一轮
外部 Gazebo runtime，因此 r26 只能作为训练候选隔离保存。完整 hash、训练配置、来源拆分和声明边界
见 [`docs/evidence/onboard-policy-r26-gazebo3d-scene-20261005.json`](evidence/onboard-policy-r26-gazebo3d-scene-20261005.json)。

## 2026-10-05：从真实 Gazebo 3-D 连续 episode 开始训练准备

红车/绿车只保留为旧回归夹具，不是 Gazebo 场景定义。新的
[`scripts/build_onboard_trajectory_staging.py`](../scripts/build_onboard_trajectory_staging.py) 读取真实
Gazebo 3-D world 产生的 RGB wire-v3、Depth companion 和同帧遥测，把每次采集按时间组成 episode，
逐帧重新生成与 Jetson 相同的 40 维 `OnboardModelInput`。它会绑定下一帧、RGB/Depth/GPS/场景/机体
哈希，并拒绝时间倒退、缺深度、重复帧和仿真真值字段。

当前暂存：

```text
artifacts/training/onboard-trajectory-3d-staging-20261005.json
3 episodes / 9 transitions
training_ready=false
review_required=true
recording_bound=false
```

这不是“训练标签生成器”：目标匹配、任务阶段和飞行动作必须来自独立审查以及 planner/Mission
重验证，不能由红车/绿车条件或 Gazebo world truth 直接写入。当前采集还缺完整电量、风场、geofence
和可重开的 raw replay，因此不会进入 `train_onboard_model.py` 的 training-ready 路径。下一步要先做
完整多目标、多天气、多视角的连续 3-D episode，再生成 review receipt、独立 pixel holdout，最后才在
RTX 4060 上从现有父模型增量微调，并保持 SIM/Jetson 同一模型输入输出契约。

## 2026-10-05 r24：同一 OnboardAI 的 3-D 多场景增量微调（未 promotion）

本轮没有把红车/绿车示例当作 Gazebo 验证目标，而是继续使用实际持久化的 RGB、Depth、PX4
telemetry 和三种载具的共同输入契约。`r24` 从 r23 的同一个 40 维 ONNX 权重继续微调，使用本机
RTX 4060、30 epochs、12,944 条按 source group 切分的样本；模型 SHA-256 为
`c764e08a46208802315898b853404b6455a6f99995a09b438c2f5be31f42f8ae`。独立结构化 holdout
相对 r23 从 task/flight=`0.676667/0.777500` 提升到 `0.718333/0.800000`，兼容输出率从
`0.949167` 提升到 `0.970833`。

这仍然不是“视觉模型已经学会了目标搜索”：当前 9 个 RGB+Depth 传感器帧 detector hits 为 0，
标签仍是 `sensor_contract_heuristic_plumbing_only`。因此 r24 只是开发候选，不替换 r17，
不 promotion，也不代表外部 Gazebo 自动飞行、Jetson、Pixhawk、HIL、REAL 或室外 GNSS 通过。
完整记录见 [`docs/evidence/onboard-policy-r24-gazebo-multiscene-dev-20261005.json`](evidence/onboard-policy-r24-gazebo-multiscene-dev-20261005.json)。

训练数据契约现在还增加了一个显式的 `materialized_sources=true` 路径：真正可训练的审核 episode
必须重新打开 RGB/Depth manifest，校验 manifest SHA、frame_id、时间戳以及每个 RGB/Depth 字节的
SHA-256；没有可复现录制文件时 loader 会 fail-closed。当前 r23/r24 plumbing 数据还没有升级到
这个 training-ready 状态。

训练器还会保留审核轨迹的 episode split：`train` 只给 optimizer，`evaluation` 用于开发评估，
`test` 只在训练完成后单独测量，绝不混入前两者。混合 synthetic/plumbing 行没有声明 split 时
才使用 source-group 兼容切分；这保证同一段 3-D 飞行轨迹不会被随机拆到两边。下一批真正训练
数据必须是多场景、多机型的完整连续轨迹，并提供正式 review receipt、可复现 RGB/Depth/telemetry
录制和独立 pixel-level test。

## 2026-10-05 r23：RGB+Depth 3-D plumbing 微调（未 promotion）

这一轮先修正训练数据边界，再继续沿用 r17 parent 做增量微调。渲染数据构建器现在要求每帧
RGB/Depth payload SHA-256、同帧 telemetry 时间戳和 source split group；训练器按 source group
切分，`--rendered-3d-repeat` 只增加训练权重，不会把同一帧泄漏到 evaluation。

```text
artifacts/training/onboard-rendered-3d-depth-v2.json
  3 个 Gazebo 3-D VTOL 采集、9 个 RGB+Depth 帧、7 个 split group、depth_rows=9
  detector hits=0
  labels_from=sensor_contract_heuristic_plumbing_only
  training_ready=false
```

用同一套 40 维 OnboardAI 输入、同一 r17 ONNX 权重、RTX 4060、学习率 `0.0003` 和 40 epochs
得到 r23：`artifacts/models/onboard-policy-r23-3d-depth-plumbing/onboard_policy_r23.onnx`。
结构化同 seed holdout 的 task/flight 从 r17 的 `0.574444/0.705556` 到 r23 的
`0.677778/0.778889`，但该提升不能替代 rendered-pixel 或连续 episode 验证。数据没有目标检测命中，
因此这次只记录 sensor-contract plumbing，不把 r23 设为默认模型。

后续训练必须补齐审核标签和连续 episode：起飞、搜索、候选错误、匹配、记录 GPS、返航、GPS/电量/
障碍物中断；并按 episode/scene/airframe/seed 留出独立 pixel holdout。详情见
[`docs/evidence/onboard-policy-r23-3d-depth-plumbing-20261005.json`](evidence/onboard-policy-r23-3d-depth-plumbing-20261005.json)。

## 2026-10-05 r22：真实 Gazebo 3-D RGB/telemetry 接入微调（未 promotion）

新增 `scripts/build_onboard_rendered_3d_dataset.py`，把已经落盘的 Gazebo wire-v3 RGB 帧和同帧
PX4 telemetry 转成现有 40 维 OnboardAI 输入；它只使用真实像素、同帧 detector 结果、相机几何与
telemetry，不读取任何场景 truth。当前数据集
`artifacts/training/onboard-rendered-3d-mainline-v1.json` 覆盖 x500 多旋翼、standard VTOL 和
rc_cessna 固定翼，共 25 帧；这些帧没有有效目标框，因此保留为实际观测的 `target_lost → search`
样本，不把红车/绿车当成训练身份。

使用方式：

```powershell
python scripts/build_onboard_rendered_3d_dataset.py `
  --rgb-manifest <persisted-gazebo-bgr24-manifest.json> `
  --detector artifacts/models/coco-yolo26n-traditional/yolo26n-traditional.onnx `
  --output artifacts/training/onboard-rendered-3d-mainline-v1.json

python scripts/train_onboard_model.py `
  --output-directory artifacts/models/onboard-policy-r22-rendered3d-finetune `
  --parent-model artifacts/models/onboard-policy-r17-teacher-consistent/onboard_policy_r17.onnx `
  --parent-sha256 7434d94fb676427cdf34086f92b42f57cec5ac467b7942e7b116ea5c8bfdbed3 `
  --gazebo-3d-manifest configs/scenarios/gazebo-3d-mainline-v1.manifest.json `
  --rendered-3d-dataset artifacts/training/onboard-rendered-3d-mainline-v1.json `
  --rendered-3d-repeat 80 --device cuda
```

r22 的结构化 3-D holdout 为 task/flight=`0.984444/0.981111`，但像素帧数量太少且没有独立
像素留出集，所以只作为训练管线候选，不 promotion、不宣称 Jetson/Pixhawk/HIL/REAL 或最终发布。

## 2026-10-05 OnboardAI r18：3-D 传感器契约增量训练（r22 的结构化父线）

当前最新候选为 `onboard-policy-r18-gazebo3d-sensor`。它从 r17 继续训练，没有新增第二套飞控
模型，也没有改变 40 维输入、任务头、飞行动作头或 PX4/Mission 安全边界。训练分布读取
`configs/scenarios/gazebo-3d-mainline-v1.manifest.json`，覆盖实体多旋翼、VTOL、固定翼 world 和
载具组合，并把 RGB、Depth、PX4 telemetry、airspeed 的可用性、不同载具的速度/高度/距离工况、
目标匹配阶段和故障转移映射到同一 OnboardAI 结构化观测接口。红车/绿车只在旧回归夹具中出现，
不再定义训练分布。

本轮在 RTX 4060 上使用 35,100 条合成专家转移样本、80 epochs 训练。独立 holdout 为 `7/7`，
22 个 truth-free 泛化用例 guarded 结果为 `22/22`；raw head 为 `17/22`，其中 5 个由固定安全/阶段
契约遮罩补救，不能算模型单独学会。完整绑定见
[`docs/evidence/onboard-policy-r18-gazebo3d-sensor-20261005.json`](evidence/onboard-policy-r18-gazebo3d-sensor-20261005.json)。

这一步是 3-D 场景传感器契约训练，不是已经启动 Gazebo 渲染器后的像素级训练；本机 Docker engine
仍不可达，因此没有把外部 Gazebo/PX4、Jetson、Pixhawk、HIL、REAL 或室外 GNSS 结果混进来。

可复现实验命令（继续从 r17 增量，不会覆盖旧 artifact）：

```powershell
python scripts/train_onboard_model.py `
  --output-directory artifacts/models/onboard-policy-r18-gazebo3d-sensor `
  --samples 6000 --epochs 80 --seed 20261008 --device cuda `
  --model-id onboard-policy-r18-gazebo3d-sensor `
  --model-version 0.3.0-pre.8-onboard-policy-r18-gazebo3d-sensor `
  --output-name onboard_policy_r18.onnx `
  --parent-model artifacts/models/onboard-policy-r17-teacher-consistent/onboard_policy_r17.onnx `
  --parent-sha256 7434d94fb676427cdf34086f92b42f57cec5ac467b7942e7b116ea5c8bfdbed3 `
  --temporal-augment --semantic-augment --phase-augment `
  --gazebo-3d-manifest configs/scenarios/gazebo-3d-mainline-v1.manifest.json
```

## 2026-10-05 OnboardAI r17：teacher 一致性、类别无关训练与阶段安全回归（r18 父模型）

当前候选为 `onboard-policy-r17-teacher-consistent`，从 r14/r15/r16 连续增量继承，仍使用同一
40 维输入和同一任务/飞行动作输出，不创建第二套飞控模型。训练入口
`scripts/train_onboard_model.py` 现在把红车/绿车样本明确降级为可选的 legacy fixture；新候选
默认使用 class/color match、confidence、match score、事件和 phase 的参数化组合，目标类别
字符串只保存在评估元数据中，不能被误称为模型视觉识别能力。r17 还修正了 teacher：属性
未知或不匹配时不会生成真实 perception adapter 不可能产生的高 match 正例。

训练报告新增 north/east/altitude/speed 的 MAE、P95 和 clip rate，并统一 altitude 解码为
120 m 上限。r17 的独立 holdout 为 `7/7`；22 个类别/颜色不变性、phase、故障和左右导航
压力用例经同一个 `OnboardOnnxModelRunner` 的 guarded 结果为 `22/22`，其中
`masked_success_count=5` 仍单独标记为 safety/lifecycle adapter 的补救，不算 raw head 学习。
证据：`docs/evidence/onboard-policy-r17-teacher-consistent-20261005.json`。

这一步仍然只是 RTX 4060 的合成/软件模型候选。真正的 3D Gazebo 入口必须继续使用实体
载具模型、动力学、RGB/Depth/GPS/odometry 和 PX4 transport；外部运行、Jetson、Pixhawk、
HIL/REAL、室外 GNSS 和最终 release 仍需单独证据，不能用本节训练结果替代。

## 2026-10-02 策略上下文身份与 parity 快照门

模拟训练、deterministic SIM 和外部 PX4/Gazebo campaign 继续共用 `SharedTaskPolicy`、planner
和 Mission contract。现在如果 `ContextSnapshot` 携带 `airframe_profile_hash`，策略决策必须与
`PolicyArtifact.airframe_profile_hash` 精确相等；缺失 hash 的旧 v1 context 只保留兼容读取，不能
借此宣称新的 profile 身份。策略层仍只选择任务，不拥有 actuator、arm/disarm 或 transport 权限。

外部证据 receipt 的 runtime parity snapshot 还必须逐项匹配当前 parity 的 bytes、file SHA、契约
hash 和 release label。稳定但来自旧源码的 snapshot 会被记录为 false 并保留文件审计范围，不会
升级为 current runtime parity。`context-profile-r6` 是 source-bound 重算，不是本轮新增外部运行；
HIL/REAL、硬件、室外 GNSS 和最终 `0.3.0` 仍未宣称。

## 2026-10-02 外部 PX4/Gazebo shared-policy campaign（SIM-only）

在前置 Docker/PX4/Gazebo runtime 真实启动、heartbeat/GPS fix type 3 与 2 帧
RGB/depth/NavSat/odometry wire-v3 通过后，使用同一个 `SharedTaskPolicy` artifact 进入已有
VTOL Mission 生命周期。执行入口仍是 `run_vtol_mission_sensor_capture.py`，没有另造 transport 或
飞控路径；两次 sortie 都重新走 Mission upload/readback/commit/permit、completion、journal/hash-chain
和 exact-ID cleanup。campaign 输入是
前置 runtime 的可复核输出为 `artifacts/evaluation/sequence-shared-policy-docker-runtime-20261002-r1.json`
（SHA-256=`8d5d142264364ba430e8cff4a3c25eedfd1597468147acf1ba5aff0bca0aea5f`）；该 run 的
container/telemetry/sensor gates 通过，Mission transaction、QGC 与像素捕获均未请求。
`configs/scenarios/vtol-shared-policy-routes.synthetic-campaign.json`（SHA-256=
`c4941ea10aeffef7ac3c54463b7ec66def02b3ab92d4b7be1a0b14e7041a67d9`），policy artifact 文件 SHA-256
为 `bece2bde9f781ded4269bb5b546fd48c9886718f47d4f9f4df25540e80cf6e3b`，canonical policy SHA-256
为 `de39b730fd2acf3dc6f2fc75cfe201b576e7dd74d1e13834a9bcbc56aeea34d7`，ranker canonical SHA-256
为 `ebfedee61ca668b28e98cce65b5f60cad7ffe02fb166a9fd91682b256ce40ed8`。

可复核输出：`artifacts/evaluation/shared-policy-vtol-campaign-20261002-r2.json`（SHA-256=
`412608cd752206699a1f18817447d1575b0ec8f5e5eb4396e5d8b5231e1eaabc`）。结果为 `gate_passed=true`、
`synthetic-east`/`synthetic-west` 两个 sortie 均完成，两个 policy evidence SHA 分别为
`999793780c77fe24c1dcbedd6d84322cb1f5fdbf4a4639c1d13c9bec3be831cd` 与
`5a4638a8821f42473ed14aeca82839fb9816c6d5b6af49179a781a1adabb008b`；campaign summary 的
planner、artifact、cleanup 和 transaction 校验均由独立 validator 重开。该 run 没有开启像素捕获，
QGC 未启动，`flight_started=false`、`physical_output_enabled=false`、manual visual/QGC、HIL/REAL、
室外 GNSS 与 hardware 仍为未声明；它只证明“同一策略 artifact 进入现有 external SIM Mission
路径并保持安全边界”，不证明策略相对 baseline 有收益，也不打开最终 `0.3.0` 发布门。

为避免只依赖外层运行摘要，已从保留的 campaign 文件生成只读 receipt：
`artifacts/evaluation/shared-policy-vtol-campaign-20261002-r2.receipt.json`（文件 SHA-256=
`c6c89e06c0307a566222a01154f4c0e55fe89f39316b1c6f03d2440a8e6c9f9b`，canonical
`receipt_sha256=a8803481fc93261151a77d4eefe4e7a746d815603297dad1b38be53845d2a26d`）。
receipt 逐项绑定两份 Mission report、transaction journal、policy evidence、campaign summary、
固定 digest runtime identity、容器 exact cleanup 和当前 `release-parity.json`；
`evidence_scope=retained_files_audit`、`external_sim_rerun_performed=false`，所以它是可复核的
文件审计收据，不是额外一次外部运行，也不提升 HIL/REAL、室外 GNSS、硬件或最终发布门。

仍有一个刻意保留的契约边界：外部 campaign 的 runtime evidence 同时保存并校验
`airframe_profile_id`、profile document 与 `airframe_profile_sha256`；通用 advisor 输入中的
`ContextSnapshot.airframe_profile_id` 目前只是文本字段，没有随上下文携带可重算的 profile
document/hash。因此 advisor 结果继续是不可部署的诊断，不能把自由文本 ID 当成 hash 绑定，
也不能据此打开 PX4 Mission、HIL/REAL 或实飞授权。

## 2026-10-02 有界序列提案 advisor（pre.8）

现有 `SharedTaskPolicy` 是单步任务选择器；它可以在每次返航后重新选择，
但不会把多个任务直接拼成一条连续执行链。为量化这个缺口，新增
`build_sequence_proposal` 与 `scripts/propose_task_sequence.py`：

- 输入仍是 truth-free `ContextSnapshot`、`TaskCandidate[]`、同一个
  `SharedTaskPolicy/PolicyArtifact`、同一个 airframe profile hash 和既有 runtime；
- baseline 复用 `simulate_task_budget_episode`，提案复用现有 `select_next_task`、Mission
  compiler 和有界 exact reference，所有边都重新做 planner/compile 校验；
- 输出只包含 baseline/proposed 顺序、奖励/估算时长、identity hash 与安全标记，第一步
  也只是 proposal，不上传 Mission、不启动 Mission、不 arm/disarm、不产生 actuator 输出；
- 任务数硬上限为 6，节点超限、runtime 缺失、airframe 不匹配和不合格 GPS 均 fail-closed；
- `candidate_promoted=false`、`deployed=false`、`final_release_allowed=false` 固定保留。

示例（必须显式提供 ranker 与 policy artifact 文件 SHA）：

```powershell
$env:PYTHONPATH = "src"
python scripts/propose_task_sequence.py `
  --dataset configs/training/task-ranker-budget-v1.json `
  --dataset-sha256 fcd1693881c1c7e2d51e738d8126b0388e161e62a5ea07b0ecbbbb69ca5fb6e6 `
  --case-id budget-train-0 `
  --ranker artifacts/evaluation/task-ranker-training-20261001-r1/candidate.json `
  --ranker-sha256 9dccfc3391a228b093df58e7aabe3867c977c081d5ec61d0519b6215901cec23 `
  --policy-artifact artifacts/evaluation/task-ranker-training-20261001-r1/candidate-policy-artifact.json `
  --policy-artifact-sha256 418bd5bc9762dce80e1ccd2f4f4d267d39e9af8732feca0786aa7a8662b8d169 `
  --output artifacts/evaluation/my-sequence-proposal.json
```

本轮 smoke case 的 baseline 与 reference 均为 `train-0-route-0`、奖励差为 `0`；这不是
模型提升证据。下一步若要把 advisor 变成部署策略，必须先用独立、objective-matched 的
留出集证明稳定收益，并另行扩展安全契约；在此之前不修改 PX4 Mission/actuator 路径。
该次可复核输出保存在
`artifacts/evaluation/sequence-proposal-smoke-20261002.json`（4,325 bytes，SHA-256
`b90ceed5d1e0fda4f112d9ec6d398e70b87d6d6e9d7796a96dc53cfa7e88933c`）。

同一 artifact 在冻结的 `task-policy-independent-holdout-v1.json`（12 cases）上批量复验，严格
绑定完整训练 dataset（9 inputs）、评估 dataset（12 inputs）、ranker、policy artifact 和 15
个 evaluator/core source 文件；已知 input hash 与声明的 scenario family 均不重叠。输出为
`artifacts/evaluation/sequence-proposal-independent-holdout-20261002-r4.json`（20,466 bytes，
SHA-256=`fd197a0db653573342d93af4ed7014e8c5c89ff3ff2d76857d8f5740a1752f89`）：baseline/reference
奖励 `120/120`，reward 改进 `0`、回退 `0`、不变 `12`；完整 objective 改进 `0`、回退 `0`、tie `12`。
报告逐行保存 `reward → completed_count → -estimated_time` 元组；ranker/artifact 的文件哈希来自
首次绑定读取的同一快照，不再二次读取形成 TOCTOU 窗口。
报告明确这只证明已知输入和声明 family 的
分离，`semantic_independence_proven=false`、`physical_independence_proven=false`；这是
objective-matched 软件预算诊断，不是 HIL/REAL、室外 GNSS、硬件或最终发布证据。r2/r3 原始报告保留不改写。

## 2026-10-01 序列参考器收敛（r2）

将原先只在 `scripts/benchmark_task_sequences.py` 中实现的有限 DFS 提取为
`src/multidetect/autonomy/task_sequence_reference.py`，脚本现在委托同一个库函数，避免脚本和
测试各自维护一份搜索逻辑。参考目标明确为
`sum_priority_plus_one_then_count_then_minus_modeled_time_v1`，与 `TaskRanker` 的单步线性分数
不是同一个目标；它没有写入 `PolicyArtifact`，也没有改变 `SharedTaskPolicy` 或 PX4 campaign。

搜索对每条边复用现有 `select_next_task`、RETURN 完成约束、`simulate_task_budget_episode` 和
Mission 编译器，最多 6 个任务；节点超限或输入缺少 runtime 直接失败，不输出伪最优。新增库层
回归覆盖输入顺序不变性、GPS 不合格、runtime 缺失、任务上限和 node limit。该结果仍是预算
模型开发诊断，不是动力学、能耗、HIL/REAL、室外 GNSS、真实硬件或最终发布证据。

## 2026-10-01 异质 development 复验（r1）

使用已有、明确标记为 development-only 的 `task-ranker-heterogeneous-development-v2.json`（36
组系数候选）重新选择 candidate，并用未参与选择的独立 holdout 重跑 parent/candidate 对照。
结果为：

- train/validation/test：parent 与 candidate 均为 `40/40/40`；
- independent holdout：12 个输入、3 个独立场景族，总奖励 parent 与 candidate 均为 `120`；
- candidate ranker SHA 仍为 `ebfedee61ca668b28e98cce65b5f60cad7ffe02fb166a9fd91682b256ce40ed8`；
- `candidate_promoted=false`。

这次结果排除了“只扩大当前三特征线性系数网格就能得到收益”的路径。下一步应优先改进任务
序列级目标或新增经过验证的 truth-free 特征，并重新设计独立 holdout；不能把同一 parent 的
重复参数扫描写成模型提升。

同日用库化参考器对冻结的 `task-policy-independent-holdout-v1.json` 做 objective-matched 对照（r2）：
12 个输入的 greedy/reference reward 为 `120/120`，改进案例为 `0`，同贪心耗时上限的改进也为
`0`。报告为 `artifacts/evaluation/task-sequence-reference-independent-holdout-20261001-r2.json`
（31,696 bytes，SHA-256=`26ef1ebbb3b8b6c7f4f1c0b2df8696dee5df924695797ab1572da48f575`）。这轮没有支持
将序列参考器接入部署策略的证据；报告仍是软件预算诊断。

## 2026-10-01 训练 artifact 外部 SIM 绑定（r1）

在不改变 ranker 的前提下，将训练 candidate 重新绑定到当前 VTOL airframe profile，然后把该
artifact 直接交给既有 `run_vtol_mission_sensor_capture.py --execute --campaign-file ...` 外部
PX4/Gazebo campaign。实际同一容器、同一 telemetry session 连续完成 `synthetic-east` 与
`synthetic-west`；两次 Mission transaction、planner revalidation、artifact canonical SHA、
hash-chain journal、router graceful stop 和 exact-ID cleanup 均通过。artifact 的 ranker SHA 为
`ebfedee61ca668b28e98cce65b5f60cad7ffe02fb166a9fd91682b256ce40ed8`，canonical policy SHA 为
`de39b730fd2acf3dc6f2fc75cfe201b576e7dd74d1e13834a9bcbc56aeea34d7`。

保留证据：

- `artifacts/evaluation/task-ranker-training-20261001-r2-vtol-bound/`
- `artifacts/evaluation/shared-policy-trained-vtol-two-task-20261001-r1.json`
- `artifacts/evaluation/shared-policy-trained-vtol-two-task-20261001-r1.receipt.json`

这是“同一训练 artifact 进入现有外部 SIM Mission 路径”的验证，不是 candidate 性能提升证明；
candidate 仍未晋升，且没有 HIL/REAL、室外 GNSS、真实硬件或最终发布声明。

## 2026-10-01 独立留出复验（r1）

当前训练链已按冻结 parent 重新执行，并单独运行独立 holdout 对照：

```powershell
$parentHash = (Get-FileHash configs/training/task-ranker-baseline-v1.json).Hash.ToLower()
python scripts/train_task_ranker.py `
  --dataset configs/training/task-ranker-budget-v1.json `
  --parent configs/training/task-ranker-baseline-v1.json `
  --parent-sha256 $parentHash `
  --output-directory artifacts/evaluation/task-ranker-training-20261001-r1

python scripts/run_shared_policy_campaign.py `
  --training-dataset configs/training/task-ranker-budget-v1.json `
  --evaluation-dataset configs/training/task-policy-independent-holdout-v1.json `
  --parent configs/training/task-ranker-baseline-v1.json `
  --parent-sha256 $parentHash `
  --output artifacts/evaluation/shared-policy-independent-sim-20261001-r1.json
```

结果：训练/验证/测试奖励 parent 与 candidate 均为 26/28/35；独立 holdout 12 个输入、3 个
场景族的总奖励 parent 与 candidate 均为 120。输入哈希与场景族均不相交，规划器重新校验通过，
candidate ranker SHA 仍为 `ebfedee61ca668b28e98cce65b5f60cad7ffe02fb166a9fd91682b256ce40ed8`，
因此 `candidate_promoted=false`。这证明训练选择与独立评估链路可运行，不证明模型相对 parent 有
收益，也不证明动力学飞行、HIL/REAL、室外 GNSS、硬件或最终发布资格。

## 执行级记录准入（2026-09-08）

### 被拒绝记录的独立隔离

尝试清单入口`inventory_sim_attempts.py --report PATH SHA256`可重复指定，每个报告重新校验哈希，重复报告/同容器尝试拒绝，矛盾状态保留为unresolved_or_inconsistent。明确区分planned_not_executed、execution_acceptance_passed与execution_acceptance_rejected；它整理报告声明，不重新执行底层验收，不输出策略成功率。

A–F显式范围清单为`artifacts/evaluation/qpc-camera-a-f-attempt-inventory-20260908.json`：A仅计划；B/E验收拒绝；C/D/F验收通过。共1个计划、5个实际执行请求，不把它称为全项目全部尝试。不同运行包含代码/配置变化，不是成对策略对照；C的独立能量窗口不完整、E存在completed结果声明等更细结论仍以各专项审计为准。下一阶段应预先固定实验清单、政策/场景身份与结果语义，再收集比较样本，避免事后选择成功运行。

`scripts/classify_rejected_execution.py --mission FILE --mission-sha256 SHA --output NEW_FILE`只生成故障分诊/尝试清单记录，不生成训练奖励。分类分开记录验收gate和shared outcome声明，并检查后者与执行文档canonical hash的关联。任务完成及policy_reward均为null，不能将gate=false映射成任务失败或负奖励。此脚本不重放原始遥测，不重验事务，不证明失败根因。

E实件输出`artifacts/evaluation/qpc-camera-e-quarantine-20260908.json`：acceptance_rejected_despite_completed_outcome_claim。原报告中的shared outcome为completed/terminal_reached_landed_disarmed，采集验收被拒绝；这是已绑定报告的声明冲突，不是新的成功验收。31项隔离/准入/分组测试通过。F成功索引与E隔离记录应共同保留供尝试清单审查，但不能组成成功率训练集或宣称已穷尽全部尝试。

数据集索引入口：`python scripts/build_execution_dataset.py --manifest configs/training/execution-f-local-v1.json --output artifacts/evaluation/my-execution-dataset.json`。示例依赖本地F原始证据，仓库没有这些artifacts时会明确失败；不下载、补造或改用自签准入结果。每个条目重新校验三文件，输入路径相对清单目录解析。

索引要求显式family及train/validation/test。重复Mission报告或事务日志拒绝；相同family、scenario_id、manifest SHA、去task_id后的任务语义SHA、host session任一个跨集合均拒绝。它是保守的已知关联检查，不证明不存在语义相似场景；不同家族是否独立仍需数据设计审查。

F实际索引结果为train=1、validation=0、test=0，all_splits_present=false，未拟合、未晋升。当前入口只接收成功执行样本，因此也不能用它估计任务成功率；故障/失败样本需要独立结果契约，不能从数据集中省略失败后宣称成功概率提升。

`scripts/admit_sim_execution_record.py`接收显式`--outer/--outer-sha256`、`--mission/--mission-sha256`、`--journal/--journal-sha256`和新`--output`路径。只读复核成功SIM父报告与清理后绑定、Mission身份、真实事务日志hash链和文档/协议绑定、完成状态、同一QPC身份与事件时长。原始事务文件重新打开后再次验证哈希；不使用报告内路径自动寻址。

输出记录种类为successful_sim_execution_receive_window，只允许offline_sim_completion_comparison和same_semantics_host_receive_duration_comparison两类用途。完成标签与ARM到完成主机接收时长是明确语义，不等于物理飞行时间或保守规划预算。像素同步、物理能量、数据集划分独立性及模型晋升不随之合格。当前版本要求QPC事件身份，不把旧无身份时间自动迁移。

F原始外层/Mission/事务文件实际通过新准入，输出`artifacts/evaluation/qpc-camera-f-execution-record-20260908.json`，同域时长102.8616189秒。229项准入和既有事务/采集回归通过；新准入单元测试用日志校验替身验证接线和拒绝分支，F实件使用真实validate_mission_journal，不混淆两类证据。

当前尚未将记录接入参数拟合。下一步需定义任务/场景族去重、训练与留出划分及与同语义基线的比较；一条合格执行记录不是策略改善或可部署的证据。

## Mission完整窗口采集

高分辨率采集API已可注入同一个`HighResolutionSystemClock`。注入后探针与JSONL使用QPC，记录完整host_clock_identity；`wait_after(clock.read())`验证源和会话，拒绝裸整数及其他会话stamp。独立审计只在父报告、Mission window/flight、原始JSONL及探针身份一致时接受高分辨率数据，原始SHA仍必须匹配。仅通过同源检查不意味着容器时钟/Gazebo/PX4已同步。CLI跨进程接线尚未完成，不能将旧运行报告加上新标签代替重新采集。

探针默认已改为一批查询共享同一个docker exec会话：先READY，再按0..count-1请求，每次记录主机前后时间；响应必须包含相同序号，正常EOF/exit0后才接受结果。启动时间不计入单次查询RTT。原逐次exec保留为`sample_exec_probes`对照，Mission采集器调用`sample_probes`默认使用新路径。查询数量2..32，响应等待/进程退出各有5秒上限，失败terminate/kill并关闭管道。

**本机时钟限制**：Python3.12 `time.get_clock_info('monotonic')`实测为GetTickCount64、resolution=0.015625秒。新记录包含`host_resolution_ns`，区间扩展为[h0-resolution-c,h1+resolution-c]；这是建立在“每次读数误差不超过声明分辨率”假设下的条件区间，不是操作系统时钟准确度认证。历史无此字段的数据按原零扩展方式解析，不能视作已补充量化误差。`host_quantization_limited=true`意味着至少一次相同tick，RTT=0不能解读为零延迟。

2026-09-08同容器6次对照：逐次exec区间宽259.580998ms、长连接27.095559ms。该短时实验不验证Mission全窗口等速/漂移；下一步评估统一高分辨率host clock，不能把QPC与旧GetTickCount64值直接相减。新协议尚未执行完整外部Mission。

采集器现在在开始/结束各采3次`clock_offset_probe.sample_probes`。命令固定为同一精确容器ID内的Python monotonic查询，主机在子进程调用前后记时。令h0/h1为主机前后时间、c为远端读数，在`h=c+offset`（等速、无重置、恒定偏移）假设下，单次offset属于[h0-c,h1-c]；多次取交集，不相容输出null。保留每次原始数据和假设，不输出“精确偏移”中点。此处RTT包含进程启动/调度/传输，不是纯网络延迟。

2026-09-08无网络容器6次烟测，条件交集宽213.842163ms、观测跨度1.5秒。不能外推为整个Mission的漂移界，也未对齐Gazebo源消息生成时刻或PX4 boot时间。新采集器整段Mission验证尚待执行；旧campaign-clock-c不具备此字段，禁止补造历史探针。

独立重放命令（路径显式提供，不追随报告内文件路径）：

```powershell
python scripts/audit_mission_clock_receipts.py --outer artifacts/evaluation/campaign-clock-20260908-c.json --outer-sha256 affda48e8d3b4518030c54ee20b63bc521c0854c6c5138b4f416a136bdc2b778 --clock artifacts/evaluation/campaign-clock-20260908-c/gazebo-clock.jsonl --mission artifacts/evaluation/campaign-clock-20260908-c/campaign-missions/mission-001.json --output artifacts/evaluation/clock-receipt-new.json
```

输出必须为新文件。父报告须成功并含成功Clock采集；Clock字节SHA/长度/summary和Mission SHA/同容器身份必须一致。最多32MiB/文件、10000条记录，严格JSON拒绝重复键及非有限常量。每个事件查找严格早于/晚于其主机时间的接收样本，保留原始源纳秒整数；事件本身来自历史float秒，转换方法明确为round(seconds*1e9)。没有包围样本时返回null，不补点。`receipt_associated_sim_ns`只是这些接收记录携带的sim值，不是源事件发生时间界限。审计不重新验证Mission journal，也不授权下一任务。

campaign-clock-c重放得到west边界94/94ms、east203/203ms；整段最大主机接收间隙750ms。采集参数0.1秒是最小间隔，不是每100ms必达保证。不要把间隙直接解释为传输延迟或固定误差；继续保持`training_label_eligible=false`。

现有`run_vtol_mission_sensor_capture.py --execute --campaign-file ...`命令增加`--capture-clock`即可同时采集；保留原有显式能量配置和隔离运行参数。采集器在同一精确容器ID内运行，先确认`/world/default/clock`真正存在publisher，再订阅。不能只检查topic名称：固定PX4镜像会以topic存在判断世界已启动，过早订阅会干扰其启动判断。

采集间隔按容器接收时钟至少0.1秒，最多10000条、1800秒，要求显式STOP正常结束；样本上限提前到达按失败处理。JSONL每行包含完整源时钟记录与独立主机接收monotonic_ns。保存原始整数精度，不将容器monotonic与主机monotonic直接相减。外层在任务前后等待接收样本，结束时校验时钟序列并绑定SHA，清理后复核。该覆盖是接收时间覆盖，不证明源时间同步或传输延迟上限。

campaign-clock-c实际双任务通过，2396条，sim/real窗口比率0.8736882828；是这一次采样窗口的观测值，不能作为固定倍率或物理标定参数。详细证据见`docs/evidence/mission-clock-20260908.json`。

## Gazebo原生时钟采集器

在固定Harmonic镜像内可执行：

```sh
python3 /workspace/scripts/gazebo_clock_capture.py --world default --samples 20 --timeout-seconds 15
```

只读订阅`/world/<world>/clock`的gz.msgs.Clock，不读取目标真值或发飞控指令。每条JSONL保存sim_ns、real_ns、system_ns、container_receive_monotonic_ns、序列与topic。容器monotonic不是Windows主机monotonic；绝不能直接相减。字段缺失、非法纳秒、sim/real/receiver回退或序列断裂拒绝；sim暂停可返回零推进。样本2..10000、超时0..3600秒；stdout用于数据，stderr用于诊断，外部运行器必须分别保存两者并检查退出码。

首次无网络empty世界烟测20条，sim跨度19ms、Gazebo real跨度19.002669ms，主机重新解析一致。该短窗口不代表任务期间倍率，未与PX4/主机对齐。不要从这次烟测产生训练标签。下一步需同Mission容器覆盖ARM前至完成后，并绑定原始时钟与任务记录身份。见gazebo-clock证据。

## 规划/执行窗口差距审计

`clock_window_comparison`单独报告主机接收窗口与接收归属的boot区间包络：下界是严格位于窗口内部的有效boot区间总和，上界再加接触起止边界的完整区间，不跨时钟插值。除以主机窗口仅是描述性比率，不是Gazebo实时倍率或同步证明。覆盖不足时区间/比率均null；`calibration_eligible=false`始终明确，禁止将该比率自动写入规划参数或训练标签转换。

实际tail记录：west主机124.172秒、boot归属92.192–92.272秒；east主机106.422秒、boot归属80.704–80.760秒。下一步若构建训练标签，应分别记录时钟来源、窗口语义与保守预算语义，并补Gazebo时钟或明确时间契约证据；不能直接用host duration替换模型耗时。

首次带尾段的外部campaign-tail-a已通过两项任务。使用audit_planning_execution_gap对新原报告重放，两项`receive_window_bracketed_by_valid_energy_prefix=true`，缺失边界为空。合成能耗包络分别为2.7025772–2.7033772Wh、2.2563316–2.2568916Wh，按完整区间归属、边界触碰区间计入上界，不跨时钟插值。包络依赖36W航电+324W最大推进的显式合成profile；不能当作物理误差界或拟合真实电池参数。旧u报告仍保留缺结束边界结论。

新Mission报告在接受终态后执行有界接收尾段：默认2秒，最大256次pump（冻结时钟也能退出）。保持ARM与原完成时间，额外观测只用于形成结束边界后的样本。`post_terminal_tail.end_position_sample_received`不是整段能量覆盖门；必须继续用原始历史执行audit_planning_execution_gap及其中的窗口重放。适配器缺失或超时被显式记录，不能补造样本或从完成事件推导真实电量。终态任务完成和数据覆盖质量是两个独立结论。

```powershell
python scripts/audit_planning_execution_gap.py --report artifacts/evaluation/campaign-20260908-u/campaign-missions/mission-001.json --report-sha256 618d59a0ae6ebf9c4d5a30f8a2a38d5b241ac0c0309376a18893be7053f44b17 --output artifacts/evaluation/my-planning-gap.json
```

只读分析绑定的成功SIM报告。核验嵌入任务/上下文/计划/执行文档hash，重算当前计划必须与原计划一致，核验完成结果的Mission身份以及观察器/ARM/完成的主机时间顺序。此脚本不重新验证完整journal，也不重编译可执行Mission，相关声明显式false；完整执行证据仍须参考原验收与父进程审计。

输出当前估算与两种主机窗口（ARM→完成、observer→完成），并复用原始能量历史重放。缺少起止覆盖时，不填补能量或给出校准误差。主机时间和PX4 boot时间不自动等同，估算含保守余量，任何数值比率都不能直接成为生产修正参数。历史u两项都缺结束边界，下一步必须产生带有界观测尾段的新原始证据。

## 小规模多步参考基准

v2报告增加收益/完成数最大化、估算时间最小化的非支配前沿；相同收益/任务数仅保留最短时间的首个序列。`reference_within_greedy_time`从前沿中选择估算时间不超过贪心时间的最佳收益（浮点累加容差1e-9秒），`estimated_elapsed_delta_s`明确显示额外时间。前沿是模型内不同目标权衡，不代表所有实际成本或风险。初始可行数量与拒绝原因另行记录。

配对诊断入口：

```powershell
python scripts/benchmark_task_sequences.py --dataset configs/training/task-sequence-heterogeneous-v1.json --output artifacts/evaluation/my-heterogeneous.json
python scripts/benchmark_task_sequences.py --dataset configs/training/task-sequence-duration-control-v1.json --output artifacts/evaluation/my-duration-control.json
```

第一份为3个相关几何家族×4组固定优先级/续航/电量/风输入，按任务设置返航余量和时长限制；第二份只改case标签及maximum_duration_s=600，其他输入逐字段保持不变。沿用train/validation/test标签是现有数据加载格式，明确不作训练或未见留出声明。首次结果120→120、147→147，没有晋升依据。证据sequence-frontier-20260908。

```powershell
python scripts/benchmark_task_sequences.py --dataset configs/training/task-ranker-budget-v1.json --output artifacts/evaluation/my-sequence-reference.json --max-nodes 10000
```

该入口不训练、不更新候选、不执行飞行。它在同一个预算模型中枚举全部可行任务顺序及停止前缀，每步调用原规划/编译与预算推进；目标依次为完成priority+1收益、任务数、较少估算耗时。相同目标精确同分按稳定任务ID遍历保留首个。每例最多6任务，节点超限或编译失败直接报错，不能把部分搜索结果当作最优；输出路径必须不存在。

报告记录每例输入hash、贪心/参考顺序、收益、估算耗时、搜索节点、拒绝边和实际运算时间。首次正式b运行9例总收益89→90，仅1例改善；属于有限预算模型内的可行组合差距，不是实机性能或独立留出结论。完整评审见autonomy-architecture-reassessment.md，证据task-sequence-reference-20260908.json。

## 冻结候选的故障评估

```powershell
python scripts/evaluate_task_ranker_faults.py --dataset configs/training/task-ranker-budget-v1.json --candidate artifacts/evaluation/ranker-fit-20260908-a/candidate.json --candidate-sha256 9dccfc3391a228b093df58e7aabe3867c977c081d5ec61d0519b6215901cec23 --output artifacts/evaluation/my-ranker-faults.json
```

输出必须是新文件。上例绑定本仓库首轮候选的精确字节身份；使用其他候选时须提供其独立确认的SHA。评估器不搜索参数、不更新候选，不产生控制指令。每个原场景检查正常条件能完成至少一个任务，再逐一注入GPS fix=2、GPS精度=7m、围栏无效、相机/感知/地图/DEM不可用、电量0、续航0。每次必须零完成，拒绝全部任务且原因精确匹配；任一检查失败，报告保留且进程返回1。输入解析失败直接报错。

固定矩阵版本frozen_ranker_planning_faults_v1，报告保存原输入/扰动输入hash、上下文改动、拒绝原因、正常对照以及评估器文件SHA。首轮81项通过，仅证明这些规划输入下拒绝仍有效，不证明动态断流检测、故障恢复、动力学性能或独立泛化。已用的训练/验证/test原场景均属于复用回归，不作为新的未见留出。

## 可运行训练入口（2026-09-08）

在仓库根目录PowerShell执行（输出目录必须不存在）：

```powershell
$parentHash = (Get-FileHash configs/training/task-ranker-baseline-v1.json).Hash.ToLower()
python scripts/train_task_ranker.py --dataset configs/training/task-ranker-budget-v1.json --parent configs/training/task-ranker-baseline-v1.json --parent-sha256 $parentHash --output-directory artifacts/evaluation/my-ranker-fit
```

输出candidate.json与report.json。数据集包含显式上下文、原任务与runtime、airframe身份、分组和有限参数网格；不创建另一套飞行模型。每组3例共9例，train只用于选择，冻结后才评估validation/test。CLI拒绝重复输入、跨组同名家族、缺组、未知字段和非有限参数；家族声明与精确hash并不证明语义独立。

实际首轮ranker-fit-20260908-a：父候选加8组参数，selected_trial=0。train/validation/test收益均与父候选相同，分别26/28/35，candidate_promoted=false。数据是理想可用传感器与相关旋转路线的合成预算场景，不是动力学飞行或实测数据。不要因已看test结果继续调参后仍将该组称为未见测试集。下一轮须预先定义新评估方案；本轮没有新模型晋升或实机部署。

以下API交付说明为历史记录；其中“数据集/CLI尚未交付”已由上述入口补齐。

预算训练API（src/multidetect/simulation/task_training.py）：TaskTrainingCase包含context、vehicle、
TaskCandidate元组、每任务runtime及airframe hash。evaluate_task_ranker调用原预算episode/规划/编译，
记录完成ID、sum(priority+1)、估算耗时、剩余续航和拒绝原因；不伪装成测得的飞行性能。
fit_task_ranker要求显式parent与1..256组有限候选系数，先评估parent，再按收益、完成数、较少估算耗时
依次比较，同分保留先出现者（parent优先）。报告记录parent/selected hash、训练输入hash和全部评估。
训练API不接受留出集；独立evaluate可传forbidden_input_hashes阻止完全相同输入重用。
该检查不保证几何/扰动家族语义独立，数据集仍须按场景家族划分。最多512个case，重复ID/输入拒绝。
预算episode仍要求RETURN完成、按原估算扣减续航，无新增飞行动力学或真实能耗模型。
本轮测试从刻意差fixture父候选恢复默认基线，不是超越项目基线；项目数据集/训练CLI/留出结果尚未交付。

u轮候选外部执行通过：vtol-ranked-routes.synthetic-campaign.json中的未训练候选驱动west→east，
两项实际完成103.937秒/109.422秒；ranker规范hash为a63c2ce429148d5786812325f5a65b6f8f649f5d61c6adb39fbb5791ef26d428。
父进程使用相同候选重算选择/编译，并执行完整journal验证，不再使用单元测试替身。
清理后复核及精确容器移除通过，外层exit0；见docs/evidence/campaign-u-20260908.json。
这是候选装载与原Mission执行路径的外部证据，不是训练结果、泛化收益、物理电池标定或同一UI验收。

campaign候选接线：原schema_version=1配置允许可选ranker字段，内容是TaskRanker.to_mapping的
严格对象；整个campaign输入文件SHA由外层绑定，省略ranker保持原排序，显式null不是有效候选。
load_campaign保持原二元返回，新增load_campaign_definition返回候选列表/runtime/可选ranker三元组。
子进程将ranker传入run_selected_task_campaign，在原可行性检查之后评分；选择结果仍由原编译器
和Mission事务执行。汇总及completed项记录ranker_sha256。父进程读取相同候选重算顺序、编译和完成证据，
并检查汇总/逐项候选hash；独立负向audit也使用同一ranker重算第一项选择。
外部测试配置configs/scenarios/vtol-ranked-routes.synthetic-campaign.json使用priority=-1，其余系数0，
故应先synthetic-west再synthetic-east；这是未训练的接线fixture，不是学习效果或生产模型。
本轮332项测试通过，父进程新测试替换journal验证以聚焦排序/身份，完整journal外部验收仍待新运行。

候选扩展：src/multidetect/autonomy/task_ranker.py的TaskRanker为共享planner上的可选排序层，
不是替换检测/深度模型的checkpoint。格式固定schema_version=1、
extension=linear-feasible-task-ranker-v1、base=shared-planner-baseline-v1、provenance、coefficients。
coefficients严格为priority、total_distance_m、estimated_duration_s三项有限数（绝对值不超过1e6），
直接使用观测中的SI单位，分数为三项乘积之和，越大越优先；等分保留原排序，零系数保留基线。
provenance是来源说明，不是训练质量认证；本轮测试系数均为未训练fixture。
load_task_ranker(path, expected_sha256=...)要求预先给定字节哈希，最多读取65537字节并拒绝超过64KiB，
使用严格JSON而非可执行序列化。运行返回规范hash供每步记录；不推断来源模型已被验证。
run_observed_task_session新增可选ranker参数；省略仍走原select_next_task。
启用时只重排可行集，经resolve_policy_action再次校验后进入原编译和完成监测，每步记录ranker_sha256。
尚未接入外部campaign CLI、UI或训练器；不得把共享会话fixture通过升级为HIL/REAL或学习效果证明。

共享策略接口位于src/multidetect/autonomy/task_policy.py：
build_policy_observation复用原select_next_task输出可行任务及SI单位估算值、任务/计划/context哈希，
不接受模拟器truth作为策略观测。baseline_policy_action保留原排序，TaskPolicyAction仅含
schema_version、observation_sha256、task_id；task_id=null是明确不选择，不是悬停/降落指令。
resolve_policy_action根据当前候选/context重算观测身份和可行集，返回原EvaluatedTask，
不接受策略传入自定义plan、score或飞控命令；新鲜度、编译、人工授权和Mission事务门仍由原调用方负责。
本轮是共享API扩展，未接运行循环或加载模型；任务选择的新能力不能据此宣称已部署到SIM/HIL/REAL。
core/task parity已变化，历史t证据保持原绑定，不能升级为新策略端到端证据。

t轮外部复测通过：使用新报告和audit --require-energy，第一轮合成消耗0.04464Wh，
第二轮累计2.5244368Wh、新增2.4797968Wh，剩余30.9755632Wh；两轮上下文hash与重放一致。
第一项主机观测133.656秒完成，第二项拒绝且没有创建事务/journal或记录控制命令，容器清理通过。
第二轮能量覆盖117.052秒是PX4 boot时基，不能与主机任务时长直接互换。旧s证据仍保持原报告级范围。
证据见docs/evidence/campaign-t-20260908.json；这完成能量负向集成门，不代表策略已经学习、泛化或实机通过。

原始能量输入留存：新Mission报告的planning_energy_input包含history和input_context，
选择拒绝时planning_energy也保留已成功投影的结果。投影本身失败时不能视为已有合格能量。
audit增加--require-energy，使用第一轮声明的能量参数新建一个累计器，依次重放两轮历史，
核对累计/新增消耗、剩余能量/续航、投影上下文hash与选择器实际上下文hash；不在落地后重置。
该选项要求所有原始字段存在，旧s报告不能通过此选项。本轮5项模拟数据测试覆盖正常重放与
消耗、前缀、上下文篡改和输入缺失；新外部运行尚未验证，不能回填旧报告。

外部低能量负向 s 轮：显式初始33.5Wh（容量100Wh、航电36W、最大推进324W），
第一任务完成后，规划续航从334.592秒降至308.170228秒，第二项因预算不足被拒绝。
`scripts/audit_campaign_rejection.py`接收`--campaign`、`--run-directory`、`--outer-report`，
校验配置输入哈希，重新调用原选择器核对两轮选择/拒绝，核对同容器/同采集起点、完成hash、
第二项无事务/条目/控制命令、磁盘journal不存在以及精确清理记录。输出绑定文件大小和SHA。
这是报告级负向验收：外层campaign仍exit1，不改写为所有任务完成；无网络抓包或实机电池标定声明。
第二项报告只有投影上下文，没有完整能量原始历史，后续仍需补齐累计积分的独立重放证据。

无可行候选的证据：campaign 使用原 select_next_task，不修改可行性阈值。
拒绝时 TaskSelectionRejected 携带 context、context_sha256、rejected（task_id/reason），
原 Mission 失败报告在 selection_rejection 字段保存这些值；其他失败为 null。
该字段本身不是“上传前拒绝”的证明，外部验收还必须核对事务/journal、控制事件及实际候选输入。
自动化测试分别覆盖第二次选择的低电量/低续航输入，以及注入异常后的报告留存；后者不代表实飞时序。
现有模型权重与共享规划器不变；训练开始、模型产物发布和 HIL/REAL 验收仍是独立未完成项。

不同路线配置：`configs/scenarios/vtol-diverse-routes.synthetic-campaign.json`，
第二条路线相对声明的home向西镜像，task中心线与runtime航点同时变换，保留同一安全参数。
r轮实际执行synthetic-east后再执行synthetic-west，两项wire坐标/hash不同，
落地完成、固定home、累计能量及清理后复核均通过。
低电量/低续航测试使用任务完成后的合成上下文，证明共享选择拒绝后不返回第二项编译结果；
未据此声明真实PX4的低能量负向验收通过，也不声称策略已学习或泛化。

q轮实际双任务验收通过（证据campaign-q-20260908）：同一个PX4/Gazebo容器、同一遥测起点，
共享选择器先选synthetic-high，完成后以更新上下文选择synthetic-low。两个候选共用路线，
这是连续执行/排序/累计能量的集成验收，不是泛化能力或学习效果测试。
心跳流在首项准备时通过容器内px4-mavlink设4Hz；端口来自已验证容器的PX4_MAVLINK_UDP_PORT，
检查其UDP绑定，禁止默认固定18570；PX4的not running/error输出必须判为失败。
新鲜度仍为1.5s。q轮可用历史重放9719段无gap，194.38秒PX4 boot覆盖，
5.0623828Wh为显式功耗假设结果；不是完整任务时间映射或物理标定声明。
任务耗时95.015/120.578秒是主机观测时间，不能与boot覆盖相加或互换。

Docker外层现在支持`--campaign-file configs/scenarios/vtol-two-task.synthetic-campaign.json`，
搭配原`--execute`、全新run/evidence路径和五项显式能量参数。
该文件是两个共享路线、不同优先级的合成任务，用于顺序选择/连续能量验证，不是训练集或实机航线。
campaign模式不启用相机门；父进程复算候选选择、运行编译hash、journal并在清理后复查。
后续任务上传后，只有新鲜同源的落地且解除武装状态才允许MISSION_SET_CURRENT到0；
收到第0项确认后继续严格回读。未改规范wire current字段要求，也不因此跳过permit。
复用会话的配置读取及Docker检查现在通过持续采集准备函数运行。
m/n两次外部双任务尝试失败并清理；最新修复仍需复测，详见campaign-docker证据。

连续任务CLI入口：`python -m scripts.vtol_selected_task_campaign --help`。
在原Mission连接/身份/超时/显式能量参数之外，增加`--campaign-file <文件>`和
`--campaign-output-directory <新目录>`；`--evidence`为最终汇总路径。
配置结构为`{"schema_version":1,"tasks":[{"task":<ResolvedTaskSpec>,"priority":<非负整数>,"runtime":<MissionCompileRuntime>}]}`。
任务数1..32，ID唯一；JSON重复键、额外字段、非法版本、task/runtime契约错误拒绝。
CLI要求五项能量参数齐全，并要求原disposable SITL确认；输出路径必须全新。
它附着调用者已经创建的容器，不创建Docker容器；相机ready/complete参数暂不支持，
不得把campaign通过等同单任务相机门通过。Docker外层接线与实际双任务仍待完成。

连续任务入口：`scripts.vtol_selected_task_campaign.run_selected_task_campaign`，接收调用者拥有的
MissionTelemetrySession、TaskCandidate序列、每任务runtime映射、新输出目录和原Mission参数。
每轮在原Mission的能量投影后通过共享select_next_task选择，再由共享runtime编译器编译；
原Mission重新检查容器身份/新鲜度并执行事务。报告门通过且共享outcome为同一mission hash的completed，
才移除候选。失败向调用者传播，不继续下一项；语义不一致会标记会话失败。
不关闭调用者会话、不重置能量、不创建容器、不复用任务证据路径。
当前测试使用模拟Mission执行器，只证明驱动排序/新上下文传递/停止条件；真实双任务与外层CLI仍待验证。

外部Mission的Python入口新增`selection_provider(context) -> SelectedMissionInputs`。
回调在能量投影后收到原上下文，返回任务、共享编译结果、几何与策略；收到None或其他类型会拒绝。
执行端复核计划/编译一致性，策略必须等于原验收策略，任务预算不能超过执行超时。
回调在持续采集遥测的准备阶段运行，不应操作MAVLink连接；完成后以采集器时钟再检查新鲜度。
不提供回调时保留既有单任务路径。该接线尚未证明外部连续任务运行。

`build_selected_acceptance_documents`接受共享PlannedMissionCompilation及其原task/context、
运行geometry/policy和身份hash。先验证上下文双时钟新鲜度与运行机型，重新计算共享计划及编译结果作精确比较，
再把**原对象**交给与单任务相同的事务文档绑定函数。任何不一致均拒绝，不用复算产物替换原产物。
该入口只构建SIM测试文档，不打开传输、不上传、不代表人工许可；连续任务驱动仍待接入。

同一MissionTelemetrySession现固定首次合格GPS的**规划原点**；它不是新观测的PX4 HOME_POSITION，
也不发送设home指令。后续aircraft_position仍来自实时GPS，home_position/围栏/任务几何保持原点，
防止GPS抖动改变累计会话身份。真实飞控home与规划原点的一致性仍需原有验收，不由此项自动证明。
绑定只允许健康active会话；失败/关闭后拒绝。外部双任务执行及共享选择回调仍待接通。

外层 `scripts/run_vtol_mission_sensor_capture.py` 现已支持下述全部五项能量参数，
并在准备路径和创建Docker容器前验证。省略全部参数仍走观测续航路径；缺测时拒绝规划。
2026-09-08 l轮用显式合成配置完成单任务与固定翼相机采集；不代表多任务闭环或训练效果。
初始配置100Wh、航电36W、推进上限324W，规划时剩余99.95784Wh，
这些数字仅用于接线验证，不是实机推荐值或标定值。可复核命令见证据文件。
上下文采样时刻现在保留原始单调时钟精度，避免取整后落在同刻能量样本之前。
以下“外层未转发”的叙述为上一轮历史。

最新入口增量：`scripts/vtol_sitl_mission_acceptance.py` 支持同时提供
`--energy-capacity-wh`、`--energy-avionics-power-w`、`--energy-propulsion-max-power-w`、
`--energy-initial-remaining-wh`、`--energy-provenance`。参数无默认值，须来自明确的模拟配置；
初始Wh定义在首个完整能量样本，并非PX4启动时刻。使用这些参数仍需原disposable SITL确认及全部容器/任务身份参数。
它们进入已有SimulationEnergyProfile与累计状态，不改变共享规划器。外层启动器尚未转发，
尚无新外部验收结果；下面CLI未接描述为上一轮历史。参数数值不能声明为真实电池标定。

外部验收规划路径纠偏：发现原脚本手工拼接 PlanStep，现改为共享 plan_task；不再用 mission_timeout 作为剩余续航。观测续航须来自1/1新鲜电池消息；可复用会话支持显式 energy_profile+initial_remaining_wh，累计状态在原上下文/计划哈希绑定前投影。新增12项回归，相关292 passed。协议测试明确1800s合成续航/600s任务预算，另保留低预算拒绝；未改共享规划阈值。尚无新外部运行/全量；CLI与orchestrator未接显式能量配置，PX4续航缺失时旧命令现在会在上传前拒绝，不能沿用j轮通过结论。

Python会话入口：`MissionTelemetrySession(..., energy_profile=profile, initial_remaining_wh=...)`，
profile复用原SimulationEnergyProfile，初值定义在第一个完整样本处，不是假装电池实测。
会话借给原run_mission_acceptance后，会在采集上下文后截取累计历史，再通过project_context、
原plan_task和原PX4编译链生成任务；报告planning_energy保存输入/投影哈希与功耗来源。
原CLI尚无这些选项。后续需补CLI/orchestrator配置与同一home绑定，不应靠增加timeout绕过续航缺失。

复用会话的成功报告写入/输出现在通过现有工作线程辅助执行，主线程持续独占接收；任务占用直到报告输出完成才释放，发布或接收异常会阻止下一任务并等待工作结束。能量历史新增显式容量：单任务默认8192不变，复用会话默认65536，上限131072；超限仍导出丢弃数量，累计重放拒绝截断，不宣称无限连续。新增9项回归，相关264 passed；尚未新外部双任务/全量回归。

`MissionTelemetrySession(..., energy_history_limit=65536)` 为显式有界配置，
`TelemetryCollector(..., energy_history_limit=...)` 接受1..131072整数，布尔/浮点拒绝。
报告包含该次任务结束时的历史快照，发布过程中收到的新样本保留于同一 collector，供下一次累计观测消费。
失败任务保持原写失败报告/退出路径并禁用会话复用，不以后台采集宣称故障恢复。
下一步接实际双任务执行与累计能量观测；时效/截断拒绝继续保留，增加容量不等于完成连续性验收。

外部任务生命周期拆分：MissionTelemetrySession 持有一次 loopback 连接/TelemetryCollector，run_mission_acceptance 可借用；成功后保留采集起点和历史，失败后禁止复用，连接由外层显式关闭。容器仍由原 orchestrator 管理，任务来源/所有权/协议门保留；已有证据路径拒绝覆盖。7 项新增回归、相关238 passed，包含原函数失败写报告并 SystemExit(1) 的清理验证。未运行外部双任务；报告输出期间接收与8192条历史上限仍需处理，未重跑全量。

复用入口为 `with MissionTelemetrySession(container_id=..., evaluator_port=...) as session`，
每项调用原 `run_mission_acceptance(..., telemetry_session=session)` 并使用独立证据/事务文件。
该接口仅供同一调用者顺序使用；创建 session 不代表获得任务授权，各次任务仍检查容器与执行事务。
当前不得直接作为完成的多任务 runner：需保证任务间/报告输出持续 pump、历史不截断，
并将累计能量投影接到下一任务真实观测，而不是重用旧任务 timeout 作为续航。

累计能量通过 project_context 接入既有 ContextSnapshot：仅 SIM 传感器适配，复用原任务循环/planner/PX4 编译与完成监控；只替换电量和假设续航，不刷新时间戳或位置/GPS/围栏/感知质量。上下文和最后有效能量时钟样本均要求现有 1.5s 时效，拒绝未来、换会话、机型/home改变；失败不提交累计状态。10项新增回归、相关84 passed，双任务回调 fixture 已验证第二项绑定第一项后的能量快照。未进行外部连续双任务，未重跑全量、未训练；调用方仍须绑定同车会话与 collector-relative Clock，不构成来源认证。

`state.project_context(context, history, session_id=..., clock=..., runtime_mode=RuntimeMode.SIM)`
返回 `(projected_context, energy_evidence)`。上下文快照 ID/时间与全部非能量字段保留，
能量证据带输入/输出 context 哈希和原始能量接收时间；不是任务完成判定或新执行许可。
SIM/HIL/REAL 的规划器仍同一实现；差异仅在 SIM 从显式功耗假设产生观测，HIL/REAL 不接受该模拟来源。
下一步把外部任务回调生命周期拆成同一容器内可重复的任务执行段，再验证两任务间连续采集和待机耗电。

同一外部模拟会话的累计能量状态已加入现有 energy_replay：CumulativeSimulationEnergy 复用原 replay/flight_model 功耗函数，初始 Wh 显式绑定首个完整样本，追加历史计算增量，重复输入不重复扣账。修改旧样本、缩短历史、换会话/时钟起点、截断或时效缺口均拒绝且不改变已接受状态；解除武装不补能，归零导出未满足能量需求，不声称掉电动力学。新增8项回归，相关55 passed。j记录两批重算（非两次任务）：10Wh测试初值→8.7754996→7.8331452Wh，重复新增0Wh。当前仅进程内状态，尚无磁盘恢复、实时多任务接线或补能入口；全量未重跑。

调用 `CumulativeSimulationEnergy(profile, session_id=..., initial_remaining_wh=...)`，
然后对同一会话累计的 `energy_input_history` 调用 `observe(history, session_id=...)`。
返回剩余 Wh、百分比及按 profile 满功率估计的续航；不预扣 planner 储备，不修改真实遥测。
这个数值只是模拟假设，不是电池测量；session_id 由调用者绑定，不是来源认证。
不能在新任务边界重新创建实例或只提交裁剪后的任务窗口，否则会丢失待机消耗或触发连续性拒绝。
下一步运行适配器需将该状态与新鲜、同车的任务后观测关联；当前未向共享任务循环注入快照。

窗口能量增量已实现：复用 SimulationEnergyProfile.consumed_wh 与因果左侧负载，严格内部区间计入下界，接触/跨越接收边界的区间计入上界；相同接收时刻也保守视为边界，不插值两种时钟。完整前缀积分与原 replay 结果交叉核对，缺失窗口端点不产生区间。18 项相关测试通过。j 原始记录重算为 2.1126148–2.1132548Wh，边界量 0.00064Wh（3 区间、0.064 boot 秒），不是全前缀 2.1668548Wh。仅在测试功耗/接收归属假设下成立，不是物理误差界限，不授权下一任务。

输出 `window_energy_envelope`；`window_energy_wh` 仍为 null，避免暗示精确点估计。
上下界不包含功耗模型误差、网络延迟、传感器时间偏移，不能用于宣称实机续航可靠性。
下一步将有明确来源的保守模拟能量状态接入多任务观测；未标定数据不能自动解锁实机。

j 轮任务/画面采集与清理通过（任务 101.594s）。5584 条能量输入因果前缀 5069 区间、101.380s PX4 boot 时间无时效中断；ARM 请求至完成接收窗口 18.047–120.750s，两端由前缀样本包围（boot 11400/107404ms）。新增只读 audit_mission_energy_window.py 从原始绝对边界重算，不信嵌入通过标记，不以全前缀能耗冒充窗口能耗。7 项新增回归，相关 16 passed。物理同步、窗口能量、下一任务授权仍未声明。
见 [输入身份与审计结果](evidence/mission-window-audit-20260908-j.json)。
下方“需要新外部记录”是历史阶段；下一步计算窗口区间能量并显式处理边界采样误差。

任务时间窗口已显式记录：能量历史输出实际 collector 的宿主单调时钟起点，任务报告保存 permit 后 ARM 命令前至接受落地/解除武装观测的窗口，并保存飞行监控自身起点。缺失、非有限、布尔或倒序边界不生成相对窗口。新增 10 项测试，相关 240 passed；这只是接收时间绑定，仍不声称 PX4 boot 对时或完整能量覆盖。旧 i 轮缺少这些字段，不回填推断；需要新外部记录。

新增字段：`energy_input_history.receive_origin_host_monotonic_s`、顶层 `mission_receive_window`
及 `flight.observer_origin_host_monotonic_s/completed_host_monotonic_s`。
窗口相对值只按同一宿主单调时钟作差，不能与旧 transaction 相对时间或 PX4 boot 秒直接相减。
下一步验证 GLOBAL_POSITION_INT 样本是否包围该窗口，并按因果状态积分；尾部不足时继续标明未覆盖。

外部 i 轮已通过：任务 84.547s、画面采集门通过且精确 ID 容器清理成功；4433 条输入因果重放覆盖 4023 区间/80.456s PX4 boot 时间，没有 1.5s 空档停止。测试 profile 积分 2.5506572Wh，不是物理标定或全任务边界证明，仍不授权下一任务。h 轮此前未收到启动心跳而失败，原报告保留并已清理。见 [本轮证据](evidence/preparation-telemetry-20260908.json)。

准备阶段接收修复：四条既有参数命令与任务文档生成在单个工作线程执行，主线程继续独占 collector.pump；参数设置完成后重新取得上下文。子进程原 10s 超时保留，任何退出均等待工作结束，防止准备操作与容器清理交错。新增 3 项线程/错误回归，相关 230 passed；未改变模型、参数值或任务安全门。此次变更后的全量回归未重跑，之前 3921 passed 属于前一版源码。

## 协议接收连续性修复

任务协议等待已接入现有 collector.pump，上传请求、回读 COUNT/ITEM 等待期间的遥测进入同一记录器；无关消息不刷新超时、不消耗协议重试。新增 3 项回归，相关 227 passed，Ruff 通过。尚未外部重跑，不能断言已消除 g 轮 1.516s 空档，也未完成全任务能量覆盖或策略训练。

原带类型过滤的 recv_match 在等待任务协议时绕过采集器，是可从源码确认的漏记路径；
修复后由采集器先记录所有收到的消息，协议层再选择所需类型，原来源/目标/INT-only/hash 校验保留。
回归包含交错消息只记录一次、连续无关消息仍在 1.5s 到期、旧 MISSION_REQUEST 仍被拒绝。

## 外部记录因果能量重放

`simulation.energy_replay.replay_energy_inputs(history, profile)` 与原飞行模型复用
`SimulationEnergyProfile.consumed_wh`。每个区间使用左侧 boot 样本已知的武装/油门状态，
不把后到状态追溯到过去；按 PX4 boot dt 积分而非宿主 dt。最大接收间隙、负载年龄及 boot 间隔
均为 1.5s，错源、无效字段、回退/重复时钟、截断历史会拒绝或停止。SYSTEM_TIME 不与另一时钟流交错积分。
输出明确是已覆盖前缀，首个完整样本之前及停止之后不估算，不授权任务。

g 轮原始数据实际结果：131 个区间、2.62s，接收空档 1.516s 导致左右负载不再满足时效，
停止时负载年龄 1.719s。测试 profile 下 0.0262Wh，不是校准参数或全航程能耗。
见 [输入哈希与结果](evidence/causal-energy-replay-g-20260908.json)。相关 112 passed；全量未重跑。
下一步定位接收空档与任务覆盖边界，不能从后续状态填补过去，也不能放宽规则把失败变通过。

g 轮实测输入已取得：6066 条无截断，5508 条 GLOBAL_POSITION_INT、112 条 HEARTBEAT、446 条 VFR_HUD。
全部字段有效且来自 1/1，油门 0–93%；SYSTEM_TIME 未收到，不能把缺失当作已采集。
GLOBAL_POSITION_INT 的 PX4 boot 时间无倒退/重复，首尾差 111.416s，而宿主差 136.953s。
这提供了后续关联的原始数据，但尚未证明与 Gazebo sensor 时间等价，未对该记录执行能量积分。
任务 130.484s completed，清理完成；详见 [原始输入身份与统计](evidence/energy-input-20260908-g.json)。

外部输入采集：post_mission_observation.energy_input_history 新增最多 8192 条独立记录，覆盖
HEARTBEAT.base_mode、VFR_HUD.throttle、SYSTEM_TIME/GLOBAL_POSITION_INT.time_boot_ms。
每条记录来源、独立宿主接收时间和字段有效性；0 启动时间保留为潜在复位，禁止当连续时钟直接积分。
PX4 boot 时间未假定等于 Gazebo sensor 时间，油门是设置而非电功率；未做同步/积分声明。
新增 11 项非法字段、外来源、时钟复位保留及截断回归，相关 223 passed。以前全量结果属于本次改动前。

当前全量验证：3898 passed / 7 skipped / 0 failed（203.460s），全仓 Ruff 通过。
跳过项均依赖 Windows 符号链接创建权限，不计为通过；源码在测试过程中未修改。
报告身份、对应五组 parity 和跳过原因见 [全量证据](evidence/energy-scenario-full-20260908.json)。
当前外部负载来源候选为 VFR_HUD.throttle（0–100% 油门设置，不是电功率），需与武装状态和
模拟时钟做因果关联；依据 [MAVLink VFR_HUD](https://mavlink.io/en/messages/common.html#VFR_HUD)。
本轮未添加这条外部关联，未以全量软件测试替代外部或实机验收。

兼容性补充：启用能量 profile 时，BATTERY_STEP_DROP 是当前电量的观测偏差，不是固定百分比覆盖。
底层持续放电，有限故障结束后恢复当时的真实模拟状态；未启用 profile 的旧 fixture 保持原行为。
新增回归涵盖 20→10→5 的底层状态与 10→0→5 的故障观测，相关 81 passed。
外部输入审计：现有 Gazebo wire-v3 提供 sensor_stamp_ns，但相机遥测适配器 armed=None，
没有油门字段。外部负载适配必须加入同一会话、因果时效受限的状态输入，不能以像素帧或宿主时间猜测。

## 场景配置入口（确定性软件 runner）

在场景 runtime JSON 的 `vehicle` 中可加入以下字段（示例数值是测试假设，不是推荐实机参数）：

```json
"energy_profile": {
  "capacity_wh": 10,
  "avionics_power_w": 36,
  "propulsion_max_power_w": 324,
  "provenance": "training fixture assumption; not physically calibrated"
}
```

容量必须正数，功率非负且总功率为正，来源不得空白；额外字段、布尔数值、NaN 等被拒绝。
修改后必须按既有 `canonical_sha256(runtime_document)` 更新 manifest runtime 通道哈希，
不是直接对文件字节散列。机型与运行输入身份随配置变化，目标真值仍只留在 evaluator 通道。
既有 `SimulatorScenarioRunner` 自动将配置传给 `SimulatorClosedLoop` 和原飞行模型，证据保留能量假设。
省略字段保持历史固定电量 fixture；训练能量策略时必须显式提供它，不能把省略视为已校准默认值。
磁盘加载、篡改拒绝、重复确定性和非法配置已测试，相关 144 passed；外部 Gazebo 尚未消费该配置。

## 跨架次能量：现有飞行模型扩展

`simulator/flight_model.py` 中新增 `SimulationEnergyProfile`，通过现有
`DeterministicFlightModel(energy_profile=...)` 启用，不新建并行飞行模型。
容量 Wh、航电 W、最大推进 W、provenance 均显式提供，无默认伪标定参数。
每个运动步长使用 `P = 航电功率 + (武装 ? 油门 * 最大推进功率 : 0)`，
消耗 `P * dt / 3600` Wh。该线性负载近似不是电机/电池物理仿真。

- `battery_remaining_pct` 原遥测字段反映连续状态，降落/解除武装不会恢复电量。
- `energy_observation()` 导出参数来源、剩余 Wh 和按满功率估计的续航；不预扣 planner 的返航储备。
- `recharge_energy(added_wh, reason=...)` 只允许地面、静止、未武装，限制容量并记录实际补能量。
- 启用后 `set_battery_remaining` 禁止覆盖，防止重新接入 PX4 复位百分比；未启用保持旧 fixture 行为。
- `reset()` 是显式重开整个模拟 episode，恢复初始状态；任务/架次边界不得调用它。
- 电量耗尽钳制到零；尚未实现物理掉电/滑翔动力学，不能宣称坠落行为有效。
- 57 项相关测试通过，覆盖三机型跨架次、待机耗电、油门功耗、充电限制/事件、确定性、非法配置。

尚未接到外部 Gazebo、场景 manifest 或自动多任务循环；不得用本地模型通过声称外部能量闭环完成。
下方“持久能量状态待实现”等为此前阶段的记录。

最新 f 轮：42 条电池记录无截断，任务与采集通过，88.578s completed、清理成功。原始电流恒 -1、
累计电量 0、累计能量 -1、剩余时间 0，不足以估算续航。容器 px4-ver all 返回 commit
`83c4f4e580558816f0e398918f66358988004696`，其
[BatterySimulator.cpp](https://raw.githubusercontent.com/PX4/PX4-Autopilot/83c4f4e580558816f0e398918f66358988004696/src/modules/simulation/battery_simulator/BatterySimulator.cpp)
显示按 PX4 时钟固定放电、无电流传感器、解除武装内部电量复位到 1，并有最低电量钳制。
这是当前版本源码行为核验，不是二进制可复现构建证明，也不是物理电池标定。
下一步必须保持跨任务模拟能量状态，只有显式补能才恢复；通过既有观测接口送入共享 planner。
详见 [f 轮证据](evidence/battery-history-20260908-f.json)；下方未核验动力学为历史阶段状态。

能量输入补齐：VTOL 接收器的 post_mission_observation 现在包含 battery_history（上限 8192 条），
保留 system/component/battery ID、宿主接收时刻和原始 cA/mAh/hJ/s 字段；截断数量显式导出。
不能将宿主耗时当 SITL 模拟耗时，不能把未知哨兵解释为零耗电/无限续航。
字段单位与哨兵见 [MAVLink BATTERY_STATUS](https://mavlink.io/en/messages/common.html#BATTERY_STATUS)。
PX4 文档将 SIM_BAT_DRAIN 标为秒、SIM_BAT_MIN_PCT 标为最低百分比，
但 [v1.12 参数说明](https://docs.px4.io/v1.12/en/advanced_config/parameter_reference#SIM_BAT_DRAIN)
不足以证明当前固定镜像的完整放电动力学。仍需绑定当前镜像源码/实际模拟时间验证后再拟合。
新增 8 项回归，相关 212 passed；连续任务续航输入尚未生成，采集不构成校准。

发布身份更新：task_selection、task_session、mission_outcome、task_episode 已绑定到共享核心哈希。
新增模块修改将使旧核心证据失效，且规范清单测试现在检查遗漏文件。旧 v197 四模式证据均已明确失配，
没有改写旧报告哈希；当前 E7 尚未重新验收。此绑定不等于模型训练或四模式运行接入完成。

最新外部验证 e 轮：任务与 3 帧采集门通过，共 340 条旁路事件；共享监控器在 116.234s 判定
completed，1.140s 的领先消息在既有 1.5s 时效内获得确认，容器清理并证明不存在。
飞后电量 72%，剩余续航仍无有效值，未授权下一任务。只读查得模拟参数 SIM_BAT_DRAIN=60、
SIM_BAT_MIN_PCT=50，未改参数、未核验单位/动力学；此前 50% 尾部记录不代表固定电量或真实能耗。
见 [e 轮外部证据](evidence/freshness-confirmation-20260908-e.json)。单轮通过不代表多场景稳定、
连续任务、训练或实机验收完成；下方“新外部运行待验证”均为历史阶段记录。

最新时效规则：d 轮实测 current 晚 406ms，250ms 上限不适用。现在确认等待复用既有
`Px4MissionFeedbackConfig.maximum_age_s`（1.5s），到期未确认拒绝，仍只容纳 1 条领先 1 项消息。
c/d 原始事件重放均通过；原始失败报告未改，新外部运行待验证。见
[时效确认证据](evidence/confirmation-freshness-replay-20260908.json)。下方 250ms 为历史实现。

消息顺序更新：结果监控器允许一条同源、仅领先 1 项的 reached 等待 current 确认，期限 250ms，
重复不延长。确认后原严格跟踪器使用确认处理时刻，独立审计保留原接收时刻；不宣称重排后的处理时间
是原始接收时间。c 轮 285 事件重放 completed（101.141s），仅一次约 31ms 确认；新外部运行待验证。
参见 [重放证据](evidence/mission-order-replay-20260908.json)，不足以证明多场景稳定性或连续任务完成。

最新 c 轮实测：任务完成，但严格旁路因 reached=1 先于 current=2 到达而锁定失败；后者晚约 31ms。
后任务五类观测新鲜且同源，电量 50%，PX4 time_remaining 无有效值。新增采集字段保留缺失，
不能将任务超时上限当剩余续航。需先解决有界消息时序处理及带来源的续航估算，才可接连续任务。
证据见 [后任务输入实测](evidence/postflight-observation-20260908.json)；先前成功不是稳定性证明。

观测驱动循环：`autonomy/task_session.py::run_observed_task_session` 复用选择器、标准编译和
MissionOutcomeMonitor，后端回调返回观测快照。只有 completed 且快照时效、时间域、机型/home
一致才继续；等待/失败/超时、缺失或错误快照停止，不使用预算生成下一电量。每次后端调用仍需既有
事务与确认流程，本循环没有上传、武装或授权操作。7 项新增回归通过（mission planning 共 23 项）。
尚待外部多任务 Gazebo 后端接线；它与 `simulate_task_budget_episode` 的估算用途明确分开。

最新验证（2026-09-08）：外部 headless VTOL 任务成功，3 帧画面保存，新增旁路记录 269 条事件，
86.421 秒判定完成；原事件离线重放的每次接受结果及最终判定一致。首次运行暴露像素门与 manifest v2
不兼容，修复后重新运行成功，未修改失败报告。详情及重跑命令见
[外部旁路证据](evidence/sim-outcome-shadow-20260908.json)。尚未训练策略或由任务循环消费执行结果。

用户确定的方向：自主任务决策与航线策略；不是视觉检测微调。训练阶段使用模拟环境，部署阶段加载
同一候选策略文件，保留原有实机验收门。产品仍使用 `0.3.0-pre.8`。

## 必须保持一致的部分

- 一个共享策略实现和一份版本化参数/权重文件；SIM/HIL/REAL 不允许各自维护策略分支。
- 同一观测契约、特征顺序、单位、归一化、缺失值与时序处理。
- 输出平台无关任务决策/航线计划，经过现有 planner、能量/地理围栏等安全约束，再走统一 PX4 Mission 编译路径。
- 模型加载必须绑定文件 SHA-256、契约版本及训练输入身份；模型过期/不兼容时拒绝加载。
- 训练器可读取仿真真值计算奖励；策略推理输入不得读取目标真值、未来帧或隐藏模拟状态。
- 只替换传感器、时钟和动力学后端；训练完成不会自动启用实机控制或修改发布状态。

## 当前代码事实

`src/multidetect/autonomy/planner.py` 是跨模式共享的任务规划核心。现有
`src/multidetect/simulator/ai_executor.py` 中的 `DeterministicSimulatorAiPolicy` 只用于模拟执行器，
输出有限 MANUAL_CONTROL 轴值，不是已经实现的共享学习型航线模型。不得把它的测试通过报告为
同一训练模型在 SIM/HIL/REAL 完成接入。

## 增量扩展约束（2026-09-08 用户补充）

在现有模型和运行链基础上增加功能，不另建平行产品，不以新策略替换已有感知能力。
现有权重的感知职责与 planner 的任务职责保持分层；不能把检测权重直接称为已有航线策略权重。

- 保留现有检测、跟踪、测距、定位及任务执行接口；新能力消费已有观测并接入共享 planner。
- 若涉及权重微调，从经核验的现有 checkpoint 初始化，记录父模型路径/版本/SHA-256、
  新增模块、冻结/可训练参数与训练配置；禁止静默换成随机初始化模型或无关底座。
- 当前没有证据证明已存在可继续训练的共享学习型航线权重。新增决策模块应明确标为扩展，
  不能声称是在微调一个不存在的航线 checkpoint，也不能擅自替换现有视觉权重。
- 候选保存为新版本，原模型保留可回滚；SIM/HIL/REAL 加载同一候选组合与前后处理。
- 验收必须同时覆盖旧功能回归与新增能力收益；新增功能失败或候选不兼容时不得绕过既有安全拒绝。

优先新增：多任务优先级与访问顺序、约束内航线候选选择、环境/任务变化后的重规划建议，
以及决策理由与回放记录。以上是待实现范围，不表示已经上线；任务确认和执行继续沿用现有链路。

## 实施顺序

### 已实现的增量基线

`src/multidetect/autonomy/task_selection.py` 提供 `TaskCandidate` 和 `select_next_task`。
输入现有 `ResolvedTaskSpec` 候选、同一个 `ContextSnapshot` 和机型；输出确定性排序、原 planner
的估算与原样计划、上下文哈希和拒绝原因。优先级最高，再比较耗时/距离/ID；排序层没有降低安全门的接口。
这是离线 API 基线，不是已训练权重，也尚未接入 UI/运行循环或新增发布 parity 文件清单。
现有 planner 不变，候选计划仍需原有下游验收与任务确认。每项是从当前快照出发的独立完整任务，
不能将排序结果直接拼接执行；任务完成或上下文改变后必须用新快照重算。

回归命令：`python -m pytest -q tests/test_autonomy_planner.py`（32 passed，包含 7 项新增测试）。
覆盖确定性平局、计划不变、优先级/耗时排序、超能量候选拒绝、GPS/围栏/低电量/感知失效、空集和重复 ID。

### 后续接入

`autonomy/mission_outcome.py::MissionOutcomeMonitor` 增加共享只读结果判定 API，复用
`Px4MissionFeedbackTracker`、注入 Clock 和绑定的 ExecutableMission。完成要求合格的末项到达反馈，
以及其后同源、新鲜的 landed=true/armed=false 样本；相同时间戳下的先后顺序也会清除旧落地样本。
超时/错误结果锁定，迟到消息不会改成成功。当前等待不代表失败，缺失/过期反馈最终由期限判超时。
生命周期适配器必须提供真实、同一车辆的相干观测，不能用缺省布尔值代替；本 API 不验证物理来源认证。
新增 10 项测试，反馈模块共 25 项通过。尚待 Gazebo 事件适配与任务循环接入，不作为已实现外部闭环。

原始消息适配已加入同一 `ingest_message`：接受 MAVLink-shaped mapping 或解码后的 pymavlink
对象，解析 HEARTBEAT.base_mode 和 EXTENDED_SYS_STATE.landed_state。字段必须存在且为合法整数，
来源必须匹配；未知落地状态不视为已落地。两条消息分别保留接收时刻，配对时使用较旧时间，
不能通过新消息刷新另一条旧状态；首次末项到达清除此前缓存，要求两条状态重新观测。
新增 11 项适配测试，反馈模块累计 36 passed，包含实际 pymavlink 包编码/解码（仍非网络接收）。
尚未挂接外部 Gazebo receive loop；模型训练、UI 和发布 parity 接入仍待完成。

后续接入更新：`scripts/vtol_sitl_mission_acceptance.py` 的 `observe_mission_flight` 已接旁路监控，
生产调用传入本次实际编译的 ExecutableMission，按接收循环的 elapsed 时间推进 ManualClock。
报告 `shared_outcome_shadow` 保存相关消息字段、接收时间、是否接受和锁定结果，明确
`affects_acceptance_gate=false`；不替换旧门、不增加控制命令。传入的可变证据对象在原循环抛错时
仍由总报告保留，正常退出也保存最后结果。缺失 executable_mission 的旧调用兼容但不声明监控已启动。
28 项 VTOL 适配回归通过，包含旧缓存状态成功而消息字段缺失时旁路失败的对照。
这是接入代码及模拟接收器回归，尚未运行新的外部 Gazebo 任务；前述未接 receive loop 为历史状态。

新增 `simulation/task_episode.py::simulate_task_budget_episode`，把选择器接入确定性预算模拟循环。
每次任务先通过现有 `compile_planned_task_mission_from_runtime` 生成 PX4 Mission，再以 planner
的保守预计耗时推进现有 `ManualClock`。剩余续航扣减耗时，电量按当前电量/剩余续航的恒定消耗率更新，
回到 home、速度归零后使用新 `ContextSnapshot` 重选。保留每步 before/after/编译产物和最终拒绝原因。

这一阶段仅描述独立 RETURN 任务的预算预测，不发送 MAVLink、不计为实飞完成、不声称 Gazebo 动力学验证。
恒定耗电近似尚未标定；未建模充电、周转等待、任意落点或空中连续任务。LAND 完成类型明确拒绝；
缺失/错误编译输入直接失败，不把无法编译的计划计作完成。初始上下文不修改。
新增 5 项测试（含三机型参数化）验证确定性、多轮状态/能量更新、PX4 绑定、无电量补满、
不足时停止、编译错误及无可行任务；`tests/test_mission_planning.py` 共 16 项通过。

1. 定义共享策略观测/动作契约与候选模型加载器，接入任务规划核心；固定安全约束不作为可降低的奖励项。
2. 模拟任务环境使用相同规划核心，加入风、观测噪声/延迟、能耗、障碍和任务变化；按场景/种子隔离训练、验证、测试。
3. 以现有确定性 planner 为基线，训练任务优先级与航线决策；比较完成率、航程、耗时、能耗、约束违反及返航余量。
4. 固定留出场景与压力场景评估，失败案例回归；只保存可复现的候选，不以训练奖励上涨宣称泛化优化。
5. PX4/Gazebo 闭环验证同一候选文件，再做 HIL/REAL 验收；避免训练器接入真实端点。

## 本轮环境验证（2026-09-08）

- 本机 RTX 4060 8 GB，Torch `2.6.0+cu124` 检查 CUDA 可用。
- Docker Engine 可用；固定 PX4/Gazebo 镜像成功启动，采集 60 帧并保存，时间戳检查通过。
- 原始证据：`artifacts/evaluation/sim-training-bootstrap-199.json`；运行目录
  `artifacts/evaluation/gazebo-sitl-camera-20260908T023520Z-69040/`。
- 一次性容器 `multidetect-sim-training-bootstrap-199` 已由脚本停止，未留下后台模拟进程。
- `.venv-train` 使用 system-site-packages 复用本机 CUDA Torch，并安装了视觉方向调查时所需的
  Ultralytics `8.4.92`；不属于完全隔离的可移植锁定环境。用户随后选择航线决策，未继续视觉微调。
- 尚未训练新的决策模型，也未证明候选优于基线。下一步是共享策略契约和模拟任务训练环境，不能跳过这一步。

## 2026-10-05 红车 detector 增量训练与外部 SIM 复验

本轮训练目标不是新建 OnboardAI 或飞控模型，而是沿用仓库现有 YOLO26n checkpoint，修复远距离
小目标候选的训练数据并生成可回滚 detector candidate。v1 复测发现：随机粘贴车辆时，原始车辆
仍留在背景但没有标签，造成真实帧上的反向监督；v2 已在生成器中对背景目标做遮罩，并以 20%
正样本保留原始位置并正确标注。

- 数据集：`artifacts/training/red-car-detector-v2`，`600 train / 120 val`，10% negative；
  audit `clean=true`，全部框为 COCO `car` 类。
- 训练：Ultralytics `8.4.92`、Torch `2.6.0+cu124`、RTX 4060、父模型仍为
  `artifacts/models/coco-yolo26n-traditional/yolo26n-traditional.pt`。
- 产物：`artifacts/models/red-car-detector-v2/yolo26n-red-car-v2/weights/best.pt` 和 raw
  `best.onnx`；`production_approved=false`、`candidate_promoted=false`。
- 独立原始帧复测：v2 在两帧上约 `0.966`，框与 parent 一致；v1 无有效 car，说明修复了
  数据标签回归，而非只调整 detector 门限。
- 外部 r29：同一 PX4/Gazebo Docker 和同一 OnboardAI policy 完成起飞、真实 RGB/Depth 推理、
  红车匹配、GPS 记录、Mission、RTL、Home 和 REPORT；详见
  `docs/evidence/external-red-car-autonomous-20261005-r29-v2.json`。

训练指标和 r29 仅作为候选/软件 SIM 证据；它们不等价于 Jetson、Pixhawk、HIL、室外 GNSS、
实飞或最终 `0.3.0` 发布验收。
