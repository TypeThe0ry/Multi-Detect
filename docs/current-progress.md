# Multi-Detect 当前进度

最后更新：2026-09-05（v63 工作树）

GitHub 审计基线：`origin/main`，提交 `b5c3f10baf4cc9febaa3a10103752f202f3b0611`；本轮工作基线为
本地 `main` 提交 `508bf3c78382e4199d3d5f8d203b30b913d361c7`（当前工作树变更前领先远端 6 个提交）。

产品版本：`v0.3.0-pre.8`（最终 `v0.3.0` 尚未发布）

机器可读镜像：[current-progress.json](current-progress.json)

## 结论先行

当前仓库已经形成统一的感知、跟踪、测距、目标落图、QGC、数字孪生、任务规划、PX4 Mission、
operator link、安全和证据框架。v59 已让三组各 5 帧的实际 Gazebo BGR24 像素经过同一候选 ONNX、
共享语义地面锚点、DEM/GNSS 测距和 WGS-84 投影；三个场景覆盖约 `6.20/12.17/20.15 m` AGL、
`8/15/25 m` 目标距离和 `0/4.00/7.97 deg` 坡度，均由独立 verifier 重现。对应近地平线输入 5/5 帧
都拒绝生成坐标。v60 又把 high-slope evidence 的测距、坐标和 1σ 经签名 operator link 送入现有
原生 QGC。v61 在同一 HIL 进程内持续刷新 type 10/22，按 exact PID/HWND 采集无外部遮挡的目标地图
与证据卡片机器截图，并把证据与当前五组 release parity 严格绑定。v62 又让固定翼、多旋翼和 VTOL
通过同一 AI/MAVLink 路径驱动 disposable PX4/Gazebo，以 `GLOBAL_POSITION_INT` 证明实际位移，并将
可重放的三机型矩阵正式接入 E7。v63 又完成每机型两次正常响应与三种显式故障，共 15 份外部
PX4/Gazebo 源报告，并把 repeatability/fault campaign 接入 E7；人工视觉签字和现场证据仍未完成。

最终版本仍然阻断。当前 E7 report-only 结果为 `gate_passed=false`、`release_allowed=false`，共
32 条 blocker，主要来自部署域数据/模型精度、现场链路、四模式人工 QGC/Mission、HIL/REAL、
三机型硬件控制和 prerelease 版本面。候选 YOLO 模型仍为 `quarantined`，不是生产批准模型。

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
`08807AA127A1F1E61D31FF503E4EC4D3BC6803C7369D671DFD864B4B22646A73`。

| 契约组 | 当前哈希 |
|---|---|
| UI contract | `f20ca6c5fce0de613a38f58080e98ccb1bdf29c3425878c57ff9b9ec62084942` |
| Core algorithm | `505e5e8d623761287e86a9c69e4573f75238ffd5d1f88b7c89469dad7e0f0ad9` |
| Task contract | `2079ace640a9e37e4998dfede94fb0f4406d399b709881dd61920a7fceb1d8af` |
| Safety policy | `daf5af662f058820b01b33256847d1d5cd578790fb49aa9ff30dcad8264d710a` |
| PX4 Mission path | `18023962f68a61fdfb14c44e5f0a6a5bb713c782166386bd2b1f2bf7b567651d` |

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
签字，再经 schema/manifest 评审接入 E7；负向样本必须继续 0 假点。

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
