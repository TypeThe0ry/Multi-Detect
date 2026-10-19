# 步间三角化与地面不确定度融合

这是方案 B2 的几何部分。**机上策略自 2026-10-18 起默认开启**（地面高度 sigma 默认 5 m），不加载模型，不使用 GPU，也不包含单目深度推理。依据见文末“地面偏 2 m 闭环对比”。

飞机从不同位置看同一静止目标时，可以把几条视线交会成一个位置。地面高度越不确定，地面相交位置的权重越低；多视角条件不足时，直接退回原来的框底边地面相交位置。

## 如何配置

`scripts/onboard_policy.py` 不传配置时等价于：

```json
{
  "step_triangulation": true,
  "step_triangulation_max_views": 8,
  "ground_height_sigma_m": 5.0,
  "step_triangulation_sigma_scale": 2.5
}
```

机上策略的默认值（`POSITION_DEFAULTS`）就是这四项；直接构造 `LoopConfig` 时 `step_triangulation` 仍默认 `false`、`ground_height_sigma_m` 默认 1.0，供旧回放与测试使用。要关掉就显式写 `"step_triangulation": false`。开关必须是 JSON 布尔值；视线数必须是 2～64 的整数；地面高度 sigma 必须是有限正数；三角化 sigma 系数必须有限且至少为 1。字符串 `"false"`、`true` 充当数字、非整数窗口和 NaN 都会被拒绝。

适合先在静止目标、相机标定与位姿时间对齐可靠的 SIM 回放或受控测试中开启，特别是接近、侧移、绕飞时。只有转头、原地悬停，不能产生测距基线。只有任务提供了可信的地面模型（高程图或测距）时才应把 `ground_height_sigma_m` 调小。

## 位置在闭环中用在哪里

| 用途 | 第一版选择 | 原因 |
|---|---|---|
| 每帧检测关联、外观距离归一化、地面重识别 | 继续用 `track.ground` | 这些决定对象身份；新几何不能改变关联结果。 |
| 地点身份记忆、拒绝候选复查、复查目标匹配 | 继续用原地面锚点 | 防止融合位置移动导致旧身份记忆错误复用。 |
| 确认报告 `position_estimate_ne` | 用新位置估计，有失败则回退地面 | 改善静态目标位置报告。 |
| 计数空间去重 | 使用确认记录中的新位置 | 与报告位置保持一致；共同可见、外观与去重半径规则保持原样。 |
| 接近目标、短暂丢失后的接近 | 使用新位置的水平距离和方位 | 让目标中心位置直接参与接近；避障、步长、下降和视野约束继续生效。 |
| 找到目标后的绕飞中心 | 沿确认记录传给 mission finding 和绕飞中心 | 绕飞围绕估计的目标，而不是旧地面投影。 |
| 任务摘要、任务报告/下传、policy 确认标签 | 沿 finding 传递位置和附加诊断 | 计算结果与实际报告一致，不丢弃来源信息。 |
| 动态 `follow` | 强制关闭静态三角化 | 移动目标的历史视线通常不会交于同一位置。 |
| 巡检/监控告警的地点聚类 | 保留地面锚点 | 聚类同时承担身份和地点关联，第一版不改此判定。 |
| 巡检/监控告警的位置报告 | 开启且有估计时使用新位置和诊断 | 报告与任务摘要一致，聚类成员和重复告警判定仍用旧地面锚点。 |

关闭时不会采集视线、不调用新增相机射线回调，也不会新增确认/计数记录字段。`track.ground` 仍是每帧框底边地面相交点，开启时也不会被融合结果覆盖。规划与身份 prompt 没有修改。

## 算法和回退

每条 Track 保存最近 N 个动作步内、最多 N 条观测：相机中心 NED、框中心单位视线、步号和时间戳。同一步重复调用只替换该步观测；时间倒退会清空旧窗口。完整 policy 使用与 `ground_point` 同一模型的内参、畸变/安装参数、杆臂和机体姿态。模拟相机和真实 `GroundProjector` 分别提供配套射线；自定义地面投影器必须提供对应的 `ray`，不能混用模拟相机。独立调用 Loop 且没有自定义地面投影时，使用与其 `CameraModel.ground_point` 相同的水平机体近似。

纯函数最小二乘交会的质量门与原离线评估一致：

- 至少两条不同动作步的视线。
- 最新相机位置相对历史位置的最大基线至少 0.5 米。
- 最新视线相对历史视线的最大夹角至少 0.5 度。
- 各视线到交点的最大垂距不超过 5 米，交点在全部相机前方。
- 奇异矩阵、无穷值、NaN、错误单位向量等异常均返回 `None`。

基线与夹角选择“最新对历史”与旧评估一致，不会仅凭窗口中更早的一对好视线放过当前视线。三角化 sigma 是残差、当前距离和视差角的保守传播结果，再乘配置中的放大系数。地面高度 sigma 沿框底边单位视线传播为 `ground_height_sigma_m / ray_down`，接近地平线时会明显增大。地面三维交点由原投影器返回的 N/E 和同一底边视线反推 down，因此可用平地、高程图或已有 AGL 平面。垂直底边射线不能仅凭 N/E 反推高度，此时不构造三维地面来源；无有效三角化则仍回退原有 N/E 地面点。相机回调异常也会回退，不打断任务状态机。

融合采用 `1 / sigma²` 权重。任意两个来源的三维位置相差超过 `2.5 * hypot(sigma_a, sigma_b)` 时，停止加权，选择时间较新的来源；同一时间选择 sigma 更大的来源，并扩大 sigma 至覆盖来源分歧，标记 `position_consistent=false`。框中心与底边本来可能位于不同高度，这种差异也算入一致性检查。三角化失败时使用当前地面结果；当前射线也无效时不沿用此前的融合结果，回退原地面路径。

开启后确认/计数记录新增：`position_source`（ground / triangulation / fused）、`position_sigma_m`、`position_view_count`、`position_baseline_m`、`position_ray_angle_deg`、`position_consistent`、`position_consistency_note`。没有可传播的有效射线时 sigma 为 `null`，表示未知。原有字段仍保留；任务 finding、摘要和 policy 下传标签只在存在这些字段时传递。

未来深度来源可提供 `RangeMeasurement(distance_m, sigma_m, timestamp_s, source="depth")`，经 `position_from_range` 转成位置后加入同一 `fuse_position_estimates` 接口。当前没有深度模型和深度测量调用。

## 误差覆盖与可复现验证

`scripts/validate_step_triangulation_replay.py` 只读主仓库录像和已有 segmentation 缓存，解析标签并做 CPU 几何，不调用检测、VLM 或深度模型。结果保存在 `docs/evidence/onboard-step-triangulation-replay.json`。

原报告的几何部分实际是排序前 30 集的全部 1,274 个已关联目标观测；“分层抽样”属于深度部分。762 次交会有效（59.81%），水平 P95 约 4.45 米。相同射线输入下，新模块和旧评估的位置、质量指标、放大后的 sigma 最大差均为 0，质量门无差异。

| sigma 系数 | 95% 斜距误差覆盖率 | 水平径向误差 ≤ 1.96 sigma 的比例 |
|---:|---:|---:|
| 1.0 | 84.91% | 86.09% |
| 1.5 | 93.04% | 93.83% |
| 2.0 | 96.33%（734/762） | 96.19%（733/762） |
| **2.5（默认）** | **98.16%（748/762）** | **98.43%（750/762）** |

达到 93% 斜距覆盖率的旧输入同批数据最小系数约 1.48472。但更贴近实际机载输入的诊断中，系数 2.0 只有 92.34% 斜距覆盖率，因此默认选 **2.5**，三个回放口径均超过 93%：

| 回放口径 | 有效次数 / 1,274 | 水平 P95 | ×2.5 后 95% 斜距覆盖率 | 水平径向覆盖率 |
|---|---:|---:|---:|---:|
| 原评估相同输入 | 762（59.81%） | 4.4522 m | 748/762，98.1627% | 750/762，98.4252% |
| 旧相机几何、最近 8 动作步、同一步去重 | 575（45.13%） | 6.1767 m | 553/575，96.1739% | 555/575，96.5217% |
| 共享相机模型实际中心、录像 track ID、最近 8 动作步 | 496（38.93%） | 7.0725 m | 472/496，95.1613% | 476/496，95.9677% |

这是同批数据校准与复核，不是独立测试集或真机保证；上述覆盖率属于三角化本身，不代表融合后位置已获得同等概率保证，也不是完整三维协方差或二维置信椭圆。诊断只重放离线关联成功的观测，没有重新运行闭环关联、地面重识别、融合或控制动作。

旧评估按真值目标合并历史，存在同一步多条 track 关联到同一目标的情况。逐点一致性对照保留这些旧输入并使用唯一观测序号；证据另列真实动作步去重的回放，不能把旧评估的真值关联当作机载关联。旧评估还以机体 NED 原点作为射线起点；机载使用与 ground projector 一致的相机中心杆臂修正，因此同输入纯函数一致性也不等于完整机载流程的误差保证。

复现命令（Windows / PowerShell）：

```powershell
New-Item -ItemType Directory -Force .tmp | Out-Null
$env:TEMP=$env:TMP=$env:TMPDIR="D:\Projects\mdwt\steptri\.tmp"
$env:CUDA_VISIBLE_DEVICES="-1"
$env:PYTHONPATH="src"
python scripts/validate_step_triangulation_replay.py D:/Projects/Multi-Detect/artifacts/datasets/onboard-loop --max-episodes 30 --sigma-scale 2.5 --output docs/evidence/onboard-step-triangulation-replay.json
python -m pytest tests/test_step_triangulation.py tests/test_onboard_loop_step_triangulation.py tests/test_onboard_policy_step_triangulation.py -q -p no:warnings
python scripts/run_onboard_gate.py -q -p no:warnings
```

目标移动、框中心因遮挡变化、错误关联、位姿漂移、时间不同步、杆臂或相机标定错误，均可能造成错误交会。质量门不能识别所有系统性误差。当前选项应保持默认关闭，需经后续闭环 SIM 和真机验证后再决定任务中是否开启。

## B2：近距单目深度（默认关闭）

B2 在原有的步间三角化和地面相交之上，增加一个可选的近距单目深度来源。`near_depth` 默认是 `false`；关闭时不创建深度估计器、不加载 ONNX Runtime 或 TensorRT，也不增加任务日志字段，原有输出和控制路径保持不变。

深度门控在 `OnboardPolicy` 的慢身份判断完成后、`next_action` 之前同步执行，因此同一帧不会和 VLM 请求并发。普通 loop 只在 `approaching` 或已到最近视角时放行；MissionExecutor 的 tracking loop 只在 `approaching`/最近视角放行，`inspect` 只在 `face` 和 `capture` 阶段放行。门控还要求当前焦点轨迹、当前估计斜距小于 `near_depth_max_range_m`（默认 10 米）、距离上次推理至少 `near_depth_min_interval_s`（默认 0.2 秒），并用 step token 保证每步最多一次。缺少图像、相机标定或中心视线时会跳过。

配置项如下：

```json
{
  "near_depth": false,
  "near_depth_model_path": "D:/Projects/Multi-Detect/artifacts/evaluation/depth-anything-v2-metric-outdoor-small.onnx",
  "near_depth_backend": "onnx-cpu",
  "near_depth_max_range_m": 10.0,
  "near_depth_min_interval_s": 0.2,
  "near_depth_max_age_s": 1.0,
  "near_depth_sigma_scale": 1.0
}
```

开发和 SIM 使用 `onnx-cpu`，明确选择 `CPUExecutionProvider`，线程数固定为 intra-op/inter-op 各 2。Orin 使用 `tensorrt` 时必须给出已经预置的 `.engine` 或 `.plan`；路径不存在、后缀不匹配、ONNX digest 不在白名单或后端不可用都会在启动时抛错，绝不静默降级到 CPU。估计器只在 `near_depth=true` 的构造分支中惰性加载，任务结束时释放会话。

每次有效深度结果先检查正数、有限值和新鲜度（当前观测时钟减结果的 `captured_at_s` 不超过 `near_depth_max_age_s`，默认 1 秒）。有效结果包装成已有的 `RangeMeasurement(distance_m, sigma_m, timestamp_s, source="near_depth")`，沿目标中心视线调用 `position_from_range`，然后和三角化/地面估计一起走 `fuse_position_estimates`。深度 sigma 取模型 sigma 与离线经验桶的较大值，再乘 `near_depth_sigma_scale`：小于 10 米为 1.60 米，10–20 米为 10.57 米，20–40 米为 26.29 米，40 米以上为 50.71 米。融合仍使用逆方差加权和现有 2.5-sigma 一致性门；不一致时沿用现有的最新来源回退策略并放大 sigma。深度只改变位置估计，不改变 track association、地面锚点、身份票数、VLM verdict 或目标确认。

任务日志在开启时增加 `onboard.near_depth`，包含 `inference_count`、`failure_count`、各类跳过计数、最近一次推理耗时以及推理前后的 RSS。常见跳过原因包括 `stage`、`not_focus`、`range`、`interval`、`step`、`calibration`、`image`、`expired` 和 `invalid`。

### CPU 小规模 SIM 回放

使用只读录像 `eval-v2-test-r3seg-descent/gen_v2_w0035/episode-0000-3be7faf9` 的 48 帧，近距段优先覆盖（目标斜距约 6.9–9.0 米），运行固定 outdoor-small ONNX、CPU、2 线程。回放脚本是 `scripts/validate_near_depth_replay.py`，结果写入 `.tmp/near_depth_replay.json`。目标框采用录像真值投影，几何基线使用最近 8 个视线，深度只在几何当前斜距小于 10 米时尝试。

本次样本中有 19 帧得到几何估计，2 帧通过近距门并实际完成深度推理，2 帧深度结果进入融合。几何位置误差均值为 1.02 米，几何加深度融合为 2.74 米，变化为 **+1.71 米**；对应斜距误差均值为 0.85 米和 1.54 米。这个结果说明在该小样本上深度没有改善几何位置，不能据此关闭一致性门或把模型当作普适测距器。深度结果只有 2 个，且目标框来自真值投影，回放不是端到端机载评估。离线模型评估本身在小于 10 米桶只有 3 个样本，10–20 米 P95 约 10.57 米，更远距离迅速恶化，因此默认保持关闭，开启前应使用任务数据重新标定 sigma 和门控。

复现命令（PowerShell）：

```powershell
$tmp = "D:\Projects\mdwt\neardepth\.tmp"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$env:TEMP=$env:TMP=$env:TMPDIR=$tmp
$env:CUDA_VISIBLE_DEVICES = "-1"
$env:PYTHONPATH = "src"
python scripts/validate_near_depth_replay.py --max-frames 48
```

## 闭环失效复盘与修复（2026-10-17）

对 `eval-v3-test-r12id-r14plan-tri` 的 12 个 episode 逐行读取 `steps.jsonl`，并用各世界 `*.truth.json` 的真值复算确认误差。三角化开启时，w0035 的两架次误差为 3.245 m 和 3.801 m，而同一确认记录的地面相交误差为 2.062 m 和 1.228 m；w0037 的正确目标架次为 3.655 m 对 1.290 m，另一架次还出现 13.562 m 对 1.488 m。w0036 的两架次为 0.312/0.576 m，地面相交为 0.355/0.496 m；w0038 为 0.174/0.686 m，地面相交为 0.132/0.369 m。

日志把根因暴露得很清楚：坏样本的视线夹角只有 6.45--15.02 度，虽然记录的基线有 15.1--60.97 m，几何条件仍然接近沿视线方向病态；三角化或融合位置与当前地面锚点相差 1.18--12.15 m。w0037 的错误架次明确记录 `position_consistent=false` 和 `source_disagreement:newest_then_larger_sigma`，旧逻辑却按同时间戳下较大的 sigma 选择了三角化。w0035/w0038 的 `position_consistent=true` 也不能证明正确：框中心视线对应物体的高处，地面锚点对应底部，二者在真实闭环里存在系统偏差。现有日志没有证据表明跟踪 ID 在这些确认帧发生了串轨，也没有发现图像与位姿时间戳错位；这些假设不能解释“地面锚点和三角化同时分离”的共同模式。

修复包括三点：

1. 闭环有可靠地面模型时，最小视线夹角同时受 10 度绝对下限和 `0.3 度/米 * 目标距离` 约束，避免固定 0.5 度门限放过远距离近共线视线。
2. 三角化结果必须与当前地面锚点的水平距离不超过 0.1 m；框中心与底部的高度差只用于三维残差，不允许把跟踪/导航锚点横向带走。
3. 来源冲突时始终回退地面相交，并记录 `ground_anchor_disagreement:fallback_ground` 或 `parallax_below_distance_gate:fallback_ground`。只有 `ground_height_sigma_m > 10` 的调用者才保留原来的几何优先行为，供明确知道地面模型不可靠的离线/实验调用使用。（该例外已于 2026-10-18 取消，见下节。）

按上述门控对闭环确认记录离线重算，真实目标成功架次的修复误差均不大于地面相交误差：w0035 两架次为 2.062/1.228 m，w0036 为 0.312/0.496 m，w0037 正确目标架次为 1.290 m，w0038 两架次为 0.132/0.369 m。w0039 的两次确认属于错误目标，地面和三角化都偏离真值约 70 m，不能用来证明定位算法质量；修复对两次都回退地面。

离线回放仍通过同输入 parity：1274 个样本、762 个有效几何样本，P95 斜距覆盖率 98.16%，水平覆盖率 98.43%。这批闭环数据规模很小，且成功目标类型、距离和视角有限，因此不建议把三角化作为任务默认开启项；当前结论是保持 `step_triangulation=false`，仅在有同步位姿、可靠相机标定和足够视差的任务上显式开启。

## 按地面不确定度放宽锚点门（2026-10-18）

上一节的门控有两个缺口：`ground_height_sigma_m <= 10` 时三角化只能在地面交点 0.1 m 内生效，等于在有地面模型时永远不能修正错误的地面高度；`> 10` 时视差门和锚点门同时关闭，又回到 6–15 度近共线视线造成 3–13 m 误差的旧模式。而纠正未知地面高度（坡地、无高程图）恰恰是三角化的用途。

现在的规则：

1. 距离相关视差门（`max(step_triangulation_min_ray_angle_deg, step_triangulation_parallax_deg_per_m × 距离)`）对每个三角化结果始终生效，与地面 sigma 无关；没有三维地面来源时，被拒绝的三角化同样丢弃，报告回退 `track.ground`。
2. 三角化相对地面交点允许的水平修正量为 `max(step_triangulation_ground_gate_m, step_triangulation_ground_gate_sigma × ground_height_sigma_m / tan(框底边视线俯角))`。后一项正是地面高度偏 1 sigma 时交点沿视线的水平位移：地面越不确定、视线越平，允许三角化移动得越多。超出时仍回退地面并记 `ground_anchor_disagreement:fallback_ground`；通过后仍走原来的 2.5 sigma 三维一致性检查与 `1/sigma²` 融合。
3. 新增 `step_triangulation_ground_gate_sigma`，默认 2.5（与融合一致性门同为 2.5 sigma），须有限且 `>= 0`；设为 0 即严格等价于原来的固定 0.1 m 门。`step_triangulation_ground_gate_m` 保持原名、原默认值，含义变为下限。没有高程图或地面高度不可信时，应把 `ground_height_sigma_m` 设成真实的不确定度（例如 5 m，与终验坡地专项一致），而不是靠大于 10 的“模式开关”。

回放验证：`validate_step_triangulation_replay.py --fusion-gate-replay` 把录像中的 track ID、框、位姿与时间戳原样送入 `OnboardLoop._update_position`，只改变地面模型（`FlatGround(ground_up_m)`，即机上以为地面比真实高）和配置，不重跑关联与动作；以分割框 IoU ≥ 0.1 选出真实目标帧，误差为水平 N/E 距离。`step_triangulation_ground_gate_sigma=0` 的回放与改动前代码逐帧一致，并与 `tri-fallback` 录像中记录的确认位置差 0.000 m。三组闭环录像（`eval-v3-test-r12id-r14plan-{tri-fallback,tri,notri}`，39 集，325 个真实目标帧，27 次真实目标确认）：

| 地面 | 配置 | 帧误差 中位 / P95 (m) | 确认误差 中位 / P95 (m) | 帧来源 fused / ground |
|---|---|---:|---:|---:|
| 正确 | 原门控（σ=1） | 1.22 / 4.77 | 0.50 / 1.60 | 4 / 321 |
| 正确 | 新门控 σ=1 | 1.16 / 4.60 | 0.56 / 1.60 | 85 / 240 |
| 正确 | 新门控 σ=5 | 1.12 / 4.60 | 0.56 / 1.60 | 85 / 240 |
| 高 2 m | 原门控（σ=1） | 4.37 / 9.89 | 3.44 / 6.98 | 0 / 325 |
| 高 2 m | 新门控 σ=1 | 4.07 / 9.89 | 3.44 / 6.98 | 65 / 260 |
| 高 2 m | 新门控 σ=5 | 4.00 / 9.89 | 3.44 / 6.98 | 85 / 240 |
| 高 5 m | 原门控（σ=1） | 12.47 / 23.13 | 8.07 / 15.62 | 0 / 325 |
| 高 5 m | 新门控 σ=1 | 12.47 / 23.13 | 8.07 / 15.62 | 0 / 325 |
| 高 5 m | 新门控 σ=5 | 12.02 / 23.13 | 7.98 / 15.62 | 85 / 240 |

旧的 σ=20 几何模式在正确地面下为 1.38 / 22.61 m（确认 1.00 / 10.13 m），不能作为无地面先验时的安全选项。

通过视差门的 85 帧上，三角化本身不依赖地面：地面高 5 m 时新门控 σ=5 的误差为中位 0.75 m、P95 3.48 m，同帧地面交点为 6.62 / 13.78 m；11 次融合确认为中位 0.32 m、最大 3.89 m，地面交点为 4.64 / 9.93 m。正确地面下，这 85 帧为 0.66 / 2.72 m（σ=1）对地面 0.64 / 2.76 m；11 次融合确认中分别有 2 次（σ=1）和 4 次（σ=5）比地面差 0.05 m 以上，最大的一次是 `tri` 录像 w0035 的 3.25 m（σ=1）/ 3.75 m（σ=5）对地面 2.06 m。总体中位数和 P95 改善有限，原因是只有约 26% 的真实目标帧有足够视差，其余帧仍只能回退（可能有偏的）地面交点；放宽视差门（例如固定 5 度）能让更多帧被修正，但正确地面下确认 P95 会升到 8.65 m，因此不放宽。旧报告排序前 30 集（176 帧）结论相同：正确地面下帧 P95 从 1.42 m 变为 1.80 m（由单帧 1.11 → 1.96 m 引起，最大值 4.02 m 不变），地面高 5 m 且 σ=5 时确认中位从 4.75 m 降到 2.62 m。样本仍很小，三角化继续默认关闭。

```powershell
$env:CUDA_VISIBLE_DEVICES="-1"; $env:PYTHONPATH="src;."; $env:PYTHONUTF8="1"
$d="D:/Projects/Multi-Detect/artifacts/datasets/onboard-loop"
python scripts/validate_step_triangulation_replay.py --fusion-gate-replay "$d/eval-v3-test-r12id-r14plan-tri-fallback" "$d/eval-v3-test-r12id-r14plan-tri" "$d/eval-v3-test-r12id-r14plan-notri" --loop-configs '{\"old_gate\": {\"step_triangulation_ground_gate_sigma\": 0}, \"new_s1\": {}, \"new_s5\": {\"ground_height_sigma_m\": 5}}' --output tmp/tri-gate-replay.json
```

## 平地闭环验收与结论修正（2026-10-18）

6 个测试世界 × 2 架次，真实 PX4/Gazebo 闭环，身份 r12、规划 r14、候选框 YOLOE v3：

| 组 | 成功率 | 误确认率 | 位置误差中位（全部 / 成功架次） |
|---|---:|---:|---:|
| 三角化，10/17 修复前（`eval-v3-test-r12id-r14plan-tri`） | 7/12 | 3/12 | 3.66 / 0.69 m |
| 三角化，10/17 修复后（`...-tri-fallback`） | 8/12 | 2/12 | 0.74 / 0.56 m |
| 不开三角化（`...-notri`） | 9/12 | 2/12 | 0.65 / 0.40 m |

修复后 3–13 m 的大误差全部消失。成功率差的那一架次在停车牌世界 w0034，是搜索没看到目标，与测距无关；w0039 的两次 ~69 m“误确认”其实是任务文字有歧义：任务是“找到行人”，场地里另一个人（干扰物 casual_female）同样是行人，飞机确认的就是她，判分器早已把它标为 task_equivalent（按任务语义算成功）。

但要更正一点：修复后的“三角化组”和不开三角化几乎一样，不是因为三角化有效，而是 10/17 的门控让它在有地面模型时只能偏离地面交点 0.1 m——等于关掉了。上一节的新门控才让它在地面不确定时真正起作用。平地上地面交点本来就准，三角化在这里不会有明显收益；它的价值要看“地面高度不对”的场景，闭环对比（地面被当成高 2 m，`configs/onboard/sim-loop-v3-r12id-r14plan-*-ground2.json`）已排队。在那之前 `step_triangulation` 仍默认关闭。

## 地面偏 2 m 闭环对比与默认开启（2026-10-18）

同 4 个测试世界（w0035–w0038）× 2 架次，机上地面模型故意设成比真实地面高 2 m（`ground_model: {type: flat, ground_up_m: 2}`，`ground_height_sigma_m: 5`），新门控（`4ce3715`）：

| 组 | 成功 | 误确认 | 位置误差中位 / 平均 |
|---|---:|---:|---:|
| 三角化（`eval-v3-test-triangulation-ground2`） | 8/8 | 0 | **2.05 / 2.77 m** |
| 不开三角化（`eval-v3-test-no-triangulation-ground2`） | 8/8 | 0 | 3.83 / 4.08 m |

逐世界：w0035 2.0 vs 4.2 m，w0036 0.3–0.9 vs 2.3 m，w0038 0.35/3.45 vs 3.49/2.81 m；w0037（车辆世界）两组都约 6.6 m，三角化没帮上（平地时两组也都约 0.9 m）。平地上两组相当（同 4 个世界成功架次平均 0.55 vs 0.64 m）。

按终验判定规则（成功率不降、误确认不升、定位误差改善），机上策略默认开启步间三角化，地面高度 sigma 默认 5 m。

平地复验（默认配置 `sim-loop-v3-r12id-r14plan-defaults.json`，6 个测试世界 × 2 架次，`eval-v3-test-defaults-flat`）：成功 8/12，与旧门控相同（不开三角化 9/12，差的一架是 w0034 搜到与否）；误确认 3 次：w0039 两次是上面说的“场地里另一个行人”（任务语义下算成功，三组都有），w0034 一次是把红色加油站招牌认成红色停车牌（10/14 就报过的易混物，前两组航线没靠近它），确认位置就在招牌处——都与测距无关。w0035–w0038 成功架次平均误差 0.69 m（不开三角化 0.64 m、旧门控 0.55 m）：平地上新默认平均多约 0.05 m，最差 w0036 多 0.3 m；换来地面高度不对时平均少 1.3 m。保留默认开启。
