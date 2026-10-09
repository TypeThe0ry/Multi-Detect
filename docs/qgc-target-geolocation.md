# 目标 GPS 落图操作说明

## 目的和边界

Multi-Detect 以机载 GPS 位置、经过融合的目标距离和相对方位/方向估计目标的 WGS84 位置。Jetson 在已签名的 operator metadata 中发送目标 ID、源视频帧、坐标和水平 `1σ` 不确定度；MultiGCS 只把与当前选中目标匹配的结果投影到主地图。

此功能是**只读定位证据**：它不创建航点、Survey、Guided action、任务项，也不会向 PX4 发送位置或姿态指令。黄色圆圈是报告的不确定度在当前地图缩放级别的显示，不是禁飞区、航迹半径或自动控制边界。

### SIM/REPLAY 证据卡片契约

Python 侧的 `multidetect.target_evidence_card.TargetEvidenceCard` 是同一条
目标事件的可审计摘要：它绑定 `TargetEvent`、现有 WGS-84 `TargetGeolocation`、GPS
新鲜度、测距有效性和可选的 SIM runtime identity。`available=true` 时才包含纬度、经度
和水平 `1σ`；GPS 不合格、GPS 过期、测距不可用或定位结果缺失时，卡片只写入
`available=false` 及明确原因，三个坐标字段保持 `null`。卡片的 `claims` 固定声明
`read_only_evidence=true`、`contains_flight_control=false`、`contains_truth=false`，
因此不能被任务编译器、Permit 或执行器当成输入。`from_mapping()` 还会拒绝非规范 JSON、
极点坐标、坐标泄漏和任何不安全 claim；这是机器契约，不等同于 QGC 人工视觉签字。

在 integrated SIM/QGC ranging acceptance 中，卡片列表位于
`target_geolocation_acceptance.target_evidence_cards`。combined-SIM verifier 会重新解析每张
卡片，并检查 `target_evidence_card_count`、错误列表、`available_geolocation_count` 与卡片数
一致；卡片的 `run_identity_sha256` 还必须等于本次 integrated SIM 的 `run_identity_hash`。
这使目标点和不确定性圆与同一批传感器/回放证据绑定，不能通过复制旧坐标或伪造汇总字段来通过
combined gate。未配置目标定位的历史 artifact 不包含这组字段，仍按历史兼容规则读取。

## SIM 高度基准

Gazebo 的世界 ENU `z/up` 不是地形相对高度；在坡地、台地或带 MSL 原点的 world 中，把它当 AGL 会让
测距几何产生系统误差。SIM backend 因此只把 3-D Odometry 的 world position 转成 local NED，
`local_down_m=-position_up_m`，而 `altitude_agl_m` 必须由同帧附近的 qualified NavSat MSL 高度减去
scenario DEM 的 WGS-84/MSL 高程得到。有效 reference 只有
`GAZEBO_NAVSAT_MSL_MINUS_SCENARIO_DEM_MSL`，证据同时写入 DEM SHA-256 和
`odometry_local_up_used_as_agl=false`。

缺少 DEM、DEM 与 scenario 不一致、GPS 未过 canonical 质量门、位置不在 DEM 覆盖内或 datum/高度非法时，
SIM AGL 会保持 unavailable；不会退回 local-up、home 高度或常量平面。SIM 的 fix/satellite/accuracy 是
明确的模拟参数，不代表室外 GNSS 通过。v54 非平地 external sensor-runtime 已证明这一数据通路可运行，
但该记录只在 home 原点稳定采样，没有飞越坡面、执行目标检测/测距闭环或进行人工 Fly View 验收。

v55 另有一份独立的 control-response 记录：truth-free synthetic target 经过共享 perception → AI →
`MANUAL_CONTROL` 路径后，PX4 `GLOBAL_POSITION_INT` 报告最大 `0.818282 m` 水平位移；没有使用 Gazebo
truth 或 pose injection。该记录不经过 camera detector、ranging、`TARGET_GEOLOCATION`、地图落点或
1σ 圆，也未启动/人工检查 QGC，因此不能作为本页目标落图功能的视觉或定位精度证据。上述只读定位
边界不变：目标落图本身仍不产生任何飞控指令。

当前三机型 3-D control 证据已按加入 OdometryPublisher 后的 SDF 字节重新生成并做 SHA-256 binding；
binding 为 2,359 bytes / `78066C298A9B9EEDB012B7596D9B7DA66E334DE7449EACDF1CCD59513181B00C`。
它证明 fixed-wing / multirotor / VTOL 的当前资产仍复用同一 manual/AI/authority/watchdog 契约，不证明
地图上的目标坐标或 1σ 圆已被人工观看，也不证明室外 GPS/距离精度。E7 strict 仍有 32 个真实 blocker，
最终 `v0.3.0` 保持未发布。

## 生效门限

Jetson 只在以下条件同时成立时发布可用坐标：

- 自适应导航状态为 `gps-aided`；默认要求 `position_healthy=true`、GPS `fix_type >= 3`、至少 6 颗
  已知卫星（`255` 为 unknown）、水平精度 `<= 6 m`、垂直精度 `<= 10 m`，并且原始坐标和观测
  时间通过实时新鲜度门。实现复用 `mavlink_gps.py` 的 wire-domain 归一化和
  `adaptive_ranging.assess_gps_quality()`，不要在调用方复制阈值。
- `navigation_fusion.py` 的 GPS 速度源也复用同一组 `MAVLINK_QUALIFIED_*` 最低质量阈值；配置只允许
  收紧（例如现场要求 RTK），不能把 fix 或卫星数放宽到 canonical floor 以下。速度融合默认使用
  `GPS age <= 0.75 s` 的更严格窗口，这是速度一致性门，不会放宽上面的目标落图 `1.5 s` 坐标门。
- 飞机经纬度有效且新鲜；融合结果具有有效地面距离、NED 北/东偏移和对应的目标 ID。
- 目标坐标及其水平 `1σ` 均为有限值，且目标 ID 与 MultiGCS 当前目标一致。
- MultiGCS 的地图、复制和本地快照共用同一坐标域：纬度必须严格位于 `(-90, 90)`，经度允许
  `[-180, 180]`。精确南/北极点没有稳定的局部经度方向，因此会在落图、复制和保存前被拒绝；
  这条边界也适用于从本地 JSONL 重新加载的记录。

实时测距也执行独立新鲜度门：`RANGE_STATUS` 的 source frame age 和 data freshness 对 `VALID/DEGRADED` 都必须 `<=1.5 s`；过期包在 Python/C++ 解码层拒绝，QGC 状态层清除当前距离/方位，证据快照不持久化 stale range。

测距不确定度还必须覆盖视线几何，而不是只报告深度传感器的斜距误差。`MultiModalRangingEngine`
对相机内参、机体 roll/pitch、安装 pitch/yaw/roll 和目标像素中心分别做一阶 one-sigma 扰动，把这些端点
传播到 `ground_range_ci95_m`；因此直接激光/VIO 路径也会随着目标偏轴、姿态抖动或 FOV 边缘而扩大
地面距离区间。任一 one-sigma 端点穿过地平线（地面交点变为无界），或校准/像素不确定度无法生成
合法端点，结果会 fail-closed 为 `INVALID`，不会跳过该扰动并落一个过窄的假精度。这个规则与
`target_ray_does_not_intersect_ground_safely` 的 nominal 视线门互补：前者检查名义方向，后者检查
不确定性包络。

固定翼在滚转或俯仰时，姿态扰动会改变 NED 射线的水平方位；`RANGE_STATUS` 现在用姿态
roll/pitch 与相机安装 pitch/yaw/roll 的一阶端点中心差分，把这些耦合同时计入地面交点和绝对
`bearing_sigma_deg`。安装测量的 `boresight_sigma_deg` 作为三轴各自独立的共同 `1σ`，不再只
扰动安装俯仰后又用固定方位标量近似；任何三轴 `1σ` 端点越过地平线都会 fail-closed。目标
坐标的水平误差再将角度 `σθ` 按 WGS-84 地面弧长 `r·σθ(rad)` 转换，而不是用会在
宽方位门下低估结果的弦长近似。这样 SIM、REPLAY 和 REAL 共享同一不确定度语义；它只影响
证据卡片和 `1σ` 圆，不产生飞控指令。

配置 `--ranging-dem` 后，测距引擎使用合格 raw GPS 样本的 WGS-84 位置和绝对 MSL 高度，在严格局部
DEM 的双线性地形面上求射线交点。GPS 水平/垂直误差、DEM 高程与坡度误差都会进入同一个距离区间；
该路径不会再把同一 GNSS 派生的相对 AGL 当作独立传感器重复融合。GPS/DEM 时间偏斜、位置不在 DEM
覆盖区、射线不向下、无有限交点或不确定性端点越界时，地形候选 fail-closed，不能回退到假设平坦地面后
落点。直接深度等真正独立来源仍可按原有质量门生成明确标注的 degraded 结果。三个正式机型 DEM 仍与
各自平坦 world 对齐；另有 `terrain_ranging_validation_001` 非平坦 SDF/DEM 对，已用独立解析的 SDF
平面真值比较 41 个表面点和 7 条射线（6 条跨单元、1 条垂直基准）。该确定性资产真值不等于外部 Gazebo 已运行，也不代表人工 QGC
视觉、室外 survey、真实 GNSS/Pixhawk、HIL/REAL 已验收。

目标地理位置沿用同一条源帧时间语义：`source_captured_at_s` 到产出时间的年龄必须 `<=1.5 s`。
生产者遇到合格但过期的源帧时原子发布 `available=false`，Python 消息模型、wire 编码器/解码器和 QGC C++ 解码器对 stale 坐标保持一致的 fail-closed 行为；带新签名的旧坐标不会重新落图、复制或进入快照。

当现场同时提供 `GPS_RAW_INT` 的观测时间、目标帧时间、N/E 速度以及速度自身的观测时间时，WGS-84
投影会先把飞机参考点沿速度方向对齐到目标帧时刻，再叠加目标的 N/E 偏移。这样可以消除固定翼高速飞行中
“GPS 样本比图像早/晚几十到几百毫秒”造成的系统性落点偏移；反向时间差也按同一条椭球直解处理。GPS 与
速度样本距离都必须在 `1.5 s` 内，超过窗口、未来帧或陈旧速度都会 fail-closed。补偿后的水平 `1σ` 还会
加入速度模型误差（默认 `0.5 m/s` 乘以对齐时间）；如果时间补偿路径收到部分或非有限 N/E 速度样本，
也会 fail-closed，不会把旧 GPS 点静默当作零运动；完全缺少速度/时间字段的旧回放仍不猜测位移，保持兼容的未补偿路径。

当飞控/融合器能够给出当前机型的水平加速度误差包络时，回放或实时 telemetry 还可以提供
`acceleration_uncertainty_mps2`（一西格玛、非负、有限）。投影均值仍采用恒速模型，但会把
`0.5·|dt|²·acceleration_uncertainty_mps2` 与速度项按独立误差平方和合并；这能覆盖固定翼转弯、
垂起转换或多旋翼急停时恒速外推漏掉的二阶位移误差。缺省为 `null`/`NaN` 时保持旧结果，负值、
非有限值和布尔/字符串等伪造输入会 fail-closed；若给了加速度包络却没有完整的 N/E 速度，
也会 fail-closed，避免把这项预算静默丢掉。该字段只扩大证据卡的 `horizontal_sigma_m`，不改变
wire 坐标、不产生飞控指令；建议由各机型的实测 IMU/EKF 误差或保守动力学包络标定后再写入现场
replay，而不是凭单帧视觉结果猜测。

相机光心与 GNSS/导航参考点不重合时，标定 JSON 还可以声明 body-NED 杆臂
`camera_offset_forward_m/right_m/down_m`。测距引擎会先把该平移按当前 roll/pitch/heading
旋转到 NED，再用光心高度求地面交点，最后把相对飞机参考点的完整 N/E 偏移用于 WGS-84 落图；
因此固定的安装前后/左右偏差不会被误写成目标距离。`camera_offset_sigma_m` 是杆臂测量的各向同性
`1σ`，会同时并入光心高度、地面距离和方位不确定度；未提供这些字段时全为 `0.0`，旧标定结果
保持兼容。字段和 100 m 域限制见 [`configs/calibration/README.md`](../configs/calibration/README.md)。
杆臂修正只改变几何估计与证据卡，不产生飞控指令，也不能替代现场 RTK/测量仪真值验收。

多源绝对距离即使通过了硬一致性门，也不会自动获得过窄的置信区间。`_fuse_consistent()`
会对被接受源计算加权残差 `χ²`；当 `χ²/(N-1)>1` 时按 Birge ratio 放大融合 `σ`，并在
`RangeSolution.reasons` 增加 `absolute_range_uncertainty_inflated`（无垂直参考的 direct
路径使用 `direct_range_uncertainty_inflated`），同时保持 `DEGRADED`。因此相机地面交点、
激光、带绝对尺度的 VIO 在“数值上仍相容但模型误差偏大”的情况下会扩大 `slant_range_ci95_m`、
`ground_range_ci95_m` 及后续目标水平误差预算；单一来源或残差符合声明协方差时不改变历史结果。
这只是证据/态势感知的保守修正，不会产生飞控或载荷动作。

`v0.3.0-pre.8` 的 v31 回归还验证了精确 `±90°` 纬度：由于极点没有稳定的局部经度方向，
Python `TargetGeolocation`、QGC C++ wire 解码器和所有地图/复制/快照/JSONL 重载入口统一拒绝该值，
而不是允许记录落盘后再在地图投影阶段失败。对应证据为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-target-geo-pole-v31.json`，
SHA-256 `CC6371291F416E62E1308C78CF70CC2E08B10854D7B5A5817194E5B5164D18EE`。

MAVLink 原始输入还执行逐帧归一化：`GPS_RAW_INT.satellites_visible=255` 是“未知”哨兵，
不会被当成 255 颗可用卫星；越界/缺失经纬度不会刷新 raw GPS 位置时间戳，也不会把上一帧坐标
和本帧 fix/精度拼接成新鲜样本；缺失 `GPS_RAW_INT.alt` 或合法但降级的 `fix_type` 还会清除旧 raw
位置新鲜度及 GPS-home 高度回退；未来时间戳统一视为未知并拒绝通过新鲜度门。`GLOBAL_POSITION_INT`
作为独立估计器来源仍可提供位置，但 raw 质量门不因它而伪造通过。

现场接线后的第一轮只读采集使用 [`docs/field-bringup.md`](field-bringup.md) 中的
`scripts/capture_gnss_samples.py`：它保存 `GPS_RAW_INT`/`GPS2_RAW` 的原始坐标、fix、
卫星数、hAcc/vAcc 和主机接收时间，再由 `scripts/verify_gnss_capture.py` 离线重算质量门。
该报告用于把现场 GNSS 样本交给后续目标事件回放和真值调查，不会替代 `RANGE_STATUS` 同帧
证据，也不会因为收到融合位置就伪造 raw GPS 质量或关闭 REAL/HIL 门。
采集器允许为诊断选择更严格的阈值；任何放宽 canonical `hAcc≤6 m`、`vAcc≤10 m` 或
`GPS age≤1.5 s` 的参数都会使 `threshold_policy_passed=false`，不会进入通过的质量子门。

RGB-SLAM 也沿用同一条 past-only 时间规则：相机帧只能匹配不晚于该帧的姿态和本地 NED；
位置失鲜后，速度/空速 dead-reckoning 必须分别通过自己的时间新鲜度窗口，未来或陈旧样本会使
该帧 RGB-SLAM 量测直接缺席；重复/倒序相机帧也不会重发上一帧三角化结果。这样可以在户外
设备时钟发生 skew 或传输重试时阻止“未来姿态 + 当前图像”拼接出看似稳定、且被重复加权的目标距离。

任何一项不满足时，Jetson 发送不可用状态或 MultiGCS 隐藏地图标记；界面不会用旧坐标、未锁定 GPS 或猜测位置填充地图。

## 室外实际距离标定

室外单目深度不再只依赖一个全局比例。现场从已知真值靶标导出的 CSV 可以选择
`piecewise_linear` 模型：先按 raw depth 排序并合并重复读数，再剔除明显的距离顺序冲突和孤立错配，
最后用单调 PAVA 曲线保留近、中、远端的非线性尺度。曲线至少需要 6 条测量、4 个严格递增 knot；
测量域外使用最近端线性外推，同时把拟合残差门作为额外 `σ` 下限，避免远离实测域时继续显示过窄的置信区间。
旧的 affine 标定文档 schema v1 继续可读，分段文档使用 schema v2，并在启动时重新校验所有 knot、样本数、
内点数和单调性。

```powershell
Set-Location D:\Projects\Multi-Detect
$env:PYTHONPATH = 'src;.'
$py = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
& $py scripts\fit_outdoor_depth_calibration.py `
    captures\outdoor-depth-candidates.csv `
    --output artifacts\calibration\outdoor-piecewise-v1.json `
    --profile outdoor-piecewise-v1 `
    --model piecewise_linear
& $py scripts\validate_outdoor_depth_calibration.py `
    captures\outdoor-depth-candidates.csv `
    --calibration artifacts\calibration\outdoor-piecewise-v1.json `
    --output artifacts\calibration\outdoor-piecewise-v1.validation.json `
    --require-independent-holdout `
    --minimum-independent-holdout 3 `
    --maximum-holdout-mae-m 2.0 `
    --maximum-holdout-rmse-m 3.0 `
    --maximum-holdout-error-m 6.0 `
    --maximum-holdout-relative-mae 0.20
& $py scripts/verify_outdoor_depth_calibration_report.py `
    --calibration artifacts\calibration\outdoor-piecewise-v1.json `
    --report artifacts\calibration\outdoor-piecewise-v1.validation.json `
    --require-quality-gate
```

运行时 metadata 会记录 `metric_depth_calibration_mode`、raw 实测范围和外推状态；
`calibration_extrapolated=true` 时距离仍可作为估计值参与融合，但不允许把它当作已完成该距离段的室外真值。
`--require-independent-holdout` 会把独立样本数、MAE、RMSE、最大绝对误差和相对 MAE
变成 fail-closed 质量门；未通过时验证器写出完整报告但返回非零，不能继续把该文档作为部署就绪标定。
不带该开关时报告仍是 advisory，兼容早期仅用于诊断的回归。当前台式机没有连接 Jetson、相机或
室外 GNSS，因此仓库只验证拟合器、独立 holdout 质量门、文档和运行时映射，尚未生成现场实测标定结论。
部署前可用 `verify_outdoor_depth_calibration_report.py` 再绑定一次报告与标定文件的 SHA-256；
Jetson 启动器只有在显式设置 `METRIC_DEPTH_REQUIRE_CALIBRATION_VALIDATION=1` 时才强制要求
严格门通过。这个绑定仍是软件证据，不替代室外 RTK/测量仪真值。

## 室外定位真值验收

接入飞机后，把 MultiGCS 卡片中的估计坐标与测量员/RTK/测量仪记录的同一目标坐标写成
CSV（也支持 JSON/JSONL）。离线验收器使用 WGS-84 Vincenty 逆解，不使用平面球面距离，
并输出水平误差的均值、中位数、P95、RMSE、最大值、北/东偏差以及报告 `1σ` 的覆盖率。
默认至少需要 10 条独立样本；产品阈值和不确定性覆盖率必须显式传入，避免把“样本数量足够”
误写成“精度达标”。该报告是 `REAL/field` 的只读证据，不会修改运行时或标定文件，也不会
生成飞控命令：

从当前报告版本开始，评估器还会把所有残差转换到真值点的局部 WGS-84 `N/E` 米制坐标，
输出 `error_ellipse`：北/东 bias、`2×2` 样本协方差、`1σ` 主轴/次轴及真北顺时针方向，
并用二维卡方 `χ²(0.95, 2)=5.991464547` 给出 `major_95_m/minor_95_m`。这是对误差
方向性的描述，不会把圆形 `horizontal_sigma_m` 改成椭圆，也不会改变 QGC 现有落图/复制/快照
门；旧的 v1 报告没有该可选节时仍可被 verifier 读取。只有把现场 RTK/测量仪真值配对后的
报告绑定进发布门，椭圆数字才具备 REAL 精度证据含义；SIM/Docker 合成样本只能验证算法和
报告重算链路。

```powershell
Set-Location D:\Projects\Multi-Detect
$env:PYTHONPATH = 'src;.'
$py = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
& $py scripts\validate_target_geolocation_accuracy.py `
    captures\target-geolocation-survey.csv `
    --output artifacts\evaluation\target-geolocation-accuracy-20260901.json `
    --minimum-samples 10 `
    --maximum-p95-m 8 `
    --minimum-one-sigma-coverage 0.50 `
    --minimum-two-sigma-coverage 0.90
```

如果现场已经保存了 `target-event-replay` bundle，不要手工抄写估计坐标。新的入口会先
验证 `manifest.json`、事件流和媒体哈希，再用 `event_id` 或 `frame_id` 与独立真值表严格配对；
只有 GPS `qualified` 且在目标帧时刻为 `fresh` 的事件才会进入报告。默认要求每个可用事件和每条
真值记录都被配对，任何漏配、重复 ID 或目标 ID 冲突都会 fail-closed：

```powershell
Set-Location D:\Projects\Multi-Detect
$env:PYTHONPATH = 'src;.'
$py = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
& $py scripts\build_target_geolocation_accuracy.py `
    artifacts\target-events\manifest.json `
    captures\target-geolocation-truth.csv `
    --output artifacts\evaluation\target-geolocation-accuracy-from-replay-20260901.json `
    --minimum-samples 10 `
    --maximum-p95-m 8 `
    --minimum-one-sigma-coverage 0.50 `
    --minimum-two-sigma-coverage 0.90
```

安装后的等价命令是 `multidetect-target-accuracy-from-replay`。真值表的标识列可用
`sample_id`、`event_id`、`frame_id` 或 `id`；若同时存在多个标识，它们必须指向同一个值。
需要只评估一个目标时使用 `--target-id`。`--allow-unmatched-events` 和 `--allow-extra-truth`
只用于明确标记的诊断子集，不能放宽 GPS 质量、新鲜度或报告重算门。

由 replay 入口生成的 accuracy report 还会带 `selection` 对象：其中包含源 manifest 的
SHA-256/字节数、选中/可用/匹配/跳过计数、未匹配事件 ID、未使用真值 ID、目标过滤器和
诊断开关。`verify_geolocation_accuracy_report()` 会校验这些计数的守恒关系（例如
`eligible = matched + unmatched`、`truth_rows = matched + unused`）；旧版没有该对象的
报告仍按 v1 统计字段兼容读取。

### 自动生成现场真值采集表

为了避免从回放包手工抄写估计坐标，先用模板入口导出一份可填写的 CSV（也支持 `.json`/
`.jsonl`）：

```powershell
Set-Location D:\Projects\Multi-Detect
$env:PYTHONPATH = 'src;.'
$py = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
& $py scripts\create_target_geolocation_survey_template.py `
    artifacts\target-events\manifest.json `
    --output captures\target-geolocation-survey-template.csv `
    --summary-output captures\target-geolocation-survey-template.summary.json
```

入口会先验证 replay 的 manifest、事件流和媒体哈希，只导出在目标帧时刻同时满足
`GPS status=qualified`、观测时间存在且 `gps_freshness=fresh` 的事件。表格保留
`sample_id/event_id`、源帧、目标 ID、估计纬经度、报告 `1σ`、GPS 观测时间和数据年龄；
`ground_truth_latitude_deg` 与 `ground_truth_longitude_deg` 两列留空，由 RTK/测量仪或
独立测量员填写。由于 `frame_id` 与 `event_id` 可能不同，模板把源帧放在
`source_frame_id` 列，避免被真值加载器误认为第二个冲突标识。

JSON envelope 的机器可读约束见
[`configs/schemas/target-geolocation-survey-template.schema.json`](../configs/schemas/target-geolocation-survey-template.schema.json)；
CSV/JSONL 是同一行结构的表格化变体。

摘要会记录源 manifest SHA-256、筛选总数、可用数和被跳过的 stale/unknown/unqualified
原因。模板声明 `contains_truth=false`、`gate_passed=false`，没有 fresh 事件时命令返回
`1`；它不是精度证据。填写完真值列后，再把同一 CSV 交给上面的
`build_target_geolocation_accuracy.py`，由 WGS-84 evaluator 重新计算并显式指定精度门限。
已存在的模板默认受保护，重新生成需显式加 `--overwrite`。

CSV 最小字段为 `sample_id,estimated_latitude_deg,estimated_longitude_deg,
ground_truth_latitude_deg,ground_truth_longitude_deg`，`reported_sigma_m`、`target_id` 和
`captured_at_s` 可选。`ground_truth` 只存在于这条离线评估输入，SIM/实时运行时数据结构
仍保持 truth-free；只有 `gate_passed=true` 且报告哈希被记录后，才可作为最终 REAL 精度
材料的一部分。

## MultiGCS 显示

在 Fly 主地图处于主视图时，合格结果会显示：

- 红色十字：目标的 WGS84 投影位置；
- 黄色圆圈与标签：水平 `1σ` 误差；
- 左上角“目标落图 · 只读”证据卡：坐标、误差、类别、融合距离、**相对**方位、目标/源帧和数据年龄；
- “复制坐标”：把 `纬度, 经度` 写入本机剪贴板；
- “保存快照”：把当前合格的目标 ID/类别、坐标、`1σ`、距离、相对方位、测距有效性、源帧和 UTC 保存时间写入本机 JSONL 证据文件；只有当前目标的 `VALID/DEGRADED` 测距会写入距离/方位，`INVALID/UNAVAILABLE` 不会被当成实际距离。

切换目标、取消 TRK/LCK、目标 ID 不匹配、GPS 降级或数据失效时，地图标记会清除。仍存在当前目标但不能定位时，证据卡明确显示“不可定位”、说明原因并禁用复制/保存按钮；它不会用旧坐标、估算点或空坐标填充地图。若地图画面没有覆盖该点，证据卡仍保留合格的文本状态，但不会在错误位置绘制图标。

目标事件回放弹窗对每一条通过 native hash、GPS 新鲜度和坐标域校验的历史事件提供“落图预览”。
预览使用橙色标记和同一 WGS-84 局部曲率换算的 `1σ` 圆，只把事件投影到当前主地图，
不写入 `MultiDetectState`、不改变实时目标、不产生 Mission/Guided/飞控消息。选择新清单或点击
“清除落图预览”会清除该历史标记；不可定位事件没有预览按钮，也不会保留上一个事件的点。

## 本地快照记录

快照保存到应用的本机数据目录下 `snapshots/target-snapshots.jsonl`，每行是一条 `multidetect.target-evidence.v1` JSON 记录；不上传网络、不包含 operator key、视频帧、深度图或飞控指令。记录上限为 500 条，达到上限时界面会拒绝继续保存，要求先归档该本地证据文件。`v1` 快照的纬度采用严格 `(-90, 90)` 边界；损坏、越界或精确极点记录在加载时被忽略，不会重新变成地图点。该本地快照规则不改变目标事件回放 `multidetect.target-event-replay.v1` 的读取兼容性。

这是用于事后核对的轻量元数据快照，不等同于目标事件回放。快照故意不携带视频帧和深度图；需要把画面、深度、实际测距和 GPS 绑定成同一条时间线时，使用下面的本地回放 bundle。

## 目标事件回放 bundle

Python 侧的 `multidetect.target_event_replay.TargetEventReplayBuilder` 将一次目标捕获写成一个只读证据目录：

```text
<bundle>/
├── manifest.json       # bundle/schema/事件流/媒体摘要与 canonical bundle_hash
├── events.jsonl        # 每行一个 multidetect.target-event.v1 事件
└── media/
    ├── 000000-video_frame.jpg
    └── 000000-depth_map.bin
```

每个事件同时绑定：视频帧或深度图、目标 ID/类别、捕获时间、实际距离及 `VALID/DEGRADED/UNAVAILABLE` 状态、相对方位、GPS 质量、WGS84 坐标和水平 `1σ`。GPS 可带与事件共用单调时钟的 `observed_at_s`；回放事件会写入派生的 `gps_age_s` 和 `gps_freshness`（`fresh`/`stale`/`unknown`），加载时重新计算并校验，防止把旧坐标冒充当前观测。通过校验的事件可以在 QGC 做上述只读地图预览，但仍不能成为实时目标或任务输入。写入和加载都会执行以下门：

定位核心把 GNSS→camera 杆臂和相机射线组成一个完整 NED 目标向量。姿态、mount 三轴和像素中心的
`±1σ` 扰动会同时更新 ray/camera-offset 端点，并用于 slant、DEM、ground range 和 bearing sigma；
WGS-84 只投影最终 N/E offset 一次，不会再次叠加杆臂。

- `events.jsonl`、每个媒体文件和 manifest 都有 SHA-256 与字节数校验；路径必须留在 bundle 内，禁止 `..` 穿越；
- 事件序号从 0 连续递增，捕获时间严格递增，媒体路径不能重复；
- 若多个事件带有 GPS `observed_at_s`，这些观测时间也必须在回放中保持非递减（允许中间的旧版事件省略该字段）；这样不会把较新的目标帧和较旧的飞机定位样本拼成“时间倒退”的证据。
- `qualified` GPS 要求 `fix_type >= 3`（MAVLink 合法范围 `0..8`）、至少 6 颗可用卫星（`0..254`，
  `255` 是未知哨兵）、水平精度 `<= 6 m`、垂直精度 `<= 10 m`，且纬度必须在 `(-90, 90)`、
  经度在 `[-180, 180]`；越界经纬度和未来观测时间均按未知处理。若提供 `observed_at_s`，它不能晚于捕获时间，
  且超过 `1.5 s` 的合格观测会被判为 stale 并拒绝物化。旧 bundle 缺少该字段时显示 `unknown`，保持可读但不构成实时新鲜度证明；
- 回放 v1 的 `status=qualified` 使用产品默认 GPS 阈值，并把位置健康作为已由来源声明的质量状态；v1 没有序列化自定义
  `AdaptiveRangingConfig` 或独立 `position_healthy` 字段。实时发布仍必须通过当前 telemetry 的 `position_healthy`、时间新鲜度和调用方策略，
  因此回放的 legacy `unknown`/来源声明不能替代 SIM/HIL/REAL 的实时 GPS 质量门；
- 实时 `AdaptiveRangingConfig` 也由 `mavlink_gps.py` 的 canonical 常量约束：`hAcc`、`vAcc`、GPS age
  只能收紧，不能放宽到 `6 m`、`10 m`、`1.5 s` 之外；放宽配置会在构造时失败，不会绕过目标落图门；
- `UNAVAILABLE` 测距不得携带距离或相对方位，`unavailable` GPS 不得携带坐标/质量测量，不会填入假坐标；
- manifest 的 claims 固定为 `read_only_evidence=true`、`contains_flight_control=false`、`contains_truth=false`。回放加载器没有任务、执行器、MAVLink 或 simulator truth 输入口。

现场采集器或 Jetson 可以先写一个 JSON capture manifest：顶层包含 `session_id`、`events` 和相对于
manifest 的 `camera_calibration`（也可改用 CLI 参数）；
每个事件包含 `event_id/frame_id/target_id/target_label/captured_at_s/gps/ranging`，
`video`/`depth` 则填相对于该 capture manifest 的本机文件路径、编码和可选宽高。脚本会把源文件复制进一个新的空 bundle，
写入哈希和严格事件流：

```powershell
Set-Location D:\Projects\Multi-Detect
$env:PYTHONPATH = 'src;.'
$py = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
& $py scripts\create_target_event_replay.py `
    captures\target-events.json captures\target-event-bundle `
    --camera-calibration captures\camera-calibration.json
```

标定会以原始字节复制为 bundle 根目录的 `camera-calibration.json`，manifest 绑定 ID、bytes 与 SHA-256；
加载时重新哈希并执行完整相机标定语义校验。用于 field evidence 时，该标定还必须与物理 mount survey
引用的 calibration ID/SHA-256 完全一致。

随后验证并筛选 bundle（脚本只打印元数据和相对媒体路径，不读取或输出媒体字节；输出中的 `gps_age_s`/`gps_freshness` 是加载时重新计算的结果）：

```powershell
Set-Location D:\Projects\Multi-Detect
$env:PYTHONPATH = 'src;.'
$py = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
& $py scripts\inspect_target_event_replay.py artifacts\target-events\manifest.json
& $py scripts\inspect_target_event_replay.py artifacts\target-events\manifest.json `
    --target-id target-42 --start-s 10 --end-s 30
```

机器可读结构见 [`configs/schemas/target-event-replay-manifest.schema.json`](../configs/schemas/target-event-replay-manifest.schema.json)。地图黄色 `1σ` 圆仅使用 WGS-84 局部曲率把米转换为当前投影像素；跨日期变更线由经度归一化处理，后端目标坐标仍以 Vincenty 椭球直解为准。QGC 的 `target-snapshots.jsonl` 仍然是轻量卡片记录；事件 bundle 是分开的本地容量/保留单元，二者都不会自动产生飞控指令或上传网络。

## 室外验收

1. 在开阔天空等待飞控 GPS 进入合格状态，并确认 MultiGCS/Jetson 状态不再报告 `fix_type=0` 或 `gps_navigation_not_qualified`。
2. 对一个静止、可识别的目标建立 TRK；等待坐标、距离和 `1σ` 都稳定，并确认卡片的目标 ID 与当前 TRK 一致。
3. 在已知真值点记录至少 10 个时间间隔样本：目标坐标、`1σ`、数据年龄、飞机 GPS 水平精度、融合来源与距离。
4. 用独立测量计算水平误差。不要以单次命中宣称精度；分别报告中位数、P95 和误差是否被报告的 `1σ` 合理覆盖。
5. 重复目标切换、取消 TRK、GPS 遮挡/降级和地图缩放测试，确认不会残留过期标记，也没有生成任何任务或飞控命令。

当前现场服务可运行并不等价于已完成上述室外真值验收。若 GPS 未锁定，隐藏坐标是预期安全行为。
