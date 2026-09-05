# Multi-Detect 当前进度

最后更新：2026-09-06（v70 工作树）

GitHub 审计基线：`origin/main`，提交 `b5c3f10baf4cc9febaa3a10103752f202f3b0611`；本轮工作基线为
本地 `main` 提交 `c297a9285a014de394c5bf42e6098ff43b20a295`（当前文档提交前领先远端 29 个提交）。

产品版本：`v0.3.0-pre.8`（最终 `v0.3.0` 尚未发布）

机器可读镜像：[current-progress.json](current-progress.json)

## 结论先行

v70 已把原生 QGC 的真实目标点击纳入同一个 PX4/Gazebo/QGC Combined SIM 生命周期：机器按
QML 候选框几何映射到 exact PID/HWND，发送一次 `SELECT_TRK`，验证 ACK 与后续 TRACKING 状态均与
同一 command/target 相关，再让该 operator target 驱动共享测距和目标定位。相机安装俯角同步修正为
35°，GPS/DEM/相机地面射线链恢复可观测区域；测距 contribution wire 权重改为总预算量化，避免极小
VIO 权重与主来源四舍五入后总和超过 1 而间歇性拒绝 packet。当前 parity 的权威 900 帧 run 中，
831 份 range 和 831 份 geolocation status 全部发出，112 帧合格落图、719 帧明确不可定位、0 transport
error，Combined、VTOL attached 与 7-item Mission 均通过。

当前仓库已经形成统一的感知、跟踪、测距、目标落图、QGC、数字孪生、任务规划、PX4 Mission、
operator link、安全和证据框架。v59 已让三组各 5 帧的实际 Gazebo BGR24 像素经过同一候选 ONNX、
共享语义地面锚点、DEM/GNSS 测距和 WGS-84 投影；三个场景覆盖约 `6.20/12.17/20.15 m` AGL、
`8/15/25 m` 目标距离和 `0/4.00/7.97 deg` 坡度，均由独立 verifier 重现。对应近地平线输入 5/5 帧
都拒绝生成坐标。v60 又把 high-slope evidence 的测距、坐标和 1σ 经签名 operator link 送入现有
原生 QGC。v61 在同一 HIL 进程内持续刷新 type 10/22，按 exact PID/HWND 采集无外部遮挡的目标地图
与证据卡片机器截图，并把证据与当前五组 release parity 严格绑定。v62 又让固定翼、多旋翼和 VTOL
通过同一 AI/MAVLink 路径驱动 disposable PX4/Gazebo，以 `GLOBAL_POSITION_INT` 证明实际位移，并将
可重放的三机型矩阵正式接入 E7。v63 又完成每机型两次正常响应与三种显式故障，共 15 份外部
PX4/Gazebo 源报告，并把 repeatability/fault campaign 接入 E7。v64 新增 source-bound QGC 人工视觉
review 合同、12 项 checklist、0 假点负控和独立 verifier。v65 已把它接入 E7，并强制要求
REPLAY/SIM/HIL/REAL 各自独立的机器来源与人工记录；当前仍是 0/4，人工签字和现场证据未完成。
v66 进一步把机器捕获抽成四模式共用的 schema-v2 契约，绑定实际 EXE、parity、run、exact PID/client
rect、遮挡和 PNG；历史 PX4 报告仅保留 HIL 兼容，REPLAY/SIM/REAL 不再伪装成 HIL 格式。v67 修正
Gazebo Harmonic 的传感器时间语义：CameraInfo 是会话级静态标定，NavSat/Odometry 仍受 1.5 秒动态门；
短暂 transport callback 错序只丢弃不安全图像。v68 又把实际 CameraInfo K/P/R/D、PLUMB_BOB 与仓库
标定逐字段绑定，并将语义状态带入 runtime/frame-set。v69 再由独立 verifier 强制该状态、sensor 完整
计数与逐帧 odometry。当前权威 900 帧联合 SIM 安全丢弃 6 张不新鲜动态图像，并以增强 verifier 44/44
通过；900 个有效帧全部取得 QGC post-adoption type-24 ACK。人工记录仍为 0/4。

最终版本仍然阻断。当前 E7 report-only 结果为 `gate_passed=false`、`release_allowed=false`，共
32 条 blocker，主要来自部署域数据/模型精度、现场链路、四模式人工 QGC/Mission、HIL/REAL、
三机型硬件控制和 prerelease 版本面。候选 YOLO 模型仍为 `quarantined`，不是生产批准模型。

## v70 本轮已收口

- 原生 QGC Fly View 候选目标支持 source-bound 点击，`SELECT_TRK` ACK、TRACKING 状态和 QML
  `TRK` adoption 必须属于同一 command/target；取消仍保持单击状态机语义。
- active operator target 进入共享 multimodal ranging；GPS/测距合格时发布 WGS-84/1σ，任何质量门
  不合格时发布明确不可定位状态，不落假点，也不产生飞控或载荷指令。
- Gazebo VTOL 相机与标定统一为 35° 俯角；range contribution 使用 254 单位总预算量化，保留每个
  非零来源且保证解码权重总和不超过 1。主来源约 0.99875、VIO 约 0.00125 的回归已覆盖。
- 当前 parity 真实 source `combined-sim-qgc-20260905T172856Z-51520`：258,942 bytes，SHA-256
  `FE45B4DCAAD69AEFA7871609A58D33723547C1B46B5A708DBF1A00E495DC59E0`；900/900 frame/type23/type24，
  831/831 range/geolocation status，112 个合格坐标，0 transport error，最大端到端 ACK 609 ms。
- 原生 QGC 点击第一次命中，相关目标 `flame` 进入 TRK；native QQuickItem pixels、transport-bound
  visible surface 和 toolbar smoke 均通过。VTOL Mission 最大离家 `134.918 m`、相对高度 `24.116 m`，
  MC/FW/MC、land/disarm 完整；容器移除、9 个测试端口释放、14550 不变。
- portable EXE 为 46,579,712 bytes，SHA-256
  `FFCC4FCF45EAE6E7A52ED6B2585BCCF29E5F36776D03D2EFA7F6527D1A96255B`。
- 当前 HEAD 全仓 3,317 tests：`3316 passed, 1 skipped, 0 failed, 0 errors`；JUnit 482,309 bytes，
  SHA-256 `67CBBE1C25256FA4619B375819748BFD9605CD4A2BC8F898CB7D7042DF5DE203`；Ruff 全仓通过。
- 以上仍是 dirty-worktree、current-parity 的 software/SIM 机器证据，不声明人工视觉、真实硬件、
  室外 GNSS/RTK、HIL/REAL 或实飞；最终 `v0.3.0` 继续阻断。

## v69 本轮已收口

- independent combined verifier 新增 `camera_info_intrinsics_semantics_validated`、
  `sensor_frames_complete`、`odometry_frames_complete` 三个硬检查；旧 pending/missing/tampered 结构均失败。
- combined runner 在写最终 source 前也强制 exact CameraInfo semantic status，防止 runner 根门掩盖传感器
  语义缺失；E7 fixture 已升级到同一契约。
- 当前权威 source `combined-sim-qgc-20260905T152520Z-75420` 来自干净 `46920e1`：900/900 sensor、
  RTSP、type-23、post-adoption type-24、full odometry；6 张 stale dynamic 图像被拒绝，0 张旧帧发出。
- source 255,256 bytes / SHA-256
  `9CAB1BC405571C2F287D1545D77B7EA1F7C4A56CC163AB76E1402CB362DB121B`；增强 verifier 2,139 bytes /
  SHA-256 `1EEFD154DF164F307D62EF1CFFCC7479E343B8E3FC6A7017A9BBE61EB19502D0`，44/44 checks 通过。
- Mission 最大水平距离 `130.616 m`、最大相对高度 `23.857 m`；PiP 第 1 次精确命中；容器移除、
  9 个动态端口释放、14550 不变。
- 当前 HEAD 全仓 3,309 tests：`3308 passed, 1 skipped, 0 failed, 0 errors`；JUnit 481,013 bytes、
  SHA-256 `3AF69527D05CCC985502C195D09DDE44793D7280111B04410B8B2DCB3C3661C6`；ruff 全仓通过。
- 最终版本仍阻断：四模式人工视觉 0/4、HIL/REAL、真实硬件、室外 GNSS/RTK 和实飞均未完成。

## v68 本轮已收口

- `gazebo_sensor_stream.py` 对实际 `CameraInfo` 的 width/height、K/P/R、distortion model 与 D 执行
  fail-closed 语义验证；标准 pinhole 零位、R identity、零 stereo translation 与仓库标定任一不符即失败。
- `GazeboSimBackend`、CLI、standalone/combined runner 均要求显式 camera calibration。canonical 标定 hash
  进入 sensor binding identity，验证状态进入 runtime result 与 persisted frame-set source attribution。
- 真实三帧诊断 `gazebo-camera-semantics-20260905T150106Z.json` 证明 K/P/R/D；Gazebo float 序列化的
  最大焦距差约 `3.53e-5 px`，故 intrinsic 使用 `1e-4 px` 明确容差，R/D 保持 `1e-12`。
- QGC PiP 原生点击增加最多 8 次的有界精确重试；每次仍同时验证坐标、QGC PID/HWND ownership 和
  foreground。本轮真实运行 `pointer_position_attempts=1`，不是靠放宽 ownership 通过。
- 权威 source `combined-sim-qgc-20260905T150510Z-27780`：900/900 sensor、RTSP、signed type-23、
  post-adoption type-24 和 full odometry；7 张 stale dynamic 图像被拒绝，0 张 stale frame 被发出。
- source 256,194 bytes / SHA-256
  `E800968AB63BF847CA53BE7798EAD3B2501B731E958D76AB5E8EAC447A999471`；独立 verifier 41/41，
  verifier SHA-256 `3D58600C9B57ED981C651DE79CCA90F0FED275096E9FD5CA416B104016E51078`。
- 7-item Mission 完成 upload/readback/commit/START_PERMITTED 和 VTOL 全生命周期；最大水平距离
  `129.057 m`、最大相对高度 `22.082 m`。容器移除、9 个动态端口释放、14550 ownership 不变。
- 当前 HEAD 全仓 3,306 tests：`3305 passed, 1 skipped, 0 failed, 0 errors`；JUnit 480,349 bytes、
  SHA-256 `0414978C7C13B2726CCA9CB94153F018D5AC16B8A399748255E52F9C08DA64AA`；ruff 全仓通过。
- CameraInfo K/P/R/D 机器语义子门已关闭；目标测距/落图并未在本次 900 帧联合 run 中重新配置。
  四模式人工视觉仍为 0/4，HIL/REAL、硬件、室外 GNSS/RTK 和实飞继续阻断最终 `v0.3.0`。

## v67 本轮已收口

- `CameraInfo` 保持正时间戳、因果性和逐帧 width/height 绑定，但按 Harmonic 实际行为视为会话静态标定；
  NavSat/Odometry 的 1.5 秒动态时效限制没有放宽。
- 独立 worker 短暂错序时丢弃无法绑定新鲜动态遥测的 image；若 gap 持续，helper 仍由统一 deadline
  fail-closed。新增计数分别记录 stale dynamic/NavSat/Odometry rejection。
- 中间失败 source `combined-sim-qgc-20260905T141805Z-19928` 在 532 emitted frames 暴露 NavSat
  `7,000,000,000 ns` 落后，门正确失败、容器和端口清理完成；它保留为根因 provenance。
- 最终 source `combined-sim-qgc-20260905T142324Z-32372`：900/900 sensor、RTSP、signed type-23、
  post-adoption type-24；4 张 stale dynamic 图像安全丢弃，VTOL attached、7-item Mission、QGC toolbar、
  router 和 cleanup 全部通过。
- source 255,480 bytes / SHA-256
  `FD386165212A68C6E2D053A0D58B05D796565AB716CB088439F0A6756EDC1090`；独立 verifier 2,006 bytes /
  SHA-256 `C195FFC92E88F53C54C4AC07C62891BD3806C13E97B47692F895DB9988DEAD60`，41/41 checks 全绿。
- 四模式 machine-binding adapter 对该 source 的 SIM 绑定检查 19/19 通过；将同一 source 指定为 HIL 时
  只在 `runtime_mode_matches` 失败，跨模式重标负控有效。该检查没有创建人工 accepted record。
- 全仓 3,294 tests：`3293 passed, 1 skipped, 0 failed, 0 errors`；JUnit 478,731 bytes、SHA-256
  `8261544D868E9222A22D7E5EFE5E1F437B19ECEFF5DB1567ECC02C3C38F617CD`。
- 该机器门没有配置 target-geolocation acceptance，CameraInfo K/P/畸变语义仍 pending；四模式人工
  视觉、HIL/REAL、硬件、室外 GNSS/RTK 和实飞不因本轮结果提升。

## v66 本轮已收口

- `capture_qgc_native_style.ps1` 与仓库 JSON Schema 定义 schema v2 并强制显式 runtime mode；报告绑定
  run ID、实际 QGC
  EXE identity、canonical parity identity/五组摘要、exact PID/client rect、遮挡状态和 PNG identity。
- 单记录 verifier 使用 native-v2 / legacy-HIL 双适配器；新报告直接覆盖四模式，旧 PX4 event 仅允许
  HIL。mode/PID/rect/遮挡/PNG/EXE/parity/UI contract 任一不一致都会关闭记录。
- HIL acceptance wrapper 已传入明确的 `hil`、同一 run ID 和 canonical parity。当前仍未产生任何人工
  accepted record，因此 E7 仍为 0/4、32 blockers、最终 `v0.3.0` 继续阻断。
- 全仓 3,286 tests：`3285 passed, 1 skipped, 0 failed, 0 errors`；JUnit 477,631 bytes，SHA-256
  `15F37922F866E1FCFBEDE011A461185333E25244B70447629955363635356C78`。Ruff、compileall、schema、
  PowerShell parser 与 parity 重算通过；parity 文件前后同为
  `289C1A3DD638C2A503ABEA44EE3F87564BB38D1232FB1428E92EA004180D060B`。

## v65 本轮已收口

- 人工记录新增强制 `runtime_mode`，且必须与绑定 machine report 一致；旧 v61 图只属于 HIL，不能
  冒充 REPLAY/SIM/REAL。
- E7 manifest writer/evaluator/schema/CLI/wrapper 已接入 `qgc_operator_visual_acceptance` 四模式集合；
  每次评估会重新打开记录和全部源文件，复算 review digest、source identity 与 verifier 结果。
- 集合必须按 `replay/sim/hil/real` 覆盖 4/4；重复 mode、重复记录、复用 machine report 或相同
  screenshot、文件篡改和任一缺失均 fail-closed。当前 accepted records 为 0/4，因此集合
  `present=false/passed=false`，总发布门继续关闭。
- release-gate schema 改动使 `px4_mission_path_hash` 更新为
  `F0804FEB609A9CBBBCE6E3932ED77BD7852C9AF71BDB612A76BBD56EAD12247A`；matrix、campaign、external
  join/revalidation 与当前 E7 已从不可变源按新 parity 重建并独立复验通过。
- 全仓 3,282 tests：`3281 passed, 1 skipped, 0 failed, 0 errors`；JUnit 477,082 bytes，SHA-256
  `C893DDEDEE6A07F659F354C0C3DD13214DFA81014F08EEC380F977ED4C77EE7A`；Ruff、compileall、全部 JSON
  Schema、PowerShell parser、E7 wrapper 与 campaign/matrix verifier 通过。

## v64 本轮已收口

- 新增 `multidetect.qgc_visual_acceptance` 和 `multidetect-qgc-visual-review` CLI，绑定 v61 exact
  PID/HWND machine report、原始 PNG、实际 QGC EXE、当前 `ui_contract_hash` 与 geolocation 负控。
- 12 项人工 checklist 覆盖单一原生 surface、地图点、1σ 圆、经纬度、类别、距离、方位、更新时间、
  复制、快照、记录和只读声明；缺项、false 或非 accepted decision 均关闭 gate。
- 负控必须至少 5 帧均有检测、全部 range invalid、全部包含预期 fail-closed reason 且 0 个假坐标；
  当前 v58 负控 source-binding 预检为 5/5 检测、0/5 坐标。
- 新增严格 JSON Schema、内容完整性摘要、独立重开 verifier 和 7 项定向回归。全仓 3,276 tests 为
  `3275 passed, 1 skipped, 0 failed, 0 errors`；JUnit 476,289 bytes，SHA-256
  `CA7A4F84146518BE23B47F9DFB0375BE58F77EF01846829375BFC3A323FD8881`。当前没有 accepted 人工 record，
  因此 `qgc_manual_visual_acceptance=not_claimed`，E7 blocker 数不变。

## v63 本轮已收口

- 固定翼、多旋翼、VTOL 各完成 2 次正常 reported-position 响应；位移 P50/P95 分别为
  `36.414368/38.702007 m`、`2.446236/3.593912 m`、`3.951534/4.101479 m`。
- 三机型分别执行 `target_refresh_dropout`、`heartbeat_dropout`、`flight_mode_loss`，共 9 次真实
  Docker/PX4 负向运行；每次 runtime gate 保持通过，对应状态分别为
  `target_refresh_not_sustained`、`heartbeat_continuity_failed`、`flight_mode_loss_observed`，全部
  `fail_closed=true`。
- 新增 campaign builder/verifier/schema，重开 6 份 positive 与 9 份 fault 源报告并复算 P50/P95、
  源哈希和 campaign digest。当前 campaign SHA-256 为
  `BCD78748FD9D727E0E9C91510B7F82BE6AF3BDFEEA72A0022755FC1F6643CD81`。
- E7 新增 `px4_reported_position_response_campaign`；当前 matrix/campaign 均
  `present=true`、`passed=true`，总体仍有 32 个真实 blocker。
- canonical release parity 文件 SHA-256 为
  `E329C0BCFDC9673044D5E44D328B955B00902A97C6FCD545878EB31BDDF0B7FE`，
  `px4_mission_path_hash=4B5A95F3A89F35557820FD530A105B011EC0095E8E84F24701291AC29DA802DD`。
- 全量回归 3,269 tests：`3268 passed, 1 skipped, 0 failed, 0 errors`；JUnit 475,433 bytes、
  SHA-256 `66B5910ED572B909D8533A09E683AB434DC70798BF62EFB3B80246A82686C199`；全仓 Ruff、compileall、
  campaign/matrix 独立复验和 JSON Schema 校验均通过。

## v62 本轮已收口

- 固定翼、多旋翼、VTOL 的外部 PX4/Gazebo 动态响应全部通过；8 秒窗口内最大水平位移分别为
  `33.872547 / 1.171039 / 3.784929 m`，PX4 position samples 为 `262 / 223 / 245`。
- 三者都使用 `SimulatorTelemetryBridge → SimulatorAiExecutor → SimulatorMavlinkControlAdapter →
  MAVLink MANUAL_CONTROL → PX4 SITL/Gazebo`；目标更新无拒绝、POSCTL 无 mode-loss、heartbeat 连续。
- 新增 source-bound matrix builder/verifier 与 schema。矩阵会重开三份报告、scenario manifest、当前
  release parity 和三个工具源码，拒绝真值消费、pose 注入、非 pinned image、残留容器、硬件声明或
  任一机型/刷新/心跳/位移门失败。当前 matrix SHA-256 为
  `7124F746870F8CC2ADCF7A78A385EBE8190320CBAB67D972D437CAAC902C00CA`。
- E7 writer/evaluator/schema/CLI 和当前 wrapper 已加入
  `px4_reported_position_response_matrix`；当前绑定 `present=true`、`passed=true`，总门仍有 32 条真实
  blocker，未提升硬件、HIL、REAL、人工视觉或实飞声明。
- `release-parity.json` 已刷新，`px4_mission_path_hash` 为
  `B79C941AF889BAB50B2F42E674EC6662921233926E1C9271A65CEB9FC2702E55`；external join 与 E7 均已按
  新身份重建。
- 全量回归 3,261 tests：`3260 passed, 1 skipped, 0 failed, 0 errors`；JUnit 474,112 bytes、
  SHA-256 `746E1897E4E59BEC2CAF2BACA16592470C1FD224633815F704DAE62DC824341B`；全仓 Ruff 与 compileall
  通过。

## v61 本轮已收口

- HIL 驱动现在在授权完成后按 `0.2 s` 周期有界刷新 evidence-derived type 10/22，并在发送前严格
  校验 canonical `release-parity.json` 的版本和五组 hash；任何旧 UI/core/task/safety/Mission 身份
  都会 fail-closed。
- 原生视觉采集支持附着已经运行的精确 `MultiDetectGCS` PID，核对 exact HWND/client rect、单一原生
  surface、目标尺寸和外部遮挡恢复；QGC 的验收专用延迟退出仅由显式环境变量启用，默认行为不变。
- 最新 pinned PX4 SIH + 原生 QGC acceptance 通过：132 个认证 metadata、type 10/22 各 47 个、
  60 次定位刷新、14 个 PX4 heartbeat；禁止写入、物理输出和 14550 端口均保持为 0/不变，所有测试
  资源清理完成。acceptance SHA-256 为
  `BF9E9856F91244161CDB38E8FE5DDA9BC6972A0BFC17E989A427BDAA296A2861`。
- exact PID `4900` 的机器截图为 2,580,889 bytes、SHA-256
  `86D279AA8260B4AC6AA708203F5DC8499D485B30C72DA18F1EEA59BF745755D3`；画面包含目标点、1σ、类别、
  距离、方位、更新时间、帧摘要和复制/保存/记录入口。该项只声明机器采集通过，
  `operator_visual_acceptance=not_claimed`。
- QGC Release/portable 已从当前源码重建且字节一致：46,576,640 bytes、SHA-256
  `62F832239C375998E8431985DA02BB063ABEBA69BDB551E46527D2EFD97F747F`。
- 全量回归 3,257 tests：`3256 passed, 1 skipped, 0 failed, 0 errors`；JUnit 473,487 bytes、
  SHA-256 `E5D307C7F4AC57A4198358FBF84CFD034B27C20F415F2D1B7BF8F49850CB0837`。

## v60 本轮已收口

- QGC HIL 驱动新增可选 evidence intake：只接受 schema v2、`gate_passed=true` 且 display-only 的
  Gazebo geolocation evidence，保留其文件 SHA、内部摘要、场景和 release identity；失败/不完整输入
  在任何网络发送前拒绝。
- 最后一帧实际融合结果被重绑定到同一实时 target/frame，作为 type 10 `RANGE_STATUS` 与 type 22
  `TARGET_GEOLOCATION_STATUS` 通过既有 MAVLink 2 签名 localhost operator link 进入原生 QGC。
- 实际 pinned PX4 SIH + 原生 QGC acceptance 通过：40 个认证 metadata、type 10/22 各 1 个、PX4
  heartbeat 6 个；forbidden/arm/Mission/FTP/physical output 均为 0，14550 不变，端口、容器和进程
  全清理，无新 crash dump。证据 SHA-256 为
  `9E40159EEC767CD8F1AEF106BCC7E48ED9C00BC0DDFC7C1E4CECC7D2A060D053`。
- 全量回归 3,255 tests：`3254 passed, 1 skipped`；JUnit 473,177 bytes，SHA-256
  `0BD69A52C24CC8EC1438988CBB54A2223DFC656FB10D31F6C80D18B0DA725BE5`。Ruff、compileall、diff check
  通过；临时 parity 与 canonical SHA 相同。QGC C++/QML、五组 parity 和 EXE 字节均未改变。
- 这是 evidence→wire→native state 的机器验收，不冒充地图点、1σ 圆、卡片/快照的人工视觉验收，
  也不提升 E7、真实 GNSS、硬件 HIL/REAL、实飞或最终版本状态；E7 仍为 32 blockers。

## v59 本轮已收口

- 新增 mid/high slope 两套完整 scenario/runtime/truth/expectations、GeoJSON、DEM 和 SDF 资产；自动测试
  对 channel hash、DEM/SDF/truth、WGS-84、坡面法向、billboard 尺寸和相机投影逐项对账。
- disposable PX4/Gazebo Docker 分别采集两组各 5 帧实际 camera/NavSat/Odometry，Docker acceptance
  均通过并安全 stop/rm；对应定位 evidence 均 5/5 落图，独立 verifier 都
  `passed=true/reproduced=true`。
- v58 定义的矩阵现 `gate_passed=true`：正向 `3/3` 场景、`15/15` 帧，高度/距离/坡度档全部满足；
  负向 `1/1` 场景、5/5 检测、0/5 坐标。三个正向工况的最大水平误差分别为
  `0.674040 / 1.175656 / 1.855370 m`，1σ 和 range CI95 coverage 均为 `1.0`。
- Gazebo helper 只在首个有效 Odometry 与首帧之前有界丢弃最多 8 个出生点速度毛刺；第 9 个或进入
  有效流后的相同异常仍立即 fail-closed。Docker capture 异常路径会先关闭 backend 并保留 helper stderr。
- 本轮未改变五组 release parity；临时重建文件与 canonical parity 逐字节一致。QGC C++/QML 未改，
  没有重复构建内容相同的 EXE。
- 全量回归 3,251 tests：`3250 passed, 1 skipped, 0 failed, 0 errors`；JUnit 472,512 bytes，
  SHA-256 `9F62B10AA176760E9F4CB8C4431CAF62EA9E28748D22C42C749D7C942DD100E1`。全仓 Ruff、
  compileall 与 diff check 通过；唯一 skip 仍是 Windows symlink 能力项。
- E7 尚未接入 v59 matrix，仍为 32 blockers；software/SIM 结果没有提升候选模型、QGC 人工视觉、
  室外 GNSS、硬件、HIL/REAL、实飞或最终版本状态。

## v57 仓库状态重整与定位复核

- 以远端 `origin/main` 最新提交为基线重新盘点源码、README、CHANGELOG、路线图、配置、测试和
  本地证据，不再依赖已丢失的聊天历史。
- 修复远端已提交但不可运行的 Gazebo 定位证据 verifier：补齐仓库内路径解析、SHA-256 格式校验、
  去时间戳确定性结果摘要和公开导出；复核时会重新打开 scenario、frame-set、模型和 manifest，
  重跑推理/测距/定位并比较当前五组 release parity。
- 新增 verifier 正向复现与“共享核心任意字节变化后旧证据失效”的回归测试。
- 将 `target_image_anchor.py` 纳入 `core_algorithm` parity。检测框到地面接触点的语义锚点现在与
  shared ranging/WGS-84 一起参与四模式一致性身份。
- 删除最新提交中无 `model.config`、无 `model.sdf`、无引用的 v2 孤立重复纹理。该文件可从 Git
  提交 `b5c3f10` 恢复；删除后 Gazebo 正式 model-root 回到 11 个已绑定文件。
- 重新生成 `configs/release-parity.json`，并从六份不可变原始外部 runtime/AI 记录重建当前 parity
  的三机型 join 与独立 revalidation。这里没有冒充一次新的外部 Gazebo 或硬件运行。
- 重跑正向和近地平线负向定位证据，两份 verifier 都从当前输入完整复现。
- 重跑 E7 report-only：仍有 32 条真实 blocker，版本和物理能力没有被 software/SIM 结果提升。
- 全量 Python 回归：3,233 tests，`3232 passed, 1 skipped, 0 failed, 0 errors`；唯一跳过为当前
  Windows 账户缺少 symlink 能力。JUnit 为 469,702 bytes，SHA-256
  `2FD79F5349A56DBDB72EB6AD1A77653707310D141F99CF996A36FFA3A23A9B6F`；全仓库
  `python -m ruff check .` 通过。
- 全仓库 `python -m ruff format --check .` 仍报告 410 个历史未格式化文件：`ground-control` 205、
  `src` 95、`tests` 86、`scripts` 24，其中 31 个属于 parity 绑定的 Python 源。本轮没有把格式迁移
  混入功能/证据修复，因为这会制造大面积无关 diff 并使当前绑定证据失效；现有 manual validation
  workflow 的 format step 在独立基线迁移完成前仍会失败。
- 将原 10,602 行 README 原样迁入 `docs/history/`；新的 README 只承担稳定入口和快速上手。
- 新增本进度页、机器可读状态和仓库更新规范，保证以后每轮结果进入 Git 文档而不是只留在聊天里。

## 当前验证证据

下表中的 `artifacts/` 文件是本机验收制品，默认受 `.gitignore` 排除；表内同时保存文件摘要和
内部确定性摘要。源代码仓库可以验证契约与脚本，但新的 clone 若没有配套模型/帧集，不能重放这些
本地制品。

| 证据 | 结果 | 关键指标 | 文件 SHA-256 |
|---|---|---|---|
| `gazebo-car-geolocation-positive-control-evaluation-v58-pre8-v4.json` | `gate_passed=true` | schema v2；5/5 检测与落图；最大/均值误差 `0.674040/0.651476 m`；AGL 均值 `6.195847 m`；range `8.000000 m`；坡度 `0 deg` | `FA8E526CC26683CF7638A33C82D5AECC379A5B55975BAA71DBECE55B959ED35A` |
| `gazebo-car-geolocation-positive-control-verification-v58-pre8-v4.json` | `passed=true` / `reproduced=true` | evaluator/source/parity 全部重开复算；无原因项 | `DC9DA51413113E5A935C55AFDED742834FD192BDFFBE92F846ED71A3525231C4` |
| `gazebo-car-near-horizon-negative-evaluation-v58-pre8-v2.json` | `gate_passed=false`（预期） | schema v2；5/5 检测；0/5 坐标；理由为 `target_ray_does_not_intersect_ground_safely` | `AA7A7BB6AAF8469D5FC0D25E9C28412C331D6F96A8BFD26243DDDAE767455895` |
| `gazebo-car-near-horizon-negative-verification-v58-pre8-v2.json` | `passed=true` / `reproduced=true` | 允许失败门后完整复现 fail-closed 结果 | `BE75F0ACA71C2B970EAF355BE9AD53AC09D1468CD5AE99540BA54406EE207F75` |
| `gazebo-mid-slope-geolocation-evaluation-v59-pre8-v1.json` | `gate_passed=true` | 5/5 落图；最大/均值误差 `1.175656/1.159927 m`；AGL `12.168137 m`；range `15 m`；坡度 `4.004173 deg` | `28A25CCAE2E8AAA1CB9E8028C156A6DEE0898E4C7F1948C08A89F48682487987` |
| `gazebo-mid-slope-geolocation-verification-v59-pre8-v1.json` | `passed=true` / `reproduced=true` | 完整 source 重放；无原因项 | `9969120F597129144612D28D31BBB1AB550AF6EEB16C27F400A8BFE411A6D95F` |
| `gazebo-high-slope-geolocation-evaluation-v59-pre8-v1.json` | `gate_passed=true` | 5/5 落图；最大/均值误差 `1.855370/1.841585 m`；AGL `20.149371 m`；range `25 m`；坡度 `7.969610 deg` | `CC3B670248BEE4F3224927AEDAE0D5A13D5301E5106DC2BA66C4C1FEF90025F6` |
| `gazebo-high-slope-geolocation-verification-v59-pre8-v1.json` | `passed=true` / `reproduced=true` | 完整 source 重放；无原因项 | `857A5AA71D938A5B247C212474C53BB32291A2D28308A01E8D920207935CE1A2` |
| `gazebo-geolocation-regime-matrix-v59-pre8-v1.json` | `gate_passed=true` | 正场景/帧 `3/3`、`15/15`；高度/距离/坡度档 `3/3`、`3/3`、`3/2`；负场景/帧 `1/1`、`5/5` | `A6D33E43B26B805939B6A14BB76C5C13CD8B9AC7EBD41A727BAFA8AB76022F35` |
| `gazebo-high-slope-geolocation-evaluation-v61-pre8-v2.json` | `gate_passed=true` | 当前五组 parity；5/5 落图；供 v61 HIL 持续刷新 | `548905E36A076240F3C4DEC6DC09540831456E175AC4A372434827D1A5F7BBE4` |
| `px4-sitl-qgc-geolocation-native-visual-v61-pre8-v8.json` | `gate_passed=true` | 认证 132；type 10/22 各 47；刷新 60；PX4 heartbeat 14；exact PID/HWND capture；写入/物理输出 0 | `BF9E9856F91244161CDB38E8FE5DDA9BC6972A0BFC17E989A427BDAA296A2861` |
| `native-visual-v8/qgc-native-style.png` | machine capture passed；人工签字未声明 | exact PID 4900；client `1585×862`；单一原生 surface；采集后外部遮挡 0 | `86D279AA8260B4AC6AA708203F5DC8499D485B30C72DA18F1EEA59BF745755D3` |
| `px4-sitl-qgc-geolocation-operator-acceptance-v60-pre8-v2.json` | `gate_passed=true` | QGC 认证 40 包；type 10/22 各 1；PX4 heartbeat 6；写入/物理输出 0；14550 不变 | `9E40159EEC767CD8F1AEF106BCC7E48ED9C00BC0DDFC7C1E4CECC7D2A060D053` |
| `simulator-external-runtime-ai-join-v57-current-parity-pre8.json` | `gate_passed=true` | 三机型六份历史不可变 source 在当前 parity 下重算 | `BAF6FC33DB0E39C758420B2DE3573EAB9A2751721476D25CACF5EBC2D2199D3B` |
| `simulator-external-runtime-ai-revalidation-v57-current-parity-pre8.json` | `gate_passed=true` | `source_recomputed=true` | `3706D8BE96F4AB74BB68679C6C2640848908769D03F01A07BE5D8B4701444CCE` |
| `release-gate-pre.8-current-20260905T091300Z-v6-bound-report.json` | `gate_passed=false` / `release_allowed=false` | 32 blockers | `4A58A02328D57593FA52D6EE8243F1A9211D3E0ACCF7A543BC0A8F0770940B2D` |

正向 evidence 内部摘要为：

- `evidence_sha256=9f626d06842748fda7f90a84c3fa4f7442e4687d9f18c2980c61246875467d00`
- `deterministic_result_sha256=2f30b9a869b12e63b7d8de31d741c3a83e8274938c72c5dc8a74705ae1262d4d`
- verifier `verification_sha256=91ce81015f5a51f6fb32d198bfb0b8f75f07c3215a6256900ee980c84d9a12d1`

负向 evidence 内部摘要为：

- `evidence_sha256=71bf863749c40270838a795753407e554e4e6fb0cfd9056bfa40f38206e8e0b9`
- `deterministic_result_sha256=7c1694f06f029fd60c5c2a506f45fe0def509db483c5fe5cea57daee7fc200f5`
- verifier `verification_sha256=56ac67565d1490080b7ab1e9907e4c255e23fc0b571a29d08c2a7cd77686a390`

v59 矩阵内部摘要为：

- `evidence_sha256=d542d951c22e96857d1d3acb2013d35ab8fb5a03f3096b101736d1cfc6cb2f5c`
- `deterministic_result_sha256=e892f16550b7349efcbdc6360b6151129ff8f7ddb59555f597b5dfb79fc54716`
- matrix evaluator source `ba30f792508890f00d4c1ad4de60fcece2f3bdf809760db47fa4cdf45b8b3a94`

## 当前共享契约身份

`configs/release-parity.json`：5,146 bytes，文件 SHA-256
`289C1A3DD638C2A503ABEA44EE3F87564BB38D1232FB1428E92EA004180D060B`。

| 契约组 | 当前哈希 |
|---|---|
| UI contract | `f20ca6c5fce0de613a38f58080e98ccb1bdf29c3425878c57ff9b9ec62084942` |
| Core algorithm | `505e5e8d623761287e86a9c69e4573f75238ffd5d1f88b7c89469dad7e0f0ad9` |
| Task contract | `2079ace640a9e37e4998dfede94fb0f4406d399b709881dd61920a7fceb1d8af` |
| Safety policy | `daf5af662f058820b01b33256847d1d5cd578790fb49aa9ff30dcad8264d710a` |
| PX4 Mission path | `f0804feb609a9cbbbce6e3932ed77bd7852c9af71bdb612a76bbd56ead12247a` |

## 功能成熟度矩阵

| 子系统 | 已实现/已自动验证 | 当前有效边界 | 仍需完成 |
|---|---|---|---|
| 输入与运行模式 | 本地/RTSP/Replay/Gazebo；REPLAY/SIM/HIL/REAL profile 与五组 parity | 软件契约和模拟输入 | 四模式同屏人工验收与真实 profile 证据 |
| 感知与模型治理 | ONNX provider、manifest、raw/post-NMS、候选隔离、延迟/精度工具 | v56 COCO car 仅 quarantined SIM candidate | 部署域 fire/smoke/safety 模型、数据集、评审和阈值 |
| 多目标跟踪 | 多目标池、短时恢复、ReID、遮挡/丢失/重获、TRK/LCK/TGT | 自动化与软件/SITL | 真实复杂场景身份精度与 G20/QGC 触控验收 |
| 测距 | metric depth、相机地面射线、多源融合、DEM/GNSS、时间/运动/残差不确定性 | v59 实际 Gazebo 像素；三档坡高/距离；advisory-only | 真实外参与 outdoor survey |
| 目标落图 | WGS-84、1σ 圆、GPS/RANGE fail-closed、事件/证据存储 | 三正场景 15/15 落图；近地平线 0/5 假点；v61 exact PID/HWND 机器截图通过 | 人工视觉签字与真实 GNSS |
| MultiDetectGCS | 原生 Fly View 覆盖、状态、目标地图、operator protocol、portable build | 本机 portable 可运行；v61 签名持续刷新与单一原生 surface 机器采集通过 | 四模式人工视觉对等与 E7 绑定 |
| 数字孪生 | PX4/Gazebo、三机型、场景/地图/DEM、wire v2、故障、watchdog | external software evidence；x500 position response；三档定位 | fixed-wing/VTOL 对等动态响应 |
| 任务与控制 | 平台无关 planner、PX4 Mission 编译/反馈、Mode 3 动态权限 | 测试/SIM/HIL 框架；物理输出默认锁定 | 六份 HIL/REAL×三机型 feedback 与真实飞控验收 |
| Jetson/Pixhawk/链路 | Jetson profile、RTSP bench、只读 MAVLink、V6X 参数备份、G20/GR01 认证协议 | 软件/localhost 与部分只读现场快照 | 实机 Jetson provider、相机、GR01、V6X 同时在线验收 |
| 载荷安全 | 独立授权、双反馈、惰性 HIL、超时/重放/状态不一致 fail-closed | 无物理输出的 HIL | 经评审真实 controller transport 与受控地面台架 |
| 发布门 | E7 schema、证据绑定、recompute、严格/报告模式 | 当前 32 blocker，发布拒绝 | 全部现场/模型/视觉/Mission/HIL/REAL 门关闭后才发 v0.3.0 |

## 本机可运行 QGC

当前 portable 主程序：

```text
D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.8-release\portable-v0.3.0-pre.8\bin\MultiDetectGCS.exe
```

- 大小：`46,576,640` bytes
- SHA-256：`62F832239C375998E8431985DA02BB063ABEBA69BDB551E46527D2EFD97F747F`
- v61 已从当前 C++/QML 源码完成 Release 与 portable 重建，两份主 EXE 字节一致。
- 该文件位于被忽略的本机 `build/`，不是 GitHub 下载资产；运行时必须保留完整 portable 目录。

## 当前 E7 阻断分类

当前 32 条 blocker 可归为：

1. replay-set 没有完整部署域覆盖、精度批准和人工评审；
2. REAL field-link 报告已过期，且 Jetson/GR01/相机/目标网段尚未同时 ready；
3. REPLAY、SIM、HIL、REAL 的人工 QGC visual 和 Mission path acceptance 未全部通过；
4. HIL/REAL 缺真实 hardware acceptance 与 real-control acceptance；
5. PX4 Mission feedback 六叶矩阵未完成；
6. 部分被引用 artifact 自身仍为失败门；
7. 产品仍是 `prerelease`，版本面不能伪装为最终 `0.3.0`。

## 下一步优先级

### 仓库维护债务：独立 Ruff format 基线迁移

单独建立机械格式化变更，冻结功能修改，格式化并复核 410 个历史文件；随后重新生成 release parity、
重跑受影响的定位/三机型/E7 证据和全量测试。该迁移不能与目标落图功能开发混在同一提交里，也不能
通过删除 CI format gate 假装完成。

### P0：原生 QGC 目标落图与证据卡片人工签字

v61 已将 high-slope evidence 的坐标、1σ、类别、距离和方位经签名协议持续送入原生
MultiDetectGCS，并完成 exact PID/HWND 动态地图/证据卡片机器采集。下一步由人工检查同一证据画面并
使用 v64 checklist 生成 accepted record；独立 verifier 通过后再经 manifest 接线评审接入 E7。当前
负向 source-binding 预检保持 5/5 检测、0 假点。v67 combined-SIM source 已能被通用 adapter 识别为
真实 SIM runtime，但它的 toolbar/Mission 截图没有配置 target-geolocation acceptance，不能直接充当
包含地图点/1σ/证据卡片的人工记录；SIM 模式仍需专门运行该定位画面。

### P1：三机型动态对等（机器子门已完成）

fixed-wing、multirotor、VTOL 已完成同一持续目标刷新、authority/watchdog、MANUAL_CONTROL 与 PX4
reported-position 路径；v63 又完成每机型双重复与三故障 fail-closed。剩余工作属于人工、硬件和实飞门，
不能用 SIM campaign 替代。

### P2：部署域模型和 replay-set

采集 day/night/haze/reflection/smoke-only/no-fire 数据，完成 fire/smoke 与 person/vehicle 安全模型，
测量 per-class precision/recall/false alarms/misses/latency，完成独立评审后再改变 model status。

### P3：真实硬件与室外定位

在 Jetson Orin Nano、实际 RTSP 相机、Pixhawk V6X 和 GR01 同时在线时采集 freshness/readiness；使用
实测相机内外参、lever arm、boresight sigma、真实 GNSS/RTK 和多距离/多坡度 survey truth 完成精度门。

### P4：HIL/REAL 与最终发布

补齐 HIL/REAL × fixed-wing/multirotor/VTOL 六份签名 profile/Mission feedback，完成四模式同 UI、
同核心、同契约、同安全、同 Mission 的人工验收。E7 strict 只有在 blocker 为 0 且版本通道切到
`release` 后才允许发布 `v0.3.0`。

## 状态解释规则

- “有代码”只表示 implemented。
- “测试通过”只表示 automated verification。
- “SIM 通过”只表示相应 software/SIM 子门。
- “HIL 通过”必须有真实或明确界定的 HIL 设备/传输证据。
- “REAL/生产批准/实飞通过”必须由独立现场证据和 E7 严格门支持。
- 未写明的能力一律不从更低层证据推断。
