# Multi-Detect 当前目标验收矩阵

本页对应截至 2026-07-16 的 Mode Setting、DET/TRK/LCK/TGT、固定相机、通用检测与真实控制链目标。它只记录可由当前文件、构建和运行结果证明的状态。

## 软件交付状态

| # | 要求 | 当前证据 | 状态 |
|---|---|---|---|
| 1 | 独立 `Mode Setting` | QGC `SelectViewDropdown.qml`、桌面目视启动 | 已通过 |
| 2 | 精简无关设置并隐藏机架切换 | `VehicleConfigView.qml` 屏蔽 PX4 Airframe 页；Mode Setting 只保留任务模式 | 已通过 |
| 3 | 模式 3 仅 LCK/TGT 后显示白色传统准星，并由固定翼姿态控制使目标趋近中心 | `MultiDetectVideoOverlay.qml`；`fixed_wing_aim_control.py` 的真实 MAVLink sender、限幅/失锁/航向保持回归 | 软件与消息级通过，实机舵面待标定 |
| 4 | 触控端滑动、桌面端确认/取消 | `FlyViewCustomLayer.qml` 按 `ScreenTools.isMobile` 分流 | 已通过 |
| 5 | 生产端使用真实链路 | QGC 固定 GR01、RTSP 和签名元数据链；Jetson 已同步真实控制模块并完成在线 smoke/常驻重启 | 联机通过；控制动态待标定 |
| 6 | 人、车、火等候选显示分类颜色 `+` | `MultiDetectVideoOverlay.qml` 候选模型与类别颜色映射 | 已通过 |
| 7 | TRK/LCK 分离，模式 2/3 才允许 LCK | `MultiDetectState.qml` 状态机和协议 ACK | 已通过 |
| 8 | LCK 红框、主目标优先、临近脱锁保持观察 | QGC 红框；`SelectionTargetPool` 和 `FixedCameraObservationEngine` | 软件已通过，动态实测待联机 |
| 9 | 模式 2/3：TRK→LCK→确认→TGT | QGC 状态机、签名协议、自测 | 已通过 |
| 10 | UI 文案精简 | 生产界面使用短状态词；模式 3 只显示视轴误差 | 已通过 |
| 11 | 类别检测优先，任意物体最后回退 | Orin 上火烟、COCO80、VisDrone 三模型错峰；CSRT/KCF/模板回退 | 已通过，部署域车辆/火情数据仍需扩充 |

## 固定相机约束

- 相机没有可动轴。
- 自定义 `FlyViewVideo.qml` 不实例化 `OnScreenGimbalController` 或 `OnScreenCameraTrackingController`。
- 视频非选择态只保留双击全屏，拖动和单击不再发送相机控制输入。
- `FixedCameraObservationEngine` 将主 LCK 图像中心偏差与 V6X roll/pitch/heading 合并；`FixedWingAimController` 仅在 Mode 3 + 主 LCK + 确认 + 飞行门限全部成立时生成有界姿态目标。

## 已验证命令

```powershell
cd D:\Projects\Multi-Detect
ground-control\MultiDetectGCS\.venv\Scripts\python.exe -m ruff check src tests scripts
ground-control\MultiDetectGCS\.venv\Scripts\python.exe -m pytest -q
.\scripts\run_goal_acceptance.ps1 -QgcBuildRoot .\build\windows-v0.3.0-pre.8-release
```

迁移到新工作站、尚未写入 Windows 用户级 operator key 时，软件/静态门可以显式使用：

```powershell
.\scripts\run_goal_acceptance.ps1 `
  -QgcBuildRoot .\build\windows-v0.3.0-pre.8-release `
  -ComputerUseOperatorAuthorized `
  -AllowMissingOperatorCredentials
```

此开关只放行本地软件证据，结果会写 `software_status=pass_with_environment_gap`，并把
缺失的两个 key 加入 `remaining_live_gates`；它不会伪造签名链路或把人工 Computer Use
画面验收标为通过。QML 语法检查使用 Qt 构建同源的 `qmlcachegen --bare`，避免裸
`qmlformat` 因 QGC 自定义 import graph 缺失而产生误报。

当前核心测试收集数以验收输出为准；当前 QGC 自定义应用回归为 `64/64`。当前 v0.3.0-pre.8
QGC 完整运行入口：

`D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.8-release\portable-v0.3.0-pre.8\bin\MultiDetectGCS.exe`

### 2026-08-30 最新 combined SIM 证据

最新 disposable PX4/Gazebo/QGC/Mission machine gate 已通过（传感器 transport teardown 修复后）：

`artifacts/evaluation/combined-sim-qgc-20260830T101014Z-56628/combined-evidence.json`

该证据的 SHA-256 为
`8423E667A9C9668EF8FE4B1AE5D4166E41F3E21F423ED2FD766EBBECB925C37D`，`gate_passed=true`，
覆盖 600 帧同核 integrated SIM/RTSP、传感器 helper `exit=0`、VTOL MC→FW→MC、7 项 INT-only
Mission upload/readback/active、端口/容器清理及 QGC 原生 toolbar 指示器稳定几何映射后的抽屉
开/关机器 smoke。它是 software-only 证据；真实 Pixhawk/Jetson/相机/室外 GNSS、执行器、HIL/REAL、
实飞、完整 QML/桌面 GPU 视频渲染和人工 Fly View 仍保持 `not_claimed`，最终 `v0.3.0` 尚未发布。

### 2026-08-30 台式机 goal acceptance 快照（历史基线）

在迁移后的台式机上使用 `-ComputerUseOperatorAuthorized -AllowMissingOperatorCredentials`
重新执行了完整脚本，机器可读证据为：

`artifacts/evaluation/multidetect-goal-acceptance-20260830-key-format.json`

SHA-256 为
`3469C05C8C560BB1EFA52393C43BDA6C04F35759F9F1CB30CABAABA1BD623E84`。该快照记录
`software_status=pass_with_environment_gap`、`hardware_status=offline_pending`、
`core_tests_collected=1857`、QGC 自定义回归 `63/63`、QGC 协议自测、QML 解析和
`git -c core.whitespace=cr-at-eol diff --check` 全部通过，并重新绑定到当时的
pre.8 portable EXE SHA-256 `600E98F31EFD8D9751F954EDA0473D0746543F1504245979A96EBB1F856D19DE`
（历史构建；当前 GPS/回放修复构建见下方）。

这不是实机通过记录：台式机网络接口 `Ethernet 2` 当前为 disconnected，Jetson
`192.168.144.20` 与相机 `192.168.144.108` 均不可达，两个 operator key、ChArUco
合格标定、断动力舵面验证、真实检测状态链和 Jetson soak 仍在
`remaining_live_gates` 中；因此 HIL、REAL、室外 GNSS、实飞、人工 Fly View 和最终
`v0.3.0` 继续保持 pending/not_claimed。

### 2026-08-30 统一 SIM clock/descriptor 后的复验

为验证确定性 SIM 与运行时契约、QGC 证据卡片空值处理一致，使用同一台机器执行的 goal
acceptance 历史快照为：

`artifacts/evaluation/multidetect-goal-acceptance-20260830-rebuilt-evidence-card.json`

SHA-256 为
`1EA3C005B02B8E7022C33BB4446D2E7C21585495BB66C8AACF7F5244E2AFA881`。该历史快照记录
`core_tests_collected=1860`、QGC 自定义回归 `63/63`、软件状态
`pass_with_environment_gap`、硬件状态 `offline_pending`；SIM scenario runner 的
`ManualClock`/`RuntimeDescriptor` 与 QGC 快照可选字段格式化回归已纳入计数；该证据已绑定
重建后的 portable EXE SHA-256 `600E98F31EFD8D9751F954EDA0473D0746543F1504245979A96EBB1F856D19DE`；
当前 latest 已由后续共享时钟/descriptor 回归快照取代。
Ethernet 2、
Jetson、相机和所有真实硬件门状态没有变化，不能从该复验推导 HIL/REAL、室外 GNSS、实飞
或最终版本通过。

验收脚本还会校验 QGC 的 Jetson 元数据零配置默认值（`192.168.144.20:14580`、本地 `14581`、strict signing），以及 Windows 用户级两个 operator key 是否已就绪；证据 JSON 只写布尔状态，不写密钥内容。

### 2026-08-30 共享交互时钟/descriptor 后的历史复验

交互验收新增 `ManualClock`/`RuntimeDescriptor` 绑定和 deterministic router duration 回归后，
重新执行完整 goal acceptance，当时的机器可读快照为：

`artifacts/evaluation/multidetect-goal-acceptance-20260830-interactive-clock.json`

SHA-256 为
`5AC0A405BF6B32405903FFFB8E58D37DAF45F817979A5E5275762854FBDB363B`，同时同步为
`artifacts/evaluation/multidetect-goal-acceptance-latest.json`（随后已由 GPS/回放快照取代）。本次记录
`core_tests_collected=1861`、QGC 自定义回归 `63/63`、软件状态
`pass_with_environment_gap`、硬件状态 `offline_pending`，并继续绑定当时 portable EXE SHA-256
`600E98F31EFD8D9751F954EDA0473D0746543F1504245979A96EBB1F856D19DE`。Ethernet 2 仍为
Disconnected，Jetson 与相机不可达，operator credentials、室外 GNSS、HIL/REAL、实飞和
人工 Fly View 视觉验收仍保持 pending/not_claimed。

### 2026-08-30 GPS/目标事件回放与 QGC 落图复验（历史记录）

GPS 新鲜度、目标事件回放和 QGC WGS-84 落图修复后，重新执行完整 goal acceptance；该历史机器可读
快照为：

`artifacts/evaluation/multidetect-goal-acceptance-20260830-gps-replay.json`

SHA-256 为
`5CB38AA3654130F5025B2227802F9FAE974CD8EC73A1A261460B4E5B14479171`，并同步为
`artifacts/evaluation/multidetect-goal-acceptance-latest.json`（随后已由 QA8、再由 QA10 取代）。完整脚本退出码为 `0`，核心测试
收集 `1870` 项、QGC 自定义回归 `63/63`，`ruff`、`compileall`、CRLF-aware `git diff --check`、
QML 解析和协议自测均通过；软件状态为 `pass_with_environment_gap`，硬件状态为
`offline_pending`。

本次回放事件绑定 `gps.observed_at_s`、`gps_age_s` 与 `gps_freshness`，未来或超过阈值的 GPS
观测 fail-closed，缺少有效测距时不写入方位；QGC 1σ 不确定性圆使用 WGS-84 局部曲率，且对
跨日期变更线经度做归一化。该历史 GPS-replay 快照绑定的 pre.8 portable EXE 为
`build/windows-v0.3.0-pre.8-release/portable-v0.3.0-pre.8/bin/MultiDetectGCS.exe`，
SHA-256 为
`0E18EB2371B868FD462C632945A369214DBEC1D34B8C1C88AADA58B43D59E654`（46,553,088 bytes）。
Ethernet 2、Jetson、相机和真实 operator credentials 仍不可用，因此该快照不提升室外 GNSS、
真实 Pixhawk/Jetson、HIL/REAL、实飞或人工 Fly View 视觉验收。随后加入 E7 清单生成器与回归，
下方回归快照保留为历史记录，最新 QA 快照另行写入 `latest`。

### 2026-08-30 E7 清单生成器与 QGC 证据快照加固后的历史复验（现由 QA22 取代）

重新执行 `run_goal_acceptance.ps1 -ComputerUseOperatorAuthorized -AllowMissingOperatorCredentials`
后，当前机器可读快照为：

`artifacts/evaluation/multidetect-goal-acceptance-20260830-final-regression.json`（历史回归快照）

SHA-256 为
`3C7FE0BF8E84BAF8142CFCCC660A21F3E6D4628B5F592CBB48EA631F474769DC`。

传感器 transport teardown 回归修复后的 QA 快照（历史记录）为
`artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa.json`，SHA-256 为
`440AB514681408DB5B583C0AD9A62054441004BD107756C0229423546AF36334`。随后加入 release-gate
runtime/event 归一化回归后的 QA3 历史快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa3.json`，SHA-256 为
`ED550A122814A32D8823CC482A909B1BEF4AC0D234E03D242185E33BC37EA1F3`（历史快照）。加入
canonical parity 同步回归后的 QA4 快照（历史记录）为
 `artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa4.json`，SHA-256 为
 `C9A04BA94DDD159BBBC979735BCD53910335A612B256EA0E1195D512D404D552`，并已同步为
`artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本）。网关 graceful-drain、
GPS 原始质量二次校验和物理网卡选择修复后的 QA5/QA6 快照作为历史记录保留。Windows
PowerShell 5.1 无参数入口与 UTF-8 无 BOM 证据写入修复后的 QA7 历史快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa7.json`，SHA-256 为
`D82F07B6E2935D40D0A16CF87644EB7AD0AD91FE9A67A3620B29CC78583AAAFF`，完整脚本退出码为 `0`，核心测试
全量 pytest 为 `1937 passed / 1 skipped`（共收集 `1938` 项；唯一跳过项是该 Windows 账户不支持符号链接），QGC 自定义回归 `63/63`，`ruff`、`compileall`、CRLF-aware `git diff --check`、
 QML 解析和协议自测均通过；软件状态为 `pass_with_environment_gap`，硬件状态为
 `offline_pending`。QA7 记录绑定的 pre.8 portable EXE 为
`build/windows-v0.3.0-pre.8-release/portable-v0.3.0-pre.8/bin/MultiDetectGCS.exe`，SHA-256
`6079B6E567304E3484C664426B549B0067E86D7A40DACF2F09BBDFD87486D829`（历史 QA7 指纹；当前 QA20
指纹见下方）。

QGC source-age/frame-hold、SIM world-origin 和 QGC 自定义测试更新后的 QA8 快照现作为历史记录保留：
`artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa8.json`（SHA-256
`A9C1AC9CE94B19BDE4AB551C27EEC4780554D601C913BE157CD86AE431C5ED98`）；核心测试收集 `1943` 项，
QGC 自定义回归 `64/64`。上面的 QA7/QA8 记录仅作历史证据，软件状态仍为
`pass_with_environment_gap`、硬件状态为 `offline_pending`。

2026-08-30 QA10 已降为历史 goal acceptance（原文件和 SHA 保留）：
`artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa10.json`，SHA-256
`1F3515C50D2EA95D481450189ED72AC2FCBD814B965F342C5777BED6D30B6F65`。

同日 QA11 已降为历史 goal acceptance（原文件和 SHA 保留）：
`artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa11.json`（SHA-256
`FFB41D20E2C220C8391D82DD062AF44C39DED020DBD2CD12C3B702A169403993`），脚本退出码 `0`，并已提升为
`artifacts/evaluation/multidetect-goal-acceptance-latest.json`（随后被 QA12 更新）。QA11 核心 pytest 收集
`1964` 项，`1963 passed / 1 skipped`（唯一跳过项是该 Windows 账户不支持符号链接），QGC 自定义回归
`64/64`；QGC protocol self-test、QML parse、Ruff、compileall、CRLF-aware diff check、三个 C++
self-test 和 portable headless smoke 均通过。当前 Release/portable 主 EXE 为 `46,574,080` bytes，
SHA-256 `E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`。QA11 当时为
`software_status=pass_with_environment_gap`、`hardware_status=offline_pending`，不提升真实硬件或最终
`v0.3.0` 发布门。QA11 在验收启动时写入源码快照 provenance：`HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、
`dirty_entry_count=290`、`worktree_clean=false`、`status_sha256=5e156cf0952025104665e287d9a87099acb2e989b8c0b9d67719c19befc86a1e`。

同日 QA12 已降为历史 goal acceptance（原始文件和 SHA 保留不动）：
`artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa12.json`（SHA-256
`D10F40F27E401250E677AC80E89ECE7459F5170172499E9EB81E2D65DF6960DF`）。

同日 QA13 现作为历史 goal acceptance 保留（原始文件和 SHA 不覆盖）：
`artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa13.json`（SHA-256
`EF1684856B274607A2C39F951D620803149495C9F323863CC096788528E16069`）。

QA14 现作为历史 goal acceptance 保留（原始文件和 SHA 不覆盖）：
`artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa14.json`（SHA-256
`6856130808EDEB05C543DE24A42F2C17545725C5D6615338ECF52613B5CA3277`）。

QA15 现作为历史 goal acceptance 保留（原始文件和 SHA 不覆盖）：
`artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa15.json`（SHA-256
`B978D244A7440B3C102E1BDD8408110F511BE2C0209C4BC7DDB340EE56487808`），脚本退出码 `0`，曾提升为
`artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本；现由 QA22 指向）。核心 pytest 收集
`1975` 项，`1974 passed / 1 skipped`（唯一跳过项仍是该 Windows 账户不支持符号链接），QGC 自定义回归
`64/64`；QGC protocol self-test、QML parse、Ruff、compileall、CRLF-aware diff check、三个 C++
self-test 和 portable headless smoke 均通过。当时 Release/portable 主 EXE 为 `46,574,080` bytes，
SHA-256 `E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`。QA15 的
`software_status=pass_with_environment_gap`、`hardware_status=offline_pending`，不提升真实硬件或最终
`v0.3.0` 发布门。QA15 在验收启动时写入源码快照 provenance：`HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、
`dirty_entry_count=293`、`worktree_clean=false`、`status_sha256=b98a96dd5850dac360714ff836f0818da09338cc382a4bbe3af1f01c3235dbba`。
QA15 同时固定 GPS_RAW_INT 越界坐标、255 卫星未知哨兵、缺失 `alt`、降级 fix 和未来观测时间的 fail-closed
回归，并在失效时恢复先前独立 EKF/LOCAL 高度；修复 SIM 手动 `arm_on_start` 语义，为三份 PX4/SITL 验收脚本
加入 Docker 标准安装目录回退，并同步重算 core parity。这些都是软件/协议门，不替代真实 Pixhawk、Jetson、
室外 GNSS、HIL/REAL、实飞或人工 Fly View 验收。

QA16 现作为历史 goal acceptance 保留（原始文件和 SHA 不覆盖）：
`artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa16.json`（SHA-256
`403B319255A9C2446012BD060C85BB2633DB9A4D7DE45CF4B9BB495D96B234EE`），脚本退出码 `0`，曾提升为
`artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本；现由 QA22 指向）。核心 pytest 收集
`1976` 项，`1975 passed / 1 skipped`（唯一跳过项仍是该 Windows 账户不支持符号链接），QGC 自定义回归
`64/64`；QGC protocol self-test、QML parse、Ruff、compileall、CRLF-aware diff check、三个 C++
self-test 和 portable headless smoke 均通过。当前 Release/portable 主 EXE 仍为 `46,574,080` bytes，
SHA-256 `E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`。QA16 的
`software_status=pass_with_environment_gap`、`hardware_status=offline_pending` 与
`operator_visual_acceptance=not_claimed` 保持不变；开发线继续是 `v0.3.0-pre.8`，最终 `v0.3.0` 未发布。
源码 provenance 为 `HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、`dirty_entry_count=293`、
`worktree_clean=false`、`status_sha256=b98a96dd5850dac360714ff836f0818da09338cc382a4bbe3af1f01c3235dbba`。
QA16 复验并保留 GPS_RAW_INT 缺失 `alt`/降级 fix/非法坐标/未知卫星哨兵/未来时间戳的原子
fail-closed 语义，并在失效时恢复先前独立 EKF/LOCAL 高度；SIM `arm_on_start`、Docker 标准路径回退
和 canonical parity 继续有效。这些历史软件/协议证据不升级为真实 Pixhawk、Jetson、室外 GNSS、HIL/REAL、
实飞或人工 Fly View 验收。

QA17 现作为历史 goal acceptance 保留（原始文件和 SHA 不覆盖）：
`artifacts/evaluation/multidetect-goal-acceptance-20260831-final-qa17.json`（SHA-256
`BF93CE4FFD7DEE0CA9D166614DAF8D6216BF0ACC7D09808DDF913B3E11D6FFA2`），脚本退出码 `0`，曾提升为
`artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本；现由 QA22 指向）。核心 pytest 收集
`2000` 项，运行输出为 `1999 passed / 1 skipped`（唯一跳过项仍是该 Windows 账户不支持符号链接），QGC 自定义回归
`64/64`；QGC protocol self-test、QML parse、Ruff、compileall、CRLF-aware diff check、三个 C++
self-test 和 portable headless smoke 均通过。当时 Release/portable 主 EXE 仍为 `46,574,080` bytes，
SHA-256 `E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`。QA17 的
`software_status=pass_with_environment_gap`、`hardware_status=offline_pending` 与
`operator_visual_acceptance=not_claimed` 保持不变；开发线继续是 `v0.3.0-pre.8`，最终 `v0.3.0` 未发布。
源码 provenance 为 `HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、`dirty_entry_count=293`、
`worktree_clean=false`、`status_sha256=b98a96dd5850dac360714ff836f0818da09338cc382a4bbe3af1f01c3235dbba`。
QA17 已验证 GPI/GPS 原子解析与非法 sentinel 回归：无效坐标、未知卫星、缺失高度、降级 fix 或未来时间戳
不会复用旧 raw 新鲜度/坐标，失效时恢复独立 EKF/LOCAL 高度；SIM `arm_on_start`、Docker 标准路径回退
和 canonical parity 继续有效。该历史快照只证明软件/协议门，不升级为真实 Pixhawk、Jetson、室外 GNSS、
HIL/REAL、实飞或人工 Fly View 结论。

QA18 现作为历史 goal acceptance 保留（原始文件和 SHA 不覆盖）：
`artifacts/evaluation/multidetect-goal-acceptance-20260831-final-qa18.json`（SHA-256
`230A2EC5EC44982DE4563AEAEB949E32ACD85123D706C320E056EA7BF3951F7F`），脚本退出码 `0`，曾提升为
`artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本；现由 QA22 指向）。QA18 核心 pytest 收集
`2000` 项，运行输出为 `1999 passed / 1 skipped`（唯一跳过项是该 Windows 账户不支持符号链接）；该数量
来自当次全量运行输出统计，不是 QA18 JSON 的额外字段。QGC 自定义回归 `64/64`；QGC protocol self-test、QML parse、
Ruff、compileall、CRLF-aware diff check、三个 C++ self-test 和 portable headless smoke 均通过。当前
当时 Release/portable 主 EXE 为 `46,574,080` bytes，SHA-256
`E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`。QA18 的
`software_status=pass_with_environment_gap`、`hardware_status=offline_pending` 与
`operator_visual_acceptance=not_claimed` 保持不变；开发线继续是 `v0.3.0-pre.8`，最终 `v0.3.0` 未发布。
源码 provenance 为 `HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、`dirty_entry_count=293`、
`worktree_clean=false`、`status_sha256=b98a96dd5850dac360714ff836f0818da09338cc382a4bbe3af1f01c3235dbba`。
QA18 补充并验证 GPI `GLOBAL_POSITION_INT` 与 GPS 原子解析/非法 sentinel 语义：坐标、卫星数、高度、
fix 和时间戳在同一帧内原子校验；任一非法或缺失字段都会清除对应 raw freshness/坐标，拒绝未来时间戳，
并恢复独立 EKF/LOCAL 高度，live bridge 与此语义一致。SIM `arm_on_start`、Docker 标准路径回退和 canonical
parity 继续有效。该历史快照只证明软件/协议门，不把 Docker/SITL 或静态证据升级为真实 Pixhawk、Jetson、
相机、室外 GNSS、执行器、HIL/REAL、实飞或人工 Fly View 结论。

QA19 现作为历史 goal acceptance 保留（原始文件和 SHA 不覆盖）：
`artifacts/evaluation/multidetect-goal-acceptance-20260831-final-qa19.json`（SHA-256
`082092AB58848D498050901C5BD9768AABF2357569C91D72D531DD105E527515`），脚本退出码 `0`，并已提升为
`artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本；现由 QA22 指向）。QA19 核心 pytest 收集
`2001` 项；QGC 自定义回归 `64/64`；QGC protocol self-test、QML parse、Ruff、compileall、CRLF-aware
diff check、三个 C++ self-test 和 portable headless smoke 均通过。当时 Release/portable 主 EXE 为
`46,574,080` bytes，SHA-256
`E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`。QA19 的
`software_status=pass_with_environment_gap`、`hardware_status=offline_pending` 与
`operator_visual_acceptance=not_claimed` 保持不变；开发线继续是 `v0.3.0-pre.8`，最终 `v0.3.0` 未发布。
源码 provenance 为 `HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、`dirty_entry_count=293`、
`worktree_clean=false`、`status_sha256=b98a96dd5850dac360714ff836f0818da09338cc382a4bbe3af1f01c3235dbba`。
QA19 补充并回归 Pixhawk `GPS_RAW_INT` 的 per-source timestamp cursor 单调性：同一来源的旧/乱序观测不会
回退 freshness、坐标或高度游标；非法 sentinel 与未来时间戳仍原子 fail-closed，独立 EKF/LOCAL 高度恢复和
live bridge 语义保持一致。该历史快照只证明软件/协议门，不把 Docker/SITL 或静态证据升级为真实 Pixhawk、
Jetson、相机、室外 GNSS、执行器、HIL/REAL、实飞或人工 Fly View 结论。

QA20 现作为历史 goal acceptance 保留（原始文件和 SHA 不覆盖）：
`artifacts/evaluation/multidetect-goal-acceptance-20260831-final-qa20.json`（SHA-256
`624B31E03C04D60CD0FF26386C23DC3E6FED19E011375C31DE8578E897ACE756`），脚本退出码 `0`，并已提升为
`artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本；现由 QA22 指向）。QA20 核心 pytest 收集
`2001` 项；QGC 自定义回归 `64/64`；QGC protocol self-test、QML parse、Ruff、compileall、CRLF-aware
`git diff --check`、三个 C++ self-test 和 portable headless smoke 均通过。当前 Release/portable 主 EXE
仍为 `46,574,080` bytes，SHA-256
`E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`。QA20 的
`software_status=pass_with_environment_gap`、`hardware_status=offline_pending` 与
`operator_visual_acceptance=not_claimed` 保持不变；开发线继续是 `v0.3.0-pre.8`，最终 `v0.3.0` 未发布。
源码 provenance 为 `HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、`dirty_entry_count=294`、
`worktree_clean=false`、`status_sha256=3b2fb046735714f18012b659cbd652dc0f84761084980ee3e312ee37f0fc9211`。
QA20 延续并回归 Pixhawk `GPS_RAW_INT` 的 per-source timestamp cursor 单调性：同一来源的旧/乱序观测不会
回退 freshness、坐标或高度游标；GPI/GPS 坐标、卫星数、高度、fix 与时间戳在同一帧内原子校验，非法
sentinel 或未来时间戳清除 raw 状态并恢复独立 EKF/LOCAL 高度，live bridge 与此语义一致。同时修复
`depth_grid_udp` 发送完成后才起算限流窗口的竞态，并以全量回归验证。该当前快照只证明软件/协议门，
不把 Docker/SITL 或静态证据升级为真实 Pixhawk、Jetson、相机、室外 GNSS、执行器、HIL/REAL、实飞或人工
 Fly View 结论。

QA22 是当前 goal acceptance：`artifacts/evaluation/multidetect-goal-acceptance-20260831-final-qa22.json`
（SHA-256 `F38EE2065DCB57A1CA2C12A99AD1F401DC4D48DA0F87848D5B6BBDD2913B7ACF`，核心 pytest 收集 `2012` 项，
QGC 自定义回归 `64/64`，脚本退出码 `0`，latest 与该文件字节一致）。software=`pass_with_environment_gap`、
hardware=`offline_pending`、dirty=`295`、status SHA=`851a1abde3f79abd859ad032242cee0ee9bfd790663f615c3fe73b6b67d97c74`。
回放 GPS 质量要求 vAcc≤10 m 且纬经度与 MAVLink fix/satellite 边界对齐；legacy `observed_at` 缺省、自定义 live policy
与 `position_healthy` 语义差异独立记录，不代表真实硬件通过。

QA15 之前使用当前源码执行的真实 disposable Docker/PX4/Gazebo AI armed-heartbeat 闭环：
`artifacts/evaluation/simulator-docker-ai-acceptance-20260830T153110Z-qa13.json`（SHA-256
`678C233C7164E123B0272CF76CC0831F1D601797F7E66877BC6CE463F4FE619C`），容器日志 SHA-256
`04F57BBF9F7A9FEB7EF5C6990DBAF4187D2AE8B5E3C49E922E3919B568B10EBD`。运行/AI/遥测门、armed
heartbeat、GPS、一个共享 policy step 和清理均通过；这是 machine-only disposable SITL 证据，
不改变真实 Pixhawk、Jetson、室外 GNSS、HIL/REAL、执行器、实飞或人工 Fly View 的 `not_claimed` 状态。

本轮同时收紧 QGC 原生 Fly View 的证据保存边界：map/PiP 对象尚未就绪时不解引用；只有当前
目标具备有限、合法的 VALID/DEGRADED 测距时才保存距离和方位；越界坐标、非法 1σ、过期
source age 或损坏 JSONL 记录均 fail-closed，不会在本地历史中恢复成假目标。

E7 当前诊断报告 `artifacts/evaluation/release-gate-pre.8-report-final-qa8.json`（SHA-256
`2C609808BB8A6B154F92F1F93F4FC91508F5AA88D96DCB496044F49070229566`）明确把该 goal acceptance
识别为非四模式清单并保持 `gate_passed=false`；REPLAY/HIL/REAL evidence、五项 parity、真实
硬件与人工 Fly View 状态仍需独立补齐。QA4 报告（`release-gate-pre.8-report-final-qa4.json`，
SHA-256 `AB16BFEE68B0CD3DA5151402BAA84E3F053C63071B7C6B9CE7A0835AA2B5E2B7`）和更早的
`release-gate-pre.8-report.json` 继续作为历史基线保留。上述报告均是 prerelease 阻塞证据，
不是发布通过证明。

## 剩余实机门

1. 采集固定三体 RGB 相机的 ChArUco 原始帧，生成通过门禁的内参/安装外参 JSON。
2. 断开动力和物理载荷执行器，验证 roll/pitch 舵面方向、绝对限幅、slew、人工取消和失锁恢复进入前模式。
3. 用真实检测完成 DET→TRK→LCK→TGT、撤销和短时脱锁恢复。
4. 完成长时间运行并记录 FPS、P50/P95 延迟、温度、RSS、重连和队列高水位。

机器可读结果写到 `artifacts/evaluation/multidetect-goal-acceptance-latest.json`。
