# MultiDetectGCS 源码

`MultiDetectGCS/` 是统一仓库中的定制 QGroundControl 源码。它包含上游 QGroundControl 基线、Multi-Detect 的 `custom/` 应用层、协议自检和桌面/Android 构建配置。

任务包编辑、地图中心取点、飞前风险／失联策略、机载监督器监控及 TUNNEL v2 接入说明见
[`任务包操作流程`](../docs/gcs-mission-package-workflow.md)。

## 版本与发布

- 产品名：`MultiDetectGCS`
- 版本序列起点：`v0.2.0`
- 当前开发版本：`v0.3.0-pre.8`
- 最终目标版本：`v0.3.0`（尚未发布）
- Windows 主运行入口：`portable-v<version>/bin/MultiDetectGCS.exe` 及同目录完整运行时
- 可选安装器名：`MultiDetectGCS-v<major>.<minor>.<patch>[-pre.<n>]-windows-amd64.exe`
- 构建目录、CPM 缓存、虚拟环境和安装包均被 Git 忽略；只将源码、配置、文档和可重复构建说明纳入版本控制。

版本单一真源是仓库根目录 `configs/product-version.json`。构建脚本把
`release_label=0.3.0-pre.8` 映射到用户可见版本、目录和产物名，把 `numeric=0.3.0` 映射到
CMake/PE 数值版本；Python 使用对应的 PEP 440 版本 `0.3.0rc8`。不要直接修改
`custom/cmake/CustomOverrides.cmake` 制造第二套版本。每次发布需要更新 manifest、根 README、
CHANGELOG 和 `docs/qgc-windows-release.md` 的真实构建证据、哈希与归档文件名。

推荐构建入口：

```powershell
cd D:\Projects\Multi-Detect
& .\scripts\build_multidetect_windows.ps1 `
  -Configuration Release -Parallel 8 -BuildSelfTests -InstallPortable
```

台式机当前的 `v0.3.0-pre.8` portable 主 EXE 位于
`build/windows-v0.3.0-pre.8-release/portable-v0.3.0-pre.8/bin/MultiDetectGCS.exe`，
大小 `46,574,080` bytes，SHA-256
`E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`；Release 与 portable
字节一致。QGC source-age/frame-hold、SIM world-origin、GPS/目标事件回放新鲜度、QGC WGS-84
落图和原生 Fly View 证据快照加固后的当前 goal acceptance（QA22）快照为
`D:\Projects\Multi-Detect\artifacts\evaluation\multidetect-goal-acceptance-20260831-final-qa22.json`
（SHA-256 `F38EE2065DCB57A1CA2C12A99AD1F401DC4D48DA0F87848D5B6BBDD2913B7ACF`，核心测试收集
`2012` 项、QGC `64/64`，software=`pass_with_environment_gap`、hardware=`offline_pending`）。QA11/QA10/QA8/QA7、`final-qa`、`final-qa2`、`final-qa3` 与 `final-regression`
文件作为历史快照保留。map/PiP 尚未就绪时不会解引用；无效坐标、过期 GPS、非法测距/方位
或损坏 JSONL 证据均保持 fail-closed。2026-08-30 的最新完整 software-only PX4/Gazebo/QGC/Mission
machine gate 证据为
`D:\Projects\Multi-Detect\artifacts\evaluation\combined-sim-qgc-20260830T101014Z-56628\combined-evidence.json`
（`gate_passed=true`，309,899 bytes，SHA-256
`8423E667A9C9668EF8FE4B1AE5D4166E41F3E21F423ED2FD766EBBECB925C37D`，600 帧、传感器 helper
`exit=0`、VTOL MC→FW→MC、7 项 INT-only Mission、原生 toolbar 抽屉机器 smoke）。
真实 Pixhawk/Jetson/相机/室外 GNSS、执行器、HIL/REAL、实飞和人工 Fly View 仍保持
`not_claimed`，最终 `v0.3.0` 尚未发布。
历史 QA11 在当时验收启动时使用工作树快照构建（`HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`，
`dirty_entry_count=290`、`worktree_clean=false`、`status_sha256=5e156cf0952025104665e287d9a87099acb2e989b8c0b9d67719c19befc86a1e`）；
artifact 现在显式绑定该 provenance，属于本机可复验 dirty snapshot，不是干净提交发布制品。

历史保留的 `v0.3.0-pre.3` 在目录 `build/windows-v0.3.0-pre.3-release/` 完成
`E3 BUILT`：Ninja Release 图、三个 C++ 协议/wire 自检、install/windeployqt、portable
依赖闭包和 15 秒 offscreen/software 启动冒烟均通过，wrapper 最终退出码为 `0`。
完整运行入口是
`build/windows-v0.3.0-pre.3-release/portable-v0.3.0-pre.3/bin/MultiDetectGCS.exe`；
主 EXE 为 `46,556,672` bytes，PE File/Product version 均为 `0.3.0.0`，SHA-256 为
`5F462F8EDAD3677AE3CE88C0EC1B49CA9ECCA6B0B25A7E504A80B0BC7BBC0FD3`。portable
共有 `1013` 个文件、`529,265,043` bytes，`bin` 有 `233` 个文件；Qt、GStreamer、QML
与 platform plugin 闭包完整。机器可读证据清单是
`build/windows-v0.3.0-pre.3-release/verification/v0.3.0-pre.3-evidence.json` 已写入本轮构建、
测试、QGC E4 和桌面画面证据；pre.2 evidence 继续作为历史归档。
该历史 pre.3 evidence 的 SHA-256 为
`865E321F0F618220181BE45E2F7C7B48E467F47BC88A781DA5BA8BAE4B8F776B`。

同时完整保留 2026-08-25 的 `v0.3.0-pre.1` 历史 prerelease portable：
`build/windows-v0.3.0-pre.1-release/portable-v0.3.0-pre.1/bin/MultiDetectGCS.exe`；构建/部署
退出码 `0`，主 EXE `46,525,440` bytes，SHA-256
`83BBAB5BD4D0C560E07DF1BBE79DE02F7815921DF2833524A598C74087F7349B`，完整依赖闭包通过，
offscreen 启动存活 `15 s` 且 stderr `0 bytes`。v0.2.0 portable 仍作为独立回退基线；
当前 wrapper 不创建单文件安装器，裸 Release EXE 也不是可移植交付物。

## 同 UI、同算法边界

`REPLAY`、`SIM`、`HIL` 和 `REAL` 只允许通过 RuntimeBackend/world adapter 改变时钟、传感器、
动力学和故障源；它们共用 MultiDetectGCS Fly/Plan UI，以及 Detector、Tracker、Ranging、
Geolocation、Planner、Safety 和 Mission 路径。检测后 `ObservationReplayBackend` 继续用于
`FrameObservation` 下游回归；`v0.3.0-pre.3` 的 headless `integrated-replay-qgc` 已使 raw pixels
经 `RawSensorRuntimeBackend -> shared production perception factory -> LiveMissionRunner` 运行完整录制，
当前已扩展为真实 QGC transport/原生 Fly View 的 `E4`；它也不是
Gazebo、HIL 或真实飞行证据。

### v0.3.0-pre.3 QGC E4 验收与原生 UI 复用

真实 QGC 验收使用便携目录中的 `MultiDetectGCS.exe`，不是 mock window 或独立 simulator：

- transport 结果：`D:\Projects\Multi-Detect\build\e4-qgc-20260825-222239\e4.result.json`，
  3/3 RTSP frame、3/3 signed metadata barrier、最大 ACK→metadata skew `0.0 ms`、QGC peer
  `127.0.0.1:14581`、helper exit `0`、EOS/summary 均通过；
- 画面结果：`D:\Projects\Multi-Detect\build\e4-qgc-visual-20260825-224648\`，
  `qgc-flyview-video-initial.png` 与 `qgc-flyview-video-stable.png` 是真实 QGC Fly View 截图；
- 为了避免验收点击 PiP 时触发 video receiver 重启，本次临时 profile 设置
  `QGCQml/MainFlyWindowIsMap=false`，让视频启动即处于 QGC 原生 Fly View 主面板，地图仍作为原生
  PiP。该设置只属于验收 profile，不改变默认产品风格或引入新 UI。

视觉实现严格复用 QGC 的 `FlightDisplayViewVideo`、`PipState`、toolbar、map/PiP、`QGCLabel`、
`QGCButton`、`QGCPalette` 和 `ScreenTools`；Multi-Detect overlay 是同一个 QGC surface 上的
QML layer，REPLAY/SIM/HIL/REAL 不各自维护一套界面。机器流程的 `operator_visual_acceptance`
字段继续为 `not_claimed`，人工截图检查另记在 pre.3 evidence。
历史 pre.3 palette rebuild 的 native-QGC style smoke 为
`D:\Projects\Multi-Detect\build\e4-qgc-visual-20260826-024914\qgc-flyview-video-stable.png`；
截图来自当时的 pre.3 portable binary，但 RTSP helper 提前断开，未计入 E4 transport gate，`operator_visual_acceptance` 仍为 `not_claimed`。

同一 portable QGC 随后通过固定翼 PX4 SITL signed-operator 验收：
`D:\Projects\Multi-Detect\artifacts\evaluation\px4-sitl-qgc-operator-acceptance-20260825T190317Z-5732.json`。
QGC/PX4 均 exit `0`，PX4 heartbeat `5`，signed metadata `37`，tracking `30 @ 20 Hz`，目标池
`3 tracks / 2 pages`，`SELECT_TRK` 与 challenge-bound `APPROVE` 完成；QGC forbidden PX4 messages、
file-mutating FTP、system-time、operator tunnel-to-PX4 和所有任务/参数/飞控/载荷写入均为 `0`。
operator listener/source/PX4/router/SITL 端口 `14669/14666/14670/14667/14668/18570` 全部在收尾后释放，
受保护 `14550` 未改变，真实 V6X/Jetson/G20/GR01 未接入。该链路只接受严格 MAVLink 2 signing，
没有 unsigned fallback；UI 仍是同一个 QGC Fly View layer，不增加独立 simulator 或第二套样式。
此前同一修复后的 acceptance-only 关闭路径连续运行 3 次均 exit `0`、`all_passed=true`、端口释放且无
新 crash dump；此前三次结果以及两次 palette rebuild 均以 `stability_runs` 保存在 pre.3 evidence 中，当前共 5 次；
最新结果为 `20260825T190317Z-5732`，仍 exit `0`、`all_passed=true`、端口释放且无新 crash dump，普通 QGC 关闭路径不受影响。

fixed-wing SIH Mission upload/readback/hash 子门证据为
`D:\Projects\Multi-Detect\artifacts\evaluation\px4-fixed-wing-sitl-auto-mission-acceptance-20260825T170332Z-62660.json`：
3 个 item 的顺序/字段与 canonical SHA-256 均匹配（`hash_match=true`）。它不替代完整
fixed-wing/VTOL Mission、Gazebo、真实 HIL 或 REAL flight 门。

### Gazebo Harmonic sensor 子门（pre.3）

新增 `scripts/run_gazebo_sitl_camera_acceptance.ps1` 后，Windows + Docker Desktop 可以启动
digest 固定的 PX4 Gazebo Harmonic 镜像，以 `gz_x500_mono_cam` 订阅真实 camera、CameraInfo 和
NavSat topic。最近一次 12 帧 capture 证据为
`D:\Projects\Multi-Detect\artifacts\evaluation\gazebo-sitl-camera-acceptance-20260825T193511Z-3908.json`
（SHA-256 `63EC2715ABDC54929DABE6975D1014693E0C69761394F63101B0803367E88C2D`）。这个历史
sensor/camera 结果本身只闭合来源子门；它不把采集器日志冒充成 QGC E4，
`operator_visual_acceptance=not_claimed`。

### Gazebo raw pixels → shared perception → QGC 同帧 transport 子门（pre.3）

随后用 `-PersistFrames` 保留同一批 Gazebo RGB payload，再由
`scripts/create_gazebo_raw_replay_fixture.py` 生成 raw-only recording。桥接器只复制已收到的像素
和 NavSat 只读样本，不读取 Scenario Ground Truth，也不产生飞控/执行器输出；recording 继续经过
shared production perception、`LiveMissionRunner`、RTSP helper 和真实便携版 QGC：

```text
source capture:
  D:\Projects\Multi-Detect\artifacts\evaluation\gazebo-sitl-camera-acceptance-20260825T194926Z-22752.json
  result JSON SHA-256: DB7C0705E6E281745B8A05A33507058D55A31A502D6D893B3DDD2307C8178823
  captured payload SHA-256: 6D677CAD22D8949BA7A8FA0BC081B0740680D118C8BD415F5759589F07847BC5
fixture manifest:
  D:\Projects\Multi-Detect\build\qgc-replay-fixture-gazebo-v0.3.0-pre.3\recording\recording.manifest.json
  SHA-256: 87334899FB51950CE6BF2D82108BC5B7A43267B4538D8900CB0087B8F56421A4
QGC result:
  D:\Projects\Multi-Detect\build\e4-gazebo-qgc-20260826\e4.result.json
  SHA-256: 520B1BD6732E8FB35B1FE876E457A3C95111AB751C338287252E7E7ED3529926
```

真实 QGC transport barrier 为 `12/12` video、`12/12` signed metadata、最大 ACK→metadata 偏差
`0.0 ms`，RTSP helper exit `0`，EOS/summary 均收到。该结果闭合 **Gazebo pixels → shared core →
QGC transport**，但机器 barrier 不替代人工 Fly View 画面检查，`operator_visual_acceptance` 仍为
`not_claimed`；完整 fixed-wing/VTOL SIM、真实 Pixhawk/Jetson HIL、室外 GNSS/REAL 仍是后续门。

版本、运行模式、证据等级和当前边界见：

- [`../README.md`](../README.md)
- [`../docs/autonomy-architecture.md`](../docs/autonomy-architecture.md)
- [`../docs/qgc-windows-release.md`](../docs/qgc-windows-release.md)
- [`../CHANGELOG.md`](../CHANGELOG.md)

## 来源与边界

该目录从原本独立的 `QGroundControl-MultiDetect` 工作树导入；其上游锚点保存在
`MultiDetectGCS/.multidetect-upstream.json`。迁入时没有复制嵌套 `.git`、构建目录、CPM 缓存、
虚拟环境、临时 staging 或旧安装包。原工作树被保留在原位置，仅作为迁移回退副本；之后以本目录
作为 MultiDetectGCS 的唯一开发入口。

`.multidetect-gcs-root` 是 QGC 工具使用的源码根目录标记：QGC 脚本会在这里停止向上查找，
而 Git 元数据仍由外层统一仓库拥有。

### VTOL SIM 车辆子门（pre.3）

`pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_px4_gazebo_vtol_scenario_acceptance.ps1 -ScenarioManifest .\configs\scenarios\river_rescue_vtol_001.scenario.json -EvidencePath .\artifacts\evaluation\px4-vtol-sitl-scenario-acceptance-20260826-final.json -MavlinkPort 18573` 启动真实 digest 固定的 PX4 Gazebo `standard_vtol` disposable 容器，并将同一 vehicle telemetry 送进 bounded evaluator。结果 `artifacts/evaluation/px4-vtol-sitl-scenario-acceptance-20260826-final.json`（SHA-256 `2E82C2453F2D7B28815235E02B06C793366782112E8329C0B679DC1EF01ECF6E`）通过 heartbeat/type 22、GPS fix、ARM、fixed-wing/multicopter transition、DISARM 和 false-geolocation 检查；容器日志 SHA-256 `022EB07077DEB3216966C5BD5C21C4CCDFB8B8942A4EB381AC386D06C7247E88`。

这不是第二个 simulator UI：QGC 仍使用原生 `FlyView`、`FlightDisplayViewVideo`、toolbar/map PiP、`QGCLabel`、`QGCButton` 和 `QGCPalette`。VTOL runner 的 `operator_visual_acceptance` 为 `not_claimed`，相机像素和 QGC transport 继续引用前述 Gazebo raw-pixel 子门；完整 Mission、人工画面、HIL/REAL 仍 pending。

证据清单更新后的 SHA-256：`865E321F0F618220181BE45E2F7C7B48E467F47BC88A781DA5BA8BAE4B8F776B`。

### 固定翼 QGC/PX4 续验（pre.3，2026-08-26）

`artifacts/evaluation/px4-sitl-qgc-operator-acceptance-continue-20260826T090000Z.json`（SHA-256
`8B4271BE2FDEA1A62576EE77392B458774C65440BA9D1985F23AEFEADF18F2F7`）再次通过固定翼 SIH
software gate：QGC/PX4 exit `0`、heartbeat `6`、signed metadata `38`、tracking `30 @ 20 Hz`、
target pool `3/2 pages`、`APPROVE` challenge binding，所有写入、forbidden PX4 message、FTP、
SYSTEM_TIME、operator tunnel-to-PX4 与 payload action 均为 `0`。wrapper 已能在 PATH 缺少
`docker.exe` 时自动回退到 Docker Desktop 标准安装路径。该证据仍不代表真实 Pixhawk/Jetson、
完整 Gazebo 画面或 REAL flight；QGC UI 仍只使用原生 Fly View，`operator_visual_acceptance` 保持
`not_claimed`。
## pre.3 原生 QGC / E4 收口补充（2026-08-26）

MultiDetectGCS 只扩展 QGC 原生 `FlyView` / `PipView` / `FlightDisplayViewVideo`。没有第二个窗口，
没有 simulator-only UI，也没有自绘 FixedWing HUD。PiP 只读 annotations；目标框选和操作只在 full
view 启用。界面继续采用 `QGCPopupDialog`、`QGCSlider`、`QGCToolInsets`、`QGCButton`、
`QGCLabel`、`QGCPalette` 与 `ScreenTools`。

E4 每帧的因果顺序为：

```text
RTSP actual push / frame_accepted
  -> signed RuntimeStatus type 23 send
  -> MultiDetectOperatorController native-state adoption
  -> authenticated matching RuntimeStatusAck type 24
```

Type 24 的含义止于 native-controller post-adoption；它不证明 QML render、人工画面正确或 runtime
policy acceptance。机器字段保持 `operator_visual_acceptance=not_claimed`。

RTSP helper 为每个 media/appsrc 会话分配 monotonic generation，使 QGC 原生 PiP/startVideo 在首帧前
或流中断开后能重新连接。旧 media 的迟到 `unprepared` 不能清掉新 appsrc；同一 wire frame 跨
新旧 generation 重试时只在实际 push 成功后返回一次 `frame_accepted`。超时、push 失败、非法帧
和最终没有客户端仍 fail closed。

`REPLAY` / `SIM` 与后续 `HIL` / `REAL` 共用相同 controller、QML 与核心算法，只替换 runtime
channel、传感器/动力学和时钟。本节不声称 combined 300-frame gate 已通过；完整 REPLAY/SIM、PX4
Mission、HIL、REAL、室外 GNSS 与人工 Fly View 点验尚未全部闭合。

已配置树的 rebuild 命令：

```powershell
cd D:\Projects\Multi-Detect
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_multidetect_windows.ps1 `
  -SkipConfigure -Configuration Release -Parallel 2 `
  -BuildSelfTests -InstallPortable
```

预期 portable 入口为
`D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.3-release\portable-v0.3.0-pre.3\bin\MultiDetectGCS.exe`，
RTSP helper 为同目录的 `MultiDetectReplayRtspPublisher.exe`。本轮重构后的最终大小和 SHA-256 只从
`D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.3-release\verification\v0.3.0-pre.3-evidence.json`
读取；前文 pre.3 精确值视为中间快照，不是最终 portable 身份。
## 历史 pre.3 权威 portable / combined QGC gate（2026-08-26）

本节记录历史 pre.3 的权威 portable / combined gate，取代当时前面的中间 hash 和 pending gate 状态；
当前开发线为 `v0.3.0-pre.8`，当前 portable 见本文顶部：

| 文件 | bytes | SHA-256 |
| --- | ---: | --- |
| `MultiDetectGCS.exe` | `46,561,280` | `4C3584F984592FDDF9114EC9C08EC3C68F86C768BFAD4EB3D0F505A263FD55FE` |
| `MultiDetectReplayRtspPublisher.exe` | `79,872` | `AB3E0043DA4A124270BEE1D5EC75A717A9845CF37CB63A8ADC8A50B4752D206F` |

权威 evidence 是
`D:\Projects\Multi-Detect\artifacts\evaluation\combined-sim-qgc-20260826T013505Z-64000\combined-evidence.json`
（`87,623` bytes，SHA-256
`6374DB67E985BBC003682BFC9F764B416604A9C3566A4A33D0164728D44219AA`，
`gate_passed=true`）。Gazebo/RTSP `300/300`、type 23 `1..300`、native post-adoption type 24
`1..300` 全连续，helper exit `0` 且 EOS/summary 完整。

原生 PiP machine evidence 锁定 PID/HWND `40048 / 0x1C534FC`；
`WindowFromPoint(286,937)` 命中相同窗口和 PID 后通过 `SendInput` 点击。
`QGCQml/MainFlyWindowIsMap` 从 `true(default)` 变为显式 `false`。before/after QSettings UTC mtime
为 `2026-08-26T01:35:38.3155174Z` / `2026-08-26T01:35:40.4183819Z`，SHA-256 为
`BD2E993A77172B0668F6ADBF3DCA9F4F3C0083235F9F1B7258734F111CF9B578` /
`764677D852C516ACE28A070886277565D0282E8D09756B1A6A56EF96401C52B8`。普通 PiP swap 不重启
video receiver，300-frame 链未被切断。

PX4 完成 ARM、fixed-wing transition、multicopter return 与 DISARM；GPS qualified，UDP `14550`
未改变，全部测试端口释放，combined-owned container 停止并删除。

`PrintWindow` 截图只是 machine smoke；GPU-backed surface 可能显示黑面，因此仍为
`operator_visual_acceptance=not_claimed`、`qml_rendering_proved=false`。真实 HIL/REAL、室外硬件、
完整 PX4 Mission 与人工 Fly View 验收仍阻塞最终 `v0.3.0`。
## pre.3 唯一权威 QGC/combined run（2026-08-26T02:08Z）

该历史 pre.3 QGC 为 `46,561,280` bytes、SHA-256
`7A4D8B90F0DF0680536B9A91BEF36177542153F97745049D83D25B13BF52554D`；RTSP helper 为
`79,872` bytes、SHA-256 `AB3E0043DA4A124270BEE1D5EC75A717A9845CF37CB63A8ADC8A50B4752D206F`。
唯一权威 run 是
`D:\Projects\Multi-Detect\artifacts\evaluation\combined-sim-qgc-20260826T020829Z-57624`，其
`combined-evidence.json` 为 `87,530` bytes、SHA-256
`62CFCA2B42EE1A9A69676B91220476609D1505DA46DE36C187E2A85BBA14EB6F`、`gate_passed=true`。
旧 `T013505Z-64000` / `4C3584F9...` 仅保留为历史。

QGC 启动前用 `Remove-Item Env:` 真删除三个 acceptance-only 变量，而不是置空：
`MULTIDETECT_DISABLE_VEHICLE_LINK`、`MULTIDETECT_HIL_UI_AUTO_EXERCISE`、
`MULTIDETECT_HIL_UI_AUTO_EXIT`。证据为 `env_absent=true`、
`hil_ui_auto_exercise_log_count=0`、`hil_ui_auto_exercise_disabled=true`。300-frame stream timeout
动态为 `180 s`；`combined-sim-qgc-20260826T020108Z-52536` 是固定 `100 s` timeout 导致的历史失败重试，不计成功 gate。

该历史 run 的 type 23/type 24 均连续 `1..300`，helper exit `0`、EOS/summary 完整；该历史 run 的 PID/HWND 为
`55056 / 0x18D20DE`，普通 PiP swap 不重启视频。截图语义检查显示 `环境 SIM · HEALTHY`，但
GPU-backed surface 可在 `PrintWindow` 中为黑面，因此仍为
`operator_visual_acceptance=not_claimed`、`qml_rendering_proved=false`。

PX4 transition/return、GPS qualified、UDP `14550` unchanged、端口释放、container 删除通过；最终
`v0.3.0` 仍被真实 HIL/REAL、室外硬件、完整 PX4 Mission 与人工视觉验收阻塞。

## pre.8 Ground Control 集成状态（源码候选）

当前 manifest 为 `0.3.0-pre.8` / Python `0.3.0rc8` / numeric `0.3.0`。pre.8 Windows
Release/portable 已由 root 重建并封存；下文保留的 pre.4 说明仅用于历史上下文，当前 SIM/Mission
基线以本节后面的 pre.8 combined evidence 为准，且不会把它误标成真实硬件或人工视觉验收。pre.8 的室外定位核心已切换到 WGS-84
Vincenty direct geodesic；QGC 画面继续只挂在原生 Fly View surface 内。

### QGC 原生 UI 不可替换

所有运行模式继续进入同一个上游 QGC `FlyView`、`FlyViewVideo`、`PipState` 和
`FlightDisplayViewVideo`。Multi-Detect 只添加 surface 内 overlay，并采用 `QGCPalette`、
`QGCButton`、`QGCLabel`、`QGCSlider`、`QGCPopupDialog`、`QGCToolInsets` 与 `ScreenTools`；
PiP 只读，full video state 才允许目标选择、LCK/TRK 与取消。

禁止 `Window`/`ApplicationWindow`、第二视频 surface、独立 simulator、simulator-only QML、
自绘 FixedWing HUD 或自定义主题。Computer Use、SendInput 和 screenshot helper 只能操作/验证 exact
PID/HWND 的同一个 QGC 窗口，不能创建替代 UI。

### exact-client visible-video evidence

此前 pre.4 combined capture 使用 exact QGC PID/HWND、Per-Monitor V2、physical client rect、one-monitor
bounds、外部遮挡枚举和 25 点 `WindowFromPoint` ownership grid；仅对 exact client rectangle 执行
desktop `CopyFromScreen`，并在完成后恢复 cursor/window/topmost/foreground/DPI 状态。相比 pre.3
`PrintWindow` machine smoke，这条路径可以读取 GPU-backed surface 的真实桌面像素。

`scripts/validate_qgc_desktop_capture.py` 对排除 toolbar/PiP/crosshair/badge/action/depth/status 的
video-only ROI 做双帧 nonblack/nonuniform 检查，并与 RTSP streaming/decoding 和 native type 24
sequence advancement 绑定。temporal diff 仅记录诊断。其声明边界仍是：

```text
transport_bound_visible_video_surface_proved=true  # 仅在真实 gate 通过后
exact_source_frame_identity_proved=false
qml_rendering_proved=false
operator_visual_acceptance=not_claimed
```

### 同一 QGC/PX4 上的 VTOL Mission helper

当前 `px4_mission.py` 只把 runtime geometry 编译为 hash-bound、INT-only 的 PX4 VTOL Mission；
`px4_gazebo_qgc_router.py` 保持 QGC read-only，只给 evaluator 开放 identity/mission/item/command 有界的
协议；`vtol_sitl_mission_acceptance.py` 只附着 combined runner 已拥有的 container/router/PX4，完成
upload、exact readback/hash、AUTO Mission 与 lifecycle 观察。它不创建第二个 simulator、窗口或 UI。

combined wrapper 已有 Mission active-ready 与同一原生 Fly View 截图/evidence 编排入口；当前最新
独立 combined Mission 证据为 pre.8 SIM 记录
`artifacts/evaluation/combined-sim-qgc-20260830T101014Z-56628/combined-evidence.json`
（309,899 bytes，SHA-256
`8423E667A9C9668EF8FE4B1AE5D4166E41F3E21F423ED2FD766EBBECB925C37D`，600 帧、传感器
helper `exit=0`、VTOL Mission 与原生 toolbar machine smoke）。它仍是 software-only 证据；真实
HIL、REAL、室外硬件/GNSS 和人工 Fly View 验收未完成，最终 `v0.3.0` 继续阻塞。
