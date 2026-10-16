# 步间三角化与地面不确定度融合

这是方案 B2 的几何部分。默认关闭，不加载模型，不使用 GPU，也不包含单目深度推理。

飞机从不同位置看同一静止目标时，可以把几条视线交会成一个位置。地面高度越不确定，地面相交位置的权重越低；多视角条件不足时，直接退回原来的框底边地面相交位置。

## 如何打开

在传给 `scripts/onboard_policy.py` 的现有 onboard JSON 配置中加入：

```json
{
  "step_triangulation": true,
  "step_triangulation_max_views": 8,
  "ground_height_sigma_m": 1.0,
  "step_triangulation_sigma_scale": 2.5
}
```

这四项也是默认值，只有 `step_triangulation` 默认是 `false`。开关必须是 JSON 布尔值；视线数必须是 2～64 的整数；地面高度 sigma 必须是有限正数；三角化 sigma 系数必须有限且至少为 1。字符串 `"false"`、`true` 充当数字、非整数窗口和 NaN 都会被拒绝。

适合先在静止目标、相机标定与位姿时间对齐可靠的 SIM 回放或受控测试中开启，特别是接近、侧移、绕飞时。只有转头、原地悬停，不能产生测距基线。没有高程图、地形未知或地面高度误差明显大于一米时，使用者应调大 `ground_height_sigma_m`，不能把默认一米当作地形已测准。

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
