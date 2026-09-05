# Multi-Detect 飞行模拟器架构计划

状态：`v0.3.0-pre.8`。S1 编排器、交互控制代理、确定性 flight model、manual/AI
closed-loop fixture、多旋翼/VTOL/fixed-wing 3-D 资产 gate、有界 `sim_flight` 控制策略和
interactive `MANUAL_CONTROL` freshness watchdog 已实现；headless Gazebo 单帧目标像素和 quarantined
candidate 三控 smoke 已通过，完整 PX4/Gazebo 视觉/测距/落图闭环、人工 QGC 视觉、HIL、REAL 仍须分别
通过验收门；Windows Docker 的三机型
external runtime/live-AI machine gates 与单机型持续 PX4 reported-position response 子门已通过，
但不等同于视觉/QGC/HIL/REAL。最终
`v0.3.0` 不提前发布。

## v82 实际 PX4 estimator covariance 观察

在 pinned PX4/Gazebo VTOL 外部容器中，主机只读 companion link 请求 `ODOMETRY` 并走生产
`PixhawkReadOnlyTelemetryProvider`。最终正式 capture 收到 64 条、接受 64 条 source-bound covariance，
47 条变化记录全部具有非零角方差；最新 roll/pitch/heading `1σ` 为
`0.478°/0.476°/7.287°`。这与 v81 Gazebo sensor publisher 的全零 covariance 是两条不同来源：v82
验证的是 PX4 estimator 经 MAVLink 的真实非零输出，v81 验证的是 Gazebo camera/odometry wire。

采集只发 telemetry interval request，未发送飞行控制；报告和 verifier 绑定当前 provider、脚本、parity
并重建派生门。容器 stop/rm 已通过。SIM 观察不能代替 HIL/REAL Pixhawk 或室外飞行验收。

## v81 三机型外部 `MDGZSIM3` evidence

三类机型各由独立 pinned container 采集 3 帧，frame-set 保存 wire 原始 covariance 和 backend 最终
sigma/source，验收时重新从 wire 计算并与持久化值比较。三次运行的 covariance 均为全零，因而使用
显式 floor；这暴露的是 Gazebo 默认 publisher 的能力边界，不是 transport 丢字段。所有容器均已
stop/rm，证据仍保持 software/SIM、truth-free、无物理飞控输出的边界。

## v80 `MDGZSIM3` 姿态 covariance 路径

Gazebo transport 读取 `OdometryWithCovariance.pose_with_covariance.covariance.data` 的 36 项 row-major
矩阵，提取角度 3×3 block 并在 producer/backend 双端验证 finite、对称和正半定。wire-v3 的固定 header
为 276 bytes，矩阵与像素/导航状态共同受 record SHA-256 保护。backend 以数值 Jacobian 把 Gazebo
fixed-axis X/Y/Z 不确定性转换为 NED/FRD Euler `1σ`，变换后再应用配置下限。该 producer/consumer 已
纳入 core parity；v81 已补齐外部 PX4/Gazebo v3 runtime，旧 v2 证据仍不被重命名或冒充新协议通过。

## v71 干净提交上的 Combined provenance 闭环

在提交 `73429e505d33ca5c645be3d3d0b6cda667f33c5f` 且 Git status 为空时，完整重跑 v70 的
原生目标点击→TRK→共享测距/落图→PX4 Mission Combined 路径。source
`combined-sim-qgc-20260905T175019Z-64536` 自身记录 `dirty=false`，900/900 sensor、RTSP、
type-23/type-24、full odometry，832 份 range/geolocation、336 份合格坐标、496 份 withheld、
0 transport error；独立 verifier 44/44。该证据把源码、portable、五组 parity、原生 UI 交互和
同一 Mission run 绑定到一个可审计干净 HEAD，但仍明确保留 `operator_visual_acceptance=not_claimed`、
`real_pixhawk_contacted=false` 和 software-only 边界。

## v70 原生 QGC 目标选择与定位进入同一 Combined 生命周期

当前 VTOL composite camera 的 SDF pose 与 calibration 都使用 35° pitch-down。Combined runner 在
RTSP 画面出现候选目标后，从 QGC 导出的 QML scene geometry 计算屏幕中心，验证命中窗口属于 exact
PID/HWND，再发送真实鼠标点击。验收要求 selection ACK、TRACKING status 与 QML adoption 同时相关，
随后 active operator target 才进入共享 ranging/geolocation。

range contribution 的 wire 权重以 254 为总预算联合量化：每个上报来源至少保留 1，但四舍五入总和
不得超过 254。这避免主来源约 0.99875 与微小 VIO 约 0.00125 被独立编码成 254+1 后遭 decoder
fail-closed。current-parity 900 帧 run 发布 831 份 range/geolocation、112 份合格坐标且 0 transport
error；该闭环仍是 software/SIM display-only，不升级人工视觉、HIL/REAL、真实硬件或室外 GNSS 门。

### v59 多高度/距离/坡度定位矩阵（2026-09-05，pre.8）

单场景定位 evidence 升级为 schema v2，并绑定 `gazebo_target_geolocation_acceptance.py` 的源码
SHA-256。证据在所有 prediction 结束后从 truth/DEM 派生 AGL 高度区间、真实地面距离区间和目标处
坡度；这些值只用于评分与工况分类，不返回 detector 或 ranging。矩阵评估器会重新复算每份 source，
并要求 source release/evaluator identity 完全一致。

默认覆盖门为：3 个正场景/15 帧、1 个零坐标负场景/5 帧、3 个高度档（间隔至少 `5 m`）、3 个
距离档（间隔至少 `5 m`）和 2 个坡度档（间隔至少 `3 deg`）。v59 新增 mid/high slope 资产并取得
实际 Gazebo camera/NavSat/Odometry 帧，正向平均 AGL 为 `6.196 / 12.168 / 20.149 m`，距离为
`8 / 15 / 25 m`，坡度为 `0 / 4.004 / 7.970 deg`；三档均 5/5 落图，独立 verifier 重现，近地平线
负向 5/5 拒绝坐标，因此 matrix `gate_passed=true`。

Gazebo OdometryPublisher 在高出生点首回调会用接近零的 `dt` 派生瞬时竖直速度。helper 仅在首个有效
Odometry 与首帧之前最多丢弃 8 次该精确异常；预算耗尽或进入有效流后仍 fail-closed。该适配不改变
wire、共享算法或 release parity，且不声明 QGC rendering、人工视觉、室外 GNSS、HIL、REAL、硬件或
实飞通过。

### v56 相机 frame 语义与实际 Gazebo 像素三控（2026-09-05，pre.8）

三个 composite model 的 `mono_cam` include 和 fixed joint 现在都以 `base_link` 为同一 pose reference。
运行时对照确认 x500 `base_link` 高于 model origin；旧 include 以 model frame 解释会让 camera 低于 terrain，
新 frame 语义把 lens 恢复到地面以上。图像链固定为：

```text
Gazebo Image (top_to_bottom RGB)
  -> gazebo_sensor_stream: stride normalize + RGB-to-BGR, no row flip
  -> MDGZSIM2 BGR24 (top_to_bottom)
  -> GazeboSimBackend: preserve row order
  -> detector / existing integrated SIM path
```

v21 从 pinned disposable PX4/Gazebo 采集 3/3 帧，frame-set 同时绑定 image、CameraInfo、NavSat、Odometry、
wire record、model-root aggregate SHA 与 `source_row_order=wire_row_order=top_to_bottom`、
`vertical_flip_applied=false`。runtime 证据 SHA-256 为
`3FBF2BDE258718EC9287703CDC5905F5B6766A1A60301145A5EB99BCD182643D`；首帧原始 BGR24 SHA-256 为
`3D1BA6BB669AD9A01AEE3031B88CBBB607C7BD2190DA637ECC2159B41B5BD933`；正立预览 SHA-256 为
`98C980BE9A3C0BBAC889A3D95D4545D1DA1CE15F5232631E290DD3C1DF787559`。容器清理完成。

独立 truth 没有进入 Gazebo runtime 或 detector input。评估前先用仓库纹理标注与 billboard 平面投影冻结
frame annotation，再由同一个 quarantined candidate session 依次读取 present、zero blank、bbox occluded
三份像素。v22 得到 present `car` confidence `0.8012787104` / IoU `0.8882733111`，两个 negative control
均无检测，`gate_passed=true`；证据 SHA-256 为
`D48865EC9296C2778D7CF7D6C486D632410689EF1B23BAFC99BEE04A55365BC9`。评估器允许 frame-set 添加不可矛盾的
provenance 字段，但核心 Gazebo/MDGZSIM2/truth-isolation 身份仍逐项严格，行序字段缺失成组或值错误会在
session construction 前失败。相关聚焦回归 `326 passed`，Ruff/compileall 通过。

相机 model-root/stack 字节变化后，v54 三机型 deterministic sources 被 verifier 正确拒绝。v56 为
fixed-wing / multirotor / VTOL 各重新生成 source，并产生 2,371-byte binding，SHA-256
`7DAB627CC1211EFDD60A98C71575EE697E5062B9CB1020AB905B7A0CFFF896B2`；它仍是 dry-run
protocol/asset/control preflight，不是三机型外部 camera runtime。release parity 的 safety-policy hash 更新为
`8131E780BD60AEAD395F3860D2137F36C103C047D6BDB739E4AA3F60C4F9489D`，完整 parity 文件 SHA-256 为
`893E55ED8BDADE76DF85A5F50ADB3471D517237DE6F7B5F094C07AFE278C3397`，与当前 source byte-identical。
最终全量严格回归为 3,199 tests：`3198 passed, 1 skipped, 0 failed, 0 errors`；JUnit SHA-256
`2799FCA4C87F095F0218CC73FB65B03B7E6BD8C63978CE8C470B6ED5911CBBBC`。

E7 的当前输入不再指向 v54 3-D source 或旧 v52 external summary。wrapper 默认绑定三份 v56
camera-frame source；external 层先证明旧 v52 join 对当前 parity 会 fail-closed，再从其六份不可变源记录
创建 v56 current-parity join，并执行第二次 source recomputation。新 join / revalidation SHA-256 为
`D5120A0F72D6BE3236D74BFFAAC89644F8022881C76EAF08B32E95B67404327F` / 
`AA925E65C0501C6A159EDE440391704B912D75F7B0690A988AB18D818BF96D46`；E7 内部
`simulator_external_evidence.passed=true`、`simulator_3d_control.passed=true`。这只重新绑定既有
software-only source，未重新启动外部 runtime。

report-only / strict 对同一组当前输入分别退出 `0` / `2`；两份报告都保持 `gate_passed=false`、
`release_allowed=false` 和 32 条 blocker。strict report SHA-256 为
`B4BE420E2E8C0D9F9008E7AF74BED1C9849AC8789E8A3CA1E0FDEE2FB52F87DF`。候选检测证据仍未接入
model-review，因为它处于 quarantined 且没有部署域 accuracy；因此当前 E7 不会把一帧三控提升为生产模型。

v56 只证明真实 headless Gazebo camera pixels 上的一帧三控 smoke，不是模型 accuracy benchmark、生产模型
批准或人工 QGC rendering。共享 ranging、target geolocation/map/evidence、三机型动态视觉、真实
Jetson/相机/Pixhawk、室外 GNSS、HIL、REAL 与实飞继续独立阻断；版本仍是 prerelease。

### v55 持续 AI → PX4/Gazebo reported-position response（2026-09-05，pre.8）

v55 没有新建 simulator-only UI、控制器或飞行算法，而是在现有 Docker acceptance 上增加显式且有界的
动态响应子门：

```text
PX4 heartbeat + GPS_RAW_INT + GLOBAL_POSITION_INT
                                      |
truth-free synthetic target refresh   |
  -> SimulatorTelemetryBridge.ingest_perception
  -> SimulatorAiExecutor
  -> shared SimulatorControlAuthority + watchdog
  -> existing SimulatorMavlinkControlAdapter
  -> loopback MAVLink MANUAL_CONTROL
  -> disposable PX4 SITL / Gazebo
  -> PX4 GLOBAL_POSITION_INT
  -> shared wgs84_distance_m response gate
```

synthetic target 只携带 visible/bearing/distance/confidence，不得携带或制造 GPS、armed、姿态、Gazebo pose
或 simulator truth。`SET_MODE(POSCTL)` 和 `MANUAL_CONTROL` 都走同一个 loopback control socket；只有
flight exercise 才在 hash-pinned disposable container 内设置 `COM_RC_IN_MODE=1`。第一个 accepted TRACK
原子锁定最新有效位置；持续窗口要求 target lease coverage、最大 refresh gap `<=0.5 s`、heartbeat 首尾
连续、当前仍为 armed POSCTL、mode loss/rejection/failsafe/watchdog 全为零，并达到配置的 PX4-reported
WGS-84 位移。接收空 poll 不再耗用 frame 配额；失败/成功 observation end 都在 DISARM 与容器清理前冻结。

canonical v4：

| 检查 | 结果 |
|---|---|
| artifact | `terminal-continuation-v55/terrain-ai-flight-response-v55-pre8-v4.json`；661,936 bytes；`B56F20AB40B3E8AF9DF169FAAED9D4F326FFCDFDD052ED0FC6FF804FD7C44073` |
| gates | runtime / AI policy / target policy / flight response 全部通过 |
| sustained target | elapsed `8.172 s`；68 accepted / 0 rejected；coverage `8.062 s`；max gap `0.485 s` |
| PX4 state | armed POSCTL、custom-mode enabled、mode loss `0`；heartbeat max gap `2.844 s < 3.0 s` |
| response | 171 个 `GLOBAL_POSITION_INT` 样本；最大水平位移 `0.818282 m >= 0.5 m` |
| AI safety | 557 accepted；AI rejection/failsafe/watchdog 均为 `0` |
| provenance | `gazebo_truth_consumed=false`；`gazebo_pose_injection_used=false` |
| cleanup | DISARM + adapter neutral + container stop/rm；容器无残留 |

当前代码全量回归共 2,907 tests：`2906 passed, 1 skipped, 0 failed, 0 errors`；JUnit
`artifacts/evaluation/terminal-continuation-v55/full-regression-v55-px4-position-response-final-20260905.xml`
为 411,702 bytes，SHA-256
`A2C2595C49D0B663468D09274D22976CC1B4D9128425BB23458445B1C5397F51`。唯一跳过为 Windows 当前账户
缺少 symlink 能力，不是 runtime 或安全门失败。

这只关闭 x500 一次性 PX4 SITL/Gazebo 模拟机体位置响应机器子门。它没有使用 Gazebo camera detection，
也没有经过共享 ranging、`TARGET_GEOLOCATION`、QGC map/evidence UI；因此不是完整视觉闭环、坡地测距精度、
actuator/airborne flight、fixed-wing/VTOL 对等动态、人工 Fly View、硬件、室外 GNSS、HIL 或 REAL 证据。
v55 不修改 QGC C++/QML、不产生新 EXE，也没有进入现有 E7 三机型 external 六源；最近 E7 strict 仍是
v54 的 exit `2` / 32 blockers。

### v54 Gazebo wire-v2、Odometry 与 terrain AGL（2026-09-05，pre.8）

外部传感器路径现在是完整的 pose/velocity 路径，不再从常量姿态补数据：

```text
Gazebo Image + CameraInfo + NavSat + OdometryPublisher(3-D, 50 Hz)
  -> scripts/gazebo_sensor_stream.py / MDGZSIM2 wire v2 / whole-record SHA-256
  -> GazeboSimBackend
       world ENU -> local NED
       body FLU -> body FRD
       quaternion -> roll / pitch / heading
       NavSat MSL + scenario LocalDemGrid MSL -> AGL
  -> existing IntegratedSimSession / perception / mission / operator bridge
  -> existing native QGC Fly View
```

wire v2 的固定 header 为 204 bytes；image、CameraInfo、NavSat 和 Odometry 都是 required source。
每个输出帧检查时间戳、尺寸、有限位置/姿态/速度、四元数范数和 source skew。启动诊断只声明订阅已建立，
保持 `full_odometry_pose_velocity_fusion_claimed=false`；结束诊断只有在全部请求帧被接受并写出后才置 true。
模型使用 Gazebo 官方 `gz::sim::systems::OdometryPublisher` 的
`/model/<model>/odometry_with_covariance`，`dimensions=3`、`odom_publish_frequency=50`。

高度语义被强制拆开：Odometry world-up 只转换为 `local_down_m`，不会写入 `altitude_agl_m`。后者只能由
合格 NavSat 的 MSL 高度减去同一 scenario DEM 的 MSL 高程，reference 为
`GAZEBO_NAVSAT_MSL_MINUS_SCENARIO_DEM_MSL`。DEM 缺失/身份不一致、GPS 质量失败、越界、坏 datum 或
非正高度会原子清空 AGL。Docker evidence 还写入 `terrain_dem_sha256`、
`terrain_agl_reference` 和 `odometry_local_up_used_as_agl=false`。

v53 的非平地资产已接成隔离的
`configs/scenarios/validation/terrain_ranging_validation_001.scenario.json`；runtime、truth 和 expectations
各自 hash 绑定，外部 runtime 只读取 runtime channel。确定性 8-step 记录 SHA-256 为
`FB1F532E340F7E468DE0002824AC374C12C5960391F1EC3B1A6BBE6871CAD2D2`。真实外部
PX4/Gazebo x500 在该非平地 world 中采集 3/3 wire-v2 帧，证据 SHA-256 为
`8FB0FE4338EEA71021C7CCD8515ACFB2577847A2517297126C921AE4D825C4B0`，DEM AGL 为
`0.2303031043 m`，容器已清理且飞控命令数为 0。v54 该次 sensor runtime 发生在坡地 world 的 home 原点，
并未飞越不同
坡高或完成目标检测/测距闭环，因此只关闭 external sensor transport 子门；人工 QGC 视觉、完整动态坡地
航迹、室外 survey、HIL/REAL 和实飞继续独立验收。

OdometryPublisher 改变了三份 model SDF 的字节，因此 v52 三机型预检不能继续代表当前资产。verifier
按设计拒绝旧 model SHA，随后对当前 fixed-wing / multirotor / VTOL 重新生成 source 并绑定为
`artifacts/evaluation/terminal-continuation-v54/simulator-3d-control-binding-v54-odometry-pre8.json`
（2,359 bytes，SHA-256 `78066C298A9B9EEDB012B7596D9B7DA66E334DE7449EACDF1CCD59513181B00C`）。
该 binding 只关闭 deterministic protocol/asset/control preflight，继续记录
`external_runtime_gate_passed=false`、视觉/硬件 `not_claimed`。

E7 wrapper 的三份 3-D 输入已固定切换到 v54；当前 strict 原生退出 `2` 并保留 32 个 blocker。完整
Python 回归为 `2888 passed, 1 skipped, 0 failed, 0 errors`（2,889 tests），JUnit 409,003 bytes、
SHA-256 `C3D01A9F3A157A3B57225319A67FA7EE79883AAB70C025DA14E896F0C1EA4532`；唯一跳过为 Windows
symlink 权限项。

### v53 非平坦 world 与独立几何真值（2026-09-05，pre.8）

新增 `terrain_ranging_validation_001.sdf` 独立验证 world。它使用与 DEM 相同的 WGS-84/MSL 原点，
在 Gazebo ENU 中把局部平面滚转 `atan(0.1)`，形成北向 `+0.1 m/m` 的真实 SDF 坡面；两个高对比目标
按解析坡面高度放置。world 不含 Fuel、model include 或网络资源，后续可以在外部 Gazebo/PX4 中直接
加载，但本轮没有启动该进程。

验收器不读取注释中的公式，而是严格解析 SDF model pose、collision/visual plane 和
`spherical_coordinates`，独立求世界法向、有限平面覆盖、MSL 高程和 ray-plane truth。共享 DEM 的全部
25 个节点、16 个单元中心和 7 条射线均通过，其中 6 条跨越网格边界、1 条为垂直基准；最大斜距误差
`5.684e-14 m`。证据为
`artifacts/evaluation/terminal-continuation-v53/simulator-terrain-ranging-asset-truth-v53-pre8-v2.json`，
SHA-256 `CCB34D5AE3088DE1A66CCF4E6D521C7E009DEF1CD25051E6DDA61DC4A526FB9B`。

该结果关闭的是 deterministic SDF/DEM asset-geometry truth 子门。证据固定写入
`external_gazebo_runtime_gate_passed=false` 和视觉/硬件 `not_claimed`；仍需真实启动 Gazebo/PX4、由
camera/navsat/runtime 数据经过同一 ranging 路径并做人工 Fly View 检查，之后才能关闭 SIM 坡地 runtime
子门。室外 GNSS、HIL 和 REAL 也不由本结果替代。v53 完整回归为
`2865 passed, 1 skipped, 0 failed, 0 errors`（2,866 tests），JUnit SHA-256
`2E54AD65923CDB93D1BD570A0C726FCBFD32316FB3A0EB5A8D3B13A116205092`；唯一跳过是 Windows
symlink 权限门。

### v52 共享 DEM/GNSS 地形交点（2026-09-05，pre.8）

SIM 的 terrain 输入现在与 REPLAY/HIL/REAL 共用 `terrain_dem.py` 和 `MultiModalRangingEngine`。局部 DEM
以 WGS-84 原点和 MSL 高程绑定，网格单元内使用双线性面；相机射线按穿越顺序逐单元求精确交点，不再把
terrain URI 只当身份标签。GPS 绝对 MSL 高度、水平/垂直精度、DEM 高程/坡度误差、杆臂与三轴视线误差
进入同一个区间传播。GPS/DEM 时间错位、越界或无交点会停止该候选，不会退回平面地面伪造目标位置。

v52 三机型确定性 binding 为
`artifacts/evaluation/terminal-continuation-v52/simulator-3d-control-binding-v52-dem-ranging-pre8.json`，
SHA-256 `2B886AEF69197268A8658E1A5C8AA24D753DE4E560934CC245A667D7AA49948B`。它验证三种机型仍走同一
manual/AI authority、watchdog、场景与资源 hash 链，但记录
`external_runtime_gate_passed=false`、视觉/硬件 `not_claimed`。v52 当时的三机型 DEM 为平面；后续 v53
已加入独立非平坦 SDF/DEM 资产真值，但外部 Gazebo runtime、室外 survey、HIL 与 REAL 仍未关闭。

### v51 地图/DEM 文件级绑定与测距失败协议闭环（2026-09-05，pre.8）

每个 scenario runtime 的 `builtin://maps/<scenario>.geojson` 和
`builtin://terrain/<scenario>.dem.json` 现在必须解析到仓库内真实文件。stack plan 记录 map/DEM 的规范
URI、绝对/显示路径、大小、SHA-256 和 validated 状态，并通过 `MD_SIM_MAP_*` / `MD_SIM_DEM_*` 注入
外部 runtime；聚合 asset hash 覆盖 world、map、DEM、model SDF 与 model config。只写一个看似合法但
不存在的 URI 会立即 fail-closed，VTOL acceptance 也不会再报告资源可用。

当前三个 DEM 是匹配平坦 Gazebo world 的最小可信网格。它们让 SIM 资产身份和未来 terrain adapter 有了
确定输入，但尚未让 `MultiModalRangingEngine` 读取局部坡度；因此不能把这项通过写成坡地测距、室外
定位或真实 GNSS 通过。测距几何失败在 UI 方向则已补齐 bit 31 的共享 wire reason，确保 SIM 与 REAL
都能报告同一 `target_geometry_uncertainty_unbounded` fail-closed 状态。

### v50 三轴安装角误差共核传播（2026-09-05，pre.8）

SIM 不再只复现名义 camera ray；它与 REPLAY/HIL/REAL 一样，由同一个
`MultiModalRangingEngine` 对安装 pitch/yaw/roll 的共同 per-axis `1σ` 做正负端点扰动。banked fixed-wing
和离轴目标下，yaw/roll 对地面距离与方位的耦合会进入误差圆；任何轴的端点越过地平线都拒绝距离。
v50 external join/revalidation 在新 core parity 下通过，但复用了既有 truth-free software 运行证据，不能
替代新场景视觉精度、室外标定、HIL/REAL 或人工 QGC 验收。

### v49 共享 Fly View 的输出锁定显示（2026-09-05，pre.8）

SIM/HIL/REAL 继续使用同一个 `APPROACH_STATUS` 和 QGC Fly View。UI 的“模式 3 正在瞄准”现在不仅要求
approach active，还要求当前动态 `flightControlEnabled=true`；否则进入“目标已确认 / 飞控输出锁定 / 仅
显示姿态建议”分支。该变化没有增加 SIM 专用 QML 或分叉控制逻辑，反而把模拟器、HIL 与 REAL 的显示
契约收紧到同一 provider 权限源。offscreen boot、原生协议自测和软件回归已通过；人工视觉、真实 HIL/
REAL 与外场定位仍保持 pending。

### v40 3-D scene stream 的 Mission 进度绑定（2026-09-04，pre.8）

`SimulatorSceneSnapshot` 现在可以在同一 runner tick 保存可选的
`MissionProgressSnapshot`。该字段直接来自 `SimulatorClosedLoop` 的 observation-only 进度观察器，
所以 3-D renderer、headless replay、SIM fallback 与后续 HIL/REAL adapter 看到的是同一份
Mission canonical hash、GPS/link freshness、距离、驻留和完成游标。没有 Mission 的旧 snapshot
省略字段，保持历史 canonical hash 与回放兼容；带 Mission 的 snapshot 则由
`MissionProgressSnapshot.from_mapping()` 再解析，比例、序列、未知字段或 truth 注入都会闭锁；
同一条 stream 还要求各帧 Mission ID/hash 一致，拒绝任务混流或半缺帧。

`python -m multidetect.simulator.scene_state` 与 scenario runner 对齐，接受裸
`ExecutableMission` 或 `PlannedMissionCompilation` JSON 的 `--mission`，只有显式
`--start-mission` 才记录 Mission start。对外 schema 为
`configs/schemas/simulator-scene-stream.schema.json`，其内联定义保持单文件验证，不依赖网络解析。
这仍然只是 truth-free software/SIM replay 子门，不替代外部 Gazebo/PX4 `MISSION_CURRENT`、真实
GNSS/Pixhawk、HIL/REAL、人工 Fly View 或最终 `v0.3.0` 发布门。

### v35 加速度预算完整性门与当前 parity（2026-09-04，pre.8）

目标帧与 GPS 时间对齐时，telemetry 若给出 `acceleration_uncertainty_mps2` 却没有完整 N/E
速度，三机型共用的 WGS-84 投影现在 fail-closed，避免丢掉 `0.5·dt²·σa`。当前 parity SHA-256
为 `D9CF6D9E625E6B0ADB56084628CCB39ED358E2156AF89201633C2F6E1F2B9AEF`，core hash 为
`7D35526D63E3BFE247776F1ED2CC14F2950E0E61AB72E10A170C5C41BB930CF3`。v35 external
join/revalidation SHA-256 为
`B459F6CDA7E6BD60EE5BED486E56F25870D388E31650F54333AC2FA3741CD12B` /
`D825A2A8A1D8E3F152C35C6C8C70C3D847D9C26E3329A4F8A7EE2D27974B338C`，均为 software-only；
E7 总门仍为 `gate_passed=false`。

### v34 机动运动不确定度与 SIM/REPLAY 证据身份（2026-09-04，pre.8）

> v34 是上一份 parity snapshot，保留用于审计；当前 wrapper 使用 v35 external 输入。

三机型共用的目标 WGS-84 投影现在可接收水平加速度一西格玛包络
`acceleration_uncertainty_mps2`。均值仍以恒速外推，运动误差按
`hypot(|dt|·σv, 0.5·dt²·σa)` 进入目标落图圆；非法/非有限值或缺少完整 N/E 速度 fail-closed，字段
缺失的历史回放使用 NaN 哨兵并保持旧结果。该层覆盖 REPLAY、SIM、HIL、REAL 的共享 Python 核心，
不向 QGC wire 或飞控/载荷控制链路增加输入。

telemetry identity 扩展后，历史 Gazebo replay-set 以旧 recording hash 无法通过当前 verifier。
本轮从同一 capture 生成未覆盖历史文件的 v34 副本：
`artifacts/evaluation/terminal-continuation-v34/replay-set-sim-gazebo-2026/`，manifest SHA-256
`F577BF8085F33311D20AE27BB882C5C5F0F7225FC6F86B379A5384982E1FEDFF`、canonical hash
`F2834DC4CD03C95DC6502C137EAA66430A3F68CC2F145498889156621FA32E4C2`。其六层仍是
`synthetic_demo` 空 truth/prediction 基线，不能当作部署域精度或室外 GNSS 证据。

v34 阶段按当时 parity 重新验证的 external join/revalidation SHA-256 为
`80599422FD5D39E3445F8D3ADA5473CAC0F62D184023A2B2F2BD4CFF6E46C1FE` /
`06D4C67C5A095661F5D54A691C334C8F6D2576E5821CB365166C8CE2E28E1BAE`，均为 software-only。
v34 阶段 `run_current_release_gate_pre8.ps1` 的默认 replay-set 与 accuracy/calibration 切换到 v34；
E7 report-only 快照仍明确 `gate_passed=false`，现场、HIL/REAL、人工视觉和最终 release channel
保持 pending。

### v32 导航速度残差不确定度膨胀（2026-09-04，pre.8）

SIM、REPLAY、HIL、REAL 共用的 `NavigationVelocityFusionEngine` 现在对 GPS/VIO/空速候选
在通过 `3.5σ` 成对一致性门后计算 North/East 两轴残差 `χ² / (2*(N-1))`。Birge ratio
会在实际分歧超过声明协方差时放大融合速度 `σ`，追加
`navigation_velocity_uncertainty_inflated`，并将结果保持 `DEGRADED`、`advisory_only=true`；
该算法只影响态势感知和证据，不生成飞控或物理释放命令。GPS 质量门统一为 `fix_type>=3`、
卫星数 `>=6`，`255` 未知哨兵直接拒绝。

可重算软件验收记录：
`artifacts/evaluation/terminal-continuation-v32/navigation-residual-inflation-acceptance-20260904.json`
（3,351 bytes，SHA-256 `AA1E0EF13A40F9EDBD777B5D214F1601299E280DFF8E4B2B40353FF5037D47C7`）；
nominal `(10.0,10.1)` m/s 保持 valid，spread `(10.0,10.8)` m/s 在硬门内被降级且 σ 从
`0.2121` 放大到 `0.2828` m/s，专项测试 18 passed。`configs/release-parity.json` 当前
SHA-256 为 `723CC760FE277DB378BB3FA55BDB0D54149F72AF38C29D2EEC67EC95BA434B4B`，core hash
为 `354d6a61f1dc986eca15e30fefff7ee820e7d83d0c3a18f4071904fc2a194d35`。

基于新 parity 的 VTOL fresh live-AI 源、六源 external join 和独立 revalidation 均通过：
源文件 SHA-256 `081B922CD725ACD1F42ED218F142E7AE1E8E5A684CA02F622B4AECE738E12DA`，join
`7AA749EB13AA18A42A734D890401588B9885A7805969CEE2BAF3AB500923E392`，revalidation
`F5D1F057D85498C8E1A8BC563D950419E2D5AAAA9FDBBB28D13DF768D8A7348F`；文件均在
`artifacts/evaluation/terminal-continuation-v32/`，并保持 software-only 语义。

当前 wrapper report-only E7 快照位于
`artifacts/evaluation/terminal-continuation-v32/release-gate-current-20260904T070708Z/`：
manifest SHA-256 `064FD23D88A0FA65280D4493A6AB9330748DC8D23C1FA261557DC8E7E515EBA4`，report
SHA-256 `87D0D7F61FC802DBA66D54A592D3ED0AB30EB83115ADEBB091162D1E580D9187`。external、
scene-stream set 和 parity 子门通过；总门仍 false，现场硬件、真实 GNSS/Pixhawk、HIL/REAL、
人工 Fly View、replay-set 和最终 release channel 待验收。

### v33 现场链路只读预检刷新（2026-09-04，pre.8）

本轮台式机预检以 2 个样本重复 Ethernet、三端点协议和被动 route 检查。结构/freshness
校验通过，但当前 `Ethernet=192.168.1.13/24` 不在 `192.168.144.0/24`，Jetson/GR01/相机
均超时，`hardware_link_ready=false`。报告
`artifacts/evaluation/terminal-continuation-v33/field-link-preflight-live-20260904T-current.json`
为 12,923 bytes、SHA-256 `2B3694E74332B96B3257F405842B3584A44AE07D5FCEA8EC5E47B07DF31E651A`；
接口诊断为 4,412 bytes、SHA-256
`9800D930FD0FAF1D945420BF3849175F165DC463AEBD3308411143BDD99E9D68`。预检只读，不改 IP/路由，
不登录、不启动视频、不发送飞控数据。

用该报告重新绑定的 E7 report-only manifest/report 位于
`artifacts/evaluation/terminal-continuation-v33/release-gate-current/`，SHA-256 分别为
`C66DB565A12DC664C28F4DB12E38AD7E6611E28A14F650839362A18EDB817C19` /
`340CE3C01968194B77010A808AE8A09BD62F269D9D56C786224E88099A3827D1`。总门仍为
`gate_passed=false` / `release_allowed=false`，不升级现场 GNSS/Pixhawk、HIL/REAL 或最终版本。

### v31 多源测距不确定度传播（2026-09-04，pre.8）

> 历史条目：v32 导航速度残差层已在上方生效；本节保留 v31 测距演进记录。

SIM、REPLAY、HIL、REAL 共用的 `MultiModalRangingEngine` 现在对通过硬一致性门的多源
absolute/direct range 计算加权残差 `χ²/(N-1)`。残差超过声明协方差时按 Birge ratio 放大
融合 `σ`，并分别记录 `absolute_range_uncertainty_inflated` 或
`direct_range_uncertainty_inflated`；`RangeValidity` 变为 `DEGRADED`，放大的区间继续传给
WGS-84 目标落图和 evidence card。单源、协方差足以解释残差的多源结果保持兼容，且该路径
始终 `advisory_only`，不生成飞控或物理释放命令。

可重算软件验收记录：
`artifacts/evaluation/terminal-continuation-v31/ranging-residual-inflation-acceptance-20260904.json`
（SHA-256 `88516D2203D168C894945EDC5F1D7DFA6569F07B8DF9DD8ED910058BC27FA2DA`）。当前 parity
核心哈希为 `4ed386e9eaf91952b7c7be56d3d272efeaca4bb70a50e78ed57e04aa5307f5ad`。

算法变更后的标准 wrapper E7 report-only 快照位于
`artifacts/evaluation/terminal-continuation-v31/release-gate-current-20260904T065500Z/`：
manifest SHA-256 为 `CA97AE02767F93B691A3CB2B41F823FA0E53EB1D5D6B726C657D086861F523E1`，report
SHA-256 为 `2E66751A6D7E0DEB16276431C5F9DECE74969D783C3EC78461EDD95FE0EC9B6E`。Docker
Desktop Server `28.4.0` 已完成当前 VTOL live-AI 外部运行，三机型六源 external join
`simulator-external-runtime-ai-join-20260904-current-pre8.json` 与独立
`source_recomputed=true` revalidation 均通过；external runtime、AI policy 与 scene-stream
set 子门均为 `passed`。现场 preflight freshness 通过但硬件端点和 aircraft route 仍不可达，
真实 GNSS/Pixhawk、HIL/REAL、QGC 人工视觉和最终 release channel 继续待验收；总门仍为
`gate_passed=false` / `release_allowed=false`（31 条阻断原因）。
同一工作树的严格全量回归为 `2668 passed, 1 skipped`、退出码 `0`，日志
`artifacts/evaluation/terminal-continuation-v31/full-regression-v31-external-rebind-20260904.log`
（SHA-256 `837E9E87D8B5F40EB93001BDB8F674CF151B97D0E4DEFD0DD291934FD15A86F8`）。

### v30d scene-stream set 正式接入 E7 release-gate（2026-09-04，pre.8）

三机型 scene-stream aggregate 现在不再只是旁路 provenance：
`build_release_gate_manifest()` 与 `create_release_gate_manifest.py` 支持重复的
`--simulator-scene-stream-set-evidence`，Windows pre.8 wrapper 也默认把 fixed-wing、
multirotor、VTOL 三份源流送入同一份 E7 清单。`release-gate-manifest.schema.json` 对 aggregate
及其每条 source record 使用严格的 schema 定义。

`evaluate_release_gate()` 每次读取清单都会重新解析 aggregate，按 manifest directory 解析源流
路径，并调用 scene-stream set verifier 重算 source SHA、canonical SHA、scenario manifest hash、
bytes、release label、mode/scenario 唯一性、deterministic、truth-free 与 snapshot checks。
任何路径、版本、摘要或源流篡改都会把显式 binding 置为 `passed=false`；最终 `release` channel
要求该 binding 存在且通过，prerelease 缺失则保持历史兼容。这个子门仍只证明 truth-free
software/SIM 状态流契约，不声明外部 Gazebo/PX4、QGC 人工视觉、真实 GNSS/Pixhawk、HIL 或实飞。

本轮 E7 bound manifest/report 为
`artifacts/evaluation/terminal-continuation-v30/release-gate-continuation-20260904T133400Z/release-gate-pre.8-current-20260904T133400Z-v6-bound.json`
（20,828 bytes，SHA-256 `98A301DA0147010CBF6F927122304E3AE1F47B08B095B8E9D89D535A2C37547C`）和
`artifacts/evaluation/terminal-continuation-v30/release-gate-continuation-20260904T133400Z/release-gate-pre.8-current-20260904T133400Z-v6-bound-report.json`
（52,806 bytes，SHA-256
`1A8B3A8402132B02788217787D564B32F87E2552506152232843B8D528C61EE8`）。报告中
`simulator_scene_stream_set` 的 3 条记录与 9 项 checks 全部通过，但总
`gate_passed=false` / `release_allowed=false` 仍由 replay-set、现场链路、人工视觉、Mission、
HIL/REAL 与 prerelease channel 阻断。现场绑定使用的只读 preflight
`field-link-preflight-continuation-20260904-133250.json`（17,366 bytes，SHA-256
`F5E12FB3A202F87B0102FBBB0DEE122538D1328FEB292CCDF4FE68E4ADAF4B1F`）请求并收集 3/3 个样本，
仍为 `hardware_link_ready=false`，三个端点不可达且 route 源地址为 `192.168.1.13`。

专项 release-gate/scene-stream 回归为 `85 passed`；严格全量回归为 `2667 passed, 1 skipped`、
退出码 `0`，最终日志
`artifacts/evaluation/terminal-continuation-v30/full-regression-scene-stream-set-release-gate-final-20260904.log`
（3,700 bytes，SHA-256
`BEB6A74BC7CCB083F3B7AD3D93A99F0932FD9850D0E1684D5057F8BB45C4F8BF`）。结构化摘要
`artifacts/evaluation/terminal-continuation-v30/scene-stream-set-release-gate-acceptance-20260904-v3.json`
（2,858 bytes，SHA-256
`3FAAEF71A53E303B56EFBA550EDCD57A41C704894F860FA6DD3D080EF7D81214`；v3 绑定本次 3/3 现场样本，保留
v1/v2 历史，专项回归计数为 85）保留
`software_status=pass_with_environment_gap`、`hardware_status=offline_pending` 和
`release_allowed=false`。

### v30c 现场链路只读刷新（2026-09-04，pre.8）

台式机执行了带协议检查和被动 route 检查的单样本预检：
`run_field_link_preflight.py --protocol-checks --samples 1 --timeout-s 0.5
--expected-source-cidr 192.168.144.0/24 --require-route`。快照
`artifacts/evaluation/terminal-continuation-v30/field-link-preflight-terminal-20260904-125150.json`
为 4,570 bytes，SHA-256 为
`D3511368A646821965A6B6A7E16895E5547FCCE964CDB57CD1AB28B0C1347C78`。保存报告在
`--allow-unready` 下结构校验退出 `0`，但 `hardware_link_ready=false`：三个远端端点及其协议
探测均超时，route 源地址均为 `192.168.1.13`，不匹配 `192.168.144.0/24`；本地 UDP 端口可用。
这只刷新现场环境事实，不改变设备选择、飞控输出或现场/GNSS/HIL/REAL 发布门。

### v30b 三机型 scene-stream set 绑定（2026-09-04，pre.8）

单流 `scene_state` verifier 已经能够验证一个 truth-free、可重放的 3-D runtime stream；本轮
在其上增加 `src/multidetect/simulator/scene_stream_set.py` 聚合边界。`build_binding()` 要求
fixed-wing、multirotor、VTOL 恰好各一份，先逐文件调用既有 verifier，再绑定 scenario ID、
mode、scenario manifest/version、release label、canonical stream SHA、source evidence SHA、
文件 SHA/bytes 和 snapshot 数。`verify_binding()` 会从 manifest directory 重新打开每个源文件，
重算哈希和 checks，并拒绝重复场景、重复机型、版本/路径/记录漂移、未知字段和源流篡改。
因此同一份集合可以被 CI、headless replay 或外部 3-D bridge 作为只读 provenance 输入，仍不把
evaluator truth 混入运行时。

命令入口为 `scripts/verify_simulator_scene_stream_set.py`，安装 console script 为
`multidetect-sim-scene-stream-set-verify`，契约 schema 为
`configs/schemas/simulator-scene-stream-set.schema.json`。当前三份 120 帧流的聚合证据为
`artifacts/evaluation/terminal-continuation-v30/scene-stream-set-20260904.json`（2,785 bytes，
SHA-256 `A2141EE7115483885C717EE9944749609A83FC5B64872D5867EC149C4BCC76A5`），独立重验输出
为 `scene-stream-set-20260904.verify.json`（2,628 bytes，SHA-256
`A3A34A9C19FC96C7A40031B67909E0C77E7DC5E0E9C90A0E1971C7CDDAFED2E3`）。集合中的三个 mode
和 scenario ID 均唯一，所有 checks 为 true；external runtime、visual 和 hardware acceptance
均明确为 `not_claimed`。这是 software/SIM provenance 子门，不提升外部 PX4/Gazebo、人工
QGC Fly View、真实 GNSS/Pixhawk/Jetson、HIL、REAL、室外实飞或最终 `v0.3.0` 发布门。
聚合器、单流 verifier 与 runtime provenance 的专项回归为 `31 passed`，退出码 `0`；日志
`artifacts/evaluation/terminal-continuation-v30/scene-stream-set-runtime-focused-20260904-v3.log`
（101 bytes，SHA-256 `54F52A6D3F11141977876DDB2D6A7AF00DE6FB7FFCFE2AB8883643877CD9964D`）。新增的
顺序无关性回归确认同一组三机型源流以任意输入顺序生成完全一致的绑定字节。
结构化摘要为 `artifacts/evaluation/terminal-continuation-v30/scene-stream-set-acceptance-20260904-v2.json`
（2,198 bytes，SHA-256 `DD528AC09A8C006CF7EEF4ABC2346DE8937A42B7A7C270968CEB2755E7806D3F`），其中
`software_status=pass_with_environment_gap`、`hardware_status=offline_pending`、
`release_allowed=false` 被显式保留。
随后执行严格全量回归 `python -W error -m pytest --assert=plain -ra`，结果为
`2663 passed, 1 skipped`、退出码 `0`；日志为
`artifacts/evaluation/terminal-continuation-v30/full-regression-scene-stream-set-determinism-20260904.log`
（3,521 bytes，SHA-256 `95DEB2C9EE32624D9B117F026D34BD169F30320E6D735A298B0D66FC6404865B`）。

### v29 三机型外部 runtime/live-AI parity 重新绑定（2026-09-04，pre.8）

历史 external join 仍绑定旧 core-algorithm parity，当前 wrapper 已明确拒绝该漂移。本轮
保持 `configs/release-parity.json` 原值不动，重新执行三种机型各一份 passive PX4/Gazebo
runtime 和一份 live-AI runtime，六个源文件全部通过共同的 scenario identity、manifest hash、
release label、digest-pinned image、truth-free 和硬件控制隔离检查。AI 源记录还通过正向
armed heartbeat、`ai_steps>0`、target-policy 和 target-exercise gate；所有容器均在退出时
停止并移除。

当前 join：
`artifacts/evaluation/terminal-continuation-v29/simulator-external-runtime-ai-evidence-20260904-current.json`
（5,479 bytes，SHA-256 `1CD106451833C2CABBDFF3873DBF2893BB23D98C1D4EF0266ACAAD879AAFBE2E`）；
独立 revalidation：
`artifacts/evaluation/terminal-continuation-v29/simulator-external-runtime-ai-revalidation-20260904-current.json`
（560 bytes，SHA-256 `2848B8D8663DAB23AA8226E68876A513994D1F8BC1C59FC550432DFF3B443F8C`）。两者
均绑定 `v0.3.0-pre.8` 和当前 `core_algorithm_hash=994ae1dffb3856eac5d71b479a7377f3f1b1c5beda2d154d6c2ee3b733c15af3`。

当前源码严格回归 `python -W error -m pytest --assert=plain` 为 `2639 passed, 1 skipped`，
退出码 `0`；日志 `artifacts/evaluation/terminal-continuation-v29/full-regression-strict-current.log`
（3,041 bytes，SHA-256 `2615E7E55929F6AF229DD98CDBF0AA25798BFAFA0C676D3F13D94B7622B3EA37`）。
外部证据模块补齐显式 `python -m` 入口，模块 `--help` 与安装 console script 均验证通过。
随后根包 release-gate 导出改为惰性加载，`python -W error -m
multidetect.simulator_external_evidence --help` 也通过且不再产生 runpy warning；补充严格回归
日志 `artifacts/evaluation/terminal-continuation-v29/full-regression-strict-after-lazy-root.log`
（3,041 bytes，SHA-256 `F858F0C8FDC90706353EC0E9AADBF0B723D5F01BA129AC204B9D4C1645C3C778`）
仍为 `2639 passed, 1 skipped`。

重新绑定后的 release-gate report
`artifacts/evaluation/terminal-continuation-v29/release-gate-pre.8-current-20260904T014510Z-v6-bound-report.json`
仍为 `gate_passed=false`：外部 SIM 子门通过，但 replay-set、真实现场链路/室外 GNSS、REAL
目标精度与状态、人工 QGC Fly View、mission-path、HIL/REAL 以及最终 release channel
仍未满足。Docker/PX4/Gazebo 记录是 software-only disposable evidence，不宣称真实飞机、
真实相机或室外定位；版本继续为 prerelease。

现场补充诊断在当前 `192.168.1.0/24` 上只读扫描 254 台主机，5 台响应 SSH，MAVLink/RTSP
均无命中；报告
`artifacts/evaluation/terminal-continuation-v29/field-link-discovery-live-20260904.json`
（8,526 bytes，SHA-256 `479732E9D458CA620DCB658B485FB2F4675EBD279BB1B8B2B65E51838A367369`）
通过结构复核但 `hardware_link_ready=false`。发现结果仅用于定位网段，不会自动选择端点。

### v29 正向 replay evaluator 诊断夹具（2026-09-04，pre.8）

为验证 evaluator 的正例分层、IoU、延迟和帧对齐路径，在独立目录
`artifacts/evaluation/terminal-continuation-v29/replay-set-positive-synthetic/` 从已校验 raw
replay 派生了确定性夹具。五个正例层 (`day`、`night`、`haze`、`reflection`、`smoke_only`)
逐帧带真值/预测，`no_fire` 保留为空负例；manifest SHA-256 为
`61E69B979C33F88B1E044F6192292E2CC55BF5EF6928645A7F28E16F5172BF46`。

显式独立 evaluator 重算（`--reviewed --reviewer qa-team`）的 report SHA-256 为
`6EF07FAFE7BA56E696498A856532BF46FE35227F22ADDC5C048FE4DE8C8048F2`，五层正例覆盖通过，
aggregate precision/recall 为 `1.0/1.0`；readiness SHA-256 为
`1EB519C6A5350007973742593A60F669F94AECFD646E2CEED5730BD4E507C13C`。来源仍严格标记为
`synthetic_demo`，部署覆盖、独立标注和 `accuracy_validated` 保持 false，故该夹具只证明
评估器接线，不能绑定 canonical replay-set，也不能提升最终 REPLAY/SIM/HIL/REAL 发布门。

### v28e simulator orchestrator 资源句柄收口（2026-09-04，pre.8）

`SimulatorOrchestrator._launch_process()` 现在在 `Popen`/进程工厂返回后关闭
orchestrator 的父进程日志句柄，创建失败路径同样释放句柄；新增生命周期回归覆盖该约束。
严格警告全量回归 `python -W error -m pytest -q --assert=plain` 为
`2639 passed + 1 skipped`、退出码 `0`，日志为
`artifacts/evaluation/terminal-continuation-v28e/full-regression-warnings.log`。
带跳过摘要的复核日志为
`artifacts/evaluation/terminal-continuation-v28e/full-regression-with-counts.log`，SHA-256
`807A392CC092389C8E1F51361A1C7922671E32111473F760F65347D13D0566C6`。
版本仍为 `v0.3.0-pre.8`；该修复只改善本地 SIM 生命周期，不提升真实链路、外部 3-D、
人工 QGC、HIL/REAL 或最终发布门。

### v28d authenticated UDP header type hardening（2026-09-04，pre.8）

告警链的 signed UDP header 校验现在严格要求整数协议版本，拒绝 JSON 布尔值伪装的版本
或时间字段，并由 sender/receiver/离线 verifier 共用。3 个类型混淆回归向量和当前源码
全量回归 `2638 passed + 1 skipped` 已通过，ruff/compileall 均为 `0`；带完整 pytest 汇总的
日志 `artifacts/evaluation/terminal-continuation-v28d/full-regression-with-counts.log` 为
305,776 bytes，SHA-256 为 `D8769517FA595283741E7129ABBD96AB2CC5B868B83CED54117953B441B2B068`。
该修复只收紧软件数据链边界，不改变 SIM 的控制/场景流，也不提升真实链路、外部 3-D、
人工 QGC、HIL/REAL 或最终发布门。

### v28c scene-state lazy import 与全量回归（2026-09-04，pre.8）

`multidetect.simulator` 对 `scene_state` 类型和 verifier 使用惰性导出，scenario runner 只有
在 `record_scene_stream=true` 时才加载 scene-state 模块；因此直接运行
`python -m multidetect.simulator.scene_state --help` 不再触发 `runpy RuntimeWarning`，而默认
runner/matrix 的输出与 parity 不变。editable venv 已重新生成
`multidetect-sim-scene-stream-verify`，valid 120 帧 stream 复核退出 `0`。

当前源码全量回归为 `2635 passed + 1 skipped`，ruff/compileall 均为 `0`。日志
`artifacts/evaluation/terminal-continuation-v28c/full-regression-summary.log`（2,997 bytes，
SHA-256 `3FAF04E313E44E1EBBE4C5FADD85E17CF247EA35AC36A255B77B4DF530336811`）。该改动只改善
本地 SIM 启动边界，不提升外部 3-D、人工 QGC、真实硬件、室外 GNSS、HIL/REAL 或最终发布门。

### v28b 独立 scene-stream verifier 回归（2026-09-04，pre.8）

当前源码已经包含 `verify_file`/`verify_main` 和
`scripts/verify_simulator_scene_stream.py`，三份 120 帧 stream 均以
`--expected-release-label 0.3.0-pre.8` 通过文件 SHA、canonical stream SHA、逐帧 hash、
JSON Schema 和 round-trip replay。最新全量回归为 `2635 passed + 1 skipped`、退出码 `0`；
日志 `artifacts/evaluation/terminal-continuation-v28b/full-regression-summary.log`（3,521
bytes，SHA-256 `B04577D8AE62918985336F718DAB039B38C7819304B4D119A340CA828615C5D8`），
ruff/compileall 均为 `0`。

### v28 场景流 parser/schema fail-closed hardening（2026-09-04，pre.8）

`SimulatorSceneStream.load_json()` 现在通过 `strict_json_loads` 读取状态流，重复 JSON 键、
NaN/Infinity、非字符串嵌套键、重复 fault ID 以及 `pose/world_origin` 中的 evaluator truth
字段均拒绝；`simulator-scene-stream.schema.json` 对运行时对象的保留 truth 字段同步加上
`propertyNames` 约束。三份既有 120 帧 stream 重新通过 schema、canonical hash 与 replay，
未改写历史 artifact。全量回归日志为
`artifacts/evaluation/terminal-continuation-v28/full-regression-summary.log`（3,522 bytes，
SHA-256 `997AF8F7E50D96A699342ACE72EFBF8D778D247B5110AF5C2787D9E93EF1C171`），结果
`2634 passed + 1 skipped`、退出码 `0`；ruff/compileall 均为 `0`。该收紧只影响
truth-free software/SIM 状态流子门，REAL/HIL/硬件和最终版本门保持原状态。

独立 verifier 为 `scripts/verify_simulator_scene_stream.py`（安装后命令名
`multidetect-sim-scene-stream-verify`），输出文件级 SHA、canonical stream SHA、逐帧 hash、
schema 和 release label 检查，作为后续外部 3-D bridge/release-gate 的只读入口。

### v27 可重放 3-D 场景状态流（2026-09-04，pre.8）

`SimulatorScenarioRunnerConfig.record_scene_stream` 是一个显式 opt-in 的记录开关。打开后，
runner 在既有 `ManualClock → RuntimeBackend → flight model → AI/manual authority → watchdog`
闭环的每个 tick 生成 `SimulatorSceneSnapshot`，并在结束时封装成 `SimulatorSceneStream`。
快照绑定 scenario manifest/version/hash、SIM release label、仓库内 WGS-84 origin、实测
telemetry-after pose（含 local N/E）、传感器 observation、决策/控制收据、watchdog、authority
和 active fault IDs；snapshot 与 stream 均有 canonical SHA-256。默认关闭以保持历史矩阵
evidence hash 不变。

公共入口为 `multidetect.simulator.scene_state:main`（模块命令和
`multidetect-sim-scene-stream` 脚本），对外契约为
`configs/schemas/simulator-scene-stream.schema.json`。读取器只接受 truth-free runtime
channel，并对坐标域 `latitude ∈ (-90,90)`、`longitude ∈ [-180,180]`、序列连续性、时间
单调性和嵌套 hash 做 fail-closed 校验。native QGC `Fly View`、外部 3-D bridge 和 headless
replay 共享这条状态流；没有新增 simulator-only UI，也没有把 evaluator `truth.json` 混入
运行时。

本轮三份 120 帧记录分别为 `terminal-continuation-v27-scene-stream-vtol.json`
（SHA-256 `F5D3BB2674E52871CC8729AD05C108768A8B759E9AC3023E163C9CE922DD0A22`）、
`terminal-continuation-v27-scene-stream-fixed-wing.json`
（`D149453D429E2336FE217F075953C70B8897721B1A645A9DC7CE137C4E679D6A`）和
`terminal-continuation-v27-scene-stream-multirotor.json`
（`0A1792FE46A7D2544FB0D14794EFC0DFBB801204A76D9776C9D12E05712DF7E1`）；三者均通过 schema、
逐帧 hash、stream hash 与 round-trip replay。它们仍是 truth-free software/SIM 证据，不
替代外部 PX4/Gazebo、人工 Fly View、真实 GNSS/Pixhawk/Jetson、HIL、REAL 或实飞验收。

本轮回归收集 `2634` 项，`2633 passed + 1 skipped`，退出码 `0`；ruff/compileall 均为 `0`。
完整日志 `artifacts/evaluation/terminal-continuation-v27c/full-regression.log`（3,477
bytes，SHA-256 `79204B900F8FBAC1F6E48B4670A1B0C008975689933B6091CA63EE9E3A989D37`）。

### v26 三机型 deterministic SIM 复核（2026-09-04，pre.8）

本轮重新运行三种机型的共享 `ManualClock`、runtime backend、flight model、AI authority、
watchdog 和故障尾段矩阵，输出
`artifacts/evaluation/terminal-continuation-v26-scenario-matrix.json`（13,300 bytes，
SHA-256 `06C8E829CCA2321C5D10F2F700E04D2469772E0C9C6E7711738B160DBC110FC1`）。三个场景
均完成 2,611 帧，故障覆盖率为 `1.0`，重复 hash 一致，矩阵 verifier 通过。随后执行
`artifacts/evaluation/terminal-continuation-v26-interactive-acceptance.json`（113,273
bytes，SHA-256
`041B882EA4A9C3CB44A3AF2A74E61D3F216C99C4D3AE24B924D74E300BBFBAB8`），VTOL、fixed-wing、
multirotor 共用 native-QGC protocol adapter、AI/manual authority 和 watchdog 路径。

这两份记录仍是 truth-free software/SIM evidence；外部 PX4/Gazebo 3-D 渲染、GPU 视频、
人工 Fly View、真实 Pixhawk/Jetson/GNSS、HIL、REAL 和最终 `v0.3.0` 继续保持未验收。

v26 全量回归日志
`artifacts/evaluation/terminal-continuation-v26/full-regression.log` 记录
`2624 passed + 1 skipped`、退出码 `0`，SHA-256
`A70388821B0E7A4B8BE782162681BD3FDEFB5F710F40701A8A24C08617BC596E`。

### v25 现场链路 freshness-bound wrapper 复核（2026-09-04，pre.8）

为验证 SIM/REAL 证据边界仍然清晰，当前 wrapper 使用 v25 现场 watch 报告
`artifacts/evaluation/terminal-continuation-v25/field-link-preflight-live-20260904a.json`
重新生成绑定样本（SHA-256
`53E45C0C4D7B6EB7942BB7438A90F29BC422893E3E6259F97A8A4C33EF74AB39`）。manifest SHA-256
为 `6C854C43E932FF4CC010B099A44ACED2CF62749C3A86D8405F27C6CFB6271CA4`，report SHA-256 为
`E192B4E7BED44B2B13660F58A019CF2CCAE572DB12D8C2F2012E4FCC67AD6B84`；`max_age_s=900` 和
`report_timestamp_epoch_s` 均存在，schema 错误数为 `0`。这次绑定时现场仍
`hardware_link_ready=false`，所以只证明 freshness/fail-closed 证据链，不能替代外部
PX4/Gazebo、室外 GNSS、HIL、REAL 或人工 Fly View。当前工作树全量回归收集 `2625` 项，
结果为 `2624 passed + 1 skipped`，日志
`artifacts/evaluation/terminal-continuation-v25/full-regression-final.log` SHA-256 为
`74CCDE8D3C72276B8122DFB03579D1E76CAF98AAD0F5030F6F7AEB7DD3CDC0B7`；ruff 与 compileall
均以退出码 `0` 完成。v25b 现场报告随后绑定到完整 goal-acceptance，快照
`artifacts/evaluation/terminal-continuation-v25/goal-acceptance/multidetect-goal-acceptance.json`
SHA-256 为 `4514AF0836C0E33117475B3BD1C272F24D94A857B215C253BAFE05CFCE16F5D7`；其
`software_status=pass`、三机型 SIM/external 子门通过，但 `hardware_status=offline_pending`，
不能替代外部 PX4/Gazebo、室外 GNSS、HIL、REAL 或人工 Fly View。

### 2026-09-04 goal-acceptance v24 最新 freshness-bound 快照（pre.8）

REAL 现场链路 freshness 绑定后重新执行的总验收快照为
`artifacts/evaluation/terminal-continuation-v24/multidetect-goal-acceptance.json`，46,146
bytes、SHA-256
`5E5820365FD95E8BBC3987E8559A88878922CC3E3CD478D3A60AB6272C53E3C3`。
`software_status=pass`、核心收集 `2624`、QGC custom `66`，现场 freshness 约 `27.2 s`
且在 `900 s` 窗口内，但 `hardware_status=offline_pending`、`field_link_ready=false`；
三机型确定性 SIM 子门保持通过，外部 PX4/Gazebo、GPU/人工 Fly View、HIL、REAL 和实飞
仍未宣称。`release_gate_manifest` 与 evaluator 现在绑定并重验成对的
`max_age_s`/`report_timestamp_epoch_s`，最终版本继续保持关闭。

### 2026-09-04 goal-acceptance v23 最新 freshness 快照（pre.8）

在缺失时间戳回归加入后重新执行的总验收快照为
`artifacts/evaluation/terminal-continuation-v23/multidetect-goal-acceptance.json`，46,003
bytes、SHA-256
`ABB54E58F04C12330E37B3F2574DF73979B8C6224DC8D8F5EDDEF9BDE2D35DEB`。
`software_status=pass`、核心收集 `2623`、QGC custom `66`，现场 freshness 通过但
`hardware_status=offline_pending`、`field_link_ready=false`；三机型确定性 SIM 子门保持
通过，外部 PX4/Gazebo、GPU/人工 Fly View、HIL、REAL 和实飞仍未宣称。

### 2026-09-04 field-link freshness 绑定与 goal-acceptance v22（pre.8）

现场链路证据现在由 `verify_field_link_report(..., max_age_s=...)` 进行可选时间约束：
one-shot 以 `generated_at_epoch_s` 为准，watch 以 `finished_at_epoch_s` 为准。
`run_goal_acceptance.ps1` 默认使用 900 秒窗口，并把 freshness 结果和年龄写入总快照。
v22 使用的现场报告 SHA-256 为
`0F211E23FD0C2671C8424C276E41993EB7DABB15059A4585A044B000D3F1D682`，freshness 通过；
总验收快照
`artifacts/evaluation/terminal-continuation-v22/multidetect-goal-acceptance.json` 为
46,003 bytes、SHA-256
`B2569EAF162AD9E33266DE25ABC1D61EEFB7A214C9021807A02A80C7078CD8B2`，
`software_status=pass`、核心收集 `2622`、QGC custom `66`。设备端点仍不可达，
`hardware_status=offline_pending`，所以该 freshness 子门不等于真实 PX4/Gazebo、HIL、REAL
或人工 Fly View 通过。

### 2026-09-04 goal-acceptance v21 当前台式机快照（pre.8）

三机型 3-D 控制绑定后重新执行的完整验收快照为
`artifacts/evaluation/terminal-continuation-v21/multidetect-goal-acceptance.json`，45,234
bytes，SHA-256
`78498FAA088110E49B640BF3BA605B23B133142BC10AAFC6B184E1091F53D089`；原始日志
`artifacts/evaluation/terminal-continuation-v21/goal-acceptance-rerun.log` 为 57,303 bytes，
SHA-256 `F23336F60FE289A63DDAE65E9551180A5B594B28EE4924FD2181352D1913292F`。本快照确认
软件门、QGC custom `66`、协议/QML 和三机型确定性 SIM 子门通过，但现场网络仍离线，
`hardware_status=offline_pending`、`field_link_ready=false`，六项现场/REPLAY live gates
未关闭。它没有启动或宣称外部 PX4/Gazebo、GPU/人工 Fly View、HIL、REAL 或实飞通过。

### 2026-09-04 三机型 3-D SIM 控制绑定（当前 pre.8）

当前源码重新生成了 VTOL、fixed-wing、multirotor 三份 3-D 控制/资产预检证据，并使用
`scripts/verify_simulator_3d_control_evidence.py --bind` 进行哈希绑定。绑定文件
`artifacts/evaluation/terminal-continuation-v20/simulator-3d-control-binding.json` 为
2,284 bytes，SHA-256
`967C162D4B996C4CA709B678732A8690FFA18256B6AD958CDCC08EE6CAC61B4E`；
`all_airframes=true`、`all_deterministic_gate_passed=true`、`records_hash_bound=true`。
该结果关闭的是共享 manual/AI/authority/watchdog 契约与仓库 world/model 的确定性子门，
没有启动外部 PX4/Gazebo 进程，也没有宣称 GPU、人工 Fly View、HIL、REAL 或实飞通过。

### 2026-09-04 外部组合 SIM/QGC/Mission 续验（当前 pre.8）

外部 Docker PX4/Gazebo、同一 native QGC Fly View 和 VTOL Mission helper 已完成一次 900 帧
组合重跑。300 帧首轮在 QGC type-24 sequence `300` 处无 post-adoption 余量而严格失败，失败
目录继续保留；900 帧结果位于
`artifacts/evaluation/combined-sim-qgc-20260903T164443Z-4900/combined-evidence.json`，
249,113 bytes，SHA-256
`0DC4044DB459888D4DC1648CC8266B545FBEF86D346FF45E8199C75909911762`。独立
`scripts/verify_combined_sim_evidence.py` 生成的复核文件为 1,956 bytes、SHA-256
`A31188E69D6EAA5A8752CA11CEF06D2CFE63AAB8601A3ABCEC2F980B536CF0CF`，40 项检查全部通过。
随后为该 source 独立生成 sim release-mode envelope
`artifacts/evaluation/terminal-continuation-v10/release-mode-sim-combined-qgc-20260903T164443Z-4900.json`
（542 bytes，SHA-256 `80AB0572968BE5992E523BDD8ED2E402D996FAA772A9ED70AB0974769FF1EB52`），并用
`--require-release-mode` 重新复核，输出 2,596 bytes、SHA-256
`BDD55C221332DDB27599433AF09B58F078C26CC95399EA88B97D84EC5D1489F9`，40/40 checks 通过。
随后使用同一 source/envelope 写入独立 E7 report-only/strict 目录
`artifacts/evaluation/terminal-continuation-v10/e7-combined-qgc-rerun/`；manifest、report-only
和 strict report 的 SHA-256 分别为
`D2A4FE349CB45D0AC872ACA589532B6A3E2586EB1196856791647BE708FA9207`、
`DA99390FE22DFFD8D3280DCAA94B3A5B89E5755BAB23DA2C0A661E14A4D71B18`、
`D71E6AB91BEEE570A5F321ED3A9309D9AADCE9433B197D2E0CE9CC03A023E3D5`。E7 的
`simulator_combined` 子门保持通过，strict 总门仍退出 `2` 并保持 `release_allowed=false`。

这次运行保持单一 QGC Fly View surface：仓库 portable QGC 与固定 digest
`px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`
绑定同一场景、RTSP/type-23/type-24 帧身份和 Mission journal。camera/CameraInfo/NavSat 必需
publisher 稳定 `2/2`；9 个动态 loopback 端口唯一、`14550` 未改变、全部释放；容器真实启动并
清理。900 帧同步屏障完整，Mission-ready 后 QGC type-24 为 `569→574→579`；7 个 INT-only
Mission item 全部 readback/reached，VTOL `MC→FW→MC`、AUTO.MISSION、land/disarm 通过。
这仍是 software-only machine evidence：原生 QQuickItem 像素/传输绑定证明为真，
`operator_visual_acceptance=not_claimed`，不提升人工视觉、Pixhawk/Jetson/相机、室外 GNSS、
执行器、HIL/REAL 或最终版本门。

### 2026-09-03 迁移稳定的 SIM evidence（当前 pre.8）

runner 与矩阵的 canonical 序列化现在使用仓库根相对 POSIX 路径：
`world_origin.world_path`、`manifest` 和 `scenario_directory` 不再携带本机盘符或临时 checkout
前缀；绝对路径仍由运行时保留用于解析 Gazebo/SDF 资产。把相同场景复制到另一份等价
checkout 的回归已证明单场景 `evidence_hash` 和矩阵 `sha256` 一致。

当前矩阵
`artifacts/evaluation/simulator-scenario-matrix-20260903-portable-paths-pre8.json`（13,512
bytes，文件 SHA-256 `06C8E829CCA2321C5D10F2F700E04D2469772E0C9C6E7711738B160DBC110FC1`，
canonical SHA-256 `CE87333B43B3D6E5964C031E27AE57EA3F682787EB54FE2F202DCD5FAD1FF95E`）经独立
verifier 退出码 `0`；fixed-wing、VTOL、multirotor 的 9 个声明故障事件均已观测且重复 hash
一致。它已被显式绑定至 report-only E7 清单
`release-gate-pre.8-current-20260903T084500Z-v6-bound.json`（SHA-256
`818A77FAB6F7C5AC6735726438CB16E447D16A7058A7C17E7611E8CC4438ED9D`）；最新报告为
`release-gate-pre.8-current-20260903T103906Z-v7-alert-link-bound-report.json`（SHA-256
`BD79B0B83FB46C2A531327B273AC5E40A8566FBF52A9F0D49780D497FEA4FF64`）。矩阵子门通过，但
E7 总门仍因 REPLAY 正向真值、现场链路、人工 Fly View、HIL/REAL 等缺口保持关闭。
本轮全量 Python pytest 收集 `2603` 项，结果为 `2602 passed / 1 skipped`；PowerShell wrapper
解析与独立矩阵 verifier 退出码均为 `0`。完整日志为
`artifacts/evaluation/pytest-full-20260903-release-gate-alert-link-wrapper-v1.log`（SHA-256
`4D3E0AA472C2675EFF31EA656293317C926D7BD63056C94412ED2C74EFADAD1C`）。

当前导航融合也遵循同一条 SIM/HIL/REAL 质量契约：`NavigationFusionConfig` 的 GPS 速度源
复用 canonical `fix_type >= 3`、已知卫星数 `>= 6` 下限，尝试放宽时配置构造直接失败；速度
新鲜度使用更紧的 `0.75 s`，目标落图坐标仍为 `1.5 s`。本轮 parity 已更新为
`0AFF3DF8913E8C99C8495FAD743F1E85D68716DE86719E1D91BA628F47343DF7`，核心 hash 为
`434b86c6a8b677e60a4ae5766586b603855609e43cc0521695aa1e21995963f9`。相机标定与户外深度
 标定样本现在走严格 JSON loader（BOM 兼容、重复键/NaN 拒绝）；该 parity 与当前 E7 绑定
 仍不改变真实硬件、HIL/REAL 和最终发布门状态。

ONNX Runtime provider 选择由共享 helper 固定为 `TensorRT → CUDA → CPU`。默认 provider
列表只有在 session 构造失败时按后缀回退，显式 provider 列表构造失败则直接记录错误；
model-check/jetson-bench 会输出可用 provider、尝试顺序、异常摘要和最终选择，SIM/HIL/REAL
三模式仍复用同一感知契约。

2026-09-03 又完成了一次三机型 native-QGC interactive 复核：
`artifacts/evaluation/simulator-interactive-acceptance-20260903-followup.json`
（113,273 bytes，SHA-256
`041B882EA4A9C3CB44A3AF2A74E61D3F216C99C4D3AE24B924D74E300BBFBAB8`）。VTOL、fixed-wing、
multirotor 的 deterministic gate 全部通过；AI、人工接管、watchdog neutral/failsafe、
loopback/identity 和 mission write 阻断均经过同一 native-QGC protocol adapter。该记录是
truth-free software fixture，外部 PX4/Gazebo、桌面视觉、Pixhawk/Jetson/相机、HIL/REAL
仍为 `not_claimed`，最终发布门不因该记录改变。

### 当前 pre.8 故障覆盖矩阵（2026-09-03）

随后用 `scripts/run_simulator_scenario_matrix.py --cover-faults --fault-tail 1` 对三机型
manifest 做了带覆盖要求的确定性重跑。该模式把 `requested_frames` 从默认的 `120` 自动
扩展到 `2,611`，等待每个有限时长故障事件实际触发后再收尾；fixed-wing 的 4 个、VTOL
的 2 个和 multirotor 的 3 个声明事件全部 `fired=true`，每个场景
`fault_coverage.coverage_ratio=1.0` 且 `all_declared_faults_exercised=true`。重复运行的
场景 `evidence_hash` 全部一致，顶层 `gate_passed=true`、`deterministic_gate_passed=true`。

当前证据文件为
`artifacts/evaluation/simulator-scenario-matrix-20260903-fault-coverage-pre8.json`
（13,512 bytes，SHA-256
`026EDF1BA4A4D436C9BEDF2AA61F88A2C86BD645EE632D292567E5E7C8F66307`）；独立
`verify_simulator_scenario_matrix.py --expected-release-label 0.3.0-pre.8` 退出码为 `0`。
它与 `simulator-scenario-matrix-20260903-wgs84-strict.json` 内容字节一致，仅作为本轮
故障覆盖运行的 provenance 别名，不产生新的算法 hash。该矩阵仍是 truth-free/software-only
SIM 证据，不能替代外部 Gazebo 3-D、人工 QGC、室外 GNSS、真实 Pixhawk/执行器、HIL/REAL
或最终 `v0.3.0` 发布门。

该故障覆盖矩阵随后被显式绑定到当时的 E7 report-only 清单（历史 provenance；当前 E7 以本节
开头的 portable-paths 矩阵和 084500Z 清单为准）
`artifacts/evaluation/release-gate-pre.8-current-20260903T133500Z-v6-bound.json`
（17,699 bytes，SHA-256
`52208E7C3EAA781369C304B1525EFA8D1FD2CEC99F2A1AE28A05B5D538656E42`）；对应报告
`release-gate-pre.8-current-20260903T133500Z-v6-bound-report.json` 为 46,311 bytes，
SHA-256 `3A9182A4E9576FF81FD248859C552DD4A5F971A996E667497B5FB0DEDC257780`。E7 清单重算
确认矩阵 verifier exit code 为 `0`，但总门仍 `gate_passed=false` / `release_allowed=false`，
不改变现场、REPLAY accuracy、人工 Fly View、HIL/REAL 或最终发布的 pending 状态。

## 2026-09-03 WGS-84 endpoint 与严格验收快照（历史基线）

SIM `DeterministicFlightModel` 的 telemetry 位置端点现与目标落图共用
`wgs84_forward_from_ned()` 椭球直解；高纬度米制换算、跨日期变更线归一化和极点穿越的
fail-closed 规则不再由两个近似实现分别维护。该历史基线使用的 parity 文件为
`configs/release-parity.json`（SHA-256
`CCDCA6FA7FBE5512200B9A4DCE40F7303E6591ED43BDC33D009AE93075441C2D`），其中
`core_algorithm_hash=338981686f2142144f438ce34b4f91a18f6cab76b9f3d8efd238ac1e3fd525e7`。

测距核心同时对相机/姿态/安装俯仰/目标像素的 one-sigma 视线扰动做地面距离传播；固定翼
roll/pitch 通过 NED 射线端点中心差分进入绝对方位 `σ`，目标落图再用 WGS-84 地面弧长
`r·σθ(rad)` 计算水平误差，避免 banked/pitched 状态与宽方位门下的过度自信；
近地平线扰动包络跨过地平线时不发布看似精确的 ground range；目标帧与 GPS 观测之间若有
完整时间/速度信息，也会在同一 WGS-84 路径中做有界推进或回推；时间差非零时如果输入了部分或
非有限 N/E 速度，目标坐标会 fail-closed，不会把旧 GPS 点静默当作零运动。时间对齐/视线不确定度基线快照
`artifacts/evaluation/multidetect-goal-acceptance-20260903-partial-velocity-fail-closed.json`
（SHA-256 `FBB3637449BD68013325B3840DAF7FC20414836EA65FC1B11CFBCBC64FA8DF01`）仍保留；
该轮 parity + external runtime/live-AI 的总验收是
`artifacts/evaluation/multidetect-goal-acceptance-20260903-bearing-uncertainty.json`
（SHA-256 `04AFC8C6C61E340020FB7E89483D00111B00C8D61B799289EEDF7F2B4B2BF459`）。

严格总验收快照
`artifacts/evaluation/multidetect-goal-acceptance-20260903-bearing-uncertainty.json`
（45,571 bytes，SHA-256
`04AFC8C6C61E340020FB7E89483D00111B00C8D61B799289EEDF7F2B4B2BF459`）以退出码 `0` 完成，
`software_status=pass`、核心测试收集 `2536`、QGC custom `66/66`，SIM 矩阵、external
runtime/AI、三机型 3-D 控制和 PX4 Mission transaction 子门通过；现场链路、Replay-set
accuracy、真实 GNSS/飞控/执行器、HIL/REAL 和人工 Fly View 仍是 pending。
对应场景矩阵
`artifacts/evaluation/simulator-scenario-matrix-20260903-wgs84-strict.json`（SHA-256
`026EDF1BA4A4D436C9BEDF2AA61F88A2C86BD645EE632D292567E5E7C8F66307`）由独立 verifier
重算通过，并将三机型/故障覆盖/重复运行摘要绑定进该快照。

对应 E7 绑定为
`artifacts/evaluation/release-gate-pre.8-current-20260903T024500Z-v6-bound.json`
（SHA-256 `3D379F24C67F438A287D5F714CD4AEE79C6877DE67B6691EE49AF2D012258BEC`），
report-only SHA-256 为
`C8749E0D7B5AB8C976477CF244586E734DFCEEBD5C07A8047689F62D5BCE040C`；严格门仍保持
`gate_passed=false` / `release_allowed=false`，因为现场、Replay-set accuracy、HIL/REAL
与人工 Fly View 证据尚未齐全。

### 台式机迁移后的现场网段发现（2026-09-03）

当前台式机先按显式 `192.168.1.0/24` 做了有界只读发现，避免把旧的
`192.168.144.0/24` 默认端点当成事实。随后追加只读协议级检查，最新报告
`artifacts/evaluation/field-link-discovery-20260903-093500-protocol.json`（8,436 bytes，SHA-256
`E10B84D77128E4C0FE64FB41C9805D5757C7F5F5832086C451ABB4B653193EC4`）扫描 `254` 个地址，
其中 `4` 个只命中普通 SSH banner；`5760/MAVLink` 与 `554/RTSP` 均无命中，本地 QGC metadata UDP
端口仍可用。`verify_field_link_preflight.py --allow-unready` 重新打开并校验该文件退出 `0`，
但预检摘要保持 `hardware_link_ready=false`。发现结果不会自动选端点或提升硬件门；接通飞机
后仍需对真实 Jetson/GR01/相机显式地址重跑预检，再采集室外 GNSS、`RANGE_STATUS` 与执行器证据。

## 2026-09-03 上一轮 parity 的 external runtime/live-AI 六源复核

台式机 Docker Desktop 已按当时 parity 重新完成三机型六源运行：每个 VTOL、fixed-wing、
multirotor 场景都有独立 runtime source 和 `--exercise-arm --exercise-target` live-AI source。
所有 runtime/AI source 使用同一 digest-pinned
`px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`；
runtime gate、telemetry GPS（`fix_type=3`、`satellites_visible=10`）、AI/target policy、目标
exercise 和容器 cleanup 均通过。输出仍由共享 `SimulatorMavlinkControlAdapter` 经本机
loopback `MANUAL_CONTROL` 产生，`hardware_control_enabled=false`。

该轮 join 报告为
`artifacts/evaluation/simulator-external-runtime-ai-evidence-20260903-bearing-uncertainty.json`
（5,365 bytes，SHA-256
`403785A43DACE03E2E9C029EBB6FE060E382001ABC32C59CD9987C43D2FFBB36`），`--join` revalidation
为
`artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260903-bearing-uncertainty-acceptance.json`
（545 bytes，SHA-256
`9DAC947BE5A24AEB1722677B51880D6F9EC8513CCC3F784B9D1684272CC4210B`）。两者都记录
`source_recomputed=true`、`gate_passed=true`、`release_label=0.3.0-pre.8`，并绑定当时
parity SHA-256
`CCDCA6FA7FBE5512200B9A4DCE40F7303E6591ED43BDC33D009AE93075441C2D`。对应总验收
`artifacts/evaluation/multidetect-goal-acceptance-20260903-bearing-uncertainty.json`
（45,571 bytes，SHA-256
`04AFC8C6C61E340020FB7E89483D00111B00C8D61B799289EEDF7F2B4B2BF459`）记录软件门通过、
核心测试 `2536`、QGC custom `66/66`，但现场 hardware 仍 `offline_pending`。该六源证据
是 software-only SIM provenance，不代替室外 GNSS/Jetson/Pixhawk、真实执行器、HIL/REAL、
视觉精度或人工 Fly View 验收。

2026-09-02 当前三机型 external runtime/live-AI 已拆成六个独立 source，并按同一
digest-pinned image 重新绑定：每个 VTOL、fixed-wing、multirotor 场景各有一份被动
runtime 报告和一份 `--exercise-arm --exercise-target` live-AI 报告。
`artifacts/evaluation/simulator-external-runtime-ai-evidence-20260902-final-six-sources-independent-v2.json`
（5,353 bytes，SHA-256
`79DBB81E4BB85366FFB7114DB2B3E9E6F921E8027133EE64603A680184B10440`）保存六源路径、
manifest/image/version 和每份 source 的 hash；`--join` revalidation 为
`artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260902-final-six-sources-independent-v2.json`
（558 bytes，SHA-256
`95A4CC58EDE1723E51B0DEBDFFECC261B6150A5374BBA49CD6B2BDEDBFFF783A`）。verifier 已强制
六个 runtime/AI source 路径互不相同，同一路径复用会 fail-closed。六源的
telemetry/GPS/AI/arm/cleanup 子门全部通过，`source_recomputed=true`、`truth_free=true`、
硬件边界关闭；原先三源/旧六源合并记录继续作为历史证据保留。
这批证据已强制接入
`artifacts/evaluation/multidetect-goal-acceptance-20260902-external-three-airframes-v7-independent-source-credentials-strict.json`
（42,100 bytes，SHA-256
`F69398FDC88D14AFA4FF0D182D0F6717108904CEDD2865DC6680CA9AD4EA5EC0`），goal acceptance
使用的 `--join` revalidation 为
`artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260902-final-six-sources-independent-v2-goal-v7.json`
（558 bytes，SHA-256
`DD60719E8969FA5F4D62680CBD545E9EF0A6F231CC6AA8FCEBDAAC3CF8D3FBF2`）。严格凭据模式下
`operator_credential_readiness.ready=true`、`gate_overridden=false`。该快照收集核心
测试 `2459`、QGC custom `66/66`、软件门与三机型 3-D binding；剩余清单只保留人工
Fly View、真实设备链路、ChArUco 实采、真实检测/伺服/失锁、Jetson soak 和 HIL/REAL。
Docker/SITL 证据不提升室外 GNSS、真实 Pixhawk/执行器或人工视觉状态。

执行器安全契约接入后的最新严格总验收为
`artifacts/evaluation/multidetect-goal-acceptance-20260902-actuator-safety-external-strict.json`
（43,745 bytes，SHA-256
`68F4B7565197E58B9346B668CE27234896D32DFA44A9E909FD03355C7B5B6C26`），收集核心测试
`2467`、QGC custom `66/66`，并保持 `operator_credential_readiness.ready=true`、
`gate_overridden=false`。执行器软件节点通过但 `physical_outputs_present=false`；
`hardware_status=offline_pending`，剩余现场门仍是 ChArUco 实采、真实舵机/失锁、真实
检测闭环、Jetson soak 与人工 Fly View，不提前推进最终 `v0.3.0`。

2026-09-02 的当前软件证据已重新绑定到六源 external runtime/live-AI 最新一轮：
`artifacts/evaluation/simulator-external-runtime-ai-evidence-20260902-final-six-sources-independent-v2.json`
与 `--join` revalidation 均通过，最新总验收为
`artifacts/evaluation/multidetect-goal-acceptance-20260902-external-three-airframes-v7-independent-source-credentials-strict.json`
（核心测试 `2459`、QGC custom `66/66`、`software_status=pass`）。这些 hash-bound 记录仍属于
truth-free/software-only SIM provenance；`hardware_status=offline_pending`，不能替代现场
Jetson、室外 GNSS、真实 Pixhawk、HIL/REAL 或人工 QGC Fly View 验收。

当前 pre.8 的 E7 发布门也已按这轮 provenance 重新固定：
`artifacts/evaluation/release-gate-pre.8-current-20260902-v7-independent-source.json`
（14,068 bytes，SHA-256
`5FCF05088BD73F03231BB8B0FDD49B74F79BC1C2849D2D4292BF98913794A8AB`）通过 release-gate
manifest schema，并绑定 REPLAY/SIM/HIL/REAL envelope、field-link、三机型 3-D control、
SIM safety/matrix、combined SIM/QGC/Mission 与 PX4 transaction。对应 report-only
`artifacts/evaluation/release-gate-pre.8-current-20260902-v7-independent-source-report.json`
为 40,740 bytes、SHA-256
`AF5DBA49793813E3B1A52F40814CDEB2DFCE1727B246D4DAFC8BE4A89EE8281A`，严格复核
`artifacts/evaluation/release-gate-pre.8-current-20260902-v7-independent-source-strict.json`
为 40,740 bytes、SHA-256
`568E8F34073CD31A3096C6A51DECCB70222CC462047DA32E329C7CA12DC0C441`；退出码为 `0`/`2`。
两份结果都保持 `gate_passed=false`、`release_allowed=false`。当前 E7 writer/evaluator
同时把六源 external runtime/live-AI join、独立 revalidation、六份 source hash 和 parity
写入顶层 `simulator_external_evidence`；goal acceptance 的同名节点继续作为软件总验收
provenance，两条证据都保持 truth-free/software-only，不混写成 REAL 或室外 GNSS 通过。

2026-09-03 E7 writer/evaluator/schema 新增共享 `actuator_safety` 顶层绑定。当前 pre.8
清单为 `artifacts/evaluation/release-gate-pre.8-current-20260903-actuator-bound.json`
（14,980 bytes，SHA-256
`393A329AE46944E20035D899FCF5BD7686183B71E172B132F62137CCA1071F65`），固定时间报告为
42,498 bytes、SHA-256
`94878B1D5230D26CAC56B5049251603F7BA7787FCD4D3ADC1345A021AE2F4355`。执行器绑定经过
源文件重哈希和 canonical verifier 复算后通过，并明确 `runtime_mode=HIL`、
`software_only=true`、`physical_outputs_present=false`；整体仍是 prerelease，现场真实
舵机/失锁、HIL/REAL 与人工 Fly View 门不变。

2026-09-03 又按当前源码重跑了带故障覆盖的三机型确定性矩阵：
`artifacts/evaluation/simulator-scenario-matrix-20260903-pre.8-fault-coverage.json`
（13,512 bytes，SHA-256
`C2A2D906263F0271A0873A1D42EAF25FA184423840DC0E050A3361E16BE6817A`）。fixed-wing、
VTOL、multirotor 都完成 2,611 帧，9 个声明故障事件覆盖率为 `1.0`，第二次运行的
`evidence_hash` 与首轮一致；`verify_simulator_scenario_matrix.py` 退出码为 `0`。
该文件是 truth-free/software-only SIM 证据，仍不替代外部 Gazebo 3-D、人工 QGC、真实
GNSS/Pixhawk、HIL、REAL 或实飞验收。

同一轮完整总验收已绑定该矩阵：
`artifacts/evaluation/multidetect-goal-acceptance-20260903-sim-matrix.json`（43,118 bytes，
SHA-256 `0FA334AA6824A6E965DB686A0CF5E5CADAD007840EE8C182AA872B70DDB90BA5`，脚本退出码 `0`）。
快照收集核心测试 `2480`、QGC custom `66/66`，`software_status=pass`、
`hardware_status=offline_pending`，并把矩阵 SHA-256
`C2A2D906263F0271A0873A1D42EAF25FA184423840DC0E050A3361E16BE6817A` 和 verifier 退出码 `0`
写入 `simulator_scenario_matrix_acceptance`。这只是 truth-free/software-only 绑定；真实 GNSS、
Pixhawk/Jetson、外部 Gazebo 运行、人工 Fly View、HIL/REAL 和最终 `v0.3.0` 仍由各自现场门决定。

2026-09-03 在台式机 Docker Desktop 上又完成了一轮三机型 external runtime + live-AI 复核。
VTOL、fixed-wing、multirotor 的新鲜 AI/telemetry source 均为 `gate_passed=true`，GPS
`fix_type=3/satellites_visible=10`、`ai_steps=2/ai_accepted=2`、目标 exercise 接受，
并完成容器 stop/remove；MAVLink `vehicle_type` 为 `22/1/2`。source 文件完整 SHA-256
记录在对应 artifact 中，分别为 `D646C94C...`、`23468BA7...`、`00E8428A...`。

新的 distinct 六文件 join 使用 2026-09-02 独立 runtime source 和上述三份新鲜 AI source：
`artifacts/evaluation/simulator-external-runtime-ai-evidence-20260903-final-fresh-ai-v1.json`
（5,338 bytes，SHA-256 `FE00BB61023738E3F3F1CEE98CA5ECA7D194A26585EFF06044140A1AEDA6FBA4`），
独立 `--join` revalidation 为
`artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260903-final-fresh-ai-v1.json`
（543 bytes，SHA-256 `A03F66998AB62558595A52201847E927D0676C78D4F4D7E996C7C955212FAD42`）；两者
均 `source_recomputed=true`、`gate_passed=true`。严格凭据总验收
`artifacts/evaluation/multidetect-goal-acceptance-20260903-fresh-ai-evidence-strict.json`
（43,480 bytes，SHA-256 `8971E0D996AD39A12DE2EEC822D338B4A03E70049730BE11B74AF98534AF8903`）
收集核心测试 `2480`、QGC custom `66/66`，`software_status=pass`、
`hardware_status=offline_pending`、`gate_overridden=false`。这只是最新 software/SIM
provenance 绑定，不改变真实 Jetson/相机/室外 GNSS/Pixhawk、人工 Fly View、HIL/REAL 或
最终 `v0.3.0` 发布门。

同一轮现场只读预检已重新采集并绑定
`artifacts/evaluation/field-link-preflight-20260903-current.json`（6,686 bytes，SHA-256
`8A6F3624D84FF11B94F8C65B87F2BDF5CABB7A6AF3A4B6075EAA39439A4E3798`）。显式 aircraft CIDR
为 `192.168.144.0/24`，但当前 source `192.168.1.13` 不匹配；Jetson `:22`、GR01 `:5760`、
RTSP `:554` 均超时，route/protocol 失败，本地 UDP bind 成功，故
`hardware_link_ready=false`。严格 verifier 退出 `1`，`--allow-unready` 结构 verifier 退出
`0`。新的 goal acceptance
`artifacts/evaluation/multidetect-goal-acceptance-20260903-fresh-ai-field-preflight.json`
（43,758 bytes，SHA-256 `1553655AEFCCC504F0C4111F265A36C39FAD29728523EB5ECE11729ABA3080B7`）
收集核心测试 `2480`、QGC custom `66/66`，software pass、hardware offline_pending，并将
field report 的 verified/ready/gate/route 状态原样绑定；飞机接通后沿同一端点重跑即可。

现场接入脚本现在有一个显式的迁移辅助开关：
`scripts/run_field_link_preflight.ps1 -AutoSelectInterface` 会调用
`prepare_field_link.ps1` 的只读网卡解析，仅在目标飞机网段命中唯一活动物理网卡时使用该
alias；零候选和多候选都立即给出诊断并保持失败关闭。它不替换 Jetson/GR01/RTSP 端点，不
添加 IP/路由，也不改变默认 `Ethernet` 行为。这样台式机迁移后把飞机插到 `Ethernet 2` 时
不需要先猜网卡名，同时不会把 `192.168.1.13/24` 误当成飞机网段；当前机器实际烟测因此
仍是明确失败而不是伪造 ready。

2026-09-02 的 SIM AI 质量门收口把 canonical GNSS policy 继续下沉到返航路径：
`SimulatorTelemetrySnapshot.gps_quality_valid` 采用 hAcc `<=6 m`、vAcc `<=10 m`，
`_return_gps_valid()` 对旧的单一 accuracy/age 配置按轴和 `1.5 s` freshness 上限做 fail-closed
钳制。对应 parity 为 `core_algorithm_hash=6c2d186e8b6c31330b16fae096c4d90302797bdf5d697e28ff4efbc55fbdc06d`，
软件验收快照为 `artifacts/evaluation/multidetect-goal-acceptance-20260902-ai-gps-accuracy.json`；
现场链路、外部 PX4/Gazebo 视觉、HIL/REAL 和人工 Fly View 仍保持 `not_claimed`。

三机型 Docker external runtime/live-AI 的六份 source 现在由
`src/multidetect/simulator_external_evidence.py` 做一次 join、再做一次 `--join` 重算；
`scripts/run_goal_acceptance.ps1` 通过 `-SimulatorExternalEvidence` 把这条重算链绑定进总验收，
并在 `simulator_external_evidence` 节点保存 join/revalidation 的路径、SHA-256、版本和门状态。
当前总验收绑定快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260902-external-three-airframes-v7-independent-source-credentials-strict.json`，
external binding 的 `verified=true`、`source_recomputed=true`、`gate_passed=true`；goal
acceptance 使用的 revalidation 是
`artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260902-final-six-sources-independent-v2-goal-v7.json`；这仍是
software/SIM provenance，不能替代真实 Pixhawk/Jetson/相机、室外 GNSS、HIL、REAL 或人工 QGC
验收。

该快照还绑定了 22:00 后的现场只读预检
`artifacts/evaluation/field-link-preflight-20260902-after-2200.json`（6,692 bytes，SHA-256
`37D68E88AB7433898BE77FED696009564F558274B7824EB0D3164D5FC563200A`）。本地 metadata/QGC
UDP 端口可用，但三个机载端点和 `192.168.144.0/24` route 均未就绪，故
`hardware_link_ready=false`；这份失败事实不改变 SIM gate，也不宣称 REAL。

2026-09-02 又把当前台式机的现场入口按严格 route/protocol 合同复核了一次：
`artifacts/evaluation/field-link-preflight-20260902-continue-v4.json`（4,396 bytes，SHA-256
`0438D61B311BBA94150CF59F580DBC4A29E6B31999310E33238A1E52F1A3506C`）记录了本机
`192.168.1.13` 被选为三个端点的 source，而期望飞机网段是 `192.168.144.0/24`；SSH/MAVLink/RTSP
均超时，本地 metadata/QGC UDP 端口可用，`hardware_link_ready=false`。同一报告已绑定
`artifacts/evaluation/multidetect-goal-acceptance-20260902-field-preflight-v4.json`
（31,777 bytes，SHA-256
`250824C312DD0CD8326721C0B27B3B10F05F53E9644FA82CC00EC87F668905F7`），软件回归保持通过，
硬件仍 `offline_pending`。因此这次只更新了现场 provenance；Jetson/GNSS/Pixhawk、室外精度、
HIL/REAL 和人工 Fly View 仍需在接通飞机后单独验收。

## 1. 边界先定死

模拟环境和现实环境是两个运行环境，不是同一个环境的开关：

```text
SIM  = PX4 SITL + Gazebo + 虚拟传感器 + 模拟时钟 + 模拟世界
HIL  = 真实 Pixhawk + 仿真动力学/传感器 + 受信 HIL profile
REAL = 真实飞机 + Jetson + 相机/GNSS + 真实飞控
```

三者只共享以下内容：

- `Detector -> Tracker -> Ranging -> Geolocation -> Mission/Safety` 核心算法；
- `LiveMissionRunner`、任务契约、签名 metadata、审计格式和 PX4 Mission transaction；
- `MultiDetectGCS` 原生 `FlyView`/`PlanView`、QGC toolbar、地图、PiP、视频和控件风格。

三者不共享运行时设备：SIM 不能打开真实串口、真实相机、真实 GNSS、真实载荷控制器，
也不能携带 `FLIGHT_CONTROL_OUTPUT` capability。SIM 的控制输出只能送到 PX4 SITL。

## 2. 推荐的模拟器组成

### 2.1 进程拓扑

```text
multidetect-sim (orchestrator)
  ├─ PX4 SITL                # 飞控状态机、MAVLink、Mission、failsafe
  ├─ Gazebo                  # 3D 世界、刚体动力学、风场、碰撞
  ├─ sensor bridge            # RGB/Depth/NavSat/IMU/Baro/Mag/Odom
  ├─ perception runtime       # shared production perception factory
  ├─ scenario controller      # actor、故障、时间轴、随机种子
  ├─ QGC RTSP + signed metadata bridge
  └─ MultiDetectGCS           # 唯一操作 UI
```

Gazebo GUI 可以显示世界和模型，但它不是第二套 Multi-Detect 操作界面；任务操作、目标框、
地图落图、证据卡片和状态抽屉全部在 QGC 原生 Fly View 内完成。

### 2.2 端口和设备隔离

每次运行生成独立 job 目录和端口组，禁止复用 REAL/HIL 端点：

| 通道 | SIM 默认 | 规则 |
|---|---:|---|
| PX4 SITL command output | `14540/udp` | adapter 唯一输出，literal loopback |
| PX4 telemetry → SIM router | `14541/udp` | router 输入，literal loopback |
| QGC native isolated-HIL | `14649/udp` | QGC 自己绑定的本地收发端口 |
| QGC → SIM router | `14650/udp` | router 输入；`read_only` 只接收 MANUAL_CONTROL/只读发现，`sim_flight` 另接受有界 ARM/模式/Mission |
| QGC metadata | `14640/udp` | 必须带 SIM runtime identity 和 scenario hash |
| RTSP | `8554/tcp` | 只服务当前 job 的虚拟相机 |
| 真实串口/UDP | 禁止 | SIM 启动前做 deny 检查 |

编排器在启动前验证 `RuntimeProfile.sim()`、`RuntimeMode.SIM`、scenario hash、端口空闲和
`flight_control_output` 缺失；任一条件不满足直接失败，不自动回退 REAL。

## 3. 场景模型

现有 `configs/scenarios/river_rescue_vtol_001.*` 作为 VTOL 可复现实例，
`configs/scenarios/coastal_patrol_fixed_wing_001.*` 作为固定翼实例。后者绑定仓库内
`simulation/gazebo/worlds/coastal_patrol_fixed_wing_001.sdf` 与
`simulation/gazebo/models/rc_cessna_mono_cam/model.sdf`（`rc_cessna` + `mono_cam`），含
WGS-84 原点、跑道/停机坪/进近信标、受控横风和救生浮标目标。每个场景拆成四份：

```text
scenario.json       # 身份、版本、通道 hash
runtime.json        # 世界、飞行器、传感器、故障、seed
truth.json          # evaluator-only 真值，不进入生产核心
expectations.json   # 终态、围栏、假定位、未授权命令上限
```

后续场景建议按同一 schema 增加：

1. `river_rescue_vtol_001`：河道搜救、漂移目标、风和相机掉线；
2. `coastal_patrol_fixed_wing_001`：固定翼海岸巡逻、横风、空速冻结、GNSS 漂移与相机掉线；
3. `urban_corridor_fixedwing_001`：固定翼航线、建筑遮挡、GNSS 多路径；
4. `coastal_vtol_transition_001`：垂起转固定翼、海风、目标落图；
5. `mountain_gnss_denied_001`：峡谷遮挡、定位降级、失锁恢复。

场景控制器只产生传感器输入和故障事件。`truth` 只供 evaluator 对比，不得作为检测结果、
目标坐标或安全判定的输入。

### 3.1 fixed-wing Mission compiler（pre.8 已实现）

`src/multidetect/px4_mission.py` 的
`compile_fixed_wing_executable_mission(...)` 把 runtime-channel 几何、平台无关计划、
`ContextSnapshot` 和 `Px4FixedWingSafetyPolicy` 编译为 hash-bound 的
`CompiledPx4Mission`。输入模型为 `FixedWingMissionGeometry`：`home`、waypoints、显式的
`landing_approach`、零高度 `landing`、起飞高度/接受半径和可选起飞/着陆航向；
`from_runtime_mapping()` 只读取 runtime 字段并拒绝 `truth`/evaluator 字段。

编译前的 fail-closed 检查包括：

- 计划恰好一次 `TAKEOFF`、一次末尾 `LAND`，且在着陆前包含 `RETURN` 或 `DIVERT`；
- airframe allow-list、geofence/home 一致性、坐标/高度/任务半径和最多 64 个 mission item；
- 固定翼转弯半径、最小航段、最大转角，以及显式 landing approach 的距离和 glide slope；
- 起飞/着陆航向对齐，着陆段 tailwind/crosswind 限制；
- GPS fix/水平精度、电池最低保留和按风场修正的航程能量 + reserve time。

默认 `Px4FixedWingSafetyPolicy` 的关键包络为：相对高度 `20..180 m`、任务半径 `5,000 m`、
最小转弯半径 `45 m`、最小航段 `25 m`、进近 `120..1,000 m`、最大下滑角 `12°`、
着陆航向误差 `20°`、巡航/最低空速 `17/12 m/s`、尾风/侧风 `8/10 m/s`、GPS 精度 `25 m`、
电池保留 `30%`、reserve `120 s`；策略可收紧但不能绕过。输出顺序为
`TAKEOFF → waypoints → landing_approach → LAND`，每项均为 canonical
`MAVLink2/MISSION_ITEM_INT`，并绑定 `PX4_FIXED_WING_ADAPTER_ID/HASH`、计划 hash、
上下文 hash 和 wire hash。

先跑 compiler 的 12 项回归（不触碰 PX4 端口）：

```powershell
cd D:\Projects\Multi-Detect
$python = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
$env:PYTHONPATH = 'src'
& $python -m pytest -q tests\test_px4_fixed_wing_mission.py
```

该测试只证明 deterministic compile、包络拒绝、风/能量/转弯/进近约束和 hash 绑定；
它不等于 PX4 upload/readback、Gazebo 运行、QGC 视觉或真实飞行验收。

## 4. 传感器和物理真实性分层

### L0：确定性闭环（先交付）

- `ManualClock` 驱动，固定 seed、固定帧数、固定世界初始状态；
- Gazebo 相机帧经过真实 wire/header/hash 校验；
- PX4 NavSat/姿态/高度通过统一 telemetry provider；
- 同一 `production perception factory` 进入 `LiveMissionRunner`；
- 可重复得到相同目标框、距离、GPS 质量和终态。

### L1：传感器误差模型

每个传感器配置独立的延迟、噪声、丢包和时间戳策略：

```text
RGB:      exposure, blur, rolling-shutter delay, dropout
Depth:    scale noise, invalid pixels, min/max range
GNSS:     fix type, HDOP/VDOP, bias, multipath, outage
IMU:      bias random walk, white noise, saturation
Baro:     pressure bias, temperature drift
Mag:      declination, hard/soft iron, disturbance
Odom/VIO: timestamp skew and covariance
```

测距和定位算法只能使用通过质量门的观测；GPS 不合格时保持 `不可定位`，不使用 truth 回填。

### L2：动力学和环境

- PX4 参数固定在场景 manifest 的 airframe profile；
- Gazebo 风、阵风、地面碰撞、河面/建筑/地形来自仓库内资产；
- 多旋翼、固定翼、VTOL 和垂起转场采用各自 PX4 SITL 模型，但上层 Mission 和目标链路不分叉；
- 记录仿真时间、墙钟时间、RTF、CPU/GPU、丢帧和端到端延迟。

### 确定性 flight model fallback（pre.8 已实现）

在 PX4/Gazebo 不可用时，`src/multidetect/simulator/flight_model.py` 提供无外部 I/O 的
确定性、bounded kinematic fallback；它用于协议/控制回归，不替代 Gazebo 刚体动力学，也不
把真值暴露给感知或 AI。`DeterministicFlightModel` 支持 `multirotor`、`vtol` 和 `fixed-wing`，输入为
`FlightControlSetpoint`（roll/pitch/yaw `[-1,1]`、throttle `[0,1]`，可带单调时间戳），输出
`FlightTelemetrySnapshot`（位置、姿态、空速、风和 GPS 质量，`truth_free=true`）。默认边界为
`45 m/s` 空速、`2,000 m` 高度、roll `±55°`、pitch `±30°`、yaw rate `90°/s`、垂向速度
`12 m/s` 和单步 `dt <= 10 s`；seed、east/north 风场和命令序列相同则 evidence hash 相同。

`src/multidetect/simulator/closed_loop.py` 把该模型接到生产控制栈：

```text
telemetry/observation -> AI policy (可选) -> authority
-> SimulatorMavlinkControlAdapter -> MANUAL_CONTROL
-> in-process MAV sink -> flight model -> telemetry snapshot
```

闭环与 manifest runner 共用一个注入的 `ManualClock`：AI executor 的 freshness、控制
receipt、watchdog 截止点、flight-model telemetry 和最终 evidence 均读取同一 monotonic
时间轴；physics step 成功后才推进时钟，并对时钟/模型漂移 fail-closed。manifest runner
额外生成 `RuntimeDescriptor(mode=SIM, profile=sim())`，绑定 scenario id、manifest hash、
seed 和 airframe profile hash，并把 descriptor/hash 写进运行结果。这样 deterministic SIM
的时间与身份边界和现有 Replay/HIL/REAL runtime contract 对齐，后续替换为 PX4 SITL/HIL
transport 时不需要复制一套 AI 或安全时钟逻辑。

人工输入立即夺回 authority；AI envelope、watchdog、故障和关闭路径统一发送 neutral 并进入
failsafe。`qgc_legacy` 油门 neutral 为 `z=500`，`signed` 为 `z=0`。顶层 evidence 事件为
`simulator_closed_loop_evidence`，`flight_model` 子对象含 canonical `sha256`，不含 scenario
truth。该闭环路径与 interactive router 使用相同 authority/adapter 契约，区别仅在 transport
sink（进程内记录器或 PX4 SITL loopback）。

控制契约内部始终使用归一化 `throttle ∈ [0,1]`；编排器 CLI 的
`--throttle-wire-mode {qgc_legacy,signed}` 将同一选择传给 adapter、interactive router 解码、
AI executor 和 closed-loop MAV sink。`qgc_legacy` 编码 `MANUAL_CONTROL.z` 为 `[0,1000]`，
`signed` 编码为 `[-1000,1000]`；因此 neutral/failsafe 分别是 `500` 与 `0`。选择会写入
`job.json`、`control.json`、`result.json` 及 `interactive-router.json.adapter`，回归比较时必须
确认各处一致，不能只看归一化 setpoint。

独立运行 `python -m multidetect.simulator.interactive_router` 时，CLI 允许
`--throttle-wire-mode auto|qgc_legacy|signed`，默认 `auto`；`auto` 对非负 `z` 保持 QGC legacy、
对负值兼容 signed 输入，但 adapter 输出仍采用 `qgc_legacy`。编排器 `run` CLI 只暴露
`qgc_legacy|signed`，默认显式选 `qgc_legacy`，保证 job 内输入解码、AI 输出和 model sink 同一
wire 语义。

standalone CLI 不传 `--duration-seconds` 时持续监听；做不启动外部进程的配置 probe 时使用
隔离的高位 loopback 端口：

```powershell
cd D:\Projects\Multi-Detect
$python = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
$env:PYTHONPATH = 'src'
& $python -m multidetect.simulator.interactive_router `
  --throttle-wire-mode auto `
  --qgc-input-port 24650 --qgc-output-port 24649 --px4-input-port 24641 `
  --px4-output-endpoint udpout:127.0.0.1:24640 `
  --duration-seconds 0.5
```

命令只输出 router/adapter JSON，不启动 PX4、QGC 或真实 UDP 对端；接入 job 时改用编排器并
为该 job 分配独占端口。

确定性闭环命令：

```powershell
cd D:\Projects\Multi-Detect
$python = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
$env:PYTHONPATH = 'src'

& $python -m multidetect.simulator closed-loop `
  --mode vtol --seed 42017 --steps 60 --dt 0.1 `
  --wind-east 3 --wind-north -1 `
  --out artifacts/evaluation/simulator-closed-loop-vtol.json

& $python -m multidetect.simulator closed-loop `
  --mode fixed-wing --seed 42017 --steps 60 --dt 0.1 `
  --wind-east 3 --wind-north -1 `
  --out artifacts/evaluation/simulator-closed-loop-fixed-wing.json

& $python -m multidetect.simulator closed-loop `
  --mode multirotor --seed 42017 --steps 60 --dt 0.1 `
  --wind-east 3 --wind-north -1 `
  --out artifacts/evaluation/simulator-closed-loop-multirotor.json

& $python scripts/run_simulator_closed_loop_acceptance.py `
  --out artifacts/evaluation/simulator-closed-loop-acceptance.json
```

验收 JSON 应有 `gate_passed=true`，同时覆盖 VTOL/fixed-wing 的历史双模式、AI hover/track、人工接管、AI
恢复、watchdog/failsafe 和 `truth_free`。这些是 software/protocol machine checks；不得将其
写成人工 QGC 视觉、Gazebo 渲染、真实 Pixhawk/Jetson/GNSS、相机或飞行通过。

### manifest scenario runner 与错误终态（pre.8）

`python -m multidetect.simulator scenario` 是按 runtime channel 时间轴逐帧执行的确定性入口，
不启动外部 PX4/Gazebo/QGC。示例：

仓库提供的 `scripts/run_simulator_*.py` 验收包装器也支持干净 checkout 的直接调用：脚本
执行时只将本仓库 `src/` 放入解释器路径，不依赖开发者机器上的 `PYTHONPATH` 或 editable
安装。

```powershell
cd D:\Projects\Multi-Detect
$python = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
$env:PYTHONPATH = 'src'
& $python -m multidetect.simulator scenario `
  --scenario configs/scenarios/coastal_patrol_fixed_wing_001.scenario.json `
  --frames 600 --dt 0.1 --mode fixed-wing `
  --out artifacts/evaluation/simulator-scenario-fixed-wing.json
```

`frame_budget_complete` 为正常终态并返回 `0`。provider、AI 或模型 step 异常时，runner
仍执行 close/failsafe 并序列化关闭后的 evidence，将 `terminal_reason` 写成
`error:<ExceptionType>:<message>`，CLI 返回 `2`；CI 必须同时检查退出码和 evidence，不能把
“已生成 JSON”当作成功。`truth_loaded=false` 是该路径的硬断言。

### 三机型 deterministic scenario matrix（pre.8）

`python -m multidetect.simulator scenario-matrix` 是单场景 runner 之上的离线矩阵入口。
默认按稳定文件名发现 `configs/scenarios/*.scenario.json`，因此一次覆盖当前的
`fixed-wing`、`vtol` 和 `multirotor`；也可以重复 `--scenario` 只选择一个或多个清单。
每个清单都用同一组手动时钟、AI/authority、MAVLink loopback 和 truth-free 规则执行，
并默认重复一次运行比较 evidence hash。AI 尝试/接受/拒绝/failsafe 以及控制消息计数来自
完整 runner evidence，而不是仅保留给人看的末尾步骤窗口。

```powershell
cd D:\Projects\Multi-Detect
$python = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
$env:PYTHONPATH = 'src'
& $python -m multidetect.simulator scenario-matrix `
  --frames 120 --dt 0.1 `
  --out artifacts/evaluation/simulator-scenario-matrix.json
```

矩阵通过的必要条件是每个选中场景正常完成、关闭、truth-free、SIM runtime descriptor/
world-origin/clock 自洽，并且重复 hash 一致。`claims.shared_ai_authority_watchdog_path`
只有在启用 AI 且**每个**选中场景至少接受一条 AI 控制时才为 `true`；`--no-arm` 或 `--no-ai`
仍可用于验证确定性和 fail-closed 行为，但不会虚报 AI 控制路径。矩阵证据的
`external_3d_runtime`、`qgc_visual_acceptance`、`hardware_acceptance` 和
`real_flight_acceptance` 永远保持 `not_claimed`，所以这条入口不启动 PX4/Gazebo/QGC，
也不替代外部 3-D、人工 Fly View、HIL 或 REAL 现场门。

当前 pre.8 三机型证据为
`artifacts/evaluation/simulator-scenario-matrix-20260901-pre.8.json`（11,494 bytes，
SHA-256 `95847C56FB0AAB7FA4CE459B4C43CA15B73D46B8F4F6834455A7CD61E73323E9`），
`gate_passed=true`、`deterministic_gate_passed=true`，三个场景各完成 4 帧并各接受 4
条 AI 控制。该文件只关闭离线 software/SIM matrix 子门，不能被写成真实飞行或最终版本
发布证明。

### native QGC interactive acceptance fixture（pre.8）

`src/multidetect/simulator/interactive_acceptance.py`、
`scripts/run_simulator_interactive_acceptance.py` 与
`python -m multidetect.simulator interactive-acceptance` 提供不启动外部进程的 native QGC
MANUAL_CONTROL/telemetry machine gate。fixture 复用
`SimulatorInteractiveRouter`、`SimulatorControlAuthority`、`SimulatorMavlinkControlAdapter`
和 `SimulatorClosedLoop`，仅把 socket/PX4 替换为进程内 deterministic flight-model
connection；默认 `--mode both` 保留 `vtol` 与 `fixed-wing` 双模式，`--mode all` 再加入
plain PX4 `multirotor`，也可用 `--mode multirotor` 单跑。

每个 mode 验证 AI observed-telemetry TRACK、QGC `MANUAL_CONTROL (69)` 接管、manual 时 AI
抑制与释放后恢复、watchdog legacy neutral/`FAILSAFE`、PX4 heartbeat 原字节转发、只读
`PARAM_REQUEST_LIST` 转发、`MISSION_COUNT` 等写入阻断，以及 loopback/QGC/PX4 identity
拒绝计数。输出包含 `gate_passed`、`modes[*].gate`、`router`、`transport`、`closed_loop`
和 claims；`truth_free_closed_loop=true`，`visual_acceptance`、`px4_gazebo_acceptance`、
`pixhawk_jetson_camera_acceptance`、`real_flight_acceptance` 均保持 `not_claimed`。

```powershell
cd D:\Projects\Multi-Detect
$python = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
$env:PYTHONPATH = 'src'

& $python -m multidetect.simulator interactive-acceptance `
  --mode both `
  --out artifacts/evaluation/simulator-interactive-acceptance.json

# 三种机型共用同一 authority/adapter/AI/watchdog 路径
& $python -m multidetect.simulator interactive-acceptance `
  --mode all `
  --out artifacts/evaluation/simulator-interactive-acceptance-all.json

# 等价脚本入口；通过=0，gate 失败=1，初始化/运行异常=2
& $python scripts/run_simulator_interactive_acceptance.py `
  --mode fixed-wing `
  --out artifacts/evaluation/simulator-interactive-acceptance-fixed-wing.json
```

该 machine gate 通过不提升 3-D `visual_acceptance`、Gazebo/PX4 SITL、桌面 QGC、Pixhawk/
Jetson、相机/GNSS 或真实飞行状态；`run_simulator_3d_control_acceptance.py` 会把结果嵌入
`interactive_qgc_path`，并在顶层 `claims.native_qgc_interactive_path` 记录子门结果。

同日按当前手动 watchdog 代码重跑的 VTOL 3-D 快照为
`artifacts/evaluation/simulator-3d-control-acceptance-20260902-manual-watchdog-v1.json`
（83,149 bytes，SHA-256
`0C6E2A418FB808EE6B8E33BD6CFD940826678543FD4C7A6F20974CBE0D2EC81E`）。其
`gate_passed=true`、`deterministic_gate_passed=true`，嵌入的 native QGC interactive 子门
同样通过并记录 `manual_watchdog_s=0.5`；外部 PX4/Gazebo、桌面视觉和硬件门仍保持
`not_claimed`。

2026-08-30 的双模式重跑证据为
`artifacts/evaluation/simulator-interactive-acceptance-20260830-rerun.json`（SHA-256
`33FF690CE41B1E77E3B8D2EE035E36255AD9370F7BD35B248647A75DFFE52B03`），顶层
`gate_passed=true`，`modes` 包含 `vtol` 与 `fixed-wing`。两种 mode 都通过 AI TRACK、QGC
手动接管/释放、watchdog neutral/failsafe、只读遥测原字节转发、身份/loopback 拒绝计数和
blocked-write；外部 PX4/Gazebo、桌面视觉和硬件字段仍按上面的边界保持 `not_claimed`。

随后在共享时钟收口后重新生成了确定性证据
`artifacts/evaluation/simulator-interactive-acceptance-20260830-clock-descriptor.json`
（SHA-256 `6C237C5B1759706B7E563398D0E839A0E90FA7DF6FFEB96C0F3CF9E33AE0DDF5`，
68,581 bytes）。该文件仍是同一 machine gate（`gate_passed=true`，`vtol` 与 `fixed-wing`
均通过），但额外绑定 `RuntimeDescriptor`、`runtime_descriptor_hash` 和每个 router 的
deterministic `started_monotonic_s`/`ended_monotonic_s`/`duration_seconds`；两种 mode 的
router duration 均为 `0.3 s`。因此交互验收、AI、watchdog、flight model 和 evidence
现在共享同一条 `ManualClock` 时间轴，重复运行的序列化结果可直接比较。该证据仍不提升
3-D 渲染、PX4/Gazebo、真实硬件、室外 GNSS、HIL/REAL 或人工 Fly View 视觉验收状态。

2026-09-02 又按当前 `v0.3.0-pre.8` 重新生成了三机型快照
`artifacts/evaluation/simulator-interactive-acceptance-20260902-manual-watchdog-v1.json`
（113,245 bytes，SHA-256
`BE9E290B1D04BF191B2DB8B372739B8F62E4FFD0F4A46EF94F790EA14275DC2F`）。三种机型的
`router` 与共享 adapter 都明确记录 `manual_watchdog_s=0.5`，快照 `gate_passed=true`；
其 `manual_watchdog_neutral_count=0` 是因为主流程在 watchdog 前显式释放 manual，真正的
过期 MANUAL_CONTROL→单次 neutral/FAILSAFE 行为由 adapter/gateway 回归测试覆盖。该快照
同样不提升桌面视觉、外部 PX4/Gazebo 或真实硬件门。

### 当前 3-D 控制资产与三机型交互门（2026-09-02，pre.8）

`scripts/run_simulator_3d_control_acceptance.py` 将 `river_rescue_vtol_001` 的场景
manifest、Gazebo world/model、WGS-84 原点和共享控制契约装配成 deterministic 3-D
协议/资产门。当前证据
`artifacts/evaluation/simulator-3d-control-20260902-pre.8-continue-v1.json`
（SHA-256 `56099CE79D65DF7D240BFC633E3479EA848053E4665F62AA0B6157F2509A82AC`）的
`gate_passed` 与 `deterministic_gate_passed` 均为 `true`。证据逐项记录 QGC manual 输入、
AI TRACK、manual takeover 时 AI 抑制、释放后的 AI resume、watchdog neutral/failsafe，
并要求 QGC 是唯一操控 UI、没有第二个 simulator window、传输只走 loopback。

`scripts/run_simulator_interactive_acceptance.py --mode all` 在同一 authority/adapter/
flight-model 路径上覆盖 `vtol`、`fixed-wing`、`multirotor`，当前证据
`artifacts/evaluation/simulator-interactive-20260902-pre.8-continue-v1.json`
（SHA-256 `24B57DAA77551CBE9970D95A7A93C5AC6209269F044285F5DD7B551467B313BE`）三种
mode 全部通过 manual-priority、AI resume 和 watchdog gate。该两条入口没有启动外部
PX4/Gazebo 进程，因此 `external_runtime_gate_passed`、外部 3-D 视觉、Pixhawk/Jetson/
相机、室外 GNSS、HIL/REAL 和实飞字段仍是 `not_claimed`；外部运行时与人工 Fly View
必须另行执行现场/桌面验收。

2026-09-02 随后按同一源码和同一 `pre.8` manifest 刷新了 `-continue-v2` provenance：
`artifacts/evaluation/simulator-interactive-20260902-pre.8-continue-v2.json`
（111,844 bytes，SHA-256
`24B57DAA77551CBE9970D95A7A93C5AC6209269F044285F5DD7B551467B313BE`）顶层
`gate_passed=true`，三种 mode 均通过；VTOL、fixed-wing、multirotor 的 3-D 控制证据
分别为：

* `artifacts/evaluation/simulator-3d-control-20260902-pre.8-continue-v2.json`
  （82,435 bytes，SHA-256
  `1D4F90E0C631BCB42A7F9DF5BD6F9C4B7468CD37AAA891BF96AF8CF11DD64177`）；
* `artifacts/evaluation/simulator-3d-control-20260902-fixed-wing-continue-v2.json`
  （83,126 bytes，SHA-256
  `FB879AE4F627474B24D3397739A6E3ECC60F92891CCF671CC846414D02697982`）；
* `artifacts/evaluation/simulator-3d-control-20260902-multirotor-continue-v2.json`
  （83,089 bytes，SHA-256
  `84419C67A853D6467A900CC76154EE1699052123B456C0C1D0BC66B006ABFCFE`）。

三份 3-D 证据均为 `gate_passed=true`、`deterministic_gate_passed=true`，并明确
`external_runtime_gate_passed=false`；外部 PX4/Gazebo、3-D 视觉、Pixhawk/Jetson/相机、
室外 GNSS、HIL/REAL 与实飞继续保持 `not_claimed`。v2 只刷新可重复的软件资产/控制子门，
不替代人工 QGC Fly View 或现场硬件验收。

#### 三机型 source binding 与独立复核（当前 pre.8）

当前工作树又按 `v0.3.0-pre.8` 重跑了三份 source：VTOL 83,212 bytes、fixed-wing
83,903 bytes、multirotor 83,866 bytes。`src/multidetect/simulator/three_d_control.py`
提供第二边界 `verify_file()` / `build_binding()`：它不执行控制，只重新读取 source，绑定
场景 manifest、world/model SHA-256、stack-plan canonical digest、PX4 launch preflight、
manual/AI/watchdog/QGC-only contract，并强制 `external_runtime_gate_passed=false`、
视觉与硬件 `not_claimed`。三份文件必须恰好覆盖
`fixed-wing`、`multirotor`、`vtol` 各一次。

绑定结果落在
`artifacts/evaluation/release-gate-pre.8-current-20260902-3d-bound.json`，大小 13,373
bytes，SHA-256 为
`967F8C5CFE4DBF16EAFF88503BBF18C497BAEA4ECE8190DF2D5602F1D7B8D206`。`release_gate.py`
每次评估都会重新哈希三份 source 并调用独立 verifier；最终 `v0.3.0` 频道还要求绑定
存在且通过。预发布聚合仍保持 `gate_passed=false`，因为现场链路、人工 Fly View、HIL/REAL
和真实硬件尚未闭环。

复核/绑定命令：

```powershell
Set-Location D:\Projects\Multi-Detect
$env:PYTHONPATH = 'src'
python scripts\verify_simulator_3d_control_evidence.py --bind `
  --expected-release-label 0.3.0-pre.8 `
  --manifest-directory artifacts\evaluation `
  artifacts\evaluation\simulator-3d-control-acceptance-20260902-current-pre8.json `
  artifacts\evaluation\simulator-3d-control-acceptance-20260902-current-fixed-wing-pre8.json `
  artifacts\evaluation\simulator-3d-control-acceptance-20260902-current-multirotor-pre8.json
```

`scripts/run_goal_acceptance.ps1` 默认会重新生成并绑定这三份 source；输出可通过
`-Simulator3dVtolOutput`、`-Simulator3dFixedWingOutput`、`-Simulator3dMultirotorOutput`
覆盖，`-RequireSimulator3dControlEvidence` 用于把它设置成硬前置。

完整 goal-acceptance 已实际跑过这条默认链：
`artifacts/evaluation/multidetect-goal-acceptance-20260902-3d-binding.json`（41,392 bytes，
SHA-256 `57F9B416A8E4FF4EC20D395F905A87DAA4290304B930EF1DA448CFBAEE8197E9`），收集 2,456
项核心测试、QGC custom 66 项，三机型 binding `verified=true`/`gate_passed=true`；最新 E7
binding 为
`artifacts/evaluation/release-gate-pre.8-current-20260902-3d-bound-rerun.json`（13,328 bytes，
SHA-256 `BA87F55307793069A04BC941E3F62586AE64476EE5BD86A27C1B52AF8CA97187`）。现场链路、
人工视觉、HIL/REAL 和硬件字段仍按 fail-closed 语义保持 pending/not_claimed。

本轮续跑还绑定了最新台式机现场只读报告
`artifacts/evaluation/field-link-preflight-20260902-final-continuation.json`（3,713 bytes，
SHA-256 `476AB14F0E48887A33AA4107D86A6782F13B8A7B4334B95954681F99A3A48E52`）。报告显示
Ethernet `192.168.1.13/24` 未进入期望 `192.168.144.0/24`，Jetson/GR01/RTSP 均不可达，
所以 `hardware_link_ready=false`；这条记录只作为现场事实和后续接线基线。

与它绑定的总验收
`artifacts/evaluation/multidetect-goal-acceptance-20260902-final-continuation.json`
（41,681 bytes，SHA-256 `6F6388CA393F49FEADC6B95693A0CB7AC93959A2AA9E17236B8BC7DC7DF3AB9C`）
收集核心测试 2,456、QGC custom 66/66，三机型 3-D binding 强制复核通过。最终 source
文件分别为 VTOL 83,240 bytes / `50B8E18B1A7B7D85CF4D37AF48BD5DB1B51534C57F709B864E2FB8827A1BEF5F`、
fixed-wing 83,931 bytes / `715A3437101846271A901CC90140DAC9969D459361D7FEE6EF438F75DE945208`、
multirotor 83,894 bytes / `98B1971CC17F9EF3190EB83759329C1BFDDE1EF64F2E2383B9ED4DA036641E19`。
三份 source 的独立绑定报告为
`artifacts/evaluation/simulator-3d-control-binding-final-continuation.json`（2,278 bytes，
SHA-256 `262C659B1860FAA2098AC48B211588A2CE523604A4C45A465E7F530C6F3F0C96`），其
`all_airframes`、`records_hash_bound`、`all_gate_passed` 和
`external_runtime_not_claimed` 均为 `true`。
这组 deterministic source 仍不声称外部 PX4/Gazebo 渲染、人工 QGC 视觉、室外 GNSS、
真实硬件或 HIL/REAL 已完成。

#### 台式机 Docker 直接重跑（历史 parity 快照，2026-09-02，pre.8）

当前台式机 Docker Desktop server `28.4.0` 可从
`C:\\Program Files\\Docker\\Docker\\resources\\bin\\docker.exe` 调用。本轮对三个
repository-owned Gazebo 场景各启动一次 pinned PX4 external runtime，并在同一 telemetry
流上执行 `--exercise-arm --exercise-target` live-AI。VTOL、fixed-wing、multirotor 均收到
`GPS_RAW_INT fix_type=3/satellites=10` 与 armed heartbeat，AI 各完成 2 个 policy step、
truth-free `TRACK`，容器 stop/rm 清理无误。六份 source 的完整路径/哈希由
`artifacts/evaluation/simulator-external-runtime-ai-evidence-20260902-pre.8-continue-v1.json`
保存（SHA-256 `1A3D7F99BC0E6C21E68BDFA57B9DFA77856C030E3DC3DE406CB4ED3A78FD63B5`，
`gate_passed=true`），并绑定当前 parity SHA-256
`6D0CDBD3D376FBE1AF46A51C721AF3785528CA6BF22016DCD4216979769D7FDF`（该 evidence 生成时的历史
parity；当前清单已加入 manual-control safety source）。该结果只表示
headless external-SIM/runtime 与 AI policy 子门通过；QGC 人工画面、真实硬件、室外 GNSS、
HIL/REAL 和实飞仍需独立证据。

### Docker external runtime 与 live-AI 三机型闭环（历史 parity 快照，2026-09-02，pre.8）

确定性 3-D 资产门之后，台式机曾使用固定 digest 的 PX4/Gazebo Docker runtime 对
`river_rescue_vtol_001`、`coastal_patrol_fixed_wing_001` 和
`urban_inspection_multirotor_001` 各执行一次 runtime 与一次 live-AI target exercise。
每组 runtime 均有 `GPS_RAW_INT fix_type=3`、10 颗卫星、HEARTBEAT、loopback UDP I/O 和
容器清理；每组 AI 均在新鲜 armed heartbeat 后完成 ARM、2 个 policy step、目标
`TRACK` 与 DISARM。六份 source 的路径/哈希和当时 digest 由
`scripts/verify_simulator_external_evidence.py` 重新计算并绑定到
`artifacts/evaluation/simulator-external-runtime-ai-evidence-20260902-pre.8-v2.json`
（SHA-256 `656531912FC0FCF8AF975469CBB0C340136CF4D8515BECE5BFC1827858BDDC35`，
`gate_passed=true`）。该 join 是 machine/runtime 子门，输入仍为 truth-free synthetic
perception，`hardware_control_disabled=true`；视觉精度、QGC 人工画面、真实设备、
室外 GNSS、HIL/REAL 和实飞不在声明范围内。

为支持并行 job 的自定义 telemetry 端口，`stack.py` 保留 PX4 manifest 的 canonical
`14541` 语义，同时在选定 loopback 端口与 canonical sensor 之间写入
`sensor_port_bindings`。只有明确的 `mavlink_telemetry` canonical remap 被接受，其他
MAVLink/传感器 URI 仍按原端口隔离规则 fail closed；`tests/test_simulator_stack.py` 覆盖
重映射与无关端口拒绝。这保证三机型外部运行使用隔离端口而不降低来源校验。

复核 join 的入口：

```powershell
Set-Location D:\\Projects\\Multi-Detect
$python = '.\\ground-control\\MultiDetectGCS\\.venv\\Scripts\\python.exe'
& $python scripts\\verify_simulator_external_evidence.py `
  --vtol-runtime artifacts\\evaluation\\simulator-docker-acceptance-20260902-pre.8-desktop-v2.json `
  --vtol-ai artifacts\\evaluation\\simulator-docker-ai-acceptance-20260902-pre.8-desktop-v1.json `
  --fixed-wing-runtime artifacts\\evaluation\\simulator-docker-acceptance-20260902-pre.8-fixed-wing-v1.json `
  --fixed-wing-ai artifacts\\evaluation\\simulator-docker-ai-acceptance-20260902-pre.8-fixed-wing-v1.json `
  --multirotor-runtime artifacts\\evaluation\\simulator-docker-acceptance-20260902-pre.8-multirotor-v1.json `
  --multirotor-ai artifacts\\evaluation\\simulator-docker-ai-acceptance-20260902-pre.8-multirotor-v1.json `
  --parity-json configs\\release-parity.json `
  --out artifacts\\evaluation\\simulator-external-runtime-ai-evidence-20260902-pre.8-v2.json
```

external join 不是一次性可信摘要：`verify_simulator_external_evidence.py --join <join.json>`
会从 join 取回三机型六个 source 与 parity 文件，重新调用同一个 build/validation 路径，
对比 source SHA-256、字节数、manifest、镜像、版本、claims 和 gate。当时二次验证结果为
`artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260902-pre.8-v1.json`
（SHA-256 `8366977C0F3800D09C348DCA629DA43FD8DFC9815DD9009D03F0FB2E3CAC8C18`，
`source_recomputed=true`、`gate_passed=true`）；schema 是
`configs/schemas/simulator-external-evidence-revalidation.schema.json`。

## 5. 同一 UI、同一逻辑的实现规则

### QGC 侧

- 继续覆盖原生 `FlyViewVideo.qml` 和 `FlyViewCustomLayer.qml`，不创建 `Window`、
  `ApplicationWindow`、第二个 `VideoOutput` 或 simulator-only QML；
- 目标框、LCK/TRK、地图落图、1σ 圆、证据卡片、GPS 质量、运行模式都挂在原生 QGC layer；
- toolbar 使用 `QGCColoredImage`、`QGCLabel`、`ToolIndicatorPage`、`SettingsGroupLayout`；
- PiP 只读，full video 才开放目标交互；SIM/HIL/REAL 只改变 metadata/backend，不改变 QML；
- 所有颜色、间距、字体和按钮使用 `QGCPalette`、`ScreenTools`、`QGCButton` 等 QGC token。

### 5.1 交互飞行与 AI 共用控制入口

联合验收用的 `px4_gazebo_qgc_router` 继续保持 QGC 只读策略；交互 SIM 使用独立的
`SimulatorInteractiveRouter`（旧版/兼容模式仍可选 `SimulatorControlGateway`），避免把只读验收
契约改成可写。interactive router 绑定两条 job 专属 loopback 输入端口（默认 QGC
`127.0.0.1:14650`、PX4 telemetry `127.0.0.1:14541`），QGC 自己绑定 `127.0.0.1:14649`，
adapter 输出固定为 `udpout:127.0.0.1:14540`。QGC 输入首先经过 `qgc_control_mode` 策略：

| 模式 | 用途 | QGC 写入面 |
|---|---|---|
| `read_only`（默认） | acceptance fixture、headless/dry-run、协议探针 | `MANUAL_CONTROL (69)` + 明确只读发现帧；mission、parameter、flight write 全部拒绝 |
| `sim_flight` | 用户在原生 Fly View 操控 disposable PX4 SITL | 在相同 loopback/身份/target 门槛下，额外允许下表所列的有界命令和 Mission；不是通用 MAVLink 透传 |

两种模式均只接受：

1. datagram 来源为 `127.0.0.1`；
2. MAVLink source system/component 为 QGC `255/190`；
3. message id 为 `MANUAL_CONTROL (69)`；
4. target system 为 SITL `1`，四个轴在 MAVLink 合法范围内。

`sim_flight` 的 allow-list 固定为：

- `COMMAND_LONG`：`COMPONENT_ARM_DISARM`、`DO_VTOL_TRANSITION`、`NAV_RETURN_TO_LAUNCH`、
  `NAV_LAND`、`MISSION_START`；
- `SET_MODE`：PX4 主模式/ AUTO 子模式通过固定集合校验，OFFBOARD 与 TERMINATED 排除；
- Mission：`MISSION_COUNT` + `MISSION_ITEM_INT`，最多 64 项，序号连续、mission type 为 0，
  仅 global-int/mission frame，命令限于航点、起飞/降落、返航、盘旋和 VTOL transition；
- `PARAM_SET`、FTP 写入、legacy `MISSION_ITEM`、未列出的 `COMMAND_LONG` 及任何非法参数继续
  fail closed，并记录逐帧拒绝原因。

通过校验的 `x/y/z/r` 被还原为 `pitch/roll/throttle/yaw` 的 `[-1,1]`/`[0,1]` 归一化值，
再进入同一个 `SimulatorControlAuthority` 和 `SimulatorMavlinkControlAdapter`。因此路径是：

```text
QGC native UDP MANUAL_CONTROL ─┐
                               ├─> SimulatorControlAuthority
AI executor -------------------┘          └─> loopback PX4 SITL
```

人工输入一到即夺回 authority；AI 未显式启用、超出 envelope、过 watchdog 或 job 关闭时，
控制链 fail closed。interactive router 证据写入 `interactive-router.json`（兼容路径写
`control-gateway.json`），包含手动接受数、只读转发数、sim_flight 命令/Mission 转发数、拒绝数、来源/目标身份、非 loopback
丢弃数、拒绝原因和 adapter 的 AI/manual/watchdog 计数；跨来源 partial frame 会重置 decoder，
`qgc_fragment_source_resets`/`px4_fragment_source_resets` 与
`*_rejected_reasons.fragment_source_changed` 必须记录，避免把不同 datagram/source 的字节拼接。
`qgc_identity` 明确记录
`system_id=255`、`component_id=190`、`target_system=1` 与 `target_component_ids`，并分别
保留 QGC/PX4 datagram blocked 计数。该代理没有 QML、
`Window`、第二个 `VideoOutput` 或独立操作界面，用户仍只在 MultiDetectGCS 原生 Fly View
里操控模拟飞行器。

交互 QGC 路径还对 `MANUAL_CONTROL` 建立独立的输入新鲜度门：
`SimulatorOrchestrator(interactive=True)`、standalone `control-gateway` 和
standalone `interactive-router` 默认使用 `manual_watchdog_s=0.5`。在 authority 仍为
`MANUAL` 且超过该期限没有新帧时，adapter 原子地进入 `FAILSAFE`，发出且只发出一次
`source=manual_watchdog` 的 canonical neutral；随后重复 tick 不再刷 neutral。显式释放
manual authority 会清除旧时间戳，不会把已经停止的 QGC 流误判为 stale；AI 接管也会清除
manual lease。headless/deterministic 编排器默认保持 `None`，避免改变旧 fixture 的时间语义，
也可以通过 `--manual-watchdog-seconds <s>` 显式覆盖。`authority.json`、
`interactive-router.json`/`control-gateway.json` 和 `job.json` 会记录阈值、
`manual_watchdog_neutral_count` 及 `manual_control_active`，便于检查实际安全门与运行配置一致。

带 QGC 且非 `--headless` 时，`SimulatorOrchestrator` 默认启用 interactive router；也可用
`--interactive` 显式启用，或用 `--legacy-control-gateway` 强制旧的 MANUAL_CONTROL-only
兼容路径。CLI 到端点的绑定必须保持一一对应：

| CLI 选项 | 默认值 | 绑定 |
|---|---:|---|
| `--qgc-control-port` | `14650` | router 接收 QGC MANUAL_CONTROL |
| `--qgc-output-port` | `14649` | QGC native isolated-HIL 收发 |
| `--px4-telemetry-port` | `14541` | router 接收 PX4 telemetry |
| `--px4-control-endpoint` | `udpout:127.0.0.1:14540` | adapter 唯一控制输出 |
| `--qgc-control-mode` | 交互时 `sim_flight`；否则 `read_only` | 只读探针或有界原生 Fly View 飞行策略 |
| `--throttle-wire-mode` | `qgc_legacy` | `MANUAL_CONTROL.z` 编码；可选 `qgc_legacy`/`signed` |
| `--manual-watchdog-seconds` | 交互时 `0.5` s；headless 时禁用 | QGC `MANUAL_CONTROL` 输入新鲜度；过期后一次 neutral/failsafe |
| `--gateway-start-timeout` | `2.0` s | router/gateway readiness 超时 |

编排器会在启动前校验四个端点互不相同、均为 literal loopback 且不占保护端口 `14550`；
router bind 失败、线程提前退出或 readiness 超时会把 job 标为失败并清理已启动进程，不能
只依据 PX4/Gazebo 进程存在来判定成功。停止时 router/gateway、AI executor 与 adapter
按顺序发送一次 neutral/failsafe，并把 `authority.json`、`control.json`、
`interactive-router.json`、`control-gateway.json`、`ai.json`、`job.json` 和 `result.json` 刷新为最终
状态；`job.json.throttle_wire_mode`、`control.json.throttle_wire_mode`、
`result.json.control.throttle_wire_mode` 与 router adapter evidence 必须一致；重复
`start()`/`stop()` 是幂等的。

### 5.2 validated PX4 telemetry → live AI bridge（pre.8，默认关闭）

`--ai-live`（别名 `--live-ai`）是显式 opt-in，只有 `--interactive --ai-live` 同时出现时才
创建 `SimulatorTelemetryBridge`。interactive router 先完成 literal-loopback、PX4 source
system/component、MAVLink frame 和长度校验，再把**已转发给 QGC 的原始 frame**通过 telemetry
sink 交给 bridge；因此观测和原生 Fly View 转发相互隔离，链路固定为：

```text
PX4 validated telemetry
        -> interactive_router telemetry sink
        -> truth-free SimulatorTelemetrySnapshot
        -> shared SimulatorAiExecutor
        -> SimulatorControlAuthority
        -> SimulatorMavlinkControlAdapter
        -> MANUAL_CONTROL -> PX4 SITL
```

bridge 只从 `HEARTBEAT`、GPS/位置、姿态、速度和系统状态等观测构造 snapshot，不读取场景
`truth.json`、Gazebo entity 或 evaluator 结果；错误 PX4 identity、截断/不支持的已知帧会被
拒绝并记录。除 executor 的 telemetry-age/watchdog 外，bridge 自己维护 `HEARTBEAT` freshness
lease，默认 `heartbeat_timeout_s=2.0s`：年龄达到阈值就把 `link_healthy=false`，沿共享
executor/adapter 发送一次 canonical neutral 并进入 FAILSAFE；同一 stale 窗口中的后续
GPS/位置帧只更新观察证据，不重复发 neutral。新 HEARTBEAT 只标记 telemetry lease 已恢复，
不会自动解除已经进入 terminal FAILSAFE 的 authority，恢复必须由操作员显式 release/enable。
内部 replay fixture 可传 `heartbeat_timeout_s=None` 禁用这条 lease，但 executor 自带 watchdog
仍生效。AI 的 envelope、过期遥测、watchdog、人工接管和停止 neutral/failsafe 仍由同一
authority/adapter 契约负责，bridge 不创建第二 actuator transport。需要原生 QGC Fly View
操控时，命令应显式带 `--qgc <MultiDetectGCS.exe> --interactive --ai-live`；`--manual` 会
保留人工优先，适合验证 takeover，不用于让 AI 取得初始 authority。

job 证据必须同时检查：

- `ai.json` 和 `result.json.ai.live_bridge` 的 `frames_observed`、`frames_accepted`、
  `frames_rejected`、`snapshots`、`heartbeat_seen`、`heartbeat_timeout_s`、
  `heartbeat_age_s`、`heartbeat_gate`、`heartbeat_stale`、`heartbeat_stale_events`、
  `heartbeat_recoveries`、`heartbeat_last_decision`、`ai_steps`、`ai_accepted`、
  `ai_rejected`、`failsafe_steps`、`watchdog_failsafe_steps`、`truth_free` 和
  `real_control_acceptance`；顶层 `live_ai_enabled`/`live_ai_transport` 也必须与 CLI 一致；
- `interactive-router.json` 的 `telemetry_sink_enabled`、`px4_telemetry_sink_frames`、
  `px4_telemetry_sink_errors`、`px4_telemetry_sink_message_ids`，确认 sink 只观察通过校验的
  PX4 帧且没有改写 QGC payload。

watchdog 的生命周期归属也必须从证据中可见：interactive router 的 event-loop 每轮先调用
`poll_watchdog()`，再等待两个 loopback UDP 输入。启用 live AI 时，该 tick 通过
`watchdog_sink` 调用 `SimulatorOrchestrator._consume_live_watchdog()`，由
`SimulatorTelemetryBridge.watchdog_tick()` 同时检查 HEARTBEAT freshness lease 和共享
`SimulatorAiExecutor` watchdog；因此 stale 或 executor 超时沿同一
`SimulatorControlAuthority` -> `SimulatorMavlinkControlAdapter` 路径只发一份 canonical
neutral/failsafe。`interactive-router.json` 的 `watchdog_ticks`、`watchdog_sink_calls`、
`watchdog_sink_errors` 和 `watchdog_sink_last_error` 记录调用与异常；没有 live sink 时，
router 回退到 adapter `check_watchdog()`，保持旧路径行为。sink 异常同样必须记录后回退，
不能让 event-loop 因观察回调崩溃，也不能重复发送 neutral。该字段证明的是 watchdog
调度与 fail-safe 证据，不是 PX4 已实际执行或真实飞行安全已验收。

默认关闭的是 PX4 telemetry `live bridge`：headless/dry-run 未显式 `--ai-live` 不会创建
`SimulatorTelemetryBridge`。这不影响 5.3 所述 integrated SIM builder 的 observation-only
`SimulatorPerceptionBridge`，后者只消费 `LiveMissionRunner` 已产出的生产感知对象，不打开
控制输出。这里的基础 `run_simulator_docker_acceptance.py` 和未显式打开 exercise 的 AI acceptance
仍是被动 PX4/Gazebo/qualified-telemetry machine gate，`flight_control_commands_sent=0`；它不启用
控制输出，也不因此升级为真实 AI-control、QGC 视觉或硬件 gate。v55 的持续 position-response 是
后续独立、显式 opt-in 子门，不改变这些默认语义。

### 5.3 integrated SIM production perception feed（pre.8，默认 observation-only）

`build_integrated_sim_session(...)` 是 Gazebo raw-sensor 到
`LiveMissionRunner` 的生产绑定，不另起一条 SIM 感知实现。runner 在每帧完成 detector、
`UnifiedTrackSnapshot`/target-pool、`RangeSolution` 和单目避障评估后，通过唯一的
`simulator_perception_sink` 槽位把下列**已经存在的生产对象**交给
`SimulatorPerceptionBridge`：

```text
Gazebo camera/NavSat raw stream
        -> GazeboSimBackend -> LiveMissionRunner
        -> VehicleTelemetry + unified tracks + range + avoidance
        -> SimulatorPerceptionBridge
        -> SimulatorTelemetrySnapshot (truth-free)
        -> [optional caller-owned shared SimulatorAiExecutor]
```

builder 的默认行为是创建一个不附带 executor 的
`SimulatorPerceptionBridge()`，因此 integrated SIM 即使 headless 也会执行同一套 GPS、
距离、目标可见性和避障字段转换并保留 feed evidence，但不会因为构建 session 就产生
`MANUAL_CONTROL`。需要让生产观测驱动已经存在的 SIM AI 时，通过 `runner_options` 传入
`simulator_perception_bridge=<bridge>`；bridge 可以引用调用者唯一持有的
`SimulatorAiExecutor`/`SimulatorMavlinkControlAdapter`，不在 builder 内新建控制器。也可以
传 `simulator_perception_sink=<callable>` 接入其他观察消费者。两个选项互斥，显式传 `None`
会关闭默认 observation bridge；省略则启用默认 bridge。

builder 仍拥有并锁定 `frame_source`、`telemetry_provider`、runtime/performance clock 和
`runtime_status`，这些键在 `runner_options` 中会被拒绝。LiveMissionRunner 每帧只调用一个
sink；传入 bridge 时绑定其 `feed_result`，不会再创建第二个 executor、socket 或 actuator
transport。sink 接收到的 track 参数保持一致：完整 pool 走 `tracks`，选中的目标走
`selected_track_id`；无 pool 时两者分别为 `()` 和 `None`，不允许悬空目标 ID。

`IntegratedSimSession.perception_evidence()` 合并 bridge 与 runner 计数，至少核对
`observations`、`last_snapshot`、`truth_free`、`secondary_transport_emitted`、
`runner_feed_count` 和 `runner_error_count`。这只证明 raw-sensor → production observation
绑定；不提升 `qml_rendering_proved`、`operator_visual_acceptance`、真实 Pixhawk/Jetson、
室外 GNSS 或飞行验收等级。默认 observation bridge 也与 5.2 的 PX4 telemetry
`SimulatorTelemetryBridge` 分开：前者消费 LiveMissionRunner 已产出的感知对象，后者只在
`--interactive --ai-live` 下消费 router 校验后的 MAVLink telemetry；同一个 executor 若被
两条路径同时驱动，必须由调用者显式协调并保留单一控制输出。

真实 QGC/SITL 交互命令（先用 `probe --json` 确认工具）：

```powershell
cd D:\Projects\Multi-Detect
$python = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
$env:PYTHONPATH = 'src'
$qgc = '.\build\windows-v0.3.0-pre.8-release\portable-v0.3.0-pre.8\bin\MultiDetectGCS.exe'

& $python -m multidetect.simulator probe --json
& $python -m multidetect.simulator run `
  --scenario configs/scenarios/river_rescue_vtol_001.scenario.json `
  --qgc $qgc --interactive --manual --realtime --keep-job `
  --qgc-control-port 14650 --qgc-output-port 14649 `
  --px4-telemetry-port 14541 --px4-control-endpoint udpout:127.0.0.1:14540 `
  --qgc-control-mode sim_flight `
  --throttle-wire-mode qgc_legacy `
  --job-root artifacts/evaluation/simulator-interactive
```

只检查 CLI/authority 而不启动外部进程时，使用
`run --headless --dry-run --ai-step [telemetry.json] --keep-job`。dry-run 的 AI step 仍经
truth-free telemetry schema、AI envelope 和共享 adapter，但 transport 是进程内 recording
sink；结果状态应为 `planned`。读取 `interactive-router.json` 时至少核对
`literal_loopback_only=true`、`telemetry_frames_rewritten=false`、
`qgc_parameter_writes_allowed=false`、`qgc_manual_control_via_shared_adapter=true`，以及
`qgc_identity.system_id=255`、`qgc_identity.component_id=190`、`qgc_identity.target_system=1`。
若该 dry-run 显式带 `--interactive`（包括 `--ai-live` 的 CLI smoke），实际
`qgc_control_mode` 默认是 `sim_flight`，因此
`qgc_flight_commands_allowed=true`、`qgc_mission_writes_allowed=true` 只表示受下方
allow-list 约束的模拟飞行入口已启用；没有 `--interactive` 的 headless/dry-run 则为
`read_only`，这两个字段为 `false`。无论哪种模式都不是通用 MAVLink 透传。

### 核心侧

```text
FrameSource + TelemetryProvider
        -> production perception factory
        -> LiveMissionRunner
        -> target/tracking/range/geolocation state
        -> MissionController + SafetySupervisor
        -> RuntimeStatus / audit / QGC metadata
```

禁止在 `if mode == SIM` 分支里直接生成检测、距离、坐标或成功状态。模拟器只替换输入端和
时钟，不能替换核心决策。

## 6. 编排器接口草案

`src/multidetect/simulator/` 是编排层，现有 `backends/gazebo_sim.py` 继续作为真实 Gazebo
传输适配器。当前已落地的模块与后续扩展位如下：

```text
  simulator/
  orchestrator.py       # 生命周期、端口、job 目录、退出清理
  control.py            # MANUAL/AI/PAUSED/FAILSAFE authority
  mavlink_control.py    # 唯一 loopback MANUAL_CONTROL 输出与 watchdog
  control_gateway.py    # 兼容的 MANUAL_CONTROL-only QGC 输入
  interactive_router.py # native QGC↔PX4 双向遥测/手动代理
  live_ai_bridge.py     # 已校验 PX4 telemetry → truth-free snapshot → shared AI
  perception_bridge.py  # production telemetry/track/range/avoidance → SIM snapshot
  ai_executor.py        # truth-free observation → bounded AI axes
  closed_loop.py        # flight model + shared adapter 的无外部 I/O 闭环
  isolation.py          # SIM deny rules and profile checks
  flight_model.py       # 确定性 CI 动力学/传感器 fixture（不替代 Gazebo）
  px4_launch.py         # job-local rc.mavlink/wrapper、readiness 和 hash 绑定
  readiness.py          # PX4/MAVLink heartbeat readiness probe
  external_stack.py     # PX4/Gazebo argv/cwd/env/log 的纯计划层，不启动进程
  interactive_acceptance.py # native QGC 路径的确定性 acceptance fixture
  scenario_runner.py    # manifest、seed、时间轴、终态
  stack.py              # 仓库 world/model 解析、端口隔离与 hash-bound 启动计划
  safety_evidence.py     # 证据摘要、哈希绑定与 fail-closed 质量门
```

上面四个职责已经由现有模块承载，不再作为未实现的 `[planned]` 文件重复规划：
`orchestrator.py` 负责生命周期与持久化，`readiness.py` 负责 SITL heartbeat/mission
runtime health，`stack.py` 与 `external_stack.py` 负责 world/model/plugin 生命周期计划，
`perception_bridge.py` 负责传感器与故障日程适配。后续新增进程监督或独立 writer 时，
必须扩展这些接口并保持与现有证据 schema、端口隔离和安全门兼容，不能另起一套 SIM 算法。

同级的 `src/multidetect/integrated_sim.py` 负责把 `GazeboSimBackend` 绑定到完整
`LiveMissionRunner`，并按 5.3 的规则注入 production perception bridge；它不属于第二套
simulator UI 或独立算法栈。

跨层 Mission 编译器位于 `src/multidetect/px4_mission.py`，由 simulator/SIM、HIL 和 REAL
共用；它不拥有网络连接，`Px4MissionTransaction`/acceptance adapter 才负责 upload、readback
和 commit。这样固定翼的几何/包络约束不会因运行模式分叉。

CLI 目标：

```powershell
python -m multidetect.simulator run `
  --scenario configs/scenarios/river_rescue_vtol_001.scenario.json `
  --qgc .\build\windows-v0.3.0-pre.8-release\portable-v0.3.0-pre.8\bin\MultiDetectGCS.exe `
  --interactive --qgc-control-port 14650 --qgc-output-port 14649 `
  --px4-telemetry-port 14541 --px4-control-endpoint udpout:127.0.0.1:14540 `
  --seed 42017 --frames 600 --realtime --manual
```

多旋翼场景使用同一 `run`/stack-plan 入口，只替换 manifest：

```powershell
python -m multidetect.simulator run `
  --scenario configs/scenarios/urban_inspection_multirotor_001.scenario.json `
  --qgc .\build\windows-v0.3.0-pre.8-release\portable-v0.3.0-pre.8\bin\MultiDetectGCS.exe `
  --interactive --qgc-control-port 14650 --qgc-output-port 14649 `
  --px4-telemetry-port 14541 --px4-control-endpoint udpout:127.0.0.1:14540 `
  --seed 42017 --frames 600 --realtime --manual
```

调试选项：

```text
--headless          不启动 Gazebo GUI，但仍运行同一物理/传感器链
--realtime          1.0x wall-clock；默认 deterministic fast mode
--pause-at <s>      在场景时间暂停，等待人工检查 QGC
--fault <id>        单独启用 manifest 中的故障事件
--interactive       启用 native QGC↔PX4 双向 loopback router
--legacy-control-gateway  强制 MANUAL_CONTROL-only 兼容网关
--ai-step [JSON]    注入一份 truth-free telemetry，执行单步 AI decision
--throttle-wire-mode <mode>  MANUAL_CONTROL 油门编码：qgc_legacy（默认）或 signed
--manual-watchdog-seconds <s>
                     QGC MANUAL_CONTROL 新鲜度门；interactive 默认 0.5 s，
                     headless/deterministic 默认禁用
--gateway-start-timeout <s>  router/gateway readiness 超时（默认 2.0）
--readiness-timeout <s>      可选 PX4/MAVLink readiness 接收窗口（省略时非阻塞）
--readiness-require-heartbeat / --readiness-no-heartbeat
                             是否要求 readiness probe 观察 HEARTBEAT
--readiness-strict / --readiness-nonstrict
                             readiness 失败是否阻断真实运行
--keep-job          保留日志、录制和证据
--ai-live / --live-ai  仅与 --interactive 一起启用 validated PX4 telemetry live bridge
```

退出码必须区分：启动失败、传输不完整、核心断言失败、终态失败和完整通过；不能只看进程
是否启动。

### 6.1 `stack-plan` 资产/启动计划

`src/multidetect/simulator/stack.py` 提供无副作用的 `stack-plan` CLI。它读取 SIM runtime
channel，解析 `builtin://gazebo/<world>` 到仓库内 SDF，绑定对应 vehicle model/model.config，
校验 world/model 不含网络/Fuel 外链，检查 `127.0.0.1`/`::1` literal-loopback 端口唯一性（并
拒绝保护端口 `14550`），最后输出带 scenario/asset SHA-256 的 PX4/Gazebo 命令和环境变量。
该步骤不启动进程、不打开 socket、不读取 `truth.json`，适合作为真实运行前的可审计预检。

```powershell
cd D:\Projects\Multi-Detect
$env:PYTHONPATH = 'src'
$python = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
& $python -m multidetect.simulator stack-plan `
  --scenario configs\scenarios\coastal_patrol_fixed_wing_001.scenario.json `
  --out artifacts\evaluation\stack-plan-fixed-wing.json
```

输出字段至少包括 `scenario_id`/`scenario_manifest_hash`、`vehicle_model`、`world_path`、
`assets_sha256`、`ports.loopback_only`、`px4_command`、`gazebo_command` 和
`environment`。`probe --json` 对 Docker 还会给出 `daemon_available`、
`daemon_version`、`daemon_reason`：Docker 客户端存在不代表 Server 可连接；只有 daemon
health 通过时，Docker acceptance runner 才能作为可启动的外部 runtime。native 编排器
只有在 plan 通过且 PX4/Gazebo 可执行文件可用后才进入 disposable 运行；Docker acceptance
走单独的 pinned-image runner。plan/probe 通过都不等价于 Gazebo 渲染、QGC 桌面视觉、硬件
HIL 或真实飞行验收。

### 6.2 job-local PX4 wrapper 与 `external-plan`

#### 为什么不能只传环境变量

stock PX4 POSIX 启动链由 `etc/init.d-posix/rcS` 继续加载 `px4-rc.mavlink`，其中 GCS/offboard
端口可能继续使用 PX4 自身的 `18570/14580` 默认值，而不是 `stack-plan` 环境中的
`PX4_MAVLINK_UDP_PORT=14540`、`PX4_MAVLINK_TELEMETRY_PORT=14541`。因此真实 SIM 进程不能只看
environment 声称端点已绑定。

`src/multidetect/simulator/px4_launch.py` 的 `build_px4_launch_plan(...)` 在 job 已创建后执行：

```text
PX4 source build/etc/init.d-posix/px4-rc.mavlink       # read-only
                  │ copy + patch
                  ▼
<job>/px4-init/px4-rc.mavlink
  mavlink start -x -t 127.0.0.1 -u 14540 -o 14541 ...
                  │ explicit source from generated rcS
                  ▼
<job>/px4-init/rcS
  . "$MD_SIM_PX4_RC_MAVLINK"
                  │ explicit -s binding
                  ▼
<job>/px4-sim-launch.sh
  cd <PX4_WORKDIR>
  PATH=<job>/px4-init:$PATH
  MD_SIM_PX4_RCS=<job>/px4-init/rcS
  PX4_NET_INTERFACE=
  exec px4 -i 0 -s "$MD_SIM_PX4_RCS"
```

生成器不修改 PX4 checkout，不打开 socket，不启动 PX4，也不读 truth/evaluator channel。源
`rcS`/`px4-rc.mavlink`、生成副本和 wrapper 均记录 SHA-256；副本和 wrapper 都带
`MD_SIM_STACK_PLAN_SHA256`、`MD_SIM_SCENARIO_SHA256`、端口和 literal-loopback host marker。
artifact/evidence 路径必须留在 job 内、leaf symlink 被拒绝，`verify_px4_launch_plan(...)` 会重新
计算生成文件 hash 并检查 provenance marker。

#### 三层命令

1. **诊断模板（没有 PX4 也可运行）**：

   ```powershell
   cd D:\Projects\Multi-Detect
   $env:PYTHONPATH = 'src'
   $python = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
   & $python -m multidetect.simulator px4-launch-plan `
     --scenario configs\scenarios\river_rescue_vtol_001.scenario.json `
     --job-dir artifacts\evaluation\px4-launch-vtol-dry-run `
     --dry-run
   ```

   该命令写入 job-local `px4-launch.json` 和 deterministic template。工具/源脚本缺失时
   `source_is_template=true`、`readiness.gate_passed=false`；命令仍返回完整诊断，但没有进程/I/O。

2. **严格无启动 preflight + external process plan**：

   ```powershell
   $px4Workdir = (Resolve-Path `
     '.\vendor\PX4-Autopilot\build\px4_sitl_default').Path
   $px4 = Join-Path $px4Workdir 'bin\px4'
   $job = 'artifacts\evaluation\external-stack-vtol'

   & $python -m multidetect.simulator px4-launch-plan `
     --scenario configs\scenarios\river_rescue_vtol_001.scenario.json `
     --job-dir "$job\px4-preflight" `
     --px4-workdir $px4Workdir --px4 $px4 --strict

   & $python -m multidetect.simulator external-plan `
     --scenario configs\scenarios\river_rescue_vtol_001.scenario.json `
     --job-dir $job `
     --px4-workdir $px4Workdir --px4 $px4 --gazebo gz `
     --px4-launch --strict --px4-launch-strict `
     --out "$job\external-stack.json"
   ```

   `external-plan` 固定 `PX4 -> Gazebo` 顺序、依赖、argv/cwd/env 和 job-local log 路径；启用
   `--px4-launch` 后 PX4 process spec 指向 wrapper，原始 command 保留在 `source_stack_plan`。
   该命令本身仍不调用 `subprocess`、不打开 socket，`external_processes_started=false`。

3. **编排器实际运行**（严格计划、工具和 POSIX runner 均就绪后）：

   ```powershell
   $qgc = '.\build\windows-v0.3.0-pre.8-release\portable-v0.3.0-pre.8\bin\MultiDetectGCS.exe'
   & $python -m multidetect.simulator run `
     --scenario configs\scenarios\river_rescue_vtol_001.scenario.json `
     --stack-px4-workdir $px4Workdir --stack-px4 $px4 --stack-gazebo gz `
     --px4-launch --px4-launch-strict `
     --qgc $qgc --interactive --qgc-control-mode sim_flight `
     --manual --realtime --keep-job `
     --job-root artifacts\evaluation\simulator-interactive
   ```

repository stack 且没有 custom process specs 时，orchestrator 默认启用 PX4 wrapper；non-dry-run
默认 strict。`--no-px4-launch` 只保留给经过独立端口证明的旧/自管启动链。wrapper 是 POSIX
shell：Windows 只负责生成/取证时可以保留 Windows path；真正由 WSL/Linux/container 执行前，
runner 必须翻译/mount repository、job 和 PX4 build path，并重新校验相同 hash。找到 `sh`/`bash`
本身不能证明 Linux PX4 binary 可执行。

#### 证据和失败边界

`px4-launch.json` 至少保存：source stack/scenario hash、`control_endpoint`/`telemetry_endpoint`、
source/generated script hashes、wrapper hash、command/cwd/env、每项 readiness 与 probe command。
orchestrator 还在 `job.json`/`result.json` 保存 `px4_launch_enabled`、`strict_requested`、
`px4_launch_sha256`、`artifacts_verified` 和 strict error。**wrapper/preflight strict** 失败时，
job 会在启动外部进程前标记为 `blocked`；`readiness_strict` 的运行时 heartbeat probe 则在
PX4/Gazebo（及可选 QGC）启动后执行，失败会触发关闭并将结果标记为 `failed`，不能与 preflight
`blocked` 混写。

两个容易误判的字段必须分开：

- `px4-launch.json.readiness.gate_passed` 是 **wrapper preflight**；文件中仍明确
  `process_started=false`、`telemetry_observed=false`；
- `external-stack.json.validation.gate_passed` 在 dry-run 中会忽略工具/readiness advisory；外部
  runtime readiness 必须检查 `validation.ready_to_launch`、`readiness.tools_available`、
  `readiness.px4_workdir_resolved` 和嵌套 `px4_launch.readiness.gate_passed`。

完成 planner 之后仍必须由 runtime evidence 证明：process PID/exit、`14540/14541` 实际 socket、
PX4 heartbeat/Mission health、Gazebo camera/NavSat、QGC RTSP/signed metadata 同帧 barrier、清理和
端口释放。它们也仍然不能替代桌面 GPU/QML 人工视觉、真实 Pixhawk/Jetson/GNSS/相机、HIL、
REAL 和实飞验收。

### 6.3 Docker Desktop runtime gate

当 Windows 主机没有 native PX4/Gazebo 时，`scripts/run_simulator_docker.py` 负责生成
repository-owned world/model 的 pinned Docker 命令，`scripts/run_simulator_docker_acceptance.py`
负责可选的外部运行。默认调用只做 preflight；只有显式 `--execute` 才会启动 disposable
container。执行门核对 image digest、purpose label、bridge 网络、只读 bind mount、control
端的 host loopback 发布边界、环境与 protected UDP `14550`，然后用被动 MAVLink probe 等待
PX4 heartbeat 和 qualified GPS，finally 固定 stop/rm 容器。基础 probe 只发送 GCS heartbeat，
飞行控制帧计数必须为零；AI 专用脚本默认同样被动。显式 `--exercise-arm` 才会在 disposable
SITL 内走 arm/AI/disarm 闭环，`--exercise-target` 再增加 truth-free target fixture；只有继续给出
`--exercise-flight-seconds 1..30` 与 `--minimum-flight-displacement-m`，才进入 v55 持续刷新、armed
POSCTL、heartbeat continuity、零 rejection/failsafe 和 PX4 `GLOBAL_POSITION_INT` 位移联合门。
三档 opt-in 都复用同一 authority/watchdog/MAVLink adapter，且各自完整记录并清理（见下文与本文 v55 节）。
当前 pinned image 为
`px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`。

Docker Desktop 的容器到宿主机 telemetry 需要 `host.docker.internal` bridge 地址，因此证据同时
记录 `ports.host_loopback_only=true` 与 `ports.container_target_is_loopback=false`；不能把这两个
边界混成一个“全链路 loopback”结论。当前 planner 只发布 control `14540`，telemetry `14541`
明确记为 `direction=container_to_host`、`publication=outbound_only`、`published=false`。
发布同端口会让 Docker Desktop UDP proxy 产生 hairpin/drop，导致容器已经运行却在宿主收不到
PX4 telemetry；因此 container inspection 和 acceptance fixture 都要求
`telemetry_port_not_published=true`。Windows UDP proxy 可能拒绝 `udpin:127.0.0.1:<port>` 的
独占绑定，probe 可以记录并退到 `0.0.0.0` 接收，但唯一的 host publication 必须是明确的
`127.0.0.1` control 端。

本机的 VTOL 与 fixed-wing external telemetry gate 均已通过：

* `artifacts/evaluation/simulator-docker-acceptance-20260828c.json`，VTOL，qualified GPS
  fix type 3，容器已清理；SHA-256 `B014B98EAE8602C0C0D4CD3DBA8DABC01FB0902F1C635A5CE26B274949706AF6`；
  container log SHA-256 `B35DC53623FF8E2F1DA9B390F732F4B4ADC2662A9E26B51E1F5F8825F1A1E192`；
* `artifacts/evaluation/simulator-docker-acceptance-fixedwing-20260828.json`，`coastal_patrol_fixed_wing_001`、
  `PX4_SYS_AUTOSTART=4003`、`gz_rc_cessna_mono_cam`、qualified GPS fix type 3、容器已清理；
  SHA-256 `8EAFD249C5118E9703C5F20AE95D7B64FAF8450B627DC3A0911A785B587FC2F2`；
  container log SHA-256 `AB3A28ED802A4967ABF2DEEF6EF9788B7930212F713EDCEFAFA97118771CC301`。

这两份 evidence 只提升 container/PX4/Gazebo/telemetry machine gate；`visual`、`hardware`、
QGC Fly View 人工操控、Mission upload/readback、HIL、REAL 和实飞字段仍必须分别验收。

2026-08-30 的 AI acceptance 修复回归
`artifacts/evaluation/simulator-docker-ai-acceptance-20260830-fixed.json`（SHA-256
`8C43D86830783AFBE1C5CF45C50B7EAA108555259DDD3BBD809FBC2A300AB8CC`）验证了这条边界：
`runtime_gate_passed=true`、`telemetry.qualified=true`、`4057` 帧观察、`3956` 帧接纳、
`101` 帧身份拒绝、`wildcard_bind_fallback=false`、容器清理 `removed=true`。车辆保持
`armed=false`，所以 `ai_policy_gate_passed=false`、`ai_steps=0`、总 `gate_passed=false`；
该证据只证明 telemetry/runtime 修复，不能写成 AI 已控制 PX4。

随后使用显式 `--exercise-arm` 运行了 disposable SITL arm 闭环：
`artifacts/evaluation/simulator-docker-ai-acceptance-20260830-arm-v6.json`（SHA-256
`35A19F6FF3656C82F170AB2061A044CFFA0DB9C24EB125CE21914132E7684D62`），容器日志 SHA-256
`5F34112A19F05FF13DEAAD270EE53444494F6AD444D0EA4A83F9EEF1BCCC9BA5`。该选项在容器启动
后先应用四个已有 VTOL SITL arm 预检参数，再沿同一条 loopback control socket 发送
`MAV_CMD_COMPONENT_ARM_DISARM`（arm 的 PX4 force sentinel `21196`），等待真实 `armed`
heartbeat、执行一个共享 AI policy step，最后发送 disarm。证据为
`runtime_gate_passed=true`、`ai_policy_gate_passed=true`、`ai_policy_steps=1`、
`armed_observed=true`、`disarm_command_sent=true`、清理 `removed=true`；4 次有界 arm
尝试中前 2 次为 `MAV_RESULT_TEMPORARILY_REJECTED`，后 2 次为 `accepted`。该 machine gate
仍不等同于真实 Pixhawk/Jetson/物理 GNSS/相机、HIL/REAL、执行器响应、实飞或人工 QGC
Fly View 验收，版本仍为 `v0.3.0-pre.8`。

台式机迁移后又用仓库自动发现的 Docker Desktop 安装路径重跑同一闭环：
`artifacts/evaluation/simulator-docker-ai-acceptance-20260830-desktop-rerun.json`，SHA-256
`A0D561EBD3907B10578990E4713562749BE186CF8D7BF9AF5EF2FC902E4A62B5`；容器日志 SHA-256
`8A1F4F7927B3D4AA5D9F1932102892E70F72EB5E2D06951B840451166F28D893`。Docker Server
`28.4.0/linux x86_64` 可连接，`runtime_gate_passed=true`、`telemetry.qualified=true`、
`armed_observed=true`、`ai_policy_gate_passed=true`、`ai_steps=1`/`ai_accepted=1`，并完成
disarm 与 stop/rm 清理。该证据仍只闭合 disposable PX4/Gazebo SITL machine gate；真实
Pixhawk/Jetson/GNSS、物理执行器、HIL/REAL、实飞和人工 QGC Fly View 仍分别验收。

2026-09-01 Docker Desktop 恢复后又完成一次同路径 live AI 重跑：
`artifacts/evaluation/simulator-docker-ai-acceptance-20260901-sim-rerun.json`（SHA-256
`80A21B60271E9AEC9C41AB9346FB01D66C93DD499B6B6AAA45A3FB70170C95FF`，96,607 bytes）。
该证据的 `runtime_gate_passed`、`telemetry.qualified`、`armed_observed` 和
`ai_policy_gate_passed` 均为 `true`，`ai_steps=1`；一个 AI policy step 通过共享
`SimulatorMavlinkControlAdapter` 输出 `MANUAL_CONTROL`，随后发送 disarm、stop/rm 清理。
它只更新 disposable SITL 的 live AI provenance，不提升真实硬件、室外 GNSS、HIL/REAL、
人工 Fly View 或实飞门。

在此基础上，`SimulatorTelemetryBridge.ingest_perception()` 已接入一个显式、可审计的
感知输入边界。它只接受 truth-free `SimulatorTelemetrySnapshot` 中的目标四元组
（`target_visible`、`target_bearing_deg`、`target_distance_m`、`target_confidence`），
PX4 telemetry 仍独占 GPS、姿态、armed/link、速度和高度；感知 mapping 中携带的其它字段
会被丢弃。目标样本按默认 `1.5 s` lease 做 future/stale/out-of-order 校验，过期会在下一个
policy snapshot 前清空，随后仍走 heartbeat freshness、首个 armed heartbeat、authority、
watchdog 和同一个 `SimulatorMavlinkControlAdapter` `MANUAL_CONTROL` 输出。

`run_simulator_docker_ai_acceptance.py --execute --exercise-arm --exercise-target` 只在
disposable SITL 中开启一次 synthetic perception fixture，并把 `target_exercise`、TRACK
decision 和 `visual_accuracy=not_claimed` 写入 evidence。2026-09-01 三种机型的证据如下：

* VTOL：`artifacts/evaluation/simulator-docker-ai-target-river_rescue_vtol_001-20260901.json`
  （SHA-256 `5BD19143E24272243961D36202804275CDB4A1F88027AF05500BB9EC6CF9B50A`）；
* fixed-wing：`artifacts/evaluation/simulator-docker-ai-target-coastal_patrol_fixed_wing_001-20260901.json`
  （SHA-256 `4FB9B384FBA588603BB9AF166E43AF368644F9B35D531D3556FAA16223E5833A`）；
* multirotor：`artifacts/evaluation/simulator-docker-ai-target-urban_inspection_multirotor_001-20260901.json`
  （SHA-256 `9E13005E7D722018015B6048DB6F6F3885CF78A6BFB5CFF747C626056A4119AC`）。

三份 evidence 都为 `gate_passed=true`、`target_policy_gate_passed=true`、`TRACK` 且
`accepted=true`，并完成 disarm/stop/rm；它们仍不提升相机视觉准确率、室外 GNSS、真实
Pixhawk/Jetson、执行器响应、HIL/REAL、人工 Fly View 或实飞状态。

随后将拒绝路径也固定为 fail-closed：`ingest_perception()` 的 future、stale、out-of-order
或 malformed 分支统一清空 perception-owned target lease，再记录 rejection reason；因此旧
目标不能跨越被拒帧继续进入 policy snapshot，同时不触碰 PX4 独占的 GPS、armed/link 和姿态
字段。三类“已有目标后再拒绝”回归已纳入 pre.8 goal-acceptance；最新快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-target-ai-v2.json`，核心测试
`2176`、QGC 自定义 `66`，硬件/视觉门仍保持未声明。

共享入口随后完成收口：当 `SimulatorPerceptionBridge` 绑定
`telemetry_bridge` 时，`feed_result()` 委托唯一持有的
`SimulatorTelemetryBridge.ingest_perception()`，不再绕过 heartbeat、armed、target lease、
watchdog 或 authority 直接调用 executor。bridge 拒绝的 future/stale/out-of-order/malformed
帧在返回 runner 前清空目标四元组并返回
`bridge_rejection_reason=telemetry_bridge_rejected_perception`；PX4 独占的 GPS、armed/link、
姿态、速度、高度和电量字段保持不变。共享入口回归已进入最新完整快照
`artifacts/evaluation/multidetect-goal-acceptance-20260901-target-ai-v3.json`（SHA-256
`A49DDB605FD90AA04BBFCA7185ECB600FACA52E3BAF3185F262C2B9DE6F3D560`），核心测试 `2178`、
QGC 自定义 `66`，脚本退出码 `0`；`software_status=pass_with_environment_gap`、
`hardware_status=offline_pending`、现场 `hardware_link_ready=false` 和人工 Fly View
`not_claimed` 均保持不变。该修复同步重算 `configs/release-parity.json` 的
`core_algorithm_hash=71e59bb128a652d67827aa3da615af4b8f8af8f0ad8a6358cf7acc6ab16bcd63`，
不改变 portable EXE，也不把 SIM 证据升级为室外 GNSS、真实 Pixhawk/执行器、HIL/REAL 或最终
`v0.3.0`。

### 5.2.1 perception range freshness seam（pre.8）

`SimulatorPerceptionBridge` 现将 `RANGE_STATUS` 的时间契约带入 production perception adapter，
默认复用 `MAX_RANGE_STATUS_SOURCE_AGE_S=1.5` 秒。`RangeSolution` 的
`data_freshness_s`（以及兼容的 `freshness_s`/`age_s`）和带源帧时间的
`RangeStatusMessage`（`source_captured_at_s`，兼容 `captured_at_s`/`observed_at_s`/`timestamp_s`）
在目标匹配后、进入共享 AI/authority 之前统一校验：年龄必须有限、非负、非未来且不超过阈值。
过期、未来或显式缺失源时间的测距只保留视觉 track 可见性，清空距离、方位和测距置信度；负数
或非有限值按 malformed 输入拒绝。`evaluated_at_s` 单独存在时不被强行跨时钟域比较，以保持旧
回放的时间域兼容；生产源只要提供显式 freshness/source timestamp 就会得到 fail-closed 保护。

这使 SIM、HIL 和 REAL 在测距陈旧时拥有同一行为，不会因 adapter 与 operator-link 使用了不同
的时间门而产生“地图/卡片显示实时、AI 却使用旧距离”的分叉。bridge evidence 记录阈值、识别的
字段别名和拒绝原因。对应完整 pre.8 goal-acceptance 为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-target-ai-v4.json`（SHA-256
`D4B0EE2E7F274B0904DE2CDB60AC42DA4195CFAFE31D1774418A8180E46DE5CF`），核心测试收集 `2183`、
QGC 自定义 `66`，脚本退出码 `0`；`software_status=pass_with_environment_gap`、
`hardware_status=offline_pending`、现场 `hardware_link_ready=false`、人工 Fly View
`not_claimed` 均保持不变。该 Python-only 修复同步更新 canonical parity 的
`core_algorithm_hash=6f224eb199fd53979648f477e364f30c318c303b6a97d303eae548948f9290f0`，不需
重编 QGC，且不改变任何室外 GNSS、真实 Pixhawk/执行器、HIL/REAL 或最终发布门状态。

### 5.2.2 SIM AI return GPS freshness seam（pre.8）

返航与低电量 return path 现在不再只看 fix、卫星数和水平精度，而是额外要求独立的
`gps_observed_at_s` raw-GPS 游标：时间戳必须存在、不得在 `now_s` 之后，且年龄不超过
`maximum_return_gps_age_s=1.5 s`。持续的 heartbeat 或 `GLOBAL_POSITION_INT` 融合位置不会刷新
该游标；缺失、stale、future 均保持 `return_gps_quality_insufficient` fail-closed，防止旧 GPS
坐标被误用于 RTL。fresh-boundary、missing、stale、future 和配置边界已加入
`tests/test_simulator_ai_executor.py`，脚本闭环 `run_simulator_ai_acceptance.py` 退出码为 `0`。

该轮总验收绑定
`artifacts/evaluation/multidetect-goal-acceptance-20260901-field-recheck-v5.json`（SHA-256
`7EAE02CC318160650673508692C8D5F5C6636ADBADA1ACED2BBF098661FAE70`），核心测试收集 `2183`、
QGC 自定义测试 `66/66`，软件=`pass_with_environment_gap`、硬件=`offline_pending`、现场
`hardware_link_ready=false`、人工 Fly View=`not_claimed`。为避免共享算法 hash 漏项，
`src/multidetect/simulator/ai_executor.py` 已加入 `core_algorithm` parity；canonical
`core_algorithm_hash=da5fe6ff7d1455b85c51bf975e14f2da007e603de5aed69b93470cd3a31de006`。

返航门改动落盘后又执行了一次完整总验收，最新绑定快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-gps-return-freshness-v6.json`
（SHA-256 `B43DC058C34A7ACEC7E489DEF0BFC55FD8D9A7348FB30D2F5B3F54B256F4189C`），核心测试
收集 `2188`、QGC 自定义测试 `66/66`，脚本退出码 `0`。现场三采样预检的报告结构验证通过，
但 `hardware_link_ready=false`，所以仅软件门关闭；Jetson、GR01、相机、室外 GNSS、真实
Pixhawk/执行器、HIL/REAL、人工 Fly View 与最终 `v0.3.0` 仍保持 pending。

2026-08-30 又按仓库内两个历史场景重跑了 3-D deterministic control/asset gate：

* VTOL `river_rescue_vtol_001`：
  `artifacts/evaluation/simulator-3d-control-acceptance-20260830-rerun.json`，SHA-256
  `AE338FFEB0833D6BC7B771E1D5562B923CEA21E1FD9C8FC88B2FB2A82A5A1B40`；
* fixed-wing `coastal_patrol_fixed_wing_001`：
  `artifacts/evaluation/simulator-3d-control-acceptance-fixedwing-20260830-rerun.json`，SHA-256
  `2BD93FE5B3591EE14791645E267200D92A9F79516F64A6DD91D42C4ED5051D40`。

两份 evidence 均为 `gate_passed=true`、`deterministic_gate_passed=true`，并验证
manual/AI/watchdog 共享 authority、loopback、资产哈希绑定和 `truth_free=true`。本机探针显示
`px4`/`gz` 均不可用，故 `external_runtime_gate_passed=false`；这次重跑只确认协议与资产
确定性门，不提升 Gazebo 桌面渲染、人工 Fly View、真实硬件、HIL/REAL 或实飞状态。

本轮新增 plain PX4 multirotor 场景 `urban_inspection_multirotor_001`：
`x500_mono_cam` 组合 `model://x500` 与 `model://mono_cam`，并配套独立的 runtime/truth/
expectations channel。其 deterministic 3-D asset/control gate 证据为
`artifacts/evaluation/simulator-3d-control-multirotor-continue.json`（SHA-256
`84419C67A853D6467A900CC76154EE1699052123B456C0C1D0BC66B006ABFCFE`），结果
`gate_passed=true`、`deterministic_gate_passed=true`、`PX4_SYS_AUTOSTART=4001`；
`external_runtime_gate_passed=false`，所以仍不代表外部 PX4/Gazebo 渲染或硬件验收。

2026-09-01 按当前 `v0.3.0-pre.8` 源码再次执行了三条确定性 SIM 闭环：

* `artifacts/evaluation/simulator-closed-loop-acceptance-20260901-pre.8-v2.json`（SHA-256
  `11195BBC7B8E28082A446524040A32F8D026B7B9C87164F85389771C17ECA949`）；
* `artifacts/evaluation/simulator-ai-acceptance-20260901-pre.8-v2.json`（SHA-256
  `6E0F467B2947C8E1AED0DA333D9520F324B61AF8A53EEF4EC12BE595FE1F84CE`）；
* `artifacts/evaluation/simulator-3d-control-acceptance-20260901-pre.8-v2.json`（SHA-256
  `56099CE79D65DF7D240BFC633E3479EA848053E4665F62AA0B6157F2509A82AC`）。

三份 evidence 均报告 `gate_passed=true`，确认 manual/AI 共享 authority、watchdog、truth-free
输入和 native QGC sole-UI 路径在当前源码下仍闭合；外部 PX4/Gazebo 进程、GPU/人工画面、真实
Pixhawk/Jetson/相机、室外 GNSS、HIL/REAL 仍按门定义保持 `not_claimed` 或 `offline_pending`。

同一轮 `interactive-acceptance --mode all` 覆盖 `vtol`、`fixed-wing` 和 `multirotor` 三种机型，
证据 `artifacts/evaluation/simulator-interactive-acceptance-20260901-pre.8-v2.json` 的 SHA-256 为
`24B57DAA77551CBE9970D95A7A93C5AC6209269F044285F5DD7B551467B313BE`，报告
`gate_passed=true`、`qgc_is_sole_operator_ui=true`、`shared_ai_manual_authority=true`；外部
PX4/Gazebo、GPU/人工画面和真实硬件字段仍保持未声明。

同一轮又分别重跑了三种机型的 repository-owned 3-D asset/control gate：

* VTOL `river_rescue_vtol_001`：
  `artifacts/evaluation/simulator-3d-control-acceptance-20260901-river_rescue_vtol_001-continue.json`
  （SHA-256 `1D4F90E0C631BCB42A7F9DF5BD6F9C4B7468CD37AAA891BF96AF8CF11DD64177`）；
* fixed-wing `coastal_patrol_fixed_wing_001`：
  `artifacts/evaluation/simulator-3d-control-acceptance-20260901-coastal_patrol_fixed_wing_001-continue.json`
  （SHA-256 `FB879AE4F627474B24D3397739A6E3ECC60F92891CCF671CC846414D02697982`）；
* multirotor `urban_inspection_multirotor_001`：
  `artifacts/evaluation/simulator-3d-control-acceptance-20260901-urban_inspection_multirotor_001-continue.json`
  （SHA-256 `84419C67A853D6467A900CC76154EE1699052123B456C0C1D0BC66B006ABFCFE`）。

三份报告均为 `gate_passed=true`、`deterministic_gate_passed=true`，确认三种机型都沿同一
manual/AI authority、watchdog、loopback 与资产哈希绑定路径运行；本机 `px4`/`gz` 仍不可用，
故 `external_runtime_gate_passed=false`，不宣称外部 Gazebo 画面、Pixhawk/Jetson/相机、室外
GNSS、HIL/REAL 或人工 Fly View 已验收。

### Windows Docker external runtime 与 live AI target（2026-09-01）

Docker Desktop daemon 恢复后，使用固定镜像
`px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`
分别运行 VTOL、fixed-wing、multirotor disposable PX4/Gazebo 容器。对应的
`artifacts/evaluation/simulator-runtime-preflight-20260901-continue.json`（文件 SHA-256
`0B304D5CCEE184AC25747BD85B08D2310D4A764DF6D253E0C150B9F48746D5D6`）记录
`execution_mode=docker`、`docker_daemon_ready=true`、`ready_to_attempt=true`，并明确 native
`px4`/`gz` 不在 PATH。三份 runtime evidence
为：

* VTOL：`artifacts/evaluation/simulator-docker-acceptance-20260901-continue-vtol.json`，
  SHA-256 `391BDA5D7832F08174B8260D9B1B43ADC6BB5FB3E67E82AC5EE6F1859905E16D`；
* fixed-wing：`artifacts/evaluation/simulator-docker-acceptance-20260901-continue-fixed-wing.json`，
  SHA-256 `FC20E7A241E53A7BBE82B4498D86BD06D2AA614D4AD44A1AD57464A3A11F4A22`；
* multirotor：`artifacts/evaluation/simulator-docker-acceptance-20260901-continue-multirotor.json`，
  SHA-256 `D3CA94B17C3A1461E059D078020BFA1010930257A76A18CE28FA8935040BD75F`。

三份报告均通过 `gate_passed`、`preflight_gate_passed`、`runtime_gate_passed` 和
`telemetry.qualified`，收到 PX4 `GPS_RAW_INT fix_type=3`，并在退出时 stop/rm 清理容器。
随后同一镜像以 `--execute --exercise-arm --exercise-target` 执行 live AI target acceptance：

* VTOL：`artifacts/evaluation/simulator-docker-ai-acceptance-20260901-continue-vtol.json`，
  SHA-256 `C60E759BC99E5BF9D5190CE711B9B93F219880A7A7ED2534D797E394A3CC1B57`；
* fixed-wing：`artifacts/evaluation/simulator-docker-ai-acceptance-20260901-continue-fixed-wing.json`，
  SHA-256 `99FBF23B71A94A21A9F6EB34A43D7737FEC651DAF5E3E267E837C7A0F0CB4032`；
* multirotor：`artifacts/evaluation/simulator-docker-ai-acceptance-20260901-continue-multirotor.json`，
  SHA-256 `0633659951E05C9BD5384251AE95D7D834FEE076CE782D6197CF79167A6D032A`。

这些 live evidence 都通过 `ai_policy_gate_passed=true`、`target_policy_gate_passed=true`、
`ai_steps=2`、`ai.armed_observed=true` 和 `target_exercise.accepted=true`，决策动作为
`track`，并完成 disarm/容器清理。它们验证的是“PX4 telemetry + truth-free 合成感知 →
共享 executor/authority/loopback MANUAL_CONTROL”的 SIM 子门；不提升相机视觉精度、QGC
人工画面、真实 Pixhawk/Jetson/GNSS、执行器、HIL/REAL 或实飞。版本仍为 `v0.3.0-pre.8`。

为把上述六份文件从“并列通过”收口为可审计的单一证据，新增
`simulator_external_evidence.py`/`verify_simulator_external_evidence.py` join 点。它不重新启动
容器，也不从文件名猜测结果，而是读取每个 JSON 的显式 gate、场景 manifest、GPS 质量、
truth-free/hardware 边界、AI target/armed/decision 和 cleanup，重新计算文件 SHA-256，并绑定
当前 parity 五项哈希。三机型当前 join 输出为
`artifacts/evaluation/simulator-external-runtime-ai-evidence-20260901-pre.8.json`（SHA-256
`43D34D1E7697CDB950816B9C1057104EB4F90820C60AF1A5163B081174BC3632`，`gate_passed=true`）。
该输出是 SIM external runtime/AI policy 的机器子门，`truth_free`、`operator_visual_acceptance`
和 `real_hardware` 的边界仍显式保留为合成/未声明；它不关闭室外 GNSS、人工 QGC、真实
Pixhawk/Jetson/相机、执行器、HIL/REAL 或最终 `v0.3.0` 发布门。

### Windows combined SIM 最新外部重跑（2026-09-01）

在当前 `v0.3.0-pre.8` portable 构建上完成了最新一轮 900 帧 Docker PX4/Gazebo/QGC
联合验收：

```text
artifacts/evaluation/combined-sim-qgc-20260901T074256Z-48728/combined-evidence.json
bytes=772,736
sha256=4A50E88770FF1B2084A92D72E228C4C1913E49E69BAAFE46BA43B742939D774F
gate_passed=true; sensor topics ready; VTOL MC→FW→MC; Mission active; cleanup=true
```

本次使用九个 `30000..60000` 动态 loopback host 端口，完成传感器流、原生 QGC Fly View
toolbar/video machine smoke、7 项 INT-only Mission upload/readback/active、Mission-ready
后 type-24 barrier、VTOL 转换和 Docker stop/rm。独立 33/33 verifier 输出为
`artifacts/evaluation/combined-sim-qgc-20260901T074256Z-48728/combined-evidence-verification.json`
（SHA-256 `B2855FD4C7B64DA178676A00B01EFB3E05969681ED01B9DE597D130883D1FCD7`）。新 SIM
envelope 为
`artifacts/evaluation/release-mode-sim-20260901-pre.8-combined-dynamic-ports-v2.json`
（SHA-256 `F608493C7F1385D070B8CCAE903444F4028B78FF21AB30152C9C05958BC8ABC0`）。

该证据已绑定到
`artifacts/evaluation/multidetect-goal-acceptance-20260901-combined-qgc-v3.json`
（SHA-256 `64E92ADB8B99033520EE27703BAC442505828E1EA12B3E56D211864C66EC7882`）和
`artifacts/evaluation/release-gate-pre.8-current-20260901-combined-qgc-v3.json`
（SHA-256 `8B78B49D7F857051FB171C5A64C2DC6D920990B926004291A7472C9745F0B1D7`）。两份
聚合结果均保持 `software-only`；真实 Pixhawk/Jetson/相机、室外 GNSS、执行器、HIL/REAL、
人工 Fly View 和最终 `v0.3.0` release gate 仍需现场证据。

### Windows combined SIM 的动态端口与有界清理（历史说明，2026-09-01）

在上述 external runtime/AI 子门之后，使用当前 `v0.3.0-pre.8` 源码再次执行完整的
PX4/Gazebo/QGC combined Mission gate。`run_px4_gazebo_qgc_combined_sim_acceptance.ps1`
不再把 host 端口固定为一组容易碰撞的常量，而是在 `30000..60000` 内随机选择九个互不重复的
端口；每个候选先同时 bind IPv4 TCP/UDP loopback，再排除容器内 PX4 输入 `18570` 和受保护
地面站 UDP `14550`。容器内部端口和生产端点不随本次选择改变，端口列表、释放状态和保护端口
前后快照全部写入 combined evidence。Docker inspect stop/rm 路径设置 `20 s` 超时，Docker
Desktop API 卡顿时 wrapper 会有界退出并保留诊断，而不是无限等待。

运行命令：

```powershell
Set-Location D:\Projects\Multi-Detect
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\run_px4_gazebo_qgc_combined_sim_acceptance.ps1 `
  -Frames 900 -RouterDurationSeconds 420 -MissionTimeoutSeconds 240 `
  -MissionSetupAllowanceSeconds 120 -FixedWingHoldSeconds 12 `
  -MissionCaptureSequenceAdvance 5
```

独立复核可直接把排序稳定的 JSON 报告写入同一 run 目录：

```powershell
$env:PYTHONPATH = 'src;.'
python scripts\verify_combined_sim_evidence.py `
  artifacts\evaluation\combined-sim-qgc-20260901T051239Z-76220\combined-evidence.json `
  --expected-release-label 0.3.0-pre.8 `
  --release-mode-evidence artifacts\evaluation\release-mode-sim-20260901-pre.8-combined-dynamic-ports.json `
  --require-release-mode `
  --out artifacts\evaluation\combined-sim-qgc-20260901T051239Z-76220\combined-evidence-verification.json
```

当前源码版本还会为联合 Mission helper 传入 `--mission-journal`，并把 journal 的路径、字节数、
SHA-256 和恢复/终态摘要写入 combined evidence；独立 verifier 在新 artifact 上会再次读取并
校验 JSONL hash-chain 与完整请求序列。上面的历史 run 生成于该字段接入前，仍按原 33 项
契约记录，下一次真正的 Docker/SIM 重跑才会产生带 journal 的新证据。

最新 run 为：

```text
artifacts/evaluation/combined-sim-qgc-20260901T051239Z-76220/combined-evidence.json
bytes=772,886
sha256=4DE2F6AD236EBE65F97CD9A279A101453FA9C938D45E25B9A0FB2DF0D3B7F4F1
gate_passed=true; frames=900; Docker Server=28.4.0
VTOL attach + MC→FW→MC + 7-item INT-only Mission + QGC barrier + cleanup=passed
```

本轮实际 host 端口为
`38392, 32511, 46147, 30192, 55621, 30275, 49253, 53375, 31727`，
证据中的 `all_test_ports_released=true` 和 `protected_udp_14550_unchanged=true` 均为真。
SIM release-mode envelope 为
`artifacts/evaluation/release-mode-sim-20260901-pre.8-combined-dynamic-ports.json`
（SHA-256 `86B6A7B4E220D7E05C8C9E56A678B22B269587AB758D05DE8BBEE855644FD981`）；E7
动态端口/S4 绑定快照和报告分别为
`artifacts/evaluation/release-gate-pre.8-current-20260901-combined-dynamic-ports-s4-bound.json`
（SHA-256 `08C3B431C57329855051BDF3D5FF6FD8F8DBE54F8D09E474EB36563ADEF2B13E`）和
`artifacts/evaluation/release-gate-pre.8-current-20260901-combined-dynamic-ports-s4-bound-report.json`
（SHA-256 `5A5D7E0435F8906E692ADB3A44D6D7A972A5CCF381AC960B0E92DB3D7B123298`）。
聚合报告确认 SIM source/S4 子门通过，但整体 E7 仍因 `hardware_link_ready=false`、REAL
现场证据、人工 Fly View 与最终 release channel 未满足而保持关闭；这次 run 仍是
software-only disposable SIM，不升级为真实 Pixhawk/Jetson/室外 GNSS/HIL/实飞。

本轮同时加入了独立的 Combined SIM 证据契约复核器：
`src/multidetect/simulator_combined_evidence.py`（CLI：
`multidetect-sim-combined-evidence`，脚本入口：
`scripts/verify_combined_sim_evidence.py`）。它会重新读取并校验 source evidence，拒绝
固定端口、端口重叠、受保护 UDP `14550` 被占用、容器生命周期不完整、伪造硬件/人工视觉
结论和 release-mode 文件错绑等情况；不会信任 source JSON 中未经重算的 `gate_passed`。
本次验证输出为
`artifacts/evaluation/combined-sim-qgc-20260901T051239Z-76220/combined-evidence-verification.json`
（SHA-256 `E8440B7B4DDD5981A1A8B7FD279CF210AE016E1858D008B9DE8D5DD507BFFCA2`，
33/33 checks 为真）。E7 清单现在可通过
`--simulator-combined-evidence` 与
`--simulator-combined-release-mode-evidence` 写入 hash-bound 的
`simulator_combined` 记录；当前 v2 清单/报告分别为
`artifacts/evaluation/release-gate-pre.8-current-20260901-combined-dynamic-ports-s4-bound-v2.json`
（SHA-256 `C026AE1A3209BFE4798DDD600A12EBC4F1684DCE49E871DF5F28D80253D5A188`）和
`artifacts/evaluation/release-gate-pre.8-current-20260901-combined-dynamic-ports-s4-bound-v2-report.json`
（SHA-256 `47222082B48FFB8E20AB1264A78CC4C25F6FFBBD9F0321AF17C18E420A65F0FD`）。
报告中的 `simulator_combined.passed=true` 只关闭 Combined SIM/S4 子门；它仍不改变
`operator_visual_acceptance=not_claimed`、真实硬件链路、室外 GNSS、HIL/REAL 或最终
`v0.3.0` 发布门。

## 7. 开发阶段和验收门

### S1：模拟器最小闭环（编排器与确定性 fixture 已完成）

已完成进程编排骨架、SIM profile deny、scenario hash/seed、loopback 端点校验、控制权仲裁、
job 证据目录、PX4/Gazebo/QGC 生命周期管理，以及 native QGC↔PX4 interactive router、
deterministic AI executor、`DeterministicFlightModel`、`SimulatorClosedLoop` 和 3-D
asset/control gate（多旋翼 `urban_inspection_multirotor_001`、VTOL `river_rescue_vtol_001` 与
fixed-wing `coastal_patrol_fixed_wing_001`），
`sim_flight` bounded QGC command/mission allow-list 与 `stack-plan` 预检也已落地。
job-local `px4-rc.mavlink`/wrapper 和 `external-plan --px4-launch` 已补上 stock PX4 默认端口与
规划端口之间的启动缺口，并在真实启动前提供 strict readiness/hash gate；当前 native desktop
仍缺 PX4/Gazebo，故就 `px4_launch.py` wrapper/native 外部链而言，这部分只达到
planner/测试证据，尚未产生新的 wrapper-backed 3-D runtime heartbeat/telemetry 结论。
为避免把 PATH 上的客户端误写成可运行环境，`python -m multidetect.simulator runtime-preflight
--out <evidence.json>` 现在会一次性记录 native PX4/Gazebo、Docker daemon 和 Windows WSL 的
只读状态，并写入 `ready_to_attempt`、`execution_mode` 及 `process_started=false`/
`telemetry_observed=false` 等声明边界；当前台式机证据为
`artifacts/evaluation/simulator-runtime-preflight-20260901-pre.8.json`（文件 SHA-256
`E973A4F3B93B255F61D8F600E4D90F8EE251D9D106F010D83A12BB93538506C3`；内嵌 canonical digest
`bef94984df6580a3caf062ee503697aceca2fb0f5924442c17fa8d643f0e7c9a`），daemon 探针超时，
所以仍是 `ready_to_attempt=false`。
在 terminal continuation v10 中，预检又增加了显式的场景资产/启动前置绑定：传入
`--repository-root`、`--scenario`、`--px4-workdir` 后，会在不创建目录、不启动外部进程的前提下
重算 scenario manifest、世界/模型文件和 stack-plan SHA-256，并单独输出
`asset_preflight`、`px4_workdir_preflight` 与 `external_runtime`。后者把
`ready_to_attempt`（可尝试条件）和 `gate_passed`/`external_runtime_gate_passed`（已观察到的
外部运行结果）分开；预检阶段后两个字段固定为 `false`。确定性 flight-model 回退带有
`automatic=false/used=false`，上层必须显式记录回退，不能因工具缺失而静默改变运行时语义。
当前场景绑定快照为
`artifacts/evaluation/terminal-continuation-v10/runtime-preflight-scenario.json`（7,188 bytes，
SHA-256 `1E243FBA41273CA94FBB6B349573FB4206B4A26CEED5C55778CF545A3A2590E1`）。同轮随后以固定
digest 的 Docker `px4io/px4-sitl-gazebo` 实际启动 PX4/Gazebo，跑通 2 个 AI policy steps、
qualified telemetry、目标策略和 cleanup；证据为
`artifacts/evaluation/terminal-continuation-v10/simulator-docker-ai-acceptance-external.json`
（114,687 bytes，SHA-256 `DF58B68B40D34683DFE1309A4A45965FCD26EC40887B7077DBBA6BB65752C1FF`）。
这只关闭 software-only Docker/SITL 子门，不改变 QGC 人工视觉、真实硬件、室外 GNSS、HIL/REAL
或最终版本门。
当 Docker client 存在而 daemon 探针失败时，证据还会带有限的 `docker_diagnostics` 节点：它只读取
backend 日志尾部的错误标记并给出归因/建议动作，不启动、重启或清理 Docker，也不会把整份日志复制到
evidence。Windows CLI stdout 使用 JSON ASCII 转义，避免主机 GBK 代码页因日志中的 Unicode 字符导致
预检命令异常退出。当前台式机的诊断归因是陈旧 `dockerInference` reparse/socket 条目阻塞 backend；
这仍然是环境缺口，不构成外部 3-D、PX4 telemetry 或硬件验收。
为使现场恢复步骤可重复，仓库新增
`scripts/recover_docker_desktop_stale_socket.ps1`：默认只读盘点当前账户的
`dockerInference` reparse-point，`-Apply` 优先移动到时间戳备份目录，只有显式
`-RemoveStaleEntry` 才允许精确 `fsutil` 清理，`-RestartDocker` 仅在处理成功后请求启动
Docker Desktop。脚本不碰其它 Docker 数据；处理后仍必须重新生成 runtime-preflight，并由
`ready_to_attempt=true` 驱动外部 PX4/Gazebo gate。
组合入口 `run_px4_gazebo_qgc_combined_sim_acceptance.ps1` 现在在任何 Docker `info`、镜像拉取或
容器操作之前调用这份预检，并将 JSON 写入本次组合验收目录；未就绪时 20 秒内 fail-fast 并保留
诊断，而不是继续等待无响应的 Docker server。恢复后沿用原有 digest、端口隔离、QGC Fly View
和 PX4 Mission 路径，不需要另建 simulator UI。
该入口同时兼容 Windows PowerShell 5.1：当 .NET Framework 没有
`ProcessStartInfo.ArgumentList` 时，使用 `CommandLineToArgvW` 规则构造 quoted `Arguments`，保证
带空格路径和多行 `sh -c` 启动载荷不被 PowerShell 展平。当前环境烟测已在约 4 秒内留下预检证据并
fail-fast，未创建容器或 QGC 进程。
Docker Desktop 的 VTOL 与 fixed-wing external telemetry gates 已另行通过（见 §6.3）；它们
证明 container/PX4/Gazebo/telemetry machine 层，不替代 wrapper-backed native 运行或桌面/QGC 视觉。
`run_simulator_closed_loop_acceptance.py` 保留 VTOL/fixed-wing 历史双模式、AI
hover/track、人工接管/恢复、watchdog/failsafe；`run_simulator_interactive_acceptance.py`
再覆盖 native QGC MANUAL_CONTROL/telemetry、只读转发、blocked-write、identity 和
watchdog 子门；`run_simulator_3d_control_acceptance.py` 已固定河道场景、WGS-84 world 和
standard VTOL camera model，并把 interactive 结果写入 `interactive_qgc_path`、
`claims.native_qgc_interactive_path`；`interactive-acceptance --mode all` 再把同一子门扩展到
多旋翼。VTOL、fixed-wing 和多旋翼资产/协议 gate 均可独立运行。PX4 heartbeat、Gazebo
camera/NavSat、QGC RTSP/metadata 与 1 个目标的 60/600 帧**wrapper-backed 真实外部进程闭环**，
待安装并运行 native POSIX 工具链后执行；Docker telemetry gates 已通过但不含这些同帧视觉/
QGC/目标闭环；deterministic fixture 不能写成人工/硬件验收。

### S2：三类飞行器（确定性模型与三机型 Mission compiler 已覆盖，外部模型待验收）

`DeterministicFlightModel` 已提供 `multirotor`、`vtol` 与 `fixed-wing` 三种 bounded kinematic 路径；
上层通过 `compile_executable_mission(vehicle_class=...)` 统一 dispatch，底层仍由
`compile_multirotor_executable_mission(...)`、`compile_vtol_executable_mission(...)` 与
`compile_fixed_wing_executable_mission(...)` 已将三种机型的 runtime 几何/平台计划编译为
hash-bound `MISSION_ITEM_INT`；多旋翼/VTOL 共享 geofence、相对海拔、盘旋和落点门，fixed-wing
额外执行能量 reserve、最小转弯半径、landing approach/glide slope、航向和 landing 风场检查。
同一场景契约后续仍需在 digest
固定的 standard VTOL、fixed-wing SIH 和垂起转场 PX4/Gazebo 模型上复跑。只允许模型、动力学
和 PX4 参数不同，上层 Mission、感知、authority、审计和 QGC UI 不得分叉。

### S3：真实算法压力

加入风、遮挡、GNSS 质量变化、相机 dropout、目标漂移和时间戳 skew，验证距离/定位退化策略。
当前确定性 `ScenarioRunner` 已将 `timestamp_skew`、`target_drift`（径向 m/s）和
`target_bearing_drift`（相对方位 deg/s）接入 observation 层：不改变共享仿真时钟、不加载
truth channel，负时间偏移在跨过 t=0 后转为 unavailable，正偏移形成 future cursor，统一由
GPS freshness/return safety 门 fail-closed；距离在 0 m 饱和，方位归一到 `[-180, 180)`。故障
事件的 `active_frames`、开始时间和幅值进入运行证据，可用固定 seed 重放，且 manual/AI 仍
经过同一 authority、watchdog、MAVLink adapter 路径。实现、schema 与回归分别见
`src/multidetect/simulation/faults.py`、`src/multidetect/simulator/scenario_runner.py`、
`configs/schemas/scenario-runtime-input.schema.json` 和
`tests/test_simulator_scenario_runner.py`。

场景矩阵的 `--cover-faults` 模式进一步把故障时间轴变成机器门：它根据所有选中 runtime
channel 的最后一个事件自动扩展共同 frame budget（无限时长事件使用显式 tail），并在
`fault_coverage` 中同时核对 `active_frames` 与 `fired`。有限时长故障的 `fired` 现在会在
第一次实际观测变换时置真；任何声明事件未被观察到时，矩阵和 goal acceptance 都保持失败。
这保证 nominal-only 的短跑不会掩盖 GPS dropout、相机丢帧、风突变或电池阶跃等已声明故障。

### S4：任务和安全

验证 PX4 Mission transaction、LCK/TRK/TGT 状态机、取消后不残留方框、未授权命令为零、
围栏违规为零、假定位为零。

当前 pre.8 已把这组门收口成可重算的 deterministic SIM 证据：
`src/multidetect/simulator/safety_evidence.py` 会沿用生产的
`UnifiedSelectionTargetPool`、`SimulatorMavlinkControlAdapter`、`SafetyRuleEngine` 和
`MissionStateMachine`，不另造一套 simulator-only 规则。一次运行覆盖以下断言：

* LCK → TRK → `CANCEL_TRK` 只移除选中的目标；随后全量取消会原子清空 active/tracked/locked/
  primary/evidence，必须经过新的 SELECT 才允许重新捕获，解决“第一次取消后方框残留”的回归；
* QGC/PX4 datagram 只接受 literal loopback、严格 system/component/target/message allowlist，
  Mission 写入和飞行指令写入保持关闭；AI watchdog 触发后进入 `FAILSAFE`、发送唯一中立帧，
  之后的 AI 输出不会再次进入 adapter；
* `SafetyRuleEngine` 对 allowed-zone、geofence、position unhealthy 全部 fail-closed，WGS-84
  极点/越界坐标不生成假目标坐标；终态 `TERMINATED` 后所有后续 Mission event 都被拒绝。

运行和复核命令：

```powershell
Set-Location D:\Projects\Multi-Detect
$env:PYTHONPATH = 'src;.'
python scripts\run_simulator_safety_acceptance.py `
  --out artifacts\evaluation\simulator-safety-acceptance-latest.json
python scripts\verify_simulator_safety_evidence.py `
  artifacts\evaluation\simulator-safety-acceptance-latest.json
```

机器契约为 [`configs/schemas/simulator-safety-evidence.schema.json`](../configs/schemas/simulator-safety-evidence.schema.json)。
当前 artifact 的顶层事件是 `simulator_safety_acceptance_finished`，`runtime_mode=sim`，
`truth_free=true`；`hardware_acceptance`、`operator_visual_acceptance`、Pixhawk/Jetson/相机和
真实飞行字段均明确为 `not_claimed`。这组 S4 软件门通过不等同于外部 PX4/Gazebo、人工 QGC 画面、
真实 GNSS 或 HIL/REAL 通过。

### S5：可视化和人工点验

只在唯一 QGC Fly View 窗口检查 toolbar、地图、PiP、视频、状态抽屉、目标卡片和 LCK/TRK
一次点击语义；机器截图和人工视觉结论分开记录。

### S6：回归和发布

每次 prerelease 必须通过 Python/C++/QML 静态门、确定性重复运行、portable EXE smoke、
SIM/HIL/REAL 共享 UI/算法/契约检查。所有门完成前版本保持 `v0.3.0-pre.N`。

## 8. 证据包

每次模拟运行输出到 `artifacts/evaluation/<job-id>/`：

```text
manifest.json             # scenario/runtime/profile identity
job.json                  # CLI options, isolated endpoints, throttle wire mode
px4-launch.json           # job-local rc/wrapper、source/generated hashes、readiness
external-stack.json       # optional no-launch argv/cwd/env/log plan
processes.json            # PID、命令摘要、退出码
transport.json            # frame/telemetry/RTSP/metadata counts
algorithm.json            # detector/tracker/range/geolocation identities
mission.json              # PX4 transaction and terminal state
faults.json               # injected events and observed recovery
qgc-ui.json               # native surface and geometry machine evidence
interactive-router.json   # QGC/PX4 identity, forwarding/blocked counters, adapter evidence
screenshots/              # optional operator-visible captures
combined-evidence.json    # gate summary and SHA-256s
```

独立 deterministic fixture 的输出文件名可自定义；其顶层事件分别为
`simulator_closed_loop_acceptance_finished`、`simulator_interactive_acceptance_finished` 和
`simulator_3d_control_acceptance_finished`。interactive fixture 的 `modes[*].router` 保存
QGC/PX4 identity、forwarded/blocked/fragment-reset 计数和 adapter evidence；3-D gate
则将完整 interactive 结果嵌套到 `interactive_qgc_path`，并在
`claims.native_qgc_interactive_path` 记录子门结果。closed-loop event 在
`modes[*].evidence.flight_model` 中保存 flight-model state/hash；3-D event 在
`three_d_runtime` 中保存 world/model/scenario hash 与 WGS-84/physics 检查结果。它们可以
作为 `artifacts/evaluation/` 的 machine evidence 合并到更大的 combined 包，但不能替代
`processes.json` 中的 PX4/Gazebo PID、QGC RTSP 同帧 ACK、桌面人工视觉或硬件验收字段。

证据必须区分 `machine_passed`、`operator_visual_acceptance`、`qml_rendering_proved`、
`hardware_acceptance` 和 `flight_acceptance`，不能用一个截图或一个 ACK 代替全部验收。

## 9. 当前仓库到目标状态的差距

当前已有：`RuntimeMode.SIM`、`RuntimeProfile.sim()`、`GazeboSimBackend`、多旋翼城市巡检/河道 SDF、
scenario channels、确定性 clock、`DeterministicFlightModel`、`SimulatorClosedLoop`、
manual/AI 共用 adapter、interactive router、`compile_fixed_wing_executable_mission`、
3-D asset/control gate、S4 safety evidence/verifier、job-local PX4 launch wrapper、external
process plan、integrated SIM 和原生 QGC Fly View 接入。

截至 2026-09-03，下一步实现顺序已按实际证据前移：本轮新增
`scripts/inspect_field_link_interfaces.ps1` 作为迁移后只读网卡诊断，当前快照
`artifacts/evaluation/field-link-interface-diagnostic-20260903.json` 的
`selection.status=missing` 明确记录活动 `Ethernet=192.168.1.13/24` 与目标
`192.168.144.0/24` 不匹配；直接全量 Python pytest 已收集 `2546` 项并退出 `0`，但
REPLAY 正向分层、现场硬件、HIL/REAL 和人工 Fly View 仍未通过。后续顺序如下：

1. 台式机 Docker external PX4/Gazebo/QGC 的 900 帧、VTOL 转场、INT-only Mission、QGC
   barrier、动态端口、Mission journal、AI `TRACK` 和严格 source verifier 已通过；对应
   最新总验收为 `artifacts/evaluation/multidetect-goal-acceptance-20260902-fault-coverage-v6.json`；
   v6 已绑定当天现场只读预检，但 `hardware_link_ready=false`，所以仍只关闭软件/SIM 子门。
   native POSIX wrapper-backed 运行仍需在具备 `px4`/`gz` 的环境中补 heartbeat、RTSP 和
   QGC 同帧证据，不能用当前 Docker 结果替代。
2. 接通现场 Ethernet 后，使用同一 `run_field_link_preflight.py` 复核 Jetson SSH、GR01
   MAVLink、相机 RTSP 和 metadata UDP；现场报告必须连续满足 `hardware_link_ready=true`，
   再把真实 `GPS_RAW_INT`、姿态、测距、目标 ID 和执行器响应绑定到同一事件回放。
3. 在室外已测真值距离上完成分段深度标定（固定翼/VTOL/多旋翼分别覆盖高度、俯仰、光照和
   目标尺寸），输出目标距离误差、GPS 误差椭圆和 95% 置信区间；任何未标尺单目流继续
   保持 `DEGRADED`/不可定位，不写入假坐标。
4. 用真实视频在 QGC 原生 Fly View 完成 DET → TRK → LCK → TGT → 取消 → 重捕获，重点
   验证一次点击取消不残留旧框、目标 ID 不串线、旧 GPS/RANGE freshness 不复活，并单独
   留存人工视觉验收证据。
5. 在推进器断开条件下完成 HIL/REAL 的 servo 方向/行程、失锁恢复、watchdog neutral 和
   Mission journal 掉电恢复；之后才允许真实执行器、实飞和最终 release gate 参与判定。
6. 每轮只递增 `v0.3.0-pre.N`，同步更新 README、CHANGELOG、版本清单和带哈希证据；所有
   SIM/HIL/REAL、人工 Fly View、室外 GNSS 和目标精度门完成前，最终 `v0.3.0` 保持未发布。
