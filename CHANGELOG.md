# Multi-Detect Changelog

本文件记录 Multi-Detect 产品版本。`ground-control/MultiDetectGCS/CHANGELOG.md` 属于上游
QGroundControl 历史，不作为本产品发布记录。

## [Unreleased]

### v0.3.0-pre.8 v65 四模式 QGC 人工视觉 E7 门（2026-09-05）

- 单份视觉记录新增强制 `runtime_mode` 并复验 machine report 的模式身份；v61 exact PID/HWND 捕获只
  归属 HIL，不能冒充其他模式。
- E7 writer/evaluator/schema/CLI/wrapper 新增 `qgc_operator_visual_acceptance` 集合。最终 release 要求
  replay/sim/hil/real 各一份独立通过记录；重复 mode/evidence、复用 machine report 或相同 screenshot、
  任一源漂移都会 fail-closed。
- 当前没有人工 accepted record，集合保持 `present=false/passed=false`，现有 32 条发布阻断不减少。
- `px4_mission_path_hash` 更新为
  `F0804FEB609A9CBBBCE6E3932ED77BD7852C9AF71BDB612A76BBD56EAD12247A`；matrix、campaign、external
  join/revalidation 和当前 E7 均按新 parity 重建并独立复验通过。
- 全仓 3,282 tests：`3281 passed, 1 skipped, 0 failed, 0 errors`；JUnit 477,082 bytes、SHA-256
  `C893DDEDEE6A07F659F354C0C3DD13214DFA81014F08EEC380F977ED4C77EE7A`。Ruff、compileall、Schema、
  PowerShell wrapper 与 E7/campaign/matrix 独立复验通过。

### v0.3.0-pre.8 v64 QGC 人工视觉验收合同（2026-09-05）

- 新增 `multidetect.qgc_visual_acceptance` 与 `multidetect-qgc-visual-review` CLI；人工 review 绑定 exact
  PID/HWND 机器报告、原始 PNG、实际 QGC EXE、当前 UI contract 和 geolocation fail-closed 负控。
- 新增 12 项严格 checklist、JSON Schema、内容摘要和独立 verifier；截图、EXE、parity、机器报告或
  负控任一字节漂移都会关闭验收。
- 真实 v61 screenshot/source 与 v58 近地平线负控已通过 source-binding 预检；负控 5/5 有检测、0/5
  目标坐标。人工尚未逐项确认，因此 `operator_visual_acceptance` 与 E7 总门继续保持未声明/阻断。
- 新增 fail-closed checklist 模板和 7 项回归，覆盖 schema、完整通过、截图/记录篡改、负控假点、缺项、
  rejected decision 和 CLI template。
- 全仓 3,276 tests：`3275 passed, 1 skipped, 0 failed, 0 errors`；Ruff、compileall、JSON Schema 和
  真实 v61/v58 source-binding 预检通过。

### v0.3.0-pre.8 v63 三机型 PX4 响应重复性与故障注入（2026-09-05）

- Docker AI acceptance runner 新增仅限 disposable loopback PX4 SITL 的目标刷新中断、心跳消费中断和
  PX4 模式退出注入；三种故障都记录 requested/activated/observed/fail-closed，负向运行保持 runtime
  gate 通过并由对应飞行安全门拒绝。
- 实际执行固定翼、多旋翼、VTOL 各两次正常位移响应与每机型三种故障，共 `6` 个 positive 和 `9` 个
  fault 报告。正常运行全部使用 PX4 `GLOBAL_POSITION_INT`，负向覆盖 `9/9` 且全部 fail-closed。
- 新增 campaign builder/verifier/JSON Schema，统计每机型位移、持续时间、position samples、目标更新率
  的 minimum/P50/P95/maximum，并绑定全部源文件、scenario、工具源码与 release parity。
- E7 writer/evaluator/schema/CLI/wrapper 新增 `px4_reported_position_response_campaign`；当前 manifest 中
  matrix 与 campaign 均为 `present=true`、`passed=true`，总体仍为 32 个真实 blocker。
- canonical `px4_mission_path_hash` 更新为
  `4B5A95F3A89F35557820FD530A105B011EC0095E8E84F24701291AC29DA802DD`；matrix、campaign、external join、
  revalidation 和 E7 均按该 parity 重建。硬件、HIL、REAL、人工视觉和实飞继续 `not_claimed`。
- 全量回归 3,269 tests：`3268 passed, 1 skipped, 0 failed, 0 errors`；全仓 Ruff、compileall、Schema、
  campaign/matrix 独立复验和 E7 wrapper 均通过。

### v0.3.0-pre.8 v62 三机型 PX4 reported-position 动态响应矩阵（2026-09-05）

- 新增固定翼、多旋翼、VTOL 外部 PX4/Gazebo 动态响应矩阵生成器、独立验证器和 JSON Schema；
  三机型均通过同一 truth-free perception/AI/authority/watchdog/MAVLink `MANUAL_CONTROL` 路径。
- 使用 PX4 `GLOBAL_POSITION_INT` 实测最大水平位移 `33.872547 / 1.171039 / 3.784929 m`，禁止读取
  Gazebo 真值或注入 pose；目标刷新、heartbeat、POSCTL 连续性、pinned image 和清理门全部通过。
- E7 manifest writer/evaluator/schema/CLI 与当前 pre.8 wrapper 新增 hash-bound
  `px4_reported_position_response_matrix`，每次重开三份源证据并复算矩阵；final release 缺失该项时
  fail-closed。
- `px4_mission_path_hash` 刷新为
  `B79C941AF889BAB50B2F42E674EC6662921233926E1C9271A65CEB9FC2702E55`，相关 external join 与 E7
  证据按新 parity 重建。该结果只关闭 SIM 动态响应子门，不声明硬件、HIL、REAL、实飞或人工视觉。
- 全量回归 3,261 tests：`3260 passed, 1 skipped, 0 failed, 0 errors`；全仓 Ruff 和 compileall 通过。

### v0.3.0-pre.8 v61 原生 QGC 定位卡片/地图 exact-PID 机器捕获（2026-09-05）

- HIL driver 增加 bounded post-authorization refresh：定位 evidence 在截图窗口内持续以同 target/frame
  绑定发送 type 10/22；同时强制比较 evidence 内五组 parity 与 canonical `release-parity.json`，旧 UI
  parity、错误版本、失败门或非 display-only 输入均在传输前 fail-closed。
- `capture_qgc_native_style.ps1` 可附着已运行的 exact `MultiDetectGCS` PID，不会关闭所附着进程；捕获前
  将窗口扩到至少 `1600x900`，选择能容纳完整布局的屏幕，保存/恢复 QGC 与外部窗口状态，并记录 exact
  client rect、PID、标题、遮挡和 PNG SHA。`run_px4_sitl_qgc_operator_acceptance.ps1` 将该路径接入同一
  pinned PX4 SIH/只读 router acceptance。
- QGC isolated-HIL 自动退出新增最长 15 秒的验收专用延时；机器捕获使用 8 秒延时，随后仍走 QGC 自身
  受控退出。普通 acceptance 延时为 0，行为不变。增量构建与 portable 部署完成，Release/portable EXE
  均为 46,576,640 bytes，SHA-256
  `62F832239C375998E8431985DA02BB063ABEBA69BDB551E46527D2EFD97F747F`。
- current parity 下重跑 high-slope 定位为 5/5 落图；source 文件 SHA-256
  `548905E36A076240F3C4DEC6DC09540831456E175AC4A372434827D1A5F7BBE4`。最终 HIL evidence
  `gate_passed=true`，QGC exit 0、认证 132 包、type 10/22 各 47、PX4 heartbeat 14、写入/物理输出 0、
  14550 不变且资源全清理；evidence SHA-256
  `BF9E9856F91244161CDB38E8FE5DDA9BC6972A0BFC17E989A427BDAA296A2861`。
- 最终 exact-client PNG 为 2,580,889 bytes / SHA-256
  `86D279AA8260B4AC6AA708203F5DC8499D485B30C72DA18F1EEA59BF745755D3`，显示完整定位卡片与地图目标。
  这关闭的是 machine dynamic-render capture 子门；本线程没有 `@oai/sky`，所以人工视觉、室外 GNSS、
  硬件 HIL/REAL、实飞和最终 `v0.3.0` 均未声明通过，E7 blocker 仍为 32。
- 全量回归 3,257 tests：`3256 passed, 1 skipped, 0 failed, 0 errors`；JUnit 473,487 bytes、SHA-256
  `E5D307C7F4AC57A4198358FBF84CFD034B27C20F415F2D1B7BF8F49850CB0837`。

### v0.3.0-pre.8 v60 Gazebo 定位证据进入原生 QGC（2026-09-05）

- 扩展既有 `operator_closed_loop_hil.py`，可读取 schema v2 且门禁通过的 Gazebo 定位 evidence，
  保留文件/内部确定性摘要和原始场景身份，把最后一帧测距与 WGS-84/1σ 结果重绑定到同一个实时
  HIL target/frame，再通过现有 MAVLink 2 signing operator link 发送 type 10/type 22。失败门、非
  display-only 证据和不完整帧均在传输前 fail-closed。
- 扩展 `run_px4_sitl_qgc_operator_acceptance.ps1` 的可选
  `-TargetGeolocationEvidencePath`：QGC 必须认证 range/geolocation 两类消息，且路径、包计数和安全边界
  必须进入 acceptance JSON；仍使用 pinned disposable PX4 SIH、localhost 独立链路和只读 router。
- 实际 v60 验收 `gate_passed=true`：原生现有 EXE 认证 40 个 metadata 包，其中 type 10/type 22
  各 1 个，PX4 autopilot heartbeat 6 个；operator tunnel→PX4、forbidden PX4、FTP write、arm、Mission
  upload、physical payload 全为 0，UDP 14550 所有权不变，端口/容器/进程全部清理且无新 crash dump。
  evidence 文件 SHA-256 为
  `9E40159EEC767CD8F1AEF106BCC7E48ED9C00BC0DDFC7C1E4CECC7D2A060D053`。
- 全量回归 3,255 tests：`3254 passed, 1 skipped, 0 failed, 0 errors`，JUnit 473,177 bytes / SHA-256
  `0BD69A52C24CC8EC1438988CBB54A2223DFC656FB10D31F6C80D18B0DA725BE5`；Ruff、compileall、diff check
  通过，临时 parity 与 canonical SHA-256 均为
  `C670B2AAE30C392C2B7FBB981EDB58719EC66E4112B51F135E69AC662AFE152B`。
- QGC C++/QML 和五组 release parity 未改变，EXE 仍为既有 `0.3.0-pre.8` portable 字节。该结果只关闭
  evidence→signed wire→native controller 的 machine transport/adoption 子门；地图/1σ/卡片人工视觉、
  E7 接入、真实 GNSS、硬件 HIL/REAL 和最终 `v0.3.0` 仍未声明通过，E7 blocker 仍为 32。

### v0.3.0-pre.8 v59 三档坡高/距离 Gazebo 定位矩阵通过（2026-09-05）

- 新增 `gazebo_car_geolocation_mid_slope_001` 与 `gazebo_car_geolocation_high_slope_001` 两套完整
  scenario/runtime/truth/expectations、GeoJSON、DEM 和 SDF 资产；二者分别以约 `12 m / 15 m / 4 deg`
  和 `20 m / 25 m / 8 deg` 扩展原 `6 m / 8 m / 0 deg` 平地工况，所有 manifest channel SHA-256、
  DEM/SDF/truth、WGS-84、billboard 几何和相机投影均有自动回归。
- 在 disposable PX4/Gazebo Docker 中分别采集两组 5 帧实际 BGR24 camera/NavSat/Odometry 数据；
  mid/high Docker evidence 均 `gate_passed=true`，容器 stop/rm 成功且无残留。两组像素经同一
  quarantined ONNX candidate、共享 anchor、DEM/GNSS ranging 与 WGS-84 geolocation 后均 5/5 落图，
  独立 verifier 都 `passed=true/reproduced=true`。
- 三正一负工况矩阵现 `gate_passed=true`：正向 `3/3` 场景、`15/15` 帧，平均 AGL
  `6.195847 / 12.168137 / 20.149371 m`，距离 `8 / 15 / 25 m`，坡度
  `0 / 4.004173 / 7.969610 deg`；三档最大水平误差分别为 `0.674040 / 1.175656 / 1.855370 m`，
  各档 1σ 与 range CI95 coverage 都为 `1.0`。负向仍为 `1/1` 场景、5/5 检测、0/5 坐标。
- Gazebo helper 现在只在首个有效 Odometry 和首帧之前有界丢弃最多 8 个官方
  OdometryPublisher 的出生点速度派生毛刺；第 9 个、首个有效样本之后或任何已接收帧后的相同异常仍
  立即 fail-closed。Docker acceptance 出错时会先安全关闭 backend，再报告 helper exit/stderr。
- 全量回归 3,251 tests：`3250 passed, 1 skipped, 0 failed, 0 errors`，JUnit 472,512 bytes /
  SHA-256 `9F62B10AA176760E9F4CB8C4431CAF62EA9E28748D22C42C749D7C942DD100E1`；Ruff、compileall、
  `git diff --check` 通过，临时重建 parity 与 canonical 文件逐字节一致。
- 本轮只关闭 software/SIM 多工况定位机器子门；E7 尚未接入该输入，仍为 32 blockers。候选模型、
  QGC 人工视觉、室外 GNSS、真实相机外参、硬件、HIL/REAL、实飞和最终发布状态均未提升。

### v0.3.0-pre.8 v58 多高度/距离/坡度目标定位矩阵门（2026-09-05）

- Gazebo camera → shared ranging → WGS-84 acceptance evidence 升级为 schema v2：绑定评估器源码
  SHA-256，并从绑定的逐帧 telemetry/truth/DEM 派生 AGL 高度、真实地面距离及目标位置坡度；truth
  仍在全部 prediction 完成后才读取，不进入 detector/ranging 输入。评估器字节变化会使旧证据 fail-closed。
- 新增 `multidetect-gazebo-geolocation-matrix` 与
  `scripts/run_gazebo_geolocation_regime_matrix.py`。矩阵会先逐份重开原始 scenario、frame-set、模型、
  manifest 和当前 parity，完整复算每份证据，再要求至少 3 个相隔 `5 m` 的高度档、3 个相隔 `5 m`
  的距离档、2 个相隔 `3 deg` 的坡度档、3 个/15 帧正样本，以及 1 个/5 帧的零坐标负样本；所有
  source 必须具有相同 release/evaluator identity。
- 当前 v58 实际正向证据仍为 5/5 落图，最大/平均误差 `0.674040/0.651476 m`；近地平线负向仍为
  5/5 检测、0/5 坐标。两份 schema-v2 verifier 都 `passed=true/reproduced=true`。
- 当前矩阵报告按设计 `gate_passed=false`：正样本 `1/3`、正帧 `5/15`、高度档 `1/3`、距离档
  `1/3`、坡度档 `1/2`；零坐标负样本 `1/1`、负帧 `5/5` 已满足。没有用单一平地场景冒充多工况
  定位完成，也没有提升 QGC 人工视觉、户外 GNSS、HIL/REAL、硬件、实飞或生产批准。
- 全量回归 3,241 tests：`3240 passed, 1 skipped, 0 failed, 0 errors`，JUnit 470,851 bytes /
  SHA-256 `08FEBF2327722653503715CF1180ACA2FABEE7578F5937D734430C6CCC9FE7F0`；全仓 Ruff check 与
  compileall 通过。历史 Ruff format baseline debt 保持独立，不在 v58 功能提交中批量改写。

### v0.3.0-pre.8 v57 可复现的 Gazebo 像素到 WGS-84 定位门与仓库状态重整（2026-09-05）

- 以 GitHub `origin/main` 的 `b5c3f10baf4cc9febaa3a10103752f202f3b0611` 为审计基线，重新盘点
  当前源码、功能、证据、版本和门禁；原 10,602 行 README 迁入 `docs/history/`，根 README 改为稳定
  入口，并新增人/机可读 `docs/current-progress.*`、更新规范和 PR 检查表。以后功能更新必须同步
  CHANGELOG、当前进度、路线图/专题文档和相应测试，聊天记录不再充当唯一状态源。
- 收口远端已经存在但缺少内部 helper/公开导出的 Gazebo geolocation verifier。它现在会重新打开并
  严格约束 repository-contained scenario、frame-set、候选模型和 manifest，重新计算五组 release parity，
  重跑 detector/ranging/WGS-84，再比较去时间戳确定性摘要；新增正向复现和 shared-core 改字节后旧证据
  失效的回归。
- `target_image_anchor.py` 正式纳入 `core_algorithm` parity。删除无 `model.config`/`model.sdf`、无引用且
  会污染正式 model-root 摘要的 v2 孤立重复纹理；该字节仍可从基线提交恢复。新 parity 文件 5,146 bytes，
  SHA-256 `C670B2AAE30C392C2B7FBB981EDB58719EC66E4112B51F135E69AC662AFE152B`，core/safety hash 为
  `505E5E8D...F0AD9` / `DAF5AF66...D710A`。
- 当前 parity 下重跑实际 Gazebo 像素定位：正向 5/5 检测和落图，最大/平均水平误差
  `0.674040/0.651476 m`，最大帧间漂移 `0.035197 m`，1σ 与 range CI95 覆盖率均为 `1.0`；证据文件
  SHA-256 `5AA0257D...AC08`，独立 verifier `passed=true/reproduced=true`。近地平线负向 5/5 检测但
  0/5 坐标，理由为 `target_ray_does_not_intersect_ground_safely`，负向 verifier 也完整复现。
- 从六份不可变 historical external runtime/AI source 重建当前 parity 三机型 join，join/revalidation
  均通过且 `source_recomputed=true`；这不是新的外部 Gazebo 运行或硬件声明。E7 report-only 重新生成，
  `gate_passed=false`、`release_allowed=false`、32 blockers，最终 `v0.3.0` 继续阻断。
- 全量回归 3,233 tests：`3232 passed, 1 skipped, 0 failed, 0 errors`；唯一 skip 是 Windows symlink
  权限项。JUnit 469,702 bytes / SHA-256
  `2FD79F5349A56DBDB72EB6AD1A77653707310D141F99CF996A36FFA3A23A9B6F`；全仓库 Ruff check 通过。
  候选模型继续 `quarantined`，QGC 人工视觉、真实硬件、室外 GNSS、HIL/REAL、实飞与生产模型批准
  均未声明；v57 未改 QGC C++/QML，不重复链接 EXE。
- 全仓库 Ruff format baseline 仍有 410 个历史文件未格式化（`ground-control/src/tests/scripts` 为
  `205/95/86/24`，其中 31 个是 parity 绑定 Python 源）。本轮不混入数百文件机械改写；该项已登记为
  独立迁移债务，必须在格式化后重建 parity、相关 evidence 和 E7，现有 manual validation format step
  在此之前仍会失败。

### v0.3.0-pre.8 v56 Gazebo 相机像素与候选检测三控（2026-09-05）

- 三个组合机型的 `mono_cam` merge include 显式改为 `pose relative_to="base_link"`，避免相机按组合 model
  frame 落到地面以下；相机固定 joint 继续使用同一 base-link transform。
- `gazebo_sensor_stream.py` 与 `GazeboSimBackend` 统一为 source/wire `top_to_bottom`，取消重复垂直翻转。
  frame-set 评估从“整个 source-attribution 对象必须完全相等”收紧为核心身份逐项严格验证、允许附加 provenance；
  行序元数据一旦出现就必须三字段成组且为 top-to-bottom/no-flip，错误值在推理前 fail-closed。
- hash-pinned disposable PX4/Gazebo v21 采集 3/3 个实际 1280×960 BGR24 camera frame；目标完整、正立，
  runtime JSON 46,503 bytes / SHA-256
  `3FBF2BDE258718EC9287703CDC5905F5B6766A1A60301145A5EB99BCD182643D`，首帧 PNG SHA-256
  `98C980BE9A3C0BBAC889A3D95D4545D1DA1CE15F5232631E290DD3C1DF787559`，容器已删除且无残留。
- 独立标注在推理前由源纹理 bbox 与 billboard 平面投影冻结并绑定首帧 SHA-256。quarantined YOLO candidate
  三控 v22 通过：present 唯一 `car`，confidence `0.8012787104`、IoU `0.8882733111`；blank/occluded
  均为 0 检测。证据 5,908 bytes / SHA-256
  `D48865EC9296C2778D7CF7D6C486D632410689EF1B23BAFC99BEE04A55365BC9`，CLI exit `0`。
- 本轮聚焦回归 `326 passed`，Ruff/compileall 通过。该结果仍是单帧 software/SIM smoke；模型继续
  `quarantined`、`production_approved=false`、`accuracy_validated=false`。未完成完整精度、测距/落图闭环、
  人工 QGC Fly View、真实硬件、室外 GNSS、HIL/REAL 或实飞；QGC 源码未改，不生成新 EXE，最终
  `v0.3.0` 继续阻断。
- 相机 model-root 改变使 v54 三机型 source 按设计失效；未复用旧 hash。新的 fixed-wing / multirotor /
  VTOL deterministic sources 重新绑定，binding 2,371 bytes / SHA-256
  `7DAB627CC1211EFDD60A98C71575EE697E5062B9CB1020AB905B7A0CFFF896B2`。更新后的 release parity
  5,098 bytes / SHA-256 `893E55ED8BDADE76DF85A5F50ADB3471D517237DE6F7B5F094C07AFE278C3397`，
  与当前共享源码一致。
- 最终全量严格回归 3,199 tests：`3198 passed, 1 skipped, 0 failed, 0 errors`，exit `0`；JUnit
  464,558 bytes / SHA-256 `2799FCA4C87F095F0218CC73FB65B03B7E6BD8C63978CE8C470B6ED5911CBBBC`。
  唯一跳过是 Windows symlink 权限项。
- E7 wrapper 默认三机型输入切到 v56，并将旧 v52 external join 保留为历史。旧 join 对当前 parity 的
  source recomputation 正确失败；失败记录 SHA-256
  `46EB3D11336985DB8F2A608011E17C862D8794C9485DDD0F5EC1722FCB771704`。从同六份不可变 software-only
  source 与当前 parity 新建的 join / revalidation SHA-256 为
  `D5120A0F72D6BE3236D74BFFAAC89644F8022881C76EAF08B32E95B67404327F` / 
  `AA925E65C0501C6A159EDE440391704B912D75F7B0690A988AB18D818BF96D46`，均通过；这不是一次新的
  external runtime 或硬件运行。
- 当前 E7 report-only 退出 `0`，但 `gate_passed=false` / `release_allowed=false`；manifest/report SHA-256
  为 `94C38CF64B1B53B862758D516CDA8DDB04952110BB5A53C4C3A6F20D6DD6492D` / 
  `AD14941F7D0726EC2AFEC3329EAC82B0D7CE72350659395A73B7E4EC700B0D87`。strict 退出 `2`，保留
  32 条实际 blocker；strict report SHA-256
  `B4BE420E2E8C0D9F9008E7AF74BED1C9849AC8789E8A3CA1E0FDEE2FB52F87DF`。quarantined candidate 未传给
  model-review，生产审核仍 absent；相关 E7 聚焦回归 `153 passed`。

### v0.3.0-pre.8 v55 PX4/Gazebo 持续 AI 控制与位置响应（2026-09-05）

- `run_simulator_docker_ai_acceptance.py` 新增 `--exercise-flight-seconds 1..30` 与
  `--minimum-flight-displacement-m`，并强制与 `--execute --exercise-arm --exercise-target` 组合；普通 dry-run
  和基础 Docker telemetry acceptance 的被动行为不变。
- truth-free synthetic target 通过公开 perception seam、生产 `SimulatorAiExecutor`、共享 authority/watchdog
  与既有 loopback `SimulatorMavlinkControlAdapter` 持续发送 `MANUAL_CONTROL`。disposable container 仅在该
  opt-in exercise 中设置 `COM_RC_IN_MODE=1`，并在同一 socket 上有界请求 `POSCTL`、等待 armed heartbeat
  确认；没有 `set_pose`、teleport、Gazebo truth 或第二条飞控路径。
- flight gate 只在第一个 accepted TRACK 时锁定最新 `GLOBAL_POSITION_INT` 基线；要求持续 target refresh、
  当前 armed POSCTL、零 mode loss、零 rejection/failsafe/watchdog、完整 heartbeat continuity 以及达到配置的
  WGS-84 位移。frame/poll 计数已分离，frame limit 提高到 100,000；失败 end time 在清理前冻结，避免证据
  elapsed 漂移。
- canonical v4 证据为 661,936 bytes / SHA-256
  `B56F20AB40B3E8AF9DF169FAAED9D4F326FFCDFDD052ED0FC6FF804FD7C44073`。runtime、AI、target、flight gate
  全部通过；8.172 s 内 68 次 target refresh、0 拒绝、最大 gap 0.485 s；557 次 accepted policy/control、
  0 AI rejection/failsafe/watchdog；PX4 `GLOBAL_POSITION_INT` 最大水平位移 0.818282 m（门限 0.5 m），
  171 个位置样本。三层 elapsed 一致，DISARM/neutral/stop/rm 完成且无容器残留。
- v4 container log 为 2,200,000 bytes / SHA-256
  `04F57BBF9F7A9FEB7EF5C6990DBAF4187D2AE8B5E3C49E922E3919B568B10EBD`。新增 frame-limit、time-freeze、
  target lease、mode-loss、heartbeat-dropout、wire/preflight 和 safety-fault 负向回归；当前相关组合
  `105 passed`，Ruff/compileall 通过。当前代码全量回归为 2,907 tests：`2906 passed, 1 skipped,
  0 failed, 0 errors`；JUnit 411,702 bytes、SHA-256
  `A2C2595C49D0B663468D09274D22976CC1B4D9128425BB23458445B1C5397F51`。唯一跳过仍是 Windows
  symlink 权限项。
- v1/v3 失败 artifact 原样保留；v2 保留为首次成功诊断但不是最终 current-code canonical。v55 没有修改
  QGC C++/QML，也没有重新链接 EXE。该门不是 camera/ranging/geolocation/Fly View、actuator、airborne、
  fixed-wing/VTOL、硬件、室外 GNSS、HIL/REAL 或 E7 完成证据；最终 `v0.3.0` 仍阻断。

### v0.3.0-pre.8 v54 Gazebo wire-v2 与非平地动态 runtime（2026-09-05）

- 三个 Gazebo 机型加入官方 3-D、50 Hz `OdometryPublisher`；新的 `MDGZSIM2` wire v2 用固定
  204-byte header 同步传递 image、CameraInfo、NavSat、world pose、body velocity 和整记录 SHA-256。
- `GazeboSimBackend` 统一执行 ENU/FLU → NED/FRD 转换；world local-up 只提供 local NED，不能作为
  AGL。AGL 只能由 canonical-qualified NavSat MSL 减去 scenario DEM MSL 得到；缺 DEM、错 scenario、
  GPS 降级、越界或坏 datum 均 fail-closed。
- Docker planner 将 scripts 目录只读挂载，并把 host-side DEM 路径/SHA 写入计划；sensor evidence 新增
  `terrain_agl_reference` 与 `odometry_local_up_used_as_agl=false`。SIM GPS 质量值是显式假设并参与
  sensor identity，不作为硬件证据。
- 新增隔离的 `configs/scenarios/validation/terrain_ranging_validation_001.*` 三通道 manifest 和对应
  GeoJSON。运行时不加载 truth/expectations；该 validation 子目录不会扩张默认三机型发布矩阵。
- 外部 VTOL wire-v2 v3 证据为 27,570 bytes / SHA-256
  `224F79095247521ED6AE320299CD1E428C4E49072375C8BB2647950E464A8525`；非平地 x500 runtime v2
  证据为 27,089 bytes / SHA-256
  `8FB0FE4338EEA71021C7CCD8515ACFB2577847A2517297126C921AE4D825C4B0`，均采集 3/3 同步帧并清理容器。
  后者报告 DEM AGL `0.2303031043 m`，明确 local-up 未用于 AGL。
- validation scenario 的 8 步确定性 AI/control 记录为 4,215 bytes / SHA-256
  `FB1F532E340F7E468DE0002824AC374C12C5960391F1EC3B1A6BBE6871CAD2D2`，repeat hash 一致、truth-free。
  当前跨 Gazebo/backend/Docker/terrain/integrated-SIM/document/parity 聚焦回归 `265 passed`；三维控制/E7
  wrapper 专项 `22 passed`；Ruff/compileall 通过。
- 三个 model SDF 因新增 OdometryPublisher 改变字节后，历史 v52 三机型证据被 verifier 正确拒绝；未放宽
  校验。重新生成的 fixed-wing / multirotor / VTOL 文件 SHA-256 分别为
  `91C8482BE3B828455045023A30B1AADB7B471764F57CCE701F7BAAA73FDEB484`、
  `AF03A19145308D217EE48FFFB8D62BD49019CA6CDA321AE821658D9276D868CF`、
  `665C9885461518769A5F370237F2B4BFCD5B31397509E4904EE4983CDCBC77A5`；新 binding 为 2,359 bytes / SHA-256
  `78066C298A9B9EEDB012B7596D9B7DA66E334DE7449EACDF1CCD59513181B00C`，仍明确 external/视觉/硬件未宣称。
- E7 默认 wrapper 的三份 3-D control 输入切换到 v54，保留 v52 parity-valid external 六源。当前 strict
  原生退出 `2`，32 个真实 blocker 不变；manifest/report/strict SHA-256 为
  `913CB8FE13245A25FA5BEA73F19ACF3294CE90A42C6EE0CF419E8EE9D34349D7` /
  `7212733034F21994D20D1A41F2B4CF789A213B67981DA379A63D731124CB1900` /
  `E20D2523E771D203698D8EB35462D83279CA2B2E350A9BA936361A2C150D8B84`。
- 完整回归为 `2888 passed, 1 skipped, 0 failed, 0 errors`（2,889 tests）；JUnit 409,003 bytes、SHA-256
  `C3D01A9F3A157A3B57225319A67FA7EE79883AAB70C025DA14E896F0C1EA4532`。唯一跳过仍是 Windows symlink
  权限项。
- 五组 release parity 重算后仍是 5,098 bytes / SHA-256
  `C28AC3C300DDACAD7B97097EC80987AED78E8EB8B19101AD98FD8D66F7985739`。QGC 源码未改，不重建 EXE。
  本轮没有飞越坡面、目标测距闭环、人工 Fly View、硬件、室外 survey、HIL/REAL 或实飞验收。

### v0.3.0-pre.8 v53 非平坦 3-D 地形资产真值（2026-09-05）

- 新增完全本地的 `terrain_ranging_validation_001.sdf` 与配套 `.dem.json`：ENU 平面北向坡度
  `+0.1 m/m`、东向 `0 m/m`，WGS-84/MSL 原点一致，并放置两个贴合坡面的高对比场景目标。
- 新增严格的独立 SDF truth parser：限制文件大小/UTF-8/XML 结构，拒绝 DTD/entity、外部 URI、
  include、嵌套 pose 和歧义 collision/visual；从 `Rz * Ry * Rx` 独立计算 ENU 法向、坡度、平面覆盖和
  解析 ray-plane intersection，不复用 DEM 插值公式。
- 新增 schema-bound acceptance：比较 25 个 DEM 节点、16 个单元中心和 7 条确定性 NED 射线，其中
  6 条实际跨越网格边界、1 条为垂直基准；
  最大高程/坡度/斜距误差为 `3.553e-15 m` / `1.388e-17 m/m` / `5.684e-14 m`。DEM、SDF 坡度和
  world-origin mutation 均 fail-closed。
- 正式 v2 证据 11,952 bytes、SHA-256
  `CCB34D5AE3088DE1A66CCF4E6D521C7E009DEF1CD25051E6DDA61DC4A526FB9B`；相关聚焦回归
  `107 passed`。证据只关闭 deterministic asset-geometry truth 子门，明确保留 external Gazebo、QGC
  visual、硬件、室外 GNSS 和 HIL/REAL 为未验收。
- 产品 parity 未变化，仍为 5,098 bytes、SHA-256
  `C28AC3C300DDACAD7B97097EC80987AED78E8EB8B19101AD98FD8D66F7985739`；QGC 源码未变，无需重建
  portable EXE。
- 完整回归为 `2865 passed, 1 skipped`（2,866 tests，失败/错误为 0）；唯一跳过是当前 Windows 账户
  缺少 symlink 权限。JUnit 404,145 bytes、SHA-256
  `2E54AD65923CDB93D1BD570A0C726FCBFD32316FB3A0EB5A8D3B13A116205092`。

### v0.3.0-pre.8 v52 共享 DEM/GNSS 坡地测距（2026-09-05）

- 新增严格 `LocalDemGrid`：要求 WGS-84、MSL、原点覆盖/高程一致、有限矩形网格与 100 km 局部域，提供
  双线性高程/坡度采样和逐单元精确射线交点；越界、向上射线、无交点和无界不确定度 fail-closed。
- `MultiModalRangingEngine` 新增 `dem_gps` 垂直源。GPS 水平/垂直误差、DEM 高程/坡度误差、相机杆臂与
  三轴射线端点共同进入距离区间；该路径不重复融合同源相对 AGL，避免人为缩小方差。
- `VehicleTelemetry`、Pixhawk、raw replay、GNSS sidecar 和 serializer 增加同一合格 raw GPS 样本的
  `gps_absolute_altitude_m`（MSL）。实时 CLI 增加严格 `--ranging-dem`，并要求同时启用
  `--multimodal-ranging`；时间错位或越界时不平地回退。
- parity 更新为 5,098 bytes、SHA-256
  `C28AC3C300DDACAD7B97097EC80987AED78E8EB8B19101AD98FD8D66F7985739`，其中
  `core_algorithm_hash=e90559d121468097f9378790e15902002aca8e6cdacd80c8d5bb8bb5426a93a1`；聚焦回归
  `458 passed`。
- 完整回归为 `2855 passed, 1 skipped`（2,856 tests，失败/错误为 0）；JUnit 402,544 bytes、SHA-256
  `E82F4F8FCF3C0639B272ECCECC06B8BFE85B8D946B78C3E8B29A1F1BFEDCE077`。
- 新的三机型确定性 binding SHA-256 为
  `2B886AEF69197268A8658E1A5C8AA24D753DE4E560934CC245A667D7AA49948B`。它明确保留
  `external_runtime_gate_passed=false`、视觉/硬件 `not_claimed`；当前平坦 DEM 不构成非平坦 3-D、室外
  GNSS/距离真值、HIL/REAL 或最终发布证据。QGC 源码未变，沿用 v51 portable。
- 既有三机型 external runtime/AI 六源按 v52 parity 重绑和独立重算通过（SHA-256
  `3B82910E3122DF3A929DA24E5D4C119916B0BE22035BBEB3C25208CE491F7A79` /
  `CE6A3E4DE25D3E60466BA20FFF70C9FC51B59A7455AC5506935E2A3C3AD460B6`）。v52 阶段 E7 wrapper 改用 v52
  固定路径；report-only 退出 `0`，strict 精确退出 `2` 并保留 32 项现场/发布阻断。

### v0.3.0-pre.8 v51 测距协议与 SIM 地理资源真实性收口（2026-09-05）

- 将三种内部射线/相机偏置不确定性失败原因规范化为认证 `RANGE_STATUS` bit 31 的
  `target_geometry_uncertainty_unbounded`；Python 与 QGC C++ 注册表同步，编码、解码和原生自测覆盖
  bit 31，避免有效的 fail-closed 结果因协议 reason 未注册而丢失。
- 三个场景新增仓库自带 GeoJSON 地图和 schema-bound `.dem.json` 高程网格；stack resolver 严格校验
  场景身份、WGS-84、world origin、数据边界、文件 containment 和 SHA-256，并把 map/DEM 纳入聚合
  asset hash 与进程环境。VTOL acceptance 不再以非空 URI 充当资源存在性证据。
- `task_contract` 新增 operator link/status，`safety_policy` 新增 DEM schema 与 simulator stack；当前
  parity 为 4,860 bytes、SHA-256
  `FA7EF5D6B1E8042F88F119D0FB9832540A1517B2320784956BD990567F77DD43`，专项 `24 passed`。
- QGC 增量构建与原生 operator protocol self-test 退出 `0`；Release/portable EXE 字节一致，均为
  46,576,128 bytes，SHA-256
  `06E7B1BBEC1B96DC6E5A231B12B88725122476075ED86A24BBEF554CB899E5FE`。
- 当前 DEM 与 Gazebo world 都是平面，只构成资源/身份 gate，不构成坡地测距或室外精度证据；真实硬件、
  GNSS、HIL/REAL、人工视觉和最终 `v0.3.0` 发布门保持未关闭。

### v0.3.0-pre.8 v50 三轴安装角误差传播（2026-09-05）

- `MultiModalRangingEngine` 将共同 `boresight_sigma_deg` 分别施加到相机安装 pitch/yaw/roll，三轴正负
  端点共同进入相机斜距、直接测距地面投影和几何方位误差；不再只扰动安装俯仰并另加固定方位标量。
- 三轴方位分量和机体 roll/pitch 分量分别通过 NED 射线中心差分计算并按平方和合并。新增回归覆盖
  banked/off-axis 固定翼几何以及“pitch 端点安全、yaw 端点越过地平线”时的 fail-closed。
- 确定性探针中方位 `1σ` 从旧近似 `2.000°` 调整为三轴几何值 `2.630°`，地面距离 95% 区间宽从
  `10.804 m` 调整为 `11.659 m`；该结论仅为 software geometry probe，不宣称室外精度提升。
- `boresight_sigma_deg` 的 per-axis 语义已写入 calibration schema、标定说明、落图文档、架构与现场流程。
- camera-calibration schema 也纳入 core parity；当前 SHA-256 为
  `0DA306BCD0A635DF70584BB14514153FB05433A452F52F7D58FB32F7B1C46336`，
  `core_algorithm_hash=f5ae7dca085db1556ad2c63ad6b08c1f155b229eb72ad78fb6bc6653e7b46044`；v50 external
  join/revalidation 均通过。完整回归为 `2828 passed, 1 skipped`（2,829 tests），JUnit SHA-256
  `862C80D0D1AAF52AA8F9ABEE2FF4E8691C6A771FD45EE5F54A2B9E49ED08577E`。
- schema-bound 汇总证据 SHA-256 为 `9400FF54A6347A9F7D0FCE116CAA497E01438EFEC9D4DF39E824BD3C408536DD`。本轮
  QGC 源码未变，沿用 v49 已验证 portable；E7 strict 仍以 32 项真实阻断退出 `2`，最终版本未发布。

### v0.3.0-pre.8 v49 模式 3 输出锁定 UI 真值化（2026-09-05）

- `MultiDetectState.fixedWingAimActive` 现在同时要求 approach active 与当前
  `fixedWingAimControlEnabled`；QGC 不会在 live provider 未认证、权限撤销或输出锁定时显示“模式 3 正在
  瞄准”。新增 locked/stale/abort/cancel/waiting 五类明确文案，状态通知也复用该 fail-closed 派生属性。
- `FlyViewCustomLayer.qml` 继续使用 QGC 原生 `QGCLabel`、`QGCButton`、palette 和 ScreenTools；没有新增
  simulator-only UI。新增静态回归验证 locked 分支先于 active 分支，且 active predicate 必须含输出权限。
- 重新链接并部署 Windows portable；Release/portable EXE 均为 46,575,616 bytes、字节一致，SHA-256
  `EE2A6A5E7B4561D56D6130FE9D81AEAC8F7093EB1A352792CB85AB353133F57D`。隐藏 offscreen simple boot
  退出 `0`。三项原生协议自测全部通过，其中 Python→C++ depth-grid 使用 44 个认证分片；自测 JSON
  SHA-256 为 `FFE34FF1B6CF7D3D933C56DB7F2BCFCE0C2D789A3B487F8E5D045C254A839667`。
- parity SHA-256 更新为 `B8D2918DBC3040596A86BB6B9FC8FB7843E660929DBCD2D677C70D3592F69167`，
  `ui_contract_hash=1dc7e2ae6edbb7ed2f4b212232ebf24fcb2fd54cf84373e4e2941af0dc88c992`；v49 external
  join/revalidation SHA-256 为 `217BBC60F506D82E5159164C7A17D06345843B1A7BFD539FCAD42F4344FACC28` /
  `302969731360683BEDD629BF54CE7ED365D39BC9BC45D6B8296EEB12E9280C29`，均为 software-only。
- 完整回归为 `2826 passed, 1 skipped`（2,827 tests，失败/错误为 0），JUnit SHA-256
  `79083F9262E924559B84C117D494D84533CAFFD47857648C19DB869250C39764`。E7 strict 预期退出 `2`、
  32 项阻断未关闭；人工视觉、硬件、HIL/REAL、室外定位和最终 `v0.3.0` 均未宣称通过。

### v0.3.0-pre.8 v48 运行 provider 独立实时再认证（2026-09-05）

- `ProfileBoundFlightControlProvider` 现在接收共享 `Clock` 并从未认证/锁定状态启动。launcher probe 的
  heartbeat/firmware freshness 不会转移到新的发送 provider；只有该 provider 自己的 receive-only
  snapshot 或显式 refresh 观察到新鲜同源 heartbeat 与匹配固件身份后，才建立带时间戳的 live
  attestation。输出命令不能成为第一次 attestation 事件。
- 未 attested 时，飞控命令、模式切换、测距 stream request 和 RC input stream request 均被阻断；
  heartbeat 超时、车辆稳定身份变化、transport close 或固件不一致会撤销 attestation 并清除缓存版本。
  同 source 重连仍必须重新观察 `AUTOPILOT_VERSION`，旧 snapshot/重认证调用不能把陈旧身份续命。
- `ProfileBoundLiveBackend`、`FixedWingAimExecutor` 和 `LiveApproachHilCoordinator` 改为动态读取当前
  `flight_control_enabled` / `output_locked`。CLI 启动事件分别输出 configured、静态
  `flight_control_authorized`、实时 `live_binding_attested`、当前 enabled/locked 和两类 reason；live
  固定翼审计事件不再硬编码控制已启用。
- PX4 Mission parity 新增 `approach_live.py` 与 `fixed_wing_aim_control.py`，当前 parity 为 4,622 bytes、
  SHA-256 `49D9B243A26591784DA0651A0B8789C5AEC0E162911199EC447C38EE370152D6`，
  `px4_mission_path_hash=544079908cbb34f9b6a28fe02e344153595cb1b983271aaa027c50d1e2247ce1`。
  v48 software-only join/revalidation SHA-256 为
  `65CCC80DC09CA1094884D82D24CC499BAD12AA382986572E75EA69CC2BDAECC6` /
  `E26B5149CBECB877529B85A0DD51B32864670110412EA62D0B3EADD056604B2F`。
- 完整回归为 `2825 passed, 1 skipped`（2,826 tests，失败/错误为 0）；JUnit 398,376 bytes、SHA-256
  `59D78D2D621C4EE6395E4EE341D3101E3CC7424531B4C2950C3D0165D9749267`。最新 E7 report-only 返回
  `0`，strict evaluator 预期返回 `2`，仍为 `gate_passed=false` / `release_allowed=false`，并记录
  `px4_mission_feedback.present=false`。误用 `0.0-pre.8` 产生的首次失败 revalidation 作为失败诊断保留，
  wrapper/canonical E7 只绑定 `-v2.json` 成功文件。这轮不关闭硬件、HIL/REAL、室外 GNSS、人工视觉或
  最终 `v0.3.0` 门；未改 QML/C++，无需重建现有 portable。

### v0.3.0-pre.8 v47 REAL 输出固件身份硬锁（2026-09-05）

- `load_runtime_profile_document()` 新增严格布尔参数 `require_firmware_identity_binding`。legacy HIL/REAL
  profile 在无输出的泛用诊断读取中仍保持兼容；REAL `flight` profile 一旦请求 output opt-in，则无论
  调用方是否显式设置该参数，都必须同时存在 profile/observed firmware hash 且完全一致。
- HIL/REAL `ProfileBoundRuntimeLauncher` 始终要求 firmware binding；REAL `observer` 仍只读，legacy
  firmwareless flight profile 会以明确原因保持 invalid/locked。`ProfileBoundFlightControlProvider`
  构造器增加独立防线，直接实例化也不能绕过该要求；动态身份比较对所有 flight profile 强制包含固件
  hash。
- wrapper 默认 external evidence 更新到 v47，当前 parity SHA-256 为
  `B40053E5EE74D09FA890E506BEACF7392288778AAE22F449016FC88D1563BBCF`，
  `px4_mission_path_hash=63674e1adedc5884561ffa62f6c6b4f35d1e4a067bb5114e29ab3a6d305917c5`；
  software-only join/revalidation SHA-256 为
  `456524B74F70F8431C186557255E067C6F70DFB4A8E6C42D93F2C16622DA1890` /
  `F4000DF499E8DEE33C4D795D49B6CE177347DEA5E661959118277E450692907C`。
- 完整回归为 `2820 passed, 1 skipped`（2,821 tests，失败/错误为 0），JUnit 397,695 bytes、SHA-256
  `557FA4D8D306193388DB3D5600A8E5AD4228C2C2E494112F4857EB201EDD8934`。E7 report-only 返回 `0`，
  strict 预期返回 `2`；strict manifest/report/strict-report SHA-256 为
  `88C4E86F007F92F9ABC5BB9D8D507BEF3E4933B9A1374F059025EE2B351C92C6`、
  `2E871C79E3F58C7520B0EF70993AF7C2B1BFE29FEB8E2BCDD75CBB58B9F72155`、
  `CC62CD5D4BBFCB026022DEDE23483746AF7E9C76C32AFF31BAD16603FF5CC931`；external evidence 通过，
  `px4_mission_feedback.present=false`。这轮是 software contract 收口，不关闭真实 V6X、HIL/REAL、
  室外 GNSS、人工视觉或最终 `v0.3.0` 门；未改 QML/C++，无需重建现有 portable。

### v0.3.0-pre.8 v46 固件身份绑定 RuntimeProfile/E7（2026-09-05）

- `RuntimeProfile` / `RuntimeIdentity` 新增可选 `firmware_identity_hash`。泛用 loader 保持 legacy
  HIL/REAL profile 可读；当 profile 携带固件 hash 时，身份匹配会一并严格比较，错配继续锁死输出。
- `px4_mission_feedback_cli.py provision` 现在强制要求同一 heartbeat source 的完整
  `AUTOPILOT_VERSION`，而且该版本帧的观察时间必须位于本次 probe 的开始与完成之间。缺失、错来源、
  陈旧/预先缓存、未来时间、接收错误或任何非零发送都 fail-closed；新签发的 HIL/REAL profile 必然包含
  隐私保护 firmware identity hash，摘要仍不包含原始 UID。
- HIL/REAL Mission-feedback v2 observer 强制要求 profile 和实时 runtime identity 两侧都存在同一个
  firmware hash。legacy profile 可用于历史诊断，但会以
  `runtime_profile_firmware_identity_missing` 阻断 E7；实时版本缺失或不匹配也有独立原因码。
- 六叶 `px4_mission_feedback` release binding 新增 `firmware_identity_hash` 与
  `all_firmware_identities_bound`，独立 verifier 会重开每份 report 并比较 HIL/REAL × 三机型的叶节点值；
  schema、wrapper 默认路径和回归同步更新。
- 当前 parity SHA-256 为
  `3A85B02436CD545E1409B842D28AC3A600DC02749B5F4A5D2130A525D4A21F95`，
  `px4_mission_path_hash=3639c0a56d8d9ab3e799242ff190838a8f9959d07a938996b36e5e86ab775e5a`；v46
  software-only external join/revalidation SHA-256 为
  `31079BDC34DA919613C130B5466C13C0003D4401DD7B2CC030DE4847A15E23F0` /
  `EBAB5126102B511A31614273C83F1DDF6D149D15344A8108A20AFE3C807F00B5`。真实 V6X、HIL/REAL 六源、
  室外 GNSS、人工视觉和最终 `v0.3.0` 仍未通过。
- 完整 Python 回归为 `2816 passed, 1 skipped`（2,817 tests，失败/错误为 0），JUnit 397,164 bytes、
  SHA-256 `18EDEF1953C0F4E4EA452AF210185B5074C4D61C3E02F331B368E802806401F1`。E7 report-only
  返回 `0`，strict 预期返回 `2`；strict run 的 manifest/report/strict-report SHA-256 为
  `CC1BB8BF9F68AD904CA90049293D7C5526005217F199EE9C3ECB369AF5778C33`、
  `CA5601CB7D3BB18CE0387E9AD8809ED3087EC55A0751544EBD6303738069AC00`、
  `F413957547B5AE31A3F5E48AA1B68BC5D1A8EA155EE4E1C51FE7B9CA54831020`；
  `px4_mission_feedback.present=false` 保持真实门关闭。

### v0.3.0-pre.8 v45 PX4 固件身份被动观测（2026-09-04）

- `PixhawkReadOnlyTelemetryProvider` 新增 `AUTOPILOT_VERSION` 的严格解析和缓存：只接受当前 autopilot
  heartbeat 同一 system/component 的完整 uint64/uint32/uint16/固定长度 byte-array 字段；来源错、越界、
  截断、观察时钟倒退和当前帧畸形均 fail-closed，heartbeat 的稳定身份字段变化会立即清除旧版本身份。
- 64-bit MAVLink 整数现在优先保留 Python 整数，不再通过 IEEE-754 `float` 丢失低位。版本报告解码
  major/minor/patch/release type，同时保留 packed 值、capabilities、custom versions、board/vendor/product。
- `uid2` 优先、`uid` 回退，但二者只进入 domain-separated SHA-256；新增对象和 JSON 明确
  `raw_device_uid_serialized=false`，不保存或打印原始设备 UID。
- `multi-detect pixhawk-check --require-autopilot-version` 增加零发送的被动固件门，并输出版本 age、拒绝
  计数和规范 identity hash。该门不会主动发送 `MAV_CMD_REQUEST_MESSAGE`；v45 截止时真实 V6X 采集与
  RuntimeProfile 绑定仍 pending。v46 已完成软件侧强制绑定，真实设备/HIL/REAL 仍 pending。
- 全量回归 `2808 passed, 1 skipped`，JUnit SHA-256
  `E1EB8D218E039A59D6A9084D837B17C965DC9D67A44330A39DA2A6B0333646B3`；新
  `px4_mission_path_hash=9dfcd64df28141ad9f08f439bf839b7a49e2b0ebdefb9cd8367d9a720e2a91f8`。
  v45 external join/revalidation 均通过，当前 E7 仍失败且 strict 按预期返回 `2`。

### v0.3.0-pre.8 v44 PX4 RuntimeProfile 现场自动签发（2026-09-04）

- `px4_mission_feedback_cli.py` 新增 `provision` 子命令和
  `multidetect-px4-runtime-profile-provision` console script。它从当前 Mission、实际 resolved endpoint 与
  新鲜 PX4 heartbeat 自动派生 airframe/endpoint/vehicle identity，生成后续 observation v2 所需的签名
  HIL/REAL RuntimeProfile，移除现场手工拼 hash 的易错步骤。
- provision probe 只使用 `PixhawkReadOnlyTelemetryProvider`，关闭 ranging stream request，并要求
  `read_only=true`、`messages_transmitted=0`、fresh heartbeat、完整 system/component/autopilot/MAV_TYPE/
  MAVLink version、PX4 autopilot 和 `STANDBY/ACTIVE`。Mission adapter 与 heartbeat MAV_TYPE 必须属于同一
  fixed-wing/multirotor/VTOL 类；错机型、错来源、陈旧 heartbeat、未解析 endpoint 或任何发送均
  fail-closed。
- HIL profile 禁止 REAL signing identity；REAL profile 强制要求 signing identity hash。HMAC 和 signing
  key 只从环境变量读取。输出先在临时文件中用同一 HMAC/observed identity 严格重载，确认有效且输出
  锁定后才原子写入；默认不覆盖既有 profile，显式 `--overwrite` 才允许替换。
- 新增 profile provisioning 回归，覆盖三机型、HIL/REAL、错误 vehicle/component/autopilot/status、陈旧
  heartbeat、receive error、非零发送、签名约束、错误 key、原子写入、CLI 环境变量与覆盖策略；相关
  Mission feedback/release wrapper 聚焦回归共 `47 passed`；`provision --help` smoke 退出 `0`。
- v44 parity 为 4,529 bytes、SHA-256
  `BB5D6285E81B813494BF56432F76FB045F7F4DAC454553F43B4041728F8C2F62`，
  `px4_mission_path_hash=a8d5a95d18e7efb5c829f53a8402c0d8bc3019d6012186351196a20c553b1f87`；
  software-only external join/revalidation SHA-256 为
  `3F01945CAC2E79295279418154D6DFF3F490DEF09FFE0F3AB71A2580FA2F28E8` /
  `D6240C13461B2633DE80D431CD5877AF6694703F71EDFD4E28FC7AA6BAA8B4EE`。
- v44 E7 manifest/report/strict-report 为 21,036 / 54,830 / 54,830 bytes，SHA-256 为
  `432A9835B2D061B15E8CB9D54759FBF36D9D1E511F0FD09E26E14953DB0DACDF`、
  `423D15645792457F7107BF7E1C9A59F0D1ED125F87733A72B62B80DF26CB4527`、
  `2D5510308BC3CB49C517F69C86EEE09F2941274A41193489970520F6972D72BC`；strict 预期退出 `2`，
  `px4_mission_feedback` 仍 absent。生成 profile 不等于 HIL/REAL/硬件验收，最终 `v0.3.0` 保持未发布。
- 完整 Python 回归为 `2793 passed, 1 skipped`、退出码 `0`；3,226-byte 最终汇总日志位于
  `artifacts/evaluation/terminal-continuation-v44/full-regression-v44-runtime-profile-provision-final-20260904.log`，
  SHA-256 为 `C437BD893FF13E5E161161F6A762A821B661CC1C44A7534FF5D27573A4938C5F`。本轮未改
  QML/C++，现有 QGC portable 无需重建。

### v0.3.0-pre.8 v43 HIL/REAL Mission feedback 运行身份绑定（2026-09-04）

- 新增 `src/multidetect/px4_mission_feedback_runtime.py` 与
  `configs/schemas/px4-mission-feedback-observation-v2.schema.json`。v2 报告把 receive-only PX4 Mission
  feedback 同时绑定到签名 RuntimeProfile 文档/规范 profile/hash/key ID、完整 heartbeat 派生车辆身份、
  configured/resolved endpoint + baud、Mission airframe profile 与输出锁决定；profile HMAC 和 REAL
  signing key 只从环境变量读取，证据不保存原始密钥。
- HIL 叶节点只接受精确 `hil` profile、禁止 signing identity；REAL 只接受精确 `flight` profile，但
  观测窗口统一以 `allow_flight_control_output=false` 加载，因此合格证据必须保持
  `flight_control_enabled=false` / `output_locked=true`。端点、heartbeat、source、airframe、profile 文件、
  派生 hash 或锁状态不一致均 fail-closed。
- `px4_mission_feedback_cli.py` 在 HIL/REAL 模式强制要求 `--runtime-profile`、
  `--runtime-profile-hmac-key-env` 和 `--runtime-profile-key-id`；REAL 另需 signing identity hash 或 signing
  key env。schema v1 继续作为历史诊断读取，但 E7 明确拒绝 v1。
- `px4_mission_feedback_binding.py` schema 升至 v2。E7 每项输入从
  `MODE VEHICLE MISSION REPORT` 升为
  `MODE VEHICLE MISSION SIGNED_RUNTIME_PROFILE REPORT`；六份 profile 与六份 report 必须各自不同，独立
  verifier 会重开三类来源并重算 identity/hash/bytes。release manifest/schema、PowerShell wrapper、
  parity 文件组和回归同步更新。
- 当前 parity 为 4,529 bytes、SHA-256
  `B4E080286DAEA35AF55734DE1C3B0AF05427373A10F90A5E186712AF5E6E7D5B`，其中
  `px4_mission_path_hash=c5cb36fed3a1c8576178a1021908529ea7d8e12dd3035a39ab4ee5bd5d1f3ccb`；
  v43b software-only external join/revalidation SHA-256 为
  `DC56F383B41D9042F0ED0B385762C78DC9987F1BF2C2E3E6A1FAD5625370159C` /
  `C58FB2F3ACB6DF8668776C01633499385896ABE25940CD8603C0C300E9230467`。
- 当前 E7 manifest/report/strict-report 为 21,038 / 54,833 / 54,833 bytes，SHA-256 为
  `C649E62BF1E4C88DEB5CE6BE173B2987DF55826DF9799812934477981CDFF996`、
  `89E9A82B4675C0DBB4F1FB36301C9F98BDC6BEBF9CE31F49808F3B470FAFD06B`、
  `C609F0BA2E1B7F8E7886797FFD8A1AEE3B7812B85F72AEEDD92CF1DCB3211814`；严格门按预期返回 `2`，
  `px4_mission_feedback` 仍 absent，总门保持关闭。完整回归为 `2772 passed, 1 skipped`、退出码 `0`；
  3,163-byte 汇总日志 SHA-256 为
  `399453AB6E33089442CDB13795F242683777C0D2A15D948AB4BF5963BDD7EA95`。
  本轮无 QML/C++ 变化；真实 Pixhawk/Jetson、室外 GNSS、HIL/REAL、实飞、人工 Fly View 和最终
  `v0.3.0` 继续 pending。

### v0.3.0-pre.8 v42 HIL/REAL 三机型 PX4 Mission 反馈发布绑定（2026-09-04）

- 新增 `src/multidetect/px4_mission_feedback_binding.py`：将 v41 的最小 receive-only 观测单元直接
  汇入 E7 `px4_mission_feedback`，要求且只接受 HIL/REAL × fixed-wing/multirotor/VTOL 六个组合。
  每对来源会重新打开 Mission 与观测报告，绑定文件 SHA-256/bytes、Mission ID/hash/item count、
  当前 airframe adapter、runtime mode、MAVLink source、feedback/evidence hash 和 read-only/
  zero-transmit 状态；六份报告必须不同，缺失、重复、篡改或错配均 fail-closed。
- `release_gate_manifest.py` 增加可重复 CLI 参数
  `--px4-mission-feedback-evidence MODE VEHICLE MISSION REPORT`；
  `run_current_release_gate_pre8.ps1` 增加六项
  `-Px4MissionFeedbackBinding 'MODE|VEHICLE|MISSION|REPORT'` 输入。prerelease 可以暂缺该记录，
  但已提供的失败记录在所有通道都阻断；最终 release 强制要求完整通过。
- `release_gate.py` 会独立重验六对来源，并把记录内的 `px4_mission_path_hash` 与 REPLAY/SIM/HIL/REAL
  四份模式证据的统一 hash 比较；`release-gate-manifest.schema.json` 同步使用封闭对象和精确六项约束。
  parity 的 PX4 Mission path 组已纳入 binding module/schema。
- 新增八个专项回归，覆盖完整矩阵、CLI/schema、源文件后改、缺组合、错误 adapter、四模式 path hash
  脱离、最终发布缺门和 `bool` 冒充零发送。完整 Python 回归为 `2764 passed, 1 skipped`、退出码 `0`；
  带精确汇总的日志 SHA-256 为
  `310EA89D0414C068B6D06196152863E0B6003E19A3CF2D8BB2E8253FEA19ABAC`。
- 当前 parity 文件 SHA-256 为
  `C2BAC5A77719B24B9F899B278053DE5CC3526FEDB2C46371AF6D94090DDAB8E9`，
  `core_algorithm_hash=19d5e6c4e22e491affce746acb566d179ffff5975e3128a3bf997209ee4b93f8`，
  `px4_mission_path_hash=9ff62c8ac9daea498732a9f5690ef195b8091dd94e621947e5d2ea3fb11500e8`。
- v42 report-only E7 manifest/report/strict-report SHA-256 分别为
  `9F8330CEC3D4175E314F78C00C99A3D6CCFBCE711D08F0D6C5E4A0417B859A2A`、
  `8CBBD0DD0468796EDAF596C6A6359EF4A7A9D27CA32168DD1F953D5F55C8FB7E`、
  `27E5A41035A6D31251F85F0913CB0B25256B15B3FD7DC762E93F07F95167BE5C`。当前没有六份真实
  HIL/REAL 报告，故该子门显示 absent、总门保持关闭；没有将 fake/Docker/历史数据写成现场通过。
- v42 external join/revalidation 是保存的 software-only 六源对当前 parity 的重新计算，SHA-256 为
  `4843163B46915F42553144C2388F95329C8B28A89C3EBDA34938ECF3297955B3` /
  `BC0CBFED82D24DC9C970EBAB11726D0C708ECD4480FB21C9815D697E4966DD40`，不是新 Docker 或硬件运行。
  本轮无 QML/C++ 变更，无需重建 QGC；真实 Pixhawk/Jetson、室外 GNSS、HIL/REAL、实飞、人工
  Fly View 与最终 `v0.3.0` 继续 pending。

### v0.3.0-pre.8 v41 PX4 Mission 实机反馈契约（2026-09-04）

- 新增 `src/multidetect/px4_mission_feedback.py` 与
  `configs/schemas/px4-mission-feedback.schema.json`：只读消费 `MISSION_CURRENT` /
  `MISSION_ITEM_REACHED`，绑定 executable Mission ID/hash/item count、MAVLink source identity、
  单调时间和 `<=1.5 s` freshness；换源、序列回退、越界/reached 超前、倒退时间或 stale 输入均
  fail-closed，不产生控制输出。
- `PixhawkReadOnlyTelemetryProvider` 增加可注入的 feedback tracker，并为 current/reached 游标分别
  记录时间；旧游标按 provider stale timeout 清除，HEARTBEAT 新鲜不再延长 Mission 数据寿命。
  diagnostics 同时携带 current/reached age 与 hash-bound feedback 文档。
- `MissionProgressConfig.require_px4_feedback` 提供可选严格门；同一决策时刻、同一 Mission 的合格
  feedback 可作为进度佐证，不合格反馈阻断已启动任务，反馈仍不能自行推进或产生飞控命令。
  mission-progress 与 3-D scene-stream schema 支持可选嵌套反馈并保持旧快照 hash 兼容。
- raw replay、普通 replay、CLI 输出与 Pixhawk bench 增加 current/reached 时间字段。完全缺失的新字段
  在重新序列化旧 schema-v1 记录时继续省略，历史 `recording_hash` 保持不变；真实观测后才写入。
- 新增现场入口 `multidetect-px4-mission-feedback` / `python -m
  multidetect.px4_mission_feedback_cli`，以及同模块的 `verify` 子入口和独立 verifier console script。
  它可读取裸 `ExecutableMission`、
  `CompiledPx4Mission` 或完整 `PlannedMissionCompilation`，在有界时间窗内只读监听 PX4，原子写出
  `px4-mission-feedback-observation.schema.json` 报告。验证时会重算 Mission/feedback/evidence hash、
  消息计数、只读状态和 gate reasons；`messages_transmitted` 必须为 `0`，hardware/HIL/REAL/release/
  人工视觉 claims 永远为 `false`。`--report-only` 只允许保留不合格诊断而返回 `0`，不会伪造通过。
- 本机 loopback/no-data provider 冒烟已生成
  `artifacts/evaluation/terminal-continuation-v41/px4-mission-feedback-report-only-smoke-20260904.json`
  （2,253 bytes，SHA-256 `D34FB2237628BF7367F178E42EE8640B43EA4FEDD537FC75253A755655EED6E1`）；
  schema/独立 verifier 通过，严格 verifier 按预期返回 `1`，报告保持
  `feedback_gate_passed=false`、`mission_feedback_unavailable`、read-only/zero-transmit。它不是 PX4/HIL
  成功证据。
- 新增来源绑定、freshness 边界、篡改、序列错误、provider 接线和 replay 兼容性回归。完整 Python
  回归为 `2756 passed, 1 skipped`、退出码 `0`；日志 SHA-256 为
  `079ECE9B161174A42BFEAA4B53E270E7B13C0F577CD9622759F750C25E9B9961`。当前 parity 文件 SHA-256
  为 `B5FE05726534F5781995E56C85D3CA7043553B766C548B17083711FCAFF53776`，
  `core_algorithm_hash=19d5e6c4e22e491affce746acb566d179ffff5975e3128a3bf997209ee4b93f8`，
  `px4_mission_path_hash=1fa6bfe715ace25dc0e1c0818571e25af113d4ab1e104d40da002e5a5c765b7b`。
- 本轮没有 QML/C++ 变更，无需重建 QGC；这些仍是 software-only 证据。真实 Pixhawk/Jetson、室外
  GNSS、HIL/REAL、实飞、人工 Fly View 和最终 `v0.3.0` 发布门继续保持 pending。

### v0.3.0-pre.8 v40 3-D 场景流携带 Mission 进度（2026-09-04）

- `SimulatorSceneSnapshot` 增加可选的 `mission_progress`（省略表示无 Mission，出现时必须是完整对象，
  不接受显式 `null`）：有编译 Mission 时逐帧保存同一份严格
  `MissionProgressSnapshot`，把当前序列、GPS/link freshness、距离、驻留和完成比例一起带入
  3-D replay stream；没有 Mission 的历史快照继续省略该字段，旧 canonical hash 保持不变。
- `configs/schemas/simulator-scene-stream.schema.json` 增加内联 `mission_progress` 定义并保持
  向后兼容；读取器会重用 `MissionProgressSnapshot.from_mapping()`，未知字段、非法比例、伪造
  hash-bound Mission 或 truth 字段继续 fail-closed；同一条流还要求各帧 Mission ID/hash 一致，
  不允许把不同任务或缺失帧拼接到一起。
- `python -m multidetect.simulator.scene_state` 增加 `--mission` / `--start-mission`，因此
  直接导出的 3-D 场景流也能复用 runner 的显式 Mission start 语义；省略启动开关仍只生成
  `ready` 观测，不隐式控制飞控。
- 新增场景流 Mission 进度 round-trip、schema、CLI 和篡改回归。该层仍是 truth-free 的
  software/SIM 证据；外部 3-D、PX4 `MISSION_CURRENT`、真实 GNSS/Pixhawk、HIL/REAL、人工
  Fly View 和最终 `v0.3.0` 发布门继续保持 pending。
- v40 场景流 smoke 已用现有编译 VTOL Mission 生成并独立重验：
  `artifacts/evaluation/terminal-continuation-v40/scene-stream-mission-smoke-20260904.json`
  （2 帧、schema/verifier 通过，文件 SHA-256 `C7A413D9755B9379E6EB32ED4330115928563615201DA1E904F9DA95D34ED9B2`）。
  同一工作树最新全量 Python 回归为 `2727 passed, 1 skipped`、退出码 `0`，日志
  `artifacts/evaluation/terminal-continuation-v40/full-regression-final-20260904.log`
  （SHA-256 `B28F57D34E30F7116A3735FB3ADA401F0F684E83406187BC53B4327BEA32CD24`）。

### v0.3.0-pre.8 v39 场景运行器接入编译 Mission 进度（2026-09-04）

- `SimulatorScenarioRunner` 新增可选 `mission` / `mission_progress_config`，并提供
  `bind_mission()`、`clear_mission()`、`start_mission()` 转发；编译出的 `ExecutableMission` 与
  SIM 场景共享同一个 authority → adapter → MAVLink sink → flight-model 生命周期。
- `python -m multidetect.simulator scenario` 增加 `--mission` 与 `--start-mission`，可直接读取
  `ExecutableMission` 或完整 `PlannedMissionCompilation` JSON；省略启动开关时只记录 `ready`，
  不产生隐式任务控制。
- 场景步骤现在带有 `mission_progress` 快照；未显式调用 `start_mission()` 时保持 `ready`，不会
  因 authoring artifact 存在而隐式上传 Mission、发出控制或绕过安全门。
- 增加 runner 绑定/启动/步骤证据与无 Mission 配置闭锁回归；补强 Mission progress 配置类型和
  home 坐标成对校验。能力仍为 truth-free、observation-only software/SIM 观测，真实 GNSS、
  Pixhawk、SITL `MISSION_CURRENT`、HIL/REAL、人工 Fly View 和最终 `v0.3.0` 发布门保持 pending。

### v0.3.0-pre.8 v38 编译 Mission 的同核进度观测链（2026-09-04）

- 新增 `src/multidetect/mission_progress.py`：将 hash-bound `ExecutableMission` 与
  truth-free telemetry 绑定为确定性的 `MissionProgressSnapshot`，统一推进 VTOL、fixed-wing
  和 multirotor 的任务序列；坐标项使用共享 WGS-84 inverse，支持起飞、航点、盘旋驻留、VTOL
  转换、返航和降落。
- 进度层沿用全局 GPS/RANGE 安全语义：GPS 必须满足 `fix_type>=3`、卫星数 `>=6`、水平/垂直
  精度上限和 `1.5 s` 新鲜度；链路不健康或车辆未解锁时保持 `WAITING_FOR_*`/`BLOCKED`，不
  自动推进，不产生 MAVLink 或 actuator 输出。
- 新增 `mission-progress.schema.json`，并将 `MissionProgressSnapshot` 暴露到根包、SIM 包；
  `SimulatorClosedLoop(mission=...)`、`bind_mission()`、`start_mission()` 和每 tick 证据现在
  复用同一个进度快照与 canonical hash。SIM 仍通过原有 authority/adapter/MAVLink sink，
  没有新增 simulator-only UI 或控制旁路。
- 新增 `tests/test_mission_progress.py`，覆盖三段式任务推进、GPS/链路闭锁、VTOL 转换、驻留、
  极点坐标拒绝、快照重载/篡改和 closed-loop 绑定；专项回归 `7 passed`，与既有 closed-loop/
  planner 回归合计 `30 passed`。
- v38 parity 已把 `mission_progress.py` 与其 schema 纳入 `core_algorithm`：当前
  `configs/release-parity.json` SHA-256 为
  `A95880A967954CFD4F8F11A6C8C35B12C5F1AFB1E0A1FE5D656DBA31012A8CD0`，
  `core_algorithm_hash=0f29c28f30c581f292fe9496ca5f14b7ee3b12bfd6ae72bc19300b82cf67031e`。
- 版本继续保持 `v0.3.0-pre.8`；真实 GNSS、Pixhawk、HIL/REAL、人工 Fly View 和最终
  `v0.3.0` 发布门仍按总门定义保持 pending。

### v0.3.0-pre.8 v37 文件化 Planner → PX4 Mission 编译入口（2026-09-04）

- 新增 `MissionCompileRuntime v1` 严格输入契约：将 runtime home、VTOL/fixed-wing/multirotor
  geometry 与对应 safety policy 收在一个 JSON 文档中，顶层未知字段、truth/expectations 和
  vehicle-class 不匹配都会 fail-closed；Schema 位于
  `configs/schemas/autonomy-mission-compile-runtime.schema.json`。
- `VtolMissionGeometry`、`FixedWingMissionGeometry`、`Px4MissionSafetyPolicy` 和
  `Px4FixedWingSafetyPolicy` 增加严格 `to_mapping()` / `from_mapping()`，保留所有默认包线参数，
  因而同一份 runtime 文档可以在 REPLAY/SIM/HIL/REAL authoring 端重载而不靠手工拼 dataclass。
- 新增 `multidetect.mission_planning.parse_mission_compile_runtime(...)` 与
  `compile_planned_task_mission_from_runtime(...)`，在既有 typed seam 上统一解析并复用三机型
  canonical PX4 compiler；仍只生成 hash-bound artifact，不打开端口、不上传、不发送飞控输出。
- 新增 `multidetect-mission-compile` / `python -m multidetect.mission_compile_cli`，从严格
  `ResolvedTaskSpec`、`ContextSnapshot`、`MissionCompileRuntime` 和 airframe SHA-256 生成可由
  `PlannedMissionCompilation.from_mapping()` 重载的 JSON；CLI 只负责 authoring/审计。
- `tests/test_mission_planning.py` 新增三机型 runtime/policy round-trip、CLI artifact 重载、
  机型错配和未知字段拒绝；新 CLI 与 runtime schema 纳入 `px4_mission_path` parity。
- v37 parity 已按生成器重算：`configs/release-parity.json` SHA-256 为
  `AD1ED71E79A8E048D5F50365F6BE9B80F69AB505917C7CA476B66B1B57A91310`，其中
  `px4_mission_path_hash=e533372c6b49bdfa05760f02328305e5ec012777487a8e708a829773a4f22ada`；VTOL CLI smoke artifact 为
  `artifacts/evaluation/terminal-continuation-v37/mission-compile-smoke-20260904.json`
  （SHA-256 `BEA3FFBE5D42464534E366FF3BEEAB661EA474A6AEC9CB281D959E93E7ED0B1C`），
  `from_mapping()` 重载、8 步 plan 和 7 个 wire item 均通过；同一源码的完整 Python 回归为
  `2713 passed, 1 skipped`、退出码 `0`，日志 SHA-256 为
  `7A02B4E7C4D10D5E7656FA3B91234A5766B5D0A8D0FBECA681902EF55A534BB8`（日志为
  `artifacts/evaluation/terminal-continuation-v37/full-regression-v37-mission-compile-20260904-rerun4-summary.log`）。

### v0.3.0-pre.8 v36 确定性 Platform-Neutral Planner（2026-09-04）

- 新增 `src/multidetect/autonomy/planner.py`：四类 `ResolvedTaskSpec`（区域搜索、走廊视觉搜索、
  目标观察、测绘）统一编译为 `PlatformNeutralPlan`，VTOL、fixed-wing、multirotor 共享同一
  连续步骤和 task/context hash；机型差异继续留给各自 PX4 Mission compiler。
- 新增 `multidetect-plan` / `python -m multidetect.autonomy.planner_cli`，从严格 task/context
  JSON 生成 schema-compatible plan，并可单独落盘审计用距离/能源/时长估算。
- 新增 `multidetect.mission_planning.compile_planned_task_mission(...)` typed seam：同一份
  task/context 先生成 platform-neutral plan，再按 VTOL、fixed-wing 或 multirotor 复用现有
  PX4 Mission compiler；返回 plan、estimate、executable mission 与 wire hash 的绑定结果，
  不打开端口、不上传、不产生飞控输出。`tests/test_mission_planning.py` 覆盖三机型和跨上下文
  绑定拒绝。
- 将 `autonomy/planner.py` 纳入 `core_algorithm` parity、将 `mission_planning.py` 纳入
  `px4_mission_path` parity；v36 阶段 `configs/release-parity.json` SHA-256 为
  `66DB8D58198B183E8322D4CE2575DA98FD940F56120F05CF3326F5DCB41F22E2`，对应
  `core_algorithm_hash=FE16E5D9D59EEAE67C6AC613C029912704CD68A5A53EBD28BF70603AF8A5B7BA`、
  `px4_mission_path_hash=F2B692965D3FD761AA76F8D5CCBB5EA9112C76B51497BA62681A39BF8DDD9E3C`。
- Planner 先执行 fail-closed 任务就绪、几何置信度、GPS fix/水平精度、地理围栏、相机/感知/
  地图/DEM、返航电量与时长检查；航段使用 WGS-84 inverse，声明长度不会被稀疏 centerline
  低估，逆风和 VTOL 前后转换纳入保守预算。
- `on_detection` 只允许目标观察、证据采集、定位上报和 revisit，禁止把 return/land/divert
  等生命周期动作混入检测回调；新增 `tests/test_autonomy_planner.py` 覆盖全部任务类型、
  三机型 byte-identical 结果、别名、资源/能源/时长和非法动作拒绝。
- 已用真实 task/context JSON 通过 `planner_cli` 端到端 smoke：计划可 round-trip 解析，8 个步骤从
  `takeoff` 连续到 `land`；证据清单为
  `artifacts/evaluation/terminal-continuation-v36/planner-cli-smoke-20260904.json`（SHA-256
  `CD1FC9418EF6A9199210F0663A9E53836A288261B472848E3C3806061232DAEB`）。全量 Python 回归为
  `2709 passed, 1 skipped`，最新重跑日志
  `artifacts/evaluation/terminal-continuation-v36/full-regression-v36-planner-20260904-rerun7.log`
  （SHA-256 `D531C53B29FB887FB612E9064E9EC6F6DC53632809641F9B40EEA8E55651ABFB`）。
- 该模块只生成不可变计划/估算，不产生 MAVLink 或飞控输出；固定翼 Dubins/覆盖/进近、VTOL
  转换轨迹、多旋翼姿态控制、真实 PX4/Gazebo、HIL/REAL 和最终 `v0.3.0` 发布门仍保持 pending，
  版本继续为 `v0.3.0-pre.8`。

### v0.3.0-pre.8 v35 加速度预算完整性门与 parity 重绑（2026-09-04）

- 当目标帧与 GPS 时间对齐时，若 telemetry 提供了
  `acceleration_uncertainty_mps2` 却缺少完整 N/E 速度，目标投影现在 fail-closed，避免静默丢掉
  `0.5·dt²·σa` 误差预算；完整速度仍按 `hypot(|dt|·σv, 0.5·dt²·σa)` 合并，旧 JSONL 缺省保持兼容。
- 重新生成 `configs/release-parity.json`：SHA-256 为
  `D9CF6D9E625E6B0ADB56084628CCB39ED358E2156AF89201633C2F6E1F2B9AEF`，
  `core_algorithm_hash=7D35526D63E3BFE247776F1ED2CC14F2950E0E61AB72E10A170C5C41BB930CF3`。
- v34 历史文件保持不变；当前 parity-valid external SIM/runtime + AI join/revalidation 写入
  `artifacts/evaluation/terminal-continuation-v35/`，SHA-256 为
  `B459F6CDA7E6BD60EE5BED486E56F25870D388E31650F54333AC2FA3741CD12B` /
  `D825A2A8A1D8E3F152C35C6C8C70C3D847D9C26E3329A4F8A7EE2D27974B338C`，独立重算均通过，仍为
  software-only。
- wrapper 默认切到 v35 external 输入；E7 report-only manifest/report 位于
  `artifacts/evaluation/terminal-continuation-v35/release-gate-current/`，SHA-256 为
  `B4801000CCF23B3243ADCBAA4788C00539973A0BA1A3D6D48233758782A728E4` /
  `2E4CCE1FA23899958BBF05E5F399171CA462B83745D6435509BF5AEEC2FE71E7`。parity/external 子门通过，
  但合成 replay-set、现场硬件、人工视觉、HIL/REAL 和 prerelease channel 仍关闭总门。
- 目标定位/回放专项、全量 Python 回归、ruff 和 compileall 均通过；版本保持
  `v0.3.0-pre.8`，没有发布最终 `v0.3.0`。

### v0.3.0-pre.8 v34 运动补偿不确定度与 replay-set 校验重绑（2026-09-04）

- `VehicleTelemetry` 新增可选 `acceleration_uncertainty_mps2`。目标 WGS-84 投影均值仍为恒速
  模型，但把 `0.5·|dt|²·σa` 与速度不确定度按平方和合并，覆盖固定翼转弯、垂起转换和急加减速
  的未建模运动误差；字段必须有限且非负，非法 live/replay telemetry fail-closed，旧 JSONL
  省略字段保持兼容。
- 更新 `target_geolocation.py`、live/operator-status 转发、raw replay mapping/schema 与目标落图
  文档；新增 acceleration 数值/非法输入/operator-status 回归。
- 从同一已保存 Gazebo capture 重新生成 v34 replay-set 副本，按当前 telemetry identity 重算
  recording hash。历史 `artifacts/replay-sets/sim-gazebo-2026/` 保留不改；当前副本的 manifest
  SHA-256 为 `F577BF8085F33311D20AE27BB882C5C5F0F7225FC6F86B379A5384982E1FEDFF`，canonical
  hash 为 `F2834DC4CD03C95DC6502C137EAA66430A3F68CC2F145498889156621FA32E4C2`，synthetic 空
  基线仍保持 `accuracy_validated=false` / `gate_passed=false`。
- v34 external SIM/runtime + AI join/revalidation 的 SHA-256 为
  `80599422FD5D39E3445F8D3ADA5473CAC0F62D184023A2B2F2BD4CFF6E46C1FE` /
  `06D4C67C5A095661F5D54A691C334C8F6D2576E5821CB365166C8CE2E28E1BAE`，均通过独立重算且
  仍是 software-only。
- v34 阶段 `run_current_release_gate_pre8.ps1` 无参数默认切换到 v34 replay-set/accuracy/calibration 和
  external 输入。report-only E7 manifest/report（目录
  `artifacts/evaluation/terminal-continuation-v34/release-gate-current/`）SHA-256 为
  `7C08D3254C49DB9531D1B1032939FF1D4C01426164299C0EEF212A0B6C661FBF` /
  `12C8C52A9F58EEA97EC6AF863295BAAA73605CC65D1AC0B83408BD48D75156C5`；总门仍关闭，版本仍
  `v0.3.0-pre.8`。

### v0.3.0-pre.8 v32 导航速度残差不确定度膨胀（2026-09-04）

- `NavigationVelocityFusionEngine` 在 GPS/VIO/空速候选通过 `3.5σ` 成对一致性门后，继续
  计算 North/East 两轴残差 `χ² / (2*(N-1))`。当实际分歧超过声明协方差时，按 Birge
  ratio 放大融合速度 `σ`，追加 `navigation_velocity_uncertainty_inflated`，并把结果保持
  为 `DEGRADED`、`advisory_only=true`；该路径不产生飞控或物理释放输出。
- GPS 速度源复用全局质量底线：`fix_type>=3`、卫星数 `>=6`，`255` 未知哨兵拒绝；配置
  不再允许通过放宽默认值把未知/二维 fix 提升为室外可控导航源。
- 新增 `tests/test_navigation_fusion.py` 与
  `scripts/create_navigation_residual_acceptance.py` 的 nominal/spread 对照验收：18 个
  专项测试通过，spread case 的 σ 从 `0.2121` 放大到 `0.2828` m/s。
- 验收记录：
  `artifacts/evaluation/terminal-continuation-v32/navigation-residual-inflation-acceptance-20260904.json`
  （3,351 bytes，SHA-256
  `AA1E0EF13A40F9EDBD777B5D214F1601299E280DFF8E4B2B40353FF5037D47C7`）。当前 parity
  SHA-256 为 `723CC760FE277DB378BB3FA55BDB0D54149F72AF38C29D2EEC67EC95BA434B4B`，core hash
  为 `354d6a61f1dc986eca15e30fefff7ee820e7d83d0c3a18f4071904fc2a194d35`。
- 基于新 parity 的 VTOL live-AI 源、三机型六源 external join 和独立 revalidation 均通过，
  源/join/revalidation SHA-256 分别为
  `081B922CD725ACD1F42ED218F142E7AE1E8E5A684CA02F622B4AECE738E12DA` /
  `7AA749EB13AA18A42A734D890401588B9885A7805969CEE2BAF3AB500923E392` /
  `F5D1F057D85498C8E1A8BC563D950419E2D5AAAA9FDBBB28D13DF768D8A7348F`，均位于
  `artifacts/evaluation/terminal-continuation-v32/`。
- 当前 wrapper report-only E7 快照为
  `artifacts/evaluation/terminal-continuation-v32/release-gate-current-20260904T070708Z/`，
  manifest/report SHA-256 分别为
  `064FD23D88A0FA65280D4493A6AB9330748DC8D23C1FA261557DC8E7E515EBA4` /
  `87D0D7F61FC802DBA66D54A592D3ED0AB30EB83115ADEBB091162D1E580D9187`；external、
  scene-stream set 和 parity 子门通过，但总门仍 `gate_passed=false`、`release_allowed=false`，
  现场硬件、真实 GNSS/Pixhawk、HIL/REAL、人工 QGC Fly View、replay-set 和最终 release
  channel 继续 pending。严格全量回归 `2670 passed, 1 skipped`，退出码 `0`，日志
  `artifacts/evaluation/terminal-continuation-v32/full-regression-v32-navigation-residual.log`
  （3,078 bytes，SHA-256
  `F1B63141BC7B6B05B897711C30EF97BB2230F779633CA71833537B62EE360656`）。
- Windows portable 构建脚本以 `-SkipConfigure -BuildSelfTests -InstallPortable -Parallel 4`
  复核成功（退出码 `0`、`ninja: no work to do`）；Release/portable 主 EXE 保持字节一致，
  46,575,616 bytes、SHA-256
  `4D7A024F80157E5EB8671B77D6E1817FB960ECE8A8A97D0F470A222E7F7C8E40`。
  构建后 `--offline --simple-boot-test` 退出码 `0`，日志
  `artifacts/evaluation/terminal-continuation-v32/qgc-simple-boot-v32.log`（350 bytes，SHA-256
  `FA85CF0BA419D1335A2695DF9C9E44FAB4F2BA19C4EB1CBFA08DA60D3CF762F7`）。

### v0.3.0-pre.8 v33 现场链路只读预检刷新（2026-09-04）

- 台式机重新执行现场 Ethernet/协议/被动 route watch（2/2 samples）。接口诊断和报告结构
  校验通过，但 `Ethernet=192.168.1.13/24` 不属于期望的 `192.168.144.0/24`，没有候选
  aircraft 网卡/route；Jetson `:22`、GR01 `:5760`、相机 `:554` 均超时，最终保持
  `hardware_link_ready=false`。本轮只读，不改网卡/IP/路由，不登录 Jetson，不请求 MAVLink
  stream，不启动 RTSP，不发送飞控控制数据。
- 现场报告：
  `artifacts/evaluation/terminal-continuation-v33/field-link-preflight-live-20260904T-current.json`
  （12,923 bytes，SHA-256
  `2B3694E74332B96B3257F405842B3584A44AE07D5FCEA8EC5E47B07DF31E651A`）；接口诊断：
  `artifacts/evaluation/terminal-continuation-v33/field-link-interface-diagnostic-live-20260904T-current.json`
  （4,412 bytes，SHA-256
  `9800D930FD0FAF1D945420BF3849175F165DC463AEBD3308411143BDD99E9D68`）。
- 用 v33 现场报告重新绑定的 E7 report-only 快照位于
  `artifacts/evaluation/terminal-continuation-v33/release-gate-current/`，manifest/report
  SHA-256 分别为
  `C66DB565A12DC664C28F4DB12E38AD7E6611E28A14F650839362A18EDB817C19` /
  `340CE3C01968194B77010A808AE8A09BD62F269D9D56C786224E88099A3827D1`；结构通过但总门仍
  `gate_passed=false`、`release_allowed=false`、31 条阻断，版本仍为 `v0.3.0-pre.8`。
- `scripts/run_current_release_gate_pre8.ps1` 无参数默认改为 v33 现场报告；v32 parity-valid
  external SIM 输入保持不变。接通飞机网段并确认实际机载 IP 后再重跑现场预检，当前不关闭
  真实 GNSS/Pixhawk、HIL/REAL 或最终 release 门。
- 无参数 wrapper 烟测输出到
  `artifacts/evaluation/terminal-continuation-v33/default-wrapper-smoke/`；manifest 保持
  `C66DB565A12DC664C28F4DB12E38AD7E6611E28A14F650839362A18EDB817C19`，report 为 54,098 bytes、
  SHA-256 `ABAA3EE7AFE0387DBABB9F34CED6C8C8B6CECDF91851DC13C4AFB5DE85D15C1C`，并明确保留
  `hardware_link_ready=false`。

### v0.3.0-pre.8 v31 多源测距残差方差膨胀（2026-09-04）

- `MultiModalRangingEngine` 的逆方差融合现在在硬一致性门内继续计算加权残差
  `χ²/(N-1)`。当传感器声明的 `σ` 无法解释实际分歧时，融合 `σ` 按 Birge ratio
  保守放大，并把结果标记为 `DEGRADED`，原因分别为
  `absolute_range_uncertainty_inflated` 或 `direct_range_uncertainty_inflated`。
  单源结果和残差可由声明协方差解释的正常多源结果保持原有数值，避免把刚好位于
  一致性门内的相机/激光/VIO 偏差压成过窄的目标落图圆。
- 新增回归覆盖“同样通过一致性门但测量分散”的 direct-range 对照，确认 `slant_range_ci95_m`
  会变宽且结果保持降级；目标坐标、QGC wire 字段和飞控只读边界不变。
- 可重算验收记录为
  `artifacts/evaluation/terminal-continuation-v31/ranging-residual-inflation-acceptance-20260904.json`
  （3,165 bytes，SHA-256
  `88516D2203D168C894945EDC5F1D7DFA6569F07B8DF9DD8ED910058BC27FA2DA`）。100/100 m 对照与
  100/110 m spread case 均通过固定测试夹具，后一组触发 `direct_range_uncertainty_inflated`，
  95% slant-range 区间由 13.86 m 扩大为 19.60 m；两组都保持 advisory-only，未产生飞控或
  物理释放输出。
- 算法源变化使 `configs/release-parity.json` 的 core hash 更新为
  `4ed386e9eaf91952b7c7be56d3d272efeaca4bb70a50e78ed57e04aa5307f5ad`。Docker Desktop Server
  `28.4.0` 已完成当前 VTOL live-AI 外部运行；三机型六份独立 runtime/AI 源记录重新 join
  为
  `artifacts/evaluation/terminal-continuation-v31/simulator-external-runtime-ai-join-20260904-current-pre8.json`
  （5,491 bytes，SHA-256
  `0681A9BC24883DBE284793217E9DBC79C62B40A920B2372625714A53010EE1DD`，`gate_passed=true`），
  独立 revalidation（561 bytes，SHA-256
  `DDF73895D5D8E6B4E7B51670E7644972248F41B17286E32AA84507369D6C9C6E`）
  `source_recomputed=true` 且通过。当前 report-only E7 绑定为
  `artifacts/evaluation/terminal-continuation-v31/release-gate-current-20260904T063854Z/`：
  manifest 20,855 bytes、SHA-256
  `0FFE4FB341A972C02E7B3C15C1005198C0ED139504A7C2053C40571135AE2BC4`，report 52,924 bytes、
  SHA-256 `6B1CF975E36F65ADC2B51703BC983A0E4D3E331DE42CCD3E5E03DBA643D5EDC0`。external SIM
  与 scene-stream set 子门均通过；现场 preflight freshness 已通过但硬件端点和 route 仍未就绪，
  replay-set review/accuracy、真实 GNSS/Pixhawk、HIL/REAL、人工 Fly View 和最终 release
  channel 仍未验收。总门继续为 `gate_passed=false` / `release_allowed=false`，31 条阻断原因，
  版本继续为 `v0.3.0-pre.8`。
- 标准 wrapper 的默认输入已切换到本轮 v31 external join/revalidation 与最新现场 preflight，
  不再把 v29 旧 parity 绑定作为无参默认。严格全量回归为 `2668 passed, 1 skipped`，退出码 `0`；
  日志 `artifacts/evaluation/terminal-continuation-v31/full-regression-v31-external-rebind-20260904.log`
  为 3,078 bytes，SHA-256 `837E9E87D8B5F40EB93001BDB8F674CF151B97D0E4DEFD0DD291934FD15A86F8`。

### v0.3.0-pre.8 v30d scene-stream set 正式接入 E7 release-gate（2026-09-04）

- 三机型 `scene_stream_set` 现在是正式 E7 manifest binding：writer/API/CLI 支持重复传入
  `--simulator-scene-stream-set-evidence`，Windows `run_current_release_gate_pre8.ps1` 的默认
  输入也绑定 fixed-wing、multirotor、VTOL 三条源流。
- `release-gate-manifest.schema.json` 增加严格的
  `simulatorSceneStreamSetRecord` / `simulatorSceneStreamRecord` 定义；evaluator 每次重开
  aggregate 与 source stream，重算哈希、bytes、scenario/mode、release label、deterministic、
  truth-free 和 snapshot checks。最终 release 缺少该 binding 会 fail-closed，prerelease 缺失仍
  向后兼容，显式提供的篡改 binding 会关闭总门。
- 当前 bound manifest/report 为
  `artifacts/evaluation/terminal-continuation-v30/release-gate-continuation-20260904T133400Z/release-gate-pre.8-current-20260904T133400Z-v6-bound.json`
  （20,828 bytes，SHA-256 `98A301DA0147010CBF6F927122304E3AE1F47B08B095B8E9D89D535A2C37547C`）和
  `artifacts/evaluation/terminal-continuation-v30/release-gate-continuation-20260904T133400Z/release-gate-pre.8-current-20260904T133400Z-v6-bound-report.json`
  （52,806 bytes，SHA-256
  `1A8B3A8402132B02788217787D564B32F87E2552506152232843B8D528C61EE8`）；scene-stream 子门
  `present=true/passed=true`、3 records、9 checks 全 true，但总门仍
  `gate_passed=false/release_allowed=false`，保留 31 条真实/发布阻断原因。
- 新鲜现场只读预检
  `artifacts/evaluation/terminal-continuation-v30/field-link-preflight-continuation-20260904-133250.json`
  （17,366 bytes，SHA-256
  `F5E12FB3A202F87B0102FBBB0DEE122538D1328FEB292CCDF4FE68E4ADAF4B1F`）请求并收集 3/3 个样本，
  仍显示 Jetson/GR01/相机不可达、route 源地址 `192.168.1.13`、`hardware_link_ready=false`。
- 结构化摘要为
  `artifacts/evaluation/terminal-continuation-v30/scene-stream-set-release-gate-acceptance-20260904-v3.json`
  （2,858 bytes，SHA-256
  `3FAAEF71A53E303B56EFBA550EDCD57A41C704894F860FA6DD3D080EF7D81214`；v3 绑定本次 3/3 现场样本，
  保留 v1/v2 历史，专项回归计数为 85）。专项回归 `85 passed`；
  全量严格回归 `2667 passed, 1 skipped`，退出码 `0`，日志
  `artifacts/evaluation/terminal-continuation-v30/full-regression-scene-stream-set-release-gate-final-20260904.log`
  （3,700 bytes，SHA-256
  `BEB6A74BC7CCB083F3B7AD3D93A99F0932FD9850D0E1684D5057F8BB45C4F8BF`）。

### v0.3.0-pre.8 v30c 现场链路只读刷新（2026-09-04）

- 台式机执行 `run_field_link_preflight.py --protocol-checks --samples 1 --timeout-s 0.5
  --expected-source-cidr 192.168.144.0/24 --require-route`，生成当前快照
  `artifacts/evaluation/terminal-continuation-v30/field-link-preflight-terminal-20260904-125150.json`
  （4,570 bytes，SHA-256 `D3511368A646821965A6B6A7E16895E5547FCCE964CDB57CD1AB28B0C1347C78`）。
- `verify_field_link_preflight.py --allow-unready` 的结构校验退出 `0`，但 readiness 仍为
  `hardware_link_ready=false`：Jetson/GR01/相机 TCP 与协议探测全部超时，三个 route probe
  均选择 `192.168.1.13`，不在期望的 `192.168.144.0/24` 内；本地 UDP 端口可用。该刷新只
  记录当前网卡和端点事实，不选择设备、不发送飞控指令，也不关闭现场/GNSS/HIL/REAL 门。

### v0.3.0-pre.8 v30b 三机型 scene-stream set 绑定（2026-09-04）

- 新增 `multidetect.simulator.scene_stream_set` 与
  `scripts/verify_simulator_scene_stream_set.py`，把 fixed-wing、multirotor、VTOL 各一份
  已验证 scene stream 聚合为同一份 hash-bound evidence。聚合器会重开并重验每个 120 帧
  源流，检查 scenario identity、mode 唯一性、release label、manifest/canonical/source
  SHA-256、deterministic、truth-free 和 snapshot 数量；任何重复、漂移或篡改均保持
  fail-closed。
- 增加严格 schema `configs/schemas/simulator-scene-stream-set.schema.json`、console
  entrypoint `multidetect-sim-scene-stream-set-verify` 和回归测试。当前绑定文件
  `artifacts/evaluation/terminal-continuation-v30/scene-stream-set-20260904.json` 为
  2,785 bytes，SHA-256 `A2141EE7115483885C717EE9944749609A83FC5B64872D5867EC149C4BCC76A5`；
  独立重验为 2,628 bytes，SHA-256
  `A3A34A9C19FC96C7A40031B67909E0C77E7DC5E0E9C90A0E1971C7CDDAFED2E3`，两者均通过。
- 该证据只表示三机型 truth-free 软件/SIM scene-stream 契约已聚合，不提升外部 3-D、
  QGC 人工视觉、真实硬件、室外 GNSS、HIL/REAL 或最终 `v0.3.0` 发布门。版本继续为
  `v0.3.0-pre.8`。
- 聚合器、单流 verifier 和 runtime provenance 的专项回归为 `31 passed`，退出码 `0`；日志
  `artifacts/evaluation/terminal-continuation-v30/scene-stream-set-runtime-focused-20260904-v3.log`
  为 101 bytes，SHA-256 `54F52A6D3F11141977876DDB2D6A7AF00DE6FB7FFCFE2AB8883643877CD9964D`。
  新增顺序无关性回归，确认同一组三机型源流以任意输入顺序生成完全一致的绑定字节。
- 结构化摘要 `artifacts/evaluation/terminal-continuation-v30/scene-stream-set-acceptance-20260904-v2.json`
  为 2,198 bytes，SHA-256 `DD528AC09A8C006CF7EEF4ABC2346DE8937A42B7A7C270968CEB2755E7806D3F`，
  明确记录 `software_status=pass_with_environment_gap`、`hardware_status=offline_pending` 和
  `release_allowed=false`。

### v0.3.0-pre.8 v30 运行身份快照与 fail-closed provenance（2026-09-04）

- `AutonomyRuntime.run_with_report()` 现在在消费第一帧前快照化并校验
  `RuntimeDescriptor.canonical_hash` 与 backend `input_hash`；处理期间或资源关闭时若后端身份被
  替换，运行直接失败，不会用结束时的可变字段伪造报告身份。该约束同时覆盖 REPLAY、SIM、HIL
  和 REAL 的共享运行报告边界。
- 新增 backend provenance 交换回归测试，确认身份变化会 fail-closed 且 backend 仍完成关闭；
  同时保证 observation iterator 创建失败时也会执行 backend cleanup，避免 SIM/REPLAY/HIL
  资源半关闭。随后纳入 scene-stream set 回归后的 Python 全量回归为 `2663 passed, 1 skipped`，
  退出码 `0`。唯一跳过仍是 Windows 符号链接权限限制。日志
  `artifacts/evaluation/terminal-continuation-v30/full-regression-scene-stream-set-determinism-20260904.log`
  为 3,521 bytes，SHA-256 `95DEB2C9EE32624D9B117F026D34BD169F30320E6D735A298B0D66FC6404865B`。
- 结构化验收摘要
  `artifacts/evaluation/terminal-continuation-v30/runtime-provenance-acceptance-20260904.json`
  为 1,939 bytes，SHA-256 `FCA1733F94EDBCFCFD31CB612955595A5EBE4E38105235A2637E9BAEDE906654`；
  `software_status=pass_with_environment_gap`、`hardware_status=offline_pending`、
  `release_allowed=false` 均被显式保留。
- 使用当前 pre.8 默认输入重新生成 E7 report-only 清单，确认新 runtime 收口没有掩盖其他门：
  manifest
  `artifacts/evaluation/terminal-continuation-v30/release-gate/release-gate-pre.8-current-20260904T115800Z-v6-bound.json`
  为 18,010 bytes，SHA-256 `402565280E94540A5F5A2E84DE3DBE7F45F3AA3E5B0AD825936BD64B0D60DFC`；
  report 为 48,589 bytes，SHA-256
  `E2014A9B6C992057D257899551CF35E2418BD97942DA5C6201B89EC8E6CA3794`。
  `gate_passed=false`、`release_allowed=false`，共保留 32 条阻断原因（replay-set、现场链路
  freshness/hardware、人工视觉、mission path、HIL/REAL 及 prerelease channel）。
- 随后重新采集当前台式机的只读现场 preflight（3/3 samples，协议检查开启）：
  `artifacts/evaluation/terminal-continuation-v30/field-link-preflight-live-20260904T120100Z.json`
  为 18,177 bytes，SHA-256 `3F9D1F06FA6021DA9228213B076D12169D58865491324983585A4D3F8EE4FB48`；
  接口诊断为 4,411 bytes，SHA-256
  `6D801CA49A4DC3C7F7BC21DB779B0C66F58C72C7DF5D94CF45DDB665BA083458`。报告结构 verifier
  退出 `0`、freshness 通过，但 3 个端点均超时且 route 源地址为 `192.168.1.13`，所以
  `hardware_link_ready=false`。绑定该新鲜报告的 E7 manifest/report 分别为 17,922/48,467
  bytes，SHA-256 分别为 `99CF86290FDEE4FFCA82FF24B4BB458A7C325F6BBDAAA878166DA518E37E8686` /
  `AE30959E4366CD069804FD33651ABB4EC52E5892BEC9719D74ABC13730CCA425`；旧的 stale 原因已
  消失，但总门仍 `gate_passed=false`、`release_allowed=false`，保留 31 条真实阻断原因。
- 现场 preflight、E7 wrapper 与 runtime backend 的专项回归共 `66 passed`，退出码 `0`；日志
  `artifacts/evaluation/terminal-continuation-v30/field-runtime-release-focused-v2-20260904.log`
  为 101 bytes，SHA-256 `0651EAAEBAB70DBE258889DDADA0AFDAFE4C77BEA1F38F840369B234D1A02232`。
- 版本继续保持 `v0.3.0-pre.8`。本轮只收紧本地运行身份与证据绑定，不改变现场硬件、室外 GNSS、
  HIL/REAL、人工 Fly View 或最终 `v0.3.0` 发布门的状态。

### v0.3.0-pre.8 v29 三机型外部 SIM/live-AI parity 重新绑定（2026-09-04）

- 发现历史外部 join 绑定旧 `core_algorithm_hash=434b86c6...` 后，保持当前 canonical
  parity `994ae1dffb3856eac5d71b479a7377f3f1b1c5beda2d154d6c2ee3b733c15af3` 不变，重新执行
  VTOL、fixed-wing、multirotor 的三份 runtime + 三份 live-AI 外部源记录。六份源记录均使用
  digest-pinned `px4io/px4-sitl-gazebo@sha256:bcf8278e...dc8ef8`、loopback 端口、只读
  场景挂载和 cleanup；三份 AI 记录均通过 armed heartbeat、AI step 与 target exercise gate。
- 当前 join `artifacts/evaluation/terminal-continuation-v29/simulator-external-runtime-ai-evidence-20260904-current.json`
  为 5,479 bytes，SHA-256 `1CD106451833C2CABBDFF3873DBF2893BB23D98C1D4EF0266ACAAD879AAFBE2E`，
  `gate_passed=true`；独立 revalidation 为 560 bytes，SHA-256
  `2848B8D8663DAB23AA8226E68876A513994D1F8BC1C59FC550432DFF3B443F8C`，
  `source_recomputed=true`、`gate_passed=true`。
- 当前源码严格回归 `python -W error -m pytest --assert=plain` 为 `2639 passed, 1 skipped`，
  退出码 `0`；日志 `artifacts/evaluation/terminal-continuation-v29/full-regression-strict-current.log`
  的 SHA-256 为 `2615E7E55929F6AF229DD98CDBF0AA25798BFAFA0C676D3F13D94B7622B3EA37`。补齐
  `simulator_external_evidence.py` 的 `python -m` 入口后，模块 `--help` 与安装的 console
  script 均退出 `0`。
- 随后将 `multidetect` 包根的 release-gate 导出改为惰性加载，消除模块运行时的重复导入
  `runpy` warning；`python -W error -m multidetect.simulator_external_evidence --help` 通过，
  严格全量回归仍为 `2639 passed, 1 skipped`。补充日志
  `artifacts/evaluation/terminal-continuation-v29/full-regression-strict-after-lazy-root.log`
  的 SHA-256 为 `F858F0C8FDC90706353EC0E9AADBF0B723D5F01BA129AC204B9D4C1645C3C778`。
- 绑定后的 report-only release-gate report
  `artifacts/evaluation/terminal-continuation-v29/release-gate-pre.8-current-20260904T014510Z-v6-bound-report.json`
  为 47,012 bytes，SHA-256 `4BC6D27971AF76F9D100D5CE2C5C03C6613D811260CC58AF916CC4076A50BEB8`；
  manifest SHA-256 为 `4A494D154E582B99A8FADF0137E68EE01AEF804C2AFE91FFC4177BCDA8E06DFB`。
- 外部 SIM 子门已通过，但总 `gate_passed=false` / `release_allowed=false`。replay-set、现场
  Ethernet/Jetson/相机、室外 GNSS、REAL 目标、人工 Fly View、mission-path、HIL/REAL 与
  final release channel 仍未验收；不得把本轮 software-only Docker 证据写成真实硬件或最终
  `v0.3.0`。
- 迁移后的当前以太网 `192.168.1.13/24` 上完成一次只读 `192.168.1.0/24` 服务发现：254 台
  主机中 5 台响应 SSH，MAVLink `5760`/RTSP `554` 均为 0 命中。报告
  `artifacts/evaluation/terminal-continuation-v29/field-link-discovery-live-20260904.json`
  为 8,526 bytes，SHA-256 `479732E9D458CA620DCB658B485FB2F4675EBD279BB1B8B2B65E51838A367369`；
  结构 verifier 通过，但 readiness 仍保持 false，未自动改变任何现场配置。
- 现场 route 诊断摘要新增 `summary.route_source_addresses`，按 Jetson、GR01 和 RTSP 端点
  显示操作系统实际选中的源地址；verifier 会把该投影与 `route_probes` 逐项重算，篡改或
  错配会保持 fail-closed。台式机最新只读报告
  `artifacts/evaluation/terminal-continuation-v29/field-link-preflight-live-20260904e.json`
  为 3,886 bytes、SHA-256 `8A5DF5874723819A2B5754061A42C682C7230A51CCCCA1E9AD55A59EEBD54A73`，
  verifier `--allow-unready` 退出 `0`，但三个源地址均为 `192.168.1.13`、
  `hardware_link_ready=false`。该字段只增强现场接线诊断，不改变 GNSS、RANGE_STATUS、
  REAL 或最终 `v0.3.0` 发布门。
- route 摘要回归接线后的严格全量测试为 `2654 passed, 1 skipped`，退出码 `0`；唯一跳过
  仍是 Windows 符号链接权限。日志
  `artifacts/evaluation/terminal-continuation-v29/full-regression-route-source-wrapper-final-20260904.log`
  为 3,521 bytes、SHA-256 `20BD8EF8F756B47224CEB090BC985F5DDAF2E7E9901D87C1518CEEAD292A421F`。
- 两样本 watch 复核
  `artifacts/evaluation/terminal-continuation-v29/field-link-preflight-watch-route-source-20260904.json`
  为 10,539 bytes、SHA-256 `C259273466907B4D2FF4188C0CA582BA13FD18CF3102205CC5D7D1B2EEFB8831`；
  保存报告 verifier 退出 `0`、结构门通过，但两样本都选中 `192.168.1.13`，聚合
  `hardware_link_ready=false`，保持现场门禁关闭。
- `scripts/run_field_link_preflight.ps1` 新增 `-GenerateInterfaceDiagnostic`：可在同一次
  wrapper 调用中生成并 hash-bind `inspect_field_link_interfaces.ps1` 的最新只读快照，避免
  迁移台式机时误用旧 JSON；它只写证据文件，不修改网卡地址、路由或 DNS。实跑生成的绑定
  报告 `artifacts/evaluation/terminal-continuation-v29/field-link-wrapper-generated-diagnostic-20260904.json`
  为 11,329 bytes、SHA-256 `294A962F0F2E03B585C0E8770A3CD5B59EED66EEB00ADC4E3C76F2F1D28A3ECA`，
  接口快照为 4,408 bytes、SHA-256 `E2F27056CC5900E17CFB64E2FE06E2D695ACD3B945BE515524B54310FB5472E`；
  wrapper 和保存报告 verifier 均退出 `0`，但现场 readiness 仍为 `false`。

### v0.3.0-pre.8 模型审核 envelope 与模型—回放 hash 绑定（2026-09-04）

- `evaluate_replay_set()` 和 `scripts/evaluate_replay_set.py` 新增可选的
  `--model-artifact/--model-id/--model-version` 绑定。报告会记录精确 artifact SHA-256，
  readiness 重算会保留该绑定；未提供模型绑定的旧报告继续兼容，但不能作为新模型审核的
  精确 artifact 证据。
- 新增 `src/multidetect/model_review.py` 与
  `scripts/create_model_review_bundle.py`。审核包重新验证 fire-candidate 的 post-NMS `N x 6`
  manifest、model-check 输出、部署域 IoU 报告和四类治理审核，所有缺口都写入
  `failure_reasons/next_actions`，不会修改候选 manifest。
- synthetic HIL 永远保持 `production_approval_eligible=false`，即使外部报告写入正向指标；
  `production_approved`、`flight_control_enabled`、`physical_release_enabled` 固定为 `false`。
  诊断输出 `artifacts/evaluation/terminal-continuation-v29/model-review-synthetic-20260904.json`
  的 SHA-256 为 `52C72DB0C562D7317E3BEBE85E6A8E8E69DB63B0C68414F7024B093B2C487002`。
- `create_release_gate_manifest.py --model-review-bundle` 现在把审核包作为顶层
  `model_review` hash-bound 记录写入 E7 清单；writer 与 `check_release_gate.py` 都会重算源
  bundle 并复核全部审核输入。预发布可以用 `--allow-incomplete` 保存 pending 诊断，最终
  `release` channel 则强制要求 `gate_passed=true` 与 `production_approval_eligible=true`。
- `scripts/run_current_release_gate_pre8.ps1` 新增 `-ModelReviewBundle` 转发，并将无参数默认
  external SIM join/revalidation 切换到当前 v29 parity-valid 文件；旧的
  `20260903-current-provider-fallback-parity` 快照保留为历史证据，不再参与默认 manifest 生成。
- 模型审核接线后的严格源码回归为 `2653 passed + 1 skipped`、退出码 `0`；完整汇总日志
  `artifacts/evaluation/terminal-continuation-v29/full-regression-model-review-wrapper-v2-summary-20260904.log`
  的 SHA-256 为 `DCE90748ABBFD8E1A370D8CE285D603C9BFEC9BDAA23A0EAED9DBAF9671CD0FC`。
  唯一跳过项是 Windows 符号链接权限，未被改写成通过。
- wrapper report-only smoke 使用 synthetic model-review envelope，退出码 `0`，但报告保持
  `model_review.present=true`、`model_review.passed=false`、`gate_passed=false`；输出目录为
  `artifacts/evaluation/terminal-continuation-v29/wrapper-model-review-smoke-b2a16a4739ad4ed79a655893500ddf5d/`，
  manifest/report SHA-256 分别为 `A71D4384550A551D229A06FD36E78149C2FD02E2ACD0994A8510EC529B23A70F`
  和 `887FBB62121CCE48A30A9CC65C2127D68D6AC4CCC15775F234CF77301B563B81`。
- 无参数 wrapper smoke 也已完成，输出目录为
  `artifacts/evaluation/terminal-continuation-v29/wrapper-default-smoke-57b3fc4661a040499a5a05cb0a14968f/`；
  manifest/report SHA-256 分别为 `3348C6C1DA4716377B58DCA5F238DA996F1D2A92D2E9AD47D074B160E22D6331`
  和 `83EAF47AAD51B8F70B483B1D325DCB1D4372F9B3C372A9E758B6322C87BC6605`，退出码 `0`，默认
  v29 external join/revalidation 成功绑定，`model_review.present=false` 是未传审核包时的预期。
- verifier 现在允许 `synthetic_hil` 作为结构有效但始终关闭的诊断候选重新打开；只有
  `require_eligible=true` 的最终审核路径才会把它判为失败，避免旧 synthetic envelope 因
  状态标签被误判为损坏，同时保持 production eligibility 为 `false`。
- 新增模型审核和模型 hash 绑定回归；版本继续保持 `v0.3.0-pre.8`，真实部署模型、室外
  GNSS/RTSP、HIL/REAL 和最终发布门状态不变。

### v0.3.0-pre.8 正向 replay evaluator 诊断夹具（2026-09-04）

- 在独立目录 `artifacts/evaluation/terminal-continuation-v29/replay-set-positive-synthetic/`
  生成正向 replay evaluator 诊断夹具。manifest 为 4,975 bytes、SHA-256
  `61E69B979C33F88B1E044F6192292E2CC55BF5EF6928645A7F28E16F5172BF46`；五个正例 strata
  均有确定性真值/预测，aggregate IoU precision/recall 为 `1.0/1.0`。
- 显式 `--reviewed --reviewer qa-team` 的重算报告为 12,649 bytes、SHA-256
  `6EF07FAFE7BA56E696498A856532BF46FE35227F22ADDC5C048FE4DE8C8048F2`，readiness 为
  10,940 bytes、SHA-256 `1EB519C6A5350007973742593A60F669F94AECFD646E2CEED5730BD4E507C13C`。
  由于所有条目仍是 `synthetic_demo`、未完成独立标注且不具备部署域来源，三份报告均保持
  `gate_passed=false`；它只验证 evaluator plumbing，永不替换 canonical replay-set 或最终
  REPLAY/SIM/HIL/REAL 发布证据。

### v0.3.0-pre.8 simulator orchestrator log-handle lifecycle（2026-09-04）

- `SimulatorOrchestrator._launch_process()` 现在在进程启动成功或失败后都及时关闭父进程
  持有的日志句柄；`Popen`/进程工厂仍接收同一日志路径，停止流程与结果证据格式不变。
- 新增生命周期回归，覆盖启动后的句柄关闭；严格警告全量回归
  `python -W error -m pytest -q --assert=plain` 为 `2639 passed + 1 skipped`，退出码 `0`。
- 日志 `artifacts/evaluation/terminal-continuation-v28e/full-regression-warnings.log` 的
  SHA-256 为 `57F43D18293A2E93B1B18642BEEC0C71D1E55DD0E55ED0F65B3E690C11287F21`；
  带跳过摘要的复核日志 `artifacts/evaluation/terminal-continuation-v28e/full-regression-with-counts.log`
  SHA-256 为 `807A392CC092389C8E1F51361A1C7922671E32111473F760F65347D13D0566C6`；
  `--assert=plain` 只避开 pytest assertion-rewrite 的环境 AST deprecation。
- 版本保持 `v0.3.0-pre.8`，不改变真实数据链、室外 GNSS、人工 Fly View、外部 3-D、
  HIL/REAL 或最终发布门状态。

### v0.3.0-pre.8 authenticated UDP header type hardening（2026-09-04）

- signed UDP alert header 现在要求 `version` 为严格整数 `1`，并拒绝
  `sent_at_unix_s` 的 JSON 布尔值、非数值和非有限值；sender、receiver 与 verifier
  继续共用同一 fail-closed 规则。
- 新增 3 个类型混淆回归向量；合法 wire format、ACK 关联、重试和飞控/载荷隔离保持不变。
- 当前源码全量回归为 `2638 passed + 1 skipped`，ruff/compileall 退出码均为 `0`；带完整
  pytest 汇总的日志 `artifacts/evaluation/terminal-continuation-v28d/full-regression-with-counts.log`
  为 305,776 bytes，SHA-256 为
  `D8769517FA595283741E7129ABBD96AB2CC5B868B83CED54117953B441B2B068`。
- 同一版本在真实 localhost UDP socket 上复跑 20 轮 authenticated baseline：20/20 ACK、
  关联/HMAC 通过、丢包 0；报告
  `artifacts/evaluation/terminal-continuation-v28d/alert-link-software-baseline.json`
  的 SHA-256 为 `6F0FAABAF3554C07D1070D5A02C1B91CC4D028BF8724B197A2E40E334DE579A6`，并明确
  `simulation_only=true`，不构成 GR01/REAL 证据。
- 对当前 release-gate 清单重新执行 report-only 评估仍为 `gate_passed=false`、
  `release_allowed=false`；输出
  `artifacts/evaluation/terminal-continuation-v28d/release-gate-current-report.json` 的
  SHA-256 为 `5236032C53AF6C0437512C7D23DFDF2956AF18DEEBFD2727C6537ACD7F83586E`。
- 版本仍为 `v0.3.0-pre.8`；真实数据链、室外 GNSS、人工 Fly View、HIL/REAL 与最终
  `v0.3.0` 发布门继续保持未验收。

### v0.3.0-pre.8 simulator scene-state lazy import（2026-09-04）

- `multidetect.simulator` 改为惰性加载 scene-state 导出，`python -m
  multidetect.simulator.scene_state --help` 不再产生误导性的 `runpy RuntimeWarning`；
  scenario runner 仅在显式记录状态流时导入 scene-state，默认运行和 canonical parity 保持不变。
- editable venv 已重新生成 `multidetect-sim-scene-stream-verify` console script；valid 120 帧
  VTOL stream 复核退出 `0`。
- 当前源码全量回归为 `2635 passed + 1 skipped`，ruff/compileall 退出码均为 `0`；日志
  `artifacts/evaluation/terminal-continuation-v28c/full-regression-summary.log` 的 SHA-256
  为 `3FAF04E313E44E1EBBE4C5FADD85E17CF247EA35AC36A255B77B4DF530336811`。
- 版本仍为 `v0.3.0-pre.8`；不改变真实硬件、室外 GNSS、人工 Fly View、HIL/REAL 或最终
  `v0.3.0` 发布门。

### v0.3.0-pre.8 场景流 parser/schema fail-closed hardening（2026-09-04）

- `SimulatorSceneStream.load_json()` 改用严格 JSON 解码，重复键、NaN/Infinity、非字符串
  嵌套键、重复 fault ID 和 `pose/world_origin` 真值字段均会 fail-closed；schema 对运行时
  对象保留字段同步收紧。
- 新增独立 `scripts/verify_simulator_scene_stream.py` / `multidetect-sim-scene-stream-verify`
  入口，分别报告文件 SHA、canonical stream SHA、逐帧 hash、schema 和 release label 绑定。
- 三份既有 120 帧 truth-free SIM stream 重新通过 schema、canonical stream hash 和
  round-trip replay，历史 artifact 未被改写；最新全量回归为 `2634 passed + 1 skipped`，
  退出码 `0`，日志 SHA-256 为
  `997AF8F7E50D96A699342ACE72EFBF8D778D247B5110AF5C2787D9E93EF1C171`。
- verifier 接入后的当前源码回归为 `2635 passed + 1 skipped`，退出码 `0`；日志
  `artifacts/evaluation/terminal-continuation-v28b/full-regression-summary.log` 的 SHA-256
  为 `B04577D8AE62918985336F718DAB039B38C7819304B4D119A340CA828615C5D8`。
- 版本仍为 `v0.3.0-pre.8`；本轮不提升外部 PX4/Gazebo、真实硬件、室外 GNSS、人工视觉、
  HIL/REAL 或最终 `v0.3.0` 发布门。

### v0.3.0-pre.8 可重放 3-D 场景状态流（2026-09-04）

- 新增 `SimulatorSceneSnapshot` / `SimulatorSceneStream`：记录同一 deterministic SIM
  tick 的 WGS-84 原点、局部 N/E/高度/姿态、telemetry、observation、AI/manual/authority/
  watchdog 与活动故障，并为每帧和整条 stream 生成 SHA-256。
- `record_scene_stream` 默认关闭，保持历史 scenario-runner/matrix evidence hash 稳定；
  `python -m multidetect.simulator.scene_state` 与 `multidetect-sim-scene-stream` 均可显式
  生成状态流，三种机型共用同一契约和 native QGC Fly View 消费边界。
- 新增严格 schema `configs/schemas/simulator-scene-stream.schema.json` 及回归测试，
  拒绝真值 observation、极点坐标、乱序/时间倒退和篡改 hash。
- 三个 120 帧 truth-free SIM artifact 已生成并通过 schema/round-trip 校验；它们不提升
  为外部 PX4/Gazebo、真实硬件、人工视觉、HIL/REAL 或最终版本发布证据。
- 全量 Python 回归收集 `2634` 项，`2633 passed + 1 skipped`，退出码 `0`；ruff、compileall
  和 canonical parity 校验均通过。最新日志
  `artifacts/evaluation/terminal-continuation-v27c/full-regression.log` 的 SHA-256 为
  `79204B900F8FBAC1F6E48B4670A1B0C008975689933B6091CA63EE9E3A989D37`。

### v0.3.0-pre.8 v26 三机型 deterministic SIM 复核（2026-09-04）

- 重新执行三种机型共用的确定性 SIM 场景矩阵，启用故障尾段覆盖；输出
  `artifacts/evaluation/terminal-continuation-v26-scenario-matrix.json`，三场景各完成
  2,611 帧，9 个声明故障覆盖率均为 `1.0`，重复 hash 一致，矩阵 verifier 退出码为 `0`。
- 重新执行 native-QGC interactive fixture，输出
  `artifacts/evaluation/terminal-continuation-v26-interactive-acceptance.json`；VTOL、
  fixed-wing、multirotor 共用 AI/manual authority、watchdog 和 Mission write 阻断路径，
  fixture gate 通过。
- 两份记录均明确标记为 software/SIM、truth-free，外部 PX4/Gazebo、人工 Fly View、真实
  Pixhawk/Jetson/GNSS、HIL/REAL 和最终 `v0.3.0` 仍未验收；版本继续保持 `pre.8`。
- v26 随后执行全量 Python 回归：`2624 passed + 1 skipped`，退出码 `0`；日志
  `artifacts/evaluation/terminal-continuation-v26/full-regression.log` 的 SHA-256 为
  `A70388821B0E7A4B8BE782162681BD3FDEFB5F710F40701A8A24C08617BC596E`。

### v0.3.0-pre.8 v25 field-link freshness-bound wrapper 复核（2026-09-04）

- 使用新的现场 watch 报告重新执行当前 E7 wrapper，验证 freshness 成对绑定在新清单上
  生效。输入报告
  `artifacts/evaluation/terminal-continuation-v25/field-link-preflight-live-20260904a.json`
  为 16,808 bytes，SHA-256
  `53E45C0C4D7B6EB7942BB7438A90F29BC422893E3E6259F97A8A4C33EF74AB39`。
- wrapper 输出目录为
  `artifacts/evaluation/terminal-continuation-v25/e7-freshness-bound-900/`；manifest 为
  17,902 bytes，SHA-256
  `6C854C43E932FF4CC010B099A44ACED2CF62749C3A86D8405F27C6CFB6271CA4`，report 为 47,074
  bytes，SHA-256 `E192B4E7BED44B2B13660F58A019CF2CCAE572DB12D8C2F2012E4FCC67AD6B84`。
- manifest 使用 `max_age_s=900` 并同时绑定 `report_timestamp_epoch_s`；schema 校验为
  `0` 个错误，现场报告年龄约 `157.1 s`。由于设备端点、REPLAY 和人工/硬件门仍未就绪，
  `hardware_link_ready=false`、`gate_passed=false`；wrapper report-only 退出码 `0` 不等于
  REAL/HIL 或最终版本通过。
- 同一工作树的全量回归收集 `2625` 项，结果为 `2624 passed + 1 skipped`（Windows symlink
  权限）；日志 `artifacts/evaluation/terminal-continuation-v25/full-regression-final.log`
  为 3,492 bytes，SHA-256
  `74CCDE8D3C72276B8122DFB03579D1E76CAF98AAD0F5030F6F7AEB7DD3CDC0B7`，ruff 与 compileall
  退出码均为 `0`。
- 同一 v25b 报告绑定到完整 goal-acceptance：快照
  `artifacts/evaluation/terminal-continuation-v25/goal-acceptance/multidetect-goal-acceptance.json`
  为 46,791 bytes，SHA-256
  `4514AF0836C0E33117475B3BD1C272F24D94A857B215C253BAFE05CFCE16F5D7`；日志为 58,812 bytes，
  SHA-256 `78342FAA5DDC8B43BBDDDC1EAC986A1F494F693E87AFED36366479CE03A56F59`。快照记录
  `software_status=pass`、核心 `2625`、QGC custom `66`、SIM/external 子门通过，
  `hardware_status=offline_pending`，六项 live gate 继续未关闭。
- 随后用同一 v25b 报告再次执行 E7 wrapper，输出目录
  `artifacts/evaluation/terminal-continuation-v25/e7-freshness-bound-900-b/`；manifest SHA-256
  为 `7946161569981FD367820A54C45D7EDA51EDB8D9FDF581D7921089DEB4560CA7`，report SHA-256
  为 `79EB13C0B1132E81D48E86DB6EAF2ED2A7CAF320044A29F63005429D1379AC3D`。wrapper 退出码为
  `0`，但 `gate_passed=false`，没有将现场离线状态升级为通过。

### v0.3.0-pre.8 goal-acceptance v24 freshness-bound release gate (2026-09-04)

- 将 REAL `field_link` 绑定扩展为 freshness-bound 记录：manifest 写入
  `max_age_s` 与源报告时间戳，evaluator 每次读取清单都会重新调用同一 verifier；缺失、
  未来或过期时间戳保持 fail-closed；`run_current_release_gate_pre8.ps1` 新增
  `-RealFieldLinkMaxAgeSeconds` 透传覆盖；`max_age_s` 与 `report_timestamp_epoch_s` 必须
  成对出现，旧 prerelease 清单仍兼容读取，最终 `release`
  通道强制要求这两个字段。
- 重新执行完整验收。快照
  `artifacts/evaluation/terminal-continuation-v24/multidetect-goal-acceptance.json` 为
  46,146 bytes，SHA-256
  `5E5820365FD95E8BBC3987E8559A88878922CC3E3CD478D3A60AB6272C53E3C3`；日志为 58,167
  bytes，SHA-256 `CB4541FEAF9030022B4ADDAB7D7BEF9F06E4A9073E9D671D5399EB0860B33BF7`。
  `software_status=pass`、核心收集 `2624`、QGC custom `66`，现场仍为
  `hardware_status=offline_pending`，六项 live gates 未关闭。
- v24 watch 报告
  `artifacts/evaluation/terminal-continuation-v24/field-link-preflight-live-20260904e.json`
  为 16,810 bytes，SHA-256
  `2341C97D1146CC33D0734D7947A5A543D05A6D96C7E6C8B0896E1C974EA6B5F2`；离线 verifier 为
  `artifacts/evaluation/terminal-continuation-v24/field-link-verification.json`，SHA-256
  `C489A288FA478E01C980238149FB26FDD0BE81BBC7A50C2E6B0FF94A356DFB6F`，freshness 约
  `27.2/900 s` 通过，但 `hardware_link_ready=false`。
- Windows wrapper 使用 `-RealFieldLinkMaxAgeSeconds 1200` 的写入→重读样本位于
  `artifacts/evaluation/terminal-continuation-v24/e7-freshness-bound-1200/`；manifest SHA-256
  为 `3A8F97E45F7A9F17E1ED88762AD4E029C9C3CF1425CF1CEA60FDBE330BDDFFC0`，report SHA-256
  为 `74E9AC9FC1144127FC800C755D43C01EB1082E693954945535C774309C2D62B5`，记录的
  `max_age_s=1200` 与源时间戳一致，硬件门仍为 false。
- 重点回归 `final-regression-summary.log` 退出码为 `0`，记录 `276 passed`，SHA-256
  `B9424480C9EFC6120221B7F47DD9C325A17048A8FD6528936ADAB01F4B215C2F`；该证据不升级为
  室外 GNSS、真实硬件、HIL/REAL、人工 Fly View 或最终 `v0.3.0` 发布通过。
- 移除旧 pytest 的重复 `pythonpath` 配置后，独立全量回归
  `artifacts/evaluation/terminal-continuation-v24/full-regression-post-wrapper-no-warning.log`
  退出码 `0`，结果为 `2624 passed + 1 skipped`（Windows symlink 权限），3,477 bytes，
  SHA-256 `0CB6EE557B7529E0124540D5A1B37513B8EA2CEAB9A1F17C9B59D973310D7EAA`；ruff 与
  compileall 也为 `0`。剩余 deprecation warning 来自 pytest 6.2.5/ Python 3.12 兼容层。

### v0.3.0-pre.8 goal-acceptance v23 freshness regression snapshot (2026-09-04)

- 在 freshness verifier 增加缺失时间戳回归后，重新生成 watch 现场报告并执行完整验收。
  快照 `artifacts/evaluation/terminal-continuation-v23/multidetect-goal-acceptance.json`
  为 46,003 bytes，SHA-256
  `ABB54E58F04C12330E37B3F2574DF73979B8C6224DC8D8F5EDDEF9BDE2D35DEB`；日志
  `artifacts/evaluation/terminal-continuation-v23/goal-acceptance-rerun.log` 为 58,072
  bytes，SHA-256 `D26E844E778D53AB926077935E8980464C565109CCA9445DF4859B0D90F68762`。
- 核心收集增至 `2623`，QGC custom `66`，软件门和三机型确定性 SIM 子门通过；现场报告
  freshness 通过但 `hardware_link_ready=false`，`hardware_status=offline_pending`，
  最终 `v0.3.0` 继续保持未发布。
- 现场 verifier、目标验收脚本、文档、版本、发布门 manifest 和三机型 SIM 回归共 `108` 项
  通过；`artifacts/evaluation/terminal-continuation-v23/final-regression.log` 的 SHA-256
  为 `08321BCC6DFDEBCC3713EB62156CA65C4E934D67FED1540BBC413A0C0427FFB7`，ruff、compileall
  与 `git diff --check` 均返回 `0`。

### v0.3.0-pre.8 field-link freshness binding and goal-acceptance v22 (2026-09-04)

- `verify_field_link_report()` 新增可选 `max_age_s`：one-shot 报告校验
  `generated_at_epoch_s`，watch 报告校验 `finished_at_epoch_s`；时间戳缺失、未来或过期时
  保持 fail-closed。`run_goal_acceptance.ps1` 通过 `-FieldLinkMaxAgeSeconds` 以默认 900 秒
  窗口绑定现场报告，freshness 通过不等于端点可达或硬件就绪。
- 新鲜现场 watch 报告为
  `artifacts/evaluation/terminal-continuation-v22/field-link-preflight-live-20260904c.json`
  （16,807 bytes，SHA-256
  `0F211E23FD0C2671C8424C276E41993EB7DABB15059A4585A044B000D3F1D682`）；verifier 输出
  `field-link-verification.json` 为 1,797 bytes，SHA-256
  `4E58BF188B0D99D10CBCCFE1DBCA18E0A04B4E655E7BE2541A2D1090F50BFF94`，freshness 通过、
  `hardware_link_ready=false`。
- 对应完整快照
  `artifacts/evaluation/terminal-continuation-v22/multidetect-goal-acceptance.json` 为
  46,003 bytes，SHA-256
  `B2569EAF162AD9E33266DE25ABC1D61EEFB7A214C9021807A02A80C7078CD8B2`；日志
  `goal-acceptance-rerun.log` 为 58,072 bytes，SHA-256
  `4F57F20702EC1CDE80228C66DB2D4BD6E03C0B2C2A9ACCB20058590C0CE70D35`。
  `software_status=pass`、核心收集 `2622`、QGC custom `66`；现场和最终发布门仍未关闭。
- freshness、现场 verifier、目标验收脚本、文档、版本和三机型回归共 `107` 项通过；
  `artifacts/evaluation/terminal-continuation-v22/final-regression.log` 的 SHA-256 为
  `CBB3706472BC99CDB39412F0A4283DEDB3C58B5B08466BB086B244352367A02E`，ruff、compileall
  与 `git diff --check` 均返回 `0`。

### v0.3.0-pre.8 goal-acceptance v21 snapshot (2026-09-04)

- 在三机型 3-D SIM 控制绑定之后重新执行完整 `goal-acceptance`；快照
  `artifacts/evaluation/terminal-continuation-v21/multidetect-goal-acceptance.json`
  为 45,234 bytes，SHA-256
  `78498FAA088110E49B640BF3BA605B23B133142BC10AAFC6B184E1091F53D089`，运行日志
  `artifacts/evaluation/terminal-continuation-v21/goal-acceptance-rerun.log` 为 57,303
  bytes，SHA-256 `F23336F60FE289A63DDAE65E9551180A5B594B28EE4924FD2181352D1913292F`。
- `software_status=pass`、核心测试收集 `2619`、QGC custom `66`、QML parse、协议
  self-test 和当前三机型确定性 3-D SIM 证据均通过；现场仍为
  `hardware_status=offline_pending`、`field_link_ready=false`，六项 live gates 未关闭。
- 该快照不代表外部 PX4/Gazebo、室外 GNSS、人工 Fly View、HIL/REAL 或实飞通过；最终
  `v0.3.0` 继续保持未发布。

### v0.3.0-pre.8 three-airframe 3-D SIM binding (2026-09-04)

- 重新执行 VTOL、固定翼、多旋翼三份 3-D SIM 控制资产预检，并将三份独立证据用
  `verify_simulator_3d_control_evidence.py --bind` 做哈希绑定。
- 绑定结果位于 `artifacts/evaluation/terminal-continuation-v20/simulator-3d-control-binding.json`，
  SHA-256 为 `967C162D4B996C4CA709B678732A8690FFA18256B6AD958CDCC08EE6CAC61B4E`；
  `all_airframes`、`all_deterministic_gate_passed`、`records_hash_bound` 均为 `true`。
- 该记录只关闭确定性的协议/资产预检，不宣称外部 PX4/Gazebo、GPU 画面、人工 Fly View、
  HIL 或 REAL 通过；最终 `v0.3.0` 继续保持未发布。

### v0.3.0-pre.8 goal-acceptance v19 rebind (2026-09-04)

- 在构建 wrapper 的 PowerShell 兼容性修复之后，重新执行 canonical `goal-acceptance` 并
  将当前现场链路 live-preflight 重新绑定到新快照。
- 快照 `artifacts/evaluation/terminal-continuation-v19/multidetect-goal-acceptance.json`
  为 45,234 bytes，SHA-256
  `5C6E0618F0C25A04A52832F7B00CB5113EF21BC3C2C1375F658899A8B2A04066`；运行日志
  `artifacts/evaluation/terminal-continuation-v19/goal-acceptance-rerun.log` 为 57,255
  bytes，SHA-256 `CF2EC57CE361AD811733986F96BA7606005EF4BE347E80BE0F78E444FA4EC392`。
- 本轮确认 `software_status=pass`、核心测试收集 `2619`、QGC custom `66`；现场仍为
  `hardware_status=offline_pending`、`field_link_ready=false`，六项 live gate 继续保持
  未完成，最终 `v0.3.0` 未发布。

### v0.3.0-pre.8 goal-acceptance PowerShell exit-code hardening (2026-09-04)

- `scripts/run_goal_acceptance.ps1` 现在把所有原生 Python、Git 和辅助脚本的退出码统一
  通过 `Get-NativeExitCode` 读取。该 helper 在 `Set-StrictMode`、嵌套
  `powershell.exe -File` 或命令无输出时，对缺失的自动变量采用确定性 `0`，对已有失败码
  原样保留，避免把宿主差异误报成 acceptance 异常。
- 新增静态回归覆盖 helper 存在、缺失变量的成功默认值、失败码读取和禁止裸读
  `$LASTEXITCODE`；PowerShell AST、目标脚本回归和全量 Python 回归均通过。
- 全量回归日志为
  `artifacts/evaluation/terminal-continuation-v18/pytest-full-20260904-after-goal-exitcode.log`
  （3,481 bytes，SHA-256
  `E688279FEF98088AD9874A63B65D4477E3AC176B8808CFEC95B6E155FDB357FA`），退出码 `0`；
  这是系统 Python fallback 的 `2617` 项记录（`2616 passed / 1 skipped`）。canonical QGC
  venv 的最新全量记录为
  `artifacts/evaluation/terminal-continuation-v18/pytest-full-venv-after-goal-exitcode-v2.log`
  （2,997 bytes，SHA-256
  `4FBA774CEB187D57C2C27E7A50F03B25502352B395D014B97BC512293CA5C616`），收集 `2619` 项、
  退出码 `0`，进度中仅出现 1 个 skip、没有失败标记。
- 这只提升门禁 wrapper 的可移植性，不改变任何 `hardware_link_ready`、REPLAY、HIL、REAL
  或最终 `v0.3.0` 发布条件。
- Windows PowerShell 5.1 全流程冒烟输出到
  `artifacts/evaluation/terminal-continuation-v18/goal-acceptance-after-goal-exitcode/`：总快照
  `multidetect-goal-acceptance.json` 为 45,382 bytes、SHA-256
  `C5B41616636DC9668314BCB934BBF0637F0E4E23C8CCE5110E4AFB24AE4D6851`，脚本完成并写出
  `software_status=pass`、`hardware_status=offline_pending`、`core_tests_collected=2619`、
  `qgc_custom_tests=66`；原始日志 57,403 bytes、SHA-256
  `C56ECD6682D15E096BFA2E815F369ED523F0D6DB574445609EEEF713604FE002`。这份快照仍保留
  六项现场/REPLAY live gates，未把宿主兼容性冒烟升级成硬件或最终发布通过。

### v0.3.0-pre.8 Windows build wrapper SHA-256 portability (2026-09-04)

- `scripts/build_multidetect_windows.ps1` 使用 .NET `SHA256::Create()` 流式计算 Release EXE
  指纹，不再依赖 PowerShell 可选哈希模块；构建、安装 portable 和最终摘要在 Windows
  PowerShell 5.1、嵌套 `powershell.exe -File` 与 pwsh 中保持一致。
- `-SkipConfigure -BuildSelfTests -InstallPortable -Parallel 4` 完整执行退出码为 `0`。原始日志
  `artifacts/evaluation/terminal-continuation-v18/build-wrapper-after-sha256-fix.log` 为
  142,536 bytes，SHA-256 `F28147F8FEBEF709936A31BEFD88EA7B6243AB294A00C1CF447E9AFBD2752E23`。
- 同一命令在 pwsh 宿主下也返回 `0`；日志
  `artifacts/evaluation/terminal-continuation-v18/build-wrapper-pwsh-after-sha256-fix.log` 的大小和
  SHA-256 完全一致，证明哈希收尾不依赖特定 PowerShell 模块加载状态。
- 同轮 canonical QGC venv 全量回归日志为
  `artifacts/evaluation/terminal-continuation-v18/pytest-full-venv-after-build-wrapper-fix.log`
  （2,997 bytes，SHA-256
  `4FBA774CEB187D57C2C27E7A50F03B25502352B395D014B97BC512293CA5C616`），退出码 `0`。
- 当前 Release/portable `MultiDetectGCS.exe` 均为 46,575,616 bytes、SHA-256
  `4D7A024F80157E5EB8671B77D6E1817FB960ECE8A8A97D0F470A222E7F7C8E40`，字节一致；portable
  `MultiDetectReplayRtspPublisher.exe` 为 95,744 bytes、SHA-256
  `DE94D5404BEF48B80919BFC06465BD53EBFD50A8B65A50F38DA3BCDDADB58312`。
- 构建后 portable `--offline --simple-boot-test` 退出码为 `0`，日志
  `artifacts/evaluation/terminal-continuation-v18/qgc-simple-boot-after-sha256-fix.log` 为
  220 bytes、SHA-256 `946D08771ACF7DE07C43218D395CC514F16CDB253A34C9AC5D4601F70299BEA9`。
- 该项只改善构建 wrapper 的宿主兼容性，不改变硬件链路、室外 GNSS、HIL/REAL 或最终版本
  发布条件。

### v0.3.0-pre.8 replay fixture smoke-only semantics (2026-09-04)

- 修正 `src/multidetect/replay_set_fixture.py` 的正例分层语义：`day`、`night`、`haze`、
  `reflection` 保留固定 `flame+smoke`，`smoke_only` 现在只写入 `smoke` 真值和预测，避免
  用混合类别掩盖火焰幻检。新增回归断言逐帧检查该标签契约。
- 当前 pre.8 夹具重新生成于
  `artifacts/replay-sets/synthetic-positive-evaluator-pre8-deterministic-v5/`：manifest
  4,981 bytes、SHA-256 `2F1305E7BA634B49932186EF5E04088285587D6A8702ECA3CDD0EC61BE2F4BF5`；
  accuracy report 12,149 bytes、SHA-256
  `37E2958D9BC5AC3221F641BC6E40278CFBA48D724E1FA450B61051E5B336C63D`；fixture summary
  2,800 bytes、SHA-256 `C7EC9DB4A8E27468CB312AD165FFE6741130D95F0DBC2A782A6B76063465FFF2`。
- 独立 evaluator/readiness 重算确认五个正例层的 truth/prediction 计数均大于零，
  `smoke_only` 各为 5，aggregate precision/recall 均为 `1.0`；`synthetic_demo`、
  `annotations_reviewed=false` 仍使 accuracy/replay gate 为 `false`，不构成部署域或最终
  版本证据。

### v0.3.0-pre.8 field-link PowerShell exit-code hardening (2026-09-04)

- `scripts/run_field_link_preflight.ps1` 现在对原生 Python 和只读网卡探测统一采用
  `Get-Variable -Name LASTEXITCODE -ErrorAction SilentlyContinue`，缺少自动变量时以确定性
  `0` 处理，避免 `Set-StrictMode` 因 PowerShell 主机差异把有效诊断误报为 wrapper 异常。
  该改动不改变 `hardware_link_ready` 或 `-RequireReady` 的门槛；失败的链路仍在 JSON 中
  保持 `hardware_link_ready=false`。
- 现场 wrapper 冒烟记录为
  `artifacts/evaluation/terminal-continuation-v18/field-link-wrapper-smoke-20260904b.json`
  （7,861 bytes，SHA-256
  `C437051B274C76ED4732FD0019FF20EC67FCCA16824900D9609193E1891CA0C`）。它以 1 个样本和
  `192.168.1.0/30` 有界发现运行，wrapper/verifier 退出 `0`（诊断模式），实际链路仍为
  `all_passed=false`、`hardware_link_ready=false`；发现到 1 个 SSH、0 个 MAVLink、0 个
  RTSP 响应。脚本、wrapper 和 field-preflight 回归为 `51 passed`，日志
  `artifacts/evaluation/terminal-continuation-v18/field-link-wrapper-regression-20260904.log`
  （565 bytes，SHA-256
  `5ED87FCDFDBE91521B726EDEBBC270047EA6F0DE4FA104749D72A3D0F5DEDBEF`）。随后完整 Python
  回归为 `2616 passed / 1 skipped`（共收集 2617，退出码 `0`），日志
  `artifacts/evaluation/terminal-continuation-v18/pytest-full-20260904-after-field-wrapper.log`
  （3,481 bytes，SHA-256
  `29AD92466E928C6DEC1CFBF1EC4D58C14AFF4190A74C11D1D2C976D372ABC5C0`）。

### v0.3.0-pre.8 field-link live watch after desktop migration (2026-09-04)

- 显式选择 `Ethernet` 后运行 3 样本、2 秒超时、协议检查和路由检查的只读 watch；报告
  `artifacts/evaluation/terminal-continuation-v18/field-link-preflight-live-20260904b.json`
  为 16,807 bytes，SHA-256 `AC62A886C3723A5209D9C4F441537C4D7D82B0AC530DF90AE52D340F02B7238A`。
- wrapper/verifier 均退出 `0`，但 `all_passed=false`、`hardware_link_ready=false`：本机
  `192.168.1.13/24` 不在期望 `192.168.144.0/24`，Jetson `:22`、GR01 `:5760`、相机
  `:554` 三次均 timeout；本地 UDP `14580/14581` 可绑定。原始日志为
  `artifacts/evaluation/terminal-continuation-v18/field-link-preflight-live-20260904b.log`
  （18,533 bytes，SHA-256
  `362095D6D841613B338CF8268445A3C3FCB3D596152261FDFCD10B54D4475C92`）。
- `-AutoSelectInterface` 在期望网段没有活动地址时按设计以退出码 `1` 停止且不写假报告；
  显式接口诊断路径随后写出上述 fail-closed 报告。该事实不关闭现场、GNSS、HIL/REAL 或最终
  `v0.3.0` 发布门。

### v0.3.0-pre.8 GNSS-to-camera lever-arm correction (2026-09-04)

- `CameraCalibration` 新增可选的 `camera_offset_forward_m`、`camera_offset_right_m`、
  `camera_offset_down_m` 和 `camera_offset_sigma_m`。它们表达相机光心相对于导航/GNSS
  参考点的 body-NED 杆臂及测量不确定度；旧标定缺省为零，保持历史结果兼容。
- `MultiModalRangingEngine.solve()` 与 `solve_direct()` 会按姿态把杆臂旋转到 NED，使用光心
  高度求地面交点，并将相对于导航参考点的完整 N/E 偏移用于地面距离、绝对方位和 WGS-84
  目标落图。杆臂测量 sigma 同时进入高度、地面距离和方位不确定度，异常或无界条件保持
  fail-closed。
- ChArUco 标定函数/CLI 将杆臂字段写入 calibration JSON 与 `mount_survey`，并在
  `configs/calibration/README.md` 记录坐标约定、100 m 域限制和示例命令；专项测距/标定回归
  `38 passed`。该软件改动不产生飞控或载荷指令，也不替代现场 GNSS/RTK、HIL/REAL 验收。
- 共享契约 `configs/release-parity.json` 已按当前源码重算，`core_algorithm_hash` 为
  `994ae1dffb3856eac5d71b479a7377f3f1b1c5beda2d154d6c2ee3b733c15af3`；历史 E7 清单不会被
  覆盖，最终版本仍保持 `v0.3.0-pre.8`。

### v0.3.0-pre.8 parity-bound E7 revalidation (2026-09-04)

- 杆臂校正后重新生成 `configs/release-parity.json`：文件 SHA-256 为
  `BFE773CE40773A9AFA651E29FC2BA80FB1119B6B2519424A7A2A2348302BED24`，
  `core_algorithm_hash=994ae1dffb3856eac5d71b479a7377f3f1b1c5beda2d154d6c2ee3b733c15af3`。
- 全量 Python 回归重新收集 `2617` 项，`2616 passed / 1 skipped / 0 failed`，退出码 `0`；
  原始汇总日志为 `artifacts/evaluation/terminal-continuation-v14/pytest-full-20260904-lever-arm-summary.log`
  （3,536 bytes，SHA-256 `0194FF54B0A24456B1CF045698602C428F0D4FDE3DD4AB1AB7D9B63773329B1C`）。
  专项测距/标定/兼容回归 `38 passed`，schema、ruff 和 compileall 均通过。
- 既有六源 external SIM join（SHA-256
  `EF0631BF43B11DF6AC170C2F2A6A68C7CFF26AF800FD16260DC861C2EDF07557`）仍记录旧 parity
  `0AFF3DF8913E8C99C8495FAD743F1E85D68716DE86719E1D91BA628F47343DF7`。当前 verifier 对该
  文件重算得到 `gate_passed=false`，诊断为 `join: stored summary does not match source recomputation`；
  结果写入 `artifacts/evaluation/terminal-continuation-v15/external-join-revalidation-current-parity.json`
  （625 bytes，SHA-256 `5F874F6BDE9FBD62F17226465D46C2242C786EC3020B0C342E29B2793430E283`）。
- 严格 E7 wrapper 在 manifest 写入前检测到
  `simulator_external_evidence join parity hashes do not match the release-gate parity input`，
  退出码 `1`；日志 `artifacts/evaluation/terminal-continuation-v15/e7-rebind-attempt-20260904T023000Z.log`
  的 SHA-256 为 `F3DF0617A033997169741B4F4BFBBBB9076FB042393BD7F556343210BC6D7721`，没有新的
  manifest/report 被写入。当时的 PATH-only 探测未找到 `docker.exe`；后续直接调用 Docker Desktop
  运行时目录中的 CLI 完成了当前 parity 重绑，见下一个条目。
- 本轮未触及 QGC C++/QML，canonical portable EXE 不需要重编译；旧 portable 启动证据继续作为
  独立 provenance 保留，不能替代新 parity 的 external SIM 重跑。

### v0.3.0-pre.8 external PX4/Gazebo + live-AI six-source rebind (2026-09-04)

- 通过 `C:\Program Files\Docker\Docker\resources\bin\docker.exe` 使用固定 digest
  `px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`
  重新执行 fixed-wing、VTOL、multirotor 三份 runtime 与三份 live-AI source。六份 source
  均为 `gate_passed=true`；runtime 的 PX4/GPS/loopback/只读挂载/清理门通过，AI 的 arm/disarm、
  `ai_policy_gate_passed=true`、`target_policy_gate_passed=true`、`target_exercise_accepted=true`
  通过。所有 source 都是 truth-free，未接触真实 Pixhawk、Jetson 或相机。
- 当前 parity join 为 `artifacts/evaluation/terminal-continuation-v17/simulator-external-runtime-ai-evidence-20260904-current-pre8.json`
  （5,287 bytes，SHA-256 `E0B2BA71345D6C046CA9EEFAA42DA45325B8712069CB2000DDAEEF3E7B3D858C`），
  `external_runtime_gate=passed`、`ai_target_policy_gate=passed`，硬件和人工视觉仍为
  `not_claimed`。独立重验 `artifacts/evaluation/terminal-continuation-v17/external-join-revalidation-current-parity.json`
  （565 bytes，SHA-256 `0BEF11653C13933E10F27E6A63F0DB3A1E89DCBC4438F53FE56CEF1EE58FD688`）
  `source_recomputed=true`、`gate_passed=true`、`reasons=[]`。
- 以新 join 生成的 E7 manifest/report/strict report 位于
  `artifacts/evaluation/terminal-continuation-v17/e7-rebind/`；report 已接受新 external join，
  但总门仍 `gate_passed=false`、`release_allowed=false`，保留 31 项 REPLAY accuracy、现场
  field-link、HIL/REAL、人工 Fly View 和 prerelease 阻断。该条目只关闭 software-only external
  SIM/runtime/AI 子门，最终 `v0.3.0` 继续未发布。
- 随后运行完整 goal-acceptance，所有输出写入独立目录
  `artifacts/evaluation/terminal-continuation-v18/`；主快照
  `multidetect-goal-acceptance-20260904-current-pre8.json` 为 60,087 bytes，SHA-256
  `CB6A884DFA76E52DCB8EB34F8EF678C58EA137361C31D08FDF01F3D991306507`，脚本退出码 `0`。
  核心测试收集 `2617`、QGC 自定义测试 `66`、QGC protocol self-test/QML parse 通过，六源
  external gate 已绑定且通过；`hardware_status=offline_pending`、REPLAY readiness 和现场
  链路仍未通过，剩余 live gate 保持 deployment replay accuracy、现场标定/舵面/取消/失锁、
  真实 DET→TRK→LCK→TGT、Jetson soak 与三场景人工 Fly View 验收。
- 为排除迁移后飞机 IP 变化，额外运行了有界只读发现
  `artifacts/evaluation/terminal-continuation-v18/field-link-discovery-192.168.1-20260904.json`
  （8,525 bytes，SHA-256 `79D3AA8F6E873D7C682DF3C719E941F25E5172EBEBC517BA01410651B0B89A92`）：
  扫描 254 个地址，5 个 SSH 响应，MAVLink `5760`/RTSP `554` 均 0 命中。结果仅用于诊断，
  不自动选择端点、不连接未知 SSH 主机、不改变 `hardware_link_ready`。
- 随后将现场链路预检显式绑定到新目录
  `artifacts/evaluation/terminal-continuation-v17/e7-rebind-fieldlink/`（不覆盖旧 E7）：manifest
  `release-gate-pre.8-current-20260903T184551Z-v6-bound.json` SHA-256
  `E8841054FB71BD5FC0374ED6C12A5E3485444C161869AE627C29673CC1506A0C`，report-only
  `release-gate-pre.8-current-20260903T184551Z-v6-bound-report.json` SHA-256
  `9E40DC8A46E7235504A244D8B00697B0D0D2C13AD5A6E986EDC3ECFB19F1B3D9`，strict report
  `release-gate-pre.8-current-20260903T184551Z-v6-bound-strict.json` SHA-256
  `0840AD72023A877BB258B2A7975ACC8002DF1A5C0F89502CE972A307961EC92D`。严格退出码 `2`，
  31 项阻断保持不变，`real_field_link` 明确绑定 `hardware_link_ready=false`。
- 同一轮保存现场链路只读预检
  `artifacts/evaluation/terminal-continuation-v17/field-link-preflight-current-20260904.json`
  （3,434 bytes，SHA-256 `C64890B38EC040FD365C8AB9D6BA3D31620F7442F256A7BF812CBE803009039F`）。
  默认 `192.168.144.0/24` 下 Jetson `192.168.144.20:22`、GR01 `192.168.144.11:5760`、
  相机 RTSP `192.168.144.108:554` 均 timeout；本机 UDP `14580/14581` 可绑定，故
  `hardware_link_ready=false`。这是只读可达性证据，不关闭现场、HIL 或 REAL 门。

### v0.3.0-pre.8 E7 wrapper robustness and field-bound refresh (2026-09-04)

- 修复 `scripts/run_current_release_gate_pre8.ps1` 的 `Invoke-Python`：在严格模式下通过
  `Get-Variable -Name LASTEXITCODE` 读取原生 Python 退出状态，并在变量不存在时使用确定性
  `0`，避免部分 PowerShell 主机在 manifest 写入前因未定义自动变量中止。这个修复只处理
  wrapper 状态读取，不放宽发布门；对应 wrapper/goal-acceptance 接线回归为 `15 passed`，完整
  E7 专项集合为 `56 passed`。原始日志为
  `artifacts/evaluation/terminal-continuation-v18/e7-wrapper-regression-20260904.log`（618 bytes，
  SHA-256 `762CEA25A1B6F793DECC8B945CEF2FE81D7EB780B44CD689FB57CE3FAA594C12`）。
- 用当前六源 external SIM join、v18 现场预检和当前 SIM/安全/Mission evidence 生成独立
  E7 绑定目录 `artifacts/evaluation/terminal-continuation-v18/e7-rebind-current-v5/`，不覆盖
  历史产物：manifest `release-gate-pre.8-current-20260903T190334Z-v6-bound.json` 为
  17,702 bytes、SHA-256 `1C88F8C7EC0F087126C3B274C17CFF23412969C213A158BA261DB6B8EB498CC8`；
  report-only 为 46,713 bytes、SHA-256
  `04D04E9B822935DB0150F7AC0CE93BAE209E5C7D0674B24244B6E173DA9ECDFA`；strict report 为
  46,713 bytes、SHA-256
  `2F9A7CD22BEA34C12832316759A8147BAC08E7F5E9F6415B4DF99A4ED0C394EF`，strict evaluator
  退出 `2`。
- E7 已接受当前 external join（`simulator_external_evidence.passed=true`），但总门仍为
  `gate_passed=false`，保留 31 项 REPLAY accuracy/deployment、现场链路、人工 Fly View、
  HIL/REAL 硬件与真实控制、prerelease channel 等阻断；`hardware_link_ready=false` 仍是
  现场事实。该次只改 wrapper 与文档，canonical portable EXE 不变，最终 `v0.3.0` 继续未发布。

### v0.3.0-pre.8 deterministic SIM scenario matrix fault coverage (2026-09-04)

- 用当前源码运行三机型场景矩阵：`coastal_patrol_fixed_wing_001`、`river_rescue_vtol_001`、
  `urban_inspection_multirotor_001`。AI、arm、重复运行和 fault coverage 全部开启；每个场景
  自动延长到 `5221` 帧，AI accepted `5221` 步，fixed-wing/VTOL/multirotor 的 evidence hash
  在重复运行中完全一致。
- GPS dropout/drift、camera dropout、airspeed freeze、wind gust、battery drop 等声明故障全部
  触发，`gate_passed=true`、`deterministic_gate_passed=true`、`repeatability_checked=true`。
  证据 `artifacts/evaluation/terminal-continuation-v16/simulator-scenario-matrix-20260904-current-pre8.json`
  为 13,302 bytes，SHA-256 `6F39876CE26EFF7B8E4DCFF9E4E083E3A450441BA97F63439B62392BFB29A2FF`；
  独立 verifier 退出码 `0`。
- 该记录使用 truth-free 进程内 SIM runner，不启动 Docker/PX4/Gazebo，不读取场景 truth；
  `external_3d_runtime`、QGC 人工视觉、硬件和 REAL 仍保持 `not_claimed`，它只关闭确定性
  software/SIM 矩阵子门，不改变 E7、HIL/REAL 或最终 `v0.3.0` 发布状态。

### v0.3.0-pre.8 outdoor geolocation error ellipse (2026-09-04)

- WGS-84 真值评估现在把估计点与独立真值的残差转换为局部 `N/E` 米制坐标，输出北/东偏差、
  `2×2` 样本协方差、`1σ` 主轴/次轴/方向和二维 `95%` 椭圆（`χ²(0.95,2)`）。这为室外
  多距离定位提供方向性误差证据，同时保留运行时/QGC 的保守 `horizontal_sigma_m` 圆和现有
  wire 兼容性。
- `verify_geolocation_accuracy_report()` 会对新增 `error_ellipse` 重新计算并拒绝篡改；缺少
  该可选节的历史 v1 报告仍按原字段验证，避免历史证据失效。JSON schema、单元测试和
  `jsonschema` smoke 均同步通过。该评估输出仍是离线只读证据，不关闭现场 GNSS、HIL/REAL
  或最终 `v0.3.0` 发布门。
- 本轮全量 Python 回归为 `2612 passed, 1 skipped`（共收集 `2613`，退出码 `0`）；唯一跳过
  是 Windows 符号链接权限 `WinError 1314`。原始日志和机器可读汇总保存在
  `artifacts/evaluation/terminal-continuation-v11/`，没有把该环境缺口写成产品失败。
- canonical portable EXE 以 `--offline --simple-boot-test` 运行退出码 `0`；启动证据同时绑定
  当前 `MultiDetectGCS.exe` 的大小和 SHA-256，确认本轮 Python/文档变更没有改变已验证的
  Windows 运行入口。
- 现场链路只读 watch 重新采样两次：本机 `192.168.1.13/24` 仍不在期望的
  `192.168.144.0/24`，Jetson/GR01/RTSP 均超时而本地 UDP 端口可用；报告保留
  `hardware_link_ready=false`，不向 HIL/REAL 门注入假阳性。
- 最新现场快照已经绑定到 `artifacts/evaluation/terminal-continuation-v13/` 的 pre.8 E7
  清单；严格 verifier 退出 `2`、`gate_passed=false`、`release_allowed=false`，保持
  31 项真实阻断原因可追溯。

### v0.3.0-pre.8 external SIM runtime preflight and Docker AI closure (2026-09-04)

- `runtime-preflight` 现在可选接收 `--repository-root`、`--scenario` 和 `--px4-workdir`，只读
  解析仓库自有场景、世界/模型资产哈希以及 PX4 `etc/init.d-posix/rcS` 启动锚点；输出新增
  `asset_preflight`、`px4_workdir_preflight` 和 `external_runtime`。预检阶段的
  `external_runtime.gate_passed`/`external_runtime_gate_passed` 固定为 `false`，确定性
  flight-model 回退明确记录 `automatic=false`、`used=false`，避免外部运行时静默降级。
- 场景绑定证据写入 `artifacts/evaluation/terminal-continuation-v10/runtime-preflight-scenario.json`
  （7,188 bytes，SHA-256 `1E243FBA41273CA94FBB6B349573FB4206B4A26CEED5C55778CF545A3A2590E1`）。
- 使用固定 digest 的 `px4io/px4-sitl-gazebo` 完成一次 Docker/SITL（非硬件）AI 闭环，证据为
  `artifacts/evaluation/terminal-continuation-v10/simulator-docker-ai-acceptance-external.json`
  （114,687 bytes，SHA-256 `DF58B68B40D34683DFE1309A4A45965FCD26EC40887B7077DBBA6BB65752C1FF`）。
  `gate_passed`、`preflight_gate_passed`、`runtime_gate_passed`、`telemetry.qualified`、
  `ai_policy_gate_passed`、`target_policy_gate_passed` 均为真，`ai_policy_steps=2`，容器
  已真实启动并在结束后清理；该证据不代表 QGC 视觉、Pixhawk/Jetson/相机、室外 GNSS、HIL/REAL
  或最终 `v0.3.0` 通过。
- 随后按脚本的 post-adoption 序列余量要求完成 900 帧外部 PX4/Gazebo + 原生 QGC Fly View
  + VTOL Mission 组合 SIM 重跑。成功 source 为
  `artifacts/evaluation/combined-sim-qgc-20260903T164443Z-4900/combined-evidence.json`
  （249,113 bytes，SHA-256 `0DC4044DB459888D4DC1648CC8266B545FBEF86D346FF45E8199C75909911762`），
  独立 verifier 为同目录 `combined-evidence-verification.json`（1,956 bytes，SHA-256
  `A31188E69D6EAA5A8752CA11CEF06D2CFE63AAB8601A3ABCEC2F980B536CF0CF`），40/40 checks 通过。
  另生成 source 专属 sim release-mode envelope
  `artifacts/evaluation/terminal-continuation-v10/release-mode-sim-combined-qgc-20260903T164443Z-4900.json`
  （542 bytes，SHA-256 `80AB0572968BE5992E523BDD8ED2E402D996FAA772A9ED70AB0974769FF1EB52`），
  `--require-release-mode` 复核输出 `combined-evidence-verification-release-mode.json`
  （2,596 bytes，SHA-256 `BDD55C221332DDB27599433AF09B58F078C26CC95399EA88B97D84EC5D1489F9`）仍为
  40/40 checks 通过。
  该 source/envelope 已绑定到独立 E7 目录
  `artifacts/evaluation/terminal-continuation-v10/e7-combined-qgc-rerun/`：manifest SHA-256
  `D2A4FE349CB45D0AC872ACA589532B6A3E2586EB1196856791647BE708FA9207`，report-only SHA-256
  `DA99390FE22DFFD8D3280DCAA94B3A5B89E5755BAB23DA2C0A661E14A4D71B18`，strict report SHA-256
  `D71E6AB91BEEE570A5F321ED3A9309D9AADCE9433B197D2E0CE9CC03A023E3D5`；`simulator_combined`
  子门为真，strict 总 E7 仍以退出码 `2` 保持 `gate_passed=false`/`release_allowed=false`。
  900 帧 type-23/type-24/RTSP 屏障、Gazebo 必需传感器稳定 `2/2`、动态端口隔离与释放、容器
  清理、VTOL `MC→FW→MC`、7 项 INT-only Mission readback/reached、AUTO.MISSION、land/disarm
  均通过；Mission-ready 后 QGC 序列 `569→574→579`。此前 300 帧超时文件保留为失败历史，
  不被改写。组合证据仍明确 `operator_visual_acceptance=not_claimed`、software-only，不能
  代表人工 QGC 视觉、真实硬件、室外 GNSS、HIL/REAL 或最终版本发布。

### v0.3.0-pre.8 terminal continuation E7 rebind v8 and replay readiness v7 (2026-09-04)

- 使用当前默认输入在独立目录 `artifacts/evaluation/terminal-continuation-v8/` 刷新 E7
  report-only/strict 绑定。manifest `release-gate-pre.8-current-20260903T161008Z-v6-bound.json`
  为 17,786 bytes（SHA-256 `9A4F03F02A49A69DA47DC6C0BE7B15A8849FA9DE230A05A1B37B70207C3C83B9`），
  report 为 47,742 bytes（SHA-256 `C3A86200F591941473CF8B684C128AAC1B7FE837F3A8BA2882ED03E2BB40173D`），
  strict report 为 47,742 bytes（SHA-256 `FC498FF3ED174E0F740409AC988139E2664D694875247C900E8B835EFDEB9382`）。
- 严格 verifier 退出码为 `2`，`gate_passed=false`、`release_allowed=false`，共 31 个阻断项；
  三机型 3-D control、deterministic 和 S4 safety 的 hash-bound 子门保持通过，现场、REPLAY
  accuracy、人工 Fly View/Mission、HIL/REAL 与最终 release channel 继续 fail-closed。
- 对正例 synthetic evaluator fixture 重新执行 readiness，输出
  `artifacts/evaluation/terminal-continuation-v7-replay-readiness.json`（11,263 bytes，
  SHA-256 `E3FA53E95FA566BC9816FD4D9CFFC3902949B3D5176BC974E1A5F0ADFBA50EFC`）。六层正例
  真值/预测覆盖和 evaluator 重算通过，但 `synthetic_demo`、未复核标注使 readiness
  `gate_passed=false`；该证据不替代真实部署域录像与人工复核。
- 台式机现场链路只读续检写入
  `artifacts/evaluation/terminal-continuation-v9/field-link-preflight-20260903T161925Z.json`
  （16,804 bytes，SHA-256 `15AA28819D7B1D94AE082C4EE3EA458D64A4BCBFC2CF05F299EEED07586B992E`），
  wrapper/verifier 均退出 `1`。三样本 Jetson/GR01/RTSP 端点全部超时，路由源
  `192.168.1.13` 不在期望 `192.168.144.0/24`，`hardware_link_ready=false`；本地
  `14580/14581` UDP 端口仍可用。该失败快照只更新现场事实，不升级 REAL/HIL/GNSS 门。

### v0.3.0-pre.8 terminal continuation E7 binding v6 (2026-09-03)

- 在独立目录 `artifacts/evaluation/terminal-continuation-v6/` 生成当前默认 E7
  report-only 绑定，未覆盖任何 `latest` 或历史证据。清单
  `release-gate-pre.8-current-20260903T160000Z-v6-bound.json` 为 17,839 bytes，SHA-256
  `8D86A97CD4C8CB099CB852EA11FE3C325AB783936A00F1E5EDC77EEE58DCC619`；报告
  `release-gate-pre.8-current-20260903T160000Z-v6-bound-report.json` 为 47,799 bytes，
  SHA-256 `86EC0A976FB5AD1D4968805BF21AE270A561F77017D25BAF0B22CDD674ADD572`。
- 清单把 v5 三机型 3-D control（fixed-wing、multirotor、VTOL）和 S4 safety 证据按路径、
  字节数及 SHA-256 重新绑定；聚合结果确认三种机型的 `gate_passed`、deterministic 和
  record hash binding 均为真，S4 safety 也通过。外部 PX4/Gazebo、视觉和硬件仍保持
  `false/not_claimed`。
- E7 report-only 状态仍为 `gate_passed=false` / `release_allowed=false`：部署域
  REPLAY/accuracy、现场 `hardware_link_ready`、人工 Fly View/Mission、HIL/REAL 硬件与
  真实控制、最终 release channel 尚未满足。wrapper 退出码 `0` 仅表示失败门状态已被
  完整记录，版本继续保持 `v0.3.0-pre.8`；随后对同一清单运行严格 verifier，退出码为
  `2`，再次确认 fail-closed。

### v0.3.0-pre.8 developer dependency closure (2026-09-03)

- `[dev]` 依赖补齐 `jsonschema>=4.20`，并将 `attrs`、`jsonschema-specifications`、
  `referencing`、`rpds-py` 及主包 extra 关系写入 `uv.lock`；项目 venv 的 `uv lock --check`
  通过，clean checkout 不再在 RTSP schema 测试收集阶段缺少 `jsonschema`。
- 新增跨 replay-set manifest 的校准 verification 绑定回归：verification 文件即使自身
  `gate_passed=true`，只要绑定到另一份 replay manifest，release-gate manifest writer 立即
  fail-closed 拒绝。项目 venv 全量 pytest 收集 `2606` 项，`2605 passed / 1 skipped`；日志为
  `artifacts/evaluation/pytest-full-20260903-dev-dependency-v1.log`（SHA-256
  `B4F17F1B835467D0338B82EC7FBF825B756D4F611163A2CEFAE703001994CC91`）。
- 重新执行 `pip install -e ".[dev]"`，修复迁移后项目 venv 残留的 `multi-detect 0.1.0`
  editable metadata；现在 pip metadata、模块版本与产品清单一致为 `0.3.0rc8 /
  v0.3.0-pre.8`，并通过 `pip check`。

### v0.3.0-pre.8 terminal continuation acceptance (2026-09-03)

- 在不覆盖既有 `latest` 或历史证据的前提下，使用项目 venv 重新执行完整终端验收：核心
  pytest 收集 `2606` 项，QGC 自定义测试 `66` 项，ruff、QML 解析、协议自测、SIM 三机型
  故障覆盖、PX4 Mission 事务和 git diff 检查均通过。新快照为
  `artifacts/evaluation/terminal-continuation-v1/multidetect-goal-acceptance-20260903-terminal-continuation-v1.json`
  （44,931 bytes，SHA-256
  `9E207A3B7E6C03BF69C9EB28E3568F56438EA9ACB8C66C60CC1E5FCCD46065DD`）。
- 快照记录版本仍为 `v0.3.0-pre.8`、`hardware_status=offline_pending`；活动网卡
  `Ethernet=192.168.1.13/24` 未连接 `192.168.144.0/24`，Jetson 与相机不可达。现场 GNSS、
  Pixhawk、Jetson soak、部署域 replay、HIL/REAL 和人工 Fly View 门继续保持 pending。
- 同一 portable 入口完成 headless `--offline --simple-boot-test` 烟测并以退出码 `0` 完成：
  `artifacts/evaluation/terminal-continuation-v1/qgc-simple-boot-20260903T135047Z.log`，
  SHA-256 `83437047EDDDE232A7E6F6AA3C7727CB0B6C6555D62ED0C19FA84D62E3F135D9`。这只证明
  无界面启动链路，不关闭人工 Fly View 或真实硬件门。
- 22:00 后补充同一 QGC 窗口的句柄级 `PrintWindow` 观察：
  `artifacts/evaluation/terminal-continuation-v1/qgc-flyview-window-20260903T141750Z.png`
  （SHA-256 `5645F55713060EB8D8C2D51F97D73F609DE5B08DFCB19FBE60193827CFE772E6`），记录了
  原生 Fly View、`M1·DET`、视频/地图表面和断开等待态。元数据
  `qgc-flyview-capture-20260903T141750Z.json` 明确标记 `operator_visual_acceptance=not_claimed`，
  不把工程截图写成操作员验收。

### v0.3.0-pre.8 current-provider external SIM revalidation (2026-09-03)

- 以当前 `configs/release-parity.json`（SHA-256
  `0AFF3DF8913E8C99C8495FAD743F1E85D68716DE86719E1D91BA628F47343DF7`，
  `core_algorithm_hash=434b86c6a8b677e60a4ae5766586b603855609e43cc0521695aa1e21995963f9`）为基准，
  重新校验六源 external SIM join
  `simulator-external-runtime-ai-evidence-20260903-current-provider-fallback-parity.json`
  （SHA-256 `EF0631BF43B11DF6AC170C2F2A6A68C7CFF26AF800FD16260DC861C2EDF07557`）。verifier
  退出码为 `0`，`source_recomputed=true`、`gate_passed=true`、版本为 `0.3.0-pre.8`。
- 新的独立 revalidation 为
  `artifacts/evaluation/terminal-continuation-v2/simulator-external-runtime-ai-revalidation-current-provider-fallback-parity.json`
  （558 bytes，SHA-256
  `5576A33BE03572344825430CE78DDF4B2F7FB03087F1DE025DAFC8325E9E6D35`）。完整 goal acceptance
  续跑写入
  `artifacts/evaluation/terminal-continuation-v2/multidetect-goal-acceptance-20260903-terminal-continuation-v2.json`
  （45,325 bytes，SHA-256
  `B4B3E9C3F2566FAF6313FE7D42CD7E8706C8FAEB3AB3B4E715491287D8491589`），核心 pytest `2606`、
  QGC custom `66`、`software_status=pass`，external evidence 四项绑定状态均为 `true`。
- external runtime 子门已在当前 acceptance 中验证，剩余项只保留三机型人工 QGC Fly View
  视觉验收及部署域 replay、标定、执行器断推进器、真实检测和 Jetson soak；该证据继续标记
  truth-free/software-only，`hardware_status=offline_pending`、`operator_visual_acceptance=not_claimed`，
  不改变最终 `v0.3.0` 发布门。

### v0.3.0-pre.8 desktop field-link preflight continuation v3 (2026-09-03)

- 在同一台式机上重新执行只读现场 preflight：不登录 Jetson、不请求 MAVLink 流、不启动 RTSP，
  也不发送飞控或执行器数据。报告为
  `artifacts/evaluation/terminal-continuation-v3/field-link-preflight-20260903-terminal-continuation-v3.json`
  （6,686 bytes，SHA-256
  `CD8D3819670444803DD793E1B7F13258E5101CCA8E7CFD71F8428D7BBC6B665D`）。
- `Ethernet=192.168.1.13/24` 仍未接入 `192.168.144.0/24`；Jetson `:22`、GR01 `:5760`、相机
  `:554` 均超时，本地 `14580/14581` 端口可绑定，路由源地址仍为 `192.168.1.13`。wrapper 和
  verifier 都返回 `1`，`hardware_link_ready=false`；该快照只更新现场事实，保留
  `hardware_status=offline_pending`，不改变软件 acceptance、HIL/REAL 或最终发布门。
- 将该现场报告绑定进新的完整 goal acceptance：
  `artifacts/evaluation/terminal-continuation-v3/multidetect-goal-acceptance-20260903-terminal-continuation-v3.json`
  （45,914 bytes，SHA-256
  `A112A891F52B137EF293673DF7A4F1CC7FDC4F6DFE2DA9B09042D8737F8BA8C8`）。核心 pytest `2606`、
  QGC custom `66`、`software_status=pass`；field preflight `provided/verified=true` 但
  `hardware_link_ready=false`、verifier exit `1`，硬件仍 `offline_pending`，六项 live gates
  保持不变。
- 额外执行三机型 deterministic SIM 长帧故障覆盖：
  `artifacts/evaluation/terminal-continuation-v4/simulator-scenario-matrix-fault-covered-pre8.json`
  （13,298 bytes，SHA-256
  `7AB2CB409B27FB9AB94B95CD9F450700CA79B9814C827B66DD56F7A70AABCAF3`）。`--cover-faults`
  自动将帧预算扩到 `2651`，`--require-fault-coverage`、三机型 coverage ratio `1.0` 和
  repeatability 均通过；该证据仍是 truth-free/software-only，不改变硬件或最终发布门。

### v0.3.0-pre.8 simulator control/safety continuation v5 (2026-09-03)

- 使用当前源码重新执行共享 3-D 控制契约：
  `artifacts/evaluation/terminal-continuation-v5/simulator-3d-control-pre8.json`
  （83,252 bytes，SHA-256
  `493AE72466F1BDEC1F5C8AD225B41B86D1B4A94A5E4DC1AD0F7791E77059C7BD`）。
  `gate_passed=true`、`deterministic_gate_passed=true`；manual QGC、AI authority takeover、
  AI 在人工接管期间抑制、释放后恢复、watchdog neutral/failsafe、native QGC-only path、
  Mission/flight write block 和 loopback 隔离均通过。PX4/Gazebo 可执行文件当前不可用，
  因而 `external_runtime_gate_passed=false`、视觉/硬件验收仍为 `not_claimed`。
- 同一源码下重新执行 S4 safety：
  `artifacts/evaluation/terminal-continuation-v5/simulator-safety-pre8.json`
  （6,991 bytes，SHA-256
  `7293321BF87CD10E15199847BB9AEC5CCBFBE083FDD2DC0665F8071E95FC38F6`）。
  `gate_passed=true`、`truth_free=true`；目标取消只清理选中目标、极点/非法 GPS fail-closed、
  failsafe 后不再接受控制、终态后拒绝 Mission event 均通过。该证据仍是 software/SIM 子门，
  不提升真实 Pixhawk、Jetson、相机、执行器、HIL/REAL 或人工 Fly View。
- 随后分别以 fixed-wing、VTOL、multirotor 场景生成三份当前控制证据，并完成独立 hash-bound
  binding：`artifacts/evaluation/terminal-continuation-v5/simulator-3d-control-binding-pre8.json`
  （2,380 bytes，SHA-256
  `6C5F4DD6F65490A13ACDC3B19887233EFFDC1FD27F5ACD6611A6AD85B1CA73BF`）。binding verifier
  逐项确认 `all_airframes=true`、`all_gate_passed=true`、`all_deterministic_gate_passed=true`、
  `records_hash_bound=true`、`unique_modes=true`、`unique_scenarios=true`；三份 evidence 的
  `external_runtime_gate_passed=false`，因此仍只代表 software/SIM control contract。
  同一 verifier 输入连续运行两次，binding 输出字节完全一致，输出 SHA 均为
  `6C5F4DD6F65490A13ACDC3B19887233EFFDC1FD27F5ACD6611A6AD85B1CA73BF`。

### v0.3.0-pre.8 replay evaluator/calibration binding (2026-09-03)

- 为已存在的 synthetic positive replay fixture 生成独立的 threshold calibration 与
  verification。calibration 的 `promotion_ready=false` 保持真实状态，verification 的
  `gate_passed=true` 只表示报告、manifest/canonical SHA-256 和重算结果一致：
  `threshold-calibration-20260903-pre.8-positive-fixture.json`（SHA-256
  `4CF5D9831729B692867D243203C7CF9F2B6935BA2E8050DB4372725597049E4C`）和
  `threshold-calibration-20260903-pre.8-positive-fixture-verification.json`（SHA-256
  `BE978504F28A6D7553438E1FA2E93E81BAE37B02699C9445B0D073427E026557`）。
- 将该 fixture 的 accuracy report 与 calibration verification 绑定到独立诊断 E7
  `release-gate-pre.8-current-20260903T131700Z-v6-bound-report.json`（SHA-256
  `46E1D0BBAAED0F0C321DF025F7CB7156B7A98F846CC5AE17CD84CCDB607844BC`）。五个正例层覆盖、
  aggregate IoU precision/recall 和校准哈希链路均可重算，但因为来源仍是 `synthetic_demo`、
  标注未独立复核、现场链路未就绪以及其它 REAL/HIL 门，`gate_passed=false`、
  `release_allowed=false` 保持不变。严格输出为
  `release-gate-pre.8-current-20260903T131700Z-v6-bound-strict.json`（SHA-256
  `80C4E0370891004613B58CCCE00369F35480E898B9F5EEF75FFCD0F5FE9FF673`）；该诊断绑定不替换
  canonical `sim-gazebo-2026` 空正例基线，也不升级为部署域精度或最终 v0.3.0 证据。

### v0.3.0-pre.8 desktop field-link fail-closed snapshot (2026-09-03)

- 在迁移后的台式机上新增只读网卡/路由快照：`Ethernet=192.168.1.13/24`，目标飞机网段
  `192.168.144.0/24` 无候选接口或直连 route，`selection.status=missing`。快照为
  `artifacts/evaluation/field-link-interface-diagnostic-20260903-desktop-pre8.json`
  （4,384 bytes，SHA-256
  `CAB5758FA853649A74CAD3E9F7D7D3B5F718DA1F2BA8118CE58302635CFFCC6D`）。
- 新增一份 hash-bound 三采样 fail-fast 现场预检：
  `artifacts/evaluation/field-link-preflight-20260903-desktop-pre8-interface-bound.json`
  （6,658 bytes，SHA-256
  `913F6E0BB79EA0F5831B6998F8F7F9415CD380DAC6C9F00F5CA1F5A0D5A10584`）。首个样本记录
  Jetson/GR01/RTSP 超时和 route source `192.168.1.13`，本地 `14580/14581` 可绑定，
  `hardware_link_ready=false`；`--allow-unready` 只验证结构与绑定，不提升现场门。
- 用该最新报告生成了 pre.8 report-only E7 绑定
  `release-gate-pre.8-current-20260903T124800Z-v6-bound.json`（SHA-256
  `80BEDA6B77D76B5E3F050B3DDC7D7A035A232438B7FCBAC18159ECD2BDD0CF8F`）及报告
  `release-gate-pre.8-current-20260903T124800Z-v6-bound-report.json`（SHA-256
  `D073D15CA04C46EE02DB0B06E3704C6DC2E25F64C32C275825DAA7F820B4C464`）。总门继续
  `gate_passed=false` / `release_allowed=false`，版本仍为 `v0.3.0-pre.8`。
- 随后用 Windows wrapper 的 `-ProtocolChecks -RequireRoute -RequireReady -StopOnFailure`
  重跑严格现场预检，报告
  `field-link-preflight-20260903-desktop-pre8-wrapper-strict-r2.json`（7,449 bytes，SHA-256
  `A3F2037643363DCCBDC5BFC83000073AB12EA382E5385F5C0CBA56A7C815EA36`）。严格 wrapper 和
  verifier 均返回 `1`，但完整 fail-closed 报告保留并可离线复核；对应最新 E7 绑定为
  `release-gate-pre.8-current-20260903T125900Z-v6-bound.json`（SHA-256
  `D0D71A48E44358D919E24179B1262ACDF47471D353E0AA9FF0630F746D3455F8`）及报告
  `release-gate-pre.8-current-20260903T125900Z-v6-bound-report.json`（SHA-256
  `8CB65C5C607B069C4DDC9FBE9A3976C8A7F85453726A93F11B57FBC9918B5F2E`）。

### v0.3.0-pre.8 RTSP multi-client fan-out (2026-09-03)

- `MultiDetectReplayRtspPublisher` keeps the default single-client path on a
  non-shared RTSP media factory, tracks every active `appsrc` generation and
  fans one validated BGR24 frame out with independent `GstBuffer` references.
  Reconnect retries exclude generations that already accepted the frame, while
  stale/retired generations remain fail-closed.
- Added `--minimum-client-count 1..64`. For a minimum above one the helper uses
  GStreamer shared media, which is the native multi-client fan-out path and
  avoids serialising concurrent `DESCRIBE`/encoder preroll; minimum one retains
  non-shared generation isolation. `frame_accepted` is emitted only after the
  selected mode reaches its configured client/media lower bound.
- RTSP caps advertise a positive `30/1` nominal rate solely for SDP negotiation;
  replay `captured_at_ns` remains the authoritative `GST_BUFFER_PTS/DTS` source.
- Added `scripts/verify_replay_rtsp_multiclient.py`, a concurrent raw
  `OPTIONS → DESCRIBE → SETUP → PLAY` and interleaved RTPv2/PT96 verifier, plus
  `configs/schemas/replay-rtsp-multiclient-bench.schema.json` and regression
  coverage. The current 2-client/3-frame software-only artifact is
  `artifacts/evaluation/replay-rtsp-multiclient-20260903-pre.8-v3.json`
  (2,781 bytes, SHA-256
  `C22A0EAFBF9915CBC61B65E583BB4CCB333B6E1E4B9433784E914743C3F81989`).
  It records `gate_passed=true`, `source_kind=software_only_local_rtsp` and
  `hardware_claim=none`; it does not close aircraft, outdoor GNSS, HIL or REAL
  release gates.
- Release helper `build/windows-v0.3.0-pre.8-release/Release/MultiDetectReplayRtspPublisher.exe`
  is 95,744 bytes with SHA-256
  `DE94D5404BEF48B80919BFC06465BD53EBFD50A8B65A50F38DA3BCDDADB58312`.
- After portable deployment, the same bench was rerun against the canonical portable helper at
  `build/windows-v0.3.0-pre.8-release/portable-v0.3.0-pre.8/bin/MultiDetectReplayRtspPublisher.exe`.
  `artifacts/evaluation/replay-rtsp-multiclient-20260903-pre.8-v4-portable.json` is 2,798 bytes,
  SHA-256 `96199FF2CF802B42E3A3CEDA4BFD44A8BAF328798F7C32D27ECCAC35C8BF3456`, and records
  `gate_passed=true` for two concurrent raw RTSP/TCP clients, H.264/positive-framerate SDP,
  interleaved RTPv2/PT96 packet deltas, stable fan-out, EOS and helper summary. This remains
  software-only transport evidence and does not close aircraft, outdoor GNSS, HIL or REAL gates.

- After the shared-media handshake fix, the canonical portable helper passed a
  four-client/two-frame raw RTSP bench:
  `artifacts/evaluation/replay-rtsp-multiclient-20260903-pre.8-v9-portable-4clients-shared.json`
  (3,299 bytes, SHA-256
  `994818A9FE96AD5D1BF1C02286E8A1FF67181F63F203C89B7FB6CE75076308B2`). The bound helper is
  95,744 bytes with SHA-256
  `DE94D5404BEF48B80919BFC06465BD53EBFD50A8B65A50F38DA3BCDDADB58312`; every client completed
  `OPTIONS → DESCRIBE → SETUP → PLAY` and received both steady RTPv2/PT96 frame deltas. The
  artifact remains `software_only_local_rtsp` / `hardware_claim=none` and does not close aircraft,
  outdoor GNSS, HIL or REAL release gates.

### v0.3.0-pre.8 authenticated alert-link bench (2026-09-03)

- 新增 `src/multidetect/alert_link_bench.py`、`scripts/run_alert_link_bench.py` 和
  `multidetect-alert-link-bench`：复用 live patrol 的 HMAC-SHA256 UDP/关联 ACK transport，
  用全新的 `alert_id` 做有界 round-trip，记录重试、缺 ACK、丢包率及 ACK P50/P95。
- 新增 `scripts/verify_alert_link_bench.py` 与
  `multidetect-alert-link-bench-verify`：离线使用严格 JSON 重算样本关系、阈值、事件后缀和
  software/hardware 分域，拒绝回环/未解析端点被写成硬件证据；报告中的飞控和物理释放开关
  固定为 `false`。
- 新增 `configs/schemas/alert-link-bench.schema.json`，并在数据链路、现场接线和仓库架构文档中
  补充运行及归档方式。回环 bench 只证明软件 transport 子门；真实 GR01/射频、时钟同步、
  断链恢复和 HIL/REAL 仍需现场证据，最终 `v0.3.0` 发布门保持未关闭。
- E7 清单新增可选 `real_alert_link` hash-bound 记录及
  `--real-alert-link-bench` CLI 参数；装配与评估会重哈希并重跑硬件域、非回环端点和认证
  ACK 阈值，最终 release channel 强制要求通过的真实告警链路报告。
- `scripts/run_current_release_gate_pre8.ps1` 同步增加可选 `-RealAlertLinkBench` 转发，
  现场 bench 通过后无需手工拼接参数即可完成同一次 E7 装配；默认空值保持历史 prerelease
  report-only 行为。
- 加入本轮 bench/verifier 与 E7 binding 回归后，全量 Python pytest 共收集 `2605` 项，`2604 passed / 1 skipped`；
  最新完整日志为 `artifacts/evaluation/pytest-full-20260903-rtsp-multiclient-v4.log`（3,481 bytes，
  SHA-256 `1CF6757BB855F7D0F51B311D2BA7E528D210550A26EC73399A1B298A68028B69`）。
- 对当前 E7 清单执行最新 report-only 重算，产物为
  `artifacts/evaluation/release-gate-pre.8-current-20260903T103906Z-v7-alert-link-bound-report.json`
  （47,716 bytes，SHA-256 `BD79B0B83FB46C2A531327B273AC5E40A8566FBF52A9F0D49780D497FEA4FF64`）；
  因尚无真实非回环告警链路报告，结果仍为 `gate_passed=false` /
  `release_allowed=false`，不提前发布最终 `v0.3.0`。

### v0.3.0-pre.8 deterministic ONNX Runtime provider fallback (2026-09-03)

- 新增统一的 ONNX Runtime session 选择器：未显式指定 provider 时按
  `TensorRT → CUDA → CPU` 的确定顺序尝试，并且只在 session 构造失败时按后缀继续回退；
  显式 provider 列表保持严格失败，不会静默改用 CPU。
- TensorRT engine-cache 选项只对实际尝试 TensorRT 的 session 注入；`model-check` 与
  `jetson-bench` 现在记录可用 provider、每次尝试、异常摘要和最终选择，方便现场 Orin
  复核真实加速来源。
- `vision`、metric depth、appearance/vehicle ReID 和 semantic environment 共用该策略，
  新增构造失败回退、显式策略 fail-closed、TensorRT cache 选项和可用 provider 过滤回归。
- provider 变更后的 full pytest 共收集 `2581` 项，`2580 passed / 1 skipped`；日志为
  `artifacts/evaluation/pytest-full-20260903-provider-fallback-final.log`（2,916 bytes，
  SHA-256 `2CABB7F82BA1030135CC1EA1D31F5528D833EF44D6E8414669A0E63549A1F7B3`）。
- canonical parity 更新为 `configs/release-parity.json`（SHA-256
  `0AFF3DF8913E8C99C8495FAD743F1E85D68716DE86719E1D91BA628F47343DF7`，
  `core_algorithm_hash=434b86c6a8b677e60a4ae5766586b603855609e43cc0521695aa1e21995963f9`）。
  六源 external SIM join/revalidation 已以该 parity 重新生成并独立重算通过，文件分别为
  `simulator-external-runtime-ai-evidence-20260903-current-provider-fallback-parity.json`
  （SHA-256 `EF0631BF43B11DF6AC170C2F2A6A68C7CFF26AF800FD16260DC861C2EDF07557`）和
  `simulator-external-runtime-ai-revalidation-20260903-current-provider-fallback-parity.json`
  （SHA-256 `EE0B97095738CEBB37BC3AC6435385D22B51FBD63FB4EE6645662392A4D2CC95`）。
- 该阶段的 report-only E7 绑定为
  `artifacts/evaluation/release-gate-pre.8-current-20260903T084500Z-v6-bound.json`
  （SHA-256 `818A77FAB6F7C5AC6735726438CB16E447D16A7058A7C17E7611E8CC4438ED9D`）及报告
  `artifacts/evaluation/release-gate-pre.8-current-20260903T084500Z-v6-bound-report.json`
  （SHA-256 `F97E634AACE4F8E87D01B6880D16DA2A18689CA87B73E77299BC97DF71CA30CA`）。两者
  均保持 `gate_passed=false` / `release_allowed=false`：真实现场链路、部署域 REPLAY 精度、
  人工 Fly View/Mission、HIL/REAL 和最终 release channel 仍未满足。

### v0.3.0-pre.8 deterministic positive replay evaluator fixture (2026-09-03)

- 新增 `src/multidetect/replay_set_fixture.py` 与
  `scripts/create_replay_set_positive_fixture.py`：从已校验的六分层 raw replay 派生一个
  明确标记为 `synthetic_demo` 的正例 fixture，五个正例层逐帧包含固定 `flame`/`smoke`
  框，`no_fire` 保留为空负例，预测框、帧 ID、延迟序列和所有文件哈希均可重复生成。
- 新 fixture 会自动写出 `accuracy-report.json` 与 `fixture-summary.json`，验证 IoU、P50/P95
  延迟、正例分层覆盖、严格 JSON 和 manifest 哈希绑定；它保留
  `annotations_reviewed=false`、`deployment_domain_evidence_complete=false`、
  `accuracy_validated=false`，不会提升 REPLAY 或最终 v0.3.0 发布门。
- 新增回归覆盖正例指标通过、`no_fire` 负例、输出目录冲突和源目录边界；它只验证评估器
  接线，真实部署域数据仍需现场采集、独立标注和人工复核。
- 加入该 fixture 后的完整 Python 回归共收集 `2584` 项，`2583 passed / 1 skipped`；日志为
  `artifacts/evaluation/pytest-full-20260903-positive-replay-fixture-final.log`（2,916 bytes，
  SHA-256 `EA942313DB273CEBEE6FA40DA1D2F824E84AB864002C011A4F4C091FE6D7D77E`）。
- 修正 evaluator 报告中的临时绝对路径：fixture 现在序列化 root-relative manifest 名称，
  新增跨输出目录 58 文件逐字节确定性回归；最新 full pytest 共收集 `2585` 项，`2584 passed / 1 skipped`，
  日志为 `artifacts/evaluation/pytest-full-20260903-positive-replay-fixture-deterministic-v3-final.log`
  （SHA-256 `040FDEF824CB88E7D1BC4DAAF692644583E86DAE999B0725E1B5374608990C89`）。

### v0.3.0-pre.8 field-session, outdoor calibration and parity-bound parser hardening (2026-09-03)

- 现场会话 JSONL、现场捕获报告和室外目标真值 JSON/JSONL 现在统一调用
  `autonomy.serialization.strict_json_loads()`：接受 Windows UTF-8 BOM，拒绝重复 JSON key
  与 `NaN/Infinity`，避免后出现的字段覆盖 `DET → TRK → LCK → TGT`、捕获 provenance 或
  WGS-84 真值连接数据。
- 相机标定文档、室外深度标定文档和 JSON 样本行也通过同一严格入口读取；标定前拒绝重复
  key、非有限数字和非法 UTF-8，避免迁移到室外设备后把不确定的尺度参数当成有效距离。
- 捕获器按行将严格解析错误记录为 `invalid_json`，报告 verifier 重新打开并校验复制输出；
  旧 `gate_passed`、旧摘要和源文件追加内容都不能把篡改或不完整的现场流提升为通过。
- replay-set calibration、SIM combined/external/safety、场景矩阵、3-D control、integration
  与 zone evidence verifier 的文件入口也接入同一严格 helper，发布门所有 hash-bound 子门不再
  留下宽松 JSON 旁路。
- 新增回归覆盖现场 session loader、BOM、重复 key、非有限数字、capture source/report verifier
  以及 survey JSON/JSONL 两种输入；不触网、不登录设备，不改变现场、HIL/REAL 或最终版本门。
- 全量 Python pytest 收集 `2577` 项，`2576 passed / 1 skipped`（Windows 账户缺少 symlink
  特权的路径回归自动跳过）；完整日志为
  `artifacts/evaluation/pytest-full-20260903-depth-camera-parity-final2.log`（2,916 bytes，SHA-256
  `2A916958D996C8FF2A2E0032805CCAA282A9CFD43C9E54AABE261C5C13B88FD4`）。当前 parity 文件
  `configs/release-parity.json` SHA-256 为
  `D5A4A5321058EAFB7481D2145382A4D86F003123BA37CFB82CEBC12484A01589`，
  `core_algorithm_hash=8d3d5c815fcab4a07d1e0bbb13e0e954b12804fb22f5e38b1ef8049f45292fe2`。
  当前 report-only
  E7 报告为
  `artifacts/evaluation/release-gate-pre.8-current-20260903T080220Z-v6-bound-report.json`
  （47,462 bytes，SHA-256
  `CFBDCEB875385144ED7723F2C267A27FFF91D5FA4A7217E273DEF26746C23D64`），门仍为
  `gate_passed=false` / `release_allowed=false`。

### v0.3.0-pre.8 replay evidence parser hardening (2026-09-03)

- `load_replay_set_manifest()` 现在拒绝重复 JSON key 与 `NaN/Infinity`，并兼容 Windows UTF-8
  BOM；manifest 解析失败保持 `AutonomyContractError` 的具体原因，不再允许 last-key-wins 语义
  污染 hash-bound replay 证据。
- replay-set readiness 对 accuracy report 使用同样的严格解析，拒绝符号链接路径，并在读取后
  继续执行 manifest/canonical SHA-256 绑定与 fresh evaluator recomputation。新增重复 `reviewed`
  key、`NaN` 和 manifest BOM 回归；不触网、不启动设备，不改变 SIM/现场/HIL/REAL pending 状态。
- IoU evaluator 读取 `ground-truth.jsonl` / `predictions.jsonl` 时同样拒绝重复 key 与
  `NaN/Infinity`，并兼容 UTF-8 BOM；新增 JSONL 回归，防止 hash-bound 标签或预测流被宽松解析
  语义污染。
- replay-set manifest、recording、truth、prediction 引用链和 accuracy report 路径现在拒绝符号
  链接，防止路径别名改变证据来源；新增路径回归（在不支持符号链接的 Windows 账户上自动跳过）。
- release-gate、release-mode-evidence 和 release-gate-manifest 的通用 JSON 入口也统一拒绝重复
  key 与 `NaN/Infinity`，simulator-safety 绑定读取复用同一解析器；模式证据、校准报告、目标
  精度和三维安全证据不会因宽松 JSON 解析出现字段覆盖。
- 全量 Python pytest 收集 `2563` 项，`2562 passed / 1 skipped`（Windows 账户缺少 symlink
  特权的路径回归自动跳过）；完整运行日志为
  `artifacts/evaluation/pytest-full-20260903-release-json-strict.log`（19,550 bytes，SHA-256
  `B8AEE0BB93FED04EF6F3CD2C21CDBA8137A51BA94943BCD0A8D51418E7145755`）；当前 report-only
  E7 清单
  `release-gate-pre.8-current-20260903T064950Z-v6-bound.json` SHA-256 为
  `138C5A6997B10742BF20567D983D0196A76E40FC0511C441E1AD836BFB5FF585`，报告 SHA-256 为
  `06AF4EE65A3B67A7A09CC549A9A2A548E0FF1162BCACC9D33EBCF369D1D3822A`，门仍为
  `gate_passed=false` / `release_allowed=false`。

### v0.3.0-pre.8 field-link report parser hardening (2026-09-03)

- `verify_field_link_report()` 现在以拒绝重复 JSON key 和 `NaN/Infinity` 的严格解析器重开
  主现场预检报告，并拒绝符号链接路径；UTF-8 BOM 继续兼容。恶意或意外的后出现字段不能
  覆盖 `hardware_link_ready`、probe、route 或接口绑定摘要，异常输入保持 fail-closed。
- 新增回归覆盖重复 `summary` key 和 `NaN` 常量；现场报告、接口诊断与其 hash-bound 绑定
  均继续通过现有只读 verifier。该收口不触网、不登录、不改变现场 `offline_pending`、HIL/REAL
  或最终 `v0.3.0` 发布状态。
- 全量 Python pytest 收集 `2555` 项并全部通过；E7 report-only 复算报告
  `release-gate-pre.8-current-20260903T062700Z-v6-bound-report.json` 为 47,441 bytes，
  SHA-256 `B6D5C4048316F7B7D56556AAE7324572FCAEFD5916B1A04585B0D3DDA7D9CE4B`，仍保留
  `gate_passed=false` / `release_allowed=false`。

### v0.3.0-pre.8 portable SIM evidence paths (2026-09-03)

- 修复 deterministic SIM 证据的迁移不稳定性：`world_origin.world_path`、矩阵 `manifest` 和
  `scenario_directory` 现在使用仓库根相对 POSIX 路径；绝对路径只保留在运行时文件访问层。
  等价 checkout 回归证明同一场景的 `evidence_hash` 和三机型矩阵 `sha256` 与盘符/临时目录
  无关。
- 新矩阵为
  `artifacts/evaluation/simulator-scenario-matrix-20260903-portable-paths-pre8.json`（13,512
  bytes，文件 SHA-256 `06C8E829CCA2321C5D10F2F700E04D2469772E0C9C6E7711738B160DBC110FC1`，
  canonical SHA-256 `CE87333B43B3D6E5964C031E27AE57EA3F682787EB54FE2F202DCD5FAD1FF95E`），
  独立 verifier 退出码 `0`；三机型 9 个故障事件全部触发且重复 hash 一致。
- 路径收口后的全量 Python pytest 收集 `2549` 项并全部通过；PowerShell wrapper 解析、
  `compileall` 与 `ruff` 也通过。
- E7 report-only 重新绑定该矩阵：清单
  `release-gate-pre.8-current-20260903T054500Z-v6-bound.json`（17,699 bytes，SHA-256
  `6BED228D980FE08248F764DE3DDCFAA3B0D31D560939DD5270BFFB6BA6218B99`），报告（46,311 bytes，
  SHA-256 `21A634283F817179499C76A44CE97AB1CAFAD77BBB23031F8581DBFF4DC53FD4`）。矩阵子门
  通过；总 E7 仍保持 `gate_passed=false` / `release_allowed=false`，现场、REPLAY accuracy、
  人工 Fly View 和 HIL/REAL 阻断不变。

### v0.3.0-pre.8 field-link interface binding (2026-09-03)

- `run_field_link_preflight.py` 新增 `--interface-diagnostic` 与 `--interface-alias`；Windows
  wrapper 同步暴露 `-InterfaceDiagnostic`，把迁移后只读网卡快照的 SHA-256、字节数、CIDR、
  候选接口和显式 alias 写入同一份预检报告。
- `verify_field_link_interface_diagnostic()` 与 `verify_field_link_report()` 会离线重开诊断
  文件、拒绝符号链接/重复 JSON key、重算 hash 并逐字段比对；源文件漂移、CIDR 或候选
  alias 不一致都会保持 `gate_passed=false`。
- `selection.status=missing|ambiguous` 仍是可验证的诊断状态，不会升级
  `hardware_link_ready`，也不代表 GNSS、传感器、执行器、HIL 或 REAL 已通过。
- 新增现场绑定回归覆盖：有效 missing 快照、alias/CIDR 漂移、源文件 hash 变化和 CLI 绑定。
- 本轮全量 Python 回归收集 `2554` 项并全部通过；`ruff check src scripts tests`、`compileall`
  与 PowerShell 语法解析也通过。

本机绑定 smoke 产物为
`artifacts/evaluation/field-link-preflight-interface-binding-smoke-20260903.json`（4,463 bytes，
SHA-256 `E96AD6241C976F85E9FCE1CFE07DCD7A6A73ADEEA0CD0B2817C09A15357278D`）；接口诊断子门
通过，但端点/路由未接通，`hardware_link_ready=false`，不改变现场与最终发布门状态。

默认现场输入已切换为
`artifacts/evaluation/field-link-preflight-20260903-interface-bound.json`（6,648 bytes，SHA-256
`140054B79FD9F74008EDA8551E5EC9109B04688520F60BD2092D2E6F0E5EFDE0`）。使用该报告重算的
E7 report-only 清单为
`release-gate-pre.8-current-20260903T061300Z-v6-bound.json`（17,699 bytes，SHA-256
`138C5A6997B10742BF20567D983D0196A76E40FC0511C441E1AD836BFB5FF585`），对应报告为 47,412
bytes、SHA-256 `427AA13F1F2FB0C552A6B4CDACCB70164989B01F6D05479407E13526E182E0C6`；接口
诊断绑定复核通过，E7 仍因现场端点未接通保持 `gate_passed=false`。

### v0.3.0-pre.8 deterministic SIM fault-coverage rerun (2026-09-03)

- 使用 `scripts/run_simulator_scenario_matrix.py --cover-faults --fault-tail 1` 重新执行
  fixed-wing、VTOL、multirotor 三机型场景；公共预算从 `120` 帧自动扩展到 `2,611` 帧，
  因此覆盖所有声明的有限时长故障，而不是只验证 nominal-only 短跑。
- 当前证据为
  `artifacts/evaluation/simulator-scenario-matrix-20260903-fault-coverage-pre8.json`
  （13,512 bytes，SHA-256
  `026EDF1BA4A4D436C9BEDF2AA61F88A2C86BD645EE632D292567E5E7C8F66307`）。
  `fault_coverage_required=true`、`fault_coverage_auto_extended=true`，三种机型的
  `fault_coverage.coverage_ratio=1.0`、`all_declared_faults_exercised=true`，重复运行的
  `evidence_hash` 保持一致；独立 `verify_simulator_scenario_matrix.py`（传入
  `--expected-release-label 0.3.0-pre.8`）退出码为 `0`。
- 该文件与当前 WGS-84 严格矩阵内容字节一致，只是本轮故障覆盖运行的独立 provenance
  别名；它仍是 `truth_free` / software-only SIM 证据，不提升外部 Gazebo、人工 QGC、
  室外 GNSS、真实 Pixhawk/执行器、HIL/REAL 或最终 `v0.3.0` 发布门。

### v0.3.0-pre.8 E7 显式绑定故障覆盖矩阵（2026-09-03）

- 使用 `scripts/run_current_release_gate_pre8.ps1` 显式传入上述故障覆盖矩阵，生成新的
  report-only 清单
  `artifacts/evaluation/release-gate-pre.8-current-20260903T133500Z-v6-bound.json`
  （17,699 bytes，SHA-256
  `52208E7C3EAA781369C304B1525EFA8D1FD2CEC99F2A1AE28A05B5D538656E42`）和报告
  `release-gate-pre.8-current-20260903T133500Z-v6-bound-report.json`
  （46,311 bytes，SHA-256
  `3A9182A4E9576FF81FD248859C552DD4A5F971A996E667497B5FB0DEDC257780`）。
- E7 清单中的 `simulator_scenario_matrix` 已重新绑定
  `simulator-scenario-matrix-20260903-fault-coverage-pre8.json`，矩阵 verifier exit code
  为 `0`、`gate_passed=true`、`deterministic_gate_passed=true`；E7 聚合结果仍按
  `report-only` 返回 `gate_passed=false` / `release_allowed=false`，阻断项仍是
  REPLAY accuracy、现场链路、人工 Fly View、HIL/REAL 和最终 release channel。

### v0.3.0-pre.8 field-link interface diagnostic (2026-09-03)

- 新增 `scripts/inspect_field_link_interfaces.ps1`：迁移电脑或重新接线后，以只读方式枚举物理
  网卡（包括断开接口）、IPv4 地址和目标飞机网段的直连 route，给出
  `selection.status=ready|ambiguous|missing`，不修改 IP、route、DNS，也不自动改写设备端点。
- 台式机当前快照为
  `artifacts/evaluation/field-link-interface-diagnostic-20260903.json`（4,371 bytes，SHA-256
  `230598F40AC192A34E3C3094B527D33B035DFDC2D7EBE7C148FA47CC36055999`）。结果为
  `selection.status=missing`：`Ethernet` 仅有 `192.168.1.13/24`，目标
  `192.168.144.0/24` 没有候选接口或直连 route；该报告只记录网卡层阻断，不提升硬件/GNSS
  或 REAL 状态。
- 2026-09-03 15:00（本地时间）完成一次新的只读复核：
  `artifacts/evaluation/field-link-interface-diagnostic-20260903-150022.json`（4,378 bytes，
  SHA-256 `472E36A2D912D5B95F9B975B1F1025E35ACEF475D706C0CF71F6076850F8B628`）。结果仍为
  `selection.status=missing`，`Ethernet` 仍是 `192.168.1.13/24` 且无目标网段直连 route；
  该复核尚未重新绑定 E7，保持为现场网卡状态证据。
- 新增静态回归 `tests/test_field_link_interface_diagnostic_script.py`，锁定只读行为、CIDR
  归一化和结构化字段；迁移后的直接全量回归收集 `2546` 项并全部通过（`pytest` 退出码
  `0`）。

### v0.3.0-pre.8 checkout bootstrap compatibility (2026-09-03)

- 验收脚本 `run_actuator_safety_acceptance.py`、`run_fixed_wing_software_acceptance.py` 及
  `verify_actuator_safety_evidence.py` 现在与其他仓库脚本统一：从源码 checkout 直接执行时会
  自动定位相邻的 `src/`，不要求先安装 editable package 或手动设置 `PYTHONPATH`。
- 新增 `tests/conftest.py` 作为旧版 pytest 的 collection fallback；台式机上的全局 pytest
  `6.2.5` 即使不识别 `pyproject.toml` 的 `pythonpath` 选项，也能收集并运行完整测试。
- 迁移后复核：整套 Python 回归收集 `2546` 项、退出码 `0`；Windows Release/portable
  增量构建退出码 `0`，主 EXE SHA-256 继续为
  `4D7A024F80157E5EB8671B77D6E1817FB960ECE8A8A97D0F470A222E7F7C8E40`。

### v0.3.0-pre.8 navigation GPS quality floor (2026-09-03)

- `NavigationFusionConfig` 现在直接复用 `mavlink_gps.py` 的
  `MAVLINK_QUALIFIED_FIX_TYPE_MIN` / `MAVLINK_QUALIFIED_SATELLITES_MIN`；速度融合默认要求
  3-D fix 和至少 6 颗已知卫星，配置只能收紧质量门，不能用 2-D/unknown GPS 伪装成户外导航源。
- 速度一致性路径保留独立的 `GPS age <= 0.75 s` 严格窗口；它比目标落图的 canonical `1.5 s`
  坐标窗口更紧，不改变目标定位门，也不产生飞控指令。新增回归验证默认阈值与放宽配置均 fail-closed。
- Jetson 窄传输部署清单同步加入 `mavlink_gps.py` 与 `navigation_fusion.py`，并在远端 stage
  校验 canonical 常量和不可放宽的速度融合下限；部署仍保持原子备份、哈希校验和失败回滚。
- 该变更重新生成当前 `configs/release-parity.json`：文件 SHA-256 为
  `F099537C3FE5FB604599DD8760C2B472ABD61DE7533EAFF4BA904E6B4D9E9FC2`，
  `core_algorithm_hash=aca794c00666e69fc47c204775f94f0b74fe50da29ef3351b49d0e3dd5fbf375`。
  六源 external SIM join 与独立 revalidation 以新 parity 重算均通过：
  `simulator-external-runtime-ai-evidence-20260903-navigation-gps-floor.json`
  （5,365 bytes，SHA-256 `8B4F85F87B386CCFEBC1584045FDEDA94A71489745D3D0E5D8E3241254E70D15`）和
  `simulator-external-runtime-ai-revalidation-20260903-navigation-gps-floor.json`
  （546 bytes，SHA-256 `697D4993159B5536E39604298EA19611688FDA9D632096BEAF49BE68EE69F479`）。
- 完整 goal acceptance 新快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260903-navigation-gps-floor.json`
  （57,678 bytes，SHA-256 `CB1CE2EC05DADA49E2666E62720D71C47ECC7AA77835C264F0AA090A54D57638`，
  核心测试收集 `2538`，`software_status=pass`）；E7 report-only 清单为
  `release-gate-pre.8-current-20260903T123000Z-v6-bound.json`
  （17,686 bytes，SHA-256 `5BD9E2800DC4DC2C4440880347AD2C7A5D95A915B03FBFC274CED8B6CE9470D3`），
  报告为 46,300 bytes、SHA-256
  `D66D22FEF31E29BCF9347D9AC3AF4DB41E14AC782D1AB0EE952C479B0F6BBD07`；E7 仍明确
  `gate_passed=false` / `release_allowed=false`，因为现场链路、REPLAY accuracy、HIL/REAL
  和人工 Fly View 尚未完成。
- `run_current_release_gate_pre8.ps1` 的无参数默认输入已切换到上述 navigation-GPS-floor
  external join/revalidation，避免 parity 更新后误绑旧快照；旧 bearing-uncertainty 文件继续
  作为历史 provenance 保留。

### v0.3.0-pre.8 replay positive-strata and raw NavSat closure (2026-09-03)

- REPLAY accuracy evaluator 新增固定正向分层契约：`day`、`night`、`haze`、`reflection`、
  `smoke_only` 每层都必须同时出现独立复核的真值对象和 pinned detector 预测；`no_fire` 保持
  负例控制用途，不计入正例分母。报告新增 `positive_strata`、`positive_stratum_coverage`、
  `positive_strata_coverage_passed`，readiness 新增逐层 truth/prediction checks。
- E7 release-gate writer 与 evaluator 对声称通过的 accuracy report 执行同一分层列表、字段和
  非零计数校验；缺字段、零计数、篡改或只在单一条件有正例都会 fail-closed。
- SIM/Gazebo 空基线的新报告为
  `artifacts/replay-sets/sim-gazebo-2026/accuracy-report-20260903-positive-strata.json`
  （SHA-256 `7DE80E0E43D669A3D8DD0D0A4D625CBDCF86B23C25B7580C2ABDEBF0848500CF`）和
  `artifacts/evaluation/replay-set-readiness-20260903-positive-strata.json`
  （SHA-256 `38EFF80B758CBDED613FA6A882DB20AE97A00FA9A00A7DB76B1328CBF32C4018`）；两者均
  `gate_passed=false`，因为当前来源仍是 `synthetic_demo` 且五层正向内容为空。
- `create_gazebo_raw_replay_fixture.py` 对 NavSat 纬度/经度/海拔做 WGS-84 域校验；越界帧计入
  `invalid_navsat_count`，输出为 `position_healthy=false`，不会把非法坐标提升为定位点。对应
  回归覆盖极点、越界经度和异常高程。
- 当前 E7 wrapper 已切换到新格式 accuracy report，并生成独立 report-only 绑定：
  `artifacts/evaluation/release-gate-pre.8-current-20260903T123000Z-v6-bound.json`
  （SHA-256 `5BD9E2800DC4DC2C4440880347AD2C7A5D95A915B03FBFC274CED8B6CE9470D3`）及
  `...-report.json`（SHA-256 `D66D22FEF31E29BCF9347D9AC3AF4DB41E14AC782D1AB0EE952C479B0F6BBD07`）。
  wrapper 返回 `0` 仅表示 report-only 执行完成；`gate_passed=false` / `release_allowed=false`
  仍由合成 replay-set、现场链路、HIL/REAL 和最终 release channel 阻断。

### v0.3.0-pre.8 positive-strata total acceptance rebinding (2026-09-03)

- 用新的五层正向覆盖 accuracy report 重新执行独立总验收，快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260903-positive-strata-v1.json`
  （52,222 bytes，SHA-256
  `ADF74A58DCF7804C3DA4D0C02A8DBFC878F06B450BBF2660E0601D06E2C2760D`）。核心测试收集
  `2544`、QGC custom `66/66`、`software_status=pass`、`hardware_status=offline_pending`。
- 绑定的 readiness 为
  `artifacts/evaluation/replay-set-readiness-goal-acceptance-20260903-positive-strata-v1.json`
  （13,246 bytes，SHA-256
  `38EFF80B758CBDED613FA6A882DB20AE97A00FA9A00A7DB76B1328CBF32C4018`），五个正向分层的
  truth/prediction 计数均为零，故 `gate_passed=false`；这确认了新的分层门确实进入总验收，
  而不是只停留在单独 evaluator。
- external SIM 重验证输出为
  `artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260903-positive-strata-v1.json`
  （SHA-256 `084B08A7B72F7651C22C8E6FA25B1802ADB761FBC8186C071F713B0865821457`）。该子门和
  三机型确定性控制继续通过，但现场链路、HIL/REAL、人工 Fly View 与最终 `v0.3.0` 仍未完成。
- 最新现场 continuation 预检为
  `artifacts/evaluation/field-link-preflight-20260903-continuation.json`（16,803 bytes，
  SHA-256 `98E3D9BA1ECEBB6D41E61CBB54B6DE1E0B648BC86D4554C683E4D1526D2EFF98`）。Ethernet
  仍为 `192.168.1.13/24`，不匹配期望的 `192.168.144.0/24`；Jetson/GR01/RTSP 三个端点
  连续超时，`hardware_link_ready=false`，因此 E7 继续保持 report-only 阻断。

### v0.3.0-pre.8 line-of-sight uncertainty closure (2026-09-03)

- 多模态测距现在把相机/姿态/安装偏差/目标像素的一阶视线扰动同时传播到
  `ground_range_ci95_m`；直接测距路径不再只用斜距 `σ` 乘水平分量，避免室外落图的
  `1σ` 圆被系统性压窄。
- 固定翼 roll/pitch 通过 NED 射线端点的中心差分进入绝对方位 `bearing_sigma_deg`；目标落图
  再以 WGS-84 地面弧长 `r·σθ(rad)` 转换水平误差，替代会低估宽方位门的 chord 近似。
- 统一复用 nominal NED ray 与一组 one-sigma perturbation endpoints。任一扰动穿过地平线、
  或因非法校准/像素不确定度无法生成端点时，`solve()`/`solve_direct()` 都返回明确的
  `INVALID` 原因，不再跳过该项导数后继续发布看似精确的距离。
- 新增近地平线 fail-closed、直接测距 CI 随视线不确定度扩张的回归；将
  `src/multidetect/multimodal_ranging.py` 纳入 `core_algorithm` parity，当前
  `core_algorithm_hash=338981686f2142144f438ce34b4f91a18f6cab76b9f3d8efd238ac1e3fd525e7`；
  当前 `configs/release-parity.json` 文件 SHA-256 为
  `CCDCA6FA7FBE5512200B9A4DCE40F7303E6591ED43BDC33D009AE93075441C2D`。
- 该收口只改变共享测距与证据不确定度计算，不产生飞控指令；现场 GNSS、真实 Jetson/相机、
  HIL/REAL、室外真值和最终 `v0.3.0` 发布门继续保持原状态。

- 室外目标落图新增可选的时间对齐：生产者同时带上 GPS 观测时刻、目标帧时刻、N/E 速度和速度
  观测时刻时，先在 WGS-84 椭球上推进/回推飞机参考点，再叠加目标偏移；对齐窗口以及速度样本
  距离都复用 `GPS age≤1.5 s`，默认速度模型误差 `0.5 m/s` 乘以时间差并入水平 `1σ`。时间补偿
  路径若收到部分或非有限 N/E 速度样本会直接 fail-closed，避免把旧 GPS 点当作零运动且不增加误差；
  完全缺少这些可选字段的旧回放仍保持兼容路径，不做猜测。

### v0.3.0-pre.8 bearing-uncertainty external SIM + live-AI revalidation (2026-09-03)

- 在台式机 Docker Desktop 上重新执行 VTOL、fixed-wing、multirotor 三个独立 PX4/Gazebo
  runtime 和三个 `--exercise-arm --exercise-target` live-AI source。六份 source 均使用同一
  digest-pinned image，runtime/telemetry/cleanup gate、AI policy、target policy 与 target
  exercise 全部通过；每个 AI run `ai_steps=2`，GPS `fix_type=3`、`satellites_visible=10`。
- 六源 join：
  `artifacts/evaluation/simulator-external-runtime-ai-evidence-20260903-bearing-uncertainty.json`
  （5,365 bytes，SHA-256
  `403785A43DACE03E2E9C029EBB6FE060E382001ABC32C59CD9987C43D2FFBB36`）；独立 revalidation：
  `artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260903-bearing-uncertainty-acceptance.json`
  （545 bytes，SHA-256
  `9DAC947BE5A24AEB1722677B51880D6F9EC8513CCC3F784B9D1684272CC4210B`）。两者均为
  `source_recomputed=true` / `gate_passed=true`，并绑定 parity SHA-256
  `CCDCA6FA7FBE5512200B9A4DCE40F7303E6591ED43BDC33D009AE93075441C2D`。
- 最新严格总验收为
  `artifacts/evaluation/multidetect-goal-acceptance-20260903-bearing-uncertainty.json`
  （45,571 bytes，SHA-256
  `04AFC8C6C61E340020FB7E89483D00111B00C8D61B799289EEDF7F2B4B2BF459`），核心测试
  `2536`、QGC custom `66/66`、`software_status=pass`、credentials
  `ready=true/gate_overridden=false`。该快照补入最终 release-label parser 回归，
  并显式绑定当前六源 external evidence（`provided=true`、`source_recomputed=true`、
  `gate_passed=true`）；证据仍属于 software-only SIM；真实硬件、室外 GNSS、HIL/REAL、
  视觉精度和人工 Fly View 不在声明范围，最终 `v0.3.0` 继续保持 prerelease 阻断。
  此前 19,357-byte 同名快照作为历史 provenance 保留。

### v0.3.0-pre.8 E7 six-source external binding (2026-09-03)

- E7 `release-gate` writer/evaluator 现在将上述六源 external runtime/live-AI join、独立
  `--join` revalidation、六个 source 的 SHA-256/字节数、五组 parity hash 和 13 项检查作为
  顶层 `simulator_external_evidence` 绑定；写入与读取都会重新计算 source，路径复用、摘要
  漂移、伪造 gate 或 parity 不一致均 fail-closed。
- external `release_status` parser 同时接受 `v0.3.0-pre.N` 与最终 `v0.3.0` 标签；这只是
  最终 source evidence 的解析兼容，最终 release 仍要求 product channel、parity、六源重算
  以及 REPLAY/SIM/HIL/REAL 全部发布门通过。
- 当前 manifest：
  `artifacts/evaluation/release-gate-pre.8-current-20260903T024500Z-v6-bound.json`
  （17,696 bytes，SHA-256
  `3D379F24C67F438A287D5F714CD4AEE79C6877DE67B6691EE49AF2D012258BEC`）；report-only：
  `artifacts/evaluation/release-gate-pre.8-current-20260903T024500Z-v6-bound-report.json`
  （46,307 bytes，SHA-256
  `C8749E0D7B5AB8C976477CF244586E734DFCEEBD5C07A8047689F62D5BCE040C`）。E7 总门仍为
  `gate_passed=false` / `release_allowed=false`，现场、Replay accuracy、HIL/REAL 和人工
  Fly View 尚未完成；最终 `v0.3.0` 仍需新的最终标签 source evidence。

### v0.3.0-pre.8 desktop field discovery provenance (2026-09-03)

- 台式机迁移后新增一次有界只读 `192.168.1.0/24` 服务发现，并追加 SSH banner、被动 MAVLink
  帧和 RTSP `OPTIONS` 检查：扫描 `254` 个地址，发现 `4` 个普通 SSH 响应主机，但
  `5760/MAVLink` 与 `554/RTSP` 均为 `0`。最新报告
  `artifacts/evaluation/field-link-discovery-20260903-093500-protocol.json` 为 8,436 bytes，SHA-256
  `E10B84D77128E4C0FE64FB41C9805D5757C7F5F5832086C451ABB4B653193EC4`；
  `verify_field_link_preflight.py --allow-unready` 退出 `0`，`hardware_link_ready=false`。
- 现场发现只进入诊断证据，不自动选择 Jetson/GR01/相机端点，也不发送 MAVLink、RTSP 或控制帧。
  `run_current_release_gate_pre8.ps1` 的默认 external revalidation 已固定到最新
  `...bearing-uncertainty-acceptance.json`，现场 link 输入也固定到最新
  `field-link-preflight-20260903-desktop-recheck.json`（SHA-256
  `3C6BF75D5D352129A77212FB284400E396EF36282317BC5C9F14F982FF96830E`），避免后续无参数复核
  重新绑定较早的同内容历史快照；该预检仍明确 `hardware_link_ready=false`。

### v0.3.0-pre.8 wrapper/build recheck (2026-09-03)

- 从仓库根目录使用默认输入（仅显式传入唯一 UTC stamp 防止覆盖历史文件）执行
  `scripts/run_current_release_gate_pre8.ps1` 并生成新的 report-only
  清单：`artifacts/evaluation/release-gate-pre.8-current-20260903T024500Z-v6-bound.json`
  （17,696 bytes，SHA-256 `3D379F24C67F438A287D5F714CD4AEE79C6877DE67B6691EE49AF2D012258BEC`）及
  报告（46,307 bytes，SHA-256
  `C8749E0D7B5AB8C976477CF244586E734DFCEEBD5C07A8047689F62D5BCE040C`）。脚本退出码 `0`，
  但 `gate_passed=false` / `release_allowed=false`，没有提前关闭 Replay-set、现场、HIL/REAL
  或最终 release 门。
- 已有 `v0.3.0-pre.8` Release/portable 主 EXE 重新核对为 `46,575,616` bytes、SHA-256
  `4D7A024F80157E5EB8671B77D6E1817FB960ECE8A8A97D0F470A222E7F7C8E40`；portable
  `--offline --simple-boot-test --allow-multiple --no-windows-assert-ui --log-output`
  退出码 `0`，日志未出现 QML 初始化失败或崩溃。全量 Python/QGC custom 回归、compileall 与
  ruff 均通过；全局 `git diff --check` 受脏工作树中既有 CRLF/尾随空白噪声影响，未把它记为通过。
  本轮没有调用 computer-use。

### v0.3.0-pre.8 partial-velocity motion guard (2026-09-03)

- `target_geolocation_from_ned_offset()` 现在拒绝时间差非零时携带的部分/非有限 N/E 速度对，
  不再静默发布未补偿且未扩张运动误差预算的坐标；完整速度对继续按 WGS-84 有界推进/回推，
  完全没有速度输入的旧低层调用仍保持历史兼容。
- 新增直接投影和 operator-status fail-closed 回归；最新总验收为
  [`multidetect-goal-acceptance-20260903-partial-velocity-fail-closed.json`](artifacts/evaluation/multidetect-goal-acceptance-20260903-partial-velocity-fail-closed.json)
  （44,935 bytes，SHA-256
  `FBB3637449BD68013325B3840DAF7FC20414836EA65FC1B11CFBCBC64FA8DF01`），核心测试收集
  `2529`、QGC custom `66/66`、`software_status=pass`。
- 同一 parity 重新绑定 E7：
  `artifacts/evaluation/release-gate-pre.8-current-20260902T233717Z-v6-bound.json`
  （15,971 bytes，SHA-256
  `F3BF46225EA499D2BFEBF98BDCA0DD5FF6B6A4FDC7D6C2C52080A8C41441B248`），report/strict
  （64,949 bytes）SHA-256 均为
  `101A83E9AF1CD8DBFF1E1F72DD43BB97BEE2FBBA3D42324770AE24645092AB27`；发布门仍保持
  `gate_passed=false`，因为现场、REPLAY accuracy、HIL/REAL 和人工 Fly View 尚未完成。

### v0.3.0-pre.8 motion-compensation acceptance rebind (2026-09-03)

- 用时间对齐实现和当前 `configs/release-parity.json` 重新执行严格总验收，生成
[`artifacts/evaluation/multidetect-goal-acceptance-20260903-velocity-timestamp-stale-velocity-strict.json`](artifacts/evaluation/multidetect-goal-acceptance-20260903-velocity-timestamp-stale-velocity-strict.json)
（44,935 bytes，SHA-256
`114DCC517BEAB5C9B882C55E73D08A86B358A4749E42A07E0FB3350B16E0605C`）。脚本退出码为
`0`，核心测试收集 `2527`、QGC custom `66/66`、`software_status=pass`，operator
credentials 为 `ready=true` 且 `gate_overridden=false`。
- E7 writer 随后以同一 parity 和最新现场只读 bundle 生成
[`artifacts/evaluation/release-gate-pre.8-current-20260902T232002Z-v6-bound.json`](artifacts/evaluation/release-gate-pre.8-current-20260902T232002Z-v6-bound.json)
（15,971 bytes，SHA-256
`9C9D918A947F7AB13FEA1108921DF315B50D40327E65EA3F71007AA6CE83C7DE`）；对应
report/strict（64,949 bytes）SHA-256 均为
`AE2DDB51FF1C23C9B2CCE5B5AA4736346DDCDF1B59771032E9398A1F2D1364E6`。严格门仍为
  `gate_passed=false` / `release_allowed=false`，现场链路、Replay-set accuracy、HIL/REAL、
  人工 Fly View 和 prerelease 版本门保持阻断；本轮没有提前发布最终 `v0.3.0`。

### v0.3.0-pre.8 line-of-sight acceptance rebind (2026-09-03)

- 用更新后的测距核心重新执行 `run_goal_acceptance.ps1`，生成
  [`artifacts/evaluation/multidetect-goal-acceptance-20260903-los-uncertainty-strict.json`](artifacts/evaluation/multidetect-goal-acceptance-20260903-los-uncertainty-strict.json)
  （44,935 bytes，SHA-256
  `D8916CC0737F90117238DCB9AE610E4CB22F94CF06C5278700760907DA5061A5`）。软件门为
  `pass`，核心测试收集 `2519`、QGC custom `66/66`，operator credentials 为
  `ready=true` 且 `gate_overridden=false`；硬件仍为 `offline_pending`。
- 同一 parity 重新执行 E7 writer，生成
  `artifacts/evaluation/release-gate-pre.8-current-20260902T224050Z-v6-bound.json`
  （15,971 bytes，SHA-256
  `4BB67B9826D78B35E9EC1213D0E83A09C080677BCB55DBB73AC04742AB5D1815`）及其
  report/strict（64,949 bytes，SHA-256
  `4C4956C2FFD09187C3BA4C6578CEFAC5B979072353229E3AB28CE3A4DCCA506B`）。`gate_passed=false`
  / `release_allowed=false` 是预期结果，Replay-set、现场链路、HIL/REAL、人工 Fly View
  和 prerelease 版本门仍阻断最终发布。

### v0.3.0-pre.8 geodetic-domain convergence (2026-09-03)

- 收口 SIM 入口、Gazebo SDF/wire、perception bridge 和 WGS-84 目标投影的纬度域：
  纬度统一严格位于 `(-90, 90)`，经度继续允许 `[-180, 180]`；精确极点在配置、
  世界原点读取、传感器帧发送/接收和目标投影前 fail-closed，避免先落盘后在地图阶段才失败。
- `SimulatorScenarioRunnerConfig` 现在在构造期拒绝 `±90°`，不再把错误延迟到
  `FlightModelConfig`；Gazebo v1 wire producer/consumer 和 perception bridge 对相同边界给出
  一致结果。
- 重新生成 `configs/release-parity.json`，当前 `core_algorithm_hash` 为
  `3cde43180e906d88aca1c0ebb9d75983ab13be9f6e2db12db44428dc08aead48`；core 清单新增
  `src/multidetect/simulator/flight_model.py` 和 `src/multidetect/multimodal_ranging.py`，
  使 SIM geodetic endpoint、目标投影和测距不确定度一起受 parity 绑定。
- `DeterministicFlightModel` 的 SIM telemetry endpoint 改用同一
  `wgs84_forward_from_ned()` 椭球直解，移除高纬度/跨日期变更线场景中的固定
  `111,320 m/degree` equirectangular 近似；极点穿越在 geodesy 入口 fail-closed。
- 新增极点回归覆盖 runner、世界 SDF、Gazebo sensor/backend wire 和 perception bridge；
  聚焦 geolocation/SIM/Gazebo 回归全部通过。该收口不改变现场 GNSS、HIL/REAL 或最终
  `v0.3.0` 发布门状态。

### v0.3.0-pre.8 strict goal acceptance and E7 rebind (2026-09-03)

- 使用当前 parity、严格 operator credential 模式和 WGS-84 三机型场景矩阵重跑总验收：
  [`artifacts/evaluation/multidetect-goal-acceptance-20260903-wgs84-geodetic-strict.json`](artifacts/evaluation/multidetect-goal-acceptance-20260903-wgs84-geodetic-strict.json)
  （56,577 bytes，SHA-256
  `E322E7A7CB8C81EED2DE49D89281261B31BFC4EB1F06CA9AA0B286A4CFDA9D9A`）。脚本退出码为
  `0`，核心测试收集 `2516`、QGC custom `66/66`、`software_status=pass`，凭据
  `ready=true` 且 `gate_overridden=false`；external runtime/AI、scenario matrix、
  三机型 3-D 控制和 PX4 Mission transaction 子门均通过。
- 新的 truth-free/software-only 场景矩阵为
  [`artifacts/evaluation/simulator-scenario-matrix-20260903-wgs84-strict.json`](artifacts/evaluation/simulator-scenario-matrix-20260903-wgs84-strict.json)
  （13,512 bytes，SHA-256
  `026EDF1BA4A4D436C9BEDF2AA61F88A2C86BD645EE632D292567E5E7C8F66307`）；三机型、故障
  覆盖、重复运行和共享 WGS-84 endpoint 校验均通过，不代表真实飞行或室外 GNSS 精度。
- E7 writer 用同一 parity 和最新现场只读 bundle 重新生成
  [`artifacts/evaluation/release-gate-pre.8-current-20260902T221518Z-v6-bound.json`](artifacts/evaluation/release-gate-pre.8-current-20260902T221518Z-v6-bound.json)
  （16,077 bytes，SHA-256
  `E1108579B8D20695812112096FF12DB14E43609ADD2426EA9C01F9D45EE0EC44`），对应
  report/strict 输出为
  [`artifacts/evaluation/release-gate-pre.8-current-20260902T221518Z-v6-bound-report.json`](artifacts/evaluation/release-gate-pre.8-current-20260902T221518Z-v6-bound-report.json)
  （65,062 bytes，SHA-256
  `382EC288174553A2239675C1B437880E59220D879A00AB0177F6E80E618CFC21`）；严格输出
  [`artifacts/evaluation/release-gate-pre.8-current-20260902T221518Z-v6-bound-strict.json`](artifacts/evaluation/release-gate-pre.8-current-20260902T221518Z-v6-bound-strict.json)
  与 report-only 字节和 SHA-256 相同。E7 仍为
  `gate_passed=false`、`release_allowed=false`，阻断项是 Replay-set、现场链路、HIL/REAL
  和人工验收；本次只更新 provenance/hash 绑定，不提前发布最终 `v0.3.0`。

### v0.3.0-pre.8 field evidence bundle join (2026-09-03)

- 新增 `src/multidetect/field_evidence_bundle.py`、
  `scripts/check_field_evidence_bundle.py` 和
  `configs/schemas/field-evidence-bundle.schema.json`，将现场链路、receive-only raw
  GNSS、严格 `DET → TRK → LCK → TGT` 会话、目标事件媒体、raw replay telemetry 时间轴和
  独立 WGS-84 精度报告做六层 hash-bound 只读复核。
- 检查器逐文件重算 SHA-256/字节数，拒绝 stale GPS/RANGE、sidecar 与 raw recording 漂移、
  未来或无前置 telemetry、跨会话/跨目标绑定和 scenario-bound recording 混入默认 REAL
  域；`--allow-sim-domain` 只用于 SIM 诊断，`--report-only` 只记录待办不改变门状态。
- E7 release-gate 新增可选 `--field-evidence-bundle` 绑定：写入时和每次读取清单时都
  重新打开七个输入、复算六层摘要并检查 SHA-256/字节数；预发布可保存失败待办，最终
  `v0.3.0` 要求真实域六层全部通过，防止复制 `gate_passed` 或旧摘要绕过发布门。
- 修复待机报告重验证：即使输入文件尚未生成，也保留报告中记录的未来绝对路径参与重算，
  因此合法的 `gate_passed=false` bring-up 报告不会被误报为摘要篡改；文件后来出现时，
  旧报告会因 `present`/哈希变化而 fail-closed，必须重新生成 bundle。
- 报告和 schema 固定 `release_eligible=false`、`hardware_acceptance=not_claimed`、
  `real_flight_acceptance=not_claimed`、`contains_flight_control=false`；新增 5 项专项
  回归覆盖缺件、完整合成 join、sidecar 篡改、CLI 和 schema fail-closed 约束。
- 同步 `docs/field-bringup.md`、`docs/release-gate.md`、`docs/repository-architecture.md`
  和 README 的现场命令与证据边界说明。该工具只提高证据连接完整性，不关闭当前 Jetson、
  室外 GNSS、Pixhawk、HIL/REAL、人工 Fly View 或最终 `v0.3.0` 门。
- 台式机迁移后的第一份 report-only 起点为
  [`artifacts/evaluation/field-evidence-bundle-20260903-desktop-pending.json`](artifacts/evaluation/field-evidence-bundle-20260903-desktop-pending.json)
  （8,858 bytes，SHA-256
  `A551FA06762C4BE8697AB358D6109F54DCBDCDAE62FC60B2943AD4468251A57B`）；现场链路和其余
  五类真实采集仍未就绪，`gate_passed=false` 为预期结果。
- 2026-09-03 再做一次桌面只读链路复核，生成
  [`artifacts/evaluation/field-link-preflight-20260903-desktop-recheck.json`](artifacts/evaluation/field-link-preflight-20260903-desktop-recheck.json)
  （5,892 bytes，SHA-256
  `3C6BF75D5D352129A77212FB284400E396EF36282317BC5C9F14F982FF96830E`）和新的
  [`artifacts/evaluation/field-evidence-bundle-20260903-desktop-recheck.json`](artifacts/evaluation/field-evidence-bundle-20260903-desktop-recheck.json)
  （8,966 bytes，SHA-256
  `CFC201B338A2E8A11B02CF582755E4AF1F0C062F04468B3EA070A3CD29EB88FE`）。目标网段、
  Jetson/GR01/RTSP 端点仍不可达；bundle 完整性重验通过，但现场六层门继续保持关闭。
- `scripts/run_current_release_gate_pre8.ps1` 新增可选 `-FieldEvidenceBundle`，在不改变
  历史默认输入集合的前提下，将最新现场 bundle 传给 E7 manifest writer 做哈希绑定与重验。
- 本轮实际用最新桌面链路和 bundle 生成
  [`artifacts/evaluation/release-gate-pre.8-current-20260902T213355Z-v6-bound.json`](artifacts/evaluation/release-gate-pre.8-current-20260902T213355Z-v6-bound.json)
  （15,978 bytes，SHA-256
  `DD14E6263DA6329754B90903658B765BFDFC64D0000BA0F8285680FE9173EC52`）及其 report-only
  结果
  [`artifacts/evaluation/release-gate-pre.8-current-20260902T213355Z-v6-bound-report.json`](artifacts/evaluation/release-gate-pre.8-current-20260902T213355Z-v6-bound-report.json)
  （64,963 bytes，SHA-256
  `44220D50A64C73D8A0C831896C05E011E9E9D3156FB0A19498098DB8AC367E3F8`）。绑定重验通过，
  但现场 bundle 和总体发布门仍为 `gate_passed=false` / `release_allowed=false`。

### v0.3.0-pre.8 E7 current evidence rebind (2026-09-03)

- 以当前 `v0.3.0-pre.8` 版本清单重新生成并哈希绑定四模式 E7 清单
  [`artifacts/evaluation/release-gate-pre.8-current-20260903-v6-bound.json`](artifacts/evaluation/release-gate-pre.8-current-20260903-v6-bound.json)
  （14,890 bytes，SHA-256
  `EA1B50B3CC039F3EE4DFA5DCFF3DFFC750F35A79B7ED334FD4A63D8880D27B9E`）。本次绑定当前
  Replay-set/calibration、现场 follow-up、SIM S4、执行器安全、三机型 scenario matrix、
  combined SIM/QGC/Mission、PX4 Mission 事务和 VTOL/fixed-wing/multirotor 3-D 控制记录；
  所有共享 parity hash 保持一致。
- 固定时间 report-only 复核为
  [`release-gate-pre.8-current-20260903-v6-bound-report.json`](artifacts/evaluation/release-gate-pre.8-current-20260903-v6-bound-report.json)
  （42,367 bytes，SHA-256
  `6291DB0052822BC348BDD9DA888960BA2C44C5C00262F11A1BD3F937788DD216`），严格检查同样
  返回非零并写出同字节的 `...-strict.json`；`release_allowed=false` 是预期结果。阻断原因
  仍为 Replay 合成数据/accuracy 未通过、现场 `192.168.144.0/24` 链路未就绪、四模式
  operator Fly View/Mission 状态未声明，以及 HIL/REAL 硬件与 real-control 未完成；没有把
  software-only SIM 或历史证据升级为真实验收。
- `docs/release-gate.md` 的复核命令改为 UTC 时间戳输出并保留 `--no-overwrite`，可重复执行
  而不会覆盖历史清单；`tests/test_goal_acceptance_script.py` 新增 E7 输入完整性回归。全仓
  Python pytest 当前收集 `2488` 项并以退出码 `0` 完成。
- 新增 [`scripts/run_current_release_gate_pre8.ps1`](scripts/run_current_release_gate_pre8.ps1)
  作为同一组显式 E7 输入的可重复入口：自动解析 canonical 版本、校验所有源文件、生成
  唯一 UTC 清单/report，并以 `-Strict` 额外写出严格报告。脚本实际 smoke run 的默认结果为
  `gate_passed=false`、`release_allowed=false`；复用同一 stamp 会直接拒绝覆盖已有文件。
  新增 PowerShell 解析/路径/输入完整性测试后，全仓 pytest 收集数为 `2491`，退出码 `0`。
- 只读现场预检再次确认本机 `Ethernet=192.168.1.13/24` 会从目标
  `192.168.144.0/24` 外选路，Jetson/GR01/相机端点均超时，保存的
  `artifacts/evaluation/field-link-preflight-20260902-200533-followup.json`（11,756 bytes，
  SHA-256 `D1E587996539374FE543D757A132CEED4329E53E0161EE373D529D279C1A9B2D`）保持
  `hardware_link_ready=false`；未修改网卡、IP、路由或飞控状态。

### v0.3.0-pre.8 goal-acceptance strict credentials re-run (2026-09-03)

- 在 external/combined provenance 已绑定的 v5 基础上去掉
  `-AllowMissingOperatorCredentials`，按用户级 operator/MAVLink key 严格模式重跑总验收，生成
  `artifacts/evaluation/multidetect-goal-acceptance-20260903-replay-readiness-v6-strict.json`
  （56,347 bytes，SHA-256
  `CD979D0C0E6DE1FC6C777D0592049DDB289DE7E3DEAB1D2D4DCDA4AA36937AAD`）。本轮
  `dirty_entry_count=430`、核心测试 `2486`、QGC custom `66/66`，脚本退出码 `0`；
  `operator_credential_readiness.ready=true` 且 `gate_overridden=false`，external/combined
  软件子门均保持 `gate_passed=true`。
- readiness 输出为
  `artifacts/evaluation/replay-set-readiness-goal-acceptance-20260903-v6.json`
  （9,379 bytes，SHA-256
  `77D65F2EF5596DAAA7135B7E91CD9927DF5EF70BF43C97CC6F64BB67D7EF6414`）；REPLAY 严格子命令
  仍返回 `2`/`gate_passed=false`，现场链路仍为 `hardware_status=offline_pending`。该严格
  快照只关闭软件凭据/外部 SIM provenance 缺口，不升级真实 Jetson、室外 GNSS、Pixhawk、
  HIL/REAL 或人工 Fly View。
- 同步 `docs/replay-set-manifest.md` 的复核命令：当前示例同时绑定 strict credentials、
  external runtime/live-AI join 和 combined SIM/QGC/Mission envelope，避免仅刷新 REPLAY
  节点时覆盖掉 v6 总验收中的其他 provenance。
- 新增 `test_replay_docs_keep_current_goal_acceptance_provenance_bound` 回归测试，锁定这组
  文档参数和 strict 约束，后续命令漂移会在 CI 直接暴露。

### v0.3.0-pre.8 goal-acceptance external/combined provenance rebind (2026-09-03)

- 在 v4 已绑定 REPLAY readiness 的基础上，重新验证并绑定当前三机型
  external runtime/live-AI join 与 hash-bound combined SIM/QGC/Mission envelope，生成
  `artifacts/evaluation/multidetect-goal-acceptance-20260903-replay-readiness-v5-external-bound.json`
  （56,346 bytes，SHA-256
  `A071857A582836B166FAFA4A4661A7929A36B40A53AA865B718B09E522BD7C7F`）。本轮
  `dirty_entry_count=430`、核心测试 `2486`、QGC custom `66/66`，脚本退出码 `0`；
  `simulator_external_evidence.gate_passed=true`、`source_recomputed=true`，
  `simulator_combined_acceptance.gate_passed=true`。
- external join 为
  `artifacts/evaluation/simulator-external-runtime-ai-evidence-20260903-final-fresh-ai-v1.json`
  （5,338 bytes，SHA-256
  `FE00BB61023738E3F3F1CEE98CA5ECA7D194A26585EFF06044140A1AEDA6FBA4`），独立重算为
  `artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260903-final-fresh-ai-goal-v5.json`
  （543 bytes，SHA-256
  `135CCF223DEE62824522F0A06E458886F4BF3F9E490AEBFF0DB3271CB85C7F22`）。这些仍是
  software-only Docker/PX4/Gazebo 证据，不升级为真实硬件、室外 GNSS、HIL/REAL 或人工
  Fly View 通过；REPLAY readiness 仍因合成来源、未复核标注、无正例和无 accuracy report
  返回 `2`/`gate_passed=false`，现场仍为 `hardware_status=offline_pending`。

### v0.3.0-pre.8 goal-acceptance replay readiness provenance re-run (2026-09-03)

- 在补齐 `/.artifacts/`、`/.tmp-*` 临时树忽略规则并加入 provenance 回归后，按当前 SIM
  manifest、现场 follow-up 和同一 dirty worktree 重跑总验收，生成
  `artifacts/evaluation/multidetect-goal-acceptance-20260903-replay-readiness-v4.json`
  （48,040 bytes，SHA-256
  `521C07A4505559E97E208D44CCAA7B7356DD77B7859F44A2FFCE81D858482559`）。本轮
  `dirty_entry_count=430`、核心测试 `2486`、QGC custom `66/66`，脚本退出码 `0`。
- 同轮 readiness 输出为
  `artifacts/evaluation/replay-set-readiness-goal-acceptance-20260903-v4.json`
  （9,379 bytes，SHA-256
  `77D65F2EF5596DAAA7135B7E91CD9927DF5EF70BF43C97CC6F64BB67D7EF6414`）。严格 readiness
  子命令返回 `2`、`gate_passed=false`，明确记录六个 SIM 条目仍为 `synthetic_demo`、
  标注未独立复核、无正例/accuracy report；现场链路仍为 `hardware_status=offline_pending`。

### v0.3.0-pre.8 acceptance provenance hygiene (2026-09-03)

- `.gitignore` 现在覆盖迁移后验收工具产生的 `/.artifacts/`、`/.tmp-*` 和
  `/.tmp-pytest-*/` 临时树；保留磁盘上的既有内容，不把测试输出混入 Git provenance。
- 在同一当前 SIM manifest 和现场 follow-up 上重跑总验收，生成
  `artifacts/evaluation/multidetect-goal-acceptance-20260903-replay-readiness-v3.json`
  （48,040 bytes，SHA-256
  `628D4CC2D05D1FA080CCCBFFAB3560D0A58C0F85FBBBAB55785B1E8E36A9849D`）。本轮
  `dirty_entry_count=430`、核心测试 `2485`、QGC custom `66/66`，脚本退出码 `0`；
  REPLAY readiness 严格子命令仍返回 `2`/`gate_passed=false`，现场硬件仍为
  `offline_pending`。

### v0.3.0-pre.8 goal-acceptance replay-set binding (2026-09-03)

- `scripts/run_goal_acceptance.ps1` 现在支持 `-ReplaySetManifest`、可选
  `-ReplaySetAccuracyReport`/`-ReplaySetReadinessOutput` 以及 `-RequireReplaySet`；脚本会
  调用 `scripts/check_replay_set_readiness.py`，把 manifest/readiness/accuracy 的哈希、
  verification exit code、逐项 `blocking_reasons` 和 `next_actions` 写入总验收证据。
- 使用当前 SIM manifest 和最新现场 follow-up 生成
  `artifacts/evaluation/multidetect-goal-acceptance-20260903-replay-readiness.json`
  （48,037 bytes，SHA-256
  `4E708B6843CFC26773E5EEEB5D5A3999A4428AECE5998468966E7BADE67ED650`）。本轮核心测试
  `2485`、QGC custom `66/66`、脚本退出码 `0`；嵌入 readiness 命令按严格语义返回
  `2`，因为 SIM 条目仍是 `synthetic_demo`、未完成独立标注且没有正例/accuracy report。
- 总快照新增 REPLAY 待办：必须提供六分层 `deployment_recording`、复核真值/预测和
  哈希绑定 accuracy report；该软件绑定不会把 SIM 空基线、现场断链或 prerelease channel
  升级成 HIL/REAL 或最终 `v0.3.0`。

### v0.3.0-pre.8 goal-acceptance rebind to latest field follow-up (2026-09-03)

- 用最新 `field-link-preflight-20260903-followup.json` 重新执行完整 goal-acceptance，生成
  `artifacts/evaluation/multidetect-goal-acceptance-20260903-followup.json`（43,370 bytes，
  SHA-256 `8E448A74D4606A681CC225CB6D106A7AFD86B692D1FA9B7FB97E1A678BECFC1A`）。快照绑定
  `field_link_preflight.verified=true`、`hardware_link_ready=false`，版本仍为
  `0.3.0-pre.8`，`software_status=pass`、核心测试 `2484`、QGC custom `66/66`。
- 本次总验收脚本退出 `0` 只表示软件/结构门完成；剩余现场门仍是 ChArUco 实采、推进器
  断开后的真实舵机与失锁、真实 DET→TRK→LCK→TGT、Jetson soak 和三机型人工 Fly View。
  新快照没有把断链现场或 prerelease channel 升级成最终 `v0.3.0`。

### v0.3.0-pre.8 bounded field-link follow-up (2026-09-03)

- 在台式机当前 `Ethernet=192.168.1.13/24` 上重新执行一轮有界、只读现场链路 watch：
  `artifacts/evaluation/field-link-preflight-20260903-followup.json`（5,895 bytes，SHA-256
  `951CAC7DD09775904F6E56CDC9D70D00C916B0D803EABBB77F040F6A0DD00BB4`）。本轮确认本地
  UDP `14580/14581` 可绑定，但 Jetson `192.168.144.20:22`、GR01 `192.168.144.11:5760`、
  相机 `192.168.144.108:554` 均超时，路由源地址也落在期望 `192.168.144.0/24` 之外。
- `--allow-unready` 结构 verifier 返回 `0` 只代表报告形状/哈希一致；报告仍为
  `hardware_link_ready=false`、`all_passed=false`、`stop_reason=failure`，没有升级 HIL/REAL
  或最终 `v0.3.0` 发布门。接通飞机网后必须用实际三端点执行
  `-ProtocolChecks -RequireRoute -RequireReady`。

### v0.3.0-pre.8 replay-set readiness/intake checks (2026-09-03)

- 新增 `src/multidetect/replay_set_readiness.py`、`multi-detect check-replay-set-readiness` 和
  `scripts/check_replay_set_readiness.py`。readiness 会重新验证 replay-set 的原始/规范哈希、
  六层部署来源、独立标注、预测流和正例分母；如果提供 accuracy report，还会按报告门限重跑
  evaluator 并比对关键字段，不能靠手工改写 `gate_passed` 绕过。
- 新增 `configs/schemas/replay-set-readiness.schema.json` 与回归
  `tests/test_replay_set_readiness.py`，覆盖 SIM 空基线、完整正例夹具、报告篡改和 clean-checkout
  wrapper。当前 SIM readiness 仍为 `gate_passed=false`，这是预期的 fail-closed 结果。
- 生成 `artifacts/evaluation/replay-set-readiness-20260903-pre.8.json` 作为当前六层 SIM
  收口清单；它只记录采集缺口和下一步动作，不升级为真实部署域精度或最终 `v0.3.0` 发布证据。

### v0.3.0-pre.8 field-link interface auto-selection (2026-09-03)

- `scripts/prepare_field_link.ps1` 新增可选 `-AutoSelectInterface`：只读枚举活动物理网卡，
  仅当目标飞机网段命中一个候选时选中；零候选或多候选均 fail-closed，并给出地址/候选
  诊断。提权子进程沿用已解析的明确 alias，不重复猜测。
- `scripts/run_field_link_preflight.ps1` 转发该开关，默认显式 `-InterfaceAlias Ethernet`
  路径保持兼容；预检仍不改 IP、默认路由、DNS 或任何飞控流量。
- PowerShell AST 语法检查、现场 wrapper/field-link 回归通过；当前迁移台式机实际烟测因
  只有 `192.168.1.13/24` 而无 `192.168.144.0/24` 候选，正确退出 `1`，没有升级
  `hardware_link_ready`。

### v0.3.0-pre.8 actuator safety contract (2026-09-02)

- 新增 [`src/multidetect/actuator_safety.py`](src/multidetect/actuator_safety.py)：统一有界
  语义命令到 PWM 的方向/行程映射，并将 cancel、lost-lock、watchdog neutral 和 failsafe
  后拒绝命令收口为同一不可绕过的内存状态机。
- 新增 `scripts/run_actuator_safety_acceptance.py`、
  `scripts/verify_actuator_safety_evidence.py` 和
  `configs/schemas/actuator-safety-evidence.schema.json`。当前 pre.8 证据
  `artifacts/evaluation/actuator-safety-software-acceptance-20260902-pre8.json`（2,226 bytes，
  SHA-256 `8AD952BF8EBFDDFCF716EE11F212161B2869EDFA929CFED549657C3D8F58C2CC`）的
  `gate_passed=true`，但明确 `software_only=true`、`physical_outputs_present=false`，
  不提升真实舵机/HIL/REAL 或最终发布门。
- 固定翼软件验收现在嵌入同一执行器安全证据；`run_goal_acceptance.ps1` 会独立运行并验证
  该 artifact，避免只依赖一个复制的 `gate_passed` 标志。
- E7 `release-gate` writer/evaluator/schema 现在支持顶层 `actuator_safety` 哈希绑定；绑定
  会重算 HIL/software-only 边界和八项 canonical check，最终 `release` channel 强制要求
  通过该共享执行器契约。
- 本轮全仓库回归收集 `2467` 项并全部通过（退出码 `0`）。

### v0.3.0-pre.8 strict goal acceptance refresh after actuator safety (2026-09-02)

- 重新执行 `run_goal_acceptance.ps1`，绑定六源 external runtime/live-AI、22:00 后现场预检、
  canonical ChArUco 板和独立执行器安全证据，生成
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-actuator-safety-external-strict.json`
  （43,745 bytes，SHA-256
  `68F4B7565197E58B9346B668CE27234896D32DFA44A9E909FD03355C7B5B6C26`）。
- 快照收集 `core_tests_collected=2467`、QGC custom `66/66`，`software_status=pass`，
  external 与三机型 3-D deterministic binding 均通过；`operator_credential_readiness.ready=true`
  且 `gate_overridden=false`。新增 `actuator_safety_acceptance` 节点独立记录
  `gate_passed=true`、`software_only=true`、`physical_outputs_present=false`。
- 现场预检报告本身 `verified=true`，但 `hardware_link_ready=false`；ChArUco 实采、推进器
  断开后的真实舵机/失锁、真实 DET→TRK→LCK→TGT、Jetson soak 和人工 Fly View 仍在
  `remaining_live_gates`，最终 `v0.3.0` 继续保持未发布。

### v0.3.0-pre.8 E7 actuator safety binding (2026-09-03)

- `release_gate_manifest.py`、`release_gate.py` 与
  `configs/schemas/release-gate-manifest.schema.json` 新增顶层 `actuator_safety` 绑定。
  writer 在生成清单时执行独立 actuator verifier，记录源 SHA-256、HIL/software-only
  边界、推进器软件 fixture 标记和八项 canonical check；evaluator 每次读取都会重哈希、
  重算并比较声明结果。
- 当前绑定清单为
  `artifacts/evaluation/release-gate-pre.8-current-20260903-actuator-bound.json`（14,980
  bytes，SHA-256 `393A329AE46944E20035D899FCF5BD7686183B71E172B132F62137CCA1071F65`）；固定
  时间 report-only/严格报告同为 42,498 bytes、SHA-256
  `94878B1D5230D26CAC56B5049251603F7BA7787FCD4D3ADC1345A021AE2F4355`，退出码 `0`/`2`，
  `actuator_safety.present=true`、`passed=true`。
- 最终 `release` channel 现在强制要求该顶层绑定；当前 pre.8 整体仍
  `gate_passed=false`/`release_allowed=false`，真实舵机/失锁、HIL/REAL、现场链路和人工
  Fly View 等门没有被软件 bench 改写。

### v0.3.0-pre.8 field-link discovery integrity (2026-09-03)

- `src/multidetect/field_link_preflight.py` 新增无网络的
  `verify_field_link_discovery()`，并由 `verify_field_link_report()` 自动调用。它重算
  显式 IPv4 CIDR 的主机数、响应主机数、SSH/MAVLink/RTSP 服务命中数、候选地址顺序、
  端口/传输类型和逐主机 `responsive` 标志；越界地址、重复/乱序主机、篡改计数或伪造
  `hardware_link_ready` 字段都会 fail-closed。
- 保留早期 schema-v1 发现快照的可读性：缺少后来加入的 `service_candidates` 时只标记
  `warnings`，仍校验其服务计数和逐主机记录，并明确候选列表完整性在源文件中不可用。
- 新增字段发现篡改回归测试，并把当前迁移后发现证据
  `artifacts/evaluation/field-link-discovery-20260903-ethernet.json`（SHA-256
  `C1D655F4A8639FF1C1469B40BC8DBC61AA439D0607733B74047074E07D3BDCE4`）纳入离线复核。
  独立复核输出为
  `artifacts/evaluation/field-link-discovery-20260903-ethernet-verified.json`（2,378 bytes，
  SHA-256 `6C0AD7F2BCC97568A719DFADD8FA3BE4E0B596E8F0D0493FB5E60CEE4CC3FE73`，退出码 `0`）。
  该诊断仍为 `hardware_link_ready=false`，不会替代实际 Jetson/GR01/相机端点预检。

### v0.3.0-pre.8 deterministic SIM matrix refresh (2026-09-03)

- 使用当前 pre.8 源码重新运行带 `--cover-faults` 的 fixed-wing、VTOL 和 multirotor
  场景矩阵；三种机型都自动扩展到 `2,611` 帧，声明的 `9` 个故障事件覆盖率均为
  `1.0`，并通过第二次运行的 `evidence_hash` 重复性校验。
- 最新证据为
  `artifacts/evaluation/simulator-scenario-matrix-20260903-pre.8-fault-coverage.json`
  （13,512 bytes，SHA-256
  `C2A2D906263F0271A0873A1D42EAF25FA184423840DC0E050A3361E16BE6817A`）；独立
  `verify_simulator_scenario_matrix.py` 退出码为 `0`。
- 该 artifact 仍明确是 truth-free、software-only SIM 子门，不提升 external 3-D、真实
  硬件、人工 Fly View、HIL/REAL 或最终 `v0.3.0` 发布状态。

### v0.3.0-pre.8 goal acceptance rebinding to SIM matrix (2026-09-03)

- 在同一台式机工作树上重新执行 `scripts/run_goal_acceptance.ps1`，把最新故障覆盖矩阵绑定到
  `artifacts/evaluation/multidetect-goal-acceptance-20260903-sim-matrix.json`（43,118 bytes，
  SHA-256 `0FA334AA6824A6E965DB686A0CF5E5CADAD007840EE8C182AA872B70DDB90BA5`）。脚本退出码为
  `0`，`core_tests_collected=2480`、QGC custom `66/66`，`software_status=pass`、
  `hardware_status=offline_pending`。
- 快照中的 `simulator_scenario_matrix_acceptance` 绑定
  `simulator-scenario-matrix-20260903-pre.8-fault-coverage.json`（SHA-256
  `C2A2D906263F0271A0873A1D42EAF25FA184423840DC0E050A3361E16BE6817A`），三机型/2,611 帧/9 个
  声明故障覆盖和独立 verifier 退出码 `0` 均保持一致。
- 这次是软件/SIM 证据重绑定，不改变真实现场门：ChArUco 实采、推进器断开后的舵机/失锁、真实
  DET→TRK→LCK→TGT、Jetson soak、人工 QGC Fly View 以及 HIL/REAL 仍在 `remaining_live_gates`，
  最终 `v0.3.0` 继续保持未发布。

### v0.3.0-pre.8 fresh external runtime/live-AI revalidation (2026-09-03)

- 在台式机 Docker Desktop 上重新执行 VTOL、fixed-wing、multirotor 三种 external
  PX4/Gazebo + live-AI 场景。新鲜 source 均 `gate_passed=true`、`runtime.status=passed`、
  GPS `fix_type=3/satellites_visible=10`、`ai_steps=2/ai_accepted=2`、目标 exercise 接受、
  stop/remove 清理完成；MAVLink `vehicle_type` 分别为 `22/1/2`。
- 新鲜 source 分别为
  `artifacts/evaluation/simulator-docker-ai-acceptance-20260903-vtol.json`（SHA-256
  `D646C94C309A853641600D8AD5479E3BDF21176FA8B9B016A214987B99EC7193`）、
  `...-fixed-wing.json`（`23468BA7A59C446F7A1D5FA212D0A981A04CCB131982EC003B05013646760375`）和
  `...-multirotor.json`（`00E8428A5214383FC9D762F38B7126A973D314759A02FE6BC9EB2DD9A9106E5D`）。
- 使用旧的独立 runtime source 加上上述三份新鲜 AI source 生成 distinct 六文件 join：
  `artifacts/evaluation/simulator-external-runtime-ai-evidence-20260903-final-fresh-ai-v1.json`
  （5,338 bytes，SHA-256 `FE00BB61023738E3F3F1CEE98CA5ECA7D194A26585EFF06044140A1AEDA6FBA4`）和
  独立 revalidation `...-revalidation-20260903-final-fresh-ai-v1.json`（543 bytes，SHA-256
  `A03F66998AB62558595A52201847E927D0676C78D4F4D7E996C7C955212FAD42`）均为
  `source_recomputed=true`、`gate_passed=true`。
- 严格凭据模式的总验收为
  `artifacts/evaluation/multidetect-goal-acceptance-20260903-fresh-ai-evidence-strict.json`
  （43,480 bytes，SHA-256 `8971E0D996AD39A12DE2EEC822D338B4A03E70049730BE11B74AF98534AF8903`），
  收集核心测试 `2480`、QGC custom `66/66`，`software_status=pass`、
  `hardware_status=offline_pending`、external binding 通过，且 `gate_overridden=false`。
  该刷新只更新 truth-free/software-only SIM provenance，不关闭真实设备、室外 GNSS、
  人工 Fly View、HIL/REAL 或最终 `v0.3.0` 发布门。

### v0.3.0-pre.8 field-link preflight rebind (2026-09-03)

- 重新执行只读现场链路预检并绑定显式 aircraft CIDR `192.168.144.0/24`：
  `artifacts/evaluation/field-link-preflight-20260903-current.json`（6,686 bytes，SHA-256
  `8A6F3624D84FF11B94F8C65B87F2BDF5CABB7A6AF3A4B6075EAA39439A4E3798`）。本机 source
  `192.168.1.13` 不在目标网段，Jetson/GR01/RTSP 均超时，route/protocol 失败，本地 UDP
  bind 可用；报告保持 `hardware_link_ready=false`、`stop_reason=failure`。
- 严格 ready verifier 退出码 `1`，`--allow-unready` 结构/来源复核退出码 `0`，没有把失败
  诊断改写为硬件通过。新的 goal acceptance
  `artifacts/evaluation/multidetect-goal-acceptance-20260903-fresh-ai-field-preflight.json`
  （43,758 bytes，SHA-256 `1553655AEFCCC504F0C4111F265A36C39FAD29728523EB5ECE11729ABA3080B7`）
  仍收集核心测试 `2480`、QGC custom `66/66`，`software_status=pass`、
  `hardware_status=offline_pending`，并记录 field report 已验证但 `gate_passed=false`、
  `route_checks_passed=false`、`gate_overridden=false`。
- 这次只更新现场 provenance，飞机接通后可直接用同一端点、协议检查和 route 要求重跑；
  SIM、external runtime/live-AI、HIL/REAL 和最终 `v0.3.0` 状态不被伪造改变。

### v0.3.0-pre.8 current E7 release-gate refresh (2026-09-02)

- 重新生成当前 pre.8 E7 发布门清单
  `artifacts/evaluation/release-gate-pre.8-current-20260902-v7-independent-source.json`
  （14,068 bytes，SHA-256 `5FCF05088BD73F03231BB8B0FDD49B74F79BC1C2849D2D4292BF98913794A8AB`），
  并通过 `configs/schemas/release-gate-manifest.schema.json` 校验。清单绑定当前
  REPLAY/SIM/HIL/REAL envelope、field-link、三机型 3-D control、SIM safety/matrix、
  combined SIM/QGC/Mission 与 PX4 airframe transaction。
- 生成 report-only 与严格复核：分别为
  `artifacts/evaluation/release-gate-pre.8-current-20260902-v7-independent-source-report.json`
  （40,740 bytes，SHA-256 `AF5DBA49793813E3B1A52F40814CDEB2DFCE1727B246D4DAFC8BE4A89EE8281A`，退出码 `0`）
  和
  `artifacts/evaluation/release-gate-pre.8-current-20260902-v7-independent-source-strict.json`
  （40,740 bytes，SHA-256 `568E8F34073CD31A3096C6A51DECCB70222CC462047DA32E329C7CA12DC0C441`，退出码 `2`）。
  两份报告都保持 `gate_passed=false`、`release_allowed=false`，共 31 条阻断原因；E7
  清单与 goal acceptance 的六源 external provenance 分层记录，避免把 source join 当成
  最终发布门通过。
- 固定接线前可打印的高分辨率 ChArUco 校准板资产
  `configs/calibration/charuco-7x5-40mm-20mm-4200px.png`（4200×3000，88,844 bytes，
  SHA-256 `66AAF16F10689E9EDFF68B4684128E028AD89B09AABC55AB9CDC4D02521B23AC`）。OpenCV
  自检可识别 24 个 corners；真实相机仍必须采集至少 20 个多姿态有效视图并通过完整质量门，
  该资产不提升室外标定或 REAL 状态。

### v0.3.0-pre.8 independent six-source runtime/AI provenance hardening (2026-09-02)

- 将 VTOL、fixed-wing、multirotor 的 Docker/PX4/Gazebo external runtime 与 live-AI 结果拆成
  六个独立 source，并用同一 digest-pinned image
  `px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`
  重新计算绑定。runtime source 的 telemetry/GPS、external process 和 cleanup，AI source 的
  armed heartbeat、`ai_steps=2`、target policy 与 truth-free 边界均通过。
- verifier 现在强制 runtime/AI source 使用六个不同文件；同一路径同时作为 runtime 与 AI
  source（包括跨机型复用）会直接 fail-closed。当前 v2 六源 join 为
  `artifacts/evaluation/simulator-external-runtime-ai-evidence-20260902-final-six-sources-independent-v2.json`
  （5,353 bytes，SHA-256 `79DBB81E4BB85366FFB7114DB2B3E9E6F921E8027133EE64603A680184B10440`），
  独立 `--join` revalidation 为
  `artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260902-final-six-sources-independent-v2.json`
  （558 bytes，SHA-256 `95A4CC58EDE1723E51B0DEBDFFECC261B6150A5374BBA49CD6B2BDEDBFFF783A`）。
  两者均 `source_recomputed=true`、`gate_passed=true`；旧的三源/旧六源记录保留为历史证据。
- 重新执行 `run_goal_acceptance.ps1` 并在未启用凭据 override 的严格模式下绑定该 join，生成
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-external-three-airframes-v7-independent-source-credentials-strict.json`
  （42,100 bytes，SHA-256 `F69398FDC88D14AFA4FF0D182D0F6717108904CEDD2865DC6680CA9AD4EA5EC0`），
  `core_tests_collected=2459`、QGC custom `66/66`、`software_status=pass`，external 与
  deterministic 3-D binding 均通过；goal acceptance 使用的 revalidation 为
  `artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260902-final-six-sources-independent-v2-goal-v7.json`
  （558 bytes，SHA-256 `DD60719E8969FA5F4D62680CBD545E9EF0A6F231CC6AA8FCEBDAAC3CF8D3FBF2`）。
- 严格凭据模式下 `operator_credential_readiness.ready=true`、`gate_overridden=false`；现场
  预检仍原样保留为 `hardware_link_ready=false`。同一快照绑定 22:00 后只读现场预检
  `artifacts/evaluation/field-link-preflight-20260902-after-2200.json`
  （6,692 bytes，SHA-256 `37D68E88AB7433898BE77FED696009564F558274B7824EB0D3164D5FC563200A`）。
  本地 metadata/QGC UDP bind 通过，但 Jetson/GR01/RTSP 与期望 `192.168.144.0/24` route
  仍失败，`hardware_link_ready=false`；失败事实保持绑定，不升级为 REAL。
- `hardware_status=offline_pending`、人工 QGC Fly View、室外 GNSS、真实 Pixhawk/Jetson/相机、
  伺服/失锁、真实检测闭环、HIL/REAL 和最终 `v0.3.0` 发布门继续保持未完成；external
  runtime 不再出现在 `remaining_live_gates` 中。

### v0.3.0-pre.8 external three-airframe runtime/live-AI acceptance (2026-09-02)

- 在台式机 Docker 环境按同一 `px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`
  重新执行 VTOL、fixed-wing、multirotor 三个 scenario；每个 source 的 telemetry、
  GPS quality、arm/disarm、live-AI target policy、truth-free 和 cleanup 均通过。
- 三机型 source 绑定为
  `artifacts/evaluation/simulator-external-runtime-ai-evidence-20260902-final-three-airframes.json`
  （5,365 bytes，SHA-256 `E1CE9408A39F9C4316EB63624DEFC3C2D590302804041A8409B7F65488761AEF`），
  独立 `--join` revalidation 为
  `artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260902-final-three-airframes.json`
  （547 bytes，SHA-256 `B4981FCCE82059D951BCB22EB29EC4A6F13B1E5550895853667675FCF3C1FFC6`）；
  两者均 `source_recomputed=true`、`gate_passed=true`。
- 重新执行 `run_goal_acceptance.ps1` 并强制绑定上述 join，生成
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-external-three-airframes-v2.json`
  （41,770 bytes，SHA-256 `C34FE659F11DDC5AA94CF8B945B9018F68491A6B3FA808DBB00B7DD9C06BC46F`），
  `core_tests_collected=2457`、QGC custom `66/66`、`software_status=pass`，external 和
  deterministic 3-D binding 均通过。
- `run_goal_acceptance.ps1` 的 `remaining_live_gates` 现在按 external binding 动态收敛：
  join 已通过时只留下人工 QGC Fly View 视觉验收，不再重复列出 external runtime。
- 以上仍是 disposable/truth-free software SIM 证据；真实设备、室外 GNSS、Pixhawk/执行器、
  HIL/REAL、人工 Fly View 与最终 `v0.3.0` 发布门继续保持未完成。

### v0.3.0-pre.8 full regression environment completion (2026-09-02)

- 台式机回归环境补齐 CPU `onnxruntime=1.29.0`，并将 `protobuf` 固定为兼容现有
  mediapipe/streamlit 依赖的 `4.25.8`；原先缺少运行时的两条 ONNX/集成测试已实际执行。
- 当前完整回归命令 `pytest --disable-warnings -ra` 结果为 `2449 passed`、退出码 `0`；Ruff、
  compileall、diff-check 和 release-gate Schema 验证保持通过。原始输出日志为
  `artifacts/evaluation/pytest-full-20260902-onnxruntime.log`（2,879 bytes，SHA-256
  `4F5360DEC8E64B9C8B71CDF9BEF3ACEF7FB8C7FE87C7CD4FF7361B4B2C296012`）。该结果属于软件质量证据，
  不升级真实 Jetson/相机/室外 GNSS/Pixhawk、HIL/REAL 或人工 Fly View 发布门。
- `docs/autonomy-architecture.md` 与 `docs/repository-architecture.md` 已将当前 portable
  EXE 指纹与历史构建指纹分开标注，当前版本仍为 `v0.3.0-pre.8`。
- 重新执行 goal-acceptance 后生成当前软件快照
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-onnxruntime.json`
  （31,162 bytes，SHA-256
  `8F4DA9CDC38EF65CF069620FF70871C5806BA3246AD0CEE04CE3A7188488D27D`），其中
  `core_tests_collected=2449`、QGC custom `66`、`software_status=pass`，硬件仍为
  `offline_pending`。
- 随后在未传入 `--AllowMissingOperatorCredentials` 的严格模式下再次运行，生成
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-onnxruntime-credentials.json`
  （31,175 bytes，SHA-256
  `F17D535362B2F18351178A7F9865766A3EAA3D015AC3392C2C437129C0750A52`）。该快照确认
  `operator_credential_readiness.ready=true` 且 `gate_overridden=false`，核心测试 `2449`、
  QGC custom `66`、`software_status=pass`；硬件仍为 `offline_pending`，人工 Fly View
  继续保持 `not_claimed`。

### v0.3.0-pre.8 three-airframe 3-D SIM binding (2026-09-02)

- 新增 `src/multidetect/simulator/three_d_control.py` 独立 verifier/binding：重新打开每份
  3-D evidence，校验场景 manifest、Gazebo world/model、WGS-84、stack-plan canonical
  digest、PX4 launch preflight、QGC manual/AI authority、watchdog/failsafe 和 claim 边界。
- 新增 `scripts/verify_simulator_3d_control_evidence.py` 及 `multidetect-sim-3d-control-verify`
  入口；单文件复核和 `--bind` 三机型绑定都返回机器可读 JSON，绑定要求
  `fixed-wing`、`multirotor`、`vtol` 恰好各一次。
- `release_gate.py` / `release_gate_manifest.py` / release-gate schema 新增
  `simulator_3d_control` hash-bound 子记录；最终 `v0.3.0` 要求该绑定存在且重新复核通过。
- `run_goal_acceptance.ps1` 默认重新生成并绑定三种机型的 3-D source，新增
  `-Simulator3dVtolOutput`、`-Simulator3dFixedWingOutput`、`-Simulator3dMultirotorOutput` 和
  `-RequireSimulator3dControlEvidence` 参数。
- 当前三份 source 均通过 deterministic gate：VTOL 83,212 bytes / SHA-256
  `3733B2E02F4751B5E9262451EC6C7BF73FC5EDEC94E57B6F3F4755A603ADB1F7`，fixed-wing 83,903
  bytes / `336CFABAB3CC88E4419F5FD3744890D4F789F3EAFA1C571D063F98515881C6EE`，multirotor
  83,866 bytes / `2F99FBCDE6457EE634EA65FB5D58988B90628CF38E7A74C83EB3CEA5D470EDE0`。
- 生成的 pre.8 E7 绑定清单为
  `artifacts/evaluation/release-gate-pre.8-current-20260902-3d-bound.json`（13,373 bytes，
  SHA-256 `967F8C5CFE4DBF16EAFF88503BBF18C497BAEA4ECE8190DF2D5602F1D7B8D206`）。聚合仍为
  `gate_passed=false` / `release_allowed=false`；三份 source 的
  `external_runtime_gate_passed=false`、视觉/硬件 `not_claimed` 不会被升级成外部
  PX4/Gazebo、Pixhawk、室外 GNSS、HIL/REAL 或实飞验收。
- 随后用完整 `run_goal_acceptance.ps1` 实际重生成三份 latest source 并完成绑定：
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-3d-binding.json`（41,392 bytes，
  SHA-256 `57F9B416A8E4FF4EC20D395F905A87DAA4290304B930EF1DA448CFBAEE8197E9`）记录
  `core_tests_collected=2456`、QGC custom `66`、`software_status=pass`、
  `hardware_status=offline_pending` 和 `simulator_3d_control_acceptance.gate_passed=true`。
  同批 source 的 E7 绑定清单为
  `artifacts/evaluation/release-gate-pre.8-current-20260902-3d-bound-rerun.json`（13,328 bytes，
  SHA-256 `BA87F55307793069A04BC941E3F62586AE64476EE5BD86A27C1B52AF8CA97187`）；总发布门
  仍关闭。
- 继续执行一次带现场报告绑定的总验收，现场预检
  `artifacts/evaluation/field-link-preflight-20260902-final-continuation.json`（3,713 bytes，
  SHA-256 `476AB14F0E48887A33AA4107D86A6782F13B8A7B4334B95954681F99A3A48E52`）确认当前
  Ethernet `192.168.1.13/24`、Jetson/GR01/RTSP 不可达，`hardware_link_ready=false`。
- 最新总验收为
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-final-continuation.json`
  （41,681 bytes，SHA-256 `6F6388CA393F49FEADC6B95693A0CB7AC93959A2AA9E17236B8BC7DC7DF3AB9C`），
  核心测试 `2456`、QGC custom `66/66`、`software_status=pass`、`hardware_status=offline_pending`，
  三机型 binding `verified=true`/`gate_passed=true`。本批 VTOL/fixed-wing/multirotor source
  指纹分别为 `50B8E18B1A7B7D85CF4D37AF48BD5DB1B51534C57F709B864E2FB8827A1BEF5F`、
  `715A3437101846271A901CC90140DAC9969D459361D7FEE6EF438F75DE945208`、
  `98B1971CC17F9EF3190EB83759329C1BFDDE1EF64F2E2383B9ED4DA036641E19`。
- 三机型 source 又生成独立绑定报告
  `artifacts/evaluation/simulator-3d-control-binding-final-continuation.json`（2,278 bytes，
  SHA-256 `262C659B1860FAA2098AC48B211588A2CE523604A4C45A465E7F530C6F3F0C96`），确认
  `all_airframes`、`records_hash_bound`、`all_gate_passed`、`external_runtime_not_claimed`。
- 现场、外部 PX4/Gazebo、人工 Fly View、室外 GNSS、真实 Pixhawk/Jetson/相机和 HIL/REAL
  仍保持未完成/未声明，版本继续为 `v0.3.0-pre.8`。

### v0.3.0-pre.8 replay-set threshold calibration (2026-09-02)

- 新增 `src/multidetect/replay_set_calibration.py` 与
  `scripts/calibrate_replay_set.py`：直接复用哈希绑定的六分层 replay-set，执行类别级
  confidence threshold 扫描，并输出聚合与 day/night/haze/reflection/smoke-only/no-fire
  分层结果。
- 新增 `stability` 跨层汇总和 `--stability-tolerance`（默认 `0.10`）：报告每个类别的
  推荐层覆盖率、缺失层、阈值 min/max/mean/range 与 `stable_at_tolerance`；缺少正例的
  层保持显式缺失，不折算为零阈值。
- 新增 `verify_replay_set_calibration.py` 与 `verify_replay_set_calibration_report`：重新
  加载 manifest、核对 raw/canonical SHA-256，并重跑报告中编码的校准请求；复核
  `gate_passed=true` 只证明证据未漂移，不会把 `promotion_ready=false` 升级为通过。
- E7 `release-gate` writer/evaluator 新增可选的
  `--replay-set-calibration-verification`：把独立校准复核 artifact 作为
  `replay_set.calibration_verification` 子绑定，读取清单时再次校验 artifact/report/manifest
  SHA-256 并确定性重算；该子门只证明校准报告完整性，不替代 IoU 精度门。
- 校准报告只生成可审阅的调参建议，不会修改模型清单、Jetson 参数或在线阈值；
  `promotion_ready` 仍要求完整 `deployment_recording`、全部预测流和显式独立复核。
- SIM/camera-only 或空正例基线会保留 `promotion_ready=false` 与明确失败原因，继续阻断
  replay-set 精度门，不升级为室外/真实部署证据。
- 当前 SIM/Gazebo report-only 快照为
  `artifacts/replay-sets/sim-gazebo-2026/threshold-calibration-20260902-pre.8-report-only.json`
  （32,703 bytes，SHA-256
  `CA32418F6C253D42800C4DCA4218EA9C0425DB4B87F712C054FA456A0275517D`）。
  对应独立复核快照为
  `artifacts/replay-sets/sim-gazebo-2026/threshold-calibration-20260902-pre.8-verification.json`
  （856 bytes，SHA-256
  `BDAB5475B2683A801B6DB43CA1CB55CDE416493D3128D7020C7AFE8D6446B0F7`）。
- 生成并复核新的 replay-set 绑定 E7 pre-release 清单
  `artifacts/evaluation/release-gate-pre.8-current-20260902-replay-set-bound-v1.json`
  （10,680 bytes，SHA-256
  `0661B9DF660613C038AD967D07EF3892325C1A44D4AB7E1E13CE9BB19A671762`）及固定时间
  report-only 输出
  `artifacts/evaluation/release-gate-pre.8-current-20260902-replay-set-bound-v1-report.json`
  （21,523 bytes，SHA-256
  `045E7160950B123EB559FC8C89C250147D3F0AB173648BCA30B56C5A199A91FB`）。复核结果中
  `replay_set.present=true`、六层 coverage 和 raw/canonical hash 均已绑定，但
  `replay_set.gate_passed=false`、`release_allowed=false` 仍由 SIM 合成部署域门和其它
  未完成的 HIL/REAL/人工验收明确阻断。
- 新增校准复核绑定的 pre.8 诊断清单
  `artifacts/evaluation/release-gate-pre.8-current-20260902-replay-set-bound-v2-calibration.json`
  （11,490 bytes，SHA-256
  `04ACDA079F66B3ECCB387124FA66BDE3DE8959A43328BBC8D8618C67DC908A92`）及固定时间报告
  `artifacts/evaluation/release-gate-pre.8-current-20260902-replay-set-bound-v2-calibration-report.json`
  （23,554 bytes，SHA-256
  `E77388B10F71E13245AEE15CFEC04C3D8AEEAB9A1488C019FAD22DC3F82046DA`）；其中
  `replay_set.calibration_verification.passed=true`，而 `report_promotion_ready=false`
  和 `replay_set.gate_passed=false` 仍保持 fail-closed。

### v0.3.0-pre.8 current acceptance evidence refresh (2026-09-02)

- 重新按当前 `configs/release-parity.json` 生成三机型 external runtime/live-AI join：
  `artifacts/evaluation/simulator-external-runtime-ai-evidence-20260902-manual-watchdog-v2.json`
  （5,407 bytes，SHA-256
  `6701803509C4DE7322B553034B6ABAC72D4C04836C6289C85B51C61F88652770`）。独立 `--join`
  revalidation 为
  `artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260902-manual-watchdog-v2.json`
  （544 bytes，SHA-256
  `A1C653C53E71F22186D2216858628575C3020200E2949C38653D8996510078D0`），
  `source_recomputed=true`、`gate_passed=true`；修复了旧 join 引用已更新 safety parity
  时造成的摘要漂移。
- 重新执行完整 `run_goal_acceptance.ps1`：
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-manual-watchdog-v2.json`
  （39,946 bytes，SHA-256
  `13467799B7924BED99E72DAFCEA214FBA4FFBE96C246C160B4C37772DDCE6929`）。核心测试收集
  `2431`、QGC custom `66/66`、ruff、compileall、QGC protocol self-test 和各软件验收子门
  全部通过，`software_status=pass`；现场仍为 `hardware_status=offline_pending`。
- 用同一批当前 source 重新生成 E7 清单
  `artifacts/evaluation/release-gate-pre.8-current-20260902-manual-watchdog-v2.json`
  （10,481 bytes，SHA-256
  `AAC4FDA23C6F47CC23D856F8B5C06F59E164B4C3222538B9B0A5D2F847D05572`）及固定时间
  report-only 输出
  `artifacts/evaluation/release-gate-pre.8-current-20260902-manual-watchdog-v2-report.json`
  （21,863 bytes，SHA-256
  `040EC28064A4C4BCA17BA7273DB1E929B661E029A7786F7CC62D8B9846AEB848`）。五项 parity 和
  SIM safety/scenario/Mission 子门均为 `true`，但 E7 仍按定义保持关闭，原因是 REPLAY 正
  样本与独立复核、现场网段/设备、人工 Fly View、HIL/REAL 和 prerelease channel 尚未完成。

### v0.3.0-pre.8 field-link PowerShell fail-fast forwarding (2026-09-02)

- Windows `scripts/run_field_link_preflight.ps1` 现在暴露 `-StopOnFailure`，并将它原样
  转发给 Python bounded-watch 实现；现场首个失败样本出现时可以立即结束等待。
- partial watch 仍由同一 verifier fail-closed：报告保留 `samples_requested`、
  `samples_collected`、`first_failure_index` 和 `hardware_link_ready=false`，不会把不完整
  的现场窗口升级为设备就绪证据。
- 新增 wrapper 回归断言，确认该开关不会修改网卡/IP/路由，只影响只读预检的采样停止策略。

### v0.3.0-pre.8 partial-watch route verifier closure (2026-09-02)

- 修复现场 `-StopOnFailure` 在启用 `-ExpectedSourceCidr` 时的 verifier 矛盾：未收齐
  请求窗口的 watch 允许聚合 `route_checks_passed=null`，并把它解释为“剩余样本尚未
  观察”，而不是伪造一个布尔结果。
- 完整 watch 和 one-shot 仍严格要求路由结果为布尔值；partial watch 若提交布尔值仍
  会因聚合结果与采样数量不一致而 fail-closed。
- 新增结构回归与 Windows wrapper 冒烟：1/3 样本首失败即停止，`--allow-unready`
  verifier 返回 0，报告仍明确 `hardware_link_ready=false`。

### v0.3.0-pre.8 E7 field-link binding refresh (2026-09-02)

- 用最新 fail-fast 现场报告重新生成当前 E7 清单
  `artifacts/evaluation/release-gate-pre.8-current-20260902-field-link-watch-v3.json`
  （10,336 bytes，SHA-256
  `E498B45064682280DE3F82A9BA479CD5B4BBD261BABB56D16B45CCB00638AFBA`）。清单绑定的
  `real_field_link` 保留 `route_checks_requested=true`、`route_checks_passed=null`，并锁定
  报告 SHA/字节数；`null` 只表示 partial watch 尚未收齐窗口，不表示路由通过。
- 固定时间 report-only 复核为
  `artifacts/evaluation/release-gate-pre.8-current-20260902-field-link-watch-v3-report.json`
  （20,260 bytes，SHA-256
  `5CF73EA62905AD41F3FABEC0C904A5C558681AB978589051C9512BAF15EE9254`）。复核已移除旧的
  “route_checks_passed 必须为 boolean”结构误报，仅保留现场 `hardware_link_ready=false`
  和对应 E7 阻断；`gate_passed=false`、`release_allowed=false` 仍是正确结果。
- 该绑定不会把当前 `192.168.1.13/24` 桌面网段、Jetson/GR01/RTSP 超时或未完成样本窗口
  升级为 REAL/GNSS 证据；接通飞机后必须重新采满完整 watch，并用真实 route CIDR 重新绑定。

### v0.3.0-pre.8 interactive MANUAL_CONTROL freshness gate (2026-09-02)

- 为 QGC 原生 Fly View 的 `MANUAL_CONTROL` 输入增加可选 freshness watchdog。
  interactive orchestrator、standalone `control-gateway` 和 `interactive-router` 默认阈值为
  `0.5 s`；headless/deterministic 调用保持 `None`，也可通过
  `--manual-watchdog-seconds` 显式覆盖。
- 手动 authority 在阈值内没有新帧时，唯一的
  `SimulatorMavlinkControlAdapter` 原子进入 `FAILSAFE`，发送一次
  `source=manual_watchdog` 的 wire-mode 正确 neutral；重复 tick 不重复发送。释放 manual
  authority 或 AI 接管会清理旧 lease，关闭路径同样保持单次 neutral。
- `authority`、`interactive-router`/`control-gateway` 与 `job` evidence 新增阈值、
  `manual_watchdog_neutral_count`、`manual_control_active` 字段；`mavlink_control.py` 已纳入
  canonical safety parity，当前 `safety_policy_hash` 为
  `f781b90021938e5b9d45056eb48c6367599401ee65f2f8f0279f0aaa03bab2d3`。
- 新增 adapter、gateway、orchestrator 和 CLI 回归；软件门继续保持可复现，真实 Jetson/
  相机/Pixhawk、室外 GNSS、HIL/REAL、人工 Fly View 与最终 `v0.3.0` 发布门仍待现场验收。
- 交互路径重新生成三空架构的验收快照
  `artifacts/evaluation/simulator-interactive-acceptance-20260902-manual-watchdog-v1.json`
  （113,245 bytes，SHA-256
  `BE9E290B1D04BF191B2DB8B372739B8F62E4FFD0F4A46EF94F790EA14275DC2F`），三种机型
  `gate_passed=true`，并确认 router/adapter 均绑定 `manual_watchdog_s=0.5`。
- 同一配置重新生成 VTOL 3-D 控制快照
  `artifacts/evaluation/simulator-3d-control-acceptance-20260902-manual-watchdog-v1.json`
  （83,149 bytes，SHA-256
  `0C6E2A418FB808EE6B8E33BD6CFD940826678543FD4C7A6F20974CBE0D2EC81E`），其
  deterministic protocol/asset gate 与嵌入的 native QGC interactive 子门均通过；外部
  PX4/Gazebo、桌面视觉与硬件状态仍按门定义保持未声明。
- 用新的 `safety_policy_hash` 和 17:30 现场报告重新生成 E7 清单
  `artifacts/evaluation/release-gate-pre.8-current-20260902-manual-watchdog-v1.json`
  （SHA-256 `55D5D65E7E4574D11BA77EF4DA1AD92EBA056CCB32ACF7242B7E1560D84381E6`）及固定时间 report-only 报告
  `artifacts/evaluation/release-gate-pre.8-current-20260902-manual-watchdog-v1-report.json`
  （23,063 bytes，SHA-256
  `28F678330DECF412CECD30BF3073060A2113BA648F8C25D2BD79DA29E49DF4AA`）。五项 parity
  均为 `true`，但 REPLAY 精度、现场链路、人机视觉和 HIL/REAL 仍按门定义保持关闭。

### v0.3.0-pre.8 replay accuracy gate hardening (2026-09-02)

- Added a fail-closed guard for replay reports whose aggregate precision or
  recall is undefined. Empty/all-negative SIM baselines remain useful for
  transport and false-alarm regression, but cannot become deployment accuracy
  evidence without positive truth and threshold-qualified prediction samples.
- Added a regression test for a complete, explicitly reviewed, empty replay set
  and documented the exact `aggregate metrics precision/recall are undefined`
  failure reason.
- Refreshed the software acceptance snapshot at
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-replay-guard-v8.json`
  (39,850 bytes, SHA-256
  `C0CF30A37C028B3E526C0068EE10DC5A8DBDAE276F2EBCD3C6FC7B9BBD849391`), with
  2,423 core tests and QGC custom `66/66`; hardware remains `offline_pending`.
- Bound the guarded replay evidence to
  `artifacts/evaluation/release-gate-pre.8-current-20260902-replay-guard-v8.json`
  (SHA-256
  `8B62437E85583F244047F4CFA18D34C724F5E0C4C51DA39A66C01096EF1CE6E9`) and its
  fixed-time report-only output
  `artifacts/evaluation/release-gate-pre.8-current-20260902-replay-guard-v8-report.json`
  (SHA-256
  `1E5B1FAFA11D8058DC0C21A3FF0435CD92E7D30CF1BCDEE40C7D79DBBD8E5823`). The
  verifier returned `0` in report-only mode and `2` in strict mode; the release
  gate remains closed until positive deployment truth and independent review exist.

### v0.3.0-pre.8 external runtime/AI evidence binding (2026-09-02)

- 重新校验 VTOL、fixed-wing、multirotor 六份 runtime/AI source，并将当前 join
  `artifacts/evaluation/simulator-external-runtime-ai-evidence-20260902-pre.8-continue-v2.json`
  （5,407 bytes，SHA-256 `1F834FC5A86409E3BD338B349C9D3FC188D4FEDDEBB725E1CCBAB1DFB99722B2`）
  绑定到最新 goal acceptance。
- `--join` revalidation 输出为
  `artifacts/evaluation/simulator-external-runtime-ai-revalidation-combined-qgc-v7.json`
  （543 bytes，SHA-256 `206E667E1996C4E530BF5988E85F396E1824A407E565B97D445963AF4F9A49A0`），
  `source_recomputed=true`、`gate_passed=true`。
- 新总验收为
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-combined-qgc-v7.json`
  （39,850 bytes，SHA-256 `082E33BEA216460271BDB44435AC6281A1F5E3F3C3F0FEC2BDDD03B9E95CD2D1`），
  核心测试 `2421`、QGC custom `66/66`、`software_status=pass`，并写入
  `simulator_external_evidence.provided/verified/source_recomputed/gate_passed=true`。
  该证据仍是 truth-free、无物理输出的软件 SIM 子门，硬件继续 `offline_pending`。
- portable `MultiDetectGCS.exe --offline --simple-boot-test` 退出码 `0`；日志摘要保存在
  `artifacts/evaluation/qgc-simple-boot-20260902-combined-qgc-v7.log`。

### v0.3.0-pre.8 field-link read-only re-probe (2026-09-02)

- 台式机迁移后重新执行 2 个样本、启用 `ProtocolChecks` 的只读现场复探：Ethernet=`192.168.1.13/24`，
  期望 `192.168.144.0/24` route 未命中，Jetson SSH、GR01 MAVLink 与相机 RTSP 均超时，
  `hardware_link_ready=false`。报告为
  `artifacts/evaluation/field-link-preflight-20260902-154000-continue.json`（11,758 bytes，
  SHA-256 `1F9A30F4407C1B31435F57155D675CB7BD09EC781BB3734E848D81BB4904F4FC`）。
- 现场报告已绑定到 E7 `artifacts/evaluation/release-gate-pre.8-current-20260902-combined-qgc-v7.json`
  （10,895 bytes，SHA-256 `1E16B731CBF6E9252986210266785CE68A505E380065978699A7A421B00A58D2`）和
  goal acceptance `artifacts/evaluation/multidetect-goal-acceptance-20260902-combined-qgc-v7.json`
  （39,850 bytes，SHA-256 `082E33BEA216460271BDB44435AC6281A1F5E3F3C3F0FEC2BDDD03B9E95CD2D1`）。
  `software_status=pass`、核心测试 `2421`、QGC custom `66/66`；硬件继续为
  `offline_pending`，没有把局域网候选主机当成飞控或相机。
- E7 固定时间 `2026-09-02T07:50:00Z` report-only/strict 均保持 `gate_passed=false`、
  `release_allowed=false`（报告 SHA-256 `A16160554D6A0DFD02414EC67F15E635835B89C7F80D5F0CCF1950C479D04035`，
  23,016 bytes，退出码 `0`/`2`）。
- 17:30 又执行一次同样的两样本只读复探，最新报告为
  `artifacts/evaluation/field-link-preflight-20260902-173000-manual-watchdog.json`
  （11,757 bytes，SHA-256
  `5ABE546780E703740B8C1A3866F38691ADF7F8CCDF7FB60E2C8C6D7DD3A4E428`）。结构、路由与
  watch 一致性 verifier 通过，但两个样本均未通过：本机 `192.168.1.13` 不在期望的
  `192.168.144.0/24`，Jetson/GR01/RTSP 仍不可达，因此 `hardware_link_ready=false`。

### v0.3.0-pre.8 combined SIM/QGC Mission full-window rerun (2026-09-02)

- 用 `Frames=900`、`RouterDurationSeconds=420`、`MissionTimeoutSeconds=240`、
  `MissionSetupAllowanceSeconds=120` 和固定翼停留 `12 s` 重新执行同一条 PX4/Gazebo/QGC
  联合链路。证据为
  `artifacts/evaluation/combined-sim-qgc-20260902T071623Z-66836/combined-evidence.json`
  （250,024 bytes，SHA-256
  `38E179075340A41CF040406DC89F7CF106EC1282374AD3B6376329C17CB6191B`），顶层
  `gate_passed=true`，独立 verifier 为同目录的
  `combined-evidence-verification.json`（2,567 bytes，SHA-256
  `DC06C1CC646EE37C94FC644AD59DF49074A688ECB361F69A206E475354430419`），退出码 `0`。
- 完整 VTOL Mission 在 `117.938 s` 内观察到 `armed`、多旋翼起飞、固定翼转换、reached 序列
  `0,1,2,3,4,5,6`（current telemetry 为 `0,2,3,4,5,6`，transition `1` 由 transaction/journal
  确认）、返程降落和最终 `disarmed`；最大相对高度 `25.423 m`、离家最远
  `127.632 m`。13 条 Mission transaction journal 记录、哈希链、动态 loopback 端口、
  Docker 清理和所需 Gazebo camera/CameraInfo/NavSat topics 均通过。
- 同一 QGC 进程的原生 `videoContent` fixed-wing QQuickItem 抓帧和 AUTO Mission 窗口证据
  通过：`qgc-native-video-item-fixed-wing.png`、`qgc-native-video-grab-proof.json`、
  `qgc-live-video-surface-proof.json` 与 `qgc-fly-view-auto-mission.png` 均在运行目录生成。
  这证明的是 software-only SIM/QGC/Mission 子门，不是人工视觉、真实 Pixhawk、Jetson、
  室外 GNSS、HIL/REAL 或实飞。
- 为该通过的 source 生成了 pre.8 SIM envelope
  `artifacts/evaluation/release-mode-sim-20260902-pre.8-combined-qgc-v5.json`（579 bytes，
  SHA-256 `7434FE3E3C47A947B58C53F49AE1E083CEFBF871F3B03A06A0417E21EFBE6493`），并写入新的
  E7 清单 `artifacts/evaluation/release-gate-pre.8-current-20260902-combined-qgc-v5.json`
  （10,879 bytes，SHA-256
  `1340AC8CCCB62CB02F4F82B20DBAEA8C570353C10C18D719D951A430DD46402A`）。固定时间
  `2026-09-02T07:30:00Z` 的 report-only/strict 报告均为 22,968 bytes、SHA-256
  `E8051A6C7BD68DD793ACAB6B77415BF2D44915C5474952D23F95476CFE18C847`，退出码 `0`/`2`；
  REPLAY 精度、现场链路、HIL/REAL、人工视觉和 prerelease channel 仍保持阻断，最终版本
  继续为 `v0.3.0-pre.8`。
- 在同一输入上完成独立 goal acceptance 续跑：
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-combined-qgc-v5.json`（39,499
  bytes，SHA-256 `75340E0AACCFBF41538DA7B024C9484BA757090862867E92D6CDC4C62D10B169`）。
  核心测试收集 `2421`、QGC custom `66/66`、ruff/compileall/diff-check 和 QGC protocol
  self-test 均通过，`software_status=pass`；绑定的现场报告仍为
  `hardware_link_ready=false`、`hardware_status=offline_pending`，剩余现场标定、真实
  DET→TRK→LCK→TGT、Jetson soak、HIL/REAL 与人工视觉门不变。

### v0.3.0-pre.8 3-D SIM control and field-subnet discovery (2026-09-02)

- 重新执行仓库内 VTOL `river_rescue_vtol_001` 的 3-D SIM 控制预检：固定的
  PX4/Gazebo world/model、hash-bound stack plan、原生 QGC Fly View 手动控制、共享 AI
  控制路径和 watchdog/failsafe 均通过确定性门。证据为
  `artifacts/evaluation/simulator-3d-control-acceptance-20260902-continue.json`
  （82,463 bytes，SHA-256
  `56099CE79D65DF7D240BFC633E3479EA848053E4665F62AA0B6157F2509A82AC`）；
  `gate_passed=true`、`deterministic_gate_passed=true`，但 `external_runtime_gate_passed=false`、
  PX4/Gazebo `ready_to_launch=false`，因此没有把 dry-run 预检写成真实 3-D、GPU、Pixhawk 或
  人工视觉验收。
 - 在当前台式机 `Ethernet=192.168.1.13/24` 上做了有界的 `192.168.1.0/24` 服务发现：
   254 个地址中 4 个 SSH 端口响应，MAVLink `5760` 和 RTSP `554` 均为 0 命中；期望的
   `192.168.144.0/24` 路由仍未出现，现场预检 `hardware_link_ready=false`。发现报告为
   `artifacts/evaluation/field-link-preflight-20260902-discovery-continue.json`
   （8,542 bytes，SHA-256
   `6974A7410B5779E71CDDF39C158F2356BF6C67A394E29433C528EEB16062607C`）。该结果只用于
   接线诊断，不自动替换 Jetson/GR01/相机端点，也不改变最终发布门。
- 在相同的显式网段再次启用只读 `ProtocolChecks`：报告
  `artifacts/evaluation/field-link-preflight-20260902-discovery-protocol-continue.json`
  （9,402 bytes，SHA-256
  `3D8CBBF0E6F2A2B40966BB31694700C394FBFF49050FA57B5DB2A792B63D95CF`）确认 4 个 SSH
  banner，但 MAVLink `5760`、RTSP `554` 均 0 命中，期望 route 与显式三设备端点均未就绪。
  该报告保持 `protocol_checks_passed=false`、`hardware_link_ready=false`，只作未绑定
  的现场诊断 provenance，不会把普通 SSH 主机提升为 Jetson、Pixhawk 或相机。
- 使用项目 venv 重跑 `pytest -q`，全量回归退出码为 `0`；现场捕获/GNSS sidecar、QGC
  custom `66/66`、桥接首次 armed heartbeat、Docker AI/external evidence 和三机型
  scenario matrix verifier 均保持通过。真实 Jetson、室外 GNSS、Pixhawk、HIL/REAL、
  人工 Fly View 与最终 `v0.3.0` 发布门继续保持 pending。
- 将本次现场发现报告绑定进完整 goal acceptance：
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-discovery-continue-pre8.json`
  （31,413 bytes，SHA-256
  `83B90A14ADFCDEF7456F87B1F3357D25EB17D95830FB2570F75E696050E8D099`）。快照记录
  `software_status=pass`、`hardware_status=offline_pending`、核心测试 `2421`、QGC custom
  `66`，现场报告本身已通过结构/哈希复核但 `hardware_link_ready=false`；它没有把当前
  局域网 SSH 候选机或 dry-run SIM 当成飞控、GNSS、相机或实飞证据。
- goal acceptance 重跑刷新了 PX4 Mission artifact 的 SHA 后，重新装配了对应 E7 清单
  `artifacts/evaluation/release-gate-pre.8-current-20260902-discovery-continue.json`
  （10,855 bytes，SHA-256
  `372EA72BAB5B116A394F5168BF46FFE61827A1BFA4B3B934C2DFF8DA9BF12BB8`）。固定时间
  `2026-09-02T06:50:00Z` 的 report-only/strict 报告均为 22,905 bytes、SHA-256
  `1020954A94282AB4C3A8399A50DC3F74664C1262E610C921AB8E29ACD9EEA3F8`，退出码 `0`/`2`，
  `px4_airframe_transaction`、SIM safety、三机型矩阵和 combined SIM 均通过；整体
  `gate_passed=false`、`release_allowed=false`，31 条阻断原因仍如实保留。

### v0.3.0-pre.8 direct-range fail-closed safety (2026-09-02)

- `MultiModalRangingEngine.solve_direct()` 现在拒绝非有限或负的评估时间、重复的直接测距
  来源，以及不具备最低向下 NED 分量的水平/向上目标射线；这些输入返回明确无效原因，
  不发布伪造的地面交会距离。该路径继续保持 `DEGRADED` 语义，并与带垂向参考的测距门
  共用目标射线安全约束。
- 新增 `tests/test_multimodal_ranging.py` 回归，覆盖重复来源、非法时间和不安全射线；
  总验收快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-direct-range-safety-pre8.json`
  （39,808 bytes，SHA-256
  `CEEBFAF6B0D0006C64E337A98FB9E30D153FCB1127C4B8A9CE38D0AFCFAC295`），记录核心测试
  `2421`、QGC custom `66/66`、`software_status=pass` 与
  `hardware_status=offline_pending`。真实设备、室外 GNSS、HIL/REAL、REPLAY 精度和人工
  Fly View 门仍保持未完成，版本不提升为最终 `v0.3.0`。
- 重新装配 E7 清单
  `artifacts/evaluation/release-gate-pre.8-current-20260902-direct-range-safety.json`
  （10,870 bytes，SHA-256
  `87B88E5FAF2AB25435CD74272B4F9EB9F52D5F24FFC9D57F51523D79A11E7A6E`），绑定总验收重跑后
  的当前三机型 PX4 Mission artifact（SHA-256
  `99AFC6FEA5BA0CD19F1FA6DB23570C316FE23F0F92002BEF053E4C3C08B3F0A9`）。固定时间
  `2026-09-02T06:25:00Z` 的 report-only/strict 输出均为 22,936 bytes、SHA-256
  `165BF83E476FE9F9CA79186950EAD49FF1E9EAFEE405E275D71AD5A1F35FA60C`，退出 `0`/`2`，
  31 条阻断原因和 `release_allowed=false` 保持不变。

### v0.3.0-pre.8 video-to-raw-replay recording (2026-09-02)

- 新增 `src/multidetect/video_replay_recording.py` 与
  `scripts/create_video_replay_recording.py`：将录制视频逐帧解码为 PNG，并生成经过
  `RawSensorReplayRecording` 二次加载校验的 `events.jsonl`、`recording.manifest.json` 和
  `recording-summary.json`。源视频、事件流、每一帧图像与 recording identity 均带 SHA-256，
  `--frame-stride`/`--frame-limit`/`--fps` 用于可重复抽样和缺失 FPS 的显式补参。
- 转换器明确写入 `camera_only_unknown_navigation`：`position_healthy=false`、GPS 未绑定、
  无真值/预测、部署域精度与发布门均为 `false`。它只补齐视频到同一 raw replay/evaluator
  的接线，不把普通录像升级为室外 GNSS、目标落图、HIL/REAL 或最终版本证据。
- 新增 `tests/test_video_replay_recording.py`，覆盖哈希篡改 fail-closed、FPS override、帧抽样、
  失败清理、脚本 `--help` 和真实 OpenCV 容器烟测；`docs/replay-set-manifest.md`、
  `docs/repository-architecture.md`、`README.md` 已补充目录约定与现场接入命令。
- 新增 `src/multidetect/replay_telemetry_join.py` 与
  `scripts/join_raw_replay_telemetry.py`：把独立完整 `RawReplayTelemetrySample` JSONL
  sidecar 以 latest-at-or-before、无插值、最大年龄门合并到已验证录像，重建并校验图像/事件/
  recording hash，逐帧重算 GPS 质量和失败原因。joined recording 仍显式保持
  `deployment_domain_evidence_complete=false`、`accuracy_validated=false`、`gate_passed=false`；
  如果源 manifest 已有 `scenario_id`/`scenario_manifest_hash`/`scenario_seed`，会在新 manifest
  和 recording identity 中保留同一场景绑定；没有源绑定时不生成伪 scenario。新增回归覆盖
  scenario binding 保留以及 stale/future/duplicate/malformed sidecar、哈希验证和 CLI 帮助。
- 新增 `src/multidetect/gnss_capture_sidecar.py`、
  `scripts/gnss_capture_to_replay_telemetry.py` 与
  `configs/schemas/raw-replay-telemetry-sidecar.schema.json`：校验只读 GNSS capture 报告，
  仅将 `GPS_RAW_INT/GPS2_RAW` 归一化为完整 telemetry sidecar；显式 origin/offset 用于和分开
  启动的录像对齐，未知姿态/AGL/围栏/任务/执行器字段不被猜测，`GLOBAL_POSITION_INT` 不会
  旁路 raw GPS 质量门。新增回归覆盖无 raw GPS、篡改报告、负时间映射、原子写入和 schema/CLI。
- `scripts/run_field_link_preflight.ps1` 现在显式暴露并转发 Jetson、GR01/Pixhawk、相机的
  host 与 `22/5760/554/14580/14581` 端口参数，迁移到新电脑或新网段时不再只能使用脚本
  内置端点；发现仍是有界诊断，不会自动替换端点或修改网络。单样本转发回归报告为
  `artifacts/evaluation/field-link-preflight-20260902-wrapper-explicit-after-acceptance.json`
  （3,712 bytes，SHA-256
  `6A5689AF656F6FB51DCD781A7110F296F74A4E38EA1A3492CA47FB3B251CD326`），严格 exit `1`
  如实保留 `hardware_link_ready=false`。

### v0.3.0-pre.8 complete goal-acceptance continuation (2026-09-02)

- 用当前 `pre.8` portable EXE、v4 combined SIM、三机型 external live-AI join 和严格现场
  preflight 重跑完整 goal acceptance，快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-continue-pre8.json`
  （39,771 bytes，SHA-256
  `CEC975A754E7F4F918C50F76F92EB46D0B85D7F41645FE35BDC8F9F5E75CF7CC`）。项目测试收集
  `2418`、QGC custom `66/66`、`ruff`、`compileall`、`git diff --check`、QGC protocol
  self-test 均通过；SIM combined 与 external live-AI join 均复核为 `gate_passed=true`。
- 快照如实保留现场 `field_link_ready=false`、`hardware_status=offline_pending`，因为
  Jetson/GR01/相机端点超时且当前网卡为 `192.168.1.13`；REPLAY 精度、室外 GNSS、HIL/REAL、
  人工 Fly View 和最终 release channel 仍未完成，继续使用 `v0.3.0-pre.8`，不发布最终
  `v0.3.0`。开发机已存在的 operator/MAVLink key 只作为环境自检，不代替设备或实飞证据。
- 同一批 mode evidence 重新生成 E7 清单
  `artifacts/evaluation/release-gate-pre.8-current-20260902-continue-pre8.json`
  （10,844 bytes，SHA-256
  `8470F5AE136154D264D273D1E7D3446370B7A83CCC3BC0ECB809291E4A37D0AA`）。固定时间
  report-only/strict 复核报告均为 22,921 bytes、SHA-256
  `29DF553263AC38AC944C9209ABB5C56CFCEBD8298FAAB1B843C65BBD8BA776EE`，退出码分别为
  `0`/`2`，`gate_passed=false`、`release_allowed=false` 保持不变。

### v0.3.0-pre.8 endpoint-forwarding acceptance continuation (2026-09-02)

- wrapper 显式端点修复后再次执行总验收，快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-wrapper-endpoint-continue-pre8.json`
  （39,814 bytes，SHA-256
  `3C3F036E62FDA5C7314AE142F0714F502C6E40599013E81EFD1D5ACCA95A8ED5`）。当前 E7 清单为
  `artifacts/evaluation/release-gate-pre.8-current-20260902-wrapper-endpoint-continue-pre8.json`
  （10,870 bytes，SHA-256
  `8FE2FAC78FFAC69140C3693CA902B32F8BEEA2CAEABF9456464F75FD96616CE4`）；固定时间报告
  SHA-256 为 `24B8247077C2CBC9DB6E85FA688712FB62D0CDC38E337FD02544A08C62C07C9F`，
  report-only/strict 退出 `0`/`2`，发布门继续关闭。

### v0.3.0-pre.8 desktop live-AI external-SIM continuation v2 (2026-09-02)

- 在同一 pinned PX4/Gazebo digest 上重新执行 VTOL、fixed-wing、multirotor 三机型
  disposable external runtime，并使用 `--execute --exercise-arm --exercise-target` 验证
  live-AI ARM/目标 `TRACK` 路径。三组 runtime 均取得 qualified `GPS_RAW_INT`
  （`fix_type=3`、10 颗卫星）、armed heartbeat、loopback UDP I/O 和 stop/rm 清理；三组
  AI 均记录 `ai_steps=2`、`target_exercise.accepted=true`、`decision.action=track`，输入
  仍为 truth-free synthetic perception，硬件控制保持关闭。
- 当前三组 live-AI source 为：VTOL
  `simulator-docker-ai-acceptance-20260902-pre.8-vtol-continue-v2.json`
  （133,609 bytes，SHA-256
  `284A2C56A0698C0DF537300BAA7CCB0DF6B538FC1EFE401328D3923E210BC362`）；fixed-wing
  `simulator-docker-ai-acceptance-20260902-pre.8-fixed-wing-continue-v2.json`
  （104,297 bytes，SHA-256
  `FE87A9C3DC356D74AFFF2D24ABDF340054224AF74FD34D453017335DC25D539B`）；multirotor
  `simulator-docker-ai-acceptance-20260902-pre.8-multirotor-continue-v2.json`
  （110,065 bytes，SHA-256
  `4BE8C2F263C48FEE6C9B87B246A1BD56DA86A4C4C8FAD48F565CE87DD87DDFF4`）。
- 六份 runtime/AI source 重新 join 为
  `artifacts/evaluation/simulator-external-runtime-ai-evidence-20260902-pre.8-continue-v2.json`
  （5,407 bytes，SHA-256
  `1F834FC5A86409E3BD338B349C9D3FC188D4FEDDEBB725E1CCBAB1DFB99722B2`，
  `gate_passed=true`）；`--join` 二次重算输出为
  `artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260902-pre.8-continue-v2.json`
  （543 bytes，SHA-256
  `9066B558F5F9047A0DE210AFBD5895DD66DEC395BFE38229037DC398CA1829DD`，
  `source_recomputed=true`、`gate_passed=true`）。
- 该 join 已接入新的总验收快照
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-live-ai-continue-v2.json`
  （16,939 bytes，SHA-256
  `BB371E3B1242E991CC6539D64961C7F4321ADCDC1F1539C3B3C39E842A9ECEF9`）。快照记录
  外部证据 `provided/verified/source_recomputed/gate_passed=true`、核心测试 `2400`、
  QGC custom `66/66`、`software_status=pass`；绑定的严格现场报告仍为
  `hardware_link_ready=false`，故 `hardware_status=offline_pending`，没有关闭室外 GNSS、
  真实硬件、HIL/REAL、人工视觉或最终发布门。
- 同一 continuation 还保留 deterministic 3-D 控制资产
  `simulator-3d-control-20260902-continue-v2.json`（82,463 bytes，SHA-256
  `56099CE79D65DF7D240BFC633E3479EA848053E4665F62AA0B6157F2509A82AC`）。其
  `gate_passed=true`、`deterministic_gate_passed=true`，但 external 3-D runtime、视觉和
  硬件字段继续为 `not_claimed`；版本仍为 `v0.3.0-pre.8`。

### v0.3.0-pre.8 full regression parity coverage (2026-09-02)

- 强化 `tests/test_release_parity.py` 的仓库越界测试：symlink 特权不可用时回退到 Windows
  目录 junction（`mklink /J`），仍使用 `Path.resolve()` 验证外部目标被拒绝；不改变生产
  parity 算法或任何发布门语义。
- 台式机全量 Python 回归现在为 `2400` 项全部通过，原先唯一的 symlink 环境跳过已消除；
  `ruff` 与 `compileall` 均退出 `0`。该结果仍只代表源码测试覆盖，不代表现场 GNSS、Pixhawk、
  HIL、REAL 或人工 Fly View 通过。

### v0.3.0-pre.8 combined SIM/QGC/Mission v4 acceptance (2026-09-02)

- 在 QGC 主窗口 `MainWindowHandle` 短暂为零时启用同一 PID 的唯一可见顶层窗口回退，并
  用 `900` 帧、`MissionTimeoutSeconds=240`、动态 loopback 端口重新执行联合验收。
- 新的顶层证据为
  `artifacts/evaluation/combined-sim-qgc-20260902-hwnd-fallback-v4.json`（249,689 bytes，
  SHA-256 `8F90EAB7232A29382451CF99BB5B3CF0E4D44674E9BC4B027C0962120C46D692`），
  `gate_passed=true`。独立 verifier 为
  `artifacts/evaluation/combined-sim-qgc-20260902T030210Z-77524/combined-evidence-verification.json`
  （1,941 bytes，SHA-256
  `8913C015977B1C879E07A76A48E9F9D2AD86E0F2F8784F1D81AC68FAE9C71055`），退出码 `0`。
- 集成 SIM、VTOL attach、VTOL Mission、900/900 RTSP/传感器帧、动态端口、Docker 清理和
  13-record hash-chain Mission journal 均通过。Mission 观察到 `AUTO.MISSION`、armed、
  in-air、MC、FW、landed 和 disarmed；item `0..6` 全部到达，持续 `116.078 s`，最远
  距离 `142.571 m`、相对高度 `26.587 m`。
- QGC 原生 `videoContent` fixed-wing QQuickItem 抓帧通过，
  `native_qquickitem_video_pixels_proved=true`、
  `transport_bound_native_qquickitem_video_surface_proved=true`；工具栏 indicator/drawer
  机器烟测通过。桌面 GPU/BitBlt ROI 诊断仍失败且人工视觉保持 `not_claimed`，没有把截图
  证据升级成 Fly View 人工验收。
- v4 仍是 software-only SIM：无真实 Pixhawk/Jetson/相机/执行器，现场 GNSS、HIL、REAL、
  人工视觉和最终 `v0.3.0` 发布门继续关闭；此前 v2/v3 失败 artifact 原样保留。

### v0.3.0-pre.8 v4 SIM release-mode binding (2026-09-02)

- 为当前 `combined-sim-qgc-20260902-hwnd-fallback-v4.json` 生成 hash-bound SIM envelope：
  `artifacts/evaluation/release-mode-sim-20260902-pre.8-combined-qgc-v4.json`（562 bytes，
  SHA-256 `65B96C6DF3BC66BD77B13D56BE8D5019210B6C52C6638BECA0162980DE9D7CC8`）。
- 独立 verifier 使用 `--require-release-mode` 重新检查 source path/hash，输出
  `artifacts/evaluation/combined-sim-qgc-20260902T030210Z-77524/combined-evidence-verification-v4-bound.json`
  （2,535 bytes，SHA-256 `5BDAB531DD120645BFE5AF4A8D9816A222EE21B458C020F3EECBA48FE7E1D415`），
  `gate_passed=true`。该绑定仍是 software-only SIM provenance，不改变现场 GNSS、HIL/REAL、
  人工视觉或最终 `v0.3.0` 门。

### v0.3.0-pre.8 E7 manifest refresh for v4 (2026-09-02)

- 生成当前联合 SIM/QGC/Mission v4 的统一 pre.8 E7 清单：
  `artifacts/evaluation/release-gate-pre.8-current-20260902-combined-qgc-v4.json`（10,852 bytes，
  SHA-256 `EAACCA3F40A4B9A6734C550EFFC435A0A13B2F1FC4E08FA12FA0C2F613B66D9F`）。清单绑定
  v4 source、SIM envelope、S4 safety、scenario matrix、PX4 airframe transaction、REPLAY
  accuracy provenance 和严格现场链路报告；SIM/S4/scenario/PX4 子门均为真。
- 固定时间 report-only 与 strict 复核分别写入
  `artifacts/evaluation/release-gate-pre.8-current-20260902-combined-qgc-v4-report.json` 和
  `...-strict.json`，两者均 22,896 bytes、SHA-256
  `F7AEC221A979F03D0AED6FC3B5E983B6458A84CE2C89FCBCB9439546CFC22F19`；report-only 退出 `0`，
  strict 退出 `2`，`gate_passed=false`/`release_allowed=false` 保持不变。
- 报告继续明确 REPLAY 精度/部署覆盖、现场 `hardware_link_ready=false`、四模式
  `not_claimed`、prerelease channel 和最终 release surfaces 是阻断项；该刷新不把 software-only
  SIM 或诊断报告升级成室外 GNSS、HIL、REAL 或人工 Fly View 验收。

### v0.3.0-pre.8 strict field-link recheck (2026-09-02)

- 在当前台式机 Ethernet `192.168.1.13/24` 上重新执行两样本只读 field-link watch，报告为
  `artifacts/evaluation/field-link-preflight-20260902-continue.json`（11,755 bytes，
  SHA-256 `B779A8D4CCD3FAD11DEDDF03E9E691E0C53552E82381440490814058E14850EB`）。两次
  Jetson/GR01/RTSP、协议和期望 `192.168.144.0/24` route 均失败，本机 UDP `14580/14581`
  可绑定，`hardware_link_ready=false`。
- 额外保存严格单样本 `artifacts/evaluation/field-link-preflight-20260902-continue-strict.json`
  （4,401 bytes，SHA-256 `2D1F71C74B7B5F13516C29D3C8BF900A3B3F3B54D8EDAB9E0463595749B5D1F6`）。
  `-RequireReady` 下 wrapper/verifier 均退出 `1`；诊断模式的结构校验 `0` 不被解释为硬件
  通过，REAL、室外 GNSS、HIL 和最终 `v0.3.0` 门继续关闭。

### v0.3.0-pre.8 combined SIM evidence serialization hardening (2026-09-02)

- `scripts/run_px4_gazebo_qgc_combined_sim_acceptance.ps1` 现在先把聚合证据投影为
  有界 JSON-safe 图，再交给 Windows PowerShell 的 `ConvertTo-Json`：包含活动引用环检测、
  `4096` 项集合上限和显式截断标记。各子验收的原始日志、截图和 JSON 路径仍单独保留，
  因此不会因为 PowerShell 混合对象图或历史诊断对象导致聚合器内存失控。
- 进一步收口 Windows PowerShell 5.1 兼容性：`Get-Content -Raw` 产生的
  `PSPath`/`PSProvider`/`ReadCount` 等 ETS 扩展属性会先通过
  `[string]::Copy()` 脱离；文件 SHA-256 改用带 `FileShare.ReadWrite` 的 .NET 流实现，
  不依赖 Utility 模块的 `Get-FileHash` 命令自动加载。每个顶层字段最多 `20,000` 个投影节点，
  字段独立序列化；顶层 `$null` 明确写成 JSON `null`，避免产生无效的 `"key":`。
- 顶层 envelope 改用 `Write-Utf8NoBomText` 显式写入 BOM-free UTF-8，绕过 Windows
  PowerShell 5.1 `Set-Content -Encoding utf8` 的 BOM 默认值；新证据可被严格 JSON 解析器直接
  读取，历史文件仍按原始字节和 SHA-256 保留并使用 BOM-aware reader 复核。
- QGC 窗口屏障在 `MainWindowHandle` 短暂回零时仅接受同一 PID 的唯一可见、非最小化、非
  cloaked 顶层窗口候选；零候选或多候选继续 fail-closed，避免把其他窗口或猜测坐标写成视觉证据。
  回退事件只进入 stage-progress 诊断旁路，不参与控制路径或 gate。
- 新增 `combined-stage-progress.jsonl` 与
  `combined-evidence.json.serialization-progress.jsonl` 两个只读诊断旁路，记录哈希、
  清理、证据对象和逐字段投影进度；它们不参与任何控制路径或发布门，只用于在桌面会话中
  快速定位长时间运行的验收阶段。
- 台式机最新缩短帧数复核写出
  `artifacts/evaluation/combined-sim-qgc-20260902T022832Z-50960/combined-evidence.json`
  （31,347 bytes，SHA-256
  `EC3B88DC55CDCB460CEF80ACA3D4C672C71AA12A608F098F4F11DE22CEBF8756`），字段数为 26，
  `serialization.mode=per_top_level_field`，通过 BOM-aware reader 可重载（原始历史文件保留
  BOM）。该次 run 的门禁仍为
  `gate_passed=false`，失败原因为当前桌面会话在后续阶段未能取得 QGC 主 HWND；初始窗口捕获、
  真实 QGC 日志和容器清理记录均保留，`operator_visual_acceptance=not_claimed`。
- 三次当前台式机重跑均成功写出顶层证据：
  `artifacts/evaluation/combined-sim-qgc-20260902-bounded-evidence.json`（72,135 bytes，
  SHA-256 `9FDFD6526900078AA755282545CC24E7CE836155B60EBFD4E3D8C1C3971A42BC`）、
  `...-r2.json`（70,928 bytes，SHA-256
  `55C36F6E52F59592D073DC70D9942571F48BD4178ACD7F43B18B4429B18C4382`）和
  `...-r3.json`（71,440 bytes，SHA-256
  `9D065FA5DF2CAE3A30E643BB1354E7ACAB5DED7DF1D53E4496343B568690FD69`）。
- 当前三次 run 都按门禁语义记录为 `gate_passed=false`：QGC 原生 toolbar 机器视觉步骤的
  请求中心 `(1960,432)` 在本桌面 PER_MONITOR_V2 会话中实际读回 `(1960,480)`，所以
  drawer 未被点击且 `operator_visual_acceptance=not_claimed`。这项诊断不把坐标猜测写成
  视觉通过；接入可控桌面会话后应重新运行同一脚本。
- 新增静态回归覆盖 JSON-safe 投影、引用环和最终写入路径；PowerShell parser、脚本契约、
  desktop capture 与 native video grab 测试均保持通过。该修复不改变 `v0.3.0-pre.8` 版本，
  也不关闭室外 GNSS、REAL/HIL、真实硬件或人工 Fly View 发布门。

### v0.3.0-pre.8 field-link discovery wrapper evidence (2026-09-02)

- 用 `scripts/run_field_link_preflight.ps1` 对当前台式机 `192.168.1.0/24` 做了一次
  有界、只读的服务发现；扫描 254 台主机，命中 4 个通用 SSH banner，未发现
  `5760/MAVLink` 或 `554/RTSP`。证据为
  `artifacts/evaluation/field-link-preflight-wrapper-discovery-lan-20260902-r1.json`
  （SHA-256 `44E30678D5CB354A54FDA8EF4FBFC11376DE289274384FC828BD7B26918EF52C`）。
- 发现结果继续保持诊断语义：不自动改写 Jetson/GR01/相机地址，不替换端点，不提升
  `hardware_link_ready`；真实飞机接通后仍须使用显式端点和 `-ProtocolChecks -RequireRoute -RequireReady`
  完成现场门禁。

### v0.3.0-pre.8 strict field-link recheck baseline (2026-09-02)

- 保存当前时刻的严格单样本 wrapper 复核：
  `artifacts/evaluation/field-link-preflight-current-20260902-continue.json`
  （4,397 bytes，SHA-256
  `0D3F411D80F01F7D5B4D3CFA63FF1427ABBBFE9A5FC5360BB19E4BDA3B18BA36`）。
  Ethernet 源地址仍为 `192.168.1.13/24`；Jetson/GR01/RTSP 三端点、协议和期望
  `192.168.144.0/24` route 均未通过，本机 UDP 14580/14581 可绑定，严格 wrapper/verifier
  退出码为 `1`，`hardware_link_ready=false`。
- 该文件只作为接线后的时间对照基线，保持 fail-closed，不自动选择发现到的主机，也不关闭
  REAL/HIL、室外 GNSS、人工 Fly View 或最终 `v0.3.0` 发布门。

### v0.3.0-pre.8 field-link PowerShell discovery forwarding (2026-09-02)

- Extended `scripts/run_field_link_preflight.ps1` with bounded, explicit
  `-DiscoverCidr`, `-DiscoverMaxHosts`, `-DiscoverWorkers` and
  `-DiscoverTimeoutSeconds` parameters.  The wrapper forwards discovery to the
  existing Python implementation only when a CIDR is supplied, keeps the
  candidate list diagnostic-only, and never rewrites Jetson/GR01/camera
  endpoints or `hardware_link_ready`.
- Added PowerShell syntax, forwarding-contract and discovery regression checks;
  the wrapper smoke report is
  `artifacts/evaluation/field-link-preflight-wrapper-discovery-20260902.json`
  (5,679 bytes, SHA-256
  `CF9C2B0C8238631C5E8BA662A3EEC63F550BB9284AFE32B9C5201C04DDE8AA04`).  It
  found only generic SSH hosts on the desktop LAN and no MAVLink/RTSP service,
  so `hardware_link_ready=false` remains the correct result.

### v0.3.0-pre.8 field-link v4 evidence rebind (2026-09-02)

- Re-ran the strict, read-only field-link preflight on the desktop with explicit
  `192.168.144.0/24` route and protocol checks.  The new report is
  `artifacts/evaluation/field-link-preflight-20260902-continue-v4.json`
  (4,396 bytes, SHA-256
  `0438D61B311BBA94150CF59F580DBC4A29E6B31999310E33238A1E52F1A3506C`).
  The active Ethernet source remains `192.168.1.13`; Jetson/GR01/RTSP and all
  route probes are unavailable, while local UDP 14580/14581 remain bindable.
  The strict hardware-link result is therefore `false` with exit code `1`.
- Bound that exact report into
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-field-preflight-v4.json`
  (31,777 bytes, SHA-256
  `250824C312DD0CD8326721C0B27B3B10F05F53E9644FA82CC00EC87F668905F7`).
  The aggregate reran `2,397` core tests and QGC custom `66/66`; software is
  `pass`, hardware is `offline_pending`, and the already verified external-SIM/
  live-AI join remains unchanged.
- The join revalidation was refreshed at
  `artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260902-pre.8-continue-v1.json`
  (543 bytes, SHA-256
  `54CB1001F907F8D612411579E863CFA2F8D166C71702A707C29B8105ABBA4589`).
  This keeps the final release fail-closed: physical Ethernet/Jetson/GNSS/
  Pixhawk, outdoor accuracy, HIL/REAL and operator visual gates remain pending.
- Generated a fresh E7 manifest bound to the v4 field report at
  `artifacts/evaluation/release-gate-pre.8-current-20260902-field-preflight-v4.json`
  (11,453 bytes, SHA-256
  `0E8DB97FDA3157967FB567F8EA5DB91088C52BD2D45FB345A5A04A5BA9CC7AE3`).
  With the fixed evaluation timestamp `2026-09-02T00:00:00Z`, both report-only
  and strict outputs are 25,259 bytes with SHA-256
  `55805E813C3DC2FCC8120341C9D5B153EAACFD081AC2F6CB4E5DCFE15A3B3E3`;
  exits are `0` and `2`, respectively, and both remain
  `gate_passed=false`/`release_allowed=false`.

### v0.3.0-pre.8 Docker external SIM + live-AI rerun (2026-09-02)

- Confirmed Docker Desktop server `28.4.0` on the desktop and reran the pinned
  PX4/Gazebo runtime for VTOL, fixed-wing and multirotor from the current
  checkout.  Each runtime produced a qualified `GPS_RAW_INT`/heartbeat stream,
  loopback MAVLink I/O and a clean disposable-container stop/rm.
- Replayed the same three scenarios with `--exercise-arm --exercise-target`.
  Each live-AI bridge waited for an armed heartbeat, executed two policy steps,
  accepted a truth-free target as `TRACK`, emitted through the shared
  `SimulatorMavlinkControlAdapter`, and returned to a neutral shutdown frame.
- The six source records were joined and independently rehashed in
  `artifacts/evaluation/simulator-external-runtime-ai-evidence-20260902-pre.8-continue-v1.json`
  (5,426 bytes, SHA-256
  `1A3D7F99BC0E6C21E68BDFA57B9DFA77856C030E3DC3DE406CB4ED3A78FD63B5`,
  `gate_passed=true`).  This closes the headless external-SIM/runtime and
  truth-free AI/target-policy sub-gates only; visual, hardware, outdoor GNSS,
  HIL/REAL and final-release gates remain explicitly pending.
- Rebound that join into
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-docker-ai-continue-v1.json`
  (31,778 bytes, SHA-256
  `CDDD0C7E6F2DFA7C4160AC88DA9CC393C372371F5B3D1204D8C30C5666CFFB28`).  The
  total acceptance records `simulator_external_evidence` as provided,
  verified, source-recomputed and passed; software remains `pass` while the
  physical field status remains `offline_pending`.

### v0.3.0-pre.8 SIM AI GPS return-quality hardening (2026-09-02)

- Applied the canonical outdoor GNSS ceilings to the shared SIM AI telemetry
  contract: `gps_quality_valid` now requires hAcc `<=6 m` and vAcc `<=10 m`,
  while the return policy clamps legacy single-value accuracy and age knobs to
  the same per-axis/`1.5 s` limits.  A relaxed caller configuration can no
  longer promote a weak fix into `RETURN`.
- Added regression coverage for both accuracy boundaries and for relaxed
  legacy return configuration; the full Python suite now collects `2,397`
  tests with one Windows symlink capability skip, and QGC custom tests remain
  `66/66`.
- The fresh software acceptance snapshot is
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-ai-gps-accuracy.json`
  (31,407 bytes, SHA-256
  `074DE9C2C8551C3B3CEF7E8462BDF05DFD2CD8AFC70A598BFDF800FA8468F03E`).
- Regenerated `configs/release-parity.json` (3,317 bytes, SHA-256
  `6D0CDBD3D376FBE1AF46A51C721AF3785528CA6BF22016DCD4216979769D7FDF`),
  with `core_algorithm_hash=6c2d186e8b6c31330b16fae096c4d90302797bdf5d697e28ff4efbc55fbdc06d`.
  The corresponding E7 manifest/report are
  `artifacts/evaluation/release-gate-pre.8-current-20260902-ai-gps-accuracy.json`
  (SHA-256
  `47C5C9DA6E0F00B8B6CF4F000D1B693AEE71E632847ABD1F7C6380F06AABFE42`) and
  `artifacts/evaluation/release-gate-pre.8-current-20260902-ai-gps-accuracy-report.json`
  (SHA-256
  `42A18B5929122FFB4F09ABC5638757F44F99587054D2827B2D2C16BEDB4A24F1`).
  Both remain fail-closed (`gate_passed=false`, `release_allowed=false`)
  while the physical field, outdoor GNSS, HIL/REAL and visual gates are
  pending.

### v0.3.0-pre.8 control-input and field-link hardening (2026-09-02)

- Re-ran the hash-bound 3-D SIM control/asset acceptance on the current
  `v0.3.0-pre.8` tree.  The fresh VTOL evidence is
  `artifacts/evaluation/simulator-3d-control-20260902-continue.json` (82,463
  bytes, SHA-256
  `56099CE79D65DF7D240BFC633E3479EA848053E4665F62AA0B6157F2509A82AC`).
  The deterministic shared QGC/manual/AI/watchdog path, WGS-84 world binding,
  asset digests and loopback port isolation all pass.  PX4/Gazebo executables
  are not installed on this desktop, so `external_runtime_gate_passed=false`,
  `external_3d_runtime_acceptance=not_claimed`, and no visual, hardware, HIL or
  REAL claim was made.
- Unified the canonical outdoor GNSS quality constants in
  `src/multidetect/mavlink_gps.py` and made `AdaptiveRangingConfig` reject any
  `hAcc`, `vAcc` or GPS-age limit looser than `6 m`, `10 m` or `1.5 s`.
  Real-time ranging/geolocation and the receive-only capture/verifier now use
  the same fail-closed policy, so a relaxed caller configuration cannot promote
  a weak fix into a map coordinate.  Added regression coverage for all three
  relaxed-limit paths.
- Regenerated `configs/release-parity.json` after the shared GNSS policy change;
  the current parity document SHA-256 is
  `21744240FAF737EF6F63C74014ADFF50AE321A71B3D2BD5CD6427A06E6BB9500` and its
  `core_algorithm_hash` is
  `ab0593e3f986d49e72645340a0f2e2f0fa81f3fc4bafab004f2507d1d39fec9a`.
- Re-ran the complete goal acceptance after regenerating parity.  The current
  snapshot is
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-gnss-policy.json`
  (31,407 bytes, SHA-256
  `4892878FEBFA17E2473002293296762B950009FB2060C3EFD69A93DBFD44B24D`), with
  2,393 core tests and QGC custom `66/66` passing.  The matching pre.8 E7
  manifest and report-only aggregation are
  `artifacts/evaluation/release-gate-pre.8-current-20260902-gnss-policy.json`
  (11,454 bytes, SHA-256
  `1BD1DD7B207C2C439EBF0F5F1940661E1D1724BE757C6AEA5771ABEA852C1A65`) and
  `artifacts/evaluation/release-gate-pre.8-current-20260902-gnss-policy-report.json`
  (25,297 bytes, SHA-256
  `42A51E7B893C69FDF27C4D2AF4297DB001794452ECC202F08C71C51CE3FD0EDD`).
  Both remain fail-closed (`gate_passed=false`, `release_allowed=false`) while
  the field-link report is unready and REPLAY/HIL/REAL/visual gates remain open.
- Added [`docs/field-bringup.md`](docs/field-bringup.md), a repeatable post-cabling
  runbook for the Ethernet secondary address, route-aware protocol preflight and
  hash-bound field evidence.  The latest read-only three-sample baseline is
  `artifacts/evaluation/field-link-preflight-20260902-continue-v3.json` (SHA-256
  `388FFB6AE7A0DC9C6399A8CCD24F4353B5ACCE6A9C64E175270C9A2BCE52A09E`); its
  structure verifier passes, while the remote Jetson/GR01/RTSP probes remain
  unavailable and `hardware_link_ready=false`.
- Added the receive-only `multidetect-gnss-capture` entry point, its strict
  `configs/schemas/gnss-capture.schema.json` envelope, and the offline
  `multidetect-gnss-capture-verify` verifier.  The capture records
  `GPS_RAW_INT`/`GPS2_RAW` and `GLOBAL_POSITION_INT` with host receive time,
  WGS-84 coordinates, fix/satellite/accuracy fields and per-sample rejection
  reasons; it transmits zero MAVLink frames and marks hardware/REAL acceptance
  as `not_claimed`.  The verifier recomputes record counts, raw-GPS quality,
  decode errors, freshness and the two quality gates, so edited summaries,
  coordinates or receive-only flags remain fail-closed.
- GNSS capture thresholds may be tightened for a diagnostic run, but the
  canonical `hAcc<=6 m`, `vAcc<=10 m`, `GPS age<=1.5 s` policy cannot be
  relaxed: a report using looser limits records `threshold_policy_passed=false`
  and cannot pass the quality gate.
- Added `scripts/verify_gnss_capture.py` and linked the capture/verification
  commands from the field bring-up runbook and repository architecture.  The
  new regression suite covers valid/invalid wire values, exact-pole rejection,
  atomic output, tampered reports and wrapper execution without `PYTHONPATH`.
- Added `multidetect-target-survey-template` and
  `scripts/create_target_geolocation_survey_template.py`.  The helper verifies
  a target-event replay, exports only fresh/qualified events with their event
  IDs, source frames, estimated WGS-84 coordinates, GPS age and reported `1σ`,
  and leaves both independent ground-truth columns empty for RTK/survey input.
  CSV, JSON and JSONL outputs are written atomically; existing worksheets are
  protected unless `--overwrite` is explicit.  The sidecar/CLI summary is
  marked `contains_truth=false` and `gate_passed=false`, and an empty fresh
  selection returns exit code `1`, so a worksheet cannot be mistaken for REAL
  accuracy evidence.
- Added the strict JSON-envelope contract at
  `configs/schemas/target-geolocation-survey-template.schema.json`; CSV and
  JSONL remain row-oriented variants of the same truth-free worksheet.
- Replay-derived accuracy reports now include a strict optional `selection`
  object with source-manifest hash, eligible/matched/skipped counts and
  unmatched/unused IDs.  The report verifier checks the count-conservation
  equations while retaining compatibility with older reports that omit it.
- Tightened the SIM MAVLink adapter's numeric boundaries: boolean values are
  rejected for AI envelope limits, MAVLink system/component IDs, watchdog
  duration, and adapter timestamps instead of being silently coerced by
  Python's `bool`-is-an-`int` rule. Added regression coverage for the config,
  AI, watchdog, shutdown, and explicit-failsafe paths.
- Added `scripts/run_field_link_preflight.ps1` as a Windows one-shot field
  workflow. It resolves the repository and Python interpreter from the
  checkout, performs a non-mutating network-interface snapshot, runs the
  existing protocol/route preflight, and verifies the saved JSON. Diagnostic
  runs preserve `hardware_link_ready=false` with exit `0`; `-RequireReady`
  (or `-RequireAll`) turns readiness into a hard gate. The wrapper never
  changes IPs/routes or emits flight-control traffic.
- Verification after these changes: the complete Python suite is `2387
  passed, 1 skipped` (the skip is the existing Windows symlink environment
  gap), `ruff check src tests scripts` passes,
  `compileall` passes, and the repository whitespace check is clean.
- Saved the latest three-sample, route-aware desktop baseline to
  `artifacts/evaluation/field-link-preflight-20260902-desktop-continue.json`
  (16,806 bytes, SHA-256
  `98C6039B2E6988E066DA82C549530AB25C160DE6EFDAA0FEC50F21396D2A68BC`).
  The wrapper and report verifier both exited `0` in diagnostic mode, while
  all three remote Jetson/GR01/RTSP probes timed out and selected local source
  `192.168.1.13` outside `192.168.144.0/24`; `hardware_link_ready=false`
  remains fail-closed.
- Bound that report into a complete acceptance snapshot at
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-desktop-continue.json`
  (31,412 bytes, SHA-256
  `9E7347F288151F53245DD103242993183C38F6ED363079F4C53C63A3EC2A9E66`).
  The run collected 2,366 core tests and 66 QGC custom tests, with software
  status `pass`; the field verifier correctly returned `1` for the unready
  report and the aggregate stayed `hardware_status=offline_pending`.
- Rebuilt the E7 manifest against that latest field report instead of the
  earlier route snapshot. The manifest is
  `artifacts/evaluation/release-gate-pre.8-current-20260902-desktop-continue.json`
  (11,459 bytes, SHA-256
  `29729C9201B2A755C4E4017B11958944B5DCB3C4AEEE73CB660F78468370B327`); its
  report-only aggregation is
  `artifacts/evaluation/release-gate-pre.8-current-20260902-desktop-continue-report.json`
  (25,312 bytes, SHA-256
  `AC6A50DDAD83783FE3FED5F0984F5A9E04C306B9C510CB6782AF93A9B1B6E19E`).
  `gate_passed=false` and `release_allowed=false` remain expected because the
  route, replay accuracy, outdoor GNSS, HIL, REAL, and visual gates are still
  open; the current field report is now the hash-bound E7 input.
- Refreshed the deterministic SIM provenance on the same `v0.3.0-pre.8` source:
  the three-airframe native-QGC interaction artifact is
  `artifacts/evaluation/simulator-interactive-20260902-pre.8-continue-v2.json`
  (111,844 bytes, SHA-256
  `24B57DAA77551CBE9970D95A7A93C5AC6209269F044285F5DD7B551467B313BE`), and the
  VTOL, fixed-wing, and multirotor 3-D control artifacts are respectively
  `simulator-3d-control-20260902-pre.8-continue-v2.json` (82,435 bytes,
  `1D4F90E0C631BCB42A7F9DF5BD6F9C4B7468CD37AAA891BF96AF8CF11DD64177`),
  `simulator-3d-control-20260902-fixed-wing-continue-v2.json` (83,126 bytes,
  `FB879AE4F627474B24D3397739A6E3ECC60F92891CCF671CC846414D02697982`), and
  `simulator-3d-control-20260902-multirotor-continue-v2.json` (83,089 bytes,
  `84419C67A853D6467A900CC76154EE1699052123B456C0C1D0BC66B006ABFCFE`).
  All four report `gate_passed=true`; the 3-D files also report
  `deterministic_gate_passed=true` while keeping external runtime, visual,
  hardware, outdoor GNSS, HIL, and REAL claims fail-closed.

### v0.3.0-pre.8 field-link UAC convenience (2026-09-02)

- Added an explicit `-Elevate` path to `scripts/prepare_field_link.ps1`. A
  non-administrator PowerShell can request a UAC relaunch while preserving the
  interface/IP/prefix/remove arguments and propagating the child exit code;
  `-VerifyOnly` remains non-mutating and does not prompt.
- Re-ran the full goal acceptance after the UAC change. The current aggregate
  is `artifacts/evaluation/goal-acceptance-20260902-033125-field-link-uac-v3.json`
  (31,491 bytes, SHA-256
  `1292D7CB4A059DA47CA8BCCFC723855284998ED2D05627D84571C9334F627030`), with
  exit code `0`, 2,353 core tests, QGC custom `66/66`, and
  `hardware_status=offline_pending`.
- Added a bounded protocol discovery snapshot for the current desktop subnet:
  `artifacts/evaluation/field-link-discovery-20260902-033839-desktop-uac-protocol.json`
  (8,437 bytes, SHA-256
  `C5BB3A87128F452F8C9A7269CCDE44376AF77889B90AD5373EED61EED9A1F2E5`). Four SSH
  hosts responded, but no host exposed the expected `5760/MAVLink` or `554/RTSP`
  service, so the explicit aircraft endpoints and hardware gate remain unchanged.

### v0.3.0-pre.8 target-state evidence gate (2026-09-02)

- Added the optional `--require-target-state-evidence` gate to the field-session
  validator and capture path.  Qualified GPS and `RANGE_STATUS` samples now
  retain the most recent `DET/TRK/LCK/TGT` state context; a cancel event clears
  that context so post-cancel telemetry cannot be reused as evidence for the
  previous target.
- When enabled, a field session must contain at least one fresh, qualified GPS
  sample and one fresh, qualified range sample in the `TGT` context.  The
  setting is persisted in capture reports and re-applied by the provenance
  verifier, so verification cannot silently fall back to the legacy
  session-wide sensor gate.
- Added regression coverage for stale-at-TGT data, sensor-only records that
  inherit the TGT context, and cancel context clearing.  The default validator
  remains backward-compatible; real field runs should enable the strict flag
  together with `--require-link-preflight`.
- Replayed the seven-record cancel/reacquire fixture with the strict option:
  `artifacts/evaluation/field-session-software-contract-20260902-target-state-evidence.capture.json`
  (5,849 bytes, SHA-256
  `00D02947741E0FE677F5BAF91E7EA2F177E97EA7F797C498F186B916D647029D`). Both
  capture and provenance verification exited `0`; this remains a
  software-contract fixture rather than a field-hardware claim.
- Added `--real-target-state-evidence-report` to the E7 manifest writer. The
  binding stores the capture report hash, `DET/TRK/LCK/TGT` path and the
  fresh GPS/RANGE record indexes, while `check_release_gate.py` reruns the
  capture verifier on every evaluation. The final release channel now requires
  this strict TGT-session proof in addition to the REAL link and surveyed
  WGS-84 accuracy report; the prerelease remains backward-compatible when the
  optional binding is absent.
- Generated the non-destructive pre.8 bound diagnostic manifest
  `artifacts/evaluation/release-gate-pre.8-current-20260902-target-state-evidence-v1-bound.json`
  (SHA-256 `6AB1053D1DE5EE49BC81BE593210918186650990B28493F190DD33E7DC8A33DC`). Its
  target-state binding re-verifies successfully, while the aggregate remains
  `release_allowed=false` because the existing replay/REAL/HIL and operator
  acceptance gates are still pending.

### v0.3.0-pre.8 acceptance refresh (2026-09-02)

- Re-ran the complete acceptance after adding the field-link helper and its
  regression test. The latest aggregate is
  `artifacts/evaluation/goal-acceptance-20260902-031536-field-link-tool-v1.json`
  (31,965 bytes, SHA-256
  `08D0CEF2EEEDED68B79E28F7E40BA09337987D443DBDD2F5E50A9E23DFC23EF5`). It
  exited `0` with 2,353 core tests, QGC custom `66/66`,
  `software_status=pass`, and `hil_contract_acceptance.all_contracts_passed=true`.
  The bound Mode-2 and Mode-3 reports are 1,194/2,515 bytes with SHA-256
  `0E5BE4437D25B9BA97D4B21C6FC76CDDA506CA727C1E956032C51C3912CA2FD4` and
  `B731E41D9E9A9C23AB11FD912F80544E5158029F393340BA84E8D71BE7C65248`.
  The field route remains fail-closed (`192.168.1.13` selected instead of
  `192.168.144.0/24`; `hardware_status=offline_pending`).
- Regenerated the HIL release-mode envelope from the latest aggregate source:
  `artifacts/evaluation/release-mode-hil-20260902-031536-field-link-tool-v1.json`
  (622 bytes, SHA-256
  `1171756830158D276BEF0EE9FD8E4354F394A174C1CAB5589637D19991F6BE3A`).
  Rebound it into
  `artifacts/evaluation/release-gate-pre.8-current-20260902-field-link-tool-v1.json`
  (10,957 bytes, SHA-256
  `428A2E78B25C0FDFB9EC247D6DD9A4882077DF6A1A73A0D21DB6D2090CE442A8`).
  With `--evaluated-at-utc 2026-09-02T00:00:00Z`, report-only and strict
  outputs are byte-identical (22,852 bytes, SHA-256
  `A0D4073B90166D34DD1515C79B43E68F57D5CA2D14B46D58A2C1FA7E401EAE24`) and
  exit `0`/`2`; HIL source and SIM sub-gates remain green while the final
  release gate stays closed for missing visual/hardware/REAL/replay evidence.
- Added `scripts/prepare_field_link.ps1` for the migrated desktop's aircraft
  Ethernet setup. It is idempotent, parameterized, and changes only a
  secondary `192.168.144.250/24`-style host address with no gateway or DNS;
  `-VerifyOnly` is non-mutating and `-Remove` rolls back exactly the selected
  address. The current non-elevated attempt returned `Access denied`, so the
  route watch remains `route_checks_passed=false` until an operator runs the
  apply step from an elevated PowerShell after connecting the aircraft cable.
- Wired the Mode-2 payload and Mode-3 approach HIL-contract runners directly
  into `scripts/run_goal_acceptance.ps1`. Every aggregate now regenerates and
  checks a `hil_contract_acceptance` node instead of relying on manually joined
  standalone reports. The new node requires advisory/software-only boundaries:
  no flight control, no physical release, no real payload/actuator interface,
  and `advisory_only=true` for Mode 3.
- Fresh end-to-end aggregate:
  `artifacts/evaluation/goal-acceptance-20260902-030411-hil-contract-route-v1.json`
  (31,989 bytes, SHA-256
  `BDDF745A9586E67242DCDBD242AB2BFAF6CE86CDEB21C30F03BEBC03E91B737D`). The
  script exited `0` with 2,352 core tests, QGC custom `66/66`,
  `software_status=pass`, and `hil_contract_acceptance.all_contracts_passed=true`.
  Bound Mode-2 and Mode-3 reports are
  `...-mode2-payload.json` (1,195 bytes, SHA-256
  `8948429D6884D32AED11D47296A3D49055A98049831FC90ACC095C9487777997`) and
  `...-mode3-approach.json` (2,513 bytes, SHA-256
  `F9A1351A9C777E4F29229DD8BF9FD476FC68FD6080ECC60AA696D7447A7364D5`).
  These are HIL-contract/software evidence only and do not claim a real
  payload, actuator, Pixhawk, outdoor GNSS, or manual Fly View result.
- The same aggregate keeps the three-sample route watch fail-closed: all
  Jetson/GR01/RTSP endpoints selected `192.168.1.13` instead of
  `192.168.144.0/24`, so `hardware_link_ready=false` and
  `hardware_status=offline_pending`. SIM safety, the three-airframe scenario
  matrix, PX4 Mission transaction, and external-SIM revalidation remain green;
  final `v0.3.0` is still unreleased.
- Added an optional timezone-aware `--evaluated-at-utc` input to the release-gate
  evaluator. When a fixed timestamp is supplied, repeated checks of the same
  manifest and evidence produce byte-identical reports and SHA-256 values;
  the default remains the current UTC time, and no release sub-gate is
  bypassed. The current route-bound report was archived as
  `artifacts/evaluation/release-gate-pre.8-current-20260902-route-bound-reproducible-report.json`
  (SHA-256
  `765F2752F9394953D799D23BC06B3323A400710A7CC06FBC94201280C11DCA9A`); it
  remains `gate_passed=false` while the aircraft subnet is offline.
- Re-ran the Mode-2 payload and Mode-3 approach software/HIL-contract scripts and
  archived fresh evidence:
  `artifacts/evaluation/mode2-payload-hil-20260902-024833.json` (SHA-256
  `C1F4CDA12388EB44E89063EE857A13366167AD9BEF2298AF0A3D35999FD77387`) and
  `artifacts/evaluation/mode3-approach-hil-20260902-024833.json` (SHA-256
  `4A71CAA628553427AEB428EC5AE89640D72E6882144334528281B2FA4F4FDD0E`). Both
  reports pass their deterministic contract checks: signed payload authorization,
  target selection/slide expiry and target-switch revocation for Mode 2; shared
  centering/aiming, fresh-challenge recovery and fail-closed lost/occluded/avoidance
  handling for Mode 3. They remain advisory/software-only with
  `flight_control_enabled=false`, `physical_release_enabled=false`, and no real
  camera, outdoor-GNSS, Pixhawk, HIL, or REAL claim.
- Re-ran the full `v0.3.0-pre.8` goal acceptance on 2026-09-02 and bound
  the current-day field-link preflight. The v6 snapshot is
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-fault-coverage-v6.json`
  (36,169 bytes, SHA-256
  `0F59CEC1C31B2B30669598E064F71F217051E86BBD300304BB4B3CA3A4402AE1`),
  with script exit `0`, 2,337 core tests, QGC custom `66/66`, and software
  status `pass`. The bound field report is
  `artifacts/evaluation/field-link-preflight-20260902-continue-v1.json`
  (3,038 bytes, SHA-256
  `64C06112A967BC0C3BABE904C0588143C182F2A221262A0D97C062E6077119AD`);
  its structure/hash verifier passes, but the three field endpoints remain
  unreachable, so `hardware_link_ready=false`, `gate_passed=false`, and no
  outdoor-GNSS, Pixhawk, HIL, or REAL claim is made. The v6 snapshot keeps the
  combined SIM, S4 safety, three-airframe fault matrix, and PX4 Mission
  transaction bindings green.
- Rebound those v6 sources into
  `artifacts/evaluation/release-gate-pre.8-current-20260902-fault-coverage-v6-bound.json`
  (10,219 bytes, SHA-256
  `48A3D463A05FC30896B8651CB698E88FF242700A56B6CFEDFE83CB34BD78F648`).
  The report is
  `artifacts/evaluation/release-gate-pre.8-current-20260902-fault-coverage-v6-bound-report.json`
  (19,798 bytes, SHA-256
  `647C0F2EE4514DDBF60F11AC83E14B01C339D39F484E683103C4C44050D3292F`),
  and the strict output is
  `artifacts/evaluation/release-gate-pre.8-current-20260902-fault-coverage-v6-bound-strict.json`
  (19,798 bytes, SHA-256
  `12A3DDE12D57FF78FF0C5C5B22D4E880A122B2AD18F0230A99FDB293E8B49CB9`).
  Report-only exits `0`; strict evaluation exits `2` and keeps
  `release_allowed=false` for the offline field/REAL/replay-set, visual/
  hardware, and prerelease-channel gates. The v5 artifacts remain historical
  provenance.
- Exercised the read-only field-session capture/provenance path against the
  repository fixture and saved
  `artifacts/evaluation/field-session-software-contract-20260902-cancel-reacquire.capture.json`.
  The capture contains seven records with
  `DET -> TRK -> LCK -> TGT -> cancel_trk -> DET -> TRK`, a verified cancel/reacquire
  transition, six qualified GPS and RANGE samples, and matching source/output
  hashes; `verify_field_session_capture.py --require-gate` exits `0`. This is
  explicitly a software-contract fixture and does not claim a real camera,
  outdoor GNSS, or field-flight result.
- Recomputed the six-stratum SIM replay-set in report-only mode and saved
  `artifacts/evaluation/replay-set-accuracy-20260902-pre.8-report-only.json`
  (9,043 bytes, SHA-256
  `3DAAECBC63AC5BB512B60D9817DF5B1CFF0F0FA8995930D8749583A8C242B319`).
  The report is structurally valid with complete predictions and evaluated
  metrics, but deployment-domain coverage and independent accuracy review are
  still missing, so `accuracy_validated=false` and `gate_passed=false` remain
  intentional.
- Bound the same SIM replay-set and report-only accuracy evidence into a
  supplementary pre.8 E7 manifest:
  `artifacts/evaluation/release-gate-pre.8-current-20260902-replay-bound-v7.json`
  (10,812 bytes, SHA-256
  `F4D3831F024D0714934C248425C4B758C6AF557D58BD5F3FA1F10CA488C272C1`).
  Its report-only and strict outputs are
  `...-v7-report.json` (SHA-256
  `E4C9048133FB17018361A9BE414DDFDCB92F879983B7DD007A81DF513573DD49`) and
  `...-v7-strict.json` (SHA-256
  `F6BA612293DE59CD44CB3ADD29BF7F1FADE0FEA6B36E981FBAABC7BCEB31BA7C`).
  The aggregate now records `replay_set.present=true` while keeping
  `replay_set.passed=false` for the missing deployment-domain coverage and
  independent review.
- Re-ran the default field-link watch for three samples and saved
  `artifacts/evaluation/field-link-preflight-20260902-continue-v2.json`
  (12,064 bytes, SHA-256
  `F7133D83E89F758A6C4819E71D185F228404DCCAC1A4BC713C7C61C4306F253D`).
  All three remote TCP/protocol probes timed out while local UDP 14580/14581
  binds succeeded (`passed_samples=0`, `failed_samples=3`,
  `hardware_link_ready=false`). The latest field report is hash-bound into
  `artifacts/evaluation/release-gate-pre.8-current-20260902-replay-bound-v8.json`
  (SHA-256
  `A3052885BDDF7AB8513441E513A08AC57AE855F39C75E26EA59A5B1B9686BA7A`);
  report-only exits `0`, strict exits `2`, and the hardware/replay/final gates
  remain closed.
- Re-ran the full goal acceptance with the three-sample field watch bound:
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-field-watch-v7.json`
  (36,154 bytes, SHA-256
  `B240A5999B64C3EEE582EB74CDBCE75A90012FDD0BD470F91CFF30AFCBA2077A`).
  The script exits `0` with 2,337 core tests and QGC custom `66`; software is
  `pass`, while hardware remains `offline_pending` and the field preflight
  strict result remains `1`.
- Added an opt-in passive local-route gate to the field preflight. The new
  `--require-route --expected-source-cidr <aircraft-cidr>` options record the
  operating system's selected source address for Jetson, GR01 and RTSP without
  sending application data, and fail closed when a VPN/TAP or migrated NIC
  selects the wrong subnet. The current desktop diagnostic is
  `artifacts/evaluation/field-link-preflight-20260902-route-v1.json`
  (4,396 bytes, SHA-256
  `21DAD57EA9CF8AD1FA9996F8110193BEA3535E270F7E87C5D7A100A4BA699B13`): all
  three endpoints selected `192.168.1.13` instead of the expected
  `192.168.144.0/24`, so strict readiness exits `1` and no hardware/REAL claim
  is made. Route evidence is independently checked by
  `verify_field_link_preflight.py --allow-unready`.
- Bound the route-enabled field report into
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-route-v1.json`
  (SHA-256 `4A11A7ED8F7DE6422640783349343751B8A011192277AD613992531DBC64F8BB`).
  The complete software acceptance exits `0` with 2,348 core tests and QGC
  custom `66/66`; the bound route evidence remains `hardware_link_ready=false`,
  so HIL/REAL and outdoor-GNSS gates stay closed.
- Rebound the same route report after extending `run_goal_acceptance.ps1` with
  `route_checks_requested`, `route_checks_passed`, and `route_source_cidr`.
  The latest snapshot is
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-route-v2.json`
  (29,706 bytes, SHA-256
  `C7DECB029209C4CA6D05CB74D1B951E89811424C884FA0F051D1F5378E87BEE4`).
  It exits `0` with 2,348 core tests and QGC custom `66/66`, while explicitly
  retaining `route_checks_passed=false` and `hardware_status=offline_pending`.
- The same acceptance run generated the fresh external-SIM revalidation
  binding
  `artifacts/evaluation/simulator-external-runtime-ai-revalidation-acceptance-20260902-pre.8-v2.json`
  (534 bytes, SHA-256
  `B5D08D43A9C74B67C12A6BF70798BCE04557E316E4BBD1418253DE69AA4E7A8C`).
  Its `source_recomputed=true` and `gate_passed=true` fields remain a
  software-only provenance result and do not alter the offline hardware gate.
- Re-ran the complete three-sample route/protocol watch after the desktop network
  check. The report is
  `artifacts/evaluation/field-link-preflight-20260902-025107-route-continue.json`
  (16,803 bytes, SHA-256
  `0F2743810B7DC5CA6554A960B38C8876110FD980B21B062248BC2E76029F77FE`). All
  Jetson/GR01/RTSP probes timed out and all three route probes selected
  `192.168.1.13` instead of `192.168.144.0/24`; local UDP `14580/14581` binds
  succeeded. The structural verifier passes with `--allow-unready`, while
  `hardware_link_ready=false` and strict readiness remain closed.
- Bound that route watch into a fresh goal-acceptance snapshot:
  `artifacts/evaluation/goal-acceptance-20260902-025359-route-continue.json`
  (29,845 bytes, SHA-256
  `601FAA324BE21B309EC378302114150B142635B66C1F989453EE5A1721B3D6EB`). The
  script exited `0` with 2,352 core tests and QGC custom `66/66`; software is
  `pass`, external-SIM revalidation is `gate_passed=true`, and the field
  binding records `route_checks_passed=false`, `hardware_link_ready=false`, and
  `hardware_status=offline_pending`. No outdoor-GNSS, Pixhawk, HIL, or REAL
  claim is made.
- Rebound the same source set into the current pre.8 E7 manifest
  `artifacts/evaluation/release-gate-pre.8-current-20260902-route-continue.json`
  (10,925 bytes, SHA-256
  `4F389AEE91ADEEB79F4CDC9E200643F4DDB5F52D9E1A770C8D84148F9EECA9F2`). With
  `--evaluated-at-utc 2026-09-02T00:00:00Z`, report-only and strict reports are
  byte-identical (22,816 bytes, SHA-256
  `3A4DD9D10BDB34FAD12EF2878633DC956D315FFB4784D0B25FDE40507419EC35`); exits
  are `0` and `2`, respectively. SIM safety/scenario/PX4 transaction and
  external-SIM revalidation remain green, while route/GNSS, replay accuracy,
  visual, HIL/REAL, and prerelease gates remain closed.
- Extended the E7 `real_field_link` writer, evaluator, and schema to retain
  the passive route summary (`route_checks_requested`, `route_checks_passed`,
  and `route_source_cidr`). New route-enabled REAL manifests are now checked
  against those fields on every re-evaluation; historical manifests without
  them remain readable.
- Tightened the final `release` channel so a passing REAL field-link binding
  must include route provenance with `route_checks_requested=true`,
  `route_checks_passed=true`, and a non-empty aircraft `route_source_cidr`.
  Historical prerelease bindings remain readable, while a release manifest
  without this evidence stays blocked.
- Generated and re-evaluated an end-to-end route-bound E7 pre.8 sample:
  `artifacts/evaluation/release-gate-pre.8-current-20260902-route-bound.json`
  (10,924 bytes, SHA-256
  `E9DCE9776A50C6957479D26992576146A3DA1E9378CF976892FB11F85B3F1449`). The
  report-only output is
  `...-route-bound-report.json` (22,755 bytes, SHA-256
  `A174F8A999777737C5737C5B91E4908A08B12F7741CDFC5A52405AA5EA96A1AD`), and
  the strict output is
  `...-route-bound-strict.json` (22,755 bytes, SHA-256
  `73251284191B662184E4E9BF6E3BB8B18B64DE9968D7FB3B91E458C0B7B128FD`).
  Report-only exits `0`; strict exits `2` with
  `gate_passed=false`, `release_allowed=false`, and
  `real_field_link.passed=false`, retaining
  `route_checks_requested=true`, `route_checks_passed=false`, and
  `route_source_cidr=192.168.144.0/24` because the desktop selected
  `192.168.1.13` instead of the aircraft subnet.
- Re-ran the deterministic 3-D control acceptance for the `river_rescue_vtol_001`
  scenario. Source evidence is
  `artifacts/evaluation/simulator-3d-control-20260902-pre.8-continue-v1.json`
  (82,463 bytes, SHA-256
  `56099CE79D65DF7D240BFC633E3479EA848053E4665F62AA0B6157F2509A82AC`).
  The deterministic protocol/asset gate passed and proved shared QGC manual,
  AI TRACK, manual takeover, AI resume, and watchdog/failsafe behavior through
  the same adapter and flight model; external 3-D runtime and visual/hardware
  acceptance remain explicitly `not_claimed`.
- Re-ran the native QGC interactive acceptance with `--mode all` for VTOL,
  fixed-wing, and multirotor. Evidence is
  `artifacts/evaluation/simulator-interactive-20260902-pre.8-continue-v1.json`
  (111,844 bytes, SHA-256
  `24B57DAA77551CBE9970D95A7A93C5AC6209269F044285F5DD7B551467B313BE`).
  All three mode gates passed with manual-priority, AI-resume, watchdog, and
  QGC-only loopback checks. This closes a software/SIM interaction sub-gate,
  not the external PX4/Gazebo, Pixhawk/Jetson/camera, outdoor-GNSS, HIL, REAL,
  or human Fly View gates.
- Re-ran the digest-pinned Docker PX4/Gazebo external runtime and live-AI
  target exercise for all three airframes with per-job loopback ports. The
  VTOL source pair is
  `simulator-docker-acceptance-20260902-pre.8-desktop-v2.json` (21,895 bytes,
  SHA-256 `E9A95A852BBB91171E88281616A896AC8277D151C57B81DBCBE0E51A8B2802B3`)
  and `simulator-docker-ai-acceptance-20260902-pre.8-desktop-v1.json`
  (108,917 bytes, SHA-256
  `1C12C3699348C18AEF29321975511F08C281A8C26D827EFB5992D88ECB7E04F0`). The
  fixed-wing pair is
  `simulator-docker-acceptance-20260902-pre.8-fixed-wing-v1.json` (22,090
  bytes, SHA-256
  `B5D46C2C8820CF5BFC552015AD8522AE5A53AF272749A882693D017B41BF9385`) and
  `simulator-docker-ai-acceptance-20260902-pre.8-fixed-wing-v1.json` (104,476
  bytes, SHA-256
  `1A9A62C2089D4866928036AF2BCCA38A567E36156823AFD9794B4F310A2B88F7`). The
  multirotor pair is
  `simulator-docker-acceptance-20260902-pre.8-multirotor-v1.json` (22,091
  bytes, SHA-256
  `7E2338F0CA45E7886A5294DEE1E612C0B1315C8A5F6A0660A113C4E8AB775F26`) and
  `simulator-docker-ai-acceptance-20260902-pre.8-multirotor-v1.json` (157,637
  bytes, SHA-256
  `F7E1466DFE29B20912EF4FD57DE5D3AF358B118D3D9F0EC741CB27C478F289B9`).
  Every runtime reports qualified GPS (`fix_type=3`, 10 satellites), heartbeat,
  loopback I/O and cleanup; every AI run records two accepted policy steps,
  `target_exercise.accepted=true`, and `decision.action=track` from the shared
  control adapter.
- Joined those six source files with the same `configs/release-parity.json`:
  `artifacts/evaluation/simulator-external-runtime-ai-evidence-20260902-pre.8-v2.json`
  (5,383 bytes, SHA-256
  `656531912FC0FCF8AF975469CBB0C340136CF4D8515BECE5BFC1827858BDDC35`),
  `gate_passed=true`. The join is a file-level machine gate with
  `truth_free=true`, `hardware_control_disabled=true`, and visual/real-hardware
  fields explicitly `not_claimed`; it does not close outdoor GNSS, Pixhawk/
  Jetson/camera, HIL, REAL, or human Fly View acceptance.
- Fixed the dynamic telemetry-port seam in `src/multidetect/simulator/stack.py`:
  a job-selected loopback telemetry port now explicitly remaps the canonical
  PX4 `14541` sensor endpoint and records `sensor_port_bindings` in the stack
  plan. Unrelated MAVLink ports remain fail-closed. Regression coverage is in
  `tests/test_simulator_stack.py`, and all three current-day external runs use
  the remapped, isolated ports.
- Added a second-pass join revalidator. `--join` reads the six source paths and
  parity path recorded in an existing external-SIM join, recomputes every source
  hash/byte count and gate-bearing field, and fails closed on edited, moved or
  replaced inputs. The current result is
  `artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260902-pre.8-v1.json`
  (534 bytes, SHA-256
  `8366977C0F3800D09C348DCA629DA43FD8DFC9815DD9009D03F0FB2E3CAC8C18`), with
  `source_recomputed=true` and `gate_passed=true`; its contract is defined in
  `configs/schemas/simulator-external-evidence-revalidation.schema.json` and
  covered by `tests/test_simulator_external_evidence.py`.
- Bound that revalidator into `scripts/run_goal_acceptance.ps1`. Supplying
  `-SimulatorExternalEvidence` now records the join and a fresh revalidation
  artifact under `simulator_external_evidence`; `-RequireSimulatorExternalEvidence`
  promotes a missing, stale, edited, or mismatched join to a fail-closed
  acceptance error. The refreshed snapshot is
  `artifacts/evaluation/multidetect-goal-acceptance-20260902-external-revalidation-v1.json`
  (29,246 bytes, SHA-256
  `73F85C769FDE560294575DC22140C67A85CD6D82717F6475088F40396C31F506`),
  produced with exit `0`, 2,343 core tests and QGC custom `66/66`.
  Its external binding reports `provided=true`, `verified=true`,
  `source_recomputed=true`, `gate_passed=true`, while software remains `pass`
  and hardware remains `offline_pending`. The required-mode negative probe
  exits `1` when no external path is supplied, confirming the new gate is not
  silently optional when explicitly required.
  The bound revalidation output is
  `artifacts/evaluation/simulator-external-runtime-ai-revalidation-acceptance-20260902-pre.8-v1.json`
  (534 bytes, SHA-256
  `CA6A73B6012FC28669F696CB5C3D916BA8E7E10F8F59315D8ACDC9A90F9C442A`).

### Deterministic SIM scenario matrix (2026-09-01)

- Added `python -m multidetect.simulator scenario-matrix`, the equivalent
  `scripts/run_simulator_scenario_matrix.py` wrapper, and the
  `multidetect-sim-scenario-matrix` console entry. The default run discovers
  all repository-owned fixed-wing, VTOL, and multirotor manifests, executes
  them through the shared deterministic runner/clock/authority/MAVLink path,
  and repeats each run to compare evidence hashes.
- Matrix summaries now expose complete-run AI attempted/accepted/rejected/
  failsafe counters and control-message counts. The shared AI path claim is
  true only when AI is enabled and every selected scenario accepted at least
  one AI command; `--no-arm` and `--no-ai` therefore remain truthful
  fail-closed checks.
- Added the strict aggregate schema at
  `configs/schemas/simulator-scenario-matrix-evidence.schema.json` and
  regression coverage for discovery, repeatability, selection, wrapper
  invocation, schema shape, and no-arm/no-AI claims.
- Current evidence is
  `artifacts/evaluation/simulator-scenario-matrix-20260901-pre.8.json`
  (11,494 bytes, SHA-256
  `95847C56FB0AAB7FA4CE459B4C43CA15B73D46B8F4F6834455A7CD61E73323E9`),
  with `gate_passed=true` and `deterministic_gate_passed=true`. The matrix
  remains software-only: external 3-D rendering, QGC visual acceptance,
  hardware, HIL, REAL, and real-flight claims stay `not_claimed`.
- Fault timeline coverage is now an explicit opt-in gate. `--cover-faults`
  inspects the runtime channels, extends the shared frame budget through the
  latest declared event (including a one-second tail for indefinite faults),
  and requires every finite or indefinite `FaultEvent` to reach an actual
  observation. The runner now marks `fired=true` for finite windows as well as
  persistent events, so `active_frames` and `fired_event_ids` cannot disagree.
  The three-airframe coverage artifact is
  `artifacts/evaluation/simulator-scenario-matrix-20260901-pre.8-fault-coverage.json`
  (13,512 bytes, SHA-256
  `8DD6171CB60528B800BEEF98021E7C83AC00580792016A14EF27AFB94D531A7E`), with
  2,611 frames and 9/9 declared fault events exercised; the independent
  verifier exits `0`. `run_goal_acceptance.ps1` now uses this coverage mode,
  and the latest snapshot is
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-fault-coverage-v5.json`
  (36,169 bytes, SHA-256
  `7DC28AA4A36204C82C58680E7637C873859B93F1298762F6BC3EB2A072B72C41`),
  with 2,337 core tests, QGC custom `66/66`, and per-mode 4/4, 2/2 and 3/3
  fault coverage; software is `pass`, while the real field link remains
  `offline_pending`. The v5 snapshot also requires and re-verifies the
  combined PX4/Gazebo/QGC source and its release-mode envelope:
  `artifacts/evaluation/combined-sim-qgc-20260901-continue-recovery-v9/combined-evidence.json`
  (333,051 bytes, SHA-256
  `75265742413146EA78AB88CCBD30B22ACFCC0C5DC7EC48780F4A9D25F649F7FB`,
  `gate_passed=true`) and
  `artifacts/evaluation/release-mode-sim-20260901-pre.8-combined-dynamic-ports-v3.json`
  (730 bytes, SHA-256
  `ACDFB988D7BE669FFEEC4846BDA6F66837B7892CF402DA1D25B01DB84D92A045`).
- The v5 goal-acceptance snapshot binds the verified read-only field preflight
  `artifacts/evaluation/field-link-preflight-20260901-continue-v2.json`
  (4,987 bytes, SHA-256
  `13BB57F062F2C9DA687EECBAC3616A1C73291B837FE134DAFBEA20505EEE6328`).
  The report verifier exits `1` because Jetson, GR01, and camera endpoints are
  unreachable; `hardware_link_ready=false` and `gate_passed=false` remain
  truthful, with no REAL/HIL or outdoor-GNSS claim.
- Rebound the same v5 evidence into the current pre.8 E7 manifest:
  `artifacts/evaluation/release-gate-pre.8-current-20260902-fault-coverage-v5-bound.json`
  (10,219 bytes, SHA-256
  `C5892E0515D1C6C26199E35CBC80CB405880D5D6C35A0AB045F70EAA7E121393`). Its
  report is
  `artifacts/evaluation/release-gate-pre.8-current-20260902-fault-coverage-v5-bound-report.json`
  (19,841 bytes, SHA-256
  `988A88232C325B3FE787FAAA314AC5AFA1AEA0446D031B6DA3FAF2E88AE8EBFD`):
  combined SIM, S4 safety, three-airframe fault matrix, and PX4 Mission
  transaction all pass; strict evaluation exits `2` and keeps
  `release_allowed=false` for the offline field/REAL/replay-set, visual/hardware,
  and prerelease-channel gates. The strict output is preserved as
  `artifacts/evaluation/release-gate-pre.8-current-20260902-fault-coverage-v5-bound-strict.json`
  (19,841 bytes, SHA-256
  `09212FD35D096BBDAD045DEAD74BA727037A38F74F1100169D9C0CFB143A1B26`).
- Performed a bounded, read-only discovery of the active `192.168.1.0/24`
  interface after the v4 run. The scan covered 254 hosts, found four SSH
  banners, and found no MAVLink `5760` or RTSP `554` service candidates. The
  report is
  `artifacts/evaluation/field-link-discovery-20260901-continue-v1.json`
  (8,045 bytes, SHA-256
  `03EBA28A99C34420B987ADAB2AC29CED54A5363FE9E4D0C0934AC00FF203CE30`);
  its structure verifier passes with `--allow-unready`, while the hardware
  link remains `false`. Discovery is diagnostic only and never rewrites the
  configured Jetson/GR01/camera endpoints.
- The matrix schema and verifier record the requested versus auto-extended
  frame budget, coverage requirement, active/fired/uncovered event IDs and
  coverage ratio. A short run with `--require-fault-coverage` therefore fails
  closed instead of silently reporting nominal-only coverage.
- Final-channel verifier and manifest-writer paths now force both
  `fault_coverage_required=true` and `fault_coverage_auto_extended=true`.
  A hash-consistent but nominal-only matrix is rejected before a final
  `0.3.0` manifest can be written or evaluated; prerelease manifests retain
  the backward-compatible optional policy.
- Re-evaluated the current E7 matrix-bound prerelease manifest with the new
  policy. The report is
  `artifacts/evaluation/release-gate-pre.8-current-20260901-fault-coverage-matrix-bound-v2-report.json`
  (15,470 bytes, SHA-256
  `C7D92E59C8B24C41B04F8CD4DC9246C34ADA674A7D7E91361551336C96993EAD`): the
  three-airframe matrix and 9/9 fault coverage pass, while aggregate
  `release_allowed=false` remains expected for the offline hardware and
  prerelease channel.
- Added the strict `simulator_scenario_matrix` binding to the E7 release-gate
  writer/evaluator and `configs/schemas/release-gate-manifest.schema.json`.
  `--simulator-scenario-matrix-evidence` records the source byte count/SHA-256,
  embedded canonical digest, all three airframe modes, AI/arm/repeatability
  switches, and verifier checks; the evaluator re-hashes and re-verifies the
  source on every read. It is optional for backward-compatible prerelease
  manifests and required for the final `release` channel.
- Added `scripts/verify_simulator_scenario_matrix.py` and wired the matrix run
  plus independent verification into `scripts/run_goal_acceptance.ps1`, so each
  total acceptance snapshot carries an auditable three-airframe SIM sub-gate.
- Added a no-`PYTHONPATH` verifier-wrapper regression and a final-channel guard
  proving that four-mode evidence alone cannot enable `release` without the
  hash-bound three-airframe matrix. The full current regression is
  `2336 passed, 1 skipped` (exit code `0`); the captured output is
  `artifacts/evaluation/pytest-full-20260901-final-fault-coverage-policy.log`
  (2,901 bytes, SHA-256
  `F9CDC0D3CC26F276B13585B11923199780E4EFE949559D28CDB1A03B54C0D9F8`).
  The remaining skip is the explicit Windows symlink environment gap and is
  not a release claim; the optional onnxruntime tests ran in this environment.

### Field-session target binding hardening (2026-09-01)

- Tightened `field_session_acceptance` so a qualified field-session window
  requires one identical target identity across GPS and RANGE samples. A
  non-empty set intersection is no longer sufficient; partial or multi-target
  identity sets now remain fail-closed and are surfaced in
  `target_binding.cross_record_mismatches` / `multiple_target_ids`.
- Added a regression covering GPS/RANGE samples arriving in separate records
  with a misleading partial overlap.
- The refreshed goal-acceptance snapshot is
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-target-binding-v9.json`
  (23,268 bytes, SHA-256
  `7CC6B274EE6CA188A718B7DBBD306A514A92750882D5FC7D75C19B61056A6538`). It
  collected 2,319 core tests and QGC custom 66/66 with software status `pass`;
  the field link remains `hardware_link_ready=false` / `offline_pending`.

本节包含截至 2026-09-01 的 `v0.3.0-pre.8` 增量；版本清单仍保持
`release_label=0.3.0-pre.8`，未创建 `pre.9` 或最终 `v0.3.0` 标签。

### 离线 HIL/飞行器契约复核（2026-09-01）

- 重新执行 Mode-3 目标接近与失锁 fail-closed 验收，输出
  `artifacts/evaluation/mode3-approach-hil-20260901-continue.json`（2,512 bytes，
  SHA-256 `E7BFC8717F9B5BE4CCACCDB31C834ABCE0D1602F703D208F8F35B733F2690634`）。
  任意目标均保持 advisory-only；目标丢失、遮挡、切换和 avoidance 失败都会停止盲进近，
  旧确认不能复用，恢复需要新挑战值。
- 重新执行 Mode-2 payload 选择、连续滑动、授权分离和假释放回路，输出
  `artifacts/evaluation/mode2-payload-hil-20260901-continue.json`（1,194 bytes，
  SHA-256 `CE623AE49803339E60A1E1A545C4F1757FF825303011507195F97314212BF351`）。
  HMAC/MAVLink2 签名回路通过；人、普通车辆、目标切换和过期滑动均按策略拒绝，
  物理释放保持关闭。
- 重新执行固定翼 DET→TRK→LCK→TGT、姿态限幅、RC 取消和失锁恢复，输出
  `artifacts/evaluation/fixed-wing-software-20260901-continue.json`（1,950 bytes，
  SHA-256 `7B9737AD76275FF85B9E8F9BCB59420E0A75A414D46F35EF6417E4D03AFC6FE8`）。
  `gate_passed=true`、四元数归一化和滚转/俯仰限幅通过，取消后保持绑定锁存，未打开
  Pixhawk 链路，也未发送物理 MAVLink 输出。
- 重新执行多旋翼 `MISSION_ITEM_INT` 编译契约，输出
  `artifacts/evaluation/multirotor-mission-compile-20260901-continue.json`（1,320 bytes，
  SHA-256 `ADF8BAB8D4887697A1CF09E54AB8C21CEBDD9ACA0312B0A89514FA613F14DAB2`）。
  `takeoff→waypoint→loiter→land` 顺序、连续序列、计划哈希绑定、协议哈希和物理输出关闭
  均通过；该回路仍是进程内软件/SIM 门，不代表真实 Pixhawk 或实飞。
- 重新执行固定翼 `MISSION_ITEM_INT` 编译契约，输出
  `artifacts/evaluation/fixed-wing-mission-compile-20260901-continue.json`（1,263 bytes，
  SHA-256 `EBC526DEEFD443E06A4F2B782A71D0520BCD59C193DB50282064C129FCE1EA3D`），并用同一
  journalized upload/readback/commit/start-permit 路径重跑 VTOL、固定翼和多旋翼，输出
  `artifacts/evaluation/px4-airframe-transaction-20260901-continue.json`（11,947 bytes，
  SHA-256 `5B644637B85C8284EDF7120696F7DA81A85DEABA439039B10F231FE232513811`）。三机型
  entry/checksum/ACK/读回/终止检查全部通过，物理输出仍关闭。
- 重新执行 SIM S4 安全门，输出
  `artifacts/evaluation/simulator-safety-20260901-continue.json`（6,990 bytes，SHA-256
  `021D627162F99A836AD1C7C7724AF1DB56FF7901F7298381457EA0B91BB32DEC`）。它重新验证
  target-specific cancel、旧目标证据清理、假 GPS/地理围栏 fail-closed、watchdog neutral、
  failsafe 后禁止 AI 控制、loopback allow-list 和三机型 interactive authority。
- 用当前 portable `v0.3.0-pre.8`、现场预检和三机型 combined SIM 证据重新执行总验收，
  输出 `artifacts/evaluation/multidetect-goal-acceptance-20260901-desktop-v13.json`
  （23,188 bytes，SHA-256
  `93F3124E1372FAA769856E919FA1A214D78358F9AF15F7BD55AB502CC62B99E8`）。核心测试收集
  `2319`、QGC custom `66/66`、`git_diff_check=pass`、操作员密钥就绪；软件状态为 `pass`，
  现场端点仍为 `hardware_link_ready=false` / `hardware_status=offline_pending`，因此发布门
  继续保持关闭。
- 在台式机当前 `Ethernet=192.168.1.13/24` 上执行一次显式、只读的服务发现，输出
  `artifacts/evaluation/field-link-discovery-20260901-desktop-continue.json`（7,181 bytes，
  SHA-256 `0BD37CB16B5B7E1AD6B7BA4700FBEF5CBEF49AA951863A39DC17F7964DD22692`）。扫描 254
  个地址，发现 4 个 SSH 响应主机，但 `5760` MAVLink 与 `554` RTSP 均为 0 个服务命中；
  发现结果不替换配置端点、不改变 `hardware_link_ready=false`，也没有发送控制数据。
- 修正 E7 清单对上一份 PX4 transaction 文件的陈旧 SHA 绑定，重新生成
  `artifacts/evaluation/release-gate-pre.8-current-20260901-desktop-v13.json`（7,901 bytes，
  SHA-256 `B5BD654F325A2F15018142E4C1D9E913345C28306E4E38D59C7266B2BC4A6485`），并以
  `scripts/check_release_gate.py --report-only` 复核，报告为
  `artifacts/evaluation/release-gate-pre.8-current-20260901-desktop-v13-report.json`
  （16,480 bytes，SHA-256
  `501C653D2F8DE7EA880C9ED1DE8BE17EC285DC651C18A3A57E68C3B2A6FB87DD`）。当前清单的
  replay/sim/HIL、combined SIM、S4 和三机型 transaction 子门均通过；阻断原因准确收敛到
  `hardware_link_ready=false`、REAL source 未通过、缺少 replay-set/目标精度绑定及
  prerelease channel，未再包含陈旧 transaction 哈希错误。
- 对同一清单再跑严格模式（不带 `--report-only`）返回退出码 `2`，并生成
  `artifacts/evaluation/release-gate-pre.8-current-20260901-desktop-v13-strict.json`
  （16,480 bytes，SHA-256
  `6BDCCD4BF9BB45B879C82F28D7C631203B50604587367C968489EB35474BBE00`）。这确认发布门
  在当前现场/版本条件下确实 fail-closed，而不是仅靠文档标注 pending。

  上述离线证据只刷新可重复的 software/HIL-contract 子门，不升级相机、Jetson、室外 GNSS、
  执行器、HIL 台架、REAL 或人工 Fly View 验收；开发版本继续为 `v0.3.0-pre.8`。

### 台式机 Docker 外部 PX4/Gazebo 与 live AI target 重跑（2026-09-01）

- 在当前台式机 Docker Desktop 上使用固定镜像
  `px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`
  完成外部 PX4/Gazebo/UDP runtime gate。证据为
  `artifacts/evaluation/simulator-docker-acceptance-20260901-desktop.json`
  （21,876 bytes，SHA-256
  `C4D191D46604BF86D1BD2BBA64F3907E2C35AB3F1F3369BEFFFCCA709305A045`），
  `gate_passed=true`、`preflight_gate_passed=true`、`runtime_gate_passed=true`、
  `telemetry.qualified=true`，容器 stop/rm 清理成功；观察到 PX4 HEARTBEAT 和
  `GPS_RAW_INT fix_type=3 / satellites_visible=10`。
- 同一场景和镜像执行 `--execute --exercise-arm --exercise-target` 的 live AI 闭环，
  证据为 `artifacts/evaluation/simulator-docker-ai-acceptance-real-20260901-desktop.json`
  （106,789 bytes，SHA-256
  `B53ECA7F8D8F0C4D830C656FA22D3C1C39BD6DAA1E0C8D5719CDE3A6C68733B7`）。
  `ai_policy_gate_passed=true`、`target_policy_gate_passed=true`、`ai_steps=2`；
  ARM 在新鲜 armed heartbeat 后 3 次尝试成功，AI 通过共享
  `SimulatorMavlinkControlAdapter` 发出 2 个 `MANUAL_CONTROL`，truth-free 目标观测
  被接受为 `TRACK`，随后 DISARM 与清理均成功。
- 将本次台式机 VTOL runtime/AI pair 与已通过的 fixed-wing、multirotor pair 按同一
  `configs/release-parity.json` 做三机型文件级 join，输出
  `artifacts/evaluation/simulator-external-runtime-ai-evidence-20260901-desktop-v2.json`
  （5,370 bytes，SHA-256
  `C2FFB02152A96026B639CCA7EE50C243B22BEC94A5FF41A0421A6A409DEA2382`），
  `gate_passed=true`；join 重新检查了三机型 manifest/digest、pre.8、GPS、truth-free
  perception、AI `TRACK`、armed heartbeat、硬件控制边界和容器清理。
- 随后执行增量 Windows Release/portable 构建，Ninja 返回 `no work to do`，构建与
  `--offline --simple-boot-test --allow-multiple --no-windows-assert-ui --log-output` 均以
  退出码 `0` 完成。Release 与 portable 主 EXE 均为 46,575,616 bytes，SHA-256
  `4D7A024F80157E5EB8671B77D6E1817FB960ECE8A8A97D0F470A222E7F7C8E40`；本次启动日志
  `artifacts/evaluation/qgc-simple-boot-20260901-desktop-rerun.log` 为 220 bytes，SHA-256
  `CC8B17466CA7D567A21E56563650BA8A69EDEFB4290A7BFD0857582D86767051`。
- 按当前工作树重新执行完整 goal acceptance，输出
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-desktop-v11.json`
  （14,892 bytes，SHA-256
  `2323A780DA7D62EC3AE267A4871B2D51FB7EBDF757294783FD300336D658158C`）。核心测试收集
  `2319`、QGC custom `66/66`，软件状态为 `pass`，操作员凭据 `ready=true`；现场链路仍
  `field_link_ready=false` / `hardware_status=offline_pending`。
- 将该现场预检作为可选 source 绑定到新的总验收快照
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-desktop-v12.json`
  （23,195 bytes，SHA-256
  `D57149544BFA4CE614F42B8751E93CA5156DFE328D279EF48F793B994FC91F6A`）。快照重新收集
  核心测试 `2319`、QGC custom `66/66`，并记录 `field_link_preflight.provided=true`、
  `verified=true`、`hardware_link_ready=false`、`gate_passed=false`；总体软件状态仍为
  `pass`，未升级硬件门。
- 随后执行有界、只读的现场链路预检，证据为
  `artifacts/evaluation/field-link-preflight-20260901-desktop-rerun.json`（4,982 bytes，
  SHA-256 `0A9C8561E2E91886A1555CE6F2BCE4AF2E1F720DCEC4AC969FFD5561F4A9586B`）。Jetson
  `192.168.144.20:22`、GR01 `192.168.144.11:5760`、相机 `192.168.144.108:554` 均超时，
  本机 `14580/14581` 可绑定；`--require-all` 在首个失败样本后停止，
  `samples_requested=3`、`samples_collected=1`、`hardware_link_ready=false`。该报告只记录
  网络事实，不改路由或发送控制，也不改变 software/SIM 通过状态。
- 这批最新证据只关闭 SIM external runtime 与 AI/target policy 子门；仍明确保持
  `hardware/visual/real_control=not_claimed`，不把合成 perception 升级为相机精度、Jetson、
  室外 GNSS、真实 Pixhawk/执行器、HIL、REAL 或人工 Fly View 验收。版本仍为
  `v0.3.0-pre.8`，最终 `v0.3.0` 发布门不变。

### 签名 live-control 用户级密钥配置器（2026-09-01）

- 新增 `scripts/provision_multidetect_operator_keys.ps1`：使用系统随机数生成至少 32 字节的
  `MULTIDETECT_OPERATOR_KEY` 和固定 32 字节/64 hex 的
  `MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX`，只写 Windows `User` scope。
- 配置器默认复用已有合法值，只有显式 `-Force` 才轮换；读回用户 scope 后重新校验长度和格式，
  输出仅包含创建/复用状态、字节数和 SHA-256 指纹，原始密钥不进入终端、README 或证据文件。
- `-PassThru` 提供机器可读 JSON，执行后需重启终端和 QGC；新增静态回归
  `tests/test_operator_key_provisioning.py` 并通过 Windows PowerShell 解析检查。
- `run_goal_acceptance.ps1` 的 live gate 提示现在直接指向该配置器；凭据准备仍不等价于现场网络、
  室外 GNSS、真实 Pixhawk/执行器或 HIL/REAL 验收。
- 修复 `remaining_live_gates` 在条件项为空时被 PowerShell 序列化为 `{}` 的问题；最新不带
  `-AllowMissingOperatorCredentials` 的快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-key-provisioning-v8.json`
  （23,276 bytes，SHA-256
  `094ECA0E16A33D4C3F81171B6AF2A7366BA296FDE1491DF2912247ED16A464CA`）。该快照收集核心测试
  `2318`、QGC custom `66/66`，凭据 readiness 为 `true`，现场仍为
  `hardware_status=offline_pending`、`hardware_link_ready=false`。

### 联合 SIM 重跑与 QGC 精确窗口捕获稳定化（2026-09-01）

- `scripts/run_px4_gazebo_qgc_combined_sim_acceptance.ps1` 的 QGC 精确客户端抓帧现在对
  Windows 前台切换、Explorer/XAML 瞬时抢焦点和桌面合成时序执行最多 5 次有界重试：每次都
  重新提升并验证同一个 QGC PID/HWND，再重新检查外部顶层遮挡和 5×5 所有权网格；持续遮挡
  仍然 fail-closed，不通过白名单掩盖真实遮挡。Gazebo image、CameraInfo、NavSat 发布器也
  必须连续两次稳定存在后才启动传感器流，避免订阅器启动竞态。
- `vtol_sitl_attached_acceptance.py` 在 ARM/DISARM ACK 后等待新鲜 HEARTBEAT 的 armed bit，
  `vtol_sitl_mission_acceptance.py` 修正 durable journal `resume_plan()` 调用；这两处分别
  收口异步 PX4 ACK 竞态和 Mission journal 恢复摘要错误。
- 当前 portable `v0.3.0-pre.8` 上的 900 帧 Docker PX4/Gazebo/QGC source evidence 为
  `artifacts/evaluation/combined-sim-qgc-20260901-continue-recovery-v9/combined-evidence.json`
  （333,051 bytes，SHA-256
  `75265742413146EA78AB88CCBD30B22ACFCC0C5DC7EC48780F4A9D25F649F7FB`），顶层
  `gate_passed=true`。传感器发布器稳定屏障、RTSP 900/900、QGC 原生 VideoOutput 像素与
  type-24 绑定、toolbar drawer、VTOL `MC→FW→MC→DISARM`、7 项 INT-only Mission、journal
  hash-chain、动态 loopback 端口和 Docker 清理均通过。
- 独立 verifier 输出
  `artifacts/evaluation/combined-sim-qgc-20260901-continue-recovery-v9/combined-evidence-verification.json`
  （2,591 bytes，SHA-256
  `18FDB7E6E5302229D2244075C26998D8D66E7C7EEBD4FCFB24A6A35111761960`），要求 release-mode
  envelope 后 40 项检查全部为 `true`。对应新的 SIM envelope 为
  `artifacts/evaluation/release-mode-sim-20260901-pre.8-combined-dynamic-ports-v3.json`
  （730 bytes，SHA-256
  `ACDFB988D7BE669FFEEC4846BDA6F66837B7892CF402DA1D25B01DB84D92A045`）。
- 新的 pre.8 E7 清单/聚合报告为
  `artifacts/evaluation/release-gate-pre.8-current-20260901-combined-qgc-v5.json`
  （7,887 bytes，SHA-256
  `629237034B84498537C07CB7885BCB6F28D20FCD4E8EEA4CAD5D43ACD9618A6A`）和
  `artifacts/evaluation/release-gate-pre.8-current-20260901-combined-qgc-v5-report.json`
  （15,834 bytes，SHA-256
  `59C6A45CCC08084683485E39F3D38EFE7869645640C51E7427C41E8ADF5958BD`）。SIM、S4 和三机型
  Mission 子门为真；REAL 现场链路仍为 `hardware_link_ready=false`，总报告继续
  `gate_passed=false` / `release_allowed=false`。
- 绑定上述 source/envelope 的最新 goal-acceptance 为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-combined-qgc-v5.json`
  （11,200 bytes，SHA-256
  `E4690933BC325D1CE2828DDAAC63C65689832AB8E1AFD07AB42AA9D397DE2447`）：核心测试收集
  `2315` 项，QGC custom `66/66`，脚本退出码 `0`，软件状态
  `pass_with_environment_gap`、硬件状态 `offline_pending`；它绑定了
  `field-link-preflight-20260901-final-continue.json`（SHA-256
  `59F85CEF567B198E12464B306E74DC36177CB5441F997B41A9034F3728025D54`），三处远端端点仍超时。
  桌面 BitBlt 视频 ROI 仍单独
  记录为 GPU 合成诊断；规范的原生 `videoContent` QQuickItem 证据已通过，二者都没有冒充
  人工视觉验收。
- 这组结果只关闭 software-only `SIM` 子门，不代表真实 Jetson、相机、Pixhawk、室外 GNSS、
  执行器、HIL/REAL 或人工 Fly View 已通过；版本仍为 `v0.3.0-pre.8`，最终 `v0.3.0` 不提前
  发布。

### 室外目标定位真值验收器

- 新增 `multidetect.geolocation_accuracy` 与 `scripts/validate_target_geolocation_accuracy.py`：
  通过 WGS-84 Vincenty 逆解计算目标坐标误差，报告中位数/P95/RMSE、北东偏差和 `1σ/2σ`
  覆盖率。
- 默认要求至少 10 条独立样本；最大 P95 与覆盖率阈值必须显式传入，报告只作为
  `REAL/field` 只读证据，不修改实时坐标、标定文件或飞控路径。
- E7 发布门新增可选的 `real_target_accuracy` 哈希绑定和
  `--real-target-accuracy-report` CLI；聚合器会重算报告样本，校验 WGS-84 指标、真值隔离
  标记和字节摘要。预发布清单可暂缺该绑定，最终 `release` 通道必须提供通过的现场精度报告。
- 新增 `multidetect.geolocation_survey`、`scripts/build_target_geolocation_accuracy.py` 和
  `multidetect-target-accuracy-from-replay`：从已验证的 target-event replay 读取估计坐标，按
  `event_id/frame_id` 严格连接独立真值表，拒绝漏配、重复/冲突目标 ID 以及 stale/unknown GPS；
  报告结构固定到 `configs/schemas/target-geolocation-accuracy-report.schema.json`。
- 本轮回归：全量 pytest 收集 `2296` 项，`2293 passed / 3 skipped`；`ruff`、`compileall` 和
  release-gate/QGC 相关聚焦测试均通过。3 个 skip 是两个 `onnxruntime` 环境缺口和一个
  当前 Windows 账户无法创建 symlink。

### PX4 Mission 事务持久化

- 新增 `src/multidetect/mission_journal.py`：在既有 `Px4MissionTransaction` 外提供
  hash-bound、fsync、append-only JSONL checkpoint，记录 `MISSION_REQUEST_INT` sequence、ACK、
  readback、commit 和 `AUTO.MISSION` start permit；SIM/HIL/REAL 共用同一适配器和任务路径。
- 恢复严格读取已提交前缀。未换行的掉电尾巴、hash-chain/绑定漂移、未知或跳跃状态、legacy
  `MISSION_REQUEST` 和不完整 readback 均保持 `recovery_required`，不会自动合成 ACK、跳过
  readback 或发出 start。`repair_truncated_tail()` 先隔离尾字节，再写入带操作者备注的恢复事件；
  `resume_plan.auto_start_allowed` 固定为 `false`。
- 新增 `configs/schemas/mission-transaction-journal.schema.json`、安装入口
  `multidetect-mission-journal` 和 10 项 journal/适配器回归测试。该能力只补软件事务恢复证据，
  不关闭真实 Pixhawk、Jetson、室外 GNSS、HIL/REAL、人工 Fly View 或最终 `v0.3.0` 发布门。
- `scripts/run_px4_gazebo_qgc_combined_sim_acceptance.ps1` 现在把同一 JSONL journal 传入联合
  SIM Mission helper，完成后在 `vtol_mission_journal`、`hashes.mission_journal` 和
  `artifacts.mission_journal` 三处留下路径/摘要；`simulator_combined_evidence.py` 对新 artifact
  会重新读取 journal、校验 SHA-256、字节数、hash-chain、恢复状态、最终状态和请求 sequence。
  旧的 pre.8 900 帧 artifact 尚未用此字段重跑，历史 33 项 verifier 结果不被改写。

### 三机型共用 Mission transaction smoke

- 新增 `src/multidetect/px4_mission_acceptance.py`，把 VTOL、fixed-wing、plain multirotor
  统一驱动到同一条 `Px4MissionTransaction` + `PersistentPx4MissionTransaction` 路径，逐项记录
  `MISSION_REQUEST_INT`、ACK、readback、commit 和 `START_PERMITTED`。
- 新增 `scripts/run_px4_airframe_transaction_acceptance.py` 与独立 verifier；三类机型各自输出
  journal 文件，verifier 重算字节数/SHA-256、hash-chain、完整 sequence、终态和 identity binding，
  可检测追加尾巴或替换 journal。该 gate 明确是 `SIM`/in-process、`physical_output_enabled=false`，
  不把软件证据升级成真实 Pixhawk、室外 GNSS、HIL/REAL 或人工 Fly View。
- 新增三机型共享路径回归，覆盖成功闭环和篡改 journal 的 fail-closed 行为。
- E7 `release-gate` writer/evaluator/schema 新增可选的
  `px4_airframe_transaction` 哈希绑定与 `--px4-airframe-transaction-evidence` CLI；预发布
  清单可暂缺，最终 `release` channel 会强制重新读取三份 journal、核对 SHA-256/字节数、
  hash-chain、终态、sequence 和 identity binding。新增绑定回归后全量 pytest 收集 `2313`
  项，`2310 passed / 3 skipped`；`ruff`、`compileall`、PowerShell parse 和
  release-gate 聚焦回归均通过。
- 初次绑定时的三机型 artifact 为
  `artifacts/evaluation/px4-airframe-mission-transaction-acceptance-latest.json`（11,971 bytes，
  SHA-256 `84AD6E8FE40795F65FCDAED95779EBA0481ECAE3DF285F537C91577BBD25F684`）；`vtol=7`、
  `fixed-wing=5`、`multirotor=5` 个 Mission item，三份 journal 均为
  `start_permitted`，独立 verifier `gate_passed=true`。
- 初次生成的 pre.8 E7 绑定清单为：
  `artifacts/evaluation/release-gate-pre.8-current-20260901-airframe-transaction.json`（7,584 bytes，
  SHA-256 `FC0B4B159CD784B78D3D1F56D230A1AE709DCF95AF83D944B14579B666CA2920`）。三机型绑定子门
  为真；聚合报告
  `artifacts/evaluation/release-gate-pre.8-current-20260901-airframe-transaction-report.json`
 （15,533 bytes，SHA-256 `A7A9CD90209E9DEAADEF296546CA873D7973CDBB7DF5F97A6CAD9B4687CF9CD1`）仍明确
  `gate_passed=false`/`release_allowed=false`，原因是现场链路未就绪和 prerelease channel。

- 初次绑定轮的整体验收脚本使用 D 盘临时根目录重新执行，避免台式机 C 盘空间影响 pytest：
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-rgb-slam-time-contract.json`
  （15,073 bytes，SHA-256 `5127DEDC1D7C954E819332330F1ED5B15F6D382D4AC7E7E024C00E5E7A0A4622`）。
  该快照收集 2,313 项核心测试、QGC 自定义 66/66，三机型子门为真，
  `software_status=pass_with_environment_gap`、`hardware_status=offline_pending`；
  它不声明 Jetson、Pixhawk、室外 GNSS、HIL/REAL 或人工 Fly View 已通过。

### RGB-SLAM 户外时间一致性收口

- `RgbSlamRangeEstimator` 现在对相机帧、姿态、本地 NED、速度和空速统一执行
  past-only freshness：未来遥测不再通过 `abs(now - observed)` 伪装成新鲜样本；位置失鲜后的
  dead-reckoning 只有在速度/空速时间戳仍在各自窗口内时才会推进基线，否则直接清除该帧的
  RGB-SLAM 量测。重复或倒序相机帧也会在 source-frame 边界直接丢弃，不再重发上一帧三角化
  结果充当新证据。新增未来姿态/位置、陈旧速度/空速和非单调帧序列回归，防止户外时钟 skew
  或传输重试把目标距离投到尚未发生的位置、并被融合层重复加权。
- 本轮针对 RGB-SLAM、导航融合、GPS 质量和 WGS-84 投影的聚焦回归全部通过；该改动只影响
  软件测距输入门，不把无绝对尺度的视觉流升级成室外 GNSS 或 REAL 证据。

- 随后补上非单调 source-frame 回归并重新执行整体验收：重复/倒序帧会被直接丢弃，旧三角化
  结果不会因传输重试再次进入融合。最新快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-rgb-slam-duplicate-contract.json`
 （15,084 bytes，SHA-256 `1E1A7156A24517513759F4640F44B8120A1891F273A3C3112E3E9D65A945C733`），
  收集 `2,314` 项核心测试（`2,311 passed / 3 skipped`），QGC 自定义回归 `66/66`。验收脚本
  重建的三机型 Mission artifact SHA-256 为
  `C60BD72039D2E4DCC7056D47CDD6CB6857047C05C98DDF7204ECE869A8B6030E`；E7 绑定已同步刷新，
  当前绑定清单为
  `artifacts/evaluation/release-gate-pre.8-current-20260901-airframe-transaction.json`
  （SHA-256 `92CA956279C4C5321B5C90FA8FBEE9B8956FA854C883E606F9B65E7D8FE2B4D7`），聚合报告为
  `artifacts/evaluation/release-gate-pre.8-current-20260901-airframe-transaction-report.json`
  （SHA-256 `CAACDE3AFF159101B0CACE91864F6C91B1F3944058F54195B309955F76DC5F60`）。三机型
  Mission 子门和 RGB-SLAM 回归均通过，但整体仍保持现场/HIL/REAL 和最终 release 门的
  fail-closed 状态。

### 现场链路复检（仍保持 fail-closed）

- 使用当前台式机的已知机载网段端点再次执行只读预检：
  `artifacts/evaluation/field-link-preflight-20260901-journal-followup.json`（3,035 bytes，
  SHA-256 `B817AA9CD558EA13255F8B459BE42913DB2F5F63C29A7D88FB6F31C71477700A`）。Jetson
  `192.168.144.20:22`、GR01 `192.168.144.11:5760` 和相机 `192.168.144.108:554` 均超时；
  本机 metadata/QGC UDP `14580/14581` 可绑定。`--require-all` 退出码为 `1`，独立
  verifier 在 `--allow-unready` 下仅确认报告结构和摘要一致性，`hardware_link_ready=false`。
- 该结果只记录当前网络事实，不选择或改写现场端点，也不提升 GNSS、真实 Pixhawk/执行器、
  HIL/REAL 或人工 Fly View 门；接通飞机 Ethernet 后必须用同一脚本、同一端点和协议检查重跑。

### Planned

- 继续以 `v0.3.0-pre.N` 交付多旋翼/固定翼/VTOL 自主任务与数字孪生增量。
- 只有 Replay、SIM、HIL、REAL 的最终验收门全部通过后才发布 `v0.3.0`。

### Verification refresh (2026-09-01, latest external combined SIM)

- 在最新 pre.8 portable EXE 上完整重跑 Docker PX4/Gazebo/QGC 联合验收：
  `artifacts/evaluation/combined-sim-qgc-20260901T074256Z-48728/combined-evidence.json`
  （772,736 bytes，SHA-256
  `4A50E88770FF1B2084A92D72E228C4C1913E49E69BAAFE46BA43B742939D774F`）。`gate_passed=true`，
  900 帧传感器流、VTOL `MC→FW→MC`、7 项 INT-only Mission、Mission barrier、原生 QGC
  toolbar/video machine smoke、动态 loopback 端口保护/释放和 Docker stop/rm 均通过。
- 独立 Combined SIM verifier 输出
  `artifacts/evaluation/combined-sim-qgc-20260901T074256Z-48728/combined-evidence-verification.json`
  （SHA-256 `B2855FD4C7B64DA178676A00B01EFB3E05969681ED01B9DE597D130883D1FCD7`），33/33
  checks 通过；新 SIM envelope 为
  `artifacts/evaluation/release-mode-sim-20260901-pre.8-combined-dynamic-ports-v2.json`
  （SHA-256 `F608493C7F1385D070B8CCAE903444F4028B78FF21AB30152C9C05958BC8ABC0`）。
- 将该 source/envelope 绑定到新的 E7 pre.8 清单/报告
  `artifacts/evaluation/release-gate-pre.8-current-20260901-combined-qgc-v3.json`
  （SHA-256 `8B78B49D7F857051FB171C5A64C2DC6D920990B926004291A7472C9745F0B1D7`）/
  `artifacts/evaluation/release-gate-pre.8-current-20260901-combined-qgc-v3-report.json`
  （SHA-256 `8DECBFB949C2B250311A9489CB781EC7BBA8376FF1D1C2B137F934BA14E67F99`）；SIM/S4
  子门与五组 parity 通过，整体仍因现场链路、REPLAY/HIL/REAL 和 prerelease channel 保持关闭。
- 最新完整 goal acceptance 为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-combined-qgc-v3.json`
  （20,870 bytes，SHA-256
  `64E92ADB8B99033520EE27703BAC442505828E1EA12B3E56D211864C66EC7882`），核心测试
  `2268 passed / 3 skipped`、QGC custom `66/66`，软件/硬件状态仍为
  `pass_with_environment_gap` / `offline_pending`。
- 本轮仍明确保持 software-only 语义：不声明真实 Pixhawk/Jetson/相机、室外 GNSS、执行器、
  HIL/REAL、人工 Fly View 或实飞通过；版本继续为 `v0.3.0-pre.8`。

### Verification refresh (2026-09-01, latest field-link watch)

- 在同一台式机上对默认飞机端点执行 3 次只读协议预检，原始报告为
  `artifacts/evaluation/field-link-preflight-20260901-v3.json`（12,082 bytes，SHA-256
  `C5C4F0405FE9D61E373550FBEE9DB929580838FAEF417BCCEB99E11D19AF0D05`）。本机 `14580/14581`
  均可绑定；Jetson `192.168.144.20:22`、GR01 `192.168.144.11:5760` 和相机
  `192.168.144.108:554` 的 TCP/SSH/MAVLink/RTSP 只读探针全部超时，三次均为失败样本，
  `hardware_link_ready=false`。
- 将该最新现场报告绑定到 v4 总验收：
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-gps-contract-v4.json`（20,864
  bytes，SHA-256 `1EB9D735CEB9FD9400F780074278952EFF81E20990AD83769E2AF6BD11620002`）。核心测试
  收集 `2271`（`2268 passed / 3 skipped`），QGC custom `66/66`，Combined SIM、QML parse、
  协议 self-test、portable smoke 和 parity 校验均通过；固定翼子证据为
  `artifacts/evaluation/fixed-wing-software-acceptance-20260901-gps-contract-v4.json`（SHA-256
  `1EE64A535A1C64FD41A4EA02F3164F5A62514B0ADF9006DF5CBE0C5D9215FF4C`），SIM 安全子证据为
  `artifacts/evaluation/simulator-safety-acceptance-20260901-gps-contract-v4.json`（SHA-256
  `021D627162F99A836AD1C7C7724AF1DB56FF7901F7298381457EA0B91BB32DEC`）。
- v4 仍是 `software_status=pass_with_environment_gap` / `hardware_status=offline_pending`；
  该刷新只更新现场 provenance，不关闭室外 GNSS、真实 Pixhawk/Jetson/相机、执行器、HIL/REAL、
  人工 Fly View 或最终 `v0.3.0` 发布门。

### Verification refresh (2026-09-01, native Fly View GPS/range diagnostics)

- 原生 QGC Fly View 的 toolbar drawer 新增只读 `GPS/GNSS` 与 `RANGE_STATUS` 质量行：无目标时
  也显示定位门原因、GPS/source age、RANGE_STATUS validity/source age/data age；面板复用现有
  QGC theme 和状态源，不创建第二个窗口或 simulator-only HUD，也不产生飞控指令。
- QML 静态契约、QGC custom tests、协议 self-test、portable headless smoke 和完整 Python
  回归均通过；全量 pytest 输出封存为
  `artifacts/evaluation/pytest-gps-contract-v3-final.log`（3,000 bytes，SHA-256
  `40DDFF3936D73E74A53A289CEB031E7906BC2082A0FD60383EA734CFA96A8F40`）。本次回归为
  `2268 passed / 3 skipped`；3 个 skip 是两个 `onnxruntime` 环境缺口和一个 Windows
  symlink 能力缺口。
- v3 版本化总验收快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-gps-contract-v3.json`（20,869
  bytes，SHA-256 `9995028256F43F69C75A27B71B9B1EFDD68AF11BFB5E7DD26BF5A31DFC3556B2`）：核心
  测试收集 `2271`、QGC custom `66/66`、QML parse/protocol self-test/`git diff --check` 和
  Combined SIM 绑定均通过。固定翼子证据为
  `artifacts/evaluation/fixed-wing-software-acceptance-20260901-gps-contract-v3.json`（SHA-256
  `270F3BC8C7DE033F72774ECB851C212C5058ED44F33FDB5E127AD18E87B2D99F`），SIM 安全子证据为
  `artifacts/evaluation/simulator-safety-acceptance-20260901-gps-contract-v3.json`（SHA-256
  `021D627162F99A836AD1C7C7724AF1DB56FF7901F7298381457EA0B91BB32DEC`）。
- QGC portable 主 EXE 已由同一构建重新部署：
  `build/windows-v0.3.0-pre.8-release/portable-v0.3.0-pre.8/bin/MultiDetectGCS.exe`，
  `46,575,616` bytes，SHA-256
  `4D7A024F80157E5EB8671B77D6E1817FB960ECE8A8A97D0F470A222E7F7C8E40`；Release 与 portable
  EXE 字节一致。当前 `configs/release-parity.json` SHA-256 为
  `F711096076A34673874F8F8E4E03BF3CCCEFA269F35C2B30C4C2FEAD05646341`，五组 parity hash
  已在 QML 变更后重新生成。
- 台式机现场预检仍是 `hardware_link_ready=false`，总快照保持
  `software_status=pass_with_environment_gap` / `hardware_status=offline_pending`；本轮不
  关闭室外 GNSS、真实 Pixhawk/Jetson/相机、HIL/REAL、人工 Fly View 或最终 `v0.3.0` 发布门。

### Verification refresh (2026-09-01, canonical GPS quality contract)

- 新增 `src/multidetect/mavlink_gps.py` 作为 MAVLink GPS wire-domain 的唯一常量/归一化入口：
  `GPS_FIX_TYPE` 使用 `3D fix (3)..PPP (8)` 的合法范围，`satellites_visible=255` 明确解释为
  unknown，室外质量门要求已知 `6..254` 颗卫星。SIM/live bridge、Pixhawk、现场会话和自适应
  测距都复用同一组阈值，避免 REAL/HIL/SIM 漂移。
- `GPS_RAW_INT` 现在按原子样本处理：坐标必须落在
  `latitude ∈ (-90, 90)`、`longitude ∈ [-180, 180]`，fix、卫星数、`alt`、`eph` 和时间戳必须
  合法；缺字段、未来时间、降级 fix、未知卫星数或极点坐标会清除 raw GPS freshness，不能拿旧
  坐标和新质量字段拼成可定位样本。`GLOBAL_POSITION_INT` 仍是独立的融合位置源，不会伪造
  raw GPS 质量。
- 共享 `assess_gps_quality()` 将 `position_healthy=true`、`hAcc≤6 m`、`vAcc≤10 m`、
  `GPS age≤1.5 s` 和坐标域检查集中到一个 fail-closed 门；返回策略、目标落图、证据快照和
  回放路径都必须通过该门。新增回归覆盖缺垂直精度、`alt` 缺失、`255` 卫星哨兵、未来时间、
  精确极点和可配置 fix 阈值。
- 本轮版本化总验收为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-gps-contract-v2.json`（20,869 bytes，
  SHA-256 `22612316735D42FC725E1B7B44AC87DEDAF9828198ADA147FA3E1274573EADE8`）：核心测试收集
  `2271`，实际回归 `2268 passed / 3 skipped`，QGC 自定义回归 `66/66`，Ruff、compileall、
  协议/QML/portable smoke 均通过，脚本退出码 `0`。Combined SIM source/envelope 复核通过，
  但现场报告仍为 `hardware_link_ready=false`、`hardware_status=offline_pending`；本轮不关闭
  室外 GNSS、真实 Pixhawk/Jetson/相机、HIL/REAL、人工 Fly View 或最终 `v0.3.0` 发布门。
  全量 pytest 输出为 `artifacts/evaluation/pytest-gps-contract-v2-final.log`（SHA-256
  `40DDFF3936D73E74A53A289CEB031E7906BC2082A0FD60383EA734CFA96A8F40`）。
- 版本化子证据：固定翼软件验收
  `artifacts/evaluation/fixed-wing-software-acceptance-20260901-gps-contract-v2.json`（SHA-256
  `AE7AF70829AC8AD5E8C43AA952ED84B8AABA013E812A145EC29EAB838C95AFA4`），SIM 安全验收
  `artifacts/evaluation/simulator-safety-acceptance-20260901-gps-contract-v2.json`（SHA-256
  `021D627162F99A836AD1C7C7724AF1DB56FF7901F7298381457EA0B91BB32DEC`）。

### Verification refresh (2026-09-01, combined SIM dynamic loopback ports)

- 修复 `scripts/run_px4_gazebo_qgc_combined_sim_acceptance.ps1` 的主机端口生命周期：
  运行时从 `30000..60000` 有界随机分配九个互不重复的 TCP/UDP loopback 端口，分配前同时做
  TCP/UDP bind 探测，并明确排除容器内 PX4 输入 `18570` 和受保护的地面站 UDP `14550`。
  evidence 现在保存 `allocation_mode`、动态范围、完整分配列表和释放/保护端口结果；Docker
  inspect 清理也加入 `20 s` 有界超时，避免 Docker Desktop API 卡住时 wrapper 无界等待。
- Docker Desktop 恢复后用当前 `v0.3.0-pre.8` 源码执行 900 帧组合门：
  `artifacts/evaluation/combined-sim-qgc-20260901T051239Z-76220/combined-evidence.json`
  （772,886 bytes，SHA-256
  `4DE2F6AD236EBE65F97CD9A279A101453FA9C938D45E25B9A0FB2DF0D3B7F4F1`）。
  `gate_passed=true`，集成 SIM/QGC、VTOL attach 和 PX4 Mission 均通过，Docker Server `28.4.0`
  容器 stop/rm 成功，900 帧、Mission-ready 后连续 type-24 barrier 和端口释放均有记录。
- 本次分配的 host 端口为
  `38392, 32511, 46147, 30192, 55621, 30275, 49253, 53375, 31727`；
  `protected_udp_14550_unchanged=true`、`all_test_ports_released=true`。这些端口只属于本次
  disposable SIM，不写入生产端点，也不提升为真实 Pixhawk/Jetson、室外 GNSS、执行器或人工
  Fly View 验收。
- 新的 SIM release-mode envelope 为
  `artifacts/evaluation/release-mode-sim-20260901-pre.8-combined-dynamic-ports.json`，
  SHA-256 `86B6A7B4E220D7E05C8C9E56A678B22B269587AB758D05DE8BBEE855644FD981`；对应 E7
  动态端口/S4 绑定清单和报告分别为
  `artifacts/evaluation/release-gate-pre.8-current-20260901-combined-dynamic-ports-s4-bound.json`
  （SHA-256 `08C3B431C57329855051BDF3D5FF6FD8F8DBE54F8D09E474EB36563ADEF2B13E`）和
  `artifacts/evaluation/release-gate-pre.8-current-20260901-combined-dynamic-ports-s4-bound-report.json`
  （SHA-256 `5A5D7E0435F8906E692ADB3A44D6D7A972A5CCF381AC960B0E92DB3D7B123298`）。报告重新
  校验 SIM/S4 哈希并显示 `simulator_safety.passed=true`、SIM source gate 为真，但整体
  `gate_passed=false`/`release_allowed=false`，因为现场链路、REAL 和最终 release channel
  仍未通过；版本继续为 `v0.3.0-pre.8`。
- SIM 重跑后再次执行只读现场链路预检，报告为
  `artifacts/evaluation/field-link-preflight-20260901-post-sim.json`（2,352 bytes，SHA-256
  `2B3EF846CE463BDEBABAC4F1246AA08D3D1587E356C0AEB470A0196667FACC63`），脚本退出码为 `0`。
  三个默认现场端点仍超时，`14580/14581` 本机 UDP 端口可绑定，报告保持
  `all_passed=false`、`hardware_link_ready=false`；该报告没有绑定成通过的 REAL 证据。
- 绑定该现场预检后刷新总 goal-acceptance：
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-post-dynamic-ports.json`（12,631
  bytes，SHA-256 `BB795D37E03D55FFEC7B4C9EA742BE1F8CC06E81EBCFF4A3475A6E3C52F7738F`）。核心测试
  收集 `2216`，QGC 自定义回归 `66/66`，协议/QML/portable smoke 均通过，脚本退出码为 `0`；
  `software_status=pass_with_environment_gap`、`hardware_status=offline_pending`，现场
  `hardware_link_ready=false`，因此不改变 HIL/REAL、室外 GNSS、人工 Fly View 或最终
  `v0.3.0` 发布门。
- 新增 `src/multidetect/simulator_combined_evidence.py`、
  `scripts/verify_combined_sim_evidence.py` 和 `multidetect-sim-combined-evidence` CLI，
  对 combined SIM 的动态端口（九个、唯一、`30000..60000`、排除 `14550`）、端口释放、
  Docker 清理、嵌套 Mission/SIM gate 以及 hardware/operator-visual claims 做独立
  fail-closed 复核。要求时验证器还会检查 `sim_release_mode_evidence_prepared` 的
  `source.path`/`source.sha256` 是否绑定到同一个源 JSON。
- 当前源证据的独立复核文件为
  `artifacts/evaluation/combined-sim-qgc-20260901T051239Z-76220/combined-evidence-verification.json`
  （SHA-256 `E8440B7B4DDD5981A1A8B7FD279CF210AE016E1858D008B9DE8D5DD507BFFCA2`，33/33
  check 通过）。E7 生成器新增 `--simulator-combined-evidence` 与
  `--simulator-combined-release-mode-evidence`，当前绑定清单/报告为
  `artifacts/evaluation/release-gate-pre.8-current-20260901-combined-dynamic-ports-s4-bound-v2.json`
  （SHA-256 `C026AE1A3209BFE4798DDD600A12EBC4F1684DCE49E871DF5F28D80253D5A188`）和
  `artifacts/evaluation/release-gate-pre.8-current-20260901-combined-dynamic-ports-s4-bound-v2-report.json`
  （SHA-256 `47222082B48FFB8E20AB1264A78CC4C25F6FFBBD9F0321AF17C18E420A65F0FD`）。
  `simulator_combined.passed=true`，但 REAL 现场链路和最终 release channel 仍保持关闭。
- verifier CLI 现在支持 `--out <path>` 写出排序稳定的 JSON 报告；同时将 Combined SIM
  verifier 从 release-gate 聚合器改为懒加载，`python -m multidetect.simulator_combined_evidence`
  不再因为包根导入而产生 runpy 自导入警告。该改动不改变 source、E7 清单或 portable EXE
  摘要。
- `scripts/run_goal_acceptance.ps1` 新增可选的
  `-CombinedSimEvidence`、`-CombinedSimReleaseModeEvidence` 和
  `-RequireCombinedSimEvidence`；总验收 JSON 会保存 `simulator_combined_acceptance` 的
  source/envelope SHA-256、verifier exit code 和完整 checks，默认不传参数时保持兼容。
  `-RequireCombinedSimEvidence` 与 `-RequireFieldLinkPreflight` 在缺少必需路径时现在会
  fail-fast 并返回非零退出码，不再让“required 但未提供”的 binding 继续生成快照。
- `run_goal_acceptance.ps1` 新增 `-FixedWingAcceptanceOutput` 和
  `-SimulatorSafetyOutput`，可把本次固定翼/S4 子证据写入版本化路径，避免带新 provenance 的
  总验收重跑覆盖 `*-latest` 文件；省略参数时保持旧默认路径兼容。
- 使用版本化输出路径完成一次带 Combined-SIM 与现场预检绑定的总验收：
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-versioned-combined-binding.json`
  （SHA-256 `F8E82527E0F048BE8EC60F244EC75E1B96C4FFE5AD49C5DDCAD50EBB81F12559`，核心测试
  `2224`、QGC 自定义 `66/66`）。Combined-SIM 绑定通过，现场 `hardware_link_ready=false`，
  因此软件状态为 `pass_with_environment_gap`、硬件状态为 `offline_pending`，最终 `v0.3.0`
  发布门不变。

### Working-tree delta (2026-09-01, SIM S4 safety evidence)

- 新增 `src/multidetect/simulator/safety_evidence.py`、
  `scripts/run_simulator_safety_acceptance.py`、
  `scripts/verify_simulator_safety_evidence.py` 和
  `configs/schemas/simulator-safety-evidence.schema.json`。该 deterministic SIM 子门沿用
  生产 `UnifiedSelectionTargetPool`、`SimulatorMavlinkControlAdapter`、`SafetyRuleEngine`
  与 `MissionStateMachine`，覆盖 VTOL、固定翼和多旋翼。
- S4 明确验收 `CANCEL_TRK` 的单目标清除与全量取消清空、QGC/PX4 literal-loopback 与消息
  allowlist、watchdog→`FAILSAFE` 后禁止 AI 续控、围栏/GPS/极点坐标 fail-closed，以及
  `TERMINATED` 后拒绝 Mission event。证据 verifier 会重新计算 check，篡改声明字段、计数器、
  坐标、身份或硬件/人工验收 claims 都保持失败。
- `scripts/run_goal_acceptance.ps1` 已把 S4 runner/verifier 纳入总软件验收；证据的硬件、
  人工 Fly View、Pixhawk/Jetson/相机和 REAL 字段继续固定为 `not_claimed`，版本仍为
  `v0.3.0-pre.8`，不提前切换最终 `v0.3.0`。
- 本轮总验收快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-s4-safety-v11.json`，SHA-256
  `38627B36E6522DFFF76ED7B7BFECC0E2C7F795938C1C45FD48CFD25CBFA0BCDE`；核心测试 `2209`、
  QGC 自定义 `66/66`、脚本退出码 `0`。S4 artifact SHA-256 为
  `021D627162F99A836AD1C7C7724AF1DB56FF7901F7298381457EA0B91BB32DEC`，verifier 退出码为 `0`。

### Working-tree delta (2026-09-01, E7 S4 hash binding)

- `src/multidetect/release_gate.py` 与 `src/multidetect/release_gate_manifest.py` 现在支持顶层
  `simulator_safety` 记录：生成器先执行 S4 verifier 并保存相对路径、SHA-256、版本/事件身份、
  六个 check、verifier 事件和退出码；聚合器读取时重新哈希源文件、重新计算 check 并比较声明。
- pre-release 清单可以省略该字段以兼容历史 provenance；一旦显式绑定，文件替换、声明篡改、
  check 漂移或 verifier 失败都会保持 fail-closed；最终 `release` 通道强制要求通过的 S4 绑定。
- `configs/schemas/release-gate-manifest.schema.json`、E7 writer CLI（含
  `--simulator-safety-evidence`/`--sim-safety-evidence`）和 release-gate 回归测试同步覆盖该契约。

### Verification refresh (2026-09-01, E7/S4 integration)

- 在 E7 writer/evaluator 接入顶层 `simulator_safety` 后，重新执行总验收：
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-s5-release-gate-safety-v12.json`
  （SHA-256 `C1FED75A38FAD315AE167FA47457F44D7D882E64A50CA1AFE63DBAE79A795DEA`）。核心测试
  收集 `2215`、QGC 自定义回归 `66/66`，脚本退出码 `0`；S4 runner/verifier 仍为
  `gate_passed=true`/`verifier_exit_code=0`。
- 该重跑确认哈希重算、声明/源 check 对比、schema 和篡改回归均通过；软件状态为
  `pass_with_environment_gap`，现场硬件仍 `offline_pending`，不改变 HIL/REAL/室外 GNSS
  和人工 Fly View 的待验状态。
- 针对坏路径/哈希异常的 fail-closed 健壮性修复后再次重跑，快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-s5-release-gate-safety-v13.json`
  （SHA-256 `20C790F384918B8457CC3A2BA54401B20FF55B7786214D82A2CF385864308134`），核心测试
  `2215`、QGC 自定义 `66/66`、脚本退出码 `0`；S4 绑定通过，现场仍
  `hardware_link_ready=false`/`offline_pending`。
- pre.8 接线快照为
  `artifacts/evaluation/release-gate-pre.8-current-20260901-s4-bound.json`（SHA-256
  `A00EAA2EC6AA8CCEF3A05692998932C77FE060667E42AFA72DD21B8326B448FF`），聚合报告为
  `artifacts/evaluation/release-gate-pre.8-current-20260901-s4-bound-report.json`（SHA-256
  `38357A92648DC07D1597B76AE912F6BCC61930D4B8E13AF18A666260EFDAF224`）；S4 绑定通过，
  但现场链路/REAL 仍保持关闭。
- 绑定最新 `192.168.1.0/24` 只读发现报告后重跑的总验收为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-network-discovery-v14.json`
  （SHA-256 `A6CEAEDFA6AE583B118E8E69BF39102E13FB903A0E1BE0542379970C4C952D25`）；核心测试
  `2215`、QGC 自定义 `66/66`、S4 verifier `0`、脚本退出码 `0`，现场仍
  `hardware_link_ready=false`/`offline_pending`。
- 同一现场报告与 S4 证据装配为新的 E7 pre.8 清单
  `artifacts/evaluation/release-gate-pre.8-current-20260901-network-discovery-s4-bound.json`
  （SHA-256 `D4E9412EED3F6CF2EA8E6DB11D1F81AC4A35826B7F343C682C4EFEAF619E631D`），聚合报告
  SHA-256 `98BD437EEBEB0383901403B377E3B04C69D4DDDF2EAADDDC2AA97DCDAA52E66A`；S4 通过，
  现场链路未就绪，`release_allowed=false`。

### Working-tree delta (2026-09-01, release-mode evidence envelopes)

- 新增 `src/multidetect/release_mode_evidence.py`、
  `scripts/create_release_mode_evidence.py` 和
  `configs/schemas/release-mode-evidence.schema.json`。该窄适配层把一个明确指定的
  REPLAY/SIM/HIL/REAL runner JSON 封装为 deterministic、SHA-256 绑定的 mode envelope，保留源
  路径、事件、运行模式、`gate_passed` 与 projection 原因；缺失/非布尔/`false` 源 gate 永远
  fail-closed，缺失或不匹配的 runtime identity 必须显式 opt-in。
- 四个 runner 现在显式输出 transport/software gate：`integrated-replay`、
  `integrated-replay-qgc`、`integrated-sim-qgc` 和 fixed-wing software acceptance 均写入
  `gate_passed=true`；这些字段只代表各自 runner 的软件/传输闭环，不替代人工 Fly View、硬件
  或 REAL 控制验收。
- 补充 wrapper 直执行、缺失 gate、模式 mismatch、schema 契约和 deterministic bytes 回归。
  `tests/test_release_mode_evidence.py` 与现有 release-gate/runner 测试保持通过；安装 package
  后新增 `multidetect-release-mode-evidence` console entry，clean checkout wrapper 与它共用
同一个 `main()`。

### Working-tree delta (2026-09-01, combined SIM Mission frame budget)

- `scripts/run_px4_gazebo_qgc_combined_sim_acceptance.ps1` 的默认传感器帧数从
  600 提高到 900。600 帧本身的 RTSP/type23/type24 传输是完整的，但在
  `mission-active-ready.json` 写入时可能正好耗尽，无法满足门禁要求的
  Mission-ready 后再前进 5 个原生 QGC type-24 序列；900 帧仍是有界运行，
  并为该证据留出确定性余量。
- 同步更新 README、自治架构运行命令和 PowerShell 静态契约测试；历史 600
  帧 evidence 原样保留，不被改写为新的运行结果。

### Verification refresh (Docker live SIM AI, 2026-09-01)

- Docker Desktop 运行时预检重新发现本机实际的 Docker client/daemon `28.4.0` 和运行中的
  WSL `docker-desktop`：`artifacts/evaluation/simulator-runtime-preflight-20260901-090345.json`
  （SHA-256 `E529B8EBC1A7695C3563603C7DFE666922394BC262FB78090FF25FC28FAE892E`）。native
  `px4`/`gz` 仍不在 PATH，但 `execution_mode=docker`、`ready_to_attempt=true`。
- 使用固定 digest 的 `px4io/px4-sitl-gazebo` 镜像和 `--execute --exercise-arm` 重跑共享
  telemetry/AI bridge，VTOL、fixed-wing、multirotor 三个场景全部通过：
  `simulator-docker-ai-acceptance-20260901-090608-arm-exercise.json`（SHA-256
  `9E1901FEFC3968EFE8C39B874158AAEB5CD4A3AA5E633AD9E018FEC3D35D2BF4`）、
  `simulator-docker-ai-acceptance-coastal_patrol_fixed_wing_001-20260901-090751-arm-exercise.json`
  （SHA-256 `DAF53226EE8F8C305BE487F87CA21B50CE17937D02760FF948C84E155BB5292E`）和
  `simulator-docker-ai-acceptance-urban_inspection_multirotor_001-20260901-090751-arm-exercise.json`
  （SHA-256 `59F11F3937DB80C5594F45DC0687F1C9370EAFF5BEC8181A635B5FF393E0D37C`）。三份
  evidence 都记录 `gate_passed=true`、`runtime_gate_passed=true`、`telemetry.qualified=true`、
  `armed_observed=true`、`ai_policy_gate_passed=true`、`ai_steps=1`、disarm 与容器清理成功。
- 该 live run 只证明无目标 telemetry 下的安全 `hover/no_target` policy step 和共享
  `SimulatorMavlinkControlAdapter` 输出；相机目标识别/track 仍由 deterministic perception/
  interactive fixture 覆盖，不能升级为视觉、真实 Pixhawk、HIL/REAL 或人工 Fly View 验收。
`--execute` 未显式 `--exercise-arm` 时继续等待首个 armed heartbeat 并 fail-closed，这是
  bridge 的预期参数语义。版本保持 `v0.3.0-pre.8`，最终 `v0.3.0` 发布门不变。

### Working-tree delta (2026-09-01, live synthetic perception target seam)

- `SimulatorTelemetryBridge` 新增 `ingest_perception()`：感知输入只允许更新目标可见性、相对
  方位、距离、置信度四元组，不能覆盖 PX4 提供的 GPS、armed/link、姿态、速度、高度或电量。
  目标帧按默认 `1.5 s` lease 做 future/stale/out-of-order 校验；租约到期在下一个 policy
  snapshot 前原子清空，避免旧目标持续产生控制输出。新增 evidence counters、最近感知帧和
  拒绝原因，并补充伪造导航字段隔离、过期清除和时间门测试。
- `run_simulator_docker_ai_acceptance.py` 增加显式 `--exercise-target`（必须同时使用
  `--execute --exercise-arm`）。夹具通过同一个 perception→executor→authority→loopback
  `MANUAL_CONTROL` 链路注入一帧 truth-free 目标并要求 `TRACK/accepted=true`；默认路径仍是
  无目标 `hover/no_target`，不会把合成目标写成相机视觉准确率或硬件证据。
- 进一步收紧拒绝路径：`SimulatorTelemetryBridge._reject_perception()` 现在统一清空 perception
  所有权下的目标 tuple。未来、stale、out-of-order 和 malformed 帧都不能让旧目标跨拒绝事件
  继续存活；PX4 vehicle telemetry 不受影响。新增已有目标被三类时间门拒绝后的清空回归，避免
  只验证“返回 None”却漏掉缓存状态。

### Verification refresh (live SIM target policy, 2026-09-01)

- VTOL `river_rescue_vtol_001`：
  `artifacts/evaluation/simulator-docker-ai-target-river_rescue_vtol_001-20260901.json`
  （SHA-256 `5BD19143E24272243961D36202804275CDB4A1F88027AF05500BB9EC6CF9B50A`）；
- fixed-wing `coastal_patrol_fixed_wing_001`：
  `artifacts/evaluation/simulator-docker-ai-target-coastal_patrol_fixed_wing_001-20260901.json`
  （SHA-256 `4FB9B384FBA588603BB9AF166E43AF368644F9B35D531D3556FAA16223E5833A`）；
- multirotor `urban_inspection_multirotor_001`：
  `artifacts/evaluation/simulator-docker-ai-target-urban_inspection_multirotor_001-20260901.json`
  （SHA-256 `9E13005E7D722018015B6048DB6F6F3885CF78A6BFB5CFF747C626056A4119AC`）。

三份 disposable Docker/PX4/Gazebo evidence 均为 `gate_passed=true`、`runtime_gate_passed=true`、
`target_policy_gate_passed=true`，目标决策为 `TRACK` 且 `accepted=true`，并完成 disarm 与
stop/rm 清理。它们只证明实时 telemetry 与合成 truth-free perception 的生产策略闭环；
`visual_accuracy`、相机/Jetson、室外 GNSS、真实 Pixhawk/执行器、HIL/REAL、人工 Fly View
和实飞仍为 `not_claimed`，版本保持 `v0.3.0-pre.8`。

### Verification refresh (full goal acceptance after live target policy, 2026-09-01)

- 最新完整 pre.8 goal-acceptance 快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-target-ai.json`（SHA-256
  `8BF5B9B39402196A3F31CBA42C3E966076A90599AD21853E7E6D00D76A9A904A`）。脚本退出码 `0`，
  核心测试收集 `2173`、QGC 自定义测试 `66`，软件状态为
  `pass_with_environment_gap`，硬件状态仍为 `offline_pending`；现场 preflight
  `hardware_link_ready=false`，人工 Fly View `not_claimed`。
- 快照绑定当前 portable `MultiDetectGCS.exe` SHA-256
  `61F16B6A13247316E356C7B626E79BB1033A6AC03C54D4DD676F75BCCD4CE497`，以及本轮
  `ingest_perception()`、目标租约和 Docker live target evidence。新增 SIM policy 子门已
  通过，但 Jetson/室外 GNSS/真实 Pixhawk/执行器/HIL/REAL/人工视觉仍未通过；版本继续为
  `v0.3.0-pre.8`，最终 `v0.3.0` 不发布。

### Verification refresh (perception rejection fail-closed, 2026-09-01)

- 最新完整 pre.8 快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-target-ai-v2.json`（SHA-256
  `5D2484A327587D914570416E2BC74227D34E6FFACE60AB6184941D0D2CA8DA78`）。goal-acceptance
  退出码为 `0`，核心测试收集 `2176`、QGC 自定义测试 `66`，Ruff、compileall、QML 解析、协议
  自测和 portable smoke 均通过。
- 快照仍为 `software_status=pass_with_environment_gap`、`hardware_status=offline_pending`，
  现场 `hardware_link_ready=false`、人工 Fly View `not_claimed`；该刷新只记录 perception
  rejected-frame 清空语义，不改变 `v0.3.0-pre.8` 版本，也不关闭 Jetson、室外 GNSS、真实
  Pixhawk/执行器、HIL/REAL 或最终发布门。

### Verification refresh (shared perception bridge delegation, 2026-09-01)

- `SimulatorPerceptionBridge(telemetry_bridge=...)` 现在把 production `feed_result()` 委托给
  同一个 `SimulatorTelemetryBridge.ingest_perception()`，因此绑定 bridge 的 SIM/HIL/REAL
  感知不会绕过 heartbeat、armed、target-lease、watchdog 或 authority 门；bridge 拒绝的
  帧在返回 runner 前原子清空目标四元组，并暴露明确的
  `bridge_rejection_reason`。新增共享入口集成回归覆盖 accepted、future/stale/out-of-order
  rejected 和目标清空路径。
- 完整 pre.8 goal-acceptance 刷新为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-target-ai-v3.json`（SHA-256
  `A49DDB605FD90AA04BBFCA7185ECB600FACA52E3BAF3185F262C2B9DE6F3D560`）。脚本退出码 `0`，
  核心测试收集 `2178`、QGC 自定义测试 `66`，Ruff、compileall、QML 解析、协议自测和
  portable smoke 均通过；软件状态仍为 `pass_with_environment_gap`，硬件状态
  `offline_pending`，现场 `hardware_link_ready=false`、人工 Fly View `not_claimed`。
- 共享入口修复纳入 canonical `configs/release-parity.json`，当前
  `core_algorithm_hash=71e59bb128a652d67827aa3da615af4b8f8af8f0ad8a6358cf7acc6ab16bcd63`。
  Python-only 改动不需要重编 QGC；portable EXE SHA-256 仍为
  `61F16B6A13247316E356C7B626E79BB1033A6AC03C54D4DD676F75BCCD4CE497`，版本继续为
  `v0.3.0-pre.8`，最终 `v0.3.0` 发布门不变。

### Working-tree delta (2026-09-01, perception range freshness seam)

- `SimulatorPerceptionBridge` 现在复用 operator-link 的 `MAX_RANGE_STATUS_SOURCE_AGE_S`
  （默认 `1.5 s`）做显式测距新鲜度门。`RangeSolution` 的
  `data_freshness_s`/`freshness_s`/`age_s` 与 `RangeStatusMessage` 的
  `source_captured_at_s`/`captured_at_s`/`observed_at_s`/`timestamp_s` 都会校验为有限、非负、
  非未来且不超过阈值；不再允许 adapter 把旧测距当成当前距离送进共享 AI/控制链路。
- 过期、未来或显式缺失源时间的测距样本保持视觉 track 可见，但清空距离、方位和测距置信度，
  并留下 `range_data_stale`、`range_source_stale`、`range_source_future` 或
  `range_source_timestamp_missing` 原因；负数/非有限年龄按 malformed 输入计数并抛出。
  evidence 记录阈值、字段别名和 fail-closed 契约，回放与现场诊断可直接核对。
- 新增 stale/future/missing/negative range-age 回归后，完整 pre.8 goal-acceptance 刷新为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-target-ai-v4.json`（SHA-256
  `D4B0EE2E7F274B0904DE2CDB60AC42DA4195CFAFE31D1774418A8180E46DE5CF`）。脚本退出码 `0`，
  核心测试收集 `2183`、QGC 自定义测试 `66`；软件=`pass_with_environment_gap`、硬件=
  `offline_pending`、现场 `hardware_link_ready=false`、人工 Fly View=`not_claimed`。
- canonical `configs/release-parity.json` 已同步（文件 SHA-256
  `CB51D7CDD1AC0CA0D52F21241C88A2630B50D18DAFD88A0B9D4FE01185728BD2`，
  `core_algorithm_hash=6f224eb199fd53979648f477e364f30c318c303b6a97d303eae548948f9290f0`）。
  这是 Python-only 修复，不需要重编 QGC；portable EXE SHA-256 仍为
  `61F16B6A13247316E356C7B626E79BB1033A6AC03C54D4DD676F75BCCD4CE497`，版本保持
  `v0.3.0-pre.8`。

### Working-tree delta (2026-09-01, SIM AI return GPS freshness seam)

- `SimulatorAiPolicy` 的返航/低电量路径现在单独校验 `gps_observed_at_s`：时间戳必须存在、
  不得来自未来，且年龄不超过 `maximum_return_gps_age_s=1.5 s`。持续的心跳或融合位置更新
  不会再刷新 raw-GPS 游标；缺失、过期和未来 raw GPS 统一保持
  `return_gps_quality_insufficient` / `gps_unqualified_for_return` fail-closed。
- 新增 fresh-boundary、missing、stale、future 与配置边界回归；
  `scripts/run_simulator_ai_acceptance.py` 的共享 SIM AI 闭环重新退出 `0`。
- 完整 pre.8 总验收刷新为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-field-recheck-v5.json`（SHA-256
  `7EAE02CC318160650673508692C8D5F5C6636ADBADA1ACED2BBF098661FAE70`），核心测试收集 `2183`、
  QGC 自定义测试 `66`，软件=`pass_with_environment_gap`、硬件=`offline_pending`，现场
  `hardware_link_ready=false`、人工 Fly View=`not_claimed`。
- 将 `src/multidetect/simulator/ai_executor.py` 纳入 canonical `core_algorithm` parity；
  `configs/release-parity.json` SHA-256 为
  `93269062DFCD4D2EA01E31AA8EE49F0FB4A074B0F8FF7DC63AB3979AD4AD9F70`，
  `core_algorithm_hash=da5fe6ff7d1455b85c51bf975e14f2da007e603de5aed69b93470cd3a31de006`。
  这是 Python-only 改动，portable EXE 保持原 SHA-256，版本继续为 `v0.3.0-pre.8`。

### Verification refresh (GPS return freshness acceptance, 2026-09-01)

- 在返航新鲜度门落盘后重跑完整 pre.8 验收，快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-gps-return-freshness-v6.json`
  （SHA-256 `B43DC058C34A7ACEC7E489DEF0BFC55FD8D9A7348FB30D2F5B3F54B256F4189C`）。
  核心测试收集 `2188`、QGC 自定义测试 `66/66`，脚本退出码 `0`。
- 快照绑定的现场协议预检是三采样、协议检查模式，报告结构验证通过但
  `hardware_link_ready=false`；因此 `software_status=pass_with_environment_gap`、
  `hardware_status=offline_pending`，Jetson/GR01/相机、室外 GNSS、HIL/REAL 和最终
  `v0.3.0` 发布门继续保持 pending。

### Verification refresh (bounded field-link discovery after desktop migration, 2026-09-01)

- 在显式 `192.168.1.0/24` 范围内运行只读、有界的协议发现，证据为
  `artifacts/evaluation/field-link-discovery-20260901-continue-protocol.json`（SHA-256
  `C4ECDCCE5CF1B35A1E07D80299692632D2FE56135962F291056509D4DA1061D1`）。扫描 `254` 个地址，
  仅 `192.168.1.1`、`192.168.1.62`、`192.168.1.77` 返回 SSH identification；MAVLink `5760`
  与 RTSP `554` 均没有协议命中。
- 发现结果是诊断信息，不把任意 SSH 主机当成 Jetson，也不会替换显式 field-link 配置或
  提升 `hardware_link_ready`。飞机网接通后仍需对确认过的 Jetson/GR01/相机地址运行
  `run_field_link_preflight.py --protocol-checks --samples 3 --require-all`；版本继续为
  `v0.3.0-pre.8`。

### Verification refresh (3-D control/asset gate for all vehicle classes, 2026-09-01)

- 按当前 pre.8 源码分别重跑 VTOL `river_rescue_vtol_001`：
  `artifacts/evaluation/simulator-3d-control-acceptance-20260901-river_rescue_vtol_001-continue.json`
  （SHA-256 `1D4F90E0C631BCB42A7F9DF5BD6F9C4B7468CD37AAA891BF96AF8CF11DD64177`）；fixed-wing
  `coastal_patrol_fixed_wing_001`：
  `artifacts/evaluation/simulator-3d-control-acceptance-20260901-coastal_patrol_fixed_wing_001-continue.json`
  （SHA-256 `FB879AE4F627474B24D3397739A6E3ECC60F92891CCF671CC846414D02697982`）；multirotor
  `urban_inspection_multirotor_001`：
  `artifacts/evaluation/simulator-3d-control-acceptance-20260901-urban_inspection_multirotor_001-continue.json`
  （SHA-256 `84419C67A853D6467A900CC76154EE1699052123B456C0C1D0BC66B006ABFCFE`）。
- 三份报告均为 `gate_passed=true`、`deterministic_gate_passed=true`，手控/AI 共用 authority、
  watchdog、loopback 与 repository-owned 资产哈希均通过；本机 `px4`/`gz` 不可用，
  `external_runtime_gate_passed=false`，所以视觉、真实硬件、室外 GNSS、HIL/REAL 和人工
  Fly View 仍保持 `not_claimed`/`offline_pending`。版本继续为 `v0.3.0-pre.8`。

### Verification refresh (Docker external runtime + live AI target, 2026-09-01)

- Docker Desktop daemon 恢复后，使用显式 Windows Docker 客户端和固定镜像
  `px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`
  重新执行三种机型的 disposable PX4/Gazebo runtime gate。VTOL、fixed-wing、multirotor
  evidence 分别为：
  `artifacts/evaluation/simulator-docker-acceptance-20260901-continue-vtol.json`
  （SHA-256 `391BDA5D7832F08174B8260D9B1B43ADC6BB5FB3E67E82AC5EE6F1859905E16D`）、
  `artifacts/evaluation/simulator-docker-acceptance-20260901-continue-fixed-wing.json`
  （SHA-256 `FC20E7A241E53A7BBE82B4498D86BD06D2AA614D4AD44A1AD57464A3A11F4A22`）和
  `artifacts/evaluation/simulator-docker-acceptance-20260901-continue-multirotor.json`
  （SHA-256 `D3CA94B17C3A1461E059D078020BFA1010930257A76A18CE28FA8935040BD75F`）。三份
  evidence 均为 `gate_passed=true`、`preflight_gate_passed=true`、`runtime_gate_passed=true`、
  `telemetry.qualified=true`，并在退出时完成容器 stop/rm 清理。
- 同步生成的 runtime-preflight 为
  `artifacts/evaluation/simulator-runtime-preflight-20260901-continue.json`（文件 SHA-256
  `0B304D5CCEE184AC25747BD85B08D2310D4A764DF6D253E0C150B9F48746D5D6`），记录
  `execution_mode=docker`、`docker_daemon_ready=true`、`ready_to_attempt=true`；native
  `px4`/`gz` 仍不在 PATH，故外部入口固定走 Docker Desktop。
- 同一镜像再以 `--execute --exercise-arm --exercise-target` 跑 live AI target acceptance。
  三种机型的 evidence 分别为：
  `artifacts/evaluation/simulator-docker-ai-acceptance-20260901-continue-vtol.json`
  （SHA-256 `C60E759BC99E5BF9D5190CE711B9B93F219880A7A7ED2534D797E394A3CC1B57`）、
  `artifacts/evaluation/simulator-docker-ai-acceptance-20260901-continue-fixed-wing.json`
  （SHA-256 `99FBF23B71A94A21A9F6EB34A43D7737FEC651DAF5E3E267E837C7A0F0CB4032`）和
  `artifacts/evaluation/simulator-docker-ai-acceptance-20260901-continue-multirotor.json`
  （SHA-256 `0633659951E05C9BD5384251AE95D7D834FEE076CE782D6197CF79167A6D032A`）。三份
  evidence 都记录 `ai_policy_gate_passed=true`、`target_policy_gate_passed=true`、
  `ai_steps=2`、`ai.armed_observed=true`、`target_exercise.accepted=true`、
  `decision.action=track`，并完成 disarm 与容器清理。
- 这批证据关闭的是 SIM external runtime 与 AI/target policy 子门；输入目标仍是 truth-free
  合成 perception，不等同于 QGC 人工 Fly View、相机识别精度、Jetson、室外 GNSS、真实
  Pixhawk/执行器、HIL 或 REAL。版本继续为 `v0.3.0-pre.8`，最终 `v0.3.0` 发布门不变。
- 同一版本随后执行增量 Windows Release/portable 构建，Ninja 返回 `no work to do`，脚本退出码
  `0`；portable `--offline --simple-boot-test --allow-multiple --no-windows-assert-ui --log-output`
  退出码也为 `0`。启动日志为 `artifacts/evaluation/qgc-simple-boot-20260901-continue.log`
  （SHA-256 `8D6A33F8DE158EEED2E9AB526645953C391F2BD4BB584A8F0917012CC5FDA4DB`）；Release 与
  portable 主 EXE 保持 `46,575,104` bytes、SHA-256
  `61F16B6A13247316E356C7B626E79BB1033A6AC03C54D4DD676F75BCCD4CE497`。

### Working-tree delta (SIM external runtime/AI evidence join, 2026-09-01)

- 新增 `src/multidetect/simulator_external_evidence.py`、
  `scripts/verify_simulator_external_evidence.py`、
  `configs/schemas/simulator-external-evidence.schema.json` 和
  `tests/test_simulator_external_evidence.py`。该 join 点对 VTOL、fixed-wing、multirotor 的
  runtime/AI 六份 JSON 做 fail-closed 哈希绑定，复核当前 `configs/release-parity.json`，并
  要求同一 digest-pinned PX4/Gazebo 镜像、同一场景 manifest、pre.8 版本、GPS 质量、truth-free
  感知、硬件控制关闭、AI target/armed/track 和容器清理。它是纯文件校验，不启动任何外部系统。
- 当前 join artifact 为
  `artifacts/evaluation/simulator-external-runtime-ai-evidence-20260901-pre.8.json`，SHA-256
  `43D34D1E7697CDB950816B9C1057104EB4F90820C60AF1A5163B081174BC3632`，`gate_passed=true`；
  parity 文件 SHA-256 为
  `93269062DFCD4D2EA01E31AA8EE49F0FB4A074B0F8FF7DC63AB3979AD4AD9F70`。
- 新增五项回归覆盖三机型 join、镜像漂移、未执行 target、机型集合和原子写入/重载。
  该结果只关闭 SIM external runtime 与 AI/target policy 子门，不提升人工 Fly View、室外
  GNSS、真实 Pixhawk/Jetson/相机、执行器、HIL/REAL 或最终 `v0.3.0` 发布门；版本仍为
  `v0.3.0-pre.8`。
- join 后的完整 goal acceptance 快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-s3-faults-v10.json`（SHA-256
  `A3E2CC98F3B3117D3C36E1FEF15DAA2E9CAB41E4ADB31352FA0397CA74BFEDE5`），脚本退出码 `0`，核心
  测试 `2200`、QGC 自定义 `66/66`；portable simple-boot 日志
  `artifacts/evaluation/qgc-simple-boot-20260901-external-join-v8.log`（SHA-256
  `B329814985CF63C04460E2F8DD9872CFCF2A96D48DB96DF051564578287F0F91`）也退出 `0`。软件仍为
  `pass_with_environment_gap`、硬件仍为 `offline_pending`，版本保持 `v0.3.0-pre.8`。

### Working-tree delta (SIM fault timeline hardening, 2026-09-01)

- `FaultKind`/`scenario-runtime-input.schema.json` 新增 `timestamp_skew`、`target_drift` 和
  `target_bearing_drift`。`ScenarioRunner` 在 truth-free observation 层对 sensor freshness 与
  target range/bearing 注入确定性扰动：共享 `observed_at_s` 不变，负偏移跨过 t=0 转成
  unavailable，正偏移保留 future cursor，距离在 0 m 饱和，方位归一到 `[-180, 180)`；所有
  故障仍走同一 AI/authority/MAVLink 路径，不产生第二控制通道。
- 新增 scenario-runner 回归覆盖 future/stale GPS 均触发同一 `gps_unqualified_for_return` 门、
  不产生负时间戳终止，以及距离/方位漂移、±180° wrap 和 `active_frames` 证据。该改动只
  增强 SIM/S3 软件故障注入，未提升 truth-free、人工 Fly View、室外 GNSS、真实 Pixhawk、
  HIL/REAL 或最终 `v0.3.0` 发布状态；版本仍为 `v0.3.0-pre.8`。

### Verification refresh (full goal acceptance after Docker AI and discovery, 2026-09-01)

- 最新完整 goal-acceptance 快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-docker-ai-discovery.json`（SHA-256
  `0FAA573E8AEDD83AC5A64E5E76B448FC4219D3D37FC560CF111B86FD85044B3C`）。脚本退出码 `0`，
  核心测试收集 `2167`、QGC 自定义测试 `66`，`software_status=pass_with_environment_gap`，
  `hardware_status=offline_pending`，并绑定当前 portable EXE SHA-256
  `61F16B6A13247316E356C7B626E79BB1033A6AC03C54D4DD676F75BCCD4CE497`。
- 该快照显式绑定现场 preflight 的 `hardware_link_ready=false`；运行时使用
  `-AllowMissingOperatorCredentials` 仅保留软件迁移验收，不代表 Windows 运行密钥、Jetson、
  室外 GNSS、真实 Pixhawk/执行器、HIL/REAL 或人工 Fly View 已通过。版本继续保持
  `v0.3.0-pre.8`。

### Verification refresh (bounded field-link discovery rerun, 2026-09-01)

- 在当前台式机 `192.168.1.13/24` 上重新执行显式 `192.168.1.0/24` 只读发现，证据为
  `artifacts/evaluation/field-link-discovery-20260901-continue-subnet.json`（SHA-256
  `89F80BD5BD1276D28378484481ACBAF3F8C1865FB12D8605903250FF2D71EBC7`）。扫描 `254` 个主机，
  仅发现三个 SSH banner；MAVLink TCP/RTSP 分别为 `0` 个命中。发现器只识别服务，不登录、
  不请求视频/遥测、不发送飞控数据，也不会选择或改写 Jetson/GR01/相机端点。
- 该现场报告已绑定到总验收快照
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-subnet-discovery-v7.json`
  （SHA-256 `38AE6A318E53586DBC3ABA33BA4CAA3F4B5C5357BE4697ECC3C1A896CB55D0DB`）。由于
  `hardware_link_ready=false`，硬件、室外 GNSS、HIL/REAL 及最终 `v0.3.0` 发布门仍保持关闭。
- 基于当前 `configs/release-parity.json` 重建四模式 pre.8 清单并绑定该现场报告：
  `artifacts/evaluation/release-gate-pre.8-current-20260901-subnet-discovery.json`
  （SHA-256 `93C15C0BE48EF4EAEB1ED259A62321BC4F69998B5B13CD61801594812ABC77EB`），聚合报告
  `artifacts/evaluation/release-gate-pre.8-current-20260901-subnet-discovery-report.json`
  （SHA-256 `0416E67F56EE143FC903446E8D18DCEDC2925D0EECE810F939321D1BC5A41BF5`）。五组 parity
  一致，但 `gate_passed=false`/`release_allowed=false`，因为现场、REPLAY 标注/精度、人工
  Fly View、HIL/REAL 和最终 release channel 仍未满足；该清单不改变 prerelease 状态。

### Working-tree delta (2026-09-01, field-link evidence verifier hardening)

- `verify_field_link_report(...)` 现在按 probe 列表重新计算单次报告的
  `all_passed`/`hardware_link_ready`，并在 watch 报告逐样本执行同一一致性检查；手工把摘要改成
  `true` 但保留失败端点时会 fail-closed，不会升级 REAL 门。
- 校验器读取 `utf-8` 与 Windows 常见的 `UTF-8 BOM` 两种 JSON，同时继续按原始字节计算和返回
  SHA-256，因此 BOM 兼容不改变证据绑定。新增 BOM 与摘要篡改回归；定向现场/发布门测试、Ruff、
  compileall 及完整 Python 回归均通过（`2164 passed / 1 skipped`；唯一跳过项仍是该 Windows
  账户无法创建 symlink）。

### Verification refresh (four-mode release-gate assembly, 2026-09-01)

- 先前装配的 `release-gate-pre.8-current-20260901.json` 与其报告保留为历史 provenance；
  随后的 SIM rerun 清单/报告也作为中间 provenance 保留：
  `artifacts/evaluation/release-gate-pre.8-current-20260901-sim-rerun.json`（SHA-256
  `923F78F3CD9A0444B601A448BE1C5E8E08554FC1DBB6E43BDC949461ACDD20CC`），聚合报告为
  `artifacts/evaluation/release-gate-pre.8-current-20260901-sim-rerun-report.json`（SHA-256
  `18E950A83661F0C48738F8C24C4E43700C8932F57D3318071E13164CEACB907A`）。当前权威清单/报告
  已在后续现场链路 rerun 段落中更新为 `-field-rerun` 文件。
- 本轮 SIM envelope 为
  `artifacts/evaluation/release-mode-sim-20260901-pre.8-rerun.json`（SHA-256
  `9862E26984487822DF9DCCB1D0673889C28ACD50DE1DFB02385AC829E11D516F`），绑定的源证据为
  `artifacts/evaluation/combined-sim-qgc-20260831T234111Z-53232/combined-evidence.json`，
  SHA-256 为 `5194CC77F45572483B6A17A21FC8FC0DBE222BE62B9A8EB1CC14777C1B464F6C`；该
  900 帧 combined PX4/Gazebo/QGC run 的传感器 readiness、VTOL、PX4 Mission、原生 QGC
  QQuickItem 抓帧和清理机器门均通过，源 `gate_passed=true`。
- 报告中五组 parity 全部一致，但人工视觉、Mission 状态、HIL 硬件/real-control、REAL 全部
  状态仍保持 `not_claimed`，现场链路 `hardware_link_ready=false`，`replay_set` 尚未绑定，
  因此四模式聚合 `gate_passed=false`、`release_allowed=false`。版本继续为
  `v0.3.0-pre.8`，没有提前生成最终 `v0.3.0`；机器 QQuickItem 证据不替代人工 Fly View
  点验、室外 GNSS、真实 Pixhawk/执行器或 HIL/REAL。
- 随后对当前 `192.168.1.0/24` 做了限定网段的只读发现，证据为
  `artifacts/evaluation/field-link-discovery-20260901-continue.json`（SHA-256
  `97F0A7FF64DB50C448DD5923D57617CFA8BFAAAEAAD02C46EE4D8D6062DF3273`）。254 个地址中仅
  3 个 SSH 端口响应，MAVLink `5760` 和 RTSP `554` 命中均为 `0`；发现结果不选择端点，
  `hardware_link_ready` 继续为 `false`。接通飞机专用网段或提供实际机载 IP 后再做显式
  Jetson/GR01/相机预检。
- 现场报告校验器与 PowerShell 空参数修复后的最新完整 goal-acceptance 快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-field-verifier-hardening-v2.json`
  （SHA-256 `A0FE03A238DF5CBC84E513D560C6FFE64CFE790679D0D85DE02C00B9AD069217`）。该快照记录
  `core_tests_collected=2166`、QGC 自定义 `66`、QML/C++/portable smoke 全通过，软件状态为
  `pass_with_environment_gap`、硬件仍为 `offline_pending`；没有把空现场链路或凭据覆盖写成通过。
- 该次 acceptance 刷新了 fixed-wing software evidence，因此重新封装 HIL envelope
  `artifacts/evaluation/release-mode-hil-20260901-pre.8.json`（SHA-256
  `4F905FBC6221A5A7442DAA8F9507B427F7ED3782585A62625E04AA8C030BCDDA`）；随后同步重建当前
  E7 清单/报告，避免 HIL 源文件 SHA-256 漂移造成隐性失配。

### Verification refresh (combined SIM rerun, 2026-09-01)

- Docker Desktop 恢复可用后重新执行 900 帧 `run_px4_gazebo_qgc_combined_sim_acceptance.ps1`，
  run `20260831T234111Z-53232` 退出码为 `0`。源证据记录 `gate_passed=true`、900/900
  传感器/RTSP/metadata 帧、Mission-ready 后连续 type-24 ACK、VTOL MC→FW→MC、容器清理、
  端口释放和 protected UDP `14550` 不变；`operator_visual_acceptance` 仍未声明。
- 本次重新封装和 E7 聚合只替换 SIM provenance，不改变四模式发布门：五组 parity 仍一致，
  但人工 Fly View、Mission 状态、HIL/REAL 硬件与 real-control、现场链路和 replay-set
  仍未闭合，所以报告保持 `gate_passed=false`、`release_allowed=false`，版本仍为
  `v0.3.0-pre.8`。

- 同一台式机随后重跑独立 live Docker AI armed-heartbeat 闭环，证据为
  `artifacts/evaluation/simulator-docker-ai-acceptance-20260901-sim-rerun.json`（96,607 bytes，
  SHA-256 `80A21B60271E9AEC9C41AB9346FB01D66C93DD499B6B6AAA45A3FB70170C95FF`）。
  `runtime_gate_passed=true`、`telemetry.qualified=true`、`armed_observed=true`、
  `ai_policy_gate_passed=true`、`ai_steps=1`；共享 `SimulatorMavlinkControlAdapter` 发出
  一个 AI policy `MANUAL_CONTROL`，随后发送 disarm，容器 stop/rm 清理成功。该证据仍是
  disposable PX4/Gazebo AI machine gate，不替代真实 Pixhawk/Jetson/GNSS、执行器、HIL/REAL、
  实飞或人工 Fly View。
- 同时刷新了不依赖外部进程的 SIM 逻辑回归：
  `artifacts/evaluation/simulator-ai-acceptance-20260901-sim-rerun.json`（SHA-256
  `4AAAE9EB5CCC46CC6399270ABCA09B19EE6F56ACCF9DE8118E1B787A0C6FBE8E`）验证 hover/track、
  manual takeover、AI resume 和 link-loss failsafe；
  `artifacts/evaluation/simulator-3d-control-acceptance-20260901-sim-rerun.json`（SHA-256
  `56099CE79D65DF7D240BFC633E3479EA848053E4665F62AA0B6157F2509A82AC`）验证 VTOL 场景的
  manual/AI/watchdog 共享 authority、资产哈希和 `truth_free`。两者均为软件/协议门，外部
  `px4`/`gz`、桌面视觉和硬件字段仍按实际状态保持未声明。

### Verification refresh (latest field-link probe rerun, 2026-09-01)

- 在台式机上再次执行单次只读现场链路预检，原始证据为
  `artifacts/evaluation/field-link-preflight-20260901-continue-latest.json`（2351 bytes，SHA-256
  `67FAD84F26C1DDC0301CD0197284AC70FE93281BC7773A9597093B58EFF68AC2`）。Jetson
  `192.168.144.20:22`、GR01 `192.168.144.11:5760` 与相机 `192.168.144.108:554` 均超时，
  本机 `14580/14581` 可绑定；`hardware_link_ready=false`，`--require-all` 退出码为 `1`。
- 该最新现场报告已绑定到
  `artifacts/evaluation/release-gate-pre.8-current-20260901-field-rerun.json`（SHA-256
  `A13D0223504221C986645F7EE73C6F3B15F8B1656687CE66421FCA8BD80F0A94`）及聚合报告
  `artifacts/evaluation/release-gate-pre.8-current-20260901-field-rerun-report.json`（SHA-256
  `746FBF1CD8D144D532E67B7AAF0E0E615A03B11397801DC190A88E5767F4AD43`）。四模式报告继续
  `gate_passed=false`、`release_allowed=false`；该刷新只更新现场 provenance，不提升硬件、
  GNSS、HIL/REAL、人工 Fly View 或实飞门。
- 台式机迁移后的完整 Python 回归再次通过：`2166 passed / 1 skipped`（唯一跳过为 Windows
  账户无法创建 symlink），Ruff、compileall 与 CRLF-aware `git diff --check` 均为退出码 `0`；
  Windows Release/Ninja 增量构建为 `ninja: no work to do`，portable simple-boot smoke 退出码
  `0`，当前 portable EXE SHA-256 为 `61F16B6A13247316E356C7B626E79BB1033A6AC03C54D4DD676F75BCCD4CE497`。

### Working-tree delta (2026-09-01, SIM replay-set staging fixture)

- 新增 `scripts/create_sim_replay_set_fixture.py`：把已通过 Gazebo/Harmonic 相机捕获门的
  raw recording 复制为 `day/night/haze/reflection/smoke_only/no_fire` 六层 SIM replay-set，
  并为每帧写入显式空基线真值/预测。脚本复用现有 `RawSensorReplayRecording` 和
  `ReplaySetManifest`，直接从 clean checkout 运行，不改变生产感知或控制链路。
- 当前 staging 产物为 `artifacts/replay-sets/sim-gazebo-2026/`，每层 5 帧；manifest SHA-256
  为 `C2078C778BE12E688CD903339C1CA46E698AE5BCAF83675FD01BDC9AFDC537D3`，report SHA-256
  为 `3DAAECBC63AC5BB512B60D9817DF5B1CFF0F0FA8995930D8749583A8C242B319`。六层结构、文件
  哈希和帧对齐均通过，评估器正常运行，但 `source_kind=synthetic_demo`、标注未复核且空基线
  不含检测真值，因此 `deployment_domain_evidence_complete=false`、`accuracy_validated=false`。
- 新的四模式中间清单/报告为
  `artifacts/evaluation/release-gate-pre.8-current-20260901-replay-staging.json`（SHA-256
  `1860EF768C6E9C645CFE7023078A177C7985A207BF9C27760A52871441AC4A1A`）和
  `release-gate-pre.8-current-20260901-replay-staging-report.json`（SHA-256
  `E1C1E0FB3D8A9D0EA8997ABBAD4124C59EFE795B8FBE91FC391884B95189C2E2`）。聚合器现明确记录
  `replay_set.present=true`、`passed=false`；该 SIM staging 不提升室外 GNSS、真实相机精度、
  HIL/REAL 或最终 `v0.3.0` 发布门。
- 追加生成 replay-set 绑定补充清单/报告：
  `artifacts/evaluation/release-gate-pre.8-current-20260901-field-rerun-replay-bound.json`
  （SHA-256 `261CB2755E55013278E9BDEA698AC80D110B55A09F79193C52DD91A4AB27B75C`）和
  `release-gate-pre.8-current-20260901-field-rerun-replay-bound-report.json`
  （SHA-256 `DDE5D24A785D07AE1F40D21EBA8D27B379EBD0B5CAE5CBC9633786D50FDA96C8`）。聚合器确认
  `replay_set.present=true` 但 `passed=false`（合成数据尚未人工标注/独立复核），现场链路仍未就绪；
  该补充快照不替换 field-rerun 权威清单，也不提升发布门。

### Verification refresh (2026-09-01, workstation network state)

- 重新执行完整 goal-acceptance 后，最新快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-network-refresh.json`，
  SHA-256 `7D070D0F49547D0569308959E8184E944FEDDAB7831C4A13B9BF659D2CC18458`。
  收集数为 `2167`（`2166 passed / 1 skipped`），QGC 自定义回归 `66/66`，软件状态
  `pass_with_environment_gap`，硬件状态 `offline_pending`。
- 快照绑定现场报告 `field-link-preflight-20260901-continue-latest.json`，并记录物理
  `Ethernet=Up`、`192.168.1.13/24`、无 `192.168.144.0/24` 路由以及断开的 `Ethernet 2`
  TAP；Jetson/GR01/相机端点仍未就绪。该刷新只更新 provenance，不提升 REAL/HIL、室外
  GNSS、真实执行器或人工 Fly View，也不创建 `pre.9` 或最终 `v0.3.0`。
- 同日运行 `scripts/capture_qgc_native_style.ps1` 启动并清理便携 QGC；由于 Task Manager
  覆盖 exact QGC client rectangle，捕获按单一原生界面规则停止。观察报告
  `artifacts/evaluation/qgc-native-style-20260901-continue/process-observation.json`（3423 bytes，
  SHA-256 `0EDEB12E5BC121BB7AFE86947BC22A75420A7605C9997C9B21FB4B5D61F7F81E`）记录
  `single_native_surface_machine_check=false`、`operator_visual_acceptance=not_claimed`，且
  没有残留 QGC 进程。捕获脚本新增显式 `-HideStubbornOccluders` 兜底，并且只在确认窗口
  真正隐藏后计入 hidden 数量；本机 Task Manager 属于更高完整性窗口，仍无法被普通
  PowerShell 临时隐藏，需手工最小化/关闭后重跑。
  带 `-HideStubbornOccluders` 的重试仍被同一高完整性 Task Manager 阻断；最新观察报告
  `artifacts/evaluation/qgc-native-style-20260901-continue-hide-v2/process-observation.json`
  （3574 bytes，SHA-256 `4464220068A428BC0DAFCC96CAAE3B60D3C9A1C21A640800FF74627A0521B23B`）确认
  `hidden_occluder_count=0`、`single_native_surface_machine_check=false`，且没有残留 QGC 进程。

### Working-tree delta (2026-09-01, replay-set accuracy evidence)

- 新增 `src/multidetect/replay_set_evaluation.py` 与
  `scripts/evaluate_replay_set.py`：对六类分层回放集逐条复用共享 IoU evaluator，记录每条
  真值/预测文件摘要、整体 precision/recall、误报/漏检帧率和 p95 推理延迟，并把回放集
  raw/canonical SHA-256 写入精度报告。
- 精度报告把结构完整、预测齐全、指标阈值通过和独立复核拆成独立状态；缺预测、阈值失败或
  没有显式 `--reviewed --reviewer` 时保持 `gate_passed=false`，不把“有预测文件”当成模型
  准确率验收。新增 `configs/schemas/replay-set-accuracy-report.schema.json`。
- E7 release-gate 支持 `--replay-set-accuracy-evidence`：校验事件类型、报告文件 SHA-256、
  回放集摘要绑定和 `accuracy_validated=true`；最终 `release` 通道在完整回放集之外还必须
  提供这份独立精度证据。当前仍未改变 `v0.3.0-pre.8` 版本和真实 Jetson/GNSS/Pixhawk、
  HIL/REAL 未验收状态。

### Verification refresh (replay accuracy integration, 2026-09-01)

- `scripts/run_goal_acceptance.ps1` 已在当前 pre.8 源码上重新执行，快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-replay-accuracy.json`
  （SHA-256 `F1A10EE040DD045764A7CD37FFF655AAE3DF3E86B429B4A215422A5B698D5432`）。脚本退出码为 `0`，
  核心测试收集 `2128`，QGC 自定义回归 `66/66`，协议自测、Ruff、compileall、CRLF-aware diff check
  和 portable headless smoke 均通过；软件=`pass_with_environment_gap`、硬件=`offline_pending`。
- 这份证据只覆盖回放集 evaluator、raw/canonical 哈希绑定和 E7 wiring；未提供真实 Jetson、GNSS、
  Pixhawk、室外真值、执行器、HIL/REAL 或人工 Fly View 结果，因此开发版本仍保持 `v0.3.0-pre.8`。

### Verification refresh (field-link watch, 2026-09-01)

- 现场链路预检以 `--protocol-checks --samples 3 --interval-s 0.1 --require-all` 重复三次，证据为
  `artifacts/evaluation/field-link-preflight-20260901-watch3.json`（SHA-256
  `52C7EC568A4390B3DD334B6E27B67516C9524B4F9B0A5D823B57FCB50869B7C7`）。三个样本均采集完成但均失败：
  Jetson SSH、GR01 MAVLink、相机 RTSP 全部超时，`14580/14581` 本地 UDP 端口均可绑定，
  `stability_ratio=0.0`、`max_consecutive_failures=3`、`hardware_link_ready=false`，命令退出码为 `1`。
- watch 保持只读，不登录、不请求 MAVLink stream、不启动 RTSP 视频、不发送飞控控制。当前活动网卡仍为
  `192.168.1.13`，需要接通飞机 Ethernet 或确认实际机载网段/IP 后再复跑；该证据不提升任何 GNSS、
  Pixhawk、执行器、HIL/REAL 或室外飞行状态。

### Working-tree delta (2026-09-01, field-link evidence binding)

- `field_link_preflight` 新增 `verify_field_link_report()`：对保存的单次/多次现场预检报告重算文件
  SHA-256，检查 `all_passed`/`hardware_link_ready` 一致性及 watch 样本完整性，并在报告损坏、篡改或
  readiness 未通过时保持 fail-closed。
- `field_session_capture` 与 `field_session_acceptance` 新增可选的
  `--link-preflight ... --require-link-preflight`；现场会话报告记录预检路径、字节数、SHA-256、摘要和
  复核结果，交接复核时再次哈希该文件。未指定参数的既有离线 fixture 行为保持不变。
- 新增链路绑定/报告篡改/缺失路径回归；这只是现场证据编排增强，不把当前
  `artifacts/evaluation/field-link-preflight-20260901-watch3.json` 的失败结果升级为硬件通过。版本仍为
  `v0.3.0-pre.8`，真实 Jetson/GNSS/Pixhawk、执行器、HIL/REAL 与人工 Fly View 门继续 pending。
- 增加独立 `verify_field_link_preflight.py` / `multidetect-field-preflight-verify` 入口：现场交接可以
  在不重新探测设备的情况下复核链路报告 hash、摘要和 watch 样本；默认仍要求
  `hardware_link_ready=true`，`--allow-unready` 只用于失败报告的结构诊断。

### Working-tree delta (2026-09-01, goal-acceptance field-link binding)

- `scripts/run_goal_acceptance.ps1` 新增 `-FieldLinkPreflight` 与
  `-RequireFieldLinkPreflight`。总验收现在可以绑定一份已保存的现场链路预检：重算文件
  SHA-256/字节数，调用独立 verifier，并把 `verified`、`verification_exit_code`、
  `hardware_link_ready`、`gate_passed` 和原因写入快照；要求模式遇到未就绪或篡改报告立即
  fail-closed。未传参数时仍保留离线软件验收语义。

### Verification refresh (goal-acceptance field-link binding, 2026-09-01)

- 最新 pre.8 goal acceptance 为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-field-link-bind.json`（SHA-256
  `5842826C15783B55A662CA4A81F73096BFFA31EB0D343E97A40255A666E0CAA6`）。脚本退出码为 `0`，
  核心测试收集 `2137`，QGC 自定义回归 `66/66`，软件状态为 `pass_with_environment_gap`，
  硬件状态为 `offline_pending`；已知三采样 watch 被完整记录为 `verified=true`、
  `hardware_link_ready=false`、`verification_exit_code=1`，未被升级成硬件通过。严格绑定该报告的
  行为回归确认脚本立即以非零退出，现场恢复后可复用同一命令和报告链。

### Working-tree delta (2026-09-01, E7 REAL field-link release binding)

- `release_gate_manifest.py` 新增 `--real-field-link-preflight` 与同名 API 参数，把现场预检报告的
  相对路径、SHA-256、字节数、`hardware_link_ready` 和 `gate_passed` 写入顶层 `real_field_link`；
  预发布仍兼容省略该字段，显式提供的失败/篡改报告则立即关闭 evidence gate。
- `release_gate.py` 每次聚合都会重新哈希绑定报告并调用严格的
  `verify_field_link_report(..., require_ready=True)`，检查 readiness、gate、字节数和摘要是否一致；
  最终 `release` 通道额外强制要求存在且通过的 `real_field_link`，不会接受 REAL mode 自报硬件状态作为替代。
- `configs/schemas/release-gate-manifest.schema.json` 和发布门文档补充该绑定契约，新增 schema/CLI/篡改
  回归；当前源码仍为 `v0.3.0-pre.8`，没有提前切换到最终 `v0.3.0`。

### Working-tree delta (2026-09-01, bounded field-link discovery)

- `field_link_preflight` 增加显式 `--discover-cidr`/`discover_field_link_hosts()`：在操作员给出的
  IPv4 CIDR 内并发识别 SSH、MAVLink TCP 和 RTSP 服务，默认最多 `256` 个地址、`32` 个 worker，
  每个服务使用有界超时。协议模式仍然只读：SSH 读取 banner、MAVLink 查找完整帧、RTSP 发送
  `OPTIONS`，不登录、不请求 stream、不启动视频、不发送飞控控制。
- 发现结果只写入诊断性的 `discovery` 节点，不会自动选择 Jetson/GR01/相机地址，也不会改变
  `hardware_link_ready`。候选地址必须再次通过显式三端点预检，随后才能绑定到 goal/release evidence。
- 新增 CIDR 上界、候选主机过滤和并发确定性回归；版本仍为 `v0.3.0-pre.8`，真实硬件门不因发现
  结果提前关闭。

### Verification refresh (bounded field-link discovery, 2026-09-01)

- 当前台式机活动网卡 `192.168.1.13/24` 已执行一次 `--discover-cidr 192.168.1.0/24
  --protocol-checks` 短超时扫描，证据为
  `artifacts/evaluation/field-link-discovery-20260901-043102.json`（SHA-256
  `2CAA5D70F336E2E2A641B19383EA32658CF062C668DDF7CE13EB7CF1E96BB8DC`）。扫描 `254` 个地址，
  仅发现 `192.168.1.1`、`.62`、`.77` 的 SSH banner，`5760/MAVLink` 与 `554/RTSP` 均为 `0` 命中。
- 这些地址只保留为候选服务证据，未识别为 Jetson/GR01/相机，也未写入运行配置；正常三端点预检、
  GNSS/RANGE_STATUS/执行器、HIL/REAL 门仍保持 fail-closed。版本继续为 `v0.3.0-pre.8`。
- 增加 `service_candidates` 后在同一网段完成复核，最新报告为
  `artifacts/evaluation/field-link-discovery-20260901-043203.json`（SHA-256
  `68BD8F7A1581A494EB3706AF4C5F068761AF8B9A3750C43BDCD69B2DFA413DF2`），结果与首轮一致：
  SSH 三个候选、MAVLink/RTSP 零命中。

### Verification refresh (goal acceptance after discovery validation, 2026-09-01)

- 补充发现器非法端口回归后重新执行总验收，当前 pre.8 快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-field-discovery-v4.json`（SHA-256
  `56F32DE50577E6A171CF963E32F87EF2DB2D130E20315DBEF7531A713AEF6968`）。脚本退出码 `0`，核心测试
  收集 `2143`、QGC 自定义回归 `66/66`，软件=`pass_with_environment_gap`、硬件=`offline_pending`；
  绑定的三采样现场 watch 仍 `hardware_link_ready=false`，因此没有提前关闭真实设备门或最终版本。

### Verification refresh (SIM external runtime diagnosis, 2026-09-01)

- `runtime_preflight` 现在在 daemon 不可用时读取最近的 Docker backend 日志尾部，输出有限的
  `docker_diagnostics`（schema、错误摘要、归因和建议动作），不启动/重启 Docker，也不写入原始日志；
  诊断行和 CLI stdout 均做 ASCII/JSON 安全编码，避免 Windows GBK 控制台因日志中的 Unicode 字符退出 2。
- 重新执行 `python -m multidetect.simulator runtime-preflight` 后，证据为
  `artifacts/evaluation/simulator-runtime-preflight-20260901-diagnostics-v4.json`（文件 SHA-256
  `CF1E9DD6EA07A663C7284504A6200244945DE6BFA989A35B62264D9A5F59D8CB`；canonical digest
  `3b91e5e4d0996412f73fca3f1b50ba4bedf23601789db427f47603b178cf8fac`）。Docker client 28.4.0
  存在但 server probe `TimeoutExpired`，PX4/Gazebo 仍不在 PATH，`ready_to_attempt=false`。
- 诊断日志定位到 Docker Desktop backend 无法移除陈旧的
  `C:\Users\admin\AppData\Local\Docker\run\dockerInference` reparse/socket 条目；这只解释
  本机外部 SIM runtime 不可用，不把 WSL/进程存在升级为 3-D 渲染、PX4 telemetry、HIL/REAL 或
  硬件通过。版本继续保持 `v0.3.0-pre.8`。

### Working-tree delta (Docker stale-socket recovery helper, 2026-09-01)

- 新增 `scripts/recover_docker_desktop_stale_socket.ps1`：默认只读盘点当前账户的
  `dockerInference` reparse/socket，`-Apply` 先尝试移动到时间戳备份目录，只有显式
  `-RemoveStaleEntry` 才允许执行精确的 `fsutil reparsepoint delete` + 删除，
  `-RestartDocker` 仅在 `-Apply` 后启动 Docker Desktop；脚本不接触镜像、容器、卷或其它
  Docker run 条目。
- 新增 `tests/test_docker_recovery_script.py`，覆盖只读默认、精确路径/reparse 校验、可逆移动优先、
  最后手段开关和隐藏重启行为。PowerShell parser、该测试及现有 SIM/field-link 聚焦回归均通过；
  这只是环境恢复工具，不改变 `v0.3.0-pre.8` 或任何硬件/REAL gate。
- `run_px4_gazebo_qgc_combined_sim_acceptance.ps1` 现在在 Docker `info`/镜像操作前调用同一只读
  `runtime-preflight`，把预检 JSON 写入组合验收目录，并在 `ready_to_attempt=false` 时 20 秒内
  带着诊断 fail-fast；Docker 恢复后直接重跑原有联合入口即可。

### Verification refresh (combined SIM fail-fast smoke, 2026-09-01)

- Windows PowerShell/.NET Framework 烟测确认联合入口在约 4 秒内因
  `ready_to_attempt=false` 退出，未创建容器、QGC、RTSP 或飞控进程；只读预检写入
  `artifacts/evaluation/combined-sim-qgc-20260831T213201Z-74784/runtime-preflight.json`（文件
  SHA-256 `40C947334A08C6CEFFB7F5F8EEDF699FF66E342D49C3BCD5878162D8008A3FE3`，canonical digest
  `b75146423ce09c9ebccd14f5acc391e79dedec8f9674b030c7d3b3b6edfe9147`）。此前
  `ProcessStartInfo.ArgumentList` 在 Windows PowerShell 5.1 的空值错误已修复为
  `CommandLineToArgvW` 兼容的 quoted-Arguments fallback；PowerShell parser 和静态回归均通过。
- 该烟测只验证启动边界和 fail-fast 证据，不提升 Docker/PX4/Gazebo、GNSS、Jetson、HIL/REAL 或最终
  `v0.3.0` 状态。

### Verification refresh (Windows PowerShell argument compatibility, 2026-09-01)

- 补充 `CommandLineToArgvW` quoted-Arguments fallback 的静态回归并重跑总验收，当前 pre.8 快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-docker-recovery-v3.json`（SHA-256
  `D5242D8B09C9108BC47ADE2F92CD9D26450EF56F04F9445C2B4B8A39AD7A3BE9`）。脚本退出码为 `0`，核心测试
  `2152`、QGC 自定义测试 `66/66`，软件=`pass_with_environment_gap`、硬件=`offline_pending`。
- 该快照只更新 Windows PowerShell 5.1 的启动兼容性与 fail-fast provenance；Docker stale socket、
  PX4/Gazebo 外部 3-D、Jetson/GNSS/Pixhawk、HIL/REAL 和最终 `v0.3.0` 门仍保持原状态。

### Verification refresh (latest field-link sample, 2026-09-01)

- 重新执行一次只读现场协议预检，证据为
  `artifacts/evaluation/field-link-preflight-20260901-continue-20260901-053931.json`（SHA-256
  `6FAF98209F6B7ABFD3231D84C49CE1F22870B5DB0C7AFE646CE0D9148A410B53`）。Jetson
  `192.168.144.20:22`、GR01 `192.168.144.11:5760` 与相机 `192.168.144.108:554` 均超时，
  本机 `14580/14581` 可绑定；`samples_collected=1/1`、`hardware_link_ready=false`，
  `--require-all` 退出码为 `1`。现场报告仍是只读诊断，不发送登录、MAVLink stream、视频或飞控控制数据。
- 使用该最新报告刷新总验收，快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-field-refresh-20260901-053959.json`
  （SHA-256 `1E79B2BD511AD1C4FB53BBDF522DDB2FCA53AA860960795891A90E5158881A69`）。脚本退出码为 `0`，
  核心测试 `2152`、QGC 自定义测试 `66/66`，软件=`pass_with_environment_gap`、硬件=`offline_pending`；
  该刷新只更新现场 provenance，不关闭 Jetson/GNSS/Pixhawk、执行器、HIL/REAL、人工 Fly View 或实飞门。

### Verification refresh (Docker daemon stopped-state probe, 2026-09-01)

- 在 Docker Desktop 进程完全停止后重跑只读 `runtime-preflight`，证据为
  `artifacts/evaluation/simulator-runtime-preflight-20260901-continue-20260901-054222.json`
  （文件 SHA-256 `F7BA35ACA79E39A225F4CA65841A253E323745244E860EAA661E2BE209DFB5F8`；canonical
  digest `351faffb851922ed0643af310727b2135cf192834e2a6771d26c21a4d7635ae4`）。Docker client
  `28.4.0` 仍存在，但 server named pipe 返回“找不到文件”，PX4/Gazebo 不在 PATH，
  `ready_to_attempt=false`；backend 日志仍将故障归因为遗留 `dockerInference` reparse/socket。
- 该复探只记录停机后的 fail-closed 状态，不启动 Docker、PX4/Gazebo、容器、QGC 或现场设备；管理员
  清理单一陈旧条目并重新启动 Docker 后，必须再次运行同一 `runtime-preflight`，然后才能尝试外部 SIM gate。

### Verification refresh (preflight evidence retention fix, 2026-09-01)

- 修复 `run_px4_gazebo_qgc_combined_sim_acceptance.ps1` 在预检通过后被后续变量初始化覆盖的问题；
  最终 `combined-evidence.json` 现在保留 `runtime_preflight.readiness` 与限量
  `runtime_preflight.docker_diagnostics`，而不是只留下文件路径和哈希。新增静态回归锁定解析顺序，
  PowerShell parser、Ruff 与聚焦测试均通过。
- 修复后再次执行总验收，快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-field-refresh-20260901-054426.json`
  （SHA-256 `0B1A8F418281C7AC04B32EAB857E0D34B57C80B4F7B976D56F7DCA5D9DB2DA14`）。脚本退出码为 `0`，
  核心测试 `2152`、QGC 自定义测试 `66/66`，软件=`pass_with_environment_gap`、硬件=`offline_pending`；
  真实设备、HIL/REAL、人工 Fly View 与最终 `v0.3.0` 门仍保持未关闭。

### Verification refresh (goal acceptance after Docker recovery helper, 2026-09-01)

- 使用当前仓库 `.venv` 重新执行总验收，快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-docker-recovery.json`（SHA-256
  `597EA3B619FECD5F38B8E92697EB69252D2CCC9349F99257909996616DD0FF6E`）。脚本退出码为 `0`，核心测试
  `2150`、QGC 自定义测试 `66/66`，Ruff、compileall、QML 解析、协议自测和 portable headless
  smoke 全部通过；软件=`pass_with_environment_gap`、硬件=`offline_pending`。
- Docker backend 的 `dockerInference` 陈旧 reparse/socket 仍需现场管理员 PowerShell 清理，
  当前证据没有把脚本静态回归或确定性 SIM 升级为外部 PX4/Gazebo、Jetson、室外 GNSS、执行器、
  HIL/REAL 或最终 `v0.3.0` 通过。

### Verification refresh (combined SIM fail-fast integration, 2026-09-01)

- `run_px4_gazebo_qgc_combined_sim_acceptance.ps1` 已在 Docker `info`、镜像拉取和容器操作之前调用
  同一 `runtime-preflight`，并把预检路径、readiness 和限量 `docker_diagnostics` 写入组合证据；
  runtime 不可用时在 20 秒内 fail-fast。
- 重新执行总验收后的最新快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-docker-recovery-v2.json`（SHA-256
  `2CF2F1F653E7E5B586114F9878255DBE582F8CE150C4C4272DE8FC129756ED75`）。脚本退出码为 `0`，核心测试
  `2151`、QGC 自定义测试 `66/66`，软件=`pass_with_environment_gap`、硬件=`offline_pending`；
  Docker/PX4/Gazebo 环境缺口和现场硬件门仍保持 fail-closed。

### Verification refresh (goal acceptance after runtime diagnostics, 2026-09-01)

- 使用仓库规定的 `ground-control/MultiDetectGCS/.venv` 重跑总验收，快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-runtime-diagnostics.json`（SHA-256
  `3169F3E8A6E48C6B06BB9D55299913833F5F46A2E25C61EEC076777B116753D3`）。脚本退出码 `0`，核心测试
  `2146`、QGC 自定义测试 `66/66`，Ruff、compileall、QML 解析、协议自测与 portable headless
  smoke 全部通过；软件=`pass_with_environment_gap`、硬件=`offline_pending`。
- 快照继续绑定 `field-link-preflight-20260901-watch3.json` 的 `verified=true`、
  `hardware_link_ready=false`、verifier exit `1`；当前网卡没有飞机网段/串口，Docker/Gazebo/PX4
  外部 3-D 运行时也没有就绪，故不关闭真实 GNSS、Pixhawk、执行器、HIL/REAL 或最终 `v0.3.0` 门。

### Verification refresh (E7 REAL field-link binding, 2026-09-01)

- 重新执行总验收后的快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-real-field-link-gate.json`（SHA-256
  `00F4A58B2CC809665F1580CAB5E143251387ABE671EC7D6BBC793240C97610E2`）。脚本退出码为 `0`，核心测试
  收集 `2140`，QGC 自定义回归 `66/66`，Ruff、compileall、CRLF-aware diff check、协议自测和 portable
  headless smoke 均通过；软件=`pass_with_environment_gap`、硬件=`offline_pending`。
- 快照绑定 `field-link-preflight-20260901-watch3.json`（SHA-256
  `52C7EC568A4390B3DD334B6E27B67516C9524B4F9B0A5D823B57FCB50869B7C7`），报告复核为
  `verified=true`、`verification_exit_code=1`、`hardware_link_ready=false`；Jetson/GR01/RTSP 均未接通，
  所以没有关闭真实 GNSS、Pixhawk、执行器、HIL/REAL、人工 Fly View 或实飞门。

### Verification refresh (strict writer fail-closed, 2026-09-01)

- 新增回归要求：显式传入未就绪的 `--real-field-link-preflight` 时，生成器默认返回退出码 `2`；
  只有 `--allow-incomplete` 才会写出该失败诊断清单。这样预发布可保留现场失败证据，同时不会把
  writer 的成功退出误读成现场通过。
- 重新执行总验收后的最新快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-real-field-link-gate-v2.json`（SHA-256
  `7075D414DEF8A809AFEB7FB5BDD41DFE8008F80C4FE08C0228596546D01386C0`）。脚本退出码 `0`，核心测试
  收集 `2141`，QGC 自定义回归 `66/66`；软件=`pass_with_environment_gap`、硬件=`offline_pending`。
  三采样 watch 绑定仍为 `hardware_link_ready=false`、`gate_passed=false`、verifier exit `1`，未升级
  任何真实链路或最终 `v0.3.0` 发布门。

### Verification refresh (field-link evidence binding, 2026-09-01)

- 当前 pre.8 goal acceptance 快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-preflight-cli.json`（SHA-256
  `6583EF2F2B0744439EF3255C2529EC9A832B17332682C1E41A8AFD7945C75099`）。脚本退出码为 `0`，核心测试收集
  `2136` 项，QGC 自定义回归 `66/66`，协议自测、QML 解析、Ruff、compileall、CRLF-aware diff check
  与 portable headless smoke 均通过；软件状态为 `pass_with_environment_gap`，硬件状态为
  `offline_pending`。现场三采样 watch 仍为 `hardware_link_ready=false`，因此未关闭真实 Jetson/GNSS/
  Pixhawk、室外真值、执行器、HIL/REAL 或人工 Fly View 门。

### Working-tree delta (2026-09-01, outdoor range calibration)

- 将 `src/multidetect/depth_calibration.py` 扩展为可审计的单调分段线性模型：至少 6 条实测样本和 4 个
  严格递增 knot；重复 raw-depth 读数按中位数合并，明显的实际距离顺序冲突与孤立错配在保留最低证据量的
  前提下剔除，PAVA 负责小幅非单调噪声收口。拟合结果仍保留 affine 兼容参数，但运行时优先使用曲线。
- 标定文档新增 schema v2 的 `calibration.mode=piecewise_linear`、raw/actual breakpoints 和最近端线性
  外推声明；schema v1 affine 文档保持向后兼容。`MetricDepthEstimator`、CLI 和 live audit metadata 现在
  记录曲线模式、实测 raw 范围、外推状态，并在域外估计时提升 `σ` 下限。
- `scripts/fit_outdoor_depth_calibration.py` 新增 `--model piecewise_linear`，
  `scripts/validate_outdoor_depth_calibration.py` 对 affine/curve 使用同一映射器；新增曲线往返、坏曲线
  fail-closed、运行时插值和外推不确定度回归。当前 canonical parity 已把 depth calibration/metric depth
  纳入 `core_algorithm`，hash 为 `b26c8709b2ff52e028852cb2d468da354bfa4f1d41a0c3d77112fce0dceed064`。
- 当前台式机仍未连通 Jetson、相机或室外 GNSS；本轮只证明软件拟合/加载/映射链，不能替代现场多距离真值、
  HIL/REAL 或人工 Fly View 验收。开发版本继续为 `v0.3.0-pre.8`。

### Verification refresh (outdoor piecewise, 2026-09-01)

- 全量 goal acceptance 快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-outdoor-piecewise-v33.json`
  （SHA-256 `013307F219F289A5D7F2A6CE306E472625E2C08F6FF9F484452C708747D59A57`）。脚本退出码为 `0`，
  核心测试收集 `2109`，QGC 自定义回归 `66/66`，QGC protocol/QML/Ruff/compileall/CRLF-aware diff
  check 和 portable headless smoke 均通过；软件=`pass_with_environment_gap`、硬件=`offline_pending`、
  `operator_visual_acceptance=not_claimed`。
- canonical `configs/release-parity.json` 与审计快照
  `artifacts/evaluation/release-parity-pre.8-20260901-outdoor-piecewise-v32.json` 字节一致，文件 SHA-256
  `4ED355018B24A093FB7276E26DFACEBCCB5C826BF2DD04A6B0491EB810A91B0F`，新增的
  `depth_calibration.py`/`metric_depth.py` 已包含在 `core_algorithm` parity；portable EXE 仍为
  `v0.3.0-pre.8`、SHA-256 `61F16B6A13247316E356C7B626E79BB1033A6AC03C54D4DD676F75BCCD4CE497`。
- 该轮只验证室外曲线拟合、schema v2 加载和运行时映射；台式机现场预检仍显示 Jetson、GR01 和 RTSP
  端点超时，所以真实 GNSS、Pixhawk、执行器、HIL/REAL、室外真值和人工 Fly View 门继续保持 pending。
- 同日按当前 pre.8 源码重新执行三条 SIM 闭环：
  `simulator-closed-loop-acceptance-20260901-pre.8-v2.json`（SHA-256
  `11195BBC7B8E28082A446524040A32F8D026B7B9C87164F85389771C17ECA949`）、
  `simulator-ai-acceptance-20260901-pre.8-v2.json`（SHA-256
  `6E0F467B2947C8E1AED0DA333D9520F324B61AF8A53EEF4EC12BE595FE1F84CE`）和
  `simulator-3d-control-acceptance-20260901-pre.8-v2.json`（SHA-256
  `56099CE79D65DF7D240BFC633E3479EA848053E4665F62AA0B6157F2509A82AC`）均为
  `gate_passed=true`；三份证据仍明确标注外部 PX4/Gazebo、GPU/人工画面、Pixhawk/Jetson/相机、
  HIL/REAL 和室外 GNSS 为未验收状态。
- 新增 `python -m multidetect.simulator runtime-preflight --out <evidence.json>` 只读预检，
  统一记录 native PX4/Gazebo、Docker daemon 和 Windows WSL 状态，并用
  `ready_to_attempt`/`execution_mode` 与 `process_started=false`、`telemetry_observed=false`
  明确工具发现不等于外部运行。当前台式机证据为
  `artifacts/evaluation/simulator-runtime-preflight-20260901-pre.8.json`（文件 SHA-256
  `E973A4F3B93B255F61D8F600E4D90F8EE251D9D106F010D83A12BB93538506C3`；内嵌 canonical digest
  `bef94984df6580a3caf062ee503697aceca2fb0f5924442c17fa8d643f0e7c9a`）：PX4/Gazebo 原生命令缺失，
  Docker daemon 探针超时，`ready_to_attempt=false`；版本继续保持 `v0.3.0-pre.8`。
- 新增 `ReplaySetManifest` 与 `scripts/create_replay_set_manifest.py`：六类
  `day/night/haze/reflection/smoke_only/no_fire` 分层目录现在可以把 raw recording、人工真值和
  可选预测 JSONL 逐文件绑定 SHA-256，并在加载时复核 recording 内容、帧 ID 顺序和类别集合。
  `python -m multidetect validate-replay-set` 将结构完整性与部署域覆盖分开报告；缺失分层、未复核
  标注、非 deployment recording 或尚未运行 IoU evaluator 都保持 gate/accuracy 为 false，不改变
  当前真实硬件、室外 GNSS、HIL/REAL 未验收状态。回放集 wrapper 现在和发布门 wrapper 一样，
  可在 clean checkout 中直接运行，不依赖预先设置 `PYTHONPATH`。
- E7 发布门现在可在顶层通过 `--replay-set-manifest` 绑定上述回放集的原始与 canonical SHA-256，
  重新校验六类覆盖、raw recording 和真值帧序；预发布旧清单保持向后兼容，最终 `release` 通道
  缺失完整回放集时 `release_allowed=false`。新增的 release-gate/replay-set 回归覆盖 pending
  回放集、哈希错配、最终版本缺失绑定和 clean-checkout wrapper 路径。
- `python -m multidetect.simulator interactive-acceptance --mode all` 同步覆盖三种机型，证据为
  `artifacts/evaluation/simulator-interactive-acceptance-20260901-pre.8-v2.json`（SHA-256
  `24B57DAA77551CBE9970D95A7A93C5AC6209269F044285F5DD7B551467B313BE`），报告
  `gate_passed=true`、`qgc_is_sole_operator_ui=true` 和 `shared_ai_manual_authority=true`，
  外部 PX4/Gazebo、GPU/人工画面、真实硬件、HIL/REAL 与室外 GNSS 仍保持未验收。

### Working-tree delta (2026-08-31)

- 目标落图坐标域与 QGC wire 解码再收口为同一 fail-closed 规则：纬度严格位于
  `(-90, 90)`，经度允许 `[-180, 180]`。Python `TargetGeolocation`、目标地理位置消息模型、
  QGC C++ `TargetGeolocationStatus` 解码器、地图/复制/快照/JSONL 重载均拒绝精确 `±90°` 极点，
  避免极点缺少稳定局部经度方向时留下不可投影的假证据。协议自测新增已签名极点变异向量，
  与 stale source-age 向量一起覆盖 wire 层。
- 极点门收口后的完整 goal acceptance 为
  `artifacts/evaluation/multidetect-goal-acceptance-20260901-target-geo-pole-v31.json`
  （SHA-256 `CC6371291F416E62E1308C78CF70CC2E08B10854D7B5A5817194E5B5164D18EE`）：核心测试收集
  `2101`，QGC 自定义测试 `66/66`，脚本退出码 `0`，software=`pass_with_environment_gap`、
  hardware=`offline_pending`。Release/portable 主 EXE 为 `46,575,104` bytes，SHA-256
  `61F16B6A13247316E356C7B626E79BB1033A6AC03C54D4DD676F75BCCD4CE497`；三项 C++ 协议/wire 自测
  （Operator、44-fragment depth-grid、RTSP）均退出 `0`。canonical parity 文件当前 SHA-256 为
  `54BB83A9E7091259C38BE9A17B4996BF7B9B810BF69DA76D45F29315A9993A62`，对应审计快照为
  `artifacts/evaluation/release-parity-pre.8-20260901-target-geo-pole.json`。Jetson/室外 GNSS、
  真实 Pixhawk/执行器、HIL/REAL 与人工 Fly View 仍保持 pending。

- 2026-09-01 在台式机上重新执行现场链路只读 watch：
  `artifacts/evaluation/field-link-preflight-20260901-followup.json`（SHA-256
  `1873247B49F30856483F22635CC28620458990BC78CA2C2A28BE2BD25355549E`）。命令使用
  `--protocol-checks --samples 3 --interval-s 1 --stop-on-failure --require-all`，首个样本即
  发现 Jetson SSH、GR01 MAVLink、相机 RTSP 均超时，本机 `14580/14581` 可绑定，
  `samples_collected=1/3`、`hardware_link_ready=false`，退出码 `1`；当前活动网卡 IPv4 为
  `192.168.1.13`，待接通飞机 Ethernet 或确认实际机载网段/IP 后复跑，不把该失败证据升级为
  GNSS、Pixhawk、执行器、HIL/REAL 或室外飞行通过。

- 将确定性 SIM 控制链扩展到 plain PX4 multirotor：`FlightMode.MULTIROTOR` 及
  `multicopter`/`quadcopter`/`x500` 别名贯通 `DeterministicFlightModel`、
  `SimulatorClosedLoop`、scenario runner、native QGC interactive fixture、stack-plan 和
  Docker fallback；非 VTOL 机型收到 `DO_VTOL_TRANSITION` 时 fail-closed，不改变飞行状态。
  新增 `simulation/gazebo/models/x500_mono_cam/`、
  `simulation/gazebo/worlds/urban_inspection_multirotor_001.sdf` 与分离的
  `configs/scenarios/urban_inspection_multirotor_001.{runtime,truth,expectations,scenario}.json`，
  复用同一 WGS-84、相机、AI/manual authority、watchdog 和 truth-free 证据契约。
- 补齐三机型共用 PX4 Mission 路径中的 plain multirotor compiler：新增
  `MultirotorMissionGeometry`、`Px4MultirotorSafetyPolicy`、
  `compile_multirotor_executable_mission(...)` 和独立 adapter hash；固定输出
  `TAKEOFF → waypoints → LOITER_TIME → LAND` 的 `MISSION_ITEM_INT`，并沿现有
  upload/readback/commit/AUTO.MISSION 事务门复用 geofence、相对高度、盘旋、落点偏移和
  truth-free runtime 校验。新增 `tests/test_px4_multirotor_mission.py` 的 8 项回归全部通过。
  独立编译验收证据为 `artifacts/evaluation/multirotor-mission-compile-acceptance-20260831-continue.json`
  （SHA-256 `63CC13F887254591FEBFACD5AB323FF6006392C2B6C2BDBF447A59EB321BA84A`），其
  `gate_passed=true`、`item_count=5`、`no_vtol_transition=true`、`physical_output_enabled=false`。
  随后新增 `compile_executable_mission(...)` 与 `normalize_px4_vehicle_class(...)` 统一入口，
  接受 `x500`/`multicopter` 等 simulator 别名并严格配对三种 geometry/policy 类型；多旋翼
  编译验收改为通过该 dispatcher 验证统一的 `MISSION_ITEM_INT` 事务路径。
- dispatcher 别名与类型配对回归的独立证据为
  `artifacts/evaluation/multirotor-mission-compile-acceptance-20260831-dispatcher.json`
  （SHA-256 `ADF8BAB8D4887697A1CF09E54AB8C21CEBDD9ACA0312B0A89514FA613F14DAB2`），
  `gate_passed=true`、输入 `x500` 归一化为 `multirotor`，无 VTOL transition、无物理输出。
- dispatcher、三机型 Mission contract、现场预检和 portable smoke 收口后的整体验收快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-dispatcher-v24.json`
  （SHA-256 `15CC5F31CF4EEBD7500ED63DD093F96C5108AF5D2C980F4087B5CC0463B8C4D5`）；核心测试
  收集 `2083` 项，QGC 自定义回归 `66/66`，软件门 `pass_with_environment_gap`、硬件门
  `offline_pending`，因此仍属于 `v0.3.0-pre.8`。
- 协议级只读现场预检与 SSH 前置 banner/MAVLink v1-v2/RTSP OPTIONS 回归后的最新整体验收快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-field-protocol-v27.json`（SHA-256
  `8E474F06125E92A66A341B84EA9DC57C114257A0BDAB1255FF28EA49A1BFE675`）；核心测试收集 `2088`，
  QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`，
  当前仍是 `v0.3.0-pre.8`。
  v26 原文件保留 provenance；只有硬件/HIL/REAL 门全部关闭后才可切换最终版本。
- 稳定性 watch 与其回归后的最新整体验收快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-field-watch-v29.json`（SHA-256
  `DEF51B47D75987FE948AB6D2639128B2FBC99E68F11591CABF28542C24D9E96D`）；核心测试收集 `2094`，
  QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`，
  当前仍为 `v0.3.0-pre.8`。v27 原文件保留 provenance；watch 证据只证明重复预检的失败闭环，
  不升级 Jetson/相机/室外 GNSS、执行器、HIL/REAL 或人工 Fly View 门。
- 同轮现场协议预检证据为 `artifacts/evaluation/field-link-preflight-20260831-protocol-v3.json`（SHA-256
  `6DC0830C30A675AC13064EE5D3E7F5E31FA5D8BBA28D5E26219CF4E53EB78F30`）：Jetson/GR01/RTSP
  三个端点均超时，本机 `14580/14581` 可绑定，`protocol_checks_passed=false`、
  `hardware_link_ready=false`，`--require-all` 退出码 `1`；探测未发送登录、心跳、视频启动或飞控控制数据。
- 同一 portable EXE 的原生 `--offline --simple-boot-test` 冒烟退出码为 `0`，日志证据为
  `artifacts/evaluation/qgc-simple-boot-20260831-dispatcher.log`（SHA-256
  `39D9D3F102ABD986A0C1EB71880C8C5FF5C40CADA04A0F75C4B845388BEAB3AF`）。
- 多旋翼 deterministic 3-D asset/control gate 证据为
  `artifacts/evaluation/simulator-3d-control-multirotor-continue.json`（SHA-256
  `84419C67A853D6467A900CC76154EE1699052123B456C0C1D0BC66B006ABFCFE`），
  `gate_passed=true`、`deterministic_gate_passed=true`、`PX4_SYS_AUTOSTART=4001`；
  外部 PX4/Gazebo 工具不可用，故 `external_runtime_gate_passed=false`，不提升为视觉、硬件、
  HIL/REAL 或室外 GNSS 验收。聚焦多旋翼/SIM 回归全部通过。
- native QGC interactive fixture 使用 `--mode all` 重跑三种机型，证据为
  `artifacts/evaluation/simulator-interactive-acceptance-all-continue.json`（SHA-256
  `24B57DAA77551CBE9970D95A7A93C5AC6209269F044285F5DD7B551467B313BE`），
  `modes=[vtol,fixed-wing,multirotor]` 且顶层 `gate_passed=true`；三种机型均通过共享
  AI/manual authority、watchdog、只读遥测转发和 blocked-write 子门。
- Docker/PX4/Gazebo 的多旋翼资源与端口规划已通过 no-I/O preflight，证据为
  `artifacts/evaluation/simulator-docker-acceptance-multirotor-preflight-continue.json`
  （SHA-256 `9AD08E26FB7FDB38D483A6E0FCD6B9FBF9677BF0AAF5CE10C540C4578169DD99`）：
  镜像按 digest 固定、模型/世界只读挂载、控制端口仅绑定 loopback、遥测不发布、清理命令完整，
  `preflight_gate_passed=true`；`execution_requested=false`，所以 container runtime、遥测、
  视觉、硬件和 HIL/REAL 均保持 `not_claimed`。
- 多旋翼共享 AI 闭环的 Docker 参数与策略预检也已封存于
  `artifacts/evaluation/simulator-docker-ai-acceptance-multirotor-preflight-continue.json`
  （SHA-256 `B05B6C247DA3E6C8DA6D6A6AC71424421AC2300E6A587E92CB78AAC8CE3C5D50`）：
  `preflight_gate_passed=true`、`runtime_gate_passed=true`、`ai_policy_gate_passed=true`、
  `ai_steps=6`（`ai_policy_steps=5`），共享 MANUAL_CONTROL/authority/watchdog 链路和 blocked-write 检查通过；
  该记录为 deterministic/no-I/O loopback fixture（`execution_requested=false`），不代表
  外部 PX4/Gazebo、真实 Pixhawk/Jetson、执行器、视觉或 HIL/REAL 已验收。
- 由于 `perception_bridge.py` 与三机型 Mission dispatcher 的共享契约更新，已用仓库生成器重算
  `configs/release-parity.json`；该阶段的历史文件 SHA-256 为
  `9D25D270A09C9F66515214FBE55D8C2D7DA5C25B1C4051DB8F61B13077FA098A`，canonical parity 回归已通过。
- 目标落图源帧新鲜度在 v30 阶段收口为跨层一致的 `<=1.5 s`：生产者在发现合格坐标已经过期时原子发布
  `available=false`，Python 消息模型/编码器/解码器与 QGC C++ 解码器对带新签名的 stale 坐标统一拒绝，
  防止旧坐标在 SIM、现场 metadata 或重传路径中重新变成实时目标点。新增协议与地理位置回归后，
  `tests/test_operator_protocol.py`、`tests/test_target_geolocation.py` 以及完整 goal acceptance 均通过。
  v30 阶段的 canonical `configs/release-parity.json` 已由生成器同步，`task_contract_hash` 更新为
  `965c7bb34e606af824a7f71318d4d8fa9c929655d616ff0b829e919e621ab7f1`，文件 SHA-256 为
  `676817B29C868FCEF90606BFD9E8FE0A2A6DD69E96177E6AE7B7AE0506A86964`；随后 v31 极点门重算为
  `54BB83A9E7091259C38BE9A17B4996BF7B9B810BF69DA76D45F29315A9993A62`。
- v30 阶段 goal acceptance 为
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-target-geo-age-v30.json`
  （SHA-256 `772672FE9FEAAD9369DC8B31E1A6F5B30F1EB2C1A0279ED53A3C51F2A7FD95D1`）：核心测试收集 `2097`，
  QGC 自定义测试 `66/66`，脚本退出码 `0`，software=`pass_with_environment_gap`、
  hardware=`offline_pending`。它绑定当前 pre.8 portable EXE，但不声称真实 Jetson、室外 GNSS、
  Pixhawk/执行器、HIL/REAL 或人工 Fly View 已通过。
- 新增 `src/multidetect/field_link_preflight.py` 与 `scripts/run_field_link_preflight.py`，为现场联调提供可重复的 Jetson SSH、GR01 TCP、相机 RTSP TCP 和本机 UDP metadata 端口预检；输出 JSON 证据并明确区分“端口可达”与 GNSS/传感器/执行器验收。新增 `tests/test_field_link_preflight.py` 覆盖 TCP 成功路径和 UDP 端口碰撞。
- 台式机现场预检证据写入 `artifacts/evaluation/field-link-preflight-20260831-require-all.json`：本机 `14580/14581` 均可绑定，但 `192.168.144.20:22`、`192.168.144.11:5760` 与 `192.168.144.108:554` 均超时；`--require-all` 正确返回退出码 `1`，因此 Jetson/GR01/相机现场链路继续保持 pending。
- 本轮继续执行非破坏性现场链路预检，证据为
  `artifacts/evaluation/field-link-preflight-20260831-continue.json`（SHA-256
  `692A89C9E2A6606894FA33D9038E36AB28834A93717279A331CA59045B06E31A`）：本机 metadata/QGC
  UDP 端口仍可绑定，但 Jetson/GR01/RTSP TCP 均超时，`hardware_link_ready=false`；未发送登录、
  MAVLink、视频或控制数据。
- 同日 follow-up 预检写入 `artifacts/evaluation/field-link-preflight-20260831-followup.json`（SHA-256 `4CE813D240826C6D9304A943D76FDCA675AF65E39B1B6F1688D0DC32ADE90156`），结果不变：本地 `14580/14581` 可绑定，Jetson/GR01/RTSP TCP 均超时，`hardware_link_ready=false`。
- 现场预检新增 metadata/QGC 固定端口配置级 fail-closed：`metadata_port == qgc_port` 不再因顺序 bind 而假通过；异常端口类型/范围也会转为失败探针，并由 `tests/test_field_link_preflight.py` 回归覆盖。
- 现场预检新增可选 `--protocol-checks`：Jetson 端被动读取 SSH identification banner，GR01 端被动识别完整 MAVLink v1/v2 帧，相机端只发送 `OPTIONS * RTSP/1.0` 并接受可解析的 2xx–5xx 状态（401/403 标记为“服务可达、需要认证”）。不会发送登录、心跳、视频启动或飞控控制数据；协议失败在 `--require-all` 下继续 fail-closed。
- `tests/test_field_link_preflight.py` 新增 SSH/MAVLink v1/MAVLink v2/RTSP 401 和 combined-report 回归，确认协议探测不会向 SSH/MAVLink 发送数据，并且协议门会参与 `hardware_link_ready` 聚合。
- 现场预检新增有限次数稳定性 watch：`--samples N` 重复整套端口/协议探测，`--interval-s` 控制间隔，`--stop-on-failure` 可在首次掉线时收口；聚合报告记录样本数、首个失败位置、稳定率、连续失败峰值和停止原因，任何未采满或失败样本均保持 `hardware_link_ready=false`。watch 与单次预检共用只读边界，不登录、不请求 MAVLink stream、不启动 RTSP 视频、不发送飞控控制数据；新增回归覆盖完整采样、抖动与首失败停止。
- 台式机迁移后的三次现场协议 watch 证据为 `artifacts/evaluation/field-link-watch-20260831-continuation.json`（SHA-256 `17743B74CC2A1A276BDD4C0BAB4B0026F8A6D4C1F834BBDB00E5418EE4F63167`）：本机 UDP 端口可绑定，但 Jetson/GR01/RTSP 连续超时，`max_consecutive_failures=3`、`stability_ratio=0.0`，因此硬件链路仍保持 `offline_pending`。
- 新增 `src/multidetect/field_session_acceptance.py` 与 `scripts/validate_field_session.py`，把真实现场 JSONL 的 `DET → TRK → LCK → TGT`、取消后重捕获、GPS fix/卫星数/hAcc/vAcc/age、位置健康/坐标边界和 `RANGE_STATUS` 双新鲜度收口为可重复离线 gate；验证器兼容扁平记录与 `AuditLog` 的 `event_type/details` 信封，支持 MAVLink `eph/epv` 厘米值和 `slant_range_m`；该 gate 仅验证输入证据契约，不提升为真实飞行验收。
- 现场会话 gate 进一步收口证据关联：记录时间戳必须单调不回退，合格 GPS 与 `RANGE_STATUS` 必须通过相同目标 ID 或同一记录中的明确配对建立绑定；串目标、根字段与嵌套字段冲突、以及只有跨记录匿名样本的 JSONL 均 fail-closed，并新增对应回归测试。
- `operator.remote_range_status` 审计现在保存 `source_frame_id`、源/产出时间及派生 `source_age_s`，使现场会话复核可重新执行 RANGE_STATUS source-age 门，而不是只依赖 wire 包内部字段。
- 新增 `src/multidetect/field_session_capture.py`、`scripts/capture_field_session.py` 和安装入口
  `multidetect-field-capture`：只读尾随 AuditLog/JSONL，支持 `session_id`、sequence/timestamp
  起点、`DET/TRK/LCK/TGT` 停止状态、最大记录数、时长和空闲超时；每条完整记录 flush/fsync 到
  独立证据流，并生成整份源 SHA-256、已消费源前缀 SHA-256、输出文件 SHA-256、筛选/损坏行、
  未完成尾部、停止原因和共享现场 gate 结果。新增 `verify_field_session_capture.py` 与
  `multidetect-field-capture-verify` 入口，可在交接后重算输出 gate 并验证源消费前缀；源日志
  收口后的追加内容只作为变化诊断，不会污染已采集证据。
  采集错误与 `field_session_acceptance` 失败都会使 `gate_passed=false`，不会把半截现场日志写成
  完整通过；新增捕获器单元回归覆盖 session 筛选、目标状态收口、损坏行、尾部和覆盖保护。

- 收口 QGC 目标证据坐标边界：`MultiDetectTargetEvidenceStore` 的新保存与 JSONL 重载均拒绝
  精确 `±90°` 纬度，`FlyViewCustomLayer.qml` 的合格落图与历史复制使用同一严格 `(-90°,90°)`
  规则；经度继续允许 `[-180°,180°]`。这样不会出现“记录可复制但地图不能显示”的极点分叉。
- 目标回放 v1 的 GPS 质量契约继续保持 `observed_at_s`/`gps_age_s`/`gps_freshness` 派生新鲜度、
  垂直精度 `<=10 m` 和 live `position_healthy` 语义说明；旧回放 bundle 兼容策略未改变。
- 本轮完成测距新鲜度协议收口：`VALID/DEGRADED` `RANGE_STATUS` 的 source frame age 与
  data freshness 必须同时不超过 `1.5 s`；Python 编解码器、QGC C++ 解码器和 `MultiDetectState.qml`
  同步拒绝/清除过期距离，证据快照不保存 stale range/bearing。地图证据继续拒绝精确极点纬度，
  保持 `(-90,90)` 与 `[-180,180]` 坐标域一致。QGC 静态 `82 passed`、协议/模拟器聚焦回归、三个
  C++ self-test 和全量 Python 质量门通过；未覆盖真实 Pixhawk/Jetson、室外 GNSS、HIL/REAL、实飞、
  人工 Fly View 或最终 `v0.3.0`，历史成功/失败证据均未覆盖。

### Verification refresh (pole boundary, 2026-09-01)

- Rebuilt the pre.8 Windows Release and canonical portable tree after moving exact-pole WGS-84 validation into the QGC native `TargetGeolocationStatus` decoder. Release and portable `MultiDetectGCS.exe` are byte-identical (`46,575,104` bytes, SHA-256 `61F16B6A13247316E356C7B626E79BB1033A6AC03C54D4DD676F75BCCD4CE497`).
- The portable `--offline --simple-boot-test --allow-multiple --no-windows-assert-ui --log-output` run exited `0`; log evidence is `artifacts/evaluation/qgc-simple-boot-20260901-target-geo-pole.log` (SHA-256 `E2BF6D1E4317AA721984583578D944B0D7285A8754F7D62C2BC67F91AB73CAB2`). Operator protocol, 44-fragment depth-grid, and RTSP wire C++ self-tests all exited `0`.
- Full goal acceptance is `artifacts/evaluation/multidetect-goal-acceptance-20260901-target-geo-pole-v31.json` (SHA-256 `CC6371291F416E62E1308C78CF70CC2E08B10854D7B5A5817194E5B5164D18EE`), with `2101` core tests, QGC custom `66/66`, software=`pass_with_environment_gap`, hardware=`offline_pending`. The canonical parity file and same-byte audit snapshot hash is `54BB83A9E7091259C38BE9A17B4996BF7B9B810BF69DA76D45F29315A9993A62`.

### Verification refresh (2026-08-31)

- Rebuilt the canonical Windows portable tree from the current pre.8 Release output after the offline depth-grid initialization fix; `Release/MultiDetectGCS.exe` and `portable-v0.3.0-pre.8/bin/MultiDetectGCS.exe` are byte-identical (`46,575,104` bytes, SHA-256 `F847DF0503744BA7B5C2E9446330DB3A39A5BA8CBEA7BB17BA542A133EB7725C`). The previous `759C8381C5ED34D791C8A6E9966FD3231196B1727AE1C2DD1DA946F77275E9C3` image remains historical provenance for the pre-fix rebuild.
- When no HMAC credential is configured, QGC now skips creating the depth-grid UDP receiver instead of emitting `invalid depth-grid receiver configuration`; fully configured deployments still pass the signed endpoint, port-range, and collision checks. The new portable `--offline --simple-boot-test` exited `0`; log evidence is `artifacts/evaluation/qgc-simple-boot-20260831-depth-grid-fix.log` (SHA-256 `AED8F43CD039490BA5B775C3A3FE4A7DBAC957FE228B5BEC6FE7A20B5E6AD80F`).
- The canonical portable closure contains `1,013` files / `529,288,077` bytes (`bin/` contains `233` files), with Qt, QML, translations, GStreamer, plugins, and the RTSP helper retained alongside the EXE.
- Re-ran the shared SIM control gates after the QGC rebuild: `simulator-3d-control-acceptance-20260831-followup.json` (SHA-256 `56099CE79D65DF7D240BFC633E3479EA848053E4665F62AA0B6157F2509A82AC`), `simulator-ai-acceptance-20260831-followup.json` (SHA-256 `121FEA672C027C299A3B40B9D7F249EC15DD3FB46CA17C2FC6D18BE3221AC84E`), and `simulator-closed-loop-acceptance-20260831-followup.json` (SHA-256 `11195BBC7B8E28082A446524040A32F8D026B7B9C87164F85389771C17ECA949`) all report `gate_passed=true`. The 3-D external PX4/Gazebo tools remain unavailable, so these are deterministic protocol/asset gates and do not claim visual, hardware, HIL, or REAL acceptance.
- Added randomized high-user-port allocation to `tests/test_simulator_orchestrator_interactive.py` so parallel Windows runs do not repeatedly collide with a fixed `30000`-series UDP tuple; the interactive orchestrator regression passes.
- Fresh goal acceptance: `artifacts/evaluation/multidetect-goal-acceptance-20260831-range-freshness-qa3.json` (SHA-256 `4CCE5185EA57EE41E8EE8486ECE535B4294FF6BCA20CC82961CE7B435DB1F295`), script exit `0`, `2022` core tests collected, QGC custom `66/66`, software=`pass_with_environment_gap`, hardware=`offline_pending`; Jetson/camera/GNSS and human Fly View gates remain open.
- 多旋翼路径接入后的最新 pre.8 goal acceptance：`artifacts/evaluation/multidetect-goal-acceptance-20260831-multirotor-v15.json`（SHA-256 `0290D69C8CE7822A5B4986D226396EDBB57ABC802D427C3CCAF6B4D6D4859B17`），脚本退出码 `0`，核心测试收集 `2064`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`；该快照绑定当前 portable EXE（SHA-256 `F847DF0503744BA7B5C2E9446330DB3A39A5BA8CBEA7BB17BA542A133EB7725C`）和 `v0.3.0-pre.8`，多旋翼 deterministic 3-D asset/control gate 通过但外部 PX4/Gazebo、室外 GNSS、HIL/REAL 与人工 Fly View 仍未完成。
- 文档与构建指纹收口后的最新 pre.8 goal acceptance 为
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-multirotor-v16.json`
  （SHA-256 `D9A38283F3AB2302F53160A4A312FD48E82C15FE11AF13A61648B9F0BDD1DD32`），脚本退出码 `0`，
  核心测试收集 `2064`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、
  hardware=`offline_pending`；快照绑定 `v0.3.0-pre.8` portable EXE（SHA-256
  `F847DF0503744BA7B5C2E9446330DB3A39A5BA8CBEA7BB17BA542A133EB7725C`）。
  多旋翼 deterministic 3-D asset/control 与 Docker AI preflight 仍通过，但外部 PX4/Gazebo、室外 GNSS、
  HIL/REAL、真实执行器和人工 Fly View 门保持未完成；v15 原文件不覆盖并作为历史 provenance 保留。
- 在 v16 后再次按当前工作树重跑的最新 pre.8 goal acceptance 为
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-multirotor-v17.json`
  （SHA-256 `8F075110292816249F107463571447CD0E7E8C7A84FC326114C1065AD4CBDE1F`），脚本退出码 `0`，
  核心测试收集 `2064`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、
  hardware=`offline_pending`；它绑定同一 `v0.3.0-pre.8` portable EXE（SHA-256
  `F847DF0503744BA7B5C2E9446330DB3A39A5BA8CBEA7BB17BA542A133EB7725C`），并保留外部 PX4/Gazebo、
  室外 GNSS、真实执行器、HIL/REAL 和人工 Fly View 未完成语义。v15/v16 原文件均不覆盖。
- 现场会话验收改动后的新鲜软件门证据：`artifacts/evaluation/multidetect-goal-acceptance-20260831-field-session.json`（SHA-256 `18D531C0397B349847D020C7173A81ADF4C0EAB81BF37F4FDC5434B90421CB62`），脚本退出码 `0`，核心测试收集 `2031`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`；该快照明确保留操作员凭据、Jetson/相机/GNSS、执行器、HIL/REAL 与人工 Fly View 的未完成门。
- 现场会话验收修复后的最新软件门证据：`artifacts/evaluation/multidetect-goal-acceptance-20260831-field-session-v3.json`（SHA-256 `15F0D9014D348A0F59C2B6F924C4C1F4F7FF42B47DFB54B206126F64F3C24D4E`），脚本退出码 `0`，核心测试收集 `2032`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`；该快照确认 `gps_sample` 原始质量字段兼容性回归通过，同时保留操作员凭据、Jetson/相机/GNSS、执行器、HIL/REAL 与人工 Fly View 的未完成门。
- 现场预检端口冲突与 GPS 原始整数门收口后的最新软件门证据：`artifacts/evaluation/multidetect-goal-acceptance-20260831-field-session-v4.json`（SHA-256 `BD4CFA7F0A0BBB42092B96AE571D4697D94AF594C426543EF99CE9C3F267FFAB`），脚本退出码 `0`，核心测试收集 `2036`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`；该快照确认现场验证器的 raw GPS 优先级、MAVLink 整数字段和 metadata/QGC 端口配置回归通过，真实 Jetson/相机/GNSS、执行器、HIL/REAL 与人工 Fly View 门仍保持 pending。
- 最新现场会话验收快照：`artifacts/evaluation/multidetect-goal-acceptance-20260831-field-session-v5.json`（SHA-256 `1EB7199225D16522F7535996A05C02C12E06965EED03809B9CC0C5CAC871BB69`），脚本退出码 `0`，核心测试收集 `2037`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`；该快照还覆盖非法端口范围的 fail-closed 回归，真实 Jetson/相机/GNSS、执行器、HIL/REAL 与人工 Fly View 门仍保持 pending。
- 最新现场会话验收快照：`artifacts/evaluation/multidetect-goal-acceptance-20260831-field-session-v6.json`（SHA-256 `974243F534089ADFC45FE767738CC5053A658B7305DD4CFB950494BE13BD765C`），脚本退出码 `0`，核心测试收集 `2037`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`；该快照包含 depth-grid 离线启动修复与 release-parity 重新生成，真实 Jetson/相机/GNSS、执行器、HIL/REAL 与人工 Fly View 门仍保持 pending。
- 目标绑定/时间单调性收口后的现场软件门快照：`artifacts/evaluation/multidetect-goal-acceptance-20260831-field-binding-v7.json`（SHA-256 `32F58AA8C416968C1E63DA0848BD134229F8BE8356A6F116B35FCC2FDFBC5811`），脚本退出码 `0`，核心测试收集 `2041`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`；同日 `field-session-fixture-20260831.validation-current.json` 验证 `gate_passed=true`，匿名跨记录、串目标和乱序时间回归均 fail-closed。该快照作为 v8 前一版 provenance 保留。
- 缺失/负数时间戳也收口后的现场软件门快照：`artifacts/evaluation/multidetect-goal-acceptance-20260831-field-binding-v8.json`（SHA-256 `6A7B20056F07B125133F50A03097C1CAAC4D08D233810D8925DD30E10E083202`），脚本退出码 `0`，核心测试收集 `2042`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`；`field-session-fixture-20260831.validation-current.json` 仍为 `gate_passed=true`，匿名跨记录、串目标、乱序、缺失和负数时间回归均 fail-closed。该快照作为 v9 前一版 provenance 保留。
- 嵌套传感器父节点目标 ID 也纳入绑定后的最新现场软件门快照：`artifacts/evaluation/multidetect-goal-acceptance-20260831-field-binding-v9.json`（SHA-256 `ABF6E82C1638E1FDE4D9D8BDEB1B266C5954EDC4E04904C5DCD7D5FA94F8E8AB`），脚本退出码 `0`，核心测试收集 `2045`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`；`field-session-fixture-20260831.validation-current.json` 为 `gate_passed=true`，匿名跨记录、串目标、嵌套父节点冲突、乱序、缺失和负数时间回归均 fail-closed。真实 Jetson/相机/GNSS、执行器、HIL/REAL 与人工 Fly View 门仍保持 pending。
- 嵌套别名路径解析修正后的最新现场软件门快照：`artifacts/evaluation/multidetect-goal-acceptance-20260831-field-binding-v10.json`（SHA-256 `2FE0BA7FCECB8C1F2E4B87DE99F428F2A3952C18B109C886D976A75C9A7E7743`），脚本退出码 `0`，核心测试收集 `2045`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`；现场 fixture 仍为 `gate_passed=true`，目标绑定和时间单调性回归全绿。真实 Jetson/相机/GNSS、执行器、HIL/REAL 与人工 Fly View 门仍保持 pending。

### Verification refresh (field capture, 2026-08-31)

- 新增现场会话只读采集器后的最新 pre.8 软件门快照：`artifacts/evaluation/multidetect-goal-acceptance-20260831-field-capture-v11.json`（SHA-256 `51D20F3EDDCBED4F85DD5D2F64419A5479E5544DD309B28B1B4A23C8A234C485`），脚本退出码 `0`，核心测试收集 `2051`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`。`capture_field_session.py` 的 session 筛选、目标状态收口、实时 fsync、坏行/半行报告和共享现场 gate 均有回归；真实 Jetson/相机/GNSS、执行器、HIL/REAL 与人工 Fly View 门仍保持 pending。
- 在 CLI 默认报告路径与 sequence 缺失闭锁回归补齐后，最新 pre.8 软件门快照更新为
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-field-capture-v12.json`（SHA-256
  `C96DA6C476A2B4B053E576F423113B5EA02276E665DC430E9415845788B800C8`），脚本退出码 `0`，核心测试收集
  `2053`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`；
  采集器新增测试仍覆盖 session 筛选、目标状态收口、实时 fsync、坏行/半行报告、覆盖保护和序号筛选闭锁。
- 在采集前缀/输出 hash 绑定与交接复核器回归补齐后，最新 pre.8 软件门快照更新为
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-field-capture-verify-v13.json`（SHA-256
  `FC15F9C59CE8FA3900FA8D53292D8BB7C8FAC140B8D3773127167FB9A5C9E95D`），脚本退出码 `0`，核心测试收集
  `2055`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`；
  `verify_field_session_capture.py` 会重算复制 JSONL 的现场 gate，校验 `source_prefix_sha256` 与
  `captured_output_sha256`，并把源日志收口后的追加记录仅标为诊断信息。
- 根包公开导出与最终本轮回归后的最新 pre.8 软件门快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-field-capture-verify-v14.json`（SHA-256
  `68C9F93C90D3790F3AA8D2F9F11EC8715795F6A0C6CD39A46E091207C264F7EA`），脚本退出码 `0`，核心测试收集
  `2055`，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、hardware=`offline_pending`；
  当前 portable EXE 指纹和所有真实硬件/室外 GNSS/HIL/REAL/人工 Fly View 未完成门保持不变。

### Verification (2026-08-31)

- 全量 Python 回归在台式机项目 venv 中完成 QA20 验收，核心 pytest 收集 `2001` 项；QGC 自定义回归
  `64/64`，退出码 `0`。收集数量来自本次运行输出统计，不是 QA20 JSON 的额外字段；Windows 符号链接能力
  仍作为环境差异单独记录，未改变 `software_status=pass_with_environment_gap`。
  `ruff check src tests scripts`、`compileall -q src scripts` 与
  `git -c core.whitespace=cr-at-eol diff --check` 均退出码 `0`。
- GPS/目标事件回放与 PX4 ACK 身份门收口后的首个 goal acceptance 快照（历史记录）为：
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-gps-replay.json`
  （SHA-256 `5CB38AA3654130F5025B2227802F9FAE974CD8EC73A1A261460B4E5B14479171`）。随后
  加入 E7 清单生成器、QGC map 生命周期保护和本地证据快照严格校验后重新执行的回归快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-regression.json`
  （历史 SHA-256 `3C7FE0BF8E84BAF8142CFCCC660A21F3E6D4628B5F592CBB48EA631F474769DC`）。
  传感器拆卸回归修复后的 QA 快照（历史记录）为
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa.json`
  （SHA-256 `440AB514681408DB5B583C0AD9A62054441004BD107756C0229423546AF36334`）。随后加入
  release-gate runtime/event 归一化与直接脚本入口回归后的 QA3 历史快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa3.json`
  （历史 SHA-256 `ED550A122814A32D8823CC482A909B1BEF4AC0D234E03D242185E33BC37EA1F3`）。加入
  canonical parity 同步回归后的 QA4 历史快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa4.json`
  （SHA-256 `C9A04BA94DDD159BBBC979735BCD53910335A612B256EA0E1195D512D404D552`），并同步为
  `artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本）。该历史记录的核心测试为
  `1922 passed / 1 skipped`（共收集 `1923` 项）。
  网关 graceful-drain、GPS 原始质量二次校验和物理网卡选择修复后的 QA5/QA6 快照作为历史记录
  保留。Windows PowerShell 5.1 无参数入口与 UTF-8 无 BOM 证据写入修复后的 QA7 历史快照为
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa7.json`
  （SHA-256 `D82F07B6E2935D40D0A16CF87644EB7AD0AD91FE9A67A3620B29CC78583AAAFF`）。该 QA7
  快照已明确标为历史记录；完整脚本退出码为 `0`，核心
  测试 `1937 passed / 1 skipped`（共收集 `1938` 项）、QGC 自定义测试 `63/63`；当时 pre.8
  Release/portable 主 EXE 为 `46,561,280` bytes，SHA-256
  `6079B6E567304E3484C664426B549B0067E86D7A40DACF2F09BBDFD87486D829`（QA7 历史制品）。
  QGC source-age/frame-hold、SIM world-origin 和 QGC 自定义测试更新后的 QA8 快照现作为历史记录保留：
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa8.json`（SHA-256
  `A9C1AC9CE94B19BDE4AB551C27EEC4780554D601C913BE157CD86AE431C5ED98`），并同步为
  `artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本）；QA8 脚本退出码 `0`，核心测试
  收集 `1943` 项、QGC 自定义回归 `64/64`，当时 Release/portable 主 EXE 为 `46,571,008`
  bytes，SHA-256 `C3E0FE07E7600CF0968B54A70EEDA7804007B65FE2EF4E6A4B4598E076013727`。
  回放事件现在绑定 `gps.observed_at_s`、`gps_age_s` 与 `gps_freshness`，未来/过期 GPS
  fail-closed；QGC 1σ 圆使用 WGS-84 局部曲率并归一化跨日期变更线经度；map 未就绪时
  不再解引用，保存证据时只有当前目标的有限、合法 VALID/DEGRADED 测距才写入距离/方位，
  本地 JSONL 损坏记录会被拒绝而不是还原成假目标。该快照仍为
  `software_status=pass_with_environment_gap`、`hardware_status=offline_pending`，不提升
  真实 Pixhawk/Jetson/相机、室外 GNSS、HIL/REAL、实飞或人工 Fly View 状态。
- QA10 已降为历史记录（原文件与 SHA 保留）：
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa10.json`，SHA-256
  `1F3515C50D2EA95D481450189ED72AC2FCBD814B965F342C5777BED6D30B6F65`。
  QA11 已降为历史记录；其原始文件与 SHA 保留：
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa11.json`（SHA-256
  `FFB41D20E2C220C8391D82DD062AF44C39DED020DBD2CD12C3B702A169403993`），脚本退出码 `0`，曾提升为
  `artifacts/evaluation/multidetect-goal-acceptance-latest.json`。QA11 核心 pytest 收集
  `1964` 项，`1963 passed / 1 skipped`，QGC 自定义回归 `64/64`；QGC protocol self-test、QML parse、
  Ruff、compileall、CRLF-aware diff check、三个 C++ self-test 和 portable headless smoke 均通过。
  当前 Release/portable 主 EXE 为 `46,574,080` bytes，SHA-256
  `E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`。QA11 仍为
  `software_status=pass_with_environment_gap`、`hardware_status=offline_pending`，所以不发布最终
  `v0.3.0`。QA11 在验收启动时额外记录源码快照指纹：`HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、
  `dirty_entry_count=290`、`worktree_clean=false`、`status_sha256=5e156cf0952025104665e287d9a87099acb2e989b8c0b9d67719c19befc86a1e`；
  这是带 provenance 的可复验 dirty snapshot，不是干净提交对应的发布签名制品。
- 同日 QA12 现作为历史 goal acceptance 保留（原始文件和 SHA 不覆盖）：
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa12.json`（SHA-256
  `D10F40F27E401250E677AC80E89ECE7459F5170172499E9EB81E2D65DF6960DF`）。
- QA13 现作为历史 goal acceptance 保留（原始文件和 SHA 不覆盖）：
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa13.json`（SHA-256
  `EF1684856B274607A2C39F951D620803149495C9F323863CC096788528E16069`）。QA13 只确认 canonical parity
  漂移已修正，不改变 `offline_pending` 或最终 `v0.3.0` 发布门状态。
- QA14 现作为历史 goal acceptance 保留（原始文件和 SHA 不覆盖）：
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa14.json`（SHA-256
  `6856130808EDEB05C543DE24A42F2C17545725C5D6615338ECF52613B5CA3277`）。
- QA15 现作为历史 goal acceptance 保留（原始文件和 SHA 不覆盖）：
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa15.json`（SHA-256
  `B978D244A7440B3C102E1BDD8408110F511BE2C0209C4BC7DDB340EE56487808`），曾提升为
  `artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本；现由 QA22 指向）。QA15 核心 pytest 收集
  `1975` 项，`1974 passed / 1 skipped`；QGC 自定义回归 `64/64`，QGC protocol self-test、QML parse、
  Ruff、compileall、CRLF-aware diff check、三个 C++ self-test 和 portable headless smoke 均通过。当时
  Release/portable 主 EXE 为 `46,574,080` bytes，SHA-256
  `E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`。QA15 的
  `software_status=pass_with_environment_gap`、`hardware_status=offline_pending`，不发布最终 `v0.3.0`。
  provenance 为 `HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、`dirty_entry_count=293`、
  `worktree_clean=false`、`status_sha256=b98a96dd5850dac360714ff836f0818da09338cc382a4bbe3af1f01c3235dbba`。
- QA16 现作为历史 goal acceptance 保留（原始文件和 SHA 不覆盖）：
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa16.json`（SHA-256
  `403B319255A9C2446012BD060C85BB2633DB9A4D7DE45CF4B9BB495D96B234EE`），曾提升为
  `artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本；现由 QA22 指向）。QA16 核心 pytest 收集
  `1976` 项，`1975 passed / 1 skipped`；QGC 自定义回归 `64/64`，QGC protocol self-test、QML parse、
  Ruff、compileall、CRLF-aware diff check、三个 C++ self-test 和 portable headless smoke 均通过。当时
  Release/portable 主 EXE 为 `46,574,080` bytes，SHA-256
  `E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`。QA16 仍为
  `software_status=pass_with_environment_gap`、`hardware_status=offline_pending`，并保持
  `operator_visual_acceptance=not_claimed`，所以不发布最终 `v0.3.0`。provenance 为
  `HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、`dirty_entry_count=293`、
  `worktree_clean=false`、`status_sha256=b98a96dd5850dac360714ff836f0818da09338cc382a4bbe3af1f01c3235dbba`。
  QA16 复验 GPS_RAW_INT 缺失 `alt`、降级 fix、非法坐标、未知卫星哨兵和未来时间戳的原子
  fail-closed 语义，并在失效时恢复独立 EKF/LOCAL 高度；SIM `arm_on_start`、Docker 标准路径回退
  和 canonical parity 继续有效。该快照只覆盖软件/协议门，不升级真实硬件、室外 GNSS、HIL/REAL、
  实飞或人工 Fly View 状态。
- QA17 现作为历史 goal acceptance 保留（原始文件和 SHA 不覆盖）：
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-final-qa17.json`（SHA-256
  `BF93CE4FFD7DEE0CA9D166614DAF8D6216BF0ACC7D09808DDF913B3E11D6FFA2`），曾提升为
  `artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本；现由 QA22 指向）。QA17 核心 pytest 收集
  `2000` 项，运行输出为 `1999 passed / 1 skipped`；QGC 自定义回归 `64/64`，QGC protocol self-test、QML parse、
  Ruff、compileall、CRLF-aware diff check、三个 C++ self-test 和 portable headless smoke 均通过。当时
  Release/portable 主 EXE 仍为 `46,574,080` bytes，SHA-256
  `E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`。QA17 仍为
  `software_status=pass_with_environment_gap`、`hardware_status=offline_pending`，并保持
  `operator_visual_acceptance=not_claimed`，所以不发布最终 `v0.3.0`。provenance 为
  `HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、`dirty_entry_count=293`、
  `worktree_clean=false`、`status_sha256=b98a96dd5850dac360714ff836f0818da09338cc382a4bbe3af1f01c3235dbba`。
  QA17 已验证 GPI/GPS 原子解析与非法 sentinel 回归：无效坐标、未知卫星、缺失高度、降级 fix 或未来
  时间戳不会复用旧 raw 新鲜度/坐标，失效时恢复独立 EKF/LOCAL 高度；SIM `arm_on_start`、Docker 标准路径
  回退和 canonical parity 继续有效。该快照只覆盖软件/协议门，不升级真实硬件、室外 GNSS、HIL/REAL、
  实飞或人工 Fly View 状态。
- QA18 现作为历史 goal acceptance 保留（原始文件和 SHA 不覆盖）：
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-final-qa18.json`（SHA-256
  `230A2EC5EC44982DE4563AEAEB949E32ACD85123D706C320E056EA7BF3951F7F`），脚本退出码 `0`，并已提升为
  `artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本；现由 QA22 指向）。QA18 核心 pytest 收集
  `2000` 项，运行输出为 `1999 passed / 1 skipped`（唯一跳过项是该 Windows 账户不支持符号链接）；这是当次
  全量运行输出统计，不是 artifact 的额外字段。QGC 自定义回归 `64/64`；QGC protocol self-test、QML parse、
  Ruff、compileall、CRLF-aware diff check、三个 C++ self-test 和 portable headless smoke 均通过。当前
  当时 Release/portable 主 EXE 为 `46,574,080` bytes，SHA-256
  `E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`。QA18 的
  `software_status=pass_with_environment_gap`、`hardware_status=offline_pending`，并保持
  `operator_visual_acceptance=not_claimed`，所以不发布最终 `v0.3.0`。provenance 为
  `HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、`dirty_entry_count=293`、
  `worktree_clean=false`、`status_sha256=b98a96dd5850dac360714ff836f0818da09338cc382a4bbe3af1f01c3235dbba`。
  QA18 补充并验证 GPI `GLOBAL_POSITION_INT` 与 GPS 原子解析/非法 sentinel 语义：坐标、卫星数、
  高度、fix 和时间戳在同一帧内原子校验；任一非法或缺失字段都会清除对应 raw freshness/坐标，拒绝未来
  时间戳，并恢复独立 EKF/LOCAL 高度，live bridge 与此语义一致。SIM `arm_on_start`、Docker 标准路径
  回退和 canonical parity 继续有效。该历史快照只证明软件/协议门，不把 Docker/SITL 或静态证据升级为真实
  Pixhawk、Jetson、相机、室外 GNSS、执行器、HIL/REAL、实飞或人工 Fly View 结论。
- QA19 现作为历史 goal acceptance 保留（原始文件和 SHA 不覆盖）：
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-final-qa19.json`（SHA-256
  `082092AB58848D498050901C5BD9768AABF2357569C91D72D531DD105E527515`），脚本退出码 `0`，并已提升为
  `artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本；现由 QA22 指向）。QA19 核心 pytest 收集
  `2001` 项；QGC 自定义回归 `64/64`，QGC protocol self-test、QML parse、Ruff、compileall、CRLF-aware
  diff check、三个 C++ self-test 和 portable headless smoke 均通过。当时 Release/portable 主 EXE 为
  `46,574,080` bytes，SHA-256
  `E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`。QA19 的
  `software_status=pass_with_environment_gap`、`hardware_status=offline_pending`，并保持
  `operator_visual_acceptance=not_claimed`，所以不发布最终 `v0.3.0`。provenance 为
  `HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、`dirty_entry_count=293`、
  `worktree_clean=false`、`status_sha256=b98a96dd5850dac360714ff836f0818da09338cc382a4bbe3af1f01c3235dbba`。
  QA19 补充并回归 Pixhawk `GPS_RAW_INT` per-source timestamp cursor 单调性：同一来源的旧/乱序观测不会回退
  freshness、坐标或高度游标；非法 sentinel 与未来时间戳仍原子 fail-closed，独立 EKF/LOCAL 高度恢复和 live
  bridge 语义保持一致。该历史快照只证明软件/协议门，不把 Docker/SITL 或静态证据升级为真实 Pixhawk、Jetson、
  相机、室外 GNSS、执行器、HIL/REAL、实飞或人工 Fly View 结论。
 - QA20 现作为历史 goal acceptance 保留（原始文件和 SHA 不覆盖）：
  `artifacts/evaluation/multidetect-goal-acceptance-20260831-final-qa20.json`（SHA-256
  `624B31E03C04D60CD0FF26386C23DC3E6FED19E011375C31DE8578E897ACE756`），脚本退出码 `0`，并已提升为
  `artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本；现由 QA22 指向）。QA20 核心 pytest 收集
  `2001` 项；QGC 自定义回归 `64/64`，QGC protocol self-test、QML parse、Ruff、compileall、CRLF-aware
  diff check、三个 C++ self-test 和 portable headless smoke 均通过。当前 Release/portable 主 EXE 仍为
  `46,574,080` bytes，SHA-256
  `E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`。QA20 的
  `software_status=pass_with_environment_gap`、`hardware_status=offline_pending`，并保持
  `operator_visual_acceptance=not_claimed`，所以不发布最终 `v0.3.0`。provenance 为
  `HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、`dirty_entry_count=294`、
  `worktree_clean=false`、`status_sha256=3b2fb046735714f18012b659cbd652dc0f84761084980ee3e312ee37f0fc9211`。
  QA20 延续并回归 Pixhawk `GPS_RAW_INT` per-source timestamp cursor 单调性：同一来源的旧/乱序观测不会回退
  freshness、坐标或高度游标；GPI/GPS 坐标、卫星数、高度、fix 与时间戳在同一帧内原子校验，非法
  sentinel 与未来时间戳清除 raw 状态并恢复独立 EKF/LOCAL 高度，live bridge 语义保持一致；同时修复
  `depth_grid_udp` 发送完成后才起算限流窗口的竞态，并以全量回归验证。
  该当前快照只证明软件/协议门，不把 Docker/SITL 或静态证据升级为真实 Pixhawk、Jetson、
  相机、室外 GNSS、执行器、HIL/REAL、实飞或人工 Fly View 结论。
 - QA22 是当前 goal acceptance：`artifacts/evaluation/multidetect-goal-acceptance-20260831-final-qa22.json`
   （SHA-256 `F38EE2065DCB57A1CA2C12A99AD1F401DC4D48DA0F87848D5B6BBDD2913B7ACF`，核心 pytest 收集 `2012` 项，
   QGC 自定义回归 `64/64`，脚本退出码 `0`，latest 与该文件字节一致）。software=`pass_with_environment_gap`、
   hardware=`offline_pending`、dirty=`295`、status SHA=`851a1abde3f79abd859ad032242cee0ee9bfd790663f615c3fe73b6b67d97c74`。
   回放 GPS 质量要求 vAcc≤10 m 且纬经度与 MAVLink fix/satellite 边界对齐；legacy observed_at 缺省、自定义 live policy
   与 position_healthy 语义差异分开记录，均不代表真实硬件通过。
- QA15 之前以当前源码完成的真实 Docker/PX4/Gazebo disposable AI armed-heartbeat 复验（独立 machine evidence）：
  `artifacts/evaluation/simulator-docker-ai-acceptance-20260830T153110Z-qa13.json`（SHA-256
  `678C233C7164E123B0272CF76CC0831F1D601797F7E66877BC6CE463F4FE619C`），容器日志 SHA-256
  `04F57BBF9F7A9FEB7EF5C6990DBAF4187D2AE8B5E3C49E922E3919B568B10EBD`。`gate_passed=true`、
  `telemetry.qualified=true`、`armed_observed=true`、`ai_steps=1`、`ai_policy_gate_passed=true`，
  stop/rm 清理成功；该证据仍仅覆盖 disposable SITL machine gate，不提升真实硬件、室外 GNSS、
  HIL/REAL、执行器、实飞或人工 Fly View 状态。
- QA15 同步收紧 GPS_RAW_INT 的原始质量语义：越界经纬度不再复用上一帧坐标，`satellites_visible=255`
  归一为未知，缺失 `alt` 或合法但降级的 `fix_type` 会清除旧 raw 新鲜度与 GPS-home 高度回退，未来观测
  时间统一 fail-closed；SIM manual-only 场景现在正确应用 `arm_on_start`，三份 PX4/SITL PowerShell
  验收脚本也统一支持 Docker PATH 与标准安装目录回退。上述回归只证明软件/协议行为，不提升真实
  Pixhawk/Jetson、室外 GNSS、HIL/REAL、实飞或人工 Fly View 状态。
- 此前在同一台式机上执行的带 `--exercise-arm`、GPS fix threshold=3 和 PX4 component=1
  的 Docker/PX4/Gazebo disposable AI 闭环（历史 ack-gps run）：
  `artifacts/evaluation/simulator-docker-ai-acceptance-20260830-ack-gps.json`（SHA-256
  `25947F39FF2FA66C02FBD0F2C4CB02C0CA2586701C6FA200B9AD148730A3D279`），容器日志 SHA-256
  `E848D1347A8064C92B39532F1F05BC9938CB8B50FC96BA31E5C0159E0B046220`。该 run 的
  `gate_passed=true`、`runtime_gate_passed=true`、`ai_policy_gate_passed=true`、
  `telemetry.qualified=true`、`armed_observed=true`、`ai_steps=1`，5 个 `COMMAND_ACK` 中
  4 个为目标 PX4 autopilot component=1 的合法 arm/disarm ACK，1 个无关 command 被结构化
  拒绝，且 stop/rm 清理均成功。它仍是 Docker/SITL machine evidence，真实 Pixhawk/Jetson/
  相机/室外 GNSS、执行器、HIL/REAL、实飞和人工 Fly View 继续保持 `not_claimed`。
- 新增 `scripts/create_release_gate_manifest.py`（四模式 E7 清单生成器）：四个 evidence
  路径、五个 parity hash 和人工/硬件状态都必须由调用方显式提供；脚本自动计算 SHA-256、
  写出 schema v1 清单并保持 fail-closed，不会把 goal snapshot、fixture 或 Docker 记录推断成
  缺失的 REPLAY/HIL/REAL。QA7 和 QA8 诊断报告均作为历史记录保留；QA8 诊断报告
  `artifacts/evaluation/release-gate-pre.8-report-final-qa8.json`
  （SHA-256 `2C609808BB8A6B154F92F1F93F4FC91508F5AA88D96DCB496044F49070229566`）明确为
  `gate_passed=false`（缺少四模式清单、REPLAY/HIL/REAL 和最终版本通道）；QA5/QA6/QA7 报告
  以及 QA3/QA4 更早报告均保留为历史 prerelease 门状态证据。
- 新增 `scripts/create_release_parity.py` 与 `configs/release-parity.json`：五组共享契约使用
  固定 framing 和显式源码列表计算，纳入实际 QGC qrc/QML/C++ header surface，并拒绝生成目录、
  越界路径和符号链接逃逸。当前 parity 输入 SHA-256 为
  `F585455FC0E0E36B163B1150BE1889611928FA118F7698FE22EEEE16341B28CC`（GPS 原始质量门、网关
  graceful-drain、QGC source-age/frame-hold、SIM world-origin、GPS sentinel/constant 与离线 depth-grid 初始化
  修正后重新生成；同字节审计快照为 `artifacts/evaluation/release-parity-pre.8-20260831-depth-grid-fix.json`，此前的
  `release-parity-pre.8-20260830-gps-sentinel-constants.json`、
  `gps-quality-gateway-qgc-age-world-origin.json`、`gps-quality-gateway-drain.json`、`gps-quality.json`、
  `gps-replay.json` 和 `release-parity-pre.8-20260830.json` 保留为历史快照）；
  契约修改后必须重算。
- QGC 原生 Fly View 取消状态机补上静态回归：一次 LCK/TGT 操作发送 `DEMOTE_TRK` 并进入
  TRK，下一次才发送当前目标的 `CANCEL_TRK`，待 ACK 期间不会留下旧方框或误取消其它 TRK。
- 统一 `ManualClock`/`RuntimeDescriptor` 接线后执行的完整 goal acceptance 历史快照为：
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-clock-descriptor-retry.json`
  （SHA-256 `A67D85121E202BE028C819E275848DBC8844BC117C2CC06945706344062372B9`），
  核心回归收集 `1860` 项、QGC 自定义测试 `63/63`，软件状态仍为
  `pass_with_environment_gap`、硬件状态仍为 `offline_pending`；该文件保留为历史证据，
  不再代表当前 `latest`。
- 证据卡片空值格式化回归接入后执行的 goal acceptance 历史快照为：
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-rebuilt-evidence-card.json`
  （SHA-256 `1EA3C005B02B8E7022C33BB4446D2E7C21585495BB66C8AACF7F5244E2AFA881`），
  核心回归为 `1860` 项、QGC 自定义测试 `63/63`；该文件同样保留为历史证据。
- 交互验收共享时钟/descriptor 回归接入后重新执行的完整 goal acceptance 历史快照：
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-interactive-clock.json`
  （SHA-256 `5AC0A405BF6B32405903FFFB8E58D37DAF45F817979A5E5275762854FBDB363B`），
  核心回归为 `1861` 项、QGC 自定义测试 `63/63`，软件状态仍为
  `pass_with_environment_gap`、硬件状态仍为 `offline_pending`；当时同步为
  `artifacts/evaluation/multidetect-goal-acceptance-latest.json`，现已由 GPS/回放快照取代。
- `tests/test_qgc_native_flyview_style.py` 新增目标落图回归：要求合格 GPS/目标身份/1σ
  gate、WGS-84 map projection、无效定位清除 marker、1σ 圆、证据卡片复制/保存/记录按钮，
  并确认该能力仍在原生 Fly View 单窗口内。
- `tests/test_target_event_replay.py` 新增 7 项回归，覆盖 bundle round-trip、按目标/时间筛选、
  manifest/event/media 篡改拒绝、GPS/range quality gate、非空目录保护、检查脚本输出和 schema
  结构；该批测试随全量回归一起通过。
- `simulator-interactive-acceptance --mode both` 的 VTOL/fixed-wing deterministic machine gate
  均为 `gate_passed=true`，每种模式均覆盖 AI 接受 2 次、QGC 手动帧 1 次、只读遥测转发 1 次、
  watchdog neutral 1 次；飞控/任务写入保持阻断，证据为
  `artifacts/evaluation/simulator-interactive-acceptance-pre.8.json`。主机缺少 `px4`/`gz`，
  因此 external 3-D runtime、桌面视觉和硬件 claims 仍为 `not_claimed`。
- 同一 interactive acceptance 在 2026-08-30 重新运行并写入
  `artifacts/evaluation/simulator-interactive-acceptance-20260830-rerun.json`（SHA-256
  `33FF690CE41B1E77E3B8D2EE035E36255AD9370F7BD35B248647A75DFFE52B03`），`gate_passed=true`，
  两个 mode 均通过 manual takeover、AI resume、watchdog neutral、只读遥测转发、身份/loopback
  和 blocked-write 检查；该 fixture 仍不提升外部 3-D、桌面视觉或硬件 claims。
- 交互验收 fixture 随后收口到共享 `ManualClock`/`RuntimeDescriptor`：QGC 路由 duration、AI、
  watchdog、飞行模型与 control receipt 全部使用同一 SIM 时间轴，并新增 descriptor/hash 绑定。
  新证据为 `artifacts/evaluation/simulator-interactive-acceptance-20260830-clock-descriptor.json`
  （SHA-256 `6C237C5B1759706B7E563398D0E839A0E90FA7DF6FFEB96C0F3CF9E33AE0DDF5`），
  `gate_passed=true`，VTOL/fixed-wing 均为 deterministic、router duration `0.3s`；该 fixture
  仍不提升外部 3-D、桌面视觉或硬件 claims。
- `artifacts/evaluation/simulator-docker-ai-acceptance-20260829-runtime.json` 保留为失败证据：
  Docker runtime 子门通过，但 `ai_policy_gate_passed=false`、`ai_steps=0`，车辆未报告
  `armed` heartbeat；该文件不能被写成 AI 控制通过，也不改变最终 `v0.3.0` 未发布状态。
- Docker Desktop AI acceptance 的 UDP hairpin 修复回归已封存于
  `artifacts/evaluation/simulator-docker-ai-acceptance-20260830-fixed.json`（SHA-256
  `8C43D86830783AFBE1C5CF45C50B7EAA108555259DDD3BBD809FBC2A300AB8CC`），容器日志 SHA-256
  `614B3B400F54404D061224549B4D195C7DF31ECF1A581783390D0C86E48FF7C9`。control `24540`
  保持 loopback host publication，telemetry `14541` 改为 `container_to_host`/
  `outbound_only` 且不发布；因此 `runtime_gate_passed=true`、`telemetry.qualified=true`
  （4057 observed / 3956 accepted / 101 rejected，wildcard fallback=false），容器清理
  `removed=true`。车辆仍 `armed=false`，`ai_policy_gate_passed=false`、`ai_steps=0`，总
  `gate_passed=false`；该结果只证明 telemetry/runtime 修复，不是 AI 控制成功。
- 显式 disposable SITL arm 闭环已通过：
  `artifacts/evaluation/simulator-docker-ai-acceptance-20260830-arm-v6.json`（SHA-256
  `35A19F6FF3656C82F170AB2061A044CFFA0DB9C24EB125CE21914132E7684D62`），容器日志 SHA-256
  `5F34112A19F05FF13DEAAD270EE53444494F6AD444D0EA4A83F9EEF1BCCC9BA5`。显式
  `--exercise-arm` 在 disposable PX4/Gazebo 容器内完成四项 arm 预检参数、4 次有界
  `MAV_CMD_COMPONENT_ARM_DISARM` 尝试（前两次 temporary reject，后两次 accepted）、
  `armed_observed=true`、`ai_steps=1`/`ai_accepted=1`，并在 finally 发送 disarm；
  `runtime_gate_passed=true`、`ai_policy_gate_passed=true`、总 `gate_passed=true`。
  该证据仍只覆盖 Docker/PX4/Gazebo SITL machine gate，`real_control_acceptance`、
  物理执行器、真实 Pixhawk/Jetson/GNSS/相机、HIL/REAL、实飞和人工 QGC 视觉保持
  `not_claimed`。
- 台式机迁移后的 Docker 路径复验已通过：
  `artifacts/evaluation/simulator-docker-ai-acceptance-20260830-desktop-rerun.json`
  （SHA-256 `A0D561EBD3907B10578990E4713562749BE186CF8D7BF9AF5EF2FC902E4A62B5`），容器日志
  SHA-256 `8A1F4F7927B3D4AA5D9F1932102892E70F72EB5E2D06951B840451166F28D893`。通过仓库自动
  回退的 Docker Desktop `28.4.0` daemon，`runtime_gate_passed=true`、
  `telemetry.qualified=true`、`armed_observed=true`、`ai_policy_gate_passed=true`、
  `ai_steps=1`/`ai_accepted=1`，并完成 disarm 与容器 stop/rm 清理；该 machine gate
  仍不提升真实 Pixhawk/Jetson/GNSS、执行器、HIL/REAL、实飞或人工 QGC 视觉 claims。
- 2026-08-30 的完整 disposable PX4/Gazebo/QGC/Mission combined SIM gate（历史基线）在传感器 transport
  teardown 修复后重新跑通：
  `artifacts/evaluation/combined-sim-qgc-20260830T101014Z-56628/combined-evidence.json`
  （309,899 bytes，SHA-256
  `8423E667A9C9668EF8FE4B1AE5D4166E41F3E21F423ED2FD766EBBECB925C37D`），
  `gate_passed=true`。该 run 完成 600 帧同核 integrated SIM/RTSP、传感器 helper
  `helper_exit_code=0`、原生 QGC post-adoption runtime-status ACK、PX4/Gazebo 容器与传感器
  就绪、VTOL MC→FW→MC、7 项 INT-only Mission upload/readback/active、清理，以及原生 toolbar
  指示器精确点击后抽屉开/关（机器 smoke）。工具栏证据为
  `qgc-native-toolbar-indicator-evidence.json`（SHA-256
  `D7886948F4913A555B1C5F756258D5CB6ED82D35AF5019883C659972A0F0CB63`）。该门是 software-only
  SIM/PX4/Gazebo/Mission 证据；`operator_visual_acceptance=not_claimed`、桌面 GPU 视频像素/
  完整 QML rendering、真实 Pixhawk/Jetson/相机/室外 GNSS、执行器、HIL/REAL、实飞和生产检测
  准确率仍保持 `not_claimed`，最终 `v0.3.0` 不发布。此前 `20260830T062624Z-44576` 的成功
  证据继续作为历史记录保留。
 - 2026-09-01 当前 combined SIM gate 在 Docker Desktop 恢复后重新执行 900 帧并通过：
  `artifacts/evaluation/combined-sim-qgc-20260831T234111Z-53232/combined-evidence.json`
  （771,782 bytes，SHA-256
  `5194CC77F45572483B6A17A21FC8FC0DBE222BE62B9A8EB1CC14777C1B464F6C`），
  `gate_passed=true`。该 run 的相机/CameraInfo/NavSat readiness、VTOL MC→FW→MC、7 项
  INT-only Mission、Mission-ready 后 5 个 type-24 ACK、QGC 原生 QQuickItem VideoOutput/
  PiP/fixed-wing 抓帧和清理均通过；仍保持 `operator_visual_acceptance=not_claimed`，不提升
  人工 Fly View、室外 GNSS、真实 Pixhawk/执行器或 HIL/REAL。
- 当前 portable QGC 入口
  `build/windows-v0.3.0-pre.8-release/portable-v0.3.0-pre.8/bin/MultiDetectGCS.exe`
  以 `ground-control/MultiDetectGCS/tools/run_tests.py --binary <portable-exe> --verify-only
  --headless --timeout 45 --verbose` 完成启动冒烟，退出码 `0`；EXE SHA-256 为
  `61F16B6A13247316E356C7B626E79BB1033A6AC03C54D4DD676F75BCCD4CE497`（46,575,104 bytes）。裸
  `Release/MultiDetectGCS.exe` 仍只是链接结果，交付时必须保留 portable 运行时目录。
- 台式机迁移后的完整 goal acceptance 已重新执行并生成
  `artifacts/evaluation/multidetect-goal-acceptance-20260830-key-format.json`（SHA-256
  `3469C05C8C560BB1EFA52393C43BDA6C04F35759F9F1CB30CABAABA1BD623E84`）。结果为
  `software_status=pass_with_environment_gap`、`hardware_status=offline_pending`、
  `core_tests_collected=1857`、QGC 自定义测试 `63/63`、QGC 协议自测/QML 解析/diff
  检查均通过；QGC EXE 指纹与本节 portable 入口一致。由于当前台式机 Ethernet 2
  已断开且 `192.168.144.20` Jetson、`192.168.144.108` 相机均不可达，证据保留
  operator key（含严格 64 位十六进制格式校验）、ChArUco 标定、舵面安全验证、真实
  DET→TRK→LCK→TGT、Jetson soak 等 live gates，未把软件门提升为 HIL/REAL 或最终
  `v0.3.0`。
- 3-D deterministic control/asset gate 已按两个场景重新运行：VTOL
  `artifacts/evaluation/simulator-3d-control-acceptance-20260830-rerun.json`（SHA-256
  `AE338FFEB0833D6BC7B771E1D5562B923CEA21E1FD9C8FC88B2FB2A82A5A1B40`）与 fixed-wing
  `artifacts/evaluation/simulator-3d-control-acceptance-fixedwing-20260830-rerun.json`
  （SHA-256 `2BD93FE5B3591EE14791645E267200D92A9F79516F64A6DD91D42C4ED5051D40`）均为
  `gate_passed=true`、`deterministic_gate_passed=true`，manual/AI/watchdog/shared-authority
  和 truth-free 检查通过；两份证据均记录 `external_runtime_gate_passed=false`、`px4=false`、
  `gazebo=false`，所以 3-D 桌面渲染、人工 Fly View 和硬件字段继续保持 `not_claimed`。

### Tooling

- `SimulatorClosedLoop` 与 manifest `SimulatorScenarioRunner` 现在绑定统一的确定性
  `ManualClock` 和 `RuntimeDescriptor(mode=SIM)`：AI、watchdog、模型 telemetry、control
  receipt 与 evidence 共用一条 monotonic 时间轴，physics tick 成功后才推进时钟并对漂移
  fail-closed；scenario 结果新增 `runtime_descriptor` 与 `runtime_descriptor_hash`，为
  后续 HIL/REAL 同契约对齐提供可核验身份。
- `scripts/gazebo_sensor_stream.py` 现在在退出前排空 Image 回调、显式取消四个 Gazebo
  transport 订阅并提前销毁 `gz.transport13.Node`；修复了已输出完整帧后由 C++ callback
  executor teardown 触发 `std::terminate`（exit 134）的假失败。新增回归覆盖订阅清理与
  缺失 binding 的 fail-closed 语义；当前 600-frame combined SIM 证据中的
  `sensor_transport.helper_exit_code=0`。
- 新增 `multidetect-release-gate` / `scripts/check_release_gate.py` 与
  `configs/schemas/release-gate-manifest.schema.json`。它聚合 `REPLAY`、`SIM`、`HIL`、`REAL`
  四模式证据，重新计算证据 SHA-256，校验 `gate_passed`/runtime mode、同 UI/同核心/同任务
  契约/同安全策略/同 PX4 Mission path parity，并在 `not_claimed`、缺文件或 prerelease
  通道时保持 `release_allowed=false`。新增 `tests/test_release_gate.py` 四项回归。
- `scripts/build_multidetect_windows.ps1` 增加 `-ReleaseGateManifest` 与
  `-RequireReleaseGate`；最终 `channel=release` 会在 CMake 配置前强制 E7 证据，
  prerelease 可显式演练但不会绕过最终版本门。
- `multidetect.simulator probe --json` 增加 Docker daemon health 字段：`daemon_available`、
  `daemon_version`、`daemon_reason`。它区分 Docker 客户端在 PATH/安装目录可见与
  Docker Server 真正可连接两种状态；探测保持只读，不创建容器、不打开 UDP 端口。
  新增 3 项 `tests/test_simulator_isolation_probe.py` 回归覆盖 daemon 成功、daemon
  不可达和 CLI JSON 字段。

- 新增 `scripts/capture_qgc_native_style.ps1`：启动现有 portable
  `MultiDetectGCS.exe`，只在 exact QGC client rectangle 内捕获原生 Fly View，临时移开相交
  外部窗口并在 `finally` 中恢复窗口状态。该工具只生成 machine-smoke 证据，明确保留
  `operator_visual_acceptance=not_claimed`，不会把截图冒充人工验收。
- `scripts/run_goal_acceptance.ps1` 现在显式串接固定翼 software/HIL-preflight
  acceptance 与 `tests/test_qgc_native_flyview_style.py`，主 evidence 同时记录
  `native_qgc_flyview`、单窗口/单视频 surface、无私有主题和 Computer Use 证据边界。
- 目标验收的 QML 语法门改用 Qt 构建链同源的 `qmlcachegen --bare`；直接调用
  `qmlformat` 会因 QGC 自定义导入模块未在裸命令行的 import graph 中解析而误报失败。
- 真实 COCO YOLO26n raw ONNX 已按当前导出环境重新固定：Ultralytics `8.4.92`、ONNX
  `1.22.0`、opset 17，artifact SHA-256 为
  `7ff7b97cad2fbb68d4fc6a74ed5dad67beb91f535e8cc25d5f0d97ae42d2e410`；未改变
  `production_approved=false` 和飞控/物理输出闭锁。

### Added

- 新增 `src/multidetect/target_event_replay.py` 的 hash-bound 目标事件回放契约、
  `scripts/create_target_event_replay.py` 采集清单导入入口和 `scripts/inspect_target_event_replay.py`
  检查入口。它把视频帧/深度图、目标身份、捕获时间、
  GPS 质量与 WGS84 坐标、水平 `1σ`、实际测距、相对方位绑定到本地
  `manifest.json` + `events.jsonl` + `media/` bundle；加载时执行 canonical manifest、事件流和
  每个媒体文件的 SHA-256/字节数、路径 containment、连续序号、单调时间与 GPS/range quality
  gate。`configs/schemas/target-event-replay-manifest.schema.json` 同步描述结构；claims 固定
  为只读证据、不含飞控输出、不含 simulator truth，和 QGC 轻量 `target-snapshots.jsonl` 分开保存。

- `scripts/run_simulator_docker_ai_acceptance.py` 增加显式 `--exercise-arm`：默认仍为被动
  telemetry/AI bridge；启用后等待合格 heartbeat/GPS，应用 disposable SITL arm 预检，沿
  同一 loopback control socket 发送 `MAV_CMD_COMPONENT_ARM_DISARM`，等待真实 armed
  heartbeat 后执行共享 AI policy，并在退出前发送 disarm。所有命令、ACK、重试、参数和
  清理结果都写入 evidence；不会打开真实硬件路径。

- 新增 `scripts/run_simulator_docker_acceptance.py`：在
  `run_simulator_docker.py` 的 hash-bound 计划之上提供显式 `--execute` 外部运行门。它只启动
  pinned PX4/Gazebo disposable container，核对只读 world/model mount、bridge/loopback 端口、
  image digest、环境和 purpose label，被动收 `HEARTBEAT`/GPS 并在 finally 中 stop/rm；默认
  不执行 Docker。证据把 preflight、container runtime、telemetry、QGC visual 和 hardware
  分层，飞行控制发送计数固定为零。

- 新增 `src/multidetect/simulator/px4_launch.py` 与 `px4-launch-plan` CLI。它在 disposable job
  内复制/修补 PX4 `px4-rc.mavlink`，显式绑定 loopback `14540` control 与 `14541` telemetry，
  生成 POSIX `px4-sim-launch.sh` 和 `px4-launch.json`；scenario/stack-plan、源/生成脚本和 wrapper
  均带 SHA-256，PX4 source checkout 保持不变。dry-run 可生成诊断 template，strict readiness
  失败会阻断真实编排启动。
- `external-plan` 新增 `--px4-launch`/`--no-px4-launch`/`--px4-launch-strict`：可把 PX4/Gazebo
  argv/cwd/env/log/依赖顺序与 job-local wrapper 合并成 no-launch evidence，原始 PX4 command 仍
  保存在嵌套 `source_stack_plan`；该 planner 不调用 subprocess、不打开 socket。

- 新增仓库内 fixed-wing 3-D 场景 `coastal_patrol_fixed_wing_001`：
  `rc_cessna_mono_cam`（`rc_cessna` + `mono_cam`）模型、WGS-84 海岸巡逻 world、跑道/停机坪/
  进近信标、横风与故障时间轴；`run_simulator_3d_control_acceptance.py` 现按场景参数化，
  VTOL 与 fixed-wing 均执行 world/model/scenario hash 和共享 control gate。新增
  `src/multidetect/simulator/stack.py` 与 `stack-plan` CLI，生成不启动进程的 hash-bound
  PX4/Gazebo 命令、环境变量、端口和资产 provenance 预检。

- `SimulatorInteractiveRouter` 增加显式 `qgc_control_mode=read_only|sim_flight`。默认/fixture
  保持 `read_only`；真实 native QGC Fly View interactive job 默认选择 `sim_flight`，仅允许
  严格校验的 ARM/DISARM、VTOL transition、RTL/LAND、MISSION_START、PX4 mode 和最多 64 项
  `MISSION_ITEM_INT`，参数/坐标/身份/target/loopback 逐字段校验；未列出的 MAVLink、参数或
  FTP 写入继续 fail closed。router evidence 记录策略、allow-list、转发与拒绝计数。

- `src/multidetect/simulator/interactive_router.py` 为真实交互式 SIM 提供独立的
  literal-loopback QGC↔PX4 双向 UDP 代理。QGC 原生 isolated-HIL 端口（默认 `14649`）与
  router 输入（默认 `14650`）分离，PX4 telemetry 输入（默认 `14541`）与唯一控制输出
  `udpout:127.0.0.1:14540` 分离；`read_only` 策略只转发发现类只读帧并对 mission/parameter/
  flight write fail closed，`sim_flight` 则仅放行上一条 allow-list 所述的有界 ARM/模式/任务帧。
- 新增 `src/multidetect/simulator/ai_executor.py` 的 deterministic truth-free policy/executor
  接口与 `scripts/run_simulator_ai_acceptance.py` fixture：AI 只能提交有 envelope/watchdog
  的四轴 MANUAL_CONTROL，人工帧立即夺回 authority，GPS/链路/障碍故障进入 neutral/failsafe。
- 新增 `src/multidetect/simulator/live_ai_bridge.py` 与 `run` CLI 的
  `--ai-live`/`--live-ai`：仅在 `--interactive --ai-live` 显式 opt-in 时，将 interactive
  router 已校验、且已原样转发给 QGC 的 PX4 telemetry 送入 truth-free
  `SimulatorTelemetrySnapshot`，调用共享 `SimulatorAiExecutor`，再沿同一
  `SimulatorControlAuthority` → `SimulatorMavlinkControlAdapter` → `MANUAL_CONTROL` 路径输出。
  `ai.json`/`result.json.ai.live_bridge` 记录 observed/accepted/rejected frame、snapshot、AI
  step/accept/reject/failsafe/watchdog 计数；`interactive-router.json` 记录 telemetry sink 计数。
  默认关闭且不读取 `truth.json`；Docker passive acceptance 仍固定
  `flight_control_commands_sent=0`，不因此升级为真实 AI-control gate。
- `SimulatorTelemetryBridge` 增加独立 HEARTBEAT freshness lease：默认
  `heartbeat_timeout_s=2.0s`，超时将 `link_healthy=false`，经共享 executor/adapter 只发送一次
  canonical neutral 并进入 FAILSAFE；同一 stale 窗口的后续位置帧不重复发包。新 HEARTBEAT
  只恢复 telemetry lease，不自动解除 terminal FAILSAFE；内部 replay fixture 可传
  `heartbeat_timeout_s=None`，而 executor 自带 watchdog 仍保持有效。raw `GPS_RAW_INT` 也保留
  `vel/cog` 字段，保证速度观测在 frame/mapping 两条输入路径一致，并由 bridge tests 固定回归。
- interactive router 的 event-loop 现在统一拥有 live AI watchdog 调度：每轮
  `poll_watchdog()` 先经 `watchdog_sink` 驱动 bridge 的 HEARTBEAT freshness 和共享 executor
  watchdog，stale/超时只产生一次 canonical neutral/failsafe；无 sink 或 sink 异常则回退
  adapter `check_watchdog()`。`interactive-router.json` 新增 `watchdog_ticks`、
  `watchdog_sink_calls`、`watchdog_sink_errors`、`watchdog_sink_last_error`，并以测试固定
  sink 不重复发包、异常回退和 stale 单次 fail-safe 语义。
- `SimulatorOrchestrator` 持久化 `interactive-router.json`、`ai.json` 和 control-route 端点
  证据；dry-run 的 AI step 使用进程内 recording sink，不会打开真实 UDP/pymavlink。

- 新增 `src/multidetect/simulator/control_gateway.py`：为交互式 3-D SIM 提供独立的
  loopback-only QGC `MANUAL_CONTROL` 网关。它严格校验 QGC `255/190` 身份、SITL target 1
  和轴范围，并将输入送入与 AI 共用的 authority/adapter；不改变验收路由的 QGC 只读契约。
- `SimulatorOrchestrator` 现在在 SIM job 内启动共享 adapter 的 control-gateway 后台线程，
  写入 `control-gateway.json`，停止时对活动 AI 流发送 neutral/failsafe；新增网关拒绝、人工
  接管、生命周期和证据回归测试。

- 新增 `src/multidetect/simulator/flight_model.py`：确定性、truth-free 的 bounded VTOL/
  fixed-wing kinematic flight model，支持 seed、east/north 风场、可配置姿态/速度/高度边界、
  单调时间戳和 canonical evidence hash；它是 CI/协议 fallback，不替代 Gazebo 刚体动力学。
- 新增 `src/multidetect/simulator/closed_loop.py`：将 `DeterministicFlightModel` 接到同一个
  `SimulatorControlAuthority` → `SimulatorMavlinkControlAdapter` → `MANUAL_CONTROL` 路径，
  以进程内 MAV sink 运行 AI hover/track、人工夺权、watchdog/neutral/failsafe 和 telemetry
  回环；新增 CLI `python -m multidetect.simulator closed-loop`。
- 新增 `src/multidetect/simulator/interactive_acceptance.py`、
  `scripts/run_simulator_interactive_acceptance.py` 及 CLI
  `python -m multidetect.simulator interactive-acceptance`：复用 native QGC
  `MANUAL_CONTROL`/telemetry router、共享 authority/adapter 和 deterministic flight model，
  在不启动 PX4/Gazebo/QGC/UDP 对端的情况下覆盖 VTOL/fixed-wing、AI TRACK、手动接管与恢复、
  watchdog neutral/FAILSAFE、heartbeat 原字节转发、只读 PARAM_REQUEST_LIST 转发、mission
  write 阻断和 loopback/identity 拒绝计数。输出 claims 明确保留视觉/PX4/Gazebo/硬件/真实飞行
  `not_claimed`。
- 新增 `scripts/run_simulator_closed_loop_acceptance.py`：一次性覆盖 VTOL/fixed-wing、AI
  观测、人工接管/恢复和 truth-free evidence；新增
  `scripts/run_simulator_3d_control_acceptance.py` 及 PowerShell wrapper，将场景 manifest、
  Gazebo world/model 资产哈希与共享 manual/AI control gate 绑定。该 3-D gate 只做资产/协议
  machine check，不冒充 Gazebo/QGC 渲染或硬件验收。

- `src/multidetect/px4_mission.py` 新增 fixed-wing compiler：
  `FixedWingMissionGeometry`、`Px4FixedWingSafetyPolicy` 和
  `compile_fixed_wing_executable_mission(...)` 将 runtime-channel 几何与平台无关计划编译为
  hash-bound `MAVLink2/MISSION_ITEM_INT`（`TAKEOFF → waypoints → landing_approach → LAND`）。
  编译前 fail closed 检查 geofence/home、airframe allow-list、GPS/电池/能量 reserve、
  最小航段与 turn radius、landing approach/glide slope、起降航向及 landing tailwind/crosswind；
  `truth`/evaluator-only 字段不会进入 mission。

- 新增 `src/multidetect/fixed_wing_acceptance.py` 与
  `scripts/run_fixed_wing_software_acceptance.py`：使用生产目标池、Mode-3
  challenge/确认和固定翼姿态执行器跑通 `DET → TRK → LCK → TGT`、姿态限幅/slew、
  prestream/`OFFBOARD`、RC 取消及失锁恢复，并输出结构化 evidence。
- 新增 `tests/test_fixed_wing_acceptance.py`，覆盖状态链、控制门、原生 QGC UI 契约字段及
  CLI evidence 写入。

### Changed

- repository stack 且未提供 custom process specs 时，`SimulatorOrchestrator` 默认准备 PX4
  job-local wrapper；non-dry-run 默认 strict。`job.json`/`result.json` 现在持久化
  `px4_launch_enabled`、`strict_requested`、plan hash、artifact verification 与错误。实际启动只在
  wrapper readiness 通过后替换 PX4 process spec，防止 stock PX4 静默退回 `18570/14580`。
- SIM 证据门明确区分 wrapper preflight、external no-launch plan 与外部 runtime：
  `px4-launch.json.readiness.gate_passed` 不代表进程/telemetry；dry-run 的
  `external-stack.json.validation.gate_passed` 也不能替代 `validation.ready_to_launch`。只有 PID/
  exit、PX4 heartbeat、`14540/14541` socket、Gazebo camera/NavSat、QGC RTSP/metadata 和清理证据
  完整时，才记一次外部 SIM runtime gate。

- `SimulatorOrchestrator`/`python -m multidetect.simulator run` 新增
  `--qgc-control-mode {read_only,sim_flight}`；提供 QGC 且启用 `--interactive` 时默认
  `sim_flight`，headless/dry-run 默认 `read_only`。生成的 `job.json`、`result.json` 与
  `interactive-router.json` 均持久化实际策略和 bounded command/mission evidence，避免把
  只读 acceptance fixture 误当作可飞行链路。
- SIM 文档和运行示例补充 fixed-wing `coastal_patrol_fixed_wing_001` 与 `stack-plan` 预检；
  所有条目继续区分 software/protocol/asset machine gate 与 Gazebo/QGC 视觉、Pixhawk/Jetson
  HIL、GNSS/相机硬件及真实飞行验收，不提升 `v0.3.0` 发布状态。
- Docker Desktop 计划修复同端口 UDP hairpin：只发布 loopback control `24540`，telemetry
  `14541` 作为 `host.docker.internal` outbound-only 目标（`published=false`），并在 planner、
  container inspection、Docker acceptance fixture 和 AI acceptance fixture 中固定回归；
  旧 artifact 保留原始状态，不被新证据覆盖。

- `SimulatorOrchestrator` 新增 `--throttle-wire-mode {qgc_legacy,signed}`（默认
  `qgc_legacy`），并将同一模式贯穿 QGC/AI/closed-loop adapter；legacy 的
  `MANUAL_CONTROL.z` 为 `[0,1000]`、neutral `500`，signed 为 `[-1000,1000]`、neutral `0`。
  选择与端点写入 `job.json`、`control.json`、`result.json` 和 interactive-router adapter
  evidence，防止 wire 语义漂移。
- standalone `interactive_router` CLI 同步支持 `--throttle-wire-mode auto|qgc_legacy|signed`；
  `auto` 对非负 legacy/负值 signed 输入做兼容解码，adapter 输出仍固定为 legacy，证据顶层
  记录实际模式。
- `run_simulator_3d_control_acceptance.py` 现在把 interactive acceptance 子门原样嵌入
  `interactive_qgc_path`，并在顶层 `claims.native_qgc_interactive_path` 暴露子门结果；该字段
  只表示 native QGC MANUAL_CONTROL/telemetry 与共享 authority 协议回归，不提升视觉或硬件等级。
- interactive router 对跨来源 partial frame fail closed：丢弃并重置对应 decoder，证据保留
  `qgc_fragment_source_resets`/`px4_fragment_source_resets` 及
  `*_rejected_reasons.fragment_source_changed`，避免跨端口字节拼接。
- manifest `scenario_runner` 在 provider/AI/model step 异常时仍执行 close/failsafe 并写出
  完整 evidence，`terminal_reason=error:<ExceptionType>:<message>`；CLI 以退出码 `2` 暴露错误，
  不再因 evidence 已序列化而向 CI 假绿。正常 `frame_budget_complete` 仍返回 `0`。

### Verification

- Windows Docker Desktop 真实 VTOL gate 已通过：
  `artifacts/evaluation/simulator-docker-acceptance-20260828c.json`，SHA-256
  `B014B98EAE8602C0C0D4CD3DBA8DABC01FB0902F1C635A5CE26B274949706AF6`；容器日志 SHA-256
  `B35DC53623FF8E2F1DA9B390F732F4B4ADC2662A9E26B51E1F5F8825F1A1E192`。`gate_passed=true`、
  qualified GPS fix type 3、cleanup removed=true、flight-control commands=0；wildcard receive
  仅为 Windows Docker UDP proxy 的观测 fallback，host 发布仍是 `127.0.0.1`。此证据不提升
  QGC 桌面视觉、Pixhawk/Jetson/GNSS/相机硬件或真实飞行门。

- 同一 pinned image 的 fixed-wing `coastal_patrol_fixed_wing_001` Docker gate 也已通过：
  `artifacts/evaluation/simulator-docker-acceptance-fixedwing-20260828.json`，SHA-256
  `8EAFD249C5118E9703C5F20AE95D7B64FAF8450B627DC3A0911A785B587FC2F2`；日志 SHA-256
  `AB3A28ED802A4967ABF2DEEF6EF9788B7930212F713EDCEFAFA97118771CC301`。`PX4_SYS_AUTOSTART=4003`、
  `gz_rc_cessna_mono_cam`、qualified GPS fix type 3、cleanup removed=true 均在证据中绑定；
  仍不代表 Mission upload/readback、QGC 视觉、硬件或实飞。

- PX4 launch planner/wrapper、job containment、stock rc stanza patch、template/strict 行为、tamper
  验证、external-plan opt-in 和 orchestrator dry-run evidence 已加入 simulator 回归。就
  `px4_launch.py` wrapper/native 外部链而言，当前台式机 native probe 仍为 PX4/Gazebo 缺失，
  尚未取得 wrapper-backed PX4/Gazebo heartbeat/telemetry；Docker Desktop 的 VTOL/fixed-wing
  external telemetry gates 已在上方单独通过并封存。桌面 QGC 人工视觉、真实
  Pixhawk/Jetson/GNSS/相机、HIL/REAL 或实飞验收仍未完成。版本保持 `v0.3.0-pre.8`。

- `tests/test_simulator_interactive_router.py`、`tests/test_simulator_interactive_acceptance.py`、
  `tests/test_simulator_ai_executor.py`、`tests/test_simulator_orchestrator_interactive.py` 与
  SIM fixture gates 覆盖手动接管、
  AI RETURN/GPS gate、watchdog、端点身份、QGC native isolated-HIL 参数和停止时 neutral。
  router evidence 还固定 `qgc_identity`（`255/190`、target system/component）及 QGC/PX4
  datagram blocked 计数。
  这些是软件/协议 machine checks；人工 QGC 视觉、桌面 GPU、真实 Pixhawk/Jetson/GNSS/相机和
  真实飞行验收仍分别记账，不能由 fixture 代替。

- pre.8 deterministic closed-loop/flight-model/3-D contract checks 可用以下命令复跑：

  ```powershell
  cd D:\Projects\Multi-Detect
  $python = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
  $env:PYTHONPATH = 'src'
  & $python -m pytest -q `
    tests\test_simulator_flight_model.py `
    tests\test_simulator_closed_loop.py `
    tests\test_simulator_closed_loop_acceptance.py `
    tests\test_simulator_3d_control_acceptance.py `
    tests\test_px4_fixed_wing_mission.py
  & $python scripts\run_simulator_closed_loop_acceptance.py `
    --out artifacts\evaluation\simulator-closed-loop-acceptance.json
  & $python scripts\run_simulator_3d_control_acceptance.py `
    --out artifacts\evaluation\simulator-3d-control.json
  & $python -m multidetect.simulator interactive-acceptance `
    --mode both `
    --out artifacts\evaluation\simulator-interactive-acceptance.json
  ```

  三个 acceptance JSON 的 `gate_passed` 应为 `true`；closed-loop 结果必须同时列出 `vtol`
  与 `fixed-wing`，3-D 结果必须保留 world `EARTH_WGS84`/`max_step_size=0.004`、
  `manual_and_ai_share_authority_path=true`、`native_qgc_interactive_path=true`，并把
  `visual_acceptance`/`hardware_acceptance` 保持为 `not_claimed`；interactive 结果还应有
  `qgc_manual_frames_accepted=1`、`qgc_read_only_frames_forwarded=1`、
  `qgc_mission_writes_allowed=false` 和 `telemetry_frames_rewritten=false`。

- scenario runner 的正常终态可用以下命令检查（应保存 JSON、
  `terminal_reason=frame_budget_complete` 并返回 exit code `0`）；provider/AI/model 注入异常
  的集成路径另由 runner 回归覆盖，异常时仍保存关闭后的 JSON、`terminal_reason` 以 `error:`
  开头并返回 exit code `2`：

  ```powershell
  & $python -m multidetect.simulator scenario `
    --scenario configs\scenarios\coastal_patrol_fixed_wing_001.scenario.json `
    --frames 8 --dt 0.1 --mode fixed-wing `
    --out artifacts\evaluation\simulator-scenario-fixed-wing.json
  ```

- `tests\test_px4_fixed_wing_mission.py`：12 passed，覆盖 fixed-wing compiler 的
  deterministic/hash-bound 输出、包络/围栏/能量/风/转弯/进近 fail-closed 检查和 truth 隔离；
  该结果不替代 PX4 upload/readback、Gazebo/QGC 视觉或真实飞行门。

- 固定翼 software/HIL-preflight acceptance 通过；transport 为内存记录器，
  `pixhawk_link_opened=false`、`mavlink_messages_sent=0`、`physical_output_enabled=false`。
- QGC 继续复用原生 `FlyView`/`FlyViewVideo`/`PipState`/toolbar/map surface 和
  `QGCButton`/`QGCLabel`/`QGCPalette`/`QGCToolInsets`/`ScreenTools`，没有第二窗口、第二
  视频 surface 或独立主题；人工 Computer Use/Fly View 验收仍为
  `operator_visual_acceptance=not_claimed`。

## [0.3.0-pre.8] - 2026-08-26

版本映射：release label `0.3.0-pre.8`、Python/wheel `0.3.0rc8`、CMake/PE numeric
`0.3.0`、channel `prerelease`。

### Changed

- QGC 状态工具栏改用官方 `/qmlimages/Analyze.svg` 与 `QGCColoredImage`，采用原生
  `ScreenTools.smallFontPointSize` 两行状态布局，移除 Multi-Detect 私有状态圆点。
- 状态详情仍通过 QGC 原生 `ToolIndicatorPage`/`SettingsGroupLayout` 抽屉呈现；REPLAY、SIM、
  HIL、REAL 共用同一 Fly View、toolbar、状态和核心算法，不创建第二窗口、第二视频面板或私有主题。

### Verification

- `tests/test_qgc_native_flyview_style.py`、`tests/test_qgc_native_style_capture_script.py`、
  `tests/test_qgc_desktop_capture_validation.py`：25 passed。
- 2026-08-27 combined SIM/QGC/Gazebo/VTOL machine gate 通过：
  `artifacts/evaluation/combined-sim-qgc-20260827T002714Z-61220/combined-evidence.json`
  (`309,240` bytes，SHA-256 `1E8C3271E44902B5F629524E5C04079B08A51F26B2ABEA68E0CB3306990B2EFD`；
  `gate_passed=true`，600 帧，签名 runtime-status/ACK 连续，VTOL Mission 与清理检查通过)。
  原生 QGC toolbar、PiP、视频 surface 的 exact PID/HWND machine smoke 通过；仍保留
  `operator_visual_acceptance=not_claimed`，不宣称人工 Computer Use 视觉验收。
- exact-client machine capture：
  `build/qgc-native-style-pre.8-cu-authorized-20260826/qgc-native-style.png`，`475,726` bytes，SHA-256
  `32C6461104D01200DF89F35B45C7CE87E58814E0E3273BA2B8596AB62CDD1263`；
  `single_native_surface_machine_check=true`。当前主机未暴露 `@oai/sky` Computer Use client，
  因此 `operator_visual_acceptance` 仍保持 `not_claimed`，不把截图冒充人工点验。

- pre.8 Windows Release/portable 在证据卡片空值格式化修复阶段完成的历史增量重建：主 EXE
  `46,552,576` bytes，SHA-256
  `600E98F31EFD8D9751F954EDA0473D0746543F1504245979A96EBB1F856D19DE`；RTSP helper
  `79,872` bytes，SHA-256 `50F17CF7E19542748035329585DBE59526D2739CB7BC41B65A72003C84C1F6A4`。
  portable 目录为 `1,013` 个文件、`529,265,549` bytes，`bin/` 为 `233` 个文件，Release 与
  portable 主 EXE 字节一致；构建 wrapper 退出码为 `0`。GPS/回放修复后的当前 pre.8
  构建已在本节顶部单独记录，不能用该历史指纹替代当前制品。

## [0.3.0-pre.7] - 2026-08-26

版本映射：release label `0.3.0-pre.7`、Python/wheel `0.3.0rc7`、CMake/PE numeric
`0.3.0`、channel `prerelease`。

### Changed

- 室外目标落图改用无新增依赖的 WGS-84 Vincenty direct geodesic，按椭球曲率处理不同纬度的
  南北/东西尺度，并保持经度跨日期变更线归一化。
- 非法地面测距置信区间现在 fail closed，不再以零误差悄悄发布坐标。
- QGC 自定义层继续严格复用原生 Fly View/Video/PiP/toolbar/map 和 QGC palette/controls；
  没有新增窗口、播放器或独立模拟器主题。

### Verification

- `tests/test_target_geolocation.py`：8 passed，覆盖椭球投影、曲率、高纬度、日期变更线和
  非法置信区间。
- combined SIM wrapper 现在从 `synthetic-fire-nx6-hil.manifest.json` 的 NCHW `input.shape` 自动
  推导 `--input-width/--input-height`，不再把模型尺寸硬编码为 `64x64`；专项静态回归通过。
- pre.7 Windows Release/portable 已重建：主 EXE `46,551,552` bytes，SHA-256
  `B24754D7751521632C3C1250D69B4476CD13B8A6DCA09248A79B26CCD93973EA`；RTSP helper
  `79,872` bytes，SHA-256 `769C4B0AD3DEEAE7398927B9159296D0C2172350F218663F632D5F6689B01D9D`。
- 600-frame combined PX4/Gazebo/QGC SIM machine gate 已通过：
  `artifacts/evaluation/combined-sim-qgc-20260826T201913Z-52120/combined-evidence.json`
  为 `309,608` bytes，SHA-256 `25B91825FE8426450E98D25DF55CDC7FE0641C950092A929B5CDE681E21EACCE`，
  `gate_passed=true`、7-item INT-only VTOL Mission、MC→FW→MC→land→disarm、最大离家距离
  `142.488 m`、最大相对高度 `24.735 m`；QGC 仍保持单一原生 Fly View surface。
- QGC 原生 UI 静态专项和 exact-client machine smoke 继续通过；原生截图
  `build/qgc-native-style-pre.7-20260827-034544/qgc-native-style.png` 为 `475,725` bytes，
  SHA-256 `1BC638D8AC3A3AEBD9BB19206438D621A9276C2F2B76C64679EC6779EF216634`。
  `operator_visual_acceptance=not_claimed`、`qml_rendering_proved=false`，不把机器证据冒充人工
  Computer Use/Fly View 验收。

## [0.3.0-pre.6] - 2026-08-26

版本映射：release label `0.3.0-pre.6`、Python/wheel `0.3.0rc6`、CMake/PE numeric
`0.3.0`、channel `prerelease`。

### Changed

- 修复 `short_term_tracking` 的相机运动模型选择：先拟合 affine，再仅在 projective strength
  与相对残差/内点收益同时满足时采用 homography，避免普通 roll/zoom/pan 被误判为
  `background_homography_flow`。
- 固化 REPLAY/SIM/HIL/REAL 复用同一原生 QGC `FlyView`/`FlyViewVideo`/`PipState`/toolbar/map
  surface 的 UI 约束；自定义叠加继续使用 QGC controls/palette/insets，不引入 simulator-only 页面或主题。
- 生成与当前源码版本绑定的 QGC replay fixture；Ground Truth 与 hardware output 继续保持隔离。

### Verified / Evidence boundary

- Python 回归、ruff、compileall 与 `git diff --check` 已通过；QGC replay fixture 单元/CLI 回归已通过。
- Windows Release/portable 已完成并核对：主 EXE `46,551,552` bytes，SHA-256
  `AAFA5448E74C9D5041360F4384C98E140C5197A856751768DD88135436F4C662`；RTSP helper
  `79,872` bytes，SHA-256 `33935619A2A9385E96106CC73AD9ED3F2090FC2F49C5ED2A00EC3D5732DDF387`。
  portable 目录为 `1,013` files / `529,264,527` bytes，`bin/` 为 `233` files；Release 与
  portable 主 EXE 字节一致。
- `operator_visual_acceptance=not_claimed`；HIL、REAL、室外 GNSS/真实硬件和最终人工 Fly View 验收仍 pending。
- pre.6 combined SIM/QGC machine gate 已封存：`artifacts/evaluation/combined-sim-qgc-20260826T125250Z-62292/combined-evidence.json`
  为 `309,023` bytes，SHA-256 `7D6FCD1976242C85EF783E9D812D98475E2A723BAB396241801CACDDBCA0EDFC`，
  `gate_passed=true`，600 frames、7-item VTOL Mission、MC→FW→MC→land→disarm、GPS fix type `3`、
  `10` satellites、false geolocation `0`，最大离家距离 `143.728 m`、最大相对高度 `24.473 m`。
- 同轮原生 QGC 风格 exact-client machine capture 为
  `build/qgc-native-style-20260826-current3/qgc-native-style.png`，`476,031` bytes，SHA-256
  `653997AC48944DB232AFD1A8C9F5AA66C1C62436016E863F44B6D129FB1AF3E2`；该记录不提升为人工
  Computer Use/Fly View 视觉验收。
- 后续 current4 exact-client machine capture 为
  `build/qgc-native-style-20260826-current4/qgc-native-style.png`，`475,725` bytes，SHA-256
  `1BC638D8AC3A3AEBD9BB19206438D621A9276C2F2B76C64679EC6779EF216634`；该记录同样保留
  `computer_use_tool_exposed=false` 、`operator_visual_acceptance=not_claimed` 和
  computer_use_operator_authorized=true (operator pre-approval recorded)
  `qml_rendering_proved=false`，不替代人工 Fly View 验收。
- 追加软件侧 HIL 基线：`artifacts/evaluation/px4-sitl-qgc-operator-acceptance-20260826T145956Z-69528.json`
  （`7,727` bytes，SHA-256 `0A6BD7E388E60F79D7371595481412DB21FCF7C71C18ACBFB69761680887CDFD`），
  `gate_passed=true`；它只覆盖 localhost/PX4 SITL、签名 operator loop、只读路由和状态机，
  `real_v6x_contacted=false`、`hardware_control_enabled=false`，不能替代真实台架 HIL。
- 本轮按原生 QGC 风格约束完成台式机 Release/portable 重建：构建脚本退出码 `0`，主 EXE
  `46,551,552` bytes，SHA-256 `AAFA5448E74C9D5041360F4384C98E140C5197A856751768DD88135436F4C662`；
  portable 与 Release 主 EXE 字节一致，RTSP helper 保持 `79,872` bytes、SHA-256
  `33935619A2A9385E96106CC73AD9ED3F2090FC2F49C5ED2A00EC3D5732DDF387`。
- 重建后 exact-client 原生 QGC machine capture 为
  `build/qgc-native-style-20260826-postbuild/qgc-native-style.png`，`476,031` bytes，SHA-256
  `653997AC48944DB232AFD1A8C9F5AA66C1C62436016E863F44B6D129FB1AF3E2`；仍不冒充 Computer Use
  人工视觉验收。

## [0.3.0-pre.5] - 2026-08-26

版本映射：release label `0.3.0-pre.5`、Python/wheel `0.3.0rc5`、CMake/PE numeric
`0.3.0`，channel `prerelease`。

### Added

- 为 `live-camera` 增加 profile-bound HIL/REAL CLI 回归：运行模式、签名 profile、HMAC key
  env、airframe hash、Pixhawk endpoint、scenario provenance、target component 和 RC rate
  的组合边界现在由测试固定下来。

### Changed

- `runtime_launcher.py` 继续作为 HIL/REAL 的唯一身份门：HIL 拒绝 signing identity 与
  flight-control opt-in；REAL signing key 只在本地从环境变量解码并计算 SHA-256；启动失败
  时清理 Pixhawk/frame source。
- 自定义 QGC Plan/Select 组件移除未使用的 `QtQuick.Controls` 导入，保持原生 QGC controls、
  palette、toolbar、map/PiP surface，不引入第二主题或 simulator-only 页面。

### Verified / Evidence boundary

- 项目 venv 全量回归通过：`pytest -q` 退出码 `0`，`compileall -q src tests scripts`、
  `ruff check src tests scripts` 和 `git diff --check` 均退出码 `0`。
- Windows Release/portable 构建退出码 `0`；主 EXE `46,551,552` bytes，SHA-256
  `D00B984C11C195A6DD16B95094D4629A790219F0CEF54591C0973AB6EA87610C`；RTSP helper
  `79,872` bytes，SHA-256 `B371577996B81D7F9CD4C1FA4B7D175D3944DBCF082F7CD7D666F8637BF60D73`。
- portable 目录为 `1,013` files / `529,264,519` bytes，`bin/` 为 `233` files；Release 与
  portable 主 EXE 字节一致。pre.4 的 600-frame combined SIM/QGC 证据继续作为基线，不被
  pre.5 源码变更冒充为新集成证据。
- 人工 Computer Use/Fly View 视觉验收继续单独记账，未将静态测试或 headless 日志写成人工结论。

### Verified — pre.5 authoritative artifacts

| 制品 | 大小 | SHA-256 |
|---|---:|---|
| `build/windows-v0.3.0-pre.5-release/Release/MultiDetectGCS.exe` | `46,551,552` | `D00B984C11C195A6DD16B95094D4629A790219F0CEF54591C0973AB6EA87610C` |
| `build/windows-v0.3.0-pre.5-release/portable-v0.3.0-pre.5/bin/MultiDetectGCS.exe` | `46,551,552` | `D00B984C11C195A6DD16B95094D4629A790219F0CEF54591C0973AB6EA87610C` |
| `build/windows-v0.3.0-pre.5-release/portable-v0.3.0-pre.5/bin/MultiDetectReplayRtspPublisher.exe` | `79,872` | `B371577996B81D7F9CD4C1FA4B7D175D3944DBCF082F7CD7D666F8637BF60D73` |

## [0.3.0-pre.4] - 2026-08-26

版本映射：release label `0.3.0-pre.4`、Python/wheel `0.3.0rc4`、CMake/PE numeric
`0.3.0`，channel `prerelease`。

### Added

- 新增确定性 PX4 VTOL Mission 层 `src/multidetect/px4_mission.py`：runtime-only
  `VtolMissionGeometry`、有界 `Px4MissionSafetyPolicy`、`px4-vtol-mission-int-v1` adapter、
  canonical MAVLink 2 `MISSION_ITEM_INT` 编译、完整 VTOL lifecycle 校验、hash-bound
  `MissionTransactionBindings` 与 upload/readback/commit/start transaction。
- 新增 `scripts/vtol_sitl_mission_acceptance.py` headless evaluator helper。它附着 combined runner
  已拥有的 container/router/PX4，完成 INT-only upload/readback、protocol hash 复核、exact PX4
  `AUTO Mission` 启动以及转换、航点、落地、disarm、GPS/geofence 观察；不创建第二个 simulator
  或第二套 UI。
- 扩展 `src/multidetect/px4_gazebo_qgc_router.py`：QGC 通道继续只读，evaluator 只获得
  identity-bound、mission-type-bound、item/command/frame/coordinate-bounded 的 Mission 协议和
  既定 VTOL 生命周期命令；`MISSION_CLEAR_ALL` 与无关写帧保持禁止。
- 新增 exact-client 桌面截图 validator `scripts/validate_qgc_desktop_capture.py`。combined wrapper
  通过 exact PID/HWND、Per-Monitor V2、`GetClientRect`/`ClientToScreen`、遮挡枚举、25 点
  `WindowFromPoint` ownership grid 和 `CopyFromScreen` 捕获真实桌面 client pixels，并恢复窗口与
  输入状态。
- 新增 capture/Mission/router/combined 的专项测试源码；全量 pytest 计数与静态检查结果见本版本
  `Verified` 段。

### Changed

- 把 QGC 原生 UI 规则提升为 pre.4 硬约束：只使用现有 `FlyView`、`PipState`、
  `FlightDisplayViewVideo`、`QGCPalette` 和 QGC controls/`ScreenTools`；PiP 只读，full state 才允许
  Multi-Detect 目标操作。禁止 `Window`/`ApplicationWindow`、第二视频 surface、独立模拟器、
  simulator-only QML 和自定义主题。
- pre.3 `PrintWindow` GPU 黑面截图继续保留为历史 machine smoke；pre.4 的 visible-video gate
  改为 video-only ROI 双帧 nonblack/nonuniform，加 RTSP streaming/decoding 与 native type 24
  sequence advancement 的 transport 绑定。temporal diff 仅作诊断，不冒充 exact source-frame identity。
- combined SIM/QGC wrapper 已接入同一 owned PX4/Gazebo/router/QGC 上的 Mission helper、任务活动同步点
  与 Mission evidence 槽位；真实 600-frame combined Mission run 的结果见本版本 `Evidence boundary`
  段。

### Verified

- 项目 venv 全量回归：`1513 passed in 28.21s`，exit code `0`；日志保存在
  `build/validation/pre4-full-pytest-20260826-summary.log`。
- Python `compileall`（`src/`、`tests/`、`scripts/`）exit `0`，13 个 PowerShell 脚本 AST
  解析 `0` errors，仓库 `git diff --check` exit `0`。

### Evidence boundary / Pending

- pre.4 Windows Release/portable 已完成 root 重建、portable 部署与 SHA-256 封存；权威 combined
  evidence 为 `artifacts/evaluation/combined-sim-qgc-20260826T081624Z-50796/combined-evidence.json`。
- pre.4 `river_rescue_vtol_001` 的 600-frame combined VTOL Mission 运行已通过：`gate_passed=true`、
  7-item INT-only upload/readback/hash、MC→FW→MC、land/disarm、GPS/geofence 与原生 QGC
  FlyView/toolbar machine smoke 均通过。它闭合的是该 SIM machine/E4 gate，不代表完整所有场景。
- 自动桌面取证至多允许声明 `transport_bound_visible_video_surface_proved`；当前不声明
  exact source-frame identity 或完整 QML semantics。`operator_visual_acceptance=not_claimed`、
  `qml_rendering_proved=false`，人工 Fly View 点验仍是独立发布门。
- `desktop_capture_gpu_surface_proved=false` 仍是独立诊断；不能据此否定 native QQuickItem video
  pixels，也不能把机器证据写成人工画面验收。
- 真实 `HIL`、`REAL`、室外相机/GNSS/飞行硬件以及最终发布门仍未完成，故版本继续保持 prerelease。
- 下列 pre.2/pre.3 构建、哈希、测试和 combined evidence 保留为历史版本事实，不覆盖、重命名或
  提升为 pre.4 证据。

### Verified — pre.4 authoritative artifacts

| 制品 | 大小 | SHA-256 |
|---|---:|---|
| `build/windows-v0.3.0-pre.4-release/Release/MultiDetectGCS.exe` | `46,551,552` | `B798CEE669252B3DD6F1A8F07E3D77C737C04B4D128941331CBFAFDB17B44142` |
| `build/windows-v0.3.0-pre.4-release/portable-v0.3.0-pre.4/bin/MultiDetectGCS.exe` | `46,551,552` | `B798CEE669252B3DD6F1A8F07E3D77C737C04B4D128941331CBFAFDB17B44142` |
| `build/windows-v0.3.0-pre.4-release/portable-v0.3.0-pre.4/bin/MultiDetectReplayRtspPublisher.exe` | `79,872` | `F6F8AC6DD87DAA6BA7CF477AC62BDE5889B724885AE5C9DB285A485050FEFE37` |

`gate_passed=true` 的 combined 运行命令、容器/router 状态、Mission 结果和 QGC 原生 surface
证据全部保存在上述 `combined-evidence.json` 及其同目录日志/截图中。`operator_visual_acceptance`
仍为 `not_claimed`；`v0.3.0` 的 HIL/REAL/室外硬件门继续阻塞。

## [0.3.0-pre.3] - 2026-08-25

### Added

- `integrated-replay-qgc` 的真实 QGC E4 验收记录：raw recording 经过与 `live-camera` 共用的
  production perception factory、LiveMissionRunner、RTSP 视频和 signed operator metadata，
  在实际便携版 MultiDetectGCS 进程中完成同帧 barrier。
- QGC Fly View 原生画面证据：使用原有 `FlightDisplayViewVideo`、`PipState`、QGC toolbar、
  QGC controls 和地图 PiP；不新建仿真专用 UI 或独立窗口。QGC 临时 profile 可用
  `QGCQml/MainFlyWindowIsMap=false` 让视频启动即为主 Fly View，避免 PiP 切换重启 RTSP session。
- 运行身份 badge 改为直接取 QGC `QGCPalette`（`brandingBlue`、`warningText`、`colorGreen`、`window`），
  不再维护独立的 Multi-Detect 颜色体系；REPLAY/SIM/HIL/REAL 共用同一个原生 Fly View layer。
- 目标框、目标落图卡片、状态 HUD、深度 PiP、LCK/TRK 操作按钮和选择提示统一改为 QGC
  `QGCPalette`/`QGCButton`/`QGCLabel`；只保留深度 false-colour legend 作为数据编码，不再额外
  引入一套独立的 UI 主题。
- `scripts/create_qgc_replay_acceptance_fixture.py` 修复 fixture 自身 `fixture_summary` 字段写入，
  正式 fixture 位于 `build/qgc-replay-fixture-v0.3.0-pre.3/`。
- 新增 `scripts/gazebo_camera_capture.py --persist-frames` 与
  `scripts/create_gazebo_raw_replay_fixture.py`：将 digest 固定的 Gazebo Harmonic `gz_x500_mono_cam`
  真实 RGB/NavSat payload 绑定到 raw-only recording，再经 shared production perception、
  `LiveMissionRunner`、RTSP helper 和真实便携版 QGC 完成 12 帧同帧 transport barrier；不读取
  Scenario Ground Truth，不生成硬件输出。
- 修复高帧率 Gazebo replay 在 Windows `time.monotonic` 子毫秒 tick 下的 pacer 假失败：
  `WallClockReplayPacer` 使用一个最小 timer quantum，并仍保留注入时钟不前进时的 fail-closed 检查。

### Verified

- `E3 BUILT`：Windows Release/portable 主 EXE `46,556,672` bytes，SHA-256
  `5F462F8EDAD3677AE3CE88C0EC1B49CA9ECCA6B0B25A7E504A80B0BC7BBC0FD3`；RTSP helper 为
  `75,264` bytes，SHA-256 `28CD41016BA93A406FC9B9E3B98B0809B4AB8675D9A04821E1DD64C495FC5C47`。
- `E4 INTEGRATED`：`build/e4-qgc-20260825-222239/e4.result.json` 报告
  `complete=true`、3 个 video frame、3 个 metadata barrier、最大 ACK→metadata 偏差 `0.0 ms`、
  QGC peer `127.0.0.1:14581`、RTSP helper exit `0`、EOS/summary 均收到。
- `E4 visual`：`build/e4-qgc-visual-20260825-224648/qgc-flyview-video-initial.png` 与
  `qgc-flyview-video-stable.png` 由实际 QGC 桌面画面检查；原生 QGC toolbar、RTSP 视频、地图
  PiP、REPLAY badge、M1/DEPTH overlay 均可见且无 UDP 模态错误。机器 result 中的
  `operator_visual_acceptance` 仍保留 `not_claimed`，人工检查单独在最终 evidence JSON 标记。
- 历史 pre.3 palette rebuild 的 native-QGC style smoke 位于 `build/e4-qgc-visual-20260826-024914/`；截图来自当时的 portable binary。
  RTSP helper 提前断开，未计入 E4 gate，`operator_visual_acceptance` 仍为 `not_claimed`。
- `E4 PX4 SITL signed metadata`：`artifacts/evaluation/px4-sitl-qgc-operator-acceptance-20260825T190317Z-5732.json`
  记录真实便携 QGC 进程与 pinned fixed-wing PX4 SIH 的闭环：QGC/PX4 exit `0`、PX4 heartbeat `5`、
  signed metadata `37`、tracking `30 @ 20 Hz`、目标池 `3/2 pages`、challenge-bound `APPROVE`，
  QGC forbidden PX4 messages、file-mutating FTP、system-time、operator tunnel-to-PX4 和所有写入均为 `0`。
  端口 `14669/14666/14670/14667/14668/18570` 全部释放，保护端口 `14550` 未改变；真实 V6X/Jetson/G20
  未接触。operator metadata 只走严格 MAVLink 2 signing，没有 unsigned fallback。
- 隔离 HIL 的 QGC 关闭路径已稳定化：仍由原生 `MainWindow.qml::finishCloseProcess()` 负责
  link/video teardown；Windows acceptance 进程在 `QGCApplication::shutdown()` 后使用受控退出，
  避开 QML/global-singleton 销毁竞态，普通 QGC 关闭路径不变。此前 3 次真实 QGC + pinned PX4 SITL
  验收均为 QGC/PX4 exit `0`、`all_passed=true`、端口释放且无新 crash dump；随后两次 QGC palette
  rebuild 也通过同一门槛，当前 `stability_runs` 共 5 次，最新结果为 `20260825T190317Z-5732`，
  并追加到同一清单。 QGC acceptance 启动窗口固定为 45 秒，覆盖 Windows Qt Multimedia/GStreamer 冷启动。
- `E4 PX4 fixed-wing SIH Mission transaction`：`artifacts/evaluation/px4-fixed-wing-sitl-auto-mission-acceptance-20260825T170332Z-62660.json`
  记录 3-item Mission 的 `MISSION_COUNT`/`MISSION_REQUEST_INT` readback；顺序、命令、坐标、高度
  和参数契约通过，PX4 协议默认归一化后的 upload/readback canonical SHA-256 均为
  `14acad2d86fa047ae8af8388f7f792a018793fa65d30c9be62e425c33b484018`，`items_match=true`、
  `hash_match=true`。该 run 的 MISSION sequence 到 `2`、移动 `158.250773 m`，并保持
  Multi-Detect transmit `0`、payload action `0`、保护端口不变。
- 续验固定翼 QGC/PX4 软件 HIL：`artifacts/evaluation/px4-sitl-qgc-operator-acceptance-continue-20260826T090000Z.json`
  通过 `gate_passed=true`、QGC/PX4 exit `0`、PX4 heartbeat `6`、signed metadata `38`、
  tracking `30 @ 20 Hz`、target pool `3/2 pages`、challenge-bound `APPROVE`，并保持
  forbidden PX4 messages、FTP、SYSTEM_TIME、operator tunnel-to-PX4、写入和 payload action 全部为 `0`。
  本次还修复 Windows wrapper 在 Docker Desktop 未加入 PATH 时直接调用 `docker` 的环境依赖：
  `scripts/run_px4_sitl_qgc_operator_acceptance.ps1` 现在优先使用 PATH，回退到标准 Docker Desktop
  安装路径，并由 `tests/test_px4_sitl_qgc_operator_acceptance_script.py` 锁定该行为。
- Gazebo Harmonic sensor 子门：digest 固定的 `px4io/px4-sitl-gazebo` headless 容器运行
  `gz_x500_mono_cam`，真实 Gazebo image/CameraInfo/NavSat topic 捕获 12/12 帧 `1280×960 RGB`，
  时间戳严格递增；结果 `artifacts/evaluation/gazebo-sitl-camera-acceptance-20260825T193511Z-3908.json`
  的 SHA-256 为 `63EC2715ABDC54929DABE6975D1014693E0C69761394F63101B0803367E88C2D`。该子门仍不计入
  QGC E4 同帧 barrier，`operator_visual_acceptance=not_claimed`。
- Gazebo→QGC 同帧 transport 子门：`build/qgc-replay-fixture-gazebo-v0.3.0-pre.3/` 由上述
  12 帧真实 Gazebo pixels 生成；`build/e4-gazebo-qgc-20260826/e4.result.json` 的 SHA-256 为
  `520B1BD6732E8FB35B1FE876E457A3C95111AB751C338287252E7E7ED3529926`，记录
  `complete=true`、`12/12` video、`12/12` metadata barrier、最大偏差 `0.0 ms`、helper exit `0`。
  这是 transport 子门，不是人工 `operator_visual_acceptance`，也不是完整 fixed-wing/VTOL SIM、
  HIL 或 REAL 证据。
- 最终 pre.3 机器证据清单：`build/windows-v0.3.0-pre.3-release/verification/v0.3.0-pre.3-evidence.json`，
  SHA-256 为 `865E321F0F618220181BE45E2F7C7B48E467F47BC88A781DA5BA8BAE4B8F776B`；清单包含构建、根目录
  全量 Python `1330 collected / exit 0`、QGC custom/acceptance 精确选择集 `69 passed` 和本轮扩展的 `84 passed`
  QGC/Gazebo/replay 目标测试、C++ 自检、15 秒 portable smoke、Gazebo→QGC transport 和桌面画面指纹。全量回归日志为
  `build/validation/pre3-full-regression-continued-20260826.log`，SHA-256 为
  `57845C3D37460512104A7DB4F9D2F670D626B12C13E78417A1B353DF56838B8B`；算法 benchmark 使用
  contention-adjusted wall/CPU gate，同时保留原始 wall-clock 指标。
- 继续回归：根目录 Python 全量为 `1330 passed / exit 0`，日志
  `build/validation/pre3-full-regression-continued-20260826.log`，SHA-256
  `57845C3D37460512104A7DB4F9D2F670D626B12C13E78417A1B353DF56838B8B`；QGC custom/acceptance
  精确选择集为 `69 passed`，扩展选择集为 `84 passed / exit 0`。更新后的机器证据清单 SHA-256
  为 `865E321F0F618220181BE45E2F7C7B48E467F47BC88A781DA5BA8BAE4B8F776B`。

### Pending

- Gazebo/PX4 SITL 视觉闭环、完整 fixed-wing/VTOL Mission planner/path、真实 Pixhawk/Jetson
  HIL、室外 GNSS 和受限飞行仍未完成；fixed-wing SIH 的 Mission upload/readback/hash 只闭合
  该 software-only 子门，因此 prerelease 后缀不能移除。

## [0.3.0-pre.2] - in progress

### Added

- Raw-only `RawSensorReplayBackend` 与对应的严格 manifest Schema。记录只允许受哈希绑定的
  image references 和归一化只读 `VehicleTelemetry`；Detection、Ground Truth、
  expectations 和未知字段均被拒绝。
- 必填 RFC 3339 UTC `started_at_utc`，用于将 recording 的 monotonic capture timeline 唯一
  锚定到 UTC，并纳入 canonical recording hash。
- `LiveMissionRunner.from_runtime_backend(...)`，由 raw-sensor backend 独占提供
  `FrameSource`、`TelemetryProvider` 和 `Clock`；确定性 descriptor 必须绑定
  `ManualClock`。
- `build_production_perception(...)` 生产感知 factory，供 `live-camera` 和 headless
  `integrated-replay` 共用同一 detector/verifier/safety/priority/environment 构建与
  manifest role gate。
- headless `integrated-replay` CLI：验证 raw recording 经上述 production perception factory
  和完整 `LiveMissionRunner` 运行，并输出绑定 descriptor/recording/mission/model-set 的
  run identity、session `RuntimeStatusMessage`、mission audit 与结构化 result。headless status
  对象不冒充为已完成 QGC 签名传输验收。

### Changed

- `LiveMissionRunner` 核心新鲜度、截止时间、审计和 wire timestamp 统一使用可注入
  runtime clock；延迟仪表使用独立 performance clock，避免推进 Replay 业务时间。
- `ReplayFrameSource` 只在图像字节、SHA-256 和尺寸再验证成功后推进共享
  `ManualClock`；`ReplayTelemetryProvider` 只暴露当前回放时刻之前的最新样本，
  stale/skew 均失败闭锁。

### Verified

- `E2 TESTED`：`RawSensorReplayBackend` 的 raw-only 输入、manifest/event/image 完整性、
  UTC anchor、相对路径包含性、时序、遥测新鲜度和 close/fail-closed 专项通过。
- `E2 TESTED`：`LiveMissionRunner` 不再直接读取 `time.monotonic/time.perf_counter/time.time`；
  runtime backend 绑定、确定性 ManualClock 门和回放 cadence 专项通过。
- `E2 TESTED`：production perception factory 在 live/replay 两个 launcher 间共用同一 model-set
  identity 逻辑；headless integrated Replay 验证完整录制消费、稳定运行身份、
  RuntimeStatus、审计/结果重复性以及 Pixhawk/display/flight-output 失败闭锁。
- 上述 `E2` 只证明 headless backend/perception/runner 软件契约。Replay 的默认静态 performance clock
  显示 `0 ms` 只是确定性占位，不是性能证据；可选异步组件也尚未完成
  严格 bit-for-bit 调度证明，因此 deterministic launcher 当前显式拒绝异步
  semantic-context runner。
- `E2 TESTED`：根目录 Python 全量套件 `1303 collected / exit 0`（wall time `24.2 s`）；
  QGC custom + offline 静态回归 `61 passed in 0.18 s`。
- `multi_detect-0.3.0rc2-py3-none-any.whl` 已在
  `build/wheel-smoke-v0.3.0-pre.2/` 构建，大小 `662,046` bytes，SHA-256
  `DD96AEA726F4BE09557CD0F701772FCF9EAB1E4F41182DE9A0587603CF72B8E2`；隔离安装验证
  metadata、`multidetect.__version__`、release label 为
  `0.3.0rc2 / 0.3.0rc2 / 0.3.0-pre.2`。
- `E3 BUILT`：2623-step Ninja 图完成 Release 链接，wrapper 继续完成 install/windeployqt
  并以退出码 `0` 结束；`MultiDetectOperatorProtocolSelfTest` 退出 `0`，DepthGrid Python
  fixture 生成 `44` 个 HMAC fragment 后，C++ 往返自检也退出 `0`。
- `E3 BUILT`：Release 与 portable 主 EXE 均为 `46,525,440` bytes，PE File/Product version
  均为 `0.3.0.0`，SHA-256 均为
  `D5A096F69DA89CAA58E0227B85A184C228B310BD5DCA314FDE906E7379125769`。portable 位于
  `build/windows-v0.3.0-pre.2-release/portable-v0.3.0-pre.2/bin/MultiDetectGCS.exe`，共有
  `1012` files、`529,158,529` bytes；依赖闭包检查通过。
- `E3 BUILT`：portable 在 offscreen/software backend 下存活 `15 s`，stdout/stderr 均
  `0 bytes`，随后只终止该 exact process object。
- `E3 BUILT`：机器可读证据清单
  `build/windows-v0.3.0-pre.2-release/verification/v0.3.0-pre.2-evidence.json` 的 SHA-256 为
  `D9D0EE425AEE6976A077B69B3EB8AD4C1C8F34FC69E1F41286AB28C9CCF5FC86`。

### Pending

- 同一 raw recording 的同步 QGC RTSP/Fly UI 视觉联调（`E4 INTEGRATED`）。
- PX4 SITL + Gazebo 视觉闭环、PX4 Mission 回读/hash 事务、固定翼/VTOL planner、
  HIL 与受限真实飞行。

## [0.3.0-pre.1] - 2026-08-25

### Added

- 产品 prerelease 版本清单和多平台版本映射。
- `RuntimeMode`、运行描述、统一 Clock 与 RuntimeBackend 契约。
- 自主任务文档、canonical hash、Scenario Manifest 和 Ground Truth 类型隔离基础。
- 显式 `ObservationReplayBackend` 命名和 pre-detection `RawSensorRuntimeBackend` 契约；旧
  `ReplayBackend`/`RuntimeBackend` 名称保持兼容。
- 签名运行环境状态以及同一个 MultiDetectGCS Fly UI 的环境标识。
- 同核模拟器架构与分层验收文档。
- Windows 构建 wrapper 自动定位 `patch.exe`、绑定 MSVC `ml64.exe`，并通过 batch `call`
  保证 `qt-cmake` 返回后继续执行自检、安装和 portable 部署。

### Verified

- `E2 TESTED`：根目录全量 `1260 tests collected / exit 0`。
- `E2 TESTED`：Runtime/Contracts/Scenario/ObservationReplay/RawSensor 边界与版本链专项
  `61 passed`（产品版本专项 `13 passed`），RuntimeStatus/operator 协议专项 `92 passed`，QGC
  custom + offline 静态回归 `61 passed`。
- Ruff、Python `compileall`、仓库 10 个 PowerShell 脚本 AST、仓库级 `git diff --check` 与
  Markdown 本地链接解析通过。
- `multi_detect-0.3.0rc1-py3-none-any.whl` 已构建，并在隔离 venv 中验证 metadata、
  `__version__` 与产品 release label。
- 以上只证明当前源码的单元、契约和静态边界；不证明原生 Windows 构建、完整 UI launcher、
  raw-sensor same-detector Replay、Gazebo、HIL 或实飞。
- `E3 BUILT`：2623-step Ninja 构建图完成 Release 链接，wrapper 完成 install/windeployqt 并以
  退出码 `0` 结束；两个 C++ 协议自检均退出 `0`。
- `E3 BUILT`：portable 主 EXE 位于
  `build/windows-v0.3.0-pre.1-release/portable-v0.3.0-pre.1/bin/MultiDetectGCS.exe`，大小
  `46,525,440` bytes，SHA-256
  `83BBAB5BD4D0C560E07DF1BBE79DE02F7815921DF2833524A598C74087F7349B`；完整依赖闭包存在，
  offscreen 启动存活 `15 s`、stderr `0 bytes`。

### Pending

- PX4 SITL + Gazebo 视觉闭环。
- RawSensorReplayBackend、完整 `LiveMissionRunner` Clock 注入、same-detector 集成 Replay launcher。
- PX4 Mission 上传后回读与 hash 事务。
- 固定翼任务 planner 和着陆闭环。
- VTOL 前/后转换与垂直起降闭环。
- HIL 与受限真实飞行。

## [0.2.0] - verified baseline

### Verified

- Windows Release 和完整便携运行目录已在迁移后的台式机验证。
- 历史构建证据、路径、SHA-256 和硬件验收边界保留在根 README 与
  `docs/qgc-windows-release.md`，不会被 prerelease 文档覆盖。

- VTOL vehicle-dynamics SIM 子门：新增 `scripts/vtol_sitl_scenario_acceptance.py` 与 `scripts/run_px4_gazebo_vtol_scenario_acceptance.ps1`，使用 digest 固定的 `standard_vtol` Gazebo/PX4 SITL，真实验证 GPS、ARM、VTOL→fixed-wing→multicopter 转换、DISARM 和无误定位；结果 `artifacts/evaluation/px4-vtol-sitl-scenario-acceptance-20260826-final.json`，SHA-256 `2E82C2453F2D7B28815235E02B06C793366782112E8329C0B679DC1EF01ECF6E`，容器日志 SHA-256 `022EB07077DEB3216966C5BD5C21C4CCDFB8B8942A4EB381AC386D06C7247E88`。该结果只闭合 VTOL 车辆子门，`operator_visual_acceptance=not_claimed`，不替代完整 Mission/Fly View/HIL/REAL。
- 机器证据清单 `build/windows-v0.3.0-pre.3-release/verification/v0.3.0-pre.3-evidence.json` 已追加 `e4_vtol_sitl_scenario`，更新后 SHA-256 为 `865E321F0F618220181BE45E2F7C7B48E467F47BC88A781DA5BA8BAE4B8F776B`。

## v0.3.0-pre.3 重构补充记录（2026-08-26）

### Changed

- QGC 界面收口到原生 `FlyView` / `PipView` / `FlightDisplayViewVideo`：不创建第二窗口，
  不维护自绘 FixedWing HUD。PiP 只保留只读 annotations；完整目标交互只在 full view 开放。
- 自定义层继续使用 `QGCPopupDialog`、`QGCSlider`、`QGCToolInsets`、`QGCButton`、
  `QGCLabel`、`QGCPalette` 与 `ScreenTools`，避免引入与 QGC 不一致的独立样式。
- E4 canonical frame order 明确为：实际 RTSP `frame_accepted` → signed type 23 send →
  native-controller state adoption → authenticated matching type 24 post-adoption ACK。
- RTSP publisher 改为 media/appsrc generation 生命周期。首帧前或流中发生正常 QGC
  disconnect/reconnect 时可以等待新 generation；旧 media cleanup 不得清除新 appsrc。
  同一 wire frame 跨 generation 重试仍只在实际 push 成功后计一次 `frame_accepted`。
- `REPLAY` / `SIM` 继续使用与 `HIL` / `REAL` 相同的状态模型、核心算法和 QGC UI；模拟器只替换
  输入 channel、动力学/传感器和时钟。

### Evidence boundary

- type 24 仅证明 QGC native controller 已采纳对应 type 23 状态；它不证明 QML render、操作者视觉验收
  或 runtime-mode policy acceptance。
- `operator_visual_acceptance=not_claimed` 继续是强制边界；不能用 transport ACK、源码测试、
  headless runner 或截图文件存在性代替人工 Fly View 点验。
- 本补充不声明 combined 300-frame 运行已通过。完整 REPLAY/SIM、PX4 Mission、HIL、REAL 与
  户外 GNSS gates 尚未全部闭合，最终 `v0.3.0` 仍不可发布。

### Build handoff

```powershell
cd D:\Projects\Multi-Detect
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_multidetect_windows.ps1 `
  -SkipConfigure -Configuration Release -Parallel 2 `
  -BuildSelfTests -InstallPortable
```

本轮 reconnect/type24 重构后的最终 EXE 大小与 SHA-256 尚待统一 portable rebuild。本文档前部保留的
pre.3 数值属于更早中间快照，不应作为本轮最终制品身份；权威值必须由最终构建生成的
`build/windows-v0.3.0-pre.3-release/verification/v0.3.0-pre.3-evidence.json` 给出。
预期制品路径为：

```text
build/windows-v0.3.0-pre.3-release/Release/MultiDetectGCS.exe
build/windows-v0.3.0-pre.3-release/portable-v0.3.0-pre.3/bin/MultiDetectGCS.exe
build/windows-v0.3.0-pre.3-release/portable-v0.3.0-pre.3/bin/MultiDetectReplayRtspPublisher.exe
```

## v0.3.0-pre.3 权威重建与 combined gate 补充（2026-08-26）

### Built

- 历史 pre.3 portable `MultiDetectGCS.exe` 为 `46,561,280` bytes，SHA-256
  `4C3584F984592FDDF9114EC9C08EC3C68F86C768BFAD4EB3D0F505A263FD55FE`。
- 历史 pre.3 portable helper `MultiDetectReplayRtspPublisher.exe` 为 `79,872` bytes，SHA-256
  `AB3E0043DA4A124270BEE1D5EC75A717A9845CF37CB63A8ADC8A50B4752D206F`。
- 这些值取代本文件前部 pre.3 中间快照和“待重建”状态，但不覆盖 pre.1/pre.2 历史记录。

### Integrated

- `artifacts/evaluation/combined-sim-qgc-20260826T013505Z-64000/combined-evidence.json`
  记录 `gate_passed=true`；文件为 `87,623` bytes，SHA-256
  `6374DB67E985BBC003682BFC9F764B416604A9C3566A4A33D0164728D44219AA`。
- Gazebo/RTSP 为 `300/300` 帧，helper exit `0`、EOS 和 summary 完整；signed type 23 sequence
  `1..300` 连续，QGC native post-adoption signed type 24 sequence `1..300` 连续。
- PX4 完成 ARM、multicopter→fixed-wing→multicopter、DISARM；GPS `qualified=true`、fix type `3`、
  `10` 星、`506` samples、最大跳变 `0.011 m`、无 false geolocation。
- UDP `14550` 前后未改变；所有测试端口释放，任务 container 已停止并删除。

### Native PiP evidence and boundary

- exact QGC PID/HWND 为 `40048 / 0x1C534FC`。`WindowFromPoint(286,937)` 命中同一 HWND/PID 后，
  orchestrator 才通过 `SendInput` 执行普通 PiP click。
- `QGCQml/MainFlyWindowIsMap` 从 `true(default)` 变为显式 `false`；QSettings 点击前/后 UTC mtime
  分别为 `2026-08-26T01:35:38.3155174Z` / `2026-08-26T01:35:40.4183819Z`，SHA-256 分别为
  `BD2E993A77172B0668F6ADBF3DCA9F4F3C0083235F9F1B7258734F111CF9B578` /
  `764677D852C516ACE28A070886277565D0282E8D09756B1A6A56EF96401C52B8`。
- 普通 PiP swap 不重启 video receiver；300-frame transport/metadata ACK 链保持连续。
- 截图只计 machine smoke；GPU-backed surface 经 `PrintWindow` 可能为黑面，故继续保持
  `operator_visual_acceptance=not_claimed`、`qml_rendering_proved=false`。

### Still pending

最终 `v0.3.0` 仍需真实 HIL、REAL、室外相机/GNSS 硬件、完整 PX4 Mission 与人工视觉验收。

## v0.3.0-pre.3 唯一权威 combined run 刷新（2026-08-26T02:08Z）

- 当时唯一成功 run（历史 pre.3）更新为
  `artifacts/evaluation/combined-sim-qgc-20260826T020829Z-57624`；`combined-evidence.json` 为
  `87,530` bytes、SHA-256 `62CFCA2B42EE1A9A69676B91220476609D1505DA46DE36C187E2A85BBA14EB6F`、
  `gate_passed=true`。
- Release/portable QGC 为 `46,561,280` bytes、SHA-256
  `7A4D8B90F0DF0680536B9A91BEF36177542153F97745049D83D25B13BF52554D`；RTSP helper 为
  `79,872` bytes、SHA-256 `AB3E0043DA4A124270BEE1D5EC75A717A9845CF37CB63A8ADC8A50B4752D206F`。
  更早 `T013505Z-64000` / `4C3584F9...` 保留为历史 pre.3 记录，不再是当前制品身份。
- combined launcher 通过 `Remove-Item Env:` 真正删除
  `MULTIDETECT_DISABLE_VEHICLE_LINK`、`MULTIDETECT_HIL_UI_AUTO_EXERCISE`、
  `MULTIDETECT_HIL_UI_AUTO_EXIT`，而不是赋空字符串或 `0`；结果
  `acceptance_only_environment_absent_before_launch=true`（`env_absent=true`）、
  `hil_ui_auto_exercise_log_count=0`、`hil_ui_auto_exercise_disabled=true`。
- 300-frame sensor timeout 按 frame budget 动态变为 `180 s`。历史失败 run
  `combined-sim-qgc-20260826T020108Z-52536` 是固定 `100 s` timeout 导致的失败重试，
  `gate_passed=false`，不属于成功证据。
- 该历史 pre.3 run 的 300 个 type 23/type 24 sequence 全连续，helper exit `0` 且 EOS/summary 完整；
  QGC PiP swap 使用该历史 run 的 exact PID/HWND `55056 / 0x18D20DE`，普通 swap 不重启视频。
- 截图语义检查显示 badge `环境 SIM · HEALTHY`，但截图仍只属于 machine visual smoke；GPU surface
  可在 `PrintWindow` 中显示黑面，因此继续保持 `operator_visual_acceptance=not_claimed`、
  `qml_rendering_proved=false`。
- PX4 fixed-wing transition/return、GPS qualified、UDP `14550` 不变、端口释放和 container 删除均通过；
  最终 `v0.3.0` 仍需真实 HIL/REAL、室外硬件、完整 PX4 Mission 与人工视觉验收。

## v0.3.0-pre.7 - 2026-08-26

### Build

- 完成 Windows Release + portable 构建；`MultiDetectGCS.exe` 为 `46,551,552` bytes，Release 与
  portable SHA-256 均为 `B24754D7751521632C3C1250D69B4476CD13B8A6DCA09248A79B26CCD93973EA`。
- `MultiDetectReplayRtspPublisher.exe` 为 `79,872` bytes，SHA-256 为
  `769C4B0AD3DEEAE7398927B9159296D0C2172350F218663F632D5F6689B01D9D`。
- Python 全量测试、ruff、compileall、QGC 原生 UI 静态检查、Operator/DepthGrid/RTSP wire self-test
  通过。

### Native QGC UI

- 继续只在上游 QGC `FlyView`、`FlyViewVideo`、`PipState`、`FlightDisplayViewVideo`、toolbar 和
  map surface 上增量挂载 Multi-Detect；不引入独立主题、`Window`、第二视频输出或 simulator 页面。
- `capture_qgc_native_style.ps1` 成功完成 exact-client QGC desktop smoke；截图和 observation 已保存
  到 `build/qgc-native-style-pre.7-20260827-034544/`。该结果是 machine smoke，不替代人工视觉验收。

### Boundary

- 当时 manifest 仍为 `0.3.0-pre.7` (historical snapshot)；真实 HIL/REAL、室外 GNSS/相机硬件和最终人工 Fly View 验收仍是
  final `v0.3.0` 的阻塞门。
