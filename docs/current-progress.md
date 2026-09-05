# Multi-Detect 当前进度

最后更新：2026-09-05（v58 工作树）

审计基线：GitHub `origin/main`，提交 `b5c3f10baf4cc9febaa3a10103752f202f3b0611`

产品版本：`v0.3.0-pre.8`（最终 `v0.3.0` 尚未发布）

机器可读镜像：[current-progress.json](current-progress.json)

## 结论先行

当前仓库已经形成统一的感知、跟踪、测距、目标落图、QGC、数字孪生、任务规划、PX4 Mission、
operator link、安全和证据框架。v58 已把 5 帧真实 Gazebo BGR24 像素经过候选 ONNX、共享语义
地面锚点、DEM/GNSS 测距和 WGS-84 投影，得到可重放验证的 software/SIM 正向结果；对应近地平线
输入 5/5 帧都拒绝生成坐标，证明失败路径不会落假点。新工况矩阵又明确证明当前只有一个平地
高度/距离档，尚不满足多高度、多距离、多坡度覆盖。

最终版本仍然阻断。当前 E7 report-only 结果为 `gate_passed=false`、`release_allowed=false`，共
32 条 blocker，主要来自部署域数据/模型精度、现场链路、四模式人工 QGC/Mission、HIL/REAL、
三机型硬件控制和 prerelease 版本面。候选 YOLO 模型仍为 `quarantined`，不是生产批准模型。

## v58 本轮已收口

- Gazebo camera → ranging → WGS-84 evidence 升级为 schema v2：每份 evidence 绑定 evaluator source
  SHA-256，并从绑定的逐帧 telemetry/truth/DEM 派生 AGL 高度、真实地面距离和目标处坡度。truth 仍只在
  所有 prediction 完成后进入评分，评估器字节变化会使旧证据失效。
- 新增 `gazebo_geolocation_regime_matrix.py`、脚本入口和安装后 CLI。矩阵不会信任摘要，而是逐份重开
  scenario/frame-set/model/manifest、重算当前 parity 并重跑 detector/ranging/geolocation；还要求所有
  source 使用相同 release/evaluator identity。
- 默认矩阵门要求 3 个/15 帧正样本、1 个/5 帧零坐标负样本、3 个相隔至少 `5 m` 的高度档、3 个
  相隔至少 `5 m` 的距离档和 2 个相隔至少 `3 deg` 的坡度档。
- 当前真实矩阵按设计失败：正样本 `1/3`、正帧 `5/15`、高度/距离/坡度 `1/3`、`1/3`、`1/2`；
  零坐标负样本 `1/1` 与负帧 `5/5` 已满足。它把 P0 缺口写成机器门，不把单场景冒充多工况完成。
- v57 的仓库状态重整、parity 重建、E7 32 blockers 和回归基线继续有效；v58 尚未改变 QGC C++/QML、
  release parity 或最终版本状态。
- 全量回归 3,241 tests：`3240 passed, 1 skipped, 0 failed, 0 errors`；JUnit 470,851 bytes，
  SHA-256 `08FEBF2327722653503715CF1180ACA2FABEE7578F5937D734430C6CCC9FE7F0`。全仓 Ruff check 与
  compileall 通过；唯一 skip 仍是 Windows symlink 能力项。

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
| `gazebo-geolocation-regime-matrix-v58-pre8-v4.json` | `gate_passed=false`（当前缺口） | 正场景/帧 `1/3`、`5/15`；高度/距离/坡度档 `1/3`、`1/3`、`1/2`；负场景/帧 `1/1`、`5/5` | `FF791A6DAD855A476F2E9DDB9890C905A375F6D9146D352E88B6C9F211A09257` |
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

矩阵内部摘要为：

- `evidence_sha256=4fb9e50143ef1b4a777b4292f384cc16005204bb1eb60b9a7d0ceff28aece33b`
- `deterministic_result_sha256=779ad2029f1ee904ddf83a240a3c3db33cdb123e04644dda9904e9f5116986af`
- matrix evaluator source `ba30f792508890f00d4c1ad4de60fcece2f3bdf809760db47fa4cdf45b8b3a94`

## 当前共享契约身份

`configs/release-parity.json`：5,146 bytes，文件 SHA-256
`C670B2AAE30C392C2B7FBB981EDB58719EC66E4112B51F135E69AC662AFE152B`。

| 契约组 | 当前哈希 |
|---|---|
| UI contract | `dd8b40866d3143e5eb85bf18b0028c1824cb0c91b111b7a4cbfff503152c4328` |
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
| 测距 | metric depth、相机地面射线、多源融合、DEM/GNSS、时间/运动/残差不确定性 | v58 实际 Gazebo 像素；单一平地工况；advisory-only | 矩阵尚缺 2 个高度/距离工况和 1 个坡度档；真实外参与 outdoor survey |
| 目标落图 | WGS-84、1σ 圆、GPS/RANGE fail-closed、事件/证据存储 | 正向 5/5；近地平线 0/5 假点；schema-v2 可复算 | 补齐多工况矩阵；原生 QGC 地图/证据卡片人工视觉与真实 GNSS |
| MultiDetectGCS | 原生 Fly View 覆盖、状态、目标地图、operator protocol、portable build | 本机 portable 可运行；v57 未改 QGC | v57 定位链的前台人工验收；四模式视觉对等 |
| 数字孪生 | PX4/Gazebo、三机型、场景/地图/DEM、wire v2、故障、watchdog | external software evidence；x500 position response | fixed-wing/VTOL 对等动态响应；多个坡高闭环 |
| 任务与控制 | 平台无关 planner、PX4 Mission 编译/反馈、Mode 3 动态权限 | 测试/SIM/HIL 框架；物理输出默认锁定 | 六份 HIL/REAL×三机型 feedback 与真实飞控验收 |
| Jetson/Pixhawk/链路 | Jetson profile、RTSP bench、只读 MAVLink、V6X 参数备份、G20/GR01 认证协议 | 软件/localhost 与部分只读现场快照 | 实机 Jetson provider、相机、GR01、V6X 同时在线验收 |
| 载荷安全 | 独立授权、双反馈、惰性 HIL、超时/重放/状态不一致 fail-closed | 无物理输出的 HIL | 经评审真实 controller transport 与受控地面台架 |
| 发布门 | E7 schema、证据绑定、recompute、严格/报告模式 | 当前 32 blocker，发布拒绝 | 全部现场/模型/视觉/Mission/HIL/REAL 门关闭后才发 v0.3.0 |

## 本机可运行 QGC

当前 portable 主程序：

```text
D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.8-release\portable-v0.3.0-pre.8\bin\MultiDetectGCS.exe
```

- 大小：`46,576,128` bytes
- SHA-256：`06E7B1BBEC1B96DC6E5A231B12B88725122476075ED86A24BBEF554CB899E5FE`
- v58 没有修改 QGC C++/QML，所以没有为了改 Python/文档重复链接一个内容相同的 EXE。
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

### P0：多个坡高的目标落图与原生 QGC 证据卡片

复用当前 v58 唯一共享链路，在至少三个高度、多个距离和坡度下重新采集实际 Gazebo camera/NavSat/
Odometry；逐帧经过候选检测、共享 anchor、DEM/GNSS ranging、WGS-84 geolocation，再把坐标、1σ 圆、
类别、距离、方位、时间和快照送入原生 MultiDetectGCS Fly View。完成自动精度门与人工视觉签字，
负向样本必须继续 0 假点。

### P1：三机型动态对等

为 fixed-wing 和 VTOL 生成与 x500 等价的持续目标刷新、authority/watchdog、PX4 reported-position
response 证据，不通过 simulator-only teleport 或独立控制器取巧。

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
